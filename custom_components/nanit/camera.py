"""Camera platform for Nanit."""

from __future__ import annotations

from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .api import NanitApiClient
from .const import CONF_BABY_NAME, CONF_CAMERA_UID, DOMAIN
from .coordinator import NanitLocalCoordinator
from .entity import NanitEntity


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
        """Return true if the camera is on."""
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.get("hls", {}).get("enabled", False)

    async def stream_source(self) -> str | None:
        """Return the HLS stream source."""
        if not self.is_streaming:
            return None
        return self._client.hls_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of camera image (not supported, stream only)."""
        return None

    async def async_turn_on(self) -> None:
        """Start HLS streaming."""
        await self._client.start_hls()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Stop HLS streaming."""
        await self._client.stop_hls()
        await self.coordinator.async_request_refresh()
