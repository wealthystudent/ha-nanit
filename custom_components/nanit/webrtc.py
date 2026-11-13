"""The camera's own WebRTC stream, offered to Home Assistant's frontend.

Cameras on recent firmware answer a WebRTC offer sent over the control
socket the integration already holds, and stream H.264 and Opus straight to
the browser over the LAN. This provider relays the signaling; no media passes
through Home Assistant. Anything that needs ``stream_source()`` (HLS, stills,
recording) keeps the RTMP path.

Measured against a camera on 6.61.617: sessions are never refused, but each
live one holds one of the camera's two app slots, so they can crowd out Home
Assistant's own RTMP push. Nanit Homes use this same WebRTC: they hold
slots too, and are never refused either. A session keeps streaming if the
control socket drops, and the camera ends one whose ICE never completes, or
whose viewer went away, after about 15 s.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.components.camera.webrtc import (
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import callback
from webrtc_models import RTCIceCandidateInit

from aionanit import NanitCamera
from aionanit.camera import STREAM_LIMIT_STATUS
from aionanit.exceptions import NanitError, NanitWebRtcClosed, NanitWebRtcRefused
from aionanit.models import WebRtcCandidate, WebRtcClosed

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

WEBRTC_ERROR_REFUSED = "nanit_webrtc_refused"
WEBRTC_ERROR_FAILED = "nanit_webrtc_failed"
WEBRTC_ERROR_NO_H264 = "nanit_webrtc_no_h264"

# The camera sends only H.264. Given an offer without it, the camera acks and
# then never answers, holding a stream slot until its 15 s ICE timeout. Some
# browsers have no H.264 (open-source Chromium builds, Firefox before its
# OpenH264 plugin downloads).
_H264 = re.compile(r"^a=rtpmap:\d+ H264/90000", re.IGNORECASE | re.MULTILINE)


def new_session_id(in_use: set[int]) -> int:
    """Pick a positive int64 session id for the camera.

    The camera accepts any id; the Nanit app happens to use the offer's
    origin-line number, which old frontends repeat when they restart ICE.
    """
    while True:
        session_id = secrets.randbits(63)
        if session_id and session_id not in in_use:
            return session_id


@dataclass(slots=True)
class _Session:
    camera_id: int
    send_message: WebRTCSendMessage
    answered: bool = False
    # Candidates the camera trickles before the browser has the answer.
    early: list[WebRtcCandidate] = field(default_factory=list)


class NanitWebRTCProvider(CameraWebRTCProvider):
    """Relays WebRTC signaling between the frontend and one Nanit camera.

    Not registered with Home Assistant: the camera entity sets it as its own
    provider, so no other camera can pick it.
    """

    def __init__(self, camera: NanitCamera) -> None:
        """Initialize for one camera."""
        self._camera = camera
        # Home Assistant's session id -> ours.
        self._sessions: dict[str, _Session] = {}
        self._closing: set[asyncio.Task[None]] = set()
        # A refusal is logged once; every offer is still tried, since a
        # refusal can't be told apart from another app's ack sharing our id.
        self._warned_refusal = False
        self._warned_no_h264 = False
        # Set once the provider is taken off the camera, so an offer core
        # routes here while that's under way isn't left open.
        self._unregistered = False
        # What happened to offers and sessions, for debugging.
        self.stats: dict[str, int] = {}

    @property
    def domain(self) -> str:
        """Return the integration domain of the provider."""
        return DOMAIN

    @callback
    def async_is_supported(self, stream_source: str) -> bool:
        """Support whatever the camera entity hands us; it does the choosing."""
        return True

    @property
    def session_count(self) -> int:
        """Number of sessions the frontend has open."""
        return len(self._sessions)

    async def async_handle_async_webrtc_offer(
        self,
        camera: Camera,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Pass the browser's offer to the camera and its answer back.

        Errors go to the frontend only before the answer. After it, a
        released frontend has already dropped its HLS player, and an error
        would leave it on an alert with nothing to fall back to.
        """
        if self._unregistered:
            send_message(WebRTCError(WEBRTC_ERROR_FAILED, "Camera is being removed"))
            return
        if not _H264.search(offer_sdp):
            self._count("no_h264")
            if not self._warned_no_h264:
                self._warned_no_h264 = True
                _LOGGER.info(
                    "A browser offered camera %s no H.264, the only codec it sends; "
                    "that view uses the regular stream",
                    self._camera.uid,
                )
            send_message(WebRTCError(WEBRTC_ERROR_NO_H264, "This browser has no H.264"))
            return
        in_use = {s.camera_id for s in self._sessions.values()}
        session = _Session(new_session_id(in_use), send_message)
        self._sessions[session_id] = session
        self._count("offers")
        started = time.monotonic()
        try:
            answer = await self._camera.async_webrtc_offer(
                session.camera_id,
                offer_sdp,
                lambda event: self._on_camera_event(session_id, session, event),
            )
        except NanitWebRtcRefused as err:
            if not self._forget(session_id, session):
                return
            self._count("refused")
            if err.status_code != STREAM_LIMIT_STATUS and not self._warned_refusal:
                # Never seen from a camera that supports this; most likely one
                # below Nanit's firmware gate.
                self._warned_refusal = True
                _LOGGER.warning(
                    "Camera %s refused its own WebRTC stream (%s); views use the regular "
                    "stream. Turn the option off if this camera doesn't support it",
                    self._camera.uid,
                    err,
                )
            send_message(WebRTCError(WEBRTC_ERROR_REFUSED, str(err)))
            return
        except NanitError as err:
            # Timeouts, transport errors, a close: tried again on the next offer.
            if not self._forget(session_id, session):
                return  # the frontend closed the view while the offer was out
            if isinstance(err, NanitWebRtcClosed) and err.error:
                self._count(f"camera_closed_error_{err.error}")
                _LOGGER.info("Camera %s ended a WebRTC view: %s", self._camera.uid, err)
            else:
                self._count("failed")
                _LOGGER.debug("WebRTC offer to camera %s failed: %s", self._camera.uid, err)
            send_message(WebRTCError(WEBRTC_ERROR_FAILED, str(err)))
            return
        except Exception as err:
            # Core passes on only HomeAssistantError, so nothing else may escape.
            self._count("failed")
            if self._forget(session_id, session):
                _LOGGER.exception("WebRTC offer to camera %s failed", self._camera.uid)
                send_message(WebRTCError(WEBRTC_ERROR_FAILED, str(err)))
            return
        except BaseException:
            self._forget(session_id, session)
            raise
        if self._sessions.get(session_id) is not session:
            return  # closed by the frontend while the offer was out
        send_message(WebRTCAnswer(answer))
        self._count("answered")
        _LOGGER.debug(
            "Camera %s answered a WebRTC view in %.2f s (%d open)",
            self._camera.uid,
            time.monotonic() - started,
            len(self._sessions),
        )
        session.answered = True
        for early in session.early:
            send_message(WebRTCCandidate(_to_ha_candidate(early)))
        session.early.clear()

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Pass one of the browser's ICE candidates to the camera.

        Core's candidate handler catches nothing, so nothing is raised.
        """
        session = self._sessions.get(session_id)
        if session is None or not candidate.candidate:
            # Unknown session, or the browser's end-of-candidates marker.
            return
        try:
            await self._camera.async_webrtc_candidate(
                session.camera_id,
                candidate.candidate,
                candidate.sdp_mid,
                candidate.sdp_m_line_index,
            )
        except NanitError as err:
            _LOGGER.debug("ICE candidate for camera %s not sent: %s", self._camera.uid, err)

    @callback
    def async_close_session(self, session_id: str) -> None:
        """Close a session the frontend is done with; unknown ids are ignored."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        _LOGGER.debug(
            "WebRTC view on camera %s closed (%d open)", self._camera.uid, len(self._sessions)
        )
        task = asyncio.get_running_loop().create_task(
            self._camera.async_webrtc_close(session.camera_id)
        )
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def async_close_all(self) -> None:
        """Close every open session (sleep, removal, unload)."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(self._camera.async_webrtc_close(s.camera_id) for s in sessions))

    @callback
    def attach(self) -> None:
        """Accept offers again, for an entity added back after a removal (a rename)."""
        self._unregistered = False

    async def async_unregister_camera(self, camera: Camera) -> None:
        """Close the camera's sessions when the provider is taken off it."""
        self._unregistered = True
        await self.async_close_all()

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def _forget(self, session_id: str, session: _Session) -> bool:
        if self._sessions.get(session_id) is session:
            del self._sessions[session_id]
            return True
        return False

    @callback
    def _on_camera_event(
        self, session_id: str, session: _Session, event: WebRtcCandidate | WebRtcClosed
    ) -> None:
        if self._sessions.get(session_id) is not session:
            return
        if isinstance(event, WebRtcCandidate):
            if session.answered:
                session.send_message(WebRTCCandidate(_to_ha_candidate(event)))
            else:
                session.early.append(event)
            return
        del self._sessions[session_id]
        if not session.answered:
            # Ended before the frontend had the answer, so it is still
            # waiting; this is the last moment an error is safe to send.
            session.send_message(
                WebRTCError(WEBRTC_ERROR_FAILED, event.error_message or "Camera ended the view")
            )
        # After the answer, the browser sees its connection drop and the
        # player starts over; an error message would stop it doing so.
        if event.error:
            # Usually "ICE checking timeout": the viewer never reached the
            # camera (away from home, or on a network that can't reach it).
            self._count(f"camera_closed_error_{event.error}")
            _LOGGER.info("Camera %s ended a WebRTC view: %s", self._camera.uid, event.error_message)
        else:
            self._count("camera_closed")
            _LOGGER.debug("Camera %s ended a WebRTC view", self._camera.uid)


def _to_ha_candidate(event: WebRtcCandidate) -> RTCIceCandidateInit:
    """Map a camera candidate; the camera leaves mid and index unset."""
    return RTCIceCandidateInit(
        event.candidate,
        sdp_mid=event.sdp_mid if event.sdp_mid is not None else "0",
        sdp_m_line_index=event.sdp_m_line_index if event.sdp_m_line_index is not None else 0,
    )
