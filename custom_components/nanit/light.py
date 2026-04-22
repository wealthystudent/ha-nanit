"""Light platform for the Nanit Sound & Light Machine."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aionanit_sl.exceptions import NanitTransportError

from . import NanitConfigEntry
from .coordinator import NanitSoundLightCoordinator
from .entity import NanitSoundLightEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Nanit Sound & Light Machine light entity."""
    entities: list[LightEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        sl_coordinator = cam_data.sound_light_coordinator
        if sl_coordinator is not None:
            entities.append(NanitSoundLightLight(sl_coordinator))

    async_add_entities(entities)


class NanitSoundLightLight(NanitSoundLightEntity, LightEntity):
    """Light entity for the Nanit Sound & Light Machine."""

    _attr_supported_color_modes = {ColorMode.HS}
    _attr_color_mode = ColorMode.HS
    _attr_translation_key = "sound_light_light"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        baby = coordinator.baby
        self._attr_unique_id = f"{baby.camera_uid}_sound_light_light"

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on."""
        if self.coordinator.data is None:
            return None
        state = self.coordinator.data
        if state.light_enabled is not None:
            result: bool = state.light_enabled
            return result
        if state.brightness is not None:
            return bool(state.brightness > 0.001)
        return None

    @property
    def brightness(self) -> int | None:
        """Return brightness in HA's 0-255 scale."""
        if self.coordinator.data is None:
            return None
        state = self.coordinator.data
        dev_brightness = state.brightness
        if dev_brightness is None:
            return None
        # When light is on but device reports 0.0 brightness, return 1
        # to prevent HA from interpreting it as "off"
        if state.power_on and state.light_enabled and dev_brightness < 0.004:
            return 1
        return min(255, max(0, int(dev_brightness * 255)))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return HS color."""
        if self.coordinator.data is None:
            return None
        state = self.coordinator.data
        color_a = state.color_r
        color_b = state.color_g
        if color_a is None and color_b is None:
            return None
        hue = (color_a or 0.0) * 360.0
        saturation = (color_b or 0.0) * 100.0
        return (hue, saturation)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light, optionally setting brightness and/or color."""
        try:
            await self.coordinator.sound_light.async_set_light_enabled(True)

            if ATTR_HS_COLOR in kwargs:
                hs = kwargs[ATTR_HS_COLOR]
                color_a = hs[0] / 360.0
                color_b = hs[1] / 100.0
                await self.coordinator.sound_light.async_set_color(color_a, color_b)

            if ATTR_BRIGHTNESS in kwargs:
                dev_brightness = max(0.01, kwargs[ATTR_BRIGHTNESS] / 255.0)
                await self.coordinator.sound_light.async_set_brightness(dev_brightness)

        except NanitTransportError as err:
            _LOGGER.error("Failed to control Sound & Light light: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        try:
            await self.coordinator.sound_light.async_set_light_enabled(False)
        except NanitTransportError as err:
            _LOGGER.error("Failed to turn off Sound & Light light: %s", err)
