"""Wire-format contract + control-message tests for the S&L transport.

Ported from com6056/nanit-sound-light's offline suite. Two jobs:

* **Schema lock**: assert the field numbers (proto tags) of the messages we
  send/parse match what the official app v4.68.0 uses. If someone regenerates
  `sound_light.proto` and a tag shifts, this fails here instead of silently
  misreading device state at runtime.
* **Parse round-trip**: build a device→app message with known bytes and assert
  the transport decodes it into the expected device state.

All offline: pure protobuf bytes, no socket, no Home Assistant.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.nanit.aionanit_sl import sound_light_pb2 as pb2
from custom_components.nanit.aionanit_sl.transport import SoundLightTransport

UID = "SPK123"

# Field number → name, confirmed against Nanit app v4.68.0 (nanitlite/control).
# The first 8 Settings fields are unchanged since the v4.0.6 reverse-engineering.
# v4.68.0 only *appends* favorites(9)..cryDetection(15), which protobuf skips.
SETTINGS_TAGS = {
    "brightness": 1,
    "color": 2,
    "volume": 3,
    "sound": 4,
    "isOn": 5,
    "soundList": 6,  # == app "savedSounds"
    "temperature": 7,
    "humidity": 8,
}
COLOR_TAGS = {"noColor": 1, "hue": 2, "saturation": 3}
SOUND_TAGS = {"noSound": 1, "track": 2}
# Backend readiness frame (app v4.68.0 nanitlite/control): Message.backend=3,
# Backend.device=1, BackendDevice.status=1, DeviceStatus{Disconnected=0,Connected=1}.
MESSAGE_TAGS = {"request": 1, "response": 2, "backend": 3}
BACKEND_TAGS = {"device": 1}
BACKEND_DEVICE_TAGS = {"status": 1}


def make_transport(**kwargs) -> SoundLightTransport:
    """A fresh offline transport with static token providers."""
    kwargs.setdefault("access_token_provider", AsyncMock(return_value="test-token"))
    kwargs.setdefault("device_token_fetcher", AsyncMock(return_value="dev-token"))
    return SoundLightTransport(**kwargs)


@pytest.fixture
def api() -> SoundLightTransport:
    return make_transport()


def _assert_tags(message_cls, expected: dict[str, int]) -> None:
    fields = message_cls.DESCRIPTOR.fields_by_name
    for name, tag in expected.items():
        assert name in fields, f"{message_cls.DESCRIPTOR.name}.{name} missing from proto"
        assert fields[name].number == tag, (
            f"{message_cls.DESCRIPTOR.name}.{name} tag drifted: "
            f"expected {tag}, got {fields[name].number}"
        )


def test_settings_tags_match_app():
    _assert_tags(pb2.Settings, SETTINGS_TAGS)


def test_color_tags_match_app():
    _assert_tags(pb2.Color, COLOR_TAGS)


def test_sound_tags_match_app():
    _assert_tags(pb2.Sound, SOUND_TAGS)


def test_backend_tags_match_app():
    _assert_tags(pb2.Message, MESSAGE_TAGS)
    _assert_tags(pb2.Backend, BACKEND_TAGS)
    _assert_tags(pb2.BackendDevice, BACKEND_DEVICE_TAGS)
    # Enum values gate the readiness check (_BACKEND_STATUS_CONNECTED).
    assert pb2.Disconnected == 0
    assert pb2.Connected == 1


def test_response_status_tag_matches_app():
    """Response.status is tag 9 in the app (tag 6 is firmware)."""
    fields = pb2.Response.DESCRIPTOR.fields_by_name
    assert fields["status"].number == 9
    assert fields["requestId"].number == 1
    assert fields["statusCode"].number == 2
    assert fields["settings"].number == 4


def test_diagnostics_tags_match_app():
    """Battery/wifi/firmware request+response tags (from the app's @ProtoNumber,
    NOT element-index+1, which a prior heuristic pass got wrong)."""
    req = pb2.Request.DESCRIPTOR.fields_by_name
    assert req["network"].number == 2
    assert req["firmware"].number == 3
    assert req["getStatus"].number == 11
    resp = pb2.Response.DESCRIPTOR.fields_by_name
    assert resp["firmware"].number == 6
    assert resp["networkStatus"].number == 8
    assert resp["status"].number == 9
    assert pb2.Status.DESCRIPTOR.fields_by_name["battery"].number == 1
    assert pb2.Battery.DESCRIPTOR.fields_by_name["soc"].number == 1
    assert pb2.Battery.DESCRIPTOR.fields_by_name["isCharging"].number == 2
    ap = pb2.AccessPointInfo.DESCRIPTOR.fields_by_name
    assert (ap["ssid"].number, ap["rssi"].number, ap["primaryChannel"].number) == (1, 5, 6)
    assert pb2.FirmwareInfo.DESCRIPTOR.fields_by_name["version"].number == 2
    assert pb2.NetworkStatus.DESCRIPTOR.fields_by_name["currentAp"].number == 4
    assert (pb2.SoCLow, pb2.SoC25, pb2.SoC50, pb2.SoC75, pb2.SoC90) == (0, 1, 2, 3, 4)


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------


async def test_backend_connected_frame_marks_device_attached(api):
    """A Backend{Connected} frame attaches, and attachment is STICKY thereafter.

    The real device emits bare/Disconnected backend frames periodically while
    fully usable, so a non-Connected frame must NOT flip the device unavailable.
    Only a socket drop clears attachment (covered in the reconnect suite).
    """
    connected = pb2.Message(backend=pb2.Backend(device=pb2.BackendDevice(status=pb2.Connected)))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), connected.SerializeToString())
    assert api.is_device_attached(UID) is True

    disconnected = pb2.Message(
        backend=pb2.Backend(device=pb2.BackendDevice(status=pb2.Disconnected))
    )
    await api._process_protobuf_message(
        api._conn_key(UID, "remote"), disconnected.SerializeToString()
    )
    assert api.is_device_attached(UID) is True


async def test_response_with_requestid_resolves_pending_command(api):
    """A Response{requestId,statusCode} hands the ack to the awaiting send."""
    future = asyncio.get_running_loop().create_future()
    api._pending_responses[UID] = {42: future}

    message = pb2.Message(response=pb2.Response(requestId=42, statusCode=200))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())

    assert future.done() and future.result() == 200
    # A genuine Response also implies the device is attached.
    assert api.is_device_attached(UID) is True


async def test_response_settings_parses_into_device_state(api):
    """A device→app Response{settings} updates the device state correctly."""
    settings = pb2.Settings(
        isOn=True,
        brightness=0.5,
        volume=0.8,
        sound=pb2.Sound(noSound=False, track="Pink Noise"),
        color=pb2.Color(noColor=True),
    )
    message = pb2.Message(response=pb2.Response(requestId=7, settings=settings))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())

    state = api.get_device_state(UID)
    assert state["is_on"] is True
    assert state["brightness"] == pytest.approx(0.5)
    assert state["volume"] == pytest.approx(0.8)
    assert state["current_sound"] == "Pink Noise"
    assert state["no_color"] is True


async def test_external_request_change_triggers_callback(api):
    """A device→app Request{settings} (external change) updates state and notifies."""
    notified: list[str] = []

    async def on_change(speaker_uid: str) -> None:
        notified.append(speaker_uid)

    api.set_state_change_callback(on_change)

    message = pb2.Message(request=pb2.Request(id=1, settings=pb2.Settings(isOn=False)))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())

    assert api.get_device_state(UID)["is_on"] is False
    assert notified == [UID]


async def test_battery_status_parses(api):
    """Response{status{battery}} → bucketed percent + charging in device state."""
    status = pb2.Status(battery=pb2.Battery(soc=pb2.SoC75, isCharging=True))
    message = pb2.Message(response=pb2.Response(requestId=1, status=status))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())
    state = api.get_device_state(UID)
    assert state["battery_percent"] == 75
    assert state["battery_charging"] is True


async def test_battery_not_charging_when_field_absent(api):
    """Device omits isCharging when unplugged: report not-charging, not unknown."""
    s1 = pb2.Message(
        response=pb2.Response(
            requestId=1,
            status=pb2.Status(battery=pb2.Battery(soc=pb2.SoC50, isCharging=True)),
        )
    )
    await api._process_protobuf_message(api._conn_key(UID, "remote"), s1.SerializeToString())
    assert api.get_device_state(UID)["battery_charging"] is True
    s2 = pb2.Message(
        response=pb2.Response(requestId=2, status=pb2.Status(battery=pb2.Battery(soc=pb2.SoC50)))
    )
    await api._process_protobuf_message(api._conn_key(UID, "remote"), s2.SerializeToString())
    assert api.get_device_state(UID)["battery_charging"] is False


async def test_network_status_parses(api):
    """Response{networkStatus{currentAp}} → wifi rssi/ssid/channel in device state."""
    ap = pb2.AccessPointInfo(ssid="Nursery", bssid="aa:bb", rssi=-58, primaryChannel=6)
    message = pb2.Message(
        response=pb2.Response(requestId=1, networkStatus=pb2.NetworkStatus(currentAp=ap))
    )
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())
    state = api.get_device_state(UID)
    assert state["wifi_rssi"] == -58
    assert state["wifi_ssid"] == "Nursery"
    assert state["wifi_channel"] == 6


async def test_firmware_version_parses(api):
    """Response{firmware{version}} → firmware_version in device state."""
    message = pb2.Message(
        response=pb2.Response(requestId=1, firmware=pb2.FirmwareInfo(version="1.2.3"))
    )
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())
    assert api.get_device_state(UID)["firmware_version"] == "1.2.3"


async def test_sound_list_is_sanitized(api):
    """Device-supplied track names are clamped + filtered before becoming options."""
    settings = pb2.Settings(
        soundList=pb2.SoundList(
            tracks=[
                "Pink Noise",  # valid
                "x" * 200,  # overlong, clamped to 64
                "bad\x07bell",  # non-printable, dropped
                "   ",  # blank, dropped
            ]
        )
    )
    message = pb2.Message(response=pb2.Response(requestId=1, settings=settings))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())

    options = api.get_device_state(UID)["available_sounds"]
    assert options[0] == "No sound"
    assert "Pink Noise" in options
    assert any(len(o) == 64 for o in options)  # the overlong name, clamped
    assert "bad\x07bell" not in options
    assert "   " not in options


def test_additive_v468_fields_are_ignored_not_errors():
    """Unknown higher-tag fields (app v4.68 favorites/routines/etc) parse cleanly."""
    settings = pb2.Settings(isOn=True, brightness=1.0)
    raw = settings.SerializeToString()
    # Append a bogus field at tag 12 (varint), simulating a new app-only field.
    raw += bytes([(12 << 3) | 0, 0x01])
    reparsed = pb2.Settings()
    reparsed.ParseFromString(raw)  # must not raise
    assert reparsed.isOn is True
    assert reparsed.brightness == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Control-message builds (the combined-Settings command layer)
# ---------------------------------------------------------------------------


def _decode(raw: bytes):
    msg = pb2.Message()
    msg.ParseFromString(raw)
    return msg


def test_combined_command_is_one_settings_message(api):
    """A scene-like multi-field command becomes a single Settings, all fields set."""
    raw, _ = api.build_control_message(
        is_on=True,
        sound="Pink Noise",
        volume=1.0,
        color={"noColor": True},
    )
    msg = _decode(raw)

    assert msg.HasField("request")
    settings = msg.request.settings
    # Every field rides in the one message, no per-field racing writes.
    assert settings.isOn is True
    assert settings.volume == pytest.approx(1.0)
    assert settings.sound.track == "Pink Noise"
    assert settings.sound.noSound is False
    assert settings.color.noColor is True


def test_power_on_survives_alongside_other_fields(api):
    """Regression for the on→off flap: isOn=True can't be clobbered within one msg."""
    raw, _ = api.build_control_message(is_on=True, volume=0.5, sound="Pink Noise")
    settings = _decode(raw).request.settings
    assert settings.isOn is True


