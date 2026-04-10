"""Light platform for Nanit — night light with brightness control."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
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
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# After a command, ignore contradicting push updates for this many seconds.
# The Nanit camera may echo the *old* state before processing the command;
# empirical data shows the stale-then-confirmed cycle completes within ~12 s.
_COMMAND_GRACE_PERIOD: float = 15.0

# Device brightness range (night_light_brightness setting on camera).
_BRIGHTNESS_SCALE: tuple[float, float] = (1, 100)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit light entities for all cameras on the account."""
    async_add_entities(
        NanitNightLight(cam_data.push_coordinator, cam_data.camera)
        for cam_data in entry.runtime_data.cameras.values()
    )


class NanitNightLight(NanitEntity, RestoreEntity, LightEntity):
    """Nanit night light — dimmable light entity.

    ON/OFF is controlled via PUT_CONTROL (night_light field).
    Brightness is controlled via PUT_SETTINGS (night_light_brightness field, 0-100).
    """

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
        # Track last command so stale push events are suppressed.
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

        device_brightness = state.settings.night_light_brightness
        if device_brightness is not None and device_brightness > 0:
            self._attr_brightness = value_to_brightness(_BRIGHTNESS_SCALE, device_brightness)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        After a command, push events that contradict the expected state are
        suppressed for ``_COMMAND_GRACE_PERIOD`` seconds to prevent the
        camera's stale echo from bouncing the HA state.
        """
        if self.coordinator.data is not None:
            state = self.coordinator.data
            new_is_on = (
                state.control.night_light == NightLightState.ON
                if state.control.night_light is not None
                else None
            )
            new_device_brightness = state.settings.night_light_brightness

            if self._command_is_on is not None or self._command_brightness is not None:
                elapsed = time.monotonic() - self._command_ts
                if elapsed < _COMMAND_GRACE_PERIOD:
                    # Check on/off grace.
                    if (
                        self._command_is_on is not None
                        and new_is_on is not None
                        and new_is_on == self._command_is_on
                    ):
                        self._command_is_on = None
                        self._attr_is_on = new_is_on
                    # Check brightness grace.
                    if self._command_brightness is not None and new_device_brightness is not None:
                        expected_device = int(
                            brightness_to_value(_BRIGHTNESS_SCALE, self._command_brightness)
                        )
                        if new_device_brightness == expected_device:
                            self._command_brightness = None
                            self._attr_brightness = value_to_brightness(
                                _BRIGHTNESS_SCALE, new_device_brightness
                            )
                        # else: stale push — skip brightness update.
                else:
                    # Grace expired — accept camera state.
                    self._command_is_on = None
                    self._command_brightness = None
                    if new_is_on is not None:
                        self._attr_is_on = new_is_on
                    if new_device_brightness is not None and new_device_brightness > 0:
                        self._attr_brightness = value_to_brightness(
                            _BRIGHTNESS_SCALE, new_device_brightness
                        )
            else:
                # No pending command — accept push directly.
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
            # Set brightness first (if requested), then turn on.
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
