"""Sensor entities for Nanit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import LIGHT_LUX, PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

from aionanit.models import CameraState


@dataclass(frozen=True, kw_only=True)
class NanitSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit sensor."""

    value_fn: Callable[[CameraState], float | int | None]


SENSORS: tuple[NanitSensorEntityDescription, ...] = (
    NanitSensorEntityDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        value_fn=lambda state: state.sensors.temperature,
    ),
    NanitSensorEntityDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        suggested_display_precision=1,
        value_fn=lambda state: state.sensors.humidity,
    ),
    NanitSensorEntityDescription(
        key="light",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=LIGHT_LUX,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.sensors.light,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit sensors."""
    coordinator = entry.runtime_data.push_coordinator
    async_add_entities(
        NanitSensor(coordinator, description) for description in SENSORS
    )


class NanitSensor(NanitEntity, SensorEntity):
    """Nanit Sensor."""

    entity_description: NanitSensorEntityDescription

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        description: NanitSensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{description.key}"

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
