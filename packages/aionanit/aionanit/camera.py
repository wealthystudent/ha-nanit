"""High-level API for a single Nanit camera."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp

from .auth import TokenManager
from .exceptions import (
    NanitCameraUnavailable,
    NanitConnectionError,
    NanitRequestTimeout,
    NanitTransportError,
)
from .models import (
    CameraEvent,
    CameraEventKind,
    CameraState,
    ConnectionInfo,
    ConnectionState,
    ControlState,
    NightLightState,
    SensorState,
    SettingsState,
    StatusState,
    TransportKind,
)
from .parsers import (
    _parse_control,
    _parse_control_from_proto,
    _parse_sensor_data,
    _parse_settings,
    _parse_settings_from_proto,
    _parse_status,
    _parse_status_from_proto,
)
from .proto import (
    ControlNightLight,
    ControlSensorDataTransfer,
    StreamingStatus,
)
from .proto import nanit_pb2 as proto
from .rest import NanitRestClient
from .ws.pending import PendingRequests
from .ws.protocol import (
    build_request,
    decode_message,
    extract_request,
    extract_response,
)
from .ws.transport import WsTransport

_LOGGER = logging.getLogger(__name__)

Control = proto.Control
GetControl = proto.GetControl
GetSensorData = proto.GetSensorData
GetStatus = proto.GetStatus
ProtoRequest = proto.Request
RequestType = proto.RequestType
Response = proto.Response
Settings = proto.Settings
StreamIdentifier = proto.StreamIdentifier
Streaming = proto.Streaming

_DEFAULT_REQUEST_TIMEOUT: float = 10.0
_LOCAL_PROBE_INTERVAL: float = 300.0  # 5 minutes
_MAX_LOCAL_FAILURES_BEFORE_CLOUD: int = 3
_STALE_CONNECTION_THRESHOLD: float = 300.0  # 5 min — reconnect before send
_HEALTH_CHECK_INTERVAL: float = 270.0  # 4.5 min — periodic session liveness check
_FRESH_CONNECTION_WINDOW: float = 10.0  # skip reconnect if connected within this
_DEFAULT_SENSOR_POLL_INTERVAL: float = 120.0  # 2 min — poll sensors camera doesn't push


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
        sensor_poll_interval: float | None = None,
    ) -> None:
        self._uid: str = uid
        self._baby_uid: str = baby_uid
        self._token_manager: TokenManager = token_manager
        self._rest: NanitRestClient = rest_client
        self._session: aiohttp.ClientSession = session
        self._prefer_local: bool = prefer_local
        self._local_ip: str | None = local_ip
        self._sensor_poll_interval: float = (
            sensor_poll_interval
            if sensor_poll_interval is not None
            else _DEFAULT_SENSOR_POLL_INTERVAL
        )

        self._state: CameraState = CameraState()
        self._pending: PendingRequests = PendingRequests()
        self._transport: WsTransport = WsTransport(
            session,
            self._on_ws_message,
            self._on_connection_change,
            get_headers=self._async_get_cloud_headers,
        )
        self._subscribers: list[Callable[[CameraEvent], None]] = []
        self._local_probe_task: asyncio.Task[None] | None = None
        self._health_check_task: asyncio.Task[None] | None = None
        self._sensor_poll_task: asyncio.Task[None] | None = None
        self._token_refresh_task: asyncio.Task[None] | None = None
        self._reconnected_task: asyncio.Task[None] | None = None
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._stopped: bool = False
        self._connected_event: asyncio.Event = asyncio.Event()
        self._connected_event.set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def uid(self) -> str:
        """Camera UID."""
        return self._uid

    @property
    def baby_uid(self) -> str:
        """Baby UID associated with this camera."""
        return self._baby_uid

    @property
    def state(self) -> CameraState:
        """Current aggregated camera state snapshot."""
        return self._state

    @property
    def connected(self) -> bool:
        """True when the WebSocket transport is connected."""
        return self._transport.connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Start the camera connection.

        1. If prefer_local and local_ip set: try local first
        2. If local fails or not configured: connect via cloud
        3. After connect: request initial state
        4. Enable sensor push via PUT_CONTROL
        5. Start local probe task if on cloud and local_ip configured
        """
        self._stopped = False
        connected = False

        # Try local first.
        if self._prefer_local and self._local_ip:
            try:
                token = await self._token_manager.async_get_access_token()
                await self._transport.async_connect_local(self._local_ip, token)
                connected = True
            except (NanitConnectionError, NanitTransportError) as err:
                _LOGGER.info(
                    "Local connection to %s failed (%s), falling back to cloud",
                    self._local_ip,
                    err,
                )

        # Fall back to cloud.
        if not connected:
            try:
                token = await self._token_manager.async_get_access_token()
                await self._transport.async_connect_cloud(self._uid, token)
            except (NanitConnectionError, NanitTransportError) as err:
                raise NanitCameraUnavailable(
                    f"Cannot reach camera {self._uid} via any transport: {err}"
                ) from err

        # Request initial state.
        await self._async_request_initial_state()

        # Enable sensor push.
        await self._async_enable_sensor_push()

        # Start local probe if on cloud and local_ip is configured.
        if self._transport.transport_kind == TransportKind.CLOUD and self._local_ip:
            self._start_local_probe()

        # Start periodic session health check.
        self._start_health_check()

        # Start periodic sensor polling (light values are not pushed).
        self._start_sensor_poll()

        self._start_token_refresh()

    async def async_stop(self) -> None:
        """Stop the camera connection. Cancel all tasks, close transport."""
        self._stopped = True
        self._cancel_token_refresh()
        self._cancel_local_probe()
        self._cancel_health_check()
        self._cancel_sensor_poll()
        self._cancel_reconnected_task()
        self._pending.cancel_all()
        await self._transport.async_close()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[CameraEvent], None]) -> Callable[[], None]:
        """Register a callback for state changes.

        Returns an unsubscribe function.
        """
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            self._subscribers.remove(callback)

        return _unsubscribe

    # ------------------------------------------------------------------
    # Commands — GET
    # ------------------------------------------------------------------

    async def async_get_status(self) -> StatusState:
        """GET_STATUS request (all fields)."""
        resp = await self._send_request(
            RequestType.GET_STATUS,
            get_status=GetStatus(all=True),
        )
        status = _parse_status(resp)
        self._update_state(status=status, kind=CameraEventKind.STATUS_UPDATE)
        return status

    async def async_get_settings(self) -> SettingsState:
        """GET_SETTINGS request."""
        resp = await self._send_request(RequestType.GET_SETTINGS)
        settings = _parse_settings(resp)
        self._update_state(settings=settings, kind=CameraEventKind.SETTINGS_UPDATE)
        return settings

    async def async_get_control(self) -> ControlState:
        """GET_CONTROL request."""
        resp = await self._send_request(
            RequestType.GET_CONTROL,
            get_control=GetControl(night_light=True),
        )
        control = _parse_control(resp)
        self._update_state(control=control, kind=CameraEventKind.CONTROL_UPDATE)
        return control

    async def async_get_sensor_data(self) -> SensorState:
        """GET_SENSOR_DATA request (all sensors)."""
        resp = cast(
            Any,
            await self._send_request(
                RequestType.GET_SENSOR_DATA,
                get_sensor_data=GetSensorData(all=True),
            ),
        )
        sensors = _parse_sensor_data(resp.sensor_data, self._state.sensors)
        self._update_state(sensors=sensors, kind=CameraEventKind.SENSOR_UPDATE)
        return sensors

    # ------------------------------------------------------------------
    # Commands — SET
    # ------------------------------------------------------------------

    async def async_set_settings(
        self,
        *,
        night_vision: bool | None = None,
        volume: int | None = None,
        sleep_mode: bool | None = None,
        status_light_on: bool | None = None,
        mic_mute_on: bool | None = None,
        night_light_brightness: int | None = None,
    ) -> SettingsState:
        """PUT_SETTINGS request. Only provided fields are sent."""
        proto_settings = Settings()
        if night_vision is not None:
            proto_settings.night_vision = night_vision
        if volume is not None:
            proto_settings.volume = volume
        if sleep_mode is not None:
            proto_settings.sleep_mode = sleep_mode
        if status_light_on is not None:
            proto_settings.status_light_on = status_light_on
        if mic_mute_on is not None:
            proto_settings.mic_mute_on = mic_mute_on
        if night_light_brightness is not None:
            proto_settings.night_light_brightness = night_light_brightness

        resp = cast(
            Any,
            await self._send_request(
                RequestType.PUT_SETTINGS,
                settings=proto_settings,
            ),
        )
        if resp.HasField("settings"):
            new_settings = _parse_settings(resp)
        else:
            # Camera didn't echo settings back — apply optimistic merge.
            requested: dict[str, Any] = {}
            if night_vision is not None:
                requested["night_vision"] = night_vision
            if volume is not None:
                requested["volume"] = volume
            if sleep_mode is not None:
                requested["sleep_mode"] = sleep_mode
            if status_light_on is not None:
                requested["status_light_on"] = status_light_on
            if mic_mute_on is not None:
                requested["mic_mute_on"] = mic_mute_on
            if night_light_brightness is not None:
                requested["night_light_brightness"] = night_light_brightness
            new_settings = dataclasses.replace(self._state.settings, **requested)
        self._update_state(settings=new_settings, kind=CameraEventKind.SETTINGS_UPDATE)
        return new_settings

    async def async_set_control(
        self,
        *,
        night_light: NightLightState | None = None,
        night_light_timeout: int | None = None,
    ) -> ControlState:
        """PUT_CONTROL request."""
        proto_control = Control()
        if night_light is not None:
            proto_control.night_light = (
                ControlNightLight.LIGHT_ON
                if night_light == NightLightState.ON
                else ControlNightLight.LIGHT_OFF
            )
        if night_light_timeout is not None:
            proto_control.night_light_timeout = night_light_timeout

        resp = cast(
            Any,
            await self._send_request(
                RequestType.PUT_CONTROL,
                control=proto_control,
            ),
        )
        if resp.HasField("control"):
            new_control = _parse_control(resp)
        else:
            # Camera didn't echo control back — apply optimistic merge.
            requested: dict[str, Any] = {}
            if night_light is not None:
                requested["night_light"] = night_light
            if night_light_timeout is not None:
                requested["night_light_timeout"] = night_light_timeout
            new_control = dataclasses.replace(self._state.control, **requested)
        self._update_state(control=new_control, kind=CameraEventKind.CONTROL_UPDATE)
        return new_control

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def async_get_stream_rtmps_url(self) -> str:
        """Build RTMPS URL with fresh token.

        Returns: rtmps://media-secured.nanit.com/nanit/{baby_uid}.{access_token}
        """
        token = await self._token_manager.async_get_access_token()
        return f"rtmps://media-secured.nanit.com/nanit/{self._baby_uid}.{token}"

    async def async_start_streaming(self) -> None:
        """Send PUT_STREAMING with status=STARTED to camera."""
        rtmps_url = await self.async_get_stream_rtmps_url()
        streaming = Streaming(
            id=StreamIdentifier.MOBILE,
            status=StreamingStatus.STARTED,
            rtmp_url=rtmps_url,
        )
        await self._send_request(
            RequestType.PUT_STREAMING,
            streaming=streaming,
        )

    async def async_stop_streaming(self) -> None:
        """Send PUT_STREAMING with status=STOPPED to camera."""
        streaming = Streaming(
            id=StreamIdentifier.MOBILE,
            status=StreamingStatus.STOPPED,
            rtmp_url="",
        )
        await self._send_request(
            RequestType.PUT_STREAMING,
            streaming=streaming,
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def async_get_snapshot(self) -> bytes | None:
        """Get a JPEG snapshot from the cloud REST endpoint.

        Returns None if the endpoint is unavailable or returns an error.
        """
        try:
            token = await self._token_manager.async_get_access_token()
            resp = await self._session.get(
                f"https://api.nanit.com/babies/{self._baby_uid}/snapshot",
                headers={"Authorization": token},
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if resp.status == 200:
                return await resp.read()
            _LOGGER.debug(
                "Snapshot endpoint returned %s for baby %s",
                resp.status,
                self._baby_uid,
            )
        except Exception as err:
            _LOGGER.debug("Snapshot fetch failed: %s", err)
        return None

    # ------------------------------------------------------------------
    # Internal — header refresh for reconnect
    # ------------------------------------------------------------------

    async def _async_get_cloud_headers(self) -> dict[str, str]:
        """Build fresh WebSocket headers using a current access token.

        Called by WsTransport._reconnect_loop before each reconnect attempt
        so that stale tokens are replaced with freshly issued ones.
        """
        token = await self._token_manager.async_get_access_token()
        if self._transport.transport_kind == TransportKind.LOCAL:
            return {"Authorization": f"token {token}"}
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Internal — WebSocket message handling
    # ------------------------------------------------------------------

    def _on_ws_message(self, data: bytes) -> None:
        """Handle incoming WebSocket binary frame."""
        msg = decode_message(data)

        # RESPONSE — resolve pending request future.
        response = extract_response(msg)
        if response is not None:
            resolved = self._pending.resolve(response.request_id, response)
            if not resolved:
                _LOGGER.debug(
                    "Received response for unknown request %s",
                    response.request_id,
                )
            return

        # REQUEST — push event from camera.
        request = extract_request(msg)
        if request is not None:
            self._handle_push_event(request)
            return

        # KEEPALIVE — nothing to do (transport handles ping/pong).

    def _handle_push_event(self, request: object) -> None:
        """Process a push REQUEST from the camera."""
        if not isinstance(request, ProtoRequest):
            return

        proto_request = cast(Any, request)
        req_type = proto_request.type

        if req_type == RequestType.PUT_SENSOR_DATA:
            sensors = _parse_sensor_data(proto_request.sensor_data, self._state.sensors)
            self._update_state(sensors=sensors, kind=CameraEventKind.SENSOR_UPDATE)

        elif req_type == RequestType.PUT_STATUS:
            if proto_request.HasField("status"):
                status = _parse_status_from_proto(proto_request.status)
                self._update_state(status=status, kind=CameraEventKind.STATUS_UPDATE)

        elif req_type == RequestType.PUT_SETTINGS:
            if proto_request.HasField("settings"):
                settings = _parse_settings_from_proto(proto_request.settings)
                self._update_state(settings=settings, kind=CameraEventKind.SETTINGS_UPDATE)

        elif req_type == RequestType.PUT_CONTROL:
            if proto_request.HasField("control"):
                control = _parse_control_from_proto(proto_request.control)
                self._update_state(control=control, kind=CameraEventKind.CONTROL_UPDATE)

        else:
            _LOGGER.debug("Unhandled push request type: %s", req_type)

    # ------------------------------------------------------------------
    # Internal — connection change
    # ------------------------------------------------------------------

    def _on_connection_change(
        self,
        state: ConnectionState,
        transport: TransportKind,
        error: str | None,
    ) -> None:
        """Handle connection state transitions."""
        now = datetime.now(UTC)
        old_conn = self._state.connection

        new_conn = ConnectionInfo(
            state=state,
            transport=transport,
            last_seen=now if state == ConnectionState.CONNECTED else old_conn.last_seen,
            last_error=error,
            reconnect_attempts=(
                old_conn.reconnect_attempts + 1
                if state == ConnectionState.RECONNECTING
                else 0
                if state == ConnectionState.CONNECTED
                else old_conn.reconnect_attempts
            ),
        )

        self._state = dataclasses.replace(self._state, connection=new_conn)

        if state == ConnectionState.CONNECTED:
            self._connected_event.set()
        elif state in (ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING):
            self._connected_event.clear()

        if state == ConnectionState.DISCONNECTED:
            self._pending.cancel_all(NanitTransportError("Connection lost"))

        self._notify_subscribers(CameraEventKind.CONNECTION_CHANGE)

        # After a successful reconnect, re-initialize the session.
        if state == ConnectionState.CONNECTED and old_conn.reconnect_attempts > 0:
            self._cancel_reconnected_task()
            self._reconnected_task = asyncio.get_running_loop().create_task(
                self._async_on_reconnected()
            )

    async def _async_on_reconnected(self) -> None:
        """Re-initialize session after a successful reconnect.

        Requests full state from the camera and re-enables sensor push
        so that push-based data resumes after a connection drop.
        """
        _LOGGER.info("Re-initializing session after reconnect")
        await self._async_request_initial_state()
        await self._async_enable_sensor_push()

    # ------------------------------------------------------------------
    # Internal — state management
    # ------------------------------------------------------------------

    def _update_state(
        self,
        *,
        sensors: SensorState | None = None,
        settings: SettingsState | None = None,
        control: ControlState | None = None,
        status: StatusState | None = None,
        kind: CameraEventKind,
    ) -> None:
        """Apply a partial state update and notify subscribers."""
        replacements: dict[str, Any] = {}
        if sensors is not None:
            replacements["sensors"] = sensors
        if settings is not None:
            replacements["settings"] = settings
        if control is not None:
            replacements["control"] = control
        if status is not None:
            replacements["status"] = status

        if replacements:
            self._state = dataclasses.replace(self._state, **replacements)

        self._notify_subscribers(kind)

    def _notify_subscribers(self, kind: CameraEventKind) -> None:
        """Fire all subscriber callbacks with the current state."""
        event = CameraEvent(kind=kind, state=self._state)
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("Error in camera event subscriber")

    # ------------------------------------------------------------------
    # Internal — request/response
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        request_type: int,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        """Send a protobuf request and await the correlated response.

        Includes automatic stale-connection detection and one transparent
        retry after inline reconnect so that commands succeed even when the
        server-side session has silently expired.
        """
        if not self._transport.connected:
            try:
                await asyncio.wait_for(self._connected_event.wait(), timeout=15.0)
            except TimeoutError:
                pass

        for attempt in range(2):
            # Pre-send gate: if the connection has been idle longer than
            # the threshold, the server-side session is likely dead.
            # Reconnect proactively so the send goes over a fresh session.
            if (
                attempt == 0
                and self._transport.connected
                and self._transport.idle_seconds > _STALE_CONNECTION_THRESHOLD
            ):
                _LOGGER.warning(
                    "Connection idle for %.0fs, reconnecting before send",
                    self._transport.idle_seconds,
                )
                await self._async_reconnect()

            # Ensure we are connected before attempting to send.
            if not self._transport.connected:
                if attempt > 0:
                    raise NanitCameraUnavailable(
                        f"Camera {self._uid} not reachable after reconnect"
                    )
                _LOGGER.warning("Not connected to camera %s, reconnecting", self._uid)
                await self._async_reconnect()

            request_id = self._pending.next_id()
            data = build_request(request_id, request_type, **kwargs)
            future = self._pending.track(request_id)

            try:
                await self._transport.async_send(data)
            except NanitTransportError:
                _ = self._pending.resolve(request_id, Response())
                if attempt == 0:
                    _LOGGER.warning("Send failed, reconnecting and retrying")
                    await self._async_reconnect()
                    continue
                raise

            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                _ = self._pending.resolve(request_id, Response())
                if attempt == 0:
                    _LOGGER.warning(
                        "Request %s (id=%s) timed out after %.1fs, reconnecting and retrying",
                        RequestType.Name(request_type),
                        request_id,
                        timeout,
                    )
                    await self._async_reconnect()
                    continue
                raise NanitRequestTimeout(
                    RequestType.Name(request_type), request_id, timeout
                ) from None

        # Should never be reached — the loop always returns or raises.
        raise NanitCameraUnavailable(f"Camera {self._uid} request failed")

    # ------------------------------------------------------------------
    # Internal — initial state + sensor push
    # ------------------------------------------------------------------

    async def _async_request_initial_state(self) -> None:
        """Request full state from camera after connecting."""
        try:
            await self.async_get_status()
        except (NanitRequestTimeout, NanitTransportError) as err:
            _LOGGER.warning("Initial GET_STATUS failed: %s", err)

        try:
            await self.async_get_settings()
        except (NanitRequestTimeout, NanitTransportError) as err:
            _LOGGER.warning("Initial GET_SETTINGS failed: %s", err)

        try:
            await self.async_get_sensor_data()
        except (NanitRequestTimeout, NanitTransportError) as err:
            _LOGGER.warning("Initial GET_SENSOR_DATA failed: %s", err)

        try:
            await self.async_get_control()
        except (NanitRequestTimeout, NanitTransportError) as err:
            _LOGGER.warning("Initial GET_CONTROL failed: %s", err)

    async def _async_enable_sensor_push(self) -> None:
        """Send PUT_CONTROL to enable sensor data push from camera."""
        transfer = ControlSensorDataTransfer(
            sound=True,
            motion=True,
            temperature=True,
            humidity=True,
            light=True,
            night=True,
        )
        proto_control = Control(sensor_data_transfer=transfer)
        try:
            await self._send_request(
                RequestType.PUT_CONTROL,
                control=proto_control,
            )
        except (NanitRequestTimeout, NanitTransportError) as err:
            _LOGGER.warning("Enable sensor push failed: %s", err)

    # ------------------------------------------------------------------
    # Internal — local probe
    # ------------------------------------------------------------------

    def _start_local_probe(self) -> None:
        """Start background task to probe for local connectivity."""
        self._cancel_local_probe()
        self._local_probe_task = asyncio.get_running_loop().create_task(self._local_probe_loop())

    def _cancel_local_probe(self) -> None:
        """Cancel the local probe task if running."""
        if self._local_probe_task is not None and not self._local_probe_task.done():
            self._local_probe_task.cancel()
        self._local_probe_task = None

    # ------------------------------------------------------------------
    # Internal — inline reconnect
    # ------------------------------------------------------------------

    async def _async_reconnect(self) -> None:
        """Close and re-establish the WebSocket connection inline.

        Used by ``_send_request`` to transparently recover from stale or
        broken connections without surfacing errors to the caller.

        A lock prevents concurrent reconnects, and a freshness guard skips
        the reconnect if another caller just completed one.
        """
        async with self._reconnect_lock:
            # Skip if another caller already reconnected.
            if (
                self._transport.connected
                and self._transport.idle_seconds < _FRESH_CONNECTION_WINDOW
            ):
                _LOGGER.debug(
                    "Skipping reconnect — connection is fresh (idle %.1fs)",
                    self._transport.idle_seconds,
                )
                return

            _LOGGER.info("Reconnecting camera %s inline", self._uid)
            self._cancel_local_probe()
            self._cancel_token_refresh()

            connected = False
            if self._prefer_local and self._local_ip:
                try:
                    token = await self._token_manager.async_get_access_token()
                    await self._transport.async_connect_local(self._local_ip, token)
                    connected = True
                except (NanitConnectionError, NanitTransportError) as err:
                    _LOGGER.info(
                        "Local reconnect to %s failed (%s), trying cloud",
                        self._local_ip,
                        err,
                    )

            if not connected:
                try:
                    token = await self._token_manager.async_get_access_token()
                    await self._transport.async_connect_cloud(self._uid, token)
                except (NanitConnectionError, NanitTransportError) as err:
                    raise NanitCameraUnavailable(f"Cannot reach camera {self._uid}: {err}") from err

            await self._async_enable_sensor_push()

            if self._transport.transport_kind == TransportKind.CLOUD and self._local_ip:
                self._start_local_probe()

            # Restart sensor polling after reconnect.
            self._start_sensor_poll()

            self._start_token_refresh()

    def _start_token_refresh(self) -> None:
        self._cancel_token_refresh()
        self._token_refresh_task = asyncio.get_running_loop().create_task(
            self._token_refresh_loop()
        )

    def _cancel_token_refresh(self) -> None:
        if self._token_refresh_task is not None and not self._token_refresh_task.done():
            self._token_refresh_task.cancel()
        self._token_refresh_task = None

    def _cancel_reconnected_task(self) -> None:
        if self._reconnected_task is not None and not self._reconnected_task.done():
            self._reconnected_task.cancel()
        self._reconnected_task = None

    async def _token_refresh_loop(self) -> None:
        try:
            while not self._stopped:
                ttl = self._token_manager._expires_at - time.monotonic()
                sleep_for = max(ttl - 300.0, 60.0)
                await asyncio.sleep(sleep_for)
                if self._stopped or not self._transport.connected:
                    continue
                _LOGGER.info("Pre-emptive token refresh: forcing reconnect before expiry")
                try:
                    await self._transport.async_force_reconnect()
                except Exception:
                    _LOGGER.debug("Pre-emptive reconnect trigger failed", exc_info=True)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Internal — session health check
    # ------------------------------------------------------------------

    def _start_health_check(self) -> None:
        """Start the periodic session health-check task."""
        self._cancel_health_check()
        self._health_check_task = asyncio.get_running_loop().create_task(self._health_check_loop())

    def _cancel_health_check(self) -> None:
        """Cancel the health-check task if running."""
        if self._health_check_task is not None and not self._health_check_task.done():
            self._health_check_task.cancel()
        self._health_check_task = None

    async def _health_check_loop(self) -> None:
        """Periodically verify the session is responsive.

        Sends a lightweight GET_STATUS every ``_HEALTH_CHECK_INTERVAL``
        seconds. If the session is stale, ``_send_request`` will detect it
        (via the staleness gate or timeout-retry) and reconnect
        transparently.  This keeps the session warm so that user-initiated
        commands succeed immediately even after long idle periods.
        """
        try:
            while not self._stopped:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                if self._stopped or not self._transport.connected:
                    continue
                try:
                    await self.async_get_status()
                except (
                    NanitRequestTimeout,
                    NanitTransportError,
                    NanitCameraUnavailable,
                ):
                    _LOGGER.info("Session health check failed — reconnect triggered")
                except Exception:
                    _LOGGER.debug("Health check error", exc_info=True)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Internal — periodic sensor polling
    # ------------------------------------------------------------------

    def _start_sensor_poll(self) -> None:
        """Start the periodic sensor-poll task.

        Some sensor types (notably LIGHT / illuminance) are not pushed by
        the camera firmware despite enabling sensor push via PUT_CONTROL.
        This loop issues GET_SENSOR_DATA periodically so those values stay
        up-to-date.
        """
        self._cancel_sensor_poll()
        self._sensor_poll_task = asyncio.get_running_loop().create_task(self._sensor_poll_loop())

    def _cancel_sensor_poll(self) -> None:
        """Cancel the sensor-poll task if running."""
        if self._sensor_poll_task is not None and not self._sensor_poll_task.done():
            self._sensor_poll_task.cancel()
        self._sensor_poll_task = None

    async def _sensor_poll_loop(self) -> None:
        """Periodically request sensor data from the camera.

        The Nanit camera pushes temperature and humidity via
        PUT_SENSOR_DATA, but does not push light (illuminance) values.
        This loop compensates by explicitly requesting all sensor data
        every ``_sensor_poll_interval`` seconds.
        """
        try:
            while not self._stopped:
                await asyncio.sleep(self._sensor_poll_interval)
                if self._stopped or not self._transport.connected:
                    continue
                try:
                    await self.async_get_sensor_data()
                except (
                    NanitRequestTimeout,
                    NanitTransportError,
                    NanitCameraUnavailable,
                ):
                    _LOGGER.debug("Sensor poll failed — will retry next cycle")
                except Exception:
                    _LOGGER.debug("Sensor poll error", exc_info=True)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Internal — local probe
    # ------------------------------------------------------------------

    async def _local_probe_loop(self) -> None:
        """Periodically check if local camera is reachable and promote."""
        try:
            while not self._stopped:
                await asyncio.sleep(_LOCAL_PROBE_INTERVAL)
                if self._stopped:
                    return
                if self._transport.transport_kind == TransportKind.LOCAL:
                    # Already on local — stop probing.
                    return
                if not self._local_ip:
                    return

                try:
                    _LOGGER.debug("Probing local camera at %s", self._local_ip)
                    token = await self._token_manager.async_get_access_token()
                    # Create a temporary transport to test local.
                    probe = WsTransport(
                        self._session,
                        lambda _data: None,
                        lambda _s, _t, _e: None,
                    )
                    try:
                        await asyncio.wait_for(
                            probe.async_connect_local(self._local_ip, token),
                            timeout=5.0,
                        )
                        # Local is reachable — promote.
                        await probe.async_close()
                    except (TimeoutError, NanitConnectionError, NanitTransportError):
                        _LOGGER.debug("Local probe failed, staying on cloud")
                        continue

                    _LOGGER.info("Local camera reachable, promoting from cloud to local")
                    # Close cloud, connect local.
                    self._pending.cancel_all()
                    await self._transport.async_close()
                    await self._transport.async_connect_local(self._local_ip, token)
                    await self._async_request_initial_state()
                    await self._async_enable_sensor_push()
                    return  # Stop probing — now on local.

                except Exception as err:
                    _LOGGER.debug("Local probe error: %s", err)

        except asyncio.CancelledError:
            return
