"""The care log statistics writer, against a real recorder database.

Ledgers are built through the real ``CareLogBook``: a pass is adopted, and
later reads go through ``process``, each lowering change confirmed by a
re-read a gap later. Rows are read back with Home Assistant's own query
functions and compared with what ``statistic_rows`` builds from the ledger.

All data is synthetic: Monday 2 to Wednesday 4 November 2026, synced up to
Thursday 00:00 UTC.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics as real_add,
)
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistic_during_period,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from sqlalchemy.exc import OperationalError

from aionanit.models import CareLog
from custom_components.nanit import care_log_recorder
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries, hour_start
from custom_components.nanit.care_log_ledger import CareLogBook, CareLogRead, Ledger
from custom_components.nanit.care_log_recorder import (
    CareLogRecorderUnavailableError,
    CareLogSyncError,
    Comparison,
    SyncReport,
    async_compare_care_log_statistics,
    async_sync_care_log_statistics,
    care_log_recorder_ready,
)
from custom_components.nanit.care_log_statistics import (
    statistic_id,
    statistic_metadata,
    statistic_names,
    statistic_rows,
)

S = CareLogSeries
H = 3600
UID = "baby_test_recorder"
CODE = baby_code(UID)
NAMES = statistic_names({}, "Nursery")
READER = "reader-test"
GAP = timedelta(minutes=10)

MON = datetime(2026, 11, 2, tzinfo=UTC)
SUN = MON - timedelta(days=1)
TUE = MON + timedelta(days=1)
WED = MON + timedelta(days=2)
THU = MON + timedelta(days=3)
END = int(THU.timestamp())
READ_START = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
READ_END = int(datetime(2026, 12, 31, tzinfo=UTC).timestamp())
FAR_PAST = datetime(2025, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2027, 1, 1, tzinfo=UTC)


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day + timedelta(hours=hour, minutes=minute)


def _h(day: datetime, hour: int) -> int:
    return int(_at(day, hour).timestamp())


def _log(uid: str, kind: str, when: datetime, **fields: Any) -> CareLog:
    return CareLog(uid=uid, log_type=kind, timestamp=when.timestamp(), baby_uid="b", **fields)


def _bottle(uid: str, when: datetime, ml: float | None, **fields: Any) -> CareLog:
    return _log(uid, "bottle_feed", when, feed_amount_ml=ml, **fields)


def _diaper(uid: str, when: datetime, kind: str) -> CareLog:
    return _log(uid, "diaper_change", when, change_type=kind)


def _nursing(uid: str, when: datetime) -> CareLog:
    return _log(uid, "nursing", when, left_duration_s=600.0, right_duration_s=300.0)


B0 = _bottle("b0", _at(MON, 6, 30), 90.0)
D1 = _diaper("d1", _at(MON, 7, 10), "pee")
B1 = _bottle("b1", _at(MON, 9, 20), 90.0)
D2 = _diaper("d2", _at(TUE, 12, 30), "poo")
D3 = _diaper("d3", _at(TUE, 15), "pee")
N1 = _nursing("n1", _at(WED, 8))
B2 = _bottle("b2", _at(WED, 8, 40), 120.0)
BASE = [B0, D1, B1, D2, D3, N1, B2]

# The series BASE has, and the hour each starts.
STARTS = {
    S.DIAPER_CHANGES: _h(MON, 7),
    S.DIAPER_PEE: _h(MON, 7),
    S.DIAPER_POO: _h(TUE, 12),
    S.BOTTLE_FEEDS: _h(MON, 6),
    S.BOTTLE_VOLUME: _h(MON, 6),
    S.NURSING_SESSIONS: _h(WED, 8),
    S.NURSING_TIME: _h(WED, 8),
    S.NURSING_TIME_LEFT: _h(WED, 8),
    S.NURSING_TIME_RIGHT: _h(WED, 8),
}


class _Book:
    """A real CareLogBook with a clock: every read is a gap and a minute after the last."""

    def __init__(
        self, logs: Iterable[CareLog], *, start: int = READ_START, end: int = READ_END
    ) -> None:
        self.clock = THU + timedelta(hours=12)
        self.start = start
        self.end = end
        self.book = CareLogBook(reader=READER, min_gap=GAP)
        self.book.adopt([self.read(logs)])

    @property
    def ledger(self) -> Ledger:
        return self.book.ledger

    def read(self, logs: Iterable[CareLog]) -> CareLogRead:
        self.clock += GAP + timedelta(minutes=1)
        return CareLogRead.from_logs(
            logs, start=self.start, end=self.end, read_at=self.clock, dropped=0, reader=READER
        )

    def update(self, logs: list[CareLog]) -> dict[CareLogSeries, int]:
        """A read and its confirming re-read; return the earliest changed hour per series."""
        changed: dict[CareLogSeries, int] = {}
        for _ in range(2):
            outcome = self.book.process(self.read(logs))
            for series, hour in outcome.changed_from.items():
                changed[series] = min(hour, changed.get(series, hour))
        assert not self.book.pending_requests()
        return changed


def _expected(
    ledger: Ledger, series: CareLogSeries, start: int, end: int = END
) -> list[tuple[int, float, float]]:
    rows = statistic_rows(ledger.series_hours(series, start, end), start=start, end=end)
    return [(int(row["start"].timestamp()), row["state"], row["sum"]) for row in rows]


async def _stored(
    hass: HomeAssistant, series: CareLogSeries, code: str = CODE
) -> list[tuple[int, Any, Any]]:
    sid = statistic_id(code, series)
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
    return [(int(row["start"]), row["state"], row["sum"]) for row in found.get(sid, [])]


async def _metadata_ids(hass: HomeAssistant, code: str = CODE) -> set[str]:
    ids = {statistic_id(code, series) for series in CareLogSeries}
    found = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=ids)
    )
    return set(found)


async def _assert_matches(
    hass: HomeAssistant, ledger: Ledger, starts: dict[CareLogSeries, int], code: str = CODE
) -> None:
    for series in CareLogSeries:
        stored = await _stored(hass, series, code)
        if series in starts:
            assert stored == _expected(ledger, series, starts[series]), series
        else:
            assert stored == [], series


async def _sync(
    hass: HomeAssistant,
    ledger: Ledger,
    *,
    end: int = END,
    changed_from: dict[CareLogSeries, int] | None = None,
    should_stop: Callable[[], bool] | None = None,
    code: str = CODE,
    coverage: int = READ_START,
) -> SyncReport:
    return await async_sync_care_log_statistics(
        hass,
        code=code,
        names=NAMES,
        ledger=ledger,
        end_hour=end,
        coverage_start=coverage,
        changed_from=changed_from,
        should_stop=should_stop,
    )


async def _compare(
    hass: HomeAssistant, ledger: Ledger, *, end: int = END, coverage: int = READ_START
) -> Comparison:
    return await async_compare_care_log_statistics(
        hass, code=CODE, ledger=ledger, end_hour=end, coverage_start=coverage
    )


Sent = list[tuple[str, list[tuple[int, float, float]]]]


@pytest.fixture
def sent() -> Iterator[Sent]:
    """Every call to async_add_external_statistics: the id and the (hour, state, sum) rows."""
    calls: Sent = []

    def spy(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        rows = list(rows)
        calls.append(
            (
                metadata["statistic_id"],
                [(int(r["start"].timestamp()), r["state"], r["sum"]) for r in rows],
            )
        )
        real_add(hass, metadata, rows)

    with patch.object(care_log_recorder, "async_add_external_statistics", spy):
        yield calls


def _hours(sent: Sent, series: CareLogSeries) -> list[int]:
    sid = statistic_id(CODE, series)
    return [hour for call_sid, rows in sent if call_sid == sid for hour, _s, _t in rows]


def _sent_series(sent: Sent) -> set[str]:
    return {sid for sid, _rows in sent}


def _ids(*series: CareLogSeries) -> set[str]:
    return {statistic_id(CODE, one) for one in series}


@pytest.fixture(autouse=True)
def quick_verify() -> Iterator[None]:
    with (
        patch.object(care_log_recorder, "VERIFY_RETRY_DELAY", 0.01),
        patch.object(care_log_recorder, "WRITE_TIMEOUT", 5.0),
    ):
        yield


@pytest.fixture
async def utc(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


async def _imported(hass: HomeAssistant, logs: list[CareLog] = BASE) -> _Book:
    book = _Book(logs)
    await _sync(hass, book.ledger)
    return book


# ---------------------------------------------------------------------------
# First import
# ---------------------------------------------------------------------------


async def _day_changes(
    hass: HomeAssistant, series: CareLogSeries, start: datetime, days: int
) -> tuple[list[Any], list[Any]]:
    """Daily changes by statistics_during_period, and by statistic_during_period."""
    sid = statistic_id(CODE, series)
    instance = get_instance(hass)
    end = start + timedelta(days=days)
    found = await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end - timedelta(microseconds=1),
        {sid},
        "day",
        None,
        {"change"},
    )
    by_period = [row["change"] for row in found[sid]]
    by_one = []
    for day in range(days):
        low = start + timedelta(days=day)
        result = await instance.async_add_executor_job(
            statistic_during_period, hass, low, low + timedelta(days=1), sid, {"change"}, None
        )
        by_one.append(result["change"])
    return by_period, by_one


@pytest.mark.usefixtures("utc")
async def test_a_first_import_starts_each_series_at_its_first_entry(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = _Book(BASE)
    report = await _sync(hass, book.ledger)

    await _assert_matches(hass, book.ledger, STARTS)
    assert await _metadata_ids(hass) == _ids(*STARTS)
    rows = sum((END - start) // H for start in STARTS.values())
    assert report == SyncReport(
        series_examined=len(STARTS),
        rows_read=0,
        rows_written=rows,
        batches=len(STARTS),
        stopped=False,
    )
    pee = await _stored(hass, S.DIAPER_PEE)
    assert pee[0] == (_h(MON, 7), 1.0, 1.0)
    assert pee[-1] == (END - H, 0.0, 2.0)
    assert [hour for hour, _s, _t in pee] == list(range(_h(MON, 7), END, H))

    assert await _day_changes(hass, S.DIAPER_CHANGES, MON, 3) == ([1.0, 2.0, 0.0], [1.0, 2.0, 0.0])
    assert await _day_changes(hass, S.BOTTLE_VOLUME, MON, 3) == (
        [180.0, 0.0, 120.0],
        [180.0, 0.0, 120.0],
    )
    assert await _day_changes(hass, S.NURSING_TIME, TUE, 2) == ([15.0], [None, 15.0])


# ---------------------------------------------------------------------------
# Nothing changed, and the hourly extension
# ---------------------------------------------------------------------------


async def test_an_unchanged_sync_writes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    full = await _sync(hass, book.ledger)
    assert sent == []
    assert full.rows_written == full.batches == 0
    assert full.rows_read >= sum((END - start) // H for start in STARTS.values())

    incremental = await _sync(hass, book.ledger, changed_from={})
    assert sent == []
    assert incremental.rows_read == len(STARTS)  # one last row per series

    listed = await _sync(hass, book.ledger, changed_from={S.BOTTLE_VOLUME: _h(MON, 9)})
    assert sent == []
    assert listed.rows_written == 0


async def test_an_hour_later_every_series_gets_one_zero_row(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    before = {series: await _stored(hass, series) for series in STARTS}
    sent.clear()

    report = await _sync(hass, book.ledger, end=END + H, changed_from={})

    assert len(sent) == len(STARTS)
    assert _sent_series(sent) == _ids(*STARTS)
    for series in STARTS:
        last_sum = before[series][-1][2]
        assert _hours(sent, series) == [END]
        assert (await _stored(hass, series))[-1] == (END, 0.0, last_sum)
    assert report.rows_written == len(STARTS)


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


async def test_a_late_entry_rewrites_from_its_hour_only(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    changed = book.update([*BASE, _bottle("b9", _at(MON, 12, 15), 60.0)])
    assert changed == {S.BOTTLE_FEEDS: _h(MON, 12), S.BOTTLE_VOLUME: _h(MON, 12)}
    await _sync(hass, book.ledger, changed_from=changed)

    assert _sent_series(sent) == _ids(S.BOTTLE_FEEDS, S.BOTTLE_VOLUME)
    assert _hours(sent, S.BOTTLE_VOLUME) == list(range(_h(MON, 12), END, H))
    assert _hours(sent, S.BOTTLE_FEEDS) == list(range(_h(MON, 12), END, H))
    await _assert_matches(hass, book.ledger, STARTS)


async def test_a_move_forward_and_back_sends_only_the_hours_between(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    moved = _bottle("b1", _at(TUE, 12, 20), 90.0)
    between = list(range(_h(MON, 9), _h(TUE, 12) + H, H))

    for logs in ([B0, D1, moved, D2, D3, N1, B2], BASE):
        sent.clear()
        changed = book.update(logs)
        assert changed[S.BOTTLE_VOLUME] == _h(MON, 9)
        await _sync(hass, book.ledger, changed_from=changed)

        assert _sent_series(sent) == _ids(S.BOTTLE_FEEDS, S.BOTTLE_VOLUME)
        assert _hours(sent, S.BOTTLE_VOLUME) == between
        assert _hours(sent, S.BOTTLE_FEEDS) == between
        await _assert_matches(hass, book.ledger, STARTS)


async def test_a_second_difference_after_the_totals_rejoin_is_also_written(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()
    moved = _bottle("b1", _at(TUE, 12, 20), 90.0)
    late = _bottle("b9", _at(WED, 3, 5), 50.0)

    changed = book.update([B0, D1, moved, D2, D3, N1, B2, late])
    await _sync(hass, book.ledger, changed_from=changed)

    expected = [*range(_h(MON, 9), _h(TUE, 12) + H, H), *range(_h(WED, 3), END, H)]
    assert _hours(sent, S.BOTTLE_VOLUME) == expected
    await _assert_matches(hass, book.ledger, STARTS)


async def test_deleting_the_first_entry_keeps_the_start_and_shifts_the_totals(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    changed = book.update([B0, B1, D2, D3, N1, B2])  # d1 deleted
    assert book.ledger.first_hour(S.DIAPER_PEE) == _h(TUE, 15)
    await _sync(hass, book.ledger, changed_from=changed)

    pee = await _stored(hass, S.DIAPER_PEE)
    assert pee[0] == (_h(MON, 7), 0.0, 0.0)
    assert len(pee) == (END - _h(MON, 7)) // H
    changes = await _stored(hass, S.DIAPER_CHANGES)
    assert changes[0] == (_h(MON, 7), 0.0, 0.0)
    assert changes[-1][2] == 2.0
    await _assert_matches(hass, book.ledger, STARTS)
    # A full sync agrees: nothing more to write.
    sent.clear()
    await _sync(hass, book.ledger)
    assert sent == []


async def test_deleting_the_last_entry_lowers_the_end_of_the_series(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    changed = book.update([B0, D1, B1, D2, D3, N1])  # b2 deleted
    await _sync(hass, book.ledger, changed_from=changed)

    assert _hours(sent, S.BOTTLE_VOLUME) == list(range(_h(WED, 8), END, H))
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[-1] == (END - H, 0.0, 180.0)
    assert len(volume) == (END - _h(MON, 6)) // H
    await _assert_matches(hass, book.ledger, STARTS)


async def test_an_entry_before_the_first_row_moves_the_start_back(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    changed = book.update([_diaper("d0", _at(SUN, 22, 10), "pee"), *BASE])
    await _sync(hass, book.ledger, changed_from=changed)

    for series in (S.DIAPER_CHANGES, S.DIAPER_PEE):
        assert _hours(sent, series) == list(range(_h(SUN, 22), END, H))
        assert (await _stored(hass, series))[0] == (_h(SUN, 22), 1.0, 1.0)
    assert _sent_series(sent) == _ids(S.DIAPER_CHANGES, S.DIAPER_PEE)
    await _assert_matches(
        hass, book.ledger, {**STARTS, S.DIAPER_CHANGES: _h(SUN, 22), S.DIAPER_PEE: _h(SUN, 22)}
    )


async def test_a_keep_cell_keeps_its_stored_state_until_the_amount_is_valid(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    rejected = {"feed_amount_ml": None, "invalid_fields": frozenset({"feed_amount_ml"})}
    unusable = _log("b6", "bottle_feed", _at(TUE, 10), **rejected)
    book = await _imported(hass, [*BASE, unusable])
    # No row was stored for the keep hour yet, so the ledger's amount (none) was written.
    volume = {hour: (state, total) for hour, state, total in await _stored(hass, S.BOTTLE_VOLUME)}
    assert volume[_h(TUE, 10)] == (0.0, 180.0)
    sent.clear()

    # b1 is deleted, and a new entry in its hour has a rejected amount; a late
    # entry before it shifts the totals.
    b5 = _log("b5", "bottle_feed", _at(MON, 9, 40), **rejected)
    late = _bottle("b9", _at(MON, 7, 30), 30.0)
    logs = [B0, D1, b5, late, D2, D3, N1, B2, unusable]
    changed = book.update(logs)
    assert book.ledger.series_hours(S.BOTTLE_VOLUME, _h(MON, 9), _h(MON, 10)) == {}
    await _sync(hass, book.ledger, changed_from=changed)

    volume = {hour: (state, total) for hour, state, total in await _stored(hass, S.BOTTLE_VOLUME)}
    assert volume[_h(MON, 9)] == (90.0, 210.0)  # stored state kept, sum follows
    assert volume[END - H] == (0.0, 330.0)
    assert (_h(MON, 9), 90.0, 210.0) in [
        row for sid, rows in sent if sid == statistic_id(CODE, S.BOTTLE_VOLUME) for row in rows
    ]
    sent.clear()
    # A full sync keeps the kept state too: nothing more to write.
    assert (await _sync(hass, book.ledger)).rows_written == 0
    assert sent == []

    # The amount becomes valid: the row is corrected.
    changed = book.update([B0, D1, _bottle("b5", _at(MON, 9, 40), 60.0), late, *BASE[3:], unusable])
    await _sync(hass, book.ledger, changed_from=changed)
    volume = {hour: (state, total) for hour, state, total in await _stored(hass, S.BOTTLE_VOLUME)}
    assert volume[_h(MON, 9)] == (60.0, 180.0)
    assert volume[END - H] == (0.0, 300.0)


async def test_a_diaper_type_change_lowers_one_series_and_raises_another(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()

    changed = book.update([B0, D1, B1, D2, _diaper("d3", _at(TUE, 15), "poo"), N1, B2])
    await _sync(hass, book.ledger, changed_from=changed)

    assert _sent_series(sent) == _ids(S.DIAPER_PEE, S.DIAPER_POO)
    pee = {hour: (state, total) for hour, state, total in await _stored(hass, S.DIAPER_PEE)}
    poo = {hour: (state, total) for hour, state, total in await _stored(hass, S.DIAPER_POO)}
    assert pee[_h(TUE, 15)] == (0.0, 1.0)
    assert poo[_h(TUE, 15)] == (1.0, 2.0)
    await _assert_matches(hass, book.ledger, STARTS)


# ---------------------------------------------------------------------------
# History coverage
# ---------------------------------------------------------------------------


async def test_rows_before_the_coverage_hour_are_never_compared_or_written_and_totals_continue(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    await _imported(hass)
    before = {series: await _stored(hass, series) for series in STARTS}
    sent.clear()
    # A history that starts half an hour before Tuesday: Monday's entries are
    # not in it, and the coverage hour is Tuesday 00:00.
    coverage = int(TUE.timestamp()) - 1800
    late = _bottle("b9", _at(TUE, 3, 5), 50.0)
    book = _Book([D2, D3, N1, B2, late], start=coverage)

    comparison = await _compare(hass, book.ledger, coverage=coverage)
    assert (comparison.lowering_cells, comparison.raising_cells) == (0, 2)
    assert comparison.missing_rows == 0

    await _sync(hass, book.ledger, coverage=coverage)

    assert min(hour for _sid, rows in sent for hour, _s, _t in rows) == _h(TUE, 3)
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert [row for row in volume if row[0] < _h(TUE, 0)] == [
        row for row in before[S.BOTTLE_VOLUME] if row[0] < _h(TUE, 0)
    ]
    by_hour = {hour: (state, total) for hour, state, total in volume}
    assert by_hour[_h(TUE, 2)] == (0.0, 180.0)
    assert by_hour[_h(TUE, 3)] == (50.0, 230.0)
    assert by_hour[END - H] == (0.0, 350.0)
    feeds = {hour: (state, total) for hour, state, total in await _stored(hass, S.BOTTLE_FEEDS)}
    assert feeds[_h(TUE, 3)] == (1.0, 3.0)
    assert feeds[END - H] == (0.0, 4.0)
    for series in (S.DIAPER_CHANGES, S.DIAPER_PEE):
        assert await _stored(hass, series) == before[series]
    sent.clear()
    assert (await _sync(hass, book.ledger, coverage=coverage)).rows_written == 0
    assert sent == []

    # A change after the coverage hour continues the totals too.
    changed = book.update([D2, D3, N1, B2, late, _bottle("b8", _at(WED, 1), 30.0)])
    await _sync(hass, book.ledger, changed_from=changed, coverage=coverage)
    by_hour = {hour: (s, t) for hour, s, t in await _stored(hass, S.BOTTLE_VOLUME)}
    assert by_hour[_h(WED, 1)] == (30.0, 260.0)
    assert by_hour[END - H] == (0.0, 380.0)


async def test_stale_rows_of_an_interrupted_backfill_are_found_across_a_gap_and_corrected(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = await _imported(hass)
    sent.clear()
    # An entry backdated about 5,900 hours before the rows (more than two
    # chunks); its backfill stops after the first batch.
    back = _bottle("b_back", datetime(2026, 3, 1, 8, 15, tzinfo=UTC), 70.0)
    hour = hour_start(back.timestamp)
    changed = book.update([*BASE, back])
    with patch.object(care_log_recorder, "BATCH_ROWS", 100):
        report = await _sync(
            hass, book.ledger, changed_from=changed, should_stop=lambda: bool(sent)
        )
    assert report.stopped
    feeds = await _stored(hass, S.BOTTLE_FEEDS)
    assert feeds[0] == (hour, 1.0, 1.0)
    assert [row[0] for row in feeds[:101]] == [*range(hour, hour + 100 * H, H), _h(MON, 6)]
    assert _h(MON, 6) - (hour + 100 * H) > 2 * care_log_recorder.CHUNK_HOURS * H

    # The entry is withdrawn; after a restart the ledger is rebuilt without it.
    rebuilt = _Book(BASE)
    comparison = await _compare(hass, rebuilt.ledger)
    assert comparison.lowering_cells == 1  # the stale feed
    sent.clear()
    await _sync(hass, rebuilt.ledger)

    # Dense and correct from the earliest remaining row.
    assert await _stored(hass, S.BOTTLE_FEEDS) == _expected(rebuilt.ledger, S.BOTTLE_FEEDS, hour)
    await _assert_matches(hass, rebuilt.ledger, {**STARTS, S.BOTTLE_FEEDS: hour})
    sent.clear()
    assert (await _sync(hass, rebuilt.ledger)).rows_written == 0
    assert (await _compare(hass, rebuilt.ledger)).rows_to_write == 0


async def test_a_series_stored_only_before_the_coverage_hour_goes_on_from_it_with_zeros(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    await _imported(hass, [B0, B1, D2])
    before = {series: await _stored(hass, series) for series in CareLogSeries}
    established = [series for series in CareLogSeries if before[series]]
    assert len(established) == 4
    sent.clear()
    coverage = END + 1800  # every stored row is before the coverage hour, Thursday 01:00
    empty = _Book([], start=coverage)

    comparison = await _compare(hass, empty.ledger, end=END + 3 * H, coverage=coverage)
    assert (
        comparison.lowering_cells,
        comparison.raising_cells,
        comparison.missing_rows,
        comparison.rows_to_write,
    ) == (0, 0, 2 * len(established), 2 * len(established))
    await _sync(hass, empty.ledger, end=END + 3 * H, coverage=coverage)

    # Dense from the coverage hour, the prefix's total carried on; the prefix
    # (and the hour before the coverage hour) untouched.
    assert _sent_series(sent) == _ids(*established)
    for series in established:
        total = before[series][-1][2]
        assert await _stored(hass, series) == [
            *before[series],
            (END + H, 0.0, total),
            (END + 2 * H, 0.0, total),
        ]
    sent.clear()

    # A new entry after the coverage hour starts there, from the prefix's total.
    book = _Book([_bottle("b9", _at(THU, 1, 10), 40.0)], start=coverage)
    await _sync(hass, book.ledger, end=END + 3 * H, coverage=coverage)
    assert _sent_series(sent) == _ids(S.BOTTLE_FEEDS, S.BOTTLE_VOLUME)
    assert min(hour for _sid, rows in sent for hour, _s, _t in rows) == END + H
    by_hour = {hour: (s, t) for hour, s, t in await _stored(hass, S.BOTTLE_VOLUME)}
    assert END not in by_hour
    assert by_hour[END + H] == (40.0, 220.0)
    assert by_hour[END + 2 * H] == (0.0, 220.0)
    for series in CareLogSeries:
        if series not in (S.BOTTLE_FEEDS, S.BOTTLE_VOLUME):
            assert [row for row in await _stored(hass, series) if row[0] < END] == before[series]


@pytest.mark.usefixtures("utc")
async def test_an_established_series_is_dense_from_the_coverage_hour_for_both_chart_queries(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A prior total of 1, and the next entry on Tuesday at noon: Tuesday's total is 1."""
    first = _Book([D1])  # Monday 07:10
    await _sync(hass, first.ledger, end=_h(MON, 8))  # one row, Monday 07:00: 1, 1
    coverage = int(TUE.timestamp())
    noon = _diaper("d9", _at(TUE, 12, 5), "pee")
    book = _Book([noon], start=coverage)

    await _sync(hass, book.ledger, coverage=coverage)

    # Tuesday reads 1 and Wednesday 0, by statistics_during_period and by
    # statistic_during_period alike.
    assert await _day_changes(hass, S.DIAPER_CHANGES, TUE, 2) == ([1.0, 0.0], [1.0, 0.0])
    assert await _day_changes(hass, S.DIAPER_PEE, TUE, 2) == ([1.0, 0.0], [1.0, 0.0])
    changes = await _stored(hass, S.DIAPER_CHANGES)
    assert changes[0] == (_h(MON, 7), 1.0, 1.0)
    assert changes[1] == (_h(TUE, 0), 0.0, 1.0)
    assert [hour for hour, _s, _t in changes[1:]] == list(range(_h(TUE, 0), END, H))
    assert (_h(TUE, 12), 1.0, 2.0) in changes
    # A second sync, full or not, has nothing more to write.
    assert (await _sync(hass, book.ledger, coverage=coverage)).rows_written == 0
    comparison = await _compare(hass, book.ledger, coverage=coverage)
    assert (comparison.lowering_cells, comparison.missing_rows) == (0, 0)


