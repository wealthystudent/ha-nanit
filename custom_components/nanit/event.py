"""Event platform for Nanit."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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

    def _schedule_clear(self) -> None:
        """Schedule a 'clear' event after EVENT_CLEAR_SECONDS."""
        self._cancel_clear_timer()

        @callback
        def _fire_clear(_now: Any) -> None:
            """Fire a clear event."""
            self._clear_unsub = None
            self._trigger_event("clear")
            self.async_write_ha_state()

        self._clear_unsub = async_call_later(
            self.hass, EVENT_CLEAR_SECONDS, _fire_clear
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            return

        events = self.coordinator.data.get("events", [])
        if not events:
            self.async_write_ha_state()
            return

        # Events come sorted newest-first from the API.
        # Process them oldest-first so we fire in chronological order,
        # but only fire events we haven't seen before.
        new_events = []
        for event in reversed(events):
            event_id = event.get("id")
            if event_id is None:
                continue
            if self._last_event_id is not None and event_id <= self._last_event_id:
                continue
            event_type = event.get("type", "").lower()
            if event_type not in ("motion", "sound"):
                continue
            new_events.append((event_id, event_type, event.get("time", "")))

        if not new_events:
            self.async_write_ha_state()
            return

        # Fire each new event
        for event_id, event_type, event_time in new_events:
            self._last_event_id = event_id
            self._trigger_event(event_type, {"time": event_time})

        # Reset the clear timer — 5 minutes from the latest event
        self._schedule_clear()

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the clear timer on removal."""
        self._cancel_clear_timer()
        await super().async_will_remove_from_hass()
