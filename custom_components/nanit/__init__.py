"""The Nanit integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aionanit import NanitAuthError, NanitConnectionError

from .const import (
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_CAMERA_UID,
    DOMAIN,
    LOGGER,
    PLATFORMS,
)
from .hub import CameraData, NanitHub


@dataclass
class NanitData:
    """Runtime data for a Nanit config entry."""

    hub: NanitHub
    cameras: dict[str, CameraData]


type NanitConfigEntry = ConfigEntry[NanitData]


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    session = async_get_clientsession(hass)

    hub = NanitHub(hass, session, entry)

    try:
        await hub.async_setup()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except NanitConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="connection_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    entry.runtime_data = NanitData(hub=hub, cameras=hub.camera_data)

    _async_remove_stale_devices(hass, entry, hub)
    _async_remove_deprecated_entities(hass, hub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload entry when options change (e.g. camera IPs updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.hub.async_close()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate config entry from v1 (single-camera) to v2 (multi-camera per account).

    v1 stored baby/camera info in entry.data and had unique_id = camera_uid.
    v2 stores only auth info in entry.data, camera IPs in options, and uses
    unique_id = email (account-level).
    """
    LOGGER.debug("Migrating Nanit config entry from version %s", config_entry.version)

    if config_entry.version > 2:
        # Future version — can't downgrade
        return False

    if config_entry.version == 1:
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}

        # Move camera IP from entry.data to options["camera_ips"]
        camera_ip = new_data.pop(CONF_CAMERA_IP, None)
        camera_uid = new_data.get(CONF_CAMERA_UID)
        if camera_ip and camera_uid:
            camera_ips = new_options.get(CONF_CAMERA_IPS, {})
            camera_ips[camera_uid] = camera_ip
            new_options[CONF_CAMERA_IPS] = camera_ips

        # Remove baby/camera-specific fields (now fetched at runtime)
        new_data.pop(CONF_BABY_UID, None)
        new_data.pop(CONF_CAMERA_UID, None)
        new_data.pop(CONF_BABY_NAME, None)

        # Update unique_id to email if credentials were stored
        new_unique_id = new_data.get(CONF_EMAIL) or config_entry.unique_id

        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            options=new_options,
            unique_id=new_unique_id,
            version=2,
        )

        LOGGER.info("Migrated Nanit config entry to version 2 (multi-camera support)")

    return True


def _async_remove_stale_devices(
    hass: HomeAssistant, entry: NanitConfigEntry, hub: NanitHub
) -> None:
    """Remove HA devices for cameras no longer on the Nanit account."""
    device_reg = dr.async_get(hass)
    known_camera_uids = {baby.camera_uid for baby in hub.babies}

    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        device_uids = {
            identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN
        }
        if device_uids and not device_uids & known_camera_uids:
            LOGGER.info(
                "Removing stale device %s (camera no longer on account)",
                device.name,
            )
            device_reg.async_remove_device(device.id)


_DEPRECATED_ENTITIES: list[tuple[str, str]] = [
    ("switch", "night_light"),
]


def _async_remove_deprecated_entities(hass: HomeAssistant, hub: NanitHub) -> None:
    """Remove entities that migrated to a different platform.

    When an entity moves from one platform to another (e.g. switch → light),
    the old entity becomes orphaned in the registry. This removes them so
    users don't see stale "unavailable" entities.
    """
    ent_reg = er.async_get(hass)
    camera_uids = {baby.camera_uid for baby in hub.babies}

    for old_domain, key in _DEPRECATED_ENTITIES:
        for camera_uid in camera_uids:
            unique_id = f"{camera_uid}_{key}"
            entity_id = ent_reg.async_get_entity_id(old_domain, DOMAIN, unique_id)
            if entity_id is not None:
                LOGGER.info(
                    "Removing deprecated %s entity %s (migrated to new platform)",
                    old_domain,
                    entity_id,
                )
                ent_reg.async_remove(entity_id)


async def _async_options_update_listener(hass: HomeAssistant, entry: NanitConfigEntry) -> None:
    """Reload entry when options change (e.g. camera IPs updated)."""
    await hass.config_entries.async_reload(entry.entry_id)
