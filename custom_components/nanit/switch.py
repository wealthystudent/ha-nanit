"""Switch platform for Nanit."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

from aionanit import NanitCamera
from aionanit.models import CameraState, NightLightState


@dataclass(frozen=True, kw_only=True)
class NanitSwitchEntityDescription(SwitchEntityDescription):
    """Describe a Nanit switch."""

    value_fn: Callable[[CameraState], bool | None]
    turn_on_fn: Callable[[NanitCamera], Coroutine[Any, Any, None]]
    turn_off_fn: Callable[[NanitCamera], Coroutine[Any, Any, None]]


def _night_light_value(state: CameraState) -> bool | None:
    if state.status.connected_to_server is False:
        return None
    nl = state.control.night_light
    if nl is None:
        return None
    return nl == NightLightState.ON


def _settings_flag(state: CameraState, key: str) -> bool | None:
    if state.status.connected_to_server is False:
        return None
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
            None
            if state.status.connected_to_server is False
            else not state.settings.sleep_mode
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


class NanitSwitch(NanitEntity, SwitchEntity):
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
        self._attr_is_on = None
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get(CONF_CAMERA_UID, coordinator.config_entry.entry_id)}"
            f"_{description.key}"
        )
        if coordinator.data is not None:
            self._attr_is_on = self.entity_description.value_fn(coordinator.data)

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        return self._attr_is_on

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is not None:
            self._attr_is_on = self.entity_description.value_fn(self.coordinator.data)
        else:
            self._attr_is_on = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        self._attr_is_on = True
        self.async_write_ha_state()
        await self.entity_description.turn_on_fn(self._camera)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.entity_description.turn_off_fn(self._camera)
