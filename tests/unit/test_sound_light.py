"""Tests for aionanit_sl/sound_light.py, the NanitSoundLight facade.

The facade keeps its original public surface but now sits on the ported
transport, adding command coalescing, the pin-guard, optimistic state with
rollback, and the validated light semantics (off = brightness:0, on =
explicit hue/saturation restore). These tests drive the facade against a
mocked transport; the transport itself is covered by the test_sl_transport*
suites against in-process fake servers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nanit.aionanit_sl import sound_light as sound_light_mod
from custom_components.nanit.aionanit_sl.exceptions import NanitTransportError
from custom_components.nanit.aionanit_sl.models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
)
from custom_components.nanit.aionanit_sl.sound_light import NanitSoundLight


def _make_sound_light(
    device_ip: str | None = None,
    speaker_uid: str = "L101TEST",
) -> NanitSoundLight:
    """Create a NanitSoundLight instance with mocked dependencies."""
    token_manager = MagicMock()
    token_manager.async_get_access_token = AsyncMock(return_value="mock_access_token")

    rest_client = MagicMock()
    rest_client.base_url = "https://api.nanit.com"
    rest_client.async_get_device_token = AsyncMock(return_value="mock_device_token")

    session = MagicMock()

    return NanitSoundLight(
        speaker_uid=speaker_uid,
        token_manager=token_manager,
        rest_client=rest_client,
        session=session,
        device_ip=device_ip,
    )


def _mock_transport(sl: NanitSoundLight) -> MagicMock:
    """Replace the facade's transport with a mock and mark the facade started."""
    api = MagicMock()
    api.send_control_command = AsyncMock()
    api.close = AsyncMock()
    api.is_websocket_connected = MagicMock(return_value=True)
    api.is_device_attached = MagicMock(return_value=True)
    api.active_transport = MagicMock(return_value="local")
    api.get_device_state = MagicMock(return_value={})
    sl._api = api
    sl._stopped = False
    return api


async def _flushed(sl: NanitSoundLight) -> None:
    """Wait for the coalescing window to elapse and the flush task to finish."""
    await asyncio.sleep(0.05)
    if sl._flush_task is not None:
        await sl._flush_task


@pytest.fixture(autouse=True)
def _fast_coalesce(monkeypatch):
    monkeypatch.setattr(sound_light_mod, "COMMAND_COALESCE_DELAY", 0.01)


