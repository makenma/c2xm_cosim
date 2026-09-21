"""pyuvm tests for the C2XM CHI-to-AXI bridge.

``COCOTB_TEST_MODULES`` points here; each :func:`pyuvm.test` decorated class is
one cocotb test.  All tests share the same simulation, so every test starts
with a full reset (``rstn`` low for ``RESET_HOLD_NS``) to put the DUT and the
link-layer state machines back to a known state.

There is no scoreboard yet: the tests assert on the flit/beat counters
published by ``chi_mon`` / ``axi_mon`` and print what they saw.  Replace those
assertions with real checking once a reference model exists.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
import pyuvm

from c2xm_env import C2xmEnv
from chi_flit import make_read_no_snp, make_write_data, make_write_no_snp
from tb_config import (
    CLK_PERIOD_NS,
    C2XM_NODEID,
    DEFAULT_QOS,
    DRAIN_NS,
    GEM5_SRCID,
    LINK_UP_TIMEOUT_NS,
    RESET_HOLD_NS,
)
from tb_signals import read_bit as _bit

#: Response opcodes used by the DUT (issue_initial_write_response_operation.sv).
#: Read completions arrive on TXDAT, not TXRSP.
OPC_COMPDBIDRESP = 0x5
OPC_DBIDRESP = 0x6
OPC_COMP = 0x4

#: 64-byte transfer size for the smoke stimulus.
SIZE_64B = 6


class C2xmTestBase(pyuvm.uvm_test):
    """Clock, reset and polling helpers shared by every C2XM test."""

    def build_phase(self):
        self.env = C2xmEnv("env", self)

    async def start_clock(self):
        cocotb.start_soon(
            Clock(cocotb.top.clk, CLK_PERIOD_NS, unit="ns").start()
        )
        # Let the clock run a couple of cycles before touching reset.
        for _ in range(2):
            await FallingEdge(cocotb.top.clk)

    async def reset_dut(self):
        """Assert reset for RESET_HOLD_NS, then release it."""
        top = cocotb.top
        top.rstn.value = 0
        for _ in range(max(2, int(RESET_HOLD_NS / CLK_PERIOD_NS))):
            await FallingEdge(top.clk)
        top.rstn.value = 1
        for _ in range(4):
            await FallingEdge(top.clk)

    async def wait_for(self, cond, timeout_ns: float, what: str) -> bool:
        """Poll ``cond`` every cycle until true or ``timeout_ns`` elapses."""
        waited = 0.0
        while waited < timeout_ns:
            await FallingEdge(cocotb.top.clk)
            if cond():
                return True
            waited += CLK_PERIOD_NS
        self.logger.error(f"timeout after {timeout_ns}ns waiting for {what}")
        return False

    async def wait_link_up(self) -> bool:
        top = cocotb.top
        return await self.wait_for(
            lambda: _bit(top.dbg_chi_rx_run) == 1 and _bit(top.dbg_chi_tx_run) == 1,
            LINK_UP_TIMEOUT_NS,
            "CHI link activation",
        )

    async def drain(self, ns: float = DRAIN_NS) -> None:
        for _ in range(max(1, int(ns / CLK_PERIOD_NS))):
            await FallingEdge(cocotb.top.clk)

    @property
    def txrsp_opcodes(self):
        bfm = self.env.driver.bfm
        return [] if bfm is None else [e["opcode"] for e in bfm.txrsp_history]

    def _log_summary(self, title: str):
        self.logger.info(
            f"{title}: chi={self.env.chi_counts} axi={self.env.axi_counts}"
            f" txrsp_opcodes={[hex(o) for o in self.txrsp_opcodes]}"
        )


@pyuvm.test()
class C2xmSmokeTest(C2xmTestBase):
    """Reset and link activation only: proves the env elaborates and connects."""

    async def run_phase(self):
        self.raise_objection("smoke")
        await self.start_clock()
        await self.reset_dut()

        assert await self.wait_link_up(), "CHI link never activated"
        # Nothing was requested yet, so no traffic may appear.
        await self.drain()
        assert self.env.chi_counts["txrsp"] == 0, "unexpected TXRSP before any request"
        assert self.env.chi_counts["txdat"] == 0, "unexpected TXDAT before any request"
        self._log_summary("smoke")
        self.drop_objection("smoke")


@pyuvm.test()
class C2xmReadNoSnpTest(C2xmTestBase):
    """ReadNoSnp in, AXI AR out, CompData back on the CHI TXDAT channel."""

    async def run_phase(self):
        self.raise_objection("read")
        await self.start_clock()
        await self.reset_dut()
        assert await self.wait_link_up(), "CHI link never activated"

        txn_id = 1
        addr = 0x0000_0000_1000
        flit = make_read_no_snp(
            txnid=txn_id,
            addr=addr,
            srcid=GEM5_SRCID,
            tgtid=C2XM_NODEID,
            size=SIZE_64B,
            qos=DEFAULT_QOS,
            memattr=0,
        )
        self.logger.info(f"sending {flit!r}")
        await self.env.send_flit(flit)

        got_ar = await self.wait_for(
            lambda: self.env.axi_counts["ar"] >= 1, 4000.0, "AXI AR beat"
        )
        got_data = await self.wait_for(
            lambda: self.env.chi_counts["txdat"] >= 1, 4000.0, "CHI CompData"
        )
        self._log_summary("read")

        assert got_ar, "no AXI read address was issued"
        assert got_data, "no CHI CompData was returned"
        assert self.env.chi_counts["rxreq"] == 1, "RXREQ flit was not monitored"
        self.drop_objection("read")


@pyuvm.test()
class C2xmWriteNoSnpTest(C2xmTestBase):
    """WriteNoSnp in: DBIDResp, write data, AXI AW/W/B, then CHI Comp.

    The transaction id matches the pool slot the request will be allocated to
    (empty pool -> slot 0).  See the ``TXN_ID_MODULO`` note in chi_txn.py: the
    DUT indexes its transaction pool and log buffer with ``txn_id[4:0]`` while
    allocating the lowest free slot, so write data is only accepted when those
    two agree.
    """

    async def run_phase(self):
        self.raise_objection("write")
        await self.start_clock()
        await self.reset_dut()
        assert await self.wait_link_up(), "CHI link never activated"

        txn_id = 0  # == pool slot 0 while the pool is empty
        addr = 0x0000_0000_2000
        req = make_write_no_snp(
            txnid=txn_id,
            addr=addr,
            srcid=GEM5_SRCID,
            tgtid=C2XM_NODEID,
            size=SIZE_64B,
            qos=DEFAULT_QOS,
            memattr=0,  # memattr_ewa=0 -> DBIDResp, then data, then Comp
        )
        self.logger.info(f"sending {req!r}")
        await self.env.send_flit(req)

        got_dbid = await self.wait_for(
            lambda: OPC_DBIDRESP in self.txrsp_opcodes
            or OPC_COMPDBIDRESP in self.txrsp_opcodes,
            4000.0,
            "DBIDResp",
        )
        assert got_dbid, "DUT never returned DBIDResp for the write"

        # A full 64-byte line is delivered as two 32-byte DAT beats: data_id 0
        # covers the low half (0b0011) and data_id 2 the high half (0b1100).
        for index, data_id in enumerate((0, 2)):
            beat = make_write_data(
                txnid=txn_id,
                data=(0x1000 * (index + 1)) | (index & 0xFF),
                srcid=GEM5_SRCID,
                tgtid=C2XM_NODEID,
                data_id=data_id,
                byte_enable=(1 << 32) - 1,
            )
            await self.env.send_flit(beat)

        got_aw = await self.wait_for(
            lambda: self.env.axi_counts["aw"] >= 1, 4000.0, "AXI AW beat"
        )
        got_w = await self.wait_for(
            lambda: self.env.axi_counts["w"] >= 2, 4000.0, "two AXI W beats"
        )
        got_b = await self.wait_for(
            lambda: self.env.axi_counts["b"] >= 1, 4000.0, "AXI B response"
        )
        got_comp = await self.wait_for(
            lambda: OPC_COMP in self.txrsp_opcodes, 4000.0, "CHI Comp"
        )
        self._log_summary("write")

        assert got_aw, "no AXI write address was issued"
        assert got_w, "AXI write data beats did not complete"
        assert got_b, "AXI slave stub never responded with B"
        assert got_comp, "DUT never sent the write completion"
        assert self.env.chi_counts["rxdat"] == 2, "RXDAT beats were not monitored"
        self.drop_objection("write")


@pyuvm.test()
class C2xmCosimTest(C2xmTestBase):
    """Full co-simulation: a gem5-side peer drives the CHI link through the
    socket transport, the DUT converts CHI to AXI, and the AXI side is
    served from that peer's memory system (gem5 DDR / fake_gem5's DDR).

    The peer (fake_gem5.py or the real gem5 ChiCosimBridge) owns the
    barrier: this test starts the clock free-running for reset and link
    training, then hands the clock to the runtime, which advances exactly
    ``quantum_cycles`` DUT cycles per received ``sync`` and answers ``ack``.
    The test ends when the peer sends ``bye`` (workload exit).

    Environment knobs: C2XM_COSIM_SOCK (socket path), C2XM_COSIM_CLK_NS
    (DUT clock, default matches gem5 1:1), C2XM_COSIM_QUANTUM,
    C2XM_COSIM_FREE_RUN=1 to keep the clock free-running (debug).
    """

    def build_phase(self):
        self.env = C2xmEnv("env", self)
        self.env.cosim_mode = True

    async def run_phase(self):
        import os

        from cocotb.triggers import Timer

        from cosim_runtime import CosimRuntime

        if not os.environ.get("C2XM_COSIM_SOCK"):
            self.logger.info(
                "cosim: C2XM_COSIM_SOCK not set; no peer to talk to,"
                " test body skipped (run via 'make cosim[-fake]')"
            )
            return

        self.raise_objection("cosim")
        top = cocotb.top

        clk_ns = float(os.environ.get("C2XM_COSIM_CLK_NS", "0.556"))
        clock = Clock(top.clk, clk_ns, unit="ns")
        cocotb.start_soon(clock.start())
        for _ in range(2):
            await FallingEdge(top.clk)
        await self.reset_dut()
        assert await self.wait_link_up(), "CHI link never activated"

        runtime = CosimRuntime(self.env)
        self.env.runtime = runtime
        cocotb.start_soon(runtime.dispatcher())
        await runtime.handshake(clock)
        await runtime.barrier_loop()

        assert runtime.transport.peer_closed or runtime.done, (
            "co-sim ended without a bye from the peer"
        )
        self.logger.info(
            f"cosim test summary: acks={runtime.syncs_acked}"
            f" cycles={runtime.cycles_run} bypass={runtime.bypass_served}"
            f" reason='{runtime.exit_reason}'"
            f" chi={self.env.chi_counts} axi={self.env.axi_counts}"
        )
        self.logger.info(f"cosim {runtime.run_report()}")
        self.logger.info(f"cosim {self.env.snf_agent.latency_report()}")
        # gem5 exits on m5_exit / max-insts by simply closing the socket
        # (it never sends bye), so a late peer_closed after real progress
        # is a pass; check the gem5 log for the actual exit cause.
        if runtime.exit_reason == "gem5 peer closed":
            assert runtime.syncs_acked >= 100, (
                "gem5 side closed the connection before any real progress"
                f" (only {runtime.syncs_acked} barriers acked)"
            )
        assert not runtime.exit_reason.startswith("contract"), (
            f"peer reported a contract violation: {runtime.exit_reason}"
        )
        self.drop_objection("cosim")
