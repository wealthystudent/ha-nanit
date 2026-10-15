"""Read-only mode: observe-only setup with no control over device settings."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Camera imports pull in turbojpeg; stub it like test_entities.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit import async_setup_entry, async_unload_entry
from custom_components.nanit.binary_sensor import NanitCameraPowerSensor
from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import (
    CONF_CAMERA_IPS,
    CONF_READ_ONLY,
    CONF_SPEAKER_IP,
    CONF_SPEAKER_IPS,
    DOMAIN,
    PLATFORMS,
    READ_ONLY_PLATFORMS,
)
from custom_components.nanit.entity import camera_power_on
from custom_components.nanit.switch import SWITCHES

from .conftest import MOCK_BABY_1, MOCK_EMAIL, MOCK_PASSWORD, mock_entry_data_v2
from .test_via_device_e2e import camera_fails, nanit_env  # noqa: F401  (shared fixtures)

Baby = importlib.import_module("aionanit.models").Baby

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]


def _entry(*, read_only: bool) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options={CONF_READ_ONLY: True} if read_only else {},
        version=2,
        unique_id=MOCK_EMAIL,
    )


def _camera_entity(*, read_only: bool) -> tuple[NanitCameraEntity, MagicMock]:
    coordinator = MagicMock()
    coordinator.data = None
    coordinator.camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    coordinator.baby = MOCK_BABY_1
    camera = MagicMock(uid="cam_1")
    camera.async_set_settings = AsyncMock()
    camera.async_stop_streaming = AsyncMock()
    return NanitCameraEntity(coordinator, camera, read_only=read_only), camera


# ---------------------------------------------------------------------------
# Setup / unload
# ---------------------------------------------------------------------------


async def test_read_only_setup_forwards_only_observe_platforms(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = _entry(read_only=True)
    entry.add_to_hass(hass)

    forward = AsyncMock(return_value=True)
    register_card = AsyncMock()
    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
        patch("custom_components.nanit.async_register_card", register_card),
    ):
        assert await async_setup_entry(hass, entry)

    forward.assert_awaited_once_with(entry, READ_ONLY_PLATFORMS)
    assert entry.runtime_data.platforms == READ_ONLY_PLATFORMS
    # Still registered: an existing card must keep loading, and its controls
    # have nothing left to act on.
    register_card.assert_awaited_once()


async def test_default_setup_forwards_all_platforms_and_registers_card(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = _entry(read_only=False)
    entry.add_to_hass(hass)

    forward = AsyncMock(return_value=True)
    register_card = AsyncMock()
    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
        patch("custom_components.nanit.async_register_card", register_card),
    ):
        assert await async_setup_entry(hass, entry)

    forward.assert_awaited_once_with(entry, PLATFORMS)
    register_card.assert_awaited_once()


async def test_unload_uses_the_platforms_that_were_set_up(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    """Turning read-only on changes the options before the reload unloads."""
    entry = _entry(read_only=False)
    entry.add_to_hass(hass)

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)

    # The options listener would schedule a real reload; this test drives the
    # unload half of that reload itself.
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        hass.config_entries.async_update_entry(entry, options={CONF_READ_ONLY: True})
        await hass.async_block_till_done()

    unload = AsyncMock(return_value=True)
    with patch.object(hass.config_entries, "async_unload_platforms", unload):
        assert await async_unload_entry(hass, entry)

    unload.assert_awaited_once_with(entry, PLATFORMS)


async def test_read_only_removes_control_entities_and_keeps_observe_ones(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = _entry(read_only=True)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    uid = MOCK_BABY_1.camera_uid
    kept = [
        ent_reg.async_get_or_create("camera", DOMAIN, f"{uid}_camera", config_entry=entry),
        ent_reg.async_get_or_create("sensor", DOMAIN, f"{uid}_temperature", config_entry=entry),
        ent_reg.async_get_or_create("binary_sensor", DOMAIN, f"{uid}_motion", config_entry=entry),
    ]
    removed = [
        ent_reg.async_get_or_create("switch", DOMAIN, f"{uid}_camera_power", config_entry=entry),
        ent_reg.async_get_or_create("light", DOMAIN, f"{uid}_night_light", config_entry=entry),
        ent_reg.async_get_or_create("number", DOMAIN, f"{uid}_volume", config_entry=entry),
        ent_reg.async_get_or_create("select", DOMAIN, f"{uid}_sound", config_entry=entry),
        ent_reg.async_get_or_create("media_player", DOMAIN, f"{uid}_speaker", config_entry=entry),
    ]

    # Entities of other config entries must never be swept: another
    # integration, and another Nanit account that is not read-only.
    other_integration = MockConfigEntry(domain="hue")
    other_integration.add_to_hass(hass)
    other_account = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v2(), unique_id="other@x")
    other_account.add_to_hass(hass)
    foreign = [
        ent_reg.async_get_or_create("light", "hue", "bulb_1", config_entry=other_integration),
        ent_reg.async_get_or_create(
            "switch", DOMAIN, "other_cam_camera_power", config_entry=other_account
        ),
    ]

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)

    for reg_entry in kept + foreign:
        assert ent_reg.async_get(reg_entry.entity_id) is not None
    for reg_entry in removed:
        assert ent_reg.async_get(reg_entry.entity_id) is None


async def test_default_setup_keeps_control_entities(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = _entry(read_only=False)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    switch = ent_reg.async_get_or_create(
        "switch", DOMAIN, f"{MOCK_BABY_1.camera_uid}_camera_power", config_entry=entry
    )

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)

    assert ent_reg.async_get(switch.entity_id) is not None


async def test_default_setup_removes_the_read_only_power_sensor(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    """Turning read-only off: the stand-in sensor goes, the power switch stays."""
    entry = _entry(read_only=False)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    uid = MOCK_BABY_1.camera_uid
    sensor = ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{uid}_camera_power", config_entry=entry
    )
    kept = [
        ent_reg.async_get_or_create("switch", DOMAIN, f"{uid}_camera_power", config_entry=entry),
        ent_reg.async_get_or_create("binary_sensor", DOMAIN, f"{uid}_motion", config_entry=entry),
    ]
    # Another Nanit account that is still read-only keeps its sensor.
    other_account = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options={CONF_READ_ONLY: True},
        unique_id="other@x",
    )
    other_account.add_to_hass(hass)
    # Another integration's binary sensor, even with the same unique ID, is never swept.
    other_integration = MockConfigEntry(domain="hue")
    other_integration.add_to_hass(hass)
    foreign = [
        ent_reg.async_get_or_create(
            "binary_sensor", DOMAIN, "other_cam_camera_power", config_entry=other_account
        ),
        ent_reg.async_get_or_create(
            "binary_sensor", "hue", f"{uid}_camera_power", config_entry=other_integration
        ),
    ]

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)

    assert ent_reg.async_get(sensor.entity_id) is None
    for reg_entry in [*kept, *foreign]:
        assert ent_reg.async_get(reg_entry.entity_id) is not None


async def test_read_only_setup_keeps_the_power_sensor(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = _entry(read_only=True)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    sensor = ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{MOCK_BABY_1.camera_uid}_camera_power", config_entry=entry
    )

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)

    assert ent_reg.async_get(sensor.entity_id) is not None


def test_read_only_platforms_cannot_control_a_device() -> None:
    assert set(READ_ONLY_PLATFORMS) == {Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA}
    assert set(READ_ONLY_PLATFORMS) < set(PLATFORMS)


# ---------------------------------------------------------------------------
# Camera entity
# ---------------------------------------------------------------------------


def test_read_only_camera_has_no_on_off() -> None:
    entity, _camera = _camera_entity(read_only=True)
    assert entity.supported_features == CameraEntityFeature.STREAM


def test_default_camera_keeps_on_off() -> None:
    entity, _camera = _camera_entity(read_only=False)
    assert entity.supported_features & CameraEntityFeature.ON_OFF


@pytest.mark.parametrize("method", ["async_turn_on", "async_turn_off"])
async def test_read_only_camera_refuses_power_changes(method: str) -> None:
    entity, camera = _camera_entity(read_only=True)

    with pytest.raises(ServiceValidationError) as err:
        await getattr(entity, method)()

    assert err.value.translation_key == "read_only"
    camera.async_set_settings.assert_not_awaited()
    camera.async_stop_streaming.assert_not_awaited()


async def test_default_camera_turn_off_still_sleeps() -> None:
    entity, camera = _camera_entity(read_only=False)
    with patch.object(entity, "_invalidate_stream"):
        await entity.async_turn_off()
    camera.async_set_settings.assert_awaited_once_with(sleep_mode=True)


# ---------------------------------------------------------------------------
# Camera power sensor (stands in for the power switch)
# ---------------------------------------------------------------------------


def _state(sleep_mode: bool | None) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(sleep_mode=sleep_mode))


def _power_sensor(sleep_mode: bool | None = None, *, data: bool = True) -> NanitCameraPowerSensor:
    coordinator = MagicMock()
    coordinator.data = _state(sleep_mode) if data else None
    coordinator.camera = MagicMock(uid="cam_1", baby_uid="baby_1")
    coordinator.baby = MOCK_BABY_1
    return NanitCameraPowerSensor(coordinator)


def test_power_sensor_uses_the_switch_value() -> None:
    """One function decides both, so the sensor means exactly what the switch means."""
    assert SWITCHES[0].key == "camera_power"
    assert SWITCHES[0].value_fn is camera_power_on


@pytest.mark.parametrize(("sleep_mode", "expected"), [(False, True), (True, False), (None, None)])
def test_power_sensor_state(sleep_mode: bool | None, expected: bool | None) -> None:
    sensor = _power_sensor(sleep_mode)
    assert sensor.is_on is expected
    assert sensor.unique_id == "cam_1_camera_power"
    assert sensor.device_class == "power"


def test_power_sensor_follows_updates_and_keeps_the_last_known_state() -> None:
    sensor = _power_sensor(False)
    with patch.object(sensor, "async_write_ha_state"):
        sensor.coordinator.data = _state(True)
        sensor._handle_coordinator_update()
        assert sensor.is_on is False

        # The camera reports no sleep_mode: keep the last known state, as the switch does.
        sensor.coordinator.data = _state(None)
        sensor._handle_coordinator_update()
        assert sensor.is_on is False

        sensor.coordinator.data = _state(False)
        sensor._handle_coordinator_update()
        assert sensor.is_on is True


@pytest.mark.parametrize(
    ("last", "expected"), [(STATE_ON, True), (STATE_OFF, False), ("unavailable", None)]
)
async def test_power_sensor_restores_only_on_or_off(last: str, expected: bool | None) -> None:
    sensor = _power_sensor(data=False)
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", AsyncMock()),
        patch.object(
            sensor,
            "async_get_last_state",
            AsyncMock(return_value=State("binary_sensor.x_camera_power", last)),
        ),
    ):
        await sensor.async_added_to_hass()
    assert sensor.is_on is expected


async def test_power_sensor_prefers_live_data_over_the_restored_state() -> None:
    sensor = _power_sensor(False)
    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", AsyncMock()),
        patch.object(
            sensor,
            "async_get_last_state",
            AsyncMock(return_value=State("binary_sensor.x_camera_power", STATE_OFF)),
        ),
    ):
        await sensor.async_added_to_hass()
    assert sensor.is_on is True


# ---------------------------------------------------------------------------
# Dashboard card: "Camera Off" from the power sensor
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]


def test_card_falls_back_to_the_power_sensor() -> None:
    utils = (_ROOT / "frontend" / "src" / "utils.ts").read_text(encoding="utf-8")
    card = (_ROOT / "frontend" / "src" / "nanit-card.ts").read_text(encoding="utf-8")
    bundle = (_ROOT / "custom_components" / "nanit" / "frontend" / "nanit-card.js").read_text(
        encoding="utf-8"
    )

    # the condition itself, so swapping it for `false` fails here
    assert (
        '} else if (deviceClass === "power" || suffix.endsWith("_camera_power")) {\n'
        "        result.power_state = eid;"
    ) in utils
    assert "entities.power ?? entities.power_state" in card
    # the power button still needs the switch: read-only mode shows no button
    assert "const showPowerButton = entities.power &&" in card
    assert "power_state" in bundle


# ---------------------------------------------------------------------------
# Config and options flow
# ---------------------------------------------------------------------------


def _as_dict(result: Any) -> dict[str, Any]:
    return cast(dict[str, Any], result)


@pytest.mark.parametrize(("read_only", "expected"), [(True, {CONF_READ_ONLY: True}), (False, {})])
async def test_setup_flow_stores_read_only_as_an_option(
    hass: HomeAssistant,
    mock_config_flow_client,
    read_only: bool,
    expected: dict[str, Any],
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: MOCK_EMAIL, CONF_PASSWORD: MOCK_PASSWORD, CONF_READ_ONLY: read_only},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["result"].options == expected
    assert CONF_READ_ONLY not in result_data["data"]


def _options_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1], speaker_uid_map={})
    )
    entry.add_to_hass(hass)
    return entry


async def _open(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> Any:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    assert set(result["menu_options"]) == {"device", "read_only"}
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_options_menu_turns_read_only_on(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_CAMERA_IPS: {"cam": "192.168.1.25"}})

    result = await _open(hass, entry, "read_only")
    assert result.get("step_id") == "read_only"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_READ_ONLY: True}
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["data"][CONF_READ_ONLY] is True
    # other options are merged, not replaced
    assert result_data["data"][CONF_CAMERA_IPS] == {"cam": "192.168.1.25"}


async def test_options_menu_reaches_read_only_without_devices(hass: HomeAssistant) -> None:
    """The device branch aborts with no devices, but read-only stays reachable."""
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(babies=[], speaker_uid_map={}))
    entry.add_to_hass(hass)

    result = await _open(hass, entry, "read_only")
    assert result.get("step_id") == "read_only"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_READ_ONLY: True}
    )

    assert _as_dict(result)["data"] == {CONF_READ_ONLY: True}


async def test_options_menu_turns_read_only_off(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_CAMERA_IPS: {}, CONF_READ_ONLY: True})

    result = await _open(hass, entry, "read_only")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_READ_ONLY: False}
    )

    assert CONF_READ_ONLY not in _as_dict(result)["data"]


async def test_options_ip_edit_keeps_read_only_on(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_READ_ONLY: True})
    baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", speaker_uid="spk_1")
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[baby], speaker_uid_map={"baby_1": "spk_1"})
    )

    result = await _open(hass, entry, "device")
    assert result.get("step_id") == "camera_ip"
    assert CONF_READ_ONLY not in {str(k) for k in _as_dict(result)["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SPEAKER_IP: "192.168.1.25"}
    )

    result_data = _as_dict(result)
    assert result_data["data"][CONF_READ_ONLY] is True
    assert result_data["data"][CONF_SPEAKER_IPS] == {"spk_1": "192.168.1.25"}


# ---------------------------------------------------------------------------
# End to end through hass.config_entries (real platforms, real registry)
# ---------------------------------------------------------------------------


async def test_read_only_toggle_end_to_end(hass: HomeAssistant, nanit_env) -> None:  # noqa: F811
    """On, check the camera and the registry, then off: everything comes back."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, minor_version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    observe = {platform.value for platform in READ_ONLY_PLATFORMS}

    def ours() -> dict[str, er.RegistryEntry]:
        return {e.entity_id: e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}

    def power_sensor() -> str | None:
        return next(
            (
                eid
                for eid, e in ours().items()
                if e.domain == "binary_sensor" and e.unique_id.endswith("_camera_power")
            ),
            None,
        )

    before = ours()
    control = {eid for eid, e in before.items() if e.domain not in observe}
    assert control, "a full setup creates control entities"
    assert power_sensor() is None, "the power sensor is read-only mode only"
    power_switch = next(
        eid
        for eid, e in before.items()
        if e.domain == "switch" and e.unique_id.endswith("_camera_power")
    )
    switch_state = hass.states.get(power_switch).state
    assert switch_state == STATE_ON
    camera_id = next(eid for eid, e in before.items() if e.domain == "camera")
    features = hass.states.get(camera_id).attributes["supported_features"]
    assert features & CameraEntityFeature.ON_OFF

    hass.config_entries.async_update_entry(entry, options={CONF_READ_ONLY: True})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not control & set(ours())
    sensor_id = power_sensor()
    assert sensor_id is not None
    assert hass.states.get(sensor_id).state == switch_state, "same meaning as the switch"
    features = hass.states.get(camera_id).attributes["supported_features"]
    assert features == CameraEntityFeature.STREAM
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "camera", "turn_off", {"entity_id": camera_id}, blocking=True
        )

    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert control <= set(ours()), "the same entity IDs come back"
    assert power_sensor() is None, "the power sensor goes when the switch is back"
    features = hass.states.get(camera_id).attributes["supported_features"]
    assert features & CameraEntityFeature.ON_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
