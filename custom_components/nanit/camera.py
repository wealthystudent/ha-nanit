"""Camera platform for Nanit."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit import NanitCamera

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit camera entities for all cameras on the account."""
    async_add_entities(
        NanitCameraEntity(cam_data.push_coordinator, cam_data.camera)
        for cam_data in entry.runtime_data.cameras.values()
    )


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
        super().__init__(coordinator)
        Camera.__init__(self)
        self._camera = camera
        self._prev_is_on: bool | None = None
        self._attr_unique_id = f"{camera.uid}_camera"

    @property
    def is_on(self) -> bool:
        """Return true if the camera is on (not in sleep/standby mode)."""
        if self.coordinator.data is None:
            return True
        sleep_mode = self.coordinator.data.settings.sleep_mode
        if sleep_mode is None:
            return True
        return not sleep_mode

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        cur_on = self.is_on
        prev_on = self._prev_is_on
        self._prev_is_on = cur_on

        if prev_on is not None and prev_on != cur_on:
            # Camera power changed — invalidate cached stream.
            self._invalidate_stream()

        super()._handle_coordinator_update()

    def _invalidate_stream(self) -> None:
        """Discard HA's cached stream so a fresh one is created on next view."""
        if self.stream is not None:
            _LOGGER.debug("Invalidating cached stream after power state change")
            self.stream = None

    async def stream_source(self) -> str | None:
        """Return the RTMPS stream URL.

        The RTMPS URL embeds a fresh access token and can be consumed directly
        by the HA Stream integration.  PUT_STREAMING is fired in the background
        so the URL is returned immediately without waiting for the camera ACK.
        """
        if not self.is_on:
            return None
        try:
            url: str = await self._camera.async_get_stream_rtmps_url()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to build RTMPS stream URL", exc_info=True)
            return None

        self.hass.async_create_background_task(
            self._async_start_streaming_safe(),
            name=f"nanit_start_streaming_{self._camera.uid}",
        )
        return url

    async def _async_start_streaming_safe(self) -> None:
        """Send PUT_STREAMING, logging failures without raising."""
        try:
            await self._camera.async_start_streaming()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "PUT_STREAMING failed for camera %s — the stream may not load. "
                "Check debug logs for details",
                self._camera.uid,
                exc_info=True,
            )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image from the camera."""
        if not self.is_on:
            return None
        try:
            image: bytes | None = await self._camera.async_get_snapshot()
            return image
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to get camera snapshot", exc_info=True)
            return None

    async def async_turn_on(self) -> None:
        """Turn the camera on (disable sleep/standby mode)."""
        self._invalidate_stream()
        await self._camera.async_set_settings(sleep_mode=False)

    async def async_turn_off(self) -> None:
        """Turn the camera off (enable sleep/standby mode)."""
        self._invalidate_stream()
        try:
            await self._camera.async_stop_streaming()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to stop streaming before sleep", exc_info=True)
        await self._camera.async_set_settings(sleep_mode=True)
