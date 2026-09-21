"""Co-simulation runtime: barrier loop, gated clock and message dispatch.

Ownership map (TB side):

    transport (threads) --rx_queue--> dispatcher coroutine --route--> consumers
        flit g2t req/dat -> snf_agent.handle_gem5_req/dat
        mem_data/mem_resp -> MemBroker.deliver
        bypass_read/write -> bypass proxy tasks (via MemBroker)
        sync -> barrier wakeup          bye/peer_closed -> shutdown

    barrier loop (this module)
        After the hello handshake it takes the DUT clock over from the
        free-running cocotb ``Clock`` and gates it: for every ``sync``
        received from gem5 it advances exactly ``quantum_cycles`` full
        clock cycles, then answers ``ack``.  While waiting for the next
        ``sync`` the clock is parked low, so TB cycles and gem5 cycles
        advance 1:1 -- the two simulators share one runtime.

    MemBroker
        ``read(addr, len)`` / ``write(addr, data, strb)`` coroutines used
        by the AXI DDR proxy and the bypass proxy; each call is one
        ``mem_read``/``mem_write`` message answered by ``mem_data``/
        ``mem_resp``.
"""


from __future__ import annotations


import os
import time
from typing import Dict, Optional, Tuple

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, Timer
from cocotb.utils import get_sim_time

from cosim_protocol import PROTOCOL_VERSION
from cosim_transport import CosimTransport

#: How long (sim-time ns) the dispatcher may poll without any message
#: before the co-sim is declared wedged (barrier starvation or dead peer).
PARK_TIMEOUT_NS = 20_000_000.0
#: Dispatcher poll period while the clock is parked.
PARK_POLL_NS = 20.0


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


class MemBroker:
    """Issue gem5 memory accesses and match their completions by id."""

    def __init__(self, transport: CosimTransport, clk, logger=None):
        self.transport = transport
        self.clk = clk
        self.log = logger
        self._next_id = 1
        self._pending: Dict[int, Optional[Dict]] = {}
        self.reads_issued = 0
        self.writes_issued = 0

    def _alloc_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def read(self, addr: int, length: int,
                   timeout_cycles: int = 2_000_000) -> Tuple[bytes, int]:
        rid = self._alloc_id()
        self._pending[rid] = None
        self.reads_issued += 1
        if self.log:
            self.log.debug(f"broker: read id={rid} addr=0x{addr:x} len={length}")
        self.transport.send(
            {"t": "mem_read", "id": rid, "addr": addr, "len": length}
        )
        cycles = 0
        while self._pending[rid] is None:
            await FallingEdge(self.clk)
            cycles += 1
            if cycles > timeout_cycles:
                raise TimeoutError(
                    f"gem5 mem_read id={rid} addr=0x{addr:x} never completed"
                )
        msg = self._pending.pop(rid)
        return bytes.fromhex(msg.get("data", "")), int(msg.get("resp", 0))

    async def write(self, addr: int, data: bytes, strb: bytes = b"",
                    timeout_cycles: int = 2_000_000) -> int:
        rid = self._alloc_id()
        self._pending[rid] = None
        self.writes_issued += 1
        if self.log:
            self.log.debug(f"broker: write id={rid} addr=0x{addr:x} len={len(data)}")
        self.transport.send({
            "t": "mem_write", "id": rid, "addr": addr,
            "data": data.hex(), "strb": strb.hex(),
        })
        cycles = 0
        while self._pending[rid] is None:
            await FallingEdge(self.clk)
            cycles += 1
            if cycles > timeout_cycles:
                raise TimeoutError(
                    f"gem5 mem_write id={rid} addr=0x{addr:x} never completed"
                )
        msg = self._pending.pop(rid)
        return int(msg.get("resp", 0))

    # Called by the dispatcher (any coroutine context).
    def deliver(self, msg: Dict) -> bool:
        rid = msg.get("id")
        if self.log:
            self.log.debug(f"broker: deliver {msg.get('t')} id={rid}")
        if rid in self._pending:
            self._pending[rid] = msg
            return True
        if self.log is not None:
            self.log.warning(f"broker: stray completion for id {rid}")
        return False


