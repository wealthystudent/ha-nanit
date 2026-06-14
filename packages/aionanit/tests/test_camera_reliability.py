from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiohttp
import pytest

from aionanit.auth import TokenManager
from aionanit.camera import (
    _FRESH_CONNECTION_WINDOW,
    GetStatus,
    NanitCamera,
    RequestType,
    Response,
)
from aionanit.exceptions import NanitTransportError
from aionanit.models import TransportKind
from aionanit.rest import NanitRestClient


def _make_camera() -> tuple[NanitCamera, MagicMock]:
    session = MagicMock(spec=aiohttp.ClientSession)
    rest = MagicMock(spec=NanitRestClient)
    token_manager = MagicMock(spec=TokenManager)
    token_manager.async_get_access_token = AsyncMock(return_value="test_token")

    camera = NanitCamera(
        uid="cam_uid_1",
        baby_uid="baby_uid_1",
        token_manager=token_manager,
        rest_client=rest,
        session=session,
        prefer_local=False,
    )
    return camera, token_manager


@pytest.mark.asyncio
async def test_token_refresh_task_started_on_start() -> None:
    camera, token_manager = _make_camera()
    token_manager._expires_at = time.monotonic() + 3600.0

    camera._transport = MagicMock()
    camera._transport.async_connect_cloud = AsyncMock()
    camera._transport.transport_kind = TransportKind.CLOUD
    camera._transport.connected = True

    camera._async_request_initial_state = AsyncMock()
    camera._async_enable_sensor_push = AsyncMock()
    camera._start_health_check = MagicMock()
    camera._start_sensor_poll = MagicMock()

    await camera.async_start()

    assert camera._token_refresh_task is not None
    assert not camera._token_refresh_task.done()

    camera._cancel_token_refresh()
    camera._cancel_playback_poll()
    camera._cancel_health_check()
    camera._cancel_sensor_poll()


@pytest.mark.asyncio
async def test_token_refresh_task_cancelled_on_stop() -> None:
    camera, token_manager = _make_camera()
    token_manager._expires_at = time.monotonic() + 3600.0

    camera._transport = MagicMock()
    camera._transport.async_close = AsyncMock()

    camera._start_token_refresh()
    refresh_task = camera._token_refresh_task

    await camera.async_stop()
    await asyncio.sleep(0)

    assert camera._token_refresh_task is None
    assert refresh_task is not None
    assert refresh_task.cancelled()


@pytest.mark.asyncio
async def test_token_refresh_forces_reconnect_before_expiry() -> None:
    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager._expires_at = 1050.0

    camera._transport = MagicMock()
    camera._transport.connected = True

    async def _force_and_stop() -> None:
        camera._stopped = True

    camera._transport.async_force_reconnect = AsyncMock(side_effect=_force_and_stop)

    with (
        patch("aionanit.camera.time.monotonic", return_value=1000.0),
        patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)),
    ):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    camera._transport.async_force_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_request_waits_for_reconnection() -> None:
    camera, _ = _make_camera()

    camera._transport = MagicMock()
    camera._transport.connected = False
    camera._transport.idle_seconds = 0.0

    response = Response(status_code=200)

    async def _fake_send(_: bytes) -> None:
        camera._pending.resolve(1, response)

    camera._transport.async_send = AsyncMock(side_effect=_fake_send)
    camera._async_reconnect = AsyncMock()
    camera._connected_event.clear()

    async def _simulate_reconnect() -> None:
        await asyncio.sleep(0.01)
        camera._transport.connected = True
        camera._connected_event.set()

    reconnect_task = asyncio.create_task(_simulate_reconnect())

    result = await camera._send_request(
        RequestType.GET_STATUS,
        get_status=GetStatus(all=True),
    )
    await reconnect_task

    assert result.status_code == 200
    camera._async_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_request_timeout_when_not_reconnecting() -> None:
    camera, _ = _make_camera()

    camera._transport = MagicMock()
    camera._transport.connected = False
    camera._transport.idle_seconds = 0.0
    camera._transport.async_send = AsyncMock()
    camera._connected_event.clear()
    camera._connected_event.wait = AsyncMock(side_effect=TimeoutError)
    camera._async_reconnect = AsyncMock(side_effect=NanitTransportError("reconnect failed"))

    with pytest.raises(NanitTransportError, match="reconnect failed"):
        await camera._send_request(
            RequestType.GET_STATUS,
            get_status=GetStatus(all=True),
        )

    camera._async_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_skips_when_lock_already_held() -> None:
    """Reentrant _async_reconnect returns immediately instead of deadlocking."""
    camera, _ = _make_camera()

    await camera._reconnect_lock.acquire()
    try:
        await asyncio.wait_for(camera._async_reconnect(), timeout=1.0)
    finally:
        camera._reconnect_lock.release()


@pytest.mark.asyncio
async def test_reconnect_completes_with_unresponsive_camera() -> None:
    """_async_reconnect must not deadlock when the camera never responds.

    Regression for issue #80: an offline camera whose cloud relay accepts
    the WebSocket but never answers protobuf requests caused a reentrant
    lock acquire via _async_enable_sensor_push → _send_request → timeout
    → _async_reconnect.
    """
    camera, _ = _make_camera()
    camera._stopped = False

    transport = MagicMock()
    transport.connected = True
    type(transport).idle_seconds = PropertyMock(
        return_value=_FRESH_CONNECTION_WINDOW + 1.0,
    )
    transport.transport_kind = TransportKind.CLOUD
    transport.async_connect_cloud = AsyncMock()
    transport.async_send = AsyncMock()
    transport.async_close = AsyncMock()
    camera._transport = transport

    _orig = camera._send_request

    async def _fast_send(request_type, timeout=0.05, **kw):
        return await _orig(request_type, timeout=timeout, **kw)

    camera._send_request = _fast_send

    await asyncio.wait_for(camera._async_reconnect(), timeout=5.0)
