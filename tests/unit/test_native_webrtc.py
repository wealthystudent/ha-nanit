"""Tests for the camera's own WebRTC stream (webrtc.py and its camera wiring)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from homeassistant.components.camera import Camera, StreamType
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCMessage,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from webrtc_models import RTCIceCandidateInit

from aionanit.camera import STREAM_LIMIT_STATUS
from aionanit.exceptions import (
    NanitRequestTimeout,
    NanitTransportError,
    NanitWebRtcClosed,
    NanitWebRtcRefused,
)
from aionanit.models import WebRtcCandidate, WebRtcClosed
from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import CONF_NATIVE_WEBRTC, CONF_READ_ONLY, DOMAIN
from custom_components.nanit.webrtc import (
    WEBRTC_ERROR_FAILED,
    WEBRTC_ERROR_NO_H264,
    WEBRTC_ERROR_REFUSED,
    NanitWebRTCProvider,
    new_session_id,
)

from .test_entities import _camera_state, _push_coordinator
from .test_local_stream import (  # noqa: F401  (fixtures used by name)
    _ENTITIES,
    _cancel_entity_timers,
    _cancel_pause_timers,
    _entry,
    _local_entry,
    source_ip,
)

OFFER = (
    "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96 102\r\na=rtpmap:96 VP8/90000\r\na=rtpmap:102 H264/90000\r\n"
)
ANSWER = "v=0\r\no=str0m-0.17.0 1 2 IN IP4 0.0.0.0\r\n"


class FakeCamera:
    """aionanit's WebRTC calls, scripted."""

    def __init__(self) -> None:
        self.uid = "cam_1"
        self.offers: list[tuple[int, str]] = []
        self.candidates: list[tuple[int, str, str | None, int | None]] = []
        self.closed: list[int] = []
        self.on_event: dict[int, Any] = {}
        self.answer: Any = ANSWER
        self.gate: asyncio.Event | None = None

    async def async_webrtc_offer(self, session_id: int, sdp: str, on_event: Any) -> str:
        self.offers.append((session_id, sdp))
        self.on_event[session_id] = on_event
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return cast(str, self.answer)

    async def async_webrtc_candidate(
        self, session_id: int, candidate: str, mid: str | None = None, idx: int | None = None
    ) -> None:
        self.candidates.append((session_id, candidate, mid, idx))

    async def async_webrtc_close(self, session_id: int) -> None:
        self.closed.append(session_id)


def _provider() -> tuple[NanitWebRTCProvider, FakeCamera]:
    camera = FakeCamera()
    return NanitWebRTCProvider(cast(Any, camera)), camera


async def _offer(provider: NanitWebRTCProvider, session_id: str = "ha1") -> list[WebRTCMessage]:
    sent: list[WebRTCMessage] = []
    await provider.async_handle_async_webrtc_offer(MagicMock(), OFFER, session_id, sent.append)
    return sent


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


def test_session_ids_are_random_int64_not_the_origin_line() -> None:
    ids = {new_session_id(set()) for _ in range(50)}
    assert len(ids) == 50
    assert all(0 < i < 2**63 for i in ids)
    assert 4611731400430051336 not in ids


def test_session_id_skips_ids_in_use() -> None:
    with patch("custom_components.nanit.webrtc.secrets.randbits", side_effect=[0, 7, 7, 9]):
        assert new_session_id({7}) == 9


async def test_offer_relays_the_answer() -> None:
    provider, camera = _provider()

    sent = await _offer(provider)

    assert sent == [WebRTCAnswer(ANSWER)]
    ((session_id, sdp),) = camera.offers
    assert sdp == OFFER
    assert session_id != 4611731400430051336
    assert provider.session_count == 1


async def test_two_views_get_distinct_camera_sessions() -> None:
    provider, camera = _provider()
    await _offer(provider, "ha1")
    await _offer(provider, "ha2")
    assert len({sid for sid, _ in camera.offers}) == 2


