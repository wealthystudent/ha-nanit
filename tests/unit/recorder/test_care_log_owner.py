"""Who writes a baby's care log statistics, with two sign-ins and a real recorder.

The owner is the sign-in that started the statistics; another sign-in that
sees the same baby never writes while they exist, even with the owner gone,
and takes over only after they were deleted. The integration is set up
through Home Assistant with the faked Nanit client of the shared fixtures,
real care log readers, and a calendar answering from a small synthetic
history. All data is synthetic, around Friday 20 November 2026 18:10 UTC.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

# Camera imports pull in turbojpeg; stub it like test_read_only.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from aionanit.models import CareLog, CareLogFetch
from custom_components.nanit import care_log_importer, care_log_recorder
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries, hour_start
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.care_log_statistics import statistic_id
from custom_components.nanit.care_log_writers import care_log_writer_registry
from custom_components.nanit.const import (
    CONF_IMPORT_CARE_LOG_STATISTICS,
    CONF_SHOW_CARE_LOG_NOTES,
    DOMAIN,
)
from custom_components.nanit.coordinator import NanitCareLogCoordinator

from ..conftest import MOCK_BABY_1, mock_entry_data_v2
from ..test_via_device_e2e import camera_fails, nanit_env  # noqa: F401  (shared fixtures)

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

NOW = datetime(2026, 11, 20, 18, 10, tzinfo=UTC)
UID = MOCK_BABY_1.uid
CODE = baby_code(UID)
FAR_PAST = datetime(2020, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)
STORE_KEY = "nanit.care_log_statistics"
# The other sign-in's entry id sorts first: it would write under a
# lowest-id rule.
OTHER_ID = "01aaaaaaaaaaaaaaaaaaaaaaaa"
OWNER_ID = "02bbbbbbbbbbbbbbbbbbbbbbbb"
OWNER_AGAIN_ID = "03cccccccccccccccccccccccc"
OWNER_EMAIL = "owner@example.com"
OTHER_EMAIL = "other@example.com"


def _log(uid: str, kind: str, when: datetime, **fields: Any) -> CareLog:
    return CareLog(uid=uid, log_type=kind, timestamp=when.timestamp(), baby_uid=UID, **fields)


OLD_BOTTLE = _log("b_old", "bottle_feed", NOW - timedelta(days=20, minutes=30), feed_amount_ml=90.0)
DIAPER = _log("d1", "diaper_change", NOW - timedelta(days=2), change_type="pee")
BOTTLE = _log("b1", "bottle_feed", NOW - timedelta(hours=3, minutes=40), feed_amount_ml=120.0)
HISTORY = (OLD_BOTTLE, DIAPER, BOTTLE)


def _owner_code(account: str) -> str:
    digest = hashlib.sha256(b"ha-nanit/statistics-owner/v1\x00" + account.encode())
    return digest.hexdigest()[:32]


async def _answer(_baby_uid: str, start: int, end: int) -> CareLogFetch:
    logs = tuple(log for log in HISTORY if start <= log.timestamp <= end)
    return CareLogFetch(logs=logs, start=start, end=end, dropped=0)


@pytest.fixture(autouse=True)
def quick() -> Iterator[None]:
    """No real pauses: time stands still under the freezer."""
    with (
        patch.object(care_log_recorder, "VERIFY_RETRY_DELAY", 0.0),
        patch.object(care_log_importer, "REQUEST_PAUSE", 0.0),
        patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)),
    ):
        yield


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


@pytest.fixture
async def env(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    nanit_env: Any,  # noqa: F811
) -> AsyncIterator[Callable[[str, str], MockConfigEntry]]:
    """Make entries for two accounts that see the same baby; unload them all at the end."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(NOW)
    nanit_env.async_get_babies.return_value = [MOCK_BABY_1]
    nanit_env.async_get_calendar_result = AsyncMock(side_effect=_answer)
    made: list[MockConfigEntry] = []

    def make(entry_id: str, email: str) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={**mock_entry_data_v2(), "email": email},
            options={CONF_IMPORT_CARE_LOG_STATISTICS: True, CONF_SHOW_CARE_LOG_NOTES: False},
            version=2,
            minor_version=3,
            unique_id=email,
            entry_id=entry_id,
        )
        entry.add_to_hass(hass)
        made.append(entry)
        return entry

    # The shared fixtures fake the reader; these tests use the real one.
    with patch("custom_components.nanit.hub.NanitCareLogCoordinator", NanitCareLogCoordinator):
        yield make
    for entry in made:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert care_log_writer_registry(hass) is None


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _settled(hass: HomeAssistant, importer: CareLogStatisticsImporter) -> None:
    """Wait until the importer has nothing to do."""
    for _ in range(100):
        await hass.async_block_till_done()
        if importer._idle.is_set():
            return
        await asyncio.wait_for(importer._idle.wait(), timeout=30)


