from __future__ import annotations

import asyncio
import importlib
import sys
import time

# pyright: basic, reportUnusedFunction=false
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant, is_callback
from pytest_homeassistant_custom_component.common import MockConfigEntry

_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

_MODELS = importlib.import_module("aionanit.models")
Baby = _MODELS.Baby
CameraState = _MODELS.CameraState
CloudEvent = _MODELS.CloudEvent
ConnectionInfo = _MODELS.ConnectionInfo
ConnectionState = _MODELS.ConnectionState
ControlState = _MODELS.ControlState
NightLightState = _MODELS.NightLightState
SensorState = _MODELS.SensorState
SettingsState = _MODELS.SettingsState


@dataclass(frozen=True)
class _PlaybackStateFallback:
    playing: bool = False
    current_track: str | None = None
    available_tracks: tuple[str, ...] = ()


PlaybackState = getattr(_MODELS, "PlaybackState", _PlaybackStateFallback)

from custom_components.nanit.binary_sensor import (
    BINARY_SENSORS,
    CLOUD_BINARY_SENSORS,
    NanitBinarySensor,
    NanitCloudBinarySensor,
)
from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import CLOUD_EVENT_WINDOW
from custom_components.nanit.coordinator import (
    _AVAILABILITY_GRACE_SECONDS,
    NanitPushCoordinator,
)
from custom_components.nanit.media_player import NanitMediaPlayer
from custom_components.nanit.sensor import SENSORS, NanitSensor
from custom_components.nanit.switch import SWITCHES, NanitSwitch

from .conftest import MOCK_BABY_1

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]


async def _resolve_hass(hass: Any) -> HomeAssistant:
    if hasattr(hass, "__anext__"):
        return await hass.__anext__()
    return cast(HomeAssistant, hass)


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations) -> Iterator[None]:
    _ = enable_custom_integrations
    yield


def _camera_state(
    *,
    temperature: float | None = 22.5,
    humidity: float | None = 50.0,
    light: int | None = 150,
    volume: int | None = 50,
    sleep_mode: bool | None = False,
    night_vision: bool | None = True,
    night_light: NightLightState | None = NightLightState.OFF,
    night_light_brightness: int | None = None,
    night_light_timeout: int | None = None,
    playback: PlaybackState | None = None,
    connection_state: ConnectionState = ConnectionState.CONNECTED,
    last_seen: Any = None,
) -> CameraState:
    kwargs: dict[str, Any] = {
        "sensors": SensorState(
            temperature=temperature,
            humidity=humidity,
            light=light,
        ),
        "settings": SettingsState(
            volume=volume,
            sleep_mode=sleep_mode,
            night_vision=night_vision,
            night_light_brightness=night_light_brightness,
        ),
        "control": ControlState(night_light=night_light, night_light_timeout=night_light_timeout),
        "connection": ConnectionInfo(state=connection_state, last_seen=last_seen),
    }
    if "playback" in getattr(CameraState, "__dataclass_fields__", {}):
        kwargs["playback"] = playback or PlaybackState()
        return CameraState(**kwargs)

    state = CameraState(**kwargs)
    object.__setattr__(state, "playback", playback or PlaybackState())
    return state


