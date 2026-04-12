#!/usr/bin/env python3
"""Interactive hardware probing tool for Nanit cameras.

Connects to a real Nanit camera using the aionanit library and lets you
send targeted protobuf commands one at a time to discover undocumented
fields (e.g. night light brightness).

Reads session from .nanit-session (created by nanit-login.py).

Usage:
    python3 tools/nanit-probe.py              # interactive menu
    python3 tools/nanit-probe.py <command>    # run a single command
    python3 tools/nanit-probe.py --list       # list all commands
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
    Control,
    ControlNightLight,
    GetControl,
    GetStatus,
    Request,
    RequestType,
)
from aionanit.ws.protocol import decode_message

SESSION_FILE = Path(__file__).resolve().parents[1] / ".nanit-session"

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("nanit-probe")
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

    # Unknown fields (the gold we're hunting for)
    try:
        unknown = msg.UnknownFields()
        if len(unknown):
            lines.append(f"{pad}── UNKNOWN FIELDS ({len(unknown)}) ──")
            for uf in unknown:
                wire_types = {0: "varint", 1: "64-bit", 2: "length-delimited", 5: "32-bit"}
                wt = wire_types.get(uf.wire_type, f"wire_type={uf.wire_type}")
                raw = uf.data
                interpretations = [f"raw={raw!r} ({wt})"]

                if uf.wire_type == 0:  # varint
                    interpretations.append(f"as int: {raw}")
                elif uf.wire_type == 1 and isinstance(raw, bytes) and len(raw) == 8:  # 64-bit
                    interpretations.append(f"as double: {struct.unpack('<d', raw)[0]}")
                    interpretations.append(f"as int64: {struct.unpack('<q', raw)[0]}")
                elif uf.wire_type == 5 and isinstance(raw, bytes) and len(raw) == 4:  # 32-bit
                    interpretations.append(f"as float: {struct.unpack('<f', raw)[0]}")
                    interpretations.append(f"as int32: {struct.unpack('<i', raw)[0]}")
                elif uf.wire_type == 2 and isinstance(raw, bytes):  # length-delimited
                    interpretations.append(f"as utf8: {raw.decode('utf-8', errors='replace')}")
                    interpretations.append(f"as hex: {raw.hex()}")

                lines.append(f"{pad}  field {uf.field_number}: {', '.join(interpretations)}")
    except NotImplementedError:
        # C (upb) protobuf doesn't support UnknownFields() — parse raw bytes instead
        raw_bytes = msg.SerializeToString()
        known_numbers = {f.number for f in msg.DESCRIPTOR.fields}
        unknown_fields = _parse_raw_unknown_fields(raw_bytes, known_numbers)
        if unknown_fields:
            lines.append(f"{pad}── UNKNOWN FIELDS ({len(unknown_fields)}) ──")
            for field_number, wire_type, data in unknown_fields:
                wire_types = {0: "varint", 1: "64-bit", 2: "length-delimited", 5: "32-bit"}
                wt = wire_types.get(wire_type, f"wire_type={wire_type}")
                interpretations = [f"raw={data!r} ({wt})"]

                if wire_type == 0:  # varint
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


def _build_raw_control_request(request_id: int, raw_control_bytes: bytes) -> bytes:
    """Build a PUT_CONTROL request with raw bytes for the control field.

    This bypasses the typed Control() constructor so we can inject
    arbitrary field numbers that aren't in our .proto schema.
    """
    # Build a Request proto, then splice in raw control bytes at field 15.
    req = Request(id=request_id, type=RequestType.PUT_CONTROL)
    # Field 15 (control) with wire type 2 (length-delimited)
    tag = (15 << 3) | 2
    # Encode the varint length
    length = len(raw_control_bytes)
    varint_bytes = _encode_varint(length)
    # Serialize the request without control, then append the raw field
    base = req.SerializeToString()
    raw_req = base + _encode_varint(tag) + varint_bytes + raw_control_bytes

    # Build the full Message manually (bypassing typed constructor)
    # Field 1 = type (varint), Field 2 = request (length-delimited)
    msg_type_bytes = b"\x08\x01"  # field 1, varint, value 1 (REQUEST)
    req_tag = (2 << 3) | 2  # field 2, wire type 2
    req_varint = _encode_varint(len(raw_req))
    return msg_type_bytes + _encode_varint(req_tag) + req_varint + raw_req


def _build_raw_settings_request(request_id: int, raw_settings_bytes: bytes) -> bytes:
    """Build a PUT_SETTINGS request with raw bytes for the settings field.

    Same approach as _build_raw_control_request but targets field 5 (settings)
    with request type PUT_SETTINGS.
    """
    req = Request(id=request_id, type=RequestType.PUT_SETTINGS)
    # Field 5 (settings) with wire type 2 (length-delimited)
    tag = (5 << 3) | 2
    length = len(raw_settings_bytes)
    varint_bytes = _encode_varint(length)
    base = req.SerializeToString()
    raw_req = base + _encode_varint(tag) + varint_bytes + raw_settings_bytes

    msg_type_bytes = b"\x08\x01"  # field 1, varint, value 1 (REQUEST)
    req_tag = (2 << 3) | 2  # field 2, wire type 2
    req_varint = _encode_varint(len(raw_req))
    return msg_type_bytes + _encode_varint(req_tag) + req_varint + raw_req


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _make_raw_control_with_field(field_number: int, wire_type: int, value: Any) -> bytes:
    """Build raw protobuf bytes for a Control message with an arbitrary field.

    wire_type 0 = varint (int)
    wire_type 5 = 32-bit fixed (float or fixed32)
    """
    tag = (field_number << 3) | wire_type
    data = bytearray()
    data.extend(_encode_varint(tag))

    if wire_type == 0:  # varint
        data.extend(_encode_varint(int(value)))
    elif wire_type == 5:  # 32-bit fixed
        if isinstance(value, float):
            data.extend(struct.pack("<f", value))
        else:
            data.extend(struct.pack("<I", int(value)))
    elif wire_type == 1:  # 64-bit fixed
        if isinstance(value, float):
            data.extend(struct.pack("<d", value))
        else:
            data.extend(struct.pack("<Q", int(value)))

    return bytes(data)


def _make_raw_control_with_nightlight_and_field(
    field_number: int, wire_type: int, value: Any
) -> bytes:
    """Build raw Control bytes with night_light=ON plus an arbitrary extra field."""
    # night_light = field 3, varint, value 1 (LIGHT_ON)
    nl_bytes = _encode_varint((3 << 3) | 0) + _encode_varint(1)
    extra = _make_raw_control_with_field(field_number, wire_type, value)
    return nl_bytes + extra


# ── Probe commands ───────────────────────────────────────────────────────

# Each command is a dict with: name, description, hint, run(camera, pending_counter)
# run() should be an async function that returns after one interaction.


class ProbeSession:
    """Holds the connected camera and shared state for probe commands."""

    def __init__(self, client: NanitClient, camera: Any) -> None:
        self.client = client
        self.camera = camera
        self._request_counter = 100  # start high to avoid collisions

    def next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    async def send_raw_and_dump(self, data: bytes, label: str) -> None:
        """Send raw bytes over the camera transport and wait for response."""
        print(f"\n  Sending: {label}")
        print(f"  Raw bytes ({len(data)}):")
        print(_hex_dump(data))

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
                print(f"\n  Response (status {msg.response.status_code}):")
                print(f"  Raw response bytes ({len(raw_resp)}):")
                print(_hex_dump(raw_resp))
                if msg.response.HasField("control"):
                    print("\n  Parsed control fields:")
                    print(_dump_proto_fields(msg.response.control, indent=2))
                elif msg.response.status_code != 200:
                    print(f"  Status message: {msg.response.status_message}")
            except TimeoutError:
                print("\n  ⚠ No response within 10s (camera may not support this)")
        finally:
            self.camera._transport._on_message = original_handler


COMMANDS: list[dict[str, Any]] = []


def probe_command(name: str, description: str, hint: str):
    """Decorator to register a probe command."""

    def decorator(fn):
        COMMANDS.append(
            {
                "name": name,
                "description": description,
                "hint": hint,
                "run": fn,
            }
        )
        return fn

    return decorator


# ── 1. Baseline: read current control state ──────────────────────────────


@probe_command(
    name="read-control",
    description="GET_CONTROL — read current control state and dump all fields",
    hint=(
        "Establishes a baseline. Look for any UNKNOWN FIELDS in the output.\n"
        "  If the camera has brightness state, it might show up as an unknown field here.\n"
        "  Also check the raw bytes for any data beyond the known fields."
    ),
)
async def cmd_read_control(session: ProbeSession) -> None:
    print("  Sending GET_CONTROL (night_light=True, night_light_timeout=True) ...")
    resp = await session.camera._send_request(
        RequestType.GET_CONTROL,
        get_control=GetControl(night_light=True, night_light_timeout=True),
    )
    print("\n  Parsed response fields:")
    print(_dump_proto_fields(resp, indent=2))

    # Also show raw serialized form
    raw = resp.SerializeToString()
    print(f"\n  Raw serialized response ({len(raw)} bytes):")
    print(_hex_dump(raw))


# ── 2. Read control with ALL boolean flags ───────────────────────────────


@probe_command(
    name="read-control-all",
    description="GET_CONTROL with all known boolean flags set to True",
    hint=(
        "Requests all known sub-fields. The camera might return extra data\n"
        "  when asked for everything vs just night_light."
    ),
)
async def cmd_read_control_all(session: ProbeSession) -> None:
    print(
        "  Sending GET_CONTROL (ptz=True, night_light=True, night_light_timeout=True, sensor_data_transfer_en=True) ..."
    )
    resp = await session.camera._send_request(
        RequestType.GET_CONTROL,
        get_control=GetControl(
            ptz=True,
            night_light=True,
            night_light_timeout=True,
            sensor_data_transfer_en=True,
        ),
    )
    print("\n  Parsed response fields:")
    print(_dump_proto_fields(resp, indent=2))

    raw = resp.SerializeToString()
    print(f"\n  Raw serialized response ({len(raw)} bytes):")
    print(_hex_dump(raw))


# ── 3. Turn night light ON (baseline) ────────────────────────────────────


@probe_command(
    name="light-on",
    description="PUT_CONTROL — turn night light ON (known command, baseline)",
    hint=(
        "This is the known working command. Verify the light turns on.\n"
        "  Compare the response fields with later probes."
    ),
)
async def cmd_light_on(session: ProbeSession) -> None:
    print("  Sending PUT_CONTROL (night_light=LIGHT_ON) ...")
    resp = await session.camera._send_request(
        RequestType.PUT_CONTROL,
        control=Control(night_light=ControlNightLight.LIGHT_ON),
    )
    print("\n  Parsed response fields:")
    print(_dump_proto_fields(resp, indent=2))
    print("\n  → Check: did the night light turn ON on the camera?")


# ── 4. Turn night light OFF ──────────────────────────────────────────────


@probe_command(
    name="light-off",
    description="PUT_CONTROL — turn night light OFF",
    hint="Turns the light off. Use between brightness probes to reset state.",
)
async def cmd_light_off(session: ProbeSession) -> None:
    print("  Sending PUT_CONTROL (night_light=LIGHT_OFF) ...")
    resp = await session.camera._send_request(
        RequestType.PUT_CONTROL,
        control=Control(night_light=ControlNightLight.LIGHT_OFF),
    )
    print("\n  Parsed response fields:")
    print(_dump_proto_fields(resp, indent=2))
    print("\n  → Check: did the night light turn OFF?")


# ── 5. Probe night light timeout ────────────────────────────────────────


@probe_command(
    name="probe-timeout",
    description="PUT_CONTROL with night_light=ON + night_light_timeout via typed API",
    hint=(
        "Tests the night_light_timeout field (field 6 on Control) using the typed proto.\n"
        "  The app supports 15, 30, 60 minutes — but the wire value unit is unknown.\n"
        "  We try: 15 (minutes?), 900 (seconds for 15min?), then read back."
    ),
)
async def cmd_probe_timeout(session: ProbeSession) -> None:
    cam = session.camera

    input("\n  Press Enter to send light ON + timeout=15 (might be minutes) ...")
    resp = await cam._send_request(
        RequestType.PUT_CONTROL,
        control=Control(
            night_light=ControlNightLight.LIGHT_ON,
            night_light_timeout=15,
        ),
    )
    print("  Response:")
    print(_dump_proto_fields(resp, indent=2))

    input("\n  Press Enter to read back control state ...")
    resp = await cam._send_request(
        RequestType.GET_CONTROL,
        get_control=GetControl(night_light=True, night_light_timeout=True),
    )
    print("  Control state after timeout=15:")
    print(_dump_proto_fields(resp, indent=2))

    input("\n  Press Enter to try timeout=900 (seconds for 15min) ...")
    resp = await cam._send_request(
        RequestType.PUT_CONTROL,
        control=Control(
            night_light=ControlNightLight.LIGHT_ON,
            night_light_timeout=900,
        ),
    )
    print("  Response:")
    print(_dump_proto_fields(resp, indent=2))

    input("\n  Press Enter to read back control state ...")
    resp = await cam._send_request(
        RequestType.GET_CONTROL,
        get_control=GetControl(night_light=True, night_light_timeout=True),
    )
    print("  Control state after timeout=900:")
    print(_dump_proto_fields(resp, indent=2))

    print("\n  → Check: did the app show a timer? Did the readback include a timeout value?")
    print("  → If 15 worked, unit is minutes. If 900 worked, unit is seconds.")


# ── 6. Probe field 1 as varint (int) ────────────────────────────────────


@probe_command(
    name="probe-field1-int",
    description="PUT_CONTROL with undocumented field 1 as varint (values: 0, 50, 100, 255)",
    hint=(
        "Field 1 is UNASSIGNED in the known proto. We try setting it as an integer.\n"
        "  If it's a brightness field, the light intensity should change.\n"
        "  The light must be ON first (run light-on before this).\n"
        "  Watch the camera between each value."
    ),
)
async def cmd_probe_field1_int(session: ProbeSession) -> None:
    for value in [50, 100, 255, 0]:
        input(f"\n  Press Enter to send field 1 = {value} (varint) ...")
        raw_control = _make_raw_control_with_field(1, 0, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(data, f"PUT_CONTROL {{ field_1: {value} (varint) }}")
        print(f"\n  → Check: any change in light brightness? (sent value={value})")


# ── 6. Probe field 2 as varint (int) ────────────────────────────────────


@probe_command(
    name="probe-field2-int",
    description="PUT_CONTROL with undocumented field 2 as varint (values: 0, 50, 100, 255)",
    hint=("Field 2 is also UNASSIGNED. Same idea as field 1.\n  The light must be ON first."),
)
async def cmd_probe_field2_int(session: ProbeSession) -> None:
    for value in [50, 100, 255, 0]:
        input(f"\n  Press Enter to send field 2 = {value} (varint) ...")
        raw_control = _make_raw_control_with_field(2, 0, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(data, f"PUT_CONTROL {{ field_2: {value} (varint) }}")
        print(f"\n  → Check: any change in light brightness? (sent value={value})")


# ── 7. Probe field 1 as float (32-bit fixed) ────────────────────────────


@probe_command(
    name="probe-field1-float",
    description="PUT_CONTROL with field 1 as float32 (values: 0.0, 0.25, 0.5, 1.0)",
    hint=(
        "The Sound & Light machine uses float 0.0-1.0 for brightness.\n"
        "  Maybe the camera night light does too.\n"
        "  The light must be ON first."
    ),
)
async def cmd_probe_field1_float(session: ProbeSession) -> None:
    for value in [0.25, 0.5, 1.0, 0.0]:
        input(f"\n  Press Enter to send field 1 = {value} (float32) ...")
        raw_control = _make_raw_control_with_field(1, 5, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(data, f"PUT_CONTROL {{ field_1: {value} (float32) }}")
        print(f"\n  → Check: any change in light brightness? (sent value={value})")


# ── 8. Probe field 2 as float (32-bit fixed) ────────────────────────────


@probe_command(
    name="probe-field2-float",
    description="PUT_CONTROL with field 2 as float32 (values: 0.0, 0.25, 0.5, 1.0)",
    hint=("Same as above but for field 2.\n  The light must be ON first."),
)
async def cmd_probe_field2_float(session: ProbeSession) -> None:
    for value in [0.25, 0.5, 1.0, 0.0]:
        input(f"\n  Press Enter to send field 2 = {value} (float32) ...")
        raw_control = _make_raw_control_with_field(2, 5, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(data, f"PUT_CONTROL {{ field_2: {value} (float32) }}")
        print(f"\n  → Check: any change in light brightness? (sent value={value})")


# ── 9. Combined: light ON + field 1 int ──────────────────────────────────


@probe_command(
    name="probe-light-field1-int",
    description="PUT_CONTROL with night_light=ON AND field 1 as varint in same message",
    hint=(
        "Some cameras need the light-on command in the SAME message as brightness.\n"
        "  This combines night_light=ON (field 3) with field 1 values."
    ),
)
async def cmd_probe_light_field1_int(session: ProbeSession) -> None:
    for value in [25, 50, 75, 100]:
        input(f"\n  Press Enter to send night_light=ON + field 1 = {value} (varint) ...")
        raw_control = _make_raw_control_with_nightlight_and_field(1, 0, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(
            data, f"PUT_CONTROL {{ night_light: ON, field_1: {value} (varint) }}"
        )
        print(f"\n  → Check: did the light brightness change? (value={value})")


# ── 10. Combined: light ON + field 2 int ─────────────────────────────────


@probe_command(
    name="probe-light-field2-int",
    description="PUT_CONTROL with night_light=ON AND field 2 as varint in same message",
    hint="Same as above but for field 2.",
)
async def cmd_probe_light_field2_int(session: ProbeSession) -> None:
    for value in [25, 50, 75, 100]:
        input(f"\n  Press Enter to send night_light=ON + field 2 = {value} (varint) ...")
        raw_control = _make_raw_control_with_nightlight_and_field(2, 0, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(
            data, f"PUT_CONTROL {{ night_light: ON, field_2: {value} (varint) }}"
        )
        print(f"\n  → Check: did the light brightness change? (value={value})")


# ── 11. Combined: light ON + field 1 float ───────────────────────────────


@probe_command(
    name="probe-light-field1-float",
    description="PUT_CONTROL with night_light=ON AND field 1 as float32 in same message",
    hint="Combines ON + float brightness in one message for field 1.",
)
async def cmd_probe_light_field1_float(session: ProbeSession) -> None:
    for value in [0.1, 0.25, 0.5, 0.75, 1.0]:
        input(f"\n  Press Enter to send night_light=ON + field 1 = {value} (float32) ...")
        raw_control = _make_raw_control_with_nightlight_and_field(1, 5, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(
            data, f"PUT_CONTROL {{ night_light: ON, field_1: {value} (float32) }}"
        )
        print(f"\n  → Check: did the light brightness change? (value={value})")


# ── 12. Combined: light ON + field 2 float ───────────────────────────────


@probe_command(
    name="probe-light-field2-float",
    description="PUT_CONTROL with night_light=ON AND field 2 as float32 in same message",
    hint="Combines ON + float brightness in one message for field 2.",
)
async def cmd_probe_light_field2_float(session: ProbeSession) -> None:
    for value in [0.1, 0.25, 0.5, 0.75, 1.0]:
        input(f"\n  Press Enter to send night_light=ON + field 2 = {value} (float32) ...")
        raw_control = _make_raw_control_with_nightlight_and_field(2, 5, value)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(
            data, f"PUT_CONTROL {{ night_light: ON, field_2: {value} (float32) }}"
        )
        print(f"\n  → Check: did the light brightness change? (value={value})")


# ── 13. Probe higher field numbers ───────────────────────────────────────


@probe_command(
    name="probe-fields-7-10",
    description="PUT_CONTROL probing fields 7-10 as varint with night_light=ON",
    hint=(
        "Brightness might be on a higher field number, not just 1 or 2.\n"
        "  Fields 3-6 are known. We try 7, 8, 9, 10."
    ),
)
async def cmd_probe_fields_7_10(session: ProbeSession) -> None:
    for field_num in [7, 8, 9, 10]:
        input(f"\n  Press Enter to send night_light=ON + field {field_num} = 100 (varint) ...")
        raw_control = _make_raw_control_with_nightlight_and_field(field_num, 0, 100)
        data = _build_raw_control_request(session.next_id(), raw_control)
        await session.send_raw_and_dump(
            data, f"PUT_CONTROL {{ night_light: ON, field_{field_num}: 100 (varint) }}"
        )
        print(f"\n  → Check: any change? (field {field_num}, value=100)")


# ── 14. Probe night_light_brightness (Settings field 24) ────────────────


@probe_command(
    name="probe-brightness",
    description="PUT_SETTINGS with night_light_brightness (field 24) values: 0, 25, 50, 75, 100",
    hint=(
        "Brightness is on Settings (field 24, int32), NOT on Control.\n"
        "  Discovered via eyalmichon/nanit-bridge. Range: 0-100.\n"
        "  Turn the light ON first, then watch for brightness changes."
    ),
)
async def cmd_probe_brightness(session: ProbeSession) -> None:
    for brightness in [100, 75, 50, 25, 0, 100]:
        input(
            f"\n  Press Enter to send PUT_SETTINGS {{ night_light_brightness: {brightness} }} ..."
        )

        # Settings field 24, wire type 0 (varint)
        raw_settings = _encode_varint((24 << 3) | 0) + _encode_varint(brightness)
        data = _build_raw_settings_request(session.next_id(), raw_settings)
        await session.send_raw_and_dump(
            data, f"PUT_SETTINGS {{ night_light_brightness: {brightness} }}"
        )
        print(f"\n  → Watch the night light! Did brightness change? (sent value={brightness})")


# ── 15. Custom field probe ────────────────────────────────────────────────


@probe_command(
    name="custom",
    description="Send a custom PUT_CONTROL with any field number, type, and value",
    hint="For follow-up probing once you have leads from the other commands.",
)
async def cmd_custom(session: ProbeSession) -> None:
    print("  Build a custom PUT_CONTROL probe:")
    field_num = int(input("  Field number: "))
    print("  Wire types: 0=varint(int), 5=float32, 1=float64")
    wire_type = int(input("  Wire type (0/5/1): "))
    value_str = input("  Value: ")
    value: int | float = float(value_str) if "." in value_str else int(value_str)
    include_nl = input("  Include night_light=ON? (y/n): ").strip().lower() == "y"

    if include_nl:
        raw_control = _make_raw_control_with_nightlight_and_field(field_num, wire_type, value)
        label = f"PUT_CONTROL {{ night_light: ON, field_{field_num}: {value} }}"
    else:
        raw_control = _make_raw_control_with_field(field_num, wire_type, value)
        label = f"PUT_CONTROL {{ field_{field_num}: {value} }}"

    data = _build_raw_control_request(session.next_id(), raw_control)
    await session.send_raw_and_dump(data, label)
    print("\n  → Check: any observable change?")


# ── 16. Read full state dump ─────────────────────────────────────────────


@probe_command(
    name="dump-state",
    description="Read and dump full camera state (status + settings + sensors + control)",
    hint="Get a complete picture of everything the camera reports.",
)
async def cmd_dump_state(session: ProbeSession) -> None:
    cam = session.camera

    print("  Requesting GET_STATUS ...")
    try:
        resp = await cam._send_request(
            RequestType.GET_STATUS,
            get_status=GetStatus(all=True),
        )
        print("  Status:")
        print(_dump_proto_fields(resp, indent=2))
    except (NanitError, TimeoutError) as err:
        print(f"  GET_STATUS failed: {err}")

    print("\n  Requesting GET_SETTINGS ...")
    try:
        resp = await cam._send_request(RequestType.GET_SETTINGS)
        print("  Settings:")
        print(_dump_proto_fields(resp, indent=2))
    except (NanitError, TimeoutError) as err:
        print(f"  GET_SETTINGS failed: {err}")

    print("\n  Requesting GET_SENSOR_DATA (all=True) ...")
    try:
        from aionanit.proto import GetSensorData

        resp = await cam._send_request(
            RequestType.GET_SENSOR_DATA,
            get_sensor_data=GetSensorData(all=True),
        )
        print("  Sensor data:")
        print(_dump_proto_fields(resp, indent=2))
    except (NanitError, TimeoutError) as err:
        print(f"  GET_SENSOR_DATA failed: {err}")

    print("\n  Requesting GET_CONTROL (without PTZ — cloud clients cannot query PTZ) ...")
    try:
        resp = await cam._send_request(
            RequestType.GET_CONTROL,
            get_control=GetControl(
                night_light=True,
                night_light_timeout=True,
                sensor_data_transfer_en=True,
            ),
        )
        print("  Control:")
        print(_dump_proto_fields(resp, indent=2))
    except (NanitError, TimeoutError) as err:
        print(f"  GET_CONTROL failed: {err}")


# ── Menu ─────────────────────────────────────────────────────────────────


def _print_menu() -> None:
    print("\n" + "=" * 60)
    print("  Nanit Camera Probe Tool")
    print("=" * 60)
    for i, cmd in enumerate(COMMANDS, 1):
        print(f"  {i:2d}. [{cmd['name']}] {cmd['description']}")
    print(f"  {len(COMMANDS) + 1:2d}. [quit] Exit")
    print("=" * 60)


async def _run_interactive(session: ProbeSession) -> None:
    """Run the interactive command menu."""
    while True:
        _print_menu()
        choice = input("\n  Select command (number or name): ").strip()

        if choice.lower() in ("q", "quit", "exit", str(len(COMMANDS) + 1)):
            print("  Bye!")
            return

        # Find command by number or name
        cmd = None
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(COMMANDS):
                cmd = COMMANDS[idx]
        else:
            for c in COMMANDS:
                if c["name"] == choice:
                    cmd = c
                    break

        if cmd is None:
            print(f"  Unknown command: {choice}")
            continue

        print(f"\n  ── {cmd['name']} ──")
        print(f"  {cmd['description']}")
        print(f"\n  Hint: {cmd['hint']}")
        confirm = input("\n  Run this command? (y/n): ").strip().lower()
        if confirm != "y":
            continue

        try:
            await cmd["run"](session)
        except Exception as err:
            print(f"\n  ✗ Error: {err}")
            _LOGGER.exception("Command failed")

        input("\n  Press Enter to continue ...")


# ── Main ─────────────────────────────────────────────────────────────────


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive hardware probing tool for Nanit cameras.",
    )
    parser.add_argument("command", nargs="?", help="Run a specific command (see --list)")
    parser.add_argument("--list", action="store_true", help="List all available commands")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("aionanit").setLevel(logging.DEBUG)

    if args.list:
        print("Available commands:")
        for cmd in COMMANDS:
            print(f"  {cmd['name']:30s} {cmd['description']}")
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

        session = ProbeSession(client, camera)

        try:
            if args.command:
                # Run single command
                cmd = None
                for c in COMMANDS:
                    if c["name"] == args.command:
                        cmd = c
                        break
                if cmd is None:
                    print(f"Unknown command: {args.command}", file=sys.stderr)
                    print("Use --list to see available commands", file=sys.stderr)
                    return 1

                print(f"\n  ── {cmd['name']} ──")
                print(f"  {cmd['description']}")
                print(f"\n  Hint: {cmd['hint']}")
                await cmd["run"](session)
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
