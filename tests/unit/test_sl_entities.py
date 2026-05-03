from __future__ import annotations

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_HS_COLOR
from homeassistant.util.color import brightness_to_value, value_to_brightness

_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from custom_components.nanit import light as light_platform
from custom_components.nanit import select as select_platform
from custom_components.nanit import switch as switch_platform
from custom_components.nanit.aionanit_sl.exceptions import NanitTransportError
from custom_components.nanit.aionanit_sl.models import SoundLightFullState
from custom_components.nanit.const import DEFAULT_SOUND_MACHINE_SOUNDS
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics
from custom_components.nanit.light import (
    _BRIGHTNESS_SCALE,
    NanitNightLight,
    NanitSoundLightLight,
)
from custom_components.nanit.light import (
    _COMMAND_GRACE_PERIOD as _NL_GRACE,
)
from custom_components.nanit.select import NanitNightLightTimer, NanitSoundSelect
from custom_components.nanit.switch import NanitSLPowerSwitch, NanitSLSoundSwitch

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


def _disable_state_writes(entity) -> None:
    entity.async_write_ha_state = MagicMock()


def _sl_coordinator(
    state: SoundLightFullState | None, *, last_update_success: bool = True
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = last_update_success
    coordinator.connected = True
    coordinator.sound_light = MagicMock()
    coordinator.sound_light.async_set_light_enabled = AsyncMock()
    coordinator.sound_light.async_set_brightness = AsyncMock()
    coordinator.sound_light.async_set_color = AsyncMock()
    coordinator.sound_light.async_set_power = AsyncMock()
    coordinator.sound_light.async_set_sound_on = AsyncMock()
    coordinator.sound_light.async_set_track = AsyncMock()
    return coordinator


def test_light_is_on_returns_none_when_no_data() -> None:
    entity = NanitSoundLightLight(_sl_coordinator(None))

    assert entity.is_on is None


def test_light_is_on_returns_none_when_both_fields_none() -> None:
    state = SoundLightFullState(light_enabled=None, brightness=None)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.is_on is None


def test_light_is_on_returns_true_when_light_enabled_true() -> None:
    state = SoundLightFullState(light_enabled=True, brightness=0.0)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.is_on is True


def test_light_is_on_returns_false_when_light_enabled_false() -> None:
    state = SoundLightFullState(light_enabled=False, brightness=1.0)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.is_on is False


def test_light_is_on_uses_brightness_when_light_enabled_missing_above_threshold() -> None:
    state = SoundLightFullState(light_enabled=None, brightness=0.5)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.is_on is True


def test_light_is_on_uses_brightness_when_light_enabled_missing_below_threshold() -> None:
    state = SoundLightFullState(light_enabled=None, brightness=0.001)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.is_on is False


def test_light_brightness_returns_none_when_no_data() -> None:
    entity = NanitSoundLightLight(_sl_coordinator(None))

    assert entity.brightness is None


def test_light_brightness_returns_none_when_brightness_field_is_none() -> None:
    state = SoundLightFullState(brightness=None)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.brightness is None


def test_light_brightness_converts_float_to_255_scale() -> None:
    state = SoundLightFullState(brightness=0.5)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.brightness == 127


def test_light_brightness_returns_one_when_on_and_device_reports_near_zero() -> None:
    state = SoundLightFullState(power_on=True, light_enabled=True, brightness=0.001)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.brightness == 1


def test_light_hs_color_returns_none_when_no_data() -> None:
    entity = NanitSoundLightLight(_sl_coordinator(None))

    assert entity.hs_color is None


def test_light_hs_color_returns_none_when_both_colors_none() -> None:
    state = SoundLightFullState(color_r=None, color_g=None)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    assert entity.hs_color is None


def test_light_hs_color_handles_only_color_g_set() -> None:
    state = SoundLightFullState(color_g=0.5)
    entity = NanitSoundLightLight(_sl_coordinator(state))
    hs = entity.hs_color
    assert hs is not None
    assert hs[0] == pytest.approx(0.0)
    assert hs[1] == pytest.approx(50.0)


def test_light_hs_color_converts_state_to_homeassistant_hs() -> None:
    state = SoundLightFullState(color_r=0.25, color_g=0.6)
    entity = NanitSoundLightLight(_sl_coordinator(state))

    hue, sat = entity.hs_color
    assert hue == pytest.approx(90.0)
    assert sat == pytest.approx(60.0)


async def test_light_turn_on_calls_set_light_enabled_true() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()

    coordinator.sound_light.async_set_light_enabled.assert_awaited_once_with(True)


async def test_light_turn_on_with_hs_color_calls_set_color() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on(**{ATTR_HS_COLOR: (180.0, 40.0)})

    coordinator.sound_light.async_set_color.assert_awaited_once_with(0.5, 0.4)


async def test_light_turn_on_with_brightness_calls_set_brightness() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 128})

    coordinator.sound_light.async_set_brightness.assert_awaited_once_with(128 / 255)


async def test_light_turn_off_calls_set_light_enabled_false() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_off()

    coordinator.sound_light.async_set_light_enabled.assert_awaited_once_with(False)


async def test_light_turn_on_handles_transport_error_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.async_set_light_enabled.side_effect = NanitTransportError("boom")
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()

    assert "Failed to control Sound & Light light" in caplog.text


