"""WebSocket reconnect / liveness / transaction tests for the S&L transport.

Ported from com6056/nanit-sound-light's offline suite. These run the real
client against an in-process fake relay on 127.0.0.1 (plaintext ws://, which
is why the transport only builds a TLS context for wss://). No real device,
no Nanit cloud (the `block_nanit_network` guard would fail the test).

Covered:
* the reconnect backoff schedule matches the official app (0/2/5/7),
* a control command actually reaches the socket and awaits its ack,
* a slow ack is accepted WITHOUT a re-send (re-sending wedges the device),
* a rejection (non-2xx) raises so the caller can roll back,
* when the server drops the connection, the client reconnects on its own,
* persistent auth rejections back off and quiet the logs.
"""

from __future__ import annotations

import asyncio
import http
import logging

import pytest
import websockets

from custom_components.nanit.aionanit_sl import sound_light_pb2 as pb2
from custom_components.nanit.aionanit_sl import transport as transport_mod

from .test_sl_transport import UID, make_transport

# These suites run real (loopback) sockets against in-process fake servers;
# pytest-socket blocks sockets by default in the HA test environment.
pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _wait_until(predicate, timeout=3.0, interval=0.02):
    async def loop():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(loop(), timeout)


class _FakeNanit:
    """Minimal ws server that records frames and tracks live connections.

    Behaves like the real relay enough to exercise the transport:
    * on connect it sends a `Message{backend{device{status: Connected}}}`
      frame (the readiness gate the client waits for before sending),
    * for each control `Request{settings}` it replies with a
      `Response{requestId, statusCode: 200}` so the client's await-ack
      transaction completes (set `status_code` to simulate a rejection).
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        send_backend: bool = True,
        reject_status: int | None = None,
        reject_first: int = 0,
        reject_always: bool = False,
    ):
        self._status_code = status_code
        self._send_backend = send_backend
        # Handshake rejection (for the auth-reject backoff tests).
        self._reject_status = reject_status
        self._reject_first = reject_first
        self._reject_always = reject_always
        self.handshakes = 0
        self.received: list[bytes] = []
        self.connections: list = []
        self.auth_headers: list[str | None] = []
        self._server = None
        self.port = 0

    async def _process_request(self, connection, request):
        """Reject the handshake with an HTTP status, or return None to accept."""
        self.auth_headers.append(request.headers.get("Authorization"))
        if self._reject_status is None:
            return None
        self.handshakes += 1
        if self._reject_always or self.handshakes <= self._reject_first:
            return connection.respond(http.HTTPStatus(self._reject_status), "rejected\n")
        return None

    async def start(self):
        async def handler(ws, *_args):
            self.connections.append(ws)
            if self._send_backend:
                backend = pb2.Message(
                    backend=pb2.Backend(device=pb2.BackendDevice(status=pb2.Connected))
                )
                await ws.send(backend.SerializeToString())
            try:
                async for msg in ws:
                    self.received.append(msg)
                    await self._maybe_ack(ws, msg)
            except Exception:
                pass

        self._server = await websockets.serve(
            handler, "127.0.0.1", 0, process_request=self._process_request
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def _maybe_ack(self, ws, raw: bytes) -> None:
        """Ack any request the way the device does (Response by requestId)."""
        msg = pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            return
        if not msg.HasField("request"):
            return
        response = pb2.Response(requestId=msg.request.id, statusCode=self._status_code)
        if msg.request.HasField("settings"):
            response.settings.CopyFrom(msg.request.settings)
        await ws.send(pb2.Message(response=response).SerializeToString())

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
async def fake_nanit(monkeypatch):
    server = _FakeNanit()
    await server.start()
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{server.port}")
    yield server
    await server.stop()


def _registered_transport(**kwargs):
    api = make_transport(**kwargs)
    api.register_device(UID)
    return api


def test_reconnect_backoff_matches_app():
    backoff = transport_mod._reconnect_backoff
    assert [backoff(r) for r in (0, 1, 3, 4, 10, 11, 50)] == [0, 2, 2, 5, 5, 7, 7]


async def test_connect_and_send_reaches_socket(fake_nanit):
    api = _registered_transport()

    await api.connect_device(UID)
    assert api.is_websocket_connected(UID)

    await api.send_control_command(UID, is_on=True)

    def got_control():
        for raw in fake_nanit.received:
            msg = pb2.Message()
            try:
                msg.ParseFromString(raw)
            except Exception:
                continue
            if msg.HasField("request") and msg.request.settings.isOn:
                return True
        return False

    await _wait_until(got_control)
    await api.close()


async def test_handshake_presents_provider_token(fake_nanit):
    """The remote handshake carries the injected provider's token, `token` scheme."""
    api = _registered_transport()
    await api.connect_device(UID)
    assert fake_nanit.auth_headers[-1] == "token test-token"
    await api.close()


