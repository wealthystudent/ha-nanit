"""A ledger of accepted care log entries, and the rule for lowering it.

A pure state machine, kept free of Home Assistant so it is quick to test.
Per baby, the ledger holds every accepted entry (uid to what it adds,
numbers only), and the long-term statistics are always a function of it.

Nanit's answers give no sign that they are complete and none that an entry
was deleted: a deleted entry simply stops appearing, and an answer has been
seen to miss an entry that the next one returned. So the answers ("reads")
update the ledger under one rule:

- A change that only adds (a new entry, or an edit that lowers nothing) is
  accepted at once.
- A change that would lower any series in any hour (a deleted entry, a
  lowered amount, an entry moved to another hour, a diaper type change) is
  held, per entry and as one unit, until a second, fresh read of the same
  bounds, taken at least a minimum gap later by the same reader, agrees
  with the first on every hour that was finished when the first was taken.
  Both reads must imply the change: the first one, against the ledger as
  it stood then, and the second one, against the ledger now.
- A re-read of the same bounds that comes before the gap and agrees with
  the first is neither: the change keeps waiting on the first read.

Every entry's change is applied as one unit keyed by uid, so an entry that
moves from one hour to another is never counted in both.

A measurement the library rejected adds nothing for a new entry and flags
its series. For an entry already accepted in the same hour, the accepted
value is carried forward instead, so a value that is unusable for a while
neither lowers a chart nor loses what it had.

Privacy: entry uids, the reader id and every time (timestamps, hours,
request bounds) stay out of every ``repr`` and ``str`` here, and nothing is
logged from this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from types import MappingProxyType

from aionanit.models import CareLog

from .care_log_hours import (
    CareLogSeries,
    EntryContribution,
    HourlyAmounts,
    entry_contribution,
    hour_start,
    latest_by_uid,
    series_total,
    sum_contributions,
)


def _require_aware(when: datetime, name: str) -> None:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


# ---------------------------------------------------------------------------
# Entries and reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class EntryFacts:
    """What one accepted or returned entry adds, numbers only.

    ``raw`` holds, sorted by series, what the entry adds to each series it
    adds to, in Nanit's units (1 for a count, mL, seconds). ``invalid``
    holds the series for which a measurement was rejected. ``==`` compares
    every field; ``same_for_charts`` ignores a time change within the hour.
    """

    timestamp: float
    hour: int
    raw: tuple[tuple[CareLogSeries, float], ...]
    invalid: frozenset[CareLogSeries] = frozenset()

    def __post_init__(self) -> None:
        """Check the hour matches the time, and keep ``raw`` sorted by series."""
        if self.hour != hour_start(self.timestamp):
            raise ValueError("hour must be the start of the hour the entry is dated in")
        object.__setattr__(self, "raw", tuple(sorted(self.raw, key=lambda item: item[0].value)))
        object.__setattr__(self, "invalid", frozenset(self.invalid))

    @classmethod
    def from_log(cls, log: CareLog) -> EntryFacts | None:
        """Return an entry's facts, or None for a kind that is not counted."""
        contribution = entry_contribution(log)
        if contribution is None:
            return None
        return cls(
            timestamp=log.timestamp,
            hour=contribution.hour,
            raw=contribution.raw,
            invalid=contribution.invalid,
        )

    def amount(self, series: CareLogSeries) -> float | None:
        """Return what the entry adds to a series (raw units), or None."""
        for item_series, value in self.raw:
            if item_series == series:
                return value
        return None

    def same_for_charts(self, other: EntryFacts) -> bool:
        """Whether two facts add the same to every chart cell, flags included."""
        return self.hour == other.hour and self.raw == other.raw and self.invalid == other.invalid

    def contribution(self) -> EntryContribution:
        """Return these facts in the shape the shared summing code takes."""
        return EntryContribution(hour=self.hour, raw=self.raw, invalid=self.invalid)

    def __repr__(self) -> str:
        """Show counts only: no time."""
        return f"<EntryFacts series={len(self.raw)} invalid={len(self.invalid)}>"


