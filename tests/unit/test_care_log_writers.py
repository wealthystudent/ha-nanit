"""One care log statistics importer per baby, across config entries.

The entries are set up through Home Assistant with the faked Nanit client
of ``mock_nanit_client``. No recorder runs here, so every importer waits
for one and asks Nanit for nothing; the recorder-backed run is in
``recorder/test_care_log_entry.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

# Camera imports pull in turbojpeg; stub it like test_read_only.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from aionanit.models import Baby
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.care_log_writers import (
    WRITERS_KEY,
    CareLogWriterRegistry,
    care_log_writer_registry,
)
from custom_components.nanit.const import (
    CONF_IMPORT_CARE_LOG_STATISTICS,
    CONF_READ_ONLY,
    CONF_SHOW_CARE_LOG_NOTES,
    DOMAIN,
)
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics

from .conftest import MOCK_BABY_1, mock_entry_data_v2
from .test_via_device_e2e import camera_fails, nanit_env  # noqa: F401  (shared fixtures)

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

CODE_1 = baby_code(MOCK_BABY_1.uid)
NO_CAMERA = Baby(uid="baby_4", name="Spare room", camera_uid="")
PLACEHOLDER = Baby(uid="", name="Unpaired speaker", camera_uid="")
FIRST_ID = "01aaaaaaaaaaaaaaaaaaaaaaaa"
SECOND_ID = "02bbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def client(nanit_env: Any) -> Any:  # noqa: F811
    """The faked Nanit client, with one baby whose camera works."""
    nanit_env.async_get_babies.return_value = [MOCK_BABY_1]
    return nanit_env


@pytest.fixture
def started() -> Iterator[list[CareLogStatisticsImporter]]:
    """Every importer started, in order."""
    importers: list[CareLogStatisticsImporter] = []
    real_start = CareLogStatisticsImporter.async_start

    def spy(self: CareLogStatisticsImporter) -> None:
        importers.append(self)
        real_start(self)

    with patch.object(CareLogStatisticsImporter, "async_start", spy):
        yield importers


def _entry(
    hass: HomeAssistant,
    *,
    on: bool = True,
    entry_id: str = FIRST_ID,
    email: str = "first@example.com",
    **options: Any,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_entry_data_v2(), "email": email},
        options={CONF_IMPORT_CARE_LOG_STATISTICS: on, CONF_SHOW_CARE_LOG_NOTES: False, **options},
        version=2,
        minor_version=3,
        unique_id=email,
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return entry


def _second(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    return _entry(hass, entry_id=SECOND_ID, email="second@example.com", **options)


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _registry(hass: HomeAssistant) -> CareLogWriterRegistry:
    registry = care_log_writer_registry(hass)
    assert registry is not None
    return registry


def _writer(hass: HomeAssistant, code: str = CODE_1) -> tuple[str, CareLogStatisticsImporter]:
    writer = _registry(hass).writer(code)
    assert writer is not None
    return writer


def _importer_listeners(reader: Any) -> list[Any]:
    """The reader listeners an importer added."""
    return [
        call.args[0]
        for call in reader.async_add_listener.call_args_list
        if isinstance(getattr(call.args[0], "__self__", None), CareLogStatisticsImporter)
    ]


def _no_registry_left(hass: HomeAssistant) -> bool:
    return WRITERS_KEY not in hass.data.get(DOMAIN, {})


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("read_only", [False, True], ids=["full", "read_only"])
async def test_with_the_option_on_every_baby_with_a_reader_gets_a_writing_importer(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    read_only: bool,
) -> None:
    """A record without a uid has no reader, so it gets none; read-only changes nothing."""
    client.async_get_babies.return_value = [MOCK_BABY_1, NO_CAMERA, PLACEHOLDER]
    entry = _entry(hass, **{CONF_READ_ONLY: read_only})

    await _set_up(hass, entry)

    readers = entry.runtime_data.hub.care_log_coordinators
    assert set(readers) == {MOCK_BABY_1.uid, NO_CAMERA.uid}
    assert len(started) == 2
    for uid, reader in readers.items():
        entry_id, importer = _writer(hass, baby_code(uid))
        assert entry_id == entry.entry_id
        assert importer in started
        assert importer._coordinator is reader
        assert importer._baby is reader.baby
        assert _importer_listeners(reader) == [importer._on_reader_update]
        # Setup did not wait for it: its worker runs in the background,
        # waiting for a recorder.
        assert importer._task is not None
        assert not importer._task.done()
        assert importer.diagnostics()["state"] == "waiting_for_recorder"
    assert entry.runtime_data.care_log_writers is not None
    assert entry.runtime_data.care_log_writers.codes == tuple(
        sorted(baby_code(uid) for uid in readers)
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert all(importer.diagnostics()["state"] == "stopped" for importer in started)
    assert _no_registry_left(hass)


async def test_the_importer_reads_through_the_entry_client_for_its_baby(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    entry = _entry(hass)
    await _set_up(hass, entry)
    (importer,) = started
    client.async_get_calendar_result.reset_mock()

    await importer._fetch(100, 200)

    client.async_get_calendar_result.assert_awaited_once_with(MOCK_BABY_1.uid, 100, 200)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("options", [{}, {CONF_IMPORT_CARE_LOG_STATISTICS: False}])
async def test_with_the_option_off_there_is_no_importer_and_no_listener(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    options: dict[str, Any],
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options=options,
        version=2,
        minor_version=3,
        unique_id="first@example.com",
    )
    entry.add_to_hass(hass)

    await _set_up(hass, entry)

    assert started == []
    assert _no_registry_left(hass)
    reader = entry.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    assert _importer_listeners(reader) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_switching_the_option_off_reloads_and_leaves_no_importer(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    entry = _entry(hass)
    await _set_up(hass, entry)
    (importer,) = started
    hub = entry.runtime_data.hub

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "care_logs"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMPORT_CARE_LOG_STATISTICS: False, CONF_SHOW_CARE_LOG_NOTES: False},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.hub is not hub  # reloaded
    assert importer.diagnostics()["state"] == "stopped"
    assert started == [importer]
    assert _no_registry_left(hass)
    reader = entry.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    assert _importer_listeners(reader) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_stops_the_importer_before_the_hub_closes(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    order: list[str] = []
    real_stop = CareLogStatisticsImporter.async_stop

    async def stop_spy(self: CareLogStatisticsImporter) -> None:
        await real_stop(self)
        order.append("importer stopped")

    async def close() -> None:
        order.append("client closed")

    client.async_close = AsyncMock(side_effect=close)
    entry = _entry(hass)
    await _set_up(hass, entry)
    (importer,) = started

    with patch.object(CareLogStatisticsImporter, "async_stop", stop_spy):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert order == ["importer stopped", "client closed"]
    assert importer._task is not None
    assert importer._task.done()
    assert importer._unsub_listener is None
    assert importer._unsub_timer is None
    assert _no_registry_left(hass)


async def test_a_reload_replaces_the_importer_with_a_new_one(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    entry = _entry(hass)
    await _set_up(hass, entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    first, second = started
    assert first is not second
    assert first.diagnostics()["state"] == "stopped"
    assert _writer(hass) == (entry.entry_id, second)
    assert second._coordinator is entry.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert second.diagnostics()["state"] == "stopped"
    assert _no_registry_left(hass)


async def test_a_setup_that_fails_after_registering_stops_the_importer(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    entry = _entry(hass)

    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=RuntimeError("platforms failed")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    (importer,) = started
    assert importer.diagnostics()["state"] == "stopped"
    assert importer._task is not None
    assert importer._task.done()
    assert _no_registry_left(hass)
    # The hub is not closed on this path (as before); close it so its
    # coordinators leave nothing behind.
    await entry.runtime_data.hub.async_close()


async def test_home_assistant_stopping_stops_the_importer_without_a_handover(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    first = _entry(hass)
    await _set_up(hass, first)
    second = _second(hass)
    await _set_up(hass, second)
    (importer,) = started

    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert importer.diagnostics()["state"] == "stopped"
    assert started == [importer]
    assert _no_registry_left(hass)
    hass.set_state(CoreState.running)
    assert await hass.config_entries.async_unload(first.entry_id)
    assert await hass.config_entries.async_unload(second.entry_id)
    assert started == [importer]


# ---------------------------------------------------------------------------
# Two sign-ins that see the same baby: the owner writes, nobody else
# ---------------------------------------------------------------------------

# Written out here rather than imported, so the construction is checked too.
STORE_KEY = "nanit.care_log_statistics"
THIRD_ID = "03cccccccccccccccccccccccc"
FOURTH_ID = "04dddddddddddddddddddddddd"


def _owner_code(account: str) -> str:
    digest = hashlib.sha256(b"ha-nanit/statistics-owner/v1\x00" + account.encode())
    return digest.hexdigest()[:32]


def _marker(hass_storage: dict[str, Any]) -> dict[str, Any]:
    stored = hass_storage[STORE_KEY]
    assert stored["key"] == STORE_KEY
    data: dict[str, Any] = stored["data"]
    return data


async def _check_owners(hass: HomeAssistant) -> None:
    """Let the registry's periodic look at babies without an importer run, and finish."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()
    registry = care_log_writer_registry(hass)
    task = getattr(registry, "_check_task", None)
    if task is not None:
        await task
    await hass.async_block_till_done()