async def test_provider_failure_is_transient_not_fatal(monkeypatch):
    """An access-token provider failure is a transient connect failure.

    Reauth is the hub's job (aionanit raises through its own coordinators), so
    the transport just logs and retries later, it must not crash or count the
    failure as an auth rejection (which would arm the long cooldown).
    """

    async def failing_provider():
        raise RuntimeError("token backend down")

    api = make_transport(access_token_provider=failing_provider)
    api.register_device(UID)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", "ws://127.0.0.1:1")

    await api.connect_device(UID)  # must not raise
    assert not api.is_websocket_connected(UID)
    key = api._conn_key(UID, "remote")
    assert api._auth_reject_counts.get(key, 0) == 0
    assert api._transient_fail_counts.get(key, 0) == 1

    await api.close()


async def test_reconnects_after_server_drop(fake_nanit):
    api = _registered_transport()

    await api.connect_device(UID)
    await _wait_until(lambda: len(fake_nanit.connections) == 1)
    assert api.is_websocket_connected(UID)

    # Server drops the connection, so the client should reconnect on its own.
    await fake_nanit.connections[0].close()

    await _wait_until(lambda: len(fake_nanit.connections) == 2)
    await _wait_until(lambda: api.is_websocket_connected(UID))

    await api.close()


async def test_connection_change_callback_fires_on_connect_and_drop(fake_nanit):
    """The connectivity callback fires on connect, attach, and socket drop."""
    api = _registered_transport()
    changes: list[bool] = []
    api.set_connection_change_callback(lambda uid: changes.append(api.is_websocket_connected(uid)))

    await api.connect_device(UID)
    await _wait_until(lambda: api.is_device_attached(UID))
    assert changes and changes[0] is True

    changes.clear()
    api._closing = True  # stop reconnects so the drop stays dropped
    await fake_nanit.connections[0].close()
    await _wait_until(lambda: not api.is_websocket_connected(UID))
    await _wait_until(lambda: False in changes)

    await api.close()


async def test_send_raises_when_unreachable(monkeypatch):
    """A control command to a device with no socket must raise, not no-op silently."""
    api = _registered_transport()
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", "ws://127.0.0.1:1")

    with pytest.raises(ConnectionError):
        await api.send_control_command(UID, is_on=True)

    await api.close()


async def test_diagnostics_requests_reach_socket_with_markers(fake_nanit):
    """Battery/wifi/firmware queries serialize the right request bodies/markers."""
    api = _registered_transport()

    await api.connect_device(UID)
    await _wait_until(lambda: api.is_device_attached(UID))

    await api.send_status_request(UID)
    await api.send_network_request(UID)
    await api.send_firmware_request(UID)

    def _decoded():
        out = []
        for raw in fake_nanit.received:
            msg = pb2.Message()
            try:
                msg.ParseFromString(raw)
            except Exception:
                continue
            if msg.HasField("request"):
                out.append(msg.request)
        return out

    await _wait_until(lambda: any(r.HasField("getStatus") and r.getStatus.all for r in _decoded()))
    await _wait_until(
        lambda: any(r.HasField("network") and r.network.HasField("getStatus") for r in _decoded())
    )
    await _wait_until(
        lambda: any(r.HasField("firmware") and r.firmware.HasField("info") for r in _decoded())
    )
    await api.close()


async def test_backend_connected_marks_device_attached(fake_nanit):
    """The relay's backend Connected frame flips is_device_attached (the gate)."""
    api = _registered_transport()

    assert api.is_device_attached(UID) is False
    await api.connect_device(UID)
    await _wait_until(lambda: api.is_device_attached(UID))
    await api.close()