@dataclass(frozen=True, slots=True, repr=False)
class CareLogRead:
    """One answer from Nanit's calendar endpoint, as the ledger sees it.

    ``start`` and ``end`` are the inclusive bounds sent (Unix seconds).
    ``reader`` is an opaque id for the sign-in that made the request.
    ``entries`` maps uid to facts, for the counted kinds only; what is in it
    is what Nanit returned for the bounds.
    """

    start: int
    end: int
    read_at: datetime
    dropped: int
    reader: str
    entries: Mapping[str, EntryFacts] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate, and keep a read-only copy of the entries."""
        if self.end < self.start:
            raise ValueError("a read must end at or after its start")
        _require_aware(self.read_at, "read_at")
        if self.dropped < 0:
            raise ValueError("dropped must not be negative")
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    @classmethod
    def from_logs(
        cls,
        logs: Iterable[CareLog],
        *,
        start: int,
        end: int,
        read_at: datetime,
        dropped: int,
        reader: str,
    ) -> CareLogRead:
        """Build a read from returned entries.

        One entry per uid (the later-dated copy), counted kinds only. Entries
        are kept as returned: Nanit decides what is inside the bounds.
        """
        entries: dict[str, EntryFacts] = {}
        for log in latest_by_uid(logs):
            facts = EntryFacts.from_log(log)
            if facts is not None:
                entries[log.uid] = facts
        return cls(
            start=start, end=end, read_at=read_at, dropped=dropped, reader=reader, entries=entries
        )

    def __repr__(self) -> str:
        """Show counts only: no uid, reader or time."""
        return f"<CareLogRead entries={len(self.entries)} dropped={self.dropped}>"


@dataclass(frozen=True, slots=True, repr=False)
class Change:
    """One entry's change, applied as one unit: ``before`` becomes ``after``.

    ``before`` None is a new entry; ``after`` None is a removal.
    """

    uid: str
    before: EntryFacts | None
    after: EntryFacts | None

    @property
    def lowers(self) -> bool:
        """Whether the entry would add less to any (series, hour) cell afterwards."""
        before, after = self.before, self.after
        if before is None:
            return False
        if after is None or after.hour != before.hour:
            return True
        for series, value in before.raw:
            new = after.amount(series)
            if new is None or new < value:
                return True
        return False

    @property
    def kind(self) -> str:
        """``new``, ``edit`` or ``removal``."""
        if self.before is None:
            return "new"
        return "removal" if self.after is None else "edit"

    def __repr__(self) -> str:
        """Show the kind only: no uid or time."""
        return f"<Change {self.kind} lowers={self.lowers}>"


def _changed_hours(change: Change) -> dict[CareLogSeries, int]:
    """Return, per series, the earliest hour a change may alter."""
    result: dict[CareLogSeries, int] = {}
    for facts in (change.before, change.after):
        if facts is None:
            continue
        for series in {series for series, _ in facts.raw} | facts.invalid:
            current = result.get(series)
            if current is None or facts.hour < current:
                result[series] = facts.hour
    return result


def _merge_earliest(into: dict[CareLogSeries, int], other: Mapping[CareLogSeries, int]) -> None:
    for series, hour in other.items():
        current = into.get(series)
        if current is None or hour < current:
            into[series] = hour


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class Ledger:
    """The accepted entries of one baby: uid to facts, indexed by hour.

    Changed only through ``apply``.
    """

    __slots__ = ("_by_hour", "_entries")

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[str, EntryFacts] = {}
        self._by_hour: dict[int, set[str]] = {}

    def __len__(self) -> int:
        """Return the number of accepted entries."""
        return len(self._entries)

    def __contains__(self, uid: object) -> bool:
        """Whether an entry is accepted."""
        return uid in self._entries

    def __repr__(self) -> str:
        """Show counts only."""
        return f"<Ledger entries={len(self._entries)} hours={len(self._by_hour)}>"

    def get(self, uid: str) -> EntryFacts | None:
        """Return an accepted entry's facts, or None."""
        return self._entries.get(uid)

    def _hours_within(self, start_hour: int, end_hour: int) -> list[int]:
        """Return the indexed hours in ``[start_hour, end_hour)``, oldest first."""
        return sorted(hour for hour in self._by_hour if start_hour <= hour < end_hour)

    def uids_between(self, start: int, end: int) -> set[str]:
        """Return the uids whose accepted time lies within inclusive bounds."""
        result: set[str] = set()
        for hour in self._hours_within(hour_start(start), end + 1):
            for uid in self._by_hour[hour]:
                if start <= self._entries[uid].timestamp <= end:
                    result.add(uid)
        return result

    def series_hours(
        self, series: CareLogSeries, start_hour: int, end_hour: int
    ) -> dict[int, float]:
        """Return a series' amount per hour in ``[start_hour, end_hour)``, in its unit.

        Only hours in which some entry adds to the series are included.
        """
        result: dict[int, float] = {}
        for hour in self._hours_within(start_hour, end_hour):
            values = [
                value
                for uid in self._by_hour[hour]
                if (value := self._entries[uid].amount(series)) is not None
            ]
            if values:
                result[hour] = series_total(series, values)
        return result

    def keep_hours(self, series: CareLogSeries, start_hour: int, end_hour: int) -> set[int]:
        """Return the hours in ``[start_hour, end_hour)`` holding a rejected amount of a series."""
        return {
            hour
            for hour in self._hours_within(start_hour, end_hour)
            if any(series in self._entries[uid].invalid for uid in self._by_hour[hour])
        }

    def first_hour(self, series: CareLogSeries) -> int | None:
        """Return the earliest hour in which an entry adds a positive amount to a series."""
        hours = [
            facts.hour
            for facts in self._entries.values()
            if (value := facts.amount(series)) is not None and value > 0
        ]
        return min(hours, default=None)

    def amounts(self, start_hour: int, end_hour: int) -> HourlyAmounts:
        """Return every series' amounts in ``[start_hour, end_hour)``, like ``hourly_amounts``."""
        return sum_contributions(
            self._entries[uid].contribution()
            for hour in self._hours_within(start_hour, end_hour)
            for uid in self._by_hour[hour]
        )

    def apply(self, changes: Iterable[Change]) -> dict[CareLogSeries, int]:
        """Apply changes, each as one unit; return the earliest changed hour per series.

        Each change's ``before`` must be exactly what the ledger holds for its
        uid; otherwise nothing more is applied and ``ValueError`` is raised.
        """
        changed: dict[CareLogSeries, int] = {}
        for change in changes:
            if self._entries.get(change.uid) != change.before:
                raise ValueError("a change does not start from the accepted entry")
            if change.before is not None:
                self._unindex(change.uid, change.before.hour)
            if change.after is None:
                self._entries.pop(change.uid, None)
            else:
                self._entries[change.uid] = change.after
                self._by_hour.setdefault(change.after.hour, set()).add(change.uid)
            if (
                change.before is None
                or change.after is None
                or not change.before.same_for_charts(change.after)
            ):
                _merge_earliest(changed, _changed_hours(change))
        return changed

    def _unindex(self, uid: str, hour: int) -> None:
        uids = self._by_hour[hour]
        uids.discard(uid)
        if not uids:
            del self._by_hour[hour]


