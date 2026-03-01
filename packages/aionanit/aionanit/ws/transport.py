"""WebSocket transport with reconnect and keepalive for Nanit cameras."""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
from collections.abc import Callable

import aiohttp

from aionanit.exceptions import NanitConnectionError, NanitTransportError
from aionanit.models import ConnectionState, TransportKind

from .protocol import build_keepalive

_LOGGER = logging.getLogger(__name__)

# Connection parameters — aligned with the Go reference implementation.
_KEEPALIVE_INTERVAL: float = 25.0  # seconds between protobuf keepalive msgs
_HEARTBEAT_INTERVAL: float = 60.0  # aiohttp TCP-level heartbeat
_HANDSHAKE_TIMEOUT: float = 15.0  # ws_connect timeout
_INITIAL_BACKOFF: float = 1.85
_BACKOFF_FACTOR: float = 1.618  # golden ratio
_MAX_BACKOFF: float = 60.0
_JITTER_MAX: float = 1.0


class WsTransport:
    """Manages a single WebSocket connection with reconnect and keepalive.

    Handles:
    - Connection (cloud or local URL)
    - Binary receive loop dispatching to a callback
    - Keepalive pings (protobuf KEEPALIVE message every 25 s)
    - Reconnect with exponential backoff + jitter
    - Graceful close with task cancellation
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        on_message: Callable[[bytes], None],
        on_connection_change: Callable[
            [ConnectionState, TransportKind, str | None], None
        ],
    ) -> None:
        self._session = session
        self._on_message = on_message
        self._on_connection_change = on_connection_change

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._transport_kind: TransportKind = TransportKind.NONE
        self._url: str | None = None
        self._headers: dict[str, str] = {}
        self._ssl_context: ssl.SSLContext | None = None
        self._closed: bool = False
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True when the WebSocket is open."""
        return self._ws is not None and not self._ws.closed

    @property
    def transport_kind(self) -> TransportKind:
        """Current transport type (LOCAL, CLOUD, or NONE)."""
        return self._transport_kind

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_connect_cloud(
        self, camera_uid: str, access_token: str
    ) -> None:
        """Connect to the Nanit cloud relay.

        URL:  wss://api.nanit.com/focus/cameras/{camera_uid}/user_connect
        Auth: Authorization: Bearer {access_token}
        """
        url = (
            f"wss://api.nanit.com/focus/cameras/{camera_uid}/user_connect"
        )
        headers = {"Authorization": f"Bearer {access_token}"}
        await self._async_connect(url, headers, TransportKind.CLOUD, ssl_context=None)

    async def async_connect_local(
        self,
        camera_ip: str,
        uc_token: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Connect directly to the camera on the LAN.

        URL:  wss://{camera_ip}:442
        Auth: Authorization: token {uc_token}

        If *ssl_context* is ``None`` a permissive context is created
        (self-signed cert on the camera).
        """
        url = f"wss://{camera_ip}:442"
        headers = {"Authorization": f"token {uc_token}"}
        if ssl_context is None:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        await self._async_connect(url, headers, TransportKind.LOCAL, ssl_context=ssl_context)

    async def async_send(self, data: bytes) -> None:
        """Send binary data over the WebSocket.

        Raises NanitTransportError if not connected or on send failure.
        """
        if not self.connected:
            raise NanitTransportError("Not connected")
        assert self._ws is not None  # for type-checker; guarded above
        try:
            await self._ws.send_bytes(data)
        except Exception as err:
            raise NanitTransportError(f"Send failed: {err}") from err

    async def async_close(self) -> None:
        """Close connection and cancel background tasks. Idempotent."""
        self._closed = True
        await self._async_close_ws()
        self._transport_kind = TransportKind.NONE
        self._on_connection_change(
            ConnectionState.DISCONNECTED, TransportKind.NONE, None
        )

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    async def _async_connect(
        self,
        url: str,
        headers: dict[str, str],
        kind: TransportKind,
        ssl_context: ssl.SSLContext | None,
    ) -> None:
        async with self._connect_lock:
            await self._async_close_ws()
            self._url = url
            self._headers = headers
            self._ssl_context = ssl_context
            self._transport_kind = kind
            self._closed = False

            self._on_connection_change(ConnectionState.CONNECTING, kind, None)
            try:
                self._ws = await self._session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=_HEARTBEAT_INTERVAL,
                    timeout=_HANDSHAKE_TIMEOUT,
                    ssl=ssl_context,
                )
            except Exception as err:
                self._on_connection_change(
                    ConnectionState.DISCONNECTED, kind, str(err)
                )
                raise NanitConnectionError(str(err)) from err

            loop = asyncio.get_running_loop()
            self._recv_task = loop.create_task(self._recv_loop())
            self._keepalive_task = loop.create_task(self._keepalive_loop())
            self._on_connection_change(ConnectionState.CONNECTED, kind, None)

    async def _async_close_ws(self) -> None:
        """Close WebSocket and cancel background tasks."""
        for task in (self._recv_task, self._keepalive_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._recv_task = None
        self._keepalive_task = None

        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    # ------------------------------------------------------------------
    # Internal — background loops
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Read binary frames from the WebSocket and dispatch them."""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._on_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    _LOGGER.debug("WebSocket closed by server")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error(
                        "WebSocket error: %s", self._ws.exception()
                    )
                    break
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Recv loop error: %s", err)

        # If we weren't explicitly closed, attempt to reconnect.
        if not self._closed:
            asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _keepalive_loop(self) -> None:
        """Send protobuf KEEPALIVE message every ``_KEEPALIVE_INTERVAL`` seconds."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                if not self.connected:
                    break
                try:
                    await self.async_send(build_keepalive())
                except NanitTransportError:
                    _LOGGER.warning("Keepalive send failed, triggering reconnect")
                    break
        except asyncio.CancelledError:
            return

    async def _reconnect_loop(self) -> None:
        """Exponential-backoff reconnect loop."""
        if self._closed:
            return

        backoff = _INITIAL_BACKOFF
        jitter = random.random() * _JITTER_MAX  # jitter on first retry only

        while not self._closed:
            await self._async_close_ws()
            self._on_connection_change(
                ConnectionState.RECONNECTING, self._transport_kind, None
            )

            wait_time = backoff + jitter
            jitter = 0.0  # only first retry gets jitter
            _LOGGER.info("Reconnecting in %.1fs", wait_time)
            await asyncio.sleep(wait_time)

            if self._closed:
                return

            try:
                self._ws = await self._session.ws_connect(
                    self._url,
                    headers=self._headers,
                    heartbeat=_HEARTBEAT_INTERVAL,
                    timeout=_HANDSHAKE_TIMEOUT,
                    ssl=self._ssl_context,
                )
                loop = asyncio.get_running_loop()
                self._recv_task = loop.create_task(self._recv_loop())
                self._keepalive_task = loop.create_task(self._keepalive_loop())
                self._on_connection_change(
                    ConnectionState.CONNECTED, self._transport_kind, None
                )
                _LOGGER.info("Reconnected successfully")
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Reconnect failed: %s", err)
                backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)
