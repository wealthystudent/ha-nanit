"""Event platform for Nanit."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID, DOMAIN, TRANSPORT_LOCAL_CLOUD
from .coordinator import NanitCloudCoordinator

_LOGGER = logging.getLogger(__name__)

# Clear the event state after 5 minutes of no new events
EVENT_CLEAR_SECONDS = 300

EVENT_DESCRIPTION = EventEntityDescription(
    key="activity",
    translation_key="activity",
    event_types=["motion", "sound", "clear"],
    entity_registry_enabled_default=True,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit activity event entity (cloud only)."""
    cloud_coordinator = entry.runtime_data.cloud_coordinator
    if cloud_coordinator is None:
        return

    async_add_entities([NanitActivityEvent(cloud_coordinator, entry)])


class NanitActivityEvent(CoordinatorEntity[NanitCloudCoordinator], EventEntity):
    """Nanit activity event entity.

    Fires a 'motion' or 'sound' event whenever the cloud reports new activity.
    After 5 minutes with no new events, fires a 'clear' event to indicate
    no recent activity.
    """

    _attr_has_entity_name = True
    entity_description = EVENT_DESCRIPTION

    def __init__(
        self,
        coordinator: NanitCloudCoordinator,
        entry: NanitConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.data.get('camera_uid', entry.entry_id)}_activity"
        )
        self._last_event_id: int | None = None
        self._last_event_time: datetime | None = None
        self._last_event_type: str | None = None
        self._clear_unsub: CALLBACK_TYPE | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.data.get("camera_uid", self._entry.entry_id))},
            name=self._entry.data.get("baby_name", "Nanit Camera"),
            manufacturer="Nanit",
        )

    def _cancel_clear_timer(self) -> None:
        """Cancel any pending clear timer."""
        if self._clear_unsub is not None:
            self._clear_unsub()
            self._clear_unsub = None

    def _schedule_clear(self, latest_event_time: datetime | None) -> None:
        """Schedule a 'clear' event after EVENT_CLEAR_SECONDS."""
        self._cancel_clear_timer()

        @callback
        def _fire_clear(_now: Any) -> None:
            """Fire a clear event."""
            self._clear_unsub = None
            self._trigger_event("clear")
            self._last_event_type = "clear"
            self.async_write_ha_state()

        delay = EVENT_CLEAR_SECONDS
        if latest_event_time is not None:
            now = dt_util.utcnow()
            age = (now - latest_event_time).total_seconds()
            delay = max(0, EVENT_CLEAR_SECONDS - int(age))

        if delay == 0:
            _fire_clear(None)
            return

        self._clear_unsub = async_call_later(self.hass, delay, _fire_clear)

    @staticmethod
    def _parse_event_time(raw_time: Any) -> datetime | None:
        if raw_time is None:
            return None
        if isinstance(raw_time, (int, float)):
            return datetime.fromtimestamp(raw_time, tz=dt_util.UTC)
        if isinstance(raw_time, str):
            parsed = dt_util.parse_datetime(raw_time)
            if parsed is None:
                return None
            if dt_util.is_naive(parsed):
                return dt_util.as_utc(parsed)
            return parsed
        return None

    @staticmethod
    def _normalize_event_id(raw_id: Any) -> int | None:
        if raw_id is None:
            return None
        if isinstance(raw_id, int):
            return raw_id
        if isinstance(raw_id, str):
            try:
                return int(raw_id)
            except ValueError:
                return None
        return None

    def _is_new_event(
        self, event_id: int | None, event_time: datetime | None
    ) -> bool:
        if event_id is not None and self._last_event_id is not None:
            return event_id > self._last_event_id
        if event_id is None and event_time is not None and self._last_event_time is not None:
            return event_time > self._last_event_time
        return True

    def _fire_event(
        self,
        event_type: str,
        event_id: int | None,
        event_time: datetime | None,
        raw_time: Any,
    ) -> None:
        if event_id is not None:
            self._last_event_id = event_id
        if event_time is not None:
            self._last_event_time = event_time
        elif self._last_event_time is None:
            self._last_event_time = dt_util.utcnow()
        self._last_event_type = event_type
        self._trigger_event(event_type, {"time": raw_time})

    def _latest_event(self, events: list[dict[str, Any]]) -> tuple[int | None, str | None, datetime | None, Any]:
        for event in events:
            event_type = str(event.get("type", "")).lower()
            if event_type not in ("motion", "sound"):
                continue
            raw_time = event.get("time")
            return (
                self._normalize_event_id(event.get("id")),
                event_type,
                self._parse_event_time(raw_time),
                raw_time,
            )
        return (None, None, None, None)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            return

        events = self.coordinator.data.get("events", [])
        now = dt_util.utcnow()
        if not events:
            if (
                self._last_event_time is not None
                and self._last_event_type != "clear"
                and (now - self._last_event_time).total_seconds() > EVENT_CLEAR_SECONDS
            ):
                self._trigger_event("clear")
                self._last_event_type = "clear"
            self.async_write_ha_state()
            return

        # Events come sorted newest-first from the API.
        # Process them oldest-first so we fire in chronological order,
        # but only fire events we haven't seen before.
        new_events = []
        for event in reversed(events):
            event_id = self._normalize_event_id(event.get("id"))
            event_type = str(event.get("type", "")).lower()
            if event_type not in ("motion", "sound"):
                continue
            raw_time = event.get("time")
            event_time = self._parse_event_time(raw_time)
            if not self._is_new_event(event_id, event_time):
                continue
            new_events.append((event_id, event_type, event_time, raw_time))

        if not new_events:
            if self._last_event_id is None and self._last_event_time is None:
                latest_id, latest_type, latest_time, latest_raw = self._latest_event(events)
                if latest_type is not None:
                    self._last_event_id = latest_id
                    self._last_event_time = latest_time
                    if latest_time is not None:
                        age = (now - latest_time).total_seconds()
                        if age <= EVENT_CLEAR_SECONDS:
                            self._fire_event(latest_type, latest_id, latest_time, latest_raw)
                            self._schedule_clear(latest_time)
                        else:
                            self._trigger_event("clear")
                            self._last_event_type = "clear"
                    else:
                        self._fire_event(latest_type, latest_id, None, latest_raw)
                        self._schedule_clear(None)
                    self.async_write_ha_state()
                    return
            if (
                self._last_event_time is not None
                and self._last_event_type != "clear"
                and (now - self._last_event_time).total_seconds() > EVENT_CLEAR_SECONDS
            ):
                self._trigger_event("clear")
                self._last_event_type = "clear"
            self.async_write_ha_state()
            return

        # Fire each new event
        for event_id, event_type, event_time, raw_time in new_events:
            self._fire_event(event_type, event_id, event_time, raw_time)

        # Reset the clear timer — 5 minutes from the latest event
        self._schedule_clear(self._last_event_time)

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the clear timer on removal."""
        self._cancel_clear_timer()
        await super().async_will_remove_from_hass()
