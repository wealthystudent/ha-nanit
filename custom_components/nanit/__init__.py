"""The Nanit integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL
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


type NanitConfigEntry = ConfigEntry[NanitData]


def _account_key(entry: ConfigEntry) -> str:
    """Derive the shared hub key for a config entry."""
    return entry.data.get(CONF_EMAIL) or entry.entry_id


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    account = _account_key(entry)

    # Shared hub: reuse or create
    hub_record = hass.data[DOMAIN].get(account)
    if hub_record is not None:
        hub = hub_record["hub"]
        hub_record["ref_count"] += 1
    else:
        session = async_get_clientsession(hass)
        access_token = entry.data[CONF_ACCESS_TOKEN]
        refresh_token = entry.data[CONF_REFRESH_TOKEN]
        hub = NanitHub(session, access_token, refresh_token)

        # Register token refresh callback (once per hub)
        @callback
        def _on_tokens_refreshed(new_access: str, new_refresh: str) -> None:
            """Persist refreshed tokens to all config entries for this account."""
            if not entry.data.get(CONF_EMAIL):
                # Isolated hub (legacy entry without email) — only update this entry
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_ACCESS_TOKEN: new_access, CONF_REFRESH_TOKEN: new_refresh},
                )
                return
            for other in hass.config_entries.async_entries(DOMAIN):
                if _account_key(other) == account:
                    hass.config_entries.async_update_entry(
                        other,
                        data={
                            **other.data,
                            CONF_ACCESS_TOKEN: new_access,
                            CONF_REFRESH_TOKEN: new_refresh,
                        },
                    )

        hub.setup_token_callback(_on_tokens_refreshed)
        hass.data[DOMAIN][account] = {"hub": hub, "ref_count": 1}

    # Per-entry camera + coordinators
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

    push_coordinator = NanitPushCoordinator(hass, camera)
    try:
        await push_coordinator.async_setup()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Nanit camera {camera_uid}: {err}"
        ) from err

    cloud_coordinator: NanitCloudCoordinator | None = None
    try:
        cloud_coordinator = NanitCloudCoordinator(hass, hub, baby_uid)
        await cloud_coordinator.async_config_entry_first_refresh()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError:
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
        data = entry.runtime_data
        camera_uid = entry.data[CONF_CAMERA_UID]

        # Explicitly shut down per-entry coordinators
        await data.push_coordinator.async_shutdown()

        # Remove this entry's camera from the shared hub
        await data.hub.async_remove_camera(camera_uid)

        account = _account_key(entry)
        hub_record = hass.data[DOMAIN].get(account)
        if hub_record is not None:
            hub_record["ref_count"] -= 1
            if hub_record["ref_count"] <= 0:
                await hub_record["hub"].async_close()
                del hass.data[DOMAIN][account]
    return unload_ok
