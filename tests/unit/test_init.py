from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ACCESS_TOKEN, EVENT_HOMEASSISTANT_STOP
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
    CONF_SPEAKER_IPS,
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


async def test_async_setup_entry_cancelled_still_closes_hub(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    """HA cancelling setup must still stop whatever the hub already started.

    CancelledError is not an Exception, so the generic cleanup handler used
    to miss it and leak live camera and speaker connections past the
    cancelled setup.
    """
    mock_nanit_client.async_get_babies.side_effect = asyncio.CancelledError()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(asyncio.CancelledError):
        await async_setup_entry(hass, entry)

    mock_nanit_client.async_close.assert_awaited_once()


async def test_close_failure_does_not_mask_setup_error(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    """A close-time error must not replace the exception setup failed with."""
    mock_nanit_client.async_get_babies.side_effect = NanitAuthError("token expired")
    mock_nanit_client.async_close.side_effect = RuntimeError("close boom")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
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


async def test_hub_closed_when_home_assistant_stops(
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

    mock_nanit_client.async_close.assert_not_awaited()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    mock_nanit_client.async_close.assert_awaited_once()


async def _setup_loaded_entry(hass: HomeAssistant) -> MockConfigEntry:
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
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_push_coordinators_shut_down_before_hub_on_stop(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = await _setup_loaded_entry(hass)
    cameras = list(entry.runtime_data.cameras.values())
    assert cameras

    order: list[str] = []
    mock_nanit_client.async_close.side_effect = lambda: order.append("hub")
    with patch(
        "custom_components.nanit.coordinator.NanitPushCoordinator.async_shutdown",
        AsyncMock(side_effect=lambda: order.append("coordinator")),
    ):
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    assert order == ["coordinator"] * len(cameras) + ["hub"]


async def test_unload_after_stop_is_clean(
    hass: HomeAssistant,
    mock_nanit_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await _setup_loaded_entry(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert "Unable to remove unknown job listener" not in caplog.text


async def test_stop_after_unload_does_not_close_again(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    entry = await _setup_loaded_entry(hass)
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)
    mock_nanit_client.async_close.assert_awaited_once()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    mock_nanit_client.async_close.assert_awaited_once()


async def test_camera_devices_registered_before_platforms(
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
    dev_reg = dr.async_get(hass)
    seen_at_forward: list[bool] = []

    async def _forward(*_args: object) -> bool:
        seen_at_forward.append(
            dev_reg.async_get_device_by_identifier((DOMAIN, MOCK_BABY_1.camera_uid), entry.entry_id)
            is not None
        )
        return True

    with patch.object(hass.config_entries, "async_forward_entry_setups", _forward):
        assert await async_setup_entry(hass, entry)

    # Sound & Light devices link to the camera by registry id, which HA
    # rejects unless the camera device exists when the platforms run.
    assert seen_at_forward == [True]


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

    mock_device_registry.async_update_device.assert_called_once_with(
        "device_stale", remove_config_entry_id=entry.entry_id
    )


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

    mock_device_registry.async_update_device.assert_not_called()


async def test_no_devices_removed_when_babies_list_empty(
    hass: HomeAssistant,
    mock_nanit_client,
) -> None:
    """If the API transiently returns no babies, don't wipe existing HA devices."""
    mock_nanit_client.async_get_babies.return_value = []

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    mock_device_registry = MagicMock()
    existing_device = MagicMock()
    existing_device.identifiers = {(DOMAIN, MOCK_BABY_1.camera_uid)}
    existing_device.id = "device_existing"
    existing_device.name = "Existing Camera"

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
            return_value=[existing_device],
        ),
    ):
        assert await async_setup_entry(hass, entry)

    mock_device_registry.async_update_device.assert_not_called()


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


async def test_migrate_v1_drops_the_camera_ip(hass: HomeAssistant) -> None:
    """A v1 camera IP moves to options in 2.0 and is dropped again in 2.3 (#176)."""
    entry = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v1(), version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert CONF_CAMERA_IPS not in entry.options
    assert CONF_CAMERA_IP not in entry.data


async def test_migrate_v2_3_drops_saved_camera_ips_and_keeps_speaker_ips(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options={
            CONF_CAMERA_IPS: {"cam_1": "192.168.1.10"},
            CONF_SPEAKER_IPS: {"spk_1": "192.168.1.20"},
            "read_only": True,
        },
        version=2,
        minor_version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 3
    assert dict(entry.options) == {
        CONF_SPEAKER_IPS: {"spk_1": "192.168.1.20"},
        "read_only": True,
    }


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


async def test_migrate_v2_2_scrubs_stored_password(hass: HomeAssistant) -> None:
    """Entries created with the old store-credentials option lose the password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(legacy_stored_password=True),
        version=2,
        minor_version=1,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.minor_version == 3
    assert "password" not in entry.data
    assert "store_credentials" not in entry.data
    assert entry.data[CONF_ACCESS_TOKEN] == mock_entry_data_v2()[CONF_ACCESS_TOKEN]


async def test_migrate_v2_2_clean_entry_only_bumps_minor(hass: HomeAssistant) -> None:
    """An entry without stored credentials migrates to the latest minor unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        minor_version=1,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 3
    assert dict(entry.data) == mock_entry_data_v2()


async def test_migrate_v1_also_scrubs_stored_password(hass: HomeAssistant) -> None:
    """A v1 entry runs every migration in one pass and ends clean at 2.3."""
    entry = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v1(), version=1, unique_id="cam_1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.minor_version == 3
    assert "password" not in entry.data
    assert "store_credentials" not in entry.data


# ---------------------------------------------------------------------------
# S&L identity migration (camera_uid -> speaker_uid)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.nanit import _async_migrate_sl_identities, _async_remove_stale_devices

Baby = importlib.import_module("aionanit.models").Baby

_MIG_BABY = Baby(uid="baby_4", name="Nursery", camera_uid="cam_4", speaker_uid="spk_4")


def _migration_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _migration_hub(
    babies=None,
    speaker_uid_map=None,
    camera_speaker_pairs=None,
    unpaired_speakers=None,
    manual_speakers=None,
    unpaired_resolved=None,
) -> SimpleNamespace:
    babies = babies if babies is not None else [_MIG_BABY]
    speaker_uid_map = speaker_uid_map if speaker_uid_map is not None else {"baby_4": "spk_4"}
    if camera_speaker_pairs is None:
        camera_speaker_pairs = {
            baby.camera_uid: speaker_uid_map[baby.uid]
            for baby in babies
            if baby.camera_uid and baby.uid in speaker_uid_map
        }
    return SimpleNamespace(
        babies=babies,
        babies_complete=True,
        speaker_uid_map=speaker_uid_map,
        camera_speaker_pairs=camera_speaker_pairs,
        unpaired_speakers=unpaired_speakers if unpaired_speakers is not None else {},
        manual_speakers=manual_speakers if manual_speakers is not None else {},
        unpaired_resolved=bool(babies) if unpaired_resolved is None else unpaired_resolved,
        unpaired_from_map=set(),
    )


async def test_sl_migration_moves_unique_ids_and_keeps_entity_id(
    hass: HomeAssistant,
) -> None:
    entry = _migration_entry(hass)
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "switch", DOMAIN, "cam_4_sound_machine_switch", config_entry=entry
    )
    camera_entity = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "cam_4_temperature", config_entry=entry
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    migrated = ent_reg.async_get(old.entity_id)
    assert migrated is not None
    assert migrated.unique_id == "spk_4_sound_machine_switch"
    assert migrated.entity_id == old.entity_id
    # Camera entities sharing the prefix are untouched
    assert ent_reg.async_get(camera_entity.entity_id).unique_id == "cam_4_temperature"


async def test_sl_migration_moves_device_identifier(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "cam_4_sound_light")},
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    migrated = dev_reg.async_get(device.id)
    assert migrated is not None
    assert migrated.identifiers == {(DOMAIN, "spk_4")}


async def test_sl_migration_identifier_collision_warns_instead_of_failing(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """On HA before 2026.8 another entry can hold the speaker's identifier.

    The entry-scoped lookup cannot see it there, so the registry raises on
    the update. Setup must carry on with the legacy device left in place.
    """
    entry = _migration_entry(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "cam_4_sound_light")},
    )
    collision = dr.DeviceIdentifierCollisionError({(DOMAIN, "spk_4")}, device)

    with patch.object(dev_reg, "async_update_device", side_effect=collision):
        await _async_migrate_sl_identities(hass, entry, _migration_hub())

    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, "cam_4_sound_light")}
    assert "S&L device identifier spk_4 already exists" in caplog.text


async def test_sl_migration_collision_removes_dead_old_entity(hass: HomeAssistant) -> None:
    """If the target unique_id already exists, the unusable old entity is dropped."""
    entry = _migration_entry(hass)
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "switch", DOMAIN, "cam_4_sound_machine_switch", config_entry=entry
    )
    new = ent_reg.async_get_or_create(
        "switch", DOMAIN, "spk_4_sound_machine_switch", config_entry=entry
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    assert ent_reg.async_get(old.entity_id) is None
    assert ent_reg.async_get(new.entity_id).unique_id == "spk_4_sound_machine_switch"


async def test_sl_migration_rekeys_speaker_ip_options(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass, options={CONF_SPEAKER_IPS: {"cam_4": "192.168.1.70"}})

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    assert entry.options[CONF_SPEAKER_IPS] == {"spk_4": "192.168.1.70"}


async def test_sl_migration_noop_for_standalone_speaker(hass: HomeAssistant) -> None:
    """A speaker-only baby has no camera pairing, so there is nothing to migrate."""
    entry = _migration_entry(hass, options={CONF_SPEAKER_IPS: {"spk_5": "192.168.1.71"}})
    standalone = Baby(uid="baby_5", name="Den", camera_uid="", speaker_uid="spk_5")

    await _async_migrate_sl_identities(
        hass, entry, _migration_hub(babies=[standalone], speaker_uid_map={"baby_5": "spk_5"})
    )

    assert entry.options[CONF_SPEAKER_IPS] == {"spk_5": "192.168.1.71"}


async def test_sl_migration_idempotent(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass, options={CONF_SPEAKER_IPS: {"cam_4": "192.168.1.72"}})
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "light", DOMAIN, "cam_4_sound_light_light", config_entry=entry
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "cam_4_sound_light")},
    )

    hub = _migration_hub()
    await _async_migrate_sl_identities(hass, entry, hub)
    await _async_migrate_sl_identities(hass, entry, hub)

    assert ent_reg.async_get(old.entity_id).unique_id == "spk_4_sound_light_light"
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, "spk_4")}
    assert entry.options[CONF_SPEAKER_IPS] == {"spk_4": "192.168.1.72"}


