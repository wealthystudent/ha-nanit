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
from homeassistant.const import LIGHT_LUX, PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit.models import CameraState

from . import NanitConfigEntry
from .aionanit_sl.models import SoundLightFullState
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitSoundLightEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NanitSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit sensor."""

    value_fn: Callable[[CameraState], float | int | None]


@dataclass(frozen=True, kw_only=True)
class NanitSLSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit Sound & Light sensor."""

    value_fn: Callable[[SoundLightFullState], float | int | None]


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

SL_SENSORS: tuple[NanitSLSensorEntityDescription, ...] = (
    NanitSLSensorEntityDescription(
        key="sl_temperature",
        translation_key="sl_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        suggested_display_precision=1,
        value_fn=lambda state: (
            round(state.temperature_c, 2) if state.temperature_c is not None else None
        ),
    ),
    NanitSLSensorEntityDescription(
        key="sl_humidity",
        translation_key="sl_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        suggested_display_precision=1,
        value_fn=lambda state: (
            round(state.humidity_pct, 2) if state.humidity_pct is not None else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit sensors for all cameras on the account."""
    entities: list[SensorEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        for description in SENSORS:
            entities.append(NanitSensor(cam_data.push_coordinator, description))

        # Sound & Light Machine sensors (optional)
        sl_coordinator = cam_data.sound_light_coordinator
        if sl_coordinator is not None:
            for sl_description in SL_SENSORS:
                entities.append(NanitSLSensor(sl_coordinator, sl_description))
            entities.append(NanitSLConnectionModeSensor(sl_coordinator))

    async_add_entities(entities)


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
        self._attr_unique_id = f"{coordinator.camera.uid}_{description.key}"

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NanitSLSensor(NanitSoundLightEntity, SensorEntity):
    """Nanit Sound & Light Machine Sensor (temperature, humidity)."""

    entity_description: NanitSLSensorEntityDescription

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        description: NanitSLSensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        baby = coordinator.baby
        self._attr_unique_id = f"{baby.camera_uid}_{description.key}"

    @property
    def native_value(self) -> float | int | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NanitSLConnectionModeSensor(NanitSoundLightEntity, SensorEntity):
    """Diagnostic sensor showing S&L connection type: local, cloud, or unavailable."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "sl_connection_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["local", "cloud", "unavailable"]  # noqa: RUF012

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        baby = coordinator.baby
        self._attr_unique_id = f"{baby.camera_uid}_sl_connection_mode"

    @property
    def available(self) -> bool:
        """Always available so it can report the unavailable connection state."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self) -> str:
        """Return the current connection mode."""
        result: str = self.coordinator.sound_light.connection_mode
        return result
