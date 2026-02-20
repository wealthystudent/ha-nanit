"""The Nanit integration."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import NanitApiClient, NanitAuthError, NanitConnectionError
from .const import (
    CONF_HOST,
    CONF_TRANSPORT,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    TRANSPORT_LOCAL_CLOUD,
)
from .coordinator import NanitCloudCoordinator, NanitLocalCoordinator


@dataclass
class NanitData:
    """Runtime data for a Nanit config entry."""

    session: aiohttp.ClientSession
    client: NanitApiClient
    local_coordinator: NanitLocalCoordinator
    cloud_coordinator: NanitCloudCoordinator | None


type NanitConfigEntry = ConfigEntry[NanitData]


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    transport = entry.data.get(CONF_TRANSPORT, "local")

    session = aiohttp.ClientSession()
    client = NanitApiClient(host, session)

    # Validate backend reachability
    try:
        await client.get_status()
    except NanitAuthError as err:
        await session.close()
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
        await session.close()
        raise ConfigEntryNotReady(
            f"Cannot reach Go backend at {host}: {err}"
        ) from err

    local_coordinator = NanitLocalCoordinator(
        hass, client, DEFAULT_SCAN_INTERVAL
    )
    await local_coordinator.async_config_entry_first_refresh()

    cloud_coordinator: NanitCloudCoordinator | None = None
    if transport == TRANSPORT_LOCAL_CLOUD:
        cloud_coordinator = NanitCloudCoordinator(hass, client)
        await cloud_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = NanitData(
        session=session,
        client=client,
        local_coordinator=local_coordinator,
        cloud_coordinator=cloud_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()

    return unload_ok