def test_message_ids_increment_and_are_unique(api):
    """Each control message gets a fresh id so responses can be correlated."""
    _, id1 = api.build_control_message(is_on=True)
    _, id2 = api.build_control_message(volume=0.5)
    _, id3 = api.build_control_message(sound="No sound")
    ids = [id1, id2, id3]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_no_sound_sets_no_sound_flag(api):
    raw, _ = api.build_control_message(sound="No sound")
    sound = _decode(raw).request.settings.sound
    assert sound.noSound is True
    assert sound.track == ""


def test_bare_sound_on_resumes_without_track(api):
    """sound=None means "sound on, resume last track": noSound=False, no track."""
    raw, _ = api.build_control_message(sound=None)
    sound = _decode(raw).request.settings.sound
    assert sound.noSound is False
    assert not sound.HasField("track")


def test_light_off_sends_bare_no_color(api):
    """A color{noColor:true} command carries no stray hue/sat/brightness.

    Any color sub-field not provided is omitted so the device's stored color
    survives. (The integration's light OFF is brightness:0, which round-trips
    the stored color; noColor is what the official app's own off uses.)
    """
    raw, _ = api.build_control_message(color={"noColor": True})
    settings = _decode(raw).request.settings
    assert settings.color.noColor is True
    assert not settings.color.HasField("hue")
    assert not settings.color.HasField("saturation")
    assert not settings.HasField("brightness")
    # And it must NOT touch the power primitive (sound keeps playing).
    assert not settings.HasField("isOn")


