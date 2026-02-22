"""The Nanit integration."""

from __future__ import annotations

import os
from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    NanitAddonClient,
    NanitApiClient,
    NanitAuthError,
    NanitConnectionError,
)
from .const import (
    ADDON_HOST_MARKER,
    ADDON_SLUG,
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_UID,
    CONF_REFRESH_TOKEN,
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

    client: NanitApiClient
    local_coordinator: NanitLocalCoordinator
    cloud_coordinator: NanitCloudCoordinator | None


type NanitConfigEntry = ConfigEntry[NanitData]


async def _async_resolve_addon_slug(session: aiohttp.ClientSession) -> str | None:
    """Resolve the full add-on slug (e.g., '5afd9e46_nanitd') via Supervisor API.

    Third-party add-ons are prefixed with a hash derived from the repo URL.
    We query all add-ons and match on the short slug suffix.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    try:
        resp = await session.get(
            "http://supervisor/addons",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status != 200:
            return None
        data = await resp.json()
        addons = data.get("data", {}).get("addons", [])
        for addon in addons:
            slug = addon.get("slug", "")
            if slug == ADDON_SLUG or slug.endswith(f"_{ADDON_SLUG}"):
                return slug
    except Exception:
        LOGGER.debug("Failed to resolve addon slug", exc_info=True)
    return None


async def _async_resolve_addon_host(session: aiohttp.ClientSession) -> str | None:
    """Resolve the nanitd add-on hostname via Supervisor API.

    Returns the full URL (http://<hostname>:8080) or None if unavailable.
    """
    full_slug = await _async_resolve_addon_slug(session)
    if not full_slug:
        return None

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    try:
        resp = await session.get(
            f"http://supervisor/addons/{full_slug}/info",
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


async def _async_set_addon_option(
    session: aiohttp.ClientSession, key: str, value: str
) -> None:
    """Set a single add-on option via the Supervisor API.

    Reads current options, merges the new key/value, and writes back.
    This triggers the add-on to pick up the new value on next restart.
    """
    full_slug = await _async_resolve_addon_slug(session)
    if not full_slug:
        LOGGER.warning("Cannot set add-on option: slug not found")
        return

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return

    try:
        # Read current options
        resp = await session.get(
            f"http://supervisor/addons/{full_slug}/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status != 200:
            return
        data = await resp.json()
        options = data.get("data", {}).get("options", {})

        # Merge new option
        options[key] = value

        # Write back
        resp = await session.post(
            f"http://supervisor/addons/{full_slug}/options",
            headers={"Authorization": f"Bearer {token}"},
            json={"options": options},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status == 200:
            LOGGER.info("Set add-on option %s=%s", key, value)
            # Restart add-on to pick up new option
            await session.post(
                f"http://supervisor/addons/{full_slug}/restart",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        else:
            text = await resp.text()
            LOGGER.warning("Failed to set add-on option: %s", text)
    except Exception:
        LOGGER.debug("Failed to set add-on option", exc_info=True)


async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Set up Nanit from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    transport = entry.options.get(CONF_TRANSPORT, entry.data.get(CONF_TRANSPORT, "local"))
    use_addon = entry.data.get(CONF_USE_ADDON, False)
    session = async_get_clientsession(hass)

    # Resolve add-on hostname dynamically if configured to use the add-on
    if use_addon or host == ADDON_HOST_MARKER:
        resolved = await _async_resolve_addon_host(session)
        if resolved:
            host = resolved
            LOGGER.info("Resolved nanitd add-on at %s", host)
        else:
            raise ConfigEntryNotReady(
                "Nanit Daemon add-on is not running. "
                "Please start the nanitd add-on and try again."
            )

        # Provision tokens to the add-on if it's not yet authenticated
        addon_client = NanitAddonClient(host, session)
        try:
            auth_status = await addon_client.get_auth_status()

            if not auth_status.get("authenticated"):
                LOGGER.info("Provisioning auth tokens to nanitd add-on")
                access_token = entry.data.get(CONF_ACCESS_TOKEN, "")
                refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
                baby_uid = entry.data.get(CONF_BABY_UID, "")
                camera_uid = entry.data.get(CONF_CAMERA_UID, "")
                baby_name = entry.data.get(CONF_BABY_NAME, "")

                babies = []
                if baby_uid:
                    babies.append(
                        {
                            "uid": baby_uid,
                            "name": baby_name,
                            "camera_uid": camera_uid or baby_uid,
                        }
                    )

                await addon_client.provision_token(
                    access_token, refresh_token, babies
                )

                # Wait for nanitd to process the token and become ready
                ready = await addon_client.wait_until_ready(timeout=30.0)
                if not ready:
                    raise ConfigEntryNotReady(
                        "nanitd add-on received tokens but did not become "
                        "ready within 30 seconds. Check add-on logs."
                    )
                LOGGER.info("nanitd add-on is authenticated and ready")

            elif not auth_status.get("ready"):
                # Authenticated but not ready — wait briefly
                ready = await addon_client.wait_until_ready(timeout=15.0)
                if not ready:
                    raise ConfigEntryNotReady(
                        "nanitd add-on is authenticated but not ready. "
                        "Check add-on logs."
                    )

            # Push camera_ip to add-on options if configured
            camera_ip = entry.data.get(CONF_CAMERA_IP, "")
            if camera_ip:
                await _async_set_addon_option(session, "camera_ip", camera_ip)
        except NanitConnectionError as err:
            raise ConfigEntryNotReady(
                f"Cannot reach nanitd add-on at {host}: {err}"
            ) from err

    client = NanitApiClient(host, session)

    # Validate backend reachability
    try:
        await client.get_status()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
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
        client=client,
        local_coordinator=local_coordinator,
        cloud_coordinator=cloud_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
