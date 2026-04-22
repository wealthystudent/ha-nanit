"""Tests for aionanit_sl/sl_protocol.py — S&L protobuf wire-format decode/encode."""

from __future__ import annotations

import struct

import pytest

from custom_components.nanit.aionanit_sl.sl_protocol import (
    SLDecodedState,
    _encode_fixed32_field,
    _encode_length_delimited,
    _encode_varint,
    _encode_varint_field,
    build_brightness_cmd,
    build_color_cmd,
    build_light_enabled_cmd,
    build_power_cmd,
    build_sound_on_cmd,
    build_track_cmd,
    build_volume_cmd,
    classify_message,
    decode_cloud_relay,
    decode_fields,
    decode_full_state,
    decode_sensors,
    fixed32_to_float,
    float_to_fixed32,
    is_cloud_relay_ack,
    is_cloud_relay_error,
    is_cloud_relay_forbidden,
    FIXED32,
    LENGTH_DELIMITED,
    VARINT,
)


# ---------------------------------------------------------------------------
# Helpers — build raw protobuf bytes for test fixtures
# ---------------------------------------------------------------------------


def _make_cloud_relay_status(status_code: int, body: bytes = b"") -> bytes:
    """Build a cloud relay envelope: field 2 { field 2: status, [field 4: body] }."""
    inner = _encode_varint_field(2, status_code)
    if body:
        inner += _encode_length_delimited(4, body)
    return _encode_length_delimited(2, inner)


def _make_cloud_relay_ack() -> bytes:
    """Build a cloud relay ack: field 3 { field 1 { field 1: 1 } }."""
    inner_inner = _encode_varint_field(1, 1)
    inner = _encode_length_delimited(1, inner_inner)
    return _encode_length_delimited(3, inner)


def _make_state_fields(
    brightness: float | None = None,
    power_on: bool | None = None,
    volume: float | None = None,
    temperature_c: float | None = None,
    humidity_pct: float | None = None,
    light_enabled: bool | None = None,
    sound_on: bool | None = None,
    track_name: str | None = None,
) -> bytes:
    """Build raw state field bytes (the contents of field 1.6 or cloud field 2.4)."""
    result = b""
    if brightness is not None:
        result += _encode_fixed32_field(1, float_to_fixed32(brightness))
    if light_enabled is not None:
        # INVERTED: True → 0, False → 1
        sub = _encode_varint_field(1, 0 if light_enabled else 1)
        result += _encode_length_delimited(2, sub)
    if volume is not None:
        result += _encode_fixed32_field(3, float_to_fixed32(volume))
    if sound_on is not None or track_name is not None:
        sub = b""
        if sound_on is not None:
            sub += _encode_varint_field(1, 0 if sound_on else 1)
        if track_name is not None:
            sub += _encode_length_delimited(2, track_name.encode("utf-8"))
        result += _encode_length_delimited(4, sub)
    if power_on is not None:
        result += _encode_varint_field(5, 1 if power_on else 0)
    if temperature_c is not None:
        result += _encode_fixed32_field(7, float_to_fixed32(temperature_c))
    if humidity_pct is not None:
        result += _encode_fixed32_field(8, float_to_fixed32(humidity_pct))
    return result


def _make_local_state_message(state_bytes: bytes) -> bytes:
    """Wrap state bytes in local envelope: field 1 { field 6 { state_bytes } }."""
    field_6 = _encode_length_delimited(6, state_bytes)
    return _encode_length_delimited(1, field_6)


def _make_cloud_state_message(state_bytes: bytes) -> bytes:
    """Wrap state bytes in cloud relay envelope: field 2 { 2: 200, 4: { state } }."""
    return _make_cloud_relay_status(200, body=state_bytes)


# ---------------------------------------------------------------------------
# Tests — Cloud relay message classifiers
# ---------------------------------------------------------------------------


class TestIsCloudRelayForbidden:
    def test_returns_true_for_403(self) -> None:
        msg = _make_cloud_relay_status(403)
        assert is_cloud_relay_forbidden(msg) is True

    def test_returns_false_for_200(self) -> None:
        msg = _make_cloud_relay_status(200)
        assert is_cloud_relay_forbidden(msg) is False

    def test_returns_false_for_400(self) -> None:
        msg = _make_cloud_relay_status(400)
        assert is_cloud_relay_forbidden(msg) is False

    def test_returns_false_for_empty(self) -> None:
        assert is_cloud_relay_forbidden(b"") is False

    def test_returns_false_for_garbage(self) -> None:
        assert is_cloud_relay_forbidden(b"\xff\xff\xff") is False