async def test_light_turn_off_handles_transport_error_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.async_set_light_enabled.side_effect = NanitTransportError("boom")
    entity = NanitSoundLightLight(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_off()

    assert "Failed to turn off Sound & Light light" in caplog.text


def test_select_options_returns_default_sounds_when_no_data() -> None:
    entity = NanitSoundSelect(_sl_coordinator(None))

    assert entity.options == [s.replace("_", " ").title() for s in DEFAULT_SOUND_MACHINE_SOUNDS]


def test_select_options_returns_available_tracks_from_state() -> None:
    state = SoundLightFullState(available_tracks=("rain", "wind"))
    entity = NanitSoundSelect(_sl_coordinator(state))

    assert entity.options == ["rain", "wind"]


def test_select_current_option_returns_none_when_no_data() -> None:
    entity = NanitSoundSelect(_sl_coordinator(None))

    assert entity.current_option is None


def test_select_current_option_returns_state_track() -> None:
    state = SoundLightFullState(current_track="ocean")
    entity = NanitSoundSelect(_sl_coordinator(state))

    assert entity.current_option == "ocean"


async def test_select_select_option_calls_set_track() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundSelect(coordinator)
    _disable_state_writes(entity)

    await entity.async_select_option("rain")

    coordinator.sound_light.async_set_track.assert_awaited_once_with("rain")


async def test_select_select_option_handles_transport_error_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.async_set_track.side_effect = NanitTransportError("boom")
    entity = NanitSoundSelect(coordinator)
    _disable_state_writes(entity)

    await entity.async_select_option("rain")

    assert "Failed to set sound to rain" in caplog.text


def test_sl_power_switch_is_on_returns_none_when_no_data() -> None:
    entity = NanitSLPowerSwitch(_sl_coordinator(None))

    assert entity.is_on is None


def test_sl_power_switch_is_on_returns_power_on_from_state() -> None:
    entity = NanitSLPowerSwitch(_sl_coordinator(SoundLightFullState(power_on=True)))

    assert entity.is_on is True


async def test_sl_power_switch_turn_on_calls_set_power_true() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLPowerSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()

    coordinator.sound_light.async_set_power.assert_awaited_once_with(True)


async def test_sl_power_switch_turn_off_calls_set_power_false() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLPowerSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_off()

    coordinator.sound_light.async_set_power.assert_awaited_once_with(False)


async def test_sl_power_switch_handles_transport_error_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.async_set_power.side_effect = NanitTransportError("boom")
    entity = NanitSLPowerSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert "Failed to turn on S&L device" in caplog.text
    assert "Failed to turn off S&L device" in caplog.text


def test_sl_sound_switch_is_on_returns_sound_on_from_state() -> None:
    entity = NanitSLSoundSwitch(_sl_coordinator(SoundLightFullState(sound_on=True)))

    assert entity.is_on is True


async def test_sl_sound_switch_turn_on_calls_set_sound_on_true() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLSoundSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()

    coordinator.sound_light.async_set_sound_on.assert_awaited_once_with(True)


async def test_sl_sound_switch_turn_off_calls_set_sound_on_false() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLSoundSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_off()

    coordinator.sound_light.async_set_sound_on.assert_awaited_once_with(False)


async def test_sl_sound_switch_handles_transport_error_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.async_set_sound_on.side_effect = NanitTransportError("boom")
    entity = NanitSLSoundSwitch(coordinator)
    _disable_state_writes(entity)

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert "Failed to turn on S&L sound" in caplog.text
    assert "Failed to turn off S&L sound" in caplog.text


async def test_light_async_setup_entry_adds_entities_for_sound_light_coordinators() -> None:
    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cam_with_sl = MagicMock(
        push_coordinator=MagicMock(data=None),
        camera=MagicMock(uid="cam_1"),
        sound_light_coordinator=sl_coordinator,
    )
    cam_without_sl = MagicMock(
        push_coordinator=MagicMock(data=None),
        camera=MagicMock(uid="cam_2"),
        sound_light_coordinator=None,
    )
    entry = MagicMock(
        runtime_data=MagicMock(cameras={"cam_1": cam_with_sl, "cam_2": cam_without_sl})
    )
    async_add_entities = MagicMock()

    await light_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    # 2 camera night lights + 1 S&L light = 3
    assert len(entities) == 3
    assert sum(isinstance(e, NanitNightLight) for e in entities) == 2
    assert sum(isinstance(e, NanitSoundLightLight) for e in entities) == 1


async def test_select_async_setup_entry_adds_entities_for_sound_light_coordinators() -> None:
    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cam_with_sl = MagicMock(
        push_coordinator=MagicMock(data=None),
        camera=MagicMock(uid="cam_1"),
        sound_light_coordinator=sl_coordinator,
    )
    cam_without_sl = MagicMock(
        push_coordinator=MagicMock(data=None),
        camera=MagicMock(uid="cam_2"),
        sound_light_coordinator=None,
    )
    entry = MagicMock(
        runtime_data=MagicMock(cameras={"cam_1": cam_with_sl, "cam_2": cam_without_sl})
    )
    async_add_entities = MagicMock()

    await select_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    # 2 camera night light timers + 1 S&L sound select = 3
    assert len(entities) == 3
    assert sum(isinstance(e, NanitNightLightTimer) for e in entities) == 2
    assert sum(isinstance(e, NanitSoundSelect) for e in entities) == 1


async def test_switch_async_setup_entry_adds_camera_and_sound_light_switches() -> None:
    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cam_with_sl = MagicMock(
        push_coordinator=MagicMock(),
        camera=MagicMock(uid="cam_1"),
        sound_light_coordinator=sl_coordinator,
    )
    cam_without_sl = MagicMock(
        push_coordinator=MagicMock(),
        camera=MagicMock(uid="cam_2"),
        sound_light_coordinator=None,
    )
    entry = MagicMock(
        runtime_data=MagicMock(cameras={"cam_1": cam_with_sl, "cam_2": cam_without_sl})
    )
    async_add_entities = MagicMock()

    await switch_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    assert len(entities) == 4
    assert any(isinstance(entity, NanitSLPowerSwitch) for entity in entities)
    assert any(isinstance(entity, NanitSLSoundSwitch) for entity in entities)


async def test_async_get_config_entry_diagnostics_returns_expected_structure() -> None:
    push = MagicMock(last_update_success=True, last_exception=None, connected=True, data=None)
    cam_data = MagicMock(
        baby=Baby(uid="baby_1", name="Nursery", camera_uid="cam_1"),
        push_coordinator=push,
        cloud_coordinator=None,
    )
    entry = MagicMock(
        data={"email": "test@example.com", "camera_uid": "cam_1"},
        options={"camera_ip": "192.168.0.10"},
        runtime_data=MagicMock(cameras={"cam_1": cam_data}),
    )

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["camera_count"] == 1
    assert "config_entry_data" in result
    assert "config_entry_options" in result
    assert "cameras" in result
    assert "cam_1" in result["cameras"]
    assert result["cameras"]["cam_1"]["push_coordinator"]["connected"] is True


async def test_async_get_config_entry_diagnostics_redacts_sensitive_fields() -> None:
    push = MagicMock(last_update_success=True, last_exception=None, connected=True, data=None)
    cam_data = MagicMock(
        baby=Baby(uid="baby_1", name="Nursery", camera_uid="cam_1"),
        push_coordinator=push,
        cloud_coordinator=None,
    )
    entry = MagicMock(
        data={
            "email": "test@example.com",
            "password": "secret",
            "access_token": "token",
            "refresh_token": "refresh",
        },
        options={"camera_ip": "192.168.0.10", "camera_ips": "192.168.0.11"},
        runtime_data=MagicMock(cameras={"cam_1": cam_data}),
    )

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["config_entry_data"]["email"] == REDACTED
    assert result["config_entry_data"]["password"] == REDACTED
    assert result["config_entry_data"]["access_token"] == REDACTED
    assert result["config_entry_data"]["refresh_token"] == REDACTED
    assert result["config_entry_options"]["camera_ip"] == REDACTED
    assert result["config_entry_options"]["camera_ips"] == REDACTED
    assert result["cameras"]["cam_1"]["baby_uid"] == REDACTED


async def test_async_get_config_entry_diagnostics_includes_cloud_data_when_present() -> None:
    push = MagicMock(last_update_success=True, last_exception=None, connected=True, data=None)
    cloud = MagicMock(
        last_update_success=False,
        last_exception=RuntimeError("cloud boom"),
        data=[CloudEvent(event_type="MOTION", timestamp=1234.0, baby_uid="baby_1")],
    )
    cam_data = MagicMock(
        baby=Baby(uid="baby_1", name="Nursery", camera_uid="cam_1"),
        push_coordinator=push,
        cloud_coordinator=cloud,
    )
    entry = MagicMock(
        data={},
        options={},
        runtime_data=MagicMock(cameras={"cam_1": cam_data}),
    )

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    cloud_diag = result["cameras"]["cam_1"]["cloud_coordinator"]
    assert cloud_diag["last_update_success"] is False
    assert cloud_diag["last_exception"] == "cloud boom"
    assert cloud_diag["data"][0]["event_type"] == "MOTION"
    assert cloud_diag["data"][0]["baby_uid"] == REDACTED


# ---------------------------------------------------------------------------
# NanitSwitch (camera power) — _handle_coordinator_update / turn_on / restore
# ---------------------------------------------------------------------------


def _push_coordinator(
    state: CameraState | None,
    *,
    connected: bool = True,
    last_update_success: bool = True,
) -> MagicMock:
    """Build a mock NanitPushCoordinator."""
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    coordinator.last_update_success = last_update_success
    coordinator.connected = connected
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _camera_state(*, sleep_mode: bool | None = False) -> CameraState:
    return CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(volume=50, sleep_mode=sleep_mode, night_vision=True),
        control=ControlState(),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )


