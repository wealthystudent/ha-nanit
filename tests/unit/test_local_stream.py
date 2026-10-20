"""Tests for local streaming: mode routing, failure handling, repairs, setup."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Camera imports pull in turbojpeg; stub it like test_entities.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from aionanit.camera import STREAM_LIMIT_STATUS
from aionanit.local_stream import StreamChannel
from aionanit.models import ConnectionState
from custom_components.nanit import const as const_module
from custom_components.nanit import local_stream as ls
from custom_components.nanit.camera import NanitCameraEntity
from custom_components.nanit.const import (
    CONF_LOCAL_STREAM,
    CONF_LOCAL_STREAM_HOST,
    CONF_LOCAL_STREAM_PORT,
    DOMAIN,
)
from custom_components.nanit.log_redaction import redact_stream_token
from custom_components.nanit.repairs import LocalStreamRepairFlow, async_create_fix_flow

from .conftest import MOCK_EMAIL, mock_entry_data_v2
from .test_entities import _camera_state, _push_coordinator

FLV_URL = "http://127.0.0.1:40000/nanit/cam_1.flv?k=secret"
HOST = "192.168.1.10"


class _FakeServer:
    """Stands in for LocalStreamServer without binding anything."""

    def __init__(self) -> None:
        self.rtmp_port = 1935
        self.tls = True
        self.connection_attempts = 0
        self.protocol_failures = 0
        self.attempts_by_peer: dict[str, int] = {}
        self.failures_by_peer: dict[str, int] = {}
        self.channels: dict[str, StreamChannel] = {}

    def register(self, camera_uid: str, key: str) -> StreamChannel:
        channel = StreamChannel(camera_uid, key)
        self.channels[camera_uid] = channel
        return channel

    def unregister(self, camera_uid: str) -> None:
        self.channels.pop(camera_uid, None)

    def channel(self, camera_uid: str) -> StreamChannel | None:
        return None

    def flv_url(self, camera_uid: str) -> str:
        return FLV_URL

    def publish_url(self, host: str, camera_uid: str) -> str:
        return f"rtmps://{host}:{self.rtmp_port}/nanit/{self.channels[camera_uid].stream_key}"


def _entry(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options=options or {},
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def source_ip() -> Any:
    with (
        patch(
            "custom_components.nanit.local_stream.async_get_source_ip",
            AsyncMock(return_value=HOST),
        ) as mock,
        patch("custom_components.nanit.local_stream.async_get_routed_ip", return_value=HOST),
    ):
        yield mock


def _local_entry(hass: HomeAssistant, entry: MockConfigEntry) -> ls.LocalStreamEntry:
    return ls.LocalStreamEntry(hass, entry, _FakeServer())


_ENTITIES: list[NanitCameraEntity] = []


@pytest.fixture(autouse=True)
def _cancel_pause_timers(monkeypatch: pytest.MonkeyPatch) -> Any:
    owners: list[ls.LocalStreamEntry] = []
    original = ls.LocalStreamEntry.schedule_pause_end

    def tracking(self: ls.LocalStreamEntry) -> None:
        owners.append(self)
        original(self)

    monkeypatch.setattr(ls.LocalStreamEntry, "schedule_pause_end", tracking)
    yield
    for owner in owners:
        owner.cancel_timers()


@pytest.fixture(autouse=True)
def _cancel_entity_timers() -> Any:
    yield
    while _ENTITIES:
        entity = _ENTITIES.pop()
        entity._cancel_stream_timers()
        if entity._cancel_local_cooldown_timer is not None:
            entity._cancel_local_cooldown_timer()
            entity._cancel_local_cooldown_timer = None


def _entity(hass: HomeAssistant, local: ls.LocalStreamCamera, *, on: bool = True) -> tuple:
    coordinator = _push_coordinator(_camera_state(sleep_mode=not on))
    camera = MagicMock(uid="cam_1")
    camera.async_get_stream_rtmps_url = AsyncMock(return_value="rtmps://cloud/url")
    camera.async_start_streaming = AsyncMock()
    entity = NanitCameraEntity(coordinator, camera, local=local)
    entity.hass = hass
    _ENTITIES.append(entity)
    return entity, camera


# ----------------------------------------------------------------------
# Mode routing
# ----------------------------------------------------------------------


async def test_stream_source_is_the_flv_url_with_no_side_effects(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    local.channel.accepting = False

    assert await entity.stream_source() == FLV_URL
    camera.async_start_streaming.assert_not_awaited()
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    assert local.channel.accepting is True
    assert entity._cancel_stream_expiry_timer is None


async def test_stream_source_is_none_when_camera_asleep(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local, on=False)
    assert await entity.stream_source() is None


async def test_stream_source_uses_cloud_during_cooldown(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    local.start_cooldown("test")
    entity, camera = _entity(hass, local)

    assert await entity.stream_source() == "rtmps://cloud/url"
    camera.async_start_streaming.assert_awaited_once()


async def test_stream_source_uses_cloud_while_paused(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.paused_at = time.time()
    entity, _camera = _entity(hass, owner.camera("cam_1"))
    assert await entity.stream_source() == "rtmps://cloud/url"


async def test_pause_expires_after_a_day(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.paused_at = time.time() - ls.PAUSE_RETRY_AFTER - 1
    assert not owner.paused
    assert owner.camera("cam_1").active


async def test_stream_keys_persist_in_entry_data(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    first = _local_entry(hass, entry).camera("cam_1").channel.stream_key
    second = _local_entry(hass, entry).camera("cam_1").channel.stream_key
    assert first == second
    assert entry.data[ls.ENTRY_DATA_KEY]["keys"] == {"cam_1": first}
    assert CONF_LOCAL_STREAM not in entry.options


# ----------------------------------------------------------------------
# Starting the camera
# ----------------------------------------------------------------------


async def test_local_start_success_clears_strikes(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.strikes = 1
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)

    async def publishes(_timeout: float) -> bool:
        local.channel.publish_started(1, "192.168.1.107", lambda: None)
        return True

    waits: list[float] = []

    async def publishes_and_records(timeout: float) -> bool:
        waits.append(timeout)
        return await publishes(timeout)

    local.channel.wait_for_publish = publishes_and_records

    assert await entity._async_local_start() is True
    assert waits == [ls.PUBLISH_WAIT]
    url = camera.async_start_streaming.await_args.kwargs["rtmps_url"]
    assert url.startswith(f"rtmps://{HOST}:1935/nanit/")
    assert camera.async_start_streaming.await_args.kwargs["reconnect_on_failure"] is False
    assert owner.strikes == 0


async def test_stream_limit_is_never_a_strike(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    camera.async_start_streaming = AsyncMock(return_value=STREAM_LIMIT_STATUS)
    waits: list[float] = []

    async def nobody_comes(timeout: float) -> bool:
        waits.append(timeout)
        return False

    local.channel.wait_for_publish = nobody_comes
    cooldowns = []
    for _ in range(ls.STRIKES_TO_PAUSE + 1):
        local.end_cooldown()
        assert await entity._async_local_start() is False
        cooldowns.append(local.cooldown_until - time.monotonic())
    assert waits == [ls.BUSY_PUBLISH_WAIT] * (ls.STRIKES_TO_PAUSE + 1)
    assert owner.strikes == 0
    assert not owner.paused
    assert not local.channel.accepting
    assert local.last_fallback == "the camera is already streaming to as many apps as it allows"
    # Never a strike, but the backoff grows so a slot held for hours (our own
    # cloud push, other apps overnight) doesn't stall a viewer every cooldown.
    assert local.misses == ls.STRIKES_TO_PAUSE + 1
    assert cooldowns[0] <= ls.COOLDOWN < cooldowns[1] < cooldowns[2]


async def test_stream_limit_still_takes_a_push_that_arrives(
    hass: HomeAssistant, source_ip: Any
) -> None:
    # After an HA restart the camera is still pushing to the persisted URL,
    # so it answers the new request with the limit and then reconnects.
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    camera.async_start_streaming = AsyncMock(return_value=STREAM_LIMIT_STATUS)

    async def reconnects(_timeout: float) -> bool:
        local.channel.publish_started(1, "192.168.1.107", lambda: None)
        return True

    local.channel.wait_for_publish = reconnects
    assert await entity._async_local_start() is True
    assert owner.strikes == 0


async def test_other_declined_status_falls_back_without_waiting(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    camera.async_start_streaming = AsyncMock(return_value=500)
    local.channel.wait_for_publish = AsyncMock(return_value=True)
    assert await entity._async_local_start() is False
    local.channel.wait_for_publish.assert_not_awaited()
    assert owner.strikes == 0
    assert local.misses == 1
    assert local.last_fallback == "camera declined the local stream request (500)"


async def test_local_start_not_on_does_nothing(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local, on=False)
    assert await entity._async_local_start() is False
    camera.async_start_streaming.assert_not_awaited()


async def test_failed_command_is_a_cooldown_not_a_strike(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    camera.async_start_streaming = AsyncMock(side_effect=TimeoutError)

    assert await entity._async_local_start() is False
    assert owner.strikes == 0
    assert not local.active
    assert local.channel.accepting is False
    camera.async_start_streaming = AsyncMock()
    assert await entity.stream_source() == "rtmps://cloud/url"


async def test_two_clean_misses_pause_with_a_fixable_issue(
    hass: HomeAssistant, source_ip: Any
) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.channel.wait_for_publish = AsyncMock(return_value=False)

    assert await entity._async_local_start() is False
    assert owner.strikes == 1
    assert not owner.paused
    local.end_cooldown()
    assert await entity._async_local_start() is False
    assert owner.paused

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}")
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["address"] == f"{HOST}:1935"
    state = entry.data[ls.ENTRY_DATA_KEY]
    assert state["strikes"] == 2
    assert state["paused_at"] is not None
    assert entry.options == {}


async def test_connection_without_publish_is_not_a_strike(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    server = owner.server

    async def wait(_timeout: float) -> bool:
        server.connection_attempts += 1
        server.protocol_failures += 1
        return False

    local.channel.wait_for_publish = wait
    for _ in range(ls.PROTOCOL_FAILURES_TO_PAUSE):
        local.end_cooldown()
        assert await entity._async_local_start() is False
    assert owner.strikes == 0
    assert owner.paused
    assert owner.paused_reason == ls.ISSUE_PROTOCOL


async def test_miss_moves_a_running_hls_stream_to_the_cloud(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    local.channel.wait_for_publish = AsyncMock(return_value=False)

    await entity._async_local_start()
    stream.update_source.assert_called_once_with("rtmps://cloud/url")
    assert entity._cached_stream_source == "rtmps://cloud/url"


async def test_reset_stream_closes_local_subscribers(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    sub = local.channel.subscribe()
    await entity.async_reset_stream()
    assert sub.closed


async def test_camera_asleep_turns_consumers_away(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.async_write_ha_state = MagicMock()
    sub = local.channel.subscribe()
    entity.coordinator.data = _camera_state(sleep_mode=True)
    entity._handle_coordinator_update()
    assert local.channel.accepting is False
    assert sub.closed


async def test_keepalive_resends_the_local_url_while_consumed(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    sub = local.channel.subscribe()  # schedules the keepalive
    assert entity._cancel_stream_keepalive_timer is not None
    entity._handle_stream_keepalive()
    await hass.async_block_till_done()
    url = camera.async_start_streaming.await_args.kwargs["rtmps_url"]
    assert url.startswith(f"rtmps://{HOST}")
    sub.close()
    entity._cancel_stream_timers()


async def test_keepalive_waits_while_reconnecting(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.RECONNECTING)
    sub = local.channel.subscribe()
    entity._handle_stream_keepalive()
    await hass.async_block_till_done()
    camera.async_start_streaming.assert_not_awaited()
    sub.close()
    entity._cancel_stream_timers()


# ----------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------


async def test_options_step_saves_local_stream_settings(hass: HomeAssistant) -> None:
    entry = _entry(hass, {"speaker_ips": {"spk": "192.168.1.25"}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "local_stream" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "local_stream"}
    )
    assert result["step_id"] == "local_stream"
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_LOCAL_STREAM: True, CONF_LOCAL_STREAM_PORT: 1936, CONF_LOCAL_STREAM_HOST: "nope"},
    )
    assert bad["errors"] == {CONF_LOCAL_STREAM_HOST: "invalid_ip"}
    with patch.object(hass.config_entries, "async_reload"):
        done = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_LOCAL_STREAM: False, CONF_LOCAL_STREAM_PORT: 1936, CONF_LOCAL_STREAM_HOST: ""},
        )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "speaker_ips": {"spk": "192.168.1.25"},
        CONF_LOCAL_STREAM: False,
        CONF_LOCAL_STREAM_PORT: 1936,
    }


# ----------------------------------------------------------------------
# Repairs
# ----------------------------------------------------------------------


async def test_repair_retry_clears_the_pause_without_reload(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    local = owner.camera("cam_1")
    owner.strikes = 2
    owner.paused_at = time.time()
    local.start_cooldown("x")
    entry.runtime_data = SimpleNamespace(local_stream=owner)

    flow = await async_create_fix_flow(
        hass, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}", {"entry_id": entry.entry_id}
    )
    assert isinstance(flow, LocalStreamRepairFlow)
    flow.hass = hass
    flow.issue_id = f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}"  # set by HA's manager
    menu = await flow.async_step_init()
    assert menu["type"] is FlowResultType.MENU
    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await flow.async_step_retry()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_not_called()
    assert not owner.paused
    assert owner.strikes == 0
    assert local.active


async def test_repair_disable_turns_the_option_off(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    flow = LocalStreamRepairFlow(entry.entry_id)
    flow.hass = hass
    with patch.object(hass.config_entries, "async_reload"):
        await flow.async_step_disable()
    assert entry.options[CONF_LOCAL_STREAM] is False


async def test_unknown_issue_has_no_fix_flow(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="Unknown"):
        await async_create_fix_flow(hass, "something_else", None)


# ----------------------------------------------------------------------
# Real receiver
# ----------------------------------------------------------------------


@pytest.fixture
def _sockets(request: pytest.FixtureRequest) -> None:
    with contextlib.suppress(pytest.FixtureLookupError):
        request.getfixturevalue("socket_enabled")


@pytest.mark.local_stream_receiver
@pytest.mark.usefixtures("_sockets")
async def test_setup_starts_one_shared_receiver(hass: HomeAssistant) -> None:
    first = _entry(hass, {CONF_LOCAL_STREAM_PORT: 0})
    second = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id="b")
    second.add_to_hass(hass)

    local_a = await ls.async_setup_local_stream(hass, first)
    local_b = await ls.async_setup_local_stream(hass, second)
    assert local_a is not None
    assert local_b is not None
    assert local_a.server is local_b.server
    assert local_a.server.tls
    cert_dir = Path(hass.config.path(".storage", "nanit_local_stream"))
    assert (cert_dir / "key.pem").stat().st_mode & 0o777 == 0o600

    await ls.async_unload_local_stream(hass, local_a)
    assert ls.DATA_KEY in hass.data
    await ls.async_unload_local_stream(hass, local_b)
    assert ls.DATA_KEY not in hass.data


@pytest.mark.local_stream_receiver
@pytest.mark.usefixtures("_sockets")
async def test_port_in_use_raises_an_issue_and_falls_back(hass: HomeAssistant) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        entry = _entry(hass, {CONF_LOCAL_STREAM_PORT: port, CONF_LOCAL_STREAM_HOST: "192.168.1.10"})
        assert await ls.async_setup_local_stream(hass, entry) is None
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ls.ISSUE_PORT_IN_USE}_{entry.entry_id}"
        )
        assert issue is not None
        assert not issue.is_fixable
    finally:
        blocker.close()


async def test_disabled_option_skips_the_receiver(hass: HomeAssistant) -> None:
    entry = _entry(hass, {CONF_LOCAL_STREAM: False})
    assert await ls.async_setup_local_stream(hass, entry) is None


def test_tls_material_is_created_once(tmp_path: Path) -> None:
    ls._load_or_create_tls(tmp_path)
    cert = (tmp_path / "cert.pem").read_bytes()
    ls._load_or_create_tls(tmp_path)
    assert (tmp_path / "cert.pem").read_bytes() == cert
    assert (tmp_path / "key.pem").stat().st_mode & 0o777 == 0o600


# ----------------------------------------------------------------------
# Redaction and diagnostics
# ----------------------------------------------------------------------


def test_redaction_masks_local_secrets() -> None:
    line = (
        "Error opening stream (http://127.0.0.1:41234/nanit/cam_1.flv?k=AbC-123_x) "
        "push rtmps://192.168.1.10:1935/nanit/Key-456_y and "
        "rtmps://media-secured.nanit.com/nanit/baby1.eyJhbGciOi.payload.sig"
    )
    redacted = redact_stream_token(line)
    assert "AbC-123_x" not in redacted
    assert "Key-456_y" not in redacted
    assert "eyJhbGciOi" not in redacted
    assert "/nanit/cam_1.flv?k=***" in redacted
    assert "rtmps://192.168.1.10:1935/nanit/***" in redacted
    assert "media-secured.nanit.com/nanit/baby1.***" in redacted


async def test_diagnostics_redact_keys_and_addresses(hass: HomeAssistant) -> None:
    from homeassistant.components.diagnostics import async_redact_data

    from custom_components.nanit.diagnostics import TO_REDACT

    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.camera("cam_1")
    owner.last_host = HOST
    diag = async_redact_data(owner.diagnostics(), TO_REDACT)
    assert diag["advertised_host"] == "**REDACTED**"
    assert diag["cameras"][0]["mode"] == "local"
    data = async_redact_data(dict(entry.data), TO_REDACT)
    assert data[ls.ENTRY_DATA_KEY] == "**REDACTED**"


# ----------------------------------------------------------------------
# Review fixes: keepalive routing, cloud hold, background fallback, pause
# ----------------------------------------------------------------------


async def test_watched_cloud_stream_moves_back_to_local_in_place(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    local.channel.wait_for_publish = AsyncMock(return_value=False)
    await entity._async_local_start()
    await hass.async_block_till_done()
    stream.update_source.assert_called_once_with("rtmps://cloud/url")
    stream.update_source.reset_mock()

    local.end_cooldown()  # cooldown over while the viewer keeps watching

    stream.update_source.assert_called_once_with(FLV_URL)
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is None
    assert entity._cancel_stream_expiry_timer is None
    assert local.channel.accepting is True
    assert local.active


async def test_preloaded_stream_does_not_pin_the_cloud(hass: HomeAssistant) -> None:
    """A stream whose outputs never idle out still goes back to local."""
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}  # forever, like preload
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    local.start_cooldown("x")
    local.cooldown_until = 0.0
    entity._handle_local_cooldown_end()
    assert local.active
    stream.update_source.assert_called_once_with(FLV_URL)
    assert entity._cancel_local_cooldown_timer is None


async def test_webrtc_only_cloud_cache_is_dropped_at_cooldown_end(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity._cached_stream_source = "rtmps://cloud/url"
    entity._stream_source_started_at = time.monotonic()
    entity._schedule_stream_expiry_timer()
    local.cooldown_until = 0.0
    sub = local.channel.subscribe()
    entity._handle_local_cooldown_end()
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_expiry_timer is None
    assert not sub.closed  # nothing later expires the old cloud URL onto them
    sub.close()


async def test_pause_end_hands_cameras_back_to_local(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    owner.paused_at = time.time() - ls.PAUSE_RETRY_AFTER - 1  # already lapsed
    owner.schedule_pause_end()
    assert owner._cancel_pause_timer is not None
    owner._pause_ended()
    stream.update_source.assert_called_once_with(FLV_URL)
    owner.cancel_timers()


async def test_still_paused_cooldown_end_stays_on_cloud(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.paused_at = time.time()
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    entity._handle_local_cooldown_end()
    stream.update_source.assert_not_called()
    assert entity._cached_stream_source == "rtmps://cloud/url"
    assert local.channel.accepting is False


async def test_cooldown_end_drops_an_idle_cloud_stream(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {}
    stream.stop = AsyncMock()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    local.cooldown_until = 0.0
    entity._handle_local_cooldown_end()
    await hass.async_block_till_done()
    assert entity.stream is None
    assert local.channel.accepting is True


async def test_fallback_does_not_hold_the_start_open(hass: HomeAssistant, source_ip: Any) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    local.channel.wait_for_publish = AsyncMock(return_value=False)
    release = asyncio.Event()

    async def slow_url() -> str:
        await release.wait()
        return "rtmps://cloud/url"

    camera.async_get_stream_rtmps_url = slow_url
    assert await entity._async_local_start() is False  # returns before the cloud start
    stream.update_source.assert_not_called()
    release.set()
    await hass.async_block_till_done()
    stream.update_source.assert_called_once_with("rtmps://cloud/url")


async def test_scanner_connection_does_not_hide_a_clean_miss(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.channel.publisher_peer = "192.168.1.107"  # learned from an earlier push
    entity, _camera = _entity(hass, local)
    server = owner.server
    server.attempts_by_peer = {}
    server.failures_by_peer = {}

    async def scanner(_timeout: float) -> bool:
        server.connection_attempts += 1
        server.attempts_by_peer["192.168.1.50"] = 1
        return False

    local.channel.wait_for_publish = scanner
    assert await entity._async_local_start() is False
    assert owner.strikes == 1


async def test_no_advertisable_address_is_a_cooldown(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    with patch(
        "custom_components.nanit.local_stream.async_get_source_ip",
        AsyncMock(side_effect=OSError("no IPv4")),
    ):
        assert await entity._async_local_start() is False
    camera.async_start_streaming.assert_not_awaited()
    assert owner.strikes == 0
    assert local.last_fallback == "no local address to advertise"


async def test_paused_entry_turns_consumers_away_from_the_start(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.paused_at = time.time()
    local = owner.camera("cam_1")
    _entity(hass, local)
    assert local.channel.accepting is False


async def test_retry_now_refreshes_the_camera(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    _entity(hass, local)
    local.start_cooldown("x")
    local.channel.accepting = False
    owner.async_retry_now()
    assert local.channel.accepting is True


async def test_pause_keeps_the_host_for_the_repair_after_restart(
    hass: HomeAssistant, source_ip: Any
) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    assert entry.data[ls.ENTRY_DATA_KEY]["host"] == HOST
    restored = _local_entry(hass, entry)
    assert restored.paused
    restored.async_raise_issue(ls.ISSUE_UNREACHABLE, restored.last_host or "")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["address"] == f"{HOST}:1935"


async def test_options_change_clears_the_pause_on_the_next_setup(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with patch.object(ls, "_async_acquire_server", AsyncMock(return_value=_FakeServer())):
        first = await ls.async_setup_local_stream(hass, entry)
        assert first is not None
        first.record_strike(HOST)
        first.record_strike(HOST)
        assert first.paused
        # Same options: still paused after a reload.
        again = await ls.async_setup_local_stream(hass, entry)
        assert again is not None
        assert again.paused
        # Any steering option changes (here the advertised address): retried at once.
        hass.config_entries.async_update_entry(
            entry, options={CONF_LOCAL_STREAM_HOST: "192.168.1.5"}
        )
        changed = await ls.async_setup_local_stream(hass, entry)
    assert changed is not None
    assert not changed.paused
    assert changed.strikes == 0
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}")
        is None
    )
    for local in (first, again, changed):
        local.cancel_timers()


async def test_prune_forgets_departed_cameras(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.camera("cam_1")
    owner.camera("gone")
    owner.prune_keys({"cam_1"})
    assert set(entry.data[ls.ENTRY_DATA_KEY]["keys"]) == {"cam_1"}


# ----------------------------------------------------------------------
# Second adversarial round: lifecycle wiring and the surviving mutations
# ----------------------------------------------------------------------


@pytest.mark.local_stream_receiver
@pytest.mark.usefixtures("_sockets")
async def test_entry_setup_wires_the_receiver_end_to_end(
    hass: HomeAssistant, mock_nanit_client: Any
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    from custom_components.nanit import async_setup_entry
    from custom_components.nanit.camera import async_setup_entry as camera_setup

    entry = _entry(hass, {CONF_LOCAL_STREAM_PORT: 0})
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ):
        assert await async_setup_entry(hass, entry)
    local = entry.runtime_data.local_stream
    assert local is not None
    assert ls.DATA_KEY in hass.data

    added: list[NanitCameraEntity] = []
    await camera_setup(hass, entry, lambda entities: added.extend(entities))
    assert added
    assert added[0]._local is not None
    assert added[0]._local.channel.start_callback is not None
    _ENTITIES.extend(added)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert ls.DATA_KEY not in hass.data
    await entry._async_process_on_unload(hass)  # unload after stop stays harmless
    assert ls.DATA_KEY not in hass.data


@pytest.mark.local_stream_receiver
@pytest.mark.usefixtures("_sockets")
async def test_unload_releases_the_receiver(hass: HomeAssistant) -> None:
    entry = _entry(hass, {CONF_LOCAL_STREAM_PORT: 0})
    local = await ls.async_setup_local_stream(hass, entry)
    assert local is not None
    local.camera("cam_1")
    server = local.server
    await ls.async_unload_local_stream(hass, local)
    assert server.channel("cam_1") is None
    assert ls.DATA_KEY not in hass.data


async def test_success_clears_the_issues(hass: HomeAssistant, source_ip: Any) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    issue_id = f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    owner.record_success()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_strike_resets_the_protocol_count(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.record_protocol_failure()
    owner.record_protocol_failure()
    owner.record_strike(HOST)
    owner.record_protocol_failure()
    assert not owner.paused


async def test_setup_re_raises_the_issue_while_paused(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            ls.ENTRY_DATA_KEY: {
                "keys": {},
                "strikes": 2,
                "paused_at": time.time(),
                "paused_reason": ls.ISSUE_UNREACHABLE,
                "host": HOST,
            },
        },
    )
    with patch.object(ls, "_async_acquire_server", AsyncMock(return_value=_FakeServer())):
        local = await ls.async_setup_local_stream(hass, entry)
    assert local is not None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}")
    assert issue is not None


async def test_turning_local_off_clears_its_issues(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    for issue in (ls.ISSUE_PORT_IN_USE, ls.ISSUE_UNREACHABLE, ls.ISSUE_PROTOCOL):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{issue}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
        )
    hass.config_entries.async_update_entry(entry, options={CONF_LOCAL_STREAM: False})
    assert await ls.async_setup_local_stream(hass, entry) is None
    for issue in (ls.ISSUE_PORT_IN_USE, ls.ISSUE_UNREACHABLE, ls.ISSUE_PROTOCOL):
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{entry.entry_id}") is None


async def test_miss_schedules_the_cooldown_end(hass: HomeAssistant, source_ip: Any) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.channel.wait_for_publish = AsyncMock(return_value=False)
    await entity._async_local_start()
    assert entity._cancel_local_cooldown_timer is not None


async def test_miss_drops_an_idle_hls_stream_on_the_flv_url(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {}
    stream.stop = AsyncMock()
    entity.stream = stream
    local.channel.wait_for_publish = AsyncMock(return_value=False)
    await entity._async_local_start()
    await hass.async_block_till_done()
    assert entity.stream is None


async def test_slow_ack_is_a_cooldown(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    camera.async_start_streaming = never
    with (
        patch.object(ls, "ACK_TIMEOUT", 0.05),
        patch("custom_components.nanit.camera.ACK_TIMEOUT", 0.05),
    ):
        assert await entity._async_local_start() is False
    assert owner.strikes == 0
    assert not local.active


async def test_sleep_during_the_wait_is_not_a_strike(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)

    async def sleepy(_timeout: float) -> bool:
        entity.coordinator.data = _camera_state(sleep_mode=True)
        return False

    local.channel.wait_for_publish = sleepy
    assert await entity._async_local_start() is False
    assert owner.strikes == 0


async def test_removal_clears_callbacks(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.entity_id = "camera.nursery"
    await entity.async_will_remove_from_hass()
    assert local.channel.start_callback is None
    assert local.channel.on_consumers_changed is None
    assert local.on_mode_changed is None


async def test_keepalive_skips_without_local_consumers(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    entity._handle_stream_keepalive()
    await hass.async_block_till_done()
    camera.async_start_streaming.assert_not_awaited()


async def test_invalidate_closes_local_subscribers(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    sub = local.channel.subscribe()
    entity._invalidate_stream("test")
    assert sub.closed


async def test_learned_publisher_peer_targets_the_route(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    cam = owner.camera("cam_1")
    cam.channel.publisher_peer = "10.30.0.9"
    with patch.object(ls, "async_get_routed_ip", return_value="10.30.0.2") as routed:
        assert await owner.async_advertised_host() == "10.30.0.2"
    routed.assert_called_once_with("10.30.0.9")


async def test_shared_camera_stays_on_cloud_for_the_second_account(hass: HomeAssistant) -> None:
    server = _FakeServer()
    server.channel = lambda uid: server.channels.get(uid)
    first = ls.LocalStreamEntry(hass, _entry(hass), server)
    other = MockConfigEntry(domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id="b")
    other.add_to_hass(hass)
    second = ls.LocalStreamEntry(hass, other, server)
    assert first.camera("cam_1") is not None
    assert second.camera("cam_1") is None


async def test_diagnostics_redact_the_peer_and_host_option(hass: HomeAssistant) -> None:
    from homeassistant.components.diagnostics import async_redact_data

    from custom_components.nanit.diagnostics import TO_REDACT

    owner = _local_entry(hass, _entry(hass))
    cam = owner.camera("cam_1")
    cam.channel.publisher_peer = "192.168.1.107"
    diag = async_redact_data(owner.diagnostics(), TO_REDACT)
    assert diag["cameras"][0]["publisher_peer"] == "**REDACTED**"
    options = async_redact_data({CONF_LOCAL_STREAM_HOST: HOST}, TO_REDACT)
    assert options[CONF_LOCAL_STREAM_HOST] == "**REDACTED**"


def test_redaction_covers_encoded_and_later_params() -> None:
    encoded = "src=http%3A%2F%2F127.0.0.1%3A4%2Fnanit%2Fcam_1.flv%3Fk%3DSECRET2&x"
    assert "SECRET2" not in redact_stream_token(encoded)
    later = "http://127.0.0.1:4/nanit/cam_1.flv?x=1&k=SECRET3"
    assert "SECRET3" not in redact_stream_token(later)


# ----------------------------------------------------------------------
# Round-2 survivors (integration)
# ----------------------------------------------------------------------


async def test_retry_cancels_the_pending_cooldown_timer(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.channel.wait_for_publish = AsyncMock(return_value=False)
    await entity._async_local_start()
    timer = entity._cancel_local_cooldown_timer
    assert timer is not None
    with patch.object(entity, "_cancel_local_cooldown_timer", MagicMock()) as cancel:
        local.end_cooldown()
    cancel.assert_called_once()


async def test_learned_publisher_peer_scopes_the_counts(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.channel.publisher_peer = "192.168.1.107"
    entity, _camera = _entity(hass, local)
    server = owner.server

    async def scanner(_timeout: float) -> bool:
        server.connection_attempts += 1
        server.attempts_by_peer["192.168.1.50"] = 1
        return False

    local.channel.wait_for_publish = scanner
    await entity._async_local_start()
    assert owner.strikes == 1


async def test_unparsable_learned_peer_is_skipped_for_routing(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.camera("cam_1").channel.publisher_peer = "not-an-ip"
    owner.camera("cam_2").channel.publisher_peer = "FD00:0:0::9"
    with patch.object(ls, "async_get_routed_ip", return_value="fd00::2") as routed:
        assert await owner.async_advertised_host() == "fd00::2"
    routed.assert_called_once_with("fd00::9")


async def test_publisher_peer_is_compared_in_canonical_form(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.channel.publisher_peer = "FD00:0:0::7"
    entity, _camera = _entity(hass, local)
    server = owner.server

    async def camera_tried(_timeout: float) -> bool:
        server.attempts_by_peer["fd00::7"] = 1
        server.failures_by_peer["fd00::7"] = 1
        return False

    local.channel.wait_for_publish = camera_tried
    await entity._async_local_start()
    assert owner.strikes == 0
    assert owner.protocol_failures == 1


async def test_removal_cancels_the_start_and_the_fallback(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.entity_id = "camera.nursery"
    fallback = MagicMock()
    fallback.done.return_value = False
    entity._local_fallback_task = fallback
    with patch.object(local.channel, "cancel_start") as cancel_start:
        await entity.async_will_remove_from_hass()
    cancel_start.assert_called_once()
    fallback.cancel.assert_called_once()


async def test_one_hls_move_at_a_time(hass: HomeAssistant, source_ip: Any) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    entity.stream = stream
    release = asyncio.Event()

    async def slow_url() -> str:
        await release.wait()
        return "rtmps://cloud/url"

    camera.async_get_stream_rtmps_url = slow_url
    entity._fall_back_to_cloud("a")
    first = entity._local_fallback_task
    entity._fall_back_to_cloud("b")
    assert entity._local_fallback_task is first
    release.set()
    await hass.async_block_till_done()
    stream.update_source.assert_called_once()


async def test_camera_platform_prunes_departed_keys(
    hass: HomeAssistant,
) -> None:
    from custom_components.nanit.camera import async_setup_entry as camera_setup

    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.camera("gone")
    entry.runtime_data = SimpleNamespace(local_stream=owner, cameras={})
    await camera_setup(hass, entry, lambda entities: None)
    assert "gone" not in entry.data[ls.ENTRY_DATA_KEY]["keys"]


async def test_same_entry_re_registers_its_own_camera(hass: HomeAssistant) -> None:
    server = _FakeServer()
    server.channel = lambda uid: server.channels.get(uid)
    owner = ls.LocalStreamEntry(hass, _entry(hass), server)
    assert owner.camera("cam_1") is not None
    assert owner.camera("cam_1") is not None


async def test_docker_variant_only_off_home_assistant_os(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    issue_id = f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}"
    for hassio, expected in ((True, ls.ISSUE_UNREACHABLE), (False, ls.ISSUE_UNREACHABLE_DOCKER)):
        with (
            patch.object(ls, "is_docker_env", return_value=True),
            patch.object(ls, "is_hassio", return_value=hassio),
        ):
            owner.async_raise_issue(ls.ISSUE_UNREACHABLE, "172.30.32.1")
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_key == expected


async def test_protocol_pause_is_persisted(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    for _ in range(ls.PROTOCOL_FAILURES_TO_PAUSE):
        owner.record_protocol_failure()
    assert entry.data[ls.ENTRY_DATA_KEY]["paused_reason"] == ls.ISSUE_PROTOCOL


def test_redaction_handles_case_and_encoded_ampersand() -> None:
    line = "SRC=HTTP%3A%2F%2F127.0.0.1%3A4%2FNANIT%2Fcam_1.FLV%3Fx%3D1%26K%3DSECRET9"
    assert "SECRET9" not in redact_stream_token(line)
    first = "http://127.0.0.1:4/nanit/cam_1.flv?k=REAL1%26k=OTHER"
    assert "REAL1" not in redact_stream_token(first)


# ----------------------------------------------------------------------
# Round 3
# ----------------------------------------------------------------------


def _watched_stream(source: str = "rtmps://cloud/url") -> MagicMock:
    stream = MagicMock()
    stream.outputs.return_value = {"hls": object()}
    stream.source = source
    stream.stop = AsyncMock()
    return stream


async def test_pause_timer_that_fires_early_tries_again(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.stream = _watched_stream()
    owner.paused_at = time.time()  # still paused by the wall clock
    owner._pause_ended()
    entity.stream.update_source.assert_not_called()
    assert owner._cancel_pause_timer is not None


async def test_strike_pause_schedules_the_end_and_it_hands_back(
    hass: HomeAssistant,
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    assert owner._cancel_pause_timer is not None
    owner.cancel_timers()  # fire it by hand instead
    entity.stream = _watched_stream()
    owner.paused_at = time.time() - ls.PAUSE_RETRY_AFTER - 1
    owner._pause_ended()
    entity.stream.update_source.assert_called_once_with(FLV_URL)


async def test_setup_while_paused_schedules_the_end(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            ls.ENTRY_DATA_KEY: {
                "strikes": 2,
                "paused_at": time.time(),
                "paused_reason": ls.ISSUE_UNREACHABLE,
            },
        },
    )
    with patch.object(ls, "_async_acquire_server", AsyncMock(return_value=_FakeServer())):
        local = await ls.async_setup_local_stream(hass, entry)
    assert local is not None
    assert local._cancel_pause_timer is not None


async def test_unload_cancels_the_pause_timer_for_good(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    with patch.object(ls, "_async_release_server", AsyncMock()):
        await ls.async_unload_local_stream(hass, owner)
    assert owner._cancel_pause_timer is None
    owner.record_strike(HOST)  # a late miss after unload
    assert owner._cancel_pause_timer is None


async def test_retry_cancels_the_pause_timer(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    owner.async_retry_now()
    assert owner._cancel_pause_timer is None


async def test_cooldown_end_cancels_a_cloud_task_in_flight(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    task = MagicMock()
    task.done.return_value = False
    refresh = MagicMock()
    refresh.done.return_value = False
    entity._local_fallback_task = task
    entity._stream_refresh_task = refresh
    entity._handle_local_cooldown_end()
    task.cancel.assert_called_once()
    refresh.cancel.assert_called_once()


async def test_hls_move_aborts_once_local_took_over(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = _watched_stream(FLV_URL)
    entity.stream = stream
    await entity._async_move_hls_to_cloud()  # local is active
    stream.update_source.assert_not_called()
    assert entity._cached_stream_source is None


async def test_rotation_aborts_once_local_took_over(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = _watched_stream()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    await entity._async_refresh_stream_source("test")
    # Finished as a move to local instead of left half switched.
    stream.update_source.assert_called_once_with(FLV_URL)
    assert entity._cached_stream_source is None


async def test_cooldown_end_follows_what_the_stream_plays(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = _watched_stream("rtmps://old-cloud/url")
    entity.stream = stream
    entity._cached_stream_source = None  # a failed renewal dropped it
    entity._handle_local_cooldown_end()
    stream.update_source.assert_called_once_with(FLV_URL)


async def test_stream_already_local_is_not_restarted(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = _watched_stream(FLV_URL)
    entity.stream = stream
    entity._handle_local_cooldown_end()
    stream.update_source.assert_not_called()
    assert entity.stream is stream


async def test_sleeping_camera_drops_the_stream_at_cooldown_end(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local, on=False)
    stream = _watched_stream()
    entity.stream = stream
    entity._handle_local_cooldown_end()
    await hass.async_block_till_done()
    stream.update_source.assert_not_called()
    assert entity.stream is None


async def test_turning_local_off_clears_the_stored_pause(hass: HomeAssistant) -> None:
    entry = _entry(hass, {CONF_LOCAL_STREAM: False})
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, ls.ENTRY_DATA_KEY: {"strikes": 2, "paused_at": time.time()}},
    )
    assert await ls.async_setup_local_stream(hass, entry) is None
    state = entry.data[ls.ENTRY_DATA_KEY]
    assert state["paused_at"] is None
    assert state["strikes"] == 0


async def test_misses_back_off_and_a_publish_resets(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    durations = []
    for _ in range(8):
        local.start_cooldown("x")
        durations.append(local.cooldown_seconds())
    assert durations[:3] == [ls.COOLDOWN, ls.COOLDOWN * 2, ls.COOLDOWN * 4]
    assert durations[-1] == ls.MAX_COOLDOWN
    local.record_publish()
    local.start_cooldown("x")
    assert local.cooldown_seconds() == ls.COOLDOWN


async def test_fallback_timer_uses_the_backoff(hass: HomeAssistant, source_ip: Any) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.misses = 3
    with patch.object(entity, "_schedule_local_cooldown_end") as schedule:
        entity._fall_back_to_cloud("x")
    (delay,) = schedule.call_args.args
    assert delay == pytest.approx(ls.COOLDOWN * 8, abs=1)


@pytest.mark.parametrize(
    "options",
    [{CONF_LOCAL_STREAM_PORT: 1940}, {CONF_LOCAL_STREAM_HOST: "192.168.1.20"}],
)
async def test_port_or_host_change_counts_as_an_options_change(
    hass: HomeAssistant, options: dict[str, Any]
) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner._persist()
    hass.config_entries.async_update_entry(entry, options=options)
    assert _local_entry(hass, entry).options_changed()


async def test_first_setup_records_the_fingerprint(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with patch.object(ls, "_async_acquire_server", AsyncMock(return_value=_FakeServer())):
        await ls.async_setup_local_stream(hass, entry)
    assert entry.data[ls.ENTRY_DATA_KEY]["options_fp"]


async def test_clear_pause_resets_everything_and_persists(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.record_protocol_failure()
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    owner.clear_pause()
    assert owner.protocol_failures == 0
    assert entry.data[ls.ENTRY_DATA_KEY]["strikes"] == 0
    assert entry.data[ls.ENTRY_DATA_KEY]["paused_at"] is None


async def test_unparsable_publisher_peer_falls_back_to_global_counts(
    hass: HomeAssistant, source_ip: Any
) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.channel.publisher_peer = "not-an-ip"
    entity, _camera = _entity(hass, local)
    server = owner.server

    async def someone_connected(_timeout: float) -> bool:
        server.connection_attempts += 1
        return False

    local.channel.wait_for_publish = someone_connected
    await entity._async_local_start()
    assert owner.strikes == 0  # the global count saw a connection


def test_redaction_masks_the_first_secret() -> None:
    line = "http://127.0.0.1:4/nanit/cam_1.flv?a=1&k=REAL&k=X"
    assert "REAL" not in redact_stream_token(line)


# ----------------------------------------------------------------------
# Round 4
# ----------------------------------------------------------------------


async def test_mode_notify_keeps_a_local_streams_keepalive(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    sub = local.channel.subscribe()  # arms the keepalive
    entity.stream = _watched_stream(FLV_URL)
    local.notify_mode_changed()
    assert entity._cancel_stream_keepalive_timer is not None
    sub.close()


async def test_cloud_cache_dropped_but_local_keepalive_rearmed(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    sub = local.channel.subscribe()
    entity._cached_stream_source = "rtmps://cloud/url"
    entity._handle_local_cooldown_end()
    assert entity._cached_stream_source is None
    assert entity._cancel_stream_keepalive_timer is not None
    sub.close()


async def test_publish_resets_the_backoff(hass: HomeAssistant, source_ip: Any) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.misses = 3

    async def publishes(_timeout: float) -> bool:
        local.channel.publish_started(1, None, lambda: None)
        return True

    local.channel.wait_for_publish = publishes
    assert await entity._async_local_start() is True
    assert local.misses == 0


async def test_retry_resets_the_backoff(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.misses = 7
    owner.async_retry_now()
    assert local.misses == 0


async def test_sleep_does_not_grow_the_backoff(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)

    async def sleepy(_timeout: float) -> bool:
        entity.coordinator.data = _camera_state(sleep_mode=True)
        return False

    local.channel.wait_for_publish = sleepy
    await entity._async_local_start()
    assert local.misses == 0
    assert not local.active


@pytest.mark.parametrize(
    "state",
    [
        {"strikes": 0, "paused_at": 1.0, "paused_reason": "local_stream_protocol_error"},
        {"strikes": 1, "paused_at": None},
    ],
)
async def test_turning_local_off_clears_any_stored_pause(
    hass: HomeAssistant, state: dict[str, Any]
) -> None:
    entry = _entry(hass, {CONF_LOCAL_STREAM: False})
    hass.config_entries.async_update_entry(entry, data={**entry.data, ls.ENTRY_DATA_KEY: state})
    await ls.async_setup_local_stream(hass, entry)
    stored = entry.data[ls.ENTRY_DATA_KEY]
    assert stored["paused_at"] is None
    assert stored["strikes"] == 0


async def test_early_pause_end_does_not_notify(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    local.on_mode_changed = MagicMock()
    owner.paused_at = time.time()
    owner._pause_ended()
    local.on_mode_changed.assert_not_called()


# ----------------------------------------------------------------------
# Fable gate
# ----------------------------------------------------------------------


async def test_repair_menu_shows_the_address(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    owner.async_raise_issue(ls.ISSUE_UNREACHABLE, HOST)
    issue_id = f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}"
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.issue_id = issue_id
    menu = await flow.async_step_init()
    assert menu["description_placeholders"]["address"] == f"{HOST}:1935"


async def test_no_start_while_the_relay_reconnects(hass: HomeAssistant, source_ip: Any) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.RECONNECTING)
    assert await entity._async_local_start() is False
    camera.async_start_streaming.assert_not_awaited()
    assert local.active  # no cooldown
    assert local.misses == 0


async def test_unsolicited_publish_clears_the_pause(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    owner = _local_entry(hass, entry)
    local = owner.camera("cam_1")
    _entity(hass, local)
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    local.misses = 4
    local.channel.publish_started(9, "192.168.1.107", lambda: None)
    assert not owner.paused
    assert owner.strikes == 0
    assert local.misses == 0
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"{ls.ISSUE_UNREACHABLE}_{entry.entry_id}")
        is None
    )


# ----------------------------------------------------------------------
# Round 5
# ----------------------------------------------------------------------


async def test_idle_hls_drop_keeps_local_readers_and_keepalive(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    sub = local.channel.subscribe()
    stream = _watched_stream()
    stream.outputs.return_value = {}
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    entity._handle_local_cooldown_end()
    await hass.async_block_till_done()
    assert entity.stream is None
    assert not sub.closed
    assert entity._cancel_stream_keepalive_timer is not None
    sub.close()


async def test_no_address_grows_the_backoff(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    with patch(
        "custom_components.nanit.local_stream.async_get_source_ip",
        AsyncMock(side_effect=OSError("no IPv4")),
    ):
        await entity._async_local_start()
    assert local.misses == 1


async def test_sleep_miss_gets_the_base_cooldown(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    local.misses = 6
    local.start_cooldown("sleep", count_miss=False)
    remaining = local.cooldown_until - time.monotonic()
    assert remaining <= ls.COOLDOWN + 1


async def test_refresh_with_local_active_sends_nothing_to_the_cloud(
    hass: HomeAssistant,
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    entity.stream = _watched_stream()
    entity._cached_stream_source = "rtmps://cloud/url"
    await entity._async_refresh_stream_source("test")
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()


async def test_move_with_local_active_sends_nothing_to_the_cloud(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    entity.stream = _watched_stream()
    await entity._async_move_hls_to_cloud()
    camera.async_get_stream_rtmps_url.assert_not_awaited()
    camera.async_start_streaming.assert_not_awaited()


async def test_local_taking_over_during_the_put_points_the_camera_back(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    local.start_cooldown("x")
    entity.stream = _watched_stream()
    sub = local.channel.subscribe()
    puts: list[str] = []

    async def start_streaming(*, rtmps_url: str, **_kwargs: Any) -> None:
        puts.append(rtmps_url)
        if rtmps_url == "rtmps://cloud/url":
            local.end_cooldown()  # local comes back while the cloud PUT is out

    camera.async_start_streaming = start_streaming
    await entity._async_move_hls_to_cloud()
    await hass.async_block_till_done()
    assert puts[0] == "rtmps://cloud/url"
    assert any(url.startswith(f"rtmps://{HOST}") for url in puts[1:])
    sub.close()


async def test_publish_during_a_cooldown_ends_it(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    _entity(hass, local)
    local.start_cooldown("x")
    local.channel.accepting = False
    local.channel.publish_started(3, None, lambda: None)
    assert local.active
    assert local.channel.accepting is True


async def test_keepalive_stops_while_paused(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, camera = _entity(hass, local)
    sub = local.channel.subscribe()
    owner.paused_at = time.time()
    entity._handle_stream_keepalive()
    await hass.async_block_till_done()
    camera.async_start_streaming.assert_not_awaited()
    sub.close()


# ----------------------------------------------------------------------
# Final pass
# ----------------------------------------------------------------------


async def test_unsolicited_publish_hands_cameras_back(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    local = owner.camera("cam_1")
    entity, _camera = _entity(hass, local)
    owner.record_strike(HOST)
    owner.record_strike(HOST)
    local.channel.accepting = False
    stream = _watched_stream()
    entity.stream = stream
    entity._cached_stream_source = "rtmps://cloud/url"
    local.channel.publish_started(5, "192.168.1.107", lambda: None)
    assert owner._cancel_pause_timer is None
    assert local.channel.accepting is True
    stream.update_source.assert_called_once_with(FLV_URL)


async def test_reconnecting_relay_turns_readers_away(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.async_write_ha_state = MagicMock()
    entity.coordinator.data = _camera_state(connection_state=ConnectionState.RECONNECTING)
    entity._handle_coordinator_update()
    assert local.channel.accepting is False
    entity.coordinator.data = _camera_state()
    entity._handle_coordinator_update()
    assert local.channel.accepting is True


def test_publish_wait_stays_inside_the_pre_publish_window() -> None:
    from aionanit.local_stream import server as server_mod

    assert ls.PUBLISH_WAIT < server_mod.PRE_PUBLISH_TIMEOUT


async def test_removal_clears_the_publish_callback(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    entity.entity_id = "camera.nursery"
    await entity.async_will_remove_from_hass()
    assert local.channel.on_publish is None


async def test_podman_bridge_counts_as_docker(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass))
    with (
        patch.object(ls, "is_docker_env", return_value=True),
        patch.object(ls, "is_hassio", return_value=False),
    ):
        assert owner._looks_like_docker_bridge("10.88.0.5")
        assert not owner._looks_like_docker_bridge("192.168.1.10")


async def test_configured_address_is_not_taken_for_a_docker_bridge(hass: HomeAssistant) -> None:
    owner = _local_entry(hass, _entry(hass, {CONF_LOCAL_STREAM_HOST: "172.20.0.5"}))
    with (
        patch.object(ls, "is_docker_env", return_value=True),
        patch.object(ls, "is_hassio", return_value=False),
    ):
        assert not owner._looks_like_docker_bridge("172.20.0.5")


async def test_uncounted_miss_timer_uses_the_base_cooldown(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    local.misses = 4
    with patch.object(entity, "_schedule_local_cooldown_end") as schedule:
        entity._fall_back_to_cloud("sleep", count_miss=False)
    (delay,) = schedule.call_args.args
    assert delay == pytest.approx(ls.COOLDOWN, abs=1)


async def test_rotation_losing_the_race_points_the_camera_back(
    hass: HomeAssistant, source_ip: Any
) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    local.start_cooldown("x")
    entity.stream = _watched_stream()
    entity._cached_stream_source = "rtmps://cloud/old"
    sub = local.channel.subscribe()
    puts: list[str] = []

    async def start_streaming(*, rtmps_url: str, **_kwargs: Any) -> None:
        puts.append(rtmps_url)
        if rtmps_url == "rtmps://cloud/url":
            local.end_cooldown()

    camera.async_start_streaming = start_streaming
    await entity._async_refresh_stream_source("test")
    await hass.async_block_till_done()
    assert puts[0] == "rtmps://cloud/url"
    assert any(url.startswith(f"rtmps://{HOST}") for url in puts[1:])
    sub.close()


async def test_idle_drop_stops_the_discarded_stream(hass: HomeAssistant) -> None:
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, _camera = _entity(hass, local)
    stream = _watched_stream()
    stream.outputs.return_value = {}
    entity.stream = stream
    entity._handle_local_cooldown_end()
    await hass.async_block_till_done()
    stream.stop.assert_awaited_once()


async def test_keepalive_without_an_address_ends_quietly(hass: HomeAssistant) -> None:
    """No source IP must not kill the keepalive task with an unretrieved exception."""
    local = _local_entry(hass, _entry(hass)).camera("cam_1")
    entity, camera = _entity(hass, local)
    with patch.object(
        type(local), "async_publish_url", AsyncMock(side_effect=RuntimeError("no source ip"))
    ):
        assert await entity._async_local_keepalive() is False
    camera.async_start_streaming.assert_not_awaited()


def test_routed_ip_falls_back_when_the_helper_is_gone_or_fails() -> None:
    with patch.object(ls, "_ha_routed_ip", None):
        assert ls.async_get_routed_ip("10.30.0.9") is None
    with patch.object(ls, "_ha_routed_ip", MagicMock(side_effect=TypeError("signature"))):
        assert ls.async_get_routed_ip("10.30.0.9") is None
    with patch.object(ls, "_ha_routed_ip", MagicMock(return_value="10.30.0.2")):
        assert ls.async_get_routed_ip("10.30.0.9") == "10.30.0.2"


async def test_advertised_host_uses_the_public_helper_without_a_route(
    hass: HomeAssistant,
) -> None:
    owner = _local_entry(hass, _entry(hass))
    owner.camera("cam_1").channel.publisher_peer = "10.30.0.9"
    with (
        patch.object(ls, "_ha_routed_ip", None),
        patch.object(ls, "async_get_source_ip", AsyncMock(return_value=HOST)),
    ):
        assert await owner.async_advertised_host() == HOST


@pytest.fixture
def managed_go2rtc() -> Any:
    """Stand in for go2rtc's constant, which needs go2rtc's client library to import."""
    with patch.object(
        const_module, "_ha_managed_go2rtc_url", return_value="http://localhost:11984/"
    ):
        yield


@pytest.mark.parametrize(
    ("go2rtc_url", "option", "enabled"),
    [
        (None, None, True),
        ("http://localhost:11984/", None, True),
        ("http://192.168.1.20:1984/", None, False),
        ("http://192.168.1.20:1984/", True, True),
        (None, False, False),
    ],
    ids=["no-go2rtc", "built-in", "external", "external-turned-on", "turned-off"],
)
async def test_default_is_off_only_with_an_external_go2rtc(
    hass: HomeAssistant,
    managed_go2rtc: None,
    go2rtc_url: str | None,
    option: bool | None,
    enabled: bool,
) -> None:
    options = {} if option is None else {CONF_LOCAL_STREAM: option}
    entry = _entry(hass, options)
    if go2rtc_url is not None:
        hass.data["go2rtc"] = SimpleNamespace(url=go2rtc_url)
    try:
        assert ls.local_stream_enabled(hass, entry) is enabled
    finally:
        hass.data.pop("go2rtc", None)


async def test_unexpected_go2rtc_data_counts_as_the_built_in_server(
    hass: HomeAssistant, managed_go2rtc: None
) -> None:
    hass.data["go2rtc"] = object()
    try:
        assert ls.local_stream_enabled(hass, _entry(hass)) is True
    finally:
        hass.data.pop("go2rtc", None)


async def test_go2rtc_that_does_not_import_counts_as_the_built_in_server(
    hass: HomeAssistant,
) -> None:
    """Core without go2rtc's client library, or a release that moved its constants."""
    hass.data["go2rtc"] = SimpleNamespace(url="http://192.168.1.20:1984/")
    try:
        with patch.object(const_module, "_ha_managed_go2rtc_url", return_value=None):
            assert ls.local_stream_enabled(hass, _entry(hass)) is True
    finally:
        hass.data.pop("go2rtc", None)


async def test_options_form_defaults_off_with_an_external_go2rtc(
    hass: HomeAssistant, managed_go2rtc: None
) -> None:
    entry = _entry(hass)
    hass.data["go2rtc"] = SimpleNamespace(url="http://192.168.1.20:1984/")
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "local_stream"}
        )
        assert result["type"] is FlowResultType.FORM
        key = next(k for k in result["data_schema"].schema if str(k) == CONF_LOCAL_STREAM)
        assert key.default() is False
    finally:
        hass.data.pop("go2rtc", None)