# ---------------------------------------------------------------------------
# Assessing a read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class Assessment:
    """What a read would change: what is safe at once, and what lowers."""

    safe: tuple[Change, ...]
    lowering: tuple[Change, ...]

    def __repr__(self) -> str:
        """Show counts only."""
        return f"<Assessment safe={len(self.safe)} lowering={len(self.lowering)}>"


def _carry_forward(before: EntryFacts | None, after: EntryFacts) -> EntryFacts:
    """Keep the accepted value of a series whose new measurement was rejected.

    Only for an entry already accepted in the same hour; the series stays
    flagged.
    """
    if before is None or before.hour != after.hour or not after.invalid:
        return after
    raw = dict(after.raw)
    for series in after.invalid:
        old = before.amount(series)
        if old is not None:
            raw[series] = old
    return EntryFacts(
        timestamp=after.timestamp,
        hour=after.hour,
        raw=tuple(sorted(raw.items(), key=lambda item: item[0].value)),
        invalid=after.invalid,
    )


def assess(ledger: Ledger, read: CareLogRead) -> Assessment:
    """Compare a read with the ledger, without changing it.

    A returned uid is new, unchanged, a safe edit or a lowering edit (a
    move included, wherever its old time was). An accepted uid within the
    read's bounds that the read does not return is a suspected removal.
    """
    safe: list[Change] = []
    lowering: list[Change] = []
    for uid, returned in read.entries.items():
        before = ledger.get(uid)
        after = _carry_forward(before, returned)
        if after == before:
            continue
        change = Change(uid=uid, before=before, after=after)
        (lowering if change.lowers else safe).append(change)
    for uid in sorted(ledger.uids_between(read.start, read.end) - read.entries.keys()):
        lowering.append(Change(uid=uid, before=ledger.get(uid), after=None))
    return Assessment(safe=tuple(safe), lowering=tuple(lowering))