from custom_components.nanit.switch import _COMMAND_GRACE_PERIOD, SWITCHES, NanitSwitch

_switch_desc = next(d for d in SWITCHES if d.key == "camera_power")


def test_nanit_switch_turn_on_sets_optimistic_state() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=True))
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock()
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    import asyncio

    asyncio.get_event_loop().run_until_complete(entity.async_turn_on())

    assert entity.is_on is True
    assert entity._command_state is True
    camera.async_set_settings.assert_awaited_once_with(sleep_mode=False)


async def test_nanit_switch_turn_on_reverts_on_error() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=True))
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock(side_effect=RuntimeError("fail"))
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    # Initial state: switch reports not-sleep_mode=True → is_on = True
    # But the state is sleep_mode=True → value_fn returns False
    initial = entity.is_on

    with pytest.raises(RuntimeError, match="fail"):
        await entity.async_turn_on()

    # State should revert
    assert entity.is_on == initial
    assert entity._command_state is None


async def test_nanit_switch_turn_off_reverts_on_error() -> None:
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock(side_effect=RuntimeError("fail"))
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    initial = entity.is_on

    with pytest.raises(RuntimeError, match="fail"):
        await entity.async_turn_off()

    assert entity.is_on == initial
    assert entity._command_state is None


def test_nanit_switch_handle_coordinator_update_normal() -> None:
    """Normal update without grace period sets is_on."""
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    # Simulate coordinator update with sleep_mode=True → is_on=False
    coordinator.data = _camera_state(sleep_mode=True)
    entity._handle_coordinator_update()

    assert entity.is_on is False