class CosimRuntime:
    """Barrier-locked co-simulation runtime for one test."""

    def __init__(self, env, logger=None):
        self.env = env  # C2xmEnv in cosim mode
        self.log = logger or env.logger
        self.top = cocotb.top
        self.transport = CosimTransport(logger=self.log)
        self.broker = MemBroker(self.transport, self.top.clk,
                                logger=self.log)
        #: Filled from the hello handshake.
        self.quantum_cycles = env_int("C2XM_COSIM_QUANTUM", 100)
        self.gem5_clk_ns = env_float("C2XM_COSIM_GEM5_CLK_NS", 0.556)
        self.clk_period_ns = env_float("C2XM_COSIM_CLK_NS", 0.556)
        self.snf_id = env_int("C2XM_COSIM_SNF_ID", 0x80)
        self.hnf_id = env_int("C2XM_COSIM_HNF_ID", 0x90)
        self.free_run = env_int("C2XM_COSIM_FREE_RUN", 0) == 1
        #: Optional barrier budget (0 = run until the peer exits); used to
        #: bound demo/waveform runs because gem5-side --max-insts kills
        #: this workload in the XS tree.
        self.max_syncs = env_int("C2XM_COSIM_MAX_SYNCS", 0)
        #: Runtime state.
        self.sync_cycle = -1
        self.sync_seen = False
        self.done = False
        self.exit_reason = ""
        self.syncs_acked = 0
        self.cycles_run = 0
        #: Whole-run duration (handshake -> finish), wall and DUT cycles.
        self.wall_start = None
        self.bypass_served = 0
        self._clock: Optional[Clock] = None

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    async def handshake(self, clock: Clock) -> None:
        """Connect to gem5, exchange hello, take over the gated clock.

        ``clock`` is the free-running Clock the test started before link
        training; it is stopped here (gated mode) so cycle counts on both
        sides advance in lockstep.
        """
        self._clock = clock
        path = os.environ.get("C2XM_COSIM_SOCK", "/tmp/c2xm_cosim.sock")
        self.transport.connect(path)
        hello = self._await_message("hello")
        self.quantum_cycles = int(hello.get("quantum_cycles",
                                            self.quantum_cycles))
        self.gem5_clk_ns = float(hello.get("clk_period_ns", self.gem5_clk_ns))
        self.snf_id = int(hello.get("snf_id", self.snf_id))
        self.hnf_id = int(hello.get("hnf_id", self.hnf_id))

        agent = self.env.snf_agent
        agent.snf_id = self.snf_id
        agent.dut_nodeid = 0
        agent.send_cb = self.transport.send
        self.env.axi_slave.broker = self.broker

        self.transport.send({
            "t": "hello_ack", "ver": PROTOCOL_VERSION,
            "clk_period_ns": self.clk_period_ns,
        })
        self.log.info(
            f"cosim: hello ok quantum={self.quantum_cycles}cyc"
            f" gem5_clk={self.gem5_clk_ns}ns snf=0x{self.snf_id:x}"
            f" hnf=0x{self.hnf_id:x}"
        )
        self.wall_start = time.monotonic()
        if not self.free_run:
            clock.stop()
            await Timer(self.clk_period_ns / 2, "ns")
            self.top.clk.value = 0

    def _await_message(self, kind: str) -> Dict:
        import queue as _q
        while True:
            try:
                msg = self.transport.rx_queue.get(timeout=30.0)
            except _q.Empty:
                raise TimeoutError(f"gem5 never sent '{kind}'")
            if msg.get("t") == kind:
                return msg
            # Dispatch anything that arrived early.
            self._route(msg)

    # ------------------------------------------------------------------
    # dispatcher
    # ------------------------------------------------------------------
    async def dispatcher(self) -> None:
        """Route transport messages to consumers; runs for the whole test."""
        while not self.done:
            msg = self.transport.poll()
            if msg is None:
                await Timer(PARK_POLL_NS, "ns")
                continue
            self._route(msg)

    def _route(self, msg: Dict) -> None:
        kind = msg.get("t")
        if kind == "flit":
            ch, direction = msg.get("ch"), msg.get("dir", "g2t")
            agent = self.env.snf_agent
            if direction == "g2t" and ch == "req":
                agent.handle_gem5_req(msg.get("flit", {}))
            elif direction == "g2t" and ch == "dat":
                agent.handle_gem5_dat(msg.get("flit", {}))
            else:
                self.log.warning(f"cosim: unexpected flit {ch}/{direction}")
        elif kind in ("mem_data", "mem_resp"):
            self.broker.deliver(msg)
        elif kind == "bypass_read":
            cocotb.start_soon(self._bypass_read(msg))
        elif kind == "bypass_write":
            cocotb.start_soon(self._bypass_write(msg))
        elif kind == "sync":
            self.sync_cycle = int(msg.get("cycle", 0))
            self.sync_seen = True
        elif kind == "bye":
            self.done = True
            self.exit_reason = str(msg.get("reason", "gem5 exit"))
            self.sync_seen = True  # wake the barrier loop if parked
        elif kind == "peer_closed":
            self.done = True
            self.exit_reason = "gem5 peer closed"
            self.sync_seen = True
        else:
            self.log.warning(f"cosim: unknown message type {kind!r}")

    async def _bypass_read(self, msg: Dict) -> None:
        pkt = msg["pkt"]
        data, resp = await self.broker.read(msg["addr"], msg["len"])
        self.bypass_served += 1
        self.transport.send({
            "t": "bypass_resp", "pkt": pkt, "data": data.hex(),
            "resp": resp,
        })

    async def _bypass_write(self, msg: Dict) -> None:
        pkt = msg["pkt"]
        data = bytes.fromhex(msg.get("data", ""))
        strb = bytes.fromhex(msg.get("strb", "") or "ff" * len(data))
        resp = await self.broker.write(msg["addr"], data, strb)
        self.bypass_served += 1
        self.transport.send({
            "t": "bypass_resp", "pkt": pkt, "data": "", "resp": resp,
        })

    # ------------------------------------------------------------------
    # barrier loop (owns the gated clock)
    # ------------------------------------------------------------------
    async def barrier_loop(self) -> None:
        clk = self.top.clk
        half = self.clk_period_ns / 2
        agent = self.env.snf_agent
        # self.sync_seen is NOT reset here: the first sync may already have
        # been routed by the dispatcher while the handshake was running
        # (gem5 sends outbox + sync immediately after hello).
        while not self.done:
            # Park (clock low in gated mode) until the next sync / bye.
            parked = 0.0
            while not self.sync_seen and not self.done:
                await Timer(PARK_POLL_NS, "ns")
                parked += PARK_POLL_NS
                if parked > PARK_TIMEOUT_NS:
                    raise TimeoutError(
                        f"no sync from gem5 for {parked / 1000:.0f}us"
                        f" (last cycle={self.sync_cycle})"
                    )
            self.sync_seen = False
            if self.done:
                break

            # Advance exactly one quantum of DUT cycles.
            if self.free_run:
                for _ in range(self.quantum_cycles):
                    await FallingEdge(clk)
                    agent.on_cycle()
                    self.cycles_run += 1
            else:
                for _ in range(self.quantum_cycles):
                    clk.value = 1
                    await Timer(half, "ns")
                    clk.value = 0
                    await Timer(half, "ns")
                    agent.on_cycle()
                    self.cycles_run += 1

            self.transport.send({"t": "ack", "cycle": self.sync_cycle})
            self.syncs_acked += 1
            if self.max_syncs and self.syncs_acked >= self.max_syncs:
                self.exit_reason = (
                    f"tb barrier budget reached ({self.syncs_acked})")
                self.done = True
                break
            if self.syncs_acked % 500 == 0:
                self.log.info(
                    f"cosim: alive acks={self.syncs_acked}"
                    f" cycles={self.cycles_run}"
                    f" broker r/w={self.broker.reads_issued}/"
                    f"{self.broker.writes_issued}"
                )

        self._finish()

    def run_report(self) -> str:
        wall = (time.monotonic() - self.wall_start) if self.wall_start else 0.0
        sim_ns = self.cycles_run * self.clk_period_ns
        return (
            f"program runtime: wall={wall:.1f}s"
            f" dut_cycles={self.cycles_run} ({sim_ns / 1000:.3f}us sim,"
            f" clk={self.clk_period_ns}ns) barriers={self.syncs_acked}"
        )

    def _finish(self) -> None:
        reason = self.exit_reason or "tb done"
        self.log.info(f"cosim: {self.run_report()}")
        self.log.info(
            f"cosim: finished acks={self.syncs_acked}"
            f" cycles={self.cycles_run} bypass={self.bypass_served}"
            f" reason='{reason}'"
        )
        self.transport.close(reason=reason)

    # ------------------------------------------------------------------
    # helpers for tests
    # ------------------------------------------------------------------
    def idle(self) -> bool:
        """True when every tracked transaction has completed."""
        return self.env.snf_agent.mapper.in_flight == 0