def _push_coordinator(
    state: CameraState | None,
    *,
    connected: bool = True,
    last_update_success: bool = True,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = last_update_success
    coordinator.connected = connected
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _cloud_coordinator(events: list[CloudEvent] | None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = events
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _sensor_description(key: str):
    return next(description for description in SENSORS if description.key == key)


def _binary_description(key: str):
    return next(description for description in BINARY_SENSORS if description.key == key)


def _cloud_binary_description(key: str):
    return next(description for description in CLOUD_BINARY_SENSORS if description.key == key)


def _switch_description(key: str):
    return next(description for description in SWITCHES if description.key == key)


def _disable_state_writes(entity: Any) -> None:
    entity.async_write_ha_state = MagicMock()


@pytest.mark.parametrize(
    ("sensor_key", "expected"),
    [
        ("temperature", 22.5),
        ("humidity", 50.0),
        ("light", 150),
    ],
)
def test_sensor_value_extraction(sensor_key: str, expected: float | int) -> None:
    coordinator = _push_coordinator(_camera_state())
    entity = NanitSensor(coordinator, _sensor_description(sensor_key))

    assert entity.native_value == expected


def test_sensor_returns_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    entity = NanitSensor(coordinator, _sensor_description("temperature"))

    assert entity.native_value is None


def test_binary_sensor_connectivity_on_when_connected() -> None:
    coordinator = _push_coordinator(_camera_state(connection_state=ConnectionState.CONNECTED))
    entity = NanitBinarySensor(coordinator, _binary_description("connectivity"))

    assert entity.is_on is True


def test_binary_sensor_connectivity_off_when_disconnected() -> None:
    coordinator = _push_coordinator(
        _camera_state(connection_state=ConnectionState.DISCONNECTED), connected=False
    )
    entity = NanitBinarySensor(coordinator, _binary_description("connectivity"))

    assert entity.is_on is False


def test_binary_sensor_connectivity_is_always_available_when_disconnected() -> None:
    coordinator = _push_coordinator(_camera_state(), connected=False, last_update_success=True)
    entity = NanitBinarySensor(coordinator, _binary_description("connectivity"))

    assert entity.available is True


def test_binary_sensor_connectivity_not_available_without_successful_update() -> None:
    coordinator = _push_coordinator(_camera_state(), connected=True, last_update_success=False)
    entity = NanitBinarySensor(coordinator, _binary_description("connectivity"))

    assert entity.available is False


def test_cloud_binary_motion_on_when_event_within_window() -> None:
    now = 10_000.0
    coordinator = _cloud_coordinator(
        [
            CloudEvent(
                event_type="MOTION",
                timestamp=now - (CLOUD_EVENT_WINDOW - 1),
                baby_uid="baby_1",
            )
        ]
    )
    entity = NanitCloudBinarySensor(coordinator, _cloud_binary_description("cloud_motion"))

    with patch("custom_components.nanit.binary_sensor.time_mod.time", return_value=now):
        assert entity.is_on is True


def test_cloud_binary_motion_matches_event_type_case_insensitively() -> None:
    now = 10_000.0
    coordinator = _cloud_coordinator(
        [
            CloudEvent(
                event_type="motion",
                timestamp=now - 1,
                baby_uid="baby_1",
            )
        ]
    )
    entity = NanitCloudBinarySensor(coordinator, _cloud_binary_description("cloud_motion"))

    with patch("custom_components.nanit.binary_sensor.time_mod.time", return_value=now):
        assert entity.is_on is True


def test_cloud_binary_sound_off_when_event_outside_window() -> None:
    now = 10_000.0
    coordinator = _cloud_coordinator(
        [
            CloudEvent(
                event_type="SOUND",
                timestamp=now - (CLOUD_EVENT_WINDOW + 1),
                baby_uid="baby_1",
            )
        ]
    )
    entity = NanitCloudBinarySensor(coordinator, _cloud_binary_description("cloud_sound"))

    with patch("custom_components.nanit.binary_sensor.time_mod.time", return_value=now):
        assert entity.is_on is False


def test_cloud_binary_sensor_off_when_no_events() -> None:
    coordinator = _cloud_coordinator([])
    entity = NanitCloudBinarySensor(coordinator, _cloud_binary_description("cloud_motion"))

    assert entity.is_on is False


async def test_switch_camera_power_turn_off_calls_sleep_mode() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitSwitch(coordinator, camera, _switch_description("camera_power"))
    _disable_state_writes(entity)

    with patch("custom_components.nanit.switch.time.monotonic", return_value=100.0):
        await entity.async_turn_off()

    camera.async_set_settings.assert_awaited_once_with(sleep_mode=True)
    assert entity.is_on is False


def test_media_player_state_playing_when_playback_playing_true() -> None:
    coordinator = _push_coordinator(
        _camera_state(playback=PlaybackState(playing=True, current_track="White Noise.wav"))
    )
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.state is MediaPlayerState.PLAYING


def test_media_player_state_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.state is None


def test_media_player_state_idle_when_playback_playing_false() -> None:
    coordinator = _push_coordinator(
        _camera_state(playback=PlaybackState(playing=False, current_track=None))
    )
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.state is MediaPlayerState.IDLE


def test_media_player_source_returns_current_track() -> None:
    coordinator = _push_coordinator(
        _camera_state(playback=PlaybackState(playing=True, current_track="Waves.wav"))
    )
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.source == "Waves.wav"


def test_media_player_source_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.source is None


def test_media_player_source_list_returns_available_tracks() -> None:
    coordinator = _push_coordinator(
        _camera_state(
            playback=PlaybackState(
                playing=True,
                current_track="White Noise.wav",
                available_tracks=("White Noise.wav", "Birds.wav", "Waves.wav"),
            )
        )
    )
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.source_list == ["White Noise.wav", "Birds.wav", "Waves.wav"]


def test_media_player_source_list_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.source_list is None


def test_media_player_volume_level_returns_settings_volume_scaled() -> None:
    coordinator = _push_coordinator(_camera_state(volume=75))
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.volume_level == 0.75


def test_media_player_volume_level_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.volume_level is None


def test_media_player_volume_level_none_when_volume_is_none() -> None:
    coordinator = _push_coordinator(_camera_state(volume=None))
    camera = MagicMock(uid="cam_1")
    entity = NanitMediaPlayer(coordinator, camera)

    assert entity.volume_level is None


async def test_media_player_play_calls_camera_start_playback() -> None:
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_start_playback = AsyncMock()
    entity = NanitMediaPlayer(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_media_play()

    camera.async_start_playback.assert_awaited_once_with()


async def test_media_player_stop_calls_camera_stop_playback() -> None:
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_stop_playback = AsyncMock()
    entity = NanitMediaPlayer(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_media_stop()

    camera.async_stop_playback.assert_awaited_once_with()


async def test_media_player_select_source_calls_start_playback_with_track() -> None:
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_start_playback = AsyncMock()
    entity = NanitMediaPlayer(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_select_source("Birds.wav")

    camera.async_start_playback.assert_awaited_once_with(track="Birds.wav")


async def test_media_player_set_volume_level_calls_set_settings_with_volume() -> None:
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock()
    entity = NanitMediaPlayer(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_set_volume_level(0.42)

    camera.async_set_settings.assert_awaited_once_with(volume=42)


async def test_camera_entity_is_on_false_when_sleep_mode_enabled(
    hass: HomeAssistant,
) -> None:
    _ = await _resolve_hass(hass)
    coordinator = _push_coordinator(_camera_state(sleep_mode=True))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)

    assert entity.is_on is False


def test_camera_entity_is_on_true_when_sleep_mode_disabled() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)

    assert entity.is_on is True


async def test_camera_stream_source_returns_url_when_on() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://stream-url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    source = await entity.stream_source()

    assert source == "rtmps://stream-url"
    camera.async_get_stream_rtmps_url.assert_awaited_once()
    camera.async_start_streaming.assert_awaited_once_with(rtmps_url="rtmps://stream-url")
    assert entity._stream_source_started_at > 0


async def test_camera_stream_source_returns_none_when_camera_off() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=True))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock()
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    source = await entity.stream_source()

    assert source is None
    camera.async_get_stream_rtmps_url.assert_not_awaited()


async def test_camera_stream_source_returns_none_when_camera_api_fails() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=RuntimeError("offline"))
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    source = await entity.stream_source()

    assert source is None


async def test_camera_stream_source_reuses_cached_url() -> None:
    """Repeat calls return the same URL so go2rtc keeps its warm producer."""
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://stream-url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    first = await entity.stream_source()
    started_at = entity._stream_source_started_at
    second = await entity.stream_source()

    assert first == second == "rtmps://stream-url"
    # URL built exactly once within the validity window.
    camera.async_get_stream_rtmps_url.assert_awaited_once()
    # PUT_STREAMING still sent per request so a lapsed camera resumes pushing.
    assert camera.async_start_streaming.await_count == 2
    # Cache age must keep tracking the original build time.
    assert entity._stream_source_started_at == started_at


async def test_camera_stream_source_rebuilds_after_max_age() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://fresh-url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity._cached_stream_source = "rtmps://stale-url"

    with (
        patch("custom_components.nanit.camera._STREAM_SOURCE_MAX_AGE", 10.0),
        patch("custom_components.nanit.camera.time.monotonic", return_value=100.0),
    ):
        entity._stream_source_started_at = 80.0
        source = await entity.stream_source()

    assert source == "rtmps://fresh-url"
    camera.async_get_stream_rtmps_url.assert_awaited_once()
    assert entity._cached_stream_source == "rtmps://fresh-url"


async def test_camera_stream_source_does_not_cache_on_start_failure() -> None:
    """A URL whose PUT_STREAMING never succeeded must not be reused."""
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://stream-url")
    camera.async_start_streaming = AsyncMock(side_effect=RuntimeError("ws closed"))
    entity = NanitCameraEntity(coordinator, camera)

    with patch("custom_components.nanit.camera._STREAM_RETRY_DELAY", 0):
        source = await entity.stream_source()

    assert source is None
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0


async def test_camera_start_streaming_safe_logs_failure_without_raising() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock(side_effect=RuntimeError("ws closed"))
    entity = NanitCameraEntity(coordinator, camera)

    with patch("custom_components.nanit.camera._STREAM_RETRY_DELAY", 0):
        result = await entity._async_start_streaming_safe()

    assert result is False
    assert camera.async_start_streaming.await_count == 3


@pytest.mark.asyncio
async def test_availability_grace_period_hides_brief_disconnect(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="nanit", data={}, version=2, unique_id="test@example.com")
    entry.add_to_hass(hass)

    camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    camera.connected = False
    camera.state = _camera_state(connection_state=ConnectionState.DISCONNECTED)
    camera.subscribe = MagicMock(return_value=lambda: None)
    camera.async_start = AsyncMock()
    camera.async_stop = AsyncMock()

    coordinator = NanitPushCoordinator(hass, entry, camera, MOCK_BABY_1)
    coordinator.connected = True

    cancel_timer = MagicMock()
    with patch(
        "custom_components.nanit.coordinator.async_call_later",
        return_value=cancel_timer,
    ) as mock_call_later:
        coordinator._on_camera_event(
            _MODELS.CameraEvent(
                kind=_MODELS.CameraEventKind.CONNECTION_CHANGE,
                state=_camera_state(connection_state=ConnectionState.DISCONNECTED),
            )
        )

    assert coordinator.connected is True
    mock_call_later.assert_called_once()
    assert mock_call_later.call_args.args[1] == _AVAILABILITY_GRACE_SECONDS
    assert coordinator._availability_timer is cancel_timer


@pytest.mark.asyncio
async def test_availability_grace_period_expires(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="nanit", data={}, version=2, unique_id="test@example.com")
    entry.add_to_hass(hass)

    camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    camera.connected = False
    camera.state = _camera_state(connection_state=ConnectionState.DISCONNECTED)
    camera.subscribe = MagicMock(return_value=lambda: None)
    camera.async_start = AsyncMock()
    camera.async_stop = AsyncMock()

    coordinator = NanitPushCoordinator(hass, entry, camera, MOCK_BABY_1)
    coordinator.connected = True
    coordinator.async_update_listeners = MagicMock()

    timeout_callback: Any | None = None

    def _capture_timer(_hass: HomeAssistant, _seconds: float, callback):
        nonlocal timeout_callback
        timeout_callback = callback
        return MagicMock()

    with patch(
        "custom_components.nanit.coordinator.async_call_later",
        side_effect=_capture_timer,
    ):
        coordinator._on_camera_event(
            _MODELS.CameraEvent(
                kind=_MODELS.CameraEventKind.CONNECTION_CHANGE,
                state=_camera_state(connection_state=ConnectionState.DISCONNECTED),
            )
        )

    assert coordinator.connected is True
    assert timeout_callback is not None
    baseline_calls = coordinator.async_update_listeners.call_count
    timeout_callback(None)
    assert coordinator.connected is False
    assert coordinator.async_update_listeners.call_count == baseline_calls + 1


@pytest.mark.asyncio
async def test_reconnect_within_grace_cancels_timer(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="nanit", data={}, version=2, unique_id="test@example.com")
    entry.add_to_hass(hass)

    camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    camera.connected = False
    camera.state = _camera_state(connection_state=ConnectionState.DISCONNECTED)
    camera.subscribe = MagicMock(return_value=lambda: None)
    camera.async_start = AsyncMock()
    camera.async_stop = AsyncMock()

    coordinator = NanitPushCoordinator(hass, entry, camera, MOCK_BABY_1)
    coordinator.connected = True
    coordinator.async_update_listeners = MagicMock()

    timeout_callback: Any | None = None
    cancel_timer = MagicMock()

    def _capture_timer(_hass: HomeAssistant, _seconds: float, callback):
        nonlocal timeout_callback
        timeout_callback = callback
        return cancel_timer

    with patch(
        "custom_components.nanit.coordinator.async_call_later",
        side_effect=_capture_timer,
    ):
        coordinator._on_camera_event(
            _MODELS.CameraEvent(
                kind=_MODELS.CameraEventKind.CONNECTION_CHANGE,
                state=_camera_state(connection_state=ConnectionState.DISCONNECTED),
            )
        )

    camera.connected = True
    coordinator._on_camera_event(
        _MODELS.CameraEvent(
            kind=_MODELS.CameraEventKind.CONNECTION_CHANGE,
            state=_camera_state(connection_state=ConnectionState.CONNECTED),
        )
    )

    assert coordinator.connected is True
    cancel_timer.assert_called_once()
    assert timeout_callback is not None
    baseline_calls = coordinator.async_update_listeners.call_count
    timeout_callback(None)
    assert coordinator.connected is True
    assert coordinator.async_update_listeners.call_count == baseline_calls


def test_camera_keeps_stream_on_reconnection() -> None:
    from datetime import UTC, datetime

    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)

    coordinator = _push_coordinator(_camera_state(last_seen=t1))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    _disable_state_writes(entity)

    entity._handle_coordinator_update()

    entity.stream = MagicMock()
    coordinator.data = _camera_state(last_seen=t2)
    entity._handle_coordinator_update()

    assert entity.stream is not None


