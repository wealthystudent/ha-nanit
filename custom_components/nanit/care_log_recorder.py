"""Write the care log long-term statistics so the stored rows equal the ledger.

The ledger (``care_log_ledger``) says what each finished UTC hour of each
series gained; this module makes the recorder's rows say the same, in the
format ``care_log_statistics`` defines, and checks every write by reading
it back. Nothing here schedules itself: a caller runs a sync when the
ledger changed or an hour finished.

The rows of a series, for one baby:

- Only hours the ledger's history covers are compared or written. The
  caller gives ``coverage_start``, the start of the history the ledger was
  built from; rounded up to an hour it is the coverage hour. Rows before
  it (the prefix) are never written, and never counted as lowering,
  raising or missing: the ledger knows nothing about them, so an entry
  that only they hold is not treated as deleted.
- They start at the series' first hour: the earlier of the ledger's first
  hour for it and the first row already stored, both at or after the
  coverage hour. Rows already stored keep the start where it was, so a
  deleted first entry leaves a first row of 0; an entry backdated before
  the first row moves the start back. A series with no entry and no stored
  row gets no statistic at all. An established series, one with a stored
  row before the coverage hour, starts at the coverage hour, whether or not
  the ledger has an entry for it there: Home Assistant's single-period
  change query takes the row of the hour before a period as its baseline,
  so a gap after the prefix would read the first total after it as both
  baseline and endpoint.
- They are dense from there up to, not including, ``end_hour`` (the hour in
  progress): a row for every hour, 0 for an hour with nothing. Rows at or
  after ``end_hour`` are never read or written.
- Each row's ``state`` is the ledger's amount for the hour and its ``sum``
  the running total, with one exception: in a keep cell (an hour holding a
  rejected amount, ``Ledger.keep_hours``) a stored ``state`` is kept as it
  is, and the running total uses it. The running total continues from the
  ``sum`` of the latest stored row before the series' first hour, wherever
  that row is (0 when there is none), so a prefix keeps its totals.
- Two cases cannot be made exact. An entry already counted in the prefix
  that is re-dated to a time after the coverage hour is counted again,
  since the prefix is never lowered. It can only happen when the history
  start moves later: when Nanit gives neither a birth date nor a creation
  date and the three-year fallback start moves on, or when the birth date
  Nanit gives is changed to a later one. And an entry counted after the
  coverage hour that is re-dated to before it is taken out of the covered
  rows but not added to the prefix, which is never raised either.

A sync compares stored rows with these targets, a chunk of at most
``CHUNK_HOURS`` hours at a time, and sends only the rows that differ by
more than ``TOLERANCE`` (absolute, in the series' unit, on ``state`` and on
``sum``; see ``_differs``). A batch is at most ``BATCH_ROWS`` rows and never
spans more than one chunk. After each batch the recorder is waited for and
the batch is read back; it counts as written only when the stored rows
equal what was sent. Every write is safe to repeat: the next sync finds what
still differs and continues from there.

A batch ends in one of three ways, kept apart:

- verified: the stored rows equal what was sent;
- failed before submission: the recorder refused it, nothing was sent
  (``CareLogSyncError``);
- submitted with its outcome unknown: it was sent, but the read-back did
  not match, failed, took longer than ``SETTLE_TIMEOUT`` in all, or was cut
  short by a cancellation (``CareLogWriteUnknownError``, or the
  cancellation itself). It may still land later.

Either failure stops the sync, and nothing after the batch is sent. An
unknown outcome also pauses the baby for the rest of this Home Assistant
run (in ``hass.data``, so it outlasts the importer, a reload and the writer
registry), with one warning per baby and run. Nothing but a restart ends
the pause: the recorder dequeues a task before it runs it, and an import
task that hit a retryable error queues itself again behind whatever was
queued meanwhile, so neither an empty queue nor one that drained proves
that a batch sent earlier will not still land. While a baby is paused
nothing relies on its stored rows or writes them:
``async_has_care_log_statistics`` cannot tell, and a sync or a comparison
raises ``CareLogPausedError``. A failure before submission does not pause.

A comparison first waits, at most ``SETTLE_TIMEOUT``, for the recorder to
work through what it has queued, so a batch a previous importer had
verified is compared as stored; when the queue does not drain in time it
raises ``CareLogQueueBusyError`` and compares nothing (nothing was sent, so
nothing is paused).

Finding a series' first stored row: Home Assistant has no public query for
it, and the rows need not be dense: a backfill interrupted after its first
batch, of an entry later withdrawn, leaves rows separated from the rest by
a gap of any length. So the first stored row is found by reading forward
from the coverage hour to the series' first known hour (the ledger's, or
the hour after the last stored row, whichever is earlier), one chunk at a
time, not stopping at an empty chunk, until a chunk holds a row. Every row
from there on is then compared, and corrected, whatever gap lies before the
rest. In the usual case, nothing stored before the ledger's first hour,
that is one read of an empty chunk per ``CHUNK_HOURS`` between the coverage
hour and the first entry (a year before birth: four). All of it runs in one
executor job, holding at most one chunk of rows.

Finding the total to continue from: the ``sum`` of the latest stored row
before the coverage hour (when there is one, the series is established
and that is its total) comes from Home Assistant's public
``statistic_during_period`` with no start and the coverage hour as its end,
asking only for ``change``: that is the newest stored ``sum`` before the
end minus 0, one query that orders by time and reads one row, in the
recorder's executor. The prefix itself is never loaded.

Event loop and memory: every database read, and turning its rows into plain
values, runs in the recorder's executor. On the loop, one chunk is compared
at a time, with ``asyncio.sleep(0)`` between chunks. At most one chunk of
one series' stored rows, and one batch of rows to send, are held at once.

Privacy: log lines, exception text and reprs here hold no uid, baby id,
baby name or baby code. The code goes only into statistic ids, which Home
Assistant needs, and the name only into the statistic names.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistic_during_period,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.util.hass_dict import HassKey
from sqlalchemy.exc import SQLAlchemyError

from .care_log_hours import HOUR_SECONDS, CareLogSeries, hour_start
from .care_log_ledger import Ledger
from .care_log_statistics import statistic_id, statistic_metadata
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RECORDER_DOMAIN = "recorder"

# The most rows sent in one call to async_add_external_statistics.
BATCH_ROWS = 2500
# The most hours compared on the event loop in one go, and read in one query.
CHUNK_HOURS = 2500
# How far a stored value may be from its target and still count as equal,
# absolute, in the series' unit (count, mL or minutes). Used for state and
# sum alike, when comparing and when verifying a write.
TOLERANCE = 1e-6
# How long to wait for the recorder to work through its queue after a batch.
WRITE_TIMEOUT = 30.0
# How many times a batch is read back before it counts as failed, and the
# pause before the second read, doubled before each read after it (0.1 s
# up to 6.4 s, 12.7 s in all). Each read-back first waits for a task of its
# own to come through the recorder's queue (``_async_caught_up``), so it
# runs after the task the recorder is running. The pauses are needed as
# well: an import task that hit a retryable database error sleeps (3 s by
# default) and queues itself again behind that waiting task, so only the
# next read-back's wait comes after the retry.
VERIFY_ATTEMPTS = 8
VERIFY_RETRY_DELAY = 0.1
# The most time spent, in all, verifying a batch from when it was sent
# (every wait and read-back, an executor read included: the wait for it
# ends, the read itself cannot be cancelled), and the most time the wait
# for the recorder's queue before a comparison takes. A minute: more than
# six times what a verification normally needs (12.7 s of pauses at most,
# plus reads), and well inside the 100 s Home Assistant gives the stop
# stage, in which an unload at shutdown waits for it.
SETTLE_TIMEOUT = 60.0

# The codes of the babies paused for the rest of this run: a batch was sent
# and its outcome is unknown.
PAUSED_KEY: HassKey[set[str]] = HassKey(f"{DOMAIN}_care_log_paused")

type _Row = tuple[float | None, float | None]  # stored (state, sum)


class CareLogStatisticsError(HomeAssistantError):
    """Base class for the care log statistics errors."""


class CareLogRecorderUnavailableError(CareLogStatisticsError):
    """The recorder is not loaded, its database is not ready, or it is not recording."""

    def __init__(self) -> None:
        """Use a fixed message."""
        super().__init__("The recorder is not available for care log statistics")


class CareLogQueueBusyError(CareLogRecorderUnavailableError):
    """The recorder did not work through its queue in time before a comparison."""

    def __init__(self) -> None:
        """Use a fixed message."""
        CareLogStatisticsError.__init__(self, "The recorder did not work through its queue in time")


class CareLogPausedError(CareLogStatisticsError):
    """A batch sent in this run is unverified: nothing is read or written until a restart."""

    def __init__(self) -> None:
        """Use a fixed message."""
        super().__init__("The care log statistics import is paused until Home Assistant restarts")


class CareLogSyncError(CareLogStatisticsError):
    """A batch of care log statistics could not be written and verified."""


class CareLogWriteUnknownError(CareLogSyncError):
    """A batch of care log statistics was sent, but whether it was stored is not known."""


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync did, numbers only.

    ``rows_read`` counts the stored rows read to compare (the read-backs that
    verify a batch are not counted). ``rows_written`` counts rows sent and
    verified, in ``batches`` calls. ``stopped`` says ``should_stop`` ended
    the sync early.
    """

    series_examined: int
    rows_read: int
    rows_written: int
    batches: int
    stopped: bool


