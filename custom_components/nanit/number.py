"""Number platform for Nanit."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitSoundLightEntity

from aionanit import NanitCamera

from .aionanit_sl.exceptions import NanitTransportError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit number entities."""
    coordinator = entry.runtime_data.push_coordinator
    camera = entry.runtime_data.camera
    entities: list[NumberEntity] = [NanitVolume(coordinator, camera)]

    sl_coordinator = entry.runtime_data.sound_light_coordinator
    if sl_coordinator is not None:
        entities.append(NanitSoundMachineVolume(sl_coordinator, entry))

    async_add_entities(entities)


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
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get(CONF_CAMERA_UID, coordinator.config_entry.entry_id)}"
            "_volume"
        )

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


class NanitSoundMachineVolume(NanitSoundLightEntity, NumberEntity):
    """Volume number entity for the Nanit Sound & Light Machine."""

    _attr_translation_key = "sound_machine_volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        entry: NanitConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.data.get(CONF_CAMERA_UID, entry.entry_id)}"
            "_sound_machine_volume"
        )

    @property
    def native_value(self) -> float | None:
        """Return the current sound machine volume (0-100 scale)."""
        if self.coordinator.data is None:
            return None
        vol = self.coordinator.data.volume
        if vol is None:
            return None
        return round(vol * 100, 0)

    async def async_set_native_value(self, value: float) -> None:
        """Set the sound machine volume via local WebSocket."""
        try:
            await self.coordinator.sound_light.async_set_volume(value / 100.0)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to set sound machine volume: %s", err)
