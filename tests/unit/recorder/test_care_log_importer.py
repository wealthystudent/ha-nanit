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
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
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
from homeassistant.components.recorder.tasks import (
    ImportStatisticsTask,
    RecorderTask,
    SynchronizeTask,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
    async_test_home_assistant,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.typing import RecorderInstanceContextManager

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitRateLimited,
    NanitResponseTooLarge,
)
from aionanit.models import Baby, CareLog, CareLogFetch
from custom_components.nanit import care_log_importer, care_log_recorder
from custom_components.nanit import coordinator as coordinator_module
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
from custom_components.nanit.care_log_writers import _importer_factory
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
        # Left out of every own fetch while set.
        self.omit_own: set[str] = set()
        # Awaited inside an own fetch, before it is answered.
        self.during_own: Callable[[int, int], Awaitable[None]] | None = None
        # Added to every answer, whatever the range asked for.
        self.extra: list[CareLog] = []

    def put(self, log: CareLog) -> None:
        self.logs[log.uid] = log

    def delete(self, uid: str) -> None:
        del self.logs[uid]

    def answer(self, start: int, end: int) -> CareLogFetch:
        logs = tuple(
            log
            for log in self.logs.values()
            if start <= log.timestamp <= end and log.uid not in self.omit_next
        ) + tuple(self.extra)
        dropped, self.dropped_next, self.omit_next = self.dropped_next, 0, set()
        return CareLogFetch(logs=logs, start=start, end=end, dropped=dropped)

    async def fetch(self, start: int, end: int) -> CareLogFetch:
        self.own.append((start, end))
        if self.errors:
            raise self.errors.pop(0)
        if self.during_own is not None:
            await self.during_own(start, end)
        result = self.answer(start, end)
        if self.omit_own:
            logs = tuple(log for log in result.logs if log.uid not in self.omit_own)
            result = CareLogFetch(logs=logs, start=start, end=end, dropped=result.dropped)
        if self.dropped_own:
            result = CareLogFetch(logs=result.logs, start=start, end=end, dropped=self.dropped_own)
        if self.after_own is not None:
            self.after_own(start, end)
        return result

    async def reader_fetch(self, _baby_uid: str, start: int, end: int) -> CareLogFetch:
        self.reader.append((start, end))
        if self.reader_down:
            raise NanitConnectionError("HTTP 503")
        return self.answer(start, end)

    # Every reader poll fails while set.
    reader_down = False


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

    async def advance_polling(self, delta: timedelta) -> None:
        """Move time on in steps of at most 12 hours, the reader polling at each.

        A reader that polls less often than its 24 hour look-back leaves an
        unread gap: the history pass is then brought forward.
        """
        step = timedelta(hours=12)
        while delta > step:
            await self.advance(step)
            delta -= step
        await self.advance(delta)

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
    return care_log_history_pieces(care_log_history_start(baby, NOW), reader_start)


@contextmanager
def _counting_steps(harness: Harness) -> Iterator[list[int]]:
    """Record every step the worker takes while inside."""
    steps: list[int] = []
    real_step = harness.importer._async_step

    async def step() -> None:
        steps.append(1)
        await real_step()

    with patch.object(harness.importer, "_async_step", step):
        yield steps


async def _imported(make: Callable[..., Harness], truth: Truth) -> Harness:
    harness = make(truth)
    harness.start()
    await harness.poll()
    assert harness.diag()["state"] == "running"
    truth.own.clear()
    return harness


@dataclass
class _HoldRecorder(RecorderTask):
    """Holds the recorder's thread until ``gate`` is set."""

    entered: threading.Event
    gate: threading.Event

    def run(self, instance: Recorder) -> None:
        self.entered.set()
        self.gate.wait(30)


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
    assert pieces[-1][1] == _first_poll_start()
    for previous, current in pairwise(pieces):
        assert current[0] == previous[1]
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
    assert len(harness.importer._book.ledger) == 4
    assert diag["established"] is True
    assert diag["passes_completed"] == 1
    assert _rows_sent(sent) > 0
    assert diag["waiting_for_confirmation"] is False
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
    assert len(second.importer._book.ledger) == 4
    assert _rows_sent(sent) == written


async def test_entries_dated_within_the_second_before_a_boundary_are_read(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    pieces = _expected_pieces(_first_poll_start())
    boundary = pieces[1][0]  # between two pieces, on the grid
    assert boundary % PIECE == 0
    inner = _bottle("b_inner", datetime.fromtimestamp(boundary - 0.5, UTC), 40.0)
    edge = _bottle("b_edge", datetime.fromtimestamp(_first_poll_start() - 0.5, UTC), 20.0)
    truth = Truth([*BASE, inner, edge])

    await _imported(make, truth)

    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(inner)] == 40.0
    assert volume[_hour(edge)] == 20.0


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