def test_camera_expired_stream_source_clears_cache_without_hass() -> None:
    """Pre-hass expiry falls back to a cache-only clear, keeping the stream."""
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    _disable_state_writes(entity)
    entity._handle_coordinator_update()

    stream = MagicMock()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://old-url"
    with (
        patch("custom_components.nanit.camera._STREAM_SOURCE_MAX_AGE", 10.0),
        patch("custom_components.nanit.camera.time.monotonic", return_value=100.0),
    ):
        entity._stream_source_started_at = 89.0
        entity._handle_coordinator_update()

    assert entity.stream is stream
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0


def test_camera_keeps_fresh_stream_source() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    _disable_state_writes(entity)
    entity._handle_coordinator_update()

    stream = MagicMock()
    entity.stream = stream
    with (
        patch("custom_components.nanit.camera._STREAM_SOURCE_MAX_AGE", 10.0),
        patch("custom_components.nanit.camera.time.monotonic", return_value=100.0),
    ):
        entity._stream_source_started_at = 95.0
        entity._handle_coordinator_update()

    assert entity.stream is stream
    assert entity._stream_source_started_at == 95.0


async def test_camera_stream_source_schedules_backend_expiry_timer(
    hass: HomeAssistant,
) -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://stream-url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    cancel_timer = MagicMock()

    with patch(
        "custom_components.nanit.camera.async_call_later",
        return_value=cancel_timer,
    ) as mock_call_later:
        source = await entity.stream_source()

    assert source == "rtmps://stream-url"
    delays = [call.args[1] for call in mock_call_later.call_args_list]
    assert delays == [45 * 60, 5 * 60]  # expiry timer, then keepalive timer
    for call in mock_call_later.call_args_list:
        assert call.args[0] is hass
        # The scheduled target must be an event-loop callback — a bare lambda
        # would be classified as an executor job and fire in a worker thread.
        assert is_callback(call.args[2])
    assert entity._cancel_stream_expiry_timer is cancel_timer
    assert entity._cancel_stream_keepalive_timer is cancel_timer


