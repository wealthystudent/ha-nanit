"""Base entity for Nanit."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BABY_NAME, CONF_CAMERA_UID, DOMAIN
from .coordinator import NanitLocalCoordinator


class NanitEntity(CoordinatorEntity[NanitLocalCoordinator]):
    """Base entity for Nanit."""

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
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )
