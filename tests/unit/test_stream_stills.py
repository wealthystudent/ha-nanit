"""Opt-in stills from the live stream when Nanit's cloud snapshot fails."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Camera imports pull in turbojpeg; stub it like test_entities.py does.
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

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

GRAB = "custom_components.nanit.camera.ffmpeg_get_image"
URL = "rtmps://media-secured.nanit.com/nanit/baby_1.token"
OLD_IMAGE = b"old-image"
NEW_IMAGE = b"new-image"
MAX_AGE = camera_module._STREAM_STILL_MAX_AGE


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
    # stream_source() arms the expiry and keepalive timers; don't leak them.
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

        release.set()
        await _settle(hass, entity)

    grab.assert_awaited_once_with(hass, URL)
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
    entity, _, info = _entity(hass)
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

        # ...but once the retry delay has passed.
        assert entity._stream_still_attempt_at is not None
        entity._stream_still_attempt_at -= camera_module._STREAM_STILL_RETRY_DELAY
        await entity.async_camera_image()
        await _settle(hass, entity)
        assert grab.await_count == 2


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

        entity._stream_still_attempt_at = None
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


async def test_grab_never_stops_the_camera_stream(hass: HomeAssistant) -> None:
    """PUT_STREAMING STOPPED is camera-wide and would cut off a live viewer."""
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

    assert "Failed to read a still" in caplog.text
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
    """A grab sends PUT_STREAMING once; nothing renews it without a viewer.

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
        runtime_data=SimpleNamespace(cameras={"cam_1": cam_data}),
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
