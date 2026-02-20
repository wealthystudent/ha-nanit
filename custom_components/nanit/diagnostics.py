"""Diagnostics support for Nanit."""

from __future__ import annotations

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
    local = entry.runtime_data.local_coordinator
    cloud = entry.runtime_data.cloud_coordinator

    diag: dict[str, Any] = {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "local_coordinator": {
            "last_update_success": local.last_update_success,
            "last_exception": str(local.last_exception) if local.last_exception else None,
            "data": local.data,
        },
    }

    if cloud is not None:
        diag["cloud_coordinator"] = {
            "last_update_success": cloud.last_update_success,
            "last_exception": str(cloud.last_exception) if cloud.last_exception else None,
            "data": cloud.data,
        }

    return diag
