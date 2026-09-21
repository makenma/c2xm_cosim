"""CHI link monitor.

Passively observes all four CHI link channels of C2XM and republishes decoded
gem5-style flits on analysis ports:

    rxreq_ap -> chi_flit.RawReq   flits the testbench drove into the DUT
    rxdat_ap -> chi_flit.RawDat   flits the testbench drove into the DUT
    txrsp_ap -> chi_flit.RawRsp   flits the DUT sent back
    txdat_ap -> chi_flit.RawDat   flits the DUT sent back

Sampling happens on falling clock edges (mid cycle), so it sees exactly the
values the DUT sampled on the preceding rising edge.  Each flit also carries
its decode (major/minor) so downstream components do not have to re-decode.

Skeleton status: credit-return flits (RespLCrdReturn / DataLCrdReturn) and
link deactivation are reported as ordinary flits, but nothing acts on them yet.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from tb_signals import read_bit as _bit, read_int as _int

from chi_flit import (
    RawDat,
    RawReq,
    RawRsp,
    decode_dat,
    decode_req,
    decode_rsp,
    unpack_rxdat,
    unpack_rxreq,
    unpack_txdat,
    unpack_txrsp,
)




class ChiMonitor(pyuvm.uvm_component):
    """Monitors the four CHI link channels between gem5 and C2XM."""

    def build_phase(self):
        self.rxreq_ap = pyuvm.uvm_analysis_port("rxreq_ap", self)
        self.rxdat_ap = pyuvm.uvm_analysis_port("rxdat_ap", self)
        self.txrsp_ap = pyuvm.uvm_analysis_port("txrsp_ap", self)
        self.txdat_ap = pyuvm.uvm_analysis_port("txdat_ap", self)
        #: Flit counters, handy for end-of-test logging and coverage.
        self.counts = {"rxreq": 0, "rxdat": 0, "txrsp": 0, "txdat": 0}

    async def run_phase(self):
        cocotb.start_soon(self._monitor_rxreq())
        cocotb.start_soon(self._monitor_rxdat())
        cocotb.start_soon(self._monitor_txrsp())
        cocotb.start_soon(self._monitor_txdat())

    async def _monitor_rxreq(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.rxreqfltv) != 1:
                continue
            payload = _int(top.rxreqflt)
            if payload is None:
                continue
            flit = unpack_rxreq(payload)
            self.counts["rxreq"] += 1
            self.logger.debug(
                f"RXREQ  payload=0x{payload:043x} txn={flit.txnid}"
                f" {decode_req(flit.opcode).minor.name}"
            )
            self.rxreq_ap.write(flit)

    async def _monitor_rxdat(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.rxdatfltv) != 1:
                continue
            payload = _int(top.rxdatflt)
            if payload is None:
                continue
            flit = unpack_rxdat(payload)
            self.counts["rxdat"] += 1
            self.logger.debug(
                f"RXDAT  txn={flit.txnid} data_id={flit.data_id}"
                f" {decode_dat(flit.opcode).minor.name}"
            )
            self.rxdat_ap.write(flit)

    async def _monitor_txrsp(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.txrspfltv) != 1:
                continue
            payload = _int(top.txrspflt)
            if payload is None:
                continue
            flit: RawRsp = unpack_txrsp(payload)
            self.counts["txrsp"] += 1
            self.logger.info(
                f"TXRSP  txn={flit.txnid} dbid={flit.dbid}"
                f" {decode_rsp(flit.opcode).minor.name}"
                f" tgt=0x{flit.tgtid:x} src=0x{flit.srcid:x}"
            )
            self.txrsp_ap.write(flit)

    async def _monitor_txdat(self):
        top = cocotb.top
        while True:
            await FallingEdge(top.clk)
            if _bit(top.txdatfltv) != 1:
                continue
            payload = _int(top.txdatflt)
            if payload is None:
                continue
            flit: RawDat = unpack_txdat(payload)
            self.counts["txdat"] += 1
            self.logger.info(
                f"TXDAT  txn={flit.txnid} data_id={flit.data_id}"
                f" {decode_dat(flit.opcode).minor.name}"
                f" data=0x{int.from_bytes(bytes(flit.data), 'little'):064x}"
            )
            self.txdat_ap.write(flit)

    def final_phase(self):
        self.logger.info(f"chi flit counts: {self.counts}")
