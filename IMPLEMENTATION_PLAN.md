# Nanit Integration v1.0 — Implementation Plan

## 1. Overview

This plan describes the migration of the Nanit Home Assistant integration from a two-component architecture (Python integration + Go-based `nanitd` add-on) to a single pure-Python integration backed by a standalone `aionanit` client library.

**What changes:**
- The Go daemon (`nanitd/`) is retired. All camera communication moves into Python.
- A new PyPI package `aionanit` handles Nanit protocol details (auth, WebSocket, protobuf, state).
- The HA integration (`custom_components/nanit/`) becomes a thin adapter that maps `aionanit` state to HA entities.
- Users get a simpler setup: no add-on installation, no Supervisor dependency. Works on any HA installation type.

**Migration strategy:** Clean break. v1.0 is a new major version. Users must re-add the integration. No backward compatibility with the add-on-based v0.x architecture. Entity unique IDs are preserved so dashboards and automations survive.

---

## 2. Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Streaming | HA Stream integration (`stream_source()` returns RTMPS URL) | HA's Stream component handles HLS transcoding via PyAV/FFmpeg. Nanit cameras push RTMP to a cloud relay; we return the RTMPS URL directly. No need for our own ffmpeg process. |
| Protobuf | `betterproto` | Generates clean Python dataclasses from proto2. Async-friendly. Used in production by steam.py. |
| WebSocket | `aiohttp ws_connect()` | Already a dependency of HA. No new deps. Production-proven WS patterns in aioautomower, hikari, pybotters. |
| Transport | Local-first, cloud-fallback | Local (`wss://{ip}:442`) is lower latency and works offline. Cloud (`wss://api.nanit.com/focus/...`) is the reliable fallback. Auto-promote to local when it becomes reachable. |
| MQTT | Dropped | The Go daemon used MQTT for HA auto-discovery. With native entities, MQTT is unnecessary overhead. |
| Migration | Clean break (v1.0) | The architectural change is too fundamental for an incremental migration. New config entry required. |
| Package | `aionanit` PyPI package in monorepo | Follows HA conventions (python-kasa, aionotion, aioautomower). Published to PyPI. `manifest.json` declares it as a requirement. Monorepo for coordinated development. |

---

## 3. Monorepo Structure

```
ha-nanit/
├── AGENTS.md                           # Dev guidelines (update for new architecture)
├── CHANGELOG.md
├── IMPLEMENTATION_PLAN.md              # This file
├── README.md                           # User-facing docs (rewrite for v1.0)
├── hacs.json
├── justfile                            # Release helper
│
├── packages/
│   └── aionanit/
│       ├── pyproject.toml              # PEP 517 build config, deps: aiohttp, betterproto
│       ├── README.md                   # PyPI package README
│       ├── LICENSE
│       ├── proto/
│       │   └── nanit.proto             # Source .proto (copied from nanitd/src/proto/)
│       ├── scripts/
│       │   └── generate_proto.py       # Runs protoc + betterproto plugin
│       ├── tests/
│       │   ├── test_auth.py
│       │   ├── test_rest.py
│       │   ├── test_protocol.py
│       │   ├── test_transport.py
│       │   ├── test_pending.py
│       │   ├── test_camera.py
│       │   └── test_client.py
│       └── aionanit/
│           ├── __init__.py             # Public API exports
│           ├── client.py               # NanitClient (top-level entrypoint)
│           ├── camera.py               # NanitCamera (per-camera high-level API)
│           ├── rest.py                 # NanitRestClient (cloud REST)
│           ├── auth.py                 # TokenManager
│           ├── models.py               # All dataclasses and enums
│           ├── exceptions.py           # Error hierarchy
│           ├── ws/
│           │   ├── __init__.py
│           │   ├── transport.py        # WsTransport (connection, recv loop, reconnect)
│           │   ├── protocol.py         # Protobuf encode/decode helpers
│           │   └── pending.py          # Request/response correlation
│           └── proto/
│               ├── __init__.py         # Re-exports generated types
│               └── nanit.py            # betterproto-generated from nanit.proto
│
├── custom_components/
│   └── nanit/
│       ├── manifest.json               # v1.0.0, requirements: ["aionanit==1.0.0"]
│       ├── __init__.py                 # async_setup_entry / async_unload_entry
│       ├── hub.py                      # NanitHub (NEW — owns NanitClient lifecycle)
│       ├── coordinator.py              # Push-driven coordinators (REWRITE)
│       ├── entity.py                   # NanitEntity base (MINOR CHANGE)
│       ├── camera.py                   # Camera entity (REWRITE)
│       ├── sensor.py                   # Sensor entities (MODIFY)
│       ├── binary_sensor.py            # Binary sensor entities (MODIFY)
│       ├── switch.py                   # Switch entities (MODIFY)
│       ├── number.py                   # Volume entity (MODIFY)
│       ├── config_flow.py              # Config flow (MODIFY — remove add-on steps)
│       ├── diagnostics.py              # Diagnostics (MODIFY)
│       ├── const.py                    # Constants (MODIFY)
│       ├── strings.json                # UI strings (MODIFY)
│       └── translations/
│           └── en.json
│
└── nanitd/                             # DEPRECATED — kept for reference, not shipped
```

**Key directory decisions:**
- `packages/aionanit/proto/nanit.proto` becomes the single source of truth for the proto schema (moved from `nanitd/src/proto/`).
- `packages/aionanit/aionanit/proto/nanit.py` is the generated output, committed to git (steam.py approach) so users don't need protoc installed.
- `nanitd/` stays in the repo as reference material but is excluded from releases.

---

## 4. Package Boundary

### In `aionanit` (protocol + transport — no HA dependency)

| Responsibility | Module | Why in the library |
|---|---|---|
| Cloud REST (login, MFA, refresh, babies, events, snapshot) | `rest.py` | Protocol-level, reusable by any Python app |
| Token lifecycle (store, expiry tracking, proactive refresh) | `auth.py` | Shared across REST and WS connections |
| WebSocket connection (connect, recv loop, keepalive, reconnect, backoff) | `ws/transport.py` | Transport detail, not HA-specific |
| Protobuf encode/decode (Message envelope, Request/Response builders) | `ws/protocol.py` | Binary protocol detail |
| Request/response correlation (pending map, timeouts, cleanup) | `ws/pending.py` | Protocol detail |
| Camera state aggregation (merge push events into typed state) | `camera.py` | Normalizes raw proto into clean models |
| High-level camera API (get/set settings, controls, streaming URL) | `camera.py` | Convenience layer over raw protocol |
| Event subscription (callback registration for state changes) | `camera.py` | Push event distribution |
| Data models (sensor data, settings, status, control, connection state) | `models.py` | Typed data contracts |
| Error hierarchy | `exceptions.py` | Consistent error handling |
| Local/cloud transport selection and fallback logic | `camera.py` | Transport strategy |

### In `custom_components/nanit` (HA glue — depends on aionanit + homeassistant)

| Responsibility | Module | Why in the integration |
|---|---|---|
| ConfigEntry setup/unload lifecycle | `__init__.py` | HA-specific |
| NanitHub (owns NanitClient, manages per-camera coordinators) | `hub.py` | HA lifecycle orchestration |
| DataUpdateCoordinator (push-driven + slow resync) | `coordinator.py` | HA pattern |
| Entity base class (CoordinatorEntity, device_info, availability) | `entity.py` | HA entity framework |
| Camera entity (stream_source, snapshot, on/off) | `camera.py` | HA Camera platform |
| Sensor/binary_sensor/switch/number entities | `*.py` | HA entity platforms |
| Config flow (auth UI, baby selection, options) | `config_flow.py` | HA config framework |
| Diagnostics (redaction, state dump) | `diagnostics.py` | HA diagnostics framework |
| aiohttp ClientSession creation (via `async_get_clientsession`) | `__init__.py` | HA session management |
| Token persistence (in ConfigEntry data) | `hub.py` | HA storage |

---

## 5. aionanit — Client Library Architecture

### 5.1 `exceptions.py`

```python
class NanitError(Exception):
    """Base exception for all aionanit errors."""

class NanitAuthError(NanitError):
    """Authentication failed (invalid credentials, expired token, MFA failure)."""

class NanitMfaRequiredError(NanitAuthError):
    """MFA code required to complete login."""
    def __init__(self, mfa_token: str) -> None: ...
    mfa_token: str

class NanitConnectionError(NanitError):
    """Network-level connection failure (DNS, TCP, TLS)."""

class NanitTransportError(NanitError):
    """WebSocket transport error (unexpected close, protocol violation)."""

class NanitRequestTimeout(NanitError):
    """Protobuf request did not receive a response within the timeout."""
    def __init__(self, request_type: str, request_id: int, timeout: float) -> None: ...

class NanitProtocolError(NanitError):
    """Protobuf decode failure or unexpected message structure."""

class NanitCameraUnavailable(NanitError):
    """Camera is not reachable via any transport."""
```