class TestIsCloudRelayError:
    def test_returns_true_for_400(self) -> None:
        msg = _make_cloud_relay_status(400)
        assert is_cloud_relay_error(msg) is True

    def test_returns_true_for_500(self) -> None:
        msg = _make_cloud_relay_status(500)
        assert is_cloud_relay_error(msg) is True

    def test_returns_false_for_200(self) -> None:
        msg = _make_cloud_relay_status(200)
        assert is_cloud_relay_error(msg) is False

    def test_returns_false_for_403(self) -> None:
        # 403 is handled separately by is_cloud_relay_forbidden
        msg = _make_cloud_relay_status(403)
        assert is_cloud_relay_error(msg) is False

    def test_returns_false_for_empty(self) -> None:
        assert is_cloud_relay_error(b"") is False

    def test_returns_false_for_non_field2_envelope(self) -> None:
        # field 1 envelope instead of field 2
        msg = _encode_length_delimited(1, _encode_varint_field(2, 400))
        assert is_cloud_relay_error(msg) is False

    def test_real_400_hex_from_device(self) -> None:
        """The actual hex captured from the device rejecting a keepalive."""
        raw = bytes.fromhex("121c1090031a174661696c656420746f2070617273652072657175657374")
        assert is_cloud_relay_error(raw) is True


class TestIsCloudRelayAck:
    def test_returns_true_for_ack(self) -> None:
        msg = _make_cloud_relay_ack()
        assert is_cloud_relay_ack(msg) is True

    def test_returns_false_for_status_message(self) -> None:
        msg = _make_cloud_relay_status(200)
        assert is_cloud_relay_ack(msg) is False

    def test_returns_false_for_empty(self) -> None:
        assert is_cloud_relay_ack(b"") is False

    def test_real_ack_hex(self) -> None:
        """Known ack hex signature."""
        raw = bytes.fromhex("1a040a020801")
        assert is_cloud_relay_ack(raw) is True


# ---------------------------------------------------------------------------
# Tests — State decoding
# ---------------------------------------------------------------------------


class TestDecodeFullState:
    def test_decodes_brightness_and_power(self) -> None:
        state_bytes = _make_state_fields(brightness=0.75, power_on=True)
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.power_on is True
        assert result.brightness is not None
        assert abs(result.brightness - 0.75) < 0.01

    def test_decodes_volume(self) -> None:
        state_bytes = _make_state_fields(volume=0.6)
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.volume is not None
        assert abs(result.volume - 0.6) < 0.01

    def test_decodes_temp_and_humidity(self) -> None:
        state_bytes = _make_state_fields(temperature_c=22.5, humidity_pct=45.0)
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.temperature_c is not None
        assert abs(result.temperature_c - 22.5) < 0.1
        assert result.humidity_pct is not None
        assert abs(result.humidity_pct - 45.0) < 0.1

    def test_decodes_inverted_light_enabled(self) -> None:
        state_bytes = _make_state_fields(light_enabled=True)
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.light_enabled is True

    def test_decodes_inverted_light_disabled(self) -> None:
        state_bytes = _make_state_fields(light_enabled=False)
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.light_enabled is False

    def test_decodes_sound_on_with_track(self) -> None:
        state_bytes = _make_state_fields(sound_on=True, track_name="White Noise")
        msg = _make_local_state_message(state_bytes)
        result = decode_full_state(msg)
        assert result is not None
        assert result.sound_on is True
        assert result.current_track == "White Noise"

    def test_returns_none_for_empty(self) -> None:
        assert decode_full_state(b"") is None

    def test_returns_none_for_garbage(self) -> None:
        assert decode_full_state(b"\xff\xff\xff") is None


class TestDecodeCloudRelay:
    def test_decodes_200_state(self) -> None:
        state_bytes = _make_state_fields(brightness=0.5, power_on=True)
        msg = _make_cloud_state_message(state_bytes)
        result = decode_cloud_relay(msg)
        assert result is not None
        assert result.power_on is True
        assert result.brightness is not None
        assert abs(result.brightness - 0.5) < 0.01

    def test_returns_none_for_403(self) -> None:
        msg = _make_cloud_relay_status(403)
        assert decode_cloud_relay(msg) is None

    def test_returns_none_for_400(self) -> None:
        msg = _make_cloud_relay_status(400)
        assert decode_cloud_relay(msg) is None

    def test_returns_none_for_empty(self) -> None:
        assert decode_cloud_relay(b"") is None