# ---------------------------------------------------------------------------
# Batches, stopping and failed writes
# ---------------------------------------------------------------------------


async def test_a_long_range_goes_in_batches_each_read_back_before_the_next(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    # One timeline of writes (id, first hour, last hour) and reads (id, start, end).
    events: list[tuple[str, str, int, int]] = []
    real_read = care_log_recorder._read_rows

    def read(hass: HomeAssistant, sid: str, start: int, end: int) -> Any:
        events.append(("read", sid, start, end))
        return real_read(hass, sid, start, end)

    def add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        rows = list(rows)
        hours = [int(row["start"].timestamp()) for row in rows]
        assert hours == list(range(hours[0], hours[-1] + H, H))
        events.append(("write", metadata["statistic_id"], hours[0], hours[-1]))
        real_add(hass, metadata, rows)

    book = _Book(BASE)
    with (
        patch.object(care_log_recorder, "BATCH_ROWS", 7),
        patch.object(care_log_recorder, "_read_rows", read),
        patch.object(care_log_recorder, "async_add_external_statistics", add),
    ):
        report = await _sync(hass, book.ledger)
    await _assert_matches(hass, book.ledger, STARTS)

    writes = [index for index, event in enumerate(events) if event[0] == "write"]
    assert report.batches == len(writes)
    assert all((events[i][3] - events[i][2]) // H + 1 <= 7 for i in writes)
    changes = statistic_id(CODE, S.DIAPER_CHANGES)
    assert len([i for i in writes if events[i][1] == changes]) == -(-((END - _h(MON, 7)) // H) // 7)
    # Between each batch and the next one sent, a read that covers the batch.
    for index, following in zip(writes, [*writes[1:], len(events)], strict=True):
        _kind, sid, first, last = events[index]
        assert any(
            kind == "read" and rsid == sid and start <= first and last < end
            for kind, rsid, start, end in events[index + 1 : following]
        )


async def test_a_stopped_first_import_is_finished_by_the_next_sync(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = _Book(BASE)
    with patch.object(care_log_recorder, "BATCH_ROWS", 10):
        first = await _sync(hass, book.ledger, should_stop=lambda: len(sent) >= 3)
        assert first.stopped
        assert first.batches == 3
        assert len(sent) == 3
        second = await _sync(hass, book.ledger)
    assert not second.stopped

    other = baby_code("baby_test_recorder_one_shot")
    await _sync(hass, book.ledger, code=other)
    for series in CareLogSeries:
        assert await _stored(hass, series) == await _stored(hass, series, other)
    await _assert_matches(hass, book.ledger, STARTS)


def _dropping(drop: Callable[[int, str], bool], calls: list[str]) -> Callable[..., None]:
    def add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        calls.append(metadata["statistic_id"])
        if not drop(len(calls), metadata["statistic_id"]):
            real_add(hass, metadata, rows)

    return add


async def test_a_dropped_batch_stops_the_sync_and_the_next_one_converges(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    book = _Book(BASE)
    calls: list[str] = []
    with (
        patch.object(care_log_recorder, "BATCH_ROWS", 10),
        patch.object(
            care_log_recorder,
            "async_add_external_statistics",
            _dropping(lambda n, _sid: n == 3, calls),
        ),
        pytest.raises(CareLogSyncError),
    ):
        await _sync(hass, book.ledger)
    assert len(calls) == 3  # nothing sent after the dropped batch

    await _sync(hass, book.ledger)
    await _assert_matches(hass, book.ledger, STARTS)


async def test_a_lost_final_batch_is_reported_and_written_next_time(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    book = _Book(BASE)
    last = statistic_id(CODE, S.NURSING_TIME_RIGHT)
    calls: list[str] = []
    with (
        patch.object(
            care_log_recorder,
            "async_add_external_statistics",
            _dropping(lambda _n, sid: sid == last, calls),
        ),
        pytest.raises(CareLogSyncError),
    ):
        await _sync(hass, book.ledger)
    assert calls[-1] == last
    assert len(calls) == len(STARTS)

    await _sync(hass, book.ledger)
    assert _sent_series(sent) == {last}
    await _assert_matches(hass, book.ledger, STARTS)


async def test_a_write_that_always_fails_is_never_reported_done(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    book = _Book(BASE)
    boom = OperationalError("statement", {}, Exception("disk I/O error"))
    with patch(
        "homeassistant.components.recorder.statistics._import_statistics_with_session",
        side_effect=boom,
    ):
        for _ in range(2):
            with pytest.raises(CareLogSyncError):
                await _sync(hass, book.ledger)
    assert await _metadata_ids(hass) == set()

    def refuse(*_args: Any) -> None:
        raise HomeAssistantError("Invalid timestamp")

    with (
        patch.object(care_log_recorder, "async_add_external_statistics", refuse),
        pytest.raises(CareLogSyncError),
    ):
        await _sync(hass, book.ledger)


async def test_a_write_the_recorder_retries_is_verified_after_a_retry(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The import task re-queues itself, as after a retryable database error."""
    from homeassistant.components.recorder import statistics

    real_import = statistics.import_statistics
    calls: list[int] = []
    reads: list[int] = []
    real_read = care_log_recorder._read_rows

    def flaky(*args: Any) -> bool:
        calls.append(1)
        if len(calls) == 1:
            return False  # not finished: the task queues itself again
        if len(calls) == 2:
            time.sleep(0.3)  # the retry runs after the first wait has returned
        return real_import(*args)

    def read(hass: HomeAssistant, sid: str, start: int, end: int) -> Any:
        reads.append(1)
        return real_read(hass, sid, start, end)

    book = _Book([B0])
    with (
        patch.object(statistics, "import_statistics", flaky),
        patch.object(care_log_recorder, "_read_rows", read),
    ):
        report = await _sync(hass, book.ledger)

    assert report.rows_written == 2 * (END - _h(MON, 6)) // H
    assert len(calls) == 3  # two series, the first one twice
    assert len(reads) >= 4 + 1  # two scans, two read-backs, and at least one retry
    await _assert_matches(
        hass, book.ledger, {S.BOTTLE_FEEDS: _h(MON, 6), S.BOTTLE_VOLUME: _h(MON, 6)}
    )


async def test_a_write_that_shows_up_late_is_accepted(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    def later(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        hass.loop.call_later(0.05, real_add, hass, metadata, list(rows))

    book = _Book([B0])
    with patch.object(care_log_recorder, "async_add_external_statistics", later):
        report = await _sync(hass, book.ledger)
    assert report.batches == 2
    await _assert_matches(
        hass, book.ledger, {S.BOTTLE_FEEDS: _h(MON, 6), S.BOTTLE_VOLUME: _h(MON, 6)}
    )


async def test_a_cancelled_sync_still_verifies_the_batch_it_sent_and_sends_no_other(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    """The recorder holds the first batch while the sync is cancelled."""
    from homeassistant.components.recorder import statistics

    book = _Book(BASE)
    entered = threading.Event()
    gate = threading.Event()
    real_import = statistics.import_statistics

    def held(*args: Any) -> bool:
        entered.set()
        gate.wait(30)
        result: bool = real_import(*args)
        return result

    with patch.object(statistics, "import_statistics", held):
        task = hass.async_create_task(_sync(hass, book.ledger))
        assert await hass.async_add_executor_job(entered.wait, 30)
        task.cancel()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done()  # still waiting for the batch it sent
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    ((sid, rows),) = sent
    assert sid == statistic_id(CODE, S.DIAPER_CHANGES)
    assert await _stored(hass, S.DIAPER_CHANGES) == rows


async def test_the_comparison_first_waits_for_what_the_recorder_has_queued(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    from homeassistant.components.recorder import statistics

    book = _Book([B0])
    real_import = statistics.import_statistics

    def slow(*args: Any) -> bool:
        time.sleep(0.3)
        result: bool = real_import(*args)
        return result

    with patch.object(statistics, "import_statistics", slow):
        for series in (S.BOTTLE_FEEDS, S.BOTTLE_VOLUME):
            amounts = book.ledger.series_hours(series, _h(MON, 6), END)
            rows = statistic_rows(amounts, start=_h(MON, 6), end=END)
            real_add(hass, statistic_metadata(CODE, series, NAMES[series]), rows)
        comparison = await _compare(hass, book.ledger)

    assert (comparison.missing_rows, comparison.rows_to_write) == (0, 0)


# ---------------------------------------------------------------------------
# Comparison and the start-up decision
# ---------------------------------------------------------------------------


async def test_the_comparison_counts_lowering_raising_and_missing_cells(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    await _imported(hass, [B0, B1, B2])
    sent.clear()
    rejected = {"feed_amount_ml": None, "invalid_fields": frozenset({"feed_amount_ml"})}
    # b0 gone (its hour is now before the ledger's first hour), b2's amount
    # rejected (a keep cell), and a new bottle on Tuesday.
    rebuilt = CareLogBook(reader=READER, min_gap=GAP)
    ledger = rebuilt.candidate(
        [
            _Book([]).read(
                [
                    B1,
                    _log("b2", "bottle_feed", _at(WED, 8, 40), **rejected),
                    _bottle("b3", _at(TUE, 10), 60.0),
                ]
            )
        ]
    )

    result = await _compare(hass, ledger)
    rows = 2 * (END - _h(MON, 6)) // H
    # Lowering: feeds and volume at Monday 06:00. Raising: both at Tuesday 10:00.
    assert (result.lowering_cells, result.raising_cells, result.missing_rows) == (2, 2, 0)
    assert result.rows_read >= rows
    assert result.rows_to_write > 0

    later = await _compare(hass, ledger, end=END + 2 * H)
    assert later.missing_rows == 4
    assert (later.lowering_cells, later.raising_cells) == (2, 2)
    assert sent == []

    # A ledger equal to what is stored differs in nothing.
    same = await _compare(hass, rebuilt.candidate([_Book([]).read([B0, B1, B2])]))
    assert (same.lowering_cells, same.raising_cells, same.missing_rows, same.rows_to_write) == (
        0,
        0,
        0,
        0,
    )
    assert sent == []


async def test_start_up_adopts_at_once_when_nothing_would_be_lowered(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    await _imported(hass)
    sent.clear()

    reads = _Book([])
    rebuilt = CareLogBook(reader=READER, min_gap=GAP)
    first = [reads.read([*BASE, _bottle("b9", _at(TUE, 3), 40.0)])]
    comparison = await _compare(hass, rebuilt.candidate(first))
    assert comparison.lowering_cells == 0
    assert comparison.raising_cells == 2
    assert sent == []

    rebuilt.adopt(first)
    await _sync(hass, rebuilt.ledger)
    assert _sent_series(sent) == _ids(S.BOTTLE_FEEDS, S.BOTTLE_VOLUME)
    await _assert_matches(hass, rebuilt.ledger, STARTS)
    assert (await _compare(hass, rebuilt.ledger)).rows_to_write == 0


async def test_start_up_waits_for_a_second_pass_before_lowering(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    await _imported(hass)
    sent.clear()

    reads = _Book([])
    rebuilt = CareLogBook(reader=READER, min_gap=GAP)
    without_b1 = [B0, D1, D2, D3, N1, B2]
    first = [reads.read(without_b1)]
    comparison = await _compare(hass, rebuilt.candidate(first))
    assert comparison.lowering_cells == 2  # feeds and volume at Monday 09:00
    assert sent == []

    second = [reads.read(without_b1)]
    assert rebuilt.passes_agree(first, second)
    rebuilt.adopt(second)
    await _sync(hass, rebuilt.ledger)
    assert _hours(sent, S.BOTTLE_VOLUME) == list(range(_h(MON, 9), END, H))
    await _assert_matches(hass, rebuilt.ledger, STARTS)
    after = await _compare(hass, rebuilt.ledger)
    assert (after.lowering_cells, after.raising_cells, after.rows_to_write) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Recorder not ready
# ---------------------------------------------------------------------------


async def test_without_the_recorder_nothing_is_attempted(hass: HomeAssistant) -> None:
    assert not care_log_recorder_ready(hass)
    ledger = _Book(BASE).ledger
    with pytest.raises(CareLogRecorderUnavailableError):
        await _sync(hass, ledger)
    with pytest.raises(CareLogRecorderUnavailableError):
        await _compare(hass, ledger)


async def test_a_recorder_that_is_not_recording_is_not_ready(
    recorder_mock: Recorder, hass: HomeAssistant, sent: Sent
) -> None:
    assert care_log_recorder_ready(hass)
    ledger = _Book(BASE).ledger
    with patch.object(Recorder, "recording", new_callable=PropertyMock, return_value=False):
        assert not care_log_recorder_ready(hass)
        with pytest.raises(CareLogRecorderUnavailableError):
            await _sync(hass, ledger)
        with pytest.raises(CareLogRecorderUnavailableError):
            await _compare(hass, ledger)
    with patch.object(recorder_mock, "migration_in_progress", True):
        assert not care_log_recorder_ready(hass)
    # The recorder.disable action: the listener stays, recording is off.
    await hass.services.async_call("recorder", "disable", blocking=True)
    assert recorder_mock.recording
    assert not care_log_recorder_ready(hass)
    with pytest.raises(CareLogRecorderUnavailableError):
        await _sync(hass, ledger)
    await hass.services.async_call("recorder", "enable", blocking=True)
    assert care_log_recorder_ready(hass)
    assert sent == []
    assert await _metadata_ids(hass) == set()


# ---------------------------------------------------------------------------
# Privacy and the event loop
# ---------------------------------------------------------------------------


async def test_no_uid_baby_id_name_or_code_in_logs_errors_or_reports(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    raw_id = "raw-baby-MARKER-41"
    uid = "entry-MARKER-uid-17"
    name = "Marker Baby Name"
    code = baby_code(raw_id)
    names = statistic_names({}, name)
    book = _Book([_bottle(uid, _at(MON, 6), 70.0), D1])
    caplog.set_level(logging.DEBUG, logger="custom_components.nanit")

    report = await async_sync_care_log_statistics(
        hass, code=code, names=names, ledger=book.ledger, end_hour=END, coverage_start=READ_START
    )
    late = book.update([_bottle(uid, _at(MON, 6), 70.0), D1, _bottle("x2", _at(MON, 8), 1.0)])
    calls: list[str] = []
    with (
        patch.object(
            care_log_recorder,
            "async_add_external_statistics",
            _dropping(lambda _n, _s: True, calls),
        ),
        pytest.raises(CareLogSyncError) as failure,
    ):
        await async_sync_care_log_statistics(
            hass,
            code=code,
            names=names,
            ledger=book.ledger,
            end_hour=END,
            coverage_start=READ_START,
            changed_from=late,
        )

    ours = [r for r in caplog.records if r.name.startswith("custom_components.nanit")]
    assert any(r.levelno == logging.DEBUG for r in ours)
    assert any(r.levelno == logging.WARNING for r in ours)
    texts = [
        *(r.getMessage() for r in ours),
        str(failure.value),
        repr(failure.value),
        repr(report),
        repr(await _compare_code(hass, code, book.ledger)),
    ]
    for text in texts:
        for marker in (raw_id, uid, name, code, "MARKER"):
            assert marker not in text


async def _compare_code(hass: HomeAssistant, code: str, ledger: Ledger) -> Comparison:
    return await async_compare_care_log_statistics(
        hass, code=code, ledger=ledger, end_hour=END, coverage_start=READ_START
    )


async def test_a_long_sync_yields_between_chunks(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    days = 250
    start = MON - timedelta(days=days)
    logs = [_diaper(f"d{day}", start + timedelta(days=day, hours=7), "pee") for day in range(days)]
    book = _Book(logs, start=int(start.timestamp()), end=READ_END)

    ticks = 0

    async def probe() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    seen: list[int] = []
    real_cells = care_log_recorder._cells

    def cells(*args: Any) -> Any:
        seen.append(ticks)
        return real_cells(*args)

    task = hass.loop.create_task(probe())
    try:
        with patch.object(care_log_recorder, "_cells", cells):
            report = await _sync(hass, book.ledger)
    finally:
        task.cancel()
    hours = (END - hour_start(logs[0].timestamp)) // H
    assert report.rows_written == 2 * hours
    chunks = -(-hours // care_log_recorder.CHUNK_HOURS)
    assert chunks >= 3
    assert len(seen) == 2 * chunks
    assert all(a < b for a, b in pairwise(seen))
