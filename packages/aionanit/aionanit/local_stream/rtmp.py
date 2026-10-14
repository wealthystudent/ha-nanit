"""Server side of an RTMP publish session, as a synchronous state machine.

``RtmpSession.feed()`` takes raw bytes from the socket and returns bytes to
send back. It never blocks and owns no I/O, so it can be tested byte by byte
and fuzzed without an event loop. The asyncio glue lives in ``server.py``.

Scope is exactly what a camera publishing one stream needs: the simple
handshake, chunk stream demux (formats 0-3, extended timestamps the way
FFmpeg reads and writes them, peer chunk size changes), acknowledgements,
and the ``connect``/``createStream``/``publish`` command sequence. Media and
metadata messages are handed to callbacks as FLV tags.

Every limit below exists because any host on the LAN can connect.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from . import amf0
from .flv import TAG_AUDIO, TAG_SCRIPT, TAG_VIDEO, Tag

HANDSHAKE_SIZE = 1536
MAX_MESSAGE_SIZE = 2 * 1024 * 1024
MAX_CHUNK_STREAMS = 64
MAX_PRE_PUBLISH_BYTES = 64 * 1024
MAX_PEER_CHUNK_SIZE = 1 << 24
DEFAULT_CHUNK_SIZE = 128
OUT_CHUNK_SIZE = 4096
WINDOW_ACK_SIZE = 2_500_000

_MSG_SET_CHUNK_SIZE = 1
_MSG_ABORT = 2
_MSG_ACK = 3
_MSG_USER_CONTROL = 4
_MSG_WINDOW_ACK = 5
_MSG_PEER_BANDWIDTH = 6
_MSG_AMF3_DATA = 15
_MSG_AMF3_COMMAND = 17
_MSG_AMF0_DATA = 18
_MSG_AMF0_COMMAND = 20

_TS_EXTENDED = 0xFFFFFF
_PUBLISH_STREAM_ID = 1


class RtmpError(Exception):
    """The peer broke the protocol or a limit. The connection should close."""


class _State(Enum):
    C0C1 = auto()
    C2 = auto()
    CHUNKS = auto()
    CLOSED = auto()


@dataclass(slots=True)
class _ChunkStream:
    """Per chunk stream id state, kept across messages as RTMP requires."""

    timestamp: int = 0
    delta: int = 0
    ts_field: int = 0  # last 24 bit timestamp field seen, decides extended reads
    length: int = 0
    type_id: int = 0
    stream_id: int = 0
    buffer: bytearray | None = None


class RtmpSession:
    """One RTMP connection from a publisher.

    ``authorize(stream_name)`` decides whether a publish is accepted and
    returns an opaque token for it (or ``None`` to refuse). ``on_tag`` gets
    every media and metadata tag once publishing, ``on_publish`` fires once
    when a publish is accepted.
    """

    def __init__(
        self,
        authorize: Callable[[str], object | None],
        on_publish: Callable[[object], None],
        on_tag: Callable[[Tag], None],
    ) -> None:
        """Initialize the session."""
        self._authorize = authorize
        self._on_publish = on_publish
        self._on_tag = on_tag
        self._state = _State.C0C1
        self._buf = bytearray()
        self._streams: dict[int, _ChunkStream] = {}
        self._in_chunk_size = DEFAULT_CHUNK_SIZE
        self._peer_window = 0
        self._bytes_in = 0
        self._last_ack = 0
        self._publishing: object | None = None
        self._out = bytearray()
        self._out_chunk_size = DEFAULT_CHUNK_SIZE
        self.app = ""

    @property
    def publishing(self) -> bool:
        """Return True once a publish was accepted."""
        return self._publishing is not None

    @property
    def closed(self) -> bool:
        """Return True after the peer ended the publish or broke the protocol."""
        return self._state is _State.CLOSED

    def feed(self, data: bytes) -> bytes:
        """Consume bytes from the peer and return bytes to send back.

        Raises ``RtmpError`` on a protocol violation or limit breach.
        """
        if self._state is _State.CLOSED:
            return b""
        self._bytes_in += len(data)
        if self._publishing is None and self._bytes_in > MAX_PRE_PUBLISH_BYTES:
            raise RtmpError("too much data before publish")
        self._buf += data
        try:
            self._process()
        except (amf0.Amf0Error, struct.error, IndexError) as err:
            raise RtmpError(str(err)) from err
        self._maybe_ack()
        out = bytes(self._out)
        self._out.clear()
        return out

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _process(self) -> None:
        while True:
            if self._state is _State.C0C1:
                if len(self._buf) < 1 + HANDSHAKE_SIZE:
                    return
                if self._buf[0] != 3:
                    raise RtmpError(f"unsupported RTMP version {self._buf[0]}")
                c1 = bytes(self._buf[1 : 1 + HANDSHAKE_SIZE])
                del self._buf[: 1 + HANDSHAKE_SIZE]
                s1 = b"\x00\x00\x00\x00" * 2 + os.urandom(HANDSHAKE_SIZE - 8)
                self._out += b"\x03" + s1 + c1
                self._state = _State.C2
            elif self._state is _State.C2:
                if len(self._buf) < HANDSHAKE_SIZE:
                    return
                del self._buf[:HANDSHAKE_SIZE]
                self._state = _State.CHUNKS
            elif self._state is _State.CHUNKS:
                if not self._read_chunk():
                    return
            else:
                return

    # ------------------------------------------------------------------
    # Chunk demux
    # ------------------------------------------------------------------

    def _read_chunk(self) -> bool:
        """Parse one chunk if it is complete. Return False to wait for more."""
        buf = self._buf
        if not buf:
            return False
        first = buf[0]
        fmt = first >> 6
        csid = first & 0x3F
        pos = 1
        if csid == 0:
            if len(buf) < 2:
                return False
            csid = buf[1] + 64
            pos = 2
        elif csid == 1:
            if len(buf) < 3:
                return False
            csid = buf[1] + buf[2] * 256 + 64
            pos = 3

        stream = self._streams.get(csid)
        if stream is None:
            if fmt != 0:
                raise RtmpError(f"chunk stream {csid} starts without a full header")
            if len(self._streams) >= MAX_CHUNK_STREAMS:
                raise RtmpError("too many chunk streams")
            stream = _ChunkStream()

        header_len = (11, 7, 3, 0)[fmt]
        if len(buf) < pos + header_len:
            return False
        hdr = buf[pos : pos + header_len]
        pos += header_len

        ts_field = stream.ts_field
        length, type_id, stream_id = stream.length, stream.type_id, stream.stream_id
        if fmt <= 2:
            ts_field = int.from_bytes(hdr[0:3], "big")
        if fmt <= 1:
            length = int.from_bytes(hdr[3:6], "big")
            type_id = hdr[6]
        if fmt == 0:
            stream_id = int.from_bytes(hdr[7:11], "little")

        # FFmpeg's reader: an extended timestamp follows whenever the
        # 24 bit field (carried over for format 3) is 0xFFFFFF.
        ext = 0
        if ts_field == _TS_EXTENDED:
            if len(buf) < pos + 4:
                return False
            ext = int.from_bytes(buf[pos : pos + 4], "big")
            pos += 4

        starting = stream.buffer is None
        if not starting and fmt <= 1 and (length != stream.length or type_id != stream.type_id):
            # FFmpeg's reader refuses this too ("RTMP packet size mismatch"),
            # and allowing it would let a publisher dodge the size cap.
            raise RtmpError("message header changed mid-message")
        if starting and length > MAX_MESSAGE_SIZE:
            raise RtmpError(f"message of {length} bytes exceeds the limit")
        have = 0 if stream.buffer is None else len(stream.buffer)
        take = min(self._in_chunk_size, length - have)
        if len(buf) < pos + take:
            return False

        # The chunk is complete, commit state.
        if starting:
            value = ext if ts_field == _TS_EXTENDED else ts_field
            if fmt == 0:
                # The spec and FFmpeg's reader reuse an absolute timestamp
                # as the delta for a following format 3 message.
                stream.timestamp = value
                stream.delta = value
            elif fmt in (1, 2):
                stream.delta = value
                stream.timestamp = (stream.timestamp + value) & 0xFFFFFFFF
            else:
                stream.timestamp = (stream.timestamp + stream.delta) & 0xFFFFFFFF
            stream.buffer = bytearray()
            # Header fields are only taken when a message starts, as FFmpeg does.
            stream.ts_field = ts_field
            stream.length, stream.type_id, stream.stream_id = length, type_id, stream_id
        self._streams[csid] = stream

        assert stream.buffer is not None
        stream.buffer += buf[pos : pos + take]
        del buf[: pos + take]

        if len(stream.buffer) >= stream.length:
            body = bytes(stream.buffer)
            stream.buffer = None
            self._handle_message(stream.type_id, stream.stream_id, stream.timestamp, body)
        return True

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def _handle_message(self, type_id: int, stream_id: int, timestamp: int, body: bytes) -> None:
        if type_id == _MSG_SET_CHUNK_SIZE:
            size = int.from_bytes(body[0:4], "big") & 0x7FFFFFFF
            if not 1 <= size <= MAX_PEER_CHUNK_SIZE:
                raise RtmpError(f"bad chunk size {size}")
            self._in_chunk_size = size
        elif type_id == _MSG_ABORT:
            csid = int.from_bytes(body[0:4], "big")
            if csid in self._streams:
                self._streams[csid].buffer = None
        elif type_id == _MSG_WINDOW_ACK:
            self._peer_window = int.from_bytes(body[0:4], "big")
        elif type_id in (_MSG_ACK, _MSG_USER_CONTROL, _MSG_PEER_BANDWIDTH):
            pass
        elif type_id == _MSG_AMF0_COMMAND:
            self._handle_command(stream_id, amf0.decode_all(body))
        elif type_id == _MSG_AMF3_COMMAND:
            self._handle_command(stream_id, amf0.decode_all(body[1:]))
        elif type_id in (_MSG_AMF0_DATA, _MSG_AMF3_DATA):
            self._handle_data(body if type_id == _MSG_AMF0_DATA else body[1:], timestamp)
        elif type_id in (TAG_AUDIO, TAG_VIDEO):
            if self._publishing is not None and stream_id == _PUBLISH_STREAM_ID and body:
                self._on_tag(Tag(type_id, timestamp, body))

    def _handle_data(self, body: bytes, timestamp: int) -> None:
        if self._publishing is None or not body:
            return
        values = amf0.decode_all(body)
        if values and values[0] == "@setDataFrame":
            # Strip the wrapper, FLV files carry a bare onMetaData.
            body = body[amf0.first_value_length(body) :]
            values = values[1:]
        if values and values[0] == "onMetaData":
            self._on_tag(Tag(TAG_SCRIPT, timestamp, body))

    def _handle_command(self, stream_id: int, values: list[amf0.AmfValue]) -> None:
        if not values or not isinstance(values[0], str):
            return
        name = values[0]
        txn = values[1] if len(values) > 1 else 0
        if name == "connect":
            props = values[2] if len(values) > 2 else None
            if isinstance(props, dict) and isinstance(props.get("app"), str):
                self.app = str(props["app"])
            self._send_control(_MSG_WINDOW_ACK, struct.pack(">I", WINDOW_ACK_SIZE))
            self._send_control(_MSG_PEER_BANDWIDTH, struct.pack(">IB", WINDOW_ACK_SIZE, 2))
            self._send_control(_MSG_SET_CHUNK_SIZE, struct.pack(">I", OUT_CHUNK_SIZE))
            self._out_chunk_size = OUT_CHUNK_SIZE
            self._send_command(
                0,
                "_result",
                txn,
                {"fmsVer": "FMS/3,0,1,123", "capabilities": 31.0},
                {
                    "level": "status",
                    "code": "NetConnection.Connect.Success",
                    "description": "Connection succeeded.",
                    "objectEncoding": 0.0,
                },
            )
        elif name in ("releaseStream", "FCPublish"):
            self._send_command(0, "_result", txn, None)
        elif name == "createStream":
            self._send_command(0, "_result", txn, None, float(_PUBLISH_STREAM_ID))
        elif name == "publish":
            self._handle_publish(stream_id, values)
        elif name in ("FCUnpublish", "deleteStream", "closeStream"):
            self._state = _State.CLOSED

    def _handle_publish(self, stream_id: int, values: list[amf0.AmfValue]) -> None:
        if self._publishing is not None:
            raise RtmpError("second publish on one connection")
        stream_name = values[3] if len(values) > 3 else None
        if not isinstance(stream_name, str):
            raise RtmpError("publish without a stream name")
        # FFmpeg sends the last path segment, possibly with a query string.
        token = self._authorize(stream_name.split("?", 1)[0])
        if token is None:
            raise RtmpError("publish refused")
        self._publishing = token
        self._send_command(
            stream_id or _PUBLISH_STREAM_ID,
            "onStatus",
            0,
            None,
            {
                "level": "status",
                "code": "NetStream.Publish.Start",
                "description": "Publishing.",
            },
        )
        self._on_publish(token)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _maybe_ack(self) -> None:
        if self._peer_window and self._bytes_in - self._last_ack >= self._peer_window:
            self._last_ack = self._bytes_in
            self._send_control(_MSG_ACK, struct.pack(">I", self._bytes_in & 0xFFFFFFFF))

    def _send_control(self, type_id: int, body: bytes) -> None:
        self._write_message(2, type_id, 0, body)

    def _send_command(self, stream_id: int, *values: amf0.AmfValue) -> None:
        self._write_message(3, _MSG_AMF0_COMMAND, stream_id, amf0.encode_all(*values))

    def _write_message(self, csid: int, type_id: int, stream_id: int, body: bytes) -> None:
        header = struct.pack(
            ">B3s3sB", csid, b"\x00\x00\x00", len(body).to_bytes(3, "big"), type_id
        )
        header += struct.pack("<I", stream_id)
        size = self._out_chunk_size
        chunks = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
        self._out += header + chunks[0]
        for chunk in chunks[1:]:
            self._out += bytes([0xC0 | csid]) + chunk
