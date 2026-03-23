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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NanitConfigEntry
from .const import CLOUD_EVENT_WINDOW, DOMAIN
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator
from .entity import NanitEntity

from aionanit.models import CameraState, CloudEvent, ConnectionState


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
        # Local binary sensors (from camera WebSocket push)
        for description in BINARY_SENSORS:
            entities.append(
                NanitBinarySensor(cam_data.push_coordinator, description)
            )

        # Cloud binary sensors (from Nanit cloud events API)
        if cam_data.cloud_coordinator is not None:
            for description in CLOUD_BINARY_SENSORS:
                entities.append(
                    NanitCloudBinarySensor(
                        cam_data.cloud_coordinator, description
                    )
                )

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
            return (
                self.coordinator.last_update_success
                and self.coordinator.data is not None
            )
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class NanitCloudBinarySensor(
    CoordinatorEntity[NanitCloudCoordinator], BinarySensorEntity
):
    """Cloud-based binary sensor that detects motion/sound from Nanit cloud events.

    Polls the cloud API every 30s and checks for events within a 5-minute window.
    If an event of the matching type is found within the window, the sensor is ON.
    Otherwise it is OFF (automatically clears after 5 minutes from the last event).
    """

    _attr_has_entity_name = True
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
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.baby.camera_uid)},
            name=self.coordinator.baby.name,
            manufacturer="Nanit",
        )

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
