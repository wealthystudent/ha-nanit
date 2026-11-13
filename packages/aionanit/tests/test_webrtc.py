"""Tests for the camera's own WebRTC signaling on NanitCamera."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from aionanit.auth import TokenManager
from aionanit.camera import NanitCamera
from aionanit.exceptions import (
    NanitCameraUnavailable,
    NanitRequestTimeout,
    NanitTransportError,
    NanitWebRtcClosed,
    NanitWebRtcRefused,
)
from aionanit.models import ConnectionState, TransportKind, WebRtcCandidate, WebRtcClosed
from aionanit.proto import (
    Message,
    MessageType,
    Request,
    RequestType,
    Response,
    WebRtc,
    WebRtcClose,
    WebRtcError,
    WebRtcIceCandidate,
    WebRtcSdpType,
    WebRtcSessionDescription,
)
from aionanit.rest import NanitRestClient
from aionanit.ws.protocol import decode_message

SID = 4611731400430051336  # the size of number Chrome puts in its o= line
OFFER = "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n"
ANSWER = "v=0\r\no=str0m-0.17.0 1 2 IN IP4 0.0.0.0\r\n"


def _camera_request(webrtc: Any, request_id: int = 900) -> bytes:
    """A type-50 request as the camera sends it."""
    req = Request(id=request_id, type=RequestType.PUT_WEBRTC_SERVICE_MESSAGE, webrtc=webrtc)
    return Message(type=MessageType.REQUEST, request=req).SerializeToString()


def _answer(session_id: int = SID, sdp: str = ANSWER) -> bytes:
    return _camera_request(
        WebRtc(
            session_description=WebRtcSessionDescription(
                session_id=session_id, sdp_type=WebRtcSdpType.ANSWER, description=sdp
            )
        )
    )


def _close(session_id: int = SID, error: int = WebRtcError.OK, message: str = "") -> bytes:
    return _camera_request(
        WebRtc(close=WebRtcClose(session_id=session_id, error=error, error_message=message))
    )


def _candidate(session_id: int = SID, **kwargs: Any) -> bytes:
    return _camera_request(
        WebRtc(ice_candidate=WebRtcIceCandidate(session_id=session_id, **kwargs))
    )


class FakeCamera:
    """Plays the camera's side of the control socket.

    ``ack`` is the status for each type-50 request (None sends no ack), and
    ``after_offer`` lists raw frames the camera sends once it has acked an
    offer, in order. ``before_ack`` frames go out before the ack.
    """

    def __init__(self) -> None:
        self.cam, self.tm = _make_camera()
        self.sent: list[Request] = []
        self.ack: int | None = 200
        self.ack_message = ""
        self.before_ack: list[bytes] = []
        self.after_offer: list[bytes] = [_answer()]
        self.cam._transport = MagicMock()
        self.cam._transport.connected = True
        self.cam._transport.idle_seconds = 0.0
        self.cam._transport.transport_kind = TransportKind.CLOUD
        self.cam._transport.async_send = AsyncMock(side_effect=self._send)
        self.cam._async_reconnect = AsyncMock()
        # The state re-sync after a reconnect is not under test here.
        self.cam._async_on_reconnected = AsyncMock()

    async def _send(self, data: bytes) -> None:
        msg = decode_message(data)
        assert msg.type == MessageType.REQUEST
        req = msg.request
        self.sent.append(req)
        loop = asyncio.get_running_loop()
        frames: list[bytes] = list(self.before_ack)
        if self.ack is not None:
            frames.append(
                Message(
                    type=MessageType.RESPONSE,
                    response=Response(
                        request_id=req.id,
                        request_type=req.type,
                        status_code=self.ack,
                        status_message=self.ack_message,
                    ),
                ).SerializeToString()
            )
        if self.ack == 200 and req.webrtc.WhichOneof("command") == "session_description":
            frames.extend(self.after_offer)
        for frame in frames:
            loop.call_soon(self.cam._on_ws_message, frame)

    def commands(self) -> list[str]:
        return [
            str(r.webrtc.WhichOneof("command"))
            for r in self.sent
            if r.type == RequestType.PUT_WEBRTC_SERVICE_MESSAGE
        ]


def _make_camera() -> tuple[NanitCamera, MagicMock]:
    session = MagicMock(spec=aiohttp.ClientSession)
    tm = MagicMock(spec=TokenManager)
    tm.async_get_access_token = AsyncMock(return_value="test_token")
    tm.expires_in = 3600.0
    cam = NanitCamera(
        uid="cam_uid_1",
        baby_uid="baby_uid_1",
        token_manager=tm,
        rest_client=MagicMock(spec=NanitRestClient),
        session=session,
        prefer_local=False,
    )
    return cam, tm


def _collector() -> tuple[list[Any], Callable[[Any], None]]:
    events: list[Any] = []
    return events, events.append


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestOffer:
    async def test_sends_offer_and_returns_answer(self) -> None:
        fake = FakeCamera()
        events, on_event = _collector()

        answer = await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        assert answer == ANSWER
        assert len(fake.sent) == 1  # nothing acks the camera's answer
        req = fake.sent[0]
        assert req.type == RequestType.PUT_WEBRTC_SERVICE_MESSAGE
        desc = req.webrtc.session_description
        assert desc.session_id == SID
        assert desc.sdp_type == WebRtcSdpType.OFFER
        assert desc.description == OFFER
        assert events == []
        assert SID in fake.cam._webrtc_sessions

    async def test_answer_before_ack(self) -> None:
        fake = FakeCamera()
        fake.before_ack = [_answer()]
        fake.after_offer = []
        _, on_event = _collector()

        assert await fake.cam.async_webrtc_offer(SID, OFFER, on_event) == ANSWER

    @pytest.mark.parametrize(("status", "message"), [(501, "Not implemented"), (403, "limit")])
    async def test_non_200_ack_without_an_answer_is_refused(
        self, status: int, message: str
    ) -> None:
        fake = FakeCamera()
        fake.ack = status
        fake.ack_message = message
        _, on_event = _collector()

        with pytest.raises(NanitWebRtcRefused) as err:
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05)

        assert err.value.status_code == status
        assert err.value.status_message == message
        await _settle()
        # In case the camera did take the session after all.
        assert fake.commands() == ["session_description", "close"]
        assert fake.cam._webrtc_sessions == {}

    async def test_non_200_ack_followed_by_the_answer_was_foreign(self) -> None:
        """Another app's type-50 ack sharing our id; the answer shows ours was accepted."""
        fake = FakeCamera()
        fake.ack = 404
        fake.after_offer = []
        orig = fake._send

        async def send(data: bytes) -> None:
            await orig(data)
            if fake.sent[-1].webrtc.WhichOneof("command") == "session_description":
                asyncio.get_running_loop().call_later(0.01, fake.cam._on_ws_message, _answer())

        fake.cam._transport.async_send = AsyncMock(side_effect=send)
        _, on_event = _collector()

        assert await fake.cam.async_webrtc_offer(SID, OFFER, on_event) == ANSWER
        assert fake.commands() == ["session_description"]

    async def test_no_answer_times_out_and_closes(self) -> None:
        fake = FakeCamera()
        fake.after_offer = []
        _, on_event = _collector()

        with pytest.raises(NanitRequestTimeout):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05)

        await _settle()
        assert fake.commands() == ["session_description", "close"]
        assert fake.sent[1].webrtc.close.session_id == SID
        assert fake.cam._webrtc_sessions == {}

    async def test_no_ack_times_out_without_reconnecting(self) -> None:
        fake = FakeCamera()
        fake.ack = None
        _, on_event = _collector()

        with pytest.raises(NanitRequestTimeout):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05)

        fake.cam._async_reconnect.assert_not_awaited()
        await _settle()
        # No retry of the offer; the camera may still have it, so a close follows.
        assert fake.commands() == ["session_description", "close"]
        fake.cam._transport.async_close = AsyncMock()
        await fake.cam.async_stop()  # ends the close still waiting on its ack

    async def test_send_failure_does_not_reconnect(self) -> None:
        fake = FakeCamera()
        fake.cam._transport.async_send = AsyncMock(side_effect=NanitTransportError("boom"))
        _, on_event = _collector()

        with pytest.raises(NanitTransportError):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        fake.cam._async_reconnect.assert_not_awaited()
        assert fake.cam._webrtc_sessions == {}

    async def test_answer_for_other_session_is_ignored(self) -> None:
        fake = FakeCamera()
        fake.after_offer = [_answer(session_id=SID + 1, sdp="other"), _answer()]
        _, on_event = _collector()

        assert await fake.cam.async_webrtc_offer(SID, OFFER, on_event) == ANSWER

    async def test_offer_type_from_camera_is_not_an_answer(self) -> None:
        fake = FakeCamera()
        fake.after_offer = [
            _camera_request(
                WebRtc(
                    session_description=WebRtcSessionDescription(
                        session_id=SID, sdp_type=WebRtcSdpType.OFFER, description="x"
                    )
                )
            )
        ]
        _, on_event = _collector()

        with pytest.raises(NanitRequestTimeout):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05)

    async def test_camera_close_before_answer(self) -> None:
        fake = FakeCamera()
        fake.after_offer = [_close(error=WebRtcError.INVALID_SDP, message="bad sdp")]
        events, on_event = _collector()

        with pytest.raises(NanitWebRtcClosed) as err:
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        assert err.value.error == WebRtcError.INVALID_SDP
        assert err.value.error_message == "bad sdp"
        await _settle()
        assert fake.commands() == ["session_description"]  # the camera already closed it
        assert events == []

    @pytest.mark.parametrize("session_id", [0, -1, 2**63])
    async def test_session_id_out_of_range(self, session_id: int) -> None:
        fake = FakeCamera()
        _, on_event = _collector()

        with pytest.raises(ValueError, match="out of range"):
            await fake.cam.async_webrtc_offer(session_id, OFFER, on_event)
        assert fake.sent == []

    async def test_largest_int64_is_accepted(self) -> None:
        fake = FakeCamera()
        fake.after_offer = [_answer(session_id=2**63 - 1)]
        _, on_event = _collector()

        assert await fake.cam.async_webrtc_offer(2**63 - 1, OFFER, on_event) == ANSWER

    async def test_duplicate_session_id(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        with pytest.raises(ValueError, match="already open"):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event)