def test_nanit_switch_handle_coordinator_update_grace_period_confirms() -> None:
    """Within grace period, matching push confirms command and clears grace."""
    import time

    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    # Simulate a command was sent (turn off → expect False)
    entity._command_state = False
    entity._command_ts = time.monotonic()

    # Push arrives confirming sleep_mode=True → is_on=False → matches command
    coordinator.data = _camera_state(sleep_mode=True)
    entity._handle_coordinator_update()

    assert entity.is_on is False
    assert entity._command_state is None  # cleared


def test_nanit_switch_handle_coordinator_update_grace_period_stale_push() -> None:
    """Within grace period, contradicting push is suppressed."""
    import time

    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    # Simulate command: turn off → expect False
    entity._command_state = False
    entity._command_ts = time.monotonic()
    entity._attr_is_on = False  # optimistic

    # Stale push says sleep_mode=False → is_on=True → contradicts
    coordinator.data = _camera_state(sleep_mode=False)
    entity._handle_coordinator_update()

    # Should still be False (stale push suppressed)
    assert entity.is_on is False
    assert entity._command_state is False  # still active


def test_nanit_switch_handle_coordinator_update_grace_period_expired() -> None:
    """After grace period expires, push updates are accepted."""
    import time

    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    # Simulate command long ago
    entity._command_state = False
    entity._command_ts = time.monotonic() - _COMMAND_GRACE_PERIOD - 1

    # Push says sleep_mode=False → is_on=True
    coordinator.data = _camera_state(sleep_mode=False)
    entity._handle_coordinator_update()

    assert entity.is_on is True
    assert entity._command_state is None  # cleared


async def test_nanit_switch_async_added_to_hass_restores_state() -> None:
    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitSwitch(coordinator, camera, _switch_desc)
    _disable_state_writes(entity)

    assert entity._attr_is_on is None

    last_state = MagicMock()
    last_state.state = "on"
    entity.async_get_last_state = AsyncMock(return_value=last_state)
    entity.async_on_remove = MagicMock()
    # Mock the parent async_added_to_hass
    with patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass"):
        await entity.async_added_to_hass()

    assert entity._attr_is_on is True


# ---------------------------------------------------------------------------
# S&L Sound switch — is_on returns None when no data
# ---------------------------------------------------------------------------


def test_sl_sound_switch_is_on_returns_none_when_no_data() -> None:
    entity = NanitSLSoundSwitch(_sl_coordinator(None))

    assert entity.is_on is None


# ---------------------------------------------------------------------------
# NanitSLConnectivitySensor — binary sensor
# ---------------------------------------------------------------------------


def test_sl_connectivity_sensor_is_on_when_connected() -> None:
    from custom_components.nanit.binary_sensor import NanitSLConnectivitySensor

    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.connected = True
    entity = NanitSLConnectivitySensor(coordinator)

    assert entity.is_on is True


def test_sl_connectivity_sensor_is_off_when_disconnected() -> None:
    from custom_components.nanit.binary_sensor import NanitSLConnectivitySensor

    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.connected = False
    entity = NanitSLConnectivitySensor(coordinator)

    assert entity.is_on is False


def test_sl_connectivity_sensor_available_with_data() -> None:
    from custom_components.nanit.binary_sensor import NanitSLConnectivitySensor

    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLConnectivitySensor(coordinator)

    assert entity.available is True


def test_sl_connectivity_sensor_not_available_without_data() -> None:
    from custom_components.nanit.binary_sensor import NanitSLConnectivitySensor

    coordinator = _sl_coordinator(None)
    entity = NanitSLConnectivitySensor(coordinator)

    assert entity.available is False


# ---------------------------------------------------------------------------
# NanitSLSensor — S&L temperature/humidity sensors
# ---------------------------------------------------------------------------


def test_sl_sensor_temperature() -> None:
    from custom_components.nanit.sensor import SL_SENSORS, NanitSLSensor

    desc = next(d for d in SL_SENSORS if d.key == "sl_temperature")
    state = SoundLightFullState(temperature_c=23.456)
    coordinator = _sl_coordinator(state)
    entity = NanitSLSensor(coordinator, desc)

    assert entity.native_value == pytest.approx(23.46)
    assert entity.unique_id == "cam_1_sl_temperature"


def test_sl_sensor_humidity() -> None:
    from custom_components.nanit.sensor import SL_SENSORS, NanitSLSensor

    desc = next(d for d in SL_SENSORS if d.key == "sl_humidity")
    state = SoundLightFullState(humidity_pct=55.789)
    coordinator = _sl_coordinator(state)
    entity = NanitSLSensor(coordinator, desc)

    assert entity.native_value == pytest.approx(55.79)


def test_sl_sensor_returns_none_when_no_data() -> None:
    from custom_components.nanit.sensor import SL_SENSORS, NanitSLSensor

    desc = next(d for d in SL_SENSORS if d.key == "sl_temperature")
    coordinator = _sl_coordinator(None)
    entity = NanitSLSensor(coordinator, desc)

    assert entity.native_value is None


def test_sl_sensor_returns_none_when_value_is_none() -> None:
    from custom_components.nanit.sensor import SL_SENSORS, NanitSLSensor

    desc = next(d for d in SL_SENSORS if d.key == "sl_temperature")
    state = SoundLightFullState(temperature_c=None)
    coordinator = _sl_coordinator(state)
    entity = NanitSLSensor(coordinator, desc)

    assert entity.native_value is None