### 5.2 `models.py`

All state models are frozen dataclasses. The library converts raw protobuf into these types; the HA integration only sees these — never raw proto objects.

```python
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from datetime import datetime

class TransportKind(Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    NONE = "none"

class ConnectionState(Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"

class NightLightState(Enum):
    ON = "on"
    OFF = "off"

class SensorType(IntEnum):
    SOUND = 0
    MOTION = 1
    TEMPERATURE = 2
    HUMIDITY = 3
    LIGHT = 4
    NIGHT = 5

@dataclass(frozen=True)
class SensorReading:
    sensor_type: SensorType
    value: int | None = None           # integer value (e.g., temp in C * 1000 for milli)
    value_milli: int | None = None     # milliunits (e.g., 22500 = 22.5 C)
    is_alert: bool = False
    timestamp: int | None = None

@dataclass(frozen=True)
class SensorState:
    temperature: float | None = None        # Celsius, derived from value_milli / 1000
    humidity: float | None = None           # Percentage
    light: int | None = None                # Lux
    sound_alert: bool = False
    motion_alert: bool = False
    night: bool = False                     # True = dark / night mode active

@dataclass(frozen=True)
class SettingsState:
    night_vision: bool | None = None
    volume: int | None = None               # 0-100
    sleep_mode: bool | None = None
    status_light_on: bool | None = None
    mic_mute_on: bool | None = None
    wifi_band: str | None = None            # "any", "2.4ghz", "5ghz"
    mounting_mode: str | None = None        # "stand", "travel", "switch"

@dataclass(frozen=True)
class ControlState:
    night_light: NightLightState | None = None
    night_light_timeout: int | None = None
    sensor_data_transfer_enabled: bool | None = None

@dataclass(frozen=True)
class StatusState:
    connected_to_server: bool | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    mounting_mode: str | None = None

@dataclass(frozen=True)
class ConnectionInfo:
    state: ConnectionState = ConnectionState.DISCONNECTED
    transport: TransportKind = TransportKind.NONE
    last_seen: datetime | None = None
    last_error: str | None = None
    reconnect_attempts: int = 0

@dataclass(frozen=True)
class CameraState:
    """Complete snapshot of everything known about one camera."""
    connection: ConnectionInfo = field(default_factory=ConnectionInfo)
    sensors: SensorState = field(default_factory=SensorState)
    settings: SettingsState = field(default_factory=SettingsState)
    control: ControlState = field(default_factory=ControlState)
    status: StatusState = field(default_factory=StatusState)

class CameraEventKind(Enum):
    SENSOR_UPDATE = "sensor_update"
    SETTINGS_UPDATE = "settings_update"
    CONTROL_UPDATE = "control_update"
    STATUS_UPDATE = "status_update"
    CONNECTION_CHANGE = "connection_change"

@dataclass(frozen=True)
class CameraEvent:
    kind: CameraEventKind
    state: CameraState                      # Full state snapshot after this event

@dataclass(frozen=True)
class Baby:
    uid: str
    name: str
    camera_uid: str

@dataclass(frozen=True)
class CloudEvent:
    """Event from the Nanit cloud API (motion/sound notifications)."""
    event_type: str                         # "MOTION", "SOUND", etc.
    timestamp: float                        # Unix timestamp
    baby_uid: str
```

### 5.3 `proto/` — Protobuf Generation

**Source:** `packages/aionanit/proto/nanit.proto` (the existing 263-line proto2 schema, copied from `nanitd/src/proto/`).

**Generation script** (`scripts/generate_proto.py`):
```python
#!/usr/bin/env python3
"""Generate betterproto Python code from nanit.proto."""
import subprocess, sys, pathlib

PROTO_DIR = pathlib.Path(__file__).parent.parent / "proto"
OUT_DIR = pathlib.Path(__file__).parent.parent / "aionanit" / "proto"

subprocess.run([
    sys.executable, "-m", "grpc_tools.protoc",
    f"-I{PROTO_DIR}",
    f"--python_betterproto_out={OUT_DIR}",
    str(PROTO_DIR / "nanit.proto"),
], check=True)
```

**Generated output** (`aionanit/proto/nanit.py`) produces betterproto dataclasses for every message type. Key generated classes:
- `Message` (type, request, response)
- `Request` (id, type, streaming, settings, status, sensor_data, control, playback, etc.)
- `Response` (request_id, request_type, status_code, status, settings, sensor_data, control)
- `SensorData`, `Settings`, `Control`, `Status`, `Streaming`, `Playback`
- All enums: `RequestType`, `SensorType`, `StreamIdentifier`, `MountingMode`, etc.

**Import pattern** (`aionanit/proto/__init__.py`):
```python
from .nanit import (
    Message, Request, Response, RequestType,
    SensorData, SensorType, Settings, Control, Status,
    Streaming, StreamIdentifier, Playback, GetSensorData,
    GetControl, GetStatus, GetLogs, Stream, MountingMode,
)
```

**Encode/decode** (betterproto native):
```python
# Serialize: bytes(message_instance)
# Deserialize: Message().parse(raw_bytes)
```

**CI check:** Generate, then `git diff --exit-code aionanit/proto/` to ensure committed code is up to date.

**`pyproject.toml` dependencies:**
```toml
[project]
name = "aionanit"
version = "1.0.0"
dependencies = [
    "aiohttp>=3.9.0",
    "betterproto>=2.0.0b7",
]

[project.optional-dependencies]
dev = [
    "grpcio-tools",          # For proto generation
    "pytest",
    "pytest-asyncio",
    "aioresponses",          # HTTP mocking
]
```

### 5.4 `auth.py` — TokenManager

```python
class TokenManager:
    """Manages Nanit access/refresh tokens with proactive refresh.

    Accepts an external NanitRestClient for actual refresh calls.
    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        rest_client: NanitRestClient,
        access_token: str,
        refresh_token: str,
        token_lifetime: float = 3600.0,    # 60 minutes default
        refresh_margin: float = 300.0,      # Refresh 5 min before expiry
    ) -> None: ...

    @property
    def access_token(self) -> str: ...

    @property
    def refresh_token(self) -> str: ...

    @property
    def is_expired(self) -> bool: ...

    async def async_get_access_token(self, min_ttl: float = 60.0) -> str:
        """Return a valid access token, refreshing if TTL is insufficient.

        Uses asyncio.Lock to prevent concurrent refresh storms.
        Raises NanitAuthError if refresh fails.
        """

    async def async_force_refresh(self) -> None:
        """Force an immediate token refresh. Used after auth failures."""

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update stored tokens (e.g., after initial login)."""

    def on_tokens_refreshed(self, callback: Callable[[str, str], None]) -> Callable[[], None]:
        """Register callback for token updates. Returns unsubscribe function.
        
        The HA integration uses this to persist new tokens in ConfigEntry.data.
        """
```

**Internal behavior:**
- Stores `_access_token`, `_refresh_token`, `_expires_at` (monotonic time).
- `async_get_access_token()` acquires `_refresh_lock`, checks `_expires_at - now < min_ttl`, calls `rest_client.refresh_token()` if needed.
- On successful refresh, calls all registered callbacks with `(new_access_token, new_refresh_token)`.
- On refresh failure (404 = refresh token expired), raises `NanitAuthError` — HA catches this as `ConfigEntryAuthFailed`.

### 5.5 `rest.py` — NanitRestClient

```python
class NanitRestClient:
    """Async client for Nanit cloud REST API.
    
    Does NOT own the aiohttp.ClientSession — caller provides it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = "https://api.nanit.com",
    ) -> None: ...

    async def async_login(self, email: str, password: str) -> dict[str, Any]:
        """POST /login — returns {access_token, refresh_token} or raises.
        
        Raises NanitMfaRequiredError if MFA token is returned.
        Raises NanitAuthError on invalid credentials.
        """

    async def async_verify_mfa(
        self, email: str, password: str, mfa_token: str, mfa_code: str,
    ) -> dict[str, Any]:
        """POST /login with MFA — returns {access_token, refresh_token}.
        
        Raises NanitAuthError on invalid MFA code.
        """

    async def async_refresh_token(
        self, access_token: str, refresh_token: str,
    ) -> dict[str, Any]:
        """POST /tokens/refresh — returns {access_token, refresh_token}.
        
        Raises NanitAuthError if refresh token is expired (404).
        """

    async def async_get_babies(self, access_token: str) -> list[Baby]:
        """GET /babies — returns list of Baby objects."""

    async def async_get_events(
        self, access_token: str, baby_uid: str, limit: int = 10,
    ) -> list[CloudEvent]:
        """GET /babies/{baby_uid}/messages?limit={limit}"""

    async def async_get_snapshot(
        self, access_token: str, baby_uid: str,
    ) -> bytes | None:
        """GET snapshot from cloud (exact endpoint TBD — see Open Questions)."""
```

