"""Sensor entities for Nanit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfIlluminance, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity


@dataclass(frozen=True, kw_only=True)
class NanitSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit sensor."""
    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[NanitSensorEntityDescription, ...] = (
    NanitSensorEntityDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("temperature", {}).get("value"),
    ),
    NanitSensorEntityDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("humidity", {}).get("value"),
    ),
    NanitSensorEntityDescription(
        key="light",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=UnitOfIlluminance.LUX,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("light", {}).get("value"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit sensors."""
    coordinator: NanitLocalCoordinator = entry.runtime_data.local_coordinator
    async_add_entities(
        NanitSensor(coordinator, description) for description in SENSORS
    )


class NanitSensor(NanitEntity, SensorEntity):
    """Nanit Sensor."""

    entity_description: NanitSensorEntityDescription

    def __init__(
        self,
        coordinator: NanitLocalCoordinator,
        description: NanitSensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if not self.coordinator.data or "sensors" not in self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data["sensors"])
