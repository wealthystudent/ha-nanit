"""mDNS discovery of the Sound & Light Machine's LAN address.

The speaker advertises an `_http._tcp.local.` mDNS service whose instance
name contains its uid and whose TXT properties carry `UID=<speaker_uid>`
(e.g. "Nanit Light and Sound (L151AMN2434018)" on port 442). A containerized
Home Assistant usually can't resolve `.local` names through libc (no
nss-mdns), so instead of resolving the deterministic `Nanit-<uid>.local`
hostname we browse for the service on HA's shared zeroconf instance and read
the device's IPv4 from it.

The resolver returned here is injected into the S&L transport, which treats
it as best-effort: any failure just leaves the device on the cloud relay.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_SERVICE_TYPE = "_http._tcp.local."
# How long to browse for the service when it isn't already cached.
_BROWSE_POLLS = 30  # x interval = ~3s max
_BROWSE_POLL_INTERVAL = 0.1  # seconds

# One-shot flag so a broken zeroconf import warns once, not per connect.
_zeroconf_import_warned = False


def make_local_host_resolver(
    hass: HomeAssistant,
) -> Callable[[str], Awaitable[str | None]]:
    """Build an async resolver: speaker_uid -> LAN IPv4 (or None)."""

    async def _resolve(speaker_uid: str) -> str | None:
        return await _resolve_local_host(hass, speaker_uid)

    return _resolve


async def _resolve_local_host(hass: HomeAssistant, speaker_uid: str) -> str | None:
    """Resolve the speaker's LAN IPv4 via HA's zeroconf, matched by uid.

    Returns None on any failure, leaving the device on the cloud relay.
    """
    global _zeroconf_import_warned
    try:
        from homeassistant.components import zeroconf as ha_zeroconf
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
        from zeroconf.const import _CLASS_IN, _TYPE_PTR
    except ImportError as err:
        # _CLASS_IN/_TYPE_PTR are private zeroconf API, so a zeroconf bump
        # could remove them. Warn once (not per connect attempt), otherwise
        # the local transport would silently vanish and every send would
        # ride the laggy relay.
        if not _zeroconf_import_warned:
            _zeroconf_import_warned = True
            _LOGGER.warning(
                "zeroconf internals unavailable (%s); the S&L local (LAN) "
                "connection is disabled, staying on the cloud relay",
                err,
            )
        return None

    try:
        aiozc = await ha_zeroconf.async_get_async_instance(hass)
        zc = aiozc.zeroconf
        uid = speaker_uid.lower()

        async def _ipv4_for(name: str) -> str | None:
            info = AsyncServiceInfo(_SERVICE_TYPE, name)
            if not info.load_from_cache(zc):
                await info.async_request(zc, 3000)
            for addr in info.parsed_addresses():
                if "." in addr and ":" not in addr:  # IPv4
                    _LOGGER.debug("Resolved S&L %s -> %s via mDNS", speaker_uid, addr)
                    return addr
            return None

        # Fast path: a matching service already in HA's zeroconf cache.
        for rec in zc.cache.get_all_by_details(_SERVICE_TYPE, _TYPE_PTR, _CLASS_IN):
            name = getattr(rec, "alias", None)
            if name and uid in name.lower():
                ip = await _ipv4_for(name)
                if ip:
                    return ip

        # Otherwise browse _http._tcp briefly and match as instances appear.
        seen: set[str] = set()

        def _on_change(
            zeroconf: object,
            service_type: str,
            name: str,
            state_change: object,
        ) -> None:
            if state_change is not ServiceStateChange.Removed and uid in name.lower():
                seen.add(name)

        browser = AsyncServiceBrowser(zc, _SERVICE_TYPE, handlers=[_on_change])
        try:
            for _ in range(_BROWSE_POLLS):
                await asyncio.sleep(_BROWSE_POLL_INTERVAL)
                for name in list(seen):
                    ip = await _ipv4_for(name)
                    if ip:
                        return ip
        finally:
            await browser.async_cancel()

        _LOGGER.debug("mDNS: no _http._tcp service matched S&L uid %s", speaker_uid)
        return None
    except Exception as err:  # noqa: BLE001 (local discovery is best-effort)
        _LOGGER.debug("mDNS resolve error for S&L %s: %s", speaker_uid, err)
        return None
