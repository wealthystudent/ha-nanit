"""Opt-in stills from the live stream when Nanit's cloud snapshot fails."""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Camera imports pull in turbojpeg. Stub it like test_entities.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from aionanit.models import Baby, ConnectionState
from custom_components.nanit import camera as camera_module
from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import (
    CONF_READ_ONLY,
    CONF_SPEAKER_IPS,
    CONF_STREAM_STILLS,
    DOMAIN,
    is_stream_stills,
)
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics
from custom_components.nanit.hub import StreamStillInfo

from .test_entities import _camera_state, _disable_state_writes, _push_coordinator
from .test_local_stream import FLV_URL, _entry, _local_entry

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

GRAB = "custom_components.nanit.camera.ffmpeg_get_image"
URL = "rtmps://media-secured.nanit.com/nanit/baby_1.token"
OLD_IMAGE = b"old-image"
NEW_IMAGE = b"new-image"
MAX_AGE = camera_module._STREAM_STILL_MAX_AGE
# What ffmpeg_get_image is handed for URL: input options, then the input.
FFMPEG_INPUT = f"-rw_timeout 10000000 -i {URL}"
# The real check, kept before the autouse fixture below patches it out.
BINARY_EXISTS = camera_module._ffmpeg_binary_exists


@pytest.fixture(autouse=True)
def ffmpeg_present() -> Iterator[MagicMock]:
    """The ffmpeg integration is not set up in these tests, and no binary runs."""
    with (
        patch(
            "custom_components.nanit.camera.get_ffmpeg_manager",
            return_value=MagicMock(binary="ffmpeg"),
        ),
        patch("custom_components.nanit.camera._ffmpeg_binary_exists", return_value=True) as exists,
    ):
        yield exists


def _camera_mock() -> MagicMock:
    camera = MagicMock(uid="cam_1")
    camera.async_get_snapshot = AsyncMock(side_effect=RuntimeError("404"))
    camera.async_get_stream_rtmps_url = AsyncMock(return_value=URL)
    camera.async_start_streaming = AsyncMock()
    camera.async_stop_streaming = AsyncMock()
    return camera


def _entity(
    hass: HomeAssistant,
    *,
    stream_stills: bool = True,
    sleep_mode: bool = False,
    connected: bool = True,
    cache_age: float | None = MAX_AGE + 60,
) -> tuple[NanitCameraEntity, MagicMock, StreamStillInfo]:
    coordinator = _push_coordinator(_camera_state(sleep_mode=sleep_mode), connected=connected)
    camera = _camera_mock()
    info = StreamStillInfo()
    entity = NanitCameraEntity(coordinator, camera, stream_stills=stream_stills, still_info=info)
    entity.hass = hass
    _disable_state_writes(entity)
    if cache_age is not None:
        entity._cached_snapshot = OLD_IMAGE
        entity._cached_snapshot_at = time.monotonic() - cache_age
    return entity, camera, info


async def _settle(hass: HomeAssistant, entity: NanitCameraEntity) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    # stream_source() arms the expiry and keepalive timers. Don't leak them.
    entity._cancel_stream_timers()


# ---------------------------------------------------------------------------
# When a grab starts
# ---------------------------------------------------------------------------


async def test_option_off_keeps_the_old_behaviour(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass, stream_stills=False)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        image = await entity.async_camera_image()
        await _settle(hass, entity)

    assert image == OLD_IMAGE
    camera.async_get_snapshot.assert_awaited_once()
    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert entity._stream_still_task is None