def _writer(hass: HomeAssistant) -> tuple[str, CareLogStatisticsImporter] | None:
    registry = care_log_writer_registry(hass)
    return None if registry is None else registry.writer(CODE)


async def _check_owners(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the registry's periodic look at babies without an importer run, and finish."""
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    task = getattr(care_log_writer_registry(hass), "_check_task", None)
    if task is not None:
        await task
    await hass.async_block_till_done()


async def _rows(hass: HomeAssistant, series: CareLogSeries) -> dict[int, tuple[float, float]]:
    sid = statistic_id(CODE, series)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        FAR_PAST,
        FAR_FUTURE,
        {sid},
        "hour",
        None,
        {"state", "sum"},
    )
    return {int(row["start"]): (row["state"], row["sum"]) for row in found.get(sid, [])}


async def _owner_imported(
    hass: HomeAssistant,
    make: Callable[[str, str], MockConfigEntry],
    started: list[CareLogStatisticsImporter],
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """The owner imports; then the other sign-in loads and stands by."""
    owner = make(OWNER_ID, OWNER_EMAIL)
    await _set_up(hass, owner)
    (importer,) = started
    await _settled(hass, importer)
    assert (await _rows(hass, CareLogSeries.BOTTLE_VOLUME))[hour_start(BOTTLE.timestamp)] == (
        120.0,
        210.0,
    )
    other = make(OTHER_ID, OTHER_EMAIL)
    await _set_up(hass, other)
    assert started == [importer]
    writer = _writer(hass)
    assert writer is not None
    assert writer[0] == owner.entry_id
    return owner, other


async def test_another_sign_in_never_writes_while_rows_exist_even_with_the_owner_gone(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    owner, _other = await _owner_imported(hass, env, started)
    rows = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)

    assert await hass.config_entries.async_remove(owner.entry_id)
    await hass.async_block_till_done()
    for _ in range(3):
        await _check_owners(hass, freezer)

    assert len(started) == 1
    assert _writer(hass) is None
    assert hass_storage[STORE_KEY]["data"] == {"owners": {CODE: _owner_code(OWNER_EMAIL)}}
    after = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)
    assert {hour: after[hour] for hour in rows} == rows


async def test_after_the_statistics_are_deleted_the_other_sign_in_takes_over_and_imports(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    owner, other = await _owner_imported(hass, env, started)
    # Switching: the import off on the owner, then the statistics deleted.
    hass.config_entries.async_update_entry(
        owner, options={**owner.options, CONF_IMPORT_CARE_LOG_STATISTICS: False}
    )
    await hass.async_block_till_done()
    assert started[0].diagnostics()["state"] == "stopped"
    await _check_owners(hass, freezer)
    assert len(started) == 1

    ids = [statistic_id(CODE, series) for series in CareLogSeries]
    get_instance(hass).async_clear_statistics(ids)
    await async_wait_recording_done(hass)
    assert await _rows(hass, CareLogSeries.BOTTLE_VOLUME) == {}

    await _check_owners(hass, freezer)

    assert len(started) == 2
    writer = _writer(hass)
    assert writer is not None
    assert writer[0] == other.entry_id
    assert hass_storage[STORE_KEY]["data"] == {"owners": {CODE: _owner_code(OTHER_EMAIL)}}
    await _settled(hass, writer[1])
    volume = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)
    assert min(volume) == hour_start(OLD_BOTTLE.timestamp)
    assert volume[hour_start(BOTTLE.timestamp)] == (120.0, 210.0)


