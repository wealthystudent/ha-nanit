#!/usr/bin/env python3
"""Interactive sound machine / white noise probing tool for Nanit cameras.

Connects to a real Nanit camera and probes sound-related protobuf commands
to discover white noise control capabilities:
  - PUT_PLAYBACK / GET_PLAYBACK — start/stop sound machine
  - GET_SOUNDTRACKS — list available tracks (unknown response format)
  - Settings.volume — speaker volume control
  - Unknown fields in Playback — potential track selection

Reads session from .nanit-session (created by nanit-login.py).

Usage:
    just sound                    # interactive menu
    just sound <command>          # run a single command
    just sound --list             # list all commands
    just sound start              # start playback
    just sound stop               # stop playback
    just sound set-volume 50      # set volume to 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import sys
from pathlib import Path
from typing import Any

import aiohttp

from aionanit import NanitClient
from aionanit.exceptions import NanitError
from aionanit.proto import (
    GetStatus,
    Message,
    MessageType,
    Playback,
    PlaybackStatus,
    Request,
    RequestType,
    Settings,
)
from aionanit.ws.protocol import decode_message, encode_message

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("nanit-sound")
_LOGGER.setLevel(logging.INFO)


# ── Helpers ──────────────────────────────────────────────────────────────


def _hex_dump(data: bytes, prefix: str = "  ") -> str:
    """Pretty hex dump of raw bytes."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{i:04x}  {hex_part:<48s}  {ascii_part}")
    return "\n".join(lines)


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _dump_proto_fields(msg: Any, indent: int = 0) -> str:
    """Recursively dump all protobuf fields including unknown fields."""
    lines = []
    pad = "  " * indent

    # Known fields
    for field_desc in msg.DESCRIPTOR.fields:
        is_repeated = getattr(field_desc, "is_repeated", False)
        if is_repeated:
            val = getattr(msg, field_desc.name)
            if not len(val):
                continue
        else:
            if not msg.HasField(field_desc.name):
                continue
            val = getattr(msg, field_desc.name)

        if hasattr(val, "DESCRIPTOR"):
            lines.append(f"{pad}{field_desc.name} (field {field_desc.number}):")
            lines.append(_dump_proto_fields(val, indent + 1))
        elif is_repeated:
            lines.append(f"{pad}{field_desc.name} (field {field_desc.number}): [{len(val)} items]")
            for i, item in enumerate(val):
                if hasattr(item, "DESCRIPTOR"):
                    lines.append(f"{pad}  [{i}]:")
                    lines.append(_dump_proto_fields(item, indent + 2))
                else:
                    lines.append(f"{pad}  [{i}]: {item}")
        else:
            lines.append(f"{pad}{field_desc.name} (field {field_desc.number}): {val!r}")

    # Unknown fields — parse from raw serialized bytes
    try:
        unknown = msg.UnknownFields()
        if len(unknown):
            lines.append(f"{pad}── UNKNOWN FIELDS ({len(unknown)}) ──")
            for uf in unknown:
                wire_types = {0: "varint", 1: "64-bit", 2: "length-delimited", 5: "32-bit"}
                wt = wire_types.get(uf.wire_type, f"wire_type={uf.wire_type}")
                raw = uf.data
                interpretations = [f"raw={raw!r} ({wt})"]

                if uf.wire_type == 0:
                    interpretations.append(f"as int: {raw}")
                elif uf.wire_type == 1 and isinstance(raw, bytes) and len(raw) == 8:
                    interpretations.append(f"as double: {struct.unpack('<d', raw)[0]}")
                    interpretations.append(f"as int64: {struct.unpack('<q', raw)[0]}")
                elif uf.wire_type == 5 and isinstance(raw, bytes) and len(raw) == 4:
                    interpretations.append(f"as float: {struct.unpack('<f', raw)[0]}")
                    interpretations.append(f"as int32: {struct.unpack('<i', raw)[0]}")
                elif uf.wire_type == 2 and isinstance(raw, bytes):
                    interpretations.append(f"as utf8: {raw.decode('utf-8', errors='replace')}")
                    interpretations.append(f"as hex: {raw.hex()}")

                lines.append(f"{pad}  field {uf.field_number}: {', '.join(interpretations)}")
    except NotImplementedError:
        # C (upb) protobuf doesn't support UnknownFields() — parse raw bytes
        raw_bytes = msg.SerializeToString()
        known_numbers = {f.number for f in msg.DESCRIPTOR.fields}
        unknown_fields = _parse_raw_unknown_fields(raw_bytes, known_numbers)
        if unknown_fields:
            lines.append(f"{pad}── UNKNOWN FIELDS ({len(unknown_fields)}) ──")
            for field_number, wire_type, data in unknown_fields:
                wire_types = {0: "varint", 1: "64-bit", 2: "length-delimited", 5: "32-bit"}
                wt = wire_types.get(wire_type, f"wire_type={wire_type}")
                interpretations = [f"raw={data!r} ({wt})"]

                if wire_type == 0:
                    interpretations.append(f"as int: {data}")
                elif wire_type == 1 and isinstance(data, bytes) and len(data) == 8:
                    interpretations.append(f"as double: {struct.unpack('<d', data)[0]}")
                    interpretations.append(f"as int64: {struct.unpack('<q', data)[0]}")
                elif wire_type == 5 and isinstance(data, bytes) and len(data) == 4:
                    interpretations.append(f"as float: {struct.unpack('<f', data)[0]}")
                    interpretations.append(f"as int32: {struct.unpack('<i', data)[0]}")
                elif wire_type == 2 and isinstance(data, bytes):
                    interpretations.append(f"as utf8: {data.decode('utf-8', errors='replace')}")
                    interpretations.append(f"as hex: {data.hex()}")

                lines.append(f"{pad}  field {field_number}: {', '.join(interpretations)}")

    return "\n".join(lines)