async def test_cloud_snapshot_that_works_never_grabs(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    camera.async_get_snapshot = AsyncMock(return_value=b"cloud-image")

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        image = await entity.async_camera_image()
        await _settle(hass, entity)

    assert image == b"cloud-image"
    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()


async def test_old_cache_starts_one_grab_and_returns_cached_image(hass: HomeAssistant) -> None:
    """The request returns the cached image while the grab is still running."""
    entity, camera, _ = _entity(hass)
    release = asyncio.Event()

    async def slow_grab(_hass: HomeAssistant, source: str) -> bytes:
        await release.wait()
        return NEW_IMAGE

    with patch(GRAB, new=AsyncMock(side_effect=slow_grab)) as grab:
        first = await entity.async_camera_image()
        assert first == OLD_IMAGE
        task = entity._stream_still_task
        assert task is not None
        assert not task.done()

        # Concurrent requests while the grab runs start nothing new.
        results = await asyncio.gather(*(entity.async_camera_image() for _ in range(5)))
        assert results == [OLD_IMAGE] * 5
        assert entity._stream_still_task is task

        # Still one at a time with no retry delay in the way: the running grab
        # has not ended, so nothing has started the delay yet.
        assert entity._stream_still_finished_at is None
        assert await entity.async_camera_image() == OLD_IMAGE
        assert entity._stream_still_task is task

        release.set()
        await _settle(hass, entity)

    grab.assert_awaited_once_with(hass, FFMPEG_INPUT)
    camera.async_start_streaming.assert_awaited_once_with(rtmps_url=URL)
    assert entity._cached_snapshot == NEW_IMAGE


async def test_no_cache_returns_none_and_starts_a_grab(hass: HomeAssistant) -> None:
    entity, _, _ = _entity(hass, cache_age=None)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        image = await entity.async_camera_image()
        await _settle(hass, entity)

    assert image is None
    grab.assert_awaited_once()
    assert await entity.async_camera_image() == NEW_IMAGE


async def test_recent_cache_does_not_grab(hass: HomeAssistant) -> None:
    """Older than the cloud TTL (so the cloud is asked) but younger than ten minutes."""
    entity, camera, _ = _entity(hass, cache_age=MAX_AGE - 60)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        image = await entity.async_camera_image()
        await _settle(hass, entity)

    assert image == OLD_IMAGE
    camera.async_get_snapshot.assert_awaited_once()
    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()


async def test_asleep_camera_is_not_grabbed(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass, sleep_mode=True)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        assert await entity.async_camera_image() is None
        # Even if reached directly, the guard holds.
        entity._maybe_start_stream_still()
        await _settle(hass, entity)

    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert entity._stream_still_task is None


async def test_unavailable_camera_is_not_grabbed(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass, connected=False)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        assert await entity.async_camera_image() == OLD_IMAGE
        await _settle(hass, entity)

    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()


async def test_read_only_does_not_block_stills(hass: HomeAssistant) -> None:
    coordinator = _push_coordinator(_camera_state())
    camera = _camera_mock()
    entity = NanitCameraEntity(coordinator, camera, read_only=True, stream_stills=True)
    entity.hass = hass

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        assert await entity.async_camera_image() is None
        await _settle(hass, entity)

    grab.assert_awaited_once()
    assert entity._cached_snapshot == NEW_IMAGE


# ---------------------------------------------------------------------------
# What a grab does
# ---------------------------------------------------------------------------


async def test_grab_success_updates_cache_and_diagnostics_info(hass: HomeAssistant) -> None:
    entity, camera, info = _entity(hass)
    before = time.monotonic()

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)):
        await entity.async_camera_image()
        await _settle(hass, entity)

    assert entity._cached_snapshot == NEW_IMAGE
    assert entity._cached_snapshot_at >= before
    assert isinstance(info.last_taken, datetime)
    # The fresh still is served from cache without asking the cloud again.
    camera.async_get_snapshot.reset_mock()
    assert await entity.async_camera_image() == NEW_IMAGE
    camera.async_get_snapshot.assert_not_awaited()


@pytest.mark.parametrize(
    "grab",
    [
        AsyncMock(side_effect=RuntimeError("ffmpeg died")),
        AsyncMock(return_value=None),
        AsyncMock(return_value=b""),
    ],
    ids=["error", "none", "empty"],
)
async def test_grab_failure_keeps_cache_and_allows_a_later_retry(
    hass: HomeAssistant, grab: AsyncMock
) -> None:
    entity, camera, info = _entity(hass)
    cached_at = entity._cached_snapshot_at

    with patch(GRAB, new=grab):
        assert await entity.async_camera_image() == OLD_IMAGE
        await _settle(hass, entity)
        assert entity._cached_snapshot == OLD_IMAGE
        assert entity._cached_snapshot_at == cached_at
        assert info.last_taken is None

        # Not retried on every request...
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert grab.await_count == 1
        assert camera.async_start_streaming.await_count == 1
        assert entity._stream_still_failures == 1

        # ...but once the retry delay has passed.
        assert entity._stream_still_finished_at is not None
        entity._stream_still_finished_at -= camera_module._STREAM_STILL_RETRY_DELAY
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert grab.await_count == 2
        assert camera.async_start_streaming.await_count == 2


