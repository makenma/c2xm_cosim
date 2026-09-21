"""AXI monitor.

Passively observes the AXI master port of C2XM and republishes transactions on
per-channel analysis ports:

    aw_ap -> AxiAwTxn
    w_ap  -> AxiWTxn    (one transaction per data beat)
    b_ap  -> AxiBTxn
    ar_ap -> AxiArTxn
    r_ap  -> AxiRTxn    (one transaction per data beat)

Handshakes are sampled on falling clock edges: a channel is reported when
``valid && ready`` is true mid cycle, i.e. the beat completes on the next
rising edge.

Skeleton status: no burst assembly, no AXI protocol assertions and no
outstanding-transaction tracking; each beat is published as it completes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from tb_signals import read_bit as _bit, read_int as _int




class AxiResp:
    """AXI response codes (RRESP/BRESP)."""

    OKAY = 0b00
    EXOKAY = 0b01
    SLVERR = 0b10
    DECERR = 0b11


@dataclass
class AxiAwTxn:
    """Write address beat."""

    id: int = 0
    addr: int = 0
    len: int = 0
    size: int = 0
    burst: int = 0
    qos: int = 0
    cache: int = 0
    prot: int = 0
    lock: int = 0

    @property
    def nbeats(self) -> int:
        return self.len + 1

    def describe(self) -> str:
        return (
            f"AW(id={self.id} addr=0x{self.addr:012x} len={self.len}"
            f" size={self.size} burst={self.burst})"
        )


@dataclass
class AxiWTxn:
    """One write data beat."""

    data: int = 0
    strb: int = 0
    last: int = 0
    poison: int = 0
    datachk: int = 0
    beat: int = 0

    def describe(self) -> str:
        return (
            f"W(beat={self.beat} data=0x{self.data:064x} strb=0x{self.strb:08x}"
            f" last={self.last})"
        )


@dataclass
class AxiBTxn:
    """Write response beat."""

    id: int = 0
    resp: int = 0

    def describe(self) -> str:
        return f"B(id={self.id} resp={self.resp})"


@dataclass
class AxiArTxn:
    """Read address beat."""

    id: int = 0
    addr: int = 0
    len: int = 0
    size: int = 0
    burst: int = 0
    qos: int = 0
    cache: int = 0
    prot: int = 0
    lock: int = 0
    snoop: int = 0
    domain: int = 0

    @property
    def nbeats(self) -> int:
        return self.len + 1

    def describe(self) -> str:
        return (
            f"AR(id={self.id} addr=0x{self.addr:012x} len={self.len}"
            f" size={self.size} burst={self.burst})"
        )


@dataclass
class AxiRTxn:
    """One read data beat."""

    id: int = 0
    data: int = 0
    resp: int = 0
    last: int = 0
    poison: int = 0
    datachk: int = 0
    trace: int = 0
    user: int = 0
    beat: int = 0

    def describe(self) -> str:
        return (
            f"R(beat={self.beat} id={self.id} data=0x{self.data:064x}"
            f" resp={self.resp} last={self.last})"
        )


class AxiMonitor(pyuvm.uvm_component):
    """Monitors the C2XM AXI4 master port."""

    def build_phase(self):
        self.aw_ap = pyuvm.uvm_analysis_port("aw_ap", self)
        self.w_ap = pyuvm.uvm_analysis_port("w_ap", self)
        self.b_ap = pyuvm.uvm_analysis_port("b_ap", self)
        self.ar_ap = pyuvm.uvm_analysis_port("ar_ap", self)
        self.r_ap = pyuvm.uvm_analysis_port("r_ap", self)
        #: Completed beats per channel.
        self.counts = {"aw": 0, "w": 0, "b": 0, "ar": 0, "r": 0}
        #: Completed AXI transactions, in completion order.
        self.aw_seen: List[AxiAwTxn] = []
        self.ar_seen: List[AxiArTxn] = []

    async def run_phase(self):
        cocotb.start_soon(self._monitor_aw())
        cocotb.start_soon(self._monitor_w())
        cocotb.start_soon(self._monitor_b())
        cocotb.start_soon(self._monitor_ar())
        cocotb.start_soon(self._monitor_r())

    async def _monitor_aw(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.awvalid) != 1 or _bit(top.awready) != 1:
                continue
            txn = AxiAwTxn(
                id=_int(top.awid) or 0,
                addr=_int(top.awaddr) or 0,
                len=_int(top.awlen) or 0,
                size=_int(top.awsize) or 0,
                burst=_int(top.awburst) or 0,
                qos=_int(top.awqos) or 0,
                cache=_int(top.awcache) or 0,
                prot=_int(top.awprot) or 0,
                lock=_int(top.awlock) or 0,
            )
            self.counts["aw"] += 1
            self.aw_seen.append(txn)
            self.logger.info(f"AXI {txn.describe()}")
            self.aw_ap.write(txn)

    async def _monitor_w(self):
        top = cocotb.top
        beat = 0
        while True:
            await FallingEdge(top.clk)
            if _bit(top.wvalid) != 1 or _bit(top.wready) != 1:
                continue
            txn = AxiWTxn(
                data=_int(top.wdata) or 0,
                strb=_int(top.wstrb) or 0,
                last=_bit(top.wlast) or 0,
                poison=_int(top.wpoison) or 0,
                datachk=_int(top.wdatachk) or 0,
                beat=beat,
            )
            beat = 0 if txn.last else beat + 1
            self.counts["w"] += 1
            self.logger.info(f"AXI {txn.describe()}")
            self.w_ap.write(txn)

    async def _monitor_b(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.bvalid) != 1 or _bit(top.bready) != 1:
                continue
            txn = AxiBTxn(id=_int(top.bid) or 0, resp=_int(top.bresp) or 0)
            self.counts["b"] += 1
            self.logger.info(f"AXI {txn.describe()}")
            self.b_ap.write(txn)

    async def _monitor_ar(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.arvalid) != 1 or _bit(top.arready) != 1:
                continue
            txn = AxiArTxn(
                id=_int(top.arid) or 0,
                addr=_int(top.araddr) or 0,
                len=_int(top.arlen) or 0,
                size=_int(top.arsize) or 0,
                burst=_int(top.arburst) or 0,
                qos=_int(top.arqos) or 0,
                cache=_int(top.arcache) or 0,
                prot=_int(top.arprot) or 0,
                lock=_int(top.arlock) or 0,
                snoop=_int(top.arsnoop) or 0,
                domain=_int(top.ardomain) or 0,
            )
            self.counts["ar"] += 1
            self.ar_seen.append(txn)
            self.logger.info(f"AXI {txn.describe()}")
            self.ar_ap.write(txn)

    async def _monitor_r(self):
        top = cocotb.top
        beat = 0
        while True:
            await FallingEdge(top.clk)
            if _bit(top.rvalid) != 1 or _bit(top.rready) != 1:
                continue
            txn = AxiRTxn(
                id=_int(top.rid) or 0,
                data=_int(top.rdata) or 0,
                resp=_int(top.rresp) or 0,
                last=_bit(top.rlast) or 0,
                poison=_int(top.rpoison) or 0,
                datachk=_int(top.rdatachk) or 0,
                trace=_bit(top.rtrace) or 0,
                user=_int(top.ruser) or 0,
                beat=beat,
            )
            beat = 0 if txn.last else beat + 1
            self.counts["r"] += 1
            self.logger.info(f"AXI {txn.describe()}")
            self.r_ap.write(txn)

    def final_phase(self):
        self.logger.info(f"axi beat counts: {self.counts}")