async def test_the_owner_continues_after_its_account_is_removed_and_added_again(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
) -> None:
    owner, _other = await _owner_imported(hass, env, started)
    rows = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)
    assert await hass.config_entries.async_remove(owner.entry_id)
    await _check_owners(hass, freezer)

    again = env(OWNER_AGAIN_ID, OWNER_EMAIL)
    await _set_up(hass, again)

    assert len(started) == 2
    writer = _writer(hass)
    assert writer is not None
    assert writer[0] == again.entry_id
    await _settled(hass, writer[1])
    assert writer[1].diagnostics()["state"] == "running"
    after = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)
    assert {hour: after[hour] for hour in rows} == rows


# ---------------------------------------------------------------------------
# Rows nobody is on record for, and a marker that is not saved
# ---------------------------------------------------------------------------


async def _all_rows(hass: HomeAssistant) -> dict[CareLogSeries, dict[int, tuple[float, float]]]:
    return {series: await _rows(hass, series) for series in CareLogSeries}


async def _rows_with_no_owner_on_record(
    hass: HomeAssistant,
    make: Callable[[str, str], MockConfigEntry],
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    marker: dict[str, Any] | None,
) -> dict[CareLogSeries, dict[int, tuple[float, float]]]:
    """Rows an owner wrote; then it is gone and the marker says nothing of the baby."""
    owner = make(OWNER_ID, OWNER_EMAIL)
    await _set_up(hass, owner)
    (importer,) = started
    await _settled(hass, importer)
    assert await hass.config_entries.async_remove(owner.entry_id)
    await hass.async_block_till_done()
    assert care_log_writer_registry(hass) is None
    # As after restoring a database without its .storage, or rows written
    # before the marker existed.
    if marker is None:
        del hass_storage[STORE_KEY]
    else:
        hass_storage[STORE_KEY] = marker
    stored = await _all_rows(hass)
    assert stored[CareLogSeries.BOTTLE_VOLUME]
    return stored


MARKERS = {
    "absent": None,
    "malformed": {"version": 1, "key": STORE_KEY, "data": {"owners": ["not", "a", "map"]}},
    "lacks_the_baby": {
        "version": 1,
        "key": STORE_KEY,
        "data": {"owners": {"f" * 32: _owner_code(OWNER_EMAIL)}},
    },
}


@pytest.mark.parametrize("marker", list(MARKERS))
async def test_rows_with_no_owner_on_record_are_written_by_nobody_until_deleted(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    marker: str,
) -> None:
    from custom_components.nanit import care_log_writers
    from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics

    stored = await _rows_with_no_owner_on_record(hass, env, started, hass_storage, MARKERS[marker])
    claimant = env(OTHER_ID, OTHER_EMAIL)
    with caplog.at_level(logging.WARNING, logger=care_log_writers.__name__):
        await _set_up(hass, claimant)
        for _ in range(3):
            await _check_owners(hass, freezer)

        assert len(started) == 1
        assert _writer(hass) is None
        diag = (await async_get_config_entry_diagnostics(hass, claimant))["care_log_statistics"]
        assert diag["babies"] == {"baby_0": {"writer": False, "state": "unowned"}}
        assert await _all_rows(hass) == stored
        warnings = [r for r in caplog.records if r.name == care_log_writers.__name__]
        assert len(warnings) == 1
        assert warnings[0].levelname == "WARNING"
        message = warnings[0].getMessage()
        for secret in (CODE, UID, _owner_code(OTHER_EMAIL), OTHER_ID, OTHER_EMAIL):
            assert secret not in message

        # The recovery: the baby's statistics deleted, then the next look.
        ids = [statistic_id(CODE, series) for series in CareLogSeries]
        get_instance(hass).async_clear_statistics(ids)
        await async_wait_recording_done(hass)
        await _check_owners(hass, freezer)

    assert len(started) == 2
    writer = _writer(hass)
    assert writer is not None
    assert writer[0] == claimant.entry_id
    assert _owner_code(OTHER_EMAIL) == hass_storage[STORE_KEY]["data"]["owners"][CODE]
    await _settled(hass, writer[1])
    volume = await _rows(hass, CareLogSeries.BOTTLE_VOLUME)
    assert volume[hour_start(BOTTLE.timestamp)] == (120.0, 210.0)
    warnings = [r for r in caplog.records if r.name == care_log_writers.__name__]
    assert len(warnings) == 1