**Auth header notes:**
- Regular REST endpoints: `Authorization: {token}` (no "Bearer" prefix)
- `/focus/*` endpoints: `Authorization: Bearer {token}`
- The client handles this distinction internally based on the URL path.

### 5.6 `ws/transport.py` — WsTransport

```python
class WsTransport:
    """Manages a single WebSocket connection with reconnect and keepalive.
    
    Handles:
    - Connection (cloud or local URL)
    - Binary receive loop dispatching to a callback
    - Keepalive pings (protobuf KEEPALIVE message every 25s)
    - Reconnect with exponential backoff + jitter
    - Graceful close with task cancellation
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        on_message: Callable[[bytes], None],
        on_connection_change: Callable[[ConnectionState, TransportKind, str | None], None],
    ) -> None: ...

    @property
    def connected(self) -> bool: ...

    @property
    def transport_kind(self) -> TransportKind: ...

    async def async_connect_cloud(
        self, camera_uid: str, access_token: str,
    ) -> None:
        """Connect to wss://api.nanit.com/focus/cameras/{camera_uid}/user_connect
        
        Headers: Authorization: Bearer {access_token}
        """

    async def async_connect_local(
        self, camera_ip: str, uc_token: str, ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Connect to wss://{camera_ip}:442
        
        Headers: Authorization: token {uc_token}
        Uses self-signed TLS — ssl_context should disable verification
        or use pinned fingerprint.
        """

    async def async_send(self, data: bytes) -> None:
        """Send binary data over the WebSocket. Raises if not connected."""

    async def async_close(self) -> None:
        """Close connection and cancel all background tasks."""

    # Internal methods:
    async def _recv_loop(self) -> None:
        """Read binary frames, dispatch to on_message callback.
        
        On WSMsgType.BINARY → on_message(msg.data)
        On WSMsgType.CLOSE/CLOSED/CLOSING → trigger reconnect
        On WSMsgType.ERROR → trigger reconnect
        On timeout (no message for 90s) → trigger reconnect
        """

    async def _keepalive_loop(self) -> None:
        """Send protobuf KEEPALIVE message every 25 seconds.
        
        Uses Message(type=MessageType.KEEPALIVE) serialized via betterproto.
        Also uses aiohttp heartbeat=60 as a TCP-level backup.
        """

    async def _reconnect_loop(self) -> None:
        """Exponential backoff reconnect.
        
        Base: 1.85s, factor: 1.618 (golden ratio), cap: 60s.
        Jitter: random 0-1s on first retry.
        Uses asyncio.Lock to prevent concurrent reconnect races.
        Resets backoff on successful connection.
        On reconnect: calls on_connection_change(RECONNECTING, ...).
        On success: calls on_connection_change(CONNECTED, ...).
        On failure (after attempt): increments backoff, retries.
        """
```

**Connection lifecycle:**
1. `async_connect_cloud()` or `async_connect_local()` → `aiohttp.ClientSession.ws_connect(url, headers=..., heartbeat=60)`
2. Spawns `_recv_loop` and `_keepalive_loop` as `asyncio.Task`s
3. On disconnect: cancels tasks, enters `_reconnect_loop`
4. On `async_close()`: cancels everything, closes WS gracefully

### 5.7 `ws/protocol.py` — Protobuf Helpers

```python
from aionanit.proto import (
    Message, MessageType, Request, Response, RequestType,
    SensorData, Settings, Control, Status, Streaming,
    GetSensorData, GetControl, GetStatus,
)

def encode_message(msg: Message) -> bytes:
    """Serialize a protobuf Message to bytes.
    
    Uses betterproto: bytes(msg)
    """

def decode_message(data: bytes) -> Message:
    """Deserialize bytes to a protobuf Message.
    
    Uses betterproto: Message().parse(data)
    Raises NanitProtocolError on decode failure.
    """

def build_keepalive() -> bytes:
    """Build a KEEPALIVE message."""
    return encode_message(Message(type=MessageType.KEEPALIVE))

def build_request(
    request_id: int,
    request_type: RequestType,
    *,
    streaming: Streaming | None = None,
    settings: Settings | None = None,
    control: Control | None = None,
    get_status: GetStatus | None = None,
    get_sensor_data: GetSensorData | None = None,
    get_control: GetControl | None = None,
) -> bytes:
    """Build a REQUEST message with the given payload."""

def extract_response(msg: Message) -> Response | None:
    """Extract Response from a RESPONSE message, or None if not a response."""

def extract_request(msg: Message) -> Request | None:
    """Extract Request from a REQUEST message (for push events from camera)."""
```

### 5.8 `ws/pending.py` — PendingRequests

```python
class PendingRequests:
    """Tracks outgoing requests and correlates them with responses.
    
    Each request gets a unique ID and an asyncio.Future.
    When a response arrives with a matching request_id, the future is resolved.
    Timeouts are enforced via asyncio.wait_for at the call site.
    """

    def __init__(self) -> None: ...
        # self._pending: dict[int, asyncio.Future[Response]] = {}
        # self._counter: int = 0

    def next_id(self) -> int:
        """Return next unique request ID (monotonically increasing)."""

    def track(self, request_id: int) -> asyncio.Future[Response]:
        """Register a pending request. Returns a Future to await."""

    def resolve(self, request_id: int, response: Response) -> bool:
        """Resolve a pending request with its response.
        
        Returns True if a matching request was found, False otherwise.
        """

    def cancel_all(self, error: Exception | None = None) -> None:
        """Cancel/fail all pending futures. Called on disconnect/close.
        
        If error is provided, futures are set_exception(error).
        Otherwise, futures are cancelled.
        """

    @property
    def pending_count(self) -> int: ...
```

### 5.9 `camera.py` — NanitCamera

```python
class NanitCamera:
    """High-level API for a single Nanit camera.
    
    Manages WebSocket connection, state aggregation, and command execution.
    One instance per camera/baby.
    """

    def __init__(
        self,
        uid: str,
        baby_uid: str,
        token_manager: TokenManager,
        rest_client: NanitRestClient,
        session: aiohttp.ClientSession,
        *,
        prefer_local: bool = True,
        local_ip: str | None = None,
    ) -> None: ...

    @property
    def uid(self) -> str: ...

    @property
    def baby_uid(self) -> str: ...

    @property
    def state(self) -> CameraState: ...

    @property
    def connected(self) -> bool: ...

    # --- Lifecycle ---

    async def async_start(self) -> None:
        """Start the camera connection.
        
        1. If prefer_local and local_ip is set: try local first
        2. If local fails or not configured: connect via cloud
        3. After connect: request initial state (GET_STATUS, GET_SETTINGS,
           GET_SENSOR_DATA, GET_CONTROL)
        4. Start background task for periodic local-availability check
           (if currently on cloud and local_ip is configured)
        """

    async def async_stop(self) -> None:
        """Stop the camera connection. Cancel all tasks, close transport."""

    # --- Subscriptions ---

    def subscribe(self, callback: Callable[[CameraEvent], None]) -> Callable[[], None]:
        """Register a callback for state changes.
        
        Returns an unsubscribe function.
        Called on every state mutation (sensor, settings, control, status, connection).
        """

    # --- Commands (all send protobuf REQUEST and await RESPONSE) ---

    async def async_get_status(self) -> StatusState:
        """GET_STATUS request."""

    async def async_get_settings(self) -> SettingsState:
        """GET_SETTINGS request."""

    async def async_set_settings(
        self,
        *,
        night_vision: bool | None = None,
        volume: int | None = None,
        sleep_mode: bool | None = None,
        status_light_on: bool | None = None,
        mic_mute_on: bool | None = None,
    ) -> SettingsState:
        """PUT_SETTINGS request. Only provided fields are sent."""

    async def async_get_control(self) -> ControlState:
        """GET_CONTROL request."""

    async def async_set_control(
        self,
        *,
        night_light: NightLightState | None = None,
        night_light_timeout: int | None = None,
    ) -> ControlState:
        """PUT_CONTROL request."""

    async def async_get_sensor_data(self) -> SensorState:
        """GET_SENSOR_DATA request (all sensors)."""

    # --- Streaming ---

    async def async_get_stream_rtmps_url(self) -> str:
        """Build RTMPS URL with fresh token.
        
        Returns: rtmps://media-secured.nanit.com/nanit/{baby_uid}.{access_token}
        Token is fetched via TokenManager to ensure freshness.
        """

    async def async_start_streaming(self) -> None:
        """Send PUT_STREAMING with status=STARTED to camera.
        
        Required before the camera pushes RTMP to the relay server.
        """

    async def async_stop_streaming(self) -> None:
        """Send PUT_STREAMING with status=STOPPED to camera."""

    # --- Snapshot ---

    async def async_get_snapshot(self) -> bytes | None:
        """Get a JPEG snapshot.
        
        Strategy: try cloud REST endpoint first (simpler, no WS dependency).
        Fallback: could potentially be done via WS if cloud endpoint doesn't exist.
        """

    # --- Internal ---

    def _on_ws_message(self, data: bytes) -> None:
        """Handle incoming WebSocket binary frame.
        
        1. Decode protobuf Message
        2. If KEEPALIVE: ignore (transport handles pong)
        3. If RESPONSE: resolve pending request via PendingRequests
        4. If REQUEST (push from camera): update CameraStateAggregator
           - PUT_SENSOR_DATA → update sensors
           - PUT_STATUS → update status
           - PUT_SETTINGS → update settings
           - PUT_CONTROL → update control
        5. Notify subscribers via CameraEvent
        """

    def _on_connection_change(
        self, state: ConnectionState, transport: TransportKind, error: str | None,
    ) -> None:
        """Handle connection state transitions.
        
        Updates CameraState.connection and notifies subscribers.
        On disconnect: cancel pending requests.
        """

    async def _send_request(
        self, request_type: RequestType, timeout: float = 10.0, **kwargs,
    ) -> Response:
        """Send a protobuf request and await the correlated response.
        
        1. Get next request ID from PendingRequests
        2. Build request via protocol.build_request()
        3. Track in PendingRequests
        4. Send via WsTransport.async_send()
        5. await asyncio.wait_for(future, timeout=timeout)
        6. On timeout: raise NanitRequestTimeout
        """
```

