"""Sensor entities for Nanit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

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
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from aionanit.models import CameraState, CareLog, NetworkInfo

from . import NanitConfigEntry
from .aionanit_sl.models import SoundLightFullState
from .coordinator import (
    CARE_LOG_BOTTLE,
    CARE_LOG_DIAPER,
    CARE_LOG_NURSING,
    CARE_LOG_PUMPING,
    CareLogData,
    NanitCareLogCoordinator,
    NanitNetworkCoordinator,
    NanitPushCoordinator,
    NanitSoundLightCoordinator,
)
from .entity import NanitCareLogEntity, NanitEntity, NanitNetworkEntity, NanitSoundLightEntity

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


type CareLogAttributes = dict[str, str | float | int | None]

# Diaper change types the Nanit app offers, counted separately in attributes.
DIAPER_CHANGE_TYPES = ("pee", "poo", "mixed", "dry")


@dataclass(frozen=True, kw_only=True)
class NanitCareLogSensorDescription(SensorEntityDescription):
    """Description for Care Log sensors."""

    value_fn: Callable[[CareLogData], datetime | float | int | None]
    attrs_fn: Callable[[CareLogData], CareLogAttributes] | None = None
    # A total since local midnight: last_reset is the start of the day.
    daily: bool = False


def _time(log: CareLog | None) -> datetime | None:
    return dt_util.utc_from_timestamp(log.timestamp) if log is not None else None


def _minutes(seconds: float | None) -> float | None:
    return round(seconds / 60, 1) if seconds is not None else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _pumped_ml(log: CareLog) -> float | None:
    amounts = [a for a in (log.left_amount_ml, log.right_amount_ml) if a is not None]
    return sum(amounts) if amounts else None


def _nursing_s(log: CareLog) -> float:
    return (log.left_duration_s or 0.0) + (log.right_duration_s or 0.0)


def _today(data: CareLogData, *log_types: str) -> list[CareLog]:
    return [log for log in data.today if log.log_type in log_types]


def _last_feeding(data: CareLogData) -> CareLog | None:
    feeds = [log for t in (CARE_LOG_BOTTLE, CARE_LOG_NURSING) if (log := data.latest.get(t))]
    return max(feeds, key=lambda log: log.timestamp, default=None)


def _last_diaper_attrs(data: CareLogData) -> CareLogAttributes:
    log = data.latest.get(CARE_LOG_DIAPER)
    if log is None:
        return {}
    # Only the types the app offers: the value is API text shown to users.
    kind = log.change_type if log.change_type in DIAPER_CHANGE_TYPES else None
    return {"change_type": kind}


def _last_feeding_attrs(data: CareLogData) -> CareLogAttributes:
    log = _last_feeding(data)
    if log is None:
        return {}
    if log.log_type == CARE_LOG_BOTTLE:
        return {"feeding_type": "bottle", "amount_ml": _rounded(log.feed_amount_ml)}
    return {
        "feeding_type": "nursing",
        "left_minutes": _minutes(log.left_duration_s),
        "right_minutes": _minutes(log.right_duration_s),
    }


def _last_pumping_attrs(data: CareLogData) -> CareLogAttributes:
    log = data.latest.get(CARE_LOG_PUMPING)
    if log is None:
        return {}
    return {"amount_ml": _rounded(_pumped_ml(log)), "duration_minutes": _minutes(log.duration_s)}


def _diapers_today_attrs(data: CareLogData) -> CareLogAttributes:
    diapers = _today(data, CARE_LOG_DIAPER)
    return {kind: sum(log.change_type == kind for log in diapers) for kind in DIAPER_CHANGE_TYPES}


def _feedings_today_attrs(data: CareLogData) -> CareLogAttributes:
    return {
        "bottle": len(_today(data, CARE_LOG_BOTTLE)),
        "nursing": len(_today(data, CARE_LOG_NURSING)),
    }


CARE_LOG_SENSORS: tuple[NanitCareLogSensorDescription, ...] = (
    NanitCareLogSensorDescription(
        key="last_diaper_change",
        translation_key="last_diaper_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: _time(data.latest.get(CARE_LOG_DIAPER)),
        attrs_fn=_last_diaper_attrs,
    ),
    NanitCareLogSensorDescription(
        key="last_feeding",
        translation_key="last_feeding",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: _time(_last_feeding(data)),
        attrs_fn=_last_feeding_attrs,
    ),
    NanitCareLogSensorDescription(
        key="last_pumping",
        translation_key="last_pumping",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: _time(data.latest.get(CARE_LOG_PUMPING)),
        attrs_fn=_last_pumping_attrs,
    ),
    NanitCareLogSensorDescription(
        key="diaper_changes_today",
        translation_key="diaper_changes_today",
        state_class=SensorStateClass.TOTAL,
        daily=True,
        value_fn=lambda data: len(_today(data, CARE_LOG_DIAPER)),
        attrs_fn=_diapers_today_attrs,
    ),
    NanitCareLogSensorDescription(
        key="feedings_today",
        translation_key="feedings_today",
        state_class=SensorStateClass.TOTAL,
        daily=True,
        value_fn=lambda data: len(_today(data, CARE_LOG_BOTTLE, CARE_LOG_NURSING)),
        attrs_fn=_feedings_today_attrs,
    ),
    NanitCareLogSensorDescription(
        key="bottle_volume_today",
        translation_key="bottle_volume_today",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        daily=True,
        value_fn=lambda data: round(
            sum(log.feed_amount_ml or 0.0 for log in _today(data, CARE_LOG_BOTTLE)), 1
        ),
    ),
    NanitCareLogSensorDescription(
        key="nursing_time_today",
        translation_key="nursing_time_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        daily=True,
        value_fn=lambda data: round(
            sum(_nursing_s(log) for log in _today(data, CARE_LOG_NURSING)) / 60, 1
        ),
    ),
    NanitCareLogSensorDescription(
        key="pumped_volume_today",
        translation_key="pumped_volume_today",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        daily=True,
        value_fn=lambda data: round(
            sum(_pumped_ml(log) or 0.0 for log in _today(data, CARE_LOG_PUMPING)), 1
        ),
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

        # Care Log sensors (optional)
        care_coordinator = cam_data.care_log_coordinator
        if care_coordinator is not None:
            for care_description in CARE_LOG_SENSORS:
                entities.append(NanitCareLogSensor(care_coordinator, care_description))

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


class NanitCareLogSensor(NanitCareLogEntity, SensorEntity):
    """Sensor summarising the Care Logs parents keep in the Nanit app."""

    entity_description: NanitCareLogSensorDescription

    def __init__(
        self,
        coordinator: NanitCareLogCoordinator,
        description: NanitCareLogSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.baby.camera_uid}_{description.key}"
        # Not every family uses Care Logs. An account with no entries
        # in the first fetch's lookback gets these registered disabled, so
        # it isn't handed a set of sensors that never change.
        data = coordinator.data
        self._attr_entity_registry_enabled_default = data is not None and bool(data.latest)

    @property
    def native_value(self) -> datetime | float | int | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        """Return the start of the local day for daily totals."""
        if not self.entity_description.daily or self.coordinator.data is None:
            return None
        return self.coordinator.data.day_start

    @property
    def extra_state_attributes(self) -> CareLogAttributes | None:
        """Return details of the entry or the day's breakdown."""
        attrs_fn = self.entity_description.attrs_fn
        if attrs_fn is None or self.coordinator.data is None:
            return None
        return attrs_fn(self.coordinator.data)