@dataclass(frozen=True, slots=True)
class Comparison:
    """How the stored rows of one baby differ from its ledger, numbers only.

    Over every hour from each series' first hour up to ``end_hour`` (the
    prefix before the coverage hour is never counted):
    ``lowering_cells`` are stored rows whose state is above the ledger's
    amount (keep cells excepted), ``raising_cells`` stored rows whose state
    is below it (keep cells excepted), ``missing_rows`` hours with no stored
    row, and ``rows_to_write`` the rows a sync would send (a missing row, or
    a state or sum that differs). A stored state that is empty counts as 0.
    """

    lowering_cells: int
    raising_cells: int
    missing_rows: int
    rows_to_write: int
    rows_read: int


def _database_ready(hass: HomeAssistant) -> bool:
    """Whether the recorder is loaded, its database ready and no migration running."""
    if RECORDER_DOMAIN not in hass.config.components:
        return False
    instance = hass.data.get(DATA_INSTANCE)
    if instance is None:
        return False
    ready = instance.async_db_ready
    if not ready.done() or ready.cancelled() or not ready.result():
        return False
    return not instance.migration_in_progress


def care_log_recorder_ready(hass: HomeAssistant) -> bool:
    """Whether the recorder is loaded, its database ready, and it is recording.

    The same test on every supported Home Assistant version: the integration
    is set up, its instance exists, the database was marked ready (not
    failed), no migration is running, its event listener is in place (it is
    removed when the recorder stops or gives up on a backlog), and it is
    enabled (the ``recorder.disable`` action turns that off and leaves the
    listener in place).
    """
    if not _database_ready(hass):
        return False
    instance = hass.data[DATA_INSTANCE]
    return bool(instance.recording and instance.enabled)


