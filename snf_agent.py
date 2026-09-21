"""SNF protocol agent: adapts the gem5 SN-F flit ABI to the DUT's CHI link.

This is the co-simulation counterpart of gem5's ``Chi2ClassicMemBridge``
(the real SNF).  It sits between the transport (gem5 flits as JSON) and the
existing CHI link driver, and owns every field the DUT wire format and the
gem5 model disagree on:

gem5 -> DUT (requests)
    * opcode  WriteNoSnpFull 0x5c (variant bit set) -> 0x1C the DUT admits
    * size    raw byte count (0 == whole line)       -> log2 encoding, 3 bits
    * txnid   uint32 (HNF allocates huge values)     -> pool slot number;
              the DUT looks write data up by ``txn_id[4:0] == pool slot``
              while its allocator picks the *lowest free* slot, so the
              agent models the allocator and issues requests in the same
              order the driver fires them on the link
    * AllowRetry -> 0  (a static request is never rejected with RetryAck,
              which the driver cannot replay yet, and which would poison
              gem5's pcrdtype matching)
    * order   -> 0  (the DUT would otherwise emit ReadReceipt RSP flits the
              gem5 HNF link layer panics on)
    * memattr -> 0  (EWA=0 keeps the write path DBIDResp -> data -> Comp,
              matching gem5's expectations)
    * one 64-byte write-data flit -> two 32-byte RXDAT beats (data_id 0/2)

DUT -> gem5 (responses)
    * srcid   nodeid(0)                    -> SNF node id
    * tgtid   HNF_ID_LIST[local]           -> requester srcid from the map
    * txnid   pool slot (12-bit wire)      -> gem5's 32-bit txnid
    * dbid    pool slot; the DUT's Comp carries dbid=0 -> forced to the
              DBIDResp's dbid on every response (gem5 requires
              Comp.dbid == DBIDResp.dbid)
    * dataid  0/2 halves                   -> sequential 0/1 with beatOffset
              and last, byteEnable/chunkValid all-ones, data trimmed to the
              request size (the gem5 HNF checks beat geometry and panics)

Pool-slot model
    The DUT transaction pool has 32 entries, but only *writes* (and reads
    with an address dependency on an in-flight pool entry) actually occupy
    one -- independent reads bypass straight to AXI AR through the 64-deep
    read-response queue (``s_entry_axi_ar_enable`` in the admit operation)
    and never enter the pool.  Writes allocate the lowest free slot in
    admission order; the DBIDResp carries that slot as its dbid and the
    RXDAT path looks the write up by ``txn_id[4:0] == slot``.

    The agent mirrors this: writes and address-dependent reads take
    modelled pool slots (0..31) as their wire txnid; independent reads use
    a monotonic id >= 32 that never collides with pool slots.  Slots free
    for reuse RELEASE_MARGIN cycles past the observed completion, which is
    conservative against pipeline drain.  Because gem5 silently drops a
    DBIDResp with dbid==0 and slot 0 is perfectly usable, the dbid
    forwarded to gem5 is ``slot + 1`` (the DUT itself never checks the
    echoed dbid on RXDAT).
"""


from __future__ import annotations


from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from chi_flit import (
    OPC_READNOSNP,
    OPC_WRITENOSNP_FULL,
    RawDat,
    RawReq,
    RawRsp,
    decode_rsp,
)
from chi_txn import (
    DAT_BEAT_BYTES,
    ChiTxn,
    ChiTxnItem,
    ChiWriteDataTxn,
    txn_from_req,
)

#: DUT transaction-pool size (c2xm_core_types_pkg TRANSACTION_POOL_ENTRIES).
POOL_SLOTS = 32
#: Wire txnids >= READ_ID_BASE never collide with pool slots; they are
#: used by independent reads, which bypass the pool entirely.
READ_ID_BASE = 32
#: Conservative release delay in DUT clock cycles past the observed
#: completion flit (covers the RTL's pipeline drain before the pool entry
#: is actually handed back to the allocator).
RELEASE_MARGIN_CYCLES = 8

#: gem5 SNF-side opcodes (Chi2ClassicMemTxnPolicy.hh).
GEM5_OPC_READNOSNP = 0x04
GEM5_OPC_WRITENOSNP_FULL = 0x5C
GEM5_OPC_NONCOPYBACK_WRITEDATA = 0x03