def _agree(first: CareLogRead, second: CareLogRead, cutoff: int) -> bool:
    """Whether two reads hold the same entries among hours before ``cutoff``."""
    a = {uid: facts for uid, facts in first.entries.items() if facts.hour < cutoff}
    b = {uid: facts for uid, facts in second.entries.items() if facts.hour < cutoff}
    return a.keys() == b.keys() and all(a[uid].same_for_charts(b[uid]) for uid in a)


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmRequest:
    """Bounds to read again, no earlier than ``not_before``."""

    start: int
    end: int
    not_before: datetime

    def __repr__(self) -> str:
        """Show the span only: no time."""
        return f"<ConfirmRequest span={self.end - self.start + 1}s>"


@dataclass(frozen=True, slots=True, repr=False)
class Outcome:
    """What one ``process`` call did.

    ``changed_from`` is, per series, the earliest hour whose amount may have
    changed. ``confirm`` is set while a lowering change for these bounds is
    waiting. ``confirmed`` says the read confirmed a waiting record.
    """

    changed_from: Mapping[CareLogSeries, int]
    confirm: ConfirmRequest | None
    accepted: int
    lowered: int
    waiting: int
    gave_up: bool
    confirmed: bool

    def __repr__(self) -> str:
        """Show counters only."""
        return (
            f"<Outcome accepted={self.accepted} lowered={self.lowered} "
            f"waiting={self.waiting} gave_up={self.gave_up} confirmed={self.confirmed} "
            f"series={len(self.changed_from)}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BookStats:
    """Counts for diagnostics."""

    entries: int
    pending_records: int
    waiting_entries: int
    established: bool

    def __repr__(self) -> str:
        """Show the counts."""
        return (
            f"<BookStats entries={self.entries} pending_records={self.pending_records} "
            f"waiting_entries={self.waiting_entries} established={self.established}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _Pending:
    """Lowering changes for one set of bounds, waiting for a second read.

    Each uid waits under at most one record. ``repr`` is the plain object
    one, which shows nothing of the uids.
    """

    read: CareLogRead
    uids: frozenset[str]
    attempts: int


def _covered(change: Change, cutoff: int) -> bool:
    before, after = change.before, change.after
    return before is not None and before.hour < cutoff and (after is None or after.hour < cutoff)


def _blocked(change: Change) -> bool:
    return change.after is not None and bool(change.after.invalid)


class CareLogBook:
    """One baby's ledger, and the reads waiting to confirm a lowering change."""

    __slots__ = (
        "_established",
        "_ledger",
        "_max_attempts",
        "_max_pending",
        "_min_gap",
        "_pending",
        "_reader",
    )

    def __init__(
        self, *, reader: str, min_gap: timedelta, max_attempts: int = 3, max_pending: int = 8
    ) -> None:
        """Start with an empty ledger, not yet established."""
        if min_gap <= timedelta(0):
            raise ValueError("min_gap must be positive")
        if max_attempts < 1 or max_pending < 1:
            raise ValueError("max_attempts and max_pending must be at least 1")
        self._reader = reader
        self._min_gap = min_gap
        self._max_attempts = max_attempts
        self._max_pending = max_pending
        self._ledger = Ledger()
        self._established = False
        self._pending: dict[tuple[int, int], _Pending] = {}

    def __repr__(self) -> str:
        """Show counts only."""
        return (
            f"<CareLogBook entries={len(self._ledger)} pending={len(self._pending)} "
            f"established={self._established}>"
        )

    @property
    def ledger(self) -> Ledger:
        """The accepted entries (change them only through the book)."""
        return self._ledger

    @property
    def established(self) -> bool:
        """Whether a first pass has been adopted."""
        return self._established

    @property
    def min_gap(self) -> timedelta:
        """The least time between a read and the read that confirms it."""
        return self._min_gap

    def _check_reader(self, read: CareLogRead) -> None:
        if read.reader != self._reader:
            raise ValueError("the read was made by another reader")

    # -- starting up --------------------------------------------------------

    def _check_pass(self, reads: Sequence[CareLogRead]) -> None:
        if not reads:
            raise ValueError("a pass needs at least one read")
        for read in reads:
            self._check_reader(read)
        for previous, current in pairwise(reads):
            if current.start < previous.start:
                raise ValueError("the reads of a pass must be sorted by start")
            if current.start != previous.end + 1:
                raise ValueError("the reads of a pass must be contiguous")

    def candidate(self, reads: Sequence[CareLogRead]) -> Ledger:
        """Build a ledger from one pass of contiguous reads, without changing the book.

        An entry in more than one read keeps the facts from the read taken
        last (the later ``read_at``; the later read in the pass on a tie).
        """
        self._check_pass(reads)
        chosen: dict[str, tuple[datetime, EntryFacts]] = {}
        for read in reads:
            for uid, facts in read.entries.items():
                current = chosen.get(uid)
                if current is None or read.read_at >= current[0]:
                    chosen[uid] = (read.read_at, facts)
        ledger = Ledger()
        ledger.apply(
            Change(uid=uid, before=None, after=facts) for uid, (_, facts) in sorted(chosen.items())
        )
        return ledger

    def passes_agree(self, first: Sequence[CareLogRead], second: Sequence[CareLogRead]) -> bool:
        """Whether a second pass confirms a first one.

        Same bounds pairwise, this reader throughout, nothing dropped, each
        read of the second at least ``min_gap`` after its pair in the first,
        and the same entries, read by read, in every hour that was finished
        when the first pass began.
        """
        try:
            self._check_pass(first)
            self._check_pass(second)
        except ValueError:
            return False
        if len(first) != len(second):
            return False
        cutoff = hour_start(min(read.read_at for read in first).timestamp())
        for a, b in zip(first, second, strict=True):
            if (a.start, a.end) != (b.start, b.end):
                return False
            if a.dropped or b.dropped:
                return False
            if b.read_at - a.read_at < self._min_gap:
                return False
            if not _agree(a, b, cutoff):
                return False
        return True

    def adopt(self, reads: Sequence[CareLogRead]) -> None:
        """Replace the ledger with a pass's candidate and forget every waiting change."""
        self._ledger = self.candidate(reads)
        self._pending.clear()
        self._established = True

    # -- running ------------------------------------------------------------

    def _confirms(self, first: CareLogRead, second: CareLogRead) -> bool:
        return second.read_at - first.read_at >= self._min_gap and _agrees_cleanly(first, second)

    def _waiting_elsewhere(self, key: tuple[int, int]) -> set[str]:
        return {
            uid for other, record in self._pending.items() if other != key for uid in record.uids
        }

    def _hold(
        self, key: tuple[int, int], read: CareLogRead, changes: Iterable[Change], attempts: int
    ) -> bool:
        """Make the lowering changes the record for these bounds; return whether it gave up."""
        elsewhere = self._waiting_elsewhere(key)
        uids = frozenset(change.uid for change in changes if change.uid not in elsewhere)
        if not uids:
            self._pending.pop(key, None)
            return False
        if attempts > self._max_attempts:
            self._pending.pop(key, None)
            return True
        if key not in self._pending and len(self._pending) >= self._max_pending:
            return False
        self._pending[key] = _Pending(read=read, uids=uids, attempts=attempts)
        return False

    def process(self, read: CareLogRead) -> Outcome:
        """Take one read while running; see the module docstring for the rule."""
        if not self._established:
            raise ValueError("the book has no ledger yet")
        self._check_reader(read)
        assessment = assess(self._ledger, read)
        changed = self._ledger.apply(assessment.safe)

        key = (read.start, read.end)
        pending = self._pending.get(key)
        lowered = 0
        gave_up = False
        confirmed = False
        if pending is not None and self._confirms(pending.read, read):
            confirmed = True
            cutoff = hour_start(pending.read.read_at.timestamp())
            # Only what the first read implied too: an entry accepted after it
            # was taken (say, logged late for an earlier hour) is absent from
            # it for that reason alone, so its absence now is one answer's.
            ready = [
                c
                for c in assessment.lowering
                if c.uid in pending.uids and _covered(c, cutoff) and not _blocked(c)
            ]
            _merge_earliest(changed, self._ledger.apply(ready))
            lowered = len(ready)
            del self._pending[key]
            applied = {change.uid for change in ready}
            self._hold(key, read, (c for c in assessment.lowering if c.uid not in applied), 1)
        elif (
            pending is not None
            and read.read_at - pending.read.read_at < self._min_gap
            and _agrees_cleanly(pending.read, read)
        ):
            # Too early to confirm, but it agrees: neither a confirmation nor
            # a disagreement, so the record keeps its first read, its uids and
            # its count, unchanged.
            pass
        else:
            attempts = 1 if pending is None else pending.attempts + 1
            gave_up = self._hold(key, read, assessment.lowering, attempts)

        record = self._pending.get(key)
        return Outcome(
            changed_from=MappingProxyType(changed),
            confirm=None
            if record is None
            else ConfirmRequest(
                start=read.start, end=read.end, not_before=record.read.read_at + self._min_gap
            ),
            accepted=len(assessment.safe),
            lowered=lowered,
            waiting=len(assessment.lowering) - lowered,
            gave_up=gave_up,
            confirmed=confirmed,
        )

    def pending_requests(self) -> tuple[ConfirmRequest, ...]:
        """Return every outstanding re-read, oldest bounds first."""
        return tuple(
            ConfirmRequest(start=start, end=end, not_before=record.read.read_at + self._min_gap)
            for (start, end), record in sorted(self._pending.items())
        )

    def drop_pending(self, start: int, end: int) -> bool:
        """Forget the waiting record for some bounds; return whether there was one."""
        return self._pending.pop((start, end), None) is not None

    def stats(self) -> BookStats:
        """Return counts for diagnostics."""
        return BookStats(
            entries=len(self._ledger),
            pending_records=len(self._pending),
            waiting_entries=sum(len(record.uids) for record in self._pending.values()),
            established=self._established,
        )


def _agrees_cleanly(first: CareLogRead, second: CareLogRead) -> bool:
    """Nothing dropped in either read, and the same entries in the first's finished hours."""
    return (
        first.dropped == 0
        and second.dropped == 0
        and _agree(first, second, hour_start(first.read_at.timestamp()))
    )