def _require_ready(hass: HomeAssistant) -> None:
    if not care_log_recorder_ready(hass):
        raise CareLogRecorderUnavailableError


def _require_database(hass: HomeAssistant) -> None:
    if not _database_ready(hass):
        raise CareLogRecorderUnavailableError


def care_log_paused(hass: HomeAssistant, code: str) -> bool:
    """Whether a baby's import is paused until Home Assistant restarts (see the module docstring)."""
    return code in hass.data.get(PAUSED_KEY, ())


def _pause(hass: HomeAssistant, code: str) -> None:
    """Pause a baby for the rest of this run; warn the first time."""
    paused = hass.data.setdefault(PAUSED_KEY, set())
    if code in paused:
        return
    paused.add(code)
    _LOGGER.warning(
        "A write of care log statistics could not be confirmed; the import for that baby "
        "is paused and resumes after Home Assistant restarts"
    )


def _require_not_paused(hass: HomeAssistant, code: str) -> None:
    if care_log_paused(hass, code):
        raise CareLogPausedError


async def _async_caught_up(hass: HomeAssistant, timeout: float) -> bool:
    """Wait, at most ``timeout``, until the recorder ran what it has queued and is running.

    Home Assistant's own wait (``async_block_till_done``) returns at once
    when the queue is empty, even while the recorder still runs a task it
    took off the queue. A task queued here runs only after that one and
    everything queued before it. Return whether it ran in time.
    """
    future: asyncio.Future[None] = hass.loop.create_future()
    get_instance(hass).queue_task(SynchronizeTask(future))
    try:
        async with asyncio.timeout(timeout):
            await future
    except TimeoutError:
        return False
    return True


