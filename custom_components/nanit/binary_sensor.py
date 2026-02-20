"""Binary sensor platform for Nanit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity


@dataclass(frozen=True, kw_only=True)
class NanitBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Nanit binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[NanitBinarySensorEntityDescription, ...] = (
    NanitBinarySensorEntityDescription(
        key="motion",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=True,
        value_fn=lambda data: data.get("sensors", {}).get("motion", {}).get("is_alert"),
    ),
    NanitBinarySensorEntityDescription(
        key="sound",
        translation_key="sound",
        device_class=BinarySensorDeviceClass.SOUND,
        entity_registry_enabled_default=True,
        value_fn=lambda data: data.get("sensors", {}).get("sound", {}).get("is_alert"),
    ),
    NanitBinarySensorEntityDescription(
        key="night_mode",
        translation_key="night_mode",
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            (v := data.get("sensors", {}).get("night", {}).get("value")) is not None
            and v > 0
        ),
    ),
    NanitBinarySensorEntityDescription(
        key="connectivity",
        translation_key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("status", {}).get("connected"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit binary sensors."""
    coordinator = entry.runtime_data.local_coordinator
    async_add_entities(
        NanitBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class NanitBinarySensor(NanitEntity, BinarySensorEntity):
    """Nanit binary sensor entity."""

    entity_description: NanitBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanitLocalCoordinator,
        description: NanitBinarySensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get('camera_uid', coordinator.config_entry.entry_id)}"
            f"_{description.key}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
