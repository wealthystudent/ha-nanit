"""Camera platform for Nanit."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature, StreamType
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_call_later
from webrtc_models import RTCIceCandidateInit

from aionanit import NanitCamera
from aionanit.models import ConnectionState

from . import NanitConfigEntry
from .const import DOMAIN, is_read_only
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity
from .log_redaction import async_attach_to_camera_stream, async_detach_from_camera_stream

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_STREAM_START_ATTEMPTS = 3
_STREAM_RETRY_DELAY = 2.0
_STREAM_SOURCE_MAX_AGE = 45 * 60
# Rotation is deferred while only WebRTC viewers are open, but never past this
# age: URLs are built from a token with >= 55 min left, so a session that was
# never closed must not keep an expired URL cached for new viewers.
_STREAM_ROTATION_DEFER_LIMIT = 55 * 60
# Nanit stops the camera's RTMPS push ~20 minutes after the last
# PUT_STREAMING; keepalives must land well inside that window.
_STREAM_KEEPALIVE_INTERVAL = 5 * 60
# How long stream_source() waits for the PUT_STREAMING ACK before returning
# the URL anyway. Must stay below HA's 10s CAMERA_STREAM_SOURCE_TIMEOUT.
_STREAM_START_WAIT = 8.0
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
    read_only = is_read_only(entry)
    async_add_entities(
        NanitCameraEntity(cam_data.push_coordinator, cam_data.camera, read_only=read_only)
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
        *,
        read_only: bool = False,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self._camera = camera
        # Read-only mode: no ON_OFF, so Home Assistant never offers to put
        # the camera to sleep; turn_on/turn_off also refuse if called anyway.
        self._read_only = read_only
        if read_only:
            self._attr_supported_features = CameraEntityFeature.STREAM
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
        self._stream_start_task: asyncio.Task[bool] | None = None
        self._stream_start_url: str | None = None
        # Open WebRTC sessions. go2rtc pulls the RTMPS source itself, so these
        # viewers never appear in HA's own Stream outputs.
        self._webrtc_sessions: set[str] = set()
        self._provider_refresh_task: asyncio.Task[None] | None = None

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

        if (
            conn_state is ConnectionState.CONNECTED
            and prev_conn_state is not ConnectionState.CONNECTED
            and StreamType.WEB_RTC not in self.camera_capabilities.frontend_stream_types
            and (self._provider_refresh_task is None or self._provider_refresh_task.done())
        ):
            # HA picks the WebRTC provider (go2rtc) once, when the entity is
            # added, by asking for a stream source. That needs the camera's
            # control session; when the entity is added before it connects,
            # the camera would stay HLS-only until the next restart.
            _LOGGER.debug(
                "Camera %s connected without a WebRTC provider; checking again",
                self._camera.uid,
            )
            self._provider_refresh_task = self.hass.async_create_task(
                self.async_refresh_providers(),
                name=f"nanit_refresh_webrtc_providers_{self._camera.uid}",
            )

        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Mask the access token in this camera's stream log lines."""
        await super().async_added_to_hass()
        # Home Assistant logs each camera's stream under its own logger, keyed
        # by entity_id, and a rename re-runs this with the new id.
        async_attach_to_camera_stream(self.entity_id)

    async def async_will_remove_from_hass(self) -> None:
        """Tear down stream bookkeeping so nothing outlives the entity.

        The keepalive timer reschedules itself indefinitely; without this it
        survives a config-entry reload and keeps firing on the removed
        entity — re-sending PUT_STREAMING with a stale URL through the old
        (stopped) camera, which resurrects its WebSocket and redirects the
        camera's push away from the replacement entity's stream.
        """
        async_detach_from_camera_stream(self.entity_id)
        self._invalidate_stream("entity removal")
        for task in (
            self._stream_refresh_task,
            self._stream_keepalive_task,
            self._stream_start_task,
            self._provider_refresh_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        self._stream_refresh_task = None
        self._stream_keepalive_task = None
        self._stream_start_task = None
        self._provider_refresh_task = None
        self._webrtc_sessions.clear()
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

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Count the WebRTC session as a stream consumer, then forward the candidate.

        Every browser session trickles at least one ICE candidate, so this is
        the first per-session hook HA offers without implementing the offer
        itself (overriding the offer would mark the camera as native WebRTC
        and bypass go2rtc). The session stops counting in close_webrtc_session.
        """
        if session_id not in self._webrtc_sessions:
            self._webrtc_sessions.add(session_id)
            _LOGGER.debug(
                "WebRTC session %s opened on camera %s (%d open)",
                session_id,
                self._camera.uid,
                len(self._webrtc_sessions),
            )
        await super().async_on_webrtc_candidate(session_id, candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session, ignoring duplicate go2rtc close callbacks."""
        if session_id in self._webrtc_sessions:
            self._webrtc_sessions.discard(session_id)
            _LOGGER.debug(
                "WebRTC session %s closed on camera %s (%d open)",
                session_id,
                self._camera.uid,
                len(self._webrtc_sessions),
            )
        try:
            super().close_webrtc_session(session_id)
        except KeyError:
            _LOGGER.debug("WebRTC session %s was already closed", session_id)

    def _stream_has_outputs(self) -> bool:
        """Return True when HA's own Stream (HLS/recorder) has consumers."""
        stream = self.stream
        return stream is not None and bool(stream.outputs())

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

        The entry log matters for support: when HA never asks for a source
        at all (the frontend silently falls back to still images if the
        ``stream`` integration is missing), the only way to tell that apart
        from a failed stream is the absence of this line.
        """
        _LOGGER.debug("Stream source requested for camera %s", self._camera.uid)
        if not self.is_on:
            _LOGGER.debug("Camera %s is powered off — no stream source", self._camera.uid)
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

            # None means the ACK is still outstanding: the send finishes in
            # the background, so the URL is still the one the camera gets.
            if await self._async_request_stream_start(source) is False:
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
        if await self._async_request_stream_start(source) is False:
            return None
        # PUT_STREAMING just went out — restart the keepalive countdown.
        self._schedule_stream_keepalive_timer()
        return source

    async def _async_request_stream_start(self, rtmps_url: str) -> bool | None:
        """Send a best-effort PUT_STREAMING, waiting at most _STREAM_START_WAIT.

        Returns True once the camera ACKed, False when every attempt failed,
        and None when the ACK is still outstanding; the send and its retries
        then finish in the background.

        Viewer-initiated starts used to force-reconnect the control WebSocket
        on a late ACK. That stops the camera's RTMPS push for every viewer
        (the reason keepalives are best-effort), re-enters the resume path on
        the reconnect transition, and kept stream_source() blocked for 40s+
        per attempt, far past HA's 10s stream-source timeout. A dead session
        is still recovered by aionanit's health check, whose reconnect lands
        in _handle_stream_keepalive to resume the push.
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return await self._async_start_streaming_safe(rtmps_url, reconnect_on_failure=False)

        task = self._stream_start_task
        if task is None or task.done() or self._stream_start_url != rtmps_url:
            if task is not None and not task.done():
                # A superseded URL must not be re-sent after the new one.
                task.cancel()
            task = hass.async_create_task(
                self._async_start_streaming_safe(rtmps_url, reconnect_on_failure=False),
                name=f"nanit_stream_start_{self._camera.uid}",
            )
            self._stream_start_task = task
            self._stream_start_url = rtmps_url

        done, _ = await asyncio.wait((task,), timeout=_STREAM_START_WAIT)
        if not done or task.cancelled():
            _LOGGER.debug(
                "PUT_STREAMING ACK for camera %s still pending after %.0fs; "
                "returning the stream URL",
                self._camera.uid,
                _STREAM_START_WAIT,
            )
            return None
        return task.result()

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
        consumers are kept alive — an idle camera is left to lapse. WebRTC
        viewers count: once WebRTC plays, the frontend drops its HLS player,
        so HA's Stream has no outputs while go2rtc is still pulling.

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
            _LOGGER.debug("Stream keepalive for camera %s: no cached source", self._camera.uid)
            return
        has_outputs = self._stream_has_outputs()
        data = self.coordinator.data
        if (
            data is not None
            and data.connection.state is not ConnectionState.CONNECTED
            and (has_outputs or self._webrtc_sessions)
        ):
            # The send would only add inline reconnects on top of aionanit's
            # own reconnect loop (e.g. while the Nanit relay answers 503). The
            # CONNECTED transition calls this handler again to resume the push.
            _LOGGER.debug(
                "Stream keepalive for camera %s waits for the camera to reconnect",
                self._camera.uid,
            )
        elif (
            self.is_on
            and (has_outputs or self._webrtc_sessions)
            and (self._stream_keepalive_task is None or self._stream_keepalive_task.done())
        ):
            self._stream_keepalive_task = self.hass.async_create_task(
                self._async_start_streaming_safe(source, reconnect_on_failure=False),
                name=f"nanit_stream_keepalive_{self._camera.uid}",
            )
        else:
            _LOGGER.debug(
                "Stream keepalive for camera %s skipped (on: %s, HLS outputs: %s, WebRTC sessions: %d)",
                self._camera.uid,
                self.is_on,
                has_outputs,
                len(self._webrtc_sessions),
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
        if (
            self.is_on
            and self._webrtc_sessions
            and not self._stream_has_outputs()
            and time.monotonic() - self._stream_source_started_at < _STREAM_ROTATION_DEFER_LIMIT
        ):
            # go2rtc keeps pulling the URL it was handed and only asks for a
            # new one on the next WebRTC offer, so rotating now could move the
            # camera's push away from the open viewers, and dropping the cache
            # would stop the keepalive (the push then lapses ~20 minutes
            # later). Keep both and check again once the viewers are gone.
            if self._cancel_stream_expiry_timer is None:
                _LOGGER.debug(
                    "Deferring RTMPS source rotation for camera %s after %s: "
                    "%d WebRTC session(s) open",
                    self._camera.uid,
                    reason,
                    len(self._webrtc_sessions),
                )
                self._schedule_stream_expiry_timer(_STREAM_KEEPALIVE_INTERVAL)
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
            if await self._async_request_stream_start(source) is False:
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

    def _raise_if_read_only(self) -> None:
        """Refuse a power change when the entry is in read-only mode."""
        if self._read_only:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="read_only",
            )

    async def async_turn_on(self) -> None:
        """Turn the camera on (disable sleep/standby mode)."""
        self._raise_if_read_only()
        self._invalidate_stream()
        await self._camera.async_set_settings(sleep_mode=False)

    async def async_turn_off(self) -> None:
        """Turn the camera off (enable sleep/standby mode)."""
        self._raise_if_read_only()
        self._invalidate_stream()
        try:
            await self._camera.async_stop_streaming()
        except Exception:
            _LOGGER.debug("Failed to stop streaming before sleep", exc_info=True)
        await self._camera.async_set_settings(sleep_mode=True)
