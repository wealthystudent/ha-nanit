"""One care log statistics importer per baby, across config entries.

The entries are set up through Home Assistant with the faked Nanit client
of ``mock_nanit_client``. No recorder runs here, so every importer waits
for one and asks Nanit for nothing; the recorder-backed runs are in
``recorder/test_care_log_entry.py`` and ``recorder/test_care_log_owner.py``.
Whether the baby has stored rows is the ``rows`` fixture's answer: by
default none (an empty database), so a free baby can be taken.
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
from custom_components.nanit import care_log_writers
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.care_log_writers import (
    WRITERS_KEY,
    CareLogWriterRegistry,
    care_log_writer_registry,
    owner_code,
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


@pytest.fixture(autouse=True)
def rows() -> Iterator[AsyncMock]:
    """Whether the baby has stored statistics rows: False (none) unless a test says otherwise."""
    answer = AsyncMock(return_value=False)
    with patch.object(care_log_writers, "async_has_care_log_statistics", answer):
        yield answer


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
    rows: AsyncMock,
) -> None:
    """The other sign-in sorts first by entry id, and still never writes."""
    owner = _entry(hass, entry_id=THIRD_ID)
    await _set_up(hass, owner)
    other = _second(hass)
    await _set_up(hass, other)
    (writing,) = started
    rows.return_value = True  # the owner's rows are stored
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
    rows: AsyncMock,
    how: str,
) -> None:
    owner = _entry(hass, entry_id=THIRD_ID)
    await _set_up(hass, owner)
    other = _second(hass)
    await _set_up(hass, other)
    (writing,) = started
    rows.return_value = True  # the owner's rows are stored

    if how == "polling_disabled":
        # An established owner keeps the statistics with polling disabled.
        hass.config_entries.async_update_entry(owner, pref_disable_polling=True)
        assert await hass.config_entries.async_reload(owner.entry_id)
        await hass.async_block_till_done()
        assert writing.diagnostics()["state"] == "stopped"
        assert [one._entry.entry_id for one in started] == [owner.entry_id] * 2
        writing = started[-1]
        assert _writer(hass) == (owner.entry_id, writing)
    elif how == "stopped_itself":
        # As after a refused sign-in: the worker ends, the entry stays loaded.
        writing._stopping = True
        writing._wake()
        assert writing._task is not None
        await writing._task
    elif how == "unloaded":
        assert await hass.config_entries.async_unload(owner.entry_id)
    await _check_owners(hass)
    await _check_owners(hass)

    assert started[-1] is writing
    assert {one._entry.entry_id for one in started} == {owner.entry_id}
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


async def test_the_owner_marker_is_written_owner_only_and_atomically(
    hass: HomeAssistant, client: MagicMock, started: list[CareLogStatisticsImporter]
) -> None:
    entry = _entry(hass)
    await _set_up(hass, entry)
    registry = _registry(hass)
    for store in (registry._store, registry._disk_store()):
        assert store._private is True
        assert store._atomic_writes is True
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_one_entry_unloading_while_the_other_sets_up(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    rows: AsyncMock,
) -> None:
    first = _entry(hass)
    await _set_up(hass, first)
    second = _second(hass)
    # The first one's rows are stored.
    rows.return_value = True

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
        assert {"established", "passes_completed"} <= set(item)
        assert not {"entries", "rows_written"} & set(item)
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


# ---------------------------------------------------------------------------
# Ownership is proved, never assumed
# ---------------------------------------------------------------------------


def _bare_entry(*, email: str | None, unique_id: str | None, entry_id: str) -> MockConfigEntry:
    data = mock_entry_data_v2()
    data.pop("email")
    if email is not None:
        data["email"] = email
    return MockConfigEntry(
        domain=DOMAIN, data=data, version=2, unique_id=unique_id, entry_id=entry_id
    )


def test_one_account_gets_one_owner_code_whatever_its_case_or_its_unique_id() -> None:
    """The config flow matches an account by its email, stripped and lower-cased."""
    expected = _owner_code("first@example.com")
    legacy = _bare_entry(
        email=" First@Example.com ", unique_id="First@Example.com", entry_id=FIRST_ID
    )
    again = _bare_entry(email="first@example.com", unique_id="first@example.com", entry_id=THIRD_ID)
    v1_style = _bare_entry(email="first@example.com", unique_id="cam_uid_1", entry_id=FOURTH_ID)
    no_email = _bare_entry(email=None, unique_id="First@Example.com", entry_id=SECOND_ID)
    neither = _bare_entry(email=None, unique_id=None, entry_id=SECOND_ID)

    assert owner_code(legacy) == expected
    assert owner_code(again) == expected
    assert owner_code(v1_style) == expected
    assert owner_code(no_email) == expected
    assert owner_code(neither) == _owner_code(SECOND_ID)


async def test_a_legacy_mixed_case_owner_resumes_when_added_again_in_lower_case(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    rows: AsyncMock,
) -> None:
    legacy = _entry(hass, entry_id=THIRD_ID, email="First@Example.com")
    await _set_up(hass, legacy)
    other = _second(hass)
    await _set_up(hass, other)
    rows.return_value = True  # the owner's rows are stored
    assert _writer(hass)[0] == legacy.entry_id
    assert await hass.config_entries.async_remove(legacy.entry_id)
    await _check_owners(hass)
    assert _registry(hass).writer(CODE_1) is None

    again = _entry(hass, entry_id=FOURTH_ID, email="first@example.com")
    await _set_up(hass, again)

    assert _writer(hass)[0] == again.entry_id
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("first@example.com")}}
    for entry in (again, other):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


async def test_an_entry_with_polling_disabled_takes_no_first_ownership(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    """It could never import; it sorts first, and the other one takes the baby."""
    idle = _entry(hass)
    hass.config_entries.async_update_entry(idle, pref_disable_polling=True)
    await _set_up(hass, idle)
    await _check_owners(hass)
    assert started == []
    assert STORE_KEY not in hass_storage
    idle_diag = (await async_get_config_entry_diagnostics(hass, idle))["care_log_statistics"]
    # Nothing is decided yet: no owner, known or other.
    assert idle_diag["babies"] == {"baby_0": {"writer": False, "state": "waiting_for_owner_check"}}

    second = _second(hass)
    await _set_up(hass, second)

    (importer,) = started
    assert _writer(hass) == (second.entry_id, importer)
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("second@example.com")}}
    # Now the other sign-in is known to own it.
    idle_diag = (await async_get_config_entry_diagnostics(hass, idle))["care_log_statistics"]
    assert idle_diag["babies"] == {"baby_0": {"writer": False, "state": "not_owner"}}
    for entry in (idle, second):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


@pytest.fixture
def refused_first(client: MagicMock) -> dict[str, Any]:
    """The first entry's reader has never read the care logs (Nanit refused it, say).

    Returns the read each such reader would have held, by entry id.
    """
    from custom_components.nanit import hub

    make_reader = hub.NanitCareLogCoordinator.side_effect
    held: dict[str, Any] = {}

    def reader(hass: HomeAssistant, entry: Any, owner_hub: Any, baby: Baby) -> Any:
        made = make_reader(hass, entry, owner_hub, baby)
        if entry.entry_id == FIRST_ID:
            held[entry.entry_id] = made.data
            made.data = None
            made.last_update_success = False
        return made

    hub.NanitCareLogCoordinator.side_effect = reader
    return held


async def test_a_sign_in_whose_reader_has_not_read_takes_no_first_ownership(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    refused_first: dict[str, Any],
) -> None:
    """It sorts first; the sign-in that reads takes the baby."""
    refused = _entry(hass)
    await _set_up(hass, refused)
    await _check_owners(hass)
    assert started == []
    assert STORE_KEY not in hass_storage

    working = _second(hass)
    await _set_up(hass, working)

    (importer,) = started
    assert _writer(hass) == (working.entry_id, importer)
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("second@example.com")}}
    # The refused sign-in's reader reads at last: it stays a standby.
    reader = refused.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    reader.data = refused_first[FIRST_ID]
    reader.last_update_success = True
    for call in reader.async_add_listener.call_args_list:
        call.args[0]()
    await _check_owners(hass)
    assert started == [importer]
    assert _writer(hass) == (working.entry_id, importer)
    for entry in (refused, working):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


async def test_a_sign_in_whose_reader_reads_late_takes_a_free_baby_at_once(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    refused_first: dict[str, Any],
) -> None:
    refused = _entry(hass)
    await _set_up(hass, refused)
    await hass.async_block_till_done()
    assert started == []
    reader = refused.runtime_data.hub.care_log_coordinators[MOCK_BABY_1.uid]
    (listener,) = [
        call.args[0]
        for call in reader.async_add_listener.call_args_list
        if "_wait_for_read" in getattr(call.args[0], "__qualname__", "")
    ]
    unsubscribed = reader.async_add_listener.return_value.call_count

    listener()  # an update that failed again: nothing changes
    await hass.async_block_till_done()
    assert started == []
    reader.data = refused_first[FIRST_ID]
    reader.last_update_success = True
    listener()  # the reader has read: the registry looks again, without waiting
    await hass.async_block_till_done()
    task = _registry(hass)._check_task
    assert task is not None
    await task

    (importer,) = started
    assert _writer(hass) == (refused.entry_id, importer)
    assert _marker(hass_storage) == {"owners": {CODE_1: _owner_code("first@example.com")}}
    # The listener that waited for the read is gone; the importer's stays.
    assert reader.async_add_listener.return_value.call_count == unsubscribed + 1
    assert _importer_listeners(reader) == [importer._on_reader_update]
    assert await hass.config_entries.async_unload(refused.entry_id)
    assert _no_registry_left(hass)


async def test_a_standby_tells_a_known_owner_from_no_decision_and_from_a_pause(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    rows: AsyncMock,
) -> None:
    """The owner is removed: its sign-in is still the known owner."""
    from custom_components.nanit.care_log_recorder import PAUSED_KEY

    idle = _entry(hass, entry_id=SECOND_ID, email="second@example.com")
    hass.config_entries.async_update_entry(idle, pref_disable_polling=True)
    await _set_up(hass, idle)
    await _check_owners(hass)

    async def state() -> object:
        diag = (await async_get_config_entry_diagnostics(hass, idle))["care_log_statistics"]
        return diag["babies"]["baby_0"]["state"]

    assert await state() == "waiting_for_owner_check"
    # A batch of unknown outcome: nothing can be decided until a restart
    # (the database cannot tell whether rows exist).
    hass.data[PAUSED_KEY] = {CODE_1}
    rows.return_value = None
    await _check_owners(hass)
    assert await state() == "paused_until_restart"
    hass.data[PAUSED_KEY] = set()
    rows.return_value = False

    owner = _entry(hass)
    await _set_up(hass, owner)
    (importer,) = started
    assert _writer(hass) == (owner.entry_id, importer)
    assert await state() == "not_owner"
    rows.return_value = True
    assert await hass.config_entries.async_remove(owner.entry_id)
    await _check_owners(hass)
    assert await state() == "not_owner"
    assert started == [importer]
    assert await hass.config_entries.async_unload(idle.entry_id)
    assert _no_registry_left(hass)


async def test_a_standby_of_a_known_owner_reports_a_pause_before_the_owner(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
) -> None:
    """A write may still land: every sign-in that sees the baby shows the pause."""
    from custom_components.nanit.care_log_recorder import PAUSED_KEY

    owner = _entry(hass)
    await _set_up(hass, owner)
    standby = _second(hass)
    await _set_up(hass, standby)
    (importer,) = started
    assert _writer(hass) == (owner.entry_id, importer)

    async def state() -> object:
        diag = (await async_get_config_entry_diagnostics(hass, standby))["care_log_statistics"]
        return diag["babies"]["baby_0"]["state"]

    assert await state() == "not_owner"
    hass.data[PAUSED_KEY] = {CODE_1}
    assert await state() == "paused_until_restart"
    hass.data[PAUSED_KEY] = set()
    assert await state() == "not_owner"
    for entry in (owner, standby):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


@pytest.mark.parametrize("broken", ["newer_version", "unreadable"])
async def test_a_marker_that_cannot_be_loaded_leaves_the_rest_of_setup_alone(
    hass: HomeAssistant,
    client: MagicMock,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    broken: str,
) -> None:
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store

    real_load_data = Store._async_load_data

    async def load_data(store: Store[Any]) -> Any:
        if store.key == STORE_KEY and broken == "unreadable":
            raise HomeAssistantError("zqmarkerpath9913 is unreadable")
        return await real_load_data(store)

    # A marker a newer version wrote: Home Assistant's store refuses it.
    hass_storage[STORE_KEY] = {"version": 2, "key": STORE_KEY, "data": {"owners": {}}}
    entry = _entry(hass)
    with patch.object(Store, "_async_load_data", load_data):
        await _set_up(hass, entry)
        for domain in ("camera", "sensor", "calendar"):
            assert hass.states.async_entity_ids(domain), domain
        await _check_owners(hass)
        await _check_owners(hass)

        assert started == []
        diag = (await async_get_config_entry_diagnostics(hass, entry))["care_log_statistics"]
        assert diag["babies"] == {"baby_0": {"writer": False, "state": "owner_unknown"}}
        errors = [r for r in caplog.records if r.name == care_log_writers.__name__]
        assert len(errors) == 1
        assert errors[0].levelname == "ERROR"
        assert "zqmarkerpath" not in caplog.text
        assert hass_storage[STORE_KEY]["version"] == 2  # never written over

        # Readable again: the next look decides.
        broken = "fixed"
        hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"owners": {}}}
        await _check_owners(hass)

    (importer,) = started
    assert _writer(hass) == (entry.entry_id, importer)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _no_registry_left(hass)


class _StubImporter:
    """Stands in for an importer whose stop can be held."""

    def __init__(self, gate: asyncio.Event | None) -> None:
        self.gate = gate
        self.started = False
        self.stopped = False

    def async_start(self) -> None:
        self.started = True

    async def async_stop(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.stopped = True


async def test_a_stalled_stop_of_one_importer_does_not_hold_up_other_registrations(
    hass: HomeAssistant,
) -> None:
    registry = CareLogWriterRegistry(hass)
    code_a, code_b = "a" * 32, "b" * 32
    gate = asyncio.Event()
    made: list[_StubImporter] = []

    def factory(held: asyncio.Event | None = None) -> Any:
        def make() -> Any:
            importer = _StubImporter(held)
            made.append(importer)
            return importer

        return make

    await registry.async_register(code_a, FIRST_ID, "owner-1", factory(gate))
    (stalled,) = made
    assert stalled.started
    stop = hass.async_create_task(registry.async_unregister(code_a, FIRST_ID))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert not stop.done()

        # Another baby, and another sign-in for the stopping baby, meanwhile.
        await asyncio.wait_for(
            registry.async_register(code_b, SECOND_ID, "owner-2", factory()), timeout=5
        )
        assert registry.writer(code_b) is not None
        await asyncio.wait_for(
            registry.async_register(code_a, SECOND_ID, "owner-2", factory()), timeout=5
        )
        # No importer for the stopping baby until its stop has finished.
        assert registry.writer(code_a) is None
        await registry.async_check()
        assert registry.writer(code_a) is None
    finally:
        gate.set()
    await stop
    assert stalled.stopped

    await registry.async_check()
    writer = registry.writer(code_a)
    assert writer is not None
    assert writer[0] == SECOND_ID
    for code in (code_a, code_b):
        await registry.async_unregister(code, SECOND_ID)
    assert registry.unused
