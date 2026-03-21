"""Switch platform for Nanit."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from . import NanitConfigEntry
from .const import CONF_CAMERA_UID
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitSoundLightEntity

from aionanit import NanitCamera
from aionanit.models import CameraState, NightLightState

from .aionanit_sl.exceptions import NanitTransportError
from .aionanit_sl.sound_light import NanitSoundLight

_LOGGER = logging.getLogger(__name__)

# After a command, ignore contradicting push updates for this many seconds.
_COMMAND_GRACE_PERIOD: float = 15.0

@dataclass(frozen=True, kw_only=True)
class NanitSwitchEntityDescription(SwitchEntityDescription):
    """Describe a Nanit switch."""

    value_fn: Callable[[CameraState], bool | None]
    turn_on_fn: Callable[[NanitCamera], Coroutine[Any, Any, None]]
    turn_off_fn: Callable[[NanitCamera], Coroutine[Any, Any, None]]


def _night_light_value(state: CameraState) -> bool | None:
    """Return night light on/off state, or None only when truly unknown."""
    nl = state.control.night_light
    if nl is None:
        return None
    return nl == NightLightState.ON


SWITCHES: tuple[NanitSwitchEntityDescription, ...] = (
    NanitSwitchEntityDescription(
        key="night_light",
        translation_key="night_light",
        icon="mdi:lightbulb-night",
        entity_registry_enabled_default=True,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=_night_light_value,
        turn_on_fn=lambda cam: cam.async_set_control(night_light=NightLightState.ON),
        turn_off_fn=lambda cam: cam.async_set_control(night_light=NightLightState.OFF),
    ),
    NanitSwitchEntityDescription(
        key="camera_power",
        translation_key="camera_power",
        icon="mdi:power",
        entity_registry_enabled_default=True,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda state: (
            not state.settings.sleep_mode
            if state.settings.sleep_mode is not None
            else None
        ),
        turn_on_fn=lambda cam: cam.async_set_settings(sleep_mode=False),
        turn_off_fn=lambda cam: cam.async_set_settings(sleep_mode=True),
    ),
    NanitSwitchEntityDescription(
        key="status_led",
        translation_key="status_led",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda state: state.settings.status_light_on if state.settings.status_light_on is not None else None,
        turn_on_fn=lambda cam: cam.async_set_settings(status_light_on=True),
        turn_off_fn=lambda cam: cam.async_set_settings(status_light_on=False),
    ),
    NanitSwitchEntityDescription(
        key="mic_mute",
        translation_key="mic_mute",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda state: state.settings.mic_mute_on if state.settings.mic_mute_on is not None else None,
        turn_on_fn=lambda cam: cam.async_set_settings(mic_mute_on=True),
        turn_off_fn=lambda cam: cam.async_set_settings(mic_mute_on=False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit switches."""
    coordinator = entry.runtime_data.push_coordinator
    camera = entry.runtime_data.camera
    entities: list[SwitchEntity] = [
        NanitSwitch(coordinator, camera, description) for description in SWITCHES
    ]

    # Sound & Light Machine switches (optional)
    sl_coordinator = entry.runtime_data.sound_light_coordinator
    if sl_coordinator is not None:
        entities.append(NanitSLPowerSwitch(sl_coordinator, entry))
        entities.append(NanitSLLightSwitch(sl_coordinator, entry))
        entities.append(NanitSLSoundSwitch(sl_coordinator, entry))

    async_add_entities(entities)