async def test_sl_migration_runs_when_camera_left_account(hass: HomeAssistant) -> None:
    """A pairing whose camera was unpaired pre-upgrade still migrates.

    The pairing comes from the legacy persisted map (via the hub's
    camera_speaker_pairs), and the stale sweep must then keep the renamed
    device rather than deleting the user's S&L history.
    """
    entry = _migration_entry(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    old_entity = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "cam_4_sl_temperature", config_entry=entry
    )
    old_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "cam_4_sound_light")},
    )

    standalone = Baby(uid="baby_4", name="Nursery", camera_uid="", speaker_uid="spk_4")
    hub = _migration_hub(
        babies=[standalone],
        speaker_uid_map={"baby_4": "spk_4"},
        camera_speaker_pairs={"cam_4": "spk_4"},
    )

    await _async_migrate_sl_identities(hass, entry, hub)
    _async_remove_stale_devices(hass, entry, hub)

    migrated_entity = ent_reg.async_get(old_entity.entity_id)
    assert migrated_entity is not None
    assert migrated_entity.unique_id == "spk_4_sl_temperature"
    migrated_device = dev_reg.async_get(old_device.id)
    assert migrated_device is not None
    assert migrated_device.identifiers == {(DOMAIN, "spk_4")}


async def test_stale_sweep_skipped_when_nothing_recognized(hass: HomeAssistant) -> None:
    """Babies present but zero resolvable devices: don't judge staleness."""
    entry = _migration_entry(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "spk_4")},
    )

    hub = _migration_hub(
        babies=[Baby(uid="baby_4", name="Nursery", camera_uid="", speaker_uid=None)],
        speaker_uid_map={},
        camera_speaker_pairs={},
    )
    _async_remove_stale_devices(hass, entry, hub)

    assert dev_reg.async_get(device.id) is not None