### 5.10 `client.py` — NanitClient

```python
class NanitClient:
    """Top-level entrypoint for the aionanit library.
    
    Creates and manages NanitCamera instances.
    Owns the TokenManager and NanitRestClient.
    Does NOT own the aiohttp.ClientSession.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str | None = None,
        password: str | None = None,
    ) -> None: ...

    @property
    def token_manager(self) -> TokenManager | None: ...

    @property
    def rest_client(self) -> NanitRestClient: ...

    # --- Auth ---

    async def async_login(self, email: str, password: str) -> dict[str, Any]:
        """Login and initialize TokenManager.
        
        Returns {access_token, refresh_token} on success.
        Raises NanitMfaRequiredError if MFA needed.
        """

    async def async_verify_mfa(
        self, email: str, password: str, mfa_token: str, mfa_code: str,
    ) -> dict[str, Any]:
        """Complete MFA and initialize TokenManager."""

    def restore_tokens(self, access_token: str, refresh_token: str) -> None:
        """Restore tokens from persisted storage (ConfigEntry.data).
        
        Creates TokenManager without login. Used on HA startup.
        """

    # --- Babies ---

    async def async_get_babies(self) -> list[Baby]:
        """Fetch baby/camera list from cloud API."""

    # --- Camera ---

    def camera(
        self,
        uid: str,
        baby_uid: str,
        *,
        prefer_local: bool = True,
        local_ip: str | None = None,
    ) -> NanitCamera:
        """Get or create a NanitCamera instance.
        
        Cameras are cached by uid. Calling again with same uid returns
        the existing instance (does not create a duplicate connection).
        """

    # --- Lifecycle ---

    async def async_close(self) -> None:
        """Stop all cameras and clean up resources."""
```

### 5.11 `__init__.py` — Public API

```python
from .client import NanitClient
from .camera import NanitCamera
from .models import (
    Baby, CameraEvent, CameraEventKind, CameraState, CloudEvent,
    ConnectionInfo, ConnectionState, ControlState, NightLightState,
    SensorReading, SensorState, SensorType, SettingsState, StatusState,
    TransportKind,
)
from .exceptions import (
    NanitAuthError, NanitCameraUnavailable, NanitConnectionError,
    NanitError, NanitMfaRequiredError, NanitProtocolError,
    NanitRequestTimeout, NanitTransportError,
)

__all__ = [
    "NanitClient", "NanitCamera",
    # models
    "Baby", "CameraEvent", "CameraEventKind", "CameraState", ...
    # exceptions
    "NanitError", "NanitAuthError", ...
]
```

---

## 6. HA Integration Architecture

### 6.1 `hub.py` (NEW)

```python
@dataclass
class NanitRuntimeData:
    """Runtime data stored in ConfigEntry.runtime_data."""
    hub: NanitHub

type NanitConfigEntry = ConfigEntry[NanitRuntimeData]

class NanitHub:
    """Central orchestrator for the Nanit integration.
    
    Owns:
    - NanitClient (aionanit)
    - Per-camera NanitCameraCoordinator instances
    - Token persistence callback
    - Cloud event polling coordinator
    """

    def __init__(self, hass: HomeAssistant, entry: NanitConfigEntry) -> None: ...

    @property
    def client(self) -> NanitClient: ...

    @property
    def camera_coordinators(self) -> dict[str, NanitCameraCoordinator]: ...

    @property
    def cloud_coordinator(self) -> NanitCloudCoordinator | None: ...

    async def async_start(self) -> None:
        """Initialize the hub.
        
        1. Create aiohttp session via async_get_clientsession(hass)
        2. Create NanitClient with session
        3. Restore tokens from entry.data (access_token, refresh_token)
        4. Validate tokens by calling async_get_babies()
           - On NanitAuthError → raise ConfigEntryAuthFailed
           - On NanitConnectionError → raise ConfigEntryNotReady
        5. Register token refresh callback to persist to ConfigEntry
        6. For each baby/camera:
           a. Create NanitCamera via client.camera(uid, baby_uid, ...)
           b. Create NanitCameraCoordinator
           c. Subscribe coordinator to camera events
           d. Start camera connection
           e. Wait for first state (with timeout)
        7. Create NanitCloudCoordinator for cloud event polling
        """

    async def async_stop(self) -> None:
        """Tear down the hub.
        
        1. Unsubscribe all coordinator callbacks
        2. Stop all cameras
        3. Close NanitClient
        """

    def _on_tokens_refreshed(self, access_token: str, refresh_token: str) -> None:
        """Persist refreshed tokens to ConfigEntry.data.
        
        Uses hass.config_entries.async_update_entry() to store
        new tokens without triggering a reload.
        """
```

### 6.2 `coordinator.py` (REWRITE)

```python
class NanitCameraCoordinator(DataUpdateCoordinator[CameraState]):
    """Push-driven coordinator for a single camera.
    
    Primary updates come from NanitCamera.subscribe() callbacks
    via async_set_updated_data(). A slow periodic resync (every 10 min)
    acts as a safety net to recover from missed push events.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        camera: NanitCamera,
        camera_uid: str,
    ) -> None:
        super().__init__(
            hass, _LOGGER,
            name=f"{DOMAIN}_{camera_uid}",
            update_interval=timedelta(minutes=10),  # Slow resync only
        )
        self._camera = camera
        self._unsubscribe: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Subscribe to camera events."""
        self._unsubscribe = self._camera.subscribe(self._on_camera_event)

    async def async_stop(self) -> None:
        """Unsubscribe from camera events."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_camera_event(self, event: CameraEvent) -> None:
        """Called by aionanit on every state change.
        
        Runs in the event loop. Pushes new state to all entities.
        """
        self.async_set_updated_data(event.state)

    async def _async_update_data(self) -> CameraState:
        """Periodic resync (every 10 min). Requests full state from camera.
        
        This is the fallback path. Normal updates come via push.
        """
        try:
            await self._camera.async_get_status()
            await self._camera.async_get_settings()
            await self._camera.async_get_sensor_data()
            await self._camera.async_get_control()
            return self._camera.state
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except (NanitConnectionError, NanitRequestTimeout) as err:
            raise UpdateFailed(err) from err


class NanitCloudCoordinator(DataUpdateCoordinator[list[CloudEvent]]):
    """Polls Nanit cloud API for motion/sound events.
    
    Polls every 30 seconds. Used for cloud-based binary sensors.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: NanitClient,
        baby_uid: str,
    ) -> None:
        super().__init__(
            hass, _LOGGER,
            name=f"{DOMAIN}_cloud_{baby_uid}",
            update_interval=timedelta(seconds=30),
        )
        self._client = client
        self._baby_uid = baby_uid

    async def _async_update_data(self) -> list[CloudEvent]:
        try:
            token = await self._client.token_manager.async_get_access_token()
            return await self._client.rest_client.async_get_events(
                token, self._baby_uid, limit=10,
            )
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except NanitConnectionError as err:
            raise UpdateFailed(err) from err
```