async def async_care_log_queue_drained(hass: HomeAssistant) -> bool:
    """Wait, at most ``SETTLE_TIMEOUT``, for the recorder's queue; return whether it drained.

    It says nothing about a batch whose outcome is unknown: one that hit a
    retryable error can queue itself again later (see the module docstring).
    """
    return await _async_caught_up(hass, SETTLE_TIMEOUT)


def _read_any_row(hass: HomeAssistant, sids: list[str]) -> bool:
    return any(get_last_statistics(hass, 1, sid, False, {"sum"}).get(sid) for sid in sids)


async def async_has_care_log_statistics(hass: HomeAssistant, code: str) -> bool | None:
    """Whether any of a baby's statistics has a stored row; None when that cannot be told.

    None when the baby is paused (a batch may still land), the recorder is
    not ready, or the read failed or took longer than ``WRITE_TIMEOUT``.
    One newest-row query per series, in the recorder's executor.
    """
    if care_log_paused(hass, code) or not care_log_recorder_ready(hass):
        return None
    sids = [statistic_id(code, series) for series in CareLogSeries]
    try:
        async with asyncio.timeout(WRITE_TIMEOUT):
            found: bool = await get_instance(hass).async_add_executor_job(_read_any_row, hass, sids)
    except (TimeoutError, SQLAlchemyError):
        return None
    return found


def _utc(hour: int) -> datetime:
    return datetime.fromtimestamp(hour, UTC)


def _differs(stored: float | None, target: float) -> bool:
    """Whether a stored value is not within ``TOLERANCE`` of its target.

    Written so an empty value, or one that is not a number, always differs.
    """
    return stored is None or not abs(stored - target) <= TOLERANCE


# ---------------------------------------------------------------------------
# Reads (run in the recorder's executor)
# ---------------------------------------------------------------------------


def _read_rows(hass: HomeAssistant, sid: str, start: int, end: int) -> dict[int, _Row]:
    """Return the stored rows of one statistic in ``[start, end)``, by hour."""
    found = statistics_during_period(
        hass, _utc(start), _utc(end), {sid}, "hour", None, {"state", "sum"}
    )
    return {int(row["start"]): (row.get("state"), row.get("sum")) for row in found.get(sid, [])}


def _read_last(hass: HomeAssistant, sid: str) -> tuple[int, float | None] | None:
    """Return the hour and sum of the newest stored row, or None."""
    found = get_last_statistics(hass, 1, sid, False, {"sum"})
    rows = found.get(sid)
    if not rows:
        return None
    return int(rows[0]["start"]), rows[0].get("sum")


def _read_first_from(hass: HomeAssistant, sid: str, low: int, high: int) -> tuple[int | None, int]:
    """Read forward through ``[low, high)`` a chunk at a time; return the first row's hour and rows read.

    Empty chunks do not stop it: it ends at the first chunk that holds a
    row, or at ``high``. The hour is None when no row is stored there.
    """
    step = CHUNK_HOURS * HOUR_SECONDS
    for chunk in range(low, high, step):
        rows = _read_rows(hass, sid, chunk, min(chunk + step, high))
        if rows:
            return min(rows), len(rows)
    return None, 0