async def test_grab_timeout_keeps_cache_and_allows_a_later_retry(hass: HomeAssistant) -> None:
    entity, _, _ = _entity(hass)

    async def hang(_hass: HomeAssistant, source: str) -> bytes:
        await asyncio.sleep(3600)
        return NEW_IMAGE

    with (
        patch(GRAB, new=AsyncMock(side_effect=hang)) as grab,
        patch.object(camera_module, "_STREAM_STILL_TIMEOUT", 0.01),
    ):
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert entity._stream_still_task is not None
        assert entity._stream_still_task.done()
        assert entity._cached_snapshot == OLD_IMAGE
        assert entity._stream_still_failures == 1

        entity._stream_still_finished_at = None
        await entity.async_camera_image()
        await _settle(hass, entity)

    assert grab.await_count == 2


async def test_no_stream_source_skips_the_frame_read(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=RuntimeError("cloud down"))

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_not_awaited()
    assert entity._cached_snapshot == OLD_IMAGE
    assert entity._stream_still_failures == 1


async def test_grab_never_stops_the_camera_stream(hass: HomeAssistant) -> None:
    """A grab sends no STOPPED: a viewer may be using the push it started."""
    for grab in (
        AsyncMock(return_value=NEW_IMAGE),
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        entity, camera, _ = _entity(hass)
        with patch(GRAB, new=grab):
            await entity.async_camera_image()
            await _settle(hass, entity)
        camera.async_stop_streaming.assert_not_awaited()


async def test_grab_failure_log_carries_no_url(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entity, _, _ = _entity(hass)
    caplog.set_level("DEBUG", logger="custom_components.nanit.camera")

    with patch(GRAB, new=AsyncMock(side_effect=RuntimeError(f"Error opening input {URL}"))):
        await entity.async_camera_image()
        await _settle(hass, entity)

    assert "RuntimeError reading a frame from ffmpeg" in caplog.text
    assert URL not in caplog.text


async def test_removal_cancels_a_running_grab(hass: HomeAssistant) -> None:
    entity, _, _ = _entity(hass)

    async def hang(_hass: HomeAssistant, source: str) -> bytes:
        await asyncio.sleep(3600)
        return NEW_IMAGE

    with patch(GRAB, new=AsyncMock(side_effect=hang)):
        await entity.async_camera_image()
        await asyncio.sleep(0)
        task = entity._stream_still_task
        assert task is not None
        await entity.async_will_remove_from_hass()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert task.cancelled()
    assert entity._stream_still_task is None


# ---------------------------------------------------------------------------
# The keepalive must not keep a grabbed camera streaming
# ---------------------------------------------------------------------------


async def test_keepalive_does_not_renew_a_stream_only_a_grab_asked_for(
    hass: HomeAssistant,
) -> None:
    """A grab sends PUT_STREAMING once, and nothing renews it without a viewer.

    stream_source() arms the keepalive and expiry timers. The keepalive only
    re-sends PUT_STREAMING while HA's stream has outputs, and a grab never
    creates an HA stream, so the camera's push winds down on its own.
    """
    entity, camera, _ = _entity(hass)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)):
        await entity.async_camera_image()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert camera.async_start_streaming.await_count == 1
    assert entity.stream is None
    assert entity._cached_stream_source == URL

    # The keepalive timer firing, repeatedly.
    for _ in range(3):
        entity._handle_stream_keepalive()
        await hass.async_block_till_done()
    # A control-session reconnect, which resumes watched streams.
    entity._handle_coordinator_update()
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.RECONNECTING)
    entity._handle_coordinator_update()
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.CONNECTED)
    entity._handle_coordinator_update()
    await hass.async_block_till_done()

    assert camera.async_start_streaming.await_count == 1

    # Source expiry releases the source and stops the timer chain. The expiry
    # timer is fired by hand here, so cancel the real one it stands in for.
    cancel_expiry = entity._cancel_stream_expiry_timer
    assert cancel_expiry is not None
    cancel_expiry()
    entity._handle_stream_expiry()
    await hass.async_block_till_done()
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is None
    assert entity._cancel_stream_expiry_timer is None
    assert camera.async_start_streaming.await_count == 1
    camera.async_stop_streaming.assert_not_awaited()


