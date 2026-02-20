"""Camera platform for Nanit."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .api import NanitApiClient
from .const import CONF_BABY_NAME, CONF_CAMERA_UID, DOMAIN
from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit camera entity."""
    coordinator = entry.runtime_data.local_coordinator
    client = entry.runtime_data.client
    async_add_entities([NanitCamera(coordinator, client)])


class NanitCamera(NanitEntity, Camera):
    """Nanit camera entity."""

    _attr_translation_key = "camera"
    _attr_entity_registry_enabled_default = True
    _attr_supported_features = CameraEntityFeature.ON_OFF | CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: NanitLocalCoordinator,
        client: NanitApiClient,
    ) -> None:
        """Initialize."""
        NanitEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._client = client
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get('camera_uid', coordinator.config_entry.entry_id)}"
            "_camera"
        )

    @property
    def is_streaming(self) -> bool:
        """Return true if the camera is streaming."""
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.get("hls", {}).get("running", False)

    @property
    def is_on(self) -> bool:
        """Return true if the camera is on (not in sleep/standby mode)."""
        if self.coordinator.data is None:
            return False
        # sleep_mode=True means camera is OFF (standby), so invert
        sleep_mode = self.coordinator.data.get("settings", {}).get("sleep_mode")
        if sleep_mode is None:
            # Default to on if we haven't received settings yet
            return True
        return not sleep_mode

    async def stream_source(self) -> str | None:
        """Return the HLS stream source.

        Auto-starts HLS when the camera is on and the user opens the stream.
        """
        if not self.is_on:
            return None
        if not self.is_streaming:
            try:
                await self._client.start_hls()
                await self.coordinator.async_request_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to auto-start HLS stream", exc_info=True)
                return None
        return self._client.hls_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image from the camera."""
        if not self.is_on:
            return None
        try:
            return await self._client.get_snapshot()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to get camera snapshot", exc_info=True)
            return None

    async def async_turn_on(self) -> None:
        """Turn the camera on (disable sleep/standby mode)."""
        await self._client.set_sleep_mode(False)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn the camera off (enable sleep/standby mode)."""
        # Stop the HLS stream first if running
        if self.is_streaming:
            try:
                await self._client.stop_hls()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to stop HLS before sleep", exc_info=True)
        await self._client.set_sleep_mode(True)
        await self.coordinator.async_request_refresh()