def test_camera_invalidates_stream_on_power_state_change() -> None:
    from datetime import UTC, datetime

    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    coordinator = _push_coordinator(_camera_state(last_seen=t1, sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    _disable_state_writes(entity)

    entity._handle_coordinator_update()

    mock_stream = MagicMock()
    entity.stream = mock_stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0
    coordinator.data = _camera_state(last_seen=t1, sleep_mode=True)
    entity._handle_coordinator_update()

    assert entity.stream is None
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0


async def test_camera_reset_stream_service_stops_cached_stream_before_release(
    hass: HomeAssistant,
) -> None:
    coordinator = _push_coordinator(_camera_state())
    entity = NanitCameraEntity(coordinator, MagicMock(uid="cam_1"))
    entity.hass = hass
    stream = MagicMock()
    stream.stop = AsyncMock()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0
    cancel = MagicMock()
    entity._cancel_stream_expiry_timer = cancel

    await entity.async_reset_stream()

    stream.stop.assert_awaited_once_with()
    assert entity.stream is None
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0
    cancel.assert_called_once_with()
    assert entity._cancel_stream_expiry_timer is None


async def test_camera_reset_stream_times_out_hung_worker_and_releases_cache(
    hass: HomeAssistant,
) -> None:
    """A stuck HA stream worker must not block viewer recovery forever."""
    coordinator = _push_coordinator(_camera_state())
    entity = NanitCameraEntity(coordinator, MagicMock(uid="cam_1"))
    entity.hass = hass
    never_finishes = asyncio.Event()
    stream = MagicMock()
    stream.stop = AsyncMock(side_effect=never_finishes.wait)
    cast(Any, entity).stream = stream
    entity._stream_source_started_at = 100.0

    with patch("custom_components.nanit.camera._STREAM_STOP_TIMEOUT", 0.01):
        await asyncio.wait_for(entity._async_invalidate_stream("test timeout"), timeout=0.1)

    assert entity.stream is None
    assert entity._stream_source_started_at == 0.0


async def test_camera_expiry_rotates_source_in_place_with_active_outputs(
    hass: HomeAssistant,
) -> None:
    """An actively watched stream is renewed via update_source, never stopped.

    Stopping it blanked every open card at the 45-minute mark and set off
    the frontend recovery cascade.
    """
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://renewed-url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    stream = MagicMock()
    stream.stop = AsyncMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://old-url"
    entity._stream_source_started_at = 100.0

    entity._handle_stream_expiry()
    await hass.async_block_till_done()

    assert entity.stream is stream
    stream.stop.assert_not_awaited()
    stream.update_source.assert_called_once_with("rtmps://renewed-url")
    assert entity._cached_stream_source == "rtmps://renewed-url"
    assert entity._stream_source_started_at > 0.0
    # The renewal reschedules the expiry and keepalive timers — cancel for teardown.
    assert entity._cancel_stream_expiry_timer is not None
    assert entity._cancel_stream_keepalive_timer is not None
    entity._cancel_stream_timers()


async def test_camera_expiry_discards_idle_stream(hass: HomeAssistant) -> None:
    """Without active consumers the stale stream is dropped entirely."""
    coordinator = _push_coordinator(_camera_state())
    entity = NanitCameraEntity(coordinator, MagicMock(uid="cam_1"))
    entity.hass = hass
    stream = MagicMock()
    stream.stop = AsyncMock()
    stream.outputs.return_value = {}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://old-url"
    entity._stream_source_started_at = 100.0

    entity._handle_stream_expiry()
    await hass.async_block_till_done()

    assert entity.stream is None
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0
    stream.stop.assert_awaited_once_with()


async def test_camera_keepalive_resends_put_streaming_for_watched_stream(
    hass: HomeAssistant,
) -> None:
    """Nanit drops the RTMPS push ~20 min after the last PUT_STREAMING.

    A watched stream must be kept alive with periodic PUT_STREAMING or it
    starves and trips HA's demux timeout.
    """
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0
    cancel_timer = MagicMock()

    with patch(
        "custom_components.nanit.camera.async_call_later",
        return_value=cancel_timer,
    ) as mock_call_later:
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()

    # The send must be best-effort — a forced control-session reconnect on a
    # late ACK would itself kill the RTMPS push the keepalive protects.
    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://cached-url", reconnect_on_failure=False
    )
    # The keepalive must reschedule itself for the next interval.
    mock_call_later.assert_called_once()
    assert mock_call_later.call_args.args[1] == 5 * 60
    assert is_callback(mock_call_later.call_args.args[2])
    assert entity._cancel_stream_keepalive_timer is cancel_timer


async def test_camera_keepalive_skips_idle_stream_but_reschedules(
    hass: HomeAssistant,
) -> None:
    """Without consumers the camera is left to lapse — no PUT_STREAMING."""
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    stream = MagicMock()
    stream.outputs.return_value = {}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0

    with patch("custom_components.nanit.camera.async_call_later") as mock_call_later:
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()
    mock_call_later.assert_called_once()


async def test_camera_keepalive_stops_after_source_invalidation(
    hass: HomeAssistant,
) -> None:
    """A keepalive firing after invalidation must not resurrect the timer."""
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass

    with patch("custom_components.nanit.camera.async_call_later") as mock_call_later:
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()
    mock_call_later.assert_not_called()


async def test_camera_reconnect_resumes_watched_stream_push(
    hass: HomeAssistant,
) -> None:
    """The camera's RTMPS push dies with its control session.

    aionanit's reconnect handler does not re-send PUT_STREAMING (e.g. after
    the pre-emptive token-refresh reconnect), so the entity must resume the
    push on the reconnect transition before HA's 30s demux timeout starves
    viewers.
    """
    coordinator = _push_coordinator(_camera_state(connection_state=ConnectionState.RECONNECTING))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    _disable_state_writes(entity)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    # Fresh timestamp so the age-based expiry path stays out of the way.
    entity._stream_source_started_at = time.monotonic()

    entity._handle_coordinator_update()  # observes RECONNECTING
    camera.async_start_streaming.assert_not_awaited()

    coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
    entity._handle_coordinator_update()  # RECONNECTING -> CONNECTED
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://cached-url", reconnect_on_failure=False
    )
    # Cancel the rescheduled keepalive timer for teardown.
    entity._cancel_stream_timers()