async def test_keepalive_still_renews_for_a_real_viewer_after_a_grab(
    hass: HomeAssistant,
) -> None:
    entity, camera, _ = _entity(hass)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)):
        await entity.async_camera_image()
        await hass.async_block_till_done(wait_background_tasks=True)

    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    entity.stream = stream
    entity._handle_stream_keepalive()
    await hass.async_block_till_done()

    assert camera.async_start_streaming.await_count == 2
    camera.async_start_streaming.assert_awaited_with(rtmps_url=URL, reconnect_on_failure=False)
    entity._cancel_stream_timers()


# ---------------------------------------------------------------------------
# Option plumbing
# ---------------------------------------------------------------------------


def test_is_stream_stills_defaults_off() -> None:
    assert is_stream_stills(cast(Any, SimpleNamespace(options={}))) is False
    assert is_stream_stills(cast(Any, SimpleNamespace(options={CONF_STREAM_STILLS: True})))


@pytest.mark.parametrize("enabled", [True, False])
async def test_camera_setup_passes_the_option_to_each_camera(enabled: bool) -> None:
    info = StreamStillInfo()
    cam_data = SimpleNamespace(
        push_coordinator=_push_coordinator(_camera_state()),
        camera=_camera_mock(),
        stream_still=info,
    )
    entry = MagicMock(
        options={CONF_STREAM_STILLS: True} if enabled else {},
        runtime_data=SimpleNamespace(cameras={"cam_1": cam_data}, local_stream=None),
    )
    added: list[NanitCameraEntity] = []

    await camera_module.async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))

    assert len(added) == 1
    assert added[0]._stream_stills is enabled
    assert added[0]._still_info is info


def _options_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(babies=[], speaker_uid_map={}))
    entry.add_to_hass(hass)
    return entry


async def _set_option(hass: HomeAssistant, entry: MockConfigEntry, value: bool) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    assert "stream_stills" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "stream_stills"}
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "stream_stills"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STREAM_STILLS: value}
    )
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    return cast(dict[str, Any], result["data"])


async def test_options_flow_turns_stream_stills_on(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_READ_ONLY: True, CONF_SPEAKER_IPS: {"spk": "1.2.3.4"}})

    data = await _set_option(hass, entry, True)

    assert data == {
        CONF_READ_ONLY: True,
        CONF_SPEAKER_IPS: {"spk": "1.2.3.4"},
        CONF_STREAM_STILLS: True,
    }
    assert entry.options[CONF_STREAM_STILLS] is True


async def test_options_flow_turns_stream_stills_off(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_STREAM_STILLS: True, CONF_READ_ONLY: True})

    data = await _set_option(hass, entry, False)

    assert data == {CONF_READ_ONLY: True}


async def test_options_form_defaults_to_the_current_value(hass: HomeAssistant) -> None:
    entry = _options_entry(hass, {CONF_STREAM_STILLS: True})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "stream_stills"}
    )

    (key,) = cast(dict[str, Any], result)["data_schema"].schema
    assert str(key) == CONF_STREAM_STILLS
    assert key.default() is True


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
async def test_diagnostics_report_the_option_and_last_still(enabled: bool) -> None:
    taken = datetime(2026, 10, 18, 12, 0, 0)
    push = MagicMock(last_update_success=True, last_exception=None, connected=True, data=None)
    cam_data = MagicMock(
        baby=Baby(uid="baby_1", name="Nursery", camera_uid="cam_1"),
        push_coordinator=push,
        cloud_coordinator=None,
        stream_still=StreamStillInfo(last_taken=taken if enabled else None),
    )
    entry = MagicMock(
        data={},
        options={CONF_STREAM_STILLS: True} if enabled else {},
        runtime_data=MagicMock(cameras={"cam_1": cam_data}, speakers={}),
    )

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["stream_stills"] is enabled
    camera_diag = result["cameras"]["camera_0"]
    assert camera_diag["last_stream_still"] == (taken.isoformat() if enabled else None)
    assert URL not in str(result)


# ---------------------------------------------------------------------------
# No ffmpeg binary
# ---------------------------------------------------------------------------