### 6.3 `entity.py` (MINOR CHANGE)

```python
class NanitEntity(CoordinatorEntity[NanitCameraCoordinator]):
    """Base entity for Nanit — uses the push-driven camera coordinator."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.data[CONF_CAMERA_UID])},
            name=self.coordinator.config_entry.data[CONF_BABY_NAME],
            manufacturer="Nanit",
            # NEW: add firmware/hardware version from camera state
            sw_version=self._camera_state.status.firmware_version,
            hw_version=self._camera_state.status.hardware_version,
        )

    @property
    def available(self) -> bool:
        """Entity is available when the camera is connected."""
        if not self.coordinator.last_update_success or self.coordinator.data is None:
            return False
        return self.coordinator.data.connection.state == ConnectionState.CONNECTED

    @property
    def _camera_state(self) -> CameraState:
        """Shortcut to coordinator data."""
        return self.coordinator.data
```

### 6.4 `__init__.py` (REWRITE)

Major changes:
- Remove ALL add-on logic (`_async_resolve_addon_slug`, `_async_resolve_addon_host`, `_async_set_addon_option`, `NanitAddonClient` usage)
- Remove `NanitApiClient` (HTTP-to-nanitd client) — replaced by `aionanit.NanitClient`
- `NanitData` replaced by `NanitRuntimeData` (contains `NanitHub`)
- `async_setup_entry` creates `NanitHub`, calls `hub.async_start()`
- `async_unload_entry` calls `hub.async_stop()`

```python
async def async_setup_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    hub = NanitHub(hass, entry)
    try:
        await hub.async_start()
    except NanitAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except NanitConnectionError as err:
        raise ConfigEntryNotReady(err) from err

    entry.runtime_data = NanitRuntimeData(hub=hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: NanitConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.hub.async_stop()
    return unload_ok
```

### 6.5 `config_flow.py` (MODIFY)

Changes:
- Remove add-on discovery step (no more `use_addon`, `ADDON_SLUG`, `ADDON_HOST_MARKER`)
- Remove transport selection step (transport is always "auto" — local-first with cloud fallback)
- Add optional "Camera IP" field for local connection (user can provide or leave blank for cloud-only)
- Keep: email/password step, MFA step, baby selection step, reauth flow
- Add: options flow for changing camera IP after setup

**New flow:**
1. `async_step_user` → email + password
2. `async_step_mfa` → MFA code (if needed)
3. `async_step_baby` → select baby/camera from list
4. `async_step_local` → optional camera IP (can skip for cloud-only)
5. Create entry with: email, access_token, refresh_token, baby_uid, camera_uid, baby_name, camera_ip (optional)

### 6.6 `camera.py` (REWRITE)

```python
class NanitCamera(NanitEntity, Camera):
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.ON_OFF | CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: NanitCameraCoordinator,
        hub: NanitHub,
    ) -> None:
        NanitEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._hub = hub
        self._camera = hub.client.camera(
            coordinator.config_entry.data[CONF_CAMERA_UID],
            coordinator.config_entry.data[CONF_BABY_UID],
        )
        self._attr_unique_id = (
            f"{coordinator.config_entry.data.get(CONF_CAMERA_UID, coordinator.config_entry.entry_id)}"
            "_camera"
        )

    async def stream_source(self) -> str | None:
        """Return RTMPS URL with fresh token.
        
        1. Check if camera is on (not in sleep mode)
        2. Get fresh RTMPS URL from aionanit (includes current access token)
        3. Return URL — HA Stream handles HLS conversion
        """
        if not self.is_on:
            return None
        try:
            return await self._camera.async_get_stream_rtmps_url()
        except Exception:
            _LOGGER.debug("Failed to get stream URL", exc_info=True)
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None,
    ) -> bytes | None:
        if not self.is_on:
            return None
        try:
            return await self._camera.async_get_snapshot()
        except Exception:
            _LOGGER.debug("Failed to get snapshot", exc_info=True)
            return None

    @property
    def is_on(self) -> bool:
        if self._camera_state is None:
            return True
        return not (self._camera_state.settings.sleep_mode or False)

    @property
    def is_streaming(self) -> bool:
        # With RTMPS, streaming is always available when camera is on
        return self.is_on

    async def async_turn_on(self) -> None:
        await self._camera.async_set_settings(sleep_mode=False)

    async def async_turn_off(self) -> None:
        await self._camera.async_set_settings(sleep_mode=True)
```

### 6.7–6.10 Entity Platform Changes

**sensor.py:** Change `value_fn` lambdas to read from `CameraState.sensors` instead of `data["sensors"]`:
```python
# Before: lambda data: data.get("temperature", {}).get("value")
# After:  lambda state: state.sensors.temperature
```

**binary_sensor.py:** Change `value_fn` to read from `CameraState`:
```python
# motion: lambda state: state.sensors.motion_alert
# sound: lambda state: state.sensors.sound_alert
# night_mode: lambda state: state.sensors.night
# connectivity: lambda state: state.status.connected_to_server
# cloud_motion/cloud_sound: unchanged pattern, but reads from list[CloudEvent]
```

**switch.py:** Replace `NanitApiClient` HTTP calls with `NanitCamera` commands:
```python
# Before: turn_on_fn=lambda client: client.set_night_light(True)
# After:  turn_on_fn=lambda camera: camera.async_set_control(night_light=NightLightState.ON)
```

**number.py:** Same pattern:
```python
# Before: await self._client.set_volume(int(value))
# After:  await self._camera.async_set_settings(volume=int(value))
```

### 6.11 `diagnostics.py` (MODIFY)

- Remove nanitd-specific data (HLS status, addon status)
- Add: camera connection info (transport kind, state, last_seen)
- Keep: full redaction of tokens, UIDs, email, IP addresses
- Add: aionanit library version in diagnostics output

### 6.12 `const.py` (MODIFY)

Remove:
- `ADDON_SLUG`, `CONF_USE_ADDON`, `ADDON_HOST_MARKER`
- `DEFAULT_HOST` (no more HTTP to nanitd)
- `TRANSPORT_LOCAL`, `TRANSPORT_LOCAL_CLOUD`, `CONF_TRANSPORT`

Add:
- `CONF_CAMERA_IP` (already exists, keep it)
- `DEFAULT_RESYNC_INTERVAL = 600` (10 min)
- `DEFAULT_CLOUD_POLL_INTERVAL = 30`

### 6.13 `manifest.json` (UPDATE)

```json
{
  "domain": "nanit",
  "name": "Nanit",
  "version": "1.0.0",
  "documentation": "https://github.com/wealthystudent/ha-nanit",
  "issue_tracker": "https://github.com/wealthystudent/ha-nanit/issues",
  "codeowners": ["@wealthystudent"],
  "config_flow": true,
  "iot_class": "local_push",
  "integration_type": "hub",
  "requirements": ["aionanit==1.0.0"],
  "loggers": ["custom_components.nanit", "aionanit"]
}
```

Key changes: `iot_class` → `"local_push"` (was `"cloud_polling"`), `requirements` → `["aionanit==1.0.0"]`.

---

## 7. Entity Migration Map

Unique IDs **MUST NOT change**. This ensures dashboards, automations, and history are preserved.

