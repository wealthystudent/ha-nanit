"""Switch platform for Nanit."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
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


SWITCHES: tuple[NanitSwitchEntityDescription, ...] = (
    NanitSwitchEntityDescription(
        key="night_light",
        translation_key="night_light",
        entity_registry_enabled_default=True,
        value_fn=lambda data: data.get("control", {}).get("night_light") == "on",
        turn_on_fn=lambda client: client.set_night_light(True),
        turn_off_fn=lambda client: client.set_night_light(False),
    ),
    NanitSwitchEntityDescription(
        key="sleep_mode",
        translation_key="sleep_mode",
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("settings", {}).get("sleep_mode"),
        turn_on_fn=lambda client: client.set_sleep_mode(True),
        turn_off_fn=lambda client: client.set_sleep_mode(False),
    ),
    NanitSwitchEntityDescription(
        key="status_led",
        translation_key="status_led",
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("settings", {}).get("status_light_on"),
        turn_on_fn=lambda client: client.set_status_led(True),
        turn_off_fn=lambda client: client.set_status_led(False),
    ),
    NanitSwitchEntityDescription(
        key="mic_mute",
        translation_key="mic_mute",
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("settings", {}).get("mic_mute_on"),
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
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get('camera_uid', coordinator.config_entry.entry_id)}"
            f"_{description.key}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        await self.entity_description.turn_on_fn(self._client)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        await self.entity_description.turn_off_fn(self._client)
        await self.coordinator.async_request_refresh()
