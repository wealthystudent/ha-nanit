"""Base entities for Nanit."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    NanitCloudCoordinator,
    NanitNetworkCoordinator,
    NanitPushCoordinator,
    NanitSoundLightCoordinator,
)
from .device_links import HAS_DEVICE_ID_LINKS, async_get_device
from .sanitize import display_name


class NanitEntity(CoordinatorEntity[NanitPushCoordinator]):
    """Base entity for Nanit — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.camera.uid)},
            name=display_name(self.coordinator.baby.name, self.coordinator.baby.uid),
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
            name=display_name(self.coordinator.baby.name, self.coordinator.baby.uid),
            manufacturer="Nanit",
        )


class NanitSoundLightEntity(CoordinatorEntity[NanitSoundLightCoordinator]):
    """Base entity for the Nanit Sound & Light Machine — backed by the push coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info — its own device, keyed by the speaker's uid.

        Linked to the baby's camera via via_device only when a camera
        exists on the account (standalone speakers have none).
        """
        baby = self.coordinator.baby
        info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.sound_light.speaker_uid)},
            name=f"{display_name(baby.name, baby.uid)} Sound & Light",
            manufacturer="Nanit",
            model="Sound & Light Machine",
        )
        camera_uid = self.coordinator.via_camera_uid
        if not camera_uid:
            return info
        if not HAS_DEVICE_ID_LINKS:
            # HA before 2026.8 only knows the identifier form (see device_links).
            cast(dict[str, Any], info)["via_device"] = (DOMAIN, camera_uid)
            return info
        # via_device_id must name a registered device, or HA refuses the
        # entity. async_setup_entry registers every camera device before the
        # platforms run, so the lookup only misses if the camera is gone.
        if self.hass is not None:
            camera_device = async_get_device(
                dr.async_get(self.hass),
                (DOMAIN, camera_uid),
                self.coordinator.config_entry.entry_id,
            )
            if camera_device is not None:
                info["via_device_id"] = camera_device.id
        return info

    @property
    def available(self) -> bool:
        """Return True when the coordinator has data and the device is reachable.

        Mirrors the camera entities (and HA quality-scale guidance): when we
        can't talk to the device, entities go unavailable rather than showing
        stale values as live. The coordinator's `connected` flag is debounced
        by a grace period, so brief reconnects don't flash "Unavailable". The
        connectivity binary sensor and connection-mode sensor override this
        so they can keep reporting the disconnected state.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.connected
        )


class NanitNetworkEntity(CoordinatorEntity[NanitNetworkCoordinator]):
    """Base entity for network diagnostic sensors — backed by the network coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.baby.camera_uid)},
            name=display_name(self.coordinator.baby.name, self.coordinator.baby.uid),
            manufacturer="Nanit",
        )