# ---------------------------------------------------------------------------
# NanitSLConnectionModeSensor
# ---------------------------------------------------------------------------


def test_sl_connection_mode_sensor() -> None:
    from custom_components.nanit.sensor import NanitSLConnectionModeSensor

    coordinator = _sl_coordinator(SoundLightFullState())
    coordinator.sound_light.connection_mode = "cloud"
    entity = NanitSLConnectionModeSensor(coordinator)

    assert entity.native_value == "cloud"
    assert entity.unique_id == "cam_1_sl_connection_mode"


def test_sl_connection_mode_sensor_available_with_data() -> None:
    from custom_components.nanit.sensor import NanitSLConnectionModeSensor

    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSLConnectionModeSensor(coordinator)

    assert entity.available is True


def test_sl_connection_mode_sensor_not_available_without_data() -> None:
    from custom_components.nanit.sensor import NanitSLConnectionModeSensor

    coordinator = _sl_coordinator(None)
    entity = NanitSLConnectionModeSensor(coordinator)

    assert entity.available is False


# ---------------------------------------------------------------------------
# NanitSoundMachineVolume — S&L volume number entity
# ---------------------------------------------------------------------------


def test_sl_volume_native_value() -> None:
    from custom_components.nanit.number import NanitSoundMachineVolume

    state = SoundLightFullState(volume=0.5)
    coordinator = _sl_coordinator(state)
    entity = NanitSoundMachineVolume(coordinator)

    assert entity.native_value == 50.0
    assert entity.unique_id == "cam_1_sound_machine_volume"


def test_sl_volume_returns_none_when_no_data() -> None:
    from custom_components.nanit.number import NanitSoundMachineVolume

    coordinator = _sl_coordinator(None)
    entity = NanitSoundMachineVolume(coordinator)

    assert entity.native_value is None


def test_sl_volume_returns_none_when_volume_is_none() -> None:
    from custom_components.nanit.number import NanitSoundMachineVolume

    state = SoundLightFullState(volume=None)
    coordinator = _sl_coordinator(state)
    entity = NanitSoundMachineVolume(coordinator)

    assert entity.native_value is None


async def test_sl_volume_set_value() -> None:
    from custom_components.nanit.number import NanitSoundMachineVolume

    coordinator = _sl_coordinator(SoundLightFullState(volume=0.5))
    coordinator.sound_light.async_set_volume = AsyncMock()
    entity = NanitSoundMachineVolume(coordinator)
    _disable_state_writes(entity)

    await entity.async_set_native_value(75.0)

    coordinator.sound_light.async_set_volume.assert_awaited_once_with(0.75)


async def test_sl_volume_set_value_handles_transport_error() -> None:
    from custom_components.nanit.number import NanitSoundMachineVolume

    coordinator = _sl_coordinator(SoundLightFullState(volume=0.5))
    coordinator.sound_light.async_set_volume = AsyncMock(side_effect=NanitTransportError("offline"))
    entity = NanitSoundMachineVolume(coordinator)
    _disable_state_writes(entity)

    await entity.async_set_native_value(75.0)


# ---------------------------------------------------------------------------
# async_setup_entry — sensor, number, binary_sensor platforms
# ---------------------------------------------------------------------------


async def test_sensor_async_setup_entry_creates_sl_sensors() -> None:
    from custom_components.nanit import sensor as sensor_platform

    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cam_data = MagicMock(
        push_coordinator=_push_coordinator(_camera_state()),
        sound_light_coordinator=sl_coordinator,
        network_coordinator=None,
    )
    entry = MagicMock(runtime_data=MagicMock(cameras={"cam_1": cam_data}))
    async_add_entities = MagicMock()

    await sensor_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    # 3 camera sensors + 2 S&L sensors + 1 connection mode = 6
    assert len(entities) == 6


async def test_number_async_setup_entry_creates_sl_volume() -> None:
    from custom_components.nanit import number as number_platform

    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cam_data = MagicMock(
        push_coordinator=_push_coordinator(_camera_state()),
        camera=MagicMock(uid="cam_1"),
        sound_light_coordinator=sl_coordinator,
    )
    entry = MagicMock(runtime_data=MagicMock(cameras={"cam_1": cam_data}))
    async_add_entities = MagicMock()

    await number_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    # 1 S&L volume (camera volume entity removed)
    assert len(entities) == 1


async def test_binary_sensor_async_setup_entry_creates_sl_connectivity() -> None:
    from custom_components.nanit import binary_sensor as binary_sensor_platform

    sl_coordinator = _sl_coordinator(SoundLightFullState())
    cloud_coordinator = MagicMock()
    cam_data = MagicMock(
        push_coordinator=_push_coordinator(_camera_state()),
        cloud_coordinator=cloud_coordinator,
        sound_light_coordinator=sl_coordinator,
    )
    entry = MagicMock(runtime_data=MagicMock(cameras={"cam_1": cam_data}))
    async_add_entities = MagicMock()

    await binary_sensor_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    entities = async_add_entities.call_args.args[0]
    # 1 push binary sensor + 2 cloud binary sensors + 1 S&L connectivity = 4
    assert len(entities) == 4


# ---------------------------------------------------------------------------
# NanitSoundLightEntity — base class availability
# ---------------------------------------------------------------------------


def test_sl_entity_available_when_data_present() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)

    assert entity.available is True