async def test_early_camera_candidates_follow_the_answer() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    sent: list[WebRTCMessage] = []
    task = asyncio.create_task(
        provider.async_handle_async_webrtc_offer(MagicMock(), OFFER, "ha1", sent.append)
    )
    await asyncio.sleep(0)
    sid = camera.offers[0][0]
    camera.on_event[sid](
        WebRtcCandidate(sid, "candidate:1 1 udp 1 1.2.3.4 1935 typ host", None, None)
    )
    assert sent == []
    camera.gate.set()
    await task

    assert sent == [
        WebRTCAnswer(ANSWER),
        WebRTCCandidate(
            RTCIceCandidateInit(
                "candidate:1 1 udp 1 1.2.3.4 1935 typ host", sdp_mid="0", sdp_m_line_index=0
            )
        ),
    ]


async def test_later_camera_candidates_are_sent_at_once() -> None:
    provider, camera = _provider()
    sent = await _offer(provider)
    sid = camera.offers[0][0]

    camera.on_event[sid](WebRtcCandidate(sid, "candidate:2", "1", 1))

    assert sent[-1] == WebRTCCandidate(
        RTCIceCandidateInit("candidate:2", sdp_mid="1", sdp_m_line_index=1)
    )


async def test_refusals_are_reported_and_every_view_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal can't be told from another app's ack, so nothing is latched."""
    provider, camera = _provider()
    camera.answer = NanitWebRtcRefused(501, "Not Implemented")

    with caplog.at_level("WARNING", logger="custom_components.nanit.webrtc"):
        sent = await _offer(provider, "ha1")
        again = await _offer(provider, "ha2")

    assert cast(WebRTCError, sent[0]).code == WEBRTC_ERROR_REFUSED
    assert cast(WebRTCError, again[0]).code == WEBRTC_ERROR_REFUSED
    assert len(camera.offers) == 2
    assert caplog.text.count("refused its own WebRTC stream") == 1
    assert provider.session_count == 0
    camera.answer = ANSWER
    assert await _offer(provider, "ha3") == [WebRTCAnswer(ANSWER)]


async def test_slot_limit_is_not_a_refusal() -> None:
    provider, camera = _provider()
    camera.answer = NanitWebRtcRefused(STREAM_LIMIT_STATUS, "limit")
    await _offer(provider, "ha1")
    camera.answer = ANSWER

    assert await _offer(provider, "ha2") == [WebRTCAnswer(ANSWER)]


@pytest.mark.parametrize("error", [NanitRequestTimeout("x", 1, 5.0), NanitTransportError("drop")])
async def test_transient_failures_are_retried_next_offer(error: Exception) -> None:
    provider, camera = _provider()
    camera.answer = error

    sent = await _offer(provider, "ha1")
    camera.answer = ANSWER

    assert cast(WebRTCError, sent[0]).code == WEBRTC_ERROR_FAILED
    assert provider.session_count == 0
    assert await _offer(provider, "ha2") == [WebRTCAnswer(ANSWER)]


async def test_camera_close_before_the_answer_is_an_error() -> None:
    provider, camera = _provider()
    camera.answer = NanitWebRtcClosed(2, "INVALID_SDP")

    sent = await _offer(provider)

    assert [type(m) for m in sent] == [WebRTCError]


async def test_frontend_close_during_the_offer_sends_nothing() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    sent: list[WebRTCMessage] = []
    task = asyncio.create_task(
        provider.async_handle_async_webrtc_offer(MagicMock(), OFFER, "ha1", sent.append)
    )
    await asyncio.sleep(0)
    provider.async_close_session("ha1")
    await asyncio.sleep(0)
    # aionanit fails the pending offer once the session is closed.
    camera.answer = NanitWebRtcClosed(0, "Closed before the camera answered")
    camera.gate.set()
    await task

    assert sent == []
    assert camera.closed == [camera.offers[0][0]]


async def test_frontend_close_before_a_late_answer_drops_it() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    sent: list[WebRTCMessage] = []
    task = asyncio.create_task(
        provider.async_handle_async_webrtc_offer(MagicMock(), OFFER, "ha1", sent.append)
    )
    await asyncio.sleep(0)
    provider.async_close_session("ha1")
    camera.gate.set()
    await task
    assert sent == []


async def test_camera_close_after_the_answer_is_silent() -> None:
    """Released frontends have dropped HLS by then; an error would strand them."""
    provider, camera = _provider()
    sent = await _offer(provider)
    sid = camera.offers[0][0]

    camera.on_event[sid](WebRtcClosed(sid, 1, "ICE checking timeout"))
    camera.on_event[sid](WebRtcCandidate(sid, "candidate:late", None, None))
    await provider.async_on_webrtc_candidate("ha1", RTCIceCandidateInit("candidate:x"))

    assert sent == [WebRTCAnswer(ANSWER)]
    assert provider.session_count == 0
    assert camera.candidates == []


async def test_browser_candidates_go_to_the_camera() -> None:
    provider, camera = _provider()
    await _offer(provider)
    sid = camera.offers[0][0]

    await provider.async_on_webrtc_candidate(
        "ha1", RTCIceCandidateInit("candidate:9", sdp_mid="0", sdp_m_line_index=0)
    )
    await provider.async_on_webrtc_candidate("ha1", RTCIceCandidateInit(""))
    await provider.async_on_webrtc_candidate("unknown", RTCIceCandidateInit("candidate:8"))

    assert camera.candidates == [(sid, "candidate:9", "0", 0)]


async def test_candidate_errors_are_swallowed() -> None:
    provider, camera = _provider()
    await _offer(provider)
    camera.async_webrtc_candidate = AsyncMock(side_effect=NanitRequestTimeout("x", 1, 5))

    await provider.async_on_webrtc_candidate("ha1", RTCIceCandidateInit("candidate:9"))


async def test_close_session_closes_on_the_camera_once() -> None:
    provider, camera = _provider()
    await _offer(provider)
    sid = camera.offers[0][0]

    provider.async_close_session("ha1")
    provider.async_close_session("ha1")
    provider.async_close_session("never-opened")
    await asyncio.sleep(0)

    assert camera.closed == [sid]


async def test_unregister_closes_every_session() -> None:
    provider, camera = _provider()
    await _offer(provider, "ha1")
    await _offer(provider, "ha2")

    await provider.async_unregister_camera(MagicMock())

    assert sorted(camera.closed) == sorted(sid for sid, _ in camera.offers)
    assert provider.session_count == 0


# ----------------------------------------------------------------------
# Camera entity wiring
# ----------------------------------------------------------------------


def _native_entity(hass: HomeAssistant, *, native: bool = True) -> tuple:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    coordinator = _push_coordinator(_camera_state())
    camera = MagicMock(uid="cam_1", connected=True)
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://cloud/url")
    camera.async_start_streaming = AsyncMock()
    camera.async_stop_streaming = AsyncMock()
    fake = FakeCamera()
    camera.async_webrtc_offer = fake.async_webrtc_offer
    camera.async_webrtc_candidate = fake.async_webrtc_candidate
    camera.async_webrtc_close = fake.async_webrtc_close
    entity = NanitCameraEntity(coordinator, camera, local=local, native_webrtc=native)
    entity.hass = hass
    entity.entity_id = "camera.nursery"
    entity.platform = MagicMock()
    _ENTITIES.append(entity)
    return entity, local, camera, fake


def _no_go2rtc() -> Any:
    return patch(
        f"{Camera.__module__}.async_get_supported_provider",
        AsyncMock(return_value=MagicMock(name="go2rtc")),
    )


async def test_native_provider_is_used_and_go2rtc_is_not_asked(hass: HomeAssistant) -> None:
    entity, _local, _camera, _fake = _native_entity(hass)
    with _no_go2rtc() as lookup, patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
    lookup.assert_not_awaited()
    assert isinstance(entity._webrtc_provider, NanitWebRTCProvider)
    assert entity.camera_capabilities.frontend_stream_types == {
        StreamType.HLS,
        StreamType.WEB_RTC,
    }


async def test_native_off_keeps_go2rtc(hass: HomeAssistant) -> None:
    entity, _local, _camera, _fake = _native_entity(hass, native=False)
    entity._provider_route = "local"
    with _no_go2rtc() as lookup, patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
    lookup.assert_awaited_once()
    assert not isinstance(entity._webrtc_provider, NanitWebRTCProvider)


async def test_native_provider_survives_a_flip_to_cloud(hass: HomeAssistant) -> None:
    entity, local, _camera, fake = _native_entity(hass)
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        provider = entity._webrtc_provider
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", lambda _m: None)

        entity._fall_back_to_cloud("test")
        await hass.async_block_till_done()

        assert entity._webrtc_provider is provider
        assert entity.extra_state_attributes == {"stream_route": "cloud"}
        assert entity._settled_route == "cloud"
        assert StreamType.WEB_RTC in entity.camera_capabilities.frontend_stream_types
        assert fake.closed == []  # the view is untouched

        local.cooldown_until = 0.0
        entity._handle_local_cooldown_end()
        await hass.async_block_till_done()
    assert entity._webrtc_provider is provider
    assert entity.extra_state_attributes == {"stream_route": "local"}


async def test_offers_reach_the_camera_on_the_cloud_route(hass: HomeAssistant) -> None:
    entity, local, _camera, fake = _native_entity(hass)
    local.start_cooldown("test")
    sent: list[WebRTCMessage] = []
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", sent.append)
    assert sent == [WebRTCAnswer(ANSWER)]
    assert len(fake.offers) == 1


async def test_sleep_closes_open_views(hass: HomeAssistant) -> None:
    entity, _local, _camera, fake = _native_entity(hass)
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", lambda _m: None)
        entity._prev_is_on = True
        entity.coordinator.data = _camera_state(sleep_mode=True)
        entity._handle_coordinator_update()
        await hass.async_block_till_done()
    assert fake.closed == [fake.offers[0][0]]


async def test_removal_closes_open_views(hass: HomeAssistant) -> None:
    entity, _local, _camera, fake = _native_entity(hass)
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", lambda _m: None)
        await entity.async_will_remove_from_hass()
    assert fake.closed == [fake.offers[0][0]]


@pytest.mark.usefixtures("source_ip")
@pytest.mark.parametrize(("views", "grows"), [(1, False), (0, True)])
async def test_busy_with_our_own_views_does_not_grow_the_backoff(
    hass: HomeAssistant, views: int, grows: bool
) -> None:
    entity, local, camera, _fake = _native_entity(hass)
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        for i in range(views):
            await entity.async_handle_async_webrtc_offer(OFFER, f"ha{i}", lambda _m: None)
    camera.async_start_streaming = AsyncMock(return_value=STREAM_LIMIT_STATUS)
    local.channel.wait_for_publish = AsyncMock(return_value=False)

    for _ in range(3):
        local.end_cooldown()
        assert await entity._async_local_start() is False

    assert (local.misses > 0) is grows


# ----------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------


def _options_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    from types import SimpleNamespace

    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(babies=[], speaker_uid_map={}))
    entry.add_to_hass(hass)
    return entry


async def _set_option(hass: HomeAssistant, entry: MockConfigEntry, value: bool) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "native_webrtc"}
    )
    assert result.get("step_id") == "native_webrtc"
    (key,) = cast(dict[str, Any], result)["data_schema"].schema
    assert key.default() is (entry.options.get(CONF_NATIVE_WEBRTC) is True)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NATIVE_WEBRTC: value}
    )
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    return cast(dict[str, Any], result["data"])


async def test_option_turns_on_and_keeps_other_options(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_READ_ONLY: True})
    assert await _set_option(hass, entry, True) == {CONF_READ_ONLY: True, CONF_NATIVE_WEBRTC: True}


async def test_option_turns_off(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_NATIVE_WEBRTC: True})
    assert await _set_option(hass, entry, False) == {}


async def test_option_reaches_the_entity(hass: HomeAssistant) -> None:
    from custom_components.nanit import camera as camera_module

    entry = _entry(hass, {CONF_NATIVE_WEBRTC: True})
    cam_data = MagicMock()
    cam_data.camera = MagicMock(uid="cam_1")
    entry.runtime_data = MagicMock(local_stream=None, cameras={"cam_1": cam_data})
    added: list[NanitCameraEntity] = []
    await camera_module.async_setup_entry(hass, entry, lambda ents: added.extend(ents))
    _ENTITIES.extend(added)
    assert isinstance(added[0]._native_provider, NanitWebRTCProvider)


async def test_unexpected_errors_free_the_session() -> None:
    provider, camera = _provider()
    camera.answer = RuntimeError("bug")

    sent = await _offer(provider)

    assert cast(WebRTCError, sent[0]).code == WEBRTC_ERROR_FAILED
    assert provider.session_count == 0


async def test_cancelled_offer_frees_the_session() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    task = asyncio.create_task(_offer(provider))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.session_count == 0


async def test_camera_error_close_is_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, camera = _provider()
    await _offer(provider, "ha1")
    await _offer(provider, "ha2")
    first, second = (sid for sid, _ in camera.offers)

    with caplog.at_level("INFO", logger="custom_components.nanit.webrtc"):
        camera.on_event[first](WebRtcClosed(first, 1, "ICE checking timeout"))
        camera.on_event[second](WebRtcClosed(second, 0, ""))

    assert "ICE checking timeout" in caplog.text
    assert provider.stats == {
        "offers": 2,
        "answered": 2,
        "camera_closed_error_1": 1,
        "camera_closed": 1,
    }


async def test_close_between_answer_and_return_sends_an_error() -> None:
    """R2: the camera's close lands before the offer call returns its answer."""
    provider, camera = _provider()
    orig = camera.async_webrtc_offer

    async def offer(session_id: int, sdp: str, on_event: Any) -> str:
        answer = await orig(session_id, sdp, on_event)
        on_event(WebRtcClosed(session_id, 1, "gone"))
        return answer

    camera.async_webrtc_offer = offer
    sent = await _offer(provider)
    assert [type(m) for m in sent] == [WebRTCError]


