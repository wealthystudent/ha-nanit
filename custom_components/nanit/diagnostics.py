"""Diagnostics support for Nanit."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NanitConfigEntry
from .const import is_import_care_log_statistics, is_stream_stills

TO_REDACT = {
    "email",
    "password",
    "access_token",
    "refresh_token",
    "mfa_token",
    "mfa_code",
    "baby_name",
    "baby_uid",
    "camera_uid",
    "camera_ip",
    "camera_ips",
    "speaker_ip",
    "speaker_ips",
    "speaker_uid",
    "speaker_uid_map",
    "unpaired_speakers",
    "local_stream_state",
    "cloud_push",
    "advertised_host",
    "local_stream_host",
    "publisher_peer",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NanitConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # Sections are keyed by index, not by device uid: async_redact_data
    # redacts values under matching key names but never dict keys, so
    # uid-keyed sections would leak the very uids TO_REDACT lists.
    cameras_diag: dict[str, Any] = {}

    for index, cam_data in enumerate(entry.runtime_data.cameras.values()):
        push = cam_data.push_coordinator
        cloud = cam_data.cloud_coordinator

        last_still = cam_data.stream_still.last_taken
        cam_diag: dict[str, Any] = {
            "baby_name": cam_data.baby.name,
            "baby_uid": cam_data.baby.uid,
            "last_stream_still": (
                last_still.isoformat() if isinstance(last_still, datetime) else None
            ),
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

        care = cam_data.care_log_coordinator
        if care is not None:
            # Only whether any entry was found: even which kinds of entry, or
            # how many, would be a record of the baby's day. The count of
            # malformed rows the last fetch skipped says nothing about it.
            care_data = care.data
            coverage = care_data.request_coverage if care_data is not None else None
            cam_diag["care_log_coordinator"] = {
                "last_update_success": care.last_update_success,
                "last_exception": (str(care.last_exception) if care.last_exception else None),
                "has_data": care_data is not None and bool(care_data.latest),
                "dropped_rows": coverage.dropped if coverage is not None else None,
            }

        cameras_diag[f"camera_{index}"] = cam_diag

    speakers_diag: dict[str, Any] = {}
    for index, speaker_data in enumerate(entry.runtime_data.speakers.values()):
        coordinator = speaker_data.coordinator
        speakers_diag[f"speaker_{index}"] = {
            "baby_name": speaker_data.baby.name,
            "baby_uid": speaker_data.baby.uid,
            "connected": coordinator.connected,
            "connection_mode": speaker_data.sound_light.connection_mode,
            "last_update_success": coordinator.last_update_success,
            "data": asdict(coordinator.data) if coordinator.data is not None else None,
        }

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "stream_stills": is_stream_stills(entry),
        "camera_count": len(cameras_diag),
        "cameras": async_redact_data(cameras_diag, TO_REDACT),
        "speaker_count": len(speakers_diag),
        "speakers": async_redact_data(speakers_diag, TO_REDACT),
        "care_log_statistics": _care_log_statistics(entry),
        "local_streaming": (
            async_redact_data(local.diagnostics(), TO_REDACT)
            if (local := entry.runtime_data.local_stream) is not None
            else {"enabled": False}
        ),
    }


def _care_log_statistics(entry: NanitConfigEntry) -> dict[str, object]:
    """Return whether the import is on and, per baby, whether this entry writes.

    Babies are keyed by position only; the importer's own diagnostics are
    fixed state names, flags, operational counters and ages, and say nothing
    about the baby's day (no count of entries or rows).
    """
    if not is_import_care_log_statistics(entry):
        return {"enabled": False, "babies": {}}
    writers = entry.runtime_data.care_log_writers
    if writers is None:
        return {"enabled": True, "babies": {}}
    return writers.diagnostics()
