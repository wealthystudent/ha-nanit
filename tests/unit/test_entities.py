from __future__ import annotations

import importlib
import sys

# pyright: basic, reportUnusedFunction=false
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
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
from custom_components.nanit.light import NanitNightLight
from custom_components.nanit.number import NanitVolume
from custom_components.nanit.select import NanitNightLightTimer
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
    connection_state: ConnectionState = ConnectionState.CONNECTED,
) -> CameraState:
    return CameraState(
        sensors=SensorState(
            temperature=temperature,
            humidity=humidity,
            light=light,
        ),
        settings=SettingsState(
            volume=volume,
            sleep_mode=sleep_mode,
            night_vision=night_vision,
            night_light_brightness=night_light_brightness,
        ),
        control=ControlState(night_light=night_light, night_light_timeout=night_light_timeout),
        connection=ConnectionInfo(state=connection_state),
    )


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
    coordinator = _push_coordinator(_camera_state(connection_state=ConnectionState.DISCONNECTED))
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


async def test_light_turn_on() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.OFF))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        await entity.async_turn_on()

    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.ON)
    assert entity.is_on is True


async def test_light_turn_off() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.ON))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        await entity.async_turn_off()

    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.OFF)
    assert entity.is_on is False


async def test_light_turn_on_with_brightness() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.OFF))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        await entity.async_turn_on(brightness=128)

    camera.async_set_settings.assert_awaited_once_with(night_light_brightness=50)
    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.ON)
    assert entity.brightness == 128
    assert entity.is_on is True


async def test_light_grace_period_suppresses_stale_push_echo() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.OFF))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        await entity.async_turn_on()

    coordinator.data = _camera_state(night_light=NightLightState.OFF)
    with patch("custom_components.nanit.light.time.monotonic", return_value=101.0):
        entity._handle_coordinator_update()
    assert entity.is_on is True
    assert entity._command_is_on is True

    coordinator.data = _camera_state(night_light=NightLightState.ON)
    with patch("custom_components.nanit.light.time.monotonic", return_value=102.0):
        entity._handle_coordinator_update()
    assert entity.is_on is True
    assert entity._command_is_on is None


async def test_light_grace_period_expires_and_accepts_camera_state() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.OFF))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        await entity.async_turn_on()

    coordinator.data = _camera_state(night_light=NightLightState.OFF)
    with patch("custom_components.nanit.light.time.monotonic", return_value=116.0):
        entity._handle_coordinator_update()

    assert entity.is_on is False
    assert entity._command_is_on is None


async def test_light_turn_on_reverts_state_when_command_fails() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.OFF))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock(side_effect=RuntimeError("boom"))
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        with pytest.raises(RuntimeError):
            await entity.async_turn_on()

    assert entity.is_on is False
    assert entity._command_is_on is None


async def test_light_turn_off_reverts_state_when_command_fails() -> None:
    coordinator = _push_coordinator(_camera_state(night_light=NightLightState.ON))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock(side_effect=RuntimeError("boom"))
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)

    with patch("custom_components.nanit.light.time.monotonic", return_value=100.0):
        with pytest.raises(RuntimeError):
            await entity.async_turn_off()

    assert entity.is_on is True
    assert entity._command_is_on is None


def test_number_native_value_returns_volume() -> None:
    coordinator = _push_coordinator(_camera_state(volume=50))
    camera = MagicMock(uid="cam_1")
    entity = NanitVolume(coordinator, camera)

    assert entity.native_value == 50


async def test_number_set_native_value_calls_camera_settings() -> None:
    coordinator = _push_coordinator(_camera_state(volume=10))
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock()
    entity = NanitVolume(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_set_native_value(33.7)

    camera.async_set_settings.assert_awaited_once_with(volume=33)


def test_select_current_option_returns_timer_value() -> None:
    coordinator = _push_coordinator(_camera_state(night_light_timeout=3600))
    camera = MagicMock(uid="cam_1")
    entity = NanitNightLightTimer(coordinator, camera)

    assert entity.current_option == "1_hour"


def test_select_current_option_returns_none_when_no_data() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitNightLightTimer(coordinator, camera)

    assert entity.current_option is None


def test_select_none_timeout_maps_to_off() -> None:
    coordinator = _push_coordinator(_camera_state(night_light_timeout=None))
    camera = MagicMock(uid="cam_1")
    entity = NanitNightLightTimer(coordinator, camera)

    assert entity.current_option == "off"


def test_select_unknown_timeout_maps_to_off() -> None:
    coordinator = _push_coordinator(_camera_state(night_light_timeout=9999))
    camera = MagicMock(uid="cam_1")
    entity = NanitNightLightTimer(coordinator, camera)

    assert entity.current_option == "off"


async def test_select_option_calls_camera_control() -> None:
    coordinator = _push_coordinator(_camera_state(night_light_timeout=0))
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    entity = NanitNightLightTimer(coordinator, camera)
    _disable_state_writes(entity)

    await entity.async_select_option("30_minutes")

    camera.async_set_control.assert_awaited_once_with(night_light_timeout=1800)


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
    entity.hass = MagicMock()
    entity.hass.async_create_background_task = MagicMock(
        side_effect=lambda coro, **kw: coro.close(),
    )

    source = await entity.stream_source()

    assert source == "rtmps://stream-url"
    entity.hass.async_create_background_task.assert_called_once()


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
    entity.hass = MagicMock()
    entity.hass.async_create_background_task = MagicMock()

    source = await entity.stream_source()

    assert source is None
    entity.hass.async_create_background_task.assert_not_called()


async def test_camera_start_streaming_safe_logs_failure_without_raising() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_start_streaming = AsyncMock(side_effect=RuntimeError("ws closed"))
    entity = NanitCameraEntity(coordinator, camera)

    await entity._async_start_streaming_safe()

    camera.async_start_streaming.assert_awaited_once()


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