async def test_missing_ffmpeg_sends_nothing_and_warns_once(
    hass: HomeAssistant, ffmpeg_present: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """On a Core install without ffmpeg a grab must not start the camera's stream."""
    ffmpeg_present.return_value = False
    first, camera_1, _ = _entity(hass)
    second, camera_2, _ = _entity(hass)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        for entity in (first, second, first):
            entity._stream_still_finished_at = None
            assert await entity.async_camera_image() == OLD_IMAGE
            await _settle(hass, entity)

    grab.assert_not_awaited()
    camera_1.async_get_stream_rtmps_url.assert_not_awaited()
    camera_1.async_start_streaming.assert_not_awaited()
    camera_2.async_start_streaming.assert_not_awaited()
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "need FFmpeg" in r.message
    ]
    assert len(warnings) == 1
    assert "restart Home Assistant" in warnings[0].message
    # Checked once for the whole run, not once per camera or per request.
    ffmpeg_present.assert_called_once_with("ffmpeg")
    # A missing binary is not a failed still, so it never backs off or warns twice.
    assert first._stream_still_failures == 0
    assert second._stream_still_failures == 0


async def test_ffmpeg_integration_not_set_up_counts_as_missing(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entity, camera, _ = _entity(hass)

    with (
        patch(
            "custom_components.nanit.camera.get_ffmpeg_manager",
            side_effect=ValueError("ffmpeg component not initialized"),
        ),
        patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab,
    ):
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert "need FFmpeg" in caplog.text


@pytest.mark.parametrize(
    ("official", "which", "expected"),
    [(True, None, True), (False, "/usr/bin/ffmpeg", True), (False, None, False)],
)
def test_ffmpeg_binary_exists(official: bool, which: str | None, expected: bool) -> None:
    with (
        patch("custom_components.nanit.camera.is_official_image", return_value=official),
        patch("custom_components.nanit.camera.shutil.which", return_value=which),
    ):
        assert BINARY_EXISTS("ffmpeg") is expected


# ---------------------------------------------------------------------------
# Turning the camera off while a grab waits
# ---------------------------------------------------------------------------


async def test_turn_off_cancels_a_grab_waiting_on_its_source(hass: HomeAssistant) -> None:
    """The grab must never send PUT_STREAMING after turn_off's STOPPED."""
    entity, camera, _ = _entity(hass)
    calls: list[str] = []
    release = asyncio.Event()

    async def slow_url() -> str:
        await release.wait()
        return URL

    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=slow_url)
    camera.async_start_streaming = AsyncMock(side_effect=lambda **_: calls.append("START"))
    camera.async_stop_streaming = AsyncMock(side_effect=lambda: calls.append("STOP"))
    camera.async_set_settings = AsyncMock()

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        for _ in range(5):
            await asyncio.sleep(0)
        camera.async_get_stream_rtmps_url.assert_awaited_once()
        task = entity._stream_still_task
        assert task is not None

        await entity.async_turn_off()
        entity.coordinator.data = _camera_state(sleep_mode=True)
        release.set()
        await _settle(hass, entity)

    assert task.cancelled()
    assert calls == ["STOP"]
    grab.assert_not_awaited()
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is None
    assert entity._cancel_stream_expiry_timer is None


async def test_power_change_cancels_a_running_grab(hass: HomeAssistant) -> None:
    """The camera going to sleep from elsewhere (the app, a schedule) cancels too."""
    entity, camera, _ = _entity(hass)
    release = asyncio.Event()

    async def slow_url() -> str:
        await release.wait()
        return URL

    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=slow_url)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        entity._handle_coordinator_update()  # records the camera as on
        await entity.async_camera_image()
        for _ in range(5):
            await asyncio.sleep(0)
        task = entity._stream_still_task
        assert task is not None

        entity.coordinator.data = _camera_state(sleep_mode=True)
        entity._handle_coordinator_update()
        release.set()
        await _settle(hass, entity)

    assert task.cancelled()
    camera.async_start_streaming.assert_not_awaited()
    grab.assert_not_awaited()


