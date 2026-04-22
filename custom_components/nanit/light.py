"""Light platform for Nanit — camera night light and Sound & Light Machine."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.color import (
    brightness_to_value,
    value_to_brightness,
)

from aionanit import NanitCamera
from aionanit.models import CameraState, NightLightState

from . import NanitConfigEntry
from .aionanit_sl.exceptions import NanitTransportError
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitSoundLightEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_COMMAND_GRACE_PERIOD: float = 15.0

_BRIGHTNESS_SCALE: tuple[float, float] = (1, 100)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit light entities for all cameras on the account."""
    entities: list[LightEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        entities.append(NanitNightLight(cam_data.push_coordinator, cam_data.camera))
        sl_coordinator = cam_data.sound_light_coordinator
        if sl_coordinator is not None:
            entities.append(NanitSoundLightLight(sl_coordinator))
    async_add_entities(entities)


class NanitNightLight(NanitEntity, RestoreEntity, LightEntity):
    """Nanit camera night light — dimmable light entity."""

    _attr_translation_key = "night_light"

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._camera = camera
        self._attr_unique_id = f"{camera.uid}_night_light"
        self._attr_is_on: bool | None = None
        self._attr_brightness: int | None = None
        self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._command_is_on: bool | None = None
        self._command_brightness: int | None = None
        self._command_ts: float = 0.0
        if coordinator.data is not None:
            self._sync_from_state(coordinator.data)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        if self._attr_is_on is None:
            last_state = await self.async_get_last_state()
            if last_state is not None:
                self._attr_is_on = last_state.state == STATE_ON
                if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
                    self._attr_brightness = int(brightness)

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        return self._attr_is_on

    @property
    def brightness(self) -> int | None:
        """Return current brightness (HA scale 1-255)."""
        return self._attr_brightness

    def _sync_from_state(self, state: CameraState) -> None:
        """Sync attributes from camera state."""
        nl = state.control.night_light
        if nl is not None:
            self._attr_is_on = nl == NightLightState.ON

        device_brightness: int | None = getattr(state.settings, "night_light_brightness", None)
        if device_brightness is not None and device_brightness > 0:
            self._attr_brightness = value_to_brightness(_BRIGHTNESS_SCALE, device_brightness)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is not None:
            state = self.coordinator.data
            new_is_on = (
                state.control.night_light == NightLightState.ON
                if state.control.night_light is not None
                else None
            )
            new_device_brightness: int | None = getattr(
                state.settings, "night_light_brightness", None
            )

            if self._command_is_on is not None or self._command_brightness is not None:
                elapsed = time.monotonic() - self._command_ts
                if elapsed < _COMMAND_GRACE_PERIOD:
                    if (
                        self._command_is_on is not None
                        and new_is_on is not None
                        and new_is_on == self._command_is_on
                    ):
                        self._command_is_on = None
                        self._attr_is_on = new_is_on
                    if self._command_brightness is not None and new_device_brightness is not None:
                        expected_device = int(
                            brightness_to_value(_BRIGHTNESS_SCALE, self._command_brightness)
                        )
                        if new_device_brightness == expected_device:
                            self._command_brightness = None
                            self._attr_brightness = value_to_brightness(
                                _BRIGHTNESS_SCALE, new_device_brightness
                            )
                else:
                    self._command_is_on = None
                    self._command_brightness = None
                    if new_is_on is not None:
                        self._attr_is_on = new_is_on
                    if new_device_brightness is not None and new_device_brightness > 0:
                        self._attr_brightness = value_to_brightness(
                            _BRIGHTNESS_SCALE, new_device_brightness
                        )
            else:
                if new_is_on is not None:
                    self._attr_is_on = new_is_on
                if new_device_brightness is not None and new_device_brightness > 0:
                    self._attr_brightness = value_to_brightness(
                        _BRIGHTNESS_SCALE, new_device_brightness
                    )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the night light, optionally setting brightness."""
        previous_on = self._attr_is_on
        previous_brightness = self._attr_brightness
        now = time.monotonic()

        self._attr_is_on = True
        self._command_is_on = True
        self._command_ts = now

        if ATTR_BRIGHTNESS in kwargs:
            ha_brightness = kwargs[ATTR_BRIGHTNESS]
            self._attr_brightness = ha_brightness
            self._command_brightness = ha_brightness

        self.async_write_ha_state()

        try:
            if ATTR_BRIGHTNESS in kwargs:
                device_val = int(brightness_to_value(_BRIGHTNESS_SCALE, kwargs[ATTR_BRIGHTNESS]))
                await self._camera.async_set_settings(night_light_brightness=device_val)
            await self._camera.async_set_control(night_light=NightLightState.ON)
        except Exception:
            _LOGGER.warning("Failed to turn on night light, reverting state")
            self._attr_is_on = previous_on
            self._attr_brightness = previous_brightness
            self._command_is_on = None
            self._command_brightness = None
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the night light."""
        previous_on = self._attr_is_on
        self._attr_is_on = False
        self._command_is_on = False
        self._command_ts = time.monotonic()
        self.async_write_ha_state()

        try:
            await self._camera.async_set_control(night_light=NightLightState.OFF)
        except Exception:
            _LOGGER.warning("Failed to turn off night light, reverting state")
            self._attr_is_on = previous_on
            self._command_is_on = None
            self.async_write_ha_state()
            raise


class NanitSoundLightLight(NanitSoundLightEntity, LightEntity):
    """Light entity for the Nanit Sound & Light Machine."""

    _attr_supported_color_modes = {ColorMode.HS}  # noqa: RUF012
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
