"""Camera platform for Nanit."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit import NanitCamera

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_STREAM_START_ATTEMPTS = 3
_STREAM_RETRY_DELAY = 2.0
_SNAPSHOT_CACHE_TTL = 60.0
_SNAPSHOT_PREFETCH_AGE = 30.0


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
        self._cached_snapshot: bytes | None = None
        self._cached_snapshot_at: float = 0.0

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

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_source(self) -> str | None:
        """Return the RTMPS stream URL.

        Sends PUT_STREAMING *before* returning the URL so the camera is
        already pushing to the RTMPS ingest when HA opens the connection.
        This eliminates the race condition where HA tries to connect before
        the camera has started streaming.
        """
        if not self.is_on:
            return None

        if not await self._async_start_streaming_safe():
            return None

        try:
            return await self._camera.async_get_stream_rtmps_url()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to build RTMPS stream URL", exc_info=True)
            return None

    async def _async_start_streaming_safe(self) -> bool:
        """Send PUT_STREAMING with retry.  Returns True on success."""
        for attempt in range(1, _STREAM_START_ATTEMPTS + 1):
            try:
                await self._camera.async_start_streaming()
                return True
            except Exception:  # noqa: BLE001
                if attempt < _STREAM_START_ATTEMPTS:
                    _LOGGER.debug(
                        "PUT_STREAMING attempt %d/%d failed for camera %s, retrying in %.0fs",
                        attempt,
                        _STREAM_START_ATTEMPTS,
                        self._camera.uid,
                        _STREAM_RETRY_DELAY,
                    )
                    await asyncio.sleep(_STREAM_RETRY_DELAY)
                else:
                    _LOGGER.warning(
                        "PUT_STREAMING failed after %d attempts for camera %s",
                        _STREAM_START_ATTEMPTS,
                        self._camera.uid,
                        exc_info=True,
                    )
        return False

    # ------------------------------------------------------------------
    # Snapshot (with caching)
    # ------------------------------------------------------------------

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image, using a cached snapshot when possible.

        Cache strategy:
        - Fresh cache (< TTL): return immediately.
        - Stale cache (> TTL): attempt a fresh fetch; return stale on failure.
        - No cache: fetch synchronously.

        A background prefetch is scheduled when the cache reaches
        ``_SNAPSHOT_PREFETCH_AGE`` so subsequent requests hit a warm cache.
        """
        if not self.is_on:
            return None

        now = time.monotonic()
        cache_age = now - self._cached_snapshot_at

        if self._cached_snapshot is not None and cache_age < _SNAPSHOT_CACHE_TTL:
            if cache_age >= _SNAPSHOT_PREFETCH_AGE:
                self.hass.async_create_background_task(
                    self._async_refresh_snapshot(),
                    name=f"nanit_snapshot_refresh_{self._camera.uid}",
                )
            return self._cached_snapshot

        fresh = await self._async_fetch_snapshot()
        if fresh is not None:
            return fresh

        return self._cached_snapshot

    async def _async_refresh_snapshot(self) -> None:
        """Background task: update the snapshot cache without blocking callers."""
        await self._async_fetch_snapshot()

    async def _async_fetch_snapshot(self) -> bytes | None:
        """Fetch a snapshot from the cloud and update the cache."""
        try:
            image = await self._camera.async_get_snapshot()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to fetch snapshot for %s", self._camera.uid)
            return None
        if image is not None:
            self._cached_snapshot = image
            self._cached_snapshot_at = time.monotonic()
        return image

    # ------------------------------------------------------------------
    # On/off
    # ------------------------------------------------------------------

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
