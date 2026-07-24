"""Camera platform for Nanit."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_call_later

from aionanit import NanitCamera
from aionanit.models import ConnectionState

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_STREAM_START_ATTEMPTS = 3
_STREAM_RETRY_DELAY = 2.0
_STREAM_SOURCE_MAX_AGE = 45 * 60
# Nanit stops the camera's RTMPS push ~20 minutes after the last
# PUT_STREAMING; keepalives must land well inside that window.
_STREAM_KEEPALIVE_INTERVAL = 5 * 60
_STREAM_STOP_TIMEOUT = 5.0
_SNAPSHOT_CACHE_TTL = 60.0
_SNAPSHOT_PREFETCH_AGE = 30.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit camera entities for all cameras on the account."""
    try:
        platform = async_get_current_platform()
    except RuntimeError:
        platform = None
    if platform is not None:
        platform.async_register_entity_service(
            "reset_stream",
            {},
            "async_reset_stream",
        )
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
        self._prev_conn_state: ConnectionState | None = None
        self._attr_unique_id = f"{camera.uid}_camera"
        self._cached_snapshot: bytes | None = None
        self._cached_snapshot_at: float = 0.0
        self._cached_stream_source: str | None = None
        self._stream_source_started_at: float = 0.0
        self._cancel_stream_expiry_timer: CALLBACK_TYPE | None = None
        self._cancel_stream_keepalive_timer: CALLBACK_TYPE | None = None
        self._stream_refresh_task: asyncio.Task[None] | None = None
        self._stream_keepalive_task: asyncio.Task[bool] | None = None

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

        data = self.coordinator.data
        conn_state = data.connection.state if data is not None else None
        prev_conn_state = self._prev_conn_state
        self._prev_conn_state = conn_state

        if prev_on is not None and prev_on != cur_on:
            # Camera power changed — invalidate cached stream.
            self._invalidate_stream("power state change")
        else:
            self._invalidate_stream_if_expired()

        if (
            conn_state is ConnectionState.CONNECTED
            and prev_conn_state is not None
            and prev_conn_state is not ConnectionState.CONNECTED
        ):
            # The camera's RTMPS push dies with its control session (e.g. the
            # pre-emptive token-refresh reconnect) and aionanit's reconnect
            # handler does not re-send PUT_STREAMING. Resume the push now so
            # watched streams recover well inside HA's 30s demux timeout.
            self._handle_stream_keepalive()

        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Tear down stream bookkeeping so nothing outlives the entity.

        The keepalive timer reschedules itself indefinitely; without this it
        survives a config-entry reload and keeps firing on the removed
        entity — re-sending PUT_STREAMING with a stale URL through the old
        (stopped) camera, which resurrects its WebSocket and redirects the
        camera's push away from the replacement entity's stream.
        """
        self._invalidate_stream("entity removal")
        for task in (self._stream_refresh_task, self._stream_keepalive_task):
            if task is not None and not task.done():
                task.cancel()
        self._stream_refresh_task = None
        self._stream_keepalive_task = None
        await super().async_will_remove_from_hass()

    def _invalidate_stream(self, reason: str = "state change") -> None:
        """Stop and discard HA's cached stream so a fresh one can be created."""
        if self.stream is not None:
            old_stream = self.stream
            _LOGGER.debug("Invalidating cached stream after %s", reason)
            # Dropping the reference alone leaves HA's decode worker alive until
            # its normal idle cleanup. Stop it before replacing the cached stream
            # to prevent overlapping workers during frontend recovery.
            if self.hass is not None:
                self.hass.create_task(
                    self._stop_discarded_stream(old_stream),
                    name=f"nanit_stop_discarded_stream_{self._camera.uid}",
                )
            else:
                _LOGGER.debug("Cannot stop discarded Nanit stream before HA attach")
            self.stream = None
        self._cached_stream_source = None
        self._stream_source_started_at = 0.0
        self._cancel_stream_timers()

    async def async_reset_stream(self) -> None:
        """Reset HA's cached Nanit stream so the next viewer gets a fresh RTMPS URL."""
        await self._async_invalidate_stream("frontend recovery request")

    async def _async_invalidate_stream(self, reason: str) -> None:
        """Discard the cached stream, then stop it.

        The cache slot is cleared before the (potentially multi-second)
        stop so a viewer connecting mid-stop gets a fresh stream instead
        of the dying one.
        """
        old_stream = self.stream
        if old_stream is not None:
            _LOGGER.debug("Invalidating cached stream after %s", reason)
            self.stream = None
            await self._stop_discarded_stream(old_stream)
        self._cached_stream_source = None
        self._stream_source_started_at = 0.0
        self._cancel_stream_timers()

    async def _stop_discarded_stream(self, stream: Any) -> None:
        """Best-effort cleanup for a stream removed from HA's cache."""
        try:
            async with asyncio.timeout(_STREAM_STOP_TIMEOUT):
                await stream.stop()
        except TimeoutError:
            _LOGGER.warning(
                "Timed out after %.0fs stopping discarded Nanit stream",
                _STREAM_STOP_TIMEOUT,
            )
        except Exception:
            _LOGGER.debug("Failed to stop discarded Nanit stream", exc_info=True)

    def _invalidate_stream_if_expired(self) -> None:
        """Renew or release the source shortly before its RTMPS token expires."""
        if self._stream_source_started_at == 0.0:
            return

        stream_age = time.monotonic() - self._stream_source_started_at
        if stream_age >= _STREAM_SOURCE_MAX_AGE:
            self._refresh_or_expire_stream_source(f"stream source age {stream_age:.0f}s")

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session, ignoring duplicate go2rtc close callbacks."""
        try:
            super().close_webrtc_session(session_id)
        except KeyError:
            _LOGGER.debug("WebRTC session %s was already closed", session_id)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_source(self) -> str | None:
        """Return the RTMPS stream URL.

        Sends PUT_STREAMING *before* returning the URL so the camera is
        already pushing to the RTMPS ingest when HA opens the connection.
        This eliminates the race condition where HA tries to connect before
        the camera has started streaming.

        The URL embeds the access token, so a freshly built URL is a
        *different* source string. HA/go2rtc treat a changed source as a new
        stream and tear down the warm producer — several seconds of black
        video for viewers. To keep the source stable, the URL is cached and
        reused for repeat calls (card reloads, extra viewers, WebRTC
        renegotiation) until the existing expiry window lapses.
        """
        if not self.is_on:
            return None

        source = self._cached_stream_source
        source_age = time.monotonic() - self._stream_source_started_at
        if (
            source is None
            or self._stream_source_started_at == 0.0
            or (source_age >= _STREAM_SOURCE_MAX_AGE)
        ):
            try:
                source = await self._camera.async_get_stream_rtmps_url()
            except Exception:
                _LOGGER.warning("Failed to build RTMPS stream URL", exc_info=True)
                return None

            if not await self._async_start_streaming_safe(source):
                return None

            self._cached_stream_source = source
            self._stream_source_started_at = time.monotonic()
            self._schedule_stream_expiry_timer()
            self._schedule_stream_keepalive_timer()
            return source

        # Cached URL is still valid — re-send PUT_STREAMING with the same URL
        # so a camera that lapsed while unwatched resumes pushing, without
        # changing the source string HA already consumes.
        _LOGGER.debug(
            "Reusing cached RTMPS stream URL for camera %s (age %.0fs)",
            self._camera.uid,
            source_age,
        )
        if not await self._async_start_streaming_safe(source):
            return None
        # PUT_STREAMING just went out — restart the keepalive countdown.
        self._schedule_stream_keepalive_timer()
        return source

    def _schedule_stream_expiry_timer(self, delay: float = _STREAM_SOURCE_MAX_AGE) -> None:
        """Schedule backend stream invalidation so token expiry is not update-dependent."""
        if self._cancel_stream_expiry_timer is not None:
            self._cancel_stream_expiry_timer()
        try:
            hass = self.hass
        except (AttributeError, RuntimeError):
            self._cancel_stream_expiry_timer = None
            return

        if hass is None:
            self._cancel_stream_expiry_timer = None
            return

        self._cancel_stream_expiry_timer = async_call_later(
            hass,
            delay,
            self._handle_stream_expiry,
        )

    def _schedule_stream_keepalive_timer(self) -> None:
        """Schedule the next PUT_STREAMING keepalive for the cached source."""
        if self._cancel_stream_keepalive_timer is not None:
            self._cancel_stream_keepalive_timer()
        try:
            hass = self.hass
        except (AttributeError, RuntimeError):
            self._cancel_stream_keepalive_timer = None
            return

        if hass is None:
            self._cancel_stream_keepalive_timer = None
            return

        self._cancel_stream_keepalive_timer = async_call_later(
            hass,
            _STREAM_KEEPALIVE_INTERVAL,
            self._handle_stream_keepalive,
        )

    @callback
    def _handle_stream_keepalive(self, _now: object = None) -> None:
        """Re-send PUT_STREAMING so the camera's push outlives Nanit's session.

        The camera stops pushing to the RTMPS ingest roughly 20 minutes
        after the last PUT_STREAMING. Without a keepalive, every
        continuously watched stream starves at that mark, trips HA's 30s
        demux timeout ("Immediate exit requested"), and drops frames until
        frontend recovery re-requests the stream. Only streams with active
        consumers are kept alive — an idle camera is left to lapse.

        Also invoked directly on reconnect transitions to resume the push
        immediately; the pending timer is cancelled so it cannot double up.

        The send is best-effort (no reconnect-on-failure): forcing a control
        reconnect on a late ACK would itself kill the RTMPS push and re-enter
        this handler on the reconnect transition — an endless teardown loop.
        A genuinely dead session is still recovered by aionanit's periodic
        health check, whose reconnect lands back here to resume the push.
        """
        if self._cancel_stream_keepalive_timer is not None:
            self._cancel_stream_keepalive_timer()
            self._cancel_stream_keepalive_timer = None
        source = self._cached_stream_source
        if source is None:
            return
        stream = self.stream
        if (
            self.is_on
            and stream is not None
            and stream.outputs()
            and (self._stream_keepalive_task is None or self._stream_keepalive_task.done())
        ):
            self._stream_keepalive_task = self.hass.async_create_task(
                self._async_start_streaming_safe(source, reconnect_on_failure=False),
                name=f"nanit_stream_keepalive_{self._camera.uid}",
            )
        self._schedule_stream_keepalive_timer()

    @callback
    def _handle_stream_expiry(self, _now: object = None) -> None:
        """Renew or release the cached source before its token expires."""
        self._cancel_stream_expiry_timer = None
        self._refresh_or_expire_stream_source("stream source age timer")

    def _expire_stream_source(self) -> None:
        """Drop the cached source URL without touching a running stream."""
        self._cached_stream_source = None
        self._stream_source_started_at = 0.0
        self._cancel_stream_timers()

    @callback
    def _cancel_stream_timers(self) -> None:
        """Cancel the expiry and keepalive timers for the cached source."""
        if self._cancel_stream_expiry_timer is not None:
            self._cancel_stream_expiry_timer()
            self._cancel_stream_expiry_timer = None
        if self._cancel_stream_keepalive_timer is not None:
            self._cancel_stream_keepalive_timer()
            self._cancel_stream_keepalive_timer = None

    @callback
    def _refresh_or_expire_stream_source(self, reason: str) -> None:
        """Route an aging source to in-place renewal or cache discard."""
        if self._stream_refresh_task is not None and not self._stream_refresh_task.done():
            return
        hass = getattr(self, "hass", None)
        if hass is None:
            self._expire_stream_source()
            return
        self._stream_refresh_task = hass.async_create_task(
            self._async_refresh_stream_source(reason),
            name=f"nanit_stream_source_refresh_{self._camera.uid}",
        )

    async def _async_refresh_stream_source(self, reason: str) -> None:
        """Renew the RTMPS source URL as its embedded token ages out.

        A stream with active consumers is rotated in place via
        Stream.update_source() — stopping it would blank every open card
        mid-viewing and set off the frontend recovery cascade. Without
        consumers the stream and cache are discarded so the next viewer
        starts fresh.
        """
        stream = self.stream
        if self.is_on and stream is not None and stream.outputs():
            try:
                source = await self._camera.async_get_stream_rtmps_url()
            except Exception:
                # Leave the running stream alone — playing until the token
                # actually dies beats killing it because renewal failed.
                _LOGGER.warning("Failed to renew RTMPS stream URL", exc_info=True)
                self._expire_stream_source()
                return
            if not await self._async_start_streaming_safe(source):
                self._expire_stream_source()
                return
            _LOGGER.debug(
                "Rotating RTMPS source in place for camera %s after %s",
                self._camera.uid,
                reason,
            )
            self._cached_stream_source = source
            self._stream_source_started_at = time.monotonic()
            self._schedule_stream_expiry_timer()
            self._schedule_stream_keepalive_timer()
            stream.update_source(source)
            return

        # No active consumers — discard so the next viewer starts fresh.
        self._invalidate_stream(reason)

    async def _async_start_streaming_safe(
        self,
        rtmps_url: str | None = None,
        *,
        reconnect_on_failure: bool = True,
    ) -> bool:
        """Send PUT_STREAMING with retry.  Returns True on success.

        ``reconnect_on_failure=False`` makes each send best-effort: aionanit
        will not force-reconnect the control WebSocket when the ACK is late.
        Background keepalives must use this — a control-session reconnect
        stops the camera's RTMPS push, so a keepalive that reconnects on a
        slow ACK tears down the very stream it is keeping alive and loops
        (reconnect → resume PUT_STREAMING → slow ACK → reconnect …).
        """
        for attempt in range(1, _STREAM_START_ATTEMPTS + 1):
            try:
                await self._async_send_put_streaming(rtmps_url, reconnect_on_failure)
                return True
            except Exception:
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

    async def _async_send_put_streaming(
        self, rtmps_url: str | None, reconnect_on_failure: bool
    ) -> None:
        """Send one PUT_STREAMING, degrading gracefully on older aionanit wheels."""
        if not reconnect_on_failure:
            try:
                await self._camera.async_start_streaming(
                    rtmps_url=rtmps_url, reconnect_on_failure=False
                )
                return
            except TypeError as err:
                if "reconnect_on_failure" not in str(err):
                    raise
                _LOGGER.debug(
                    "Nanit client does not support best-effort PUT_STREAMING; "
                    "falling back to default send for camera %s",
                    self._camera.uid,
                )
        try:
            await self._camera.async_start_streaming(rtmps_url=rtmps_url)
        except TypeError as err:
            if "rtmps_url" not in str(err):
                raise
            _LOGGER.debug(
                "Nanit client does not support explicit RTMPS URL reuse; "
                "falling back to legacy stream start for camera %s",
                self._camera.uid,
            )
            await self._camera.async_start_streaming()

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
        except Exception:
            _LOGGER.debug("Failed to stop streaming before sleep", exc_info=True)
        await self._camera.async_set_settings(sleep_mode=True)