async def test_a_grab_cancelled_after_its_put_still_starts_the_retry_delay(
    hass: HomeAssistant,
) -> None:
    """The PUT already went out, so a cancelled grab must not let the next one start at once."""
    entity, camera, _ = _entity(hass)
    reading = asyncio.Event()

    async def read_forever(_hass: HomeAssistant, source: str) -> bytes:
        reading.set()
        await asyncio.Event().wait()
        return NEW_IMAGE

    with patch(GRAB, new=AsyncMock(side_effect=read_forever)):
        await entity.async_camera_image()
        await asyncio.wait_for(reading.wait(), 5)
        task = entity._stream_still_task
        assert task is not None
        entity._cancel_stream_still()
        await _settle(hass, entity)

    assert task.cancelled()
    camera.async_start_streaming.assert_awaited_once()
    assert entity._stream_still_finished_at is not None
    assert entity._stream_still_failures == 0


async def test_camera_asleep_after_the_source_skips_the_frame_read(hass: HomeAssistant) -> None:
    """Asleep by the time the source is ready (no update seen yet): no ffmpeg."""
    entity, camera, _ = _entity(hass)

    async def url_then_sleep() -> str:
        entity.coordinator.data = _camera_state(sleep_mode=True)
        return URL

    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=url_then_sleep)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_not_awaited()
    assert entity._stream_still_failures == 0


# ---------------------------------------------------------------------------
# Backoff on repeated failures
# ---------------------------------------------------------------------------


async def _fail_once(hass: HomeAssistant, entity: NanitCameraEntity) -> None:
    entity._stream_still_finished_at = None
    await entity.async_camera_image()
    await _settle(hass, entity)


def test_retry_delay_doubles_and_caps_at_an_hour(hass: HomeAssistant) -> None:
    entity, _, _ = _entity(hass)
    delays = []
    for failures in (*range(11), 1000):
        entity._stream_still_failures = failures
        delays.append(entity._stream_still_retry_delay())

    # The cap sits well past the camera's ~20 minute push after each attempt.
    assert delays == [60, 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600, 3600]