| Platform | Key | Current Unique ID | v1.0 Unique ID | Data Source (before) | Data Source (after) | Notes |
|---|---|---|---|---|---|---|
| sensor | temperature | `{entry.unique_id}_temperature` | `{entry.unique_id}_temperature` | nanitd HTTP → `sensors.temperature.value` | WS push → `CameraState.sensors.temperature` | No ID change |
| sensor | humidity | `{entry.unique_id}_humidity` | `{entry.unique_id}_humidity` | nanitd HTTP → `sensors.humidity.value` | WS push → `CameraState.sensors.humidity` | No ID change |
| sensor | light | `{entry.unique_id}_light` | `{entry.unique_id}_light` | nanitd HTTP → `sensors.light.value` | WS push → `CameraState.sensors.light` | No ID change |
| binary_sensor | motion | `{camera_uid}_motion` | `{camera_uid}_motion` | nanitd HTTP → `sensors.motion.is_alert` | WS push → `CameraState.sensors.motion_alert` | No ID change |
| binary_sensor | sound | `{camera_uid}_sound` | `{camera_uid}_sound` | nanitd HTTP → `sensors.sound.is_alert` | WS push → `CameraState.sensors.sound_alert` | No ID change |
| binary_sensor | night_mode | `{camera_uid}_night_mode` | `{camera_uid}_night_mode` | nanitd HTTP → `sensors.night.value > 0` | WS push → `CameraState.sensors.night` | No ID change |
| binary_sensor | connectivity | `{camera_uid}_connectivity` | `{camera_uid}_connectivity` | nanitd HTTP → `status.connected` | WS push → `CameraState.status.connected_to_server` | No ID change |
| binary_sensor | cloud_motion | `{camera_uid}_cloud_motion` | `{camera_uid}_cloud_motion` | nanitd HTTP → cloud events | REST poll → `list[CloudEvent]` | No ID change |
| binary_sensor | cloud_sound | `{camera_uid}_cloud_sound` | `{camera_uid}_cloud_sound` | nanitd HTTP → cloud events | REST poll → `list[CloudEvent]` | No ID change |
| switch | night_light | `{camera_uid}_night_light` | `{camera_uid}_night_light` | nanitd HTTP → `control.night_light` | WS push → `CameraState.control.night_light` | No ID change |
| switch | camera_power | `{camera_uid}_camera_power` | `{camera_uid}_camera_power` | nanitd HTTP → `!settings.sleep_mode` | WS push → `!CameraState.settings.sleep_mode` | No ID change |
| switch | status_led | `{camera_uid}_status_led` | `{camera_uid}_status_led` | nanitd HTTP → `settings.status_light_on` | WS push → `CameraState.settings.status_light_on` | No ID change |
| switch | mic_mute | `{camera_uid}_mic_mute` | `{camera_uid}_mic_mute` | nanitd HTTP → `settings.mic_mute_on` | WS push → `CameraState.settings.mic_mute_on` | No ID change |
| number | volume | `{camera_uid}_volume` | `{camera_uid}_volume` | nanitd HTTP → `settings.volume` | WS push → `CameraState.settings.volume` | No ID change |
| camera | camera | `{camera_uid}_camera` | `{camera_uid}_camera` | nanitd HLS proxy | RTMPS via `stream_source()` | Stream method changes but unique ID preserved |

---

## 8. Connection Lifecycle

### 8.1 Startup

```
async_setup_entry called by HA
  │
  ├── NanitHub.__init__(hass, entry)
  ├── hub.async_start()
  │     │
  │     ├── session = async_get_clientsession(hass)
  │     ├── client = NanitClient(session)
  │     ├── client.restore_tokens(entry.data[access_token], entry.data[refresh_token])
  │     │
  │     ├── babies = await client.async_get_babies()
  │     │   └── On NanitAuthError → raise ConfigEntryAuthFailed
  │     │   └── On NanitConnectionError → raise ConfigEntryNotReady
  │     │
  │     ├── Register token refresh callback → persists to entry.data
  │     │
  │     ├── For each baby/camera:
  │     │   ├── camera = client.camera(uid, baby_uid, local_ip=entry.data.get(camera_ip))
  │     │   ├── coordinator = NanitCameraCoordinator(hass, camera, uid)
  │     │   ├── await coordinator.async_start()  # subscribes to camera events
  │     │   ├── await camera.async_start()        # connects WS (local → cloud fallback)
  │     │   └── await coordinator.async_config_entry_first_refresh()  # initial state
  │     │
  │     └── cloud_coordinator = NanitCloudCoordinator(hass, client, baby_uid)
  │         └── await cloud_coordinator.async_config_entry_first_refresh()
  │
  ├── entry.runtime_data = NanitRuntimeData(hub=hub)
  └── forward_entry_setups(entry, PLATFORMS)
```

### 8.2 Steady State

```
Camera pushes binary WebSocket frame
  │
  ├── WsTransport._recv_loop receives WSMsgType.BINARY
  ├── Calls NanitCamera._on_ws_message(data)
  │     │
  │     ├── Decode: Message().parse(data)
  │     ├── If KEEPALIVE → ignore
  │     ├── If RESPONSE → PendingRequests.resolve(request_id, response)
  │     ├── If REQUEST (push from camera):
  │     │   ├── PUT_SENSOR_DATA → update SensorState
  │     │   ├── PUT_STATUS → update StatusState
  │     │   ├── PUT_SETTINGS → update SettingsState
  │     │   └── PUT_CONTROL → update ControlState
  │     │
  │     └── Notify subscribers: callback(CameraEvent(kind, state))
  │
  ├── NanitCameraCoordinator._on_camera_event(event)
  │     └── self.async_set_updated_data(event.state)
  │
  └── All entities receive _handle_coordinator_update()
        └── self.async_write_ha_state()
```

### 8.3 Token Refresh

```
TokenManager background check (triggered by async_get_access_token)
  │
  ├── If time_to_expiry < 5 minutes:
  │   ├── Acquire _refresh_lock
  │   ├── Call rest_client.async_refresh_token(access, refresh)
  │   ├── Update stored tokens + expiry
  │   ├── Call on_tokens_refreshed callbacks
  │   │   └── NanitHub._on_tokens_refreshed → updates entry.data
  │   └── Release lock
  │
  ├── For cloud WS: next reconnect/new connection uses fresh token automatically
  │   (token is fetched at connection time, not cached in the URL)
  │
  └── For local WS: uc_token may be independent of the REST token
      (verify during implementation — see Open Questions)
```

### 8.4 Camera Disconnect

```
WebSocket closes unexpectedly
  │
  ├── WsTransport._recv_loop detects CLOSE/CLOSED/ERROR
  ├── Calls on_connection_change(DISCONNECTED, transport, error)
  │   └── NanitCamera updates state → notifies subscribers
  │       └── Entities become unavailable (connection.state != CONNECTED)
  │
  ├── PendingRequests.cancel_all() → fails any in-flight requests
  │
  └── WsTransport._reconnect_loop starts:
      ├── Attempt 1: wait 0-1s (jitter), try connect
      ├── Attempt 2: wait 1.85s, try connect
      ├── Attempt 3: wait 3.0s, try connect
      ├── Attempt 4: wait 4.8s, try connect
      ├── ... (factor 1.618, cap 60s)
      │
      ├── On success:
      │   ├── on_connection_change(CONNECTED, transport, None)
      │   ├── Request full state refresh (GET_STATUS, GET_SETTINGS, etc.)
      │   └── Entities become available again
      │
      └── Continues indefinitely until async_close() is called
```

### 8.5 Shutdown

```
async_unload_entry called
  │
  ├── Unload platforms (entities removed)
  │
  ├── hub.async_stop()
  │   ├── For each camera coordinator:
  │   │   └── coordinator.async_stop() → unsubscribes from camera events
  │   │
  │   ├── client.async_close()
  │   │   ├── For each NanitCamera:
  │   │   │   └── camera.async_stop()
  │   │   │       ├── Cancel reconnect loop task
  │   │   │       ├── Cancel keepalive task
  │   │   │       ├── Cancel recv loop task
  │   │   │       ├── PendingRequests.cancel_all()
  │   │   │       └── Close WebSocket gracefully
  │   │   └── Clear camera cache
  │   │
  │   └── Unregister token refresh callback
  │
  └── Remove from hass.data
```

### 8.6 Reauth

```
Any REST call returns 401 + refresh also fails (404)
  │
  ├── NanitAuthError raised from TokenManager
  ├── Coordinator catches → raises ConfigEntryAuthFailed
  ├── HA marks config entry as needing reauth
  │
  └── User sees reauth notification in HA UI
      ├── async_step_reauth → email/password form
      ├── async_step_reauth_mfa → MFA (if needed)
      └── Tokens updated in entry.data → entry reloaded
```

### 8.7 Local/Cloud Fallback

```
NanitCamera.async_start() with prefer_local=True, local_ip="192.168.1.50"
  │
  ├── Try local: async_connect_local("192.168.1.50", uc_token)
  │   ├── Success → connected via LOCAL transport
  │   └── Failure (timeout, refused, TLS error):
  │       └── Try cloud: async_connect_cloud(camera_uid, access_token)
  │           ├── Success → connected via CLOUD transport
  │           └── Failure → raise NanitCameraUnavailable
  │
  ├── If connected via CLOUD and local_ip is configured:
  │   └── Start background "local probe" task:
  │       ├── Every 5 minutes: attempt local WS connection test
  │       ├── If local becomes reachable:
  │       │   ├── Close cloud WS
  │       │   ├── Connect via local WS
  │       │   └── Log: "Promoted to local connection"
  │       └── Continue probing until stopped
  │
  └── If already on LOCAL and connection drops:
      ├── First: retry local (exponential backoff)
      ├── After 3 local failures: fallback to cloud
      └── Resume local probing (as above)
```