def test_sl_entity_not_available_when_no_data() -> None:
    coordinator = _sl_coordinator(None)
    entity = NanitSoundLightLight(coordinator)

    assert entity.available is False


def test_sl_entity_not_available_when_update_failed() -> None:
    coordinator = _sl_coordinator(SoundLightFullState(), last_update_success=False)
    entity = NanitSoundLightLight(coordinator)

    assert entity.available is False


def test_sl_entity_device_info() -> None:
    coordinator = _sl_coordinator(SoundLightFullState())
    entity = NanitSoundLightLight(coordinator)
    info = entity.device_info

    assert ("nanit", "cam_1_sound_light") in info["identifiers"]
    assert info["name"] == "Nursery Sound & Light"
    assert info["manufacturer"] == "Nanit"


# ---------------------------------------------------------------------------
# NanitEntity / NanitCloudEntity base class coverage
# ---------------------------------------------------------------------------


def test_nanit_entity_device_info() -> None:
    from custom_components.nanit.sensor import SENSORS, NanitSensor

    desc = next(d for d in SENSORS if d.key == "temperature")
    coordinator = _push_coordinator(_camera_state())
    entity = NanitSensor(coordinator, desc)
    info = entity.device_info

    assert ("nanit", "cam_1") in info["identifiers"]
    assert info["name"] == "Nursery"
    assert info["manufacturer"] == "Nanit"


def test_nanit_entity_available_requires_connected() -> None:
    from custom_components.nanit.sensor import SENSORS, NanitSensor

    desc = next(d for d in SENSORS if d.key == "temperature")
    coordinator = _push_coordinator(_camera_state(), connected=False)
    entity = NanitSensor(coordinator, desc)

    assert entity.available is False


def test_nanit_cloud_entity_device_info() -> None:
    from custom_components.nanit.binary_sensor import CLOUD_BINARY_SENSORS, NanitCloudBinarySensor

    desc = next(d for d in CLOUD_BINARY_SENSORS if d.key == "cloud_motion")
    coordinator = MagicMock()
    coordinator.data = []
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    entity = NanitCloudBinarySensor(coordinator, desc)
    info = entity.device_info

    assert ("nanit", "cam_1") in info["identifiers"]
    assert info["name"] == "Nursery"


def test_cloud_binary_sensor_is_on_returns_none_when_no_data() -> None:
    from custom_components.nanit.binary_sensor import CLOUD_BINARY_SENSORS, NanitCloudBinarySensor

    desc = next(d for d in CLOUD_BINARY_SENSORS if d.key == "cloud_motion")
    coordinator = MagicMock()
    coordinator.data = None
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    entity = NanitCloudBinarySensor(coordinator, desc)

    assert entity.is_on is None


def test_binary_sensor_always_available_uses_alt_check() -> None:
    from custom_components.nanit.binary_sensor import BINARY_SENSORS, NanitBinarySensor

    desc = next(d for d in BINARY_SENSORS if d.key == "connectivity")
    coordinator = _push_coordinator(_camera_state(), connected=False)
    entity = NanitBinarySensor(coordinator, desc)

    assert entity.available is True


def test_binary_sensor_is_on_returns_none_when_no_data() -> None:
    from custom_components.nanit.binary_sensor import BINARY_SENSORS, NanitBinarySensor

    desc = next(d for d in BINARY_SENSORS if d.key == "connectivity")
    coordinator = _push_coordinator(None)
    entity = NanitBinarySensor(coordinator, desc)

    assert entity.is_on is None


def test_cloud_binary_sensor_skips_non_matching_event_type() -> None:
    import time as time_mod

    from custom_components.nanit.binary_sensor import CLOUD_BINARY_SENSORS, NanitCloudBinarySensor

    desc = next(d for d in CLOUD_BINARY_SENSORS if d.key == "cloud_motion")
    coordinator = MagicMock()
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    now = time_mod.time()
    coordinator.data = [
        CloudEvent(event_type="SOUND", timestamp=now - 1, baby_uid="baby_1"),
    ]
    entity = NanitCloudBinarySensor(coordinator, desc)

    with patch("custom_components.nanit.binary_sensor.time_mod.time", return_value=now):
        assert entity.is_on is False


async def test_camera_async_setup_entry_creates_entities() -> None:
    from custom_components.nanit import camera as camera_platform

    cam_data = MagicMock(
        push_coordinator=_push_coordinator(_camera_state()),
        camera=MagicMock(uid="cam_1"),
    )
    entry = MagicMock(runtime_data=MagicMock(cameras={"cam_1": cam_data}))
    async_add_entities = MagicMock()

    await camera_platform.async_setup_entry(MagicMock(), entry, async_add_entities)

    async_add_entities.assert_called_once()


def test_camera_is_on_true_when_no_data() -> None:
    from custom_components.nanit.camera import NanitCameraEntity

    coordinator = _push_coordinator(None)
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)

    assert entity.is_on is True


def test_camera_is_on_true_when_sleep_mode_none() -> None:
    from custom_components.nanit.camera import NanitCameraEntity

    state = CameraState(
        sensors=SensorState(),
        settings=SettingsState(sleep_mode=None),
        control=ControlState(),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    coordinator = _push_coordinator(state)
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)

    assert entity.is_on is True


def test_camera_handle_coordinator_update_tracks_prev_state() -> None:
    from custom_components.nanit.camera import NanitCameraEntity

    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    _disable_state_writes(entity)

    entity._handle_coordinator_update()
    assert entity._prev_is_on is True

    coordinator.data = _camera_state(sleep_mode=True)
    entity._handle_coordinator_update()
    assert entity._prev_is_on is False


