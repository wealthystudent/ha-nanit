"""Tests for playback wire format — investigating why tracks stop after ~1 minute.

Field 2 in the Playback proto is a duration (int32, seconds), not a loop boolean.
Setting duration=1 causes 1-second playback; omitting it plays the full track once.
Still probing whether duration=0 means infinite or if looping requires a different field.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aionanit.camera import NanitCamera
from aionanit.models import CameraEventKind, PlaybackState
from aionanit.proto import (
    Message,
    MessageType,
    Playback,
    Request,
    RequestType,
    Response,
)
from aionanit.ws.protocol import build_request, decode_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_camera() -> NanitCamera:
    """Create a NanitCamera with mocked transport for wire-format inspection."""
    import time

    import aiohttp

    from aionanit.auth import TokenManager
    from aionanit.rest import NanitRestClient

    session = MagicMock(spec=aiohttp.ClientSession)
    rest = MagicMock(spec=NanitRestClient)
    tm = MagicMock(spec=TokenManager)
    tm.async_get_access_token = AsyncMock(return_value="test_token")
    tm._expires_at = time.monotonic() + 3600.0

    cam = NanitCamera(
        uid="cam_uid_1",
        baby_uid="baby_uid_1",
        token_manager=tm,
        rest_client=rest,
        session=session,
    )
    return cam


def _setup_camera_transport(cam: NanitCamera) -> list[bytes]:
    """Mock the transport and return a list that captures sent bytes."""
    sent_bytes: list[bytes] = []
    cam._transport = MagicMock()
    cam._transport.connected = True
    cam._transport.idle_seconds = 0.0

    resp = Response(request_id=1, request_type=RequestType.PUT_PLAYBACK, status_code=200)

    async def _fake_send(data: bytes) -> None:
        sent_bytes.append(data)
        cam._pending.resolve(1, resp)

    cam._transport.async_send = AsyncMock(side_effect=_fake_send)
    return sent_bytes


def _decode_playback_from_sent(data: bytes) -> Playback:
    """Decode the Playback message from raw bytes sent over WebSocket."""
    msg = decode_message(data)
    assert msg.type == MessageType.REQUEST
    assert msg.request.type == RequestType.PUT_PLAYBACK
    assert msg.request.HasField("playback")
    return msg.request.playback


# ---------------------------------------------------------------------------
# Tests — Current behavior (what we send now)
# ---------------------------------------------------------------------------


class TestCurrentPlaybackWireFormat:
    """Verify exactly what PUT_PLAYBACK bytes we currently send."""

    async def test_start_playback_sends_status_started(self) -> None:
        """Basic start sends status=STARTED (0) with no extra fields."""
        cam = _make_camera()
        sent = _setup_camera_transport(cam)

        await cam.async_start_playback()

        assert len(sent) == 1
        playback = _decode_playback_from_sent(sent[0])
        assert playback.status == Playback.STARTED

    async def test_start_playback_with_track_sends_track_submessage(self) -> None:
        """Start with track sends Soundtrack sub-message on field 3."""
        cam = _make_camera()
        sent = _setup_camera_transport(cam)

        await cam.async_start_playback(track="Birds.wav")

        assert len(sent) == 1
        playback = _decode_playback_from_sent(sent[0])
        assert playback.status == Playback.STARTED
        assert playback.HasField("track")
        assert playback.track.filename == "Birds.wav"
        assert playback.track.type == 0

    async def test_start_playback_sends_default_duration(self) -> None:
        cam = _make_camera()
        sent = _setup_camera_transport(cam)

        await cam.async_start_playback(track="White Noise.wav")

        playback = _decode_playback_from_sent(sent[0])
        assert playback.HasField("duration")
        assert playback.duration == 86400

        raw = playback.SerializeToString()
        field_numbers = _extract_field_numbers(raw)
        assert 2 in field_numbers

    async def test_stop_playback_sends_status_stopped(self) -> None:
        """Stop sends status=STOPPED (1)."""
        cam = _make_camera()
        sent = _setup_camera_transport(cam)

        await cam.async_stop_playback()

        assert len(sent) == 1
        playback = _decode_playback_from_sent(sent[0])
        assert playback.status == Playback.STOPPED


# ---------------------------------------------------------------------------
# Tests — Optimistic state update
# ---------------------------------------------------------------------------


class TestPlaybackStateUpdate:
    """Verify state is updated optimistically after PUT_PLAYBACK."""

    async def test_start_sets_playing_true(self) -> None:
        cam = _make_camera()
        _setup_camera_transport(cam)

        result = await cam.async_start_playback()
        assert result.playing is True
        assert cam.state.playback.playing is True

    async def test_start_with_track_updates_current_track(self) -> None:
        cam = _make_camera()
        _setup_camera_transport(cam)

        result = await cam.async_start_playback(track="Waves.wav")
        assert result.current_track == "Waves.wav"
        assert cam.state.playback.current_track == "Waves.wav"

    async def test_stop_sets_playing_false(self) -> None:
        cam = _make_camera()
        cam._transport = MagicMock()
        cam._transport.connected = True
        cam._transport.idle_seconds = 0.0

        request_counter = 0

        async def _fake_send(data: bytes) -> None:
            nonlocal request_counter
            request_counter += 1
            resp = Response(
                request_id=request_counter,
                request_type=RequestType.PUT_PLAYBACK,
                status_code=200,
            )
            cam._pending.resolve(request_counter, resp)

        cam._transport.async_send = AsyncMock(side_effect=_fake_send)

        await cam.async_start_playback(track="Wind.wav")
        result = await cam.async_stop_playback()
        assert result.playing is False
        assert cam.state.playback.playing is False


# ---------------------------------------------------------------------------
# Tests — Probe field 2 (loop/repeat hypothesis)
# ---------------------------------------------------------------------------


class TestPlaybackField2Duration:
    """Field 2 is int32 duration (seconds). Verify wire format correctness."""

    def test_raw_playback_with_field2_varint_1_serializes(self) -> None:
        raw_playback = (
            _encode_varint((1 << 3) | 0)  # field 1 (status), wire type 0
            + _encode_varint(0)  # STARTED = 0
            + _encode_varint((2 << 3) | 0)  # field 2 (duration), wire type 0
            + _encode_varint(1)  # value = 1 second
        )

        pb = Playback()
        pb.ParseFromString(raw_playback)
        assert pb.status == Playback.STARTED
        assert pb.duration == 1

        reserialized = pb.SerializeToString()
        field_numbers = _extract_field_numbers(reserialized)
        assert 1 in field_numbers
        assert 2 in field_numbers

    def test_raw_playback_with_field2_and_track(self) -> None:
        filename = b"Birds.wav"
        soundtrack_bytes = (
            _encode_varint((1 << 3) | 0)  # field 1 (type), varint
            + _encode_varint(0)  # type = 0
            + _encode_varint((2 << 3) | 2)  # field 2 (filename), length-delimited
            + _encode_varint(len(filename))
            + filename
        )

        raw_playback = (
            _encode_varint((1 << 3) | 0)  # field 1 (status), varint
            + _encode_varint(0)  # STARTED
            + _encode_varint((2 << 3) | 0)  # field 2 (duration), varint
            + _encode_varint(3600)  # 1 hour
            + _encode_varint((3 << 3) | 2)  # field 3 (track), length-delimited
            + _encode_varint(len(soundtrack_bytes))
            + soundtrack_bytes
        )

        pb = Playback()
        pb.ParseFromString(raw_playback)
        assert pb.status == Playback.STARTED
        assert pb.duration == 3600
        assert pb.track.filename == "Birds.wav"
        assert pb.track.type == 0

        reserialized = pb.SerializeToString()
        assert 2 in _extract_field_numbers(reserialized)

    def test_build_request_with_duration_field_produces_valid_message(self) -> None:
        playback = Playback(status=Playback.STARTED, duration=3600)
        playback.track.type = 0
        playback.track.filename = "White Noise.wav"

        data = build_request(1, RequestType.PUT_PLAYBACK, playback=playback)
        msg = decode_message(data)

        assert msg.request.playback.status == Playback.STARTED
        assert msg.request.playback.duration == 3600
        assert msg.request.playback.track.filename == "White Noise.wav"

        pb_raw = msg.request.playback.SerializeToString()
        field_numbers = _extract_field_numbers(pb_raw)
        assert 2 in field_numbers


# ---------------------------------------------------------------------------
# Tests — Push event: camera sends PUT_PLAYBACK with status=STOPPED
# ---------------------------------------------------------------------------


class TestCameraPushPlaybackStopped:
    """Verify that when the camera pushes a PUT_PLAYBACK STOPPED event,
    our state correctly reflects that playback has stopped.

    This confirms the camera DOES send a stop event (rather than just going
    silent), which means it's the camera actively stopping, not a connection
    issue.
    """

    def test_push_put_playback_stopped_updates_state(self) -> None:
        """Simulate camera pushing PUT_PLAYBACK { status: STOPPED }."""
        cam = _make_camera()
        events: list[object] = []
        cam.subscribe(lambda e: events.append(e))

        # Simulate push: camera says playback stopped
        req = Request(
            id=99,
            type=RequestType.PUT_PLAYBACK,
            playback=Playback(status=Playback.STOPPED),
        )
        msg = Message(type=MessageType.REQUEST, request=req)
        cam._on_ws_message(msg.SerializeToString())

        assert cam.state.playback.playing is False
        assert any(e.kind == CameraEventKind.PLAYBACK_UPDATE for e in events)

    def test_push_put_playback_started_updates_state(self) -> None:
        """Simulate camera pushing PUT_PLAYBACK { status: STARTED }."""
        cam = _make_camera()

        req = Request(
            id=100,
            type=RequestType.PUT_PLAYBACK,
            playback=Playback(status=Playback.STARTED),
        )
        msg = Message(type=MessageType.REQUEST, request=req)
        cam._on_ws_message(msg.SerializeToString())

        assert cam.state.playback.playing is True

    def test_push_playback_stopped_with_current_track(self) -> None:
        """Camera reports stop with current track info."""
        cam = _make_camera()

        pb = Playback(status=Playback.STOPPED)
        pb.current.type = 0
        pb.current.filename = "White Noise.wav"

        req = Request(id=101, type=RequestType.PUT_PLAYBACK, playback=pb)
        msg = Message(type=MessageType.REQUEST, request=req)
        cam._on_ws_message(msg.SerializeToString())

        assert cam.state.playback.playing is False
        assert cam.state.playback.current_track == "White Noise.wav"


# ---------------------------------------------------------------------------
# Tests — Playback poll detects external stop
# ---------------------------------------------------------------------------


class TestPlaybackPollDetectsStop:
    """Verify that the 30s playback poll detects when the camera stopped."""

    async def test_get_playback_updates_state_to_stopped(self) -> None:
        """GET_PLAYBACK response with status=STOPPED updates state."""
        cam = _make_camera()
        cam._transport = MagicMock()
        cam._transport.connected = True
        cam._transport.idle_seconds = 0.0

        # First set playing=True
        cam._update_state(
            playback=PlaybackState(playing=True, current_track="Birds.wav"),
            kind=CameraEventKind.PLAYBACK_UPDATE,
        )
        assert cam.state.playback.playing is True

        # Simulate GET_PLAYBACK response saying STOPPED
        resp = Response(
            request_id=1,
            request_type=RequestType.GET_PLAYBACK,
            status_code=200,
            playback=Playback(status=Playback.STOPPED),
        )

        async def _fake_send(data: bytes) -> None:
            cam._pending.resolve(1, resp)

        cam._transport.async_send = AsyncMock(side_effect=_fake_send)

        result = await cam.async_get_playback()
        assert result.playing is False
        assert cam.state.playback.playing is False

    async def test_get_playback_preserves_available_tracks(self) -> None:
        """GET_PLAYBACK response doesn't include tracks — preserved from state."""
        cam = _make_camera()
        cam._transport = MagicMock()
        cam._transport.connected = True
        cam._transport.idle_seconds = 0.0

        # Set state with available tracks
        cam._update_state(
            playback=PlaybackState(
                playing=True,
                current_track="Waves.wav",
                available_tracks=("Birds.wav", "Waves.wav", "White Noise.wav"),
            ),
            kind=CameraEventKind.PLAYBACK_UPDATE,
        )

        # GET_PLAYBACK response won't include available_tracks
        resp = Response(
            request_id=1,
            request_type=RequestType.GET_PLAYBACK,
            status_code=200,
            playback=Playback(status=Playback.STARTED),
        )

        async def _fake_send(data: bytes) -> None:
            cam._pending.resolve(1, resp)

        cam._transport.async_send = AsyncMock(side_effect=_fake_send)

        result = await cam.async_get_playback()
        assert result.playing is True
        assert result.available_tracks == ("Birds.wav", "Waves.wav", "White Noise.wav")


# ---------------------------------------------------------------------------
# Utility — wire format parsing
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _extract_field_numbers(data: bytes) -> set[int]:
    """Extract all field numbers from serialized protobuf bytes.

    This is a simple wire-format scanner — only extracts top-level field numbers.
    """
    field_numbers: set[int] = set()
    pos = 0
    while pos < len(data):
        # Decode tag varint
        tag = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            tag |= (b & 0x7F) << shift
            shift += 7
            pos += 1
            if not (b & 0x80):
                break

        field_number = tag >> 3
        wire_type = tag & 0x07
        field_numbers.add(field_number)

        # Skip value based on wire type
        if wire_type == 0:  # varint
            while pos < len(data) and data[pos] & 0x80:
                pos += 1
            pos += 1  # final byte
        elif wire_type == 1:  # 64-bit fixed
            pos += 8
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                length |= (b & 0x7F) << shift
                shift += 7
                pos += 1
                if not (b & 0x80):
                    break
            pos += length
        elif wire_type == 5:  # 32-bit fixed
            pos += 4
        else:
            break  # unknown wire type

    return field_numbers