async def test_camera_steady_connected_updates_do_not_resend_put_streaming(
    hass: HomeAssistant,
) -> None:
    """Routine push events while CONNECTED must not spam PUT_STREAMING."""
    coordinator = _push_coordinator(_camera_state(connection_state=ConnectionState.CONNECTED))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    _disable_state_writes(entity)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = time.monotonic()

    entity._handle_coordinator_update()
    entity._handle_coordinator_update()
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()
    assert entity._cancel_stream_keepalive_timer is None


async def test_camera_reconnect_without_cached_source_is_noop(
    hass: HomeAssistant,
) -> None:
    coordinator = _push_coordinator(_camera_state(connection_state=ConnectionState.RECONNECTING))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    _disable_state_writes(entity)

    entity._handle_coordinator_update()
    coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
    entity._handle_coordinator_update()
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()
    assert entity._cancel_stream_keepalive_timer is None


async def test_camera_stream_invalidation_cancels_keepalive_timer(
    hass: HomeAssistant,
) -> None:
    coordinator = _push_coordinator(_camera_state())
    entity = NanitCameraEntity(coordinator, MagicMock(uid="cam_1"))
    entity.hass = hass
    cancel_expiry = MagicMock()
    cancel_keepalive = MagicMock()
    entity._cancel_stream_expiry_timer = cancel_expiry
    entity._cancel_stream_keepalive_timer = cancel_keepalive
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0

    entity._expire_stream_source()

    cancel_expiry.assert_called_once_with()
    cancel_keepalive.assert_called_once_with()
    assert entity._cancel_stream_expiry_timer is None
    assert entity._cancel_stream_keepalive_timer is None


