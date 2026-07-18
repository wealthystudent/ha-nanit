"""Protobuf WebSocket transport for the Nanit Sound & Light Machine.

Ported wholesale from com6056/nanit-sound-light (`api.py` at its validated
HEAD), where every reliability behavior below was reverse-engineered from the
official app and validated against a real device. Auth is NOT handled here:
the caller injects an access-token provider (aionanit's TokenManager) and a
device-token fetcher (aionanit's REST client), so this module owns only the
sockets and the protocol.

The invariants this module encodes (do not undo without reading
docs/CONNECTION_RELIABILITY.md):

- One request in flight per device with await-ack by requestId, and NO
  re-send on a slow ack (re-sending wedges a busy device).
- A backend readiness gate: nothing is sent into the cloud relay until it
  reports the physical device attached, and attachment is sticky.
- Dual transports: a direct-LAN socket (preferred for sends) and the cloud
  relay can be open at once, with app-matching reconnect backoff schedules.
- WebSocket protocol ping (~20s) is the only keepalive; the device has no
  app-level keepalive frame.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

_LOGGER = logging.getLogger(__name__)

# Remote cloud relay: wss://remote.nanit.com/speakers/<uid>/user_connect/
SOUND_LIGHT_WS_BASE_URL = "wss://remote.nanit.com/speakers"
# Direct-LAN socket: wss://Nanit-<uid>.local:442 (deterministic mDNS name,
# with no path, unlike the relay's /user_connect/ path which is remote-only).
SOUND_LIGHT_LOCAL_MDNS_PREFIX = "Nanit-"
SOUND_LIGHT_LOCAL_WS_PORT = 442


def _clean_device_string(value: object, max_len: int = 64) -> str | None:
    """Clamp and filter an untrusted device-provided string for safe display.

    Device and cloud strings (SSID, BSSID, firmware version) are surfaced as
    entity state or attributes. Clamp the length and require printable,
    non-blank content, mirroring how the sound-track list is sanitized.
    """
    if not isinstance(value, str):
        return None
    value = value[:max_len]
    return value if value.isprintable() and value.strip() else None


def _unit_float(value: float) -> float | None:
    """Clamp an untrusted wire float to 0.0-1.0, rejecting non-finite values.

    The device protocol carries brightness/volume/hue/saturation as floats;
    a malformed or hostile frame must not push NaN/Inf/out-of-range values
    into Home Assistant state.
    """
    if not math.isfinite(value):
        return None
    return min(1.0, max(0.0, value))


def _finite_float(value: float) -> float | None:
    """Reject non-finite untrusted wire floats (temperature/humidity)."""
    return value if math.isfinite(value) else None


# WebSocket liveness. The device relies on WebSocket protocol-level ping/pong
# for keepalive. It sends no app-level keepalive frame (see CLAUDE.md). We set
# the ping interval explicitly rather than leaning on library defaults.
WS_PING_INTERVAL = 20  # seconds
WS_PING_TIMEOUT = 20  # seconds, drop a half-open socket instead of wedging
WS_CLOSE_TIMEOUT = 5  # seconds

# Command transaction model, mirroring the official app's SocketRequestManager:
# send ONE Request, then await the Response whose requestId matches (one in
# flight, drain each response). The app uses a 10s ack timeout. Fire-and-forget
# sends with undrained responses degrade the device's transaction state until it
# wedges (needs a power cycle). This is the fix for that.
COMMAND_ACK_TIMEOUT = 10  # seconds to await a matching Response

# We do NOT re-send a command on a slow/absent ack. A timed-out ack on a LIVE
# socket means the device is busy, not gone, and re-sending piles duplicate
# commands onto an already-overloaded device, which is exactly what makes it
# stop responding for ~30s and then flush the whole backlog at once. The official
# app never retries either (one in flight, await ack, done). So a slow ack is
# accepted optimistically (the pin holds the UI, the device pushes real state when
# it catches up, the 30s poll reconciles). Only an actual socket DROP fails the
# command (causing rollback). An explicit non-2xx rejection also fails it.

# device.status enum value from Backend.device (Disconnected=0, Connected=1).
# The app derives "remote route is live" solely from this and sends nothing
# until Connected. Sending into a still-Disconnected relay is what caused our
# command latency. We wait up to this long for the Connected frame before a
# command, then send best-effort (a missed/changed backend frame must not brick
# control, see send_control_command).
_BACKEND_STATUS_CONNECTED = 1
DEVICE_ATTACH_TIMEOUT = 10  # seconds to wait for backend Connected before a send

# Battery state-of-charge is a coarse 5-bucket enum (StateOfCharge). Map each to
# a representative percentage. SoCLow has no number in its name, so ~10% (low).
_SOC_TO_PERCENT = {0: 10, 1: 25, 2: 50, 3: 75, 4: 90}

# Reconnect backoff, mirroring the official app's
# RemoteControlSocketCandidate.getNextRetryTime: 0s, then 2s, 5s, capped at 7s.
# Replaces the old "reconnect lazily on the next 30s poll" behaviour.


def _reconnect_backoff(retries: int) -> int:
    """Seconds before REMOTE reconnect attempt number `retries` (0-indexed)."""
    if retries < 1:
        return 0
    if retries < 4:
        return 2
    if retries < 11:
        return 5
    return 7


# The LOCAL socket backs off more slowly than remote, mirroring the app's
# LocalControlSocketCandidate (0, 3, 10, 60, then 90s cap). Local failures are
# non-fatal (remote covers control meanwhile), so a slack schedule avoids
# hammering a `.local` name that may not resolve on this host at all.
_LOCAL_BACKOFF_SCHEDULE = (0, 3, 10, 60, 90)


def _local_reconnect_backoff(retries: int) -> int:
    """Seconds before LOCAL reconnect attempt number `retries` (0-indexed)."""
    idx = min(max(retries, 0), len(_LOCAL_BACKOFF_SCHEDULE) - 1)
    return _LOCAL_BACKOFF_SCHEDULE[idx]


# Persistent auth-rejection backoff. A handshake rejected with 401/403 (local)
# or 401/403/404 (the relay's 404 on user_connect means it holds no session for
# the device) is NOT a transient drop: retrying fast cannot fix a token the
# device has stopped accepting or a relay session that no longer exists. After
# this many consecutive auth rejections on one transport, switch it to a long,
# quiet retry interval so a wedged device (reachable but refusing auth, which
# needs a power cycle) cannot flood the log with thousands of ERROR lines or
# hammer the cloud `/udtokens` endpoint overnight. A successful connect resets
# the count, so a normal transient drop keeps the fast app-matching backoff.
AUTH_REJECT_BACKOFF_THRESHOLD = 4
AUTH_REJECT_RETRY_INTERVAL = 120  # seconds (2 min) once the threshold is crossed
_AUTH_REJECT_STATUSES_LOCAL = frozenset({401, 403})
_AUTH_REJECT_STATUSES_REMOTE = frozenset({401, 403, 404})

# Transient (non-auth) connect failures (cloud outage, unplugged device, DNS)
# get the same LOG quieting as auth rejects: ERROR for the first few, one
# WARNING at the threshold, then debug. Only the log level is throttled. The
# retry cadence (the fast app-matching backoff) is deliberately untouched, so
# recovery latency is unchanged. Without this an extended outage produced one
# ERROR per ~7s retry, thousands of lines overnight.
TRANSIENT_FAIL_LOG_THRESHOLD = 4


# Transports per device, in send-preference order: try LOCAL first (fast, direct
# LAN), fall back to the REMOTE cloud relay. The app keeps both open on the same
# network and prefers local for sends (ControlSocketDecision / priority AP>LOCAL>
# REMOTE). We mirror local then remote.
TRANSPORT_LOCAL = "local"
TRANSPORT_REMOTE = "remote"
_TRANSPORTS = (TRANSPORT_LOCAL, TRANSPORT_REMOTE)
# Separator for the per-(device, transport) websocket key. "::" can't appear in a
# Nanit uid, so split is unambiguous.
_KEY_SEP = "::"


# Import protobuf classes at module level to avoid blocking async operations
try:
    from .sound_light_pb2 import (
        Color,
        GetSettings,
        Message,
        Request,
        Settings,
        Sound,
    )

    PROTOBUF_AVAILABLE = True
except ImportError as e:
    _LOGGER.error("Failed to import protobuf classes: %s", e)

    # Create dummy classes to prevent import errors
    class Color:  # type: ignore[no-redef]
        pass

    class GetSettings:  # type: ignore[no-redef]
        pass

    class Message:  # type: ignore[no-redef]
        pass

    class Request:  # type: ignore[no-redef]
        pass

    class Settings:  # type: ignore[no-redef]
        pass

    class Sound:  # type: ignore[no-redef]
        pass

    PROTOBUF_AVAILABLE = False


class CommandTimeoutError(ConnectionError):
    """A control command was sent but not acked within COMMAND_ACK_TIMEOUT.

    A ConnectionError subclass (so existing handlers still catch it), but
    distinct so the sender can tell a slow/absent ack apart from a socket drop
    or an explicit device rejection. The distinction matters because the
    responses are OPPOSITE: a timeout on a live socket is accepted
    optimistically and must NOT re-send (duplicates wedge a busy device, and
    the retry that once lived here was removed for exactly that, see
    CLAUDE.md), while a drop or rejection propagates so the coordinator rolls
    back.
    """


class SoundLightTransport:
    """Pure protobuf WebSocket client for Nanit Sound + Light devices.

    Auth is injected: `access_token_provider` returns a fresh user access
    token (backed by aionanit's TokenManager, which refreshes as needed) and
    `device_token_fetcher` returns the per-speaker local-socket token
    (backed by aionanit's REST client). The transport itself never talks to
    the Nanit REST API.
    """

    def __init__(
        self,
        *,
        access_token_provider: Callable[[], Awaitable[str]],
        device_token_fetcher: Callable[[str], Awaitable[str]],
        local_enabled: bool = True,
    ) -> None:
        """Initialize the transport.

        `local_enabled` turns on the direct-LAN path (preferred for sends,
        with the cloud relay as fallback). It is best-effort: if the device
        token can't be fetched or the `.local` name doesn't resolve, the
        client silently stays on the relay.
        """
        self._access_token_provider = access_token_provider
        self._device_token_fetcher = device_token_fetcher
        self._local_enabled = local_enabled
        # Keyed by f"{speaker_uid}{_KEY_SEP}{transport}". A device can have BOTH a
        # local and a remote socket open at once (the app does). Device-level
        # state (attachment, pending acks, send lock, sessionId) stays keyed by
        # speaker_uid and is shared across a device's transports.
        self._websockets: dict[str, ClientConnection] = {}
        # Per-speaker local device token: speaker_uid -> (token, expires_at|None).
        # Distinct from the user access token. Only the LOCAL socket uses it.
        self._device_tokens: dict[str, tuple[str, float | None]] = {}
        # Optional async resolver: speaker_uid -> LAN IPv4 (or None), typed
        # below at first assignment. Injected by
        # the coordinator (HA's zeroconf), because a HA install in a container
        # usually can't resolve `.local` via libc (no nss-mdns). It finds the
        # device's mDNS service by uid and returns its IP. When unset we fall back
        # to handing the deterministic `.local` name to the OS resolver (works on
        # HA OS / hosts with nss-mdns). Signature: async (speaker_uid) -> str|None.
        self._local_host_resolver: Callable[[str], Awaitable[str | None]] | None = None
        # Which transport a device's one in-flight command went out on, so a
        # redundant socket dropping doesn't fail a command acked on the other.
        self._inflight_conn_key: dict[str, str] = {}
        self._device_state: dict[str, dict[str, Any]] = {}
        # Mirrors the app's AtomicInteger(0): first _next_message_id() returns 1.
        self._message_id = 0
        # Callback for real-time updates (called with speaker_uid).
        self._state_change_callback: Callable[[str], Awaitable[None]] | None = None
        # Callback for connectivity changes (called with speaker_uid) whenever
        # a socket connects/drops or attachment latches. Sync and best-effort.
        self._connection_change_callback: Callable[[str], None] | None = None
        self._device_list: list[dict[str, Any]] = []  # Store device info for reconnection

        # Connection lifecycle. `_closing` stops the reconnect loop on shutdown.
        # `_connect_locks` serialises connects per (device, transport) so a
        # proactive reconnect, a lazy `ensure_websocket_connection`, and the 30s
        # poll can't open duplicate sockets. `_reconnect_tasks` tracks the running
        # backoff loop per (device, transport). All three dicts are keyed by
        # connection key (`speaker_uid{_KEY_SEP}transport`).
        self._closing = False
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        # Consecutive auth-rejection (401/403, or relay 404) count per connection
        # key. Drives the long, quiet retry interval for a transport whose
        # handshake keeps being refused (e.g. a wedged device). Reset on a
        # successful connect, so a normal transient drop keeps the fast backoff.
        self._auth_reject_counts: dict[str, int] = {}
        # Wall-clock time (per connection key) until which connects are skipped
        # after persistent auth rejection. Armed once the count crosses the
        # threshold. This time-gates the connect ATTEMPT itself, so the 30s
        # coordinator poll (which drives connect_device -> _connect_transport via
        # ensure_websocket_connection) can't keep refetching /udtokens on a wedged
        # device. The reconnect loop's long sleep and this gate use the same
        # interval. Cleared on a successful connect.
        self._auth_reject_until: dict[str, float] = {}
        # Consecutive transient (non-auth) connect-failure count per connection
        # key. Drives log quieting only, never the retry cadence. Reset on a
        # successful connect.
        self._transient_fail_counts: dict[str, int] = {}
        # Strong refs to the per-connection message-handler tasks. asyncio only
        # holds a weak reference to a bare create_task() result, so without this
        # the handler could be garbage-collected mid-run and silently stop
        # delivering device pushes.
        self._handler_tasks: dict[str, asyncio.Task[None]] = {}

        # Backend readiness gate. The device's first frame after a remote connect
        # is Message{backend} reporting whether the physical device is attached
        # behind the relay. We must not send until it's Connected (else commands
        # stall = latency). `_device_attached` is the latched bool, `_attached_events`
        # lets a pending send await the Connected transition. Attachment is
        # STICKY: set by a Connected backend frame or any real traffic, and
        # cleared only on a socket drop, NOT by the bare/Disconnected backend
        # frames the device emits periodically while fully usable.
        self._device_attached: dict[str, bool] = {}
        self._attached_events: dict[str, asyncio.Event] = {}

        # One command in flight per device + request/response correlation. A
        # send registers a future keyed by its message id, and the message handler
        # resolves it when the matching Response arrives (drain each response).
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._pending_responses: dict[str, dict[int, asyncio.Future[int]]] = {}

        # Random per-device sessionId, mirroring the app (a per-launch
        # SecureRandom token). Created once per API lifetime and reused across
        # reconnects. The device treats it as opaque (it even tolerates a null
        # sessionId), so rotating it per socket isn't needed.
        self._session_ids: dict[str, str] = {}

    def _extract_token_expiration(self, token: str) -> float | None:
        """Extract expiration time from JWT token."""
        if not token:
            return None

        try:
            # JWT tokens have 3 parts separated by dots
            parts = token.split(".")
            if len(parts) != 3:
                _LOGGER.debug(
                    "Token is not a JWT (doesn't have 3 parts), assuming no expiration info available"
                )
                return None

            # Decode the payload (second part)
            payload = parts[1]
            # Add padding if needed for base64 decoding
            payload += "=" * (4 - len(payload) % 4)

            try:
                decoded = base64.urlsafe_b64decode(payload)
                payload_data = json.loads(decoded.decode("utf-8"))

                # JWT standard 'exp' field contains expiration timestamp
                exp = payload_data.get("exp")
                if exp:
                    exp_time = float(exp)
                    current_time = time.time()
                    expires_in_minutes = (exp_time - current_time) / 60
                    _LOGGER.debug(
                        "JWT token expires in %.1f minutes (exp=%d)",
                        expires_in_minutes,
                        exp,
                    )
                    return exp_time
                else:
                    _LOGGER.debug("JWT token has no 'exp' field")
                    return None

            except (
                binascii.Error,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as e:
                _LOGGER.debug("Failed to decode JWT payload: %s", e)
                return None

        except Exception as e:
            _LOGGER.debug("Failed to extract token expiration: %s", e)

        return None

    def register_device(self, speaker_uid: str, device_ip: str | None = None) -> None:
        """Register a speaker so connect/reconnect can find it.

        `device_ip` is an optional manual LAN address override: when set, the
        local socket connects straight to it instead of resolving the
        speaker's mDNS name.
        """
        for device in self._device_list:
            if device.get("speaker_uid") == speaker_uid:
                device["device_ip"] = device_ip
                return
        self._device_list.append({"speaker_uid": speaker_uid, "device_ip": device_ip})

    def set_connection_change_callback(self, callback: Callable[[str], None]) -> None:
        """Set a sync callback fired (with speaker_uid) on connectivity changes.

        Fires when a socket connects, when a socket drops, and when relay
        attachment latches, so the caller can re-derive its connected state.
        """
        self._connection_change_callback = callback

    def _notify_connection_change(self, speaker_uid: str) -> None:
        """Invoke the connection-change callback, swallowing its errors."""
        if self._connection_change_callback is None:
            return
        try:
            self._connection_change_callback(speaker_uid)
        except Exception:
            _LOGGER.exception("Connection-change callback failed for %s", speaker_uid)

    @staticmethod
    def _conn_key(speaker_uid: str, transport: str) -> str:
        """The websocket dict key for one device/transport pair."""
        return f"{speaker_uid}{_KEY_SEP}{transport}"

    @staticmethod
    def _split_conn_key(connection_key: str) -> tuple[str, str]:
        """Inverse of `_conn_key`: (speaker_uid, transport)."""
        speaker_uid, _, transport = connection_key.rpartition(_KEY_SEP)
        return speaker_uid, transport

    def _local_ws_url(self, speaker_uid: str) -> str:
        """Direct-LAN websocket URL for a speaker (deterministic mDNS name).

        `wss://Nanit-<speaker_uid>.local:442`, no path (the relay's
        `/<uid>/user_connect/` path is remote-only). Reverse-engineered from
        the app's ConnectivityRouteLocalMDNS.
        """
        host = f"{SOUND_LIGHT_LOCAL_MDNS_PREFIX}{speaker_uid}.local"
        return f"wss://{host}:{SOUND_LIGHT_LOCAL_WS_PORT}"

    @staticmethod
    def _build_insecure_ssl_context() -> ssl.SSLContext:
        """A trust-all TLS context for the LOCAL device socket.

        The speaker presents a self-signed cert on the LAN and the official app
        accepts ANY cert and ANY hostname for it (its local OkHttp client uses an
        empty TrustManager + always-true HostnameVerifier). We match that: there
        is no public CA to verify against and no cert to pin. Only ever used for
        the on-LAN device. The cloud relay keeps full verification.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _fetch_device_token(self, speaker_uid: str) -> None:
        """Fetch + cache the per-device token used for LOCAL socket auth.

        Delegates the REST call (`GET /speakers/{uid}/udtokens`) to the
        injected fetcher and reads the token's expiry from its JWT `exp`
        claim. Best-effort: any failure (endpoint shape, 404, network) just
        leaves the device without a local token, so the integration stays on
        the remote relay.
        """
        try:
            token = await self._device_token_fetcher(speaker_uid)
            if not token:
                _LOGGER.debug("Device-token fetch for %s returned no token", speaker_uid)
                return
            # The udtoken is an RS256 JWT, so its own `exp` claim is the
            # authoritative expiry (the REST body's expiration field matches).
            expires_at = self._extract_token_expiration(token)
            self._device_tokens[speaker_uid] = (token, expires_at)
            _LOGGER.debug("Cached local device token for %s", speaker_uid)
        except Exception as e:
            _LOGGER.debug("Device-token fetch failed for %s: %s", speaker_uid, e)

    async def _ensure_device_token(self, speaker_uid: str) -> str | None:
        """Return a usable local device token, fetching/refreshing if needed."""
        cached = self._device_tokens.get(speaker_uid)
        if cached is not None:
            token, expires_at = cached
            if expires_at is None or time.time() < expires_at - 60:
                return token
        await self._fetch_device_token(speaker_uid)
        cached = self._device_tokens.get(speaker_uid)
        return cached[0] if cached else None

    def _transport_connected(self, connection_key: str) -> bool:
        """True if the socket for this exact (device, transport) is open."""
        websocket = self._websockets.get(connection_key)
        return websocket is not None and not self._is_websocket_closed(websocket)

    def _any_transport_connected(self, speaker_uid: str) -> bool:
        """True if ANY of a device's transports has a live socket."""
        return any(self._transport_connected(self._conn_key(speaker_uid, t)) for t in _TRANSPORTS)

    def _active_connection_key(self, speaker_uid: str) -> str | None:
        """The connection key to SEND on, preferring local over remote."""
        for transport in _TRANSPORTS:  # local first
            key = self._conn_key(speaker_uid, transport)
            if self._transport_connected(key):
                return key
        return None

    def active_transport(self, speaker_uid: str) -> str | None:
        """User-facing label for the transport sends currently route over.

        `"local"` (direct LAN), `"cloud"` (the relay), or `None` when the
        device is unreachable. Backs the Connection Type diagnostic sensor.
        """
        key = self._active_connection_key(speaker_uid)
        if key is None:
            return None
        _baby, transport = self._split_conn_key(key)
        return "local" if transport == TRANSPORT_LOCAL else "cloud"

    @staticmethod
    def _handshake_status(exc: BaseException) -> int | None:
        """HTTP status from a rejected WebSocket handshake, or None.

        websockets >= 13 raises `InvalidStatus` carrying `.response.status_code`;
        older builds raised `InvalidStatusCode` with `.status_code`. Read both
        defensively so auth-rejection handling works across the supported range.
        A non-handshake error (DNS failure, refused, timeout) has neither, so
        this returns None and the caller treats it as a transient error.
        """
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        return status if isinstance(status, int) else None

    async def _connect_transport(self, device_info: dict[str, Any], transport: str) -> None:
        """Open one socket for a device on a given transport (idempotent).

        Local failures are logged at debug and swallowed (the relay covers
        control). Remote failures stay at error. Device-level attachment is NOT
        reset here. It is sticky (set by frames, cleared only when all of a
        device's transports drop), so bringing up a second transport never
        re-gates an already-working device.
        """
        speaker_uid = device_info["speaker_uid"]
        connection_key = self._conn_key(speaker_uid, transport)

        lock = self._connect_locks.setdefault(connection_key, asyncio.Lock())
        async with lock:
            if self._closing:
                return  # shutting down: never open new sockets
            if self._transport_connected(connection_key):
                return  # connected while we waited for the lock

            # Auth-rejection cooldown: a transport that keeps being refused is in
            # a back-off window. Skip the connect ATTEMPT (before the /udtokens
            # fetch and the handshake), so any driver, the reconnect loop AND the
            # 30s poll via ensure_websocket_connection, respects the back-off and
            # a wedged device stops triggering token refetches.
            cooldown_until = self._auth_reject_until.get(connection_key, 0)
            remaining = cooldown_until - time.time()
            if remaining > 0:
                _LOGGER.debug(
                    "Skipping %s connect for %s, in auth-reject cooldown for %.0fs",
                    transport,
                    speaker_uid,
                    remaining,
                )
                self._schedule_reconnect(speaker_uid, transport)
                return

            token: str | None
            if transport == TRANSPORT_REMOTE:
                ws_url = f"{SOUND_LIGHT_WS_BASE_URL}/{speaker_uid}/user_connect/"
                # The provider (TokenManager) refreshes a stale token itself,
                # so a handshake here never carries a hard-expired token into
                # a guaranteed 401 (which would count toward the auth-reject
                # backoff and cool the transport down for minutes).
                try:
                    token = await self._access_token_provider()
                except Exception as e:
                    self._log_transient_connect_failure(connection_key, transport, speaker_uid, e)
                    self._schedule_reconnect(speaker_uid, transport)
                    return
            else:  # local
                ws_url = self._local_ws_url(speaker_uid)
                device_ip = device_info.get("device_ip")
                if device_ip:
                    # Manual per-speaker IP override from the options flow:
                    # skip mDNS entirely and connect straight to it.
                    ws_url = f"wss://{device_ip}:{SOUND_LIGHT_LOCAL_WS_PORT}"
                elif self._local_host_resolver is not None:
                    # Resolve the device's LAN IP if a resolver is injected
                    # (HA-in-container can't do `.local` via libc). The resolver
                    # (HA zeroconf) finds the device by uid in its mDNS cache and
                    # returns an IP. On HA OS (no resolver) we hand the
                    # deterministic `.local` name straight to the OS resolver.
                    ip = await self._local_host_resolver(speaker_uid)
                    if not ip:
                        _LOGGER.debug(
                            "Local mDNS resolve failed for %s, staying on relay",
                            speaker_uid,
                        )
                        # Keep the local retry loop alive: the device may join
                        # the LAN (or mDNS may settle) later. The local backoff
                        # caps at 90s, so a permanently-remote setup only pays
                        # a cheap periodic browse.
                        self._schedule_reconnect(speaker_uid, transport)
                        return
                    ws_url = f"wss://{ip}:{SOUND_LIGHT_LOCAL_WS_PORT}"
                token = await self._ensure_device_token(speaker_uid)
            if not token:
                # No usable token (no access token, or local token unavailable).
                self._schedule_reconnect(speaker_uid, transport)
                return

            # The WebSocket handshake uses the `token` auth scheme, NOT `Bearer`,
            # verified in the app (WebSocketClient sends `Authorization: token
            # <token>`), for BOTH local and remote. The remote token is the user
            # access token. The local token is the per-device token.
            headers = {"Authorization": f"token {token}"}

            try:
                # TLS context only for wss:// (plaintext ws:// is used by tests
                # against an in-process fake). The local device uses a trust-all
                # context (self-signed cert, app accepts any). Remote uses full
                # verification. Build it off the event loop.
                ssl_context = None
                if ws_url.startswith("wss://"):
                    loop = asyncio.get_event_loop()
                    builder = (
                        self._build_insecure_ssl_context
                        if transport == TRANSPORT_LOCAL
                        else ssl.create_default_context
                    )
                    try:
                        ssl_context = await loop.run_in_executor(None, builder)
                    except RuntimeError as e:
                        # HA is tearing down its executor (restart/stop) while a
                        # reconnect was in flight. Not a device failure: don't
                        # count it as a transient error or log it loudly.
                        _LOGGER.debug(
                            "Skipping %s connect for %s during shutdown: %s",
                            transport,
                            speaker_uid,
                            e,
                        )
                        return

                websocket = await websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ssl=ssl_context,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=WS_CLOSE_TIMEOUT,
                )

                if self._closing:
                    # close() ran while the handshake was in flight; this
                    # socket would outlive the transport (and squat the
                    # speaker's one local slot). Close it, don't store it.
                    await websocket.close()
                    return

                self._websockets[connection_key] = websocket
                # Connected cleanly, so clear any persistent auth-rejection and
                # transient-failure state for this transport (back to the fast
                # backoff and loud logging on a future drop).
                self._auth_reject_counts.pop(connection_key, None)
                self._auth_reject_until.pop(connection_key, None)
                self._transient_fail_counts.pop(connection_key, None)

                # A local socket is a direct LAN connection to the device, so
                # connecting at all means the device is present and reachable.
                # The backend "Connected" readiness frame is relay-only, so mark
                # the device attached here. Otherwise a pure-local connection
                # (cloud down) could never bootstrap, since the poll gates on
                # attachment and attachment otherwise only latches on a Response.
                if transport == TRANSPORT_LOCAL:
                    self._mark_attached(speaker_uid)

                # Start message handler, keeping a strong reference so it can't
                # be garbage-collected mid-run, and drop the ref when it finishes.
                task = asyncio.create_task(self._handle_messages(connection_key, websocket))
                self._handler_tasks[connection_key] = task

                def _drop_handler_ref(done: asyncio.Task[None]) -> None:
                    # Pop by identity: a reconnect may have registered a NEWER
                    # handler under this key while the old one drained its
                    # close handshake, and the old task's callback must not
                    # evict the replacement's strong reference.
                    if self._handler_tasks.get(connection_key) is done:
                        self._handler_tasks.pop(connection_key, None)

                task.add_done_callback(_drop_handler_ref)

                # Nothing else to send on open. The app sends nothing until the
                # route is ready (the backend Connected frame on remote, the
                # connect itself on local), and the coordinator poll plus
                # send_saved_sounds_request both wait for attachment.
                _LOGGER.debug(
                    "Connected to Sound + Light device %s via %s",
                    speaker_uid,
                    transport,
                )
                self._notify_connection_change(speaker_uid)

            except Exception as e:
                if isinstance(e, RuntimeError) and "shutdown" in str(e).lower():
                    # HA is tearing down its executor (restart/stop) while a
                    # reconnect was in flight. The guard around the SSL-context
                    # builder above catches most of these, but the RuntimeError
                    # can also surface from inside the connect call (its DNS
                    # resolution uses the executor too). Not a device failure:
                    # no error counting, no retry scheduling, no loud log.
                    _LOGGER.debug(
                        "Skipping %s connect for %s during shutdown: %s",
                        transport,
                        speaker_uid,
                        e,
                    )
                    return
                status = self._handshake_status(e)
                reject_statuses = (
                    _AUTH_REJECT_STATUSES_LOCAL
                    if transport == TRANSPORT_LOCAL
                    else _AUTH_REJECT_STATUSES_REMOTE
                )
                if status in reject_statuses:
                    self._handle_auth_reject(connection_key, transport, speaker_uid, e)
                else:
                    # Transient error (DNS, refused, timeout, mid-handshake
                    # drop). Keeps the fast app-matching backoff. Only the log
                    # level is throttled.
                    self._log_transient_connect_failure(connection_key, transport, speaker_uid, e)
                # Arm the per-transport retry loop. Without this, a transport
                # whose FIRST-EVER connect fails (e.g. a local 403 at startup)
                # was never retried: the drop-driven reconnect only covers
                # sockets that had connected, and the poll skips reconnects
                # while the other transport is up. A no-op when the loop is
                # already the caller.
                self._schedule_reconnect(speaker_uid, transport)

    def _handle_auth_reject(
        self, connection_key: str, transport: str, speaker_uid: str, exc: Exception
    ) -> None:
        """Record a rejected handshake (401/403, or the relay's 404) and log it.

        The local socket's per-device token is dropped so the next attempt
        refetches a fresh one: the device rotates that token server-side, and it
        can rotate before our cached copy's clock expiry, so a 401/403 is the
        only signal that the cached token went stale. (The relay uses the user
        access token, refreshed elsewhere, so there is nothing to drop there.)
        Logging is loud for the first few attempts, then a single WARNING when we
        switch to the long retry interval, then debug, so a device that is
        reachable but refusing auth can't flood the log.
        """
        if transport == TRANSPORT_LOCAL:
            self._device_tokens.pop(speaker_uid, None)
        count = self._auth_reject_counts.get(connection_key, 0) + 1
        self._auth_reject_counts[connection_key] = count
        if count >= AUTH_REJECT_BACKOFF_THRESHOLD:
            # Arm (or extend) the cooldown so every connect driver backs off,
            # not just the reconnect loop. The first few rejections below the
            # threshold still retry fast and refetch the token, so a genuine
            # token rotation self-heals quickly; only a persistently-refused
            # (wedged) device gets time-gated.
            self._auth_reject_until[connection_key] = time.time() + AUTH_REJECT_RETRY_INTERVAL
        if count < AUTH_REJECT_BACKOFF_THRESHOLD:
            log = _LOGGER.debug if transport == TRANSPORT_LOCAL else _LOGGER.error
            log(
                "Sound + Light device %s rejected %s auth (%s), retrying",
                speaker_uid,
                transport,
                exc,
            )
        elif count == AUTH_REJECT_BACKOFF_THRESHOLD:
            _LOGGER.warning(
                "Sound + Light device %s keeps refusing %s auth after %d attempts. "
                "It is reachable but rejecting credentials, which usually means the "
                "device needs a power cycle. Backing off to retry every %ds and "
                "quieting these logs until it recovers",
                speaker_uid,
                transport,
                count,
                AUTH_REJECT_RETRY_INTERVAL,
            )
        else:
            _LOGGER.debug(
                "Sound + Light device %s still refusing %s auth (attempt %d)",
                speaker_uid,
                transport,
                count,
            )

    def _log_transient_connect_failure(
        self, connection_key: str, transport: str, speaker_uid: str, exc: Exception
    ) -> None:
        """Log a transient (non-auth) connect failure without flooding.

        Mirrors _handle_auth_reject's shape: loud ERROR for the first few
        attempts, one WARNING when we quiet down, then debug until the
        transport reconnects (which resets the count). Only logging changes
        here. The reconnect loop keeps its fast schedule, so an extended
        cloud outage or an unplugged device can't fill the log at one ERROR
        per retry. Local stays at debug always (best-effort, remote covers
        control).
        """
        if transport == TRANSPORT_LOCAL:
            _LOGGER.debug(
                "Failed to connect to Sound + Light device %s via %s: %s",
                speaker_uid,
                transport,
                exc,
            )
            return
        count = self._transient_fail_counts.get(connection_key, 0) + 1
        self._transient_fail_counts[connection_key] = count
        if count < TRANSIENT_FAIL_LOG_THRESHOLD:
            _LOGGER.error(
                "Failed to connect to Sound + Light device %s via %s: %s",
                speaker_uid,
                transport,
                exc,
            )
        elif count == TRANSIENT_FAIL_LOG_THRESHOLD:
            _LOGGER.warning(
                "Sound + Light device %s is still unreachable via %s after %d "
                "attempts (%s). Still retrying on the same schedule, but "
                "quieting these logs until it reconnects",
                speaker_uid,
                transport,
                count,
                exc,
            )
        else:
            _LOGGER.debug(
                "Sound + Light device %s still unreachable via %s (attempt %d): %s",
                speaker_uid,
                transport,
                count,
                exc,
            )

    async def connect_device(self, speaker_uid: str) -> None:
        """Connect a device's transports: remote always, local when enabled.

        The device must have been registered via `register_device` first.
        Both transports can be open simultaneously (the app does this
        on-LAN), and sends prefer local. Local is best-effort and never
        blocks remote. A local failure is swallowed inside
        `_connect_transport`.
        """
        device_info = next(
            (d for d in self._device_list if d.get("speaker_uid") == speaker_uid), None
        )
        if device_info is None:
            _LOGGER.error("connect_device called for unregistered %s", speaker_uid)
            return
        await self._connect_transport(device_info, TRANSPORT_REMOTE)
        if self._local_enabled:
            await self._connect_transport(device_info, TRANSPORT_LOCAL)

    def _eligible_transports(self) -> tuple[str, ...]:
        """Transports we should (re)connect: remote always, local if enabled."""
        if self._local_enabled:
            return _TRANSPORTS
        return (TRANSPORT_REMOTE,)

    def _schedule_reconnect(self, speaker_uid: str, transport: str | None = None) -> None:
        """Start backoff reconnect loop(s) for a device.

        `transport` reconnects just that one (used when a specific socket
        drops). `None` schedules every eligible transport (used when a send
        finds no live socket at all).
        """
        if self._closing:
            return
        transports = (transport,) if transport is not None else self._eligible_transports()
        for t in transports:
            connection_key = self._conn_key(speaker_uid, t)
            task = self._reconnect_tasks.get(connection_key)
            if task is not None and not task.done():
                continue
            self._reconnect_tasks[connection_key] = asyncio.create_task(
                self._reconnect_with_backoff(speaker_uid, t)
            )

    async def _reconnect_with_backoff(self, speaker_uid: str, transport: str) -> None:
        """Reconnect one dropped transport, backing off like the official app.

        Per-transport so a dead local socket can't stall remote reconnection
        (and vice-versa). Local uses the slower local schedule and gives up if no
        device token is available. A later full connect (driven by the poll)
        retries it.
        """
        device_info = next(
            (d for d in self._device_list if d.get("speaker_uid") == speaker_uid), None
        )
        if device_info is None:
            return

        connection_key = self._conn_key(speaker_uid, transport)
        backoff = _local_reconnect_backoff if transport == TRANSPORT_LOCAL else _reconnect_backoff
        retries = 0
        while not self._closing and not self._transport_connected(connection_key):
            # A transport whose handshake keeps being refused (a wedged device)
            # gets a long, quiet retry interval instead of the fast app-matching
            # schedule, so it can't hammer the cloud or flood the log. A clean
            # connect resets the count (in _connect_transport) and we fall back
            # to the fast schedule.
            if self._auth_reject_counts.get(connection_key, 0) >= AUTH_REJECT_BACKOFF_THRESHOLD:
                delay = AUTH_REJECT_RETRY_INTERVAL
            else:
                delay = backoff(retries)
            if delay:
                await asyncio.sleep(delay)
            if self._closing:
                return
            _LOGGER.debug(
                "Reconnecting to %s via %s (attempt %d)",
                speaker_uid,
                transport,
                retries + 1,
            )
            await self._connect_transport(device_info, transport)
            retries += 1

        if self._transport_connected(connection_key):
            _LOGGER.debug(
                "Reconnected to %s via %s after %d attempt(s)",
                speaker_uid,
                transport,
                retries,
            )

    def _next_message_id(self) -> int:
        """Return a unique, monotonically increasing control-message id.

        The official app stamps every control request with an incrementing id
        (an AtomicInteger) and correlates responses by it. We previously sent
        `id=1` on every command, so concurrent commands (e.g. a Home
        Assistant scene touching power + sound + volume + light at once) were
        indistinguishable, and their out-of-order responses could clobber each
        other's state. asyncio is single-threaded, so a plain increment is
        race-free here.
        """
        self._message_id += 1
        return self._message_id

    def _session_id(self, speaker_uid: str) -> str:
        """Return this device's random sessionId, creating one if needed."""
        sid = self._session_ids.get(speaker_uid)
        if sid is None:
            # ~50 random bits as an opaque token (the app uses BigInteger(50,
            # SecureRandom).toString(32). The device treats this as opaque and
            # tolerates a null sessionId, so the exact radix doesn't matter).
            sid = format(secrets.randbits(50), "x")
            self._session_ids[speaker_uid] = sid
        return sid

    def _attached_event(self, speaker_uid: str) -> asyncio.Event:
        """The per-device 'backend Connected' event (created lazily)."""
        return self._attached_events.setdefault(speaker_uid, asyncio.Event())

    def is_device_attached(self, speaker_uid: str) -> bool:
        """True once the relay reported the physical device as attached.

        Gates entity availability and command sends. A socket can be open while
        the device behind the relay is still Disconnected, in which case sending
        only produces latency. Set from the Backend frame's device.status, and
        also inferred from any genuine Response/settings traffic (if the device
        is answering, it's clearly attached) so a missed/renamed backend frame
        can't wedge us permanently.
        """
        return self._device_attached.get(speaker_uid, False)

    def _mark_attached(self, speaker_uid: str) -> None:
        """Latch the device as attached and wake anyone waiting to send."""
        newly_attached = not self._device_attached.get(speaker_uid, False)
        self._device_attached[speaker_uid] = True
        self._attached_event(speaker_uid).set()
        if newly_attached:
            # Attachment feeds the caller's connected state, so surface the
            # transition (only the first time, this latches on every Response).
            self._notify_connection_change(speaker_uid)

    def _mark_detached(self, speaker_uid: str) -> None:
        """Clear attachment (socket dropped or backend reported Disconnected)."""
        self._device_attached[speaker_uid] = False
        event = self._attached_events.get(speaker_uid)
        if event is not None:
            event.clear()

    async def wait_for_device_attached(
        self, speaker_uid: str, timeout: float | None = None
    ) -> bool:
        """Wait up to `timeout` for the backend Connected frame.

        Returns True once attached, False on timeout. Callers decide what to do
        on False (commands send best-effort, the poll just warns). `timeout`
        defaults to `DEVICE_ATTACH_TIMEOUT` read at call time (so tests can
        monkeypatch the module constant).
        """
        if timeout is None:
            timeout = DEVICE_ATTACH_TIMEOUT
        if self.is_device_attached(speaker_uid):
            return True
        try:
            await asyncio.wait_for(self._attached_event(speaker_uid).wait(), timeout)
            return True
        except TimeoutError:
            return False

    def _resolve_pending_response(self, speaker_uid: str, response: Any) -> None:
        """Resolve the awaiting send (if any) for an inbound Response by requestId.

        Mirrors the app's correlation: a Response carries the requestId of the
        Request it answers, and we hand its statusCode to the matching send so it
        can confirm success (2xx) or surface a rejection. Unmatched Responses
        (e.g. our fire-and-forget GetSettings poll) simply have no waiter.
        """
        if not response.HasField("requestId"):
            return
        request_id = response.requestId
        status_code = response.statusCode if response.HasField("statusCode") else 200
        future = self._pending_responses.get(speaker_uid, {}).get(request_id)
        if future is not None and not future.done():
            future.set_result(status_code)

    def _fail_pending_responses(self, speaker_uid: str, error: Exception) -> None:
        """Fail all in-flight sends for a device (socket dropped before ack)."""
        for future in self._pending_responses.pop(speaker_uid, {}).values():
            if not future.done():
                future.set_exception(error)

    def build_control_message(
        self, session_id: str | None = None, **kwargs: Any
    ) -> tuple[bytes, int]:
        """Build a serialized control Message from the given fields.

        Returns `(message_bytes, message_id)`. Every provided field is packed
        into a SINGLE `Settings` message, so a coalesced multi-field command
        (the app's "apply a preset" pattern) is one atomic write rather than
        several racing writes. Pure and synchronous with no websocket, so it is
        unit-testable offline.
        """
        message = Message()
        request = Request()
        settings = Settings()

        message_id = self._next_message_id()
        request.id = message_id
        if session_id is not None:
            request.sessionId = session_id

        # Set control parameters
        if "is_on" in kwargs:
            settings.isOn = kwargs["is_on"]
        if "brightness" in kwargs:
            settings.brightness = float(kwargs["brightness"])
        if "volume" in kwargs:
            settings.volume = float(kwargs["volume"])
        if "color" in kwargs:
            color_info = kwargs["color"]
            color_data = Color()
            # Only set the color sub-fields actually provided. A bare
            # {noColor: true} frame (the app's "Light off") must NOT carry
            # hue=0/saturation=0, which would overwrite the device's stored
            # color. Validated 2026-07-11: a brightness-0 off/on cycle
            # round-trips the stored color, but a bare noColor:false does NOT
            # restore it (lands on white), which is why turn_on always sends
            # explicit hue/sat.
            if "noColor" in color_info:
                color_data.noColor = color_info["noColor"]
            if "hue" in color_info:
                color_data.hue = float(color_info["hue"])
            if "saturation" in color_info:
                color_data.saturation = float(color_info["saturation"])
            # Note: brightness is sent separately in Settings.brightness, not in Color
            settings.color.CopyFrom(color_data)

            # Set brightness separately in Settings (matches official APK pattern)
            if "brightness" in color_info:
                settings.brightness = float(color_info["brightness"])
        if "sound" in kwargs:
            sound_option = kwargs["sound"]
            sound_data = Sound()
            if sound_option is None:
                # Bare "sound on" with no track named: clear noSound and let
                # the device resume whatever track it last played.
                sound_data.noSound = False
            elif sound_option == "No sound":
                sound_data.noSound = True
                sound_data.track = ""  # Empty track when no sound
            else:
                sound_data.noSound = False
                sound_data.track = str(sound_option)
            settings.sound.CopyFrom(sound_data)

        # Set the settings in the request, and the request in the message
        request.settings.CopyFrom(settings)
        message.request.CopyFrom(request)

        return message.SerializeToString(), message_id

    async def send_control_command(self, speaker_uid: str, **kwargs: Any) -> None:
        """Send one control command and await the device's ack, like the app.

        Mirrors the official app's transaction model (SocketRequestManager): one
        Request in flight per device, await the Response whose `requestId`
        matches (10s). One send, no retry: the app never re-sends, and re-sending
        on a slow ack piles duplicates onto a busy device and wedges it. A slow/
        absent ack on a LIVE socket is accepted optimistically (device busy, not
        gone, the pin holds the UI, the device pushes real state when it catches
        up). A socket drop or an explicit non-2xx rejection raises so the
        coordinator rolls back the optimistic UI.
        """
        # Ensure we have a healthy WebSocket connection. Raise (rather than
        # silently return) so the caller's failure surfaces instead of the
        # command appearing to succeed while nothing reached the device, and
        # kick a reconnect.
        if not await self.ensure_websocket_connection(speaker_uid):
            self._schedule_reconnect(speaker_uid)
            raise ConnectionError(
                f"No WebSocket connection to send control command for {speaker_uid}"
            )

        # Check if protobuf classes are available
        if not PROTOBUF_AVAILABLE:
            _LOGGER.error("Protobuf classes not available, cannot send control command")
            return

        # Readiness gate: the relay can be up while the physical device is still
        # Disconnected behind it, in which case a command just stalls. Wait for
        # the backend Connected frame, but fall back to a best-effort send if it
        # never arrives. A missed/renamed backend frame must not brick control
        # (the ack-await below still surfaces a genuine failure).
        if not await self.wait_for_device_attached(speaker_uid):
            _LOGGER.warning(
                "Device %s not confirmed attached (no backend Connected frame), "
                "sending command best-effort",
                speaker_uid,
            )

        message_bytes, message_id = self.build_control_message(
            session_id=self._session_id(speaker_uid), **kwargs
        )
        _LOGGER.debug(
            "Sending protobuf control for %s (id=%s): %s",
            speaker_uid,
            message_id,
            kwargs,
        )
        try:
            await self._transact(speaker_uid, message_bytes, message_id)
            _LOGGER.debug("Control command id=%s on %s acked", message_id, speaker_uid)
        except CommandTimeoutError:
            # Slow/absent ack but the socket is alive: the device is busy, not
            # gone. Do NOT re-send (duplicates overload it) and do NOT roll back.
            # Accept optimistically. The device applies + pushes state when it
            # drains, and the 30s poll reconciles if it never landed.
            _LOGGER.warning(
                "No prompt ack for %s command id=%s (device busy), "
                "not re-sending, keeping optimistic state",
                speaker_uid,
                message_id,
            )

    async def _transact(self, speaker_uid: str, message_bytes: bytes, message_id: int) -> int:
        """Send one CONTROL command under the per-device lock and await its ack.

        One command in flight per device (the app's model): hold the lock until
        the matching Response (by requestId) arrives or the ack times out. Raises
        CommandTimeoutError on a slow or absent ack, in which case the caller keeps
        the optimistic state. Raises ConnectionError on a non-2xx rejection, in
        which case the caller rolls back. If the socket carrying the command drops
        mid-flight while another transport is still up, the command is re-sent once
        on the surviving transport instead of failing. (Polls and diagnostics use
        _send_no_wait, which does not await, so a slow read cannot stall a command.)
        Returns the 2xx status code.
        """
        lock = self._send_locks.setdefault(speaker_uid, asyncio.Lock())
        async with lock:
            # At most two attempts. The second only happens when the first
            # socket drops mid-flight and the device is still reachable on the
            # other transport.
            for attempt in (1, 2):
                # Pick the transport to send on (prefer local).
                connection_key = self._active_connection_key(speaker_uid)
                websocket = self._websockets.get(connection_key) if connection_key else None
                if (
                    connection_key is None
                    or websocket is None
                    or self._is_websocket_closed(websocket)
                ):
                    self._schedule_reconnect(speaker_uid)
                    raise ConnectionError(
                        f"WebSocket closed before sending request for {speaker_uid}"
                    )

                future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
                self._pending_responses.setdefault(speaker_uid, {})[message_id] = future
                # Track the transport this in-flight command went out on so the
                # handler only fails it when THIS socket drops, not a redundant one.
                self._inflight_conn_key[speaker_uid] = connection_key
                try:
                    await websocket.send(message_bytes)
                    status_code = await asyncio.wait_for(future, timeout=COMMAND_ACK_TIMEOUT)
                except TimeoutError as e:
                    # Slow or absent ack on a live socket: the device is busy, not
                    # gone. Don't reconnect or re-send (re-sending piles duplicates
                    # on a busy device). The caller keeps the optimistic state.
                    raise CommandTimeoutError(
                        f"No ack for command id={message_id} on {speaker_uid} "
                        f"within {COMMAND_ACK_TIMEOUT}s"
                    ) from e
                except (ConnectionError, ConnectionClosed) as e:
                    # The socket dropped before the ack, signalled either by
                    # the handler failing the future (a real ConnectionError)
                    # or by send() itself raising on a just-died socket
                    # (websockets' ConnectionClosed, which is NOT a
                    # ConnectionError subclass. Without catching it here the
                    # failover below never ran for a send-time drop). If the
                    # device is still reachable on the other transport,
                    # re-send there once. Otherwise normalize to
                    # ConnectionError so the caller rolls back.
                    if attempt == 1 and self._any_transport_connected(speaker_uid):
                        continue
                    if isinstance(e, ConnectionError):
                        raise
                    raise ConnectionError(
                        f"WebSocket closed sending command id={message_id} on {speaker_uid}"
                    ) from e
                finally:
                    self._pending_responses.get(speaker_uid, {}).pop(message_id, None)
                    self._inflight_conn_key.pop(speaker_uid, None)
                    # When send() raised, the handler may ALSO have failed this
                    # future: retrieve the exception so asyncio doesn't log an
                    # "exception was never retrieved" error for the loser.
                    if future.done() and not future.cancelled():
                        future.exception()

                if not (200 <= status_code < 300):
                    raise ConnectionError(
                        f"Device rejected command id={message_id} on {speaker_uid}: "
                        f"status {status_code}"
                    )
                return status_code

            # Attempt 2 never `continue`s, so this is unreachable in practice;
            # it keeps the declared return type honest.
            raise ConnectionError(
                f"Command id={message_id} on {speaker_uid} exhausted both transports"
            )

    async def _send_no_wait(self, speaker_uid: str, message_bytes: bytes) -> None:
        """Send a best-effort request under the per-device lock, WITHOUT awaiting
        an ack.

        Used for polls/diagnostics. The device's Response is still drained and
        parsed by the message handler. We just don't hold the lock waiting for
        it. Awaiting poll acks (the previous behaviour) serialized them with
        control commands and, when the device was slow to ack a big GetSettings,
        held the lock for seconds and stalled/timed-out the user's toggles. One
        command still stays in flight (control commands DO await their ack). A
        read overlapping a write is fine because every Response is drained.
        """
        lock = self._send_locks.setdefault(speaker_uid, asyncio.Lock())
        async with lock:
            connection_key = self._active_connection_key(speaker_uid)
            websocket = self._websockets.get(connection_key) if connection_key else None
            if websocket is None or self._is_websocket_closed(websocket):
                self._schedule_reconnect(speaker_uid)
                return
            await websocket.send(message_bytes)

    async def send_ping_for_state(self, speaker_uid: str) -> None:
        """Send comprehensive status request to get device state and sensor data."""
        # Ensure we have a healthy WebSocket connection. Unlike a control
        # command this is a best-effort poll, so don't raise, just warn and let
        # the reconnect loop bring the socket back.
        if not await self.ensure_websocket_connection(speaker_uid):
            _LOGGER.warning("Cannot send ping request, no WebSocket connection for %s", speaker_uid)
            self._schedule_reconnect(speaker_uid)
            return

        # Wait (best-effort) for the device to attach before polling state. A
        # GetSettings into a still-Disconnected relay just stalls. Don't raise.
        # If it never attaches we let the reconnect/poll cycle retry.
        if not await self.wait_for_device_attached(speaker_uid):
            _LOGGER.debug("Skipping state ping for %s, device not attached yet", speaker_uid)
            return

        try:
            if not PROTOBUF_AVAILABLE:
                _LOGGER.error("Protobuf not available for sending ping state request")
                return

            # Use proven working pattern: all=True + explicit sensor requests
            # This is the only pattern that successfully returns sensor data
            get_settings = GetSettings()
            get_settings.all = True
            get_settings.temperature = True
            get_settings.humidity = True

            # Create Request with GetSettings in field 5. A unique id (not a
            # hardcoded 1) keeps it from colliding with control-command ids in
            # the response-correlation map.
            request = Request()
            message_id = self._next_message_id()
            request.id = message_id
            request.sessionId = self._session_id(speaker_uid)
            request.getSettings.CopyFrom(get_settings)

            # Create main Message wrapper
            message = Message()
            message.request.CopyFrom(request)
            message_bytes = message.SerializeToString()

            # Best-effort fire-and-forget: the response is drained + parsed by
            # the handler. Don't hold the lock awaiting its ack (that stalled
            # user commands behind slow GetSettings responses).
            await self._send_no_wait(speaker_uid, message_bytes)

            _LOGGER.debug("Sent GetSettings request for %s", speaker_uid)

        except Exception as e:
            _LOGGER.error("Failed to send status request: %s", e)

    async def _send_query(self, speaker_uid: str, mutate_request: Callable[[Any], None]) -> None:
        """Build a diagnostics Request via `mutate_request` and send best-effort.

        Battery/wifi/firmware ride their own query request types (GetStatus /
        Network / Firmware), not the GetSettings poll. Fire-and-forget: the
        response is drained + parsed by the handler. A device that doesn't answer
        just leaves those sensors unknown. We don't await the ack so a poll can't
        stall user commands.
        """
        if not await self.ensure_websocket_connection(speaker_uid):
            self._schedule_reconnect(speaker_uid)
            return
        if not await self.wait_for_device_attached(speaker_uid, timeout=2.0):
            return
        if not PROTOBUF_AVAILABLE:
            return

        request = Request()
        request.id = self._next_message_id()
        request.sessionId = self._session_id(speaker_uid)
        mutate_request(request)
        message = Message()
        message.request.CopyFrom(request)
        try:
            await self._send_no_wait(speaker_uid, message.SerializeToString())
        except Exception as e:
            _LOGGER.debug("Diagnostics query failed for %s: %s", speaker_uid, e)

    async def send_status_request(self, speaker_uid: str) -> None:
        """Poll battery (+ temp/humidity) via GetStatus(all=true)."""

        def _mutate(request: Any) -> None:
            request.getStatus.all = True

        await self._send_query(speaker_uid, _mutate)

    async def send_network_request(self, speaker_uid: str) -> None:
        """Poll the current WiFi access point via Network{getStatus}."""

        def _mutate(request: Any) -> None:
            request.network.getStatus.SetInParent()  # present-but-empty marker

        await self._send_query(speaker_uid, _mutate)

    async def send_firmware_request(self, speaker_uid: str) -> None:
        """Fetch the firmware version via Firmware{info}."""

        def _mutate(request: Any) -> None:
            request.firmware.info.SetInParent()  # present-but-empty marker

        await self._send_query(speaker_uid, _mutate)

    def _is_websocket_closed(self, websocket: Any) -> bool:
        """Check if websocket is closed, handling different websocket library versions."""
        if websocket is None:
            return True

        try:
            # Try the standard method first
            if hasattr(websocket, "closed"):
                return bool(websocket.closed)

            # For newer websockets library versions, check state
            if hasattr(websocket, "state"):
                from websockets.protocol import State

                return websocket.state in (State.CLOSED, State.CLOSING)

            # Fallback: assume connection is open if we can't determine
            return False
        except Exception:
            # If we can't determine the state, assume it's closed for safety
            return True

    def is_websocket_connected(self, speaker_uid: str) -> bool:
        """True if the device is reachable on ANY transport (local or remote)."""
        return self._any_transport_connected(speaker_uid)

    async def ensure_websocket_connection(self, speaker_uid: str) -> bool:
        """Ensure WebSocket connection is available and healthy."""
        if self.is_websocket_connected(speaker_uid):
            return True

        _LOGGER.info("WebSocket connection needed for %s, attempting to connect...", speaker_uid)

        if not any(d.get("speaker_uid") == speaker_uid for d in self._device_list):
            _LOGGER.error("No device info found for WebSocket connection: %s", speaker_uid)
            return False

        try:
            await self.connect_device(speaker_uid)
            return self.is_websocket_connected(speaker_uid)
        except Exception as e:
            _LOGGER.error("Failed to establish WebSocket connection for %s: %s", speaker_uid, e)
            return False

    async def _handle_messages(self, connection_key: str, websocket: ClientConnection) -> None:
        """Handle incoming WebSocket messages."""
        try:
            async for raw_message in websocket:
                try:
                    if isinstance(raw_message, bytes):
                        _LOGGER.debug("Received %d bytes on %s", len(raw_message), connection_key)
                        await self._process_protobuf_message(connection_key, raw_message)
                    elif isinstance(raw_message, str):
                        _LOGGER.debug("Received text message: %s", raw_message)

                except Exception as e:
                    _LOGGER.error("Error processing message on %s: %s", connection_key, e)

        except ConnectionClosedError:
            _LOGGER.warning("WebSocket connection closed for %s, reconnecting", connection_key)
        except Exception as e:
            _LOGGER.error("Error in message handler for %s: %s", connection_key, e)
        finally:
            # Only clean up if the stored socket is still *this* one. A proactive
            # reconnect may have already replaced it under the same key.
            if self._websockets.get(connection_key) is websocket:
                del self._websockets[connection_key]
                _LOGGER.debug("Cleaned up WebSocket reference for %s", connection_key)
                speaker_uid, transport = self._split_conn_key(connection_key)
                self._notify_connection_change(speaker_uid)
                err = ConnectionError("WebSocket closed before ack")
                if not self._any_transport_connected(speaker_uid):
                    # Last transport gone: device is no longer reachable, so it's
                    # detached and any in-flight ack will never come, so fail it now
                    # so the caller rolls back instead of waiting out the timeout.
                    self._mark_detached(speaker_uid)
                    self._fail_pending_responses(speaker_uid, err)
                elif self._inflight_conn_key.get(speaker_uid) == connection_key:
                    # A redundant socket dropped while the OTHER is still up, and
                    # the in-flight command went out on THIS one. Fail it so it
                    # re-sends over the surviving transport. A command in flight on
                    # the surviving socket is left alone (it'll still get acked).
                    self._fail_pending_responses(speaker_uid, err)
                # Proactively reconnect just this transport.
                if not self._closing:
                    self._schedule_reconnect(speaker_uid, transport)

    @staticmethod
    def _parse_settings_fields(device_state: dict[str, Any], settings: Any, source: str) -> None:
        """Parse a Settings frame's core control fields into device_state.

        Shared by the Response branch (acks and poll replies) and the Request
        branch (device/app pushes). `source` only labels the debug logs.
        soundList and temperature/humidity stay in the Response branch,
        matching where the device actually sends them.
        """
        if settings.HasField("brightness"):
            brightness = _unit_float(settings.brightness)
            if brightness is not None:
                device_state["brightness"] = brightness
                _LOGGER.debug("Settings[%s] brightness: %.3f", source, brightness)
        if settings.HasField("volume"):
            volume = _unit_float(settings.volume)
            if volume is not None:
                device_state["volume"] = volume
                _LOGGER.debug("Settings[%s] volume: %.3f", source, volume)
        if settings.HasField("isOn"):
            device_state["is_on"] = settings.isOn
            _LOGGER.debug("Settings[%s] power: %s", source, settings.isOn)
        if settings.HasField("sound"):
            sound = settings.sound
            if sound.HasField("noSound") and sound.noSound:
                device_state["current_sound"] = "No sound"
                _LOGGER.debug("Settings[%s] sound: No sound", source)
            elif sound.HasField("track"):
                # Track names are untrusted device/cloud strings that become
                # the select entity's current option; clamp + printable-check
                # like the soundList branch below.
                track = _clean_device_string(sound.track)
                if track:
                    device_state["current_sound"] = track
                    _LOGGER.debug("Settings[%s] sound: %s", source, track)
        if settings.HasField("color"):
            color = settings.color
            if color.HasField("noColor"):
                device_state["no_color"] = color.noColor
            elif color.HasField("hue") or color.HasField("saturation"):
                # hue/saturation without an explicit noColor implies color mode.
                device_state["no_color"] = False
            if color.HasField("hue"):
                hue = _unit_float(color.hue)
                if hue is not None:
                    device_state["hue"] = hue
            if color.HasField("saturation"):
                saturation = _unit_float(color.saturation)
                if saturation is not None:
                    device_state["saturation"] = saturation
        # A frame without color deliberately leaves existing color state alone.

    @staticmethod
    def _parse_battery(device_state: dict[str, Any], battery: Any) -> None:
        """Parse a Status.Battery into device_state (percent + charging)."""
        if battery.HasField("soc"):
            device_state["battery_percent"] = _SOC_TO_PERCENT.get(battery.soc)
            _LOGGER.debug("Battery soc bucket=%s", battery.soc)
        # The device OMITS isCharging when it isn't charging (proto2 drops the
        # default-false field), so an absent field inside a battery status means
        # "not charging", not unknown, and not still-charging from a prior frame.
        # Set it on every battery status so unplugging flips it back to off.
        charging = battery.isCharging if battery.HasField("isCharging") else False
        device_state["battery_charging"] = charging
        _LOGGER.debug("Battery charging=%s", charging)

    @staticmethod
    def _parse_network(device_state: dict[str, Any], network_status: Any) -> None:
        """Parse a NetworkStatus.currentAp into device_state (wifi diagnostics)."""
        if not network_status.HasField("currentAp"):
            return
        ap = network_status.currentAp
        if ap.HasField("rssi"):
            device_state["wifi_rssi"] = ap.rssi
        if ap.HasField("ssid"):
            device_state["wifi_ssid"] = _clean_device_string(ap.ssid)
        if ap.HasField("bssid"):
            device_state["wifi_bssid"] = _clean_device_string(ap.bssid)
        if ap.HasField("primaryChannel"):
            device_state["wifi_channel"] = ap.primaryChannel

    async def _process_protobuf_message(self, connection_key: str, raw_message: bytes) -> None:
        """Process incoming message using pure protobuf parsing."""
        speaker_uid, _transport = self._split_conn_key(connection_key)
        device_state = self._device_state.setdefault(speaker_uid, {})

        try:
            if not PROTOBUF_AVAILABLE:
                _LOGGER.error("Protobuf not available for processing message")
                return

            message_response = Message()
            message_response.ParseFromString(raw_message)

            _LOGGER.debug("Successfully parsed as Message for %s", speaker_uid)
            _LOGGER.debug(
                "Message fields: %s",
                [field.name for field, _ in message_response.ListFields()],
            )

            # Handle response messages (responses to our requests)
            if message_response.HasField("response"):
                response = message_response.response
                response_fields = [field.name for field, _ in response.ListFields()]
                _LOGGER.debug("Response fields: %s", response_fields)

                # A Response means the relay round-tripped to the physical
                # device, so it's attached (sticky, see the backend branch).
                self._mark_attached(speaker_uid)
                self._resolve_pending_response(speaker_uid, response)

                # Handle status response for sensors (use APK field names)
                if response.HasField("status"):
                    status = response.status
                    _LOGGER.debug("Found Status field in response")
                    _LOGGER.debug(
                        "Status fields: %s",
                        [field.name for field, _ in status.ListFields()],
                    )

                    # Alternative sensor parsing from status (might be different from settings)
                    if status.HasField("temperature"):
                        temperature = _finite_float(status.temperature)
                        if temperature is not None:
                            device_state["temperature"] = temperature
                            _LOGGER.debug("Temperature: %.1f°C", temperature)
                    if status.HasField("humidity"):
                        humidity = _finite_float(status.humidity)
                        if humidity is not None:
                            device_state["humidity"] = humidity
                            _LOGGER.debug("Humidity: %.1f%%", humidity)
                    # Battery (from GetStatus): coarse 5-bucket SoC + charging.
                    if status.HasField("battery"):
                        self._parse_battery(device_state, status.battery)

                # WiFi readback (from Network{getStatus}).
                if response.HasField("networkStatus"):
                    self._parse_network(device_state, response.networkStatus)

                # Firmware version readback (from Firmware{info}).
                if response.HasField("firmware") and response.firmware.HasField("version"):
                    device_state["firmware_version"] = _clean_device_string(
                        response.firmware.version
                    )
                    _LOGGER.debug(
                        "Firmware version for %s: %s",
                        speaker_uid,
                        response.firmware.version,
                    )

                # Handle settings response (device state, use APK field names)
                if response.HasField("settings"):
                    settings = response.settings
                    self._parse_settings_fields(device_state, settings, "response")

                    # Parse available sounds list from device. Track
                    # names come from the cloud/device as untrusted
                    # strings, so clamp length and require printable chars
                    # before exposing as HA select-entity options.
                    if settings.HasField("soundList"):
                        sound_list = settings.soundList
                        if sound_list.tracks:
                            clean_tracks = [
                                t[:64]
                                for t in sound_list.tracks
                                if t and t.isprintable() and t.strip()
                            ]
                            available_sounds = ["No sound", *clean_tracks]
                            device_state["available_sounds"] = available_sounds
                            _LOGGER.debug(
                                "Received dynamic sound list for %s: %s",
                                speaker_uid,
                                available_sounds,
                            )

                    # Parse temperature and humidity sensors with test result logging
                    temp_received = settings.HasField("temperature")
                    humidity_received = settings.HasField("humidity")

                    if temp_received:
                        temperature = _finite_float(settings.temperature)
                        if temperature is not None:
                            device_state["temperature"] = temperature
                            _LOGGER.debug("Temperature: %.1f°C", temperature)

                    if humidity_received:
                        humidity = _finite_float(settings.humidity)
                        if humidity is not None:
                            device_state["humidity"] = humidity
                            _LOGGER.debug("Humidity: %.1f%%", humidity)

                    # Log test results to determine if explicit requests are needed
                    _LOGGER.debug(
                        "Sensor data received: temp=%s, humidity=%s",
                        "yes" if temp_received else "no",
                        "yes" if humidity_received else "no",
                    )

                return  # Successfully parsed as Message response

            # Handle request messages (external changes from device/app)
            elif message_response.HasField("request"):
                request = message_response.request
                _LOGGER.debug("Processing Message request (external change) for %s", speaker_uid)
                _LOGGER.debug(
                    "Request fields: %s",
                    [field.name for field, _ in request.ListFields()],
                )

                # Check for Status field for sensor data
                if request.HasField("status"):
                    status = request.status
                    _LOGGER.debug("Found Status field in external request")
                    _LOGGER.debug(
                        "Status fields: %s",
                        [field.name for field, _ in status.ListFields()],
                    )

                    if status.HasField("temperature"):
                        temperature = _finite_float(status.temperature)
                        if temperature is not None:
                            device_state["temperature"] = temperature
                            _LOGGER.debug("External temperature: %.1f°C", temperature)
                    if status.HasField("humidity"):
                        humidity = _finite_float(status.humidity)
                        if humidity is not None:
                            device_state["humidity"] = humidity
                            _LOGGER.debug("External humidity: %.1f%%", humidity)

                # Parse external changes from request.settings field
                if request.HasField("settings"):
                    _LOGGER.debug("Found settings in external request message")
                    self._parse_settings_fields(device_state, request.settings, "external")

                    # Trigger callback for external changes
                    if self._state_change_callback:
                        _LOGGER.debug("Triggering callback for external change")
                        try:
                            await self._state_change_callback(speaker_uid)
                        except Exception as callback_error:
                            _LOGGER.debug(
                                "External change callback failed: %s",
                                callback_error,
                            )

                return  # Successfully parsed as Message request

            # Backend readiness frame. The relay reports whether the physical
            # device is attached behind it, gate availability + sends on it.
            elif message_response.HasField("backend"):
                backend = message_response.backend
                backend_status = None
                if backend.HasField("device") and backend.device.HasField("status"):
                    backend_status = backend.device.status
                if backend_status == _BACKEND_STATUS_CONNECTED:
                    _LOGGER.debug("Backend: device %s attached (Connected)", speaker_uid)
                    self._mark_attached(speaker_uid)
                else:
                    # The real device sends bare/Disconnected backend frames
                    # PERIODICALLY while fully usable (it keeps acking commands
                    # and pushing state). Treating those as a hard detach made
                    # the entity flap to unavailable and blocked sends. So a
                    # non-Connected backend frame is NOT a detach: attachment
                    # is sticky once established (by a Connected frame or any
                    # real traffic) and only cleared on a socket drop.
                    _LOGGER.debug(
                        "Backend: device %s sent non-Connected status=%s "
                        "(ignored, attachment stays sticky)",
                        speaker_uid,
                        backend_status,
                    )
                return

            # Parsed as a Message but carried none of response/request/
            # backend: an unknown frame type, ignored.

        except Exception as e:
            _LOGGER.warning("Failed to parse message for %s: %s", speaker_uid, e)
            _LOGGER.debug("Message hex: %s", raw_message.hex())
            return

    def get_device_state(self, speaker_uid: str) -> dict[str, Any]:
        """Get current state for a device."""
        return self._device_state.get(speaker_uid, {})

    def set_state_change_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Set callback function to be called when device state changes via WebSocket."""
        self._state_change_callback = callback

    def set_local_host_resolver(
        self, resolver: Callable[[str], Awaitable[str | None]] | None
    ) -> None:
        """Inject an async resolver: speaker_uid -> LAN IPv4 (or None).

        The coordinator wires this to Home Assistant's zeroconf so the LAN path
        works even when the container's libc resolver can't do mDNS. `None`
        falls back to the OS resolver. Signature: async (speaker_uid) -> str|None.
        """
        self._local_host_resolver = resolver

    async def send_saved_sounds_request(self, speaker_uid: str) -> None:
        """Request available sound list from device."""
        if not self.is_websocket_connected(speaker_uid):
            return

        # Best-effort with a SHORT attach wait: this is non-critical (the
        # all=True state ping also returns the sound list), so don't let it
        # stack a full DEVICE_ATTACH_TIMEOUT on top of the ping's wait and risk
        # blowing the coordinator's first-refresh timeout on multi-device setups.
        if not await self.wait_for_device_attached(speaker_uid, timeout=2.0):
            _LOGGER.debug(
                "Skipping saved-sounds request for %s, device not attached yet",
                speaker_uid,
            )
            return

        try:
            if not PROTOBUF_AVAILABLE:
                _LOGGER.error("Protobuf not available for sending saved sounds request")
                return

            # Request saved sounds list (field 7 in GetSettings)
            get_settings = GetSettings()
            get_settings.savedSounds = True  # Request available sounds

            # Unique id (not a hardcoded 3, which collided with control-command
            # ids) so the response map stays unambiguous.
            request = Request()
            message_id = self._next_message_id()
            request.id = message_id
            request.sessionId = self._session_id(speaker_uid)
            request.getSettings.CopyFrom(get_settings)

            message = Message()
            message.request.CopyFrom(request)
            message_bytes = message.SerializeToString()

            # Best-effort fire-and-forget (response drained by the handler).
            await self._send_no_wait(speaker_uid, message_bytes)

            _LOGGER.debug("Sent saved sounds request for %s", speaker_uid)

        except Exception as e:
            _LOGGER.error("Failed to send sounds request: %s", e)

    async def close(self) -> None:
        """Close all connections and clean up resources."""
        # Stop reconnecting before tearing sockets down, else the handler's
        # teardown would immediately schedule a fresh reconnect loop.
        self._closing = True
        pending_tasks = [
            *self._reconnect_tasks.values(),
            *self._handler_tasks.values(),
        ]
        for task in pending_tasks:
            task.cancel()
        self._reconnect_tasks.clear()
        self._auth_reject_counts.clear()
        self._auth_reject_until.clear()
        self._transient_fail_counts.clear()
        self._handler_tasks.clear()

        # Fail any in-flight command waiters and drop readiness/session state so
        # a send racing the shutdown returns instead of hanging on its ack.
        for speaker_uid in list(self._pending_responses):
            self._fail_pending_responses(speaker_uid, ConnectionError("API shutting down"))
        self._inflight_conn_key.clear()
        self._device_attached.clear()
        self._attached_events.clear()
        self._session_ids.clear()

        # Close all websockets
        websocket_close_tasks = []
        for connection_key, websocket in list(self._websockets.items()):
            try:
                if not self._is_websocket_closed(websocket):
                    websocket_close_tasks.append(websocket.close())
            except Exception as e:
                _LOGGER.debug("Error preparing websocket close for %s: %s", connection_key, e)

        # Wait for all websockets to close with timeout
        if websocket_close_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*websocket_close_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "Websocket close timeout, some connections may not have closed gracefully"
                )
            except Exception as e:
                _LOGGER.debug("Error during websocket cleanup: %s", e)

        # Clear websocket references
        self._websockets.clear()

        # Wait for the cancelled tasks to actually finish, so a reload can't
        # race the old instance's teardown (handler finallys, reconnect
        # loops). Cancelled-but-unawaited tasks would otherwise still be
        # unwinding while the replacement coordinator connects.
        if pending_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError:
                _LOGGER.warning("Some connection tasks did not finish within the close timeout")

        # Clear device state
        self._device_state.clear()
        # Drop cached local device tokens (re-fetched on next connect).
        self._device_tokens.clear()