async def test_offer_during_unregister_is_not_left_open() -> None:
    """R3: an offer accepted while close_all awaits its closes is never closed."""
    provider, camera = _provider()
    await _offer(provider, "ha1")
    gate = asyncio.Event()
    orig_close = camera.async_webrtc_close

    async def slow_close(session_id: int) -> None:
        await gate.wait()
        await orig_close(session_id)

    camera.async_webrtc_close = slow_close
    unregister = asyncio.create_task(provider.async_unregister_camera(MagicMock()))
    await asyncio.sleep(0)
    await _offer(provider, "ha2")  # core still routes offers to the provider
    gate.set()
    await unregister
    assert provider.session_count == 0, "session opened during unregister survives"


async def test_camera_error_before_the_answer_is_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, camera = _provider()
    camera.answer = NanitWebRtcClosed(2, "INVALID_SDP")

    with caplog.at_level("INFO", logger="custom_components.nanit.webrtc"):
        await _offer(provider)

    assert "INVALID_SDP" in caplog.text
    assert provider.stats == {"offers": 1, "camera_closed_error_2": 1}


async def test_a_view_closed_during_its_offer_counts_as_nothing_failed() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    task = asyncio.create_task(_offer(provider))
    await asyncio.sleep(0)
    provider.async_close_session("ha1")
    camera.answer = NanitWebRtcClosed(0, "Closed before the camera answered")
    camera.gate.set()
    await task
    assert provider.stats == {"offers": 1}