async def test_camera_removal_cancels_stream_timers_and_tasks(
    hass: HomeAssistant,
) -> None:
    """Removal must leave nothing behind that can fire after a reload.

    The keepalive timer reschedules itself indefinitely; if it survives
    entity removal it re-sends PUT_STREAMING with a stale URL through the
    old (stopped) camera, resurrecting its WebSocket and redirecting the
    camera's push away from the replacement entity's stream.
    """
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    entity.hass = hass
    stream = MagicMock()
    stream.stop = AsyncMock()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = 100.0
    cancel_expiry = MagicMock()
    cancel_keepalive = MagicMock()
    entity._cancel_stream_expiry_timer = cancel_expiry
    entity._cancel_stream_keepalive_timer = cancel_keepalive
    keepalive_task = MagicMock()
    keepalive_task.done.return_value = False
    entity._stream_keepalive_task = keepalive_task
    refresh_task = MagicMock()
    refresh_task.done.return_value = False
    entity._stream_refresh_task = refresh_task

    await entity.async_will_remove_from_hass()
    await hass.async_block_till_done()

    cancel_expiry.assert_called_once_with()
    cancel_keepalive.assert_called_once_with()
    keepalive_task.cancel.assert_called_once_with()
    refresh_task.cancel.assert_called_once_with()
    assert entity._stream_keepalive_task is None
    assert entity._stream_refresh_task is None
    assert entity._cached_stream_source is None
    assert entity._stream_source_started_at == 0.0
    assert entity.stream is None
    stream.stop.assert_awaited_once_with()


