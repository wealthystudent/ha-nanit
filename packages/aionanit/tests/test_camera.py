"""Tests for aionanit.camera — NanitCamera."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from aionanit.auth import TokenManager
from aionanit.camera import (
    NanitCamera,
    _parse_control,
    _parse_control_from_proto,
    _parse_sensor_data,
    _parse_settings,
    _parse_settings_from_proto,
    _parse_status,
    _parse_status_from_proto,
)
from aionanit.exceptions import (
    NanitCameraUnavailable,
    NanitConnectionError,
    NanitRequestTimeout,
    NanitTransportError,
)
from aionanit.models import (
    CameraEventKind,
    CameraState,
    ConnectionState,
    ControlState,
    NightLightState,
    SensorState,
    SettingsState,
    StatusState,
    TransportKind,
)
from aionanit.proto import (
    Control,
    ControlNightLight,
    ControlSensorDataTransfer,
    GetControl,
    GetSensorData,
    GetStatus,
    Message,
    MessageType,
    MountingMode,
    Request,
    RequestType,
    Response,
    SensorData,
    SensorType as ProtoSensorType,
    Settings,
    SettingsWifiBand,
    Status,
    StatusConnectionToServer,
    Streaming,
    StreamIdentifier,
    StreamingStatus,
)
from aionanit.rest import NanitRestClient
from aionanit.ws.protocol import encode_message


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_camera(
    *,
    prefer_local: bool = False,
    local_ip: str | None = None,
) -> tuple[NanitCamera, MagicMock, MagicMock]:
    """Create a NanitCamera with mocked dependencies.

    Returns (camera, mock_token_manager, mock_session).
    """
    session = MagicMock(spec=aiohttp.ClientSession)
    rest = MagicMock(spec=NanitRestClient)
    tm = MagicMock(spec=TokenManager)
    tm.async_get_access_token = AsyncMock(return_value="test_token")

    cam = NanitCamera(
        uid="cam_uid_1",
        baby_uid="baby_uid_1",
        token_manager=tm,
        rest_client=rest,
        session=session,
        prefer_local=prefer_local,
        local_ip=local_ip,
    )
    return cam, tm, session


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_uid(self) -> None:
        cam, *_ = _make_camera()
        assert cam.uid == "cam_uid_1"

    def test_baby_uid(self) -> None:
        cam, *_ = _make_camera()
        assert cam.baby_uid == "baby_uid_1"

    def test_state_is_default(self) -> None:
        cam, *_ = _make_camera()
        assert cam.state == CameraState()

    def test_connected_initially_false(self) -> None:
        cam, *_ = _make_camera()
        assert cam.connected is False


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class TestSubscriptions:
    def test_subscribe_and_receive_event(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        # Trigger a state change via internal method
        cam._notify_subscribers(CameraEventKind.SENSOR_UPDATE)

        assert len(events) == 1
        assert events[0].kind == CameraEventKind.SENSOR_UPDATE

    def test_unsubscribe(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        unsub = cam.subscribe(lambda e: events.append(e))

        cam._notify_subscribers(CameraEventKind.SENSOR_UPDATE)
        assert len(events) == 1

        unsub()
        cam._notify_subscribers(CameraEventKind.STATUS_UPDATE)
        assert len(events) == 1  # no new event after unsub

    def test_multiple_subscribers(self) -> None:
        cam, *_ = _make_camera()
        events1: list[object] = []
        events2: list[object] = []
        cam.subscribe(lambda e: events1.append(e))
        cam.subscribe(lambda e: events2.append(e))

        cam._notify_subscribers(CameraEventKind.CONTROL_UPDATE)

        assert len(events1) == 1
        assert len(events2) == 1

    def test_subscriber_error_does_not_break_others(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []

        def _bad_callback(_: object) -> None:
            raise ValueError("boom")

        cam.subscribe(_bad_callback)
        cam.subscribe(lambda e: events.append(e))

        cam._notify_subscribers(CameraEventKind.SENSOR_UPDATE)
        assert len(events) == 1  # second subscriber still called


# ---------------------------------------------------------------------------
# Connection change handling
# ---------------------------------------------------------------------------


class TestConnectionChange:
    def test_connected_updates_state(self) -> None:
        cam, *_ = _make_camera()
        cam._on_connection_change(
            ConnectionState.CONNECTED, TransportKind.CLOUD, None
        )
        assert cam.state.connection.state == ConnectionState.CONNECTED
        assert cam.state.connection.transport == TransportKind.CLOUD
        assert cam.state.connection.last_seen is not None
        assert cam.state.connection.reconnect_attempts == 0

    def test_reconnecting_increments_attempts(self) -> None:
        cam, *_ = _make_camera()
        cam._on_connection_change(
            ConnectionState.RECONNECTING, TransportKind.CLOUD, "err"
        )
        assert cam.state.connection.reconnect_attempts == 1

        cam._on_connection_change(
            ConnectionState.RECONNECTING, TransportKind.CLOUD, "err2"
        )
        assert cam.state.connection.reconnect_attempts == 2

    def test_connected_resets_attempts(self) -> None:
        cam, *_ = _make_camera()
        cam._on_connection_change(
            ConnectionState.RECONNECTING, TransportKind.CLOUD, "err"
        )
        cam._on_connection_change(
            ConnectionState.CONNECTED, TransportKind.CLOUD, None
        )
        assert cam.state.connection.reconnect_attempts == 0

    async def test_disconnected_cancels_pending(self) -> None:
        cam, *_ = _make_camera()
        future = cam._pending.track(cam._pending.next_id())

        cam._on_connection_change(
            ConnectionState.DISCONNECTED, TransportKind.NONE, "lost"
        )

        assert future.done()

    def test_fires_subscriber(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        cam._on_connection_change(
            ConnectionState.CONNECTED, TransportKind.CLOUD, None
        )
        assert len(events) == 1
        assert events[0].kind == CameraEventKind.CONNECTION_CHANGE


# ---------------------------------------------------------------------------
# State update
# ---------------------------------------------------------------------------


class TestStateUpdate:
    def test_updates_sensors(self) -> None:
        cam, *_ = _make_camera()
        new_sensors = SensorState(temperature=23.5, humidity=45.0)
        cam._update_state(sensors=new_sensors, kind=CameraEventKind.SENSOR_UPDATE)

        assert cam.state.sensors.temperature == 23.5
        assert cam.state.sensors.humidity == 45.0

    def test_partial_update_preserves_other_state(self) -> None:
        cam, *_ = _make_camera()
        cam._update_state(
            sensors=SensorState(temperature=22.0),
            kind=CameraEventKind.SENSOR_UPDATE,
        )
        cam._update_state(
            settings=SettingsState(volume=50),
            kind=CameraEventKind.SETTINGS_UPDATE,
        )
        # Sensors should still be there
        assert cam.state.sensors.temperature == 22.0
        assert cam.state.settings.volume == 50


# ---------------------------------------------------------------------------
# Proto parsing helpers
# ---------------------------------------------------------------------------


class TestParseSensorData:
    def test_parses_temperature(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.TEMPERATURE, value_milli=23500)]
        result = _parse_sensor_data(data, SensorState())
        assert result.temperature == 23.5

    def test_parses_humidity(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.HUMIDITY, value_milli=55000)]
        result = _parse_sensor_data(data, SensorState())
        assert result.humidity == 55.0

    def test_parses_light(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.LIGHT, value=120)]
        result = _parse_sensor_data(data, SensorState())
        assert result.light == 120

    def test_parses_sound_alert(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.SOUND, is_alert=True)]
        result = _parse_sensor_data(data, SensorState())
        assert result.sound_alert is True

    def test_parses_motion_alert(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.MOTION, is_alert=True)]
        result = _parse_sensor_data(data, SensorState())
        assert result.motion_alert is True

    def test_parses_night(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.NIGHT, value=1)]
        result = _parse_sensor_data(data, SensorState())
        assert result.night is True

    def test_merges_with_existing_state(self) -> None:
        existing = SensorState(temperature=22.0, humidity=40.0)
        # Only update temperature
        data = [SensorData(sensor_type=ProtoSensorType.TEMPERATURE, value_milli=25000)]
        result = _parse_sensor_data(data, existing)
        assert result.temperature == 25.0
        assert result.humidity == 40.0  # preserved

    def test_empty_data_returns_current(self) -> None:
        existing = SensorState(temperature=22.0)
        result = _parse_sensor_data([], existing)
        assert result.temperature == 22.0

    def test_temperature_fallback_to_value(self) -> None:
        data = [SensorData(sensor_type=ProtoSensorType.TEMPERATURE, value=24)]
        result = _parse_sensor_data(data, SensorState())
        assert result.temperature == 24.0


class TestParseStatus:
    def test_empty_response(self) -> None:
        resp = Response()
        result = _parse_status(resp)
        assert result == StatusState()

    def test_parses_connected_status(self) -> None:
        proto_status = Status(
            connection_to_server=StatusConnectionToServer.CONNECTED,
            current_version="1.2.3",
            hardware_version="hw4",
            mode=MountingMode.STAND,
        )
        result = _parse_status_from_proto(proto_status)
        assert result.connected_to_server is True
        assert result.firmware_version == "1.2.3"
        assert result.hardware_version == "hw4"
        assert result.mounting_mode == "stand"

    def test_non_status_type_returns_default(self) -> None:
        result = _parse_status_from_proto("not_a_status")
        assert result == StatusState()


class TestParseSettings:
    def test_empty_response(self) -> None:
        resp = Response()
        result = _parse_settings(resp)
        assert result == SettingsState()

    def test_parses_all_fields(self) -> None:
        proto_settings = Settings(
            night_vision=True,
            volume=75,
            sleep_mode=False,
            status_light_on=True,
            mic_mute_on=False,
            wifi_band=SettingsWifiBand.FR5_0GHZ,
            mounting_mode=MountingMode.TRAVEL,
        )
        result = _parse_settings_from_proto(proto_settings)
        assert result.night_vision is True
        assert result.volume == 75
        assert result.sleep_mode is False
        assert result.status_light_on is True
        assert result.mic_mute_on is False
        assert result.wifi_band == "5ghz"
        assert result.mounting_mode == "travel"

    def test_non_settings_type_returns_default(self) -> None:
        result = _parse_settings_from_proto("not_settings")
        assert result == SettingsState()


class TestParseControl:
    def test_empty_response(self) -> None:
        resp = Response()
        result = _parse_control(resp)
        assert result == ControlState()

    def test_parses_night_light_on(self) -> None:
        proto_control = Control(night_light=ControlNightLight.LIGHT_ON)
        result = _parse_control_from_proto(proto_control)
        assert result.night_light == NightLightState.ON

    def test_parses_night_light_off(self) -> None:
        proto_control = Control(night_light=ControlNightLight.LIGHT_OFF)
        result = _parse_control_from_proto(proto_control)
        assert result.night_light == NightLightState.OFF

    def test_parses_sensor_transfer_enabled(self) -> None:
        proto_control = Control(
            sensor_data_transfer=ControlSensorDataTransfer(
                sound=True, motion=True, temperature=False,
                humidity=False, light=False, night=False,
            )
        )
        result = _parse_control_from_proto(proto_control)
        assert result.sensor_data_transfer_enabled is True

    def test_non_control_type_returns_default(self) -> None:
        result = _parse_control_from_proto("not_control")
        assert result == ControlState()


# ---------------------------------------------------------------------------
# Push event handling
# ---------------------------------------------------------------------------


class TestHandlePushEvent:
    def test_put_sensor_data(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        request = Request(
            type=RequestType.PUT_SENSOR_DATA,
            sensor_data=[
                SensorData(
                    sensor_type=ProtoSensorType.TEMPERATURE,
                    value_milli=21500,
                ),
            ],
        )
        cam._handle_push_event(request)

        assert cam.state.sensors.temperature == 21.5
        assert len(events) == 1
        assert events[0].kind == CameraEventKind.SENSOR_UPDATE

    def test_put_status(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        request = Request(
            type=RequestType.PUT_STATUS,
            status=Status(
                connection_to_server=StatusConnectionToServer.CONNECTED,
                current_version="2.0.0",
            ),
        )
        cam._handle_push_event(request)

        assert cam.state.status.connected_to_server is True
        assert cam.state.status.firmware_version == "2.0.0"
        assert events[0].kind == CameraEventKind.STATUS_UPDATE

    def test_put_settings(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        request = Request(
            type=RequestType.PUT_SETTINGS,
            settings=Settings(volume=42),
        )
        cam._handle_push_event(request)

        assert cam.state.settings.volume == 42
        assert events[0].kind == CameraEventKind.SETTINGS_UPDATE

    def test_put_control(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        request = Request(
            type=RequestType.PUT_CONTROL,
            control=Control(night_light=ControlNightLight.LIGHT_ON),
        )
        cam._handle_push_event(request)

        assert cam.state.control.night_light == NightLightState.ON
        assert events[0].kind == CameraEventKind.CONTROL_UPDATE

    def test_ignores_non_proto_request(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        cam._handle_push_event("not_a_request")
        assert len(events) == 0

    def test_unhandled_request_type(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        request = Request(type=RequestType.GET_STATUS)
        cam._handle_push_event(request)
        assert len(events) == 0  # GET types are not push events


# ---------------------------------------------------------------------------
# WebSocket message dispatch
# ---------------------------------------------------------------------------


class TestOnWsMessage:
    async def test_dispatches_response(self) -> None:
        cam, *_ = _make_camera()
        req_id = cam._pending.next_id()
        future = cam._pending.track(req_id)

        # Build a RESPONSE message
        resp = Response(request_id=req_id, status_code=200)
        msg = Message(type=MessageType.RESPONSE, response=resp)
        data = bytes(msg)

        cam._on_ws_message(data)

        assert future.done()
        result = future.result()
        assert result.request_id == req_id

    def test_dispatches_push_request(self) -> None:
        cam, *_ = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        req = Request(
            type=RequestType.PUT_SETTINGS,
            settings=Settings(volume=80),
        )
        msg = Message(type=MessageType.REQUEST, request=req)
        data = bytes(msg)

        cam._on_ws_message(data)

        assert cam.state.settings.volume == 80
        assert len(events) == 1

    def test_keepalive_no_error(self) -> None:
        cam, *_ = _make_camera()
        # KEEPALIVE is type=0, which serializes to b"" for betterproto defaults
        msg = Message(type=MessageType.KEEPALIVE)
        data = bytes(msg)

        # Should not raise or trigger any state changes
        cam._on_ws_message(data)
        assert cam.state == CameraState()


# ---------------------------------------------------------------------------
# Send request + timeout
# ---------------------------------------------------------------------------


class TestSendRequest:
    async def test_sends_and_awaits_response(self) -> None:
        cam, *_ = _make_camera()

        # Mock transport to capture send calls
        cam._transport = MagicMock()
        cam._transport.async_send = AsyncMock()

        # Simulate response arriving after send
        resp = Response(status_code=200)

        async def _fake_send(data: bytes) -> None:
            # Resolve the pending request immediately
            req_id = 1  # first request id
            cam._pending.resolve(req_id, resp)

        cam._transport.async_send = AsyncMock(side_effect=_fake_send)

        result = await cam._send_request(RequestType.GET_STATUS, get_status=GetStatus(all=True))
        assert result.status_code == 200

    async def test_timeout_raises(self) -> None:
        cam, *_ = _make_camera()

        cam._transport = MagicMock()
        cam._transport.async_send = AsyncMock()

        with pytest.raises(NanitRequestTimeout):
            await cam._send_request(
                RequestType.GET_STATUS,
                timeout=0.01,
                get_status=GetStatus(all=True),
            )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_get_rtmps_url(self) -> None:
        cam, tm, _ = _make_camera()
        tm.async_get_access_token = AsyncMock(return_value="fresh_token")

        url = await cam.async_get_stream_rtmps_url()
        assert url == "rtmps://media-secured.nanit.com/nanit/baby_uid_1.fresh_token"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    async def test_snapshot_success(self) -> None:
        cam, tm, session = _make_camera()
        tm.async_get_access_token = AsyncMock(return_value="snap_token")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"\xff\xd8fake_jpeg")
        session.get = AsyncMock(return_value=mock_resp)

        result = await cam.async_get_snapshot()
        assert result == b"\xff\xd8fake_jpeg"

        session.get.assert_called_once_with(
            "https://api.nanit.com/babies/baby_uid_1/snapshot",
            headers={"Authorization": "snap_token"},
        )

    async def test_snapshot_returns_none_on_404(self) -> None:
        cam, tm, session = _make_camera()
        tm.async_get_access_token = AsyncMock(return_value="snap_token")

        mock_resp = AsyncMock()
        mock_resp.status = 404
        session.get = AsyncMock(return_value=mock_resp)

        result = await cam.async_get_snapshot()
        assert result is None

    async def test_snapshot_returns_none_on_exception(self) -> None:
        cam, tm, session = _make_camera()
        tm.async_get_access_token = AsyncMock(
            side_effect=aiohttp.ClientError("network error")
        )

        result = await cam.async_get_snapshot()
        assert result is None


# ---------------------------------------------------------------------------
# Lifecycle — async_stop
# ---------------------------------------------------------------------------


class TestAsyncStop:
    async def test_stop_cancels_probe_and_pending(self) -> None:
        cam, *_ = _make_camera()
        cam._transport = MagicMock()
        cam._transport.async_close = AsyncMock()

        req_id = cam._pending.next_id()
        future = cam._pending.track(req_id)

        await cam.async_stop()

        assert cam._stopped is True
        assert future.done()
        cam._transport.async_close.assert_awaited_once()
