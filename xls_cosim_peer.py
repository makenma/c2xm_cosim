#!/usr/bin/env python3
"""Run C2XM as an XLS Proc network against the gem5 CHI co-sim bridge.

This is the Proc-IR counterpart of the cocotb/RTL peer.  The C++ helper owns
the actual XLS JIT state; this file only translates the existing JSON co-sim
ABI to C2XM boundary-channel tuples and proxies C2XM's AXI traffic to gem5's
memory.  No C2XM behavior is reimplemented here.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import select
import socket
import subprocess
import sys
import time
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from cosim_protocol import PROTOCOL_VERSION, decode_msg, encode_msg

LINE_BYTES = 64
AXI_BEAT_BYTES = 32

OP_READ = 0x04
OP_WRITE_FULL_GEM5 = 0x5C
OP_WRITE_FULL_C2XM = 0x1C
OP_WRITE_PART_C2XM = 0x1D
OP_NCB_WRITE_DATA = 0x03
OP_RETRY_ACK = 0x03
OP_COMP = 0x04
OP_COMP_DBID = 0x05
OP_DBID = 0x06
OP_PCRD_GRANT = 0x07

CH_CONFIG = "_config_in"
CH_RXREQ = "_chi_rxreq"
CH_RXDAT = "_chi_rxdat"
CH_TXRSP = "_chi_txrsp"
CH_TXDAT = "_chi_txdat"
CH_AW = "_axi_aw"
CH_W = "_axi_w"
CH_B = "_axi_b"
CH_AR = "_axi_ar"
CH_R = "_axi_r"

_BITS = re.compile(r"bits\[\d+\]:(-?0x[0-9a-fA-F_]+|-?\d+)")


def xbits(width: int, value: int) -> str:
    mask = (1 << width) - 1
    return f"0x{value & mask:x}"


def xtuple(fields: Iterable[Tuple[int, int]]) -> str:
    return "(" + ", ".join(xbits(w, v) for w, v in fields) + ")"


def parse_xls_tuple(value: str, expected: int, channel: str) -> List[int]:
    values = [int(token.replace("_", ""), 0) for token in _BITS.findall(value)]
    if len(values) != expected:
        raise RuntimeError(
            f"{channel}: expected {expected} tuple fields, got {len(values)}: {value}"
        )
    return values


def bytes_size_to_log2(size: int) -> int:
    size = size or LINE_BYTES
    if size <= 0 or size > LINE_BYTES or size & (size - 1):
        raise ValueError(f"unsupported CHI transfer size {size}")
    return size.bit_length() - 1


class XlsRpc:
    def __init__(self, executable: str, ir: str):
        self.proc = subprocess.Popen(
            [executable, ir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self.proc.stdout.readline().rstrip("\n")
        if not ready.startswith("READY\t"):
            raise RuntimeError(f"XLS runtime did not become ready: {ready!r}")
        self.package = ready.split("\t", 1)[1]

    def command(self, *fields: object) -> List[str]:
        if self.proc.poll() is not None:
            raise RuntimeError(f"XLS runtime exited with status {self.proc.returncode}")
        self.proc.stdin.write("\t".join(str(v) for v in fields) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().rstrip("\n")
        parts = line.split("\t")
        if not parts or parts[0] == "ERR":
            raise RuntimeError(f"XLS RPC failed for {fields[0]}: {line}")
        return parts

    def write(self, channel: str, value: str) -> None:
        reply = self.command("write", channel, value)
        if reply != ["OK"]:
            raise RuntimeError(f"unexpected XLS write response: {reply}")

    def read(self, channel: str) -> Optional[str]:
        reply = self.command("read", channel)
        if reply == ["EMPTY"]:
            return None
        if len(reply) == 2 and reply[0] == "VALUE":
            return reply[1]
        raise RuntimeError(f"unexpected XLS read response: {reply}")

    def tick(self, count: int = 1) -> int:
        reply = self.command("tick", count)
        if len(reply) != 3 or reply[0] != "OK":
            raise RuntimeError(f"unexpected XLS tick response: {reply}")
        return int(reply[2])

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.command("quit")
            except (BrokenPipeError, RuntimeError):
                self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@dataclasses.dataclass
class Transaction:
    wire_id: int
    srcid: int
    gem5_txnid: int
    addr: int
    size: int
    qos: int
    kind: str
    request: dict
    started: int
    actual_dbid: Optional[int] = None
    forwarded_dbid: Optional[int] = None
    next_dataid: int = 0


class XlsCosimPeer:
    def __init__(self, args):
        self.args = args
        self.xls = XlsRpc(args.xls_runtime, args.ir)
        self.sock: Optional[socket.socket] = None
        self.rbuf = b""
        self.quantum = 0
        self.snf_id = 0
        self.hnf_id = 0
        self.proc_ticks = 0
        self.active_ticks = 0
        self.syncs = 0
        self.stop_requested = False
        self.next_wire_id = 1
        self.next_mem_id = 1
        self.by_wire: Dict[int, Transaction] = {}
        self.by_gem5: Dict[Tuple[int, int], Transaction] = {}
        self.retry_wait: Deque[Transaction] = deque()
        self.pending_mem: Dict[int, dict] = {}
        self.aw: Deque[dict] = deque()
        self.w_beats: List[Tuple[int, int]] = []
        self.stats = {
            "req": 0, "rxdat": 0, "txrsp": 0, "txdat": 0,
            "axi_reads": 0, "axi_writes": 0, "bypass": 0,
        }
        self.started = time.monotonic()

    def connect(self) -> None:
        deadline = time.monotonic() + self.args.connect_timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(self.args.socket)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out connecting to {self.args.socket}")
                time.sleep(0.25)

    def send(self, msg: dict) -> None:
        if self.args.verbose:
            print(f"[xls] >> {msg.get('t')} {msg.get('ch', '')}", flush=True)
        self.sock.sendall(encode_msg(msg))

    def recv(self, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.rbuf:
            left = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([self.sock], [], [], left)
            if not readable:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                return {"t": "peer_closed"}
            self.rbuf += chunk
        line, self.rbuf = self.rbuf.split(b"\n", 1)
        return decode_msg(line) if line else self.recv(timeout)

    def configure(self) -> None:
        fields = [(11, self.snf_id), (8, 16), (8, 16), (8, 16), (8, 16), (32, 0)]
        for _ in range(6):
            fields.extend(((1, 0), (8, 0)))
        self.xls.write(CH_CONFIG, xtuple(fields))

    def alloc_txn(self, f: dict, kind: str, size: int) -> Transaction:
        for _ in range(4095):
            wire = self.next_wire_id
            self.next_wire_id = 1 if wire == 4095 else wire + 1
            if wire not in self.by_wire:
                break
        else:
            raise RuntimeError("all 4095 XLS transaction identifiers are occupied")
        key = (int(f.get("srcid", 0)), int(f.get("txnid", 0)))
        if key in self.by_gem5:
            raise RuntimeError(f"duplicate gem5 transaction {key}")
        txn = Transaction(
            wire, key[0], key[1], int(f.get("addr", 0)), size,
            int(f.get("qos", 0)), kind, dict(f), self.proc_ticks,
        )
        self.by_wire[wire] = txn
        self.by_gem5[key] = txn
        return txn

    def retire(self, txn: Transaction) -> None:
        self.by_wire.pop(txn.wire_id, None)
        self.by_gem5.pop((txn.srcid, txn.gem5_txnid), None)

    def write_req(self, txn: Transaction, allow_retry: int) -> None:
        f = txn.request
        opcode = OP_READ if txn.kind == "read" else (
            OP_WRITE_PART_C2XM if f.get("opcode") == OP_WRITE_PART_C2XM
            else OP_WRITE_FULL_C2XM
        )
        # gem5 memattr is an encoded scalar; the current bridge policy uses
        # non-cacheable, non-EWA requests, matching the prior RTL adapter.
        value = xtuple([
            (4, txn.qos), (11, f.get("tgtid", self.snf_id)),
            (11, txn.srcid), (12, txn.wire_id),
            (11, f.get("ReturnNid", 0) or txn.srcid), (12, txn.wire_id),
            (7, opcode), (3, bytes_size_to_log2(txn.size)), (48, txn.addr),
            (1, 0), (1, allow_retry), (2, 0),
            (1, 0), (1, 0), (1, 0), (1, 0),
            (1, f.get("traceTag", 0)), (4, 0),
        ])
        self.xls.write(CH_RXREQ, value)

    def on_req(self, f: dict) -> None:
        opcode = int(f.get("opcode", -1))
        if opcode not in (OP_READ, OP_WRITE_FULL_GEM5, OP_WRITE_PART_C2XM):
            raise RuntimeError(f"unsupported gem5 REQ opcode 0x{opcode:x}")
        size = int(f.get("size", 0)) or LINE_BYTES
        bytes_size_to_log2(size)
        txn = self.alloc_txn(f, "read" if opcode == OP_READ else "write", size)
        self.write_req(txn, allow_retry=1)
        self.stats["req"] += 1

    def on_dat(self, f: dict) -> None:
        key = (int(f.get("srcid", 0)), int(f.get("txnid", 0)))
        txn = self.by_gem5.get(key)
        if txn is None or txn.kind != "write":
            raise RuntimeError(f"write data for unknown transaction {key}")
        if txn.actual_dbid is None:
            raise RuntimeError(f"write data arrived before DBIDResp for {key}")
        data = bytes.fromhex(f.get("data", ""))
        enables = bytes.fromhex(f.get("byteEnable", "")) if f.get("byteEnable") else b""
        if len(data) < txn.size:
            raise RuntimeError(f"short write data: {len(data)} < {txn.size}")
        halves = [(0, data[:32], enables[:32])]
        if txn.size > 32:
            halves.append((2, data[32:64], enables[32:64]))
        elif (txn.addr >> 5) & 1:
            halves[0] = (2, data[:32], enables[:32])
        for data_id, beat, beat_en in halves:
            beat = beat.ljust(32, b"\x00")
            if not beat_en:
                beat_en = b"\x01" * min(32, txn.size)
            strobe = sum((1 << i) for i, enabled in enumerate(beat_en[:32]) if enabled)
            self.xls.write(CH_RXDAT, xtuple([
                (256, int.from_bytes(beat, "little")), (32, strobe),
                (2, data_id), (12, txn.actual_dbid),
                (4, f.get("opcode", OP_NCB_WRITE_DATA)),
                (1, f.get("traceTag", 0)),
            ]))
        self.stats["rxdat"] += 1

    def send_mem_read(self, addr: int, length: int, context: dict) -> None:
        mid = self.next_mem_id
        self.next_mem_id += 1
        self.pending_mem[mid] = context
        self.send({"t": "mem_read", "id": mid, "addr": addr, "len": length})

    def send_mem_write(self, addr: int, data: bytes, strobe: bytes,
                       context: dict) -> None:
        mid = self.next_mem_id
        self.next_mem_id += 1
        self.pending_mem[mid] = context
        self.send({"t": "mem_write", "id": mid, "addr": addr,
                   "data": data.hex(), "strb": strobe.hex()})

    def on_mem_result(self, msg: dict) -> None:
        context = self.pending_mem.pop(int(msg.get("id", -1)), None)
        if context is None:
            raise RuntimeError(f"memory response for unknown id {msg.get('id')}")
        kind = context["kind"]
        if kind == "axi_r":
            ar = context["ar"]
            data = bytes.fromhex(msg.get("data", ""))
            nbeats = ar[2] + 1
            for beat in range(nbeats):
                chunk = data[beat * 32:(beat + 1) * 32].ljust(32, b"\x00")
                self.xls.write(CH_R, xtuple([
                    (256, int.from_bytes(chunk, "little")),
                    (2, msg.get("resp", 0)), (1, beat == nbeats - 1),
                    (11, ar[3]),
                ]))
        elif kind == "axi_b":
            self.xls.write(CH_B, xtuple([
                (11, context["id"]), (2, msg.get("resp", 0)),
            ]))
        elif kind == "bypass_r":
            self.send({"t": "bypass_resp", "pkt": context["pkt"],
                       "data": msg.get("data", ""), "resp": msg.get("resp", 0)})
        elif kind == "bypass_w":
            self.send({"t": "bypass_resp", "pkt": context["pkt"],
                       "data": "", "resp": msg.get("resp", 0)})
        else:
            raise RuntimeError(f"unknown memory context {kind}")

    def on_aw(self, values: List[int]) -> None:
        address, length, size, burst, qos, cache, prot, xid = values
        if burst not in (1, 2) or size > 5:
            raise RuntimeError(f"unsupported AXI AW burst={burst} size={size}")
        self.aw.append({"addr": address, "len": length, "size": size, "id": xid})

    def on_w(self, values: List[int]) -> None:
        data, strobe, last = values
        self.w_beats.append((data, strobe))
        if not last:
            return
        if not self.aw:
            raise RuntimeError("AXI W completed without an AW")
        aw = self.aw.popleft()
        expected = aw["len"] + 1
        if len(self.w_beats) != expected:
            raise RuntimeError(f"AXI W beat count {len(self.w_beats)} != {expected}")
        nbytes = expected * (1 << aw["size"])
        data_out = bytearray()
        strobe_out = bytearray()
        for data_word, strobe_bits in self.w_beats:
            data_out.extend(data_word.to_bytes(32, "little"))
            strobe_out.extend(0xFF if (strobe_bits >> i) & 1 else 0 for i in range(32))
        self.w_beats.clear()
        self.send_mem_write(
            aw["addr"], bytes(data_out[:nbytes]), bytes(strobe_out[:nbytes]),
            {"kind": "axi_b", "id": aw["id"]},
        )
        self.stats["axi_writes"] += 1

    def on_ar(self, values: List[int]) -> None:
        qos, lock, length, xid, prot, cache, burst, size, address = values
        if lock or burst not in (1, 2) or size > 5:
            raise RuntimeError(f"unsupported AXI AR lock={lock} burst={burst} size={size}")
        nbytes = (length + 1) * (1 << size)
        self.send_mem_read(address, nbytes, {"kind": "axi_r", "ar": values})
        self.stats["axi_reads"] += 1

    def on_txrsp(self, v: List[int]) -> None:
        (qos, local_target, raw_src, wire, opcode, resp_err, raw_resp,
         fwd_state, cbusy, dbid, pcrd_type, tag_op, trace, data_event) = v
        if opcode == OP_PCRD_GRANT:
            if not self.retry_wait:
                raise RuntimeError("PCrdGrant without a pending RetryAck")
            self.write_req(self.retry_wait.popleft(), allow_retry=0)
            return
        txn = self.by_wire.get(wire)
        if txn is None:
            raise RuntimeError(f"TXRSP opcode 0x{opcode:x} for unknown wire id {wire}")
        if opcode == OP_RETRY_ACK:
            self.retry_wait.append(txn)
            return
        if opcode not in (OP_COMP, OP_COMP_DBID, OP_DBID):
            raise RuntimeError(f"unexpected C2XM TXRSP opcode 0x{opcode:x}")
        if opcode in (OP_COMP_DBID, OP_DBID):
            txn.actual_dbid = dbid
            txn.forwarded_dbid = dbid + 1
        forwarded_dbid = txn.forwarded_dbid or 0
        self.send({"t": "flit", "ch": "rsp", "dir": "t2g", "flit": {
            "qos": qos, "srcid": self.snf_id, "tgtid": txn.srcid,
            "txnid": txn.gem5_txnid, "opcode": opcode,
            "dbid": forwarded_dbid,
            # C2XM uses CHI response encoding 0 for these non-snoop paths;
            # gem5's bridge expects RespSC=1. Error status is preserved.
            "resp": 1, "respErr": resp_err, "pcrdtype": pcrd_type,
            "rspKind": 0,
        }})
        self.stats["txrsp"] += 1
        if opcode in (OP_COMP, OP_COMP_DBID):
            self.retire(txn)

    def on_txdat(self, v: List[int]) -> None:
        (resp_err, poison, qos, raw_target, raw_src, wire, home, opcode,
         raw_resp, data_source, dbid, ccid, raw_dataid, trace, data,
         data_check, chunk_valid) = v
        txn = self.by_wire.get(wire)
        if txn is None or txn.kind != "read":
            raise RuntimeError(f"TXDAT for unknown/non-read wire id {wire}")
        dataid = txn.next_dataid
        txn.next_dataid += 1
        remaining = txn.size - dataid * AXI_BEAT_BYTES
        beat_len = min(AXI_BEAT_BYTES, max(0, remaining))
        payload = data.to_bytes(32, "little")[:beat_len]
        last = int(remaining <= AXI_BEAT_BYTES)
        self.send({"t": "flit", "ch": "dat", "dir": "t2g", "flit": {
            "qos": qos, "srcid": self.snf_id, "tgtid": txn.srcid,
            "txnid": txn.gem5_txnid, "opcode": opcode, "last": last,
            # Restore gem5's identity convention, as the RTL peer does:
            # C2XM emits its local home ID and the original request TxnID.
            "HomeNID": txn.srcid, "dbid": 0, "dataid": dataid,
            "resp": 1, "respErr": resp_err, "beatOffset": dataid * 32,
            "byteEnable": (b"\x01" * beat_len).hex(),
            "chunkValid": (b"\x01" * ((beat_len + 7) // 8)).hex(),
            "data": payload.hex(),
        }})
        self.stats["txdat"] += 1
        if last:
            self.retire(txn)

    def drain_outputs(self) -> None:
        specs = (
            (CH_AW, 8, self.on_aw), (CH_W, 3, self.on_w),
            (CH_AR, 9, self.on_ar), (CH_TXRSP, 14, self.on_txrsp),
            (CH_TXDAT, 17, self.on_txdat),
        )
        for channel, count, handler in specs:
            while True:
                value = self.xls.read(channel)
                if value is None:
                    break
                handler(parse_xls_tuple(value, count, channel))

    def handle(self, msg: dict, during_quantum: bool = False) -> Optional[str]:
        kind = msg.get("t")
        if kind == "flit":
            if msg.get("dir") != "g2t":
                raise RuntimeError(f"unexpected flit direction {msg.get('dir')}")
            if msg.get("ch") == "req":
                self.on_req(msg.get("flit", {}))
            elif msg.get("ch") == "dat":
                self.on_dat(msg.get("flit", {}))
            else:
                raise RuntimeError(f"unsupported gem5 flit channel {msg.get('ch')}")
        elif kind in ("mem_data", "mem_resp"):
            self.on_mem_result(msg)
        elif kind == "bypass_read":
            self.stats["bypass"] += 1
            self.send_mem_read(msg["addr"], msg["len"],
                               {"kind": "bypass_r", "pkt": msg["pkt"]})
        elif kind == "bypass_write":
            self.stats["bypass"] += 1
            data = bytes.fromhex(msg.get("data", ""))
            strobe = bytes.fromhex(msg.get("strb", ""))
            self.send_mem_write(msg["addr"], data, strobe,
                                {"kind": "bypass_w", "pkt": msg["pkt"]})
        elif kind in ("bye", "peer_closed"):
            return kind
        elif kind == "sync":
            if during_quantum:
                raise RuntimeError("received nested sync before acknowledging current sync")
            self.advance(int(msg.get("cycle", 0)), int(msg.get("tick", 0)))
        else:
            raise RuntimeError(f"unknown co-sim message {kind!r}")
        return None

    def pump_available(self) -> Optional[str]:
        while True:
            msg = self.recv(0)
            if msg is None:
                return None
            terminal = self.handle(msg, during_quantum=True)
            if terminal:
                return terminal

    def advance(self, gem5_cycle: int, gem5_tick: int) -> None:
        for _ in range(self.quantum):
            if self.pump_available():
                raise RuntimeError("gem5 terminated during a sync quantum")
            self.active_ticks += self.xls.tick(1)
            self.proc_ticks += 1
            self.drain_outputs()
            if self.pump_available():
                raise RuntimeError("gem5 terminated during a sync quantum")
        self.syncs += 1
        if self.args.max_syncs and self.syncs >= self.args.max_syncs:
            # End at the current barrier; an ack would let gem5 run ahead
            # before it processes bye. Keep the socket open until it exits.
            self.stop_requested = True
            self.send({"t": "bye", "reason": "XLS sync budget reached"})
        else:
            self.send({"t": "ack", "cycle": gem5_cycle})
        if self.syncs % self.args.progress_every == 0:
            elapsed = time.monotonic() - self.started
            print(
                f"[xls] syncs={self.syncs} proc_ticks={self.proc_ticks} "
                f"wall={elapsed:.1f}s in_flight={len(self.by_wire)} {self.stats}",
                flush=True,
            )

    def run(self) -> int:
        self.connect()
        hello = self.recv(30)
        if hello is None or hello.get("t") != "hello":
            raise RuntimeError(f"expected gem5 hello, got {hello}")
        if int(hello.get("ver", -1)) != PROTOCOL_VERSION:
            raise RuntimeError(f"protocol version mismatch: {hello.get('ver')}")
        self.quantum = int(hello.get("quantum_cycles", 0))
        self.snf_id = int(hello.get("snf_id", 0))
        self.hnf_id = int(hello.get("hnf_id", 0))
        if self.quantum <= 0:
            raise RuntimeError(f"invalid quantum {self.quantum}")
        self.configure()
        self.send({"t": "hello_ack", "ver": PROTOCOL_VERSION,
                   "clk_period_ns": hello.get("clk_period_ns", 0)})
        print(
            f"[xls] ready package={self.xls.package} quantum={self.quantum} "
            f"snf=0x{self.snf_id:x} hnf=0x{self.hnf_id:x}", flush=True,
        )
        deadline = time.monotonic() + self.args.max_time
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"co-simulation exceeded {self.args.max_time}s")
            msg = self.recv(10)
            if msg is None:
                continue
            terminal = self.handle(msg)
            if terminal:
                if not self.stop_requested and (self.by_wire or self.pending_mem):
                    raise RuntimeError("peer exited with unfinished transactions")
                # The real gem5 bridge closes on m5_exit without sending bye.
                # The launcher must also check gem5's exit status and log.
                print(f"[xls] transport ended: {terminal}; launcher verifies peer result",
                      flush=True)
                break
            if self.stop_requested:
                print("[xls] stopped at requested sync budget (incomplete workload)", flush=True)
                while True:
                    msg = self.recv(30)
                    if msg is None:
                        raise TimeoutError("gem5 did not close after bye")
                    if msg.get("t") in ("bye", "peer_closed"):
                        break
                break
        elapsed = time.monotonic() - self.started
        print(
            f"[xls] done wall={elapsed:.3f}s syncs={self.syncs} "
            f"proc_ticks={self.proc_ticks} active_ticks={self.active_ticks} "
            f"in_flight={len(self.by_wire)} pending_mem={len(self.pending_mem)} "
            f"stats={self.stats}", flush=True,
        )
        return 0

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.xls.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/c2xm_xls_cosim.sock")
    parser.add_argument("--xls-runtime", default=os.environ.get(
        "C2XM_XLS_RUNTIME", os.path.join(os.path.dirname(__file__), "bin", "xls_proc_rpc_main")))
    parser.add_argument("--ir", default=os.environ.get("C2XM_XLS_IR"))
    parser.add_argument("--connect-timeout", type=float, default=600)
    parser.add_argument("--max-time", type=float, default=7200)
    parser.add_argument("--max-syncs", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not os.path.isfile(args.xls_runtime) or not os.access(args.xls_runtime, os.X_OK):
        parser.error(f"XLS runtime is not executable: {args.xls_runtime}")
    if not args.ir:
        parser.error("pass --ir or set C2XM_XLS_IR")
    if not os.path.isfile(args.ir):
        parser.error(f"optimized XLS IR not found: {args.ir}")
    peer = XlsCosimPeer(args)
    try:
        return peer.run()
    except Exception as exc:
        print(f"[xls] FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        peer.close()


if __name__ == "__main__":
    sys.exit(main())