def test_camera_invalidate_stream_clears_stream() -> None:
    from custom_components.nanit.camera import NanitCameraEntity

    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = MagicMock(uid="cam_1")
    entity = NanitCameraEntity(coordinator, camera)
    entity.stream = MagicMock()

    entity._invalidate_stream()

    assert entity.stream is None


# ---------------------------------------------------------------------------
# NanitNightLight — camera night light entity
# ---------------------------------------------------------------------------


def _night_light_entity(
    *,
    night_light: NightLightState | None = None,
    night_light_timeout: int | None = None,
    night_light_brightness: int | None = None,
    data_is_none: bool = False,
) -> tuple[NanitNightLight, MagicMock]:
    control = ControlState(night_light=night_light, night_light_timeout=night_light_timeout)
    settings = SettingsState(
        volume=50,
        sleep_mode=False,
        night_vision=True,
        night_light_brightness=night_light_brightness,
    )
    state: CameraState | None = None
    if not data_is_none:
        state = CameraState(
            sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
            settings=settings,
            control=control,
            connection=ConnectionInfo(state=ConnectionState.CONNECTED),
        )
    coordinator = _push_coordinator(state)
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    camera.async_set_settings = AsyncMock()
    entity = NanitNightLight(coordinator, camera)
    _disable_state_writes(entity)
    return entity, camera


def test_night_light_is_on_when_state_on() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.ON)
    assert entity.is_on is True


def test_night_light_is_on_false_when_state_off() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.OFF)
    assert entity.is_on is False


def test_night_light_is_on_none_when_no_data() -> None:
    entity, _ = _night_light_entity(data_is_none=True)
    assert entity.is_on is None


def test_night_light_is_on_none_when_control_night_light_is_none() -> None:
    entity, _ = _night_light_entity(night_light=None)
    assert entity.is_on is None


def test_night_light_brightness_from_state() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.ON, night_light_brightness=50)
    assert entity.brightness is not None
    assert entity.brightness == value_to_brightness(_BRIGHTNESS_SCALE, 50)


def test_night_light_brightness_none_when_no_data() -> None:
    entity, _ = _night_light_entity(data_is_none=True)
    assert entity.brightness is None


def test_night_light_brightness_none_when_zero() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.OFF, night_light_brightness=0)
    assert entity.brightness is None


def test_night_light_unique_id() -> None:
    entity, _ = _night_light_entity()
    assert entity.unique_id == "cam_1_night_light"


async def test_night_light_turn_on_calls_camera() -> None:
    entity, camera = _night_light_entity(night_light=NightLightState.OFF)

    await entity.async_turn_on()

    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.ON)
    assert entity.is_on is True


async def test_night_light_turn_on_with_brightness_calls_settings_and_control() -> None:
    entity, camera = _night_light_entity(night_light=NightLightState.OFF)

    await entity.async_turn_on(**{ATTR_BRIGHTNESS: 128})

    expected_device_val = int(brightness_to_value(_BRIGHTNESS_SCALE, 128))
    camera.async_set_settings.assert_awaited_once_with(
        night_light_brightness=expected_device_val,
    )
    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.ON)
    assert entity.is_on is True
    assert entity.brightness == 128


async def test_night_light_turn_on_reverts_on_error() -> None:
    entity, camera = _night_light_entity(night_light=NightLightState.OFF)
    camera.async_set_control.side_effect = RuntimeError("fail")

    with pytest.raises(RuntimeError, match="fail"):
        await entity.async_turn_on()

    assert entity.is_on is False


async def test_night_light_turn_off_calls_camera() -> None:
    entity, camera = _night_light_entity(night_light=NightLightState.ON)

    await entity.async_turn_off()

    camera.async_set_control.assert_awaited_once_with(night_light=NightLightState.OFF)
    assert entity.is_on is False


async def test_night_light_turn_off_reverts_on_error() -> None:
    entity, camera = _night_light_entity(night_light=NightLightState.ON)
    camera.async_set_control.side_effect = RuntimeError("fail")

    with pytest.raises(RuntimeError, match="fail"):
        await entity.async_turn_off()

    assert entity.is_on is True


def test_night_light_handle_coordinator_update_syncs_on_state() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.OFF)

    new_state = CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(
            volume=50, sleep_mode=False, night_vision=True, night_light_brightness=60
        ),
        control=ControlState(night_light=NightLightState.ON),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    entity.coordinator.data = new_state
    entity._handle_coordinator_update()

    assert entity.is_on is True
    assert entity.brightness == value_to_brightness(_BRIGHTNESS_SCALE, 60)


def test_night_light_handle_coordinator_update_no_data() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.ON)

    entity.coordinator.data = None
    entity._handle_coordinator_update()

    assert entity.is_on is True


def test_night_light_handle_coordinator_update_grace_period_confirms() -> None:
    import time

    entity, _ = _night_light_entity(night_light=NightLightState.OFF)

    entity._command_is_on = True
    entity._command_ts = time.monotonic()
    entity._attr_is_on = True

    new_state = CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(volume=50, sleep_mode=False, night_vision=True),
        control=ControlState(night_light=NightLightState.ON),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    entity.coordinator.data = new_state
    entity._handle_coordinator_update()

    assert entity.is_on is True
    assert entity._command_is_on is None


def test_night_light_handle_coordinator_update_grace_period_stale() -> None:
    import time

    entity, _ = _night_light_entity(night_light=NightLightState.ON)

    entity._command_is_on = False
    entity._command_ts = time.monotonic()
    entity._attr_is_on = False

    new_state = CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(volume=50, sleep_mode=False, night_vision=True),
        control=ControlState(night_light=NightLightState.ON),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    entity.coordinator.data = new_state
    entity._handle_coordinator_update()

    assert entity.is_on is False
    assert entity._command_is_on is False


