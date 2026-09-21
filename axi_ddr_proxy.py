"""AXI4 slave that proxies every burst to gem5's DDR.

Replaces ``axi_slave_stub`` in co-simulation mode.  The DUT's AXI master
port is answered exclusively with data coming back from the gem5 memory
system through the co-sim transport, so gem5's DDR stays the single source
of truth for memory contents:

    AR -> broker.read(addr, nbytes)  -> gem5 mem_read  -> DDR -> mem_data -> R beats
    AW+W (last) -> broker.write(...) -> gem5 mem_write -> DDR -> mem_resp -> B

The broker (see ``cosim_runtime.MemBroker``) is injected by the runtime;
reads/writes of different bursts may overlap, each R/B response is driven
in order per channel once its broker call completes.
"""


from __future__ import annotations


from collections import deque
from typing import Deque, Optional, Tuple

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from tb_config import CLK_PERIOD_NS
from tb_signals import read_bit as _bit, read_int as _int

#: AXI data bus width in bytes (256-bit).
AXI_BEAT_BYTES = 32


def _int_to_bytes(value: int, nbytes: int) -> bytes:
    return value.to_bytes(nbytes, "little")


def _bytes_to_int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _strb_bits_to_bytes(wstrb: int, nbytes: int) -> bytes:
    """Expand an AXI WSTRB bit mask (one bit per byte lane) into the
    protocol's byte-granular strobe (0xFF/0x00 per byte)."""
    return bytes(0xFF if (wstrb >> i) & 1 else 0x00 for i in range(nbytes))