async def test_stale_sweep_protects_persisted_speakers(hass: HomeAssistant) -> None:
    """A speaker known only from the persisted map survives a transient outage."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4"}}
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "spk_4")},
    )

    hub = _migration_hub(
        babies=[Baby(uid="baby_4", name="Nursery", camera_uid="cam_4", speaker_uid=None)],
        speaker_uid_map={},
        camera_speaker_pairs={},
    )
    _async_remove_stale_devices(hass, entry, hub)

    assert dev_reg.async_get(device.id) is not None


async def test_remove_config_entry_device_denies_live_hardware(hass: HomeAssistant) -> None:
    from custom_components.nanit import async_remove_config_entry_device

    entry = _migration_entry(hass)
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(live_device_uids={"spk_4"}))
    device = SimpleNamespace(identifiers={(DOMAIN, "spk_4")})

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_remove_config_entry_device_allows_and_prunes_map(hass: HomeAssistant) -> None:
    from custom_components.nanit import async_remove_config_entry_device

    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4", "baby_5": "spk_5"}}
    )
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(live_device_uids={"spk_5"}))
    device = SimpleNamespace(identifiers={(DOMAIN, "spk_4")})

    assert await async_remove_config_entry_device(hass, entry, device) is True
    assert entry.data["speaker_uid_map"] == {"baby_5": "spk_5"}


async def test_sl_migration_persists_rekeyed_map(hass: HomeAssistant) -> None:
    """The baby-keyed map lands in entry.data only after migration ran."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"cam_4": "spk_4"}}
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    assert entry.data["speaker_uid_map"] == {"baby_4": "spk_4"}