async def test_camera_start_streaming_safe_falls_back_for_legacy_client_signature() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    calls: list[dict[str, Any]] = []

    async def async_start_streaming(**kwargs: Any) -> None:
        calls.append(kwargs)
        if "rtmps_url" in kwargs:
            raise TypeError(
                "NanitCamera.async_start_streaming() got an unexpected keyword argument 'rtmps_url'"
            )

    camera.async_start_streaming = AsyncMock(side_effect=async_start_streaming)
    entity = NanitCameraEntity(coordinator, camera)

    assert await entity._async_start_streaming_safe("rtmps://stream-url") is True
    assert calls == [{"rtmps_url": "rtmps://stream-url"}, {}]


async def test_camera_start_streaming_safe_falls_back_when_wheel_lacks_best_effort() -> None:
    """A wheel without reconnect_on_failure gets the default (destructive) send."""
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    calls: list[dict[str, Any]] = []

    async def async_start_streaming(**kwargs: Any) -> None:
        calls.append(kwargs)
        if "reconnect_on_failure" in kwargs:
            raise TypeError(
                "NanitCamera.async_start_streaming() got an unexpected "
                "keyword argument 'reconnect_on_failure'"
            )

    camera.async_start_streaming = AsyncMock(side_effect=async_start_streaming)
    entity = NanitCameraEntity(coordinator, camera)

    result = await entity._async_start_streaming_safe(
        "rtmps://stream-url", reconnect_on_failure=False
    )

    assert result is True
    assert calls == [
        {"rtmps_url": "rtmps://stream-url", "reconnect_on_failure": False},
        {"rtmps_url": "rtmps://stream-url"},
    ]


async def test_camera_start_streaming_safe_best_effort_passes_flag_through() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    assert (
        await entity._async_start_streaming_safe("rtmps://stream-url", reconnect_on_failure=False)
        is True
    )
    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://stream-url", reconnect_on_failure=False
    )


async def test_camera_start_streaming_safe_does_not_restart_shared_camera() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock(side_effect=RuntimeError("ws closed"))
    camera.async_stop = AsyncMock()
    camera.async_start = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera)

    with patch("custom_components.nanit.camera._STREAM_RETRY_DELAY", 0):
        result = await entity._async_start_streaming_safe()

    assert result is False
    assert camera.async_start_streaming.await_count == 3
    camera.async_stop.assert_not_awaited()
    camera.async_start.assert_not_awaited()
