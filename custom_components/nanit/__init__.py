"""The Nanit integration."""

from __future__ import annotations

import os
from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import NanitApiClient, NanitAuthError, NanitConnectionError
from .const import (
    ADDON_HOST_MARKER,
    ADDON_SLUG,
    CONF_HOST,
    CONF_TRANSPORT,
    CONF_USE_ADDON,
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


async def _async_resolve_addon_host() -> str | None:
    """Resolve the nanitd add-on hostname via Supervisor API.

    Returns the full URL (http://<hostname>:8080) or None if unavailable.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"http://supervisor/addons/{ADDON_SLUG}/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                return None
            data = await resp.json()
            addon_data = data.get("data", {})
            hostname = addon_data.get("hostname")
            state = addon_data.get("state")
            if state == "started" and hostname:
                return f"http://{hostname}:8080"
    except Exception:
        LOGGER.debug("Failed to resolve addon hostname", exc_info=True)
    return None


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    transport = entry.data.get(CONF_TRANSPORT, "local")
    use_addon = entry.data.get(CONF_USE_ADDON, False)

    # Resolve add-on hostname dynamically if configured to use the add-on
    if use_addon or host == ADDON_HOST_MARKER:
        resolved = await _async_resolve_addon_host()
        if resolved:
            host = resolved
            LOGGER.info("Resolved nanitd add-on at %s", host)
        else:
            raise ConfigEntryNotReady(
                "Nanit Daemon add-on is not running. "
                "Please start the nanitd add-on and try again."
            )

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
