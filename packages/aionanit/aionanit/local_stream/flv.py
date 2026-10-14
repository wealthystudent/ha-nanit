"""FLV framing for RTMP media.

An RTMP audio, video or data message body is exactly an FLV tag body, so
serving the push as HTTP-FLV only adds headers around the same bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18

# "FLV", version 1, audio + video present, 9 byte header, then PreviousTagSize0.
FILE_HEADER = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"

_TS_MASK = 0xFFFFFFFF


def ts_since(timestamp: int, base: int) -> int:
    """Return ``timestamp - base`` in milliseconds, wrap-safe, never negative.

    RTMP timestamps are 32 bit and wrap, so the difference is read as a
    signed 32 bit value. A tag slightly older than ``base`` (audio muxed just
    ahead of the keyframe) comes out as 0 instead of about 49.7 days.
    """
    diff = (timestamp - base) & _TS_MASK
    if diff >= 1 << 31:
        return 0
    return diff


@dataclass(frozen=True, slots=True)
class Tag:
    """One FLV tag: type, timestamp in milliseconds, body."""

    kind: int
    timestamp: int
    body: bytes

    @property
    def is_keyframe(self) -> bool:
        """Return True for an AVC keyframe NALU (not the sequence header)."""
        return (
            self.kind == TAG_VIDEO
            and len(self.body) >= 2
            and self.body[0] >> 4 == 1
            and self.body[1] == 1
        )

    @property
    def is_avc_header(self) -> bool:
        """Return True for the AVC decoder configuration record."""
        return (
            self.kind == TAG_VIDEO
            and len(self.body) >= 2
            and self.body[0] & 0x0F == 7
            and self.body[1] == 0
        )

    @property
    def is_aac_header(self) -> bool:
        """Return True for the AAC AudioSpecificConfig."""
        return (
            self.kind == TAG_AUDIO
            and len(self.body) >= 2
            and self.body[0] >> 4 == 10
            and self.body[1] == 0
        )

    def encode(self, base: int = 0) -> bytes:
        """Return the tag with header and trailing PreviousTagSize.

        ``base`` is subtracted from the timestamp (mod 2**32) so every
        subscriber's stream starts near zero whatever the camera's clock is.
        """
        ts = ts_since(self.timestamp, base)
        size = len(self.body)
        header = struct.pack(
            ">B3s3sB3s",
            self.kind,
            size.to_bytes(3, "big"),
            (ts & 0xFFFFFF).to_bytes(3, "big"),
            ts >> 24,
            b"\x00\x00\x00",
        )
        return header + self.body + struct.pack(">I", 11 + size)
