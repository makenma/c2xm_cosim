"""gem5 CHI flit -> pyuvm transaction adaptor.

The adaptor is the bridge between gem5's CHI model and this testbench:

    gem5  --(chi_flit.RawReq / RawDat)-->  Gem5Adaptor  --(ChiTxn)-->  driver

Flits arrive through ``flit_export`` (a blocking put export, so a future gem5
co-simulation shim or a UVM sequence can push them) and complete transactions
leave through ``item_port``, which the environment connects to the CHI link
driver's item fifo.

Conversion rules
----------------
``RawReq`` ReadNoSnp (0x04)
    -> ``ChiReadNoSnpTxn``, emitted immediately.
``RawReq`` WriteNoSnp (0x1C/0x1D)
    -> ``ChiWriteNoSnpTxn``, emitted immediately and remembered in
       ``self.writes[txn_id]`` so arriving data can be attributed.
``RawDat`` non-copyback write data (0x3)
    -> ``ChiWriteDataTxn`` carrying the data flit; the flit is also appended to
       the live ``ChiWriteNoSnpTxn`` object so any later checker sees the
       completed transaction.
anything else
    -> dropped with a warning (the DUT implements only ReadNoSnp/WriteNoSnp).

Skeleton status: no address translation, no cache-line regrouping (gem5 may
hand over a whole cacheline as one flit, the DUT wants 32-byte beats - see
``split_beat``), and no back-pressure handling beyond the fifo sizes.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pyuvm

from chi_flit import RawDat, RawReq, decode_dat
from chi_txn import (
    ChiReadNoSnpTxn,
    ChiTxn,
    ChiTxnItem,
    ChiWriteDataTxn,
    ChiWriteNoSnpTxn,
    txn_from_req,
)

#: Non-copyback write data opcode (DatOpcode.hh).
OPC_NCB_WRITE_DATA = 0x3
#: Copyback write data (the DUT has no snoop channel, so this is unusual).
OPC_CB_WRITE_DATA = 0x2


class Gem5Adaptor(pyuvm.uvm_component):
    """Converts gem5 CHI flits into transactions for the link driver."""

    def __init__(self, name, parent):
        super().__init__(name, parent)
        #: Live write transactions, keyed by CHI txn id.
        self.writes: Dict[int, ChiWriteNoSnpTxn] = {}
        #: Live read transactions, keyed by CHI txn id.
        self.reads: Dict[int, ChiReadNoSnpTxn] = {}
        self.converted = 0
        self.dropped = 0

    def build_phase(self):
        #: gem5 (or a sequence) pushes flits in here.
        self.flit_fifo = pyuvm.uvm_tlm_fifo("flit_fifo", self, size=64)
        self.flit_export = self.flit_fifo.put_export
        #: Completed transactions leave here for the driver.
        self.item_port = pyuvm.uvm_blocking_put_port("item_port", self)
        #: Mirror of everything pushed to the driver, for checkers/coverage.
        self.ap = pyuvm.uvm_analysis_port("ap", self)

    async def push_flit(self, flit) -> None:
        """Coroutine form of ``flit_export.put()`` for test code."""
        await self.flit_fifo.put(flit)

    def try_push_flit(self, flit) -> bool:
        """Non-blocking push; returns False if the fifo is full."""
        return self.flit_fifo.try_put(flit)

    async def run_phase(self):
        while True:
            flit = await self.flit_fifo.get()
            for txn in self.to_transactions(flit):
                await self.item_port.put(ChiTxnItem(txn))
                self.ap.write(txn)
                self.converted += 1

    # ------------------------------------------------------------------
    # conversion
    # ------------------------------------------------------------------
    def to_transactions(self, flit) -> List[ChiTxn]:
        """Flit -> zero, one or more transactions."""
        if isinstance(flit, RawReq):
            return self._req_to_txn(flit)
        if isinstance(flit, RawDat):
            return self._dat_to_txn(flit)
        self.dropped += 1
        self.logger.warning(f"adaptor: unsupported flit type {type(flit).__name__}")
        return []

    def _req_to_txn(self, flit: RawReq) -> List[ChiTxn]:
        try:
            txn = txn_from_req(flit)
        except ValueError as exc:
            self.dropped += 1
            self.logger.warning(f"adaptor: {exc}")
            return []

        if isinstance(txn, ChiReadNoSnpTxn):
            self.reads[txn.txn_id] = txn
        elif isinstance(txn, ChiWriteNoSnpTxn):
            self.writes[txn.txn_id] = txn
        self.logger.info(f"adaptor: {txn.describe()}")
        return [txn]

    def _dat_to_txn(self, flit: RawDat) -> List[ChiTxn]:
        decoded = decode_dat(flit.opcode)
        if flit.opcode not in (OPC_NCB_WRITE_DATA, OPC_CB_WRITE_DATA):
            self.dropped += 1
            self.logger.warning(
                f"adaptor: DAT opcode 0x{flit.opcode:x}"
                f" ({decoded.major.name}/{decoded.minor.name}) is not write data"
            )
            return []

        writer = self.writes.get(flit.txnid)
        if writer is None:
            self.logger.warning(
                f"adaptor: write data for unknown txn {flit.txnid};"
                " forwarding as a standalone data transaction"
            )
            return [ChiWriteDataTxn(txn_id=flit.txnid, opcode=flit.opcode, data=[flit])]

        writer.write_data.append(flit)
        if flit.data_id in writer.pending_data_ids:
            writer.pending_data_ids.remove(flit.data_id)
        self.logger.info(
            f"adaptor: {writer.describe()} += data_id={flit.data_id}"
            f" (pending={writer.pending_data_ids})"
        )
        return [
            ChiWriteDataTxn(
                txn_id=writer.txn_id,
                addr=writer.addr,
                opcode=flit.opcode,
                size=writer.size,
                qos=writer.qos,
                src_id=writer.src_id,
                tgt_id=writer.tgt_id,
                data=[flit],
            )
        ]

    def complete_read(self, txn_id: int, flit: RawDat) -> Optional[ChiReadNoSnpTxn]:
        """Attach a CompData flit arriving on the TXDAT channel to its read.

        Not wired to the CHI monitor yet - the environment has no scoreboard, so
        nothing calls this.  It is the hook for when data checking is added.
        """
        read = self.reads.get(txn_id)
        if read is None:
            return None
        read.comp_data.append(flit)
        if read.complete:
            self.logger.info(f"adaptor: {read.describe()} complete")
        return read

    def final_phase(self):
        self.logger.info(
            f"adaptor: converted={self.converted} dropped={self.dropped}"
            f" open_reads={len(self.reads)} open_writes={len(self.writes)}"
        )
