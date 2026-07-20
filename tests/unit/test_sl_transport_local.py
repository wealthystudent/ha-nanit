"""Tests for the S&L transport's direct-LAN (local) path.

Ported from com6056/nanit-sound-light's offline suite. These exercise the
local socket the transport prefers over the cloud relay: the deterministic
mDNS URL, the trust-all TLS context, the injected device-token fetcher, the
manual-IP override, and the prefer-local / fall-back-to-remote send routing.
As with the reconnect tests they run the real client against in-process fake
servers on 127.0.0.1 (plaintext ws://), never a real device or the Nanit
cloud (the `block_nanit_network` guard would fail the test if it tried).
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError

from custom_components.nanit.aionanit_sl import sound_light_pb2 as pb2
from custom_components.nanit.aionanit_sl import transport as transport_mod
from custom_components.nanit.aionanit_sl.transport import SoundLightTransport

from .test_sl_transport import UID, make_transport
from .test_sl_transport_reconnect import _FakeNanit, _wait_until

# These suites run real (loopback) sockets against in-process fake servers;
# pytest-socket blocks sockets by default in the HA test environment.
pytestmark = pytest.mark.usefixtures("socket_enabled")


def _has_control(server: _FakeNanit, **fields) -> bool:
    """True if the server received a control Request{settings} matching fields."""
    for raw in server.received:
        msg = pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            continue
        if not (msg.HasField("request") and msg.request.HasField("settings")):
            continue
        settings = msg.request.settings
        if all(getattr(settings, k) == v for k, v in fields.items()):
            return True
    return False


def _local_key(api):
    return api._conn_key(UID, "local")


def _remote_key(api):
    return api._conn_key(UID, "remote")


def _fake_jwt(exp: int) -> str:
    """A structurally valid JWT whose payload carries the given exp claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload.decode()}.sig"


# ---------------------------------------------------------------------------
# Pure helpers (no sockets)
# ---------------------------------------------------------------------------


def test_local_ws_url_is_deterministic_mdns():
    """Local URL is wss://Nanit-<speaker_uid>.local:442 with NO path."""
    api = make_transport()
    assert api._local_ws_url(UID) == "wss://Nanit-SPK123.local:442"


def test_insecure_ssl_context_disables_verification():
    """The LOCAL TLS context accepts any cert + any hostname (matches the app)."""
    ctx = SoundLightTransport._build_insecure_ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_active_key_prefers_local_then_remote():
    """_active_connection_key returns local when present, else remote, else None."""
    api = make_transport()
    assert api._active_connection_key(UID) is None

    # Fake "open" sockets via a stub that reports not-closed.
    class _Open:
        state = None

    api._is_websocket_closed = lambda ws: ws is None  # treat any obj as open
    api._websockets[_remote_key(api)] = _Open()
    assert api._active_connection_key(UID) == _remote_key(api)
    api._websockets[_local_key(api)] = _Open()
    assert api._active_connection_key(UID) == _local_key(api)


def test_active_transport_maps_local_and_cloud():
    """active_transport() exposes 'local'/'cloud'/None for the connection sensor."""
    api = make_transport()
    assert api.active_transport(UID) is None

    class _Open:
        state = None

    api._is_websocket_closed = lambda ws: ws is None
    api._websockets[_remote_key(api)] = _Open()
    assert api.active_transport(UID) == "cloud"  # remote transport -> "cloud"
    api._websockets[_local_key(api)] = _Open()
    assert api.active_transport(UID) == "local"  # local preferred


# ---------------------------------------------------------------------------
# Device-token fetch (injected fetcher)
# ---------------------------------------------------------------------------


async def test_device_token_is_fetched_and_cached():
    """The injected fetcher supplies the local token, cached until its JWT exp."""
    fetcher = AsyncMock(return_value=_fake_jwt(exp=9_999_999_999))
    api = make_transport(device_token_fetcher=fetcher)

    token = await api._ensure_device_token(UID)
    assert token == fetcher.return_value
    # Cached (JWT exp far in the future), so a second call does not re-fetch.
    assert await api._ensure_device_token(UID) == token
    assert fetcher.await_count == 1


async def test_expired_device_token_is_refetched():
    """A cached token past its JWT exp is refreshed on the next use."""
    fetcher = AsyncMock(return_value=_fake_jwt(exp=9_999_999_999))
    api = make_transport(device_token_fetcher=fetcher)
    api._device_tokens[UID] = ("stale", 1.0)  # expired long ago

    token = await api._ensure_device_token(UID)
    assert token == fetcher.return_value
    assert fetcher.await_count == 1


