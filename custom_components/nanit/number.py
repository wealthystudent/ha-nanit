"""Number platform for Nanit."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

from aionanit import NanitCamera


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit number entities for all cameras on the account."""
    async_add_entities(
        NanitVolume(cam_data.push_coordinator, cam_data.camera)
        for cam_data in entry.runtime_data.cameras.values()
    )


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
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._camera = camera
        self._attr_unique_id = f"{camera.uid}_volume"

    @property
    def native_value(self) -> float | None:
        """Return the current volume."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.settings.volume

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume."""
        await self._camera.async_set_settings(volume=int(value))
        self.async_write_ha_state()
