"""The care log statistics importer, against a real recorder database.

The importer reads through a real ``NanitCareLogCoordinator`` (the reader),
whose client and the importer's own ``fetch`` both answer from one mutable
synthetic truth. The reader is polled by the tests (``Harness.poll``) or by
its own interval, and time moves only with the freezer; timers are fired at
the exact new time, so a step of exactly the minimum gap fires the
importer's timers and not the reader's next poll (scheduled a fraction of
a second later).

All data is synthetic, around Friday 20 November 2026 18:10 UTC.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics as real_add,
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from aionanit.exceptions import NanitAuthError, NanitConnectionError
from aionanit.models import Baby, CareLog, CareLogFetch
from custom_components.nanit import care_log_importer, care_log_recorder
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries, hour_start
from custom_components.nanit.care_log_importer import (
    HISTORY_PIECE,
    MIN_GAP,
    CareLogStatisticsImporter,
    care_log_history_pieces,
    care_log_history_start,
)
from custom_components.nanit.care_log_ledger import CareLogBook
from custom_components.nanit.care_log_recorder import (
    CareLogSyncError,
    async_sync_care_log_statistics,
)
from custom_components.nanit.care_log_statistics import statistic_id
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import NanitCareLogCoordinator

from ..conftest import MOCK_EMAIL, mock_entry_data_v2

S = CareLogSeries
H = 3600
PIECE = int(HISTORY_PIECE.total_seconds())
NOW = datetime(2026, 11, 20, 18, 10, tzinfo=UTC)
UID = "baby_importer_test"
CODE = baby_code(UID)
BABY = Baby(uid=UID, name="Nursery", camera_uid="cam_1", birth_date=date(2026, 6, 1))
FAR_PAST = datetime(2020, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

type Bounds = tuple[int, int]


def _ts(when: datetime) -> float:
    return when.timestamp()


def _bottle(uid: str, when: datetime, ml: float, baby_uid: str = UID) -> CareLog:
    return CareLog(
        uid=uid, log_type="bottle_feed", timestamp=_ts(when), baby_uid=baby_uid, feed_amount_ml=ml
    )


def _diaper(uid: str, when: datetime, kind: str = "pee") -> CareLog:
    return CareLog(
        uid=uid, log_type="diaper_change", timestamp=_ts(when), baby_uid=UID, change_type=kind
    )


OLD = _bottle("b_old", NOW - timedelta(days=20, minutes=30), 90.0)  # a history piece
D1 = _diaper("d1", NOW - timedelta(days=2))  # the reader's first, 7 day, poll
D2 = _diaper("d2", NOW - timedelta(hours=5, minutes=20))  # the reader's 24 hours
B1 = _bottle("b1", NOW - timedelta(hours=3, minutes=40), 120.0)  # the reader's 24 hours
BASE = [OLD, D1, D2, B1]


def _hour(log: CareLog) -> int:
    return hour_start(log.timestamp)


class Truth:
    """What Nanit holds, answered to the reader and to the importer's own fetches."""

    def __init__(self, logs: list[CareLog]) -> None:
        self.logs = {log.uid: log for log in logs}
        self.own: list[Bounds] = []
        self.reader: list[Bounds] = []
        self.omit_next: set[str] = set()
        self.dropped_next = 0
        self.dropped_own = 0
        self.errors: list[BaseException] = []
        self.after_own: Callable[[int, int], None] | None = None

    def put(self, log: CareLog) -> None:
        self.logs[log.uid] = log

    def delete(self, uid: str) -> None:
        del self.logs[uid]

    def answer(self, start: int, end: int) -> CareLogFetch:
        logs = tuple(
            log
            for log in self.logs.values()
            if start <= log.timestamp <= end and log.uid not in self.omit_next
        )
        dropped, self.dropped_next, self.omit_next = self.dropped_next, 0, set()
        return CareLogFetch(logs=logs, start=start, end=end, dropped=dropped)

    async def fetch(self, start: int, end: int) -> CareLogFetch:
        self.own.append((start, end))
        if self.errors:
            raise self.errors.pop(0)
        result = self.answer(start, end)
        if self.dropped_own:
            result = CareLogFetch(logs=result.logs, start=start, end=end, dropped=self.dropped_own)
        if self.after_own is not None:
            self.after_own(start, end)
        return result

    async def reader_fetch(self, _baby_uid: str, start: int, end: int) -> CareLogFetch:
        self.reader.append((start, end))
        return self.answer(start, end)