---

## 9. Streaming and Snapshot

### 9.1 Stream Source

When HA requests a stream (user opens camera card, recording starts), it calls `stream_source()`:

```python
async def stream_source(self) -> str | None:
    token = await self._camera._token_manager.async_get_access_token()
    baby_uid = self._camera.baby_uid
    return f"rtmps://media-secured.nanit.com/nanit/{baby_uid}.{token}"
```

The camera must be told to start pushing RTMP to the relay. This requires a `PUT_STREAMING` request:
1. `stream_source()` calls `camera.async_start_streaming()` first (if not already streaming)
2. Camera sends `PUT_STREAMING{id=MOBILE, status=STARTED, rtmp_url=...}` to camera
3. Camera begins pushing RTMP to `rtmps://media-secured.nanit.com/...`
4. HA Stream component connects to the same RTMPS URL and transcodes to HLS

**Important:** The Go daemon currently handles this orchestration. In v1.0, the `NanitCamera` must send the PUT_STREAMING command before returning the URL. The exact `rtmp_url` field in the Streaming proto message needs to be verified — it may be the URL the *camera* should push to, not the URL the *client* reads from.

### 9.2 Token Expiry Mid-Stream

- RTMPS URL contains the token: `rtmps://.../{baby_uid}.{token}`
- When the token expires, the relay server rejects new connections
- HA's Stream component handles stream failure gracefully — it will retry
- On retry, `stream_source()` is called again, which fetches a fresh token
- Expected behavior: stream drops briefly, auto-reconnects within seconds
- No custom handling needed on our side

### 9.3 Snapshot

**Strategy (in order of preference):**
1. **Cloud REST:** `GET /babies/{baby_uid}/snapshot` or similar endpoint (needs verification)
2. **Local thumbnail:** Some Nanit cameras expose a thumbnail endpoint (needs verification)
3. **FFmpeg capture:** As a last resort, use `haffmpeg` to capture a single frame from RTMPS

The current Go daemon uses ffmpeg for snapshots. In v1.0, we prefer the cloud REST endpoint to avoid subprocess overhead.

### 9.4 Camera On/Off (Sleep Mode)

- "Camera off" = `sleep_mode=True` in Settings
- `async_turn_off()` sends `PUT_SETTINGS{sleep_mode=True}` via WebSocket
- `async_turn_on()` sends `PUT_SETTINGS{sleep_mode=False}` via WebSocket
- When in sleep mode, `stream_source()` returns `None` (no stream available)
- The current v0.x code stops HLS before sleep — this is no longer needed since we don't manage ffmpeg

---

## 10. Implementation Phases

### Phase A: Library Foundation

**Goal:** aionanit package skeleton with working REST auth, token management, models, and protobuf generation.

**Deliverables:**
- `packages/aionanit/pyproject.toml` — package config
- `packages/aionanit/proto/nanit.proto` — copied from nanitd
- `packages/aionanit/scripts/generate_proto.py` — proto generation script
- `packages/aionanit/aionanit/proto/nanit.py` — generated betterproto code
- `packages/aionanit/aionanit/proto/__init__.py` — re-exports
- `packages/aionanit/aionanit/exceptions.py` — full error hierarchy
- `packages/aionanit/aionanit/models.py` — all dataclasses and enums
- `packages/aionanit/aionanit/auth.py` — TokenManager
- `packages/aionanit/aionanit/rest.py` — NanitRestClient
- `packages/aionanit/aionanit/__init__.py` — public exports
- `packages/aionanit/tests/test_auth.py` — token refresh tests
- `packages/aionanit/tests/test_rest.py` — login/MFA tests (mocked HTTP)

**Dependencies:** None (first phase).

**Verification:**
- [ ] `generate_proto.py` produces valid betterproto code from nanit.proto
- [ ] `NanitRestClient.async_login()` works against real Nanit API
- [ ] `NanitRestClient.async_verify_mfa()` works with real MFA code
- [ ] `NanitRestClient.async_refresh_token()` works with real tokens
- [ ] `NanitRestClient.async_get_babies()` returns correct baby list
- [ ] `TokenManager` proactively refreshes before expiry
- [ ] All unit tests pass

**Estimated effort:** 3-5 days

### Phase B: WebSocket + Protobuf Protocol

**Goal:** Bi-directional WebSocket communication with protobuf encoding, request/response correlation, keepalive, and reconnect.

**Deliverables:**
- `packages/aionanit/aionanit/ws/transport.py` — WsTransport
- `packages/aionanit/aionanit/ws/protocol.py` — encode/decode helpers
- `packages/aionanit/aionanit/ws/pending.py` — PendingRequests
- `packages/aionanit/aionanit/ws/__init__.py`
- `packages/aionanit/tests/test_protocol.py` — encode/decode golden tests
- `packages/aionanit/tests/test_pending.py` — correlation tests
- `packages/aionanit/tests/test_transport.py` — connection lifecycle tests (mocked WS)

**Dependencies:** Phase A (needs TokenManager, proto types, exceptions).

**Verification:**
- [ ] Connect to `wss://api.nanit.com/focus/cameras/{uid}/user_connect` successfully
- [ ] Receive and decode KEEPALIVE messages from camera
- [ ] Send GET_STATUS request and receive correlated RESPONSE
- [ ] Keepalive loop runs every 25 seconds without drift
- [ ] Reconnect with exponential backoff after deliberate disconnect
- [ ] PendingRequests cleans up on disconnect (no leaked futures)
- [ ] Request timeout fires correctly (10s default)
- [ ] All unit tests pass

**Estimated effort:** 4-6 days

### Phase C: Camera API + State/Events

**Goal:** High-level NanitCamera and NanitClient with full command set, state aggregation, and event subscription.

**Deliverables:**
- `packages/aionanit/aionanit/camera.py` — NanitCamera
- `packages/aionanit/aionanit/client.py` — NanitClient
- `packages/aionanit/tests/test_camera.py` — command tests
- `packages/aionanit/tests/test_client.py` — lifecycle tests

**Dependencies:** Phase B (needs WsTransport, protocol, PendingRequests).

**Verification:**
- [ ] `NanitCamera.async_start()` connects and receives initial state
- [ ] Push events (sensor data) update `CameraState` correctly
- [ ] `subscribe()` callbacks fire on every state change
- [ ] `async_set_settings(volume=50)` round-trips successfully
- [ ] `async_set_control(night_light=ON)` round-trips successfully
- [ ] `async_get_stream_rtmps_url()` returns valid URL with fresh token
- [ ] Camera stop/start lifecycle is clean (no leaked tasks)
- [ ] All unit tests pass

**Estimated effort:** 3-5 days

### Phase D: HA Integration Refactor

**Goal:** Rewrite the integration to consume aionanit instead of nanitd HTTP API. All entities working.

**Deliverables:**
- `custom_components/nanit/hub.py` — NEW file
- `custom_components/nanit/__init__.py` — REWRITE
- `custom_components/nanit/coordinator.py` — REWRITE
- `custom_components/nanit/entity.py` — MODIFY
- `custom_components/nanit/camera.py` — REWRITE
- `custom_components/nanit/sensor.py` — MODIFY
- `custom_components/nanit/binary_sensor.py` — MODIFY
- `custom_components/nanit/switch.py` — MODIFY
- `custom_components/nanit/number.py` — MODIFY
- `custom_components/nanit/config_flow.py` — MODIFY
- `custom_components/nanit/const.py` — MODIFY
- `custom_components/nanit/manifest.json` — UPDATE
- `custom_components/nanit/strings.json` — MODIFY
- `custom_components/nanit/api.py` — DELETE (replaced by aionanit)

**Dependencies:** Phase C (needs working NanitClient and NanitCamera).

**Verification:**
- [ ] Config flow: can log in with email/password + MFA
- [ ] Config flow: can select baby and optionally provide camera IP
- [ ] Integration loads successfully with real camera
- [ ] All sensors show correct values (temperature, humidity, light)
- [ ] All binary sensors work (motion, sound, night, connectivity, cloud_motion, cloud_sound)
- [ ] All switches toggle correctly (night light, power, status LED, mic mute)
- [ ] Volume number entity works
- [ ] Camera stream plays in HA dashboard
- [ ] Camera snapshot works
- [ ] Camera on/off (sleep mode) works
- [ ] Entity unique IDs match v0.x (verify with entity registry)
- [ ] Entities go unavailable on camera disconnect
- [ ] Entities recover on camera reconnect
- [ ] Reauth flow works when tokens expire
- [ ] `lsp_diagnostics` clean on all changed files

