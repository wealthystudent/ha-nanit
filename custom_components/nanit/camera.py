"""Camera platform for Nanit."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

from aionanit import NanitCamera

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit camera entity."""
    push_coordinator = entry.runtime_data.push_coordinator
    camera = entry.runtime_data.camera
    async_add_entities([NanitCameraEntity(push_coordinator, camera)])


class NanitCameraEntity(NanitEntity, Camera):
    """Nanit camera entity — stream via RTMPS, snapshots from cloud."""

    _attr_translation_key = "camera"
    _attr_entity_registry_enabled_default = True
    _attr_supported_features = CameraEntityFeature.ON_OFF | CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
    ) -> None:
        """Initialize."""
        NanitEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._camera = camera
        camera_uid = coordinator.config_entry.data.get(
            CONF_CAMERA_UID, coordinator.config_entry.entry_id
        )
        self._attr_unique_id = f"{camera_uid}_camera"

    @property
    def is_on(self) -> bool:
        """Return true if the camera is on (not in sleep/standby mode)."""
        if self.coordinator.data is None:
            return True
        sleep_mode = self.coordinator.data.settings.sleep_mode
        if sleep_mode is None:
            return True
        return not sleep_mode

    async def stream_source(self) -> str | None:
        """Return the RTMPS stream URL.

        The RTMPS URL embeds a fresh access token and can be consumed directly
        by the HA Stream integration.
        """
        if not self.is_on:
            return None
        try:
            url = await self._camera.async_get_stream_rtmps_url()
            await self._camera.async_start_streaming()
            return url
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to get RTMPS stream URL", exc_info=True)
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image from the camera."""
        if not self.is_on:
            return None
        try:
            return await self._camera.async_get_snapshot()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to get camera snapshot", exc_info=True)
            return None

    async def async_turn_on(self) -> None:
        """Turn the camera on (disable sleep/standby mode)."""
        await self._camera.async_set_settings(sleep_mode=False)

    async def async_turn_off(self) -> None:
        """Turn the camera off (enable sleep/standby mode)."""
        try:
            await self._camera.async_stop_streaming()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to stop streaming before sleep", exc_info=True)
        await self._camera.async_set_settings(sleep_mode=True)
