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
    "baby_uid",
    "camera_uid",
    "camera_ip",
    "camera_ips",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NanitConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    cameras_diag: dict[str, Any] = {}

    for camera_uid, cam_data in entry.runtime_data.cameras.items():
        push = cam_data.push_coordinator
        cloud = cam_data.cloud_coordinator

        cam_diag: dict[str, Any] = {
            "baby_name": cam_data.baby.name,
            "baby_uid": cam_data.baby.uid,
            "push_coordinator": {
                "last_update_success": push.last_update_success,
                "last_exception": (str(push.last_exception) if push.last_exception else None),
                "connected": push.connected,
                "data": asdict(push.data) if push.data is not None else None,
            },
        }

        if cloud is not None:
            cam_diag["cloud_coordinator"] = {
                "last_update_success": cloud.last_update_success,
                "last_exception": (str(cloud.last_exception) if cloud.last_exception else None),
                "data": ([asdict(e) for e in cloud.data] if cloud.data is not None else None),
            }

        cameras_diag[camera_uid] = cam_diag

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "camera_count": len(cameras_diag),
        "cameras": async_redact_data(cameras_diag, TO_REDACT),
    }
