"""Cloud streams watched over WebRTC (go2rtc), and viewer-initiated stream starts."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Camera imports pull in turbojpeg. Stub it like test_entities.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from homeassistant.core import HomeAssistant
from webrtc_models import RTCIceCandidateInit

from aionanit.models import ConnectionState
from custom_components.nanit.camera import NanitCameraEntity

from .test_entities import _camera_state, _disable_state_writes, _push_coordinator

_CANDIDATE = RTCIceCandidateInit("candidate:1 1 udp 2130706431 192.0.2.1 50000 typ host")


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    _ = enable_custom_integrations


def _entity(
    hass: HomeAssistant | None,
    camera: MagicMock | None = None,
    connection_state: ConnectionState = ConnectionState.CONNECTED,
) -> NanitCameraEntity:
    coordinator = _push_coordinator(_camera_state(connection_state=connection_state))
    entity = NanitCameraEntity(coordinator, camera or MagicMock(uid="cam_1"))
    if hass is not None:
        entity.hass = hass
    _disable_state_writes(entity)
    entity._webrtc_provider = MagicMock(async_on_webrtc_candidate=AsyncMock())
    return entity


def _watched_by_webrtc(entity: NanitCameraEntity, *, age: float = 0.0) -> None:
    """A cached cloud source whose only viewer is a WebRTC session."""
    stream = MagicMock()
    stream.outputs.return_value = {}
    stream.stop = AsyncMock()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cached-url"
    entity._stream_source_started_at = time.monotonic() - age
    entity._webrtc_sessions.add("viewer")


def _camera(start: AsyncMock | None = None, url: str = "rtmps://stream-url") -> MagicMock:
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value=url)
    camera.async_start_streaming = start or AsyncMock()
    return camera


def _blocking_start() -> tuple[AsyncMock, asyncio.Event]:
    """A PUT_STREAMING whose ACK arrives only when the returned event is set."""
    ack = asyncio.Event()

    async def start(**_kwargs: Any) -> None:
        await ack.wait()

    return AsyncMock(side_effect=start), ack


# ---------------------------------------------------------------- sessions


async def test_candidate_registers_session_once_and_forwards(hass: HomeAssistant) -> None:
    entity = _entity(hass)

    await entity.async_on_webrtc_candidate("s1", _CANDIDATE)
    await entity.async_on_webrtc_candidate("s1", _CANDIDATE)

    assert entity._webrtc_sessions == {"s1"}
    assert entity._webrtc_provider.async_on_webrtc_candidate.await_count == 2


async def test_close_drops_session(hass: HomeAssistant) -> None:
    entity = _entity(hass)
    await entity.async_on_webrtc_candidate("s1", _CANDIDATE)

    entity.close_webrtc_session("s1")

    assert entity._webrtc_sessions == set()
    entity._webrtc_provider.async_close_session.assert_called_once_with("s1")


async def test_duplicate_close_is_quiet(hass: HomeAssistant) -> None:
    entity = _entity(hass)
    entity._webrtc_provider.async_close_session.side_effect = KeyError("s1")

    entity.close_webrtc_session("s1")

    assert entity._webrtc_sessions == set()


# --------------------------------------------------------------- keepalive


async def test_keepalive_sent_for_webrtc_only_viewer(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera)
    _watched_by_webrtc(entity)

    entity._handle_stream_keepalive()
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://cached-url", reconnect_on_failure=False
    )
    entity._cancel_stream_timers()


async def test_keepalive_waits_while_camera_reconnects(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera, ConnectionState.RECONNECTING)
    _watched_by_webrtc(entity)

    with patch("custom_components.nanit.camera.async_call_later") as call_later:
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()
    call_later.assert_called_once()  # still rescheduled


async def test_reconnect_resumes_push_for_webrtc_viewer(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera, ConnectionState.RECONNECTING)
    _watched_by_webrtc(entity)

    entity._handle_coordinator_update()  # observes RECONNECTING
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
    entity._handle_coordinator_update()  # RECONNECTING -> CONNECTED
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://cached-url", reconnect_on_failure=False
    )
    entity._cancel_stream_timers()


async def test_webrtc_viewers_do_not_keep_cloud_push_on_local_route(
    hass: HomeAssistant,
) -> None:
    """On the local route WebRTC viewers pull the receiver, not the cloud URL."""
    camera = _camera()
    entity = _entity(hass, camera)
    _watched_by_webrtc(entity)
    entity._local = MagicMock(active=True)
    entity._local.channel.consumer_count = 0

    with patch("custom_components.nanit.camera.async_call_later"):
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()

    camera.async_start_streaming.assert_not_awaited()


# ----------------------------------------------------------- rotation deferral


async def test_rotation_deferred_while_only_webrtc_watches(hass: HomeAssistant) -> None:
    entity = _entity(hass, _camera(url="rtmps://renewed-url"))
    _watched_by_webrtc(entity, age=46 * 60)

    with patch("custom_components.nanit.camera.async_call_later") as call_later:
        entity._handle_stream_expiry()
        await hass.async_block_till_done()

    assert entity._stream_refresh_task is None
    assert entity._cached_stream_source == "rtmps://cached-url"
    assert entity.stream is not None
    call_later.assert_called_once()
    assert call_later.call_args.args[1] == 5 * 60


async def test_rotation_not_deferred_past_token_limit(hass: HomeAssistant) -> None:
    entity = _entity(hass, _camera(url="rtmps://renewed-url"))
    _watched_by_webrtc(entity, age=56 * 60)

    entity._handle_stream_expiry()
    await hass.async_block_till_done()

    # No HLS outputs, so the source is released as for an idle stream.
    assert entity._cached_stream_source is None
    assert entity.stream is None


async def test_rotation_released_once_webrtc_viewers_leave(hass: HomeAssistant) -> None:
    entity = _entity(hass, _camera(url="rtmps://renewed-url"))
    _watched_by_webrtc(entity, age=46 * 60)
    entity.close_webrtc_session("viewer")

    entity._handle_stream_expiry()
    await hass.async_block_till_done()

    assert entity._cached_stream_source is None


async def test_rotation_in_place_is_best_effort(hass: HomeAssistant) -> None:
    camera = _camera(url="rtmps://renewed-url")
    entity = _entity(hass, camera)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://old-url"
    entity._stream_source_started_at = 100.0

    entity._handle_stream_expiry()
    await hass.async_block_till_done()

    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://renewed-url", reconnect_on_failure=False
    )
    stream.update_source.assert_called_once_with("rtmps://renewed-url")
    entity._cancel_stream_timers()


# ------------------------------------------------------------ viewer starts


async def test_start_reconnects_on_late_ack_when_nobody_watches(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera)

    assert await entity.stream_source() == "rtmps://stream-url"

    # The default send: aionanit may reconnect, which stops nobody's push.
    camera.async_start_streaming.assert_awaited_once_with(rtmps_url="rtmps://stream-url")
    entity._cancel_stream_timers()


async def test_start_is_best_effort_while_webrtc_viewers_watch(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera)
    _watched_by_webrtc(entity)

    assert await entity.stream_source() == "rtmps://cached-url"

    camera.async_start_streaming.assert_awaited_once_with(
        rtmps_url="rtmps://cached-url", reconnect_on_failure=False
    )
    entity._cancel_stream_timers()


async def test_start_is_best_effort_while_hls_viewers_watch(hass: HomeAssistant) -> None:
    camera = _camera()
    entity = _entity(hass, camera)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream

    await entity.stream_source()

    assert camera.async_start_streaming.await_args.kwargs["reconnect_on_failure"] is False
    entity._cancel_stream_timers()


async def test_start_returns_url_while_ack_is_pending(hass: HomeAssistant) -> None:
    start, ack = _blocking_start()
    camera = _camera(start)
    entity = _entity(hass, camera)

    with patch("custom_components.nanit.camera._STREAM_START_WAIT", 0.05):
        source = await entity.stream_source()

    assert source == "rtmps://stream-url"
    assert entity._cached_stream_source == "rtmps://stream-url"
    task = entity._stream_start_task
    assert task is not None and not task.done()

    ack.set()
    await hass.async_block_till_done()
    assert task.done() and task.result() is True
    entity._cancel_stream_timers()


async def test_concurrent_viewers_share_one_send(hass: HomeAssistant) -> None:
    start, ack = _blocking_start()
    camera = _camera(start)
    entity = _entity(hass, camera)

    first = hass.async_create_task(entity.stream_source())
    await asyncio.sleep(0)
    second = hass.async_create_task(entity.stream_source())
    await asyncio.sleep(0)
    ack.set()

    assert await first == await second == "rtmps://stream-url"
    camera.async_get_stream_rtmps_url.assert_awaited_once()
    camera.async_start_streaming.assert_awaited_once()
    entity._cancel_stream_timers()


async def test_start_fails_when_every_attempt_fails(hass: HomeAssistant) -> None:
    camera = _camera(AsyncMock(side_effect=RuntimeError("ws closed")))
    entity = _entity(hass, camera)

    with patch("custom_components.nanit.camera._STREAM_RETRY_DELAY", 0):
        assert await entity.stream_source() is None

    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is None


async def test_removal_during_pending_start_leaves_nothing_behind(hass: HomeAssistant) -> None:
    """A start cancelled by removal must not cache the URL or arm timers (#101)."""
    start, _ack = _blocking_start()
    entity = _entity(hass, _camera(start))

    viewer = hass.async_create_task(entity.stream_source())
    await asyncio.sleep(0)
    await entity.async_will_remove_from_hass()

    assert await viewer is None
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is None
    assert entity._cancel_stream_expiry_timer is None


async def test_superseded_start_follows_the_newer_url(hass: HomeAssistant) -> None:
    acks = {"rtmps://a": asyncio.Event(), "rtmps://b": asyncio.Event()}

    async def start(*, rtmps_url: str, **_kwargs: Any) -> None:
        await acks[rtmps_url].wait()

    entity = _entity(hass, _camera(AsyncMock(side_effect=start)))

    older = hass.async_create_task(entity._async_request_stream_start("rtmps://a"))
    await asyncio.sleep(0)
    newer = hass.async_create_task(entity._async_request_stream_start("rtmps://b"))
    await asyncio.sleep(0)
    acks["rtmps://b"].set()

    assert await newer == (True, "rtmps://b")
    # The older caller must not hand its viewer a URL the camera no longer pushes to.
    assert await older == (True, "rtmps://b")


# ------------------------------------------------------------ WebRTC provider


async def test_connect_without_webrtc_checks_providers_again(hass: HomeAssistant) -> None:
    entity = _entity(hass, connection_state=ConnectionState.RECONNECTING)
    entity._webrtc_provider = None

    with patch.object(entity, "async_refresh_providers", new=AsyncMock()) as refresh:
        entity._handle_coordinator_update()
        entity.coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
        entity._handle_coordinator_update()
        await hass.async_block_till_done()

    refresh.assert_awaited_once_with()


async def test_connect_with_webrtc_leaves_providers_alone(hass: HomeAssistant) -> None:
    entity = _entity(hass, connection_state=ConnectionState.RECONNECTING)

    with patch.object(entity, "async_refresh_providers", new=AsyncMock()) as refresh:
        entity._handle_coordinator_update()
        entity.coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
        entity._handle_coordinator_update()
        await hass.async_block_till_done()

    refresh.assert_not_awaited()


def test_first_update_before_hass_attach_is_safe() -> None:
    entity = _entity(None)
    entity._webrtc_provider = None

    entity._handle_coordinator_update()  # prev state None -> CONNECTED, no hass yet

    assert entity._provider_refresh_task is None