@pytest.mark.parametrize("first_loaded", ["first", "second"])
async def test_two_entries_loading_at_start_up_in_either_order_give_the_same_owner(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    first_loaded: str,
) -> None:
    hass.set_state(CoreState.starting)
    if first_loaded == "first":
        first = _entry(hass)
        await _set_up(hass, first)
        second = _second(hass)
        await _set_up(hass, second)
    else:
        second = _second(hass)
        await _set_up(hass, second)
        first = _entry(hass)
        await _set_up(hass, first)
    # Nobody takes ownership before every entry loading at start-up has offered.
    assert started == []
    assert STORE_KEY not in hass_storage

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await _check_owners(hass)

    (importer,) = started
    assert _writer(hass) == (first.entry_id, importer)
    assert importer._entry is first
    assert _registry(hass).entries(CODE_1) == (first.entry_id, second.entry_id)
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("first@example.com")}}
    standby_reader = second.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    assert _importer_listeners(standby_reader) == []

    for entry in (first, second):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


async def test_the_owner_resumes_after_a_reload_and_after_its_account_is_added_again(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    """The other sign-in sorts first by entry id, and still never writes."""
    owner = _entry(hass, entry_id=THIRD_ID)
    await _set_up(hass, owner)
    other = _second(hass)
    await _set_up(hass, other)
    (writing,) = started
    assert _writer(hass) == (owner.entry_id, writing)

    assert await hass.config_entries.async_reload(owner.entry_id)
    await hass.async_block_till_done()
    assert _writer(hass)[0] == owner.entry_id
    assert writing.diagnostics()["state"] == "stopped"

    assert await hass.config_entries.async_remove(owner.entry_id)
    await _check_owners(hass)
    assert _registry(hass).writer(CODE_1) is None

    again = _entry(hass, entry_id=FOURTH_ID)  # the same account, a new entry
    await _set_up(hass, again)
    entry_id, importer = _writer(hass)
    assert entry_id == again.entry_id
    assert importer._entry is again
    assert {one._entry.entry_id for one in started} == {THIRD_ID, FOURTH_ID}
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("first@example.com")}}

    for entry in (again, other):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