#: RSP opcodes on the DUT's TXRSP channel (issue_initial_write_response
#: and the retry/receipt path in admit_and_allocate).
OPC_COMP = 0x4
OPC_COMPDBIDRESP = 0x5
OPC_DBIDRESP = 0x6
#: Retry flow: the DUT only rejects ``AllowRetry=1`` requests; the agent
#: swallows these and replays the request itself (the real SNF never
#: retry-acks, so gem5 must not see them).
OPC_RETRYACK = 0x3
OPC_PCRDGRANT = 0x7

#: DUT RXDAT data_id values covering the low/high 32-byte halves.
DATA_ID_LOW = 0
DATA_ID_HIGH = 2


@dataclass
class TxnStats:
    """Latency statistics for one transaction kind, in DUT cycles."""

    count: int = 0
    total: int = 0
    min: Optional[int] = None
    max: int = 0

    def add(self, cycles: int) -> None:
        self.count += 1
        self.total += cycles
        if self.min is None or cycles < self.min:
            self.min = cycles
        if cycles > self.max:
            self.max = cycles

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0

    def summary(self, name: str) -> str:
        if not self.count:
            return f"{name}: none"
        return (f"{name}: n={self.count} avg={self.avg:.1f}cyc"
                f" min={self.min}cyc max={self.max}cyc")


def bytes_size_to_log2(size_bytes: int, line_bytes: int = 64) -> int:
    """gem5 raw byte count -> CHI log2 size; 0 means 'whole line'."""
    if size_bytes == 0:
        size_bytes = line_bytes
    log2 = (size_bytes - 1).bit_length()
    if (1 << log2) != size_bytes or log2 > 6:
        raise ValueError(f"unsupported transfer size {size_bytes} bytes")
    return log2


@dataclass
class TxnRecord:
    """One in-flight transaction as seen by the agent."""

    #: Identifier driven on the wire: pool slot for writes/dependent reads,
    #: a >= READ_ID_BASE id for independent reads.
    wire_id: int
    kind: str  # "read" | "write"
    gem5_srcid: int
    gem5_txnid: int
    addr: int
    size_bytes: int
    qos: int
    #: Original gem5 request, kept for RetryAck replays.
    orig_req: Optional[RawReq] = None
    #: DUT cycle the REQ arrived from gem5 (latency start).
    start_cycle: int = 0
    #: dbid forwarded to gem5 (wire_id+1: nonzero, constant per txn).
    fwd_dbid: int = 0
    #: Read accounting: 32-byte beats the DUT still has to emit.
    beats_left: int = 0
    #: gem5-side dataid for the next CompData beat.
    next_dataid: int = 0


class TxnIdMapper:
    """Mirror of the DUT's transaction-pool occupancy.

    ``records`` is keyed by the *wire txnid*: 0..31 are pool slots (writes
    and address-dependent reads), >= READ_ID_BASE are independent reads.
    """

    def __init__(self):
        self.records: Dict[int, TxnRecord] = {}
        self.cycle = 0
        self._pending_release: Deque[tuple] = deque()
        self._next_read_id = READ_ID_BASE
        self.alloc_failed = 0

    def on_cycle(self) -> None:
        self.cycle += 1
        while self._pending_release and self._pending_release[0][0] <= self.cycle:
            _, wire_id = self._pending_release.popleft()
            self.records.pop(wire_id, None)

    def pool_hazard_addrs(self) -> set:
        """64-byte aligned addresses currently held in the pool."""
        return {
            record.addr & ~0x3F
            for record in self.records.values()
            if record.wire_id < POOL_SLOTS
        }

    def alloc(self, kind: str, dependent: bool, **kw) -> Optional[TxnRecord]:
        """Allocate a record, or None when the modelled pool is full."""
        if kind == "read" and not dependent:
            wire_id = self._next_read_id
            self._next_read_id += 1
            if self._next_read_id >= 4096:
                self._next_read_id = READ_ID_BASE
        else:
            wire_id = None
            for slot in range(POOL_SLOTS):
                if slot not in self.records:
                    wire_id = slot
                    break
            if wire_id is None:
                self.alloc_failed += 1
                return None
        record = TxnRecord(wire_id=wire_id, kind=kind, **kw)
        self.records[wire_id] = record
        return record

    def release(self, wire_id: int,
                margin: int = RELEASE_MARGIN_CYCLES) -> None:
        if wire_id in self.records:
            self._pending_release.append((self.cycle + margin, wire_id))

    def by_wire_id(self, wire_id: int) -> Optional[TxnRecord]:
        # RX flits carry a 12-bit txnid; the agent issued every value, so a
        # truncation bug on our side would surface as an unknown id.
        return self.records.get(wire_id)

    def find_write(self, srcid: int, txnid: int) -> Optional[TxnRecord]:
        for record in self.records.values():
            if (
                record.kind == "write"
                and record.gem5_srcid == srcid
                and record.gem5_txnid == txnid
            ):
                return record
        return None

    @property
    def in_flight(self) -> int:
        return len(self.records)


