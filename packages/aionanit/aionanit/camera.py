"""High-level API for a single Nanit camera."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

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
from .proto import (
    Control,
    ControlNightLight,
    ControlSensorDataTransfer,
    GetControl,
    GetSensorData,
    GetStatus,
    MountingMode,
    RequestType,
    Response,
    SensorData,
    Settings,
    SettingsWifiBand,
    StatusConnectionToServer,
    Streaming,
    StreamIdentifier,
    StreamingStatus,
)
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

_DEFAULT_REQUEST_TIMEOUT: float = 10.0
_LOCAL_PROBE_INTERVAL: float = 300.0  # 5 minutes
_MAX_LOCAL_FAILURES_BEFORE_CLOUD: int = 3

# Maps for proto enum → model string conversions.
_WIFI_BAND_MAP: dict[int, str] = {
    SettingsWifiBand.ANY: "any",
    SettingsWifiBand.FR2_4GHZ: "2.4ghz",
    SettingsWifiBand.FR5_0GHZ: "5ghz",
}

_MOUNTING_MODE_MAP: dict[int, str] = {
    MountingMode.STAND: "stand",
    MountingMode.TRAVEL: "travel",
    MountingMode.SWITCH: "switch",
}


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
    ) -> None:
        self._uid: str = uid
        self._baby_uid: str = baby_uid
        self._token_manager: TokenManager = token_manager
        self._rest: NanitRestClient = rest_client
        self._session: aiohttp.ClientSession = session
        self._prefer_local: bool = prefer_local
        self._local_ip: str | None = local_ip

        self._state: CameraState = CameraState()
        self._pending: PendingRequests = PendingRequests()
        self._transport: WsTransport = WsTransport(
            session,
            self._on_ws_message,
            self._on_connection_change,
        )
        self._subscribers: list[Callable[[CameraEvent], None]] = []
        self._local_probe_task: asyncio.Task[None] | None = None
        self._stopped: bool = False

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
                await self._transport.async_connect_local(
                    self._local_ip, token
                )
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
        if (
            self._transport.transport_kind == TransportKind.CLOUD
            and self._local_ip
        ):
            self._start_local_probe()

    async def async_stop(self) -> None:
        """Stop the camera connection. Cancel all tasks, close transport."""
        self._stopped = True
        self._cancel_local_probe()
        self._pending.cancel_all()
        await self._transport.async_close()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(
        self, callback: Callable[[CameraEvent], None]
    ) -> Callable[[], None]:
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
        self._update_state(
            settings=settings, kind=CameraEventKind.SETTINGS_UPDATE
        )
        return settings

    async def async_get_control(self) -> ControlState:
        """GET_CONTROL request."""
        resp = await self._send_request(
            RequestType.GET_CONTROL,
            get_control=GetControl(night_light=True),
        )
        control = _parse_control(resp)
        self._update_state(
            control=control, kind=CameraEventKind.CONTROL_UPDATE
        )
        return control

    async def async_get_sensor_data(self) -> SensorState:
        """GET_SENSOR_DATA request (all sensors)."""
        resp = await self._send_request(
            RequestType.GET_SENSOR_DATA,
            get_sensor_data=GetSensorData(all=True),
        )
        sensors = _parse_sensor_data(resp.sensor_data, self._state.sensors)
        self._update_state(
            sensors=sensors, kind=CameraEventKind.SENSOR_UPDATE
        )
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

        resp = await self._send_request(
            RequestType.PUT_SETTINGS,
            settings=proto_settings,
        )
        new_settings = _parse_settings(resp)
        self._update_state(
            settings=new_settings, kind=CameraEventKind.SETTINGS_UPDATE
        )
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

        resp = await self._send_request(
            RequestType.PUT_CONTROL,
            control=proto_control,
        )
        new_control = _parse_control(resp)
        self._update_state(
            control=new_control, kind=CameraEventKind.CONTROL_UPDATE
        )
        return new_control

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def async_get_stream_rtmps_url(self) -> str:
        """Build RTMPS URL with fresh token.

        Returns: rtmps://media-secured.nanit.com/nanit/{baby_uid}.{access_token}
        """
        token = await self._token_manager.async_get_access_token()
        return (
            f"rtmps://media-secured.nanit.com/nanit/{self._baby_uid}.{token}"
        )

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
            )
            if resp.status == 200:
                return await resp.read()
            _LOGGER.debug(
                "Snapshot endpoint returned %s for baby %s",
                resp.status,
                self._baby_uid,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Snapshot fetch failed: %s", err)
        return None

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
        # request is proto Request type from extract_request
        from .proto import Request as ProtoRequest

        if not isinstance(request, ProtoRequest):
            return

        req_type = request.type

        if req_type == RequestType.PUT_SENSOR_DATA:
            sensors = _parse_sensor_data(
                request.sensor_data, self._state.sensors
            )
            self._update_state(
                sensors=sensors, kind=CameraEventKind.SENSOR_UPDATE
            )

        elif req_type == RequestType.PUT_STATUS:
            if request.status:
                status = _parse_status_from_proto(request.status)
                self._update_state(
                    status=status, kind=CameraEventKind.STATUS_UPDATE
                )

        elif req_type == RequestType.PUT_SETTINGS:
            if request.settings:
                settings = _parse_settings_from_proto(request.settings)
                self._update_state(
                    settings=settings, kind=CameraEventKind.SETTINGS_UPDATE
                )

        elif req_type == RequestType.PUT_CONTROL:
            if request.control:
                control = _parse_control_from_proto(request.control)
                self._update_state(
                    control=control, kind=CameraEventKind.CONTROL_UPDATE
                )

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
        now = datetime.now(timezone.utc)
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

        if state == ConnectionState.DISCONNECTED:
            self._pending.cancel_all(
                NanitTransportError("Connection lost")
            )

        self._notify_subscribers(CameraEventKind.CONNECTION_CHANGE)

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
        replacements: dict[str, object] = {}
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
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in camera event subscriber")

    # ------------------------------------------------------------------
    # Internal — request/response
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        request_type: RequestType,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        **kwargs: Any,
    ) -> Response:
        """Send a protobuf request and await the correlated response."""
        request_id = self._pending.next_id()
        data = build_request(request_id, request_type, **kwargs)
        future = self._pending.track(request_id)

        await self._transport.async_send(data)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as err:
            # Remove from pending if still tracked.
            _ = self._pending.resolve(request_id, Response())  # clean up
            raise NanitRequestTimeout(
                request_type.name or "UNKNOWN", request_id, timeout
            ) from err

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
        self._local_probe_task = asyncio.get_running_loop().create_task(
            self._local_probe_loop()
        )

    def _cancel_local_probe(self) -> None:
        """Cancel the local probe task if running."""
        if self._local_probe_task is not None and not self._local_probe_task.done():
            self._local_probe_task.cancel()
        self._local_probe_task = None

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
                    _LOGGER.debug(
                        "Probing local camera at %s", self._local_ip
                    )
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
                    except (
                        NanitConnectionError,
                        NanitTransportError,
                        asyncio.TimeoutError,
                    ):
                        _LOGGER.debug("Local probe failed, staying on cloud")
                        continue

                    _LOGGER.info(
                        "Local camera reachable, promoting from cloud to local"
                    )
                    # Close cloud, connect local.
                    self._pending.cancel_all()
                    await self._transport.async_close()
                    await self._transport.async_connect_local(
                        self._local_ip, token
                    )
                    await self._async_request_initial_state()
                    await self._async_enable_sensor_push()
                    return  # Stop probing — now on local.

                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Local probe error: %s", err)

        except asyncio.CancelledError:
            return


# ======================================================================
# Module-level parsing helpers (proto → model conversion)
# ======================================================================


def _parse_sensor_data(
    sensor_data_list: list[SensorData],
    current: SensorState,
) -> SensorState:
    """Convert a list of proto SensorData into a SensorState.

    Merges with the current state so unchanged sensors keep their values.
    """
    from .proto import SensorType as ProtoSensorType

    temperature = current.temperature
    humidity = current.humidity
    light = current.light
    sound_alert = current.sound_alert
    motion_alert = current.motion_alert
    night = current.night

    for sd in sensor_data_list:
        if sd.sensor_type == ProtoSensorType.TEMPERATURE:
            if sd.value_milli:
                temperature = sd.value_milli / 1000.0
            elif sd.value:
                temperature = float(sd.value)
        elif sd.sensor_type == ProtoSensorType.HUMIDITY:
            if sd.value_milli:
                humidity = sd.value_milli / 1000.0
            elif sd.value:
                humidity = float(sd.value)
        elif sd.sensor_type == ProtoSensorType.LIGHT:
            light = sd.value
        elif sd.sensor_type == ProtoSensorType.SOUND:
            sound_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.MOTION:
            motion_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.NIGHT:
            night = bool(sd.value)

    return SensorState(
        temperature=temperature,
        humidity=humidity,
        light=light,
        sound_alert=sound_alert,
        motion_alert=motion_alert,
        night=night,
    )


def _parse_status(resp: Response) -> StatusState:
    """Extract StatusState from a GET_STATUS response."""
    if resp.status:
        return _parse_status_from_proto(resp.status)
    return StatusState()


def _parse_status_from_proto(status: object) -> StatusState:
    """Convert proto Status to StatusState."""
    from .proto import Status as ProtoStatus

    if not isinstance(status, ProtoStatus):
        return StatusState()

    return StatusState(
        connected_to_server=(
            status.connection_to_server == StatusConnectionToServer.CONNECTED
        ),
        firmware_version=status.current_version or None,
        hardware_version=status.hardware_version or None,
        mounting_mode=_MOUNTING_MODE_MAP.get(status.mode),
    )


def _parse_settings(resp: Response) -> SettingsState:
    """Extract SettingsState from a GET_SETTINGS response."""
    if resp.settings:
        return _parse_settings_from_proto(resp.settings)
    return SettingsState()


def _parse_settings_from_proto(settings: object) -> SettingsState:
    """Convert proto Settings to SettingsState."""
    from .proto import Settings as ProtoSettings

    if not isinstance(settings, ProtoSettings):
        return SettingsState()

    return SettingsState(
        night_vision=settings.night_vision,
        volume=settings.volume,
        sleep_mode=settings.sleep_mode,
        status_light_on=settings.status_light_on,
        mic_mute_on=settings.mic_mute_on,
        wifi_band=_WIFI_BAND_MAP.get(settings.wifi_band),
        mounting_mode=_MOUNTING_MODE_MAP.get(settings.mounting_mode),
    )


def _parse_control(resp: Response) -> ControlState:
    """Extract ControlState from a GET_CONTROL response."""
    if resp.control:
        return _parse_control_from_proto(resp.control)
    return ControlState()


def _parse_control_from_proto(control: object) -> ControlState:
    """Convert proto Control to ControlState."""
    from .proto import Control as ProtoControl

    if not isinstance(control, ProtoControl):
        return ControlState()

    night_light: NightLightState | None = None
    if control.night_light == ControlNightLight.LIGHT_ON:
        night_light = NightLightState.ON
    elif control.night_light == ControlNightLight.LIGHT_OFF:
        night_light = NightLightState.OFF

    sensor_transfer_enabled: bool | None = None
    if control.sensor_data_transfer:
        sdt = control.sensor_data_transfer
        sensor_transfer_enabled = any(
            [sdt.sound, sdt.motion, sdt.temperature, sdt.humidity, sdt.light, sdt.night]
        )

    return ControlState(
        night_light=night_light,
        night_light_timeout=control.night_light_timeout or None,
        sensor_data_transfer_enabled=sensor_transfer_enabled,
    )