async def test_non_jwt_device_token_is_cached_without_expiry():
    """A token without a parsable exp claim is cached (invalidated on 401/403)."""
    fetcher = AsyncMock(return_value="opaque-token")
    api = make_transport(device_token_fetcher=fetcher)
    assert await api._ensure_device_token(UID) == "opaque-token"
    assert api._device_tokens[UID] == ("opaque-token", None)


async def test_device_token_fetch_failure_leaves_no_token():
    """A fetcher failure (404, network) leaves the device on the relay."""
    fetcher = AsyncMock(side_effect=RuntimeError("404 from udtokens"))
    api = make_transport(device_token_fetcher=fetcher)
    assert await api._ensure_device_token(UID) is None


# ---------------------------------------------------------------------------
# mDNS resolver injection + manual IP override
# ---------------------------------------------------------------------------


async def test_resolver_substitutes_ip_into_local_url(monkeypatch):
    """When a resolver is injected, local connects to wss://<resolved-ip>:442."""
    api = make_transport()
    api.register_device(UID)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    api._device_tokens[UID] = ("dev-tok", None)

    async def resolver(speaker_uid):
        assert speaker_uid == UID
        return "10.0.0.5"

    api.set_local_host_resolver(resolver)

    captured = {}

    async def fake_connect(url, **_kw):
        captured["url"] = url
        raise RuntimeError("stop after capture")  # short-circuit before handler

    monkeypatch.setattr(transport_mod.websockets, "connect", fake_connect)
    await api._connect_transport(api._device_list[0], "local")

    assert captured["url"] == "wss://10.0.0.5:442"


async def test_manual_device_ip_overrides_resolver(monkeypatch):
    """A configured speaker IP wins over mDNS: connect straight to it."""
    api = make_transport()
    api.register_device(UID, device_ip="192.168.1.77")
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    api._device_tokens[UID] = ("dev-tok", None)

    async def resolver(_uid):
        pytest.fail("resolver must not be consulted when a manual IP is set")

    api.set_local_host_resolver(resolver)

    captured = {}

    async def fake_connect(url, **_kw):
        captured["url"] = url
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(transport_mod.websockets, "connect", fake_connect)
    await api._connect_transport(api._device_list[0], "local")

    assert captured["url"] == "wss://192.168.1.77:442"


async def test_resolver_failure_stays_on_relay(monkeypatch):
    """If the resolver can't find the device, local connect is skipped entirely."""
    api = make_transport()
    api.register_device(UID)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    api._device_tokens[UID] = ("dev-tok", None)

    async def resolver(_host):
        return None

    api.set_local_host_resolver(resolver)

    called = {"connect": False}

    async def fake_connect(_url, **_kw):
        called["connect"] = True
        raise RuntimeError("should not be reached")

    monkeypatch.setattr(transport_mod.websockets, "connect", fake_connect)
    await api._connect_transport(api._device_list[0], "local")

    assert called["connect"] is False
    assert not api._transport_connected(_local_key(api))


# ---------------------------------------------------------------------------
# Prefer-local / failover routing (two fake servers)
# ---------------------------------------------------------------------------


async def _connect_both(monkeypatch, *, local_enabled=True):
    """Start local+remote fakes, wire the client to them, return (api, local, remote)."""
    local = _FakeNanit()
    remote = _FakeNanit()
    await local.start()
    await remote.start()
    monkeypatch.setattr(transport_mod, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{remote.port}")
    api = make_transport(local_enabled=local_enabled)
    api.register_device(UID)
    # Preload the device token so local is eligible without a fetch.
    api._device_tokens[UID] = ("dev-tok", None)
    monkeypatch.setattr(api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}")
    return api, local, remote


async def test_prefers_local_for_sends(monkeypatch):
    api, local, remote = await _connect_both(monkeypatch)

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    await api.send_control_command(UID, is_on=True)

    assert _has_control(local, isOn=True)
    assert not _has_control(remote, isOn=True)

    await api.close()
    await local.stop()
    await remote.stop()


async def test_falls_back_to_remote_when_local_down(monkeypatch):
    api, local, remote = await _connect_both(monkeypatch)

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    # Local server goes away entirely, so the client should route sends to remote.
    await local.stop()
    await _wait_until(lambda: not api._transport_connected(_local_key(api)))

    await api.send_control_command(UID, is_on=False)
    assert _has_control(remote, isOn=False)

    await api.close()
    await remote.stop()


async def test_resends_on_surviving_transport_when_inflight_socket_drops(monkeypatch):
    """A command whose socket drops mid-flight re-sends on the other transport.

    Covers the redundant-drop re-send path in _transact. Here both transports
    are up, the in-flight (local) socket dies, and the command must land on
    remote and succeed without a rollback.
    """
    api, local, remote = await _connect_both(monkeypatch)

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    # Local is preferred. Let it accept the send but never ack, so the command
    # sits in flight on local while remote keeps acking normally.
    local._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)

    send = asyncio.ensure_future(api.send_control_command(UID, is_on=True))
    await _wait_until(lambda: api._inflight_conn_key.get(UID) == _local_key(api))

    # Drop the local socket mid-flight while remote stays up.
    await local.stop()

    # The command re-sends on remote and returns without raising (no rollback).
    await asyncio.wait_for(send, timeout=5)
    assert _has_control(remote, isOn=True)

    await api.close()
    await remote.stop()


