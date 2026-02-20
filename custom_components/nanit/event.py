"""Event platform for Nanit."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID, DOMAIN, TRANSPORT_LOCAL_CLOUD
from .coordinator import NanitCloudCoordinator


EVENT_DESCRIPTIONS: tuple[EventEntityDescription, ...] = (
    EventEntityDescription(
        key="motion_event",
        translation_key="motion_event",
        event_types=["motion_detected"],
        entity_registry_enabled_default=True,
    ),
    EventEntityDescription(
        key="sound_event",
        translation_key="sound_event",
        event_types=["sound_detected"],
        entity_registry_enabled_default=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit event entities (cloud only)."""
    cloud_coordinator = entry.runtime_data.cloud_coordinator
    if cloud_coordinator is None:
        return

    async_add_entities(
        NanitEvent(cloud_coordinator, entry, description)
        for description in EVENT_DESCRIPTIONS
    )


class NanitEvent(CoordinatorEntity[NanitCloudCoordinator], EventEntity):
    """Nanit event entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanitCloudCoordinator,
        entry: NanitConfigEntry,
        description: EventEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.data.get('camera_uid', entry.entry_id)}_{description.key}"
        )
        self._last_event_time: float | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.data.get("camera_uid", self._entry.entry_id))},
            name=self._entry.data.get("baby_name", "Nanit Camera"),
            manufacturer="Nanit",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            return

        events = self.coordinator.data.get("events", [])
        event_type_map = {
            "motion_event": "motion",
            "sound_event": "sound",
        }
        target_type = event_type_map.get(self.entity_description.key)

        for event in reversed(events):
            if event.get("type") != target_type:
                continue
            event_time = event.get("time")
            if event_time is None:
                continue
            if self._last_event_time is not None and event_time <= self._last_event_time:
                continue
            self._last_event_time = event_time
            event_type_name = f"{target_type}_detected"
            self._trigger_event(event_type_name, {"time": event_time})
            break

        self.async_write_ha_state()