**Estimated effort:** 5-7 days

### Phase E: Hardening + Release

**Goal:** Production-ready release with local TLS handling, diagnostics, testing, documentation, and migration guidance.

**Deliverables:**
- Local TLS: SSL context for self-signed camera certs (verify_ssl=False with optional fingerprint pinning)
- `custom_components/nanit/diagnostics.py` — MODIFY for new data structure
- Test suite: unit tests for aionanit, integration tests for HA
- `README.md` — full rewrite for v1.0
- `CHANGELOG.md` — v1.0 entry
- `AGENTS.md` — update for new architecture
- `hacs.json` — verify compatibility
- Publish `aionanit` to PyPI (or configure for HACS direct install)
- Git tag v1.0.0

**Dependencies:** Phase D (everything must be working first).

**Verification:**
- [ ] Local camera connection works with self-signed TLS
- [ ] 2-connection limit is respected (only one WS from integration)
- [ ] Local→cloud fallback works when camera IP is unreachable
- [ ] Cloud→local promotion works when camera becomes locally reachable
- [ ] Diagnostics output is complete and properly redacted
- [ ] 24h stability test: no memory leaks, no orphaned tasks, reconnects cleanly
- [ ] Clean HA restart: integration loads, connects, serves entities
- [ ] HA shutdown: no warnings about uncancelled tasks
- [ ] README accurately documents setup, requirements, and migration from v0.x

**Estimated effort:** 4-6 days

### Total Estimated Effort: 19-29 days (~4-6 weeks)

---

## 11. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| RTMPS token expires mid-stream | High | Stream drops for a few seconds | HA Stream retries automatically. `stream_source()` returns fresh URL on retry. Document expected behavior. |
| betterproto proto2 `required` field handling | Medium | Decode failures on messages with missing required fields | Write golden decode tests with captured real frames from nanitd. If betterproto chokes on proto2 required, consider switching to `protobuf` (google) library. |
| Local TLS self-signed certificate rejection | High | Local connection fails | Create SSL context with `verify_ssl=False`. Offer optional fingerprint pinning via config options. Log clear warning about security implications. |
| 2-connection limit on local camera (port 442) | High | Third-party apps locked out, or integration gets 403 | Enforce exactly one WS per camera in NanitCamera. Never open multiple connections. Document that other apps may conflict. |
| Cloud WS rate limiting / 4xx | Medium | Reconnect storms | Exponential backoff with cap at 60s and jitter. Log rate limit responses. Don't retry faster than backoff allows. |
| Pending request map leaks on reconnect | Low | Memory growth over time | `PendingRequests.cancel_all()` called on every disconnect. `asyncio.wait_for` on every request ensures futures don't hang. Unit test verifies cleanup. |
| Multi-camera households | Medium | Resource usage, complexity | Each camera gets its own WsTransport and coordinator. All share one aiohttp session and one TokenManager. Test with 2+ cameras. |
| HA shutdown hangs from uncancelled tasks | Medium | HA logs warnings, slow shutdown | Every asyncio.Task created must be tracked and cancelled in `async_stop()`. Use `asyncio.gather(*tasks, return_exceptions=True)`. |
| Token refresh race condition | Low | Concurrent refreshes, token invalidation | `asyncio.Lock` in TokenManager. Only one refresh at a time. |
| Camera firmware updates change proto schema | Low | New fields cause decode issues or missing data | betterproto ignores unknown fields by default. New fields won't break existing decoding. If fields are removed/renumbered (unlikely), tests will catch it. |
| `PUT_STREAMING` flow differs from what Go daemon does | Medium | Camera doesn't start streaming | Study nanitd's `camera.go` streaming logic carefully. Capture WS frames from working Go daemon as golden reference. |
| aionanit PyPI publish blocks HACS install | Low | Users can't install | Publish to PyPI before tagging the release. HACS installs requirements from PyPI via pip. Test HACS install flow. |

---

## 12. Testing Strategy

### Unit Tests (aionanit)

| Test File | What It Tests |
|---|---|
| `test_protocol.py` | Encode/decode roundtrip for every message type. Golden tests with captured frames from real camera (via nanitd logs). |
| `test_pending.py` | Request tracking, resolution, timeout, cancel_all. Verify no leaked futures. |
| `test_auth.py` | Token refresh logic, expiry calculation, concurrent refresh prevention, callback notification. |
| `test_rest.py` | Login (success, failure, MFA), refresh (success, expired), get_babies, get_events. All with mocked HTTP (aioresponses). |
| `test_transport.py` | Connect, receive, keepalive timing, disconnect handling, reconnect backoff progression. Mocked WebSocket. |
| `test_camera.py` | State aggregation from push events, command round-trips, subscribe/unsubscribe, start/stop lifecycle. |
| `test_client.py` | Client creation, token restore, camera caching, close cleanup. |

### Integration Tests (HA)

Using `pytest-homeassistant-custom-component`:

| Test | What It Tests |
|---|---|
| Config flow | Full flow: user → MFA → baby → local IP. Reauth flow. Options flow. |
| Setup/unload | `async_setup_entry` creates hub, coordinators, entities. `async_unload_entry` cleans up. |
| Entity state | Coordinator update → entity state reflects CameraState correctly. |
| Availability | Camera disconnect → entities unavailable. Reconnect → entities available. |
| Diagnostics | Output format, redaction of sensitive data. |

### Manual Testing Checklist

Before release, verify on a real Nanit camera:

- [ ] Fresh install via HACS: config flow works, camera appears
- [ ] Live stream plays in HA camera card
- [ ] Snapshot displays correctly
- [ ] Temperature/humidity/light sensors update (wait for push)
- [ ] Night light toggle: switch on → light turns on within 2s
- [ ] Volume slider: change → camera volume changes
- [ ] Camera power: turn off → camera enters sleep mode, stream stops
- [ ] Camera power: turn on → camera wakes up, stream available
- [ ] Disconnect camera from network → entities go unavailable within 60s
- [ ] Reconnect camera → entities recover within 30s
- [ ] Leave running for 24h → check logs for errors, memory stable
- [ ] Restart HA → integration reloads, all entities recover
- [ ] Reauth: invalidate token → HA shows reauth notification → complete reauth → integration recovers

---

## 13. Open Questions

These are unknowns that need to be resolved during implementation (not design decisions).

| Question | Context | How to Resolve |
|---|---|---|
| **uc_token acquisition flow** | Local connections use `Authorization: token {uc_token}`. The Go daemon obtains this via `GET_UCTOKENS` request type. Is this sent over the cloud WS first? Or is it a REST call? | Read nanitd `transport.go` carefully. Test by capturing WS frames during local connection setup. |
| **Cloud snapshot endpoint URL** | The Go daemon uses ffmpeg for snapshots. Does Nanit have a REST endpoint like `GET /babies/{uid}/snapshot`? | Test with curl against `api.nanit.com`. Check python-nanit and other repos for evidence. |
| **RTMPS URL exact format** | Is the token in the URL the raw access_token or does it need a prefix? | Capture from nanitd's `hlsproxy.go` which constructs this URL. Test with ffprobe. |
| **PUT_STREAMING orchestration** | Does the camera need to be told to start pushing RTMP? Or does it start automatically when a client connects to the RTMPS URL? | Study nanitd's `camera.go` `startStreaming()` method. The proto has `Streaming{id, status, rtmp_url}` — the `rtmp_url` field suggests the camera is told WHERE to push. |
| **betterproto proto2 required fields** | proto2 `required` fields have no default. Does betterproto handle this or crash? | Generate code, try parsing a message with missing required fields. May need to switch proto2 → proto3 syntax (all fields become optional) or patch generated code. |
| **Local WS connection limit behavior** | What happens when a 3rd connection is attempted? 403? Silent rejection? | Test with 3 concurrent WS connections to port 442. |
| **Sensor data push frequency** | How often does the camera push sensor data? Every few seconds? Only on change? | Connect and observe. This determines whether the 10-min resync is necessary or just a safety net. |
| **Cloud event types** | The current code checks for "MOTION" and "SOUND". Are there other event types? | Poll `GET /babies/{uid}/messages` and catalog all `type` values observed. |
| **Token lifetime precision** | We assume 60 minutes. Is this exact? Does it vary? | Capture the token, decode JWT payload, check `exp` claim. |