async def test_send_time_close_fails_over_to_surviving_transport(monkeypatch):
    """A socket that dies exactly at send time still fails over.

    websockets' send() raises ConnectionClosed (NOT a ConnectionError) when the
    socket dropped between _transact's liveness check and the write. That must
    take the same re-send-on-the-surviving-transport path as a drop while
    awaiting the ack.
    """
    api, local, remote = await _connect_both(monkeypatch)

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    local_ws = api._websockets[_local_key(api)]
    real_close = local_ws.close

    async def dying_send(_data):
        # Close for real (so the liveness check sees CLOSED on the retry),
        # then raise the way send() does on a just-closed connection.
        await real_close()
        raise ConnectionClosedError(None, None)

    monkeypatch.setattr(local_ws, "send", dying_send)

    # Must complete without raising: attempt 1 dies at send time on local,
    # attempt 2 lands on remote and is acked.
    await asyncio.wait_for(api.send_control_command(UID, is_on=True), timeout=5)
    assert _has_control(remote, isOn=True)

    await api.close()
    await local.stop()
    await remote.stop()


async def test_device_still_available_while_one_transport_down(monkeypatch):
    """is_websocket_connected stays True if ANY transport is up (availability)."""
    api, local, remote = await _connect_both(monkeypatch)

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    assert api.is_websocket_connected(UID)

    await local.stop()
    await _wait_until(lambda: not api._transport_connected(_local_key(api)))
    # Remote still up -> device still reachable, still attached (sticky).
    assert api.is_websocket_connected(UID)
    assert api.is_device_attached(UID)

    await api.close()
    await remote.stop()


async def test_local_403_invalidates_device_token_and_refetches(monkeypatch):
    """A local handshake rejected with 403 drops the cached device token so the
    next attempt refetches a fresh one.

    The device rotates the per-device token server-side, and it can rotate
    before our cached copy's clock expiry, so without invalidation-on-403 we
    would keep presenting a stale token and loop on 403 forever.
    """
    local = _FakeNanit(reject_status=403, reject_first=1)
    await local.start()

    fetcher = AsyncMock(return_value="FRESH")
    api = make_transport(device_token_fetcher=fetcher)
    api.register_device(UID)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    # A stale cached token with no clock expiry: _ensure_device_token would
    # keep serving it indefinitely without the invalidation fix.
    api._device_tokens[UID] = ("STALE", None)
    monkeypatch.setattr(api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}")
    local_key = _local_key(api)
    device_info = api._device_list[0]

    # First attempt presents the stale token and is rejected 403.
    await api._connect_transport(device_info, "local")
    assert not api._transport_connected(local_key)
    assert UID not in api._device_tokens  # token invalidated on 403
    assert api._auth_reject_counts[local_key] == 1  # rejection counted
    assert fetcher.await_count == 0  # the stale cached token was used, no refetch yet

    # Second attempt refetches a fresh token and connects (server now accepts).
    await api._connect_transport(device_info, "local")
    await _wait_until(lambda: api._transport_connected(local_key))
    assert fetcher.await_count == 1  # refetched after invalidation
    assert api._device_tokens[UID][0] == "FRESH"
    assert local_key not in api._auth_reject_counts  # reset on a clean connect

    await api.close()
    await local.stop()