class TestProperties:
    def test_speaker_uid(self) -> None:
        sl = _make_sound_light(speaker_uid="L101ABC")
        assert sl.speaker_uid == "L101ABC"

    def test_initial_state(self) -> None:
        sl = _make_sound_light()
        assert sl.state == SoundLightFullState()

    def test_initial_connected_false(self) -> None:
        sl = _make_sound_light()
        assert sl.connected is False

    def test_connection_mode_unavailable_when_disconnected(self) -> None:
        sl = _make_sound_light()
        assert sl.connection_mode == "unavailable"

    def test_connection_mode_follows_active_transport(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        assert sl.connection_mode == "local"
        api.active_transport.return_value = "cloud"
        assert sl.connection_mode == "cloud"
        api.active_transport.return_value = None
        assert sl.connection_mode == "unavailable"

    def test_connected_requires_socket_and_attachment(self) -> None:
        """A relay socket can be up while the device is detached behind it."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        assert sl.connected is True
        api.is_device_attached.return_value = False
        assert sl.connected is False
        api.is_device_attached.return_value = True
        api.is_websocket_connected.return_value = False
        assert sl.connected is False


class TestRestoreState:
    def test_restore_state_updates_internal_state(self) -> None:
        sl = _make_sound_light()
        new_state = SoundLightFullState(power_on=True, brightness=0.5)
        sl.restore_state(new_state)
        assert sl.state.power_on is True
        assert sl.state.brightness == 0.5

    def test_restore_state_replaces_completely(self) -> None:
        sl = _make_sound_light()
        sl.restore_state(SoundLightFullState(volume=0.8))
        sl.restore_state(SoundLightFullState(brightness=0.3))
        # Second restore should replace, not merge
        assert sl.state.volume is None
        assert sl.state.brightness == 0.3

    def test_restore_state_seeds_color_and_track_restores(self) -> None:
        """Restored color/track survive a restart as the light-on/sound-on restores."""
        sl = _make_sound_light()
        sl.restore_state(
            SoundLightFullState(color_r=0.1, color_g=0.9, current_track="Rain", brightness=0.4)
        )
        assert sl._last_color == {"hue": 0.1, "saturation": 0.9, "brightness": 0.4}
        assert sl._last_track == "Rain"
        assert sl._last_brightness == 0.4


class TestSubscription:
    def test_subscribe_and_fire_event(self) -> None:
        sl = _make_sound_light()
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)
        sl._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
        assert len(events) == 1
        assert events[0].kind == SoundLightEventKind.CONNECTION_CHANGE

    def test_unsubscribe(self) -> None:
        sl = _make_sound_light()
        events: list[SoundLightEvent] = []
        unsub = sl.subscribe(events.append)
        unsub()
        sl._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
        assert len(events) == 0

    def test_subscriber_exception_does_not_propagate(self) -> None:
        sl = _make_sound_light()

        def bad_callback(event: SoundLightEvent) -> None:
            raise RuntimeError("boom")

        sl.subscribe(bad_callback)
        # Should not raise
        sl._fire_event(SoundLightEventKind.STATE_UPDATE)


class TestCoalescing:
    async def test_burst_of_commands_flushes_as_one_send(self) -> None:
        """A scene touching several entities sends ONE combined command."""
        sl = _make_sound_light()
        api = _mock_transport(sl)

        await sl.async_set_power(True)
        await sl.async_set_volume(0.5)
        await sl.async_set_track("Pink Noise")
        await _flushed(sl)

        assert api.send_control_command.await_count == 1
        kwargs = api.send_control_command.await_args.kwargs
        assert kwargs == {"is_on": True, "volume": 0.5, "sound": "Pink Noise"}

    async def test_setters_raise_when_stopped(self) -> None:
        sl = _make_sound_light()
        with pytest.raises(NanitTransportError):
            await sl.async_set_power(True)


class TestLightSemantics:
    async def test_light_off_sends_brightness_zero(self) -> None:
        """Light OFF is brightness:0 (round-trips the stored color)."""
        sl = _make_sound_light()
        api = _mock_transport(sl)

        await sl.async_set_light_enabled(False)
        await _flushed(sl)

        kwargs = api.send_control_command.await_args.kwargs
        assert kwargs == {"brightness": 0.0}

    async def test_light_on_from_off_powers_restores_and_guards_sound(self) -> None:
        """Light ON from a powered-off device: isOn + brightness restore +
        explicit hue/sat + the "No sound" guard so audio can't resume."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._device_view = {"is_on": False, "brightness": 0.0, "hue": 0.1, "saturation": 0.9}
        sl._last_color = {"hue": 0.25, "saturation": 0.8, "brightness": 0.6}
        sl._last_brightness = 0.6

        await sl.async_set_light_enabled(True)
        await _flushed(sl)

        kwargs = api.send_control_command.await_args.kwargs
        assert kwargs["is_on"] is True
        assert kwargs["sound"] == "No sound"
        assert kwargs["brightness"] == 0.6
        assert kwargs["color"] == {"noColor": False, "hue": 0.25, "saturation": 0.8}

    async def test_light_on_while_running_keeps_sound_and_brightness(self) -> None:
        """Light ON on an already-running device: no sound guard, no forced
        brightness, but color is still sent explicitly (the only reliable
        restore against the app's noColor off)."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._device_view = {"is_on": True, "brightness": 0.7, "hue": 0.5, "saturation": 0.5}

        await sl.async_set_light_enabled(True)
        await _flushed(sl)

        kwargs = api.send_control_command.await_args.kwargs
        assert kwargs["is_on"] is True
        assert "sound" not in kwargs
        assert "brightness" not in kwargs
        assert kwargs["color"] == {"noColor": False, "hue": 0.5, "saturation": 0.5}

    async def test_set_color_saves_restore_and_sends_explicit_huesat(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)

        await sl.async_set_color(0.33, 0.66)
        await _flushed(sl)

        kwargs = api.send_control_command.await_args.kwargs
        assert kwargs["color"] == {"noColor": False, "hue": 0.33, "saturation": 0.66}
        assert sl._last_color["hue"] == 0.33
        assert sl._last_color["saturation"] == 0.66


class TestSoundSemantics:
    async def test_sound_off_selects_no_sound(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        await sl.async_set_sound_on(False)
        await _flushed(sl)
        assert api.send_control_command.await_args.kwargs == {"sound": "No sound"}

    async def test_sound_on_resumes_last_track(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._last_track = "Rain"
        await sl.async_set_sound_on(True)
        await _flushed(sl)
        assert api.send_control_command.await_args.kwargs == {"sound": "Rain"}

    async def test_sound_on_without_known_track_sends_bare_resume(self) -> None:
        """No track ever seen: sound=None → bare noSound:false (device resumes)."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        await sl.async_set_sound_on(True)
        await _flushed(sl)
        assert api.send_control_command.await_args.kwargs == {"sound": None}

    async def test_set_track_remembers_last_track(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        await sl.async_set_track("Ocean")
        await _flushed(sl)
        assert api.send_control_command.await_args.kwargs == {"sound": "Ocean"}
        assert sl._last_track == "Ocean"


class TestOptimisticStateAndRollback:
    async def test_optimistic_state_applies_immediately(self) -> None:
        sl = _make_sound_light()
        _mock_transport(sl)
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)

        await sl.async_set_power(True)

        # Before the flush even runs, the published state already shows it.
        assert sl.state.power_on is True
        assert any(e.kind == SoundLightEventKind.STATE_UPDATE for e in events)

        await _flushed(sl)  # let the pending flush finish (no lingering timer)

    async def test_failed_send_rolls_back_optimistic_state(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._device_view = {"is_on": False, "brightness": 0.5}
        sl._publish()
        assert sl.state.power_on is False

        api.send_control_command.side_effect = ConnectionError("device unreachable")
        await sl.async_set_power(True)
        assert sl.state.power_on is True  # optimistic
        await _flushed(sl)

        assert sl.state.power_on is False  # rolled back


class TestPinGuard:
    async def test_stale_echo_is_suppressed_until_confirmed(self) -> None:
        """A contradicting device echo inside the pin window can't flap a
        just-commanded value back; a confirming echo releases the pin."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._device_view = {"is_on": False, "brightness": 0.5}
        sl._publish()

        await sl.async_set_power(True)
        await _flushed(sl)
        assert sl.state.power_on is True

        # Stale echo: the device still reports the pre-command value.
        api.get_device_state.return_value = {"is_on": False}
        sl._ingest_device_state()
        assert sl.state.power_on is True  # suppressed

        # Confirmation releases the pin...
        api.get_device_state.return_value = {"is_on": True}
        sl._ingest_device_state()
        assert sl.state.power_on is True
        # ...so a genuine later external change flows again.
        api.get_device_state.return_value = {"is_on": False}
        sl._ingest_device_state()
        assert sl.state.power_on is False

    async def test_pinned_float_confirms_at_float32_precision(self) -> None:
        """The device echoes float32; the pin must compare in wire precision."""
        sl = _make_sound_light()
        api = _mock_transport(sl)

        await sl.async_set_brightness(0.3)  # 0.3 is not exactly representable
        await _flushed(sl)

        import struct

        echoed = struct.unpack("<f", struct.pack("<f", 0.3))[0]
        api.get_device_state.return_value = {"brightness": echoed}
        sl._ingest_device_state()
        assert "brightness" not in sl._pinned_fields  # confirmed, pin released


class TestStateMapping:
    async def test_full_state_maps_onto_model(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        api.get_device_state.return_value = {
            "is_on": True,
            "brightness": 0.8,
            "volume": 0.4,
            "hue": 0.1,
            "saturation": 0.9,
            "current_sound": "Rain",
            "available_sounds": ["No sound", "Rain", "Ocean"],
            "temperature": 21.5,
            "humidity": 45.0,
        }
        sl._ingest_device_state()

        s = sl.state
        assert s.power_on is True
        assert s.brightness == 0.8
        assert s.volume == 0.4
        assert s.color_r == 0.1
        assert s.color_g == 0.9
        assert s.sound_on is True
        assert s.current_track == "Rain"
        assert s.available_tracks == ("Rain", "Ocean")  # "No sound" stripped
        assert s.temperature_c == 21.5
        assert s.humidity_pct == 45.0
        assert s.light_enabled is True  # isOn && brightness>0 && !noColor

    async def test_no_sound_keeps_last_track_and_flips_sound_off(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        api.get_device_state.return_value = {"current_sound": "Rain"}
        sl._ingest_device_state()
        assert sl.state.sound_on is True
        assert sl.state.current_track == "Rain"

        api.get_device_state.return_value = {"current_sound": "No sound"}
        sl._ingest_device_state()
        assert sl.state.sound_on is False
        assert sl.state.current_track == "Rain"  # retained for the select

    async def test_light_enabled_reflects_no_color_off(self) -> None:
        """An app-side "Light off" (noColor:true) reads as light off in HA."""
        sl = _make_sound_light()
        api = _mock_transport(sl)
        api.get_device_state.return_value = {"is_on": True, "brightness": 0.8, "no_color": True}
        sl._ingest_device_state()
        assert sl.state.light_enabled is False

    async def test_light_enabled_reflects_brightness_zero_off(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        api.get_device_state.return_value = {"is_on": True, "brightness": 0.0, "no_color": False}
        sl._ingest_device_state()
        assert sl.state.light_enabled is False


class TestConnectionEvents:
    async def test_connection_change_fires_on_transition_only(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)

        sl._on_connection_change("L101TEST")  # disconnected -> connected
        assert [e.kind for e in events] == [SoundLightEventKind.CONNECTION_CHANGE]

        sl._on_connection_change("L101TEST")  # still connected: no event
        assert len(events) == 1

        api.is_websocket_connected.return_value = False
        sl._on_connection_change("L101TEST")  # connected -> disconnected
        assert len(events) == 2

    async def test_async_stop_closes_transport_and_fires_event(self) -> None:
        sl = _make_sound_light()
        api = _mock_transport(sl)
        sl._last_connected = True
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)

        await sl.async_stop()

        api.close.assert_awaited_once()
        assert events[-1].kind == SoundLightEventKind.CONNECTION_CHANGE
        assert sl._stopped is True


class TestEndToEndWithRealTransport:
    """Drive the facade through its REAL transport against the fake relay.

    Everything else in this file mocks the transport. This covers the wiring
    between the two: callback registration, device registration, the startup
    state prime, and a command flowing all the way to the wire.
    """

    @pytest.mark.usefixtures("socket_enabled")
    async def test_start_command_and_stop_against_fake_relay(self, monkeypatch) -> None:
        from custom_components.nanit.aionanit_sl import transport as transport_mod

        from .test_sl_transport_reconnect import _FakeNanit, _wait_until

        server = _FakeNanit()
        await server.start()
        monkeypatch.setattr(
            transport_mod, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{server.port}"
        )
        # Keep startup fast: the fake's GetSettings ack carries no settings,
        # so don't sit out the full initial-state wait.
        monkeypatch.setattr(sound_light_mod, "_INITIAL_STATE_ATTEMPTS", 1)
        monkeypatch.setattr(sound_light_mod, "_INITIAL_STATE_INTERVAL", 0.01)

        async def no_local(_uid: str) -> None:
            return None

        sl = _make_sound_light()
        sl._api.set_local_host_resolver(no_local)  # skip real mDNS in tests
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)

        await sl.async_start()
        await _wait_until(lambda: sl.connected)
        assert sl.connection_mode == "cloud"
        assert any(e.kind == SoundLightEventKind.CONNECTION_CHANGE for e in events)

        await sl.async_set_power(True)
        await _flushed(sl)

        def got_power_on() -> bool:
            from google.protobuf.message import DecodeError

            from custom_components.nanit.aionanit_sl import sound_light_pb2 as pb2

            for raw in server.received:
                msg = pb2.Message()
                try:
                    msg.ParseFromString(raw)
                except DecodeError:
                    continue
                if msg.HasField("request") and msg.request.settings.isOn:
                    return True
            return False

        await _wait_until(got_power_on)

        await sl.async_stop()
        assert sl.connected is False
        await server.stop()
