"""Base entities for Nanit."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator, NanitSoundLightCoordinator


class NanitEntity(CoordinatorEntity[NanitPushCoordinator]):
    """Base entity for Nanit — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.camera.uid)},
            name=self.coordinator.baby.name,
            manufacturer="Nanit",
        )

    @property
    def available(self) -> bool:
        """Return True when the coordinator has data and camera is connected.

        Follows the Shelly pattern: both last_update_success and the WS
        connection flag must be True.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.connected
        )


class NanitCloudEntity(CoordinatorEntity[NanitCloudCoordinator]):
    """Base entity for Nanit cloud-polled data."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.baby.camera_uid)},
            name=self.coordinator.baby.name,
            manufacturer="Nanit",
        )


class NanitSoundLightEntity(CoordinatorEntity[NanitSoundLightCoordinator]):
    """Base entity for the Nanit Sound & Light Machine — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info — separate device from the camera."""
        baby = self.coordinator.baby
        return DeviceInfo(
            identifiers={(DOMAIN, f"{baby.camera_uid}_sound_light")},
            name=f"{baby.name} Sound & Light",
            manufacturer="Nanit",
            model="Sound & Light Machine",
            via_device=(DOMAIN, baby.camera_uid),
        )

    @property
    def available(self) -> bool:
        """Return True when the coordinator has received data.

        Does NOT require an active connection — entities retain their last
        known values when the WebSocket drops. Connection state is reported
        separately by the S&L connectivity binary sensor.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )
