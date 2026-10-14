"""Per-camera stream state: the publisher, cached headers, GOP and subscribers.

A channel is the meeting point between the camera's push (one publisher at
a time) and any number of local consumers (go2rtc and HA's stream worker
over HTTP-FLV, later an audio-only endpoint). It also coordinates starting
the camera: the first subscriber triggers the integration's start callback,
concurrent subscribers share that one attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .flv import TAG_AUDIO, TAG_SCRIPT, TAG_VIDEO, Tag, ts_since

_LOGGER = logging.getLogger(__name__)

# A replacement publisher is only accepted once the current one has been
# silent this long, so a LAN host that learned the key cannot cut in on a
# live feed. A real reconnect leaves the old socket dead or dying.
REPLACE_AFTER_SILENCE = 2.0
# Subscribers are closed when the publisher is gone this long.
PUBLISHER_GRACE = 3.0
# The GOP cache is dropped past these bounds; late joiners then wait for the
# next keyframe instead.
MAX_GOP_BYTES = 4 * 1024 * 1024
MAX_GOP_MILLIS = 10_000
# A subscriber this far behind is dropped rather than buffered.
MAX_SUBSCRIBER_BYTES = 8 * 1024 * 1024

StartCallback = Callable[[], Awaitable[bool]]


class Subscription:
    """A consumer's view of a channel: an async queue of FLV tags.

    Video subscribers receive media starting at a keyframe. ``None`` from
    ``get()`` means the subscription was closed.
    """

    def __init__(self, channel: StreamChannel, *, audio_only: bool) -> None:
        """Initialize the subscription."""
        self._channel = channel
        self.audio_only = audio_only
        self._queue: deque[Tag] = deque()
        self._bytes = 0
        self._wakeup = asyncio.Event()
        self._need_keyframe = not audio_only
        self.closed = False

    def _offer(self, tag: Tag) -> bool:
        """Queue a media tag. Return False when this subscriber is too slow."""
        if self.closed:
            return True
        if self.audio_only and tag.kind != TAG_AUDIO:
            return True
        if self._need_keyframe:
            if not tag.is_keyframe:
                return True
            self._need_keyframe = False
        self._queue.append(tag)
        self._bytes += len(tag.body)
        self._wakeup.set()
        return self._bytes <= MAX_SUBSCRIBER_BYTES

    def _offer_header(self, tag: Tag) -> None:
        """Queue a codec configuration change, ahead of any keyframe gate."""
        if self.closed or (self.audio_only and tag.kind != TAG_AUDIO):
            return
        self._queue.append(tag)
        self._bytes += len(tag.body)
        self._wakeup.set()

    def _close(self) -> None:
        self.closed = True
        self._wakeup.set()

    async def get(self) -> Tag | None:
        """Return the next tag, or None once closed."""
        while not self._queue:
            if self.closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()
        tag = self._queue.popleft()
        self._bytes -= len(tag.body)
        return tag

    def close(self) -> None:
        """Stop receiving tags."""
        self._channel.unsubscribe(self)

    def __aiter__(self) -> Subscription:
        """Iterate tags until the subscription closes."""
        return self

    async def __anext__(self) -> Tag:
        """Return the next tag, ending iteration once closed."""
        tag = await self.get()
        if tag is None:
            raise StopAsyncIteration
        return tag


class StreamChannel:
    """State for one camera's local stream."""

    def __init__(self, camera_uid: str, stream_key: str) -> None:
        """Initialize the channel."""
        self.camera_uid = camera_uid
        self.stream_key = stream_key
        self.start_callback: StartCallback | None = None
        # False while the integration wants consumers turned away at once
        # (camera asleep, cloud cooldown, local streaming paused).
        self.accepting = True
        self.on_consumers_changed: Callable[[int], None] | None = None
        # Called on every accepted publish, including one nobody asked for
        # (a camera still pushing across a restart).
        self.on_publish: Callable[[], None] | None = None

        self.publisher_peer: str | None = None
        self.last_publish_at: float | None = None
        self.publish_count = 0
        self.bytes_in = 0
        self._publisher_id: int | None = None
        self._abort_publisher: Callable[[], None] | None = None
        self._last_tag_at = 0.0

        self.metadata: Tag | None = None
        self.avc_header: Tag | None = None
        self.aac_header: Tag | None = None
        self._gop: list[Tag] = []
        self._gop_bytes = 0

        self._subscribers: set[Subscription] = set()
        self._published = asyncio.Event()
        self._headers_ready = asyncio.Event()
        self._start_task: asyncio.Future[bool] | None = None
        self._closed = False
        self._grace_handle: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # Publisher side (called by the server)
    # ------------------------------------------------------------------

    @property
    def publishing(self) -> bool:
        """Return True while a publisher is connected."""
        return self._publisher_id is not None

    def can_accept_publisher(self) -> bool:
        """Return True if a new publish may take this channel."""
        if self._publisher_id is None:
            return True
        return time.monotonic() - self._last_tag_at >= REPLACE_AFTER_SILENCE

    def publish_started(
        self, publisher_id: int, peer: str | None, abort: Callable[[], None]
    ) -> None:
        """Make ``publisher_id`` the channel's publisher."""
        if self._publisher_id is not None and self._abort_publisher is not None:
            _LOGGER.debug("Replacing silent publisher on %s", self.camera_uid)
            self._abort_publisher()
        if self._grace_handle is not None:
            self._grace_handle.cancel()
            self._grace_handle = None
        # A new publisher restarts timestamps and may change codec headers,
        # so anyone reading the old one has to start over.
        self.close_subscribers()
        self._publisher_id = publisher_id
        self._abort_publisher = abort
        self.publisher_peer = peer
        self.last_publish_at = time.time()
        self.publish_count += 1
        self._last_tag_at = time.monotonic()
        self.metadata = self.avc_header = self.aac_header = None
        self._gop = []
        self._gop_bytes = 0
        self._headers_ready.clear()
        self._published.set()
        if self.on_publish is not None:
            try:
                self.on_publish()
            except Exception:
                # A bug in the owner must not kill the camera's connection.
                _LOGGER.exception("Local stream publish callback failed")

    def publish_ended(self, publisher_id: int) -> None:
        """Forget ``publisher_id`` if it is still the publisher."""
        if self._publisher_id != publisher_id:
            return
        self._publisher_id = None
        self._abort_publisher = None
        self._published.clear()
        if self._grace_handle is None:
            loop = asyncio.get_running_loop()
            self._grace_handle = loop.call_later(PUBLISHER_GRACE, self._grace_expired)

    def _grace_expired(self) -> None:
        self._grace_handle = None
        if self._publisher_id is None:
            self.close_subscribers()

    def tag_received(self, publisher_id: int, tag: Tag) -> None:
        """Take one tag from the publisher."""
        if publisher_id != self._publisher_id:
            return
        self._last_tag_at = time.monotonic()
        self.bytes_in += len(tag.body)
        if tag.kind == TAG_SCRIPT:
            self.metadata = tag
            return
        if tag.is_avc_header or tag.is_aac_header:
            previous = self.avc_header if tag.is_avc_header else self.aac_header
            if tag.is_avc_header:
                self.avc_header = tag
                self._headers_ready.set()
            else:
                self.aac_header = tag
            if previous is None or previous.body != tag.body:
                # A late or changed configuration has to reach readers that
                # already got their head, or their decoder never sees it.
                for sub in list(self._subscribers):
                    sub._offer_header(tag)
            return
        if tag.kind not in (TAG_AUDIO, TAG_VIDEO):
            return
        self._cache(tag)
        for sub in list(self._subscribers):
            if not sub._offer(tag):
                _LOGGER.debug("Dropping slow subscriber on %s", self.camera_uid)
                self.unsubscribe(sub)

    def _cache(self, tag: Tag) -> None:
        if tag.is_keyframe:
            self._gop = [tag]
            self._gop_bytes = len(tag.body)
            return
        if not self._gop:
            return
        self._gop.append(tag)
        self._gop_bytes += len(tag.body)
        age = ts_since(tag.timestamp, self._gop[0].timestamp)
        if self._gop_bytes > MAX_GOP_BYTES or age > MAX_GOP_MILLIS:
            self._gop = []
            self._gop_bytes = 0

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    @property
    def consumer_count(self) -> int:
        """Return how many subscribers are attached."""
        return len(self._subscribers)

    def subscribe(self, *, audio_only: bool = False) -> Subscription:
        """Attach a subscriber, primed with the cached GOP when there is one."""
        sub = Subscription(self, audio_only=audio_only)
        for tag in self._gop:
            sub._offer(tag)
        self._subscribers.add(sub)
        self._notify_consumers()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Detach a subscriber."""
        sub._close()
        if sub in self._subscribers:
            self._subscribers.discard(sub)
            self._notify_consumers()

    def close_subscribers(self) -> None:
        """Close every subscriber, so consumers reconnect and start fresh."""
        for sub in list(self._subscribers):
            self.unsubscribe(sub)

    def _notify_consumers(self) -> None:
        if self.on_consumers_changed is not None:
            self.on_consumers_changed(len(self._subscribers))

    async def wait_for_publish(self, timeout: float) -> bool:
        """Wait until a publisher is connected."""
        if self.publishing:
            return True
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._published.wait()
        return self.publishing

    async def wait_for_headers(self, timeout: float) -> bool:
        """Wait until the AVC decoder configuration has arrived."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._headers_ready.wait()
        return self.avc_header is not None and not self._closed

    async def ensure_publishing(self) -> bool:
        """Make sure the camera is pushing, starting it once for all callers.

        Returns True when a publisher is connected. Concurrent callers share
        one call to ``start_callback``.
        """
        if self.publishing:
            return True
        if self.start_callback is None:
            return False
        task = self._start_task
        if task is None or task.done():
            task = asyncio.ensure_future(self.start_callback())
            self._start_task = task
        try:
            started = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                # The channel closed under us, not our caller being cancelled.
                return False
            raise
        except Exception:
            _LOGGER.debug("Local stream start failed for %s", self.camera_uid, exc_info=True)
            return False
        return started and self.publishing

    def cancel_start(self) -> None:
        """Cancel a camera start in flight (its owner is going away)."""
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()

    def close(self) -> None:
        """Tear the channel down."""
        self._closed = True
        # Wake handlers waiting for headers, or stop() waits on them.
        self._headers_ready.set()
        self._published.set()
        if self._grace_handle is not None:
            self._grace_handle.cancel()
            self._grace_handle = None
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        if self._abort_publisher is not None:
            self._abort_publisher()
        self._publisher_id = None
        self._abort_publisher = None
        self.close_subscribers()