class NanitSwitch(NanitEntity, RestoreEntity, SwitchEntity):
    """Nanit switch entity."""

    entity_description: NanitSwitchEntityDescription

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
        description: NanitSwitchEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._camera = camera
        self._attr_is_on: bool | None = None
        # Track the last command so stale push events are suppressed.
        self._command_state: bool | None = None
        self._command_ts: float = 0.0
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get(CONF_CAMERA_UID, coordinator.config_entry.entry_id)}"
            f"_{description.key}"
        )
        if coordinator.data is not None:
            self._attr_is_on = self.entity_description.value_fn(coordinator.data)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        if self._attr_is_on is None:
            if (last_state := await self.async_get_last_state()) is not None:
                self._attr_is_on = last_state.state == STATE_ON

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        return self._attr_is_on

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        After a command, push events that contradict the expected state are
        suppressed for ``_COMMAND_GRACE_PERIOD`` seconds to prevent the
        camera's stale echo from bouncing the HA state.
        """
        if self.coordinator.data is not None:
            new_value = self.entity_description.value_fn(self.coordinator.data)
            if new_value is not None:
                if self._command_state is not None:
                    elapsed = time.monotonic() - self._command_ts
                    if elapsed < _COMMAND_GRACE_PERIOD:
                        if new_value == self._command_state:
                            # Push confirms the command — accept and clear.
                            self._command_state = None
                            self._attr_is_on = new_value
                        else:
                            # Stale push contradicts the command — skip.
                            _LOGGER.debug(
                                "Ignoring stale push for %s (got %s, expected %s, %.1fs after command)",
                                self.entity_description.key,
                                new_value,
                                self._command_state,
                                elapsed,
                            )
                    else:
                        # Grace period expired — accept whatever the camera says.
                        self._command_state = None
                        self._attr_is_on = new_value
                else:
                    self._attr_is_on = new_value
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        previous = self._attr_is_on
        self._attr_is_on = True
        self._command_state = True
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_on_fn(self._camera)
        except Exception:
            _LOGGER.warning(
                "Failed to turn on %s, reverting state", self.entity_description.key
            )
            self._attr_is_on = previous
            self._command_state = None
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        previous = self._attr_is_on
        self._attr_is_on = False
        self._command_state = False
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_off_fn(self._camera)
        except Exception:
            _LOGGER.warning(
                "Failed to turn off %s, reverting state", self.entity_description.key
            )
            self._attr_is_on = previous
            self._command_state = None
            self.async_write_ha_state()
            raise


# --- Sound & Light Machine switches ---


class NanitSLPowerSwitch(NanitSoundLightEntity, SwitchEntity):
    """Power switch for the Nanit Sound & Light Machine."""

    _attr_translation_key = "sound_machine_switch"
    _attr_icon = "mdi:power"
    _attr_device_class = SwitchDeviceClass.SWITCH

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
            "_sound_machine_switch"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the device is powered on."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.power_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the device on."""
        try:
            await self.coordinator.sound_light.async_set_power(True)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn on S&L device: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the device off."""
        try:
            await self.coordinator.sound_light.async_set_power(False)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn off S&L device: %s", err)


class NanitSLLightSwitch(NanitSoundLightEntity, SwitchEntity):
    """Night light on/off switch for the Nanit Sound & Light Machine."""

    _attr_translation_key = "sl_light_switch"
    _attr_icon = "mdi:lightbulb"
    _attr_device_class = SwitchDeviceClass.SWITCH

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
            "_sl_light_switch"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the night light is on."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.light_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the night light on."""
        try:
            await self.coordinator.sound_light.async_set_light_enabled(True)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn on S&L night light: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the night light off."""
        try:
            await self.coordinator.sound_light.async_set_light_enabled(False)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn off S&L night light: %s", err)


class NanitSLSoundSwitch(NanitSoundLightEntity, SwitchEntity):
    """Sound on/off switch for the Nanit Sound & Light Machine."""

    _attr_translation_key = "sl_sound_switch"
    _attr_icon = "mdi:music-note"
    _attr_device_class = SwitchDeviceClass.SWITCH

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
            "_sl_sound_switch"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if sound is on."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.sound_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn sound on."""
        try:
            await self.coordinator.sound_light.async_set_sound_on(True)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn on S&L sound: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn sound off."""
        try:
            await self.coordinator.sound_light.async_set_sound_on(False)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to turn off S&L sound: %s", err)