async def test_failures_back_off_and_success_resets(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    grab = AsyncMock(return_value=None)

    with patch(GRAB, new=grab):
        for expected in (1, 2, 3):
            await _fail_once(hass, entity)
            assert entity._stream_still_failures == expected

        # Two minutes after a third failure in a row is too soon (four minutes).
        assert entity._stream_still_finished_at is not None
        entity._stream_still_finished_at -= 2 * 60
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert camera.async_start_streaming.await_count == 3

        entity._stream_still_finished_at -= 2 * 60
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert camera.async_start_streaming.await_count == 4

        grab.return_value = NEW_IMAGE
        await _fail_once(hass, entity)

    assert entity._stream_still_failures == 0
    assert entity._stream_still_retry_delay() == camera_module._STREAM_STILL_RETRY_DELAY
    assert entity._cached_snapshot == NEW_IMAGE


async def test_failures_warn_once_per_streak(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entity, _, _ = _entity(hass)

    def warnings() -> int:
        return sum(
            1
            for r in caplog.records
            if r.levelno == logging.WARNING and "keep failing" in r.message
        )

    with patch(GRAB, new=AsyncMock(side_effect=RuntimeError(f"cannot open {URL}"))) as grab:
        await _fail_once(hass, entity)
        await _fail_once(hass, entity)
        assert warnings() == 0
        for _ in range(4):
            await _fail_once(hass, entity)
        assert warnings() == 1
        assert "up to every 60 minutes" in caplog.text

        # A success ends the streak, and a new streak warns again.
        grab.side_effect = None
        grab.return_value = NEW_IMAGE
        await _fail_once(hass, entity)
        entity._cached_snapshot_at -= MAX_AGE
        grab.side_effect = RuntimeError("again")
        for _ in range(3):
            await _fail_once(hass, entity)

    assert warnings() == 2
    assert URL not in caplog.text


# ---------------------------------------------------------------------------
# Someone already watching
# ---------------------------------------------------------------------------


async def test_watched_stream_gives_the_frame_without_put_streaming(hass: HomeAssistant) -> None:
    entity, camera, info = _entity(hass)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    stream.async_get_image = AsyncMock(return_value=NEW_IMAGE)
    entity.stream = stream

    with patch(GRAB, new=AsyncMock(return_value=b"ffmpeg-image")) as grab:
        await entity.async_camera_image()
        await _settle(hass, entity)

    stream.async_get_image.assert_awaited_once_with()
    grab.assert_not_awaited()
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert entity._cached_snapshot == NEW_IMAGE
    assert info.last_taken is not None


async def test_idle_stream_object_still_uses_ffmpeg(hass: HomeAssistant) -> None:
    """A stream nobody is watching would have to be started, so ffmpeg reads instead."""
    entity, camera, _ = _entity(hass)
    stream = MagicMock()
    stream.outputs.return_value = {}
    stream.async_get_image = AsyncMock(return_value=b"stream-image")
    entity.stream = stream

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        await _settle(hass, entity)

    stream.async_get_image.assert_not_awaited()
    grab.assert_awaited_once_with(hass, FFMPEG_INPUT)
    camera.async_start_streaming.assert_awaited_once()
    assert entity._cached_snapshot == NEW_IMAGE


async def test_watched_stream_failure_counts_as_a_failure(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": MagicMock()}
    stream.async_get_image = AsyncMock(return_value=None)
    entity.stream = stream

    await entity.async_camera_image()
    await _settle(hass, entity)

    assert entity._stream_still_failures == 1
    assert entity._cached_snapshot == OLD_IMAGE
    camera.async_start_streaming.assert_not_awaited()
    # A failed still leaves the viewer's stream alone.
    assert entity.stream is stream
    stream.stop.assert_not_called()


async def test_persistent_failure_backs_off_to_an_hour(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)

    with patch(GRAB, new=AsyncMock(return_value=None)):
        for _ in range(8):
            await _fail_once(hass, entity)

    assert camera.async_start_streaming.await_count == 8
    assert entity._stream_still_retry_delay() == 3600


async def test_cloud_snapshot_success_ends_the_failure_streak(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    with patch(GRAB, new=AsyncMock(return_value=None)):
        for _ in range(4):
            await _fail_once(hass, entity)
    assert entity._stream_still_failures == 4

    camera.async_get_snapshot = AsyncMock(return_value=b"cloud-image")
    entity._cached_snapshot_at -= camera_module._SNAPSHOT_CACHE_TTL
    assert await entity.async_camera_image() == b"cloud-image"

    assert entity._stream_still_failures == 0


async def test_turn_on_cancels_a_running_grab(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    camera.async_set_settings = AsyncMock()
    release = asyncio.Event()

    async def slow_url() -> str:
        await release.wait()
        return URL

    camera.async_get_stream_rtmps_url = AsyncMock(side_effect=slow_url)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        for _ in range(5):
            await asyncio.sleep(0)
        task = entity._stream_still_task
        assert task is not None

        await entity.async_turn_on()
        release.set()
        await _settle(hass, entity)

    assert task.cancelled()
    camera.async_start_streaming.assert_not_awaited()
    grab.assert_not_awaited()


async def test_asleep_when_the_task_runs_is_not_a_failure(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        # The task is not eager, so it runs after the camera went to sleep.
        entity.coordinator.data = _camera_state(sleep_mode=True)
        await _settle(hass, entity)

    grab.assert_not_awaited()
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert entity._stream_still_failures == 0


async def test_cancelling_a_grab_during_the_check_keeps_the_shared_check(
    hass: HomeAssistant,
) -> None:
    """The check is shared by all cameras, so one cancelled grab must not cancel it."""
    release = asyncio.Event()

    async def slow_check(_hass: HomeAssistant) -> bool:
        await release.wait()
        return True

    entity, _, _ = _entity(hass)
    with (
        patch.object(camera_module, "_async_check_ffmpeg", side_effect=slow_check),
        patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab,
    ):
        await entity.async_camera_image()
        for _ in range(5):
            await asyncio.sleep(0)
        task = entity._stream_still_task
        assert task is not None
        entity._cancel_stream_still()
        for _ in range(3):
            await asyncio.sleep(0)
        assert task.cancelled()
        release.set()
        await _settle(hass, entity)

        entity._stream_still_finished_at = None
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_awaited_once()
    assert entity._cached_snapshot == NEW_IMAGE


async def test_a_check_that_raises_is_retried_later(hass: HomeAssistant) -> None:
    """A check that could not run is not cached as "no ffmpeg" until a restart."""
    entity, camera, _ = _entity(hass)

    with (
        patch.object(
            camera_module, "_async_check_ffmpeg", side_effect=RuntimeError("executor gone")
        ),
        patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab,
    ):
        await entity.async_camera_image()
        await _settle(hass, entity)
        task = entity._stream_still_task
        assert task is not None
        assert task.exception() is None

    grab.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()
    assert entity._stream_still_failures == 0

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        entity._stream_still_finished_at = None
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_awaited_once()
    assert entity._cached_snapshot == NEW_IMAGE


# ---------------------------------------------------------------------------
# The ffmpeg read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [None, b"", RuntimeError("Connection refused")],
    ids=["none", "empty", "error"],
)
async def test_a_missed_read_is_one_failure_and_not_read_again(
    hass: HomeAssistant, first: object
) -> None:
    """The next attempt is the next grab, after the retry delay, with its own PUT."""
    entity, camera, _ = _entity(hass)
    grab = AsyncMock(side_effect=[first, NEW_IMAGE])

    with patch(GRAB, new=grab):
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_awaited_once_with(hass, FFMPEG_INPUT)
    camera.async_start_streaming.assert_awaited_once_with(rtmps_url=URL)
    assert entity._stream_still_failures == 1
    assert entity._cached_snapshot == OLD_IMAGE


def test_stall_timeout_leaves_room_under_ha_ffmpegs_timeout() -> None:
    """ha-ffmpeg's 15s read timeout logs an error with a traceback when it fires.

    The connect and handshake come first, so ffmpeg's stall timeout needs room below it.
    """
    assert camera_module._STREAM_STILL_STALL_TIMEOUT <= 10
    assert camera_module._STREAM_STILL_STALL_TIMEOUT < camera_module._STREAM_STILL_TIMEOUT


async def test_ffmpeg_input_keeps_an_odd_url_as_one_argument(hass: HomeAssistant) -> None:
    entity, _, _ = _entity(hass)
    odd = "rtmps://media-secured.nanit.com/nanit/baby 1.tok'en"

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity._ffmpeg_read(odd)

    # ha-ffmpeg shlex-splits a multi-word input the same way.
    assert shlex.split(grab.await_args.args[1]) == ["-rw_timeout", "10000000", "-i", odd]


# ---------------------------------------------------------------------------
# The retry delay counts from the end of a grab
# ---------------------------------------------------------------------------


async def test_retry_delay_counts_from_the_end_of_a_grab(hass: HomeAssistant) -> None:
    entity, camera, _ = _entity(hass)
    clock = [time.monotonic()]
    entity._cached_snapshot_at = clock[0] - MAX_AGE - 60

    async def slow_miss(_hass: HomeAssistant, source: str) -> None:
        clock[0] += 50  # the read takes a while

    with (
        patch("custom_components.nanit.camera.time.monotonic", side_effect=lambda: clock[0]),
        patch(GRAB, new=AsyncMock(side_effect=slow_miss)),
    ):
        start = clock[0]
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert entity._stream_still_finished_at == start + 50
        assert camera.async_start_streaming.await_count == 1

        # 60 seconds after the grab started, but only 10 after it ended.
        clock[0] = start + 60
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert camera.async_start_streaming.await_count == 1

        clock[0] = start + 50 + camera_module._STREAM_STILL_RETRY_DELAY
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert camera.async_start_streaming.await_count == 2


# ---------------------------------------------------------------------------
# Local streaming
# ---------------------------------------------------------------------------


async def test_grab_in_local_mode_reads_the_receiver(hass: HomeAssistant) -> None:
    """In local mode the grab reads the receiver and asks nothing of the cloud.

    The receiver's first reader is what starts the camera, so the grab itself
    sends no PUT_STREAMING.
    """
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    coordinator = _push_coordinator(_camera_state(sleep_mode=False))
    camera = _camera_mock()
    info = StreamStillInfo()
    entity = NanitCameraEntity(
        coordinator, camera, stream_stills=True, still_info=info, local=local
    )
    entity.hass = hass
    _disable_state_writes(entity)
    entity._cached_snapshot = OLD_IMAGE
    entity._cached_snapshot_at = time.monotonic() - MAX_AGE - 60

    with patch(GRAB, new=AsyncMock(return_value=NEW_IMAGE)) as grab:
        await entity.async_camera_image()
        await _settle(hass, entity)

    grab.assert_awaited_once_with(hass, f"-rw_timeout 10000000 -i {shlex.quote(FLV_URL)}")
    camera.async_start_streaming.assert_not_awaited()
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    assert entity._cached_snapshot == NEW_IMAGE
    assert entity._cloud_push_url is None
