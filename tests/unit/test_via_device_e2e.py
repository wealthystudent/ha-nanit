"""End-to-end device linking through the real entity platforms.

Sets up a whole config entry with hass.config_entries (no patched
async_forward_entry_setups), so every platform adds its entities and HA's
own entity platform builds the device registry from their DeviceInfo.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from aionanit.exceptions import NanitConnectionError
from aionanit.models import (
    Baby,
    CameraState,
    ConnectionInfo,
    ConnectionState,
    ControlState,
    SensorState,
    SettingsState,
)
from custom_components.nanit.aionanit_sl.models import SoundLightFullState
from custom_components.nanit.const import DOMAIN

from .conftest import MOCK_EMAIL, mock_entry_data_v2

_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

BABY = Baby(uid="baby_4", name="Nursery", camera_uid="cam_4", speaker_uid="spk_4")
DEPRECATION = "Detected that custom integration 'nanit' calls"


def _camera_state() -> CameraState:
    return CameraState(
        sensors=SensorState(temperature=22.5, humidity=50.0, light=100),
        settings=SettingsState(volume=50, sleep_mode=False, night_vision=True),
        control=ControlState(),
        connection=ConnectionInfo(state=ConnectionState.CONNECTED),
    )


def _make_camera(uid: str, baby_uid: str, *, fail: bool) -> MagicMock:
    cam = MagicMock()
    cam.uid = uid
    cam.baby_uid = baby_uid
    cam.connected = True
    cam.state = _camera_state()
    cam.subscribe = MagicMock(return_value=lambda: None)
    cam.async_start = AsyncMock(
        side_effect=NanitConnectionError("camera unreachable") if fail else None
    )
    cam.async_stop = AsyncMock()
    return cam


def _make_sound_light(speaker_uid: str, **_kw: object) -> MagicMock:
    sl = MagicMock()
    sl.speaker_uid = speaker_uid
    sl.connected = True
    sl.connection_mode = "local"
    sl.state = SoundLightFullState(power_on=True, volume=0.5, brightness=0.5)
    sl.subscribe = MagicMock(return_value=lambda: None)
    sl.async_start = AsyncMock()
    sl.async_stop = AsyncMock()
    return sl


def _side_coordinator(args: tuple, *, data: object) -> MagicMock:
    baby = next(a for a in args if isinstance(a, Baby))
    return MagicMock(
        async_config_entry_first_refresh=AsyncMock(),
        data=data,
        baby=baby,
        last_update_success=True,
    )


@pytest.fixture
def camera_fails() -> list[bool]:
    return [False]


@pytest.fixture
def nanit_env(mock_nanit_client, camera_fails):
    mock_nanit_client.async_get_babies.return_value = [BABY]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_camera(
        kw["uid"], kw["baby_uid"], fail=camera_fails[0]
    )
    with (
        patch(
            "custom_components.nanit.hub.NanitCloudCoordinator",
            side_effect=lambda *a, **_k: _side_coordinator(a, data=[]),
        ),
        patch(
            "custom_components.nanit.hub.NanitNetworkCoordinator",
            side_effect=lambda *a, **_k: _side_coordinator(a, data=None),
        ),
        patch(
            "custom_components.nanit.hub.NanitSoundLight",
            side_effect=lambda **kw: _make_sound_light(**kw),
        ),
    ):
        yield mock_nanit_client


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, minor_version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    return entry


def _devices(hass: HomeAssistant, entry: MockConfigEntry):
    dev_reg = dr.async_get(hass)
    cam = dev_reg.async_get_device_by_identifier((DOMAIN, "cam_4"), entry.entry_id)
    spk = dev_reg.async_get_device_by_identifier((DOMAIN, "spk_4"), entry.entry_id)
    return cam, spk


def _speaker_entities(hass: HomeAssistant, entry: MockConfigEntry, device_id: str):
    return er.async_entries_for_device(er.async_get(hass), device_id)


def _assert_clean_log(caplog: pytest.LogCaptureFixture) -> None:
    text = caplog.text
    assert DEPRECATION not in text, text
    assert "DeviceInfoError" not in text, text
    assert "via_device" not in text, text
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


async def test_camera_and_speaker_link_by_device_id(
    hass: HomeAssistant, nanit_env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    cam, spk = _devices(hass, entry)
    assert cam is not None and spk is not None
    assert spk.via_device_id == cam.id
    assert _speaker_entities(hass, entry, spk.id), "speaker device has no entities"
    assert _speaker_entities(hass, entry, cam.id), "camera device has no entities"
    _assert_clean_log(caplog)

    # (b) reload keeps the link
    caplog.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    cam2, spk2 = _devices(hass, entry)
    assert cam2.id == cam.id and spk2.id == spk.id
    assert spk2.via_device_id == cam.id
    _assert_clean_log(caplog)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_camera_failure_speaker_loads_unlinked(
    hass: HomeAssistant, nanit_env, camera_fails, caplog: pytest.LogCaptureFixture
) -> None:
    """(a) Camera fails to connect: hub clears via_camera_uid, speaker still loads."""
    camera_fails[0] = True
    caplog.set_level(logging.WARNING)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    hub = entry.runtime_data.hub
    assert hub.camera_data == {}
    assert hub.speaker_data["spk_4"].coordinator.via_camera_uid is None

    cam, spk = _devices(hass, entry)
    assert cam is None
    assert spk is not None
    assert spk.via_device_id is None
    ents = _speaker_entities(hass, entry, spk.id)
    assert ents
    for ent in ents:
        state = hass.states.get(ent.entity_id)
        assert state is not None, ent.entity_id
    assert DEPRECATION not in caplog.text
    assert "DeviceInfoError" not in caplog.text
    assert "via_device" not in caplog.text

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("old_link", ["linked", "dangling_none"])
async def test_upgrade_from_via_device_identifier_form(
    hass: HomeAssistant, nanit_env, caplog: pytest.LogCaptureFixture, old_link: str
) -> None:
    """(c) Devices created by the old via_device=(DOMAIN, uid) code keep a correct link."""
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    # What the pre-fix code left behind: a camera device and a speaker device
    # created from DeviceInfo carrying via_device=(DOMAIN, camera_uid).
    old_cam = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "cam_4")},
        name="Nursery",
        manufacturer="Nanit",
    )
    # The old via_device=(DOMAIN, "cam_4") is resolved by the registry into
    # via_device_id at write time, so storing the id directly yields the same
    # persisted state (the identifier form itself raises outside an
    # integration frame in tests).
    kwargs: dict = {}
    if old_link == "linked":
        kwargs["via_device_id"] = old_cam.id
    old_spk = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "spk_4")},
        name="Nursery Sound & Light",
        manufacturer="Nanit",
        model="Sound & Light Machine",
        **kwargs,
    )
    assert old_spk.via_device_id == (old_cam.id if old_link == "linked" else None)

    caplog.clear()
    caplog.set_level(logging.WARNING)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    cam, spk = _devices(hass, entry)
    assert cam.id == old_cam.id
    assert spk.id == old_spk.id
    assert spk.via_device_id == old_cam.id
    _assert_clean_log(caplog)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
