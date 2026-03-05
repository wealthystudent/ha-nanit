"""Tests for aionanit.ws.transport — WsTransport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from aionanit.exceptions import NanitConnectionError, NanitTransportError
from aionanit.models import ConnectionState, TransportKind
from aionanit.ws.transport import WsTransport


def _make_transport(
    on_message: MagicMock | None = None,
    on_connection_change: MagicMock | None = None,
    get_headers: AsyncMock | None = None,
    ) -> tuple[WsTransport, MagicMock, MagicMock, MagicMock]:
    """Create a WsTransport with mocked session and callbacks."""
    session = MagicMock(spec=aiohttp.ClientSession)
    msg_cb = on_message or MagicMock()
    conn_cb = on_connection_change or MagicMock()
    transport = WsTransport(session, msg_cb, conn_cb, get_headers=get_headers)
    return transport, session, msg_cb, conn_cb

class TestInitialState:
    def test_connected_is_false(self) -> None:
        t, *_ = _make_transport()
        assert t.connected is False

    def test_transport_kind_is_none(self) -> None:
        t, *_ = _make_transport()
        assert t.transport_kind == TransportKind.NONE

    def test_idle_seconds_is_zero_initially(self) -> None:
        t, *_ = _make_transport()
        assert t.idle_seconds == 0.0


class TestAsyncSend:
    async def test_raises_when_not_connected(self) -> None:
        t, *_ = _make_transport()
        with pytest.raises(NanitTransportError, match="Not connected"):
            await t.async_send(b"\x00")


class TestAsyncClose:
    async def test_idempotent(self) -> None:
        t, _, _, conn_cb = _make_transport()
        await t.async_close()
        await t.async_close()
        # Should not raise; conn_cb called each time
        assert conn_cb.call_count == 2

    async def test_fires_disconnected_callback(self) -> None:
        t, _, _, conn_cb = _make_transport()
        await t.async_close()
        conn_cb.assert_called_with(
            ConnectionState.DISCONNECTED, TransportKind.NONE, None
        )


class TestAsyncConnectCloud:
    async def test_correct_url_and_headers(self) -> None:
        t, session, _, conn_cb = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        # Make the recv loop exit immediately
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_cloud("cam123", "tok456")

        session.ws_connect.assert_called_once()
        call_kwargs = session.ws_connect.call_args
        assert call_kwargs[0][0] == "wss://api.nanit.com/focus/cameras/cam123/user_connect"
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer tok456"
        assert call_kwargs[1]["ssl"] is None
        assert t.connected is True
        assert t.transport_kind == TransportKind.CLOUD

        # Clean up tasks
        await t.async_close()

    async def test_connection_callbacks(self) -> None:
        t, session, _, conn_cb = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_cloud("cam1", "tok1")

        # Should have called CONNECTING then CONNECTED
        calls = conn_cb.call_args_list
        assert len(calls) >= 2
        assert calls[-2][0] == (ConnectionState.CONNECTING, TransportKind.CLOUD, None)
        assert calls[-1][0] == (ConnectionState.CONNECTED, TransportKind.CLOUD, None)

        await t.async_close()

    async def test_connection_failure_fires_disconnected(self) -> None:
        t, session, _, conn_cb = _make_transport()
        session.ws_connect = AsyncMock(side_effect=aiohttp.ClientError("fail"))

        with pytest.raises(NanitConnectionError):
            await t.async_connect_cloud("cam1", "tok1")

        calls = conn_cb.call_args_list
        assert calls[-1][0][0] == ConnectionState.DISCONNECTED


class TestAsyncConnectLocal:
    async def test_correct_url_and_headers(self) -> None:
        t, session, _, conn_cb = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_local("192.168.1.50", "uctoken123")

        call_kwargs = session.ws_connect.call_args
        assert call_kwargs[0][0] == "wss://192.168.1.50:442"
        assert call_kwargs[1]["headers"]["Authorization"] == "token uctoken123"
        assert call_kwargs[1]["ssl"] is not None  # self-signed SSL context
        assert t.transport_kind == TransportKind.LOCAL

        await t.async_close()


class TestIdleSeconds:
    async def test_idle_seconds_resets_on_connect(self) -> None:
        """idle_seconds starts near zero after a successful connection."""
        t, session, _, _ = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_cloud("cam1", "tok1")
        # Just connected — idle should be very small.
        assert t.idle_seconds < 1.0

        await t.async_close()

    async def test_idle_seconds_updates_on_binary_message(self) -> None:
        """idle_seconds resets when a binary message is received."""
        t, session, msg_cb, _ = _make_transport()

        binary_msg = MagicMock()
        binary_msg.type = aiohttp.WSMsgType.BINARY
        binary_msg.data = b"\x08\x00"

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False

        async def _aiter(self_=None):
            yield binary_msg

        mock_ws.__aiter__ = _aiter
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_cloud("cam1", "tok1")
        await asyncio.sleep(0.05)

        # A binary message was received — idle should be small.
        assert t.idle_seconds < 1.0

        await t.async_close()


class TestRecvLoop:
    async def test_dispatches_binary_messages(self) -> None:
        t, session, msg_cb, _ = _make_transport()

        binary_msg = MagicMock()
        binary_msg.type = aiohttp.WSMsgType.BINARY
        binary_msg.data = b"\x08\x00"

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False

        # Make ws iterable returning one message then stopping
        async def _aiter(self_=None):
            yield binary_msg

        mock_ws.__aiter__ = _aiter
        session.ws_connect = AsyncMock(return_value=mock_ws)

        await t.async_connect_cloud("cam1", "tok1")
        # Give recv loop a moment to process
        await asyncio.sleep(0.05)

        msg_cb.assert_called_once_with(b"\x08\x00")
        await t.async_close()


class TestGetHeadersCallback:
    async def test_transport_stores_get_headers(self) -> None:
        """get_headers callback is stored on the transport."""
        headers_cb = AsyncMock(return_value={"Authorization": "Bearer fresh"})
        t, *_ = _make_transport(get_headers=headers_cb)
        assert t._get_headers is headers_cb

    async def test_transport_without_get_headers(self) -> None:
        """get_headers defaults to None."""
        t, *_ = _make_transport()
        assert t._get_headers is None

    async def test_reconnect_calls_get_headers(self) -> None:
        """_reconnect_loop calls get_headers to refresh auth before reconnect."""
        fresh_headers = {"Authorization": "Bearer refreshed_token"}
        headers_cb = AsyncMock(return_value=fresh_headers)
        t, session, _, conn_cb = _make_transport(get_headers=headers_cb)

        # Set up state as if we were previously connected and recv loop ended
        t._url = "wss://api.nanit.com/focus/cameras/cam1/user_connect"
        t._headers = {"Authorization": "Bearer stale_token"}
        t._transport_kind = TransportKind.CLOUD
        t._closed = False

        # Mock ws_connect to succeed on reconnect
        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        # Run reconnect with patched sleep to avoid waiting
        with patch("aionanit.ws.transport.asyncio.sleep", new_callable=AsyncMock):
            await t._reconnect_loop()

        # get_headers should have been called
        headers_cb.assert_awaited_once()
        # The headers passed to ws_connect should be the fresh ones
        call_kwargs = session.ws_connect.call_args
        assert call_kwargs[1]["headers"] == fresh_headers

        await t.async_close()

    async def test_reconnect_without_get_headers_keeps_original(self) -> None:
        """Without get_headers callback, reconnect reuses existing headers."""
        t, session, _, conn_cb = _make_transport()  # no get_headers

        original_headers = {"Authorization": "Bearer original"}
        t._url = "wss://api.nanit.com/focus/cameras/cam1/user_connect"
        t._headers = original_headers.copy()
        t._transport_kind = TransportKind.CLOUD
        t._closed = False

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch("aionanit.ws.transport.asyncio.sleep", new_callable=AsyncMock):
            await t._reconnect_loop()

        call_kwargs = session.ws_connect.call_args
        assert call_kwargs[1]["headers"] == original_headers

        await t.async_close()

    async def test_reconnect_header_refresh_failure_uses_existing(self) -> None:
        """If get_headers raises, reconnect proceeds with existing headers."""
        headers_cb = AsyncMock(side_effect=Exception("token refresh failed"))
        t, session, _, conn_cb = _make_transport(get_headers=headers_cb)

        stale_headers = {"Authorization": "Bearer stale"}
        t._url = "wss://api.nanit.com/focus/cameras/cam1/user_connect"
        t._headers = stale_headers.copy()
        t._transport_kind = TransportKind.CLOUD
        t._closed = False

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        session.ws_connect = AsyncMock(return_value=mock_ws)

        with patch("aionanit.ws.transport.asyncio.sleep", new_callable=AsyncMock):
            await t._reconnect_loop()

        # Should still have attempted connection with stale headers
        call_kwargs = session.ws_connect.call_args
        assert call_kwargs[1]["headers"] == stale_headers

        await t.async_close()


class TestAsyncForceReconnect:
    async def test_force_reconnect_closes_ws(self) -> None:
        """async_force_reconnect closes ws without setting _closed."""
        t, session, _, _ = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = False
        mock_ws.close = AsyncMock()
        t._ws = mock_ws

        await t.async_force_reconnect()

        mock_ws.close.assert_awaited_once()
        assert t._closed is False  # NOT set — reconnect loop should fire

    async def test_force_reconnect_noop_when_no_ws(self) -> None:
        """async_force_reconnect is a no-op when no ws connection."""
        t, *_ = _make_transport()
        assert t._ws is None
        await t.async_force_reconnect()  # Should not raise

    async def test_force_reconnect_noop_when_already_closed(self) -> None:
        """async_force_reconnect is a no-op when ws is already closed."""
        t, *_ = _make_transport()

        mock_ws = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
        mock_ws.closed = True
        mock_ws.close = AsyncMock()
        t._ws = mock_ws

        await t.async_force_reconnect()
        mock_ws.close.assert_not_awaited()