async def test_the_recorder_disable_action_pauses_the_import_until_enabled_again(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    harness = make(truth)
    await hass.services.async_call("recorder", "disable", blocking=True)
    harness.start()
    await harness.poll()
    await harness.tick_and_poll(timedelta(minutes=5))
    assert harness.diag()["state"] == "waiting_for_recorder"
    assert truth.own == []
    assert sent == []

    await hass.services.async_call("recorder", "enable", blocking=True)
    await harness.tick_and_poll(timedelta(minutes=5))  # 18:20
    assert harness.diag()["state"] == "running"
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0

    # Disabled while running: reads still reach the book, nothing is asked
    # of Nanit or written.
    await hass.services.async_call("recorder", "disable", blocking=True)
    truth.own.clear()
    written = len(sent)
    late = _bottle("b_late", NOW - timedelta(hours=1, minutes=30), 30.0)
    truth.put(late)
    truth.delete(D2.uid)
    await harness.tick_and_poll(timedelta(minutes=5))  # 18:25
    await harness.advance(MIN_GAP)  # a confirm fetch would be due
    await harness.tick_and_poll(timedelta(hours=1))  # an hour finished
    assert truth.own == []
    assert len(sent) == written
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 0.0

    await hass.services.async_call("recorder", "enable", blocking=True)
    await harness.tick_and_poll(timedelta(minutes=5))
    await harness.advance(MIN_GAP)

    assert truth.own
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 30.0
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0
    assert max(await _stored(hass, S.BOTTLE_VOLUME)) == _end_hour() - H


def _disable_after_own_requests(hass: HomeAssistant, truth: Truth, count: int) -> None:
    """Pause recording with the real ``recorder.disable`` action inside own request ``count``."""

    async def disable(_start: int, _end: int) -> None:
        if len(truth.own) == count:
            truth.during_own = None
            await hass.services.async_call("recorder", "disable", blocking=True)

    truth.during_own = disable


async def test_the_recorder_disable_action_during_a_first_pass_stops_its_requests(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    truth = Truth(BASE)
    pieces = list(_expected_pieces(_first_poll_start()))
    assert len(pieces) >= 3
    harness = make(truth)
    _disable_after_own_requests(hass, truth, 2)
    harness.start()
    await harness.poll()

    # The request in flight is answered; no other is made.
    assert truth.own == pieces[:2]
    assert harness.diag()["state"] == "waiting_for_recorder"
    await harness.tick_and_poll(timedelta(minutes=5))
    assert truth.own == pieces[:2]
    assert harness.importer._failures == 0  # not a connection failure
    assert sent == []

    await hass.services.async_call("recorder", "enable", blocking=True)
    truth.own.clear()
    await harness.tick_and_poll(timedelta(minutes=5))

    assert truth.own == list(_expected_pieces(truth.reader[-1][0]))
    assert harness.diag()["state"] == "running"
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0


async def test_the_recorder_disable_action_during_a_daily_pass_stops_its_requests(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    backdated = _bottle("b_back", NOW - timedelta(days=30), 70.0)
    truth.put(backdated)
    await harness.advance_polling(timedelta(hours=24) - timedelta(minutes=1))
    await harness.poll()
    pieces = list(_expected_pieces(truth.reader[-1][0]))
    _disable_after_own_requests(hass, truth, 2)

    await harness.advance(timedelta(minutes=1))  # the daily pass is due

    assert truth.own == pieces[:2]
    await harness.tick_and_poll(timedelta(minutes=5))
    assert truth.own == pieces[:2]
    assert harness.importer._failures == 0
    assert harness.diag()["passes_completed"] == 1

    await hass.services.async_call("recorder", "enable", blocking=True)
    truth.own.clear()
    await harness.tick_and_poll(timedelta(minutes=5))

    assert truth.own == list(_expected_pieces(truth.reader[-1][0]))
    assert harness.diag()["passes_completed"] == 2
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(backdated)] == 70.0


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


async def test_a_start_up_retry_due_without_a_recorder_waits_for_the_next_reader_update(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    truth.errors = [NanitConnectionError("HTTP 503")]
    harness = make(truth)
    harness.start()
    await harness.poll()
    assert harness.diag()["state"] == "waiting_to_retry"

    with (
        _counting_steps(harness) as steps,
        patch.object(care_log_importer, "care_log_recorder_ready", return_value=False),
    ):
        await harness.advance(timedelta(minutes=5))  # the retry is due
        assert steps == [1]
        assert harness.importer._unsub_timer is None
        polls = len(truth.reader)
        for _ in range(3):
            await harness.advance(timedelta(seconds=1))
        # One step per reader update (its own poll falls in here), no more.
        assert len(steps) == 1 + len(truth.reader) - polls
        assert len(truth.own) == 1

    await harness.tick_and_poll(timedelta(seconds=1))
    assert harness.diag()["state"] == "running"


async def test_a_sync_retry_due_without_a_recorder_waits_for_the_next_reader_update(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    calls: list[int] = []

    async def failing(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        raise CareLogSyncError("A batch of care log statistics could not be verified")

    late = _bottle("b_late", NOW - timedelta(hours=1, minutes=30), 30.0)
    truth.put(late)
    with patch.object(care_log_importer, "async_sync_care_log_statistics", failing):
        await harness.tick_and_poll(timedelta(minutes=1))
    assert calls == [1]

    with (
        _counting_steps(harness) as steps,
        patch.object(care_log_importer, "care_log_recorder_ready", return_value=False),
    ):
        await harness.advance(timedelta(minutes=15))  # the full sync is due
        assert steps == [1]
        assert harness.importer._unsub_timer is None
        polls = len(truth.reader)
        for _ in range(3):
            await harness.advance(timedelta(seconds=1))
        assert len(steps) == 1 + len(truth.reader) - polls

    await harness.tick_and_poll(timedelta(seconds=1))
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 30.0


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


async def test_stopping_during_a_long_first_import_settles_the_batch_sent_and_sends_no_other(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    """The recorder is busy when the first batch is sent; the importer is stopped meanwhile."""
    truth = Truth(BASE)
    entered = threading.Event()
    gate = threading.Event()
    events: list[str] = []
    spy = care_log_recorder.async_add_external_statistics
    real_verify = care_log_recorder._Sync._verify

    def busy_first(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        if not entered.is_set():
            # The batch queues behind a task that holds the recorder.
            get_instance(hass).queue_task(_HoldRecorder(entered, gate))
        spy(hass, metadata, rows)

    async def verify(self: Any, sid: str, batch: Any) -> None:
        await real_verify(self, sid, batch)
        events.append("verified")

    harness = make(truth)
    with (
        patch.object(care_log_recorder, "BATCH_ROWS", 50),
        patch.object(care_log_recorder, "async_add_external_statistics", busy_first),
        patch.object(care_log_recorder._Sync, "_verify", verify),
    ):
        harness.start()
        await harness.coordinator.async_refresh()
        assert await hass.async_add_executor_job(entered.wait, 30)
        stop = hass.async_create_task(harness.importer.async_stop())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not stop.done()

        events.append("released")
        gate.set()
        await stop
        events.append("stopped")

    assert events == ["released", "verified", "stopped"]
    assert len(sent) == 1
    assert harness.diag()["state"] == "stopped"
    first_sid, first_rows = sent[0]
    assert first_sid == statistic_id(CODE, S.DIAPER_CHANGES)
    assert len(first_rows) == (_end_hour() - _hour(D1)) // H
    stored = await _rows(hass, S.DIAPER_CHANGES)
    assert [(hour, *stored[hour]) for hour, _s, _t in first_rows] == first_rows

    # The next importer compares against the stored batch: it sends the rest only.
    again = make(truth)
    again.start()
    await again.poll()
    assert again.diag()["state"] == "running"
    assert first_rows[0][0] not in [
        hour for sid, rows in sent[1:] if sid == first_sid for hour, _s, _t in rows
    ]
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


@pytest.fixture
def reader_fails_at_once() -> Iterator[None]:
    """A failed reader poll is not retried within the poll (the retries sleep for real)."""
    with patch.object(coordinator_module, "_POLL_RETRY_DELAYS", ()):
        yield


@pytest.mark.usefixtures("reader_fails_at_once")
async def test_no_rows_are_written_for_hours_nobody_read_until_a_read_succeeds(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    with patch.object(care_log_importer, "HISTORY_INTERVAL", timedelta(hours=2)):
        harness = await _imported(make, truth)  # 18:10, rows up to 17:00
    truth.reader_down = True
    late = _bottle("b_down", NOW + timedelta(minutes=20), 30.0)  # 18:30
    truth.put(late)

    for _ in range(3):  # 19:10, 20:10 (a history pass and its syncs), 21:10
        await harness.advance(timedelta(hours=1))
    assert truth.own  # the history pass ran
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert max(volume) == hour_start(_ts(NOW)) - H
    assert _hour(late) not in volume

    truth.reader_down = False
    await harness.poll()  # 21:10, observes up to 21:00

    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert max(volume) == _end_hour() - H
    assert volume[_hour(late)] == 30.0
    assert volume[_hour(late) + H] == 0.0
    assert volume[_hour(late) + 2 * H] == 0.0


@pytest.mark.usefixtures("reader_fails_at_once")
async def test_a_second_pass_delayed_by_retries_writes_nothing_after_the_last_observed_hour(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # the restart needs a second pass
    second = make(truth)
    second.start()
    await second.poll()  # 18:10, the first pass; the reader's bounds end at 19:10
    assert second.diag()["state"] == "waiting_for_second_pass"
    truth.reader_down = True
    truth.errors = [NanitConnectionError("HTTP 503") for _ in range(4)]

    # Tried at 18:15, 18:20, 18:35 and 19:35; through at 20:35.
    for _ in range(30):
        await second.advance(timedelta(minutes=5))
    assert second.diag()["state"] == "running"
    assert dt_util.utcnow() >= NOW + timedelta(hours=2, minutes=25)

    # The second pass re-read the reader's bounds, which end at 19:10: 18:00
    # is the last hour any read observed whole after it ended.
    for series in (S.BOTTLE_VOLUME, S.DIAPER_PEE):
        assert max(await _stored(hass, series)) == hour_start(_ts(NOW))
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0


@pytest.mark.usefixtures("reader_fails_at_once")
async def test_after_a_ten_day_reader_outage_nothing_is_written_across_the_unread_days(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)  # 20 November 18:10, rows up to 17:00
    last_row = hour_start(_ts(NOW)) - H
    truth.reader_down = True
    # Logged during the outage, for a day the reader's next read, of seven
    # days, does not reach.
    unread = _bottle("b_unread", NOW + timedelta(days=2, hours=4), 45.0)
    truth.put(unread)
    for _ in range(10):  # the daily passes go on; the reader keeps failing
        await harness.advance(timedelta(days=1))
    assert harness.diag()["passes_completed"] == 11
    assert max(await _stored(hass, S.BOTTLE_VOLUME)) == last_row

    # The reader is back, with a seven day read: a gap after the last hour
    # read. The history pass is brought forward, and fails.
    truth.reader_down = False
    truth.errors = [NanitConnectionError("HTTP 503")]
    truth.own.clear()
    await harness.poll()  # 30 November 18:10
    assert truth.reader[-1][0] == _first_poll_start(dt_util.utcnow())
    assert truth.reader[-1][0] > unread.timestamp
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert max(volume) == last_row
    assert len(truth.own) == 1  # the pass brought forward, which failed

    # Still failing: still nothing past the gap.
    truth.errors = [NanitConnectionError("HTTP 503")]
    await harness.advance(timedelta(minutes=5))  # the retry, and a reader poll
    assert len(truth.own) == 2
    assert max(await _stored(hass, S.BOTTLE_VOLUME)) == last_row

    # The next retry reads the gap: the rows follow, with the entry in it.
    await harness.advance(timedelta(minutes=15))
    volume = await _rows(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(unread)] == (45.0, 255.0)
    assert volume[_hour(unread) - H] == (0.0, 210.0)
    assert max(volume) == _end_hour() - H
    assert volume[_end_hour() - H] == (0.0, 255.0)
    assert len(volume) == (_end_hour() - _hour(OLD)) // H


def _second_piece_too_large(truth: Truth) -> int:
    """Answer the history's second piece with more than the client reads; return its start."""
    history = care_log_history_start(BABY, NOW)
    # The pieces lie on a fixed grid: the second starts at the same second
    # whatever the end of the pass.
    second = care_log_history_pieces(history, _ts(NOW))[1][0]

    async def too_large(start: int, _end: int) -> None:
        if start == second:
            raise NanitResponseTooLarge("Calendar response larger than 16777216 bytes")

    truth.during_own = too_large
    return second


def _too_large_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == care_log_importer.__name__ and "larger than" in record.getMessage()
    ]


async def test_a_history_piece_too_large_to_read_waits_for_the_next_daily_pass(
    make: Callable[..., Harness], hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The daily pass stops at the oversized piece, and nothing reads it again for a day."""
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    passes = harness.diag()["passes_completed"]
    second = _second_piece_too_large(truth)

    with caplog.at_level(logging.WARNING):
        await harness.advance_polling(timedelta(hours=24))
        assert [start for start, _end in truth.own][1:] == [second]
        assert harness.diag()["state"] == "history_too_large"
        (warning,) = _too_large_warnings(caplog)
        for private in (UID, CODE, BABY.name):
            assert private not in warning.getMessage()

        # Not before the daily interval: not the earlier piece, not the large one.
        asked = list(truth.own)
        await harness.advance_polling(timedelta(hours=23, minutes=50))
        assert truth.own == asked
        assert harness.diag()["state"] == "history_too_large"
        assert harness.diag()["passes_completed"] == passes

        # A day later, once: still too large, and no second warning.
        await harness.advance_polling(timedelta(minutes=15))
        assert len(truth.own) == 4
        assert len(_too_large_warnings(caplog)) == 1

        # Once it can be read, the next daily pass gets through.
        truth.during_own = None
        await harness.advance_polling(timedelta(hours=24))
    assert harness.diag()["state"] == "running"
    assert harness.diag()["passes_completed"] == passes + 1


async def test_a_start_up_piece_too_large_to_read_waits_a_day(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    """At start-up too: the first pass stops at the oversized piece and waits a day."""
    truth = Truth(BASE)
    second = _second_piece_too_large(truth)
    harness = make(truth)
    harness.start()
    await harness.poll()
    assert [start for start, _end in truth.own][1:] == [second]
    assert harness.diag()["state"] == "history_too_large"

    asked = list(truth.own)
    await harness.advance_polling(timedelta(hours=23, minutes=50))
    assert truth.own == asked
    assert harness.diag()["state"] == "history_too_large"


async def test_a_held_request_waits_on_the_retry_schedule_and_keeps_its_pass(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    """Nanit throttles the calendar for two hours just before the daily pass is due.

    The importer reads through the reader's hold, as the entry builds it: the
    pass asks nothing while it is held, fails like a failed read on the
    usual retry schedule, and once the hold is over reads the whole range.
    """
    truth = Truth(BASE)
    harness = make(truth)
    client = MagicMock()

    async def own(_baby_uid: str, start: int, end: int) -> CareLogFetch:
        return await truth.fetch(start, end)

    client.async_get_calendar_result = AsyncMock(side_effect=own)
    harness.importer = _importer_factory(hass, harness.entry, client, harness.coordinator)()
    harness.start()
    await harness.poll()
    assert harness.diag()["state"] == "running"
    passes = harness.diag()["passes_completed"]
    due = harness.importer._history_due
    assert due is not None
    await harness.advance(due - timedelta(hours=1) - dt_util.utcnow())

    async def throttled() -> CareLogFetch:
        raise NanitRateLimited("Calendar fetch", 7200)

    with pytest.raises(NanitRateLimited):
        await harness.coordinator.async_request(throttled)
    held_until = dt_util.utcnow() + timedelta(hours=2)
    truth.own.clear()

    # Due an hour later, and tried on the retry schedule: nothing is asked.
    while dt_util.utcnow() + timedelta(minutes=5) < held_until:
        await harness.advance(timedelta(minutes=5))
        assert truth.own == []
    assert harness.importer._failures >= 2
    assert harness.importer._history_due == due
    assert harness.diag()["passes_completed"] == passes

    # The retry after the hold reads the whole pass, each piece once.
    for _ in range(18):
        await harness.advance(timedelta(minutes=5))
    assert harness.diag()["passes_completed"] == passes + 1
    assert truth.own
    assert len(truth.own) == len(set(truth.own))
    assert truth.own[0][0] == care_log_history_start(BABY, dt_util.utcnow())
    assert harness.importer._failures == 0


@pytest.mark.usefixtures("reader_fails_at_once")
async def test_a_reader_poll_during_the_pass_that_fills_an_outage_gap_does_not_hold_rows_back(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The seven day read leaves a gap; a 24 hour poll arrives while the pass fills it."""
    truth = Truth(BASE)
    harness = await _imported(make, truth)  # 20 November 18:10
    truth.reader_down = True
    # Before the seven day read: only the history pass reads it.
    unread = _bottle("b_unread", NOW + timedelta(days=2, hours=4), 45.0)
    # Inside the seven day read, before the 24 hour poll.
    recent = _bottle("b_recent", NOW + timedelta(days=8), 35.0)
    truth.put(unread)
    truth.put(recent)
    for _ in range(10):  # the daily passes go on; the reader keeps failing
        await harness.advance(timedelta(days=1))
    truth.reader_down = False
    truth.own.clear()

    async def poll_during_the_pass(_start: int, _end: int) -> None:
        truth.during_own = None
        freezer.tick(timedelta(seconds=30))
        await harness.coordinator.async_refresh()  # an ordinary 24 hour poll

    truth.during_own = poll_during_the_pass
    await harness.poll()  # 30 November 18:10: seven days, and the pass brought forward

    seven_days, one_day = truth.reader[-2:]
    assert seven_days[0] == _first_poll_start(NOW + timedelta(days=10))
    assert seven_days[0] > unread.timestamp
    assert seven_days[0] < recent.timestamp < one_day[0]
    # That one pass caught up, through both reader reads.
    assert truth.own == list(_expected_pieces(seven_days[0]))
    volume = await _rows(hass, S.BOTTLE_VOLUME)
    assert max(volume) == _end_hour() - H
    assert volume[_hour(unread)] == (45.0, 255.0)
    assert volume[_hour(recent)] == (35.0, 290.0)
    assert volume[_end_hour() - H] == (0.0, 290.0)


async def test_an_answer_arriving_after_the_hour_turned_does_not_certify_the_hour_it_was_asked_in(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)  # 18:10, rows up to 17:00
    await harness.advance(timedelta(minutes=49, seconds=50))  # 18:59:50, a reader poll
    hour = hour_start(_ts(NOW))  # 18:00
    late = _bottle("b_late", NOW + timedelta(minutes=49, seconds=55), 25.0)
    client = harness.coordinator._hub.client

    async def slow(baby_uid: str, start: int, end: int) -> CareLogFetch:
        answer = await truth.reader_fetch(baby_uid, start, end)
        truth.put(late)  # logged while the request was on its way
        freezer.tick(timedelta(seconds=20))  # the answer arrives at 19:00:10
        return answer

    client.async_get_calendar_result.side_effect = slow
    await harness.poll()
    client.async_get_calendar_result.side_effect = truth.reader_fetch

    assert harness.importer._book.ledger.get(late.uid) is None
    assert hour not in await _stored(hass, S.BOTTLE_VOLUME)

    await harness.tick_and_poll(timedelta(minutes=5))  # 19:05:10, asked after 19:00

    volume = await _rows(hass, S.BOTTLE_VOLUME)
    assert volume[hour] == (25.0, 235.0)
    assert max(volume) == hour


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
    assert len(harness.importer._book.ledger) == 5

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
    assert len(harness.importer._book.pending_requests()) == 1
    assert truth.own == []

    await harness.advance(MIN_GAP - timedelta(seconds=1))
    assert truth.own == []
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    await harness.advance(timedelta(seconds=1))

    assert truth.own[0] == read
    assert harness.diag()["confirm_fetches"] == 1
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0
    assert len(harness.importer._book.pending_requests()) == 0
    assert len(harness.importer._book.ledger) == 3


async def test_a_one_off_omission_is_cancelled_by_the_confirm_fetch(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.omit_next = {B1.uid}

    await harness.tick_and_poll(timedelta(minutes=1))
    read = truth.reader[-1]
    assert len(harness.importer._book.pending_requests()) == 1

    await harness.advance(MIN_GAP)

    assert truth.own == [read]
    assert len(harness.importer._book.pending_requests()) == 0
    assert harness.diag()["confirm_fetches"] == 1
    for _ in range(3):
        await harness.tick_and_poll(MIN_GAP)
    assert truth.own == [read]
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0


@pytest.mark.parametrize("ml", [30.0, 200.0], ids=["lower", "higher"])
async def test_an_amount_rejected_after_a_restart_becomes_valid_only_after_a_re_read(
    make: Callable[..., Harness], hass: HomeAssistant, ml: float
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    # After a restart, Nanit's amount for b1 cannot be used: the stored 120
    # is kept, and the ledger knows the entry but not its amount.
    truth.put(
        CareLog(
            uid=B1.uid,
            log_type="bottle_feed",
            timestamp=B1.timestamp,
            baby_uid=UID,
            invalid_fields=frozenset({"feed_amount_ml"}),
        )
    )
    second = await _imported(make, truth)
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    truth.put(_bottle(B1.uid, NOW - timedelta(hours=3, minutes=40), ml))
    await second.tick_and_poll(timedelta(minutes=1))

    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
    assert len(second.importer._book.pending_requests()) == 1

    await second.advance(MIN_GAP)

    assert second.diag()["confirm_fetches"] == 1
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == ml


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
    assert len(harness.importer._book.pending_requests()) == 0

    # The next reader poll finds the removal again; its re-read waits.
    truth.dropped_own = 0
    await harness.advance(timedelta(minutes=1))  # 18:27, the reader's own poll
    assert len(harness.importer._book.pending_requests()) == 1
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

    await harness.advance_polling(timedelta(hours=24) - timedelta(minutes=1))
    assert truth.own == []
    await harness.poll()
    written = _rows_sent(sent)

    await harness.advance(timedelta(minutes=1))

    daily = truth.own
    assert daily == list(_expected_pieces(truth.reader[-1][0]))
    assert daily[: len(first_pieces) - 1] == first_pieces[:-1]
    assert daily[-1][1] == truth.reader[-1][0]
    assert _rows_sent(sent) == written
    assert harness.diag()["passes_completed"] == 2
    assert harness.diag()["minutes_since_last_history_pass"] == 0

    # The next one a day after that.
    truth.own.clear()
    await harness.advance_polling(timedelta(hours=24) - timedelta(seconds=1))
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

    def process(book: CareLogBook, read: Any, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real_process(book, read, **kwargs)

    late = _bottle("b_late", NOW - timedelta(hours=1, minutes=30), 30.0)
    truth.put(late)
    with patch.object(CareLogBook, "process", process):
        await harness.tick_and_poll(timedelta(minutes=5))
        assert len(harness.importer._book.ledger) == 4
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "RuntimeError" in errors[0].getMessage()
        assert not harness.importer._task.done()

        await harness.tick_and_poll(timedelta(minutes=5))

    assert len(harness.importer._book.ledger) == 5
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(late)] == 30.0


# ---------------------------------------------------------------------------
# Sightings
# ---------------------------------------------------------------------------


async def test_absent_present_absent_at_start_up_lowers_nothing(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    second = make(truth)
    truth.omit_next = {B1.uid}  # the reader's read the first pass starts from
    second.start()
    await second.poll()  # 18:10
    assert second.diag()["state"] == "waiting_for_second_pass"

    await second.tick_and_poll(timedelta(minutes=2))  # 18:12, the reader returns b1
    truth.omit_own = {B1.uid}  # and the second pass leaves it out again
    await second.advance(MIN_GAP - timedelta(minutes=2))  # 18:15, the second pass

    assert second.diag()["passes_completed"] == 2
    assert second.diag()["state"] == "waiting_for_second_pass"
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    truth.omit_own = set()
    for _ in range(3):
        await second.advance(MIN_GAP)
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0


async def test_a_reader_read_during_the_confirm_fetch_cancels_the_removal(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.omit_next = {B1.uid}
    await harness.tick_and_poll(timedelta(minutes=1))  # left out once: a removal waits
    assert len(harness.importer._book.pending_requests()) == 1

    async def reader_poll_meanwhile(_start: int, _end: int) -> None:
        truth.during_own = None
        freezer.tick(timedelta(seconds=1))
        await harness.coordinator.async_refresh()  # returns b1
        freezer.tick(timedelta(seconds=1))
        truth.omit_own = {B1.uid}  # the confirm fetch, answered last, leaves it out

    truth.during_own = reader_poll_meanwhile
    await harness.advance(MIN_GAP)

    assert harness.diag()["confirm_fetches"] == 1
    assert B1.uid in harness.importer._book.ledger
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0

    truth.omit_own = set()
    for _ in range(2):
        await harness.advance(MIN_GAP)
    assert harness.importer._book.pending_requests() == ()
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0


@pytest.mark.parametrize("later", [True, False], ids=["later_outside", "earlier_outside"])
async def test_a_row_of_another_kind_outside_the_range_never_removes_the_entry_of_its_uid(
    make: Callable[..., Harness], hass: HomeAssistant, later: bool
) -> None:
    """Every answer, the reader's and the importer's own, also holds a sleep row with b1's uid."""
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    when = NOW + timedelta(days=30) if later else NOW - timedelta(days=4 * 365)
    stray = CareLog(
        uid=B1.uid, log_type="sleep", timestamp=_ts(when), baby_uid=UID, duration_s=1800.0
    )
    truth.extra = [stray]

    for _ in range(3):
        await harness.tick_and_poll(MIN_GAP)

    assert B1.uid in harness.importer._book.ledger
    assert harness.importer._book.pending_requests() == ()
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
    assert harness.coordinator.data.latest["bottle_feed"] == B1


async def test_a_reader_queue_overflow_forgets_every_waiting_change(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)
    await harness.tick_and_poll(timedelta(minutes=1))  # the removal waits
    assert len(harness.importer._book.pending_requests()) == 1

    async def reader_polls_meanwhile(_start: int, _end: int) -> None:
        truth.during_own = None
        for _ in range(3):
            freezer.tick(timedelta(seconds=1))
            await harness.coordinator.async_refresh()
        freezer.tick(timedelta(seconds=1))

    truth.during_own = reader_polls_meanwhile
    with patch.object(care_log_importer, "READER_QUEUE", 2):
        await harness.advance(MIN_GAP)

    # A read was lost while the confirm fetch ran: the fetch, which would
    # have confirmed the removal, only starts a new round.
    assert harness.diag()["queue_overflows"] == 1
    assert harness.diag()["confirm_fetches"] == 1
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
    assert len(harness.importer._book.pending_requests()) == 1

    await harness.advance(MIN_GAP)
    assert harness.diag()["confirm_fetches"] == 2
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0


async def test_an_own_answer_converted_while_reader_reads_are_lost_starts_no_lowering(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The confirm fetch's answer is turned into a read off the loop while the queue overflows."""
    # Near the start of the 18:11 poll's 24 hours: the polls from 18:16 on
    # no longer reach either.
    edge = _bottle("b_edge", NOW - timedelta(hours=23, minutes=57), 65.0)
    fresh = _bottle("b_fresh", NOW - timedelta(hours=23, minutes=56), 30.0)
    truth = Truth([*BASE, edge])
    harness = await _imported(make, truth)
    truth.omit_next = {edge.uid}
    await harness.tick_and_poll(timedelta(minutes=1))  # 18:11, left out once: a removal waits
    first = truth.reader[-1]
    assert [(r.start, r.end) for r in harness.importer._book.pending_requests()] == [first]

    async def logged_meanwhile(_start: int, _end: int) -> None:
        truth.during_own = None
        truth.put(fresh)  # in this answer only

    truth.during_own = logged_meanwhile
    truth.omit_own = {edge.uid}  # the confirm fetch leaves it out too
    real_job = hass.async_add_executor_job
    injected: list[int] = []

    def job(target: Any, *args: Any) -> Any:
        if getattr(target, "__name__", "") == "_read_from_fetch" and not injected:
            injected.append(1)

            async def polls_meanwhile() -> Any:
                for _ in range(3):
                    freezer.tick(timedelta(seconds=1))
                    await harness.coordinator.async_refresh()
                return await real_job(target, *args)

            return polls_meanwhile()
        return real_job(target, *args)

    with (
        patch.object(hass, "async_add_executor_job", job),
        patch.object(care_log_importer, "LARGE_READ", 0),
        patch.object(care_log_importer, "READER_QUEUE", 2),
    ):
        await harness.advance(MIN_GAP)  # 18:16, the confirm fetch

    assert injected
    assert harness.diag()["queue_overflows"] == 1
    assert harness.diag()["confirm_fetches"] == 1
    # The answer still adds what it holds...
    assert fresh.uid in harness.importer._book.ledger
    # ...but neither confirms nor starts the removal: a lost read may lie
    # between its request and its conversion.
    assert harness.importer._book.pending_requests() == ()
    truth.omit_own = set()

    await harness.advance(MIN_GAP)  # 18:21
    assert harness.diag()["confirm_fetches"] == 1
    assert edge.uid in harness.importer._book.ledger
    volume = await _stored(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(edge)] == 95.0


async def test_an_older_read_processed_late_ends_at_the_truth(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # the restart needs a second pass
    second = make(truth)
    second.start()
    await second.poll()  # 18:10, the first pass
    late = _bottle("b_running", NOW + timedelta(minutes=1), 50.0)  # in the running hour
    truth.put(late)
    await second.tick_and_poll(timedelta(minutes=2))  # 18:12, kept: it returns the bottle
    truth.delete(late.uid)
    await second.advance(MIN_GAP - timedelta(minutes=2))  # 18:15, the second pass agrees

    assert second.diag()["state"] == "running"
    # The kept read, older than the adopted pass, is discarded: it does not
    # add the bottle back.
    assert late.uid not in second.importer._book.ledger

    # Once its hour has finished, nothing of it is stored either.
    await second.tick_and_poll(timedelta(minutes=50))  # 19:05
    await second.advance(MIN_GAP)  # 19:10
    assert late.uid not in second.importer._book.ledger
    volume = await _rows(hass, S.BOTTLE_VOLUME)
    assert volume[_hour(late)] == (0.0, 210.0)
    assert volume[_end_hour() - H] == (0.0, 210.0)
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0


async def test_an_older_read_processed_late_cannot_bring_back_a_confirmed_removal(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """In a finished hour. The clock is moved back to stamp a read older than one processed."""
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    truth.delete(B1.uid)
    await harness.tick_and_poll(timedelta(minutes=1))  # 18:11, the removal waits
    await harness.advance(MIN_GAP)  # 18:16, the confirm fetch removes it
    assert B1.uid not in harness.importer._book.ledger
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0

    # A reader read taken at 18:15, while b1 was still there, arrives late.
    truth.put(B1)
    freezer.move_to(NOW + timedelta(minutes=5))
    await harness.poll()
    truth.delete(B1.uid)
    freezer.move_to(NOW + timedelta(minutes=7))
    await harness.poll()

    assert B1.uid not in harness.importer._book.ledger
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 0.0


async def test_a_reader_omission_between_two_agreeing_passes_is_not_replayed_after_them(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    """Both passes return b1; a reader poll between them leaves it out."""
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # the restart needs a second pass
    second = make(truth)
    second.start()
    await second.poll()  # 18:10, the first pass
    truth.omit_next = {B1.uid}
    await second.tick_and_poll(timedelta(minutes=2))  # 18:12, leaves b1 out
    await second.advance(MIN_GAP - timedelta(minutes=2))  # 18:15, the second pass agrees
    assert second.diag()["state"] == "running"

    # One more omission at that poll's bounds, a gap after it (18:17), would
    # confirm a removal the older poll had started: none was started.
    truth.omit_own = {B1.uid}
    await second.advance(timedelta(minutes=2))  # 18:17
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
    assert B1.uid in second.importer._book.ledger
    assert second.diag()["confirm_fetches"] == 0


async def test_a_removal_confirmed_by_passes_two_and_three_is_not_undone_by_an_older_sighting(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # deleted while stopped
    second = make(truth)
    second.start()
    await second.poll()  # 18:10, the first pass
    truth.put(D2)
    await second.tick_and_poll(timedelta(minutes=2))  # 18:12, a sighting of d2
    truth.delete(D2.uid)
    await second.advance(MIN_GAP - timedelta(minutes=2))  # 18:15, vetoed by the sighting
    assert second.diag()["state"] == "waiting_for_second_pass"
    assert second.diag()["passes_completed"] == 2

    await second.advance(MIN_GAP)  # 18:20, the third pass agrees with the second

    assert second.diag()["state"] == "running"
    assert second.diag()["passes_completed"] == 3
    assert D2.uid not in second.importer._book.ledger
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0


@pytest.mark.usefixtures("reader_fails_at_once")
async def test_a_sighting_beyond_the_passes_survives_a_round_that_starts_over(
    make: Callable[..., Harness], hass: HomeAssistant
) -> None:
    """Retries hold the second pass back; meanwhile a poll sees an entry after the passes' end.

    An unrelated edit makes the second pass disagree, so a third one
    follows: the poll, taken before the round started over, still counts.
    """
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # the restart needs a second pass
    second = make(truth)
    second.start()
    await second.poll()  # 18:10, the first pass
    assert second.diag()["state"] == "waiting_for_second_pass"
    passes_end = truth.reader[-1][1]  # 19:10, the end of every pass's last read
    truth.reader_down = True
    truth.errors = [NanitConnectionError("HTTP 503") for _ in range(3)]
    for _ in range(13):  # tried at 18:15, 18:20 and 18:35; next at 19:35
        await second.advance(timedelta(minutes=5))
    assert second.diag()["state"] == "waiting_to_retry"

    late = _bottle("b_late", NOW + timedelta(minutes=63), 25.0)  # 19:13
    assert late.timestamp > passes_end
    truth.put(late)
    truth.put(_bottle(OLD.uid, datetime.fromtimestamp(OLD.timestamp, UTC), 100.0))
    truth.reader_down = False
    await second.tick_and_poll(timedelta(minutes=2))  # 19:17: the only read of the bottle
    truth.reader_down = True

    await second.advance(timedelta(minutes=18))  # 19:35: the second pass, which disagrees
    assert second.diag()["state"] == "waiting_for_second_pass"
    assert second.diag()["passes_completed"] == 2
    await second.advance(MIN_GAP)  # 19:40: the third pass agrees with the second

    assert second.diag()["state"] == "running"
    assert second.diag()["passes_completed"] == 3
    assert late.uid in second.importer._book.ledger
    assert D2.uid not in second.importer._book.ledger
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 100.0


async def test_a_recent_poll_still_counts_when_a_piece_of_an_older_period_finishes_after_it(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A poll sees a newly backdated bottle; the pass's last piece is answered after it."""
    # Near the start of a 24 hour window: the poll after it no longer reaches it.
    backdated = _bottle("b_backdated", NOW - timedelta(hours=23, minutes=58), 65.0)
    truth = Truth(BASE)
    harness = make(truth)
    pieces = list(_expected_pieces(_first_poll_start()))

    async def poll_during_the_last_piece(start: int, end: int) -> None:
        if (start, end) != pieces[-1]:
            return
        truth.during_own = None
        truth.put(backdated)
        freezer.tick(timedelta(seconds=1))
        await harness.coordinator.async_refresh()  # 24 hours: it returns the bottle
        freezer.tick(timedelta(seconds=1))  # the piece, months earlier, comes after

    truth.during_own = poll_during_the_last_piece
    harness.start()
    await harness.poll()  # 18:10, the first pass; nothing stored, so adopted at once

    assert harness.diag()["state"] == "running"
    assert truth.reader[-1][0] < backdated.timestamp
    assert backdated.uid in harness.importer._book.ledger
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(backdated)] == 65.0

    await harness.tick_and_poll(timedelta(minutes=5))
    assert truth.reader[-1][0] > backdated.timestamp
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(backdated)] == 65.0
    assert harness.diag()["passes_completed"] == 1


async def test_a_reader_read_lost_while_the_adoption_waits_starts_the_round_over(
    make: Callable[..., Harness], hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    truth = Truth(BASE)
    first = await _imported(make, truth)
    await first.stop()
    truth.delete(D2.uid)  # the restart needs a second pass
    second = make(truth)
    importer = second.importer
    real_job = hass.async_add_executor_job
    injected: list[str] = []

    def job(target: Any, *args: Any) -> Any:
        name = getattr(target, "__name__", "")
        candidates = {"adopt", "candidate", "candidate_and_times"}
        if importer._passes >= 2 and name in candidates and not injected:
            injected.append(name)

            async def polls_meanwhile() -> Any:
                for _ in range(3):
                    freezer.tick(timedelta(seconds=1))
                    await second.coordinator.async_refresh()
                return await real_job(target, *args)

            return polls_meanwhile()
        return real_job(target, *args)

    second.start()
    await second.poll()  # 18:10, the first pass
    with (
        patch.object(hass, "async_add_executor_job", job),
        patch.object(care_log_importer, "READER_QUEUE", 2),
    ):
        await second.advance(MIN_GAP)  # 18:15, the second pass agrees

    assert injected
    assert second.diag()["queue_overflows"] == 1
    # The round started over: nothing was adopted or lowered.
    assert second.diag()["state"] == "waiting_for_second_pass"
    assert not importer._book.established
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 1.0

    await second.advance(MIN_GAP)
    assert second.diag()["state"] == "running"
    assert (await _stored(hass, S.DIAPER_PEE))[_hour(D2)] == 0.0


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
        "established",
        "waiting_for_confirmation",
        "passes_completed",
        "confirm_fetches",
        "give_ups",
        "sync_failures",
        "queue_overflows",
        "minutes_since_last_sync",
        "minutes_since_last_history_pass",
    }
    for value in diagnostics.values():
        assert value is None or isinstance(value, int | bool | str)
    assert diagnostics["state"] == "stopped"
    assert calls == 2


# ---------------------------------------------------------------------------
# A batch whose outcome is unknown
# ---------------------------------------------------------------------------

# The settle bound in these tests; time moves only with the freezer.
SETTLE = 10.0


@pytest.fixture
def settle_bound() -> Iterator[None]:
    # create: the same tests run against code from before the bound existed.
    with patch.object(care_log_recorder, "SETTLE_TIMEOUT", SETTLE, create=True):
        yield


@dataclass
class _Nothing(RecorderTask):
    """Queued behind a held task, so the recorder's queue is not empty."""

    def run(self, instance: Recorder) -> None:
        """Do nothing."""


def _hold_recorder(hass: HomeAssistant, gate: threading.Event) -> threading.Event:
    """Hold the recorder's thread until ``gate`` is set, with a task queued behind."""
    entered = threading.Event()
    instance = get_instance(hass)
    instance.queue_task(_HoldRecorder(entered, gate))
    instance.queue_task(_Nothing())
    return entered


async def _tick_until_idle(harness: Harness) -> None:
    """Move time on past the settle bound, now and then, until the worker idles.

    Between the steps, real time passes for the executor jobs to finish.
    """
    for attempt in range(300):
        await harness.hass.async_block_till_done()
        if harness.importer._idle.is_set():
            return
        if attempt % 25 == 24:
            harness.freezer.tick(timedelta(seconds=SETTLE + 1))
            async_fire_time_changed_exact(harness.hass, dt_util.utcnow())
        await harness.hass.async_add_executor_job(time.sleep, 0.02)
    raise AssertionError("the importer never had nothing to do")


@contextmanager
def _counting_compares() -> Iterator[list[int]]:
    calls: list[int] = []
    real_compare = care_log_importer.async_compare_care_log_statistics

    async def compare(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real_compare(*args, **kwargs)

    with patch.object(care_log_importer, "async_compare_care_log_statistics", compare):
        yield calls


@contextmanager
def _counting_syncs() -> Iterator[list[int]]:
    calls: list[int] = []
    real_sync = care_log_importer.async_sync_care_log_statistics

    async def sync(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real_sync(*args, **kwargs)

    with patch.object(care_log_importer, "async_sync_care_log_statistics", sync):
        yield calls


def _entry_on(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    return entry


@asynccontextmanager
async def _restarted(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    truth: Truth,
    async_test_recorder: RecorderInstanceContextManager,
) -> AsyncIterator[Harness]:
    """Stop this Home Assistant's recorder; yield a new harness on a fresh one, same database."""
    await async_wait_recording_done(hass)
    await get_instance(hass)._async_shutdown(None)
    async with async_test_home_assistant() as fresh, async_test_recorder(fresh):
        await fresh.config.async_set_time_zone("UTC")
        harness = Harness(fresh, freezer, truth, _entry_on(fresh))
        try:
            yield harness
        finally:
            await harness.importer.async_stop()
            await fresh.async_stop(force=True)


# Nanit's bottle raised, the batch of which is the one whose outcome is unknown.
RAISED = _bottle(B1.uid, NOW - timedelta(hours=3, minutes=40), 200.0)


async def _stays_paused(
    harness: Harness, make: Callable[..., Harness], truth: Truth, sent: Sent
) -> Harness:
    """Nanit is back at 120: neither the importer nor the next one compares, syncs or asks.

    Return the next one, started as after a reload.
    """
    written = len(sent)
    own = len(truth.own)
    truth.put(B1)
    with _counting_compares() as compares, _counting_syncs() as syncs:
        await harness.tick_and_poll(timedelta(minutes=1))
        await harness.advance(MIN_GAP)  # a confirm fetch would be due
        await harness.tick_and_poll(timedelta(minutes=15))  # and a sync retry
        assert harness.diag()["state"] == "paused_until_restart"
        assert harness.importer._unsub_timer is None
        await harness.stop()
        again = make(truth)  # as after a reload
        again.start()
        await again.poll()
        await again.tick_and_poll(timedelta(minutes=5))
        assert compares == []
        assert syncs == []
    assert again.diag()["state"] == "paused_until_restart"
    assert again.importer._unsub_timer is None
    assert len(truth.own) == own
    assert len(sent) == written
    return again


async def _ends_correct_after_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    truth: Truth,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """After a restart the stored 200 is compared as stored, then lowered to Nanit's 120."""
    async with _restarted(hass, freezer, truth, async_test_recorder) as after:
        fresh = after.hass
        assert not care_log_recorder.care_log_paused(fresh, CODE)
        assert (await _stored(fresh, S.BOTTLE_VOLUME))[_hour(B1)] == 200.0
        with _counting_compares() as compares:
            after.start()
            await after.poll()
        assert compares == [1]
        # It would lower a stored cell: a second pass a gap later first.
        assert after.diag()["state"] == "waiting_for_second_pass"
        await after.advance(MIN_GAP)
        assert after.diag()["state"] == "running"
        volume = await _rows(fresh, S.BOTTLE_VOLUME)
        assert volume[_hour(B1)] == (120.0, 210.0)
        assert volume[_end_hour() - H] == (0.0, 210.0)


async def test_a_paused_importer_lets_the_reader_go_and_one_started_while_paused_never_listens(
    make: Callable[..., Harness], hass: HomeAssistant, sent: Sent
) -> None:
    """The importer is the reader's only listener: once paused, nothing keeps it polling."""
    truth = Truth(BASE)
    harness = await _imported(make, truth)
    assert len(harness.coordinator._listeners) == 1

    async def never_matches(_self: Any, _sid: str, _batch: Any) -> bool:
        return False

    written = len(sent)
    truth.put(RAISED)
    with patch.object(care_log_recorder._Sync, "_read_back", never_matches):
        await harness.tick_and_poll(timedelta(minutes=1))  # the batch is sent, never verified
    assert len(sent) == written + 1
    assert care_log_recorder.care_log_paused(hass, CODE)
    assert harness.diag()["state"] == "paused_until_restart"
    assert not harness.coordinator._listeners
    assert not harness.importer._reader_queue
    polls = len(truth.reader)
    for _ in range(12):
        await harness.advance(timedelta(minutes=5))
    assert len(truth.reader) == polls
    assert harness.diag()["state"] == "paused_until_restart"
    await harness.stop()

    again = make(truth)  # as after a reload
    again.start()
    await again.settle()
    assert again.diag()["state"] == "paused_until_restart"
    assert not again.coordinator._listeners
    for _ in range(3):
        await again.advance(timedelta(minutes=5))
    assert len(truth.reader) == polls
    assert truth.own == []
    assert len(sent) == written + 1


@dataclass
class _HoldUntilQueued(RecorderTask):
    """Holds the recorder's thread until ``count`` tasks wait in its queue (10 s at most)."""

    count: int

    def run(self, instance: Recorder) -> None:
        for _ in range(1000):
            if instance._queue.qsize() >= self.count:
                return
            time.sleep(0.01)


async def test_a_read_back_that_stalls_ends_the_stop_in_time_and_the_next_importer_waits(
    make: Callable[..., Harness],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    sent: Sent,
    settle_bound: None,
) -> None:
    """The database read verifying the first batch never returns; the importer is stopped."""
    truth = Truth(BASE)
    entered = threading.Event()
    release = threading.Event()
    real_read = care_log_recorder._read_rows

    def read(hass: HomeAssistant, sid: str, start: int, end: int) -> Any:
        if sent and not release.is_set():
            entered.set()
            release.wait(30)
        return real_read(hass, sid, start, end)

    harness = make(truth)
    try:
        with patch.object(care_log_recorder, "_read_rows", read):
            harness.start()
            await harness.coordinator.async_refresh()
            assert await hass.async_add_executor_job(entered.wait, 30)
            stop = hass.async_create_task(harness.importer.async_stop())
            for _ in range(20):
                await asyncio.sleep(0)
            assert not stop.done()

            freezer.tick(timedelta(seconds=SETTLE + 1))
            async_fire_time_changed_exact(hass, dt_util.utcnow())
            for _ in range(50):
                await asyncio.sleep(0)
                if stop.done():
                    break
            stopped_in_time = stop.done()
    finally:
        release.set()
    await stop
    assert stopped_in_time
    assert harness.diag()["state"] == "stopped"
    assert len(sent) == 1
    assert care_log_recorder.care_log_paused(hass, CODE)
    assert await care_log_recorder.async_has_care_log_statistics(hass, CODE) is None

    # The next importer in this run waits until Home Assistant restarts, even
    # once the recorder's queue has drained.
    await async_wait_recording_done(hass)
    again = make(truth)
    own = len(truth.own)
    with _counting_compares() as compares:
        again.start()
        await again.poll()
        await again.tick_and_poll(timedelta(minutes=5))
        assert again.diag()["state"] == "paused_until_restart"
        assert again.importer._unsub_timer is None
        assert compares == []
        assert len(truth.own) == own
        assert len(sent) == 1
    assert care_log_recorder.care_log_paused(hass, CODE)


@pytest.mark.parametrize("persistent_database", [True])
async def test_a_batch_that_lands_after_its_verification_failed_is_compared_as_stored(
    make: Callable[..., Harness],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    sent: Sent,
    settle_bound: None,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """Every read-back of the first batch finds nothing: the recorder holds it."""
    truth = Truth(BASE)
    entered = threading.Event()
    gate = threading.Event()
    spy = care_log_recorder.async_add_external_statistics

    def busy_first(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        if not entered.is_set():
            # The batch queues behind a task that holds the recorder.
            get_instance(hass).queue_task(_HoldRecorder(entered, gate))
        spy(hass, metadata, rows)

    harness = make(truth)
    try:
        with (
            patch.object(care_log_recorder, "BATCH_ROWS", 50),
            patch.object(care_log_recorder, "async_add_external_statistics", busy_first),
            # Each wait before a read-back ends at once; all of them fail.
            patch.object(care_log_recorder, "WRITE_TIMEOUT", 0.0),
        ):
            harness.start()
            await harness.poll()
        assert harness.diag()["sync_failures"] == 1
        assert len(sent) == 1
        await harness.stop()
        first_sid, first_rows = sent[0]
        assert await _rows(hass, S.DIAPER_CHANGES) == {}  # not landed yet

        again = make(truth)
        own = len(truth.own)
        with _counting_compares() as compares:
            again.start()
            await again.coordinator.async_refresh()
            await _tick_until_idle(again)
            assert again.diag()["state"] == "paused_until_restart"
            assert compares == []
            assert len(truth.own) == own
            assert len(sent) == 1
        assert care_log_recorder.care_log_paused(hass, CODE)
    finally:
        gate.set()

    # The batch lands; the rest of this run compares and writes nothing more.
    await async_wait_recording_done(hass)
    stored = await _rows(hass, S.DIAPER_CHANGES)
    assert [(hour, *stored[hour]) for hour, _s, _t in first_rows] == first_rows
    with _counting_compares() as compares:
        await again.poll()
    assert compares == []
    assert again.diag()["state"] == "paused_until_restart"
    assert len(sent) == 1

    # After a restart the comparison finds the batch stored, and imports the rest.
    async with _restarted(hass, freezer, truth, async_test_recorder) as after:
        fresh = after.hass
        with _counting_compares() as compares:
            after.start()
            await after.poll()
        assert compares == [1]
        assert after.diag()["state"] == "running"
        assert first_rows[0][0] not in [
            hour for sid, rows in sent[1:] if sid == first_sid for hour, _s, _t in rows
        ]
        assert (await _stored(fresh, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0
        rows = await _rows(fresh, S.DIAPER_CHANGES)
        assert {hour: rows[hour] for hour, _s, _t in first_rows} == {
            hour: (state, total) for hour, state, total in first_rows
        }


async def test_a_queue_that_does_not_drain_at_start_up_waits_for_the_next_reader_update(
    make: Callable[..., Harness], hass: HomeAssistant, settle_bound: None
) -> None:
    truth = Truth(BASE)
    gate = threading.Event()
    harness = make(truth)
    try:
        assert await hass.async_add_executor_job(_hold_recorder(hass, gate).wait, 30)
        with _counting_compares() as compares:
            harness.start()
            await harness.coordinator.async_refresh()
            await _tick_until_idle(harness)
            # The first pass was read; the comparison found the queue still busy.
            assert compares == [1]
        assert harness.diag()["state"] == "waiting_for_recorder"
        # Nothing was sent: nothing is paused.
        assert not care_log_recorder.care_log_paused(hass, CODE)
        own = len(truth.own)

        with _counting_steps(harness) as steps:
            for _ in range(4):
                await harness.advance(timedelta(minutes=1))
            assert steps == []
            assert harness.importer._unsub_timer is None
            await harness.coordinator.async_refresh()  # the next reader update
            await _tick_until_idle(harness)
            assert steps == [1]
        assert harness.diag()["state"] == "waiting_for_recorder"
        assert len(truth.own) == own
    finally:
        gate.set()

    await harness.poll()
    assert harness.diag()["state"] == "running"
    assert not care_log_recorder.care_log_paused(hass, CODE)
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(OLD)] == 90.0


@pytest.mark.parametrize("persistent_database", [True])
async def test_a_batch_still_running_when_the_queue_is_empty_pauses_the_baby_until_a_restart(
    make: Callable[..., Harness],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    sent: Sent,
    settle_bound: None,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """The recorder has taken the batch off its queue and is still inside it."""
    from homeassistant.components.recorder import statistics

    truth = Truth(BASE)
    harness = await _imported(make, truth)
    entered = threading.Event()
    gate = threading.Event()
    real_import = statistics.import_statistics
    spy = care_log_recorder.async_add_external_statistics
    empty: list[bool] = []

    def held(instance: Recorder, *args: Any) -> bool:
        if not entered.is_set():
            # Home Assistant's wait for its queue returns at once from here on.
            empty.append(instance._queue.empty())
            entered.set()
            gate.wait(30)
        result: bool = real_import(instance, *args)
        return result

    def add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        spy(hass, metadata, rows)
        # The recorder's thread takes the batch off its queue and runs it.
        assert entered.wait(10)

    truth.put(RAISED)
    try:
        with (
            patch.object(statistics, "import_statistics", held),
            patch.object(care_log_recorder, "async_add_external_statistics", add),
        ):
            freezer.tick(timedelta(minutes=1))
            await harness.coordinator.async_refresh()
            await _tick_until_idle(harness)
            # The batch was the last task, taken off the queue and still running.
            assert empty == [True]
            assert harness.diag()["state"] == "paused_until_restart"
            assert care_log_recorder.care_log_paused(hass, CODE)
            assert await care_log_recorder.async_has_care_log_statistics(hass, CODE) is None
            assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
            again = await _stays_paused(harness, make, truth, sent)
    finally:
        gate.set()

    # The batch lands late; still nothing more in this run.
    await async_wait_recording_done(hass)
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 200.0
    with _counting_compares() as compares, _counting_syncs() as syncs:
        await again.tick_and_poll(timedelta(minutes=5))
    assert (compares, syncs) == ([], [])
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 200.0
    await again.stop()

    await _ends_correct_after_a_restart(hass, freezer, truth, async_test_recorder)


@pytest.mark.parametrize("persistent_database", [True])
async def test_a_batch_retried_behind_the_wait_for_the_queue_pauses_the_baby_until_a_restart(
    make: Callable[..., Harness],
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    sent: Sent,
    settle_bound: None,
    async_test_recorder: RecorderInstanceContextManager,
) -> None:
    """The import hits a retryable error and queues itself again behind the importer's wait."""
    from homeassistant.components.recorder import statistics

    truth = Truth(BASE)
    harness = await _imported(make, truth)
    gate = threading.Event()
    calls: list[str] = []
    queued: list[str] = []
    real_import = statistics.import_statistics
    real_queue = Recorder.queue_task
    spy = care_log_recorder.async_add_external_statistics
    armed = [True]

    def flaky(instance: Recorder, *args: Any) -> bool:
        if not calls:
            # Not finished, as after a retryable database error: Home
            # Assistant queues the task again, behind what was queued meanwhile.
            calls.append("failed")
            return False
        if len(calls) == 1:
            calls.append("retried")
            gate.wait(30)
        result: bool = real_import(instance, *args)
        return result

    def queue_task(self: Recorder, task: Any) -> None:
        if not armed and isinstance(task, ImportStatisticsTask | SynchronizeTask):
            queued.append(type(task).__name__)
        real_queue(self, task)

    def add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        if armed:
            armed.clear()
            # The batch runs only once the importer's wait is queued behind it.
            get_instance(hass).queue_task(_HoldUntilQueued(2))
        spy(hass, metadata, rows)

    truth.put(RAISED)
    try:
        with (
            patch.object(statistics, "import_statistics", flaky),
            patch.object(care_log_recorder, "async_add_external_statistics", add),
            patch.object(Recorder, "queue_task", queue_task),
        ):
            freezer.tick(timedelta(minutes=1))
            await harness.coordinator.async_refresh()
            await _tick_until_idle(harness)
            assert calls == ["failed", "retried"]
            # The retry was queued behind the importer's first wait.
            assert queued[:3] == ["ImportStatisticsTask", "SynchronizeTask", "ImportStatisticsTask"]
            assert harness.diag()["state"] == "paused_until_restart"
            assert care_log_recorder.care_log_paused(hass, CODE)
            assert await care_log_recorder.async_has_care_log_statistics(hass, CODE) is None
            assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 120.0
            again = await _stays_paused(harness, make, truth, sent)
    finally:
        gate.set()

    # The retry lands late; still nothing more in this run.
    await async_wait_recording_done(hass)
    assert (await _stored(hass, S.BOTTLE_VOLUME))[_hour(B1)] == 200.0
    with _counting_compares() as compares, _counting_syncs() as syncs:
        await again.tick_and_poll(timedelta(minutes=5))
    assert (compares, syncs) == ([], [])
    await again.stop()

    await _ends_correct_after_a_restart(hass, freezer, truth, async_test_recorder)
