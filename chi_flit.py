"""CHI flit model for the C2XM verification environment.

Two things live here:

1. A Python mirror of the gem5 CHI flit/opcode definitions in
   ``XS-DSU-GEM5/src/mem/cache/CHI/base`` (``ChiChannel.hh``, ``ReqOpcode.hh``,
   ``RspOpcode.hh``, ``DatOpcode.hh``).  Field names follow the C++ headers so
   the two can be diffed side by side.

2. The *wire* layout of the four C2XM link flits, taken bit-exactly from the
   generated RTL (see ``OFFSET_COMMENT`` references next to every layout):
     * RXREQ 172b  -- ``c2xm_core.sv`` lc_rxreqflt[] slices
     * RXDAT 426b  -- ``c2xm_core.sv`` lc2wb_rxdatflt[] slices
     * TXRSP  75b  -- ``c2xm_dsl_chi_txrsp_link.sv`` link_payload_o packing,
                      payload tail offsets confirmed against core_txrsp_payload
     * TXDAT 426b  -- ``c2xm_core.sv`` rb2lc_txdatflt[] slices

   ``pack_*`` / ``unpack_*`` convert between the gem5-style flit objects and the
   raw integers that are driven on / sampled from the cocotb ``cocotb.top``
   signals.

Only ReadNoSnp (opcode 0x04) and WriteNoSnp (opcode 0x1C / 0x1D) are accepted by
the DUT; everything else is decoded so that the monitors can still name it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Channel / opcode definitions (mirror of gem5 CHI/base)
# ---------------------------------------------------------------------------


class ChannelType(enum.IntEnum):
    """ChiChannel.hh: enum ChannelType."""

    REQ = 0
    RSP = 1
    SNP = 2
    DAT = 3


class RspKind(enum.IntEnum):
    """ChiChannel.hh: enum class RspKind."""

    MainPath = 0
    ShortPath = 1
    RetryAck = 2
    PCrdGrant = 3


class ReqVariant(enum.IntEnum):
    V0 = 0
    V1 = 1


class ReqMajor(enum.IntEnum):
    Read = 0
    Write = 1
    Maintenance = 2
    Atomic = 3
    DVM = 4
    Prefetch = 5
    ReservedOrUnsupported = 6


class ReqMinor(enum.IntEnum):
    ReadShared = 0
    MakeReadUnique = 1
    ReadClean = 2
    ReadOnce = 3
    ReadNoSnp = 4
    ReadUnique = 5
    ReadNoSnpSep = 6
    ReadPreferUnique = 7
    WriteUnique = 8
    WriteBack = 9
    WriteClean = 10
    WriteNoSnp = 11
    WriteEvict = 12
    CleanShared = 13
    CleanInvalid = 14
    CleanUnique = 15
    MakeInvalid = 16
    MakeUnique = 17
    Evict = 18
    CleanSharedPersist = 19
    CleanSharedPersistSep = 20
    Atomic = 21
    DVMOp = 22
    PrefetchTgt = 23
    ReservedOrUnsupported = 24


class RspMajor(enum.IntEnum):
    Credit = 0
    Snoop = 1
    Completion = 2
    Retry = 3
    DBID = 4
    Receipt = 5
    Tag = 6
    Persist = 7
    Stash = 8
    CMO = 9
    ReservedOrUnsupported = 10


class RspMinor(enum.IntEnum):
    RespLCrdReturn = 0
    SnpResp = 1
    CompAck = 2
    RetryAck = 3
    Comp = 4
    CompDBIDResp = 5
    DBIDResp = 6
    PCrdGrant = 7
    ReadReceipt = 8
    SnpRespFwded = 9
    TagMatch = 10
    RespSepData = 11
    Persist = 12
    CompPersist = 13
    DBIDRespOrd = 14
    StashDone = 15
    CompStashDone = 16
    CompCMO = 17
    ReservedOrUnsupported = 18


class DatMajor(enum.IntEnum):
    Credit = 0
    SnpRespData = 1
    WriteData = 2
    CompletionData = 3
    Cancel = 4
    ReservedOrUnsupported = 5


class DatMinor(enum.IntEnum):
    DataLCrdReturn = 0
    SnpRespData = 1
    SnpRespDataPtl = 2
    SnpRespDataFwded = 3
    CopyBackWriteData = 4
    NonCopyBackWriteData = 5
    CompData = 6
    DataSepResp = 7
    NCBWrDataCompAck = 8
    WriteDataCancel = 9
    ReservedOrUnsupported = 10


@dataclass
class ReqOpcode:
    """ReqOpcode.hh: 7-bit opcode, Opcode[5:0] + variant bit Opcode[6]."""

    raw: int

    @property
    def base(self) -> int:
        return self.raw & 0x3F

    @property
    def variant(self) -> ReqVariant:
        return ReqVariant((self.raw >> 6) & 0x1)

    def v1(self) -> bool:
        return bool(self.raw & 0x40)


@dataclass
class RspOpcode:
    """RspOpcode.hh: only bits[4:0] are used."""

    raw: int

    @property
    def code(self) -> int:
        return self.raw & 0x1F


@dataclass
class DatOpcode:
    """DatOpcode.hh: only bits[3:0] are used."""

    raw: int

    @property
    def code(self) -> int:
        return self.raw & 0x0F


@dataclass
class DecodedReq:
    op: ReqOpcode
    major: ReqMajor
    minor: ReqMinor


@dataclass
class DecodedRsp:
    op: RspOpcode
    major: RspMajor
    minor: RspMinor


@dataclass
class DecodedDat:
    op: DatOpcode
    major: DatMajor
    minor: DatMinor


#: CHI opcodes the DUT actually implements (from the RTL admit path:
#: ``s_entry_rxreq.opcode == 7'd4`` read, ``7'd28``/``7'd29`` write).
OPC_READNOSNP = 0x04
OPC_WRITENOSNP_FULL = 0x1C
OPC_WRITENOSNP_PTL = 0x1D
WRITE_NOSNP_OPCODES = (OPC_WRITENOSNP_FULL, OPC_WRITENOSNP_PTL)


def decode_req(raw: int) -> DecodedReq:
    """Mirror of gem5 ``decodeReq`` (ReqOpcode.hh)."""
    op = ReqOpcode(raw)
    b = op.base
    out = DecodedReq(op, ReqMajor.ReservedOrUnsupported, ReqMinor.ReservedOrUnsupported)

    if 0x28 <= b <= 0x2F or 0x30 <= b <= 0x37:
        out.major, out.minor = ReqMajor.Atomic, ReqMinor.Atomic
        return out

    if b == 0x01:
        out.major = ReqMajor.Read
        out.minor = ReqMinor.MakeReadUnique if op.v1() else ReqMinor.ReadShared
    elif b == 0x02:
        if op.v1():
            out.major, out.minor = ReqMajor.Write, ReqMinor.WriteEvict
        else:
            out.major, out.minor = ReqMajor.Read, ReqMinor.ReadClean
    elif b == 0x03:
        if op.v1():
            out.major, out.minor = ReqMajor.Write, ReqMinor.WriteUnique
        else:
            out.major, out.minor = ReqMajor.Read, ReqMinor.ReadOnce
    elif b == 0x04:
        # WriteNoSnpZero shares base 0x04 with variant bit set.
        if op.v1():
            out.major, out.minor = ReqMajor.Write, ReqMinor.WriteNoSnp
        else:
            out.major, out.minor = ReqMajor.Read, ReqMinor.ReadNoSnp
    elif b == 0x07:
        if op.v1():
            out.major = ReqMajor.Maintenance
        else:
            out.major, out.minor = ReqMajor.Read, ReqMinor.ReadUnique
    elif b == 0x08:
        out.major, out.minor = ReqMajor.Maintenance, ReqMinor.CleanShared
    elif b == 0x09:
        out.major, out.minor = ReqMajor.Maintenance, ReqMinor.CleanInvalid
    elif b == 0x0A:
        out.major, out.minor = ReqMajor.Maintenance, ReqMinor.MakeInvalid
    elif b == 0x0B:
        out.major, out.minor = ReqMajor.Maintenance, ReqMinor.CleanUnique
    elif b == 0x0C:
        if op.v1():
            out.major, out.minor = ReqMajor.Maintenance, ReqMinor.ReadPreferUnique
        else:
            out.major, out.minor = ReqMajor.Maintenance, ReqMinor.MakeUnique
    elif b == 0x0D:
        out.major, out.minor = ReqMajor.Maintenance, ReqMinor.Evict
    elif b == 0x14:
        if op.v1():
            out.major, out.minor = ReqMajor.Write, ReqMinor.WriteUnique
        else:
            out.major, out.minor = ReqMajor.DVM, ReqMinor.DVMOp
    elif b == 0x15:
        out.major, out.minor = ReqMajor.Write, ReqMinor.WriteEvict
    elif b == 0x17:
        out.major, out.minor = ReqMajor.Write, ReqMinor.WriteClean
    elif b in (0x18, 0x19):
        out.major, out.minor = ReqMajor.Write, ReqMinor.WriteUnique
    elif b == 0x1B:
        out.major, out.minor = ReqMajor.Write, ReqMinor.WriteBack
    elif b in (0x1C, 0x1D):
        out.major, out.minor = ReqMajor.Write, ReqMinor.WriteNoSnp
    elif b == 0x3A:
        out.major, out.minor = ReqMajor.Prefetch, ReqMinor.PrefetchTgt
    return out


_RSP_DECODE: Dict[int, Tuple[RspMajor, RspMinor]] = {
    0x00: (RspMajor.Credit, RspMinor.RespLCrdReturn),
    0x01: (RspMajor.Snoop, RspMinor.SnpResp),
    0x02: (RspMajor.Completion, RspMinor.CompAck),
    0x03: (RspMajor.Retry, RspMinor.RetryAck),
    0x04: (RspMajor.Completion, RspMinor.Comp),
    0x05: (RspMajor.Completion, RspMinor.CompDBIDResp),
    0x06: (RspMajor.DBID, RspMinor.DBIDResp),
    0x07: (RspMajor.Credit, RspMinor.PCrdGrant),
    0x08: (RspMajor.Receipt, RspMinor.ReadReceipt),
    0x09: (RspMajor.Snoop, RspMinor.SnpRespFwded),
    0x0A: (RspMajor.Tag, RspMinor.TagMatch),
    0x0B: (RspMajor.Completion, RspMinor.RespSepData),
    0x0C: (RspMajor.Persist, RspMinor.Persist),
    0x0D: (RspMajor.Persist, RspMinor.CompPersist),
    0x0E: (RspMajor.DBID, RspMinor.DBIDRespOrd),
    0x10: (RspMajor.Stash, RspMinor.StashDone),
    0x11: (RspMajor.Stash, RspMinor.CompStashDone),
    0x14: (RspMajor.CMO, RspMinor.CompCMO),
}


def decode_rsp(raw: int) -> DecodedRsp:
    """Mirror of gem5 ``decodeRsp`` (RspOpcode.hh)."""
    op = RspOpcode(raw)
    major, minor = _RSP_DECODE.get(
        op.code, (RspMajor.ReservedOrUnsupported, RspMinor.ReservedOrUnsupported)
    )
    return DecodedRsp(op, major, minor)


_DAT_DECODE: Dict[int, Tuple[DatMajor, DatMinor]] = {
    0x0: (DatMajor.Credit, DatMinor.DataLCrdReturn),
    0x1: (DatMajor.SnpRespData, DatMinor.SnpRespData),
    0x2: (DatMajor.WriteData, DatMinor.CopyBackWriteData),
    0x3: (DatMajor.WriteData, DatMinor.NonCopyBackWriteData),
    0x4: (DatMajor.CompletionData, DatMinor.CompData),
    0x5: (DatMajor.SnpRespData, DatMinor.SnpRespDataPtl),
    0x6: (DatMajor.SnpRespData, DatMinor.SnpRespDataFwded),
    0x7: (DatMajor.Cancel, DatMinor.WriteDataCancel),
    0xB: (DatMajor.CompletionData, DatMinor.DataSepResp),
    0xC: (DatMajor.CompletionData, DatMinor.NCBWrDataCompAck),
}


def decode_dat(raw: int) -> DecodedDat:
    """Mirror of gem5 ``decodeDat`` (DatOpcode.hh)."""
    op = DatOpcode(raw)
    major, minor = _DAT_DECODE.get(
        op.code, (DatMajor.ReservedOrUnsupported, DatMinor.ReservedOrUnsupported)
    )
    return DecodedDat(op, major, minor)


# ---------------------------------------------------------------------------
# Flit payload objects (mirror of gem5 CHI/base/ChiChannel.hh)
# ---------------------------------------------------------------------------


@dataclass
class BaseFlit:
    qos: int = 0
    srcid: int = 0
    tgtid: int = 0
    txnid: int = 0
    opcode: int = 0
    #: Pipeline stage, carried in the gem5 model only (not on the C2XM wire).
    stage: int = 0

    def next_stage(self) -> None:
        self.stage += 1


@dataclass
class RawReq(BaseFlit):
    """ChiChannel.hh: struct RawReq."""

    allow_retry: int = 0
    addr: int = 0
    size: int = 0
    return_nid: int = 0
    order: int = 0
    pcrdtype: int = 0
    #: CHI MemAttr[3:0]: bit0 EWA, bit1 Device, bit2 Cacheable, bit3 Allocate.
    memattr: int = 0
    snpattr: int = 0
    exp_comp_ack: bool = False
    trace_tag: bool = False
    src_type: int = 0
    ldid: int = 0
    hnf_dirty_victim: bool = False
    #: RTL-visible extra field (gem5 RawReq has no counterpart).
    non_secure: bool = False
    #: RTL-visible request "user" bits [3:0].
    user: int = 0
    #: RTL request carries a ReturnTxnID next to ReturnNid; gem5 keeps only
    #: ReturnNid, so this defaults to txnid when packed.
    return_txn_id: Optional[int] = None

    @property
    def is_read_no_snp(self) -> bool:
        return self.opcode == OPC_READNOSNP

    @property
    def is_write_no_snp(self) -> bool:
        return self.opcode in WRITE_NOSNP_OPCODES


@dataclass
class RawRsp(BaseFlit):
    """ChiChannel.hh: struct RawRsp."""

    dbid: int = 0
    resp: int = 0
    resp_err: int = 0
    pcrdtype: int = 0
    rsp_kind: RspKind = RspKind.MainPath
    origin_seq: int = 0
    origin_cycle: int = 0
    forward_state: int = 0
    cbusy: int = 0
    tag_op: int = 0
    trace_tag: bool = False
    data_event: int = 0


@dataclass
class RawSnp(BaseFlit):
    """ChiChannel.hh: struct RawSnp.  Not used by C2XM (no snoop channel)."""

    addr: int = 0
    size: int = 0


@dataclass
class RawDat(BaseFlit):
    """ChiChannel.hh: struct RawDat.

    ``byte_enable`` / ``chunk_valid`` / ``data`` are byte / byte-group / byte
    vectors exactly like the C++ ``std::vector<uint8_t>``, so `data` holds
    ``byteLength()`` bytes of payload.
    """

    last: int = 0
    home_nid: int = 0
    dbid: int = 0
    data_id: int = 0
    resp: int = 0
    resp_err: int = 0
    data_source: int = 0
    ccid: int = 0
    trace_tag: bool = False
    beat_offset: int = 0
    byte_enable: List[int] = field(default_factory=list)
    chunk_valid: List[int] = field(default_factory=list)
    data: List[int] = field(default_factory=list)

    def byte_length(self) -> int:
        return len(self.data)

    @property
    def is_write_data(self) -> bool:
        return self.opcode in (0x2, 0x3)  # CopyBack / NonCopyBack write data

    @property
    def is_comp_data(self) -> bool:
        return self.opcode == 0x4


@dataclass
class FlitId:
    """ChiChannel.hh: struct FlitId + DecodeFlitId()."""

    XId: int
    YId: int
    PId: int
    DId: int


def decode_flit_id(value: int) -> FlitId:
    return FlitId(
        XId=(value >> 7) & 0xF,
        YId=(value >> 4) & 0x7,
        PId=(value >> 2) & 0x3,
        DId=value & 0x3,
    )


# ---------------------------------------------------------------------------
# RTL wire layouts
# ---------------------------------------------------------------------------
#
# A layout is an ordered tuple of (field name, lsb, width).  Fields not listed
# are reserved / not consumed by the DUT and are packed as zero.

Layout = Tuple[Tuple[str, int, int], ...]

# c2xm_core.sv lines 311-328 (lc_rxreqflt -> if_chi_rxreq_payload).
RXREQ_LAYOUT: Layout = (
    ("qos", 0, 4),
    ("tgtid", 4, 11),
    ("srcid", 15, 11),
    ("txnid", 26, 12),
    ("return_nid", 38, 11),
    ("return_txn_id", 50, 12),
    ("opcode", 62, 7),
    ("size", 69, 3),
    ("addr", 72, 48),
    ("non_secure", 120, 1),
    # 121 reserved
    ("allow_retry", 122, 1),
    ("order", 123, 2),
    # 125..128 reserved
    ("memattr_ewa", 129, 1),
    ("memattr_device", 130, 1),
    ("memattr_cacheable", 131, 1),
    ("memattr_allocate", 132, 1),
    # 133..145 reserved
    ("trace_tag", 146, 1),
    # 147..157 reserved
    ("user", 158, 4),
    # 162..171 reserved
)
RXREQ_WIDTH = 172

# c2xm_core.sv lines 329-334 (lc2wb_rxdatflt -> if_chi_rxdat_payload).
RXDAT_LAYOUT: Layout = (
    ("qos", 0, 4),
    ("tgtid", 4, 11),
    ("srcid", 15, 11),
    ("txnid", 26, 12),
    ("home_nid", 38, 11),
    ("opcode", 49, 4),
    ("resp_err", 53, 2),
    ("resp", 55, 3),
    ("data_source", 58, 4),
    ("dbid", 65, 12),
    ("ccid", 77, 2),
    ("data_id", 79, 2),
    ("trace_tag", 93, 1),
    ("byte_enable", 98, 32),
    ("data", 130, 256),
    # 386..425 reserved on the RX path (data_check/poison/chunk_valid are TX-only)
)
RXDAT_WIDTH = 426

# external/c2xm_dsl_chi_txrsp_link.sv link_payload_o packing:
#   [3:0]   qos
#   [14:4]  physical target node id (HNF_ID_LIST projection of the local index)
#   [74:15] core_txrsp_payload[68:9] -- tail offsets from c2xm_core.sv 335-353.
TXRSP_LAYOUT: Layout = (
    ("qos", 0, 4),
    ("tgtid", 4, 11),
    ("srcid", 15, 11),
    ("txnid", 26, 12),
    ("opcode", 38, 5),
    ("resp_err", 43, 2),
    ("resp", 45, 3),
    ("forward_state", 48, 3),
    ("cbusy", 51, 3),
    ("dbid", 54, 12),
    ("pcrdtype", 66, 4),
    ("tag_op", 70, 2),
    ("trace_tag", 72, 1),
    ("data_event", 73, 2),
)
TXRSP_WIDTH = 75

# c2xm_core.sv lines 357-373 (rb2lc_txdatflt).
TXDAT_LAYOUT: Layout = (
    ("qos", 0, 4),
    ("tgtid", 4, 11),
    ("srcid", 15, 11),
    ("txnid", 26, 12),
    ("home_nid", 38, 11),
    ("opcode", 49, 4),
    ("resp_err", 53, 2),
    ("resp", 55, 3),
    ("data_source", 58, 4),
    ("dbid", 65, 12),
    ("ccid", 77, 2),
    ("data_id", 79, 2),
    ("trace_tag", 93, 1),
    ("data", 130, 256),
    ("data_check", 386, 32),
    ("poison", 418, 4),
    ("chunk_valid", 422, 2),
)
TXDAT_WIDTH = 426


def _field_mask(width: int) -> int:
    return (1 << width) - 1


def pack(layout: Layout, values: Dict[str, int], width: int) -> int:
    """Pack ``values`` into a ``width``-bit integer using ``layout``."""
    word = 0
    for name, lsb, fwidth in layout:
        value = int(values.get(name, 0)) & _field_mask(fwidth)
        word |= value << lsb
    return word & _field_mask(width)


def unpack(layout: Layout, word: int) -> Dict[str, int]:
    """Inverse of :func:`pack`."""
    return {
        name: (int(word) >> lsb) & _field_mask(fwidth)
        for name, lsb, fwidth in layout
    }


# ---------------------------------------------------------------------------
# gem5 flit <-> wire conversion
# ---------------------------------------------------------------------------


def pack_rxreq(flit: RawReq) -> int:
    """Pack a gem5-style :class:`RawReq` into the 172-bit RXREQ link flit."""
    memattr = int(flit.memattr) & 0xF
    values = {
        "qos": flit.qos,
        "tgtid": flit.tgtid,
        "srcid": flit.srcid,
        "txnid": flit.txnid,
        "return_nid": flit.return_nid,
        "return_txn_id": (
            flit.txnid if flit.return_txn_id is None else flit.return_txn_id
        ),
        "opcode": flit.opcode,
        "size": flit.size,
        "addr": flit.addr,
        "non_secure": int(bool(flit.non_secure)),
        "allow_retry": flit.allow_retry,
        "order": flit.order,
        "memattr_ewa": memattr & 0x1,
        "memattr_device": (memattr >> 1) & 0x1,
        "memattr_cacheable": (memattr >> 2) & 0x1,
        "memattr_allocate": (memattr >> 3) & 0x1,
        "trace_tag": int(bool(flit.trace_tag)),
        "user": flit.user,
    }
    return pack(RXREQ_LAYOUT, values, RXREQ_WIDTH)


def unpack_rxreq(word: int) -> RawReq:
    """Inverse of :func:`pack_rxreq`."""
    f = unpack(RXREQ_LAYOUT, word)
    memattr = (
        f["memattr_ewa"]
        | (f["memattr_device"] << 1)
        | (f["memattr_cacheable"] << 2)
        | (f["memattr_allocate"] << 3)
    )
    return RawReq(
        qos=f["qos"],
        tgtid=f["tgtid"],
        srcid=f["srcid"],
        txnid=f["txnid"],
        opcode=f["opcode"],
        return_nid=f["return_nid"],
        return_txn_id=f["return_txn_id"],
        allow_retry=f["allow_retry"],
        addr=f["addr"],
        size=f["size"],
        order=f["order"],
        memattr=memattr,
        trace_tag=bool(f["trace_tag"]),
        non_secure=bool(f["non_secure"]),
        user=f["user"],
    )


def _bytes_to_int(byte_list: List[int]) -> int:
    """Little-endian byte vector -> integer (CHI data beats are byte lanes)."""
    value = 0
    for i, byte in enumerate(byte_list):
        value |= (int(byte) & 0xFF) << (8 * i)
    return value


def _int_to_bytes(value: int, nbytes: int) -> List[int]:
    return [(value >> (8 * i)) & 0xFF for i in range(nbytes)]


def pack_rxdat(flit: RawDat) -> int:
    """Pack a gem5-style :class:`RawDat` into the 426-bit RXDAT link flit.

    The DUT's RX DAT payload has no data_check/poison/chunk_valid fields, so
    those are dropped; ``data`` is truncated/padded to the 256-bit beat.
    """
    data = _bytes_to_int(flit.data) if flit.data else 0
    byte_enable = _bytes_to_int(flit.byte_enable) if flit.byte_enable else 0
    values = {
        "qos": flit.qos,
        "tgtid": flit.tgtid,
        "srcid": flit.srcid,
        "txnid": flit.txnid,
        "home_nid": flit.home_nid,
        "opcode": flit.opcode,
        "resp_err": flit.resp_err,
        "resp": flit.resp,
        "data_source": flit.data_source,
        "dbid": flit.dbid,
        "ccid": flit.ccid,
        "data_id": flit.data_id,
        "trace_tag": int(bool(flit.trace_tag)),
        "byte_enable": byte_enable,
        "data": data,
    }
    return pack(RXDAT_LAYOUT, values, RXDAT_WIDTH)


def unpack_rxdat(word: int) -> RawDat:
    f = unpack(RXDAT_LAYOUT, word)
    return RawDat(
        qos=f["qos"],
        tgtid=f["tgtid"],
        srcid=f["srcid"],
        txnid=f["txnid"],
        home_nid=f["home_nid"],
        opcode=f["opcode"],
        resp_err=f["resp_err"],
        resp=f["resp"],
        data_source=f["data_source"],
        dbid=f["dbid"],
        ccid=f["ccid"],
        data_id=f["data_id"],
        trace_tag=bool(f["trace_tag"]),
        byte_enable=_int_to_bytes(f["byte_enable"], 32),
        data=_int_to_bytes(f["data"], 32),
    )


def unpack_txrsp(word: int) -> RawRsp:
    f = unpack(TXRSP_LAYOUT, word)
    return RawRsp(
        qos=f["qos"],
        tgtid=f["tgtid"],
        srcid=f["srcid"],
        txnid=f["txnid"],
        opcode=f["opcode"],
        dbid=f["dbid"],
        resp=f["resp"],
        resp_err=f["resp_err"],
        pcrdtype=f["pcrdtype"],
        forward_state=f["forward_state"],
        cbusy=f["cbusy"],
        tag_op=f["tag_op"],
        trace_tag=bool(f["trace_tag"]),
        data_event=f["data_event"],
    )


def pack_txrsp(flit: RawRsp) -> int:
    values = {
        "qos": flit.qos,
        "tgtid": flit.tgtid,
        "srcid": flit.srcid,
        "txnid": flit.txnid,
        "opcode": flit.opcode,
        "resp_err": flit.resp_err,
        "resp": flit.resp,
        "forward_state": flit.forward_state,
        "cbusy": flit.cbusy,
        "dbid": flit.dbid,
        "pcrdtype": flit.pcrdtype,
        "tag_op": flit.tag_op,
        "trace_tag": int(bool(flit.trace_tag)),
        "data_event": flit.data_event,
    }
    return pack(TXRSP_LAYOUT, values, TXRSP_WIDTH)


def unpack_txdat(word: int) -> RawDat:
    f = unpack(TXDAT_LAYOUT, word)
    return RawDat(
        qos=f["qos"],
        tgtid=f["tgtid"],
        srcid=f["srcid"],
        txnid=f["txnid"],
        home_nid=f["home_nid"],
        opcode=f["opcode"],
        resp_err=f["resp_err"],
        resp=f["resp"],
        data_source=f["data_source"],
        dbid=f["dbid"],
        ccid=f["ccid"],
        data_id=f["data_id"],
        trace_tag=bool(f["trace_tag"]),
        data=_int_to_bytes(f["data"], 32),
        chunk_valid=_int_to_bytes(f["chunk_valid"], 2),
    )


def pack_txdat(flit: RawDat) -> int:
    data = _bytes_to_int(flit.data) if flit.data else 0
    chunk_valid = _bytes_to_int(flit.chunk_valid) if flit.chunk_valid else 0
    values = {
        "qos": flit.qos,
        "tgtid": flit.tgtid,
        "srcid": flit.srcid,
        "txnid": flit.txnid,
        "home_nid": flit.home_nid,
        "opcode": flit.opcode,
        "resp_err": flit.resp_err,
        "resp": flit.resp,
        "data_source": flit.data_source,
        "dbid": flit.dbid,
        "ccid": flit.ccid,
        "data_id": flit.data_id,
        "trace_tag": int(bool(flit.trace_tag)),
        "data": data,
        "chunk_valid": chunk_valid,
    }
    return pack(TXDAT_LAYOUT, values, TXDAT_WIDTH)


# ---------------------------------------------------------------------------
# Convenience constructors for the two supported request types
# ---------------------------------------------------------------------------


def make_read_no_snp(
    txnid: int,
    addr: int,
    srcid: int,
    tgtid: int,
    size: int = 6,
    qos: int = 0,
    order: int = 0,
    memattr: int = 0,
    return_nid: Optional[int] = None,
    allow_retry: int = 1,
    exp_comp_ack: bool = False,
    **kwargs,
) -> RawReq:
    """Build a ReadNoSnp (0x04) request flit.

    ``size`` is the CHI encoded size (6 == 64 bytes); ``srcid``/``tgtid`` are
    the 11-bit physical node ids of the requester and of C2XM.
    """
    return RawReq(
        qos=qos,
        srcid=srcid,
        tgtid=tgtid,
        txnid=txnid,
        opcode=OPC_READNOSNP,
        addr=addr,
        size=size,
        return_nid=srcid if return_nid is None else return_nid,
        order=order,
        memattr=memattr,
        allow_retry=allow_retry,
        exp_comp_ack=exp_comp_ack,
        **kwargs,
    )


def make_write_no_snp(
    txnid: int,
    addr: int,
    srcid: int,
    tgtid: int,
    size: int = 6,
    qos: int = 0,
    order: int = 0,
    memattr: int = 0,
    return_nid: Optional[int] = None,
    allow_retry: int = 1,
    partial: bool = False,
    **kwargs,
) -> RawReq:
    """Build a WriteNoSnpFull (0x1C) / WriteNoSnpPtl (0x1D) request flit."""
    return RawReq(
        qos=qos,
        srcid=srcid,
        tgtid=tgtid,
        txnid=txnid,
        opcode=OPC_WRITENOSNP_PTL if partial else OPC_WRITENOSNP_FULL,
        addr=addr,
        size=size,
        return_nid=srcid if return_nid is None else return_nid,
        order=order,
        memattr=memattr,
        allow_retry=allow_retry,
        **kwargs,
    )


def make_write_data(
    txnid: int,
    data: int,
    srcid: int,
    tgtid: int,
    data_id: int = 0,
    dbid: int = 0,
    byte_enable: Optional[int] = None,
    opcode: int = 0x3,  # NonCopyBackWriteData
    resp: int = 0,
    last: int = 0,
    **kwargs,
) -> RawDat:
    """Build a non-copyback write data flit for a WriteNoSnp.

    ``data_id`` selects the 32-byte half of the 64-byte line: 0/1 -> bits
    [255:0], 2/3 -> bits [511:256] (see c2xm_core_receive_rxdat_operation.sv).
    ``data`` is a 256-bit little-endian beat.
    """
    if byte_enable is None:
        byte_enable = (1 << 32) - 1
    return RawDat(
        qos=kwargs.pop("qos", 0),
        srcid=srcid,
        tgtid=tgtid,
        txnid=txnid,
        opcode=opcode,
        home_nid=kwargs.pop("home_nid", tgtid),
        data_id=data_id,
        dbid=dbid,
        resp=resp,
        last=last,
        byte_enable=_int_to_bytes(byte_enable, 32),
        data=_int_to_bytes(data, 32),
        **kwargs,
    )