async def test_sl_migration_ip_rekey_prefers_speaker_keyed_entry(hass: HomeAssistant) -> None:
    """When legacy and speaker-keyed IPs both exist, the speaker-keyed one wins."""
    entry = _migration_entry(
        hass,
        options={CONF_SPEAKER_IPS: {"cam_4": "192.168.1.80", "spk_4": "192.168.1.81"}},
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub())

    assert entry.options[CONF_SPEAKER_IPS] == {"spk_4": "192.168.1.81"}


def test_manifest_depends_on_stream() -> None:
    """The frontend renders still images and never asks for a stream source
    unless the ``stream`` integration is loaded, so the dependency must be
    declared rather than inherited from ``default_config`` (issue #145).
    """
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2] / "custom_components" / "nanit" / "manifest.json"
        ).read_text()
    )
    assert "stream" in manifest["dependencies"]


async def test_unpaired_speakers_are_persisted(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass)

    await _async_migrate_sl_identities(
        hass, entry, _migration_hub(unpaired_speakers={"spk_9": "Living Room"})
    )

    assert entry.data["unpaired_speakers"] == {"spk_9": "Living Room"}


async def test_stale_sweep_keeps_unpaired_speakers(hass: HomeAssistant) -> None:
    """A live or persisted unpaired speaker is never swept as stale."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "unpaired_speakers": {"spk_8": "Kitchen"}}
    )
    dev_reg = dr.async_get(hass)
    live = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "spk_9")}
    )
    stored = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "spk_8")}
    )
    gone = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "spk_0")}
    )

    _async_remove_stale_devices(
        hass, entry, _migration_hub(unpaired_speakers={"spk_9": "Living Room"})
    )

    assert dev_reg.async_get(live.id) is not None
    assert dev_reg.async_get(stored.id) is not None
    assert dev_reg.async_get(gone.id) is None


async def test_remove_config_entry_device_prunes_unpaired(hass: HomeAssistant) -> None:
    from custom_components.nanit import async_remove_config_entry_device

    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "unpaired_speakers": {"spk_8": "Kitchen", "spk_9": "Living Room"}},
    )
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(live_device_uids=set()))
    device = SimpleNamespace(identifiers={(DOMAIN, "spk_8")})

    assert await async_remove_config_entry_device(hass, entry, device) is True
    assert entry.data["unpaired_speakers"] == {"spk_9": "Living Room"}


async def test_stale_sweep_keeps_manual_speakers(hass: HomeAssistant) -> None:
    """A speaker added by hand is never swept as stale."""
    entry = _migration_entry(hass)
    dev_reg = dr.async_get(hass)
    manual = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "spk_m")}
    )

    _async_remove_stale_devices(hass, entry, _migration_hub(manual_speakers={"spk_m": "Den"}))

    assert dev_reg.async_get(manual.id) is not None


async def test_remove_config_entry_device_prunes_manual(hass: HomeAssistant) -> None:
    """Deleting a hand-added speaker's device drops it and its saved IP from the options."""
    from custom_components.nanit import async_remove_config_entry_device

    entry = _migration_entry(
        hass,
        options={
            "manual_speakers": {"spk_m": "Den", "spk_n": "Nursery"},
            "speaker_ips": {"spk_m": "192.168.1.5", "spk_n": "192.168.1.6"},
        },
    )
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(live_device_uids=set()))
    device = SimpleNamespace(identifiers={(DOMAIN, "spk_m")})

    assert await async_remove_config_entry_device(hass, entry, device) is True
    assert entry.options["manual_speakers"] == {"spk_n": "Nursery"}
    assert entry.options["speaker_ips"] == {"spk_n": "192.168.1.6"}


