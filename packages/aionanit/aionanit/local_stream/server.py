"""Local stream server: RTMP(S) ingest from cameras, HTTP-FLV to consumers.

The ingest listens on the LAN for the camera's push. The FLV endpoint binds
to loopback only, on an ephemeral port, and is what Home Assistant's stream
worker and go2rtc read from.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hmac
import ipaddress
import itertools
import logging
import secrets
import ssl
import time

from aiohttp import web

from .channel import StreamChannel
from .flv import FILE_HEADER, TAG_SCRIPT, Tag
from .rtmp import RtmpError, RtmpSession

_LOGGER = logging.getLogger(__name__)

PRE_PUBLISH_TIMEOUT = 15.0
IDLE_TIMEOUT = 30.0
TLS_HANDSHAKE_TIMEOUT = 10.0
# Connections that have not published yet (TLS handshakes included). When
# full, the oldest is evicted, so idle sockets can't lock the camera out.
MAX_PENDING_CONNECTIONS = 8
MAX_PENDING_PER_PEER = 2
WRITE_TIMEOUT = 10.0
MAX_TRACKED_PEERS = 256
HEADER_WAIT = 10.0
CLOSE_TIMEOUT = 2.0
APP_NAME = "nanit"

_ids = itertools.count(1)


class _RtmpConnection(asyncio.Protocol):
    """Asyncio glue around one ``RtmpSession``."""

    def __init__(self, server: LocalStreamServer, peer: str | None) -> None:
        self._server = server
        self._peer = peer
        self._id = next(_ids)
        self.accepted_at = time.monotonic()
        self.peer = peer
        self._failed = False
        self._transport: asyncio.BaseTransport | None = None
        # The accepted socket, so a connection evicted mid-TLS can be closed.
        self.raw: asyncio.BaseTransport | None = None
        self._channel: StreamChannel | None = None
        self._session = RtmpSession(self._authorize, self._published, self._tag)
        self._deadline: asyncio.TimerHandle | None = None
        self._last_data = time.monotonic()
        # start_tls can deliver the first bytes before connection_made runs.
        self._unsent = bytearray()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport
        if self._failed:
            self.abort()
            return
        if self._session.publishing:
            self._server._connections.add(self)
        else:
            self._arm(PRE_PUBLISH_TIMEOUT)
        if self._unsent and isinstance(transport, asyncio.WriteTransport):
            transport.write(bytes(self._unsent))
            self._unsent.clear()
        if self._session.closed:
            self.close()

    def connection_lost(self, exc: Exception | None) -> None:
        self._server._pending.discard(self)
        self._server._connections.discard(self)
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None
        if self._channel is not None:
            self._channel.publish_ended(self._id)

    def data_received(self, data: bytes) -> None:
        self._last_data = time.monotonic()
        try:
            out = self._session.feed(data)
        except RtmpError as err:
            _LOGGER.debug("Closing RTMP connection from %s: %s", self._peer, err)
            self._server.record_failure(self._peer)
            self._failed = True
            self.abort()
            return
        if out:
            if isinstance(self._transport, asyncio.WriteTransport):
                self._transport.write(out)
            else:
                self._unsent += out
        if self._session.closed and self._transport is not None:
            self.close()

    def _authorize(self, stream_name: str) -> object | None:
        if self._session.app.split("/", 1)[0] != APP_NAME:
            return None
        channel = self._server._channel_for_key(stream_name)
        if channel is None or not channel.can_accept_publisher():
            return None
        return channel

    def _published(self, token: object) -> None:
        assert isinstance(token, StreamChannel)
        self._channel = token
        self._server._pending.discard(self)
        self._server._connections.add(self)
        token.publish_started(self._id, self._peer, self.abort)
        self._arm(IDLE_TIMEOUT)
        _LOGGER.debug("Camera %s is publishing from %s", token.camera_uid, self._peer)

    def _tag(self, tag: Tag) -> None:
        if self._channel is not None:
            self._channel.tag_received(self._id, tag)

    def _arm(self, delay: float) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
        loop = asyncio.get_running_loop()
        self._deadline = loop.call_later(delay, self._check_deadline, delay)

    def _check_deadline(self, delay: float) -> None:
        self._deadline = None
        if not self._session.publishing:
            _LOGGER.debug("RTMP connection from %s never published", self._peer)
            self._server.record_failure(self._peer)
            self.abort()
            return
        idle = time.monotonic() - self._last_data
        if idle >= delay:
            _LOGGER.debug("RTMP publisher %s went idle", self._peer)
            self.abort()
            return
        loop = asyncio.get_running_loop()
        self._deadline = loop.call_later(delay - idle, self._check_deadline, delay)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def abort(self) -> None:
        self._failed = True
        transport = self._transport or self.raw
        if isinstance(transport, asyncio.WriteTransport):
            transport.abort()
        elif transport is not None:
            transport.close()


class _Acceptor(asyncio.Protocol):
    """Counts every TCP connection, then hands it to TLS and RTMP."""

    def __init__(self, server: LocalStreamServer) -> None:
        self._server = server

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        server = self._server
        server.connection_attempts += 1
        server.last_connection_at = time.monotonic()
        peername = transport.get_extra_info("peername")
        peer = str(peername[0]) if peername else None
        server._count(server.attempts_by_peer, peer)
        # Evict rather than refuse: the newest connection is the likeliest
        # to be the camera retrying, the oldest to be a stale or idle one.
        same_peer = [c for c in server._pending if c.peer == peer]
        if len(same_peer) >= MAX_PENDING_PER_PEER:
            server._evict(min(same_peer, key=lambda c: c.accepted_at))
        if len(server._pending) >= MAX_PENDING_CONNECTIONS:
            server._evict(min(server._pending, key=lambda c: c.accepted_at))
        conn = _RtmpConnection(server, peer)
        conn.raw = transport
        # Counted from accept, so TLS handshakes in flight are capped too.
        server._pending.add(conn)
        if server._ssl_context is None:
            transport.set_protocol(conn)
            conn.connection_made(transport)
            return
        assert isinstance(transport, asyncio.Transport)
        transport.pause_reading()
        task = asyncio.get_running_loop().create_task(server._start_tls(transport, conn, peer))
        server._tasks.add(task)
        task.add_done_callback(server._tasks.discard)


class LocalStreamServer:
    """Receive camera pushes over RTMP(S) and serve them as HTTP-FLV."""

    def __init__(
        self,
        *,
        port: int,
        host: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        port_attempts: int = 1,
    ) -> None:
        """Initialize the server.

        ``host=None`` binds every interface (dual stack). With
        ``port_attempts`` > 1 the next ports are tried when ``port`` is taken.
        """
        self._host = host
        self._requested_port = port
        self._ssl_context = ssl_context
        self._port_attempts = max(1, port_attempts)
        self._channels: dict[str, StreamChannel] = {}
        self._by_key: dict[str, StreamChannel] = {}
        self._pending: set[_RtmpConnection] = set()
        self._connections: set[_RtmpConnection] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._rtmp_server: asyncio.Server | None = None
        self._runner: web.AppRunner | None = None
        self._flv_secret = secrets.token_urlsafe(24)
        self.rtmp_port = 0
        self.http_port = 0
        self.connection_attempts = 0
        self.protocol_failures = 0
        self.last_connection_at: float | None = None
        # Per peer address, so a caller can tell its camera's connections
        # from a scanner's or another camera's. Bounded.
        self.attempts_by_peer: dict[str, int] = {}
        self.failures_by_peer: dict[str, int] = {}

    @property
    def tls(self) -> bool:
        """Return True if the ingest speaks RTMPS."""
        return self._ssl_context is not None

    async def async_start(self) -> None:
        """Bind the ingest and the loopback FLV endpoint."""
        loop = asyncio.get_running_loop()
        last_error: OSError | None = None
        for port in range(self._requested_port, self._requested_port + self._port_attempts):
            try:
                self._rtmp_server = await loop.create_server(
                    lambda: _Acceptor(self), host=self._host, port=port, reuse_address=True
                )
            except OSError as err:
                last_error = err
                if err.errno != errno.EADDRINUSE:
                    break
                continue
            self.rtmp_port = int(self._rtmp_server.sockets[0].getsockname()[1])
            break
        if self._rtmp_server is None:
            assert last_error is not None
            raise last_error

        app = web.Application()
        # No HEAD: it would start the camera without anyone watching.
        app.router.add_get("/nanit/{name}", self._handle_flv, allow_head=False)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
        except BaseException:
            # Don't leave the ingest bound, or the next try moves ports.
            await self.async_stop()
            raise
        self.http_port = int(self._runner.addresses[0][1])
        _LOGGER.debug(
            "Local stream server on port %d (%s), FLV on 127.0.0.1:%d",
            self.rtmp_port,
            "rtmps" if self.tls else "rtmp",
            self.http_port,
        )

    async def async_stop(self) -> None:
        """Close everything, without waiting on the cameras' pushes."""
        if self._rtmp_server is not None:
            self._rtmp_server.close()
        for task in list(self._tasks):
            task.cancel()
        for channel in list(self._channels.values()):
            channel.close()
        for conn in list(self._pending | self._connections):
            conn.abort()
        if self._runner is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self._runner.cleanup()
            self._runner = None
        if self._rtmp_server is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self._rtmp_server.wait_closed()
            self._rtmp_server = None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def register(self, camera_uid: str, stream_key: str) -> StreamChannel:
        """Create (or replace) the channel for a camera."""
        old = self._channels.pop(camera_uid, None)
        if old is not None:
            self._by_key.pop(old.stream_key, None)
            old.close()
        channel = StreamChannel(camera_uid, stream_key)
        self._channels[camera_uid] = channel
        self._by_key[stream_key] = channel
        return channel

    def unregister(self, camera_uid: str) -> None:
        """Drop a camera's channel."""
        channel = self._channels.pop(camera_uid, None)
        if channel is not None:
            self._by_key.pop(channel.stream_key, None)
            channel.close()

    def channel(self, camera_uid: str) -> StreamChannel | None:
        """Return a camera's channel."""
        return self._channels.get(camera_uid)

    def _channel_for_key(self, stream_key: str) -> StreamChannel | None:
        for key, channel in self._by_key.items():
            if hmac.compare_digest(key.encode(), stream_key.encode()):
                return channel
        return None

    def publish_url(self, advertised_host: str, camera_uid: str) -> str:
        """Return the URL to send the camera for its push."""
        channel = self._channels[camera_uid]
        host = advertised_host
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
        scheme = "rtmps" if self.tls else "rtmp"
        return f"{scheme}://{host}:{self.rtmp_port}/{APP_NAME}/{channel.stream_key}"

    def flv_url(self, camera_uid: str) -> str:
        """Return the loopback HTTP-FLV URL for a camera."""
        return f"http://127.0.0.1:{self.http_port}/nanit/{camera_uid}.flv?k={self._flv_secret}"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _count(self, table: dict[str, int], peer: str | None) -> None:
        if peer is None:
            return
        if peer not in table and len(table) >= MAX_TRACKED_PEERS:
            table.pop(next(iter(table)))
        table[peer] = table.get(peer, 0) + 1

    def record_failure(self, peer: str | None) -> None:
        """Count a connection that failed TLS or RTMP, or never published."""
        self.protocol_failures += 1
        self._count(self.failures_by_peer, peer)

    def _evict(self, conn: _RtmpConnection) -> None:
        _LOGGER.debug("Evicting pending RTMP connection from %s", conn.peer)
        self._pending.discard(conn)
        conn.abort()

    async def _start_tls(
        self, transport: asyncio.Transport, conn: _RtmpConnection, peer: str | None
    ) -> None:
        assert self._ssl_context is not None
        loop = asyncio.get_running_loop()
        try:
            tls_transport = await loop.start_tls(
                transport,
                conn,
                self._ssl_context,
                server_side=True,
                ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT,
            )
        except (OSError, ssl.SSLError, TimeoutError, ConnectionError) as err:
            _LOGGER.debug("TLS handshake with %s failed: %s", peer, err)
            if conn in self._pending:
                # An eviction aborts the handshake too, and that is not the
                # peer failing the protocol.
                self.record_failure(peer)
            self._pending.discard(conn)
            transport.abort()
            return
        if tls_transport is None or conn not in self._pending:
            # start_tls returns None rather than raising when the socket goes
            # away mid-handshake. That is a failure only if the peer did it,
            # not if we evicted it.
            if conn in self._pending:
                self.record_failure(peer)
            self._pending.discard(conn)
            (tls_transport or transport).abort()
            return
        conn.connection_made(tls_transport)

    @staticmethod
    async def _write(response: web.StreamResponse, data: bytes) -> None:
        """Write to a reader, giving up on one that stopped reading."""
        async with asyncio.timeout(WRITE_TIMEOUT):
            await response.write(data)

    async def _handle_flv(self, request: web.Request) -> web.StreamResponse:
        peer = request.remote
        try:
            loopback = peer is not None and ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise web.HTTPForbidden
        if not hmac.compare_digest(request.query.get("k", "").encode(), self._flv_secret.encode()):
            raise web.HTTPNotFound
        name = request.match_info["name"]
        channel = self._channels.get(name.removesuffix(".flv")) if name.endswith(".flv") else None
        if channel is None:
            raise web.HTTPNotFound
        if not channel.accepting and not channel.publishing:
            raise web.HTTPServiceUnavailable

        response = web.StreamResponse(
            headers={"Content-Type": "video/x-flv", "Cache-Control": "no-store"}
        )
        response.enable_chunked_encoding()
        await response.prepare(request)

        if not await channel.ensure_publishing():
            return response
        sub = channel.subscribe()
        try:
            if not await channel.wait_for_headers(HEADER_WAIT):
                return response
            # go2rtc's FLV probe times out 5 s after the file header, so the
            # header goes out together with the codec configuration.
            head = FILE_HEADER
            if channel.metadata is not None:
                head += Tag(TAG_SCRIPT, 0, channel.metadata.body).encode()
            for header in (channel.avc_header, channel.aac_header):
                if header is not None:
                    head += Tag(header.kind, 0, header.body).encode()
            await self._write(response, head)
            base: int | None = None
            while (tag := await sub.get()) is not None:
                if tag.is_avc_header or tag.is_aac_header:
                    # A configuration change. FFmpeg's FLV reader takes a
                    # sequence header as new extradata whatever its timestamp.
                    await self._write(response, Tag(tag.kind, 0, tag.body).encode())
                    continue
                if base is None:
                    base = tag.timestamp
                await self._write(response, tag.encode(base))
        except (ConnectionResetError, TimeoutError):
            pass
        finally:
            sub.close()
        return response