def _read_sum_before(hass: HomeAssistant, sid: str, hour: int) -> float | None:
    """Return the ``sum`` of the newest stored row before ``hour``, or None.

    ``change`` with no start is the newest stored sum before the end, less
    0: one row, read by time, however many rows lie before it.
    """
    found = statistic_during_period(hass, None, _utc(hour), sid, {"change"}, None)
    change = found.get("change")
    return None if change is None else float(change)


def coverage_hour(coverage_start: int) -> int:
    """Return the coverage start, in Unix seconds, rounded up to the start of an hour."""
    return -(-coverage_start // HOUR_SECONDS) * HOUR_SECONDS


# ---------------------------------------------------------------------------
# The targets
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Cell:
    hour: int
    stored: _Row | None
    amount: float
    state: float
    total: float
    keep: bool


def _cells(
    ledger: Ledger,
    series: CareLogSeries,
    start: int,
    end: int,
    stored: Mapping[int, _Row],
    total: float,
) -> Iterator[_Cell]:
    """Yield the target of every hour in ``[start, end)``, the total continuing from ``total``."""
    amounts = ledger.series_hours(series, start, end)
    keep = ledger.keep_hours(series, start, end)
    for hour in range(start, end, HOUR_SECONDS):
        amount = amounts.get(hour, 0.0)
        row = stored.get(hour)
        stored_state = None if row is None else row[0]
        kept = hour in keep and stored_state is not None
        state = stored_state if kept and stored_state is not None else amount
        total += state
        yield _Cell(hour=hour, stored=row, amount=amount, state=state, total=total, keep=kept)


def _needs_write(cell: _Cell) -> bool:
    row = cell.stored
    return row is None or _differs(row[0], cell.state) or _differs(row[1], cell.total)


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


class _StopRequestedError(Exception):
    """Unwinds a sync when ``should_stop`` says so."""


class _Reader:
    """Database reads for one baby, counted."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.rows_read = 0

    async def job[T](self, target: Callable[..., T], *args: object, recording: bool = True) -> T:
        """Run a read in the recorder's executor.

        It needs the recorder ready to record; a read that checks a batch
        already sent (``recording=False``) needs only the database.
        """
        if recording:
            _require_ready(self.hass)
        else:
            _require_database(self.hass)
        try:
            return await get_instance(self.hass).async_add_executor_job(target, *args)
        except SQLAlchemyError as err:
            raise CareLogSyncError("Reading care log statistics failed") from err

    async def rows(self, sid: str, start: int, end: int) -> dict[int, _Row]:
        rows = await self.job(_read_rows, self.hass, sid, start, end)
        self.rows_read += len(rows)
        return rows

    async def last(self, sid: str) -> tuple[int, float | None] | None:
        last = await self.job(_read_last, self.hass, sid)
        if last is not None:
            self.rows_read += 1
        return last

    async def first_from(self, sid: str, low: int, high: int) -> int | None:
        earliest, read = await self.job(_read_first_from, self.hass, sid, low, high)
        self.rows_read += read
        return earliest

    async def sum_before(self, sid: str, hour: int) -> float | None:
        """Return the sum of the newest stored row before ``hour``, or None when there is none."""
        found = await self.job(_read_sum_before, self.hass, sid, hour)
        if found is not None:
            self.rows_read += 1
        return found

    async def series_start(
        self, sid: str, first: int | None, last: int | None, cov: int, end: int
    ) -> tuple[int, float] | None:
        """Return a series' first hour and the total it continues from, or None.

        The first hour is ``cov`` for an established series (one with a
        stored row before ``cov``), which continues from that row's sum.
        Otherwise it is the earlier of the ledger's (``first``, at or after
        ``cov``) and the first stored row at or after ``cov``, from a total
        of 0: nothing is stored before it. ``last`` is the newest stored
        row's hour, None when nothing is stored.
        """
        if last is not None:
            prior = await self.sum_before(sid, cov)
            if prior is not None:
                return (cov, prior) if cov < end else None
        stored_first: int | None = None
        if last is not None and last >= cov:
            before = last + HOUR_SECONDS if first is None else min(first, last + HOUR_SECONDS)
            stored_first = await self.first_from(sid, cov, min(before, end))
        candidates = [hour for hour in (first, stored_first) if hour is not None]
        if not candidates:
            return None
        return min(candidates), 0.0


class _Sync:
    """One sync of one baby's series."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        code: str,
        names: Mapping[CareLogSeries, str],
        ledger: Ledger,
        end_hour: int,
        cov: int,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        self.hass = hass
        self.code = code
        self.names = names
        self.ledger = ledger
        self.end = end_hour
        self.cov = cov
        self.should_stop = should_stop
        self.reader = _Reader(hass)
        self.series_examined = 0
        self.rows_written = 0
        self.batches = 0

    def _check_stop(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise _StopRequestedError

    def report(self, *, stopped: bool) -> SyncReport:
        return SyncReport(
            series_examined=self.series_examined,
            rows_read=self.reader.rows_read,
            rows_written=self.rows_written,
            batches=self.batches,
            stopped=stopped,
        )

    async def series(self, series: CareLogSeries, changed_from: int | None, *, full: bool) -> None:
        """Bring one series up to date."""
        sid = statistic_id(self.code, series)
        cov = self.cov
        first = _first_hour(self.ledger, series, cov, self.end)
        last_row = await self.reader.last(sid)
        if last_row is None and first is None:
            return
        if last_row is not None and last_row[0] < cov:
            # Every stored row is in the prefix: the series goes on, dense,
            # from the coverage hour, whether or not the ledger has an entry.
            if cov >= self.end:
                return
            full = True
        self.series_examined += 1
        metadata = statistic_metadata(self.code, series, self.names[series])
        if last_row is None:
            assert first is not None
            await self._scan(series, metadata, first, 0.0)
            return
        last, last_sum = last_row
        if not full and changed_from is not None and changed_from <= last:
            # A change inside the stored rows: continue from the row before it,
            # when that row is covered too.
            changed = max(changed_from, cov)
            before = changed - HOUR_SECONDS
            if before >= cov:
                previous = (await self.reader.rows(sid, before, changed)).get(before)
                if previous is not None and previous[1] is not None:
                    await self._scan(series, metadata, changed, previous[1])
                    return
            # No row before it: the change is at or before the first stored row.
            full = True
        if full or last_sum is None:
            found = await self.reader.series_start(sid, first, last, cov, self.end)
            if found is not None:
                await self._scan(series, metadata, *found)
            return
        # Nothing changed in the stored rows: append the newly finished hours.
        await self._scan(series, metadata, last + HOUR_SECONDS, last_sum)

    async def _scan(
        self, series: CareLogSeries, metadata: StatisticMetaData, start: int, total: float
    ) -> None:
        """Compare ``[start, end)`` chunk by chunk and write what differs."""
        sid = metadata["statistic_id"]
        step = CHUNK_HOURS * HOUR_SECONDS
        for low in range(start, self.end, step):
            high = min(low + step, self.end)
            stored = await self.reader.rows(sid, low, high)
            batch: list[tuple[int, float, float]] = []
            for cell in _cells(self.ledger, series, low, high, stored, total):
                total = cell.total
                if _needs_write(cell):
                    batch.append((cell.hour, cell.state, cell.total))
                    if len(batch) >= BATCH_ROWS:
                        await self._write(metadata, batch)
                        batch = []
            del stored
            if batch:
                await self._write(metadata, batch)
            await asyncio.sleep(0)
            self._check_stop()

    async def _write(
        self, metadata: StatisticMetaData, batch: list[tuple[int, float, float]]
    ) -> None:
        """Send one batch, wait for the recorder, and read it back until it matches.

        Nothing is sent once a stop was asked for. A refused batch raises
        ``CareLogSyncError`` (nothing was sent). Once a batch is sent, a
        cancellation does not abandon it at once: its verification goes on,
        bounded by ``SETTLE_TIMEOUT`` from when it was sent, and then the
        cancellation goes on. A sent batch that is not verified, for any
        reason, pauses the baby until Home Assistant restarts (see the
        module docstring) and raises ``CareLogWriteUnknownError``, or the
        cancellation.
        """
        self._check_stop()
        _require_ready(self.hass)
        rows = [
            StatisticData(start=_utc(hour), state=state, sum=total) for hour, state, total in batch
        ]
        try:
            async_add_external_statistics(self.hass, metadata, rows)
        except HomeAssistantError as err:
            _LOGGER.warning("A batch of care log statistics was refused; stopping this sync")
            raise CareLogSyncError("A batch of care log statistics was refused") from err
        self.batches += 1
        verify = asyncio.ensure_future(self._verify(metadata["statistic_id"], batch))
        verified = False
        try:
            try:
                await asyncio.shield(verify)
            except asyncio.CancelledError:
                # Let the batch settle first (bounded by SETTLE_TIMEOUT).
                await asyncio.wait([verify])
                verified = not verify.cancelled() and verify.exception() is None
                raise
            except CareLogStatisticsError as err:
                raise CareLogWriteUnknownError(
                    "A batch of care log statistics was sent but not verified"
                ) from err
            verified = True
        finally:
            if not verified:
                # Sent, and maybe still to land: nothing relies on this
                # baby's stored rows, or writes them, until a restart.
                _pause(self.hass, self.code)
                verify.add_done_callback(_retrieve)
        self.rows_written += len(batch)
        self._check_stop()

    async def _verify(self, sid: str, batch: list[tuple[int, float, float]]) -> None:
        """Read a sent batch back until it matches, for at most ``SETTLE_TIMEOUT`` in all."""
        try:
            async with asyncio.timeout(SETTLE_TIMEOUT):
                if await self._read_back(sid, batch):
                    return
        except TimeoutError:
            pass
        # The warning is the pause's (``_pause``).
        _LOGGER.debug("A batch of care log statistics could not be verified; stopping this sync")
        raise CareLogSyncError("A batch of care log statistics could not be verified")

    async def _read_back(self, sid: str, batch: list[tuple[int, float, float]]) -> bool:
        low, high = batch[0][0], batch[-1][0] + HOUR_SECONDS
        for attempt in range(VERIFY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(VERIFY_RETRY_DELAY * 2 ** (attempt - 1))
            await _async_wait_for_recorder(self.hass)
            stored = await self.reader.job(_read_rows, self.hass, sid, low, high, recording=False)
            if all(
                (row := stored.get(hour)) is not None
                and not _differs(row[0], state)
                and not _differs(row[1], total)
                for hour, state, total in batch
            ):
                return True
        return False


def _retrieve(task: asyncio.Future[None]) -> None:
    """Retrieve a finished verification's outcome, so asyncio does not report it."""
    if not task.cancelled():
        task.exception()


async def _async_wait_for_recorder(hass: HomeAssistant) -> None:
    """Wait, at most ``WRITE_TIMEOUT``, for the recorder to work through its queue.

    Only before a read-back, which tells whether the batch is there: a wait
    that times out is followed by the read all the same.
    """
    await _async_caught_up(hass, WRITE_TIMEOUT)


def _check_end(end_hour: int) -> None:
    if end_hour % HOUR_SECONDS:
        raise ValueError("end_hour must be the start of a UTC hour")


def _first_hour(ledger: Ledger, series: CareLogSeries, cov: int, end: int) -> int | None:
    """Return the ledger's first hour of a series at or after ``cov``, if before ``end``."""
    first = ledger.first_hour_from(series, cov)
    return first if first is not None and first < end else None


async def async_sync_care_log_statistics(
    hass: HomeAssistant,
    *,
    code: str,
    names: Mapping[CareLogSeries, str],
    ledger: Ledger,
    end_hour: int,
    coverage_start: int,
    changed_from: Mapping[CareLogSeries, int] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SyncReport:
    """Make one baby's stored statistics equal its ledger, for the hours before ``end_hour``.

    ``coverage_start`` (Unix seconds) is the start of the history the
    ledger was built from: nothing before it, rounded up to an hour, is
    read to compare or written (see the module docstring).

    With ``changed_from`` None, a full sync: every series is compared from
    its first row. With ``changed_from`` given, an incremental sync: only the
    series listed can differ inside their stored rows, and only from the
    hour given; every series is still extended to ``end_hour``. A listed
    hour at or before a series' first stored row is handled as a full sync
    of that series.

    ``should_stop`` is checked between batches, chunks and series; when it
    returns True the sync returns what it did so far, with ``stopped`` set.

    Raises ``CareLogRecorderUnavailableError`` when the recorder is not ready,
    ``CareLogPausedError`` when the baby is paused (see the module
    docstring), and ``CareLogSyncError`` when a batch could not be written
    and verified (``CareLogWriteUnknownError`` when it was sent, which
    pauses the baby; nothing after it is sent).
    """
    _check_end(end_hour)
    _require_ready(hass)
    missing = set(CareLogSeries) - set(names)
    if missing:
        raise ValueError("names must name every series")
    for series in CareLogSeries:
        statistic_id(code, series)  # Raises for anything but a baby code.
    _require_not_paused(hass, code)
    sync = _Sync(
        hass,
        code=code,
        names=names,
        ledger=ledger,
        end_hour=end_hour,
        cov=coverage_hour(coverage_start),
        should_stop=should_stop,
    )
    full = changed_from is None
    changed = {series: hour_start(hour) for series, hour in (changed_from or {}).items()}
    stopped = False
    try:
        for series in CareLogSeries:
            sync._check_stop()
            await sync.series(series, changed.get(series), full=full)
    except _StopRequestedError:
        stopped = True
    report = sync.report(stopped=stopped)
    _LOGGER.debug(
        "Care log statistics %s sync: %d series, %d rows read, %d written in %d batches%s",
        "full" if full else "incremental",
        report.series_examined,
        report.rows_read,
        report.rows_written,
        report.batches,
        ", stopped early" if stopped else "",
    )
    return report


async def async_compare_care_log_statistics(
    hass: HomeAssistant, *, code: str, ledger: Ledger, end_hour: int, coverage_start: int
) -> Comparison:
    """Compare every covered stored row of one baby with its ledger, without writing.

    ``coverage_start`` is as for ``async_sync_care_log_statistics``: rows
    before it, rounded up to an hour, are not compared. The recorder is
    first given up to ``SETTLE_TIMEOUT`` to work through what it has queued
    (see the module docstring).

    Raises ``CareLogRecorderUnavailableError`` when the recorder is not ready,
    ``CareLogQueueBusyError`` when its queue did not drain in time, and
    ``CareLogPausedError`` when the baby is paused.
    """
    _check_end(end_hour)
    _require_ready(hass)
    for series in CareLogSeries:
        statistic_id(code, series)  # Raises for anything but a baby code.
    _require_not_paused(hass, code)
    if not await async_care_log_queue_drained(hass):
        raise CareLogQueueBusyError
    _require_ready(hass)
    _require_not_paused(hass, code)
    cov = coverage_hour(coverage_start)
    reader = _Reader(hass)
    lowering = raising = missing = to_write = 0
    step = CHUNK_HOURS * HOUR_SECONDS
    for series in CareLogSeries:
        sid = statistic_id(code, series)
        first = _first_hour(ledger, series, cov, end_hour)
        last_row = await reader.last(sid)
        found = await reader.series_start(
            sid, first, None if last_row is None else last_row[0], cov, end_hour
        )
        if found is None:
            continue
        start, total = found
        for low in range(start, end_hour, step):
            high = min(low + step, end_hour)
            stored = await reader.rows(sid, low, high)
            for cell in _cells(ledger, series, low, high, stored, total):
                total = cell.total
                to_write += _needs_write(cell)
                if cell.stored is None:
                    missing += 1
                    continue
                if cell.keep:
                    continue
                state = cell.stored[0] or 0.0
                if state > cell.amount + TOLERANCE:
                    lowering += 1
                elif state < cell.amount - TOLERANCE:
                    raising += 1
            del stored
            await asyncio.sleep(0)
    return Comparison(
        lowering_cells=lowering,
        raising_cells=raising,
        missing_rows=missing,
        rows_to_write=to_write,
        rows_read=reader.rows_read,
    )