class TestCameraMessages:
    async def test_trickled_candidate_reaches_session(self) -> None:
        fake = FakeCamera()
        events, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        fake.cam._on_ws_message(_candidate(sdp="candidate:1 1 udp 1 192.168.1.107 1935 typ host"))
        fake.cam._on_ws_message(_candidate(sdp="candidate:2", sdp_mid="1", sdp_m_line_index=1))
        fake.cam._on_ws_message(_candidate(session_id=SID + 1, sdp="candidate:3"))

        assert events == [
            WebRtcCandidate(SID, "candidate:1 1 udp 1 192.168.1.107 1935 typ host", None, None),
            WebRtcCandidate(SID, "candidate:2", "1", 1),
        ]

    async def test_camera_close_after_answer(self) -> None:
        fake = FakeCamera()
        events, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        fake.cam._on_ws_message(_close(error=WebRtcError.INTERNAL_ERROR, message="gone"))
        fake.cam._on_ws_message(_candidate(sdp="candidate:late"))

        assert events == [WebRtcClosed(SID, WebRtcError.INTERNAL_ERROR, "gone")]
        assert fake.cam._webrtc_sessions == {}

    async def test_messages_for_unknown_sessions_are_ignored(self) -> None:
        fake = FakeCamera()
        # Copies of another app's session, as the relay sends them.
        fake.cam._on_ws_message(_answer())
        fake.cam._on_ws_message(_candidate(sdp="candidate:1"))
        fake.cam._on_ws_message(_close())
        assert fake.sent == []

    async def test_repeated_answer_is_ignored(self) -> None:
        fake = FakeCamera()
        events, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        fake.cam._on_ws_message(_answer(sdp="again"))

        assert events == []
        assert len(fake.sent) == 1

    async def test_callback_error_does_not_propagate(self) -> None:
        fake = FakeCamera()

        def boom(_: Any) -> None:
            raise RuntimeError("subscriber bug")

        await fake.cam.async_webrtc_offer(SID, OFFER, boom)
        fake.cam._on_ws_message(_candidate(sdp="candidate:1"))  # does not raise


