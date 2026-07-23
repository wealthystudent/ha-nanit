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
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit.models import CameraState, NetworkInfo

from . import NanitConfigEntry
from .aionanit_sl.models import SoundLightFullState
from .coordinator import NanitNetworkCoordinator, NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitNetworkEntity, NanitSoundLightEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NanitSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit sensor."""

    value_fn: Callable[[CameraState], float | int | None]


@dataclass(frozen=True, kw_only=True)
class NanitSLSensorEntityDescription(SensorEntityDescription):
    """Description for Nanit Sound & Light sensor."""

    value_fn: Callable[[SoundLightFullState], str | float | int | None]


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
    NanitSLSensorEntityDescription(
        key="sl_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # The device reports a coarse 5-bucket state of charge, mapped to
        # representative percentages by the transport.
        value_fn=lambda state: state.battery_percent,
    ),
    NanitSLSensorEntityDescription(
        key="sl_firmware",
        translation_key="sl_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware_version,
    ),
)


@dataclass(frozen=True, kw_only=True)
class NanitNetworkSensorDescription(SensorEntityDescription):
    """Description for network diagnostic sensors."""

    value_fn: Callable[[NetworkInfo], str | int | None]


NETWORK_SENSORS: tuple[NanitNetworkSensorDescription, ...] = (
    NanitNetworkSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda net: net.ssid,
    ),
    NanitNetworkSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda net: net.signal_dbm,
    ),
    NanitNetworkSensorDescription(
        key="wifi_frequency",
        translation_key="wifi_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda net: net.frequency_mhz,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit sensors for all devices on the account."""
    entities: list[SensorEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        for description in SENSORS:
            entities.append(NanitSensor(cam_data.push_coordinator, description))

        # Network diagnostic sensors (optional)
        net_coordinator = cam_data.network_coordinator
        if net_coordinator is not None:
            for net_description in NETWORK_SENSORS:
                entities.append(NanitNetworkSensor(net_coordinator, net_description))

    # Sound & Light Machine sensors
    for speaker_data in entry.runtime_data.speakers.values():
        for sl_description in SL_SENSORS:
            entities.append(NanitSLSensor(speaker_data.coordinator, sl_description))
        entities.append(NanitSLConnectionModeSensor(speaker_data.coordinator))
        entities.append(NanitSLWifiSensor(speaker_data.coordinator))

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
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_{description.key}"

    @property
    def native_value(self) -> str | float | int | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NanitSLWifiSensor(NanitSoundLightEntity, SensorEntity):
    """WiFi signal-strength sensor for the S&L, SSID/BSSID/channel as attrs.

    Diagnostic and registry-disabled by default, matching the pattern from
    nanit-sound-light: RSSI updates every poll cycle, so keeping it off by
    default avoids recorder churn for users who don't care.
    """

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "sl_wifi_signal"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sl_wifi_signal"

    @property
    def native_value(self) -> int | None:
        """Return the WiFi RSSI in dBm."""
        if self.coordinator.data is None:
            return None
        result: int | None = self.coordinator.data.wifi_rssi
        return result

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Return SSID / BSSID / channel as attributes."""
        state = self.coordinator.data
        if state is None:
            return {}
        return {
            "ssid": state.wifi_ssid,
            "bssid": state.wifi_bssid,
            "channel": state.wifi_channel,
        }


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
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sl_connection_mode"

    @property
    def available(self) -> bool:
        """Always available so it can report the unavailable connection state."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self) -> str:
        """Return the current connection mode."""
        result: str = self.coordinator.sound_light.connection_mode
        return result


class NanitNetworkSensor(NanitNetworkEntity, SensorEntity):
    """Diagnostic sensor for camera WiFi network information."""

    entity_description: NanitNetworkSensorDescription

    def __init__(
        self,
        coordinator: NanitNetworkCoordinator,
        description: NanitNetworkSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.baby.camera_uid}_{description.key}"

    @property
    def native_value(self) -> str | int | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