class Harness:
    """One config entry's reader and importer, and ways to move time."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        truth: Truth,
        entry: MockConfigEntry,
        baby: Baby = BABY,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.truth = truth
        self.entry = entry
        hub = MagicMock()
        hub.client.async_get_calendar_result = AsyncMock(side_effect=truth.reader_fetch)
        self.coordinator = NanitCareLogCoordinator(hass, entry, hub, baby)
        self.importer = CareLogStatisticsImporter(
            hass, entry, baby=baby, coordinator=self.coordinator, fetch=truth.fetch
        )

    def start(self) -> None:
        self.importer.async_start()

    async def settle(self) -> None:
        """Wait for the reader's polls in flight and for the worker to have nothing to do."""
        for _ in range(100):
            await self.hass.async_block_till_done()
            busy = [
                task
                for task in self.entry._background_tasks
                if task is not self.importer._task and not task.done()
            ]
            if busy:
                await asyncio.wait(busy)
                continue
            if self.importer._idle.is_set():
                return
            await self.importer._idle.wait()

    async def poll(self) -> None:
        """A reader poll, now."""
        await self.coordinator.async_refresh()
        await self.settle()

    async def advance(self, delta: timedelta) -> None:
        """Move time on and fire every timer due by then."""
        self.freezer.tick(delta)
        async_fire_time_changed_exact(self.hass, dt_util.utcnow())
        await self.settle()

    async def tick_and_poll(self, delta: timedelta) -> None:
        await self.advance(delta)
        await self.poll()

    def diag(self) -> dict[str, object]:
        return self.importer.diagnostics()

    async def stop(self) -> None:
        await self.importer.async_stop()
        assert self.importer._task is not None
        assert self.importer._task.done()
        assert self.importer._unsub_timer is None
        assert not self.coordinator._listeners


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def make(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> AsyncIterator[Callable[..., Harness]]:
    """Build harnesses at NOW (UTC); every one is stopped at the end of the test."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(NOW)
    made: list[Harness] = []

    def build(truth: Truth, *, baby: Baby = BABY, on: MockConfigEntry | None = None) -> Harness:
        harness = Harness(hass, freezer, truth, on or entry, baby)
        made.append(harness)
        return harness

    yield build
    for harness in made:
        await harness.importer.async_stop()


@pytest.fixture(autouse=True)
def quick() -> Iterator[None]:
    """No real pauses: time stands still under the freezer."""
    with (
        patch.object(care_log_recorder, "VERIFY_RETRY_DELAY", 0.0),
        patch.object(care_log_importer, "REQUEST_PAUSE", 0.0),
    ):
        yield


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


def _rows_sent(sent: Sent) -> int:
    return sum(len(rows) for _sid, rows in sent)


async def _stored(hass: HomeAssistant, series: CareLogSeries) -> dict[int, float]:
    """Stored state by hour."""
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
    return {int(row["start"]): row["state"] for row in found.get(sid, [])}


async def _rows(hass: HomeAssistant, series: CareLogSeries) -> dict[int, tuple[float, float]]:
    """Stored (state, sum) by hour."""
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


def _end_hour() -> int:
    return hour_start(dt_util.utcnow().timestamp())


def _first_poll_start(at: datetime = NOW) -> int:
    return int((at - timedelta(days=7)).timestamp())


def _expected_pieces(reader_start: int, baby: Baby = BABY) -> tuple[Bounds, ...]:
    return care_log_history_pieces(care_log_history_start(baby, NOW), reader_start - 1)


async def _imported(make: Callable[..., Harness], truth: Truth) -> Harness:
    harness = make(truth)
    harness.start()
    await harness.poll()
    assert harness.diag()["state"] == "running"
    truth.own.clear()
    return harness


# ---------------------------------------------------------------------------
# Starting up
# ---------------------------------------------------------------------------


async def test_a_first_import_reads_the_history_in_grid_pieces_and_writes_rows(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    harness = make(truth)
    harness.start()
    assert harness.diag()["state"] == "waiting_for_reader"

    await harness.poll()

    pieces = truth.own
    assert pieces == list(_expected_pieces(_first_poll_start()))
    assert len(pieces) >= 3
    assert pieces[0][0] == care_log_history_start(BABY, NOW)
    assert pieces[-1][1] == _first_poll_start() - 1
    for previous, current in pairwise(pieces):
        assert current[0] == previous[1] + 1
        assert current[0] % PIECE == 0

    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert min(volume) == _hour(OLD)
    assert max(volume) == _end_hour() - H
    assert volume[_hour(OLD)] == 90.0
    assert volume[_hour(B1)] == 120.0
    assert volume[_hour(OLD) + H] == 0.0
    pee = await _stored(hass, S.DIAPER_PEE)
    assert min(pee) == _hour(D1)
    assert pee[_hour(D2)] == 1.0
    diag = harness.diag()
    assert diag["entries"] == 4
    assert diag["established"] is True
    assert diag["passes_completed"] == 1
    assert diag["rows_written"] == _rows_sent(sent) > 0
    assert diag["minutes_since_last_sync"] == 0
    assert diag["minutes_since_last_history_pass"] == 0


async def test_a_second_start_with_the_same_truth_adopts_at_once_and_writes_nothing(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    written = _rows_sent(sent)

    second = make(truth)
    second.start()
    await second.poll()

    assert truth.own == list(_expected_pieces(_first_poll_start()))
    assert second.diag()["state"] == "running"
    assert second.diag()["entries"] == 4
    assert _rows_sent(sent) == written
    assert second.diag()["rows_written"] == 0


async def test_the_reader_answer_from_before_the_start_is_used(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = make(truth)
    await harness.coordinator.async_refresh()

    harness.start()
    await harness.settle()

    assert harness.diag()["state"] == "running"
    assert len(truth.reader) == 1


async def test_without_a_ready_recorder_nothing_is_fetched_or_written_until_it_is(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    harness = make(truth)
    with patch.object(care_log_importer, "care_log_recorder_ready", return_value=False):
        harness.start()
        await harness.poll()
        await harness.tick_and_poll(timedelta(minutes=5))
        assert harness.diag()["state"] == "waiting_for_recorder"

    assert truth.own == []
    assert sent == []

    await harness.tick_and_poll(timedelta(minutes=5))

    assert harness.diag()["state"] == "running"
    assert truth.own
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0


async def test_a_restart_after_a_deletion_waits_for_a_second_agreeing_pass(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(OLD.uid)  # while stopped
    written = _rows_sent(sent)

    second = make(truth)
    second.start()
    await second.poll()

    pieces = list(_expected_pieces(_first_poll_start()))
    assert truth.own == pieces
    assert second.diag()["state"] == "waiting_for_second_pass"
    assert _rows_sent(sent) == written

    # Not before the gap.
    await second.advance(MIN_GAP - timedelta(seconds=1))
    assert truth.own == pieces

    await second.advance(timedelta(seconds=1))

    reader_bounds = truth.reader[-1]
    assert truth.own == [*pieces, *pieces, reader_bounds]
    assert second.diag()["state"] == "running"
    assert second.diag()["passes_completed"] == 2
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(OLD)] == 0.0
    assert volume[_hour(B1)] == 120.0


async def test_a_restart_gives_up_after_three_disagreeing_passes_and_tries_again_next_day(
    make: Callable[..., Harness],
    hass: HomeAssistant,
    sent: Sent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    edited = _bottle("b_edit", NOW - timedelta(days=40), 50.0)
    truth = Truth([*BASE, edited])
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(OLD.uid)
    written = _rows_sent(sent)

    # Each answer for the piece holding the edited entry is followed by
    # another edit, so no two passes agree.
    def keep_editing(start: int, end: int) -> None:
        if start <= edited.timestamp <= end:
            current = truth.logs[edited.uid]
            assert current.feed_amount_ml is not None
            truth.put(_bottle(edited.uid, NOW - timedelta(days=40), current.feed_amount_ml + 10))

    truth.after_own = keep_editing
    second = make(truth)
    second.start()
    await second.poll()
    pieces = len(truth.own)
    await second.advance(MIN_GAP)
    assert second.diag()["state"] == "waiting_for_second_pass"
    with caplog.at_level(logging.WARNING):
        await second.advance(MIN_GAP)

    assert second.diag()["state"] == "gave_up"
    assert second.diag()["passes_completed"] == 3
    assert second.diag()["give_ups"] == 1
    assert len(truth.own) == 3 * pieces + 2
    assert _rows_sent(sent) == written
    warnings = [r for r in caplog.records if r.name == care_log_importer.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING

    # Nothing more until the next daily pass is due.
    asked = len(truth.own)
    await second.tick_and_poll(timedelta(hours=23))
    assert len(truth.own) == asked
    assert _rows_sent(sent) == written

    truth.after_own = None
    await second.advance(timedelta(hours=1))
    assert len(truth.own) > asked
    assert second.diag()["state"] == "waiting_for_second_pass"
    await second.advance(MIN_GAP)

    assert second.diag()["state"] == "running"
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(OLD)] == 0.0
    assert volume[_hour(edited)] == 80.0


async def test_a_connection_error_abandons_the_pass_and_retries_with_a_growing_backoff(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    truth.errors = [NanitConnectionError("HTTP 503") for _ in range(4)]
    harness = make(truth)
    harness.start()
    await harness.poll()
    assert truth.own == [_expected_pieces(_first_poll_start())[0]]
    assert harness.diag()["state"] == "waiting_to_retry"

    attempts = [1]
    for delay in (timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1)):
        await harness.advance(delay - timedelta(seconds=1))
        assert len(truth.own) == attempts[-1]
        await harness.advance(timedelta(seconds=1))
        attempts.append(len(truth.own))
    assert attempts == [1, 2, 3, 4]
    assert sent == []

    # Then hourly; this time it gets through.
    await harness.advance(timedelta(minutes=59))
    assert len(truth.own) == 4
    await harness.advance(timedelta(minutes=1))
    assert harness.diag()["state"] == "running"
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0


async def test_an_auth_error_starts_reauth_and_stops_the_importer(
    make: Callable[..., Harness], hass: HomeAssistant, entry: MockConfigEntry, sent: Sent
) -> None:
    truth = Truth(BASE)
    truth.errors = [NanitAuthError("Access token invalid")]
    harness = make(truth)
    harness.start()
    await harness.poll()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert harness.importer._task is not None
    assert harness.importer._task.done()
    assert not harness.coordinator._listeners
    assert harness.diag()["state"] == "stopped"
    assert sent == []

    await harness.advance(timedelta(hours=2))
    assert len(truth.own) == 1
    await harness.stop()


async def test_stopping_during_a_long_first_import_returns_promptly_and_a_new_importer_finishes(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    entered = asyncio.Event()
    stop_checks: list[Callable[[], bool]] = []

    async def endless_sync(*_args: Any, should_stop: Callable[[], bool], **_kwargs: Any) -> Any:
        stop_checks.append(should_stop)
        entered.set()
        await asyncio.Event().wait()

    harness = make(truth)
    with patch.object(care_log_importer, "async_sync_care_log_statistics", endless_sync):
        harness.start()
        await harness.coordinator.async_refresh()
        await entered.wait()
        assert stop_checks[0]() is False

        await harness.stop()

    assert stop_checks[0]() is True
    assert harness.diag()["state"] == "stopped"

    again = make(truth)
    again.start()
    await again.poll()
    assert again.diag()["state"] == "running"
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0


# ---------------------------------------------------------------------------
# History coverage
# ---------------------------------------------------------------------------


async def test_a_row_before_a_later_history_start_survives_a_restart_whose_passes_agree(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    early = _bottle("b_early", datetime(2025, 7, 15, 9, 30, tzinfo=UTC), 80.0)
    truth = Truth([*BASE, early])
    first = await _imported(make, truth)
    await first.stop()
    volume = await _rows(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(early)] == (80.0, 80.0)
    # The baby record now gives a later birth date: the history starts after
    # the early bottle. A deletion makes the restart need two passes.
    later = Baby(uid=UID, name="Nursery", camera_uid="cam_1", birth_date=date(2026, 9, 1))
    assert care_log_history_start(later, NOW) > early.timestamp
    truth.delete(D2.uid)

    second = make(truth, baby=later)
    second.start()
    await second.poll()
    assert second.diag()["state"] == "waiting_for_second_pass"
    await second.advance(MIN_GAP)

    assert second.diag()["state"] == "running"
    assert second.diag()["passes_completed"] == 2
    assert await _rows(hass, S.BOTTLE_VOLUME) == volume
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0


async def test_the_fallback_history_start_moving_on_by_a_piece_erases_nothing(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    undated = Baby(uid=UID, name="Nursery", camera_uid="cam_1")
    start = care_log_history_start(undated, NOW)
    edge = _bottle("b_edge", datetime.fromtimestamp(start, UTC) + timedelta(days=2), 70.0)
    truth = Truth([*BASE, edge])
    first = make(truth, baby=undated)
    first.start()
    await first.poll()
    assert first.diag()["state"] == "running"
    await first.stop()
    before = await _rows(hass, S.BOTTLE_VOLUME)
    assert before[_hour(edge)] == (70.0, 70.0)

    freezer.tick(HISTORY_PIECE)
    assert care_log_history_start(undated, dt_util.utcnow()) == start + PIECE
    second = make(truth, baby=undated)
    second.start()
    await second.poll()
    await second.advance(MIN_GAP)

    assert second.diag()["state"] == "running"
    after = await _rows(hass, S.BOTTLE_VOLUME)
    assert {hour: after[hour] for hour in before} == before
    assert after[_end_hour() - H] == (0.0, 280.0)
    assert max(after) == _end_hour() - H


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def test_a_new_entry_raises_its_hour_once_finished_and_an_empty_hour_gets_zero(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    late = _bottle("b_late", NOW + timedelta(minutes=5), 60.0)  # 18:15
    truth.put(late)

    await harness.tick_and_poll(timedelta(minutes=10))  # 18:20

    assert _hour(late) not in await _stored(hass, S.BOTTLE_VOLUME)
    assert harness.diag()["entries"] == 5

    await harness.tick_and_poll(timedelta(minutes=45))  # 19:05

    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(late)] == 60.0
    assert _hour(late) + H not in volume

    await harness.tick_and_poll(timedelta(hours=1))  # 20:05

    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(late) + H] == 0.0
    assert truth.own == []


async def test_a_deletion_is_lowered_only_after_one_confirm_fetch_a_gap_later(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)

    await harness.tick_and_poll(timedelta(minutes=1))
    read = truth.reader[-1]

    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
    assert harness.diag()["pending_records"] == 1
    assert truth.own == []

    await harness.advance(MIN_GAP - timedelta(seconds=1))
    assert truth.own == []
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    await harness.advance(timedelta(seconds=1))

    assert truth.own[0] == read
    assert harness.diag()["confirm_fetches"] == 1
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0
    assert harness.diag()["pending_records"] == 0
    assert harness.diag()["entries"] == 3


async def test_a_one_off_omission_is_cancelled_by_the_confirm_fetch(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.omit_next = {B1.uid}

    await harness.tick_and_poll(timedelta(minutes=1))
    read = truth.reader[-1]
    assert harness.diag()["pending_records"] == 1

    await harness.advance(MIN_GAP)

    assert truth.own == [read]
    assert harness.diag()["pending_records"] == 0
    assert harness.diag()["confirm_fetches"] == 1
    for _ in range(3):
        await harness.tick_and_poll(MIN_GAP)
    assert truth.own == [read]
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0


async def test_a_read_with_a_dropped_row_lowers_nothing(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)
    truth.dropped_next = 1

    await harness.tick_and_poll(timedelta(minutes=1))
    await harness.advance(MIN_GAP)

    # The confirm fetch does not agree with a read that dropped a row.
    assert harness.diag()["confirm_fetches"] == 1
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    await harness.advance(MIN_GAP)
    assert harness.diag()["confirm_fetches"] == 2
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0


async def test_after_the_book_gives_up_no_confirm_fetch_for_30_minutes(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)
    truth.dropped_own = 1  # no re-read can confirm

    await harness.tick_and_poll(timedelta(minutes=1))  # 18:11
    for _ in range(3):
        await harness.advance(MIN_GAP)  # 18:16, 18:21, 18:26
    assert harness.diag()["confirm_fetches"] == 3
    assert harness.diag()["give_ups"] == 1
    assert harness.diag()["pending_records"] == 0

    # The next reader poll finds the removal again; its re-read waits.
    truth.dropped_own = 0
    await harness.advance(timedelta(minutes=1))  # 18:27, the reader's own poll
    assert harness.diag()["pending_records"] == 1
    await harness.advance(timedelta(minutes=29) - timedelta(seconds=1))
    assert harness.diag()["confirm_fetches"] == 3
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    await harness.advance(timedelta(seconds=1))  # 18:56

    assert harness.diag()["confirm_fetches"] == 4
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0


async def test_a_confirm_fetch_that_cannot_reach_nanit_is_tried_again_later(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)
    await harness.tick_and_poll(timedelta(minutes=1))
    read = truth.reader[-1]
    truth.errors = [NanitConnectionError("HTTP 502")]

    await harness.advance(MIN_GAP)
    assert truth.own == [read]
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    await harness.advance(timedelta(minutes=5) - timedelta(seconds=1))
    assert truth.own == [read]
    await harness.advance(timedelta(seconds=1))

    # The lowering it confirmed then brings the history pass forward.
    assert truth.own == [read, read, *_expected_pieces(truth.reader[-1][0])]
    assert harness.diag()["confirm_fetches"] == 2
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0


async def test_a_confirmed_lowering_in_the_reader_window_brings_the_history_pass_forward_once_an_hour(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    # Moved out of the reader's 24 hours to an older date.
    moved = _bottle(B1.uid, NOW - timedelta(days=10, minutes=20), 120.0)
    truth.put(moved)

    await harness.tick_and_poll(timedelta(minutes=1))
    read = truth.reader[-1]
    await harness.advance(MIN_GAP)

    pieces = list(_expected_pieces(read[0]))
    assert truth.own == [read, *pieces]
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(B1)] == 0.0
    assert volume[_hour(moved)] == 120.0
    assert harness.diag()["passes_completed"] == 2

    # A second one within the hour waits for the hour to pass.
    truth.delete(D2.uid)
    truth.own.clear()
    await harness.tick_and_poll(timedelta(minutes=10))
    second_read = truth.reader[-1]
    await harness.advance(MIN_GAP)
    assert truth.own == [second_read]
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0

    # The early pass started at 18:16; the second lowering was confirmed at 18:31.
    await harness.advance(timedelta(minutes=45) - timedelta(seconds=1))
    assert truth.own == [second_read]
    await harness.advance(timedelta(seconds=1))
    assert truth.own[1:] == list(_expected_pieces(truth.reader[-1][0]))
    assert harness.diag()["passes_completed"] == 3


async def test_the_daily_pass_rereads_the_same_grid_and_writes_nothing_when_unchanged(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    first_pieces = list(_expected_pieces(_first_poll_start()))
    harness = await _imported(make, truth)

    await harness.advance(timedelta(hours=24) - timedelta(minutes=1))
    assert truth.own == []
    await harness.poll()
    written = _rows_sent(sent)

    await harness.advance(timedelta(minutes=1))

    daily = truth.own
    assert daily == list(_expected_pieces(truth.reader[-1][0]))
    assert daily[: len(first_pieces) - 1] == first_pieces[:-1]
    assert daily[-1][1] == truth.reader[-1][0] - 1
    assert _rows_sent(sent) == written
    assert harness.diag()["rows_written_last_sync"] == 0
    assert harness.diag()["passes_completed"] == 2
    assert harness.diag()["minutes_since_last_history_pass"] == 0

    # The next one a day after that.
    truth.own.clear()
    await harness.advance(timedelta(hours=24) - timedelta(seconds=1))
    await harness.poll()
    assert truth.own == []
    await harness.advance(timedelta(seconds=1))
    assert truth.own


async def test_a_daily_pass_finds_an_entry_logged_late_for_an_old_date(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    backdated = _bottle("b_back", NOW - timedelta(days=30), 70.0)
    truth.put(backdated)

    await harness.tick_and_poll(timedelta(hours=1))
    assert _hour(backdated) not in await _stored(hass, S.BOTTLE_VOLUME)

    await harness.advance(timedelta(hours=23))

    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(backdated)] == 70.0


async def test_a_sync_error_warns_once_and_the_next_full_sync_follows_15_minutes_later(
    make: Callable[..., Harness], hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    calls: list[Any] = []

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["changed_from"])
        if len(calls) <= 2:
            raise CareLogSyncError("A batch of care log statistics could not be verified")
        return await async_sync_care_log_statistics(*args, **kwargs)

    late = _bottle("b_late", NOW - timedelta(hours=1, minutes=30), 30.0)
    truth.put(late)
    with (
        patch.object(care_log_importer, "async_sync_care_log_statistics", flaky),
        caplog.at_level(logging.WARNING),
    ):
        await harness.tick_and_poll(timedelta(minutes=5))
        assert calls == [{S.BOTTLE_FEEDS: _hour(late), S.BOTTLE_VOLUME: _hour(late)}]
        assert harness.diag()["sync_failures"] == 1

        # No attempt before 15 minutes have passed, whatever the reader brings.
        await harness.tick_and_poll(timedelta(minutes=5))
        await harness.tick_and_poll(timedelta(minutes=5))
        assert len(calls) == 1
        assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 0.0

        await harness.advance(timedelta(minutes=5))
        assert calls[1] is None  # full, and it fails again: still one warning
        assert harness.diag()["sync_failures"] == 2

        await harness.advance(timedelta(minutes=15))

    assert calls[2] is None
    assert len(calls) == 3
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 30.0
    warnings = [r for r in caplog.records if r.name == care_log_importer.__name__]
    assert len(warnings) == 1
    assert harness.diag()["sync_failures"] == 2


async def test_an_unexpected_error_is_logged_and_the_worker_carries_on(
    make: Callable[..., Harness], hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    real_process = CareLogBook.process
    failures = [RuntimeError("boom")]

    def process(book: CareLogBook, read: Any) -> Any:
        if failures:
            raise failures.pop()
        return real_process(book, read)

    late = _bottle("b_late", NOW - timedelta(hours=1, minutes=30), 30.0)
    truth.put(late)
    with patch.object(CareLogBook, "process", process):
        await harness.tick_and_poll(timedelta(minutes=5))
        assert harness.diag()["entries"] == 4
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "RuntimeError" in errors[0].getMessage()
        assert not harness.importer._task.done()

        await harness.tick_and_poll(timedelta(minutes=5))

    assert harness.diag()["entries"] == 5
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 30.0


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


async def test_no_entry_id_baby_id_name_or_code_in_logs_or_diagnostics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(NOW)
    entry_id = "ENTRYMARKER01"
    uid = "BABYMARKERUID"
    name = "NAMEMARKER"
    code = baby_code(uid)
    markers = (entry_id, uid, name, code)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    baby = Baby(uid=uid, name=name, camera_uid="cam_1", birth_date=date(2026, 6, 1))
    logs = [
        _bottle("b_old", NOW - timedelta(days=20), 90.0, baby_uid=uid),
        _bottle("b1", NOW - timedelta(hours=3), 120.0, baby_uid=uid),
    ]
    truth = Truth(logs)
    truth.errors = [
        NanitConnectionError(f"HTTP 503 for {uid}"),
        RuntimeError(f"unexpected {uid} {name} {entry_id}"),
    ]
    harness = Harness(hass, freezer, truth, entry, baby)
    caplog.set_level(logging.DEBUG)

    harness.start()
    await harness.poll()
    await harness.advance(timedelta(minutes=5))  # the unexpected error
    await harness.advance(timedelta(minutes=15))  # through
    assert harness.diag()["state"] == "running"

    calls = 0

    async def failing_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CareLogSyncError(f"failed for {code}")
        return await async_sync_care_log_statistics(*args, **kwargs)

    with patch.object(care_log_importer, "async_sync_care_log_statistics", failing_once):
        await harness.tick_and_poll(timedelta(minutes=5))
        await harness.advance(timedelta(minutes=15))
    truth.delete("b1")
    truth.errors = [NanitAuthError(f"token for {uid} refused")]
    await harness.tick_and_poll(timedelta(minutes=1))
    await harness.advance(MIN_GAP)
    assert harness.diag()["state"] == "stopped"
    diagnostics = harness.diag()
    await harness.stop()

    ours = [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.nanit")
        # The reader's own lines name it by the code, on purpose.
        and record.name != "custom_components.nanit.coordinator"
    ]
    assert any(record.levelno == logging.ERROR for record in ours)
    assert any(record.levelno == logging.WARNING for record in ours)
    text = "\n".join(
        f"{record.getMessage()} {record.exc_text or ''} {record.exc_info or ''}" for record in ours
    )
    for marker in markers:
        assert marker not in text
        assert marker not in repr(diagnostics)
        assert marker not in repr(harness.importer._book)
    assert set(diagnostics) == {
        "state",
        "entries",
        "pending_records",
        "waiting_entries",
        "established",
        "passes_completed",
        "confirm_fetches",
        "give_ups",
        "sync_failures",
        "rows_written",
        "rows_written_last_sync",
        "minutes_since_last_sync",
        "minutes_since_last_history_pass",
    }
    for value in diagnostics.values():
        assert value is None or isinstance(value, int | bool | str)
    assert diagnostics["state"] == "stopped"
    assert calls == 2