class AxiDdrProxyBfm:
    """Pin-level AXI4 slave whose responses come from the MemBroker.

    The broker is owned by the runtime and bound after this BFM starts, so
    it is looked up through ``owner.broker`` on every use.
    """

    def __init__(self, top, clk=None, logger=None, owner=None):
        self.top = top
        self.clk = clk if clk is not None else top.clk
        self.log = logger
        self.owner = owner
        self.aw_queue: Deque[dict] = deque()
        self.ar_queue: Deque[dict] = deque()
        #: (aw, data, strb) bursts waiting for their gem5 DDR round trip.
        self._pending_b: Deque[Tuple[dict, bytes, bytes]] = deque()
        #: (id, resp) pairs ready to be driven on B, oldest first.
        self._b_ready: Deque[Tuple[int, int]] = deque()
        self.write_bursts_done = 0
        self.read_bursts_done = 0
        self.read_bytes = 0
        self.write_bytes = 0

    @property
    def broker(self):
        return getattr(self.owner, "broker", None) if self.owner else None

    # ------------------------------------------------------------------
    def drive_idle(self) -> None:
        top = self.top
        top.awready.value = 0
        top.wready.value = 0
        top.bvalid.value = 0
        top.bid.value = 0
        top.bresp.value = 0
        top.arready.value = 0
        top.rvalid.value = 0
        top.rdata.value = 0
        top.rresp.value = 0
        top.rlast.value = 0
        top.rid.value = 0
        top.rdatachk.value = 0
        top.rpoison.value = 0
        top.rtrace.value = 0
        top.ruser.value = 0

    async def wait_reset_released(self, cycles: int = 4) -> None:
        self.drive_idle()
        stable = 0
        while stable < cycles:
            await FallingEdge(self.clk)
            if _bit(self.top.rstn) == 1:
                stable += 1
            else:
                stable = 0

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------
    async def aw_task(self) -> None:
        self.top.awready.value = 1
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.awvalid) != 1:
                continue
            self.aw_queue.append(
                {
                    "id": _int(self.top.awid) or 0,
                    "addr": _int(self.top.awaddr) or 0,
                    "len": _int(self.top.awlen) or 0,
                    "size": _int(self.top.awsize) or 0,
                }
            )

    async def w_task(self) -> None:
        """Sample W beats into bursts; never blocks on the broker.

        ``wready`` stays high permanently, so beats must be captured every
        cycle even while a previous burst is still waiting for its gem5
        round trip -- sampling and DDR forwarding run in two coroutines
        (this one and :meth:`b_task`).
        """
        self.top.wready.value = 1
        beats: Deque[Tuple[int, int]] = deque()
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.wvalid) != 1:
                continue
            beats.append((_int(self.top.wdata) or 0, _int(self.top.wstrb) or 0))
            if _bit(self.top.wlast) != 1:
                continue

            aw = self.aw_queue.popleft() if self.aw_queue else None
            if aw is None:
                self._warn("W burst with no outstanding AW; dropped")
                beats.clear()
                continue
            nbytes = (aw["len"] + 1) * (1 << aw["size"])
            data = bytearray(nbytes)
            strb = bytearray(nbytes)
            for i, (wdata, wstrb) in enumerate(beats):
                off = i * AXI_BEAT_BYTES
                chunk = min(AXI_BEAT_BYTES, nbytes - off)
                if chunk <= 0:
                    break
                data[off:off + chunk] = _int_to_bytes(wdata, chunk)
                strb[off:off + chunk] = _strb_bits_to_bytes(wstrb, chunk)
            beats.clear()
            if self.log is not None:
                self.log.debug(
                    f"axi proxy: W burst queued id={aw['id']}"
                    f" addr=0x{aw['addr']:x} nbytes={nbytes}"
                )
            self._pending_b.append((aw, bytes(data), bytes(strb)))

    async def b_task(self) -> None:
        """Forward sampled write bursts to gem5 DDR and answer with B."""
        while True:
            if not self._pending_b:
                await FallingEdge(self.clk)
                continue
            aw, data, strb = self._pending_b.popleft()
            resp = 0
            if self.log is not None:
                self.log.debug(f"axi proxy: DDR write begin id={aw['id']}")
            if self.broker is not None:
                resp = await self.broker.write(aw["addr"], data, strb)
            if self.log is not None:
                self.log.debug(f"axi proxy: DDR write done  id={aw['id']}")
            self.write_bursts_done += 1
            self.write_bytes += len(data)
            self._b_ready.append((aw["id"], resp))

    async def b_drive_task(self) -> None:
        while True:
            if not self._b_ready:
                await FallingEdge(self.clk)
                continue
            bid, resp = self._b_ready.popleft()
            self.top.bid.value = bid
            self.top.bresp.value = resp & 0x3
            self.top.bvalid.value = 1
            while True:
                await FallingEdge(self.clk)
                if _bit(self.top.bready) == 1:
                    break
            self.top.bvalid.value = 0

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------
    async def ar_task(self) -> None:
        self.top.arready.value = 1
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.arvalid) != 1:
                continue
            self.ar_queue.append(
                {
                    "id": _int(self.top.arid) or 0,
                    "addr": _int(self.top.araddr) or 0,
                    "len": _int(self.top.arlen) or 0,
                    "size": _int(self.top.arsize) or 0,
                }
            )

    async def r_task(self) -> None:
        while True:
            if not self.ar_queue:
                await FallingEdge(self.clk)
                continue
            ar = self.ar_queue.popleft()
            nbytes = (ar["len"] + 1) * (1 << ar["size"])
            data, resp = b"\x00" * nbytes, 0
            if self.broker is not None:
                data, resp = await self.broker.read(ar["addr"], nbytes)
            self.read_bursts_done += 1
            self.read_bytes += nbytes

            for beat in range(ar["len"] + 1):
                off = beat * AXI_BEAT_BYTES
                chunk = min(AXI_BEAT_BYTES, nbytes - off)
                beat_data = data[off:off + chunk].ljust(AXI_BEAT_BYTES, b"\x00")
                self.top.rid.value = ar["id"]
                self.top.rdata.value = _bytes_to_int(beat_data)
                self.top.rresp.value = resp & 0x3
                self.top.rlast.value = 1 if beat == ar["len"] else 0
                self.top.rvalid.value = 1
                while True:
                    await FallingEdge(self.clk)
                    if _bit(self.top.rready) == 1:
                        break
                self.top.rvalid.value = 0

    def _warn(self, msg: str) -> None:
        if self.log is not None:
            self.log.warning(msg)


class AxiDdrProxy(pyuvm.uvm_component):
    """UVM wrapper around :class:`AxiDdrProxyBfm`.

    ``broker`` is injected by the runtime after build (it owns the
    transport); the BFM tolerates a missing broker only until the first
    burst, so the runtime must bind it before stimulus.
    """

    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.bfm: Optional[AxiDdrProxyBfm] = None
        self.broker = None

    async def run_phase(self):
        top = cocotb.top
        self.bfm = AxiDdrProxyBfm(top, top.clk, logger=self.logger,
                                  owner=self)
        await self.bfm.wait_reset_released()
        cocotb.start_soon(self.bfm.aw_task())
        cocotb.start_soon(self.bfm.w_task())
        cocotb.start_soon(self.bfm.b_task())
        cocotb.start_soon(self.bfm.b_drive_task())
        cocotb.start_soon(self.bfm.ar_task())
        cocotb.start_soon(self.bfm.r_task())

    def final_phase(self):
        if self.bfm is None:
            return
        self.logger.info(
            f"axi ddr proxy: write_bursts={self.bfm.write_bursts_done}"
            f" ({self.bfm.write_bytes}B) read_bursts={self.bfm.read_bursts_done}"
            f" ({self.bfm.read_bytes}B) (period={CLK_PERIOD_NS}ns)"
        )