async def test_a_refusal_for_a_view_already_closed_sends_nothing() -> None:
    provider, camera = _provider()
    camera.gate = asyncio.Event()
    sent: list[WebRTCMessage] = []
    task = asyncio.create_task(
        provider.async_handle_async_webrtc_offer(MagicMock(), OFFER, "ha1", sent.append)
    )
    await asyncio.sleep(0)
    provider.async_close_session("ha1")
    camera.answer = NanitWebRtcRefused(501, "Not Implemented")
    camera.gate.set()
    await task
    assert sent == []
    assert provider.stats == {"offers": 1}


async def test_an_offer_without_h264_never_reaches_the_camera(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, camera = _provider()
    vp8_only = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 VP8/90000\r\n"
    sent: list[WebRTCMessage] = []

    with caplog.at_level("INFO", logger="custom_components.nanit.webrtc"):
        for i in range(2):
            await provider.async_handle_async_webrtc_offer(
                MagicMock(), vp8_only, f"ha{i}", sent.append
            )

    assert [cast(WebRTCError, m).code for m in sent] == [WEBRTC_ERROR_NO_H264] * 2
    assert camera.offers == []
    assert caplog.text.count("no H.264") == 1
    assert provider.session_count == 0
    assert provider.stats == {"no_h264": 2}


async def test_h264_is_found_in_lf_only_lowercase_offers() -> None:
    provider, camera = _provider()
    offer = (
        "v=0\nm=video 9 UDP/TLS/RTP/SAVPF 96 97\na=rtpmap:96 VP8/90000\na=rtpmap:97 h264/90000\n"
    )
    sent: list[WebRTCMessage] = []
    await provider.async_handle_async_webrtc_offer(MagicMock(), offer, "ha1", sent.append)
    assert sent == [WebRTCAnswer(ANSWER)]
    assert len(camera.offers) == 1


async def test_a_renamed_camera_takes_native_views_again(hass: HomeAssistant) -> None:
    """A rename removes the entity and adds the same object back."""
    entity, _local, _camera, fake = _native_entity(hass)
    sent: list[WebRTCMessage] = []
    with _no_go2rtc(), patch.object(NanitCameraEntity, "async_write_ha_state"):
        await entity.async_refresh_providers()
        await entity.async_will_remove_from_hass()
        entity._removed = False  # what async_added_to_hass does on the re-add
        await entity.async_refresh_providers()
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", sent.append)
    assert sent == [WebRTCAnswer(ANSWER)]
    assert len(fake.offers) == 1


async def test_a_rename_after_a_timed_out_removal_takes_views_again(hass: HomeAssistant) -> None:
    """Removal gave up while a stalled close held the unregister; the re-add must stick."""
    from custom_components.nanit import camera as camera_module

    entity, _local, camera, fake = _native_entity(hass)
    sent: list[WebRTCMessage] = []
    stall = asyncio.Event()
    orig_close = fake.async_webrtc_close

    async def slow_close(session_id: int) -> None:
        await stall.wait()
        await orig_close(session_id)

    with (
        _no_go2rtc(),
        patch.object(NanitCameraEntity, "async_write_ha_state"),
        patch.object(camera_module, "_PROVIDER_REFRESH_WAIT", 0.05),
    ):
        await entity.async_refresh_providers()
        await entity.async_handle_async_webrtc_offer(OFFER, "ha0", lambda _m: None)
        camera.async_webrtc_close = slow_close
        await entity.async_will_remove_from_hass()
        stall.set()
        entity._removed = False
        await entity.async_refresh_providers()
        await entity._async_publish_route()  # a later route task tells detached providers
        await entity.async_handle_async_webrtc_offer(OFFER, "ha1", sent.append)
    assert sent == [WebRTCAnswer(ANSWER)]
