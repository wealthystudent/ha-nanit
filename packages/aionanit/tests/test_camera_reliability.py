from __future__ import annotations

import asyncio
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
from aionanit.exceptions import (
    NanitCameraUnavailable,
    NanitConnectionError,
    NanitTransportError,
)
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
    token_manager.expires_in = 3600.0

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
    token_manager.expires_in = 3600.0

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
    token_manager.expires_in = 50.0

    camera._transport = MagicMock()
    camera._transport.connected = True

    async def _force_and_stop() -> None:
        camera._stopped = True

    camera._transport.async_force_reconnect = AsyncMock(side_effect=_force_and_stop)

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    camera._transport.async_force_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_refresh_skips_reconnect_when_token_already_fresh() -> None:
    """A token refreshed elsewhere while sleeping must not force a reconnect."""
    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager.expires_in = 3600.0

    camera._transport = MagicMock()
    camera._transport.connected = True
    camera._transport.async_force_reconnect = AsyncMock()

    sleep_calls = 0

    async def _sleep_then_stop(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            camera._stopped = True

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(side_effect=_sleep_then_stop)):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    camera._transport.async_force_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_inline_reconnect_schedules_background_recovery() -> None:
    """When the inline reconnect fails, a transport backoff loop must be
    restored so the camera recovers once the network returns."""
    camera, _ = _make_camera()
    camera._stopped = False

    transport = MagicMock()
    transport.connected = False
    transport.idle_seconds = 0.0
    transport.transport_kind = TransportKind.CLOUD
    transport.async_connect_cloud = AsyncMock(side_effect=NanitConnectionError("down"))
    transport.schedule_reconnect = MagicMock()
    camera._transport = transport

    with pytest.raises(NanitCameraUnavailable):
        await camera._async_reconnect(force=True)

    transport.schedule_reconnect.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_schedules_recovery_when_disconnected() -> None:
    """The health check acts as a watchdog for a stranded transport."""
    camera, _ = _make_camera()
    camera._stopped = False

    transport = MagicMock()
    transport.connected = False
    transport.schedule_reconnect = MagicMock(side_effect=lambda: setattr(camera, "_stopped", True))
    camera._transport = transport

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)):
        await asyncio.wait_for(camera._health_check_loop(), timeout=0.1)

    transport.schedule_reconnect.assert_called_once()


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
async def test_concurrent_reconnect_callers_await_one_shared_result() -> None:
    """Concurrent recovery callers must not continue before reconnect completes."""
    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager.expires_in = 3600.0
    gate = asyncio.Event()

    transport = MagicMock()
    transport.connected = False
    transport.idle_seconds = 999.0
    transport.transport_kind = TransportKind.CLOUD

    async def _wait_for_gate(*_args: object) -> None:
        await gate.wait()

    transport.async_connect_cloud = AsyncMock(side_effect=_wait_for_gate)
    camera._transport = transport
    camera._async_enable_sensor_push = AsyncMock()

    first = asyncio.create_task(camera._async_reconnect(force=True))
    await asyncio.sleep(0)
    second = asyncio.create_task(camera._async_reconnect(force=True))
    await asyncio.sleep(0)

    first_was_waiting = not first.done()
    second_was_waiting = not second.done()
    connect_count_while_blocked = transport.async_connect_cloud.await_count

    gate.set()
    await asyncio.gather(first, second)

    camera._cancel_token_refresh()
    camera._cancel_sensor_poll()
    camera._cancel_playback_poll()

    assert first_was_waiting
    assert second_was_waiting
    assert connect_count_while_blocked == 1
    assert transport.async_connect_cloud.await_count == 1


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

    # Clean up background tasks spawned during reconnect.
    camera._cancel_token_refresh()
    camera._cancel_health_check()
    camera._cancel_sensor_poll()
    camera._cancel_playback_poll()
    camera._cancel_local_probe()


@pytest.mark.asyncio
async def test_token_refresh_refreshes_with_headroom_then_reconnects_once() -> None:
    """The pre-emptive loop refreshes the token FIRST, then reconnects once.

    Relying on the reconnect's own token fetch (min_ttl=60) reconnected with
    the old token, churning a reconnect per minute and pushing the real
    refresh into the final minute before expiry.
    """
    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager.expires_in = 50.0
    camera._transport = MagicMock()
    camera._transport.connected = True

    order: list[str] = []

    async def _record_refresh(min_ttl: float = 60.0) -> str:
        order.append(f"refresh:{min_ttl}")
        return "tok"

    token_manager.async_get_access_token = AsyncMock(side_effect=_record_refresh)

    async def _force_and_stop() -> None:
        order.append("reconnect")
        camera._stopped = True

    camera._transport.async_force_reconnect = AsyncMock(side_effect=_force_and_stop)

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    assert order == ["refresh:360.0", "reconnect"]


@pytest.mark.asyncio
async def test_token_refresh_transient_failure_retries_without_reconnect() -> None:
    """A transient refresh failure retries shortly and never bounces the socket."""
    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager.expires_in = 50.0
    camera._transport = MagicMock()
    camera._transport.connected = True
    camera._transport.async_force_reconnect = AsyncMock()

    calls = 0

    async def _fail_then_stop(min_ttl: float = 60.0) -> str:
        nonlocal calls
        calls += 1
        if calls >= 2:
            camera._stopped = True
        raise NanitConnectionError("dns down")

    token_manager.async_get_access_token = AsyncMock(side_effect=_fail_then_stop)

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    camera._transport.async_force_reconnect.assert_not_awaited()
    assert calls >= 2


@pytest.mark.asyncio
async def test_token_refresh_auth_rejection_stops_loop() -> None:
    """A genuine rejection ends the loop; consumer paths surface the reauth."""
    from aionanit.exceptions import NanitAuthError

    camera, token_manager = _make_camera()
    camera._stopped = False
    token_manager.expires_in = 50.0
    camera._transport = MagicMock()
    camera._transport.connected = True
    camera._transport.async_force_reconnect = AsyncMock()

    token_manager.async_get_access_token = AsyncMock(side_effect=NanitAuthError("rejected"))

    with patch("aionanit.camera.asyncio.sleep", AsyncMock(return_value=None)):
        await asyncio.wait_for(camera._token_refresh_loop(), timeout=0.1)

    camera._transport.async_force_reconnect.assert_not_awaited()