async def test_emptied_speaker_map_is_persisted_when_moved_to_unpaired(
    hass: HomeAssistant,
) -> None:
    """A map emptied on purpose is written, unlike one from a resolution collapse."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4"}}
    )
    hub = _migration_hub(speaker_uid_map={}, unpaired_speakers={"spk_4": "Nursery"})
    hub.unpaired_from_map = {"spk_4"}

    await _async_migrate_sl_identities(hass, entry, hub)

    assert entry.data["speaker_uid_map"] == {}
    assert entry.data["unpaired_speakers"] == {"spk_4": "Nursery"}


async def test_unread_baby_keeps_its_pairing_while_a_moved_speaker_leaves_the_map(
    hass: HomeAssistant,
) -> None:
    """With a /babies row unread, its saved pairing stays and an unpaired speaker still goes."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4", "baby_5": "spk_5"}}
    )
    hub = _migration_hub(speaker_uid_map={}, unpaired_speakers={"spk_4": "Nursery"})
    hub.babies_complete = False
    hub.unpaired_from_map = {"spk_4"}

    await _async_migrate_sl_identities(hass, entry, hub)

    assert entry.data["speaker_uid_map"] == {"baby_5": "spk_5"}
    assert entry.data["unpaired_speakers"] == {"spk_4": "Nursery"}


async def test_unread_baby_drops_a_saved_pairing_its_speaker_now_unpaired(
    hass: HomeAssistant,
) -> None:
    """A saved pairing whose speaker this setup saves as unpaired is not kept as well."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4", "baby_5": "spk_5"}}
    )
    hub = _migration_hub(unpaired_speakers={"spk_5": "Den"})
    hub.babies_complete = False

    await _async_migrate_sl_identities(hass, entry, hub)

    assert entry.data["speaker_uid_map"] == {"baby_4": "spk_4"}
    assert entry.data["unpaired_speakers"] == {"spk_5": "Den"}


async def test_unread_baby_speaker_now_unpaired_can_empty_the_map(hass: HomeAssistant) -> None:
    """Dropping the only saved pairing for an unpaired speaker writes the empty map."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_5": "spk_5"}}
    )
    hub = _migration_hub(speaker_uid_map={}, unpaired_speakers={"spk_5": "Den"})
    hub.babies_complete = False

    await _async_migrate_sl_identities(hass, entry, hub)

    assert entry.data["speaker_uid_map"] == {}
    assert entry.data["unpaired_speakers"] == {"spk_5": "Den"}


async def test_empty_speaker_map_from_a_collapse_is_not_persisted(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "speaker_uid_map": {"baby_4": "spk_4"}}
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub(speaker_uid_map={}))

    assert entry.data["speaker_uid_map"] == {"baby_4": "spk_4"}


