"""Transactions exchanged between the gem5 adaptor, the driver and the monitors.

The flow modelled here is:

    gem5 CHI flit (chi_flit.RawReq / RawDat)
        -> Gem5Adaptor.to_transaction()      -> ChiTxn  (this module)
        -> ChiLinkDriver                     -> packed link flit on the DUT pins
        -> ChiMonitor / AxiMonitor           -> analysis ports

Only ReadNoSnp and WriteNoSnp are modelled, matching the DUT's CHI subset
(``c2xm_core_admit_and_allocate_pool_entry_operation.sv`` decodes opcode 7'd4
as read and 7'd28/7'd29 as write).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pyuvm

from chi_flit import (
    OPC_READNOSNP,
    WRITE_NOSNP_OPCODES,
    RawDat,
    RawReq,
    decode_dat,
    decode_req,
)

#: The DUT looks transactions up in its pool by ``txn_id[4:0]``
#: (c2xm_core_receive_rxdat_operation.sv builds ``queue_id = 5'(txn_id)`` for
#: the RX DAT and log-buffer lookups), but allocation picks the *lowest free*
#: slot (admit_and_allocate: ``onehot_first(free_candidates)``) rather than the
#: slot addressed by the txn id.  The two only agree when the requester's
#: ``txn_id[4:0]`` equals the slot the request lands in - for a fresh pool that
#: means the first request must use txn_id 0, the second txn_id 1, and so on.
#: Write data sent with a mismatched txn id is committed to the wrong (free)
#: entry, so its ``received_data_mask`` never fills and no AW/AR is issued.
#: VERIFY WITH THE RTL OWNER; see README "Known issues".
TXN_ID_MODULO = 1 << 5

#: 64-byte cache line: a WriteNoSnpFull is delivered as two 32-byte DAT beats.
CACHE_LINE_BYTES = 64
DAT_BEAT_BYTES = 32


def size_to_bytes(size: int) -> int:
    """CHI encoded size -> transfer size in bytes (size 6 -> 64B)."""
    return 1 << int(size)


def required_data_ids(addr: int, size: int) -> List[int]:
    """DAT ``data_id`` values that complete a write of ``size`` at ``addr``.

    ``c2xm_core_receive_rxdat_operation.sv`` maps ``data_id`` 0/1 to the low
    256 bits (mask 0b0011) and 2/3 to the high 256 bits (mask 0b1100), while
    the pool's ``expected_data_mask`` (admit_and_allocate_operation.sv) is

    * ``size == 6`` -> 0b1111, i.e. both 32-byte halves,
    * otherwise     -> 0b0011 / 0b1100 selected by ``addr[5]``.

    So a full line needs ``data_id`` 0 and 2; a partial/short transfer needs a
    single beat whose ``data_id`` picks the half containing ``addr[5]``.
    """
    if int(size) >= 6:
        return [0, 2]
    return [2 if (int(addr) >> 5) & 0x1 else 0]


@dataclass
class ChiTxn:
    """Common CHI transaction fields, taken from the request flit."""

    #: 12-bit CHI transaction id (pool index is txn_id[4:0]).
    txn_id: int = 0
    #: 48-bit byte address.
    addr: int = 0
    #: 7-bit request opcode as seen on the RXREQ channel.
    opcode: int = 0
    #: CHI encoded size (log2 bytes).
    size: int = 6
    qos: int = 0
    src_id: int = 0
    tgt_id: int = 0
    return_nid: int = 0
    order: int = 0
    #: CHI MemAttr[3:0]: EWA | Device<<1 | Cacheable<<2 | Allocate<<3.
    memattr: int = 0
    allow_retry: int = 0
    trace_tag: bool = False
    user: int = 0
    non_secure: bool = False
    #: The flit this transaction was built from (kept for debug/coverage).
    raw_flit: Optional[RawReq] = None

    @property
    def nbytes(self) -> int:
        return size_to_bytes(self.size)

    @property
    def is_read(self) -> bool:
        return self.opcode == OPC_READNOSNP

    @property
    def is_write(self) -> bool:
        return self.opcode in WRITE_NOSNP_OPCODES

    def describe(self) -> str:
        decoded = decode_req(self.opcode)
        return (
            f"{self.__class__.__name__}(opcode=0x{self.opcode:02x}"
            f" {decoded.major.name}/{decoded.minor.name}, txn_id={self.txn_id},"
            f" addr=0x{self.addr:012x}, size={self.size})"
        )


@dataclass
class ChiReadNoSnpTxn(ChiTxn):
    """ReadNoSnp request: the DUT returns CompData beats on the TXDAT channel."""

    #: CompData flits observed back for this transaction.
    comp_data: List[RawDat] = field(default_factory=list)
    #: Number of 32-byte beats expected for a full line.
    expected_beats: int = 2

    @property
    def complete(self) -> bool:
        return len(self.comp_data) >= self.expected_beats


@dataclass
class ChiWriteNoSnpTxn(ChiTxn):
    """WriteNoSnp request: the TB supplies write data on the RXDAT channel."""

    #: True for WriteNoSnpPtl (0x1D), False for WriteNoSnpFull (0x1C).
    partial: bool = False
    #: Data beat ``data_id`` values the DUT expects (0, and 2 for a full line).
    pending_data_ids: List[int] = field(default_factory=list)
    #: Write data flits already received from gem5.
    write_data: List[RawDat] = field(default_factory=list)
    #: DBID returned by the DUT in DBIDResp/CompDBIDResp, if observed.
    dbid: Optional[int] = None

    @property
    def data_complete(self) -> bool:
        return not self.pending_data_ids


@dataclass
class ChiWriteDataTxn(ChiTxn):
    """A write data flit with no matching request yet (adaptor fallback path)."""

    data: List[RawDat] = field(default_factory=list)

    def describe(self) -> str:
        decoded = decode_dat(self.opcode)
        return (
            f"ChiWriteDataTxn(opcode=0x{self.opcode:x}"
            f" {decoded.major.name}/{decoded.minor.name}, txn_id={self.txn_id},"
            f" data_id={self.data[-1].data_id if self.data else '?'})"
        )


def txn_from_req(flit: RawReq) -> ChiTxn:
    """Build a transaction from a ReadNoSnp / WriteNoSnp request flit.

    :raises ValueError: for opcodes the DUT does not implement.
    """
    common = dict(
        txn_id=flit.txnid,
        addr=flit.addr,
        opcode=flit.opcode,
        size=flit.size,
        qos=flit.qos,
        src_id=flit.srcid,
        tgt_id=flit.tgtid,
        return_nid=flit.return_nid,
        order=flit.order,
        memattr=flit.memattr,
        allow_retry=flit.allow_retry,
        trace_tag=flit.trace_tag,
        user=flit.user,
        non_secure=flit.non_secure,
        raw_flit=flit,
    )
    if flit.is_read_no_snp:
        # The DUT turns one 256-bit AXI R beat into one 32-byte CompData flit.
        return ChiReadNoSnpTxn(
            expected_beats=max(1, size_to_bytes(flit.size) // DAT_BEAT_BYTES), **common
        )
    if flit.is_write_no_snp:
        partial = flit.opcode == 0x1D
        return ChiWriteNoSnpTxn(
            partial=partial,
            pending_data_ids=required_data_ids(flit.addr, flit.size),
            **common,
        )
    decoded = decode_req(flit.opcode)
    raise ValueError(
        f"unsupported CHI request opcode 0x{flit.opcode:02x}"
        f" ({decoded.major.name}/{decoded.minor.name}); C2XM implements only"
        " ReadNoSnp (0x04) and WriteNoSnp (0x1C/0x1D)"
    )


class ChiTxnItem(pyuvm.uvm_sequence_item):
    """TLM payload carried from the gem5 adaptor to the driver.

    A thin wrapper so transactions can travel through a ``uvm_tlm_fifo`` and
    still pick up pyuvm's item bookkeeping (ids, parent sequence, logging).
    """

    def __init__(self, txn: ChiTxn, name: str = "chi_txn_item"):
        super().__init__(name)
        self.txn = txn

    def __str__(self) -> str:
        return f"ChiTxnItem({self.txn.describe()})"
