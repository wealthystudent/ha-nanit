from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit import async_migrate_entry, async_setup_entry, async_unload_entry
from custom_components.nanit.const import (
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_CAMERA_UID,
    DOMAIN,
)

from .conftest import MOCK_BABY_1, MOCK_EMAIL, mock_entry_data_v1, mock_entry_data_v2

aionanit = importlib.import_module("aionanit")
NanitAuthError = aionanit.NanitAuthError
NanitConnectionError = aionanit.NanitConnectionError

_DEVICE_REGISTRY = "homeassistant.helpers.device_registry"
_ENTITY_REGISTRY = "homeassistant.helpers.entity_registry"


async def test_async_setup_entry_success(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=True),
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data.hub is not None
    assert len(entry.runtime_data.cameras) == 1
    assert MOCK_BABY_1.camera_uid in entry.runtime_data.cameras


async def test_async_setup_entry_auth_error_raises(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    mock_nanit_client.async_get_babies.side_effect = NanitAuthError("token expired")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_async_setup_entry_connection_error_raises(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    mock_nanit_client.async_get_babies.side_effect = NanitConnectionError("offline")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_async_unload_entry_success(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=True),
    ):
        assert await async_setup_entry(hass, entry)

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, entry)

    mock_nanit_client.async_close.assert_awaited_once()


async def test_stale_device_removed_when_camera_no_longer_on_account(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    mock_device_registry = MagicMock()
    stale_device = MagicMock()
    stale_device.identifiers = {(DOMAIN, "cam_stale")}
    stale_device.id = "device_stale"
    stale_device.name = "Stale Camera"

    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
        patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=mock_device_registry,
        ),
        patch(
            "homeassistant.helpers.device_registry.async_entries_for_config_entry",
            return_value=[stale_device],
        ),
    ):
        assert await async_setup_entry(hass, entry)

    mock_device_registry.async_remove_device.assert_called_once_with("device_stale")


async def test_active_device_not_removed(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    mock_device_registry = MagicMock()
    active_device = MagicMock()
    active_device.identifiers = {(DOMAIN, MOCK_BABY_1.camera_uid)}
    active_device.id = "device_active"
    active_device.name = "Active Camera"

    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
        patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=mock_device_registry,
        ),
        patch(
            "homeassistant.helpers.device_registry.async_entries_for_config_entry",
            return_value=[active_device],
        ),
    ):
        assert await async_setup_entry(hass, entry)

    mock_device_registry.async_remove_device.assert_not_called()


async def test_deprecated_switch_entity_removed_on_setup(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    mock_ent_reg = MagicMock()
    mock_ent_reg.async_get_entity_id.return_value = "switch.nursery_night_light"

    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
        patch(f"{_ENTITY_REGISTRY}.async_get", return_value=mock_ent_reg),
    ):
        assert await async_setup_entry(hass, entry)

    mock_ent_reg.async_get_entity_id.assert_any_call(
        "switch", DOMAIN, f"{MOCK_BABY_1.camera_uid}_night_light"
    )
    mock_ent_reg.async_get_entity_id.assert_any_call(
        "number", DOMAIN, f"{MOCK_BABY_1.camera_uid}_volume"
    )
    assert mock_ent_reg.async_remove.call_count == 2


async def test_deprecated_entity_removal_skipped_when_not_present(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    mock_ent_reg = MagicMock()
    mock_ent_reg.async_get_entity_id.return_value = None

    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
        patch(f"{_ENTITY_REGISTRY}.async_get", return_value=mock_ent_reg),
    ):
        assert await async_setup_entry(hass, entry)

    mock_ent_reg.async_remove.assert_not_called()


# --- Migration tests ---


async def test_migrate_v1_to_v2_moves_camera_ip_to_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v1(), version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.options[CONF_CAMERA_IPS] == {"cam_1": "192.168.1.10"}
    assert CONF_CAMERA_IP not in entry.data


async def test_migrate_v1_to_v2_removes_baby_camera_fields(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v1(), version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert CONF_BABY_UID not in entry.data
    assert CONF_CAMERA_UID not in entry.data
    assert CONF_BABY_NAME not in entry.data


async def test_migrate_v1_to_v2_updates_unique_id_to_email(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v1(), version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.unique_id == MOCK_EMAIL


async def test_migrate_v1_to_v2_preserves_unique_id_without_email(
    hass: HomeAssistant,
) -> None:
    data = mock_entry_data_v1()
    data.pop("email")
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.unique_id == "cam_1"


async def test_migrate_version_gt_2_returns_false(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=3, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)
