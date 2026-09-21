#!/usr/bin/env python3
"""Standalone gem5 stand-in for co-simulation bring-up (milestone M1).

Plays the gem5 side of the co-sim protocol over the same UNIX socket and
message ABI as the real ``ChiCosimBridge``:

* sends ReadNoSnp / WriteNoSnpFull requests exactly like the gem5 HNF does
  (opcode 0x04 / 0x5c, *raw byte count* size, uint32 txnids, one 64-byte
  write-data flit after DBIDResp),
* checks every response flit against the contract the gem5 HNF enforces
  before it panics (srcid/tgtid/HomeNID/dbid/resp/beat geometry, Comp.dbid
  == DBIDResp.dbid, sequential dataid, byteEnable/chunkValid all-ones),
* serves ``mem_read``/``mem_write`` from a sparse pattern DDR, which is
  also the reference for read-data comparison -- so the whole loop
  CHI -> DUT -> AXI -> proxy -> this DDR -> back is checked end to end,
* answers ``bypass_read``/``bypass_write`` the same way,
* runs the same quantum barrier (flits, then sync, then process messages
  until ack).

Usage:
    python3 fake_gem5.py --socket /tmp/c2xm_cosim.sock --scenario all

Exit code 0 = all scenarios passed, 1 = contract violation / timeout.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Dict, Optional

from cosim_protocol import PROTOCOL_VERSION, encode_msg

# gem5-side opcodes (Chi2ClassicMemTxnPolicy.hh).
OPC_READNOSNP = 0x04
OPC_WRITENOSNP_FULL = 0x5C
OPC_NONCOPYBACK_WRITEDATA = 0x03
OPC_COMP = 0x04
OPC_COMPDBIDRESP = 0x05
OPC_DBIDRESP = 0x06

LINE_BYTES = 64
BEAT_BYTES = 32


def ddr_pattern(addr: int, length: int) -> bytes:
    """Deterministic DDR content for never-written addresses."""
    out = bytearray(length)
    for i in range(length):
        out[i] = (addr * 2654435761 + i * 40503) & 0xFF
    return bytes(out)


class FakeDdr:
    """Sparse byte-granular memory with pattern fill on read."""

    def __init__(self):
        self.pages: Dict[int, bytearray] = {}
        self.page_size = 4096

    def _page(self, addr: int) -> bytearray:
        base = addr & ~(self.page_size - 1)
        page = self.pages.get(base)
        if page is None:
            page = bytearray(ddr_pattern(base, self.page_size))
            self.pages[base] = page
        return page

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray(length)
        pos = 0
        while pos < length:
            page = self._page(addr + pos)
            off = (addr + pos) & (self.page_size - 1)
            take = min(self.page_size - off, length - pos)
            out[pos:pos + take] = page[off:off + take]
            pos += take
        return bytes(out)

    def write(self, addr: int, data: bytes, strb: bytes = b"") -> None:
        if not strb or len(strb) != len(data):
            strb = b"\x01" * len(data)
        for i, byte in enumerate(data):
            if strb[i]:
                self._page(addr + i)[(addr + i) & (self.page_size - 1)] = byte


class ContractError(Exception):
    """A response flit violated the gem5 HNF acceptance contract."""


class HnfChecker:
    """Validates SNF-bound flits like HnfCoherencyController would."""

    def __init__(self, snf_id: int, hnf_id: int, ddr: FakeDdr):
        self.snf_id = snf_id
        self.hnf_id = hnf_id
        self.ddr = ddr
        # txnid -> {"addr","size","beats_expected","beats","data","done"}
        self.reads: Dict[int, Dict] = {}
        # txnid -> {"addr","size","dbid","phase","data"}
        self.writes: Dict[int, Dict] = {}
        self.completed_reads = 0
        self.completed_writes = 0

    # ------------------------------------------------------------------
    def expect_read(self, txnid: int, addr: int, size: int) -> None:
        self.reads[txnid] = {
            "addr": addr, "size": size,
            "beats_expected": (size + BEAT_BYTES - 1) // BEAT_BYTES,
            "beats": 0, "data": b"", "done": False,
        }

    def expect_write(self, txnid: int, addr: int, size: int) -> None:
        self.writes[txnid] = {
            "addr": addr, "size": size, "dbid": None,
            "phase": "wait_dbid", "data": None, "done": False,
        }

    def write_ready_for_data(self, txnid: int) -> Optional[Dict]:
        w = self.writes.get(txnid)
        if w is not None and w["phase"] == "wait_data":
            return w
        return None

    # ------------------------------------------------------------------
    def on_rsp(self, flit: Dict) -> None:
        tag = f"rsp txn=0x{flit.get('txnid', 0):x}"
        if flit.get("srcid") != self.snf_id:
            raise ContractError(f"{tag}: srcid 0x{flit.get('srcid'):x}"
                                f" != SNF 0x{self.snf_id:x}")
        if flit.get("tgtid") != self.hnf_id:
            raise ContractError(f"{tag}: tgtid 0x{flit.get('tgtid'):x}"
                                f" != HNF 0x{self.hnf_id:x}")
        if flit.get("resp") != 1:
            raise ContractError(f"{tag}: resp {flit.get('resp')} != 1")
        if flit.get("respErr") != 0:
            raise ContractError(f"{tag}: respErr {flit.get('respErr')} != 0")
        if flit.get("pcrdtype") != 0:
            raise ContractError(f"{tag}: pcrdtype {flit.get('pcrdtype')} != 0")

        txnid = flit.get("txnid", 0)
        opcode = flit.get("opcode", 0)
        w = self.writes.get(txnid)
        if w is None:
            raise ContractError(f"{tag}: RSP for unknown write")

        if opcode in (OPC_DBIDRESP, OPC_COMPDBIDRESP):
            if w["phase"] != "wait_dbid":
                raise ContractError(f"{tag}: DBIDResp in phase {w['phase']}")
            if not flit.get("dbid"):
                raise ContractError(f"{tag}: DBIDResp dbid==0 (gem5 would"
                                    " silently drop the write)")
            w["dbid"] = flit["dbid"]
            if opcode == OPC_COMPDBIDRESP:
                w["phase"] = w.get("next_phase", "done")
                w["done"] = True
                self.completed_writes += 1
            else:
                w["phase"] = "wait_data"
        elif opcode == OPC_COMP:
            if w["phase"] != "wait_comp":
                raise ContractError(f"{tag}: Comp in phase {w['phase']}")
            if flit.get("dbid") != w["dbid"]:
                raise ContractError(
                    f"{tag}: Comp.dbid {flit.get('dbid')}"
                    f" != DBIDResp.dbid {w['dbid']}")
            w["done"] = True
            self.completed_writes += 1
        else:
            raise ContractError(f"{tag}: unexpected RSP opcode"
                                f" 0x{opcode:x}")

    def on_dat(self, flit: Dict) -> None:
        txnid = flit.get("txnid", 0)
        tag = f"dat txn=0x{txnid:x}"
        r = self.reads.get(txnid)
        if r is None:
            raise ContractError(f"{tag}: CompData for unknown read")
        if r["done"]:
            raise ContractError(f"{tag}: CompData after completion")
        if flit.get("opcode") != 0x4:
            raise ContractError(f"{tag}: DAT opcode 0x{flit.get('opcode'):x}"
                                " is not CompData")
        for field, want in (("srcid", self.snf_id), ("tgtid", self.hnf_id),
                            ("HomeNID", self.hnf_id), ("dbid", 0),
                            ("resp", 1), ("respErr", 0)):
            if flit.get(field) != want:
                raise ContractError(f"{tag}: {field}={flit.get(field)!r}"
                                    f" != {want!r}")

        dataid = flit.get("dataid", 0)
        if dataid != r["beats"]:
            raise ContractError(f"{tag}: dataid {dataid} out of order"
                                f" (expected {r['beats']})")
        if flit.get("beatOffset") != dataid * BEAT_BYTES:
            raise ContractError(f"{tag}: beatOffset"
                                f" {flit.get('beatOffset')}")
        data = bytes.fromhex(flit.get("data", ""))
        want_len = min(BEAT_BYTES, r["size"] - dataid * BEAT_BYTES)
        if len(data) != want_len:
            raise ContractError(f"{tag}: data len {len(data)} != {want_len}")
        be = bytes.fromhex(flit.get("byteEnable", ""))
        if len(be) != want_len or any(b != 1 for b in be):
            raise ContractError(f"{tag}: byteEnable not all-ones"
                                f" of length {want_len}")
        cv = bytes.fromhex(flit.get("chunkValid", ""))
        if len(cv) != (want_len + 7) // 8 or any(b != 1 for b in cv):
            raise ContractError(f"{tag}: chunkValid not all-ones")

        r["beats"] += 1
        r["data"] += data
        is_last = r["beats"] == r["beats_expected"]
        if bool(flit.get("last", 0)) != is_last:
            raise ContractError(f"{tag}: last={flit.get('last')}"
                                f" but beats {r['beats']}"
                                f"/{r['beats_expected']}")

        if is_last:
            r["done"] = True
            self.completed_reads += 1
            golden = self.ddr.read(r["addr"], r["size"])
            if r["data"] != golden:
                raise ContractError(
                    f"{tag}: read data mismatch at 0x{r['addr']:x}\n"
                    f"  got    {r['data'].hex()}\n"
                    f"  expect {golden.hex()}")

    # ------------------------------------------------------------------
    @property
    def all_done(self) -> bool:
        return (all(r["done"] for r in self.reads.values())
                and all(w["done"] for w in self.writes.values())
                and self.reads and self.writes)


class FakeGem5:
    def __init__(self, args):
        self.args = args
        self.ddr = FakeDdr()
        self.checker = HnfChecker(args.snf_id, args.hnf_id, self.ddr)
        #: Messages waiting to go out at the next quantum boundary.
        self.outbox = []
        #: gem5 txnid allocators, mirroring HnfCoherencyController ranges.
        self.next_read_txn = 1
        self.next_write_txn = 0x40000000
        self.mem_id = 1
        self.sync_cycle = 0
        self.syncs = 0
        self.scenario_done = False
        self.fail_reason: Optional[str] = None
        #: (addr, data) the scenario wrote, verified in DDR after Comp.
        self.write_verify = {}

    # ------------------------------------------------------------------
    # socket plumbing
    # ------------------------------------------------------------------
    def serve(self):
        path = self.args.socket
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        print(f"[fake_gem5] listening on {path}", flush=True)
        srv.settimeout(self.args.listen_timeout)
        self.conn, _ = srv.accept()
        self.conn.settimeout(self.args.sock_timeout)
        print("[fake_gem5] TB connected", flush=True)
        self._send({
            "t": "hello", "ver": PROTOCOL_VERSION,
            "quantum_cycles": self.args.quantum,
            "clk_period_ns": self.args.clk_ns,
            "snf_id": self.args.snf_id,
            "hnf_id": self.args.hnf_id,
        })

    def _send(self, msg: Dict) -> None:
        if self.args.verbose:
            ident = msg.get("id", msg.get("pkt", msg.get("cycle", "")))
            print(f"[fake_gem5] >> {msg.get('t')} {ident}", flush=True)
        self.conn.sendall(encode_msg(msg))

    def _recv_line(self, timeout: Optional[float] = None) -> Optional[Dict]:
        """Read one message; None on timeout or close."""
        from cosim_protocol import decode_msg
        if not hasattr(self, "_rbuf"):
            self._rbuf = b""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if b"\n" in self._rbuf:
                line, self._rbuf = self._rbuf.split(b"\n", 1)
                if line:
                    return decode_msg(line)
            remain = None
            if deadline is not None:
                remain = deadline - time.time()
                if remain <= 0:
                    return None
            self.conn.settimeout(min(remain, self.args.sock_timeout)
                                 if remain else self.args.sock_timeout)
            try:
                chunk = self.conn.recv(65536)
            except socket.timeout:
                if deadline is not None:
                    continue
                return None
            if not chunk:
                return {"t": "peer_closed"}
            self._rbuf += chunk

    # ------------------------------------------------------------------
    # message handling
    # ------------------------------------------------------------------
    def handle(self, msg: Dict, quantum_msgs: list) -> None:
        kind = msg.get("t")
        if self.args.verbose:
            ident = msg.get("id", msg.get("pkt", msg.get("cycle", "")))
            print(f"[fake_gem5] << {kind} {ident}", flush=True)
        if kind == "hello_ack":
            self.hello_acked = True
        elif kind == "flit":
            flit = msg.get("flit", {})
            if msg.get("ch") == "rsp":
                self.checker.on_rsp(flit)
                print(f"[fake_gem5] RSP  {self._describe_rsp(flit)}",
                      flush=True)
            elif msg.get("ch") == "dat":
                self.checker.on_dat(flit)
                print(f"[fake_gem5] DAT  txn=0x{flit.get('txnid'):x}"
                      f" dataid={flit.get('dataid')}"
                      f" last={flit.get('last')}", flush=True)
            self._maybe_react(flit)
        elif kind == "mem_read":
            data = self.ddr.read(msg["addr"], msg["len"])
            quantum_msgs.append({"t": "mem_data", "id": msg["id"],
                                 "data": data.hex(), "resp": 0})
        elif kind == "mem_write":
            self.ddr.write(msg["addr"],
                           bytes.fromhex(msg.get("data", "")),
                           bytes.fromhex(msg.get("strb", "")))
            quantum_msgs.append({"t": "mem_resp", "id": msg["id"],
                                 "resp": 0})
        elif kind == "bypass_resp":
            # Answer to a bypass_read/write we sent earlier.
            pkt = msg.get("pkt")
            if msg.get("resp", 0) != 0:
                raise ContractError(f"bypass pkt {pkt}: resp"
                                    f" {msg.get('resp')} != 0")
            if pkt == self.bypass_read_pkt:
                got = bytes.fromhex(msg.get("data", ""))
                golden = self.ddr.read(self.bypass_verify_addr,
                                       len(self.bypass_verify_data))
                if got != golden:
                    raise ContractError(
                        f"bypass read mismatch at"
                        f" 0x{self.bypass_verify_addr:x}\n"
                        f"  got    {got.hex()}\n  expect {golden.hex()}")
                self.bypass_read_done = True
            elif pkt == self.bypass_write_pkt:
                self.bypass_write_done = True
        elif kind == "ack":
            self.acked = True
        elif kind == "bye":
            self.remote_bye = msg.get("reason", "")
        elif kind == "peer_closed":
            self.remote_bye = "peer closed"
        else:
            print(f"[fake_gem5] WARNING unknown message {kind!r}",
                  flush=True)

    @staticmethod
    def _describe_rsp(flit: Dict) -> str:
        names = {OPC_COMP: "Comp", OPC_COMPDBIDRESP: "CompDBIDResp",
                 OPC_DBIDRESP: "DBIDResp"}
        name = names.get(flit.get("opcode", 0), "?")
        return (f"{name} txn=0x{flit.get('txnid'):x}"
                f" dbid={flit.get('dbid')}")

    def _maybe_react(self, flit: Dict) -> None:
        """gem5-side behaviour triggered by a received flit (write data)."""
        if flit.get("opcode") != OPC_DBIDRESP:
            return
        # DBIDResp observed: after a delay (here: next quantum) send data.
        txnid = flit.get("txnid")
        self.pending_write_data.append(txnid)

    # ------------------------------------------------------------------
    # scenarios
    # ------------------------------------------------------------------
    def run_scenario(self) -> None:
        name = self.args.scenario
        base = 0x8000_0000
        if name in ("read", "all", "mixed"):
            for i in range(self.args.reads if name != "mixed" else 3):
                txn = self.next_read_txn
                self.next_read_txn += 1
                addr = base + i * LINE_BYTES
                self.checker.expect_read(txn, addr, LINE_BYTES)
                self.outbox.append({
                    "t": "flit", "ch": "req", "dir": "g2t",
                    "flit": self._read_req(txn, addr),
                })
        if name in ("write", "all", "mixed"):
            for i in range(self.args.writes if name != "mixed" else 2):
                txn = self.next_write_txn
                self.next_write_txn += 1
                addr = base + 0x10000 + i * LINE_BYTES
                self.checker.expect_write(txn, addr, LINE_BYTES)
                self.write_verify[txn] = (addr, ddr_pattern(
                    addr + 0x1234, LINE_BYTES))
                self.outbox.append({
                    "t": "flit", "ch": "req", "dir": "g2t",
                    "flit": self._write_req(txn, addr),
                })
        if name == "bypass":
            addr = base + 0x20000
            self.bypass_verify_addr = addr
            self.bypass_verify_data = ddr_pattern(addr + 99, 32)
            self.bypass_write_pkt = 1
            self.bypass_read_pkt = 2
            self.outbox.append({
                "t": "bypass_write", "pkt": self.bypass_write_pkt,
                "addr": addr, "data": self.bypass_verify_data.hex(),
                "strb": ("01" * 32),
            })
            self.outbox.append({
                "t": "bypass_read", "pkt": self.bypass_read_pkt,
                "addr": addr, "len": 32,
            })

    def _read_req(self, txnid: int, addr: int) -> Dict:
        return {
            "qos": 0, "srcid": self.args.hnf_id, "tgtid": self.args.snf_id,
            "txnid": txnid, "opcode": OPC_READNOSNP, "AllowRetry": 0,
            "addr": addr, "size": LINE_BYTES, "ReturnNid": self.args.hnf_id,
            "order": 0, "pcrdtype": 0, "memattr": 0, "snpattr": 0,
            "expCompAck": False, "traceTag": False, "srcType": 0, "ldid": 0,
        }

    def _write_req(self, txnid: int, addr: int) -> Dict:
        return {
            "qos": 0, "srcid": self.args.hnf_id, "tgtid": self.args.snf_id,
            "txnid": txnid, "opcode": OPC_WRITENOSNP_FULL, "AllowRetry": 1,
            "addr": addr, "size": LINE_BYTES, "ReturnNid": self.args.hnf_id,
            "order": 0, "pcrdtype": 0, "memattr": 0, "snpattr": 0,
            "expCompAck": False, "traceTag": False, "srcType": 0, "ldid": 0,
        }

    def _write_data(self, txnid: int) -> Dict:
        addr, data = self.write_verify[txnid]
        w = self.checker.writes[txnid]
        return {
            "qos": 0, "srcid": self.args.hnf_id, "tgtid": self.args.snf_id,
            "txnid": txnid, "opcode": OPC_NONCOPYBACK_WRITEDATA,
            "last": 1, "HomeNID": self.args.hnf_id, "dbid": w["dbid"],
            "dataid": 0, "resp": 0, "respErr": 0, "beatOffset": 0,
            "byteEnable": ("01" * LINE_BYTES),
            "chunkValid": ("01" * (LINE_BYTES // 8)),
            "data": data.hex(),
        }

    def verify_writes(self) -> None:
        for txn, (addr, data) in self.write_verify.items():
            got = self.ddr.read(addr, LINE_BYTES)
            if got != data:
                raise ContractError(
                    f"write txn 0x{txn:x} DDR mismatch at 0x{addr:x}\n"
                    f"  got    {got.hex()}\n  expect {data.hex()}")

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> int:
        self.hello_acked = False
        self.acked = False
        self.remote_bye = None
        self.pending_write_data = []
        self.bypass_verify_addr = None
        self.bypass_verify_data = None
        self.bypass_read_done = False
        self.bypass_write_done = False
        self.bypass_read_pkt = None
        self.bypass_write_pkt = None

        self.serve()
        # wait for hello_ack
        while not self.hello_acked:
            msg = self._recv_line(timeout=30.0)
            if msg is None or msg.get("t") in ("peer_closed", "bye"):
                print("[fake_gem5] FAIL: no hello_ack", flush=True)
                return 1
            self.handle(msg, self.outbox)

        self.run_scenario()
        deadline = time.time() + self.args.max_time
        try:
            while not self.scenario_done:
                if time.time() > deadline:
                    raise ContractError(
                        f"timeout after {self.args.max_time}s:"
                        f" reads_done={self.checker.completed_reads}"
                        f"/{len(self.checker.reads)}"
                        f" writes_done={self.checker.completed_writes}"
                        f"/{len(self.checker.writes)}")

                # flush write data for DBIDResps seen last quantum
                for txnid in self.pending_write_data:
                    self.checker.writes[txnid]["phase"] = "wait_comp"
                    self.outbox.append({
                        "t": "flit", "ch": "dat", "dir": "g2t",
                        "flit": self._write_data(txnid),
                    })
                self.pending_write_data.clear()

                for msg in self.outbox:
                    self._send(msg)
                self.outbox.clear()
                self._send({"t": "sync", "cycle": self.sync_cycle,
                            "tick": self.sync_cycle})
                self.acked = False
                quantum_msgs = []
                while not self.acked:
                    msg = self._recv_line(timeout=30.0)
                    if msg is None:
                        raise ContractError("TB silent mid-quantum")
                    self.handle(msg, quantum_msgs)
                for msg in quantum_msgs:
                    self._send(msg)

                self.sync_cycle += self.args.quantum
                self.syncs += 1

                if self.remote_bye is not None:
                    print(f"[fake_gem5] TB said bye: {self.remote_bye}",
                          flush=True)
                    return 0 if self.remote_bye != "peer closed" else 1

                reads_ok = (self.checker.completed_reads
                            == len(self.checker.reads))
                writes_ok = (self.checker.completed_writes
                             == len(self.checker.writes))
                bypass_ok = (self.args.scenario != "bypass"
                             or (self.bypass_read_done
                                 and self.bypass_write_done))
                if reads_ok and writes_ok and bypass_ok:
                    self.verify_writes()
                    if (self.args.scenario == "bypass"
                            and self.bypass_verify_addr is not None):
                        got = self.ddr.read(self.bypass_verify_addr, 32)
                        if got != self.bypass_verify_data:
                            raise ContractError(
                                "bypass write/read data mismatch")
                    self.scenario_done = True
        except ContractError as exc:
            self.fail_reason = str(exc)
            print(f"[fake_gem5] FAIL: {exc}", flush=True)
            try:
                self._send({"t": "bye", "reason": f"contract: {exc}"})
            except OSError:
                pass
            return 1

        print(f"[fake_gem5] PASS scenario={self.args.scenario}"
              f" syncs={self.syncs}"
              f" reads={self.checker.completed_reads}"
              f" writes={self.checker.completed_writes}", flush=True)
        try:
            self._send({"t": "bye", "reason": "workload exit"})
        except OSError:
            pass
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/c2xm_cosim.sock")
    parser.add_argument("--scenario", default="all",
                        choices=["read", "write", "mixed", "bypass", "all"])
    parser.add_argument("--quantum", type=int, default=100)
    parser.add_argument("--clk-ns", type=float, default=0.556)
    parser.add_argument("--snf-id", type=lambda s: int(s, 0), default=0x80)
    parser.add_argument("--hnf-id", type=lambda s: int(s, 0), default=0x90)
    parser.add_argument("--reads", type=int, default=2)
    parser.add_argument("--writes", type=int, default=2)
    parser.add_argument("--listen-timeout", type=float, default=300.0)
    parser.add_argument("--sock-timeout", type=float, default=30.0)
    parser.add_argument("--max-time", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return FakeGem5(args).run()


if __name__ == "__main__":
    sys.exit(main())
