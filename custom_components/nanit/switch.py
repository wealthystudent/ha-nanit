"""Switch platform for Nanit."""

from __future__ import annotations

import logging
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
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

from aionanit import NanitCamera
from aionanit.models import CameraState, NightLightState

_LOGGER = logging.getLogger(__name__)

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


def _settings_flag(state: CameraState, key: str) -> bool | None:
    """Return a boolean settings flag, or None only when truly unknown."""
    value = getattr(state.settings, key, None)
    if value is None:
        return None
    return value

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
        value_fn=lambda state: _settings_flag(state, "status_light_on"),
        turn_on_fn=lambda cam: cam.async_set_settings(status_light_on=True),
        turn_off_fn=lambda cam: cam.async_set_settings(status_light_on=False),
    ),
    NanitSwitchEntityDescription(
        key="mic_mute",
        translation_key="mic_mute",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda state: _settings_flag(state, "mic_mute_on"),
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
    async_add_entities(
        NanitSwitch(coordinator, camera, description) for description in SWITCHES
    )


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
        """Return true if the switch is on.

        Returns None (unknown) when no live or restored data is available,
        so that the HA frontend does not misleadingly show 'off'.
        """
        return self._attr_is_on

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Keeps the last known value when value_fn returns None so that
        ``is_on`` always has a boolean to return while the entity is available.
        """
        if self.coordinator.data is not None:
            new_value = self.entity_description.value_fn(self.coordinator.data)
            if new_value is not None:
                self._attr_is_on = new_value
            # If new_value is None, keep the previous _attr_is_on (last-known).
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        previous = self._attr_is_on
        self._attr_is_on = True
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_on_fn(self._camera)
        except Exception:
            _LOGGER.warning(
                "Failed to turn on %s, reverting state", self.entity_description.key
            )
            self._attr_is_on = previous
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        previous = self._attr_is_on
        self._attr_is_on = False
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_off_fn(self._camera)
        except Exception:
            _LOGGER.warning(
                "Failed to turn off %s, reverting state", self.entity_description.key
            )
            self._attr_is_on = previous
            self.async_write_ha_state()
            raise
