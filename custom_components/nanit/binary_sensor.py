"""Binary sensor platform for Nanit."""

from __future__ import annotations

import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit.models import CameraState, CloudEvent, ConnectionState

from . import NanitConfigEntry
from .const import CLOUD_EVENT_WINDOW
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitCloudEntity, NanitEntity, NanitSoundLightEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NanitBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Nanit binary sensor."""

    value_fn: Callable[[CameraState], bool | None]
    always_available: bool = False


BINARY_SENSORS: tuple[NanitBinarySensorEntityDescription, ...] = (
    NanitBinarySensorEntityDescription(
        key="connectivity",
        translation_key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.connection.state == ConnectionState.CONNECTED,
        always_available=True,
    ),
)


# --- Cloud-based binary sensors (motion + sound from Nanit cloud events) ---


@dataclass(frozen=True, kw_only=True)
class NanitCloudBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Nanit cloud binary sensor."""

    event_type: str  # "MOTION" or "SOUND" (uppercase, matches API)


CLOUD_BINARY_SENSORS: tuple[NanitCloudBinarySensorEntityDescription, ...] = (
    NanitCloudBinarySensorEntityDescription(
        key="cloud_motion",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=True,
        event_type="MOTION",
    ),
    NanitCloudBinarySensorEntityDescription(
        key="cloud_sound",
        translation_key="sound",
        device_class=BinarySensorDeviceClass.SOUND,
        entity_registry_enabled_default=True,
        event_type="SOUND",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit binary sensors for all cameras on the account."""
    entities: list[BinarySensorEntity] = []

    for cam_data in entry.runtime_data.cameras.values():
        for push_desc in BINARY_SENSORS:
            entities.append(NanitBinarySensor(cam_data.push_coordinator, push_desc))

        if cam_data.cloud_coordinator is not None:
            for cloud_desc in CLOUD_BINARY_SENSORS:
                entities.append(NanitCloudBinarySensor(cam_data.cloud_coordinator, cloud_desc))

        # Sound & Light connectivity sensor (optional)
        sl_coordinator = cam_data.sound_light_coordinator
        if sl_coordinator is not None:
            entities.append(NanitSLConnectivitySensor(sl_coordinator))

    async_add_entities(entities)


class NanitBinarySensor(NanitEntity, BinarySensorEntity):
    """Nanit binary sensor entity."""

    entity_description: NanitBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        description: NanitBinarySensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.camera.uid}_{description.key}"

    @property
    def available(self) -> bool:
        """Return entity availability.

        Entities flagged ``always_available`` (e.g. connectivity) stay
        available even when the camera is disconnected so they can
        report the disconnected state instead of going unavailable.
        """
        if self.entity_description.always_available:
            return self.coordinator.last_update_success and self.coordinator.data is not None
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NanitCloudBinarySensor(NanitCloudEntity, BinarySensorEntity):
    """Cloud-based binary sensor that detects motion/sound from Nanit cloud events.

    Polls the cloud API every 30s and checks for events within a 5-minute window.
    If an event of the matching type is found within the window, the sensor is ON.
    Otherwise it is OFF (automatically clears after 5 minutes from the last event).
    """

    entity_description: NanitCloudBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanitCloudCoordinator,
        description: NanitCloudBinarySensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.baby.camera_uid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return true if a matching event was found within the detection window."""
        if self.coordinator.data is None:
            return None

        events: list[CloudEvent] = self.coordinator.data
        if not events:
            return False

        now = time_mod.time()
        cutoff = now - CLOUD_EVENT_WINDOW
        target_type = self.entity_description.event_type

        for event in events:
            if event.event_type.upper() != target_type:
                continue
            if event.timestamp >= cutoff:
                return True

        return False


class NanitSLConnectivitySensor(NanitSoundLightEntity, BinarySensorEntity):
    """Connectivity binary sensor for the Sound & Light Machine."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "sl_connectivity"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        baby = coordinator.baby
        self._attr_unique_id = f"{baby.camera_uid}_sl_connectivity"

    @property
    def available(self) -> bool:
        """Always available so it can report disconnected state."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )

    @property
    def is_on(self) -> bool:
        """Return True when the S&L WebSocket is connected."""
        result: bool = self.coordinator.connected
        return result