class TestViewerCandidates:
    async def test_candidate_is_sent_after_the_offer_is_accepted(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await asyncio.sleep(0)  # the session is registered, the ack not yet in
        candidate = asyncio.create_task(fake.cam.async_webrtc_candidate(SID, "candidate:9", "0", 0))
        await asyncio.gather(offer, candidate)

        assert fake.commands() == ["session_description", "ice_candidate"]
        ice = fake.sent[1].webrtc.ice_candidate
        assert (ice.session_id, ice.sdp, ice.sdp_mid, ice.sdp_m_line_index) == (
            SID,
            "candidate:9",
            "0",
            0,
        )

    async def test_candidate_leaves_mid_and_index_unset_when_none(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        await fake.cam.async_webrtc_candidate(SID, "candidate:9")

        ice = fake.sent[1].webrtc.ice_candidate
        assert not ice.HasField("sdp_mid")
        assert not ice.HasField("sdp_m_line_index")

    async def test_candidate_for_unknown_session_is_dropped(self) -> None:
        fake = FakeCamera()
        await fake.cam.async_webrtc_candidate(SID, "candidate:9")
        assert fake.sent == []

    async def test_candidate_waiting_on_a_refused_offer_is_dropped(self) -> None:
        fake = FakeCamera()
        fake.ack = 501
        _, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05))
        await asyncio.sleep(0)
        candidate = asyncio.create_task(fake.cam.async_webrtc_candidate(SID, "candidate:9"))
        with pytest.raises(NanitWebRtcRefused):
            await offer
        await asyncio.wait_for(candidate, 1)

        # No candidate; the close is in case the camera took the session.
        assert fake.commands() == ["session_description", "close"]

    async def test_candidate_send_does_not_reconnect(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        fake.ack = None

        with pytest.raises(NanitRequestTimeout):
            await fake.cam.async_webrtc_candidate(SID, "candidate:9")
        fake.cam._async_reconnect.assert_not_awaited()


class TestClose:
    async def test_close_sends_once(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        await fake.cam.async_webrtc_close(SID)
        await fake.cam.async_webrtc_close(SID)

        assert fake.commands() == ["session_description", "close"]
        assert fake.sent[1].webrtc.close.session_id == SID
        assert fake.cam._webrtc_sessions == {}

    async def test_close_errors_are_swallowed(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        fake.ack = None

        await fake.cam.async_webrtc_close(SID)  # ack never comes; no raise

        fake.cam._async_reconnect.assert_not_awaited()

    async def test_close_while_offer_pending(self) -> None:
        fake = FakeCamera()
        fake.after_offer = []
        _, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()  # acked, waiting for the answer
        await fake.cam.async_webrtc_close(SID)

        with pytest.raises(NanitWebRtcClosed):
            await offer
        await _settle()
        assert fake.commands() == ["session_description", "close"]  # not two closes

    async def test_close_skipped_when_disconnected(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        fake.cam._transport.connected = False

        await fake.cam.async_webrtc_close(SID)

        assert fake.commands() == ["session_description"]
        assert fake.cam._webrtc_sessions == {}


class TestConnectionLoss:
    @pytest.mark.parametrize(
        "state",
        [ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED, ConnectionState.CONNECTING],
    )
    async def test_sessions_outlive_the_socket(self, state: ConnectionState) -> None:
        """Sessions keep streaming without the socket (seen live), so they stay."""
        fake = FakeCamera()
        events, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        fake.cam._on_connection_change(state, TransportKind.CLOUD, None)
        fake.cam._on_connection_change(ConnectionState.CONNECTED, TransportKind.CLOUD, None)
        # The camera's later messages arrive over the next socket.
        fake.cam._on_ws_message(_candidate(sdp="candidate:after"))
        await fake.cam.async_webrtc_close(SID)

        assert events == [WebRtcCandidate(SID, "candidate:after", None, None)]
        assert fake.commands() == ["session_description", "close"]

    async def test_offer_waiting_on_its_ack_fails(self) -> None:
        fake = FakeCamera()
        fake.ack = None
        events, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()
        fake.cam._on_connection_change(ConnectionState.RECONNECTING, TransportKind.CLOUD, None)

        with pytest.raises(NanitTransportError):
            await offer
        assert events == []
        fake.cam._transport.async_close = AsyncMock()
        await fake.cam.async_stop()

    async def test_answer_after_a_reconnect_still_counts(self) -> None:
        fake = FakeCamera()
        fake.after_offer = []
        _, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()  # acked, waiting for the answer
        fake.cam._on_connection_change(ConnectionState.RECONNECTING, TransportKind.CLOUD, None)
        fake.cam._on_connection_change(ConnectionState.CONNECTED, TransportKind.CLOUD, None)
        fake.cam._on_ws_message(_answer())

        assert await offer == ANSWER

    async def test_candidate_dropped_while_disconnected(self) -> None:
        fake = FakeCamera()
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        fake.cam._transport.connected = False

        await asyncio.wait_for(fake.cam.async_webrtc_candidate(SID, "candidate:9"), 1)

        assert fake.commands() == ["session_description"]
        fake.cam._async_reconnect.assert_not_awaited()

    async def test_stop_ends_sessions(self) -> None:
        fake = FakeCamera()
        fake.cam._transport.async_close = AsyncMock()
        events, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)

        await fake.cam.async_stop()

        assert [type(e) for e in events] == [WebRtcClosed]
        assert fake.commands() == ["session_description"]


class TestForeignAck:
    async def test_foreign_response_on_our_id_does_not_refuse(self) -> None:
        """Another app's response sharing our request id must not read as a refusal."""
        fake = FakeCamera()
        fake.ack = None
        orig = fake._send

        async def send(data: bytes) -> None:
            await orig(data)
            req = fake.sent[-1]
            if req.webrtc.WhichOneof("command") != "session_description":
                return
            loop = asyncio.get_running_loop()
            foreign = Message(
                type=MessageType.RESPONSE,
                response=Response(
                    request_id=req.id,
                    request_type=RequestType.GET_STATUS,
                    status_code=501,
                ),
            ).SerializeToString()
            loop.call_soon(fake.cam._on_ws_message, foreign)
            loop.call_soon(fake.cam._on_ws_message, _answer())

        fake.cam._transport.async_send = AsyncMock(side_effect=send)
        _, on_event = _collector()

        assert await fake.cam.async_webrtc_offer(SID, OFFER, on_event) == ANSWER


class TestAdversarial:
    async def test_offer_waiting_for_the_socket_is_not_sent_after_close(self) -> None:
        """R1: the view closes while the offer waits for a reconnect."""
        fake = FakeCamera()
        fake.cam._transport.connected = False
        fake.cam._connected_event.clear()
        _, on_event = _collector()

        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()
        await fake.cam.async_webrtc_close(SID)  # dropped: socket is down
        fake.cam._transport.connected = True
        fake.cam._connected_event.set()
        with pytest.raises(NanitWebRtcClosed):
            await offer
        await _settle()
        # Either the offer is not sent, or a close follows it.
        cmds = fake.commands()
        assert cmds in ([], ["session_description", "close"]), cmds

    async def test_close_right_after_answer_reaches_on_event_before_return(self) -> None:
        """R2 (library half): the close is emitted before the answer is returned."""
        fake = FakeCamera()
        fake.after_offer = [_answer(), _close(error=WebRtcError.INTERNAL_ERROR, message="x")]
        events, on_event = _collector()
        answer = await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        assert answer == ANSWER
        assert events == [WebRtcClosed(SID, WebRtcError.INTERNAL_ERROR, "x")]

    async def test_stop_fails_an_offer_waiting_for_its_answer(self) -> None:
        fake = FakeCamera()
        fake.after_offer = []
        fake.cam._transport.async_close = AsyncMock()
        _, on_event = _collector()
        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()

        await fake.cam.async_stop()

        with pytest.raises(NanitTransportError):
            await asyncio.wait_for(offer, 1)

    async def test_any_non_200_ack_is_refused(self) -> None:
        fake = FakeCamera()
        fake.ack = 201
        _, on_event = _collector()
        with pytest.raises(NanitWebRtcRefused):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.05)

    async def test_close_early_then_ack_timeout_sends_close(self) -> None:
        fake = FakeCamera()
        fake.ack = None
        _, on_event = _collector()
        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event, timeout=0.2))
        await asyncio.sleep(0)
        await fake.cam.async_webrtc_close(SID)
        with pytest.raises((NanitRequestTimeout, NanitWebRtcClosed)):
            await offer
        await _settle()
        assert fake.commands() == ["session_description", "close"]
        fake.cam._transport.async_close = AsyncMock()
        await fake.cam.async_stop()

    async def test_stop_while_waiting_for_the_ack_is_not_a_cancellation(self) -> None:
        fake = FakeCamera()
        fake.ack = None
        fake.cam._transport.async_close = AsyncMock()
        _, on_event = _collector()
        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()

        await fake.cam.async_stop()

        with pytest.raises(NanitCameraUnavailable):
            await asyncio.wait_for(offer, 1)

    async def test_close_before_the_ack_sends_exactly_one_close(self) -> None:
        fake = FakeCamera()
        fake.after_offer = []
        _, on_event = _collector()
        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await asyncio.sleep(0)  # registered and sent, ack not yet handled
        assert not fake.cam._webrtc_sessions[SID].settled.is_set()

        await fake.cam.async_webrtc_close(SID)

        with pytest.raises(NanitWebRtcClosed):
            await offer
        await _settle()
        assert fake.commands() == ["session_description", "close"]

    async def test_camera_close_before_the_answer_sends_no_close(self) -> None:
        fake = FakeCamera()
        fake.after_offer = [_close(error=WebRtcError.INVALID_SDP)]
        _, on_event = _collector()
        with pytest.raises(NanitWebRtcClosed):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        await _settle()
        assert fake.commands() == ["session_description"]


