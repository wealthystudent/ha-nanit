"""The Nanit integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aionanit import NanitAuthError, NanitCamera, NanitConnectionError

from .const import (
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_UID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
    PLATFORMS,
)
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator
from .hub import NanitHub


@dataclass
class NanitData:
    """Runtime data for a Nanit config entry."""

    hub: NanitHub
    camera: NanitCamera
    push_coordinator: NanitPushCoordinator
    cloud_coordinator: NanitCloudCoordinator | None


NanitConfigEntry = ConfigEntry[NanitData]


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    session = async_get_clientsession(hass)
    access_token = entry.data[CONF_ACCESS_TOKEN]
    refresh_token = entry.data[CONF_REFRESH_TOKEN]

    # Create the hub (owns NanitClient, token lifecycle)
    hub = NanitHub(session, access_token, refresh_token)

    # Register a callback to persist refreshed tokens back to the config entry
    @callback
    def _on_tokens_refreshed(new_access: str, new_refresh: str) -> None:
        """Persist refreshed tokens to the config entry."""
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_ACCESS_TOKEN: new_access, CONF_REFRESH_TOKEN: new_refresh},
        )

    hub.setup_token_callback(_on_tokens_refreshed)

    # Get camera from hub
    camera_uid = entry.data[CONF_CAMERA_UID]
    baby_uid = entry.data[CONF_BABY_UID]
    camera_ip = entry.data.get(CONF_CAMERA_IP)

    try:
        camera = hub.get_camera(
            camera_uid,
            baby_uid,
            prefer_local=camera_ip is not None,
            local_ip=camera_ip,
        )
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err

    # Create the push coordinator and start the camera WebSocket
    push_coordinator = NanitPushCoordinator(hass, camera)
    try:
        await push_coordinator.async_setup()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Nanit camera {camera_uid}: {err}"
        ) from err

    # Cloud coordinator (optional — polls for motion/sound events)
    cloud_coordinator: NanitCloudCoordinator | None = None
    try:
        cloud_coordinator = NanitCloudCoordinator(hass, hub, baby_uid)
        await cloud_coordinator.async_config_entry_first_refresh()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError:
        # Cloud events are optional — log and continue
        LOGGER.warning("Cloud event coordinator failed to start; cloud sensors disabled")
        cloud_coordinator = None

    entry.runtime_data = NanitData(
        hub=hub,
        camera=camera,
        push_coordinator=push_coordinator,
        cloud_coordinator=cloud_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.hub.async_close()
    return unload_ok