async def test_control_command_awaits_ack_then_returns(fake_nanit):
    """A command resolves only once the matching Response (requestId) arrives."""
    api = _registered_transport()

    await api.connect_device(UID)
    # Returns without raising because the fake server acks with statusCode 200.
    await api.send_control_command(UID, is_on=True)
    await api.close()


async def _serve(monkeypatch, **kwargs):
    server = _FakeNanit(**kwargs)
    await server.start()
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{server.port}")
    return server


async def test_control_command_rejection_raises(monkeypatch):
    """A non-2xx ack from the device surfaces as an error (so the UI rolls back)."""
    server = await _serve(monkeypatch, status_code=500)
    api = _registered_transport()

    await api.connect_device(UID)
    with pytest.raises(ConnectionError):
        await api.send_control_command(UID, is_on=True)

    await api.close()
    await server.stop()


async def test_slow_ack_is_accepted_without_resend(monkeypatch):
    """A slow/absent ack on a LIVE socket does NOT raise and does NOT re-send.

    The device is busy, not gone. Re-sending piles duplicates onto an already
    overloaded device (which wedges it). The command is accepted optimistically,
    and exactly one control frame reaches the wire.
    """
    monkeypatch.setattr(transport_mod, "COMMAND_ACK_TIMEOUT", 0.3)
    # Server attaches (so the gate passes) but never acks a control request.
    server = await _serve(monkeypatch, send_backend=True)
    server._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)  # swallow, never reply
    api = _registered_transport()

    await api.connect_device(UID)
    await _wait_until(lambda: api.is_device_attached(UID))
    # Returns without raising despite the missing ack.
    await api.send_control_command(UID, is_on=True)

    # Exactly one control frame on the wire, no duplicate re-send.
    controls = 0
    for raw in server.received:
        msg = pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            continue
        if msg.HasField("request") and msg.request.HasField("settings"):
            controls += 1
    assert controls == 1

    await api.close()
    await server.stop()


async def test_command_sends_best_effort_when_no_backend_frame(monkeypatch):
    """If the relay never sends a Connected frame, the gate falls back to a
    best-effort send (a missed/renamed frame must not brick control)."""
    monkeypatch.setattr(transport_mod, "DEVICE_ATTACH_TIMEOUT", 0.3)
    # Server never sends the backend frame, but still acks control requests.
    server = await _serve(monkeypatch, send_backend=False)
    api = _registered_transport()

    await api.connect_device(UID)
    assert api.is_device_attached(UID) is False  # never got a backend frame
    # Sends anyway after the short attach timeout, and the ack resolves it.
    await api.send_control_command(UID, is_on=True)
    # The ack (a Response) also defensively inferred attachment.
    assert api.is_device_attached(UID) is True

    await api.close()
    await server.stop()


async def test_persistent_remote_auth_reject_quiets_logs(monkeypatch, caplog):
    """A relay that keeps rejecting the handshake (401/403/404) is logged loudly
    only for the first few attempts, then one WARNING, then debug, so a wedged
    device can't flood the log with one ERROR per retry."""
    server = await _serve(monkeypatch, reject_status=403, reject_always=True)
    api = _registered_transport()
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    device_info = api._device_list[0]
    key = api._conn_key(UID, "remote")

    threshold = transport_mod.AUTH_REJECT_BACKOFF_THRESHOLD
    with caplog.at_level(logging.DEBUG):
        for _ in range(threshold + 3):
            await api._connect_transport(device_info, "remote")

    # The first `threshold` attempts each hit the relay and were rejected; the
    # threshold attempt armed the cooldown, so the remaining calls short-circuit
    # before the handshake. The counter and the handshake count stop climbing.
    assert api._auth_reject_counts[key] == threshold
    assert server.handshakes == threshold
    api_errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name.endswith(".transport")
    ]
    api_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith(".transport")
    ]
    # Loud ERROR for the first (threshold - 1) attempts, then a single WARNING at
    # the threshold. So ERROR lines are bounded, not one per attempt.
    assert len(api_errors) == threshold - 1
    assert len(api_warnings) == 1

    await api.close()
    await server.stop()


