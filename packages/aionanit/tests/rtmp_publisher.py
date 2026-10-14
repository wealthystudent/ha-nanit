"""A small RTMP publisher for tests, chunking the way FFmpeg does."""

from __future__ import annotations

import asyncio
import ssl
import struct

from aionanit.local_stream import amf0

HANDSHAKE_SIZE = 1536

# Minimal but well-formed bodies: AVC sequence header, keyframe, inter
# frame, AAC sequence header and an AAC frame.
AVC_HEADER = bytes([0x17, 0x00, 0, 0, 0]) + bytes([1, 0x4D, 0x40, 0x28, 0xFF, 0xE1, 0, 4]) + b"spsx"
KEYFRAME = bytes([0x17, 0x01, 0, 0, 0]) + b"\x00" * 300
INTERFRAME = bytes([0x27, 0x01, 0, 0, 0]) + b"\x01" * 50
AAC_HEADER = bytes([0xAF, 0x00, 0x12, 0x08])
AAC_FRAME = bytes([0xAF, 0x01]) + b"\x02" * 20


def chunk_message(
    csid: int,
    type_id: int,
    stream_id: int,
    timestamp: int,
    body: bytes,
    chunk_size: int,
) -> bytes:
    """Encode one message as format 0 plus format 3 continuation chunks.

    Like FFmpeg's writer, a timestamp of 0xFFFFFF or more goes in the
    extended field, repeated on every continuation chunk.
    """
    extended = timestamp >= 0xFFFFFF
    ts_field = 0xFFFFFF if extended else timestamp
    out = bytearray()
    out += bytes([csid])
    out += ts_field.to_bytes(3, "big") + len(body).to_bytes(3, "big") + bytes([type_id])
    out += struct.pack("<I", stream_id)
    if extended:
        out += struct.pack(">I", timestamp)
    parts = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)] or [b""]
    out += parts[0]
    for part in parts[1:]:
        out += bytes([0xC0 | csid])
        if extended:
            out += struct.pack(">I", timestamp)
        out += part
    return bytes(out)


class Publisher:
    """Drive a publish session against the local stream server."""

    def __init__(self, port: int, key: str, *, tls: bool = False, app: str = "nanit") -> None:
        """Initialize."""
        self.port = port
        self.key = key
        self.tls = tls
        self.app = app
        self.chunk_size = 128
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Open the socket and run the handshake."""
        ctx = None
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=ctx)
        self.writer.write(b"\x03" + b"\x00" * HANDSHAKE_SIZE)
        await self.reader.readexactly(1 + 2 * HANDSHAKE_SIZE)
        self.writer.write(b"\x00" * HANDSHAKE_SIZE)

    def send(self, data: bytes) -> None:
        """Write raw bytes."""
        assert self.writer is not None
        self.writer.write(data)

    def command(self, *values: amf0.AmfValue, stream_id: int = 0) -> None:
        """Send an AMF0 command."""
        self.send(chunk_message(3, 20, stream_id, 0, amf0.encode_all(*values), self.chunk_size))

    async def publish(self) -> None:
        """Connect, create a stream and publish ``key``."""
        await self.connect()
        self.command("connect", 1, {"app": self.app, "type": "nonprivate"})
        self.command("releaseStream", 2, None, self.key)
        self.command("FCPublish", 3, None, self.key)
        self.command("createStream", 4, None)
        self.command("publish", 5, None, self.key, "live", stream_id=1)
        assert self.writer is not None
        await self.writer.drain()

    def media(self, type_id: int, timestamp: int, body: bytes) -> None:
        """Send one audio (8) or video (9) message on stream 1."""
        csid = 4 if type_id == 8 else 6
        self.send(chunk_message(csid, type_id, 1, timestamp, body, self.chunk_size))

    def metadata(self) -> None:
        """Send @setDataFrame onMetaData like FFmpeg does."""
        body = amf0.encode_all("@setDataFrame", "onMetaData", {"width": 1920.0, "framerate": 10.0})
        self.send(chunk_message(4, 18, 1, 0, body, self.chunk_size))

    async def close(self) -> None:
        """Close the socket."""
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