def test_night_light_handle_coordinator_update_grace_period_expired() -> None:
    import time

    entity, _ = _night_light_entity(night_light=NightLightState.OFF)

    entity._command_is_on = True
    entity._command_ts = time.monotonic() - _NL_GRACE - 1

    new_state = CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(
            volume=50, sleep_mode=False, night_vision=True, night_light_brightness=80
        ),
        control=ControlState(night_light=NightLightState.ON),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    entity.coordinator.data = new_state
    entity._handle_coordinator_update()

    assert entity.is_on is True
    assert entity._command_is_on is None


def test_night_light_handle_coordinator_update_brightness_grace_confirms() -> None:
    import time

    entity, _ = _night_light_entity(
        night_light=NightLightState.ON,
        night_light_brightness=30,
    )

    target_ha = 128
    entity._command_brightness = target_ha
    entity._command_ts = time.monotonic()
    entity._attr_brightness = target_ha

    expected_device = int(brightness_to_value(_BRIGHTNESS_SCALE, target_ha))
    new_state = CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(
            volume=50,
            sleep_mode=False,
            night_vision=True,
            night_light_brightness=expected_device,
        ),
        control=ControlState(night_light=NightLightState.ON),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )
    entity.coordinator.data = new_state
    entity._handle_coordinator_update()

    assert entity._command_brightness is None
    assert entity.brightness == value_to_brightness(_BRIGHTNESS_SCALE, expected_device)


async def test_night_light_async_added_to_hass_restores_state() -> None:
    entity, _ = _night_light_entity(data_is_none=True)

    assert entity.is_on is None

    last_state = MagicMock()
    last_state.state = "on"
    last_state.attributes = {ATTR_BRIGHTNESS: 180}
    entity.async_get_last_state = AsyncMock(return_value=last_state)
    entity.async_on_remove = MagicMock()

    with patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass"):
        await entity.async_added_to_hass()

    assert entity.is_on is True
    assert entity.brightness == 180


async def test_night_light_async_added_to_hass_skips_restore_when_data_present() -> None:
    entity, _ = _night_light_entity(night_light=NightLightState.ON, night_light_brightness=50)

    entity.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))
    entity.async_on_remove = MagicMock()

    with patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass"):
        await entity.async_added_to_hass()

    assert entity.is_on is True


# ---------------------------------------------------------------------------
# NanitNightLightTimer — camera night light timer entity
# ---------------------------------------------------------------------------


def _night_light_timer_entity(
    *, night_light_timeout: int | None = None, data_is_none: bool = False
) -> tuple[NanitNightLightTimer, MagicMock]:
    control = ControlState(night_light_timeout=night_light_timeout)
    state: CameraState | None = None
    if not data_is_none:
        state = CameraState(
            sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
            settings=SettingsState(volume=50, sleep_mode=False, night_vision=True),
            control=control,
            connection=ConnectionInfo(state=ConnectionState.CONNECTED),
        )
    coordinator = _push_coordinator(state)
    camera = MagicMock(uid="cam_1")
    camera.async_set_control = AsyncMock()
    entity = NanitNightLightTimer(coordinator, camera)
    _disable_state_writes(entity)
    return entity, camera


def test_night_light_timer_unique_id() -> None:
    entity, _ = _night_light_timer_entity()
    assert entity.unique_id == "cam_1_night_light_timer"


def test_night_light_timer_current_option_none_when_no_data() -> None:
    entity, _ = _night_light_timer_entity(data_is_none=True)
    assert entity.current_option is None


def test_night_light_timer_current_option_off_when_timeout_none() -> None:
    entity, _ = _night_light_timer_entity(night_light_timeout=None)
    assert entity.current_option == "off"


def test_night_light_timer_current_option_off_when_zero() -> None:
    entity, _ = _night_light_timer_entity(night_light_timeout=0)
    assert entity.current_option == "off"


def test_night_light_timer_current_option_15_minutes() -> None:
    entity, _ = _night_light_timer_entity(night_light_timeout=900)
    assert entity.current_option == "15_minutes"


def test_night_light_timer_current_option_1_hour() -> None:
    entity, _ = _night_light_timer_entity(night_light_timeout=3600)
    assert entity.current_option == "1_hour"


def test_night_light_timer_current_option_unknown_defaults_off() -> None:
    entity, _ = _night_light_timer_entity(night_light_timeout=9999)
    assert entity.current_option == "off"


async def test_night_light_timer_select_option_calls_camera() -> None:
    entity, camera = _night_light_timer_entity()

    await entity.async_select_option("30_minutes")

    camera.async_set_control.assert_awaited_once_with(night_light_timeout=1800)


async def test_night_light_timer_select_option_off() -> None:
    entity, camera = _night_light_timer_entity(night_light_timeout=900)

    await entity.async_select_option("off")

    camera.async_set_control.assert_awaited_once_with(night_light_timeout=0)


async def test_night_light_timer_select_invalid_option_raises() -> None:
    from homeassistant.exceptions import ServiceValidationError

    entity, _ = _night_light_timer_entity()

    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("invalid_option")


def test_night_light_timer_options_list() -> None:
    entity, _ = _night_light_timer_entity()
    assert entity.options == ["off", "15_minutes", "30_minutes", "1_hour", "2_hours", "4_hours"]