async def test_empty_babies_does_not_erase_stored_unpaired(hass: HomeAssistant) -> None:
    """When setup returned before the lookup ran, its empty result is not written."""
    entry = _migration_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "unpaired_speakers": {"spk_9": "Living Room"}}
    )

    await _async_migrate_sl_identities(hass, entry, _migration_hub(babies=[], speaker_uid_map={}))

    assert entry.data["unpaired_speakers"] == {"spk_9": "Living Room"}


async def test_unpaired_found_without_babies_is_persisted(hass: HomeAssistant) -> None:
    """A Sound + Light-only account has no babies, but its lookup ran, so it's saved."""
    entry = _migration_entry(hass)

    await _async_migrate_sl_identities(
        hass,
        entry,
        _migration_hub(
            babies=[],
            speaker_uid_map={},
            unpaired_speakers={"L151ABC1234567": "Nursery"},
            unpaired_resolved=True,
        ),
    )

    assert entry.data["unpaired_speakers"] == {"L151ABC1234567": "Nursery"}


# ---------------------------------------------------------------------------
# Care log entities of babies no longer on the account
# ---------------------------------------------------------------------------

from custom_components.nanit import _CARE_LOG_ENTITY_KEYS, _async_remove_stale_care_log_entities
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.sensor import CARE_LOG_SENSORS

_LISTED = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
_NANIT_LOGGER = "custom_components.nanit"


def _care_hub(babies: list, *, complete: bool = True) -> SimpleNamespace:
    return SimpleNamespace(babies=babies, babies_complete=complete)


def _other_sign_in(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_entry_data_v2(), "email": "other@example.com"},
        version=2,
        unique_id="other@example.com",
    )
    entry.add_to_hass(hass)
    return entry


def _register_care(
    hass: HomeAssistant, entry: MockConfigEntry, baby_uid: str, *, without: str | None = None
) -> dict[tuple[str, str], str]:
    """Register a baby's care log entities; (domain, unique id) -> entity id."""
    ent_reg = er.async_get(hass)
    registered = {}
    for domain, keys in _CARE_LOG_ENTITY_KEYS.items():
        for key in sorted(keys - {without}):
            unique_id = f"{entry.entry_id}_{baby_code(baby_uid)}_{key}"
            row = ent_reg.async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)
            registered[domain, unique_id] = row.entity_id
    return registered


def _left(hass: HomeAssistant, rows: dict[tuple[str, str], str]) -> set[tuple[str, str]]:
    """Which of these rows are still registered, as (domain, unique id)."""
    ent_reg = er.async_get(hass)
    return {
        (domain, unique_id)
        for domain, unique_id in rows
        if ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
    }


async def test_care_log_entities_of_a_baby_no_longer_listed_are_removed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _migration_entry(hass)
    ent_reg = er.async_get(hass)
    camera = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "cam_1")}
    )
    kept = _register_care(hass, entry, "baby_1")
    gone = _register_care(hass, entry, "baby_2")
    # As a user may have left them: the sensors on the camera's device, the
    # calendar on none, one sensor disabled and one renamed.
    sensors = [entity_id for (domain, _), entity_id in gone.items() if domain == "sensor"]
    for entity_id in sensors:
        ent_reg.async_update_entity(entity_id, device_id=camera.id)
    ent_reg.async_update_entity(sensors[0], disabled_by=er.RegistryEntryDisabler.USER)
    ent_reg.async_update_entity(sensors[1], new_entity_id="sensor.my_own_name", name="Mine")

    with caplog.at_level("INFO", logger=_NANIT_LOGGER):
        _async_remove_stale_care_log_entities(hass, entry, _care_hub([_LISTED]))

    assert _left(hass, gone) == set()
    for (domain, unique_id), entity_id in kept.items():
        assert ent_reg.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id
    # A count, and no entity id: those hold baby names.
    assert [r.getMessage() for r in caplog.records if r.name == _NANIT_LOGGER] == [
        "Removed 9 stale care log entities"
    ]


