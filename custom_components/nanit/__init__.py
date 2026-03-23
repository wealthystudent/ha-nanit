"""The Nanit integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aionanit import NanitAuthError, NanitConnectionError

from .const import (
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_CAMERA_UID,
    CONF_REFRESH_TOKEN,
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
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = NanitData(hub=hub, cameras=hub.camera_data)

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


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> bool:
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

        LOGGER.info(
            "Migrated Nanit config entry to version 2 (multi-camera support)"
        )

    return True


async def _async_options_update_listener(
    hass: HomeAssistant, entry: NanitConfigEntry
) -> None:
    """Reload entry when options change (e.g. camera IPs updated)."""
    await hass.config_entries.async_reload(entry.entry_id)