def _parse_raw_unknown_fields(
    data: bytes, known_field_numbers: set[int]
) -> list[tuple[int, int, int | bytes]]:
    """Parse protobuf wire format and extract fields not in the known set."""
    results: list[tuple[int, int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        tag = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            tag |= (b & 0x7F) << shift
            shift += 7
            pos += 1
            if not (b & 0x80):
                break
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                value |= (b & 0x7F) << shift
                shift += 7
                pos += 1
                if not (b & 0x80):
                    break
            if field_number not in known_field_numbers:
                results.append((field_number, wire_type, value))
        elif wire_type == 1:  # 64-bit fixed
            chunk = data[pos : pos + 8]
            pos += 8
            if field_number not in known_field_numbers:
                results.append((field_number, wire_type, chunk))
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                length |= (b & 0x7F) << shift
                shift += 7
                pos += 1
                if not (b & 0x80):
                    break
            chunk = data[pos : pos + length]
            pos += length
            if field_number not in known_field_numbers:
                results.append((field_number, wire_type, chunk))
        elif wire_type == 5:  # 32-bit fixed
            chunk = data[pos : pos + 4]
            pos += 4
            if field_number not in known_field_numbers:
                results.append((field_number, wire_type, chunk))
        else:
            break

    return results


def _build_raw_playback_request(request_id: int, raw_playback_bytes: bytes) -> bytes:
    """Build a PUT_PLAYBACK request with raw bytes for the playback field.

    This bypasses the typed Playback() constructor so we can inject
    arbitrary field numbers that aren't in our .proto schema.
    """
    req = Request(id=request_id, type=RequestType.PUT_PLAYBACK)
    # Field 16 (playback) with wire type 2 (length-delimited)
    tag = (16 << 3) | 2
    varint_bytes = _encode_varint(len(raw_playback_bytes))
    base = req.SerializeToString()
    raw_req = base + _encode_varint(tag) + varint_bytes + raw_playback_bytes

    # Build the full Message: type=REQUEST (1), request=<raw_req>
    msg_type_bytes = b"\x08\x01"  # field 1, varint, value 1 (REQUEST)
    req_tag = (2 << 3) | 2  # field 2, wire type 2
    req_varint = _encode_varint(len(raw_req))
    return msg_type_bytes + _encode_varint(req_tag) + req_varint + raw_req


def _build_typed_request(request_id: int, request_type: int, **fields: Any) -> bytes:
    """Build a typed protobuf request and return serialized Message bytes."""
    req = Request(id=request_id, type=request_type, **fields)
    msg = Message(type=MessageType.REQUEST, request=req)
    return encode_message(msg)


# ── Probe session ────────────────────────────────────────────────────────


class SoundSession:
    """Holds the connected camera and shared state for sound commands."""

    def __init__(self, client: NanitClient, camera: Any) -> None:
        self.client = client
        self.camera = camera
        self._request_counter = 200  # start high to avoid collisions

    def next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    async def send_typed_and_dump(
        self,
        request_type: int,
        label: str,
        **fields: Any,
    ) -> Any:
        """Send a typed request via the camera transport and dump the response."""
        request_id = self.next_id()
        data = _build_typed_request(request_id, request_type, **fields)

        print(f"\n  Sending: {label}")
        print(f"  Request ID: {request_id}")
        print(f"  Raw bytes ({len(data)}):")
        print(_hex_dump(data))

        return await self._send_and_capture(data, request_id)

    async def send_raw_and_dump(self, data: bytes, label: str) -> Any:
        """Send raw bytes over the camera transport and dump the response."""
        print(f"\n  Sending: {label}")
        print(f"  Raw bytes ({len(data)}):")
        print(_hex_dump(data))

        return await self._send_and_capture(data, None)

    async def _send_and_capture(self, data: bytes, _request_id: int | None) -> Any:
        """Send data and capture the response."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        original_handler = self.camera._on_ws_message

        def _capture(msg_data: bytes) -> None:
            msg = decode_message(msg_data)
            from aionanit.ws.protocol import extract_response

            resp = extract_response(msg)
            if resp is not None and not future.done():
                future.set_result((msg, msg_data))
            original_handler(msg_data)

        self.camera._transport._on_message = _capture
        try:
            await self.camera._transport.async_send(data)
            try:
                msg, raw_resp = await asyncio.wait_for(future, timeout=10.0)
                resp = msg.response
                print(f"\n  Response (status {resp.status_code}):")
                if resp.status_message:
                    print(f"  Status message: {resp.status_message}")
                print(f"  Raw response bytes ({len(raw_resp)}):")
                print(_hex_dump(raw_resp))

                # Dump all known response fields
                print("\n  Parsed response fields:")
                print(_dump_proto_fields(resp, indent=2))

                return resp
            except TimeoutError:
                print("\n  ⚠ No response within 10s (camera may not support this)")
                return None
        finally:
            self.camera._transport._on_message = original_handler


# ── Commands ─────────────────────────────────────────────────────────────

COMMANDS: list[dict[str, Any]] = []


def sound_command(name: str, description: str, hint: str, *, takes_arg: str | None = None):
    """Decorator to register a sound probe command."""

    def decorator(fn: Any) -> Any:
        COMMANDS.append(
            {
                "name": name,
                "description": description,
                "hint": hint,
                "run": fn,
                "takes_arg": takes_arg,
            }
        )
        return fn

    return decorator


# ── 1. GET_PLAYBACK — current playback state ────────────────────────────


@sound_command(
    name="get-playback",
    description="GET_PLAYBACK — read current sound machine playback state",
    hint=(
        "Sends GET_PLAYBACK to discover if the sound machine is running.\n"
        "  Look for: status field, unknown fields (track info?), raw bytes."
    ),
)
async def cmd_get_playback(session: SoundSession, _arg: str | None = None) -> None:
    print("  Sending GET_PLAYBACK ...")
    await session.send_typed_and_dump(
        RequestType.GET_PLAYBACK,
        "GET_PLAYBACK",
    )


# ── 2. PUT_PLAYBACK STARTED — start sound machine ───────────────────────


@sound_command(
    name="start",
    description="PUT_PLAYBACK STARTED — start the sound machine",
    hint=(
        "Sends PUT_PLAYBACK with status=STARTED (value 0).\n"
        "  This should start playing the last-used white noise track.\n"
        "  Listen to the camera speaker for audio."
    ),
)
async def cmd_start(session: SoundSession, _arg: str | None = None) -> None:
    print("  Sending PUT_PLAYBACK (status=STARTED) ...")
    playback = Playback(status=PlaybackStatus.STARTED)
    await session.send_typed_and_dump(
        RequestType.PUT_PLAYBACK,
        "PUT_PLAYBACK { status: STARTED }",
        playback=playback,
    )
    print("\n  → Listen: is the camera playing white noise now?")


# ── 3. PUT_PLAYBACK STOPPED — stop sound machine ────────────────────────


@sound_command(
    name="stop",
    description="PUT_PLAYBACK STOPPED — stop the sound machine",
    hint="Sends PUT_PLAYBACK with status=STOPPED (value 1).",
)
async def cmd_stop(session: SoundSession, _arg: str | None = None) -> None:
    print("  Sending PUT_PLAYBACK (status=STOPPED) ...")
    playback = Playback(status=PlaybackStatus.STOPPED)
    await session.send_typed_and_dump(
        RequestType.PUT_PLAYBACK,
        "PUT_PLAYBACK { status: STOPPED }",
        playback=playback,
    )
    print("\n  → Listen: did the sound stop?")


# ── 4. GET_SOUNDTRACKS — discover available tracks ──────────────────────


@sound_command(
    name="get-soundtracks",
    description="GET_SOUNDTRACKS — request list of available sound tracks",
    hint=(
        "Sends GET_SOUNDTRACKS (request type 21). The response format is UNKNOWN.\n"
        "  This is the most important discovery command — the response should reveal\n"
        "  what white noise tracks are available and how they're identified.\n"
        "  Pay close attention to unknown fields and raw bytes in the response."
    ),
)
async def cmd_get_soundtracks(session: SoundSession, _arg: str | None = None) -> None:
    print("  Sending GET_SOUNDTRACKS (request type 21) ...")
    print("  NOTE: Response format is unknown — examining raw bytes carefully.")
    await session.send_typed_and_dump(
        RequestType.GET_SOUNDTRACKS,
        "GET_SOUNDTRACKS",
    )
    print("\n  → Check the raw bytes and unknown fields above for track data!")


# ── 5. GET_SETTINGS volume — read current volume ────────────────────────


@sound_command(
    name="get-volume",
    description="GET_SETTINGS — read current speaker volume (field 9)",
    hint="Reads Settings from the camera. Volume is field 9 (int32, 0-100).",
)
async def cmd_get_volume(session: SoundSession, _arg: str | None = None) -> None:
    print("  Sending GET_SETTINGS ...")
    resp = await session.send_typed_and_dump(
        RequestType.GET_SETTINGS,
        "GET_SETTINGS",
    )
    if resp and resp.HasField("settings"):
        settings = resp.settings
        if settings.HasField("volume"):
            print(f"\n  ✓ Current volume: {settings.volume}")
        else:
            print("\n  Volume field not present in response")


# ── 6. PUT_SETTINGS volume — set speaker volume ─────────────────────────


@sound_command(
    name="set-volume",
    description="PUT_SETTINGS — set speaker volume (0-100)",
    hint=(
        "Sends PUT_SETTINGS with a volume value.\n"
        "  Usage: just sound set-volume 50\n"
        "  Range: 0 (silent) to 100 (max). Start the sound first to hear the change."
    ),
    takes_arg="VOLUME (0-100)",
)
async def cmd_set_volume(session: SoundSession, arg: str | None = None) -> None:
    if arg is None:
        arg = input("  Volume (0-100): ").strip()

    try:
        volume = int(arg)
    except ValueError:
        print(f"  ✗ Invalid volume: {arg!r} (expected integer 0-100)")
        return

    volume = max(0, min(100, volume))
    print(f"  Sending PUT_SETTINGS (volume={volume}) ...")
    settings = Settings(volume=volume)
    await session.send_typed_and_dump(
        RequestType.PUT_SETTINGS,
        f"PUT_SETTINGS {{ volume: {volume} }}",
        settings=settings,
    )
    print(f"\n  → Did the volume change? (sent value={volume})")


# ── 7. Probe unknown Playback fields — track selection? ─────────────────


@sound_command(
    name="probe-playback-fields",
    description="Probe unknown fields on Playback message (fields 2-10) for track selection",
    hint=(
        "The Playback message only defines field 1 (status). But the Nanit app\n"
        "  lets you select different white noise tracks. The track selection might\n"
        "  be an undocumented field (2, 3, 4, ...) on the Playback message.\n"
        "  We try each field as a varint with STARTED status to discover it.\n"
        "  Start playback first so you can hear if the track changes."
    ),
)
async def cmd_probe_playback_fields(session: SoundSession, _arg: str | None = None) -> None:
    print("  Probing Playback fields 2-10 as varint with status=STARTED ...")
    print("  Start playback first (run 'start') so you can hear track changes.\n")

    for field_num in range(2, 11):
        for value in [0, 1, 2, 3, 5, 10]:
            input(
                f"  Press Enter to send Playback {{ status: STARTED, "
                f"field_{field_num}: {value} }} ..."
            )

            # Build: status=STARTED (field 1, varint 0) + unknown field
            raw_playback = (
                _encode_varint((1 << 3) | 0)  # field 1 (status), wire type 0
                + _encode_varint(0)  # STARTED = 0
                + _encode_varint((field_num << 3) | 0)  # unknown field, wire type 0
                + _encode_varint(value)
            )
            data = _build_raw_playback_request(session.next_id(), raw_playback)
            await session.send_raw_and_dump(
                data,
                f"PUT_PLAYBACK {{ status: STARTED, field_{field_num}: {value} (varint) }}",
            )
            print(f"\n  → Listen: did the sound change? (field {field_num}, value={value})")

        print(f"\n  Done probing field {field_num}.")
        cont = input("  Continue to next field? (y/n): ").strip().lower()
        if cont != "y":
            break


# ── 8. Probe Playback with string field — track name? ───────────────────


@sound_command(
    name="probe-playback-strings",
    description="Probe Playback with string fields (track name/ID as UTF-8)",
    hint=(
        "Track selection might use a string field (track name or UUID).\n"
        "  We try common white noise track names on fields 2-5.\n"
        "  Start playback first to hear if the track changes."
    ),
)
async def cmd_probe_playback_strings(session: SoundSession, _arg: str | None = None) -> None:
    # Reasonable guesses for track identifiers
    track_guesses = [
        "white_noise",
        "rain",
        "ocean",
        "fan",
        "shush",
        "heartbeat",
        "lullaby",
    ]

    print("  Probing Playback string fields for track selection ...")
    print(f"  Track name guesses: {track_guesses}\n")

    for field_num in [2, 3, 4, 5]:
        for track_name in track_guesses:
            input(
                f"  Press Enter to send Playback {{ status: STARTED, "
                f'field_{field_num}: "{track_name}" }} ...'
            )

            track_bytes = track_name.encode("utf-8")
            raw_playback = (
                _encode_varint((1 << 3) | 0)  # field 1 (status), varint
                + _encode_varint(0)  # STARTED = 0
                + _encode_varint((field_num << 3) | 2)  # unknown field, wire type 2 (string)
                + _encode_varint(len(track_bytes))
                + track_bytes
            )
            data = _build_raw_playback_request(session.next_id(), raw_playback)
            await session.send_raw_and_dump(
                data,
                f'PUT_PLAYBACK {{ status: STARTED, field_{field_num}: "{track_name}" }}',
            )
            print(f"\n  → Listen: did the track change? (field {field_num}, value={track_name!r})")

        cont = input(f"\n  Done probing field {field_num}. Continue? (y/n): ").strip().lower()
        if cont != "y":
            break


# ── 9. Select track — PUT_PLAYBACK with track sub-message on field 3 ───


@sound_command(
    name="select-track",
    description="PUT_PLAYBACK with track selection via field 3 (Soundtrack sub-message)",
    hint=(
        "Confirmed: field 3 on Playback selects the track to play.\n"
        "  Sends PUT_PLAYBACK { status: STARTED, track: { type: 0, filename: X } }.\n"
        "  Available tracks: White Noise.wav, Birds.wav, Waves.wav, Wind.wav"
    ),
    takes_arg="TRACK (filename, e.g. Birds.wav)",
)
async def cmd_select_track(session: SoundSession, arg: str | None = None) -> None:
    tracks = ["White Noise.wav", "Birds.wav", "Waves.wav", "Wind.wav"]

    if arg is None:
        print("  Available tracks:")
        for i, t in enumerate(tracks):
            print(f"    {i}: {t}")
        choice = input("  Select track (number or filename): ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(tracks):
                arg = tracks[idx]
            else:
                print(f"  ✗ Invalid index: {choice}")
                return
        else:
            arg = choice

    if arg not in tracks:
        print(f"  ⚠ Unknown track: {arg!r} (trying anyway)")

    # Build Soundtrack sub-message: { field 1: varint 0, field 2: string filename }
    filename_bytes = arg.encode("utf-8")
    soundtrack_submsg = (
        _encode_varint((1 << 3) | 0)  # field 1 (type), wire type 0
        + _encode_varint(0)  # type = 0 (built-in)
        + _encode_varint((2 << 3) | 2)  # field 2 (filename), wire type 2
        + _encode_varint(len(filename_bytes))
        + filename_bytes
    )

    # Build Playback: { field 1: STARTED, field 3: Soundtrack sub-message }
    raw_playback = (
        _encode_varint((1 << 3) | 0)  # field 1 (status), wire type 0
        + _encode_varint(0)  # STARTED = 0
        + _encode_varint((3 << 3) | 2)  # field 3 (track), wire type 2
        + _encode_varint(len(soundtrack_submsg))
        + soundtrack_submsg
    )

    data = _build_raw_playback_request(session.next_id(), raw_playback)
    await session.send_raw_and_dump(
        data,
        f'PUT_PLAYBACK {{ status: STARTED, track: "{arg}" }}',
    )
    print(f'\n  → Listen: is "{arg}" playing now?')


# ── 10. Custom playback probe ───────────────────────────────────────────


@sound_command(
    name="custom",
    description="Send a custom PUT_PLAYBACK with any field number, type, and value",
    hint="For follow-up probing once you have leads from the other commands.",
)
async def cmd_custom(session: SoundSession, _arg: str | None = None) -> None:
    print("  Build a custom PUT_PLAYBACK probe:")
    field_num = int(input("  Field number: "))
    print("  Wire types: 0=varint(int), 2=string/bytes, 5=float32")
    wire_type = int(input("  Wire type (0/2/5): "))

    if wire_type == 2:
        value_str = input("  String value: ")
        value_bytes = value_str.encode("utf-8")
        extra = (
            _encode_varint((field_num << 3) | 2) + _encode_varint(len(value_bytes)) + value_bytes
        )
        label_val = f'"{value_str}"'
    elif wire_type == 5:
        value = float(input("  Float value: "))
        extra = _encode_varint((field_num << 3) | 5) + struct.pack("<f", value)
        label_val = str(value)
    else:
        value = int(input("  Integer value: "))
        extra = _encode_varint((field_num << 3) | 0) + _encode_varint(value)
        label_val = str(value)

    include_started = input("  Include status=STARTED? (y/n): ").strip().lower() == "y"

    if include_started:
        raw_playback = (
            _encode_varint((1 << 3) | 0)  # status field
            + _encode_varint(0)  # STARTED
            + extra
        )
        label = f"PUT_PLAYBACK {{ status: STARTED, field_{field_num}: {label_val} }}"
    else:
        raw_playback = extra
        label = f"PUT_PLAYBACK {{ field_{field_num}: {label_val} }}"

    data = _build_raw_playback_request(session.next_id(), raw_playback)
    await session.send_raw_and_dump(data, label)
    print("\n  → Check: any observable change?")


# ── 10. Full state dump ─────────────────────────────────────────────────


@sound_command(
    name="dump-state",
    description="Dump full camera state including settings (volume) and playback",
    hint="Get a complete picture of sound-related camera state.",
)
async def cmd_dump_state(session: SoundSession, _arg: str | None = None) -> None:
    cam = session.camera

    print("  Requesting GET_SETTINGS ...")
    try:
        resp = await cam._send_request(RequestType.GET_SETTINGS)
        print("  Settings:")
        print(_dump_proto_fields(resp, indent=2))
        if resp.HasField("settings") and resp.settings.HasField("volume"):
            print(f"\n  → Volume: {resp.settings.volume}")
    except (NanitError, TimeoutError) as err:
        print(f"  GET_SETTINGS failed: {err}")

    print("\n  Requesting GET_PLAYBACK ...")
    try:
        resp_data = await session.send_typed_and_dump(
            RequestType.GET_PLAYBACK,
            "GET_PLAYBACK",
        )
        if resp_data is None:
            print("  → No response (camera may not support GET_PLAYBACK)")
    except (NanitError, TimeoutError) as err:
        print(f"  GET_PLAYBACK failed: {err}")

    print("\n  Requesting GET_SOUNDTRACKS ...")
    try:
        resp_data = await session.send_typed_and_dump(
            RequestType.GET_SOUNDTRACKS,
            "GET_SOUNDTRACKS",
        )
        if resp_data is None:
            print("  → No response (camera may not support GET_SOUNDTRACKS)")
    except (NanitError, TimeoutError) as err:
        print(f"  GET_SOUNDTRACKS failed: {err}")

    print("\n  Requesting GET_STATUS ...")
    try:
        resp = await cam._send_request(
            RequestType.GET_STATUS,
            get_status=GetStatus(all=True),
        )
        print("  Status:")
        print(_dump_proto_fields(resp, indent=2))
    except (NanitError, TimeoutError) as err:
        print(f"  GET_STATUS failed: {err}")


# ── Menu ─────────────────────────────────────────────────────────────────


def _print_menu() -> None:
    print("\n" + "=" * 65)
    print("  Nanit Sound Machine Probe Tool")
    print("=" * 65)
    for i, cmd in enumerate(COMMANDS, 1):
        arg_hint = f" <{cmd['takes_arg']}>" if cmd.get("takes_arg") else ""
        print(f"  {i:2d}. [{cmd['name']}{arg_hint}] {cmd['description']}")
    print(f"  {len(COMMANDS) + 1:2d}. [quit] Exit")
    print("=" * 65)


async def _run_interactive(session: SoundSession) -> None:
    """Run the interactive command menu."""
    while True:
        _print_menu()
        choice = input("\n  Select command (number or name): ").strip()

        if choice.lower() in ("q", "quit", "exit", str(len(COMMANDS) + 1)):
            print("  Bye!")
            return

        # Parse "name arg" or just "name"
        parts = choice.split(maxsplit=1)
        choice_name = parts[0]
        choice_arg = parts[1] if len(parts) > 1 else None

        # Find command by number or name
        cmd = None
        if choice_name.isdigit():
            idx = int(choice_name) - 1
            if 0 <= idx < len(COMMANDS):
                cmd = COMMANDS[idx]
        else:
            for c in COMMANDS:
                if c["name"] == choice_name:
                    cmd = c
                    break

        if cmd is None:
            print(f"  Unknown command: {choice_name}")
            continue

        print(f"\n  ── {cmd['name']} ──")
        print(f"  {cmd['description']}")
        print(f"\n  Hint: {cmd['hint']}")
        confirm = input("\n  Run this command? (y/n): ").strip().lower()
        if confirm != "y":
            continue

        try:
            await cmd["run"](session, choice_arg)
        except Exception as err:
            print(f"\n  ✗ Error: {err}")
            _LOGGER.exception("Command failed")

        input("\n  Press Enter to continue ...")


# ── Main ─────────────────────────────────────────────────────────────────


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive sound machine probing tool for Nanit cameras.",
    )
    parser.add_argument("command", nargs="?", help="Run a specific command (see --list)")
    parser.add_argument("args", nargs="*", help="Command arguments (e.g., volume level)")
    parser.add_argument("--list", action="store_true", help="List all available commands")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("aionanit").setLevel(logging.DEBUG)

    if args.list:
        print("Available commands:")
        for cmd in COMMANDS:
            arg_hint = f" <{cmd['takes_arg']}>" if cmd.get("takes_arg") else ""
            print(f"  {cmd['name']}{arg_hint:30s} {cmd['description']}")
        return 0

    # Load session
    if not SESSION_FILE.exists():
        print("No session found. Run: just login", file=sys.stderr)
        return 1

    data = json.loads(SESSION_FILE.read_text())
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    camera_uid = data["camera_uid"]
    baby_uid = data["baby_uid"]
    baby_name = data.get("baby_name", "unknown")

    print(f"  Session: baby={baby_name}, camera={camera_uid}")

    async with aiohttp.ClientSession() as http_session:
        client = NanitClient(http_session)
        client.restore_tokens(access_token, refresh_token)

        camera = client.camera(camera_uid, baby_uid, prefer_local=False)

        print("  Connecting to camera via cloud ...")
        try:
            await camera.async_start()
        except (NanitError, OSError) as err:
            print(f"  ✗ Failed to connect: {err}", file=sys.stderr)
            return 1
        print("  ✓ Connected!")

        session = SoundSession(client, camera)

        try:
            if args.command:
                # Find command
                cmd = None
                for c in COMMANDS:
                    if c["name"] == args.command:
                        cmd = c
                        break
                if cmd is None:
                    print(f"Unknown command: {args.command}", file=sys.stderr)
                    print("Use --list to see available commands", file=sys.stderr)
                    return 1

                cmd_arg = " ".join(args.args) if args.args else None

                print(f"\n  ── {cmd['name']} ──")
                print(f"  {cmd['description']}")
                await cmd["run"](session, cmd_arg)
            else:
                # Interactive menu
                await _run_interactive(session)
        finally:
            print("\n  Disconnecting ...")
            await camera.async_stop()
            await client.async_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