class SnfAgent(pyuvm.uvm_component):
    """gem5 SN-F ABI adapter feeding the existing CHI link driver.

    ``handle_gem5_req`` / ``handle_gem5_dat`` are called synchronously by
    the runtime's dispatcher; transactions queue up here and flow to the
    driver through ``item_port`` from the agent's own run_phase.
    """

    def __init__(self, name, parent):
        super().__init__(name, parent)
        #: Callback the runtime installs: send one protocol message dict.
        self.send_cb = None
        #: gem5-visible node ids (from the hello handshake / tb_config).
        self.snf_id = 0
        self.dut_nodeid = 0
        self.line_bytes = 64
        self.mapper = TxnIdMapper()
        #: Transactions bound for the driver, in issue order.
        self._txn_queue: Deque[ChiTxn] = deque()
        #: gem5 requests waiting for a pool slot: (flit, arrival_cycle).
        self._pending_reqs: Deque[tuple] = deque()
        #: Per-kind latency statistics (REQ arrival -> completion).
        self.read_stats = TxnStats()
        self.write_stats = TxnStats()
        #: Requests the DUT RetryAck'd, waiting for a PCrdGrant.
        self._retrying: Deque[TxnRecord] = deque()
        self.stats = {
            "req_in": 0, "dat_in": 0, "req_stalled": 0, "dat_dropped": 0,
            "rsp_out": 0, "dat_out": 0, "unsupported": 0,
        }

    def build_phase(self):
        self.item_port = pyuvm.uvm_blocking_put_port("item_port", self)
        self.ap = pyuvm.uvm_analysis_port("ap", self)

    async def run_phase(self):
        while True:
            while self._txn_queue:
                txn = self._txn_queue.popleft()
                await self.item_port.put(ChiTxnItem(txn))
                self.ap.write(txn)
            # Park until the clock ticks (or the sim idles between barriers).
            await FallingEdge(cocotb.top.clk)

    # ------------------------------------------------------------------
    # runtime hooks
    # ------------------------------------------------------------------
    def on_cycle(self) -> None:
        self.mapper.on_cycle()
        self._retry_pending()

    def send_to_gem5(self, msg: Dict) -> None:
        if self.send_cb is not None:
            self.send_cb(msg)

    # ------------------------------------------------------------------
    # gem5 -> DUT
    # ------------------------------------------------------------------
    def handle_gem5_req(self, payload: Dict) -> None:
        """A gem5 REQ flit arrived (JSON payload, gem5 field names)."""
        req = self._req_from_payload(payload)
        if req is None:
            return
        self.stats["req_in"] += 1
        self._pending_reqs.append((req, self.mapper.cycle))
        self._retry_pending()

    def _retry_pending(self) -> None:
        while self._pending_reqs:
            req, arrival = self._pending_reqs[0]
            if self._issue_req(req, arrival):
                self._pending_reqs.popleft()
            else:
                self.stats["req_stalled"] += 1
                return

    def _issue_req(self, gem5_req: RawReq, arrival_cycle: int = 0) -> bool:
        """Rewrite one gem5 request onto the DUT wire; False when stalled."""
        try:
            size_log2 = bytes_size_to_log2(gem5_req.size, self.line_bytes)
        except ValueError as exc:
            self.stats["unsupported"] += 1
            self.logger.warning(f"snf: {exc}; dropping request")
            return True  # consumed; do not stall the queue

        if gem5_req.opcode == GEM5_OPC_WRITENOSNP_FULL:
            opcode, kind = OPC_WRITENOSNP_FULL, "write"
        elif gem5_req.opcode == GEM5_OPC_READNOSNP:
            opcode, kind = OPC_READNOSNP, "read"
        else:
            self.stats["unsupported"] += 1
            self.logger.warning(
                f"snf: unsupported REQ opcode 0x{gem5_req.opcode:02x}"
                f" from node 0x{gem5_req.srcid:x}; dropping"
            )
            return True

        size_bytes = 1 << size_log2
        # Reads whose 64-byte line collides with a live pool entry take the
        # dependency path and occupy a pool slot themselves; independent
        # reads bypass the pool (see TxnIdMapper).
        dependent = (gem5_req.addr & ~0x3F) in self.mapper.pool_hazard_addrs()
        record = self.mapper.alloc(
            kind,
            dependent=dependent,
            gem5_srcid=gem5_req.srcid,
            gem5_txnid=gem5_req.txnid,
            addr=gem5_req.addr,
            size_bytes=size_bytes,
            qos=gem5_req.qos,
            beats_left=max(1, size_bytes // DAT_BEAT_BYTES),
            orig_req=gem5_req,
        )
        if record is None:
            return False  # modelled pool full: keep the request queued
        record.start_cycle = arrival_cycle

        wire = RawReq(
            qos=gem5_req.qos,
            srcid=gem5_req.srcid,
            tgtid=self.dut_nodeid,
            txnid=record.wire_id,
            opcode=opcode,
            addr=gem5_req.addr,
            size=size_log2,
            return_nid=gem5_req.return_nid if gem5_req.return_nid else gem5_req.srcid,
            return_txn_id=record.wire_id,
            order=0,          # no ReadReceipt on the way back
            memattr=0,        # EWA=0: DBIDResp -> data -> Comp
            # AllowRetry=1: the DUT's static (AllowRetry=0) admission path
            # stalls forever waiting for a preorder candidate, while the
            # dynamic-m path is the one the standalone tests exercise.  The
            # agent absorbs any resulting RetryAck/PCrdGrant itself.
            allow_retry=1,
            trace_tag=gem5_req.trace_tag,
        )
        try:
            txn = txn_from_req(wire)
        except ValueError as exc:
            self.mapper.release(record.wire_id, margin=0)
            self.stats["unsupported"] += 1
            self.logger.warning(f"snf: {exc}")
            return True

        self.logger.info(
            f"snf: wire_id={record.wire_id} {kind}"
            f"{' (dep)' if dependent else ''} gem5_txn=0x{gem5_req.txnid:x}"
            f" node=0x{gem5_req.srcid:x} addr=0x{gem5_req.addr:x}"
            f" size={size_bytes}B"
        )
        self._txn_queue.append(txn)
        return True

    def handle_gem5_dat(self, payload: Dict) -> None:
        """A gem5 write-data flit arrived: split into DUT RXDAT beats."""
        self.stats["dat_in"] += 1
        flit = self._dat_from_payload(payload)
        record = self.mapper.find_write(flit.srcid, flit.txnid)
        if record is None:
            # gem5 only sends data after our DBIDResp, so this means the
            # request was dropped or the slot model diverged.
            self.stats["dat_dropped"] += 1
            self.logger.warning(
                f"snf: write data for unknown txn"
                f" (src=0x{flit.srcid:x} txn=0x{flit.txnid:x}); dropping"
            )
            return

        data = bytes(flit.data)
        # The DUT never checks the echoed dbid on RXDAT (the pool lookup is
        # by txn_id only); forward gem5's value through untouched.
        dbid = flit.dbid
        for data_id, beat in self._split_write_data(record, data):
            wire = RawDat(
                qos=flit.qos,
                srcid=flit.srcid,
                tgtid=self.dut_nodeid,
                txnid=record.wire_id,
                opcode=GEM5_OPC_NONCOPYBACK_WRITEDATA,
                home_nid=record.gem5_srcid,
                data_id=data_id,
                dbid=dbid,
                resp=0,
                last=0,
                byte_enable=[0xFF] * DAT_BEAT_BYTES,
                data=list(beat),
            )
            txn = ChiWriteDataTxn(
                txn_id=record.wire_id,
                addr=record.addr,
                opcode=wire.opcode,
                size=(record.size_bytes - 1).bit_length(),
                qos=flit.qos,
                src_id=flit.srcid,
                tgt_id=self.dut_nodeid,
                data=[wire],
            )
            self._txn_queue.append(txn)
        self.logger.info(
            f"snf: wire_id={record.wire_id} write data split into"
            f" {len(data) // DAT_BEAT_BYTES if len(data) > DAT_BEAT_BYTES else 1}"
            f" beat(s) dbid={dbid}"
        )

    @staticmethod
    def _split_write_data(record: TxnRecord, data: bytes):
        """(data_id, 32-byte beat) pairs for one gem5 write-data flit."""
        if len(data) > DAT_BEAT_BYTES:
            return [
                (DATA_ID_LOW, data[:DAT_BEAT_BYTES]),
                (DATA_ID_HIGH, data[DAT_BEAT_BYTES:DAT_BEAT_BYTES * 2]),
            ]
        data_id = DATA_ID_HIGH if (record.addr >> 5) & 1 else DATA_ID_LOW
        beat = data.ljust(DAT_BEAT_BYTES, b"\x00")
        return [(data_id, beat)]

    # ------------------------------------------------------------------
    # DUT -> gem5
    # ------------------------------------------------------------------
    def on_txrsp(self, flit: RawRsp) -> None:
        """The CHI monitor saw a TXRSP flit (wire txnid == pool slot)."""
        # DUT-internal retry flow: swallow and replay ourselves.  The real
        # SNF never rejects, so gem5 must never see these.
        if flit.opcode == OPC_RETRYACK:
            record = self.mapper.by_wire_id(flit.txnid)
            if record is not None:
                self.logger.info(
                    f"snf: RetryAck slot={record.wire_id}"
                    f" gem5_txn=0x{record.gem5_txnid:x}; will replay on"
                    " PCrdGrant"
                )
                self.mapper.release(record.wire_id, margin=0)
                self._retrying.append(record)
            return
        if flit.opcode == OPC_PCRDGRANT:
            if self._retrying:
                record = self._retrying.popleft()
                self.logger.info(
                    f"snf: PCrdGrant -> replay gem5_txn=0x{record.gem5_txnid:x}"
                )
                if record.orig_req is not None:
                    self._pending_reqs.append(
                        (record.orig_req, record.start_cycle))
                    self._retry_pending()
            return

        record = self.mapper.by_wire_id(flit.txnid)
        if record is None:
            self.logger.warning(
                f"snf: TXRSP opcode=0x{flit.opcode:x} for unknown slot"
                f" {flit.txnid}; dropping"
            )
            return

        if flit.opcode in (OPC_DBIDRESP, OPC_COMPDBIDRESP):
            record.fwd_dbid = record.wire_id + 1  # nonzero for gem5
        elif flit.opcode == OPC_COMP:
            self._complete(record)
            self.mapper.release(record.wire_id)
        else:
            self.logger.warning(
                f"snf: unexpected TXRSP opcode 0x{flit.opcode:x}"
                f" ({decode_rsp(flit.opcode).minor.name}) for slot"
                f" {record.wire_id}; dropping"
            )
            return
        if flit.opcode == OPC_COMPDBIDRESP:
            self._complete(record)
            self.mapper.release(record.wire_id)

        payload = {
            "qos": record.qos,
            "srcid": self.snf_id,
            "tgtid": record.gem5_srcid,
            "txnid": record.gem5_txnid,
            "opcode": flit.opcode,
            # dbid the DBIDResp was tagged with (wire_id+1); Comp must echo
            # the same value or gem5 drops the completion.
            "dbid": record.fwd_dbid,
            "resp": 1,             # RespSC
            "respErr": 0,
            "pcrdtype": 0,
            "rspKind": 0,          # MainPath
        }
        self.stats["rsp_out"] += 1
        self.logger.info(
            f"snf: TXRSP->gem5 {decode_rsp(flit.opcode).minor.name}"
            f" slot={record.wire_id} gem5_txn=0x{record.gem5_txnid:x}"
            f" dbid={payload['dbid']}"
        )
        self.send_to_gem5({"t": "flit", "ch": "rsp", "dir": "t2g",
                           "flit": payload})

    def on_txdat(self, flit: RawDat) -> None:
        """The CHI monitor saw a TXDAT CompData beat (wire txnid == slot)."""
        record = self.mapper.by_wire_id(flit.txnid)
        if record is None or record.kind != "read":
            self.logger.warning(
                f"snf: TXDAT for unknown/closed slot {flit.txnid}; dropping"
            )
            return

        # DUT halves 0/2 -> gem5 sequential dataid 0/1 with beat geometry.
        dataid = record.next_dataid
        record.next_dataid += 1
        remaining = record.size_bytes - dataid * DAT_BEAT_BYTES
        beat_len = max(0, min(DAT_BEAT_BYTES, remaining))
        data = bytes(flit.data[:DAT_BEAT_BYTES])[:beat_len]

        record.beats_left -= 1
        last = 1 if record.beats_left <= 0 else 0
        payload = {
            "qos": record.qos,
            "srcid": self.snf_id,
            "tgtid": record.gem5_srcid,
            "txnid": record.gem5_txnid,
            "opcode": 0x4,         # CompData
            "last": last,
            "HomeNID": record.gem5_srcid,
            "dbid": 0,
            "dataid": dataid,
            "resp": 1,             # RespSC, constant across beats
            "respErr": 0,
            "beatOffset": dataid * DAT_BEAT_BYTES,
            "byteEnable": (b"\x01" * beat_len).hex(),
            "chunkValid": (b"\x01" * ((beat_len + 7) // 8)).hex(),
            "data": data.hex(),
        }
        self.stats["dat_out"] += 1
        self.logger.info(
            f"snf: TXDAT->gem5 CompData slot={record.wire_id}"
            f" gem5_txn=0x{record.gem5_txnid:x} dataid={dataid}"
            f" len={beat_len} last={last}"
        )
        self.send_to_gem5({"t": "flit", "ch": "dat", "dir": "t2g",
                           "flit": payload})
        if last:
            self._complete(record)
            self.mapper.release(record.wire_id)

    # ------------------------------------------------------------------
    # payload conversion
    # ------------------------------------------------------------------
    def _req_from_payload(self, payload: Dict) -> Optional[RawReq]:
        try:
            return RawReq(
                qos=payload.get("qos", 0),
                srcid=payload.get("srcid", 0),
                tgtid=payload.get("tgtid", 0),
                txnid=payload.get("txnid", 0),
                opcode=payload.get("opcode", 0),
                allow_retry=payload.get("AllowRetry", 0),
                addr=payload.get("addr", 0),
                size=payload.get("size", 0),
                return_nid=payload.get("ReturnNid", 0),
                order=payload.get("order", 0),
                pcrdtype=payload.get("pcrdtype", 0),
                memattr=payload.get("memattr", 0),
                snpattr=payload.get("snpattr", 0),
                trace_tag=bool(payload.get("traceTag", False)),
            )
        except (KeyError, TypeError) as exc:
            self.logger.warning(f"snf: malformed REQ payload: {exc}")
            return None

    def _dat_from_payload(self, payload: Dict) -> RawDat:
        return RawDat(
            qos=payload.get("qos", 0),
            srcid=payload.get("srcid", 0),
            tgtid=payload.get("tgtid", 0),
            txnid=payload.get("txnid", 0),
            opcode=payload.get("opcode", 0),
            dbid=payload.get("dbid", 0),
            data=list(bytes.fromhex(payload.get("data", ""))),
            byte_enable=list(
                bytes.fromhex(payload.get("byteEnable", "") or "ff" * 64)
            ),
        )

    def _complete(self, record: TxnRecord) -> None:
        """Record one transaction's latency at its completion point."""
        latency = self.mapper.cycle - record.start_cycle
        if record.kind == "read":
            self.read_stats.add(latency)
        else:
            self.write_stats.add(latency)
        self.logger.info(
            f"snf: {record.kind} gem5_txn=0x{record.gem5_txnid:x} done"
            f" latency={latency}cyc"
        )

    def latency_report(self) -> str:
        return (f"{self.read_stats.summary('ReadNoSnp ')}"
                f" | {self.write_stats.summary('WriteNoSnp')}")

    def final_phase(self):
        self.logger.info(f"snf: latency: {self.latency_report()}")
        self.logger.info(
            f"snf: {self.stats} in_flight={self.mapper.in_flight}"
            f" alloc_failed={self.mapper.alloc_failed}"
        )
