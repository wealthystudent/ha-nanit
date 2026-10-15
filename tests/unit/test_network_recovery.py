"""Tests for reloading the entry when a camera that failed setup comes back online."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit.const import DOMAIN
from custom_components.nanit.hub import NanitHub

from .conftest import MOCK_EMAIL, mock_entry_data_v2

Baby = importlib.import_module("aionanit.models").Baby


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)
    return entry


def _make_hub(hass: HomeAssistant, entry: MockConfigEntry, failed: set[str]) -> NanitHub:
    hub = NanitHub(hass, MagicMock(), entry)
    hub.failed_camera_uids.update(failed)
    return hub


async def test_recovered_camera_schedules_reload_once(
    hass: HomeAssistant, mock_nanit_client: MagicMock
) -> None:
    """A recovered camera triggers one reload, so repeat checks don't bounce the entry."""
    baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", camera_connected=True)
    entry = _make_entry(hass)
    hub = _make_hub(hass, entry, {"cam_1"})

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        hub._async_reload_recovered_cameras([baby])
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1

        # Still failed and cloud-connected on the next check (the reload
        # didn't fix it): no second reload this HA run.
        hub._async_reload_recovered_cameras([baby])
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_reload_marker_survives_the_reload(
    hass: HomeAssistant, mock_nanit_client: MagicMock
) -> None:
    """The hub built by the reload shares the attempted-reload marker."""
    baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", camera_connected=True)
    entry = _make_entry(hass)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        _make_hub(hass, entry, {"cam_1"})._async_reload_recovered_cameras([baby])
        _make_hub(hass, entry, {"cam_1"})._async_reload_recovered_cameras([baby])
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_recovery_check_handles_null_baby_name(
    hass: HomeAssistant, mock_nanit_client: MagicMock
) -> None:
    """A recovered camera with a null name must not crash the check."""
    baby = SimpleNamespace(
        uid="baby_1",
        name=None,
        camera_uid="cam_1",
        network=None,
        camera_connected=True,
    )
    entry = _make_entry(hass)
    hub = _make_hub(hass, entry, {"cam_1"})

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        hub._async_reload_recovered_cameras([baby])
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_recovery_check_tolerates_legacy_baby_without_connected_field(
    hass: HomeAssistant, mock_nanit_client: MagicMock
) -> None:
    """A Baby from an aionanit wheel without camera_connected must not crash the check."""
    baby = SimpleNamespace(uid="baby_1", name="Nursery", camera_uid="cam_1", network=None)
    entry = _make_entry(hass)
    hub = _make_hub(hass, entry, {"cam_1"})

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        hub._async_reload_recovered_cameras([baby])
        await hass.async_block_till_done()

    mock_reload.assert_not_awaited()
