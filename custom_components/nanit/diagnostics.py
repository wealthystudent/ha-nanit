"""Diagnostics support for Nanit."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NanitConfigEntry

TO_REDACT = {
    "email",
    "password",
    "access_token",
    "refresh_token",
    "mfa_token",
    "mfa_code",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NanitConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    push = entry.runtime_data.push_coordinator
    cloud = entry.runtime_data.cloud_coordinator

    diag: dict[str, Any] = {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "push_coordinator": {
            "last_update_success": push.last_update_success,
            "last_exception": str(push.last_exception) if push.last_exception else None,
            "connected": push.connected,
            "data": asdict(push.data) if push.data is not None else None,
        },
    }

    if cloud is not None:
        diag["cloud_coordinator"] = {
            "last_update_success": cloud.last_update_success,
            "last_exception": str(cloud.last_exception) if cloud.last_exception else None,
            "data": [asdict(e) for e in cloud.data] if cloud.data is not None else None,
        }

    return diag
