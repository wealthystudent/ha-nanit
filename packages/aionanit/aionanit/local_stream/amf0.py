"""Minimal AMF0 encoder and decoder for RTMP commands.

Only what an RTMP publish session needs. The decoder is bounded because it
parses bytes from any host on the LAN: nesting depth, string length and
element counts are capped, and every read is checked against the buffer.
"""

from __future__ import annotations

import struct

MAX_DEPTH = 8
MAX_STRING = 64 * 1024
MAX_ELEMENTS = 1024

_NUMBER = 0x00
_BOOLEAN = 0x01
_STRING = 0x02
_OBJECT = 0x03
_NULL = 0x05
_UNDEFINED = 0x06
_ECMA_ARRAY = 0x08
_OBJECT_END = 0x09
_STRICT_ARRAY = 0x0A
_DATE = 0x0B
_LONG_STRING = 0x0C


type AmfValue = float | bool | str | dict[str, AmfValue] | list[AmfValue] | None


class Amf0Error(ValueError):
    """Raised for malformed or oversized AMF0 data."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if n < 0 or end > len(self._data):
            raise Amf0Error("truncated AMF0 data")
        chunk = self._data[self.pos : end]
        self.pos = end
        return chunk

    @property
    def remaining(self) -> int:
        return len(self._data) - self.pos


def _read_utf8(reader: _Reader, length: int) -> str:
    if length > MAX_STRING:
        raise Amf0Error("AMF0 string too long")
    return reader.take(length).decode("utf-8", errors="replace")


def _read_key(reader: _Reader) -> str:
    (length,) = struct.unpack(">H", reader.take(2))
    return _read_utf8(reader, length)


def _read_properties(reader: _Reader, depth: int) -> dict[str, AmfValue]:
    result: dict[str, AmfValue] = {}
    for _ in range(MAX_ELEMENTS + 1):
        key = _read_key(reader)
        if not key and reader.remaining and reader.take(1)[0] == _OBJECT_END:
            return result
        if not key:
            raise Amf0Error("AMF0 object missing its end marker")
        if len(result) >= MAX_ELEMENTS:
            break
        result[key] = _read_value(reader, depth + 1)
    raise Amf0Error("AMF0 object has too many properties")


def _read_value(reader: _Reader, depth: int) -> AmfValue:
    if depth > MAX_DEPTH:
        raise Amf0Error("AMF0 nesting too deep")
    marker = reader.take(1)[0]
    if marker == _NUMBER:
        number: float = struct.unpack(">d", reader.take(8))[0]
        return number
    if marker == _BOOLEAN:
        return reader.take(1)[0] != 0
    if marker == _STRING:
        (length,) = struct.unpack(">H", reader.take(2))
        return _read_utf8(reader, length)
    if marker == _LONG_STRING:
        (length,) = struct.unpack(">I", reader.take(4))
        return _read_utf8(reader, length)
    if marker == _OBJECT:
        return _read_properties(reader, depth)
    if marker == _ECMA_ARRAY:
        reader.take(4)  # advisory count, the end marker is authoritative
        return _read_properties(reader, depth)
    if marker == _STRICT_ARRAY:
        (count,) = struct.unpack(">I", reader.take(4))
        if count > MAX_ELEMENTS:
            raise Amf0Error("AMF0 array too long")
        return [_read_value(reader, depth + 1) for _ in range(count)]
    if marker in (_NULL, _UNDEFINED):
        return None
    if marker == _DATE:
        millis: float = struct.unpack(">d", reader.take(8))[0]
        reader.take(2)  # timezone, unused
        return millis
    raise Amf0Error(f"unsupported AMF0 marker {marker:#x}")


def decode_all(data: bytes) -> list[AmfValue]:
    """Decode every AMF0 value in ``data``."""
    reader = _Reader(data)
    values: list[AmfValue] = []
    while reader.remaining:
        if len(values) >= MAX_ELEMENTS:
            raise Amf0Error("too many AMF0 values")
        values.append(_read_value(reader, 0))
    return values


def first_value_length(data: bytes) -> int:
    """Return how many bytes the first AMF0 value in ``data`` occupies."""
    reader = _Reader(data)
    _read_value(reader, 0)
    return reader.pos


def _encode_string_body(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def encode(value: AmfValue) -> bytes:
    """Encode one Python value as AMF0."""
    if value is None:
        return bytes([_NULL])
    if isinstance(value, bool):
        return bytes([_BOOLEAN, 1 if value else 0])
    if isinstance(value, int | float):
        return bytes([_NUMBER]) + struct.pack(">d", float(value))
    if isinstance(value, str):
        return bytes([_STRING]) + _encode_string_body(value)
    if isinstance(value, dict):
        body = b"".join(_encode_string_body(k) + encode(v) for k, v in value.items())
        return bytes([_OBJECT]) + body + b"\x00\x00" + bytes([_OBJECT_END])
    raise Amf0Error(f"cannot encode {type(value).__name__} as AMF0")


def encode_all(*values: AmfValue) -> bytes:
    """Encode several values back to back."""
    return b"".join(encode(v) for v in values)
