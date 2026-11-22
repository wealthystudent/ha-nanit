"""Import one baby's care logs into hourly long-term statistics.

``CareLogStatisticsImporter`` drives the ledger (``care_log_ledger``) and the
recorder writer (``care_log_recorder``) for one config entry and baby. It
feeds the book every read of the care log reader (``NanitCareLogCoordinator``)
and its own reads of Nanit, and keeps the stored rows equal to the ledger.

One worker task does everything, one step at a time: every use of the book,
the ledger and the recorder functions. The reader's update listener and the
timers only note what is due and wake it.

Starting up, the ledger is empty. The first successful reader update is the
newest part of a pass: the history before it is read in pieces of at most
``HISTORY_PIECE`` on a fixed grid, oldest first. When the pass would lower
no stored cell it is adopted and written at once. Otherwise nothing is
written until a second pass of the same bounds, at least the book's minimum
gap later, agrees with it (at most ``MAX_STARTUP_PASSES`` passes, then it
waits for the next daily pass).

The comparison and every sync are given the start of the history the
ledger was built from (``care_log_history_start``, as the last pass used
it), and leave every stored row before it alone: the ledger knows nothing
of them, so nothing there is lowered, whatever moved the start.

Running, each reader read goes through ``CareLogBook.process`` and an
incremental sync follows. A change that lowers a chart waits for the book's
confirm request, which the importer reads itself. Once a day the history is
read again.

Reads waiting to be processed, the reader's and the importer's own, go to
the book in the order they were taken: an older read processed late can
add or cancel, never confirm. From the start of the first start-up pass,
the reader's reads are kept. A second pass is adopted only when none of
the reads kept since its round's first pass began returns, in an hour that
was finished when that pass began and inside the pass's bounds, an entry
the second pass lacks or holds otherwise. Older evidence never overrides
newer: once a pass is adopted, every kept read taken no later than the
pass's latest read is discarded unprocessed, and only the reads taken
after it go to the book. When more than ``READER_QUEUE`` reads wait, the
oldest is lost: a start-up round in progress or waiting starts over (it is
checked after every wait of the round, up to the adoption), and every
change waiting for a confirming read is forgotten, so nothing is confirmed
across the gap.

Nanit is asked for nothing beyond the reader's own polls, and nothing is
written, while the recorder is not ready (not loaded, its database not
ready, or recording paused with the ``recorder.disable`` action), and
nothing at all when the config entry has polling disabled.

When a batch's outcome is unknown (``care_log_recorder``: the baby is
unsettled, by this importer or an earlier one), every step first waits, at
most ``SETTLE_TIMEOUT``, for the recorder's queue to drain. Until it has,
nothing is compared, synced or asked of Nanit, the state is
``waiting_for_recorder``, and no timer runs: the next reader update tries
again. Once it has, an importer still starting up compares as usual, and a
running one makes its next sync a full one.

Privacy: no entry id, baby uid, baby name or baby code appears in a log
line, an exception or ``diagnostics`` here. Unexpected errors are logged by
their type only.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from aionanit.exceptions import NanitAuthError, NanitConnectionError
from aionanit.models import Baby, CareLogFetch

from .baby_code import baby_code
from .care_log_hours import CareLogSeries, hour_start
from .care_log_ledger import CareLogBook, CareLogRead, ConfirmRequest, Ledger, Outcome
from .care_log_recorder import (
    CareLogRecorderUnavailableError,
    CareLogSyncError,
    async_compare_care_log_statistics,
    async_settle_care_log_statistics,
    async_sync_care_log_statistics,
    care_log_recorder_ready,
    care_log_unsettled,
)
from .care_log_statistics import async_statistic_names, statistic_baby_name
from .const import CARE_LOG_POLL_INTERVAL

if TYPE_CHECKING:
    from . import NanitConfigEntry
    from .coordinator import CareLogData, NanitCareLogCoordinator

_LOGGER = logging.getLogger(__name__)

# The least time between a read and the read that confirms it: one reader
# poll interval.
MIN_GAP: Final = timedelta(seconds=CARE_LOG_POLL_INTERVAL)

# ---------------------------------------------------------------------------
# Load on Nanit, beyond the reader's own polls
# ---------------------------------------------------------------------------

# At most one confirm fetch (a re-read of bounds the book asks for) per this.
CONFIRM_INTERVAL: Final = MIN_GAP
# After the book gave up on a waiting change, no confirm fetch for this long:
# later reads find the change again.
GAVE_UP_PAUSE: Final = timedelta(minutes=30)
# The history is read again this long after the last pass completed.
HISTORY_INTERVAL: Final = timedelta(hours=24)
# A lowering confirmed in the reader's window brings the next history pass
# forward (an entry moved to an older date is otherwise missing until the
# daily pass), but at most one such early pass starts per this.
EARLY_HISTORY_INTERVAL: Final = timedelta(hours=1)
# One request per this much history per pass. The pieces' inner boundaries
# are multiples of it since the Unix epoch, so the same bounds recur.
HISTORY_PIECE: Final = timedelta(days=90)
# The pause between two requests of one pass, in seconds.
REQUEST_PAUSE: Final = 1.0
# After a connection error, the next own request waits this long (the last
# delay repeats).
RETRY_DELAYS: Final = (timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
# Start-up passes in all before giving up until the next daily pass.
MAX_STARTUP_PASSES: Final = 3

# ---------------------------------------------------------------------------
# History and other limits
# ---------------------------------------------------------------------------

# The history starts this long before the earlier of birth and creation date.
HISTORY_BEFORE_BIRTH: Final = timedelta(days=365)
# Without either date, this long before now (moved down onto the piece grid,
# so the bounds recur from day to day).
HISTORY_FALLBACK: Final = timedelta(days=3 * 365)
# Never earlier than this, whatever the baby record says: it bounds the
# requests a wrong date could cause (Nanit's care logs are younger).
HISTORY_FLOOR: Final = datetime(2014, 1, 1, tzinfo=UTC)
# After a failed sync, the next one (a full sync) waits this long.
SYNC_RETRY_DELAY: Final = timedelta(minutes=15)
# Reader updates kept for the worker while it is busy, or for a start-up
# pass waiting for its second pass. Past this, the oldest is dropped and
# counted as an overflow: observations were lost.
READER_QUEUE: Final = 64
# An own answer with more entries than this is turned into a read off the
# event loop.
LARGE_READ: Final = 500

_PIECE_SECONDS: Final = int(HISTORY_PIECE.total_seconds())

# The fixed state names ``diagnostics`` reports.
STATE_IDLE: Final = "idle"
STATE_POLLING_DISABLED: Final = "polling_disabled"
STATE_WAITING_FOR_RECORDER: Final = "waiting_for_recorder"
STATE_WAITING_FOR_READER: Final = "waiting_for_reader"
STATE_STARTING: Final = "starting"
STATE_WAITING_FOR_SECOND_PASS: Final = "waiting_for_second_pass"
STATE_WAITING_TO_RETRY: Final = "waiting_to_retry"
STATE_GAVE_UP: Final = "gave_up"
STATE_RUNNING: Final = "running"
STATE_STOPPED: Final = "stopped"

type Bounds = tuple[int, int]
type CareLogFetcher = Callable[[int, int], Awaitable[CareLogFetch]]


# ---------------------------------------------------------------------------
# History bounds (pure)
# ---------------------------------------------------------------------------


def care_log_history_start(baby: Baby, now: datetime) -> int:
    """Return where a baby's care log history starts, in Unix seconds.

    One year before the earlier of the birth date (its UTC midnight) and the
    record's creation time. When neither is usable, three years before now,
    moved down onto the piece grid. Never before ``HISTORY_FLOOR``.
    """
    known: list[float] = []
    if baby.birth_date is not None:
        birth = baby.birth_date
        known.append(datetime(birth.year, birth.month, birth.day, tzinfo=UTC).timestamp())
    created = baby.created_at
    if created is not None and math.isfinite(created) and created > 0:
        known.append(created)
    if known:
        start = math.floor(min(known) - HISTORY_BEFORE_BIRTH.total_seconds())
    else:
        fallback = math.floor((now - HISTORY_FALLBACK).timestamp())
        start = fallback // _PIECE_SECONDS * _PIECE_SECONDS
    return max(start, int(HISTORY_FLOOR.timestamp()))


def care_log_history_pieces(start: int, end: int) -> tuple[Bounds, ...]:
    """Split ``[start, end]`` (inclusive Unix seconds) into history pieces, oldest first.

    Each piece spans at most ``HISTORY_PIECE`` (and its last second); the
    boundaries between pieces lie on multiples of it since the Unix epoch,
    so the same bounds recur on every pass. Consecutive pieces share their
    boundary second (``next.start == previous.end``): Nanit dates entries to
    a fraction of a second, so pieces that only touched would leave out an
    entry dated within the second before a boundary. The last ends at
    ``end``. Empty when ``end < start``.
    """
    pieces: list[Bounds] = []
    if end < start:
        return ()
    low = start
    while True:
        high = min((low // _PIECE_SECONDS + 1) * _PIECE_SECONDS, end)
        pieces.append((low, high))
        if high >= end:
            return tuple(pieces)
        low = high


# ---------------------------------------------------------------------------
# The importer
# ---------------------------------------------------------------------------


class _AuthFailedError(Exception):
    """Unwinds the worker after Nanit refused the sign-in."""


class _RoundLostReadsError(Exception):
    """Unwinds a start-up round after a reader read was lost to a full queue."""


def _merge_earliest(into: dict[CareLogSeries, int], other: Mapping[CareLogSeries, int]) -> None:
    for series, hour in other.items():
        current = into.get(series)
        if current is None or hour < current:
            into[series] = hour


def _taken_at(data: CareLogData) -> datetime:
    """Return when a queued reader update's answer arrived."""
    coverage = data.request_coverage
    assert coverage is not None  # only updates with coverage are queued
    return coverage.read_at