@pytest.mark.parametrize("how", ["stopped_itself", "polling_disabled", "unloaded"])
async def test_an_owner_out_of_action_is_never_replaced(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    how: str,
) -> None:
    owner = _entry(hass, entry_id=THIRD_ID)
    if how == "polling_disabled":
        hass.config_entries.async_update_entry(owner, pref_disable_polling=True)
    await _set_up(hass, owner)
    other = _second(hass)
    await _set_up(hass, other)
    (writing,) = started

    if how == "stopped_itself":
        # As after a refused sign-in: the worker ends, the entry stays loaded.
        writing._stopping = True
        writing._wake()
        assert writing._task is not None
        await writing._task
    elif how == "unloaded":
        assert await hass.config_entries.async_unload(owner.entry_id)
    await _check_owners(hass)
    await _check_owners(hass)

    assert started == [writing]
    writer = _registry(hass).writer(CODE_1)
    assert writer is None or writer[0] == owner.entry_id
    other_diag = (await async_get_config_entry_diagnostics(hass, other))["care_log_statistics"]
    assert other_diag["babies"] == {"baby_0": {"writer": False, "state": "not_owner"}}

    for entry in (owner, other):
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


async def test_the_owner_marker_holds_only_one_way_codes(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    uid = "zqmarkerbabyuid7731"
    name = "zqmarkerbabyname2290"
    email = "zqmarkeremail5518@example.com"
    entry_id = "zqmarkerentryid4407"
    client.async_get_babies.return_value = [Baby(uid=uid, name=name, camera_uid="cam_1")]
    entry = _entry(hass, entry_id=entry_id, email=email)
    await _set_up(hass, entry)
    assert len(started) == 1

    text = json.dumps(hass_storage[STORE_KEY])
    for marker in (uid, name, email, entry_id, "zqmarker"):
        assert marker not in text
    assert _marker(hass_storage) == {"owners": {baby_code(uid): _owner_code(email)}}
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


async def test_one_entry_unloading_while_the_other_sets_up(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    first = _entry(hass)
    await _set_up(hass, first)
    second = _second(hass)

    unloaded, set_up = await asyncio.gather(
        hass.config_entries.async_unload(first.entry_id),
        hass.config_entries.async_setup(second.entry_id),
    )
    await hass.async_block_till_done()

    assert unloaded
    assert set_up
    # The first is the owner: the second stands by although it is alone.
    assert _registry(hass).writer(CODE_1) is None
    assert _registry(hass).entries(CODE_1) == (second.entry_id,)
    (importer,) = started
    assert importer.diagnostics()["state"] == "stopped"
    assert await hass.config_entries.async_unload(second.entry_id)
    assert _no_registry_left(hass)


async def test_unloading_the_standby_changes_nothing(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    first = _entry(hass)
    await _set_up(hass, first)
    second = _second(hass)
    await _set_up(hass, second)
    (writing,) = started

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()

    assert _writer(hass) == (first.entry_id, writing)
    assert writing.diagnostics()["state"] == "waiting_for_recorder"
    assert started == [writing]
    assert _registry(hass).entries(CODE_1) == (first.entry_id,)
    assert await hass.config_entries.async_unload(first.entry_id)
    assert _no_registry_left(hass)


async def test_an_entry_with_the_option_off_never_writes(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    """Only entries with the option on take part, even when one sorts first."""
    first = _entry(hass, on=False)
    await _set_up(hass, first)
    second = _second(hass)
    await _set_up(hass, second)

    (importer,) = started
    assert _writer(hass) == (second.entry_id, importer)
    assert _registry(hass).entries(CODE_1) == (second.entry_id,)
    for entry in (first, second):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

UID_MARKER = "zquidmarker4410"
NAME_MARKER = "zqbabynamemarker8812"


async def test_diagnostics_show_the_writer_and_the_standby_by_position_only(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    marked = Baby(uid=UID_MARKER, name=NAME_MARKER, camera_uid="cam_1")
    client.async_get_babies.return_value = [marked, NO_CAMERA]
    first = _entry(hass)
    await _set_up(hass, first)
    second = _second(hass)
    await _set_up(hass, second)

    writing = (await async_get_config_entry_diagnostics(hass, first))["care_log_statistics"]
    standby = (await async_get_config_entry_diagnostics(hass, second))["care_log_statistics"]

    assert set(writing) == {"enabled", "babies"}
    assert writing["enabled"] is True
    assert set(writing["babies"]) == {"baby_0", "baby_1"}
    for item in writing["babies"].values():
        assert item["writer"] is True
        assert item["state"] == "waiting_for_recorder"
        assert {"entries", "rows_written", "passes_completed"} <= set(item)
    assert standby == {
        "enabled": True,
        "babies": {
            "baby_0": {"writer": False, "state": "not_owner"},
            "baby_1": {"writer": False, "state": "not_owner"},
        },
    }
    # The same order on both entries: by baby code.
    codes = sorted(baby_code(uid) for uid in (UID_MARKER, NO_CAMERA.uid))
    assert first.runtime_data.care_log_writers is not None
    assert first.runtime_data.care_log_writers.codes == tuple(codes)
    for entry in (first, second):
        text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
        section = json.dumps(
            (await async_get_config_entry_diagnostics(hass, entry))["care_log_statistics"]
        )
        for marker in (UID_MARKER, NAME_MARKER, NO_CAMERA.uid, *codes):
            assert marker not in text, marker
            assert marker not in section, marker
        assert first.entry_id not in section
        assert second.entry_id not in section
    for entry in (first, second):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_with_the_option_off(hass: HomeAssistant, client: MagicMock) -> None:
    entry = _entry(hass, on=False)
    await _set_up(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["care_log_statistics"] == {"enabled": False, "babies": {}}
    assert await hass.config_entries.async_unload(entry.entry_id)
