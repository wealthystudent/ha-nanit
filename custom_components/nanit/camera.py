"""Camera platform for Nanit."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image as ffmpeg_get_image
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.system_info import is_official_image

from aionanit import NanitCamera
from aionanit.camera import STREAM_LIMIT_STATUS
from aionanit.models import ConnectionState

from . import NanitConfigEntry
from .const import DOMAIN, is_read_only, is_stream_stills
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity
from .local_stream import (
    ACK_TIMEOUT,
    BUSY_PUBLISH_WAIT,
    COOLDOWN,
    PUBLISH_WAIT,
    LocalStreamCamera,
)
from .hub import StreamStillInfo
from .log_redaction import async_attach_to_camera_stream, async_detach_from_camera_stream

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
# Stream stills (opt-in): when the cloud snapshot fails, a cached image older
# than this is replaced by one frame read from the live stream. Each grab asks
# the camera to stream, and it keeps pushing for ~20 minutes afterwards.
_STREAM_STILL_MAX_AGE = 10 * 60
# Wait at least this long after a grab ended before starting another, so a
# failing stream is not retried on every image request. The wait doubles with
# each failure in a row, up to _STREAM_STILL_RETRY_CAP, and resets on success.
_STREAM_STILL_RETRY_DELAY = 60.0
# Every failed ffmpeg attempt still sends PUT_STREAMING, and the camera then
# pushes for ~20 minutes. A cap well past that lets a camera that never yields a
# still stop streaming for most of the time.
_STREAM_STILL_RETRY_CAP = 60 * 60
_STREAM_STILL_MAX_DOUBLINGS = 6
# Failures in a row before one warning is logged for the streak.
_STREAM_STILL_WARN_AFTER = 3
# ffmpeg gives up once a wait for data from the input lasts this long, as it
# should for a stream that is not being served yet. Any byte resets the timer,
# so a server that keeps sending control messages such as RTMP pings is not
# cut off. ha-ffmpeg's own 15s read timeout ends a read too, but its kill path
# logs a "Task exception was never retrieved" error with a traceback, so this
# stays well under it. Going lower ends a slow but working read early. A warm
# read takes about 5s in total.
_STREAM_STILL_STALL_TIMEOUT = 10.0
# Bound on the frame read. ffmpeg and then ha-ffmpeg stop a read themselves
# well before this, so it only guards against a call that never returns.
_STREAM_STILL_TIMEOUT = 20.0
# hass.data key for the shared, once-per-run check for the ffmpeg binary.
_FFMPEG_CHECK = f"{DOMAIN}_stream_still_ffmpeg_check"


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
    local_stream = entry.runtime_data.local_stream
    if local_stream is not None:
        local_stream.prune_keys(set(entry.runtime_data.cameras))
    stream_stills = is_stream_stills(entry)
    async_add_entities(
        NanitCameraEntity(
            cam_data.push_coordinator,
            cam_data.camera,
            read_only=read_only,
            local=(local_stream.camera(cam_data.camera.uid) if local_stream is not None else None),
            stream_stills=stream_stills,
            still_info=cam_data.stream_still,
        )
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
        local: LocalStreamCamera | None = None,
        stream_stills: bool = False,
        still_info: StreamStillInfo | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self._camera = camera
        # Local streaming (#175). When set and active, the camera pushes to
        # the receiver and stream_source() returns its loopback FLV URL.
        self._local = local
        self._cancel_local_cooldown_timer: CALLBACK_TYPE | None = None
        self._local_fallback_task: asyncio.Task[None] | None = None
        if local is not None:
            local.channel.start_callback = self._async_local_start
            local.channel.on_consumers_changed = self._handle_local_consumers
            local.channel.on_publish = self._handle_local_publish
            local.on_mode_changed = self._handle_local_cooldown_end
            local.channel.accepting = local.active and self.is_on
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
        # Opt-in stills from the live stream when the cloud snapshot fails.
        self._stream_stills = stream_stills
        self._still_info = still_info if still_info is not None else StreamStillInfo()
        self._stream_still_task: asyncio.Task[None] | None = None
        self._stream_still_finished_at: float | None = None
        self._stream_still_failures = 0

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
            self._cancel_stream_still()
            self._invalidate_stream("power state change")
        else:
            self._invalidate_stream_if_expired()
        self._update_local_accepting()

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
        if self._local is not None:
            self._local.channel.start_callback = None
            self._local.channel.on_consumers_changed = None
            self._local.channel.on_publish = None
            self._local.on_mode_changed = None
            self._local.channel.cancel_start()
        if self._local_fallback_task is not None and not self._local_fallback_task.done():
            self._local_fallback_task.cancel()
        self._local_fallback_task = None
        if self._cancel_local_cooldown_timer is not None:
            self._cancel_local_cooldown_timer()
            self._cancel_local_cooldown_timer = None
        for task in (
            self._stream_refresh_task,
            self._stream_keepalive_task,
            self._stream_still_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        self._stream_refresh_task = None
        self._stream_keepalive_task = None
        self._stream_still_task = None
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
        self._close_local_subscribers()

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
        self._close_local_subscribers()

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

        The entry log matters for support: when HA never asks for a source
        at all (the frontend silently falls back to still images if the
        ``stream`` integration is missing), the only way to tell that apart
        from a failed stream is the absence of this line.
        """
        _LOGGER.debug("Stream source requested for camera %s", self._camera.uid)
        if not self.is_on:
            _LOGGER.debug("Camera %s is powered off — no stream source", self._camera.uid)
            return None

        if self._local is not None and self._local.active:
            # No side effects here: Home Assistant asks for the source when it
            # picks a WebRTC provider at startup and on every WebRTC offer,
            # not only when someone watches. The camera is started by the
            # receiver's first subscriber (_async_local_start).
            self._local.channel.accepting = True
            return self._local.flv_url()

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
        # Route by what is being consumed, not by mode: a cloud stream that
        # outlived a local miss still needs its keepalive.
        if self._local is not None and self._local.channel.consumer_count:
            self._handle_local_keepalive()
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
        if self._local_reclaimed_stream():
            return
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
            if self._local_reclaimed_stream(after_put=True):
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
    # Local streaming
    # ------------------------------------------------------------------

    @callback
    def _update_local_accepting(self) -> None:
        """Turn local consumers away at once while the camera can't push."""
        local = self._local
        if local is None:
            return
        data = self.coordinator.data
        connected = data is None or data.connection.state is ConnectionState.CONNECTED
        # Readers get a 503 at once while the camera can't be asked to push.
        accepting = self.is_on and local.active and connected
        local.channel.accepting = accepting
        if not self.is_on:
            local.channel.close_subscribers()

    @callback
    def _close_local_subscribers(self) -> None:
        """Make local consumers reconnect, as a fresh cloud URL would."""
        if self._local is not None:
            self._local.channel.close_subscribers()

    async def _async_local_start(self) -> bool:
        """Start the camera's push for the receiver's first subscriber.

        One best-effort PUT_STREAMING, then a short wait for the camera to
        publish. A miss of any kind moves the camera to the cloud stream
        for a cooldown. Only a clean miss (the camera acked and nothing
        ever connected) counts toward pausing local streaming.

        A camera that already streams to as many apps as it allows answers
        STREAM_LIMIT_STATUS. That says nothing about the network, so it is
        never a strike. It still grows the backoff: the slot can stay taken
        for hours (other apps watching overnight, or this entry's own cloud
        push after a fallback), and a retry every cooldown would stall the
        viewer each time. The cloud URL may still play meanwhile, if one of
        the other apps keeps a cloud stream running.

        Connections are counted per peer once the camera's address is
        learned from an earlier publish, so a LAN scanner or another camera
        landing in the window doesn't disguise a miss.
        Without a known address the server-wide counts are the best signal.
        """
        local = self._local
        if local is None or not self.is_on or not local.active:
            return False
        data = self.coordinator.data
        if data is not None and data.connection.state is not ConnectionState.CONNECTED:
            # The relay is reconnecting (HA starting, a token refresh). The
            # request would just time out, so end this read quietly: the
            # player retries, by when the camera is reachable again.
            return False
        server = local.owner.server
        peer = local.channel.publisher_peer
        if peer is not None:
            # asyncio reports peer addresses in canonical form.
            try:
                peer = str(ipaddress.ip_address(peer))
            except ValueError:
                peer = None

        def counts() -> tuple[int, int]:
            if peer is None:
                return server.connection_attempts, server.protocol_failures
            return server.attempts_by_peer.get(peer, 0), server.failures_by_peer.get(peer, 0)

        attempts_before, failures_before = counts()
        try:
            url, host = await local.async_publish_url()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Nanit local streaming has no address to advertise: %s", err)
            # Usually lasting (no routable address), so it grows the backoff.
            self._fall_back_to_cloud("no local address to advertise")
            return False
        try:
            # aionanit leaves a late ACK to resolve a future nobody awaits,
            # which is harmless.
            async with asyncio.timeout(ACK_TIMEOUT):
                status = await self._camera.async_start_streaming(
                    rtmps_url=url, reconnect_on_failure=False
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Local PUT_STREAMING failed for camera %s: %s", self._camera.uid, err)
            self._fall_back_to_cloud("camera did not accept the local stream request")
            return False
        if not isinstance(status, int):
            status = None  # unknown: judge by whether the camera connects
        busy = status == STREAM_LIMIT_STATUS
        if status is not None and status != 200 and not busy:
            _LOGGER.debug(
                "Camera %s declined the local stream request: %s", self._camera.uid, status
            )
            self._fall_back_to_cloud(f"camera declined the local stream request ({status})")
            return False

        if await local.channel.wait_for_publish(BUSY_PUBLISH_WAIT if busy else PUBLISH_WAIT):
            return True  # _handle_local_publish cleared the strikes

        if busy:
            reason = "the camera is already streaming to as many apps as it allows"
            _LOGGER.info("Nanit local stream for %s skipped: %s", self._camera.uid, reason)
            self._fall_back_to_cloud(reason)
            return False

        if not self.is_on:
            # Put to sleep while we waited: not the network's fault.
            self._fall_back_to_cloud("camera went to sleep", count_miss=False)
            return False
        attempts_after, failures_after = counts()
        if attempts_after > attempts_before:
            if failures_after > failures_before:
                local.owner.record_protocol_failure()
            reason = "a connection arrived but never published"
        else:
            local.owner.record_strike(host)
            reason = "the camera never connected"
        _LOGGER.info("Nanit local stream for %s missed: %s", self._camera.uid, reason)
        self._fall_back_to_cloud(reason)
        return False

    @callback
    def _fall_back_to_cloud(self, reason: str, *, count_miss: bool = True) -> None:
        """Move this camera to the cloud stream for the cooldown.

        The mode flips at once. Moving a running HLS stream to the cloud
        happens in the background, so the FLV request that triggered the
        miss is not held open through the cloud start's retries.
        """
        local = self._local
        if local is None:
            return
        local.start_cooldown(reason, count_miss=count_miss)
        local.channel.accepting = False
        self._schedule_local_cooldown_end(max(0.0, local.cooldown_until - time.monotonic()))
        stream = self.stream
        hass = getattr(self, "hass", None)
        if stream is not None and not stream.outputs():
            # An idle HLS stream keeps the FLV URL forever (HA never asks for
            # the source again), so drop it and the next viewer gets the cloud.
            self._invalidate_stream("local stream unavailable")
            return
        if hass is None or stream is None or not self.is_on:
            return
        if self._local_fallback_task is None or self._local_fallback_task.done():
            self._local_fallback_task = hass.async_create_task(
                self._async_move_hls_to_cloud(),
                name=f"nanit_local_fallback_{self._camera.uid}",
            )

    async def _async_move_hls_to_cloud(self) -> None:
        """Point a running HLS stream at the cloud URL.

        WebRTC viewers re-offer and stream_source() hands them the cloud URL.
        """
        stream = self.stream
        if stream is None or self._local_reclaimed_stream():
            return
        try:
            source = await self._camera.async_get_stream_rtmps_url()
        except Exception:
            _LOGGER.debug("Could not build the cloud URL after a local miss", exc_info=True)
            return
        if not await self._async_start_streaming_safe(source, reconnect_on_failure=False):
            return
        if self._local_reclaimed_stream(after_put=True):
            return
        self._cached_stream_source = source
        self._stream_source_started_at = time.monotonic()
        self._schedule_stream_expiry_timer()
        self._schedule_stream_keepalive_timer()
        stream.update_source(source)

    def _local_reclaimed_stream(self, *, after_put: bool = False) -> bool:
        """Hand the stream to local if local took over while a cloud task waited.

        This can happen without the cooldown-end handler having run first,
        when the pause lapses by the wall clock before its timer fires (a
        host suspend). Finishing the move here keeps the stream from being
        left half switched.
        """
        if self._local is None or not self._local.active:
            return False
        self._handle_local_cooldown_end()
        if after_put and self._local.channel.consumer_count:
            # The cloud request already went out and took the push away from
            # local readers, so point the camera back at them right away.
            self._handle_local_keepalive()
        return True

    @callback
    def _schedule_local_cooldown_end(self, delay: float = COOLDOWN) -> None:
        if self._cancel_local_cooldown_timer is not None:
            self._cancel_local_cooldown_timer()
        hass = getattr(self, "hass", None)
        if hass is None:
            self._cancel_local_cooldown_timer = None
            return
        self._cancel_local_cooldown_timer = async_call_later(
            hass, delay, self._handle_local_cooldown_end
        )

    @callback
    def _handle_local_cooldown_end(self, _now: object = None) -> None:
        """Move the camera back to local streaming (cooldown or pause over).

        The camera has one push target for all of Home Assistant, so the
        cloud stream is wound down here instead of waiting for it to idle
        (a preloaded or always-on stream never does). A watched HLS stream
        is switched in place to the local URL, whose first read starts the
        camera locally, or falls back to the cloud again on a miss. The
        cached cloud URL and its timers are dropped so nothing later moves
        the push back. A WebRTC viewer still pulling the cloud URL stalls
        until its player re-offers, which the Nanit card does on its own.
        """
        if self._cancel_local_cooldown_timer is not None:
            # Also reached early, from the repair's retry or the pause end.
            self._cancel_local_cooldown_timer()
            self._cancel_local_cooldown_timer = None
        local = self._local
        if local is None:
            return
        self._update_local_accepting()
        if not local.active:
            return
        # A cloud task already waiting would move the stream back to cloud.
        for task in (self._local_fallback_task, self._stream_refresh_task):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        if self._cached_stream_source is not None:
            self._expire_stream_source()
            keepalive = self._stream_keepalive_task
            if keepalive is not None and not keepalive.done():
                # A cloud keepalive in flight would point the camera back.
                keepalive.cancel()
            if local.channel.consumer_count:
                # The keepalive timer is shared, and local readers still need it.
                self._schedule_stream_keepalive_timer()
        stream = self.stream
        flv_url = local.flv_url()
        # Keyed on what the stream plays, not on the cached URL: a failed
        # cloud renewal leaves the stream on the old URL with nothing cached.
        if stream is None or stream.source == flv_url:
            return
        if stream.outputs() and self.is_on:
            stream.update_source(flv_url)
        else:
            # Drop only the idle HLS stream: local readers are already on
            # the URL we're switching to and keep the shared keepalive.
            self.stream = None
            if (hass := getattr(self, "hass", None)) is not None:
                hass.create_task(
                    self._stop_discarded_stream(stream),
                    name=f"nanit_stop_discarded_stream_{self._camera.uid}",
                )

    @callback
    def _handle_local_publish(self) -> None:
        """Any accepted push proves the path works, asked for or not."""
        if self._local is not None:
            self._local.owner.record_success()
            self._local.record_publish()
            if not self._local.active:
                # The path works after all, so end the cooldown too.
                self._local.end_cooldown()

    @callback
    def _handle_local_consumers(self, count: int) -> None:
        """Start the keepalive when local consumers appear."""
        if count and self._cancel_stream_keepalive_timer is None:
            self._schedule_stream_keepalive_timer()

    @callback
    def _handle_local_keepalive(self) -> None:
        """Re-send the local push URL while anyone reads the local stream."""
        local = self._local
        if local is None:
            return
        channel = local.channel
        if not channel.consumer_count or not self.is_on or not local.active:
            return
        data = self.coordinator.data
        if data is not None and data.connection.state is not ConnectionState.CONNECTED:
            # The CONNECTED transition calls the keepalive again.
            self._schedule_stream_keepalive_timer()
            return
        if self._stream_keepalive_task is None or self._stream_keepalive_task.done():
            self._stream_keepalive_task = self.hass.async_create_task(
                self._async_local_keepalive(),
                name=f"nanit_local_keepalive_{self._camera.uid}",
            )
        self._schedule_stream_keepalive_timer()

    async def _async_local_keepalive(self) -> bool:
        local = self._local
        if local is None:
            return False
        url, _host = await local.async_publish_url()
        return await self._async_start_streaming_safe(url, reconnect_on_failure=False)

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

        With stream stills on, a failed cloud fetch may also start a
        background frame grab from the live stream (see
        ``_maybe_start_stream_still``). The request never waits for it.
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

        self._maybe_start_stream_still()
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
            # A working cloud ends any streak of failed stream stills.
            self._stream_still_failures = 0
        return image

    # ------------------------------------------------------------------
    # Stills from the live stream (opt-in)
    # ------------------------------------------------------------------

    def _stream_still_retry_delay(self) -> float:
        """Return the wait after the last attempt, doubled per failure in a row."""
        doublings = min(max(self._stream_still_failures - 1, 0), _STREAM_STILL_MAX_DOUBLINGS)
        return float(min(_STREAM_STILL_RETRY_DELAY * 2**doublings, _STREAM_STILL_RETRY_CAP))

    @callback
    def _maybe_start_stream_still(self) -> None:
        """Start one background frame grab if the cached image is too old.

        Called only after the cloud snapshot failed. At most one grab runs
        per camera, the next one waits out the retry delay after the last one
        ended, and a camera that is asleep or unreachable is left alone.
        """
        if not self._stream_stills:
            return
        if self._stream_still_task is not None and not self._stream_still_task.done():
            return
        if not self.is_on or not self.available:
            return
        now = time.monotonic()
        if (
            self._cached_snapshot is not None
            and now - self._cached_snapshot_at < _STREAM_STILL_MAX_AGE
        ):
            return
        if (
            self._stream_still_finished_at is not None
            and now - self._stream_still_finished_at < self._stream_still_retry_delay()
        ):
            return
        # Not eager: the image request must never run any of the grab inline.
        self._stream_still_task = self.hass.async_create_background_task(
            self._async_run_stream_still(),
            name=f"nanit_stream_still_{self._camera.uid}",
            eager_start=False,
        )

    @callback
    def _cancel_stream_still(self) -> None:
        """Cancel a running grab, e.g. because the camera is going to sleep."""
        task = self._stream_still_task
        if task is not None and not task.done():
            task.cancel()
        self._stream_still_task = None

    async def _async_ffmpeg_found(self) -> bool:
        """Return whether the ffmpeg binary exists, checked once per run.

        Home Assistant OS and Container ship ffmpeg. A Core install may not,
        and without this check every grab would send PUT_STREAMING for
        nothing and ha-ffmpeg would log an error. The check is shared by all
        cameras, so a missing binary is reported once.
        """
        check: asyncio.Task[bool] | None = self.hass.data.get(_FFMPEG_CHECK)
        if check is None:
            check = self.hass.async_create_task(
                _async_check_ffmpeg(self.hass), name="nanit_stream_still_ffmpeg_check"
            )
            self.hass.data[_FFMPEG_CHECK] = check
        try:
            # Shielded: cancelling one grab must not cancel the shared check.
            return await asyncio.shield(check)
        except Exception as err:  # noqa: BLE001
            # A check that could not run (e.g. the executor shutting down) is
            # not a verdict. Drop it so a later grab checks again.
            if self.hass.data.get(_FFMPEG_CHECK) is check:
                self.hass.data.pop(_FFMPEG_CHECK)
            _LOGGER.debug("Could not check for ffmpeg: %s", type(err).__name__)
            return False

    def _stream_still_failed(self, reason: str) -> None:
        """Count a failed grab, and warn once when failures keep coming."""
        self._stream_still_failures += 1
        _LOGGER.debug("No still from camera %s: %s", self._camera.uid, reason)
        if self._stream_still_failures == _STREAM_STILL_WARN_AFTER:
            _LOGGER.warning(
                "Stills from the live stream keep failing for camera %s (last: %s). "
                "Retrying less often, up to every %.0f minutes",
                self._camera.uid,
                reason,
                _STREAM_STILL_RETRY_CAP / 60,
            )

    async def _async_run_stream_still(self) -> None:
        """Run one grab, and start the retry delay when it ends.

        A grab can take most of a minute (PUT_STREAMING retries on a slow
        camera, then the read), so the delay counts from its end rather than
        its start. The in-flight task check keeps grabs from overlapping
        meanwhile.
        """
        try:
            await self._async_grab_stream_still()
        finally:
            self._stream_still_finished_at = time.monotonic()

    async def _async_grab_stream_still(self) -> None:
        """Read one frame from the live stream into the snapshot cache.

        With a viewer of Home Assistant's own stream, the frame comes from that
        running stream and nothing is sent to the camera. Otherwise it is read
        with a one-shot ffmpeg process, not through the stream worker, so
        nothing keeps running afterwards. ``stream_source()`` sends
        PUT_STREAMING like a viewer would. Its keepalive renews the push only
        while a stream has outputs, so a grab leaves the camera to stop
        pushing on its own about 20 minutes later. PUT_STREAMING STOPPED is
        never sent, because it is camera-wide and would cut off anyone
        watching. The source URL carries an access token and is never logged
        here.

        WebRTC (go2rtc) and HomeKit viewers do not use that stream, so for
        them the ffmpeg path runs and re-sends PUT_STREAMING, usually for the
        URL already cached.

        Cancelling this task cannot stop an ffmpeg read already under way,
        because Home Assistant shields that call. ffmpeg's stall timeout and
        ha-ffmpeg's own 15 second read timeout bound it.
        """
        if not self.is_on:
            # Put to sleep before the task ran. Not a failure.
            return
        stream = self.stream
        if stream is not None and stream.outputs():
            await self._async_read_still(stream.async_get_image, "the running stream")
            return

        if not await self._async_ffmpeg_found():
            return
        source = await self.stream_source()
        if source is None:
            self._stream_still_failed("no stream source")
            return
        if not self.is_on:
            # Put to sleep while the source was being prepared.
            return
        await self._async_read_still(lambda: self._ffmpeg_read(source), "ffmpeg")

    def _ffmpeg_read(self, source: str) -> Awaitable[bytes | None]:
        """Start a one-frame ffmpeg read of ``source`` that ends on its own if it stalls.

        ha-ffmpeg takes input options in the same string as the input, so the
        URL is quoted to stay one argument whatever it holds.
        """
        stall_us = int(_STREAM_STILL_STALL_TIMEOUT * 1_000_000)
        return ffmpeg_get_image(self.hass, f"-rw_timeout {stall_us} -i {shlex.quote(source)}")

    async def _async_read_still(
        self, read: Callable[[], Awaitable[bytes | None]], via: str
    ) -> None:
        """Await one bounded frame read and store a frame in the snapshot cache."""
        try:
            async with asyncio.timeout(_STREAM_STILL_TIMEOUT):
                image = await read()
        except TimeoutError:
            self._stream_still_failed(f"timed out reading a frame from {via}")
            return
        except Exception as err:  # noqa: BLE001
            # The type only: the message or traceback could carry the URL.
            self._stream_still_failed(f"{type(err).__name__} reading a frame from {via}")
            return
        if not image:
            self._stream_still_failed(f"no frame from {via}")
            return
        self._cached_snapshot = image
        self._cached_snapshot_at = time.monotonic()
        self._still_info.last_taken = dt_util.utcnow()
        self._stream_still_failures = 0
        _LOGGER.debug("Took a still of camera %s from %s", self._camera.uid, via)

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
        self._cancel_stream_still()
        self._invalidate_stream()
        await self._camera.async_set_settings(sleep_mode=False)

    async def async_turn_off(self) -> None:
        """Turn the camera off (enable sleep/standby mode)."""
        self._raise_if_read_only()
        # A grab still waiting on its stream source would otherwise resume
        # after the STOPPED below and send PUT_STREAMING to a sleeping camera.
        self._cancel_stream_still()
        self._invalidate_stream()
        try:
            await self._camera.async_stop_streaming()
        except Exception:
            _LOGGER.debug("Failed to stop streaming before sleep", exc_info=True)
        await self._camera.async_set_settings(sleep_mode=True)


async def _async_check_ffmpeg(hass: HomeAssistant) -> bool:
    """Look for the ffmpeg binary, and warn once if it is missing."""
    try:
        binary = get_ffmpeg_manager(hass).binary
    except ValueError:
        binary = "ffmpeg"
        found = False
    else:
        found = await hass.async_add_executor_job(_ffmpeg_binary_exists, binary)
    if not found:
        _LOGGER.warning(
            "Stills from the live stream need FFmpeg, and %s was not found. "
            "Home Assistant OS and Container include it. Install FFmpeg and restart "
            "Home Assistant, or turn the option off",
            binary,
        )
    return found


def _ffmpeg_binary_exists(binary: str) -> bool:
    """Return whether the binary can run (blocking, run in the executor)."""
    return is_official_image() or shutil.which(binary) is not None
