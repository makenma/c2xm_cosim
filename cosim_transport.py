"""TB-side co-sim transport: UNIX-socket client with background I/O threads.

The VCS/cocotb reactor must never block on the socket, so a reader thread
drains the peer into a ``queue.Queue`` and a writer thread flushes a send
queue.  Coroutines interact with the transport purely through the queues:

    transport.send(msg_dict)          thread-safe, never blocks
    transport.poll() -> msg | None    non-blocking receive

Flit conversion between the gem5 JSON field naming (ChiChannel.hh) and the
``chi_flit.py`` dataclasses also lives here, because both the runtime and
the fake-gem5 helper use it.
"""

from __future__ import annotations

import queue
import socket
import threading
from typing import Dict, List, Optional

from chi_flit import RawDat, RawReq, RawRsp
from cosim_protocol import (
    DAT_BYTE_FIELDS,
    DAT_FIELDS,
    REQ_FIELDS,
    RSP_FIELDS,
    bytes_to_hex,
    encode_msg,
    hex_to_bytes,
)


class CosimTransport:
    """Length-free (newline framed) JSON transport, TB side is the client."""

    def __init__(self, logger=None):
        self.log = logger
        self.sock: Optional[socket.socket] = None
        self.rx_queue: "queue.Queue[Dict]" = queue.Queue()
        self.tx_queue: "queue.Queue[Dict]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.connected = False
        self.peer_closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect(self, path: str, timeout_s: float = 60.0) -> None:
        """Connect to the gem5-side server at ``path`` (retry until timeout)."""
        deadline = timeout_s
        waited = 0.0
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self.sock.close()
                self.sock = None
                if waited >= deadline:
                    raise TimeoutError(f"cosim server at {path} never appeared")
                # retry, gem5 may still be building the system
                threading.Event().wait(0.5)
                waited += 0.5

        self.connected = True
        self._stop.clear()
        reader = threading.Thread(target=self._reader_main, daemon=True,
                                  name="cosim-rx")
        writer = threading.Thread(target=self._writer_main, daemon=True,
                                  name="cosim-tx")
        reader.start()
        writer.start()
        self._threads = [reader, writer]

    def close(self, reason: str = "tb done") -> None:
        if not self.connected:
            return
        self.send({"t": "bye", "reason": reason})
        # give the writer thread a moment to flush the bye
        threading.Event().wait(0.2)
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.sock = None
        self.connected = False

    # ------------------------------------------------------------------
    # queues
    # ------------------------------------------------------------------
    def send(self, msg: Dict) -> None:
        """Queue one message for the peer (never blocks the reactor)."""
        self.tx_queue.put(msg)

    def poll(self) -> Optional[Dict]:
        """Pop one received message, or None if the queue is empty."""
        try:
            return self.rx_queue.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # threads
    # ------------------------------------------------------------------
    def _reader_main(self) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        from cosim_protocol import decode_msg
                        self.rx_queue.put(decode_msg(line))
        except OSError:
            pass
        self.peer_closed = True
        # Unblock / inform the runtime even if it is waiting for nothing else.
        self.rx_queue.put({"t": "peer_closed"})

    def _writer_main(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    msg = self.tx_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.sock.sendall(encode_msg(msg))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# gem5 JSON flits <-> chi_flit.py dataclasses
# ---------------------------------------------------------------------------


def req_to_json(flit: RawReq) -> Dict:
    """chi_flit.RawReq -> gem5-named JSON payload (g2t direction)."""
    return {
        "qos": flit.qos,
        "srcid": flit.srcid,
        "tgtid": flit.tgtid,
        "txnid": flit.txnid,
        "opcode": flit.opcode,
        "AllowRetry": flit.allow_retry,
        "addr": flit.addr,
        "size": flit.size,
        "ReturnNid": flit.return_nid,
        "order": flit.order,
        "pcrdtype": flit.pcrdtype,
        "memattr": flit.memattr,
        "snpattr": flit.snpattr,
        "expCompAck": bool(flit.exp_comp_ack),
        "traceTag": bool(flit.trace_tag),
        "srcType": flit.src_type,
        "ldid": flit.ldid,
    }


def req_from_json(payload: Dict) -> RawReq:
    """gem5-named JSON payload -> chi_flit.RawReq.

    NOTE: gem5 ``size`` is a *raw byte count* (0 == whole cache line); the
    SNF agent converts it to the log2 encoding before the flit reaches the
    DUT driver.
    """
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
        exp_comp_ack=bool(payload.get("expCompAck", False)),
        trace_tag=bool(payload.get("traceTag", False)),
        src_type=payload.get("srcType", 0),
        ldid=payload.get("ldid", 0),
    )


def rsp_to_json(flit: RawRsp) -> Dict:
    """chi_flit.RawRsp -> gem5-named JSON payload (t2g direction)."""
    return {
        "qos": flit.qos,
        "srcid": flit.srcid,
        "tgtid": flit.tgtid,
        "txnid": flit.txnid,
        "opcode": flit.opcode,
        "dbid": flit.dbid,
        "resp": flit.resp,
        "respErr": flit.resp_err,
        "pcrdtype": flit.pcrdtype,
        "rspKind": int(flit.rsp_kind),
    }


def dat_to_json(flit: RawDat) -> Dict:
    """chi_flit.RawDat -> gem5-named JSON payload."""
    return {
        "qos": flit.qos,
        "srcid": flit.srcid,
        "tgtid": flit.tgtid,
        "txnid": flit.txnid,
        "opcode": flit.opcode,
        "last": flit.last,
        "HomeNID": flit.home_nid,
        "dbid": flit.dbid,
        "dataid": flit.data_id,
        "resp": flit.resp,
        "respErr": flit.resp_err,
        "beatOffset": flit.beat_offset,
        "byteEnable": bytes_to_hex(bytes(flit.byte_enable)),
        "chunkValid": bytes_to_hex(bytes(flit.chunk_valid)),
        "data": bytes_to_hex(bytes(flit.data)),
    }


def dat_from_json(payload: Dict) -> RawDat:
    """gem5-named JSON payload -> chi_flit.RawDat (byte vectors restored)."""
    return RawDat(
        qos=payload.get("qos", 0),
        srcid=payload.get("srcid", 0),
        tgtid=payload.get("tgtid", 0),
        txnid=payload.get("txnid", 0),
        opcode=payload.get("opcode", 0),
        last=payload.get("last", 0),
        home_nid=payload.get("HomeNID", 0),
        dbid=payload.get("dbid", 0),
        data_id=payload.get("dataid", 0),
        resp=payload.get("resp", 0),
        resp_err=payload.get("respErr", 0),
        beat_offset=payload.get("beatOffset", 0),
        byte_enable=list(hex_to_bytes(payload.get("byteEnable", ""))),
        chunk_valid=list(hex_to_bytes(payload.get("chunkValid", ""))),
        data=list(hex_to_bytes(payload.get("data", ""))),
    )
