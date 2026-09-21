"""Placeholder AXI slave so the C2XM AXI master port does not stall.

NOT one of the requested components - delete this file (and its instantiation
in ``c2xm_env.py``) once a real AXI agent/slave model exists.  It exists only
so the skeleton can be exercised end to end: C2XM issues AW/W/AR and waits for
B/R, so without a responder the first transaction blocks forever and the CHI
side never produces a response.

Behaviour (deliberately naive):

* ``awready``/``wready``/``arready`` are held high,
* collects a whole write burst and answers with a single OKAY ``b`` using the
  oldest outstanding AW id,
* answers every read burst with ``len + 1`` beats of a deterministic pattern
  (``addr + 32*beat``), ``rlast`` on the final beat, OKAY responses.

Replace with a memory model + AXI assertions when real checking starts.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from tb_signals import read_bit as _bit, read_int as _int

from tb_config import CLK_PERIOD_NS

#: Give the DUT a little time after reset before driving ready signals.
AXI_IDLE_CYCLES_AFTER_RESET = 4
#: 256-bit data bus.
DATA_MASK = (1 << 256) - 1




class AxiSlaveBfm:
    """Pin-level placeholder AXI4 slave."""

    def __init__(self, top, clk=None, logger=None):
        self.top = top
        self.clk = clk if clk is not None else top.clk
        self.log = logger
        self.aw_queue: Deque[dict] = deque()
        self.ar_queue: Deque[dict] = deque()
        #: Write bursts waiting for a B response (oldest first).
        self._pending_b: Deque[int] = deque()
        self.write_bursts_done = 0
        self.read_bursts_done = 0

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

    async def wait_reset_released(self, cycles: int = AXI_IDLE_CYCLES_AFTER_RESET) -> None:
        self.drive_idle()
        stable = 0
        while stable < cycles:
            await FallingEdge(self.clk)
            if _bit(self.top.rstn) == 1:
                stable += 1
            else:
                stable = 0

    # ------------------------------------------------------------------
    # tasks
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
                }
            )

    async def w_task(self) -> None:
        """Accept data beats; count the burst and pair it with the oldest AW."""
        self.top.wready.value = 1
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.wvalid) != 1:
                continue
            if _bit(self.top.wlast) == 1:
                aw = self.aw_queue.popleft() if self.aw_queue else {"id": 0}
                self.write_bursts_done += 1
                self._pending_b.append(aw["id"])

    async def b_task(self) -> None:
        while True:
            if not self._pending_b:
                await FallingEdge(self.clk)
                continue
            bid = self._pending_b.popleft()
            self.top.bid.value = bid
            self.top.bresp.value = 0b00  # OKAY
            self.top.bvalid.value = 1
            while True:
                await FallingEdge(self.clk)
                if _bit(self.top.bready) == 1:
                    break
            self.top.bvalid.value = 0

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
                }
            )

    async def r_task(self) -> None:
        while True:
            if not self.ar_queue:
                await FallingEdge(self.clk)
                continue
            ar = self.ar_queue.popleft()
            nbeats = ar["len"] + 1
            for beat in range(nbeats):
                self.top.rid.value = ar["id"]
                self.top.rdata.value = (ar["addr"] + 32 * beat) & DATA_MASK
                self.top.rresp.value = 0b00  # OKAY
                self.top.rlast.value = 1 if beat == nbeats - 1 else 0
                self.top.rvalid.value = 1
                while True:
                    await FallingEdge(self.clk)
                    if _bit(self.top.rready) == 1:
                        break
                self.top.rvalid.value = 0
            self.read_bursts_done += 1


class AxiSlaveStub(pyuvm.uvm_component):
    """UVM wrapper around :class:`AxiSlaveBfm` (placeholder)."""

    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.bfm: Optional[AxiSlaveBfm] = None

    async def run_phase(self):
        top = cocotb.top
        self.bfm = AxiSlaveBfm(top, top.clk, logger=self.logger)
        await self.bfm.wait_reset_released()
        cocotb.start_soon(self.bfm.aw_task())
        cocotb.start_soon(self.bfm.w_task())
        cocotb.start_soon(self.bfm.b_task())
        cocotb.start_soon(self.bfm.ar_task())
        cocotb.start_soon(self.bfm.r_task())

    def final_phase(self):
        if self.bfm is None:
            return
        self.logger.info(
            f"axi slave stub: write_bursts={self.bfm.write_bursts_done}"
            f" read_bursts={self.bfm.read_bursts_done}"
            f" (period={CLK_PERIOD_NS}ns)"
        )
