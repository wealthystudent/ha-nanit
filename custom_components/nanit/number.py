"""Number platform for Nanit."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .api import NanitApiClient
from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit number entities."""
    coordinator = entry.runtime_data.local_coordinator
    client = entry.runtime_data.client
    async_add_entities([NanitVolume(coordinator, client)])


class NanitVolume(NanitEntity, NumberEntity):
    """Volume number entity."""

    _attr_translation_key = "volume"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: NanitLocalCoordinator,
        client: NanitApiClient,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._client = client
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get('camera_uid', coordinator.config_entry.entry_id)}"
            "_volume"
        )

    @property
    def native_value(self) -> float | None:
        """Return the current volume."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("settings", {}).get("volume")

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume."""
        await self._client.set_volume(int(value))
        await self.coordinator.async_request_refresh()