async def test_a_marker_that_is_not_saved_starts_no_importer_and_is_tried_again(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    """The write fails inside Home Assistant's store, which logs it and returns normally."""
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError

    write = Store._async_write_data  # the test storage's own

    async def failing(store: Store[Any], data: dict[str, Any]) -> None:
        if store.key == STORE_KEY:
            raise WriteError("No space left on device")
        await write(store, data)

    first = env(OWNER_ID, OWNER_EMAIL)
    with patch.object(Store, "_async_write_data", failing):
        await _set_up(hass, first)
        await _check_owners(hass, freezer)

        assert started == []
        assert _writer(hass) is None
        assert STORE_KEY not in hass_storage
        assert await _rows(hass, CareLogSeries.BOTTLE_VOLUME) == {}

    # The disk works again: the next look takes the baby, and only now imports.
    await _check_owners(hass, freezer)
    (importer,) = started
    assert hass_storage[STORE_KEY]["data"] == {"owners": {CODE: _owner_code(OWNER_EMAIL)}}
    await _settled(hass, importer)
    stored = await _all_rows(hass)
    assert stored[CareLogSeries.BOTTLE_VOLUME]

    # A restart under another account: it does not write while the rows exist.
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert care_log_writer_registry(hass) is None
    other = env(OTHER_ID, OTHER_EMAIL)
    await _set_up(hass, other)
    for _ in range(2):
        await _check_owners(hass, freezer)
    assert len(started) == 1
    assert _writer(hass) is None
    assert await _all_rows(hass) == stored


async def test_deleting_the_statistics_while_a_batch_is_unsettled_does_not_void_the_marker(
    env: Callable[[str, str], MockConfigEntry],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    started: list[CareLogStatisticsImporter],
    hass_storage: dict[str, Any],
) -> None:
    """The owner's bottle volume batch never shows up: whether it lands later is unknown."""
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    volume = statistic_id(CODE, CareLogSeries.BOTTLE_VOLUME)

    def lost(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        if metadata["statistic_id"] != volume:
            async_add_external_statistics(hass, metadata, rows)

    owner = env(OWNER_ID, OWNER_EMAIL)
    with patch.object(care_log_recorder, "async_add_external_statistics", lost):
        await _set_up(hass, owner)
        (importer,) = started
        await _settled(hass, importer)
    assert importer.diagnostics()["sync_failures"] == 1
    assert await hass.config_entries.async_remove(owner.entry_id)
    await hass.async_block_till_done()

    ids = [statistic_id(CODE, series) for series in CareLogSeries]
    get_instance(hass).async_clear_statistics(ids)
    await async_wait_recording_done(hass)
    empty: dict[CareLogSeries, dict[int, tuple[float, float]]] = {
        series: {} for series in CareLogSeries
    }
    assert await _all_rows(hass) == empty
    other = env(OTHER_ID, OTHER_EMAIL)
    await _set_up(hass, other)
    for _ in range(3):
        await _check_owners(hass, freezer)

    assert len(started) == 1
    assert _writer(hass) is None
    assert hass_storage[STORE_KEY]["data"] == {"owners": {CODE: _owner_code(OWNER_EMAIL)}}
    assert await _all_rows(hass) == empty
    assert care_log_recorder.care_log_paused(hass, CODE)
