"""Base entity for Nanit."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BABY_NAME, CONF_CAMERA_UID, DOMAIN
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator


class NanitEntity(CoordinatorEntity[NanitPushCoordinator]):
    """Base entity for Nanit — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.data[CONF_CAMERA_UID])},
            name=self.coordinator.config_entry.data[CONF_BABY_NAME],
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


class NanitSoundLightEntity(CoordinatorEntity[NanitSoundLightCoordinator]):
    """Base entity for the Nanit Sound & Light Machine — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info — separate device from the camera."""
        camera_uid = self.coordinator.config_entry.data[CONF_CAMERA_UID]
        baby_name = self.coordinator.config_entry.data[CONF_BABY_NAME]
        return DeviceInfo(
            identifiers={(DOMAIN, f"{camera_uid}_sound_light")},
            name=f"{baby_name} Sound & Light",
            manufacturer="Nanit",
            model="Sound & Light Machine",
            via_device=(DOMAIN, camera_uid),
        )

    @property
    def available(self) -> bool:
        """Return True when the coordinator has data and S&L device is connected."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.connected
        )