async def test_persistent_auth_reject_escalates_reconnect_interval(monkeypatch):
    """Once consecutive auth rejections cross the threshold, the reconnect loop
    switches from the fast app-matching backoff to the long, quiet interval."""
    api = _registered_transport()

    async def fake_connect_transport(device_info, transport):
        # Simulate _handle_auth_reject's effect without real sockets: the
        # transport never connects and the auth-reject counter climbs.
        ck = api._conn_key(device_info["speaker_uid"], transport)
        api._auth_reject_counts[ck] = api._auth_reject_counts.get(ck, 0) + 1

    monkeypatch.setattr(api, "_connect_transport", fake_connect_transport)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)
        # Stop after the long interval has been used a couple of times.
        if delays.count(transport_mod.AUTH_REJECT_RETRY_INTERVAL) >= 2:
            api._closing = True
        await real_sleep(0)

    monkeypatch.setattr(transport_mod.asyncio, "sleep", fake_sleep)

    await api._reconnect_with_backoff(UID, "remote")

    assert delays[0] != transport_mod.AUTH_REJECT_RETRY_INTERVAL  # started fast
    assert transport_mod.AUTH_REJECT_RETRY_INTERVAL in delays  # escalated
    assert delays[-1] == transport_mod.AUTH_REJECT_RETRY_INTERVAL


async def test_executor_shutdown_during_connect_is_quiet(monkeypatch, caplog):
    """A reconnect racing HA's executor teardown (restart/stop) must not log
    ERROR or count as a transient failure. Observed in production as 'Executor
    shutdown has been called' noise during HA restarts."""
    api = _registered_transport()
    device_info = api._device_list[0]
    # wss:// so the connect path needs the executor-built TLS context.
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", "wss://127.0.0.1:1")

    loop = asyncio.get_running_loop()

    def shutdown_executor(*_a, **_k):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(loop, "run_in_executor", shutdown_executor)

    with caplog.at_level(logging.DEBUG):
        await api._connect_transport(device_info, "remote")

    loud = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name.endswith(".transport")
    ]
    assert not loud
    assert api._transient_fail_counts == {}  # not treated as a device failure

    await api.close()


async def test_close_waits_for_connection_tasks(fake_nanit):
    """close() awaits its cancelled handler/reconnect tasks, so nothing from
    the old instance is still unwinding when a reload builds the next one."""
    api = _registered_transport()

    await api.connect_device(UID)
    handler_tasks = list(api._handler_tasks.values())
    assert handler_tasks

    await api.close()
    assert all(task.done() for task in handler_tasks)


async def test_repeated_transient_remote_failures_quiet_logs(monkeypatch, caplog):
    """A remote transport failing transiently (refused/outage) logs ERROR only
    for the first few attempts, then one WARNING, then debug. Only the log
    level is throttled. Every call below still attempts a real connect
    (unlike the auth-reject cooldown, which short-circuits attempts)."""
    api = _registered_transport()
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    device_info = api._device_list[0]
    # Nothing listens here: every connect attempt fails fast (refused).
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", "ws://127.0.0.1:1")
    key = api._conn_key(UID, "remote")

    threshold = transport_mod.TRANSIENT_FAIL_LOG_THRESHOLD
    attempts = threshold + 3
    with caplog.at_level(logging.DEBUG):
        for _ in range(attempts):
            await api._connect_transport(device_info, "remote")

    assert api._transient_fail_counts[key] == attempts  # no attempt was skipped
    api_errors = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.name.endswith(".transport")
    ]
    api_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name.endswith(".transport")
    ]
    assert len(api_errors) == threshold - 1
    assert len(api_warnings) == 1

    await api.close()


async def test_inflight_command_fails_fast_on_socket_drop(monkeypatch):
    """A command awaiting an ack is failed promptly when the socket drops,
    instead of waiting out the full ack timeout."""
    monkeypatch.setattr(transport_mod, "COMMAND_ACK_TIMEOUT", 30)  # the drop must win
    server = await _serve(monkeypatch, send_backend=True)
    server._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)  # never acks
    api = _registered_transport()

    await api.connect_device(UID)
    await _wait_until(lambda: api.is_device_attached(UID))

    send_task = asyncio.ensure_future(api.send_control_command(UID, is_on=True))
    await _wait_until(lambda: bool(api._pending_responses.get(UID)))

    # Stop reconnects first (so the dropped socket isn't immediately re-dialed
    # mid-teardown), then drop the connection while the command awaits its ack.
    api._closing = True
    await server.connections[0].close()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(send_task, timeout=5)  # well under the 30s ack timeout

    await api.close()
    await server.stop()