class TestDecodeSensors:
    def test_decodes_temp_and_humidity(self) -> None:
        # Build: field 1 { field 1: varint(1), field 10 { 2: temp, 3: hum } }
        sensor_sub = (
            _encode_fixed32_field(2, float_to_fixed32(23.5))
            + _encode_fixed32_field(3, float_to_fixed32(55.0))
        )
        inner = _encode_varint_field(1, 1) + _encode_length_delimited(10, sensor_sub)
        msg = _encode_length_delimited(1, inner)
        result = decode_sensors(msg)
        assert result is not None
        assert result.temperature_c is not None
        assert abs(result.temperature_c - 23.5) < 0.1
        assert result.humidity_pct is not None
        assert abs(result.humidity_pct - 55.0) < 0.1

    def test_returns_none_for_empty(self) -> None:
        assert decode_sensors(b"") is None


# ---------------------------------------------------------------------------
# Tests — Message classification
# ---------------------------------------------------------------------------


class TestClassifyMessage:
    def test_state_message_type_0(self) -> None:
        state_bytes = _make_state_fields(brightness=0.5)
        msg = _make_local_state_message(state_bytes)
        assert classify_message(msg) == 0

    def test_sensor_message_type_1(self) -> None:
        sensor_sub = _encode_fixed32_field(2, float_to_fixed32(22.0))
        inner = _encode_varint_field(1, 1) + _encode_length_delimited(10, sensor_sub)
        msg = _encode_length_delimited(1, inner)
        assert classify_message(msg) == 1

    def test_routine_message_type_2(self) -> None:
        inner = _encode_varint_field(1, 2) + _encode_length_delimited(6, b"\x00")
        msg = _encode_length_delimited(1, inner)
        assert classify_message(msg) == 2

    def test_routine_message_type_3(self) -> None:
        inner = _encode_varint_field(1, 3) + _encode_length_delimited(6, b"\x00")
        msg = _encode_length_delimited(1, inner)
        assert classify_message(msg) == 3

    def test_network_info_returns_negative_1(self) -> None:
        """Large varint type indicators (e.g. timestamps) → network info → -1."""
        inner = _encode_varint_field(1, 999999)
        msg = _encode_length_delimited(1, inner)
        assert classify_message(msg) == -1

    def test_empty_returns_none(self) -> None:
        assert classify_message(b"") is None


# ---------------------------------------------------------------------------
# Tests — Command encoding roundtrips
# ---------------------------------------------------------------------------


class TestCommandEncoding:
    def test_power_on_roundtrip(self) -> None:
        cmd = build_power_cmd(True)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.power_on is True

    def test_power_off_roundtrip(self) -> None:
        cmd = build_power_cmd(False)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.power_on is False

    def test_brightness_roundtrip(self) -> None:
        cmd = build_brightness_cmd(0.8)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.brightness is not None
        assert abs(result.brightness - 0.8) < 0.01

    def test_volume_roundtrip(self) -> None:
        cmd = build_volume_cmd(0.42)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.volume is not None
        assert abs(result.volume - 0.42) < 0.01

    def test_light_enabled_roundtrip(self) -> None:
        cmd = build_light_enabled_cmd(True)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.light_enabled is True

    def test_light_disabled_roundtrip(self) -> None:
        cmd = build_light_enabled_cmd(False)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.light_enabled is False

    def test_sound_on_roundtrip(self) -> None:
        cmd = build_sound_on_cmd(True, current_track="Rain")
        result = decode_full_state(cmd)
        assert result is not None
        assert result.sound_on is True
        assert result.current_track == "Rain"

    def test_sound_off_roundtrip(self) -> None:
        cmd = build_sound_on_cmd(False)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.sound_on is False

    def test_track_roundtrip(self) -> None:
        cmd = build_track_cmd("Ocean Waves", sound_on=True)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.current_track == "Ocean Waves"
        assert result.sound_on is True

    def test_color_roundtrip(self) -> None:
        cmd = build_color_cmd(0.3, 0.7, light_enabled=True)
        result = decode_full_state(cmd)
        assert result is not None
        assert result.light_enabled is True
        assert result.color_r is not None
        assert abs(result.color_r - 0.3) < 0.01
        assert result.color_g is not None
        assert abs(result.color_g - 0.7) < 0.01
