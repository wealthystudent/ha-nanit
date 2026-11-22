"""A ledger of accepted care log entries, and the rule for lowering it.

A pure state machine, kept free of Home Assistant so it is quick to test.
Per baby, the ledger holds every accepted entry (uid to what it adds,
numbers only), and the long-term statistics are always a function of it.

Nanit's answers give no sign that they are complete and none that an entry
was deleted: a deleted entry simply stops appearing, and an answer has been
seen to miss an entry that the next one returned. So the answers ("reads")
update the ledger under one rule:

- A change that only adds (a new entry, or an edit that lowers nothing and
  makes no rejected amount valid) is accepted at once.
- A change that would lower any series in any hour (a deleted entry, a
  lowered amount, an entry moved to another hour, a diaper type change),
  and a change that makes valid an amount the accepted entry holds as
  rejected, whatever the amounts, needs confirmation
  (``Change.needs_confirmation``). The ledger cannot know what is stored
  for a rejected amount (the writer keeps the stored value there), so a
  valid amount in its place may lower a chart. Such a change is held, per
  entry and as one unit, until a second, fresh read of the same
  bounds, taken at least a minimum gap later by the same reader, agrees
  with the first on every hour that was finished when the first was taken.
  Both reads must imply the change: the first one, against the ledger as
  it stood then, and the second one, against the ledger now.
- A re-read of the same bounds that comes before the gap and agrees with
  the first is neither: the change keeps waiting on the first read.
- Only a change a re-read could confirm is held at all: one in hours that
  had finished when its read was taken, and with no rejected amount in it.
  Any other such change is left for a later read to find again, once its
  hour has finished or its amount is valid, so nothing waits forever.
- A later read of other bounds that returns a waiting entry in a state
  other than the one the first read implied (present where it implied a
  removal, or with other values) contradicts that read: the entry stops
  waiting, and a fresh round has to start from a new first read.
- Newer evidence is never overridden by older: once a read has changed an
  entry, what a read taken before it says about that entry is ignored,
  so a read that arrives late cannot add back an entry a newer
  confirmation removed, or undo a newer edit.

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

import math
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
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
    ``entries`` maps uid to facts, for the counted kinds only, each dated
    within the bounds.
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

        One entry per uid (the later-dated copy), counted kinds only. A
        counted entry dated outside the inclusive bounds is left out and
        counted in ``dropped``: such an answer is not what was asked for,
        so it can never confirm a removal, and a wrong old date cannot
        stretch the statistics back by years.
        """
        entries: dict[str, EntryFacts] = {}
        outside = 0
        for log in latest_by_uid(logs):
            facts = EntryFacts.from_log(log)
            if facts is None:
                continue
            if start <= facts.timestamp <= end:
                entries[log.uid] = facts
            else:
                outside += 1
        return cls(
            start=start,
            end=end,
            read_at=read_at,
            dropped=dropped + outside,
            reader=reader,
            entries=entries,
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
    def needs_confirmation(self) -> bool:
        """Whether the change waits for a confirming read before it is applied.

        It does when it lowers, and when a series the accepted entry holds
        as rejected becomes valid (or is no longer there), whatever the
        amounts: what is stored for a rejected amount is not known here, so
        the new amount may lower it.
        """
        if self.lowers:
            return True
        before, after = self.before, self.after
        if before is None or after is None:
            return False
        return bool(before.invalid - after.invalid)

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


# How the ledger stores an accepted entry: one exact tuple of numbers only,
# (timestamp, hour, series bits, invalid bits, value, ...). Bit i stands for
# _SERIES[i], the series in the order ``EntryFacts.raw`` keeps them, and the
# values are those of the series whose bit is set, in that order. Python's
# cyclic garbage collector stops tracking such a tuple at its first
# collection, so the many entries a ledger holds for the life of the process
# add nothing to every later collection. Facts, their pair tuples and
# frozensets would each stay tracked.
type _Record = tuple[float, int, int, int, *tuple[float, ...]]

_SERIES: tuple[CareLogSeries, ...] = tuple(sorted(CareLogSeries, key=lambda series: series.value))
# An hour later than any entry's: the open end of a search.
_NO_END = 2**62
_BITS: Mapping[CareLogSeries, int] = {series: 1 << index for index, series in enumerate(_SERIES)}


@lru_cache(maxsize=1024)
def _series_in(bits: int) -> tuple[CareLogSeries, ...]:
    """Return the series whose bits are set, in stored order."""
    return tuple(series for series in _SERIES if bits & _BITS[series])


@lru_cache(maxsize=1024)
def _series_set(bits: int) -> frozenset[CareLogSeries]:
    """Return the series whose bits are set, as a set."""
    return frozenset(_series_in(bits))


def _to_record(facts: EntryFacts) -> _Record:
    """Return the stored form of an entry's facts."""
    bits = 0
    values: list[float] = []
    for series, value in facts.raw:
        bits |= _BITS[series]
        values.append(value)
    if bits.bit_count() != len(values):
        raise ValueError("an entry adds to each series at most once")
    invalid = 0
    for series in facts.invalid:
        invalid |= _BITS[series]
    return (facts.timestamp, facts.hour, bits, invalid, *values)


def _pairs(record: _Record) -> tuple[tuple[CareLogSeries, float], ...]:
    """Return what a stored entry adds, as ``EntryFacts.raw`` holds it."""
    return tuple(zip(_series_in(record[2]), record[4:], strict=True))


def _to_facts(record: _Record) -> EntryFacts:
    return EntryFacts(
        timestamp=record[0], hour=record[1], raw=_pairs(record), invalid=_series_set(record[3])
    )


def _contribution(record: _Record) -> EntryContribution:
    return EntryContribution(hour=record[1], raw=_pairs(record), invalid=_series_set(record[3]))


class Ledger:
    """The accepted entries of one baby: uid to facts, indexed by hour.

    Changed only through ``apply``. Queries cost the hours they ask for: the
    indexed hours are kept sorted (rebuilt lazily after the set of hours
    changes) and searched with ``bisect``. ``first_hour`` of every series is
    cached, kept up to date by additions and recomputed in one pass only
    after a change touched an entry at a cached first hour.

    Each entry is stored as a tuple of numbers, and each hour's uids as a
    tuple of strings, so that the garbage collector need not track them (see
    ``_Record``); ``get`` builds the facts on demand.
    """

    __slots__ = ("_by_hour", "_entries", "_first", "_hours")

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[str, _Record] = {}
        self._by_hour: dict[int, tuple[str, ...]] = {}
        self._hours: list[int] | None = []
        self._first: dict[CareLogSeries, int] | None = {}

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
        record = self._entries.get(uid)
        return None if record is None else _to_facts(record)

    def _hours_within(self, start_hour: int, end_hour: int) -> list[int]:
        """Return the indexed hours in ``[start_hour, end_hour)``, oldest first."""
        if self._hours is None:
            self._hours = sorted(self._by_hour)
        hours = self._hours
        return hours[bisect_left(hours, start_hour) : bisect_left(hours, end_hour)]

    def uids_between(self, start: int, end: int) -> set[str]:
        """Return the uids whose accepted time lies within inclusive bounds."""
        entries = self._entries
        result: set[str] = set()
        for hour in self._hours_within(hour_start(start), end + 1):
            for uid in self._by_hour[hour]:
                if start <= entries[uid][0] <= end:
                    result.add(uid)
        return result

    def series_hours(
        self, series: CareLogSeries, start_hour: int, end_hour: int
    ) -> dict[int, float]:
        """Return a series' amount per hour in ``[start_hour, end_hour)``, in its unit.

        Only hours in which some entry adds to the series are included.
        """
        entries = self._entries
        bit = _BITS[series]
        below = bit - 1
        result: dict[int, float] = {}
        for hour in self._hours_within(start_hour, end_hour):
            # A value's place among the stored values is the number of set
            # series bits below its own.
            values = [
                record[4 + (record[2] & below).bit_count()]
                for uid in self._by_hour[hour]
                if (record := entries[uid])[2] & bit
            ]
            if values:
                result[hour] = series_total(series, values)
        return result

    def keep_hours(self, series: CareLogSeries, start_hour: int, end_hour: int) -> set[int]:
        """Return the hours in ``[start_hour, end_hour)`` holding a rejected amount of a series."""
        entries = self._entries
        bit = _BITS[series]
        return {
            hour
            for hour in self._hours_within(start_hour, end_hour)
            if any(entries[uid][3] & bit for uid in self._by_hour[hour])
        }

    def first_hour(self, series: CareLogSeries) -> int | None:
        """Return the earliest hour in which an entry adds a positive amount to a series."""
        if self._first is None:
            first: dict[CareLogSeries, int] = {}
            for record in self._entries.values():
                _note_first(first, record)
            self._first = first
        return self._first.get(series)

    def first_hour_from(self, series: CareLogSeries, start_hour: int) -> int | None:
        """Return the earliest hour at or after ``start_hour`` with a positive amount of a series.

        The cached first hour answers it unless it is earlier; only then are
        the hours from ``start_hour`` on searched, oldest first.
        """
        first = self.first_hour(series)
        if first is None or first >= start_hour:
            return first
        entries = self._entries
        bit = _BITS[series]
        below = bit - 1
        for hour in self._hours_within(start_hour, _NO_END):
            for uid in self._by_hour[hour]:
                record = entries[uid]
                if record[2] & bit and record[4 + (record[2] & below).bit_count()] > 0:
                    return hour
        return None

    def amounts(self, start_hour: int, end_hour: int) -> HourlyAmounts:
        """Return every series' amounts in ``[start_hour, end_hour)``, like ``hourly_amounts``."""
        entries = self._entries
        return sum_contributions(
            _contribution(entries[uid])
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
            stored = self._entries.get(change.uid)
            before = None if change.before is None else _to_record(change.before)
            if stored != before:
                raise ValueError("a change does not start from the accepted entry")
            after = None if change.after is None else _to_record(change.after)
            if stored is not None:
                self._unindex(change.uid, stored)
            if after is None:
                del self._entries[change.uid]
            else:
                self._entries[change.uid] = after
                hour = after[1]
                uids = self._by_hour.get(hour)
                if uids is None:
                    self._by_hour[hour] = (change.uid,)
                    self._hours = None
                else:
                    self._by_hour[hour] = (*uids, change.uid)
                if self._first is not None:
                    _note_first(self._first, after)
            if (
                change.before is None
                or change.after is None
                or not change.before.same_for_charts(change.after)
            ):
                _merge_earliest(changed, _changed_hours(change))
        return changed

    def _unindex(self, uid: str, before: _Record) -> None:
        hour = before[1]
        uids = tuple(other for other in self._by_hour[hour] if other != uid)
        if uids:
            self._by_hour[hour] = uids
        else:
            del self._by_hour[hour]
            self._hours = None
        first = self._first
        if first is not None and any(
            value > 0 and first.get(series) == hour
            for series, value in zip(_series_in(before[2]), before[4:], strict=True)
        ):
            # The entry may have been the only one there: recompute when asked.
            self._first = None


def _note_first(first: dict[CareLogSeries, int], record: _Record) -> None:
    """Lower the cached first hours for what a stored entry adds."""
    hour = record[1]
    for index, series in enumerate(_series_in(record[2]), 4):
        if record[index] > 0:
            current = first.get(series)
            if current is None or hour < current:
                first[series] = hour


# ---------------------------------------------------------------------------
# Assessing a read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class Assessment:
    """What a read would change: what is safe at once, and what needs confirmation.

    ``lowering`` holds every change that needs confirmation: the lowering
    ones, and those that make a rejected amount valid.
    """

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


def assess(
    ledger: Ledger,
    read: CareLogRead,
    changed_at: Mapping[str, float] | None = None,
    horizon: float = -math.inf,
) -> Assessment:
    """Compare a read with the ledger, without changing it.

    A returned uid is new, unchanged, a safe edit or an edit that needs
    confirmation (a lowering one, a move wherever its old time was, or one
    that makes a rejected amount valid). An accepted uid within the read's
    bounds that the read does not return is a suspected removal.

    ``changed_at`` maps a uid to when the read that last changed it was
    taken (Unix seconds), and ``horizon`` stands in for a uid not in it.
    What a read taken before that says about a uid is ignored: it
    neither proposes a change for it nor counts its absence. Two reads
    taken at the same moment cannot be ordered by time, so a tie is not
    older: the one processed later counts.
    """
    taken = read.read_at.timestamp()
    if taken < horizon:
        return Assessment(safe=(), lowering=())
    changed = changed_at or {}
    safe: list[Change] = []
    lowering: list[Change] = []
    for uid, returned in read.entries.items():
        if taken < changed.get(uid, horizon):
            continue
        before = ledger.get(uid)
        after = _carry_forward(before, returned)
        if after == before:
            continue
        change = Change(uid=uid, before=before, after=after)
        (lowering if change.needs_confirmation else safe).append(change)
    for uid in sorted(ledger.uids_between(read.start, read.end) - read.entries.keys()):
        if taken < changed.get(uid, horizon):
            continue
        lowering.append(Change(uid=uid, before=ledger.get(uid), after=None))
    return Assessment(safe=tuple(safe), lowering=tuple(lowering))


def _agree(first: CareLogRead, second: CareLogRead, cutoff: int) -> bool:
    """Whether two reads hold the same entries among hours before ``cutoff``."""
    a = {uid: facts for uid, facts in first.entries.items() if facts.hour < cutoff}
    b = {uid: facts for uid, facts in second.entries.items() if facts.hour < cutoff}
    return a.keys() == b.keys() and all(a[uid].same_for_charts(b[uid]) for uid in a)


def _sightings_agree(
    reads: Sequence[CareLogRead], seen: Iterable[CareLogRead], cutoff: int
) -> bool:
    """Whether no read in ``seen`` returns, before ``cutoff`` and within the pass, another state.

    The pass's state of an entry is the one its latest read holds, as for a
    candidate.
    """
    low, high = reads[0].start, reads[-1].end
    held: dict[str, tuple[datetime, EntryFacts]] = {}
    for read in reads:
        for uid, facts in read.entries.items():
            current = held.get(uid)
            if current is None or read.read_at >= current[0]:
                held[uid] = (read.read_at, facts)
    for read in seen:
        for uid, facts in read.entries.items():
            if facts.hour >= cutoff or not low <= facts.timestamp <= high:
                continue
            mine = held.get(uid)
            if mine is None or not mine[1].same_for_charts(facts):
                return False
    return True


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
    waiting. ``accepted`` counts the safe changes applied and ``lowered`` the
    lowering ones. ``waiting`` counts the lowering changes this read found
    and did not apply, whether held for a re-read or not (one in a running
    hour, with a rejected amount, or already waiting under other bounds).
    ``confirmed`` says the read confirmed a waiting record.
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


# How long the book remembers when an entry last changed, and how often it
# forgets what is older (see ``CareLogBook``).
CHANGE_MEMORY = 86400.0
_PRUNE_EVERY = 3600.0


class CareLogBook:
    """One baby's ledger, and the reads waiting to confirm a lowering change.

    While running, the book remembers for each uid when the read that last
    changed it in the ledger (added, edited, moved or removed it) was taken.
    A read taken before that is ignored for that uid (``assess``), so
    an older read processed late cannot undo a newer decision. A record
    older than ``CHANGE_MEMORY`` before the newest read processed is
    forgotten, and from then on every read older than that is ignored
    whole: such a read is never processed in practice (reads reach the book
    minutes after they are taken), so nothing it could say is lost.
    """

    __slots__ = (
        "_changed_at",
        "_established",
        "_horizon",
        "_ledger",
        "_max_attempts",
        "_max_pending",
        "_min_gap",
        "_newest",
        "_pending",
        "_pruned_at",
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
        # uid to when the read that last changed it was taken (Unix seconds):
        # plain floats, so the garbage collector does not track them.
        self._changed_at: dict[str, float] = {}
        # Reads taken before this are ignored whole; the newest read
        # processed, and when the records were last pruned.
        self._horizon = -math.inf
        self._newest = -math.inf
        self._pruned_at = -math.inf

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
            # Touching, or sharing the boundary second; no gap, no more overlap.
            if not previous.end <= current.start <= previous.end + 1:
                raise ValueError("the reads of a pass must be contiguous")

    def candidate(self, reads: Sequence[CareLogRead]) -> Ledger:
        """Build a ledger from one pass of contiguous reads, without changing the book.

        Adjacent reads touch or share their boundary second. An entry in more
        than one read keeps the facts from the read taken last (the later
        ``read_at``; the later read in the pass on a tie).
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

    def passes_agree(
        self,
        first: Sequence[CareLogRead],
        second: Sequence[CareLogRead],
        seen: Iterable[CareLogRead] = (),
    ) -> bool:
        """Whether a second pass confirms a first one.

        Same bounds pairwise, this reader throughout, nothing dropped, each
        read of the second at least ``min_gap`` after its pair in the first,
        and the same entries, read by read, in every hour that was finished
        when the first pass began.

        ``seen`` are other reads taken since the first pass began. None of
        them may return, in an hour that was finished when the first pass
        began and at a time inside the pass's bounds, an entry the second
        pass lacks or holds with other chart facts: such a sighting says
        the two passes are not the whole story.
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
        return _sightings_agree(second, seen, cutoff)

    def adopt(self, reads: Sequence[CareLogRead]) -> None:
        """Replace the ledger with a pass's candidate and forget every waiting change."""
        self.adopt_candidate(self.candidate(reads))

    def adopt_candidate(self, ledger: Ledger) -> None:
        """Replace the ledger with one ``candidate`` built, and forget every waiting change.

        Quick, so it can run on the event loop after the candidate was built
        off it.
        """
        self._ledger = ledger
        self._pending.clear()
        self._changed_at.clear()
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
        """Make the lowering changes the record for these bounds; return whether it gave up.

        Only what a re-read of these bounds could confirm is held: a change
        covered by this read's own finished hours, with no rejected amount
        in it, and not already waiting under other bounds.
        """
        elsewhere = self._waiting_elsewhere(key)
        cutoff = hour_start(read.read_at.timestamp())
        uids = frozenset(
            change.uid
            for change in changes
            if change.uid not in elsewhere and _covered(change, cutoff) and not _blocked(change)
        )
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

    def _cancel_contradicted(self, read: CareLogRead) -> None:
        """Drop waiting uids that this later read, of other bounds, contradicts.

        An entry returned since a record's first read, in a state other than
        the one that read implied, was not as that read said: the older
        answer must not count towards removing or lowering it.
        """
        key = (read.start, read.end)
        for other, record in list(self._pending.items()):
            if other == key or read.read_at <= record.read.read_at:
                continue
            contradicted = set()
            for uid in record.uids:
                seen = read.entries.get(uid)
                if seen is None:
                    continue
                said = record.read.entries.get(uid)
                if said is None or not said.same_for_charts(seen):
                    contradicted.add(uid)
            if not contradicted:
                continue
            remaining = record.uids - contradicted
            if remaining:
                self._pending[other] = _Pending(
                    read=record.read, uids=remaining, attempts=record.attempts
                )
            else:
                del self._pending[other]

    def _note_changed(self, changes: Iterable[Change], taken: float) -> None:
        """Remember that a read taken at ``taken`` changed these uids."""
        changed_at = self._changed_at
        for change in changes:
            if taken > changed_at.get(change.uid, -math.inf):
                changed_at[change.uid] = taken

    def _forget_old_changes(self, taken: float) -> None:
        """Forget records older than ``CHANGE_MEMORY`` before the newest read, hourly.

        Every read at or before that bound is ignored from then on, so a
        forgotten record can never be undercut by a read older than it.
        """
        if taken <= self._newest:
            return
        self._newest = taken
        if taken - self._pruned_at < _PRUNE_EVERY:
            return
        self._pruned_at = taken
        bound = taken - CHANGE_MEMORY
        self._horizon = max(self._horizon, bound)
        self._changed_at = {uid: at for uid, at in self._changed_at.items() if at > bound}

    def process(self, read: CareLogRead) -> Outcome:
        """Take one read while running; see the module docstring for the rule."""
        if not self._established:
            raise ValueError("the book has no ledger yet")
        self._check_reader(read)
        taken = read.read_at.timestamp()
        self._forget_old_changes(taken)
        assessment = assess(self._ledger, read, self._changed_at, self._horizon)
        changed = self._ledger.apply(assessment.safe)
        self._note_changed(assessment.safe, taken)
        self._cancel_contradicted(read)

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
            self._note_changed(ready, taken)
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

    def drop_all_pending(self) -> int:
        """Forget every waiting record, so nothing is confirmed across lost reads.

        Return how many there were.
        """
        count = len(self._pending)
        self._pending.clear()
        return count

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
