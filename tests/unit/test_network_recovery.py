"""Tests for offline-camera recovery via the network coordinator poll."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import NanitNetworkCoordinator

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


def _make_hub(babies: list[object], failed: set[str]) -> MagicMock:
    hub = MagicMock()
    hub.failed_camera_uids = failed
    hub.client.token_manager.async_get_access_token = AsyncMock(return_value="token")
    hub.client.rest_client.async_get_babies = AsyncMock(return_value=babies)
    return hub


async def test_recovered_camera_schedules_reload_once(hass: HomeAssistant) -> None:
    """A recovered camera triggers one reload — repeat polls must not bounce the entry."""
    baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", camera_connected=True)
    entry = _make_entry(hass)
    hub = _make_hub([baby], {"cam_1"})
    coordinator = NanitNetworkCoordinator(hass, entry, hub, baby)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await coordinator._async_update_data()
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1

        # Camera still listed as failed and cloud-connected on the next poll
        # (reload didn't fix it) — no second reload this HA run.
        await coordinator._async_update_data()
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_recovery_check_dedupes_across_coordinators(hass: HomeAssistant) -> None:
    """Per-camera coordinators share the attempted-reload marker."""
    baby_1 = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", camera_connected=True)
    baby_2 = Baby(uid="baby_2", name="Bedroom", camera_uid="cam_2", camera_connected=True)
    entry = _make_entry(hass)
    hub = _make_hub([baby_1, baby_2], {"cam_1"})
    coordinator_1 = NanitNetworkCoordinator(hass, entry, hub, baby_1)
    coordinator_2 = NanitNetworkCoordinator(hass, entry, hub, baby_2)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await coordinator_1._async_update_data()
        await coordinator_2._async_update_data()
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_recovery_check_handles_null_baby_name(hass: HomeAssistant) -> None:
    """A recovered camera with a null name must not crash the poll."""
    baby = SimpleNamespace(
        uid="baby_1",
        name=None,
        camera_uid="cam_1",
        network=None,
        camera_connected=True,
    )
    entry = _make_entry(hass)
    hub = _make_hub([baby], {"cam_1"})
    coordinator = NanitNetworkCoordinator(hass, entry, hub, baby)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        await coordinator._async_update_data()
        await hass.async_block_till_done()
        assert mock_reload.await_count == 1


async def test_recovery_check_tolerates_legacy_baby_without_connected_field(
    hass: HomeAssistant,
) -> None:
    """A Baby from an aionanit wheel without camera_connected must not crash the poll."""
    baby = SimpleNamespace(uid="baby_1", name="Nursery", camera_uid="cam_1", network=None)
    entry = _make_entry(hass)
    hub = _make_hub([baby], {"cam_1"})
    coordinator = NanitNetworkCoordinator(hass, entry, hub, baby)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as mock_reload:
        result = await coordinator._async_update_data()
        await hass.async_block_till_done()

    assert result is None
    mock_reload.assert_not_awaited()
