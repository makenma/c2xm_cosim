"""Co-simulation wire protocol shared by the TB transport, the runtime and
the standalone fake-gem5 / echo servers.

Message framing: one JSON object per newline-terminated line over a UNIX
stream socket.  Every message carries ``"t"`` (type):

    hello        gem5 -> tb    {"ver", "quantum_cycles", "clk_period_ns",
                                "snf_id", "hnf_id"}
    hello_ack    tb -> gem5    {"ver", "clk_period_ns"}
    flit         both ways     {"ch": "req"|"dat"|"rsp", "dir": "g2t"|"t2g",
                                "flit": {...}}   field names mirror gem5
                                ChiChannel.hh structs (see FLIT_* tables)
    mem_read     tb -> gem5    {"id", "addr", "len"}         AXI/bypass proxy
    mem_data     gem5 -> tb    {"id", "data": hex, "resp"}   read completion
    mem_write    tb -> gem5    {"id", "addr", "data": hex, "strb": hex}
    mem_resp     gem5 -> tb    {"id", "resp"}                write completion
    bypass_read  gem5 -> tb    {"pkt", "addr", "len"}        RNF bypass packet
    bypass_write gem5 -> tb    {"pkt", "addr", "data": hex, "strb": hex}
    bypass_resp  tb -> gem5    {"pkt", "data": hex, "resp"}
    sync         gem5 -> tb    {"cycle", "tick"}             quantum barrier
    ack          tb -> gem5    {"cycle"}
    bye          both ways     {"reason"}

Byte vectors (data/strb/byteEnable/chunkValid) are hex strings, byte 0 first
(``bytes.hex()``).
"""

from __future__ import annotations

import json
from typing import Dict, Optional

PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Flit JSON field tables.  Names follow the gem5 C++ structs
# (src/mem/cache/CHI/base/ChiChannel.hh) so the C++ bridge maps field for
# field; the TB side converts to/from the chi_flit.py dataclasses here.
# ---------------------------------------------------------------------------

#: gem5 RawReq members carried on the wire.
REQ_FIELDS = (
    "qos", "srcid", "tgtid", "txnid", "opcode",
    "AllowRetry", "addr", "size", "ReturnNid", "order", "pcrdtype",
    "memattr", "snpattr", "expCompAck", "traceTag", "srcType", "ldid",
)

#: gem5 RawRsp members carried on the wire.
RSP_FIELDS = (
    "qos", "srcid", "tgtid", "txnid", "opcode",
    "dbid", "resp", "respErr", "pcrdtype", "rspKind",
)

#: gem5 RawDat members carried on the wire.
DAT_FIELDS = (
    "qos", "srcid", "tgtid", "txnid", "opcode",
    "last", "HomeNID", "dbid", "dataid", "resp", "respErr", "beatOffset",
    "byteEnable", "chunkValid", "data",
)

#: RawDat fields that are byte vectors (hex encoded on the wire).
DAT_BYTE_FIELDS = ("byteEnable", "chunkValid", "data")


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------


def encode_msg(msg: Dict) -> bytes:
    """Encode one message as a newline-terminated JSON line."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: bytes) -> Dict:
    """Decode one message line; raises ValueError on malformed input."""
    return json.loads(line.decode("utf-8"))


class MessageReader:
    """Buffered newline-JSON reader for a socket file object.

    ``read()`` returns None when the peer has closed the connection.
    """

    def __init__(self, sock):
        self._sock = sock
        self._buf = b""

    def read(self) -> Optional[Dict]:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        if not line:
            return self.read()
        return decode_msg(line)


def bytes_to_hex(data: bytes) -> str:
    return data.hex()


def hex_to_bytes(value: str) -> bytes:
    return bytes.fromhex(value or "")