async def test_local_auth_reject_cooldown_stops_token_refetch(monkeypatch):
    """Once local auth rejections cross the threshold, further connect attempts
    are skipped during the cooldown, so a wedged device stops refetching the
    device token from the cloud.

    This covers the poll-path gap: the periodic poll drives
    ensure_websocket_connection -> connect_device -> _connect_transport, which
    is NOT the reconnect loop, so without a time-based gate it would keep
    hitting the cloud token endpoint every cycle.
    """
    local = _FakeNanit(reject_status=403, reject_always=True)
    await local.start()
    fetcher = AsyncMock(return_value="T")
    api = make_transport(device_token_fetcher=fetcher)
    api.register_device(UID)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    monkeypatch.setattr(api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}")
    local_key = _local_key(api)
    device_info = api._device_list[0]
    threshold = transport_mod.AUTH_REJECT_BACKOFF_THRESHOLD

    # Drive attempts up to the threshold. Each rejected attempt refetches the
    # token once (the 403 invalidated the cache), so the cloud is hit each time.
    for _ in range(threshold):
        await api._connect_transport(device_info, "local")
    assert api._auth_reject_counts[local_key] == threshold
    assert local_key in api._auth_reject_until  # cooldown armed
    fetches = fetcher.await_count
    handshakes = local.handshakes

    # Further attempts during the cooldown short-circuit: no new token fetch,
    # no new handshake. This is the poll hammering the wedged device.
    for _ in range(5):
        await api._connect_transport(device_info, "local")
    assert fetcher.await_count == fetches  # cloud not hit again
    assert local.handshakes == handshakes  # no further connect attempts

    # When the cooldown elapses the gate reopens and a connect is attempted
    # again (proving it is time-based, not a permanent lockout).
    api._auth_reject_until[local_key] = 0
    await api._connect_transport(device_info, "local")
    assert local.handshakes == handshakes + 1

    await api.close()
    await local.stop()


async def test_local_disabled_connects_remote_only(monkeypatch):
    api, local, remote = await _connect_both(monkeypatch, local_enabled=False)
    # If local were attempted this would raise. With local disabled it must not be.
    monkeypatch.setattr(
        api,
        "_local_ws_url",
        lambda _uid: pytest.fail("local must not be attempted when disabled"),
    )

    await api.connect_device(UID)
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))
    assert not api._transport_connected(_local_key(api))

    await api.close()
    await local.stop()
    await remote.stop()


# ---------------------------------------------------------------------------
# Initial-connect failures arm the retry loop (regression: local 403 at
# startup was never retried while the relay was up, found on real hardware)
# ---------------------------------------------------------------------------


async def test_initial_local_403_is_retried_and_recovers(monkeypatch):
    """A local 403 on the FIRST-EVER connect self-heals without any poll.

    The rejected attempt must arm the per-transport reconnect loop, which
    refetches a fresh device token (the 403 invalidated the cache) and
    connects. Before the fix nothing retried: the drop-driven reconnect only
    covers sockets that had connected, and the poll skips reconnects while
    the other transport is up.
    """
    monkeypatch.setattr(transport_mod, "_LOCAL_BACKOFF_SCHEDULE", (0, 0.05, 0.05, 0.05))
    local = _FakeNanit(reject_status=403, reject_first=1)
    await local.start()

    fetcher = AsyncMock(return_value="FRESH")
    api = make_transport(device_token_fetcher=fetcher)
    api.register_device(UID)
    api._device_tokens[UID] = ("STALE", None)
    monkeypatch.setattr(api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}")

    # ONE driver call, as async_start does. Recovery must be automatic.
    await api._connect_transport(api._device_list[0], "local")
    assert not api._transport_connected(_local_key(api))

    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    assert api._device_tokens[UID][0] == "FRESH"

    await api.close()
    await local.stop()


async def test_initial_resolver_miss_is_retried(monkeypatch):
    """An mDNS miss at startup keeps retrying so a late-arriving device
    still gets its local socket."""
    monkeypatch.setattr(transport_mod, "_LOCAL_BACKOFF_SCHEDULE", (0, 0.05, 0.05, 0.05))
    local = _FakeNanit()
    await local.start()

    api = make_transport()
    api.register_device(UID)
    api._device_tokens[UID] = ("dev-tok", None)
    results = ["miss", "hit"]

    async def resolver(_uid):
        return None if results.pop(0) == "miss" else "127.0.0.1"

    api.set_local_host_resolver(resolver)
    monkeypatch.setattr(
        transport_mod,
        "SOUND_LIGHT_LOCAL_WS_PORT",
        local.port,
    )
    # Downgrade wss to ws for the fake (the resolver path builds wss URLs).
    real_connect = transport_mod.websockets.connect

    def plain_ws_connect(url, **kw):
        return real_connect(
            url.replace("wss://", "ws://"), **{k: v for k, v in kw.items() if k != "ssl"}
        )

    monkeypatch.setattr(transport_mod.websockets, "connect", plain_ws_connect)

    await api._connect_transport(api._device_list[0], "local")
    assert not api._transport_connected(_local_key(api))

    await _wait_until(lambda: api._transport_connected(_local_key(api)))

    await api.close()
    await local.stop()
