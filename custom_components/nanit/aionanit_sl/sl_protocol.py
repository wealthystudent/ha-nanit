"""Protobuf wire-format decode/encode for the Nanit Sound & Light Machine.

The S&L device communicates via raw protobuf messages over WebSocket.
We decode them without .proto schema files using manual wire-format parsing.

Message types received on connect:
  MSG 0 (full state): brightness, color, sound track, volume, temp, humidity, tracks
  MSG 1 (sensor data): temperature, humidity
  MSG 2 (routines set A): Bedtime, Wakeup, Soft Light, Nighttime
  MSG 3 (routines set B): Wind down, turn off, Weekend
"""

from __future__ import annotations

# IMPORTANT: The S&L device uses inverted boolean conventions for
# no_color (field 2 sub-1) and no_sound (field 4 sub-1):
#   Wire value 0 (or absent) = ON/enabled
#   Wire value 1 = OFF/disabled
# All decode and encode functions in this module handle the inversion.
# Search for "INVERTED" to find all relevant locations.

import logging
import struct
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Wire types
VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5


@dataclass
class ProtoField:
    """A single decoded protobuf field."""

    field_number: int
    wire_type: int
    value: Any  # int, bytes, or float


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint at position, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if (byte & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def decode_fields(data: bytes) -> list[ProtoField]:
    """Decode raw protobuf bytes into a list of fields."""
    fields: list[ProtoField] = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = decode_varint(data, pos)
        except ValueError:
            break
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == VARINT:
            value, pos = decode_varint(data, pos)
            fields.append(ProtoField(field_number, wire_type, value))
        elif wire_type == FIXED64:
            if pos + 8 > len(data):
                break
            raw = data[pos : pos + 8]
            pos += 8
            fields.append(ProtoField(field_number, wire_type, raw))
        elif wire_type == LENGTH_DELIMITED:
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                break
            raw = data[pos : pos + length]
            pos += length
            fields.append(ProtoField(field_number, wire_type, raw))
        elif wire_type == FIXED32:
            if pos + 4 > len(data):
                break
            raw = data[pos : pos + 4]
            pos += 4
            fields.append(ProtoField(field_number, wire_type, raw))
        else:
            # Unknown wire type, bail
            break
    return fields


def fixed32_to_float(data: bytes) -> float:
    """Convert 4 bytes (FIXED32) to IEEE 754 single-precision float."""
    return float(struct.unpack("<f", data)[0])


def float_to_fixed32(value: float) -> bytes:
    """Convert a float to 4 bytes (FIXED32) IEEE 754."""
    return struct.pack("<f", value)


def get_field(fields: list[ProtoField], number: int) -> ProtoField | None:
    """Get the first field with the given number."""
    for f in fields:
        if f.field_number == number:
            return f
    return None


def get_fields(fields: list[ProtoField], number: int) -> list[ProtoField]:
    """Get all fields with the given number (for repeated fields)."""
    return [f for f in fields if f.field_number == number]


def try_decode_string(data: bytes) -> str | None:
    """Try to decode bytes as UTF-8 string."""
    try:
        return data.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# State decoding — Message type 0 (full state)
# ---------------------------------------------------------------------------


@dataclass
class SLDecodedState:
    """Decoded S&L full state from message type 0."""

    brightness: float | None = None
    light_enabled: bool | None = None    # field 2 sub-1
    color_r: float | None = None         # field 2 sub-2 (color param A)
    color_g: float | None = None         # field 2 sub-3 (color param B)
    volume: float | None = None          # field 3
    current_track: str | None = None     # field 4 sub-2
    sound_on: bool | None = None         # field 4 sub-1
    power_on: bool | None = None         # field 5
    available_tracks: list[str] | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    timezone_rule: str | None = None


def _parse_state_fields(state_fields: list[ProtoField]) -> SLDecodedState:
    """Parse state fields into an SLDecodedState.

    Shared by both local and cloud relay decoders. The state field structure
    is identical in both framing formats.
    """
    result = SLDecodedState()

    # Field 1: brightness (FIXED32 float)
    f_brightness = get_field(state_fields, 1)
    if f_brightness is not None and f_brightness.wire_type == FIXED32:
        result.brightness = fixed32_to_float(f_brightness.value)

    # Field 2: light config submessage { 1: light_enabled, 2: color_a, 3: color_b }
    f_light = get_field(state_fields, 2)
    if f_light is not None and f_light.wire_type == LENGTH_DELIMITED:
        light_fields = decode_fields(f_light.value)
        # Sub-1: light enabled flag (varint, INVERTED convention!)
        # Device uses: absent or 0 = ON, 1 = OFF
        f_light_en = get_field(light_fields, 1)
        if f_light_en is not None and f_light_en.wire_type == VARINT:
            result.light_enabled = f_light_en.value == 0
        else:
            result.light_enabled = True  # absent means ON
        # Sub-2: color param A (hue)
        f_color_a = get_field(light_fields, 2)
        if f_color_a is not None and f_color_a.wire_type == FIXED32:
            result.color_r = fixed32_to_float(f_color_a.value)
        # Sub-3: color param B (saturation)
        f_color_b = get_field(light_fields, 3)
        if f_color_b is not None and f_color_b.wire_type == FIXED32:
            result.color_g = fixed32_to_float(f_color_b.value)

    # Field 3: VOLUME (FIXED32 float, 0.0-1.0)
    f_vol = get_field(state_fields, 3)
    if f_vol is not None and f_vol.wire_type == FIXED32:
        result.volume = fixed32_to_float(f_vol.value)

    # Field 4: sound config submessage { 1: sound_on, 2: track_name }
    f_sound = get_field(state_fields, 4)
    if f_sound is not None and f_sound.wire_type == LENGTH_DELIMITED:
        sound_fields = decode_fields(f_sound.value)
        # Sub-1: sound enabled flag (varint, INVERTED convention!)
        f_sound_en = get_field(sound_fields, 1)
        if f_sound_en is not None and f_sound_en.wire_type == VARINT:
            result.sound_on = f_sound_en.value == 0
        else:
            result.sound_on = True  # absent means ON
        # Sub-2: track name
        f_name = get_field(sound_fields, 2)
        if f_name is not None and f_name.wire_type == LENGTH_DELIMITED:
            result.current_track = try_decode_string(f_name.value)

    # Field 5: power on/off (varint, 0 or 1)
    f_power = get_field(state_fields, 5)
    if f_power is not None and f_power.wire_type == VARINT:
        result.power_on = bool(f_power.value)

    # Field 6: available sounds (submessage with repeated field 1 = strings)
    f_sounds = get_field(state_fields, 6)
    if f_sounds is not None and f_sounds.wire_type == LENGTH_DELIMITED:
        sounds_fields = decode_fields(f_sounds.value)
        tracks = []
        for f in get_fields(sounds_fields, 1):
            if f.wire_type == LENGTH_DELIMITED:
                name = try_decode_string(f.value)
                if name:
                    tracks.append(name)
        if tracks:
            result.available_tracks = tracks

    # Field 7: temperature (FIXED32 float)
    f_temp = get_field(state_fields, 7)
    if f_temp is not None and f_temp.wire_type == FIXED32:
        result.temperature_c = fixed32_to_float(f_temp.value)

    # Field 8: humidity (FIXED32 float)
    f_hum = get_field(state_fields, 8)
    if f_hum is not None and f_hum.wire_type == FIXED32:
        result.humidity_pct = fixed32_to_float(f_hum.value)

    # Field 11: timezone info
    f_tz = get_field(state_fields, 11)
    if f_tz is not None and f_tz.wire_type == LENGTH_DELIMITED:
        tz_fields = decode_fields(f_tz.value)
        f_tz_rule = get_field(tz_fields, 2)
        if f_tz_rule is not None and f_tz_rule.wire_type == LENGTH_DELIMITED:
            result.timezone_rule = try_decode_string(f_tz_rule.value)

    return result


def decode_full_state(data: bytes) -> SLDecodedState | None:
    """Decode message type 0 — full device state.

    Outer structure: field 1 { field 6 { ...state fields... } }
    """
    try:
        outer = decode_fields(data)
        f1 = get_field(outer, 1)
        if f1 is None or f1.wire_type != LENGTH_DELIMITED:
            return None

        inner = decode_fields(f1.value)

        # Check if this is a state message (has field 6 with state data)
        f6 = get_field(inner, 6)
        if f6 is None or f6.wire_type != LENGTH_DELIMITED:
            return None

        # Also check field 1 — if it's a varint == 2 or 3, it's a routine msg
        f1_inner = get_field(inner, 1)
        if f1_inner is not None and f1_inner.wire_type == VARINT:
            msg_type = f1_inner.value
            if msg_type in (2, 3):
                return None  # This is a routines message

        state_fields = decode_fields(f6.value)
        return _parse_state_fields(state_fields)

    except Exception as err:
        _LOGGER.debug("Failed to decode S&L full state: %s", err)
        return None


def decode_cloud_relay(data: bytes) -> SLDecodedState | None:
    """Decode a cloud relay message — state wrapped in HTTP-like envelope.

    Cloud relay structure: field 2 { field 2=status, field 3=text, field 4 { state } }
    The state fields inside field 4 are the same format as local field 1.6.
    """
    try:
        outer = decode_fields(data)
        f2 = get_field(outer, 2)
        if f2 is None or f2.wire_type != LENGTH_DELIMITED:
            return None

        inner = decode_fields(f2.value)

        # Check for HTTP status (field 2 varint)
        f_status = get_field(inner, 2)
        if f_status is None or f_status.wire_type != VARINT:
            return None

        status_code = f_status.value
        if status_code != 200:
            # Non-200 (e.g. 403 Forbidden) — ignore silently
            return None

        # Extract state payload from field 4
        f_payload = get_field(inner, 4)
        if f_payload is None or f_payload.wire_type != LENGTH_DELIMITED:
            return None

        state_fields = decode_fields(f_payload.value)
        return _parse_state_fields(state_fields)

    except Exception as err:
        _LOGGER.debug("Failed to decode S&L cloud relay message: %s", err)
        return None


def is_cloud_relay_forbidden(data: bytes) -> bool:
    """Check if a message is a cloud relay 403 Forbidden (periodic, ignorable)."""
    try:
        outer = decode_fields(data)
        f2 = get_field(outer, 2)
        if f2 is None or f2.wire_type != LENGTH_DELIMITED:
            return False
        inner = decode_fields(f2.value)
        f_status = get_field(inner, 2)
        return f_status is not None and f_status.wire_type == VARINT and f_status.value == 403
    except Exception:
        return False


def is_cloud_relay_error(data: bytes) -> bool:
    """Check if a message is a non-200 error response in field 2 envelope.

    The device (and cloud relay) may return error responses such as
    400 "Failed to parse request". These use the same field 2 envelope
    as cloud relay messages but with a non-200 status code.
    """
    try:
        outer = decode_fields(data)
        f2 = get_field(outer, 2)
        if f2 is None or f2.wire_type != LENGTH_DELIMITED:
            return False
        inner = decode_fields(f2.value)
        f_status = get_field(inner, 2)
        if f_status is None or f_status.wire_type != VARINT:
            return False
        # Any status code that isn't 200 (success) or 403 (handled separately)
        return f_status.value not in (200, 403)
    except Exception:
        return False


def is_cloud_relay_ack(data: bytes) -> bool:
    """Check if a message is a cloud relay command acknowledgment.

    Cloud relay sends back field 3 { field 1 { field 1: 1 } } after
    receiving a command. These are safe to ignore.
    """
    try:
        outer = decode_fields(data)
        f3 = get_field(outer, 3)
        return f3 is not None and f3.wire_type == LENGTH_DELIMITED
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sensor decoding — Message type 1
# ---------------------------------------------------------------------------


@dataclass
class SLDecodedSensors:
    """Decoded S&L sensor readings from message type 1."""

    temperature_c: float | None = None
    humidity_pct: float | None = None


def decode_sensors(data: bytes) -> SLDecodedSensors | None:
    """Decode message type 1 — sensor data.

    Structure: 1 { 1: varint, 10 { 1: {...}, 2: float(temp), 3: float(hum) } }
    """
    try:
        outer = decode_fields(data)
        f1 = get_field(outer, 1)
        if f1 is None or f1.wire_type != LENGTH_DELIMITED:
            return None

        inner = decode_fields(f1.value)

        # Look for field 10 (sensor submessage)
        f10 = get_field(inner, 10)
        if f10 is None or f10.wire_type != LENGTH_DELIMITED:
            return None

        sensor_fields = decode_fields(f10.value)
        result = SLDecodedSensors()

        # Field 2: temperature
        f_temp = get_field(sensor_fields, 2)
        if f_temp is not None and f_temp.wire_type == FIXED32:
            result.temperature_c = fixed32_to_float(f_temp.value)

        # Field 3: humidity
        f_hum = get_field(sensor_fields, 3)
        if f_hum is not None and f_hum.wire_type == FIXED32:
            result.humidity_pct = fixed32_to_float(f_hum.value)

        return result

    except Exception as err:
        _LOGGER.debug("Failed to decode S&L sensors: %s", err)
        return None


# ---------------------------------------------------------------------------
# Routines decoding — Message types 2 and 3
# ---------------------------------------------------------------------------


@dataclass
class SLDecodedRoutine:
    """A single decoded routine."""

    name: str = ""
    sound_name: str | None = None
    volume: float | None = None
    brightness: float | None = None


def _decode_routine_entry(data: bytes) -> SLDecodedRoutine | None:
    """Decode a single routine entry."""
    try:
        fields = decode_fields(data)
        routine = SLDecodedRoutine()

        # Field 2: routine name (string)
        f_name = get_field(fields, 2)
        if f_name is not None and f_name.wire_type == LENGTH_DELIMITED:
            routine.name = try_decode_string(f_name.value) or ""

        # Field 5: sound info (submessage with field 2 = sound name)
        f_sound = get_field(fields, 5)
        if f_sound is not None and f_sound.wire_type == LENGTH_DELIMITED:
            sound_fields = decode_fields(f_sound.value)
            f_sname = get_field(sound_fields, 2)
            if f_sname is not None and f_sname.wire_type == LENGTH_DELIMITED:
                routine.sound_name = try_decode_string(f_sname.value)

        # Field 6: volume (FIXED32 float)
        f_vol = get_field(fields, 6)
        if f_vol is not None and f_vol.wire_type == FIXED32:
            routine.volume = fixed32_to_float(f_vol.value)

        # Field 4: brightness (FIXED32 float)
        f_bright = get_field(fields, 4)
        if f_bright is not None and f_bright.wire_type == FIXED32:
            routine.brightness = fixed32_to_float(f_bright.value)

        return routine if routine.name else None

    except Exception:
        return None


def decode_routines(data: bytes) -> list[SLDecodedRoutine]:
    """Decode routine messages (types 2 and 3).

    Type 2: 1 { 1: 2, 6 { 9 { repeated 1 { routine } } } }
    Type 3: 1 { 1: 3, 6 { 12 { repeated 1 { routine } } } }
    """
    routines: list[SLDecodedRoutine] = []
    try:
        outer = decode_fields(data)
        f1 = get_field(outer, 1)
        if f1 is None or f1.wire_type != LENGTH_DELIMITED:
            return routines

        inner = decode_fields(f1.value)

        f6 = get_field(inner, 6)
        if f6 is None or f6.wire_type != LENGTH_DELIMITED:
            return routines

        state_fields = decode_fields(f6.value)

        # Try field 9 (type 2 routines) and field 12 (type 3 routines)
        for container_field_num in (9, 12):
            container_fields = get_fields(state_fields, container_field_num)
            for cf in container_fields:
                if cf.wire_type != LENGTH_DELIMITED:
                    continue
                # Each container has repeated field 1 entries
                entry_fields = decode_fields(cf.value)
                for ef in get_fields(entry_fields, 1):
                    if ef.wire_type == LENGTH_DELIMITED:
                        routine = _decode_routine_entry(ef.value)
                        if routine is not None:
                            routines.append(routine)

    except Exception as err:
        _LOGGER.debug("Failed to decode S&L routines: %s", err)

    return routines


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------


def classify_message(data: bytes) -> int | None:
    """Classify a raw S&L protobuf message by its type indicator.

    Returns:
        0 = full state (no type field, has state data in 1.6)
        1 = sensor data (has field 1.10)
        2 = routine set A (type field = 2)
        3 = routine set B (type field = 3)
        -1 = network info / ignorable (field 1 with large varint type indicator)
        None = unknown
    """
    try:
        outer = decode_fields(data)
        f1 = get_field(outer, 1)
        if f1 is None or f1.wire_type != LENGTH_DELIMITED:
            return None

        inner = decode_fields(f1.value)

        # Check for type indicator in field 1 (varint)
        f1_inner = get_field(inner, 1)
        if f1_inner is not None and f1_inner.wire_type == VARINT:
            val = f1_inner.value
            if val in (2, 3):
                return int(val)
            # Large varint values (e.g. timestamps) indicate network info
            # or other device metadata messages — safe to ignore.
            if val > 3:
                return -1

        # Check for sensor data (field 10)
        f10 = get_field(inner, 10)
        if f10 is not None:
            return 1

        # Check for state data (field 6)
        f6 = get_field(inner, 6)
        if f6 is not None:
            return 0

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Command encoding — Round 18 (experimental)
#
# Strategy: mirror the state message structure with only the changed field.
# The device sends: 1 { 6 { field: value } }
# We try sending the same structure back with modifications.
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag."""
    return _encode_varint((field_number << 3) | wire_type)


def _encode_length_delimited(field_number: int, data: bytes) -> bytes:
    """Encode a length-delimited field (field number + length + data)."""
    return _encode_tag(field_number, LENGTH_DELIMITED) + _encode_varint(len(data)) + data


def _encode_varint_field(field_number: int, value: int) -> bytes:
    """Encode a varint field."""
    return _encode_tag(field_number, VARINT) + _encode_varint(value)


def _encode_fixed32_field(field_number: int, data: bytes) -> bytes:
    """Encode a FIXED32 field."""
    return _encode_tag(field_number, FIXED32) + data


def build_power_cmd(on: bool) -> bytes:
    """Build command to turn device power on/off.

    Sends: 1 { 6 { 5: 0_or_1 } }

    User confirmed: field 5 is the device power switch.
    """
    field_5 = _encode_varint_field(5, 1 if on else 0)
    field_6 = _encode_length_delimited(6, field_5)
    return _encode_length_delimited(1, field_6)


def build_light_enabled_cmd(
    on: bool,
    color_a: float | None = None,
    color_b: float | None = None,
) -> bytes:
    """Build command to turn the night light on/off.

    Sends: 1 { 6 { 2 { 1: varint, [2: color_a], [3: color_b] } } }

    INVERTED convention: device uses 0 = ON, 1 = OFF.
    Original capture had no sub-1 and light was ON. User confirmed
    sending sub-1=1 turns light OFF.

    Includes current color values to prevent the device from clearing
    them when only the enabled flag is toggled.
    """
    # Invert: on=True → send 0, on=False → send 1
    sub_1 = _encode_varint_field(1, 0 if on else 1)
    # Include current color values to preserve them
    color_fields = b""
    if color_a is not None:
        color_fields += _encode_fixed32_field(2, float_to_fixed32(color_a))
    if color_b is not None:
        color_fields += _encode_fixed32_field(3, float_to_fixed32(color_b))
    field_2 = _encode_length_delimited(2, sub_1 + color_fields)
    field_6 = _encode_length_delimited(6, field_2)
    return _encode_length_delimited(1, field_6)


def build_sound_on_cmd(on: bool, current_track: str | None = None) -> bytes:
    """Build command to turn sound on/off.

    Sends: 1 { 6 { 4 { 1: varint, [2: track_name] } } }

    INVERTED convention: device uses 0 = ON, 1 = OFF (same as light).
    User confirmed sending sub-1=1 turns sound OFF and wipes track to
    "No Sound".

    Includes current track name to prevent the device from clearing it
    when only the enabled flag is toggled.
    """
    # Invert: on=True → send 0, on=False → send 1
    sub_1 = _encode_varint_field(1, 0 if on else 1)
    # Include current track to preserve it
    track_field = b""
    if current_track:
        track_bytes = current_track.encode("utf-8")
        track_field = _encode_length_delimited(2, track_bytes)
    field_4 = _encode_length_delimited(4, sub_1 + track_field)
    field_6 = _encode_length_delimited(6, field_4)
    return _encode_length_delimited(1, field_6)


def build_volume_cmd(volume: float) -> bytes:
    """Build command to set volume (0.0-1.0).

    Sends: 1 { 6 { 3: float(volume) } }

    State field 3 was confirmed as the volume field by user testing:
    setting color accidentally sent saturation as field 3, which changed
    the device volume (orange S=0.945 → volume ~95%).

    Original capture had field 3 = 0.6058 ≈ 61% volume, which is plausible.
    """
    vol_bytes = float_to_fixed32(volume)
    field_3 = _encode_fixed32_field(3, vol_bytes)
    field_6 = _encode_length_delimited(6, field_3)
    return _encode_length_delimited(1, field_6)


def build_track_cmd(track_name: str, sound_on: bool | None = None) -> bytes:
    """Build command to change sound track.

    Sends: 1 { 6 { 4 { [1: enabled], 2: "track_name" } } }

    Includes sound_on flag to preserve it (inverted convention).
    """
    enabled_field = b""
    if sound_on is not None:
        enabled_field = _encode_varint_field(1, 0 if sound_on else 1)
    track_bytes = track_name.encode("utf-8")
    field_2 = _encode_length_delimited(2, track_bytes)
    field_4 = _encode_length_delimited(4, enabled_field + field_2)
    field_6 = _encode_length_delimited(6, field_4)
    return _encode_length_delimited(1, field_6)


def build_brightness_cmd(brightness: float) -> bytes:
    """Build command to set brightness (0.0-1.0).

    Mirrors: 1 { 6 { 1: float_value } }
    """
    bright_bytes = float_to_fixed32(brightness)
    field_1 = _encode_fixed32_field(1, bright_bytes)
    field_6 = _encode_length_delimited(6, field_1)
    return _encode_length_delimited(1, field_6)


def build_color_cmd(
    color_a: float,
    color_b: float,
    light_enabled: bool | None = None,
) -> bytes:
    """Build command to set light color (experimental).

    Sends: 1 { 6 { 2 { [1: enabled], 2: float(color_a), 3: float(color_b) } } }

    State field 2 is a submessage with sub-fields 2 and 3 (both floats).
    Includes the light_enabled flag (sub-1) to prevent accidentally
    toggling the light when only changing color.
    """
    # Include light enabled flag to preserve state (inverted convention)
    enabled_field = b""
    if light_enabled is not None:
        enabled_field = _encode_varint_field(1, 0 if light_enabled else 1)
    sub_2 = _encode_fixed32_field(2, float_to_fixed32(color_a))
    sub_3 = _encode_fixed32_field(3, float_to_fixed32(color_b))
    field_2 = _encode_length_delimited(2, enabled_field + sub_2 + sub_3)
    field_6 = _encode_length_delimited(6, field_2)
    return _encode_length_delimited(1, field_6)