def _run_sync(coro: Any) -> None:
    """Run a coroutine that finishes without awaiting, right now."""
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("coroutine awaited")


class TestRaces:
    async def test_close_between_the_answer_and_the_resume(self) -> None:
        """A view closed after the answer arrived but before the offer call resumed.

        Only possible after a non-200 ack: a 200 ack sets ``settled`` before the
        answer wait, and the close then tells the camera itself.
        """
        fake = FakeCamera()
        fake.ack = 404
        fake.after_offer = []
        orig = fake._send

        def deliver() -> None:
            fake.cam._on_ws_message(_answer())
            _run_sync(fake.cam.async_webrtc_close(SID))

        async def send(data: bytes) -> None:
            await orig(data)
            if fake.sent[-1].webrtc.WhichOneof("command") == "session_description":
                asyncio.get_running_loop().call_later(0.01, deliver)

        fake.cam._transport.async_send = AsyncMock(side_effect=send)
        _, on_event = _collector()

        with contextlib.suppress(NanitWebRtcClosed):
            await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        await _settle()

        assert fake.commands() == ["session_description", "close"]

    async def test_candidates_flow_after_a_foreign_non_200_ack(self) -> None:
        fake = FakeCamera()
        fake.ack = 404
        fake.after_offer = []
        orig = fake._send

        async def send(data: bytes) -> None:
            await orig(data)
            if fake.sent[-1].webrtc.WhichOneof("command") == "session_description":
                asyncio.get_running_loop().call_later(0.01, fake.cam._on_ws_message, _answer())

        fake.cam._transport.async_send = AsyncMock(side_effect=send)
        _, on_event = _collector()
        await fake.cam.async_webrtc_offer(SID, OFFER, on_event)
        fake.ack = 200

        await asyncio.wait_for(fake.cam.async_webrtc_candidate(SID, "candidate:9"), 1)

        assert fake.commands() == ["session_description", "ice_candidate"]

    async def test_a_real_cancellation_during_stop_still_propagates(self) -> None:
        fake = FakeCamera()
        fake.ack = None
        fake.cam._transport.async_close = AsyncMock()
        _, on_event = _collector()
        offer = asyncio.create_task(fake.cam.async_webrtc_offer(SID, OFFER, on_event))
        await _settle()

        offer.cancel()
        await fake.cam.async_stop()

        with pytest.raises(asyncio.CancelledError):
            await offer
