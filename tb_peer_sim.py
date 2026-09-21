#!/usr/bin/env python3
"""Software stand-in for the pyuvm testbench (gem5-side bring-up, M2).

Connects to the real gem5 ``ChiCosimBridge`` and emulates everything the
VCS testbench would do, minus the RTL:

* answers the quantum barrier (sync -> ack),
* acts as a protocol-correct SN-F for the CHI flits the bridge forwards
  (ReadNoSnp 0x04 -> CompData beat pairs, WriteNoSnpFull 0x5c ->
  DBIDResp/Comp with dbid bookkeeping, write data echoed into DDR),
* serves DUT-style ``mem_read``/``mem_write`` against either a local
  sparse DDR (``--mem local``) or gem5's own DDR (``--mem gem5``, the
  real architecture: reads/writes come back as mem_data/mem_resp).

Exit code 0 when ``--max-syncs`` is reached (or the peer says bye), 1 on
contract violations or timeout.  Progress lines show committed
instructions are flowing if the gem5 side runs a workload.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from typing import Dict, List, Optional

from cosim_protocol import PROTOCOL_VERSION, encode_msg

LINE_BYTES = 64
BEAT_BYTES = 32

OPC_READNOSNP = 0x04
OPC_WRITENOSNP_FULL = 0x5C
OPC_NONCOPYBACK_WRITEDATA = 0x03
OPC_COMP = 0x04
OPC_DBIDRESP = 0x06


class LocalDdr:
    """Sparse pattern DDR (same fill as fake_gem5)."""

    def __init__(self):
        self.pages: Dict[int, bytearray] = {}
        self.page = 4096

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray(length)
        for i in range(length):
            out[i] = (addr * 2654435761 + i * 40503) & 0xFF
        # overlay written pages
        base = addr & ~(self.page - 1)
        pg = self.pages.get(base)
        if pg:
            for i in range(length):
                off = (addr + i) & (self.page - 1)
                if off < len(pg) and pg[off] is not None:
                    out[i] = pg[off]
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        base = addr & ~(self.page - 1)
        pg = self.pages.get(base)
        if pg is None:
            pg = bytearray(self.page)
            self.pages[base] = pg
        for i, b in enumerate(data):
            pg[(addr + i) & (self.page - 1)] = b


class TbPeerSim:
    def __init__(self, args):
        self.args = args
        self.local_ddr = LocalDdr()
        self.mem_id = 1
        self.pending_mem: Dict[int, callable] = {}
        self.mem_results: Dict[int, dict] = {}
        # gem5 txnid -> {"addr", "size", "dbid", "phase", "srcid"}
        self.writes: Dict[int, dict] = {}
        self.dbid = 0
        self.syncs = 0
        self.reads_served = 0
        self.writes_served = 0
        self.req_seen = 0
        self.dat_seen = 0
        self._rbuf = b""

    # ------------------------------------------------------------------
    def connect(self):
        deadline = time.time() + self.args.connect_timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(self.args.socket)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if time.time() > deadline:
                    raise
                time.sleep(0.5)

    def send(self, msg: dict) -> None:
        if self.args.verbose:
            print(f"[peer] >> {msg.get('t')}", flush=True)
        self.sock.sendall(encode_msg(msg))

    def recv(self, timeout: float) -> Optional[dict]:
        from cosim_protocol import decode_msg
        deadline = time.time() + timeout
        while True:
            if b"\n" in self._rbuf:
                line, self._rbuf = self._rbuf.split(b"\n", 1)
                if line:
                    return decode_msg(line)
            remain = deadline - time.time()
            if remain <= 0:
                return None
            self.sock.settimeout(min(remain, 5.0))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return {"t": "peer_closed"}
            self._rbuf += chunk

    # ------------------------------------------------------------------
    # memory service
    # ------------------------------------------------------------------
    def mem_read(self, addr: int, length: int, then: callable) -> None:
        if self.args.mem == "local":
            then({"data": self.local_ddr.read(addr, length).hex(),
                  "resp": 0})
            return
        mid = self.mem_id
        self.mem_id += 1
        self.pending_mem[mid] = then
        self.send({"t": "mem_read", "id": mid, "addr": addr, "len": length})

    def mem_write(self, addr: int, data: bytes, then: callable) -> None:
        if self.args.mem == "local":
            self.local_ddr.write(addr, data)
            then({"resp": 0})
            return
        mid = self.mem_id
        self.mem_id += 1
        self.pending_mem[mid] = then
        self.send({"t": "mem_write", "id": mid, "addr": addr,
                   "data": data.hex(), "strb": (b"\xff" * len(data)).hex()})

    def _deliver_mem(self, msg: dict) -> None:
        mid = msg.get("id")
        cb = self.pending_mem.pop(mid, None)
        if cb:
            cb(msg)

    # ------------------------------------------------------------------
    # SN-F emulation
    # ------------------------------------------------------------------
    def on_req(self, f: dict) -> None:
        self.req_seen += 1
        txnid = f["txnid"]
        if f["opcode"] == OPC_READNOSNP:
            size = f.get("size", 0) or LINE_BYTES
            addr = f["addr"]

            def serve_read(mem_msg: dict) -> None:
                data = bytes.fromhex(mem_msg.get("data", ""))
                tgt = f.get("ReturnNid", 0) or f["srcid"]
                beats = (size + BEAT_BYTES - 1) // BEAT_BYTES
                for dataid in range(beats):
                    chunk = data[dataid * BEAT_BYTES:
                                 (dataid + 1) * BEAT_BYTES]
                    self.send({
                        "t": "flit", "ch": "dat", "dir": "t2g",
                        "flit": {
                            "qos": f.get("qos", 0),
                            "srcid": self.args.snf_id,
                            "tgtid": tgt,
                            "txnid": txnid,
                            "opcode": 0x4,  # CompData
                            "last": 1 if dataid == beats - 1 else 0,
                            "HomeNID": tgt,
                            "dbid": 0,
                            "dataid": dataid,
                            "resp": 1,
                            "beatOffset": dataid * BEAT_BYTES,
                            "byteEnable": (b"\x01" * len(chunk)).hex(),
                            "chunkValid": (b"\x01" * ((len(chunk) + 7) // 8)).hex(),
                            "data": chunk.hex(),
                        },
                    })
                self.reads_served += 1

            self.mem_read(addr, size, serve_read)

        elif f["opcode"] == OPC_WRITENOSNP_FULL:
            self.dbid = (self.dbid % 255) + 1
            self.writes[txnid] = {
                "addr": f["addr"],
                "size": f.get("size", 0) or LINE_BYTES,
                "dbid": self.dbid,
                "srcid": f["srcid"],
                "qos": f.get("qos", 0),
            }
            self.send({
                "t": "flit", "ch": "rsp", "dir": "t2g",
                "flit": {
                    "qos": f.get("qos", 0),
                    "srcid": self.args.snf_id,
                    "tgtid": f["srcid"],
                    "txnid": txnid,
                    "opcode": OPC_DBIDRESP,
                    "dbid": self.dbid,
                    "resp": 1,
                    "respErr": 0,
                    "pcrdtype": 0,
                    "rspKind": 0,
                },
            })
        else:
            print(f"[peer] WARN unsupported REQ opcode 0x{f['opcode']:x}",
                  flush=True)

    def on_dat(self, f: dict) -> None:
        self.dat_seen += 1
        txnid = f["txnid"]
        w = self.writes.get(txnid)
        if w is None:
            print(f"[peer] WARN write data for unknown txn "
                  f"0x{txnid:x}", flush=True)
            return
        data = bytes.fromhex(f.get("data", ""))

        def serve_write(mem_msg: dict) -> None:
            self.send({
                "t": "flit", "ch": "rsp", "dir": "t2g",
                "flit": {
                    "qos": w["qos"],
                    "srcid": self.args.snf_id,
                    "tgtid": w["srcid"],
                    "txnid": txnid,
                    "opcode": OPC_COMP,
                    "dbid": w["dbid"],
                    "resp": 1,
                    "respErr": 0,
                    "pcrdtype": 0,
                    "rspKind": 0,
                },
            })
            self.writes.pop(txnid, None)
            self.writes_served += 1

        self.mem_write(w["addr"], data, serve_write)

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.connect()
        # hello exchange: bridge sends hello first
        hello = self.recv(30.0)
        if hello is None or hello.get("t") != "hello":
            print("[peer] FAIL: no hello from gem5", flush=True)
            return 1
        print(f"[peer] hello: {hello}", flush=True)
        self.send({"t": "hello_ack", "ver": PROTOCOL_VERSION,
                   "clk_period_ns": 0.556})

        deadline = time.time() + self.args.max_time
        while True:
            msg = self.recv(10.0)
            if msg is None:
                if time.time() > deadline:
                    print(f"[peer] timeout after {self.args.max_time}s",
                          flush=True)
                    return 1
                continue
            kind = msg.get("t")
            if kind == "sync":
                self.syncs += 1
                self.send({"t": "ack", "cycle": msg.get("cycle", 0)})
                if self.syncs % 500 == 0:
                    print(f"[peer] syncs={self.syncs} req={self.req_seen}"
                          f" dat={self.dat_seen} reads={self.reads_served}"
                          f" writes={self.writes_served}", flush=True)
                if self.syncs >= self.args.max_syncs:
                    print(f"[peer] reached max syncs "
                          f"{self.args.max_syncs}; bye", flush=True)
                    self.send({"t": "bye", "reason": "peer sim done"})
                    return 0
            elif kind == "flit":
                flit = msg.get("flit", {})
                if msg.get("ch") == "req":
                    self.on_req(flit)
                elif msg.get("ch") == "dat":
                    self.on_dat(flit)
                else:
                    print(f"[peer] WARN flit ch={msg.get('ch')}",
                          flush=True)
            elif kind in ("mem_data", "mem_resp"):
                self._deliver_mem(msg)
            elif kind == "bypass_read":
                def serve_byp_r(mem_msg: dict, pkt=msg["pkt"]) -> None:
                    self.send({"t": "bypass_resp", "pkt": pkt,
                               "data": mem_msg.get("data", ""),
                               "resp": 0})
                self.mem_read(msg["addr"], msg["len"], serve_byp_r)
            elif kind == "bypass_write":
                def serve_byp_w(mem_msg: dict, pkt=msg["pkt"]) -> None:
                    self.send({"t": "bypass_resp", "pkt": pkt,
                               "data": "", "resp": 0})
                self.mem_write(msg["addr"],
                               bytes.fromhex(msg.get("data", "")),
                               serve_byp_w)
            elif kind == "bye":
                print(f"[peer] gem5 said bye: {msg.get('reason')}",
                      flush=True)
                return 0
            elif kind == "peer_closed":
                print("[peer] gem5 closed the connection", flush=True)
                return 0
            else:
                print(f"[peer] WARN unknown message {kind!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/c2xm_cosim.sock")
    parser.add_argument("--mem", choices=["local", "gem5"], default="gem5")
    parser.add_argument("--snf-id", type=lambda s: int(s, 0), default=0x80)
    parser.add_argument("--max-syncs", type=int, default=100000)
    parser.add_argument("--connect-timeout", type=float, default=600.0)
    parser.add_argument("--max-time", type=float, default=3600.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return TbPeerSim(args).run()


if __name__ == "__main__":
    sys.exit(main())
