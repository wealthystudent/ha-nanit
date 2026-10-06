"""Number platform for Nanit."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .aionanit_sl.exceptions import NanitTransportError
from .const import DOMAIN
from .coordinator import NanitSoundLightCoordinator
from .entity import NanitSoundLightEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit number entities for all devices on the account."""
    entities: list[NumberEntity] = []
    for speaker_data in entry.runtime_data.speakers.values():
        entities.append(NanitSoundMachineVolume(speaker_data.coordinator))
        entities.append(NanitSLClockBrightness(speaker_data.coordinator))

    async_add_entities(entities)


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
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sound_machine_volume"

    @property
    def native_value(self) -> float | None:
        """Return the current sound machine volume (0-100 scale)."""
        if self.coordinator.data is None:
            return None
        vol = self.coordinator.data.volume
        if vol is None:
            return None
        return round(float(vol) * 100, 0)

    async def async_set_native_value(self, value: float) -> None:
        """Set the sound machine volume via local WebSocket."""
        try:
            await self.coordinator.sound_light.async_set_volume(value / 100.0)
        except NanitTransportError as err:
            _LOGGER.error("Failed to set sound machine volume: %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sl_volume_failed",
            ) from err


class NanitSLClockBrightness(NanitSoundLightEntity, NumberEntity):
    """Clock display brightness on the speaker's native integer scale."""

    _attr_translation_key = "sl_clock_brightness"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 8
    _attr_native_step = 1

    def __init__(self, coordinator: NanitSoundLightCoordinator) -> None:
        """Initialize the clock brightness entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sl_clock_brightness"

    @property
    def native_value(self) -> float | None:
        """Return reported brightness, including zero and while hidden."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.clock_brightness

    async def async_set_native_value(self, value: float) -> None:
        """Change brightness without changing the clock visibility."""
        if not 0 <= value <= 8 or not float(value).is_integer():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="sl_clock_brightness_invalid"
            )
        try:
            await self.coordinator.sound_light.async_set_clock_brightness(int(value))
        except NanitTransportError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="sl_clock_brightness_failed"
            ) from err
