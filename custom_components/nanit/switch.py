"""Switch platform for Nanit."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .api import NanitApiClient
from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity


@dataclass(frozen=True, kw_only=True)
class NanitSwitchEntityDescription(SwitchEntityDescription):
    """Describe a Nanit switch."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    turn_on_fn: Callable[[NanitApiClient], Coroutine[Any, Any, None]]
    turn_off_fn: Callable[[NanitApiClient], Coroutine[Any, Any, None]]


def _connected_state(data: dict[str, Any]) -> bool | None:
    status = data.get("status")
    if not isinstance(status, dict):
        return None
    return status.get("connected")


def _night_light_value(data: dict[str, Any]) -> bool | None:
    if _connected_state(data) is False:
        return None
    value = data.get("control", {}).get("night_light")
    if value is None:
        return None
    return value == "on"


def _settings_flag(data: dict[str, Any], key: str) -> bool | None:
    if _connected_state(data) is False:
        return None
    value = data.get("settings", {}).get(key)
    if value is None:
        return None
    return value


SWITCHES: tuple[NanitSwitchEntityDescription, ...] = (
    NanitSwitchEntityDescription(
        key="night_light",
        translation_key="night_light",
        icon="mdi:lightbulb-night",
        entity_registry_enabled_default=True,
        value_fn=_night_light_value,
        turn_on_fn=lambda client: client.set_night_light(True),
        turn_off_fn=lambda client: client.set_night_light(False),
    ),
    NanitSwitchEntityDescription(
        key="camera_power",
        translation_key="camera_power",
        icon="mdi:power",
        entity_registry_enabled_default=True,
        value_fn=lambda data: (
            None
            if _connected_state(data) is False
            else not data.get("settings", {}).get("sleep_mode", False)
        ),
        turn_on_fn=lambda client: client.set_sleep_mode(False),
        turn_off_fn=lambda client: client.set_sleep_mode(True),
    ),
    NanitSwitchEntityDescription(
        key="status_led",
        translation_key="status_led",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _settings_flag(data, "status_light_on"),
        turn_on_fn=lambda client: client.set_status_led(True),
        turn_off_fn=lambda client: client.set_status_led(False),
    ),
    NanitSwitchEntityDescription(
        key="mic_mute",
        translation_key="mic_mute",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        value_fn=lambda data: _settings_flag(data, "mic_mute_on"),
        turn_on_fn=lambda client: client.set_mic_mute(True),
        turn_off_fn=lambda client: client.set_mic_mute(False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit switches."""
    coordinator = entry.runtime_data.local_coordinator
    client = entry.runtime_data.client
    async_add_entities(
        NanitSwitch(coordinator, client, description) for description in SWITCHES
    )


class NanitSwitch(NanitEntity, SwitchEntity):
    """Nanit switch entity."""

    entity_description: NanitSwitchEntityDescription

    def __init__(
        self,
        coordinator: NanitLocalCoordinator,
        client: NanitApiClient,
        description: NanitSwitchEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._client = client
        self._attr_is_on = None
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get('camera_uid', coordinator.config_entry.entry_id)}"
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
        await self.entity_description.turn_on_fn(self._client)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.entity_description.turn_off_fn(self._client)
        await self.coordinator.async_request_refresh()