@pytest.mark.parametrize(
    "hub",
    [_care_hub([_LISTED], complete=False), _care_hub([])],
    ids=["incomplete_list", "empty_list"],
)
async def test_care_log_entities_are_kept_without_a_complete_list_of_babies(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, hub: SimpleNamespace
) -> None:
    entry = _migration_entry(hass)
    _register_care(hass, entry, "baby_1")
    gone = _register_care(hass, entry, "baby_2")

    with caplog.at_level("INFO", logger=_NANIT_LOGGER):
        _async_remove_stale_care_log_entities(hass, entry, hub)

    assert _left(hass, gone) == set(gone)
    assert not [r for r in caplog.records if r.name == _NANIT_LOGGER]


@pytest.mark.parametrize(
    ("domain", "key"),
    [("sensor", "last_feeding"), ("calendar", "care_log_calendar")],
)
async def test_care_log_entities_wait_until_every_listed_baby_has_its_own(
    hass: HomeAssistant, domain: str, key: str
) -> None:
    """Per domain: an entity still to be created could take an id this frees."""
    entry = _migration_entry(hass)
    other = _other_sign_in(hass)
    ent_reg = er.async_get(hass)
    sibling = Baby(uid="baby_9", name="Nursery", camera_uid="cam_1")
    hub = _care_hub([_LISTED, sibling])
    listed = {
        **_register_care(hass, entry, "baby_1"),
        **_register_care(hass, entry, "baby_9", without=key),
    }
    gone = _register_care(hass, entry, "baby_2")
    # The sibling's missing entity exists under another sign-in: it does not count.
    missing = f"{entry.entry_id}_{baby_code('baby_9')}_{key}"
    stray = ent_reg.async_get_or_create(domain, DOMAIN, missing, config_entry=other)

    _async_remove_stale_care_log_entities(hass, entry, hub)

    assert _left(hass, gone) == {row for row in gone if row[0] == domain}
    assert _left(hass, listed) == set(listed)
    assert ent_reg.async_get(stray.entity_id) is not None

    ent_reg.async_remove(stray.entity_id)
    row = ent_reg.async_get_or_create(domain, DOMAIN, missing, config_entry=entry)
    listed[domain, missing] = row.entity_id
    _async_remove_stale_care_log_entities(hass, entry, hub)

    assert _left(hass, gone) == set()
    assert _left(hass, listed) == set(listed)


async def test_only_exact_care_log_entities_are_removed(hass: HomeAssistant) -> None:
    entry = _migration_entry(hass)
    other = _other_sign_in(hass)
    ent_reg = er.async_get(hass)
    _register_care(hass, entry, "baby_1")
    code = baby_code("baby_2")
    assert code != code.upper()
    own, theirs = entry.entry_id, other.entry_id
    lookalikes = [
        ("sensor", DOMAIN, f"{own}_{code[:-1]}_last_feeding", entry),  # 31 characters
        ("sensor", DOMAIN, f"{own}_{code}0_last_feeding", entry),  # 33 characters
        ("sensor", DOMAIN, f"{own}_{code.upper()}_last_feeding", entry),
        ("sensor", DOMAIN, f"{own}_{code}_sleep_time", entry),  # an unknown ending
        ("sensor", DOMAIN, f"{own}_{code}_care_log_calendar", entry),  # the calendar's ending
        ("calendar", DOMAIN, f"{own}_{code}_last_feeding", entry),  # a sensor's ending
        ("sensor", DOMAIN, f"{theirs}_{code}_last_feeding", other),  # another sign-in's own
        ("sensor", DOMAIN, f"{own}_{code}_last_pumping", other),  # this id, their entry
        ("sensor", "another_integration", f"{own}_{code}_last_feeding", entry),
    ]
    rows = [
        ent_reg.async_get_or_create(domain, platform, unique_id, config_entry=owner)
        for domain, platform, unique_id, owner in lookalikes
    ]

    _async_remove_stale_care_log_entities(hass, entry, _care_hub([_LISTED]))

    assert all(ent_reg.async_get(row.entity_id) is not None for row in rows)


def test_the_care_log_entity_keys_are_the_sensors_keys() -> None:
    """The calendar's ending is checked where a real calendar is set up."""
    assert set(_CARE_LOG_ENTITY_KEYS) == {"sensor", "calendar"}
    assert _CARE_LOG_ENTITY_KEYS["sensor"] == {description.key for description in CARE_LOG_SENSORS}