def test_session_id_is_stamped_when_provided(api):
    raw, _ = api.build_control_message(session_id="abc123", is_on=True)
    assert _decode(raw).request.sessionId == "abc123"


async def test_malformed_frames_are_handled_gracefully(api):
    """Garbage, truncated, and oversized frames must not crash the parser.

    This is the checklist's untrusted-deserialization contract: the speaker
    protocol is the primary untrusted input surface.
    """
    key = api._conn_key(UID, "remote")
    before = dict(api.get_device_state(UID))

    await api._process_protobuf_message(key, b"\xff\x13garbage\x00\x01")
    await api._process_protobuf_message(key, b"\x0a")  # truncated field header
    await api._process_protobuf_message(key, bytes(64 * 1024))  # zero-filled blob

    assert api.get_device_state(UID) == before
    assert api.is_device_attached(UID) is False  # junk must not latch attachment


async def test_non_finite_wire_floats_are_rejected(api):
    """NaN/Inf/out-of-range floats from the wire must not reach HA state."""
    import struct as _struct

    settings = pb2.Settings(brightness=float("nan"), volume=float("inf"), temperature=1e30)
    settings.color.hue = 5.0  # out of unit range: clamped
    message = pb2.Message(response=pb2.Response(requestId=1, settings=settings))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())

    state = api.get_device_state(UID)
    assert "brightness" not in state  # NaN rejected
    assert "volume" not in state  # Inf rejected
    assert state["temperature"] == pytest.approx(_struct.unpack("<f", _struct.pack("<f", 1e30))[0])
    assert state["hue"] == 1.0  # clamped to the unit interval


async def test_unprintable_track_name_is_dropped(api):
    """An unprintable current-track string must not become the select state."""
    settings = pb2.Settings(sound=pb2.Sound(noSound=False, track="bad\x07bell"))
    message = pb2.Message(response=pb2.Response(requestId=1, settings=settings))
    await api._process_protobuf_message(api._conn_key(UID, "remote"), message.SerializeToString())
    assert "current_sound" not in api.get_device_state(UID)