def _whole_minutes(since: datetime | None, now: datetime) -> int | None:
    if since is None:
        return None
    return max(0, int((now - since).total_seconds() // 60))


class CareLogStatisticsImporter:
    """Keep one baby's care log statistics equal to what Nanit holds.

    One instance per config entry and baby, and it assumes it is the only
    writer of this baby's statistics.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        *,
        baby: Baby,
        coordinator: NanitCareLogCoordinator,
        fetch: CareLogFetcher,
    ) -> None:
        """Prepare the importer; ``fetch(start, end)`` reads this baby's calendar."""
        self._hass = hass
        self._entry = entry
        self._baby = baby
        self._coordinator = coordinator
        self._fetch = fetch
        self._code = baby_code(baby.uid)
        self._reader = entry.entry_id
        self._book = CareLogBook(reader=self._reader, min_gap=MIN_GAP)

        self._task: asyncio.Task[None] | None = None
        self._unsub_listener: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._wake_event = asyncio.Event()
        # Set while the worker waits with nothing to do (tests wait on it).
        self._idle = asyncio.Event()
        self._stopping = False
        self._state = STATE_IDLE

        # Reader updates not yet taken by the worker, and the newest seen.
        self._last_handled_read_at: datetime | None = None
        self._reader_queue: deque[CareLogData] = deque()
        self._newest_reader: CareLogData | None = None
        # Set when a reader update was lost to a full queue, until handled;
        # the generation counts every loss, for a start-up round to check.
        self._overflowed = False
        self._overflow_generation = 0

        # Start-up. ``_keeping``: reader updates are kept for the start-up
        # passes (from the start of a first pass until the adoption).
        self._first_pass: tuple[CareLogRead, ...] | None = None
        self._first_pass_began: datetime | None = None
        self._keeping = False
        self._passes = 0
        self._startup_due: datetime | None = None

        # Running.
        self._reader_start: int | None = None
        # The start of the history the ledger was built from: the recorder
        # functions never touch rows before it.
        self._coverage_start: int | None = None
        self._piece_bounds: set[Bounds] = set()
        self._dirty: dict[CareLogSeries, int] = {}
        self._need_full = False
        self._sync_wanted = False
        self._sync_failing = False
        self._sync_retry_at: datetime | None = None
        self._last_confirm_at: datetime | None = None
        self._confirm_hold_until: datetime | None = None
        self._history_due: datetime | None = None
        self._early_requested = False
        self._last_early_at: datetime | None = None

        # Connection errors of own requests.
        self._failures = 0
        self._retry_at: datetime | None = None

        # Counters and times for diagnostics.
        self._passes_completed = 0
        self._confirm_fetches = 0
        self._give_ups = 0
        self._sync_failures = 0
        self._queue_overflows = 0
        self._last_sync_at: datetime | None = None
        self._last_history_at: datetime | None = None

    # -- lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Listen to the reader (which keeps it polling) and start the worker."""
        if self._task is not None or self._stopping:
            return
        self._unsub_listener = self._coordinator.async_add_listener(self._on_reader_update)
        # The reader has usually polled already; that answer counts as an update.
        self._take_reader_update()
        self._task = self._entry.async_create_background_task(
            self._hass, self._async_run(), "nanit care log statistics import"
        )

    async def async_stop(self) -> None:
        """Stop the worker, the listener and every timer; return once nothing runs."""
        self._stopping = True
        self._release()
        task = self._task
        if task is not None and not task.done():
            # A sync in flight sees ``should_stop`` first and sends nothing
            # more; the cancellation then ends whatever wait it is in, except
            # that a batch already sent is verified first, for at most
            # ``SETTLE_TIMEOUT`` (unverified, it leaves the baby unsettled).
            task.cancel()
            await asyncio.wait([task])
        self._state = STATE_STOPPED

    @callback
    def _release(self) -> None:
        """Remove the listener and cancel the timer."""
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None
        self._cancel_timer()

    def _should_stop(self) -> bool:
        return self._stopping

    # -- callbacks ------------------------------------------------------------

    @callback
    def _on_reader_update(self) -> None:
        if self._take_reader_update():
            self._wake()

    @callback
    def _take_reader_update(self) -> bool:
        """Queue the reader's data when it is a new, successful read."""
        coordinator = self._coordinator
        data = coordinator.data
        if not coordinator.last_update_success or data is None:
            return False
        coverage = data.request_coverage
        if coverage is None:
            return False
        last = self._last_handled_read_at
        if last is not None and coverage.read_at <= last:
            return False
        self._last_handled_read_at = coverage.read_at
        self._newest_reader = data
        if self._book.established or self._keeping:
            queue = self._reader_queue
            if len(queue) >= READER_QUEUE:
                queue.popleft()
                self._queue_overflows += 1
                self._overflowed = True
                self._overflow_generation += 1
            queue.append(data)
        # Otherwise no pass waits for it: the next first pass starts from
        # the newest, which is kept above.
        return True

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._unsub_timer = None
        self._wake()

    @callback
    def _wake(self) -> None:
        self._idle.clear()
        self._wake_event.set()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    # -- the worker -----------------------------------------------------------

    async def _async_run(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self._async_step()
                except _AuthFailedError:
                    # Nanit refused the sign-in: Home Assistant asks for it again.
                    self._entry.async_start_reauth(self._hass)
                    return
                except Exception as err:  # noqa: BLE001
                    # Logged by type only: the text could hold anything.
                    _LOGGER.error(
                        "Care log statistics import step failed unexpectedly (%s); "
                        "trying again later",
                        type(err).__name__,
                    )
                    self._after_unexpected_error()
                if self._stopping:
                    return
                self._schedule_timer()
                if not self._wake_event.is_set():
                    self._idle.set()
                await self._wake_event.wait()
                self._wake_event.clear()
                self._idle.clear()
        finally:
            self._stopping = True
            self._release()
            self._state = STATE_STOPPED
            self._idle.set()

    def _after_unexpected_error(self) -> None:
        now = dt_util.utcnow()
        self._need_full = True
        self._sync_wanted = self._book.established
        self._note_connection_failure(now)
        if not self._book.established:
            self._startup_due = self._retry_at

    async def _async_step(self) -> None:
        if not await self._async_settled():
            return
        if self._book.established:
            await self._async_running_step()
        else:
            await self._async_starting_step()

    async def _async_settled(self) -> bool:
        """Return whether the stored rows can be relied on (see the module docstring)."""
        if not care_log_unsettled(self._hass, self._code):
            return True
        if not self._recorder_ready() or not await async_settle_care_log_statistics(
            self._hass, self._code
        ):
            # Tried again at the next reader update.
            self._state = STATE_WAITING_FOR_RECORDER
            return False
        if self._book.established:
            # Whatever landed meanwhile is compared by a full sync.
            self._need_full = True
            self._sync_wanted = True
        return True

    def _polling_disabled(self) -> bool:
        return self._entry.pref_disable_polling

    def _recorder_ready(self) -> bool:
        return care_log_recorder_ready(self._hass)

    def _end_hour(self) -> int:
        return hour_start(dt_util.utcnow().timestamp())

    # -- timers ---------------------------------------------------------------

    def _next_due(self) -> datetime | None:
        """Return when the worker next has something to do without a reader update.

        Nothing while the recorder is not ready or the baby is unsettled:
        every step needs it, and a deadline already passed would otherwise
        wake the worker every second. The next reader update looks again.
        """
        if not self._recorder_ready() or care_log_unsettled(self._hass, self._code):
            return None
        dues: list[datetime] = []
        if self._book.established:
            if self._sync_wanted and self._sync_retry_at is not None:
                dues.append(self._sync_retry_at)
            if not self._polling_disabled():
                confirm = self._confirm_due()
                if confirm is not None:
                    dues.append(confirm[1])
                if self._history_due is not None:
                    dues.append(self._own_request_time(self._history_due))
        elif self._startup_due is not None and not self._polling_disabled():
            dues.append(self._startup_due)
        return min(dues) if dues else None

    def _schedule_timer(self) -> None:
        self._cancel_timer()
        due = self._next_due()
        if due is None:
            return
        # Never in the past: what is due now was just done or cannot be.
        due = max(due, dt_util.utcnow() + timedelta(seconds=1))
        self._unsub_timer = async_track_point_in_utc_time(self._hass, self._on_timer, due)

    def _own_request_time(self, due: datetime) -> datetime:
        """Return ``due``, or later while own requests wait after a connection error."""
        if self._retry_at is not None and self._retry_at > due:
            return self._retry_at
        return due

    def _note_connection_failure(self, now: datetime) -> None:
        delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
        self._failures += 1
        self._retry_at = now + delay

    def _note_connection_success(self) -> None:
        self._failures = 0
        self._retry_at = None

    # -- own reads -------------------------------------------------------------

    async def _async_own_read(self, start: int, end: int) -> CareLogRead:
        """Read some bounds with a request of the importer's own."""
        try:
            result = await self._fetch(start, end)
        except NanitAuthError as err:
            raise _AuthFailedError from err
        read_at = dt_util.utcnow()
        if len(result.logs) > LARGE_READ:
            return await self._hass.async_add_executor_job(self._read_from_fetch, result, read_at)
        return self._read_from_fetch(result, read_at)

    def _read_from_fetch(self, result: CareLogFetch, read_at: datetime) -> CareLogRead:
        return CareLogRead.from_logs(
            result.logs,
            start=result.start,
            end=result.end,
            read_at=read_at,
            dropped=result.dropped,
            reader=self._reader,
        )

    def _read_from_reader(self, data: CareLogData) -> CareLogRead:
        coverage = data.request_coverage
        assert coverage is not None
        return CareLogRead.from_logs(
            data.all_entries,
            start=coverage.start,
            end=coverage.end,
            read_at=coverage.read_at,
            dropped=coverage.dropped,
            reader=self._reader,
        )

    async def _async_pause(self, index: int) -> None:
        """Pause between the requests of one pass (not before the first)."""
        if index:
            await asyncio.sleep(REQUEST_PAUSE)

    # -- starting up ------------------------------------------------------------

    async def _async_starting_step(self) -> None:
        if self._polling_disabled():
            self._state = STATE_POLLING_DISABLED
            return
        if not self._recorder_ready():
            self._state = STATE_WAITING_FOR_RECORDER
            return
        if self._overflowed and self._first_pass is not None:
            self._start_over()
        now = dt_util.utcnow()
        if self._startup_due is not None and now < self._startup_due:
            return
        try:
            if self._first_pass is None:
                await self._async_first_pass()
            else:
                await self._async_second_pass(self._first_pass)
        except _RoundLostReadsError:
            # A reader read taken during this round was lost: what it said is
            # unknown, so the round cannot be trusted. It starts over.
            self._start_over()
        except NanitConnectionError:
            self._note_connection_failure(dt_util.utcnow())
            self._startup_due = self._retry_at
            self._state = STATE_WAITING_TO_RETRY
            _LOGGER.debug("A care log history pass could not reach Nanit; trying again later")
        except CareLogRecorderUnavailableError:
            # Tried again once the recorder is back, at a later reader update.
            self._state = STATE_WAITING_FOR_RECORDER
            self._startup_due = None
        except CareLogSyncError:
            # The comparison could not read the stored rows.
            self._note_sync_failure()
            self._startup_due = self._sync_retry_at
            self._state = STATE_WAITING_TO_RETRY
        if self._first_pass is None and not self._book.established:
            # No pass waits for the reader's reads: the next first pass
            # starts from the newest.
            self._keeping = False
            self._reader_queue.clear()

    async def _async_first_pass(self) -> None:
        newest = self._newest_reader
        if newest is None:
            self._state = STATE_WAITING_FOR_READER
            return
        self._state = STATE_STARTING
        # From here on, every reader read is kept for this round.
        self._reader_queue.clear()
        self._overflowed = False
        self._keeping = True
        generation = self._overflow_generation
        reader_read = self._read_from_reader(newest)
        history_start = care_log_history_start(self._baby, dt_util.utcnow())
        # The last piece shares the reader read's first second.
        pieces = care_log_history_pieces(history_start, reader_read.start)
        reads: list[CareLogRead] = []
        for index, (start, end) in enumerate(pieces):
            await self._async_pause(index)
            self._check_round(generation)
            reads.append(await self._async_own_read(start, end))
            self._check_round(generation)
        reads.append(reader_read)
        self._note_connection_success()
        self._passes_completed += 1
        self._passes = 1

        candidate: Ledger = await self._hass.async_add_executor_job(self._book.candidate, reads)
        self._check_round(generation)
        comparison = await async_compare_care_log_statistics(
            self._hass,
            code=self._code,
            ledger=candidate,
            end_hour=self._end_hour(),
            coverage_start=reads[0].start,
        )
        self._check_round(generation)
        if comparison.lowering_cells == 0:
            await self._async_adopt(reads, candidate)
            return
        del candidate
        # It would lower a stored cell: nothing is written until a second
        # pass of the same bounds agrees.
        self._wait_for_next_pass(reads, reader_read.read_at)

    async def _async_second_pass(self, first: Sequence[CareLogRead]) -> None:
        self._state = STATE_STARTING
        generation = self._overflow_generation
        began = dt_util.utcnow()
        reads: list[CareLogRead] = []
        for index, read in enumerate(first):
            await self._async_pause(index)
            self._check_round(generation)
            reads.append(await self._async_own_read(read.start, read.end))
            self._check_round(generation)
        self._note_connection_success()
        self._passes_completed += 1
        self._passes += 1
        since = self._first_pass_began
        seen = [
            read
            for read in map(self._read_from_reader, self._reader_queue)
            if since is None or read.read_at >= since
        ]
        agree: bool = await self._hass.async_add_executor_job(
            self._book.passes_agree, first, reads, seen
        )
        self._check_round(generation)
        if agree:
            candidate: Ledger = await self._hass.async_add_executor_job(self._book.candidate, reads)
            self._check_round(generation)
            await self._async_adopt(reads, candidate)
            return
        if self._passes >= MAX_STARTUP_PASSES:
            _LOGGER.warning(
                "Care log history reads kept disagreeing; leaving the statistics as they "
                "are until the next daily pass"
            )
            self._give_ups += 1
            self._first_pass = None
            self._first_pass_began = None
            self._keeping = False
            self._reader_queue.clear()
            self._passes = 0
            self._startup_due = dt_util.utcnow() + HISTORY_INTERVAL
            self._state = STATE_GAVE_UP
            return
        self._wait_for_next_pass(reads, began)

    def _wait_for_next_pass(self, reads: Sequence[CareLogRead], began: datetime) -> None:
        """Make ``reads`` the first pass of the next round; it began at ``began``."""
        self._first_pass = tuple(reads)
        since = min(began, *(read.read_at for read in reads))
        self._first_pass_began = since
        # Only the reads kept since this round's first pass began count as
        # sightings; older ones are dropped, never replayed.
        self._reader_queue = deque(data for data in self._reader_queue if _taken_at(data) >= since)
        self._startup_due = max(read.read_at for read in reads) + MIN_GAP
        self._state = STATE_WAITING_FOR_SECOND_PASS

    def _check_round(self, generation: int) -> None:
        """Raise ``_RoundLostReadsError`` when a reader read was lost since ``generation``."""
        if self._overflow_generation != generation:
            raise _RoundLostReadsError

    def _start_over(self) -> None:
        """Forget a waiting start-up pass: the next step starts a new first pass."""
        self._first_pass = None
        self._first_pass_began = None
        self._keeping = False
        self._reader_queue.clear()
        self._overflowed = False
        self._passes = 0
        self._startup_due = None
        self._wake()

    async def _async_adopt(self, reads: Sequence[CareLogRead], candidate: Ledger) -> None:
        """Adopt a pass whose candidate was built (no wait before the book is changed)."""
        self._book.adopt_candidate(candidate)
        # Older evidence never overrides newer: a kept read taken no later
        # than the pass's latest read is discarded, whatever it holds.
        latest = max(read.read_at for read in reads)
        self._reader_queue = deque(data for data in self._reader_queue if _taken_at(data) > latest)
        now = dt_util.utcnow()
        self._first_pass = None
        self._first_pass_began = None
        self._keeping = False
        self._passes = 0
        self._startup_due = None
        self._reader_start = reads[-1].start
        self._coverage_start = reads[0].start
        self._piece_bounds = {(read.start, read.end) for read in reads[:-1]}
        self._dirty.clear()
        self._need_full = True
        self._sync_wanted = True
        self._last_history_at = now
        self._history_due = now + HISTORY_INTERVAL
        self._state = STATE_RUNNING
        # The reader reads kept since the pass's latest read, in order.
        self._process_reads()
        await self._async_sync()

    # -- running ------------------------------------------------------------------

    async def _async_running_step(self) -> None:
        self._state = STATE_RUNNING
        self._process_reads()
        if not self._recorder_ready():
            # Reads still update the book; nothing is asked of Nanit or written
            # until the recorder is back.
            return
        await self._async_sync()
        if self._polling_disabled() or not self._recorder_ready():
            return
        try:
            confirm = self._confirm_due()
            if confirm is not None and confirm[1] <= dt_util.utcnow():
                await self._async_confirm(confirm[0])
            if not self._recorder_ready():
                return
            if self._history_due is not None and (
                self._own_request_time(self._history_due) <= dt_util.utcnow()
            ):
                await self._async_history_pass()
        except NanitConnectionError:
            self._note_connection_failure(dt_util.utcnow())
            _LOGGER.debug("A care log read could not reach Nanit; trying again later")

    def _process_reads(self, *own: CareLogRead) -> None:
        """Give the book the importer's own reads and every queued reader read, oldest first.

        None is dropped for being older than another. After a queue
        overflow, every waiting change is forgotten first: reads were lost,
        so no record may be confirmed across the gap.
        """
        if self._overflowed:
            self._overflowed = False
            self._book.drop_all_pending()
        reader = [self._read_from_reader(data) for data in self._reader_queue]
        self._reader_queue.clear()
        for read in reader:
            self._reader_start = read.start  # oldest first: the newest stays
        for read in sorted([*own, *reader], key=lambda read: read.read_at):
            self._process(read)

    def _process(self, read: CareLogRead) -> Outcome:
        """Give the book one read, and note what it changed."""
        outcome = self._book.process(read)
        _merge_earliest(self._dirty, outcome.changed_from)
        self._sync_wanted = True
        if outcome.gave_up:
            self._give_ups += 1
            self._confirm_hold_until = dt_util.utcnow() + GAVE_UP_PAUSE
        if outcome.lowered and (read.start, read.end) not in self._piece_bounds:
            self._bring_history_forward()
        return outcome

    def _bring_history_forward(self) -> None:
        now = dt_util.utcnow()
        if self._last_early_at is None or now - self._last_early_at >= EARLY_HISTORY_INTERVAL:
            soonest = now
        else:
            soonest = self._last_early_at + EARLY_HISTORY_INTERVAL
        if self._history_due is None or soonest < self._history_due:
            self._history_due = soonest
            self._early_requested = True

    def _confirm_due(self) -> tuple[ConfirmRequest, datetime] | None:
        """Return the next re-read the book asks for, and when it may be made."""
        requests = self._book.pending_requests()
        if not requests:
            return None
        request = min(requests, key=lambda one: one.not_before)
        due = request.not_before
        if self._last_confirm_at is not None:
            due = max(due, self._last_confirm_at + CONFIRM_INTERVAL)
        if self._confirm_hold_until is not None:
            due = max(due, self._confirm_hold_until)
        return request, self._own_request_time(due)

    async def _async_confirm(self, request: ConfirmRequest) -> None:
        self._last_confirm_at = dt_util.utcnow()
        self._confirm_fetches += 1
        read = await self._async_own_read(request.start, request.end)
        self._note_connection_success()
        self._process_reads(read)
        await self._async_sync()

    async def _async_history_pass(self) -> None:
        if self._early_requested:
            self._early_requested = False
            self._last_early_at = dt_util.utcnow()
        reader_start = self._reader_start
        assert reader_start is not None
        history_start = care_log_history_start(self._baby, dt_util.utcnow())
        pieces = care_log_history_pieces(history_start, reader_start)
        # Confirm requests for yesterday's last piece still count as history.
        self._piece_bounds = {*self._piece_bounds, *pieces}
        for index, (start, end) in enumerate(pieces):
            await self._async_pause(index)
            read = await self._async_own_read(start, end)
            self._process_reads(read)
        self._note_connection_success()
        # The history this pass read: from now on the rows the ledger covers.
        self._coverage_start = min(history_start, reader_start)
        now = dt_util.utcnow()
        self._passes_completed += 1
        self._last_history_at = now
        self._history_due = now + HISTORY_INTERVAL
        # Older piece bounds stay known only while a confirm request uses them.
        waiting = {(request.start, request.end) for request in self._book.pending_requests()}
        self._piece_bounds = set(pieces) | (self._piece_bounds & waiting)
        if await self._async_sync():
            # A second check of every stored row.
            self._need_full = True
            self._sync_wanted = True
            await self._async_sync()

    # -- syncing -----------------------------------------------------------------

    async def _async_sync(self) -> bool:
        """Write what the ledger changed; return whether the rows are now up to date."""
        if not self._sync_wanted:
            return True
        now = dt_util.utcnow()
        if self._sync_retry_at is not None and now < self._sync_retry_at:
            return False
        names = await async_statistic_names(
            self._hass, statistic_baby_name(self._baby.name, self._code)
        )
        changed_from = None if self._need_full else dict(self._dirty)
        coverage_start = self._coverage_start
        assert coverage_start is not None  # set when the ledger was adopted
        try:
            report = await async_sync_care_log_statistics(
                self._hass,
                code=self._code,
                names=names,
                ledger=self._book.ledger,
                end_hour=self._end_hour(),
                coverage_start=coverage_start,
                changed_from=changed_from,
                should_stop=self._should_stop,
            )
        except CareLogRecorderUnavailableError:
            # Tried again at a later wake-up, once the recorder is back.
            return False
        except CareLogSyncError:
            self._note_sync_failure()
            return False
        if report.stopped:
            return False
        self._last_sync_at = dt_util.utcnow()
        self._dirty.clear()
        self._need_full = False
        self._sync_wanted = False
        self._sync_failing = False
        self._sync_retry_at = None
        return True

    def _note_sync_failure(self) -> None:
        """Count a failed sync, warn once per streak, and make the next one full, later."""
        self._sync_failures += 1
        if not self._sync_failing:
            _LOGGER.warning(
                "Care log statistics could not be read or written; trying again with a "
                "full sync in %d minutes",
                int(SYNC_RETRY_DELAY.total_seconds() // 60),
            )
        self._sync_failing = True
        self._need_full = True
        self._sync_retry_at = dt_util.utcnow() + SYNC_RETRY_DELAY

    # -- diagnostics ---------------------------------------------------------------

    def diagnostics(self) -> dict[str, object]:
        """Return the state, flags, operational counters and ages.

        Nothing about the baby's day: no count of entries, rows or waiting
        changes, only whether a change waits for a confirming read.
        """
        stats = self._book.stats()
        now = dt_util.utcnow()
        return {
            "state": self._state,
            "established": stats.established,
            "waiting_for_confirmation": stats.pending_records > 0,
            "passes_completed": self._passes_completed,
            "confirm_fetches": self._confirm_fetches,
            "give_ups": self._give_ups,
            "sync_failures": self._sync_failures,
            "queue_overflows": self._queue_overflows,
            "minutes_since_last_sync": _whole_minutes(self._last_sync_at, now),
            "minutes_since_last_history_pass": _whole_minutes(self._last_history_at, now),
        }
