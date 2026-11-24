"""The care log ledger: when a long-term chart is allowed to go down (pure, no recorder)."""

from __future__ import annotations

import dataclasses
import gc
import operator
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from aionanit.models import CareLog
from custom_components.nanit import care_log_ledger
from custom_components.nanit.care_log_hours import (
    CareLogSeries,
    HourlyAmounts,
    hour_start,
    hourly_amounts,
    series_total,
    sum_contributions,
)
from custom_components.nanit.care_log_ledger import (
    Assessment,
    CareLogBook,
    CareLogRead,
    Change,
    ConfirmRequest,
    EntryFacts,
    Ledger,
    Outcome,
    assess,
)

S = CareLogSeries
READER = "reader-a"
GAP = timedelta(minutes=10)

# Synthetic: entries on a Tuesday morning, reads that afternoon.
DAY = datetime(2026, 3, 10, tzinfo=UTC)
AT_9 = datetime(2026, 3, 10, 9, 10, tzinfo=UTC)
AT_10 = datetime(2026, 3, 10, 10, 30, tzinfo=UTC)
T0 = datetime(2026, 3, 10, 15, 20, tzinfo=UTC)
START = int(DAY.timestamp())
END = int((DAY + timedelta(days=1)).timestamp()) - 1


def _log(uid: str, kind: str, when: datetime, **fields: Any) -> CareLog:
    return CareLog(uid=uid, log_type=kind, timestamp=when.timestamp(), baby_uid="b", **fields)


def _hour(when: datetime) -> int:
    return hour_start(when.timestamp())


def _facts(log: CareLog) -> EntryFacts:
    facts = EntryFacts.from_log(log)
    assert facts is not None
    return facts


def _read(
    logs: Iterable[CareLog],
    *,
    at: datetime,
    start: int = START,
    end: int = END,
    dropped: int = 0,
    reader: str = READER,
) -> CareLogRead:
    return CareLogRead.from_logs(
        logs, start=start, end=end, read_at=at, dropped=dropped, reader=reader
    )


def _book(logs: Iterable[CareLog], **kwargs: Any) -> CareLogBook:
    book = CareLogBook(reader=READER, min_gap=GAP, **kwargs)
    book.adopt([_read(logs, at=T0 - timedelta(hours=1))])
    return book


def _amount(book: CareLogBook, series: CareLogSeries, when: datetime) -> float:
    hour = _hour(when)
    return book.ledger.series_hours(series, hour, hour + 3600).get(hour, 0.0)


def _bottle(uid: str = "b1", when: datetime = AT_9, ml: float | None = 90.0, **kw: Any) -> CareLog:
    return _log(uid, "bottle_feed", when, feed_amount_ml=ml, **kw)


def _diaper(uid: str = "d1", when: datetime = AT_9, kind: str | None = "pee") -> CareLog:
    return _log(uid, "diaper_change", when, change_type=kind)


# ---------------------------------------------------------------------------
# Entry facts and reads
# ---------------------------------------------------------------------------


def test_entry_facts_come_from_the_shared_contribution() -> None:
    log = _log("n1", "nursing", AT_10, left_duration_s=600.0, right_duration_s=300.0)
    facts = _facts(log)
    assert facts.timestamp == AT_10.timestamp()
    assert facts.hour == _hour(AT_10)
    assert facts.raw == (
        (S.NURSING_SESSIONS, 1.0),
        (S.NURSING_TIME, 900.0),
        (S.NURSING_TIME_LEFT, 600.0),
        (S.NURSING_TIME_RIGHT, 300.0),
    )
    assert facts.invalid == frozenset()
    assert facts.amount(S.NURSING_TIME) == 900.0
    assert facts.amount(S.BOTTLE_VOLUME) is None


def test_sleep_unknown_and_timeless_entries_have_no_facts() -> None:
    assert EntryFacts.from_log(_log("s1", "sleep", AT_9, duration_s=60.0)) is None
    assert EntryFacts.from_log(_log("x1", "temperature", AT_9)) is None
    timeless = CareLog(uid="d1", log_type="diaper_change", timestamp=float("inf"), baby_uid="b")
    assert EntryFacts.from_log(timeless) is None


def test_entry_facts_check_their_hour_and_keep_raw_sorted() -> None:
    with pytest.raises(ValueError):
        EntryFacts(timestamp=AT_9.timestamp(), hour=_hour(AT_10), raw=())
    facts = EntryFacts(
        timestamp=AT_9.timestamp(),
        hour=_hour(AT_9),
        raw=((S.BOTTLE_VOLUME, 90.0), (S.BOTTLE_FEEDS, 1.0)),
    )
    assert facts.raw == ((S.BOTTLE_FEEDS, 1.0), (S.BOTTLE_VOLUME, 90.0))


def test_a_time_change_within_the_hour_is_the_same_for_the_charts() -> None:
    first = _facts(_bottle(when=AT_9))
    later = _facts(_bottle(when=AT_9 + timedelta(minutes=20)))
    assert first.same_for_charts(later)
    assert first != later  # == still compares every field
    assert not first.same_for_charts(_facts(_bottle(when=AT_10)))
    assert not first.same_for_charts(_facts(_bottle(ml=60.0)))
    flagged = _facts(_bottle(ml=None, invalid_fields=frozenset({"feed_amount_ml"})))
    assert not _facts(_bottle(ml=None)).same_for_charts(flagged)


def test_a_read_keeps_counted_kinds_once_per_uid_as_returned() -> None:
    logs = [
        _bottle("b1", AT_9, 60.0),
        _bottle("b1", AT_10, 50.0),  # the later-dated copy wins
        _log("s1", "sleep", AT_9, duration_s=600.0),
        _log("x1", "temperature", AT_9),
        _diaper("d1", AT_10),
    ]
    read = _read(logs, at=T0, dropped=2)
    assert set(read.entries) == {"b1", "d1"}
    assert read.entries["b1"].hour == _hour(AT_10)
    assert read.entries["b1"].amount(S.BOTTLE_VOLUME) == 50.0
    assert read.dropped == 2


def test_a_read_leaves_out_and_counts_entries_dated_outside_its_bounds() -> None:
    at_start = datetime.fromtimestamp(START, UTC)
    logs = [
        _diaper("d_old", DAY - timedelta(days=3)),  # a wrong, old date
        _bottle("b_early", at_start - timedelta(seconds=0.5)),
        _bottle("b_edge", at_start),  # the bounds are inclusive
        _bottle("b_last", datetime.fromtimestamp(END, UTC)),
        _bottle("b_late", datetime.fromtimestamp(END + 0.5, UTC)),
        _log("s_old", "sleep", DAY - timedelta(days=3), duration_s=600.0),  # any kind
    ]
    read = _read(logs, at=T0, dropped=1)
    assert set(read.entries) == {"b_edge", "b_last"}
    assert read.dropped == 1 + 4


def test_an_answer_with_an_entry_outside_its_bounds_never_confirms_a_removal() -> None:
    book = _book([_bottle("b1", AT_9), _diaper("d1", AT_10)])
    outside = _diaper("d_old", DAY - timedelta(days=3))
    book.process(_read([_diaper("d1", AT_10)], at=T0))
    outcome = book.process(_read([_diaper("d1", AT_10), outside], at=T0 + GAP))
    assert not outcome.confirmed
    assert "b1" in book.ledger
    assert "d_old" not in book.ledger


def test_a_later_dated_copy_outside_the_bounds_does_not_displace_the_one_inside() -> None:
    logs = [
        _bottle("b1", AT_9, 60.0),
        _bottle("b1", datetime.fromtimestamp(END + 60, UTC), 50.0),  # later, outside
    ]
    for order in (logs, logs[::-1]):
        read = _read(order, at=T0)
        assert set(read.entries) == {"b1"}
        assert read.entries["b1"].amount(S.BOTTLE_VOLUME) == 60.0
        assert read.dropped == 1


@pytest.mark.parametrize("later", [True, False], ids=["later_outside", "earlier_outside"])
def test_a_copy_of_another_kind_outside_the_bounds_does_not_displace_the_one_inside(
    later: bool,
) -> None:
    """A sleep row with a counted entry's uid, dated outside the bounds, in either order."""
    when = datetime.fromtimestamp(END + 60, UTC) if later else DAY - timedelta(days=3)
    logs = [_bottle("b1", AT_9, 60.0), _log("b1", "sleep", when, duration_s=600.0)]
    for order in (logs, logs[::-1]):
        read = _read(order, at=T0)
        assert set(read.entries) == {"b1"}
        assert read.entries["b1"].amount(S.BOTTLE_VOLUME) == 60.0
        assert read.dropped == 1
    # Two such answers never confirm the removal of another entry.
    book = _book([_bottle("b1", AT_9, 60.0), _diaper("d1", AT_10)])
    book.process(_read(logs, at=T0))
    outcome = book.process(_read(logs[::-1], at=T0 + GAP))
    assert not outcome.confirmed
    assert "d1" in book.ledger
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 60.0


def test_an_earlier_dated_copy_outside_the_bounds_is_counted_as_dropped() -> None:
    logs = [
        _bottle("b1", DAY - timedelta(days=3), 60.0),  # earlier, outside
        _bottle("b1", AT_10, 50.0),
    ]
    for order in (logs, logs[::-1]):
        read = _read(order, at=T0)
        assert read.entries["b1"].amount(S.BOTTLE_VOLUME) == 50.0
        assert read.dropped == 1
    # So an answer holding it never confirms a removal.
    book = _book([_bottle("b1", AT_10, 50.0), _diaper("d1", AT_9)])
    book.process(_read([_bottle("b1", AT_10, 50.0)], at=T0))
    outcome = book.process(_read(logs, at=T0 + GAP))
    assert not outcome.confirmed
    assert "d1" in book.ledger


def test_a_read_checks_its_bounds_time_and_dropped_count() -> None:
    with pytest.raises(ValueError):
        _read([], at=T0, start=END, end=START)
    with pytest.raises(ValueError):
        _read([], at=datetime(2026, 3, 10, 15, 20))
    with pytest.raises(ValueError):
        _read([], at=T0, dropped=-1)
    assert len(_read([], at=T0, start=START, end=START).entries) == 0


def test_a_read_cannot_be_changed_through_its_entries() -> None:
    source = {"b1": _facts(_bottle())}
    read = CareLogRead(start=START, end=END, read_at=T0, dropped=0, reader=READER, entries=source)
    source["b2"] = _facts(_bottle("b2"))
    assert set(read.entries) == {"b1"}
    with pytest.raises(TypeError):
        operator.setitem(read.entries, "b3", _facts(_bottle("b3")))


# ---------------------------------------------------------------------------
# What lowers
# ---------------------------------------------------------------------------

_BASE_BOTTLE = _bottle(ml=90.0)
_BASE_NURSING = _log("n1", "nursing", AT_9, left_duration_s=600.0)


@pytest.mark.parametrize(
    ("before", "after", "lowers"),
    [
        pytest.param(None, _BASE_BOTTLE, False, id="new"),
        pytest.param(_BASE_BOTTLE, None, True, id="removed"),
        pytest.param(_BASE_BOTTLE, _bottle(ml=120.0), False, id="raised"),
        pytest.param(_BASE_BOTTLE, _bottle(ml=60.0), True, id="lowered"),
        pytest.param(_BASE_BOTTLE, _bottle(ml=None), True, id="cleared"),
        pytest.param(
            _BASE_BOTTLE,
            _bottle(ml=None, invalid_fields=frozenset({"feed_amount_ml"})),
            True,
            id="rejected-without-carry-forward",
        ),
        pytest.param(_BASE_BOTTLE, _bottle(when=AT_10), True, id="moved-hour"),
        pytest.param(
            _BASE_BOTTLE, _bottle(when=AT_9 + timedelta(minutes=40)), False, id="moved-in-hour"
        ),
        pytest.param(_diaper(kind="pee"), _diaper(kind="poo"), True, id="diaper-type"),
        pytest.param(_diaper(kind=None), _diaper(kind="poo"), False, id="diaper-type-added"),
        pytest.param(
            _BASE_NURSING,
            _log("n1", "nursing", AT_9, left_duration_s=600.0, right_duration_s=60.0),
            False,
            id="side-added",
        ),
    ],
)
def test_which_changes_lower(before: CareLog | None, after: CareLog | None, lowers: bool) -> None:
    change = Change(
        uid="u",
        before=None if before is None else _facts(before),
        after=None if after is None else _facts(after),
    )
    assert change.lowers is lowers


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _ledger(logs: Iterable[CareLog]) -> Ledger:
    ledger = Ledger()
    ledger.apply(Change(uid=log.uid, before=None, after=_facts(log)) for log in logs)
    return ledger


def test_a_ledger_holds_accepted_entries_by_uid() -> None:
    ledger = Ledger()
    assert len(ledger) == 0
    assert "b1" not in ledger
    ledger = _ledger([_bottle("b1"), _diaper("d1")])
    assert len(ledger) == 2
    assert "b1" in ledger
    assert ledger.get("b1") == _facts(_bottle("b1"))
    assert ledger.get("nope") is None


def test_uids_between_uses_the_accepted_time_and_inclusive_seconds() -> None:
    at = datetime(2026, 3, 10, 9, 30, 15, tzinfo=UTC)
    ledger = _ledger([_bottle("b1", at), _bottle("b2", AT_10)])
    second = int(at.timestamp())
    assert ledger.uids_between(second, second) == {"b1"}
    assert ledger.uids_between(second - 100, second - 1) == set()
    assert ledger.uids_between(second + 1, int(AT_10.timestamp())) == {"b2"}
    assert ledger.uids_between(START, END) == {"b1", "b2"}


def test_series_hours_are_in_series_units_over_a_half_open_range() -> None:
    ledger = _ledger(
        [
            _log("n1", "nursing", AT_9, left_duration_s=20.0),
            _log("n2", "nursing", AT_9 + timedelta(minutes=5), left_duration_s=40.0),
            _log("n3", "nursing", AT_10, left_duration_s=90.0),
            _bottle("b1", AT_9, 0.0),
        ]
    )
    nine, ten = _hour(AT_9), _hour(AT_10)
    assert ledger.series_hours(S.NURSING_TIME, nine, ten + 3600) == {nine: 1.0, ten: 1.5}
    assert ledger.series_hours(S.NURSING_TIME, nine, ten) == {nine: 1.0}
    assert ledger.series_hours(S.NURSING_SESSIONS, nine, ten + 3600) == {nine: 2.0, ten: 1.0}
    # A recorded zero is an hour that has the series.
    assert ledger.series_hours(S.BOTTLE_VOLUME, nine, ten + 3600) == {nine: 0.0}
    assert ledger.series_hours(S.PUMPING_TIME, nine, ten + 3600) == {}


def test_keep_hours_are_the_hours_holding_a_rejected_amount() -> None:
    ledger = _ledger(
        [
            _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"})),
            _bottle("b2", AT_10, 30.0),
        ]
    )
    nine, ten = _hour(AT_9), _hour(AT_10)
    assert ledger.keep_hours(S.BOTTLE_VOLUME, nine, ten + 3600) == {nine}
    assert ledger.keep_hours(S.BOTTLE_VOLUME, ten, ten + 3600) == set()
    assert ledger.keep_hours(S.BOTTLE_FEEDS, nine, ten + 3600) == set()


def test_first_hour_is_the_first_positive_amount() -> None:
    ledger = _ledger([_bottle("b1", AT_9, 0.0), _bottle("b2", AT_10, 30.0)])
    assert ledger.first_hour(S.BOTTLE_FEEDS) == _hour(AT_9)
    assert ledger.first_hour(S.BOTTLE_VOLUME) == _hour(AT_10)
    assert ledger.first_hour(S.PUMPING_TIME) is None


def test_apply_reports_the_earliest_changed_hour_of_a_move_both_ways() -> None:
    ledger = _ledger([_bottle("b1", AT_10, 90.0)])
    before = ledger.get("b1")
    moved = _facts(_bottle("b1", AT_9, 90.0))
    changed = ledger.apply([Change(uid="b1", before=before, after=moved)])
    assert changed == {S.BOTTLE_FEEDS: _hour(AT_9), S.BOTTLE_VOLUME: _hour(AT_9)}
    back = _facts(_bottle("b1", AT_10, 90.0))
    changed = ledger.apply([Change(uid="b1", before=moved, after=back)])
    assert changed == {S.BOTTLE_FEEDS: _hour(AT_9), S.BOTTLE_VOLUME: _hour(AT_9)}
    assert ledger.series_hours(S.BOTTLE_FEEDS, START, END) == {_hour(AT_10): 1.0}


def test_apply_reports_a_type_change_and_nothing_for_a_time_change_in_the_hour() -> None:
    ledger = _ledger([_diaper("d1", AT_9, "pee")])
    poo = _facts(_diaper("d1", AT_9, "poo"))
    changed = ledger.apply([Change(uid="d1", before=ledger.get("d1"), after=poo)])
    assert changed == {
        S.DIAPER_CHANGES: _hour(AT_9),
        S.DIAPER_PEE: _hour(AT_9),
        S.DIAPER_POO: _hour(AT_9),
    }
    nudged = _facts(_diaper("d1", AT_9 + timedelta(minutes=1), "poo"))
    assert ledger.apply([Change(uid="d1", before=poo, after=nudged)]) == {}
    assert ledger.get("d1") == nudged


def test_apply_refuses_a_change_that_does_not_start_from_the_ledger() -> None:
    ledger = _ledger([_bottle("b1", AT_9, 90.0)])
    stale = _facts(_bottle("b1", AT_9, 60.0))
    with pytest.raises(ValueError):
        ledger.apply([Change(uid="b1", before=stale, after=None)])
    with pytest.raises(ValueError):
        ledger.apply([Change(uid="b1", before=None, after=stale)])
    assert ledger.get("b1") == _facts(_bottle("b1", AT_9, 90.0))


_KINDS = ("diaper_change", "bottle_feed", "nursing", "pumping", "sleep", "medicine")
_MEASURES: dict[str, tuple[str, ...]] = {
    "bottle_feed": ("feed_amount_ml",),
    "nursing": ("left_duration_s", "right_duration_s"),
    "pumping": ("left_amount_ml", "right_amount_ml", "duration_s"),
}


def _random_log(rng: random.Random, uid: str, timestamp: float, kind: str | None = None) -> CareLog:
    kind = kind or rng.choice(_KINDS[:4])
    fields: dict[str, Any] = {}
    if kind == "diaper_change":
        fields["change_type"] = rng.choice(["pee", "poo", "mixed", "dry", None])
    invalid: set[str] = set()
    for name in _MEASURES.get(kind, ()):
        roll = rng.random()
        if roll < 0.1:
            continue  # absent
        if roll < 0.15:
            invalid.add(name)
            continue
        top = 250.0 if name.endswith("_ml") else 1800.0
        fields[name] = round(rng.uniform(0, top), rng.choice([0, 1, 2]))
    return CareLog(
        uid=uid,
        log_type=kind,
        timestamp=timestamp,
        baby_uid="b",
        invalid_fields=frozenset(invalid),
        **fields,
    )


@pytest.mark.parametrize("seed", range(8))
def test_the_ledger_sums_exactly_like_hourly_amounts_in_any_order(seed: int) -> None:
    rng = random.Random(seed)
    base = DAY.timestamp()
    logs = [
        _random_log(rng, f"u{i}", base + rng.uniform(0, 12 * 3600), rng.choice(_KINDS))
        for i in range(150)
    ]
    # A few duplicate uids, dated differently: the later-dated copy counts.
    logs += [_random_log(rng, f"u{i}", base + rng.uniform(0, 12 * 3600)) for i in range(10)]
    now = DAY + timedelta(hours=20)
    expected = hourly_amounts(logs, now=now)
    for _ in range(5):
        rng.shuffle(logs)
        book = CareLogBook(reader=READER, min_gap=GAP)
        ledger = book.candidate([_read(logs, at=now)])
        result = ledger.amounts(START, hour_start(now.timestamp()))
        assert result == expected
        for hour, amounts in expected.amounts.items():
            for series, value in amounts.items():
                assert result.amounts[hour][series].hex() == value.hex()
                assert ledger.series_hours(series, hour, hour + 3600)[hour].hex() == value.hex()


def _check_ledger_against(ledger: Ledger, naive: dict[str, EntryFacts], rng: random.Random) -> None:
    """Compare every query with a recomputation from a plain dict."""
    assert len(ledger) == len(naive)
    for series in CareLogSeries:
        hours = [f.hour for f in naive.values() if (f.amount(series) or 0.0) > 0]
        assert ledger.first_hour(series) == min(hours, default=None)
    base = START - 3 * 3600
    for _ in range(3):
        lo = base + rng.randrange(-2, 40) * 3600
        hi = lo + rng.randrange(0, 30) * 3600
        inside = [f for f in naive.values() if lo <= f.hour < hi]
        assert ledger.amounts(lo, hi) == sum_contributions(f.contribution() for f in inside)
        series = rng.choice(list(CareLogSeries))
        by_hour: defaultdict[int, list[float]] = defaultdict(list)
        for f in inside:
            if (value := f.amount(series)) is not None:
                by_hour[f.hour].append(value)
        assert ledger.series_hours(series, lo, hi) == {
            hour: series_total(series, values) for hour, values in by_hour.items()
        }
        assert ledger.keep_hours(series, lo, hi) == {f.hour for f in inside if series in f.invalid}
        start = lo + rng.randrange(-1800, 1800)
        end = start + rng.randrange(-60, 20 * 3600)
        assert ledger.uids_between(start, end) == {
            uid for uid, f in naive.items() if start <= f.timestamp <= end
        }


@pytest.mark.parametrize("seed", range(4))
def test_every_ledger_query_matches_a_naive_recomputation(seed: int) -> None:
    rng = random.Random(seed)
    ledger = Ledger()
    naive: dict[str, EntryFacts] = {}
    base = START - 3 * 3600

    def random_facts(uid: str) -> EntryFacts:
        # Few hours, so entries share hours and first hours come and go.
        when = base + rng.randrange(0, 36) * 3600 + rng.uniform(0, 3599)
        return _facts(_random_log(rng, uid, when))

    for step in range(600):
        roll = rng.random()
        uid: str | None = None
        after: EntryFacts | None = None
        if roll < 0.45 or not naive:
            uid = f"u{step}"
            after = random_facts(uid)
        elif roll < 0.65:
            uid = rng.choice(sorted(naive))
            after = random_facts(uid)  # an edit, often a move
        elif roll < 0.8:
            uid = rng.choice(sorted(naive))  # a removal
        else:
            # Lower or remove an entry at some series' first hour.
            series = rng.choice(list(CareLogSeries))
            first = ledger.first_hour(series)
            candidates = sorted(u for u, f in naive.items() if f.hour == first)
            if not candidates:
                continue
            uid = rng.choice(candidates)
            old = naive[uid]
            if rng.random() < 0.5:
                zeroed = tuple((s, 0.0) for s, _ in old.raw)
                after = EntryFacts(timestamp=old.timestamp, hour=old.hour, raw=zeroed)
        ledger.apply([Change(uid=uid, before=naive.get(uid), after=after)])
        if after is None:
            naive.pop(uid, None)
        else:
            naive[uid] = after
        if step % 3 == 0:
            _check_ledger_against(ledger, naive, rng)
    _check_ledger_against(ledger, naive, rng)


def _varied_ledger(count: int, seed: int) -> Ledger:
    """Return a ledger of all four kinds, some amounts rejected, then some moved and removed."""
    rng = random.Random(seed)
    base = START - 3 * 3600

    def when() -> float:
        return base + rng.uniform(0, 500 * 3600)

    ledger = Ledger()
    ledger.apply(
        Change(
            uid=f"u{i}", before=None, after=_facts(_random_log(rng, f"u{i}", when(), _KINDS[i % 4]))
        )
        for i in range(count)
    )
    for i in range(0, count, 7):
        uid = f"u{i}"
        moved = _facts(_random_log(rng, uid, when(), _KINDS[i % 4]))
        ledger.apply([Change(uid=uid, before=ledger.get(uid), after=moved)])
    for i in range(3, count, 11):
        uid = f"u{i}"
        ledger.apply([Change(uid=uid, before=ledger.get(uid), after=None)])
    # Fill the lazily built sorted hours and first-hour cache too.
    for series in CareLogSeries:
        ledger.first_hour(series)
        ledger.series_hours(series, base, base + 500 * 3600)
    return ledger


def test_the_stored_entries_are_not_tracked_by_the_garbage_collector() -> None:
    ledger = _varied_ledger(3000, seed=1)
    gc.collect()
    records = list(ledger._entries.values())
    index = list(ledger._by_hour.values())
    assert len(records) == len(ledger) > 2500
    assert any(record[3] for record in records)  # some rejected amounts
    assert {type(record) for record in records} == {tuple}
    assert not any(gc.is_tracked(record) for record in records)
    assert sum(len(uids) for uids in index) == len(ledger)
    assert {type(uids) for uids in index} == {tuple}
    assert not any(gc.is_tracked(uids) for uids in index)


def _tracked_objects_added(build: Callable[[], Ledger]) -> tuple[Ledger, int]:
    gc.collect()
    before = len(gc.get_objects())
    ledger = build()
    gc.collect()
    return ledger, len(gc.get_objects()) - before


def test_the_tracked_objects_of_a_ledger_do_not_grow_with_its_entries() -> None:
    enabled = gc.isenabled()
    gc.disable()
    try:
        small, small_added = _tracked_objects_added(lambda: _varied_ledger(400, seed=2))
        large, large_added = _tracked_objects_added(lambda: _varied_ledger(4000, seed=2))
    finally:
        if enabled:
            gc.enable()
    assert len(large) > 9 * len(small)
    # The old layout added about six tracked objects per entry: some 20,000 here.
    assert large_added - small_added < 300


def _varied_logs(count: int, seed: int) -> list[CareLog]:
    rng = random.Random(seed)
    base = DAY.timestamp()
    return [_random_log(rng, f"u{i}", base + rng.uniform(0, 12 * 3600)) for i in range(count)]


def test_the_tracked_objects_of_a_read_do_not_grow_with_its_entries() -> None:
    """A pass waiting minutes for its second pass must not weigh on every full collection."""
    small_logs, large_logs = _varied_logs(400, seed=3), _varied_logs(4000, seed=3)
    enabled = gc.isenabled()
    gc.disable()
    try:
        gc.collect()
        before = len(gc.get_objects())
        small = _read(small_logs, at=T0)
        gc.collect()
        small_added = len(gc.get_objects()) - before
        large = _read(large_logs, at=T0)
        gc.collect()
        large_added = len(gc.get_objects()) - before - small_added
    finally:
        if enabled:
            gc.enable()
    assert len(large.entries) > 9 * len(small.entries) > 3000
    # EntryFacts held per entry added about six tracked objects each: 20,000 here.
    assert large_added - small_added < 300
    assert not any(gc.is_tracked(record) for record in large.records.values())
    # The facts are still there on demand, equal to those of each entry.
    sample = next(iter(large.entries))
    by_uid = {log.uid: log for log in large_logs}
    assert large.entries[sample] == _facts(by_uid[sample])


# ---------------------------------------------------------------------------
# Assessing a read
# ---------------------------------------------------------------------------


def _assess(ledger_logs: Iterable[CareLog], read_logs: Iterable[CareLog], **kw: Any) -> Assessment:
    return assess(_ledger(ledger_logs), _read(read_logs, at=T0, **kw))


def test_a_new_entry_and_a_safe_edit_are_safe_and_an_unchanged_one_is_nothing() -> None:
    result = _assess(
        [_bottle("b1", AT_9, 60.0), _bottle("b2", AT_9, 60.0), _diaper("d1")],
        [
            _bottle("b1", AT_9, 60.0),
            _bottle("b2", AT_9, 80.0),
            _diaper("d1", AT_9 + timedelta(minutes=3)),
            _bottle("b3", AT_10, 10.0),
        ],
    )
    assert sorted(c.uid for c in result.safe) == ["b2", "b3", "d1"]
    assert result.lowering == ()


def test_a_lowering_edit_and_an_absence_are_lowering() -> None:
    result = _assess(
        [_bottle("b1", AT_9, 60.0), _bottle("b2", AT_9, 60.0), _diaper("d1", AT_10, "poo")],
        [_bottle("b1", AT_9, 50.0), _diaper("d1", AT_10, "mixed")],
    )
    assert result.safe == ()
    assert {(c.uid, c.kind) for c in result.lowering} == {
        ("b1", "edit"),
        ("b2", "removal"),
        ("d1", "edit"),
    }


def test_an_absence_counts_only_within_the_read_bounds() -> None:
    edge = int(AT_10.timestamp())
    ledger_logs = [_bottle("b1", AT_9), _bottle("b2", AT_10)]
    result = _assess(ledger_logs, [], start=edge + 1, end=END)
    assert result.lowering == ()
    result = _assess(ledger_logs, [], start=edge, end=END)
    assert [c.uid for c in result.lowering] == ["b2"]


def test_a_returned_uid_at_another_time_is_a_move_wherever_its_old_time_was() -> None:
    old = DAY - timedelta(days=2)  # well outside the bounds
    result = _assess([_bottle("b1", old)], [_bottle("b1", AT_9)])
    assert [(c.uid, c.kind, c.lowers) for c in result.lowering] == [("b1", "edit", True)]
    assert result.safe == ()


def test_assessing_does_not_change_the_ledger() -> None:
    ledger = _ledger([_bottle("b1", AT_9, 60.0)])
    assess(ledger, _read([_bottle("b2", AT_10)], at=T0))
    assert len(ledger) == 1
    assert ledger.get("b1") == _facts(_bottle("b1", AT_9, 60.0))


def test_a_known_entry_with_a_rejected_amount_keeps_its_old_value() -> None:
    rejected = _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"}))
    result = _assess([_bottle("b1", AT_9, 90.0)], [rejected])
    assert result.lowering == ()
    (change,) = result.safe
    assert change.after is not None
    assert change.after.amount(S.BOTTLE_VOLUME) == 90.0
    assert change.after.invalid == frozenset({S.BOTTLE_VOLUME})


def test_a_carried_total_keeps_its_accepted_value() -> None:
    # One side rejected: the total and that side keep their accepted values.
    before = _log("n1", "nursing", AT_9, left_duration_s=600.0, right_duration_s=300.0)
    rejected = _log(
        "n1",
        "nursing",
        AT_9,
        right_duration_s=300.0,
        invalid_fields=frozenset({"left_duration_s"}),
    )
    result = _assess([before], [rejected])
    (change,) = result.safe
    assert change.after is not None
    assert change.after.amount(S.NURSING_TIME) == 900.0
    assert change.after.amount(S.NURSING_TIME_LEFT) == 600.0
    assert change.after.amount(S.NURSING_TIME_RIGHT) == 300.0


def test_a_rejected_amount_is_not_carried_to_another_hour() -> None:
    moved = _bottle("b1", AT_10, None, invalid_fields=frozenset({"feed_amount_ml"}))
    result = _assess([_bottle("b1", AT_9, 90.0)], [moved])
    (change,) = result.lowering
    assert change.after is not None
    assert change.after.amount(S.BOTTLE_VOLUME) is None


def test_a_new_entry_with_a_rejected_amount_adds_nothing_for_it_and_is_flagged() -> None:
    rejected = _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"}))
    (change,) = _assess([], [rejected]).safe
    assert change.after is not None
    assert change.after.amount(S.BOTTLE_VOLUME) is None
    assert change.after.invalid == frozenset({S.BOTTLE_VOLUME})


# ---------------------------------------------------------------------------
# Starting up
# ---------------------------------------------------------------------------


def _pass(
    logs: Sequence[CareLog], at: datetime, *, cuts: Sequence[int] = (), **kw: Any
) -> list[CareLogRead]:
    """Contiguous reads covering [START, END], split at ``cuts``."""
    edges = [START, *cuts, END + 1]
    return [
        _read(
            [log for log in logs if lo <= log.timestamp <= hi - 1],
            at=at + timedelta(seconds=i),
            start=lo,
            end=hi - 1,
            **kw,
        )
        for i, (lo, hi) in enumerate(pairwise(edges))
    ]


NOON = int(datetime(2026, 3, 10, 12, tzinfo=UTC).timestamp())


def test_a_candidate_is_built_without_changing_the_book() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    logs = [_bottle("b1", AT_9), _diaper("d1", AT_10)]
    ledger = book.candidate(_pass(logs, T0, cuts=[NOON]))
    assert len(ledger) == 2
    assert len(book.ledger) == 0
    assert not book.established


def _reads_at(*bounds: tuple[int, int], reader: str = READER) -> list[CareLogRead]:
    return [_read([], at=T0, start=start, end=end, reader=reader) for start, end in bounds]


@pytest.mark.parametrize(
    "reads",
    [
        pytest.param([], id="empty"),
        pytest.param(_reads_at((START, END), reader="reader-b"), id="other-reader"),
        pytest.param(_reads_at((NOON, END), (START, NOON - 1)), id="unsorted"),
        pytest.param(_reads_at((START, NOON - 1), (NOON + 1, END)), id="gap"),
        pytest.param(_reads_at((START, NOON + 1), (NOON, END)), id="overlap"),
    ],
)
def test_a_candidate_needs_contiguous_reads_by_this_reader(reads: list[CareLogRead]) -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    with pytest.raises(ValueError):
        book.candidate(reads)


def test_an_entry_in_two_reads_of_a_pass_keeps_the_later_read() -> None:
    # The entry was moved from the afternoon to the morning between requests:
    # the afternoon's read was taken first.
    book = CareLogBook(reader=READER, min_gap=GAP)
    afternoon = datetime(2026, 3, 10, 13, 5, tzinfo=UTC)
    reads = [
        _read(
            [_bottle("b1", AT_9, 90.0)],
            at=T0 + timedelta(seconds=5),
            start=START,
            end=NOON - 1,
        ),
        _read([_bottle("b1", afternoon, 50.0)], at=T0, start=NOON, end=END),
    ]
    ledger = book.candidate(reads)
    assert ledger.get("b1") == _facts(_bottle("b1", AT_9, 90.0))


def test_a_pass_may_share_boundary_seconds_and_keeps_an_entry_in_both_once() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    boundary = _log("b5", "bottle_feed", datetime.fromtimestamp(NOON, UTC), feed_amount_ml=40.0)
    reads = [
        _read([boundary], at=T0, start=START, end=NOON),
        _read([boundary], at=T0 + timedelta(seconds=1), start=NOON, end=END),
    ]
    ledger = book.candidate(reads)
    assert len(ledger) == 1
    assert ledger.series_hours(S.BOTTLE_VOLUME, NOON, NOON + 3600) == {NOON: 40.0}
    later = T0 + GAP + timedelta(seconds=1)
    second = [
        _read([boundary], at=later, start=START, end=NOON),
        _read([boundary], at=later, start=NOON, end=END),
    ]
    assert book.passes_agree(reads, second)


def test_passes_whose_reads_agree_pair_by_pair_but_whose_candidates_differ_disagree() -> None:
    """A bottle at the boundary second: the history says 100 mL, the recent read 30 mL.

    The first pass took its recent read before the history (as start-up
    does: the reader's answer comes first), the second re-read them in
    order, so each keeps the other value.
    """
    book = CareLogBook(reader=READER, min_gap=GAP)
    at_boundary = datetime.fromtimestamp(NOON, UTC)
    history = [_bottle("b5", at_boundary, 100.0)]
    recent = [_bottle("b5", at_boundary, 30.0)]
    first = [
        _read(history, at=T0 + timedelta(seconds=5), start=START, end=NOON),
        _read(recent, at=T0, start=NOON, end=END),
    ]
    later = T0 + GAP + timedelta(seconds=10)
    second = [
        _read(history, at=later, start=START, end=NOON),
        _read(recent, at=later + timedelta(seconds=5), start=NOON, end=END),
    ]
    # Each read agrees with its pair...
    for a, b in zip(first, second, strict=True):
        assert book.passes_agree([a], [b])
    # ...but the candidates hold 100 and 30 mL.
    assert book.candidate(first).get("b5") == _facts(history[0])
    assert book.candidate(second).get("b5") == _facts(recent[0])
    assert not book.passes_agree(first, second)
    # The same order in both passes: the same candidate, so they agree.
    again = [
        _read(history, at=later + timedelta(seconds=5), start=START, end=NOON),
        _read(recent, at=later, start=NOON, end=END),
    ]
    assert book.passes_agree(first, again)


def test_two_passes_agree() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    logs = [_bottle("b1", AT_9), _diaper("d1", AT_10)]
    first = _pass(logs, T0, cuts=[NOON])
    # The second has a new entry in the hour still running during the first.
    running = _diaper("d2", T0 + timedelta(minutes=10))
    second = _pass([*logs, running], T0 + GAP, cuts=[NOON])
    assert book.passes_agree(first, second)


_PASS_LOGS = [_bottle("b1", AT_9, 90.0), _diaper("d1", AT_10)]


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(_pass(_PASS_LOGS, T0 + GAP, cuts=[NOON + 60]), id="other-bounds"),
        pytest.param(_pass(_PASS_LOGS, T0 + GAP, cuts=[NOON, NOON + 60]), id="more-reads"),
        pytest.param(_pass(_PASS_LOGS, T0 + GAP, cuts=[NOON], dropped=1), id="dropped"),
        pytest.param(
            _pass(_PASS_LOGS, T0 + GAP - timedelta(seconds=1), cuts=[NOON]), id="too-soon"
        ),
        pytest.param(_pass(_PASS_LOGS[:1], T0 + GAP, cuts=[NOON]), id="missing"),
        pytest.param(_pass([*_PASS_LOGS, _diaper("d2", AT_9)], T0 + GAP, cuts=[NOON]), id="extra"),
        pytest.param(
            _pass([_bottle("b1", AT_9, 80.0), _PASS_LOGS[1]], T0 + GAP, cuts=[NOON]),
            id="changed",
        ),
        pytest.param(
            _pass(
                [
                    _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"})),
                    _PASS_LOGS[1],
                ],
                T0 + GAP,
                cuts=[NOON],
            ),
            id="rejected",
        ),
        pytest.param(
            _pass(_PASS_LOGS, T0 + GAP, cuts=[NOON], reader="reader-b"), id="other-reader"
        ),
    ],
)
def test_two_passes_disagree(second: list[CareLogRead]) -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    first = _pass(_PASS_LOGS, T0, cuts=[NOON])
    assert not book.passes_agree(first, second)


def test_a_sighting_since_the_first_pass_that_the_second_lacks_stops_agreement() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    without_b1 = _PASS_LOGS[1:]
    first = _pass(without_b1, T0, cuts=[NOON])
    second = _pass(without_b1, T0 + GAP, cuts=[NOON])
    between = T0 + timedelta(minutes=2)
    assert book.passes_agree(first, second)
    # A read in between returned b1, in a finished hour inside the pass.
    assert not book.passes_agree(first, second, [_read(_PASS_LOGS, at=between)])
    # A sighting that lacks an entry says nothing about it.
    assert book.passes_agree(first, second, [_read([], at=between)])


def test_a_sighting_with_other_facts_stops_agreement_but_not_outside_the_pass() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    first = _pass(_PASS_LOGS, T0, cuts=[NOON])
    second = _pass(_PASS_LOGS, T0 + GAP, cuts=[NOON])
    between = T0 + timedelta(minutes=2)
    edited = [_bottle("b1", AT_9, 80.0), _PASS_LOGS[1]]
    assert not book.passes_agree(first, second, [_read(edited, at=between)])
    # In the hour still running when the first pass began: not a disagreement.
    running = _diaper("d9", T0 + timedelta(minutes=1))
    assert book.passes_agree(first, second, [_read([*_PASS_LOGS, running], at=between)])
    # At a time outside the pass's bounds: not a disagreement either.
    outside = _bottle("b7", DAY - timedelta(hours=2), 40.0)
    wide = _read([*_PASS_LOGS, outside], at=between, start=START - 86400)
    assert book.passes_agree(first, second, [wide])


def test_a_first_pass_with_a_dropped_row_never_agrees() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    first = _pass(_PASS_LOGS, T0, cuts=[NOON], dropped=1)
    assert not book.passes_agree(first, _pass(_PASS_LOGS, T0 + GAP, cuts=[NOON]))


def test_adopting_replaces_the_ledger_and_clears_waiting_changes() -> None:
    book = _book([_bottle("b1"), _bottle("b2")])
    assert book.established
    outcome = book.process(_read([_bottle("b1")], at=T0))
    assert outcome.confirm is not None
    assert book.pending_requests()
    book.adopt([_read([_diaper("d1")], at=T0 + timedelta(minutes=1))])
    assert book.pending_requests() == ()
    assert set(book.ledger.uids_between(START, END)) == {"d1"}


# ---------------------------------------------------------------------------
# Running: the basic rule
# ---------------------------------------------------------------------------


def test_processing_needs_an_established_book_and_the_same_reader() -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    with pytest.raises(ValueError):
        book.process(_read([], at=T0))
    book.adopt([_read([], at=T0)])
    with pytest.raises(ValueError):
        book.process(_read([], at=T0 + GAP, reader="reader-b"))


def test_the_book_checks_its_settings() -> None:
    with pytest.raises(ValueError):
        CareLogBook(reader=READER, min_gap=timedelta(0))
    with pytest.raises(ValueError):
        CareLogBook(reader=READER, min_gap=GAP, max_attempts=0)
    with pytest.raises(ValueError):
        CareLogBook(reader=READER, min_gap=GAP, max_pending=0)


_LOWERING_READS: dict[str, list[CareLog]] = {
    "deleted": [_diaper("d1", AT_10)],
    "lowered": [_bottle("b1", AT_9, 30.0), _diaper("d1", AT_10)],
    "cleared": [_bottle("b1", AT_9, None), _diaper("d1", AT_10)],
    "moved": [_bottle("b1", AT_10, 90.0), _diaper("d1", AT_10)],
    "type": [_bottle("b1", AT_9, 90.0), _diaper("d1", AT_10, "poo")],
}


@pytest.mark.parametrize("name", sorted(_LOWERING_READS))
def test_a_lowering_change_is_never_applied_from_a_single_read(name: str) -> None:
    original = [_bottle("b1", AT_9, 90.0), _diaper("d1", AT_10, "pee")]
    book = _book(original)
    before = book.ledger.amounts(START, END)
    read = _read(_LOWERING_READS[name], at=T0)
    outcome = book.process(read)
    assert book.ledger.amounts(START, END) == before
    assert outcome.lowered == 0
    assert outcome.waiting == 1
    assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=T0 + GAP)
    # The same read again, or any number of copies of it, changes nothing.
    for _ in range(3):
        book.process(read)
        assert book.ledger.amounts(START, END) == before
    # A second read, fresh and agreeing, applies it.
    outcome = book.process(_read(_LOWERING_READS[name], at=T0 + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert book.ledger.amounts(START, END) != before


def test_an_entry_nanit_keeps_omitting_is_removed_after_the_confirming_read() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
    first = book.process(_read([_bottle("b2", AT_10)], at=T0))
    assert "b1" in book.ledger
    assert first.confirm is not None
    second = book.process(_read([_bottle("b2", AT_10)], at=first.confirm.not_before))
    assert second.confirmed
    assert "b1" not in book.ledger
    assert second.changed_from == {S.BOTTLE_FEEDS: _hour(AT_9), S.BOTTLE_VOLUME: _hour(AT_9)}
    assert second.confirm is None
    assert book.pending_requests() == ()


def test_an_entry_missing_from_one_read_and_back_in_the_next_stays() -> None:
    logs = [_bottle("b1", AT_9), _bottle("b2", AT_10)]
    book = _book(logs)
    book.process(_read([_bottle("b2", AT_10)], at=T0))
    outcome = book.process(_read(logs, at=T0 + GAP))
    assert not outcome.confirmed
    assert outcome.confirm is None
    assert "b1" in book.ledger
    # And a later single omission starts from scratch: still not removed.
    book.process(_read([_bottle("b2", AT_10)], at=T0 + 2 * GAP))
    assert "b1" in book.ledger


def test_an_entry_logged_late_and_missing_from_the_reread_is_not_removed() -> None:
    # Found by the simulation. The first read cannot show an entry logged
    # after it was taken, even one dated in a finished hour; the two reads
    # then agree on its absence, but only one answer actually missed it.
    book = _book([_bottle("b2", AT_10), _bottle("b3", AT_10)])
    book.process(_read([_bottle("b2", AT_10)], at=T0))  # b3 deleted: waits
    late = _bottle("b1", AT_9)
    nine = int(AT_9.timestamp())
    book.process(_read([late], at=T0 + timedelta(minutes=5), start=nine, end=nine))
    assert "b1" in book.ledger
    # The re-read misses b1 once: b3 goes, b1 stays and waits its turn.
    outcome = book.process(_read([_bottle("b2", AT_10)], at=T0 + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert "b3" not in book.ledger
    assert "b1" in book.ledger
    assert outcome.confirm is not None
    # Back in the next answer: kept for good.
    outcome = book.process(_read([late, _bottle("b2", AT_10)], at=T0 + 2 * GAP))
    assert outcome.confirm is None
    assert "b1" in book.ledger


def test_a_too_early_reread_neither_confirms_nor_uses_up_an_attempt() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)], max_attempts=1)
    without = [_bottle("b2", AT_10)]
    book.process(_read(without, at=T0))
    for minutes in (2, 5, 9):
        outcome = book.process(_read(without, at=T0 + timedelta(minutes=minutes)))
        assert not outcome.confirmed
        assert not outcome.gave_up
        # The request still dates from the first read.
        assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=T0 + GAP)
        assert "b1" in book.ledger
    assert book.process(_read(without, at=T0 + GAP)).confirmed
    assert "b1" not in book.ledger


def test_an_older_read_of_the_same_bounds_never_replaces_restarts_or_confirms_a_record() -> None:
    """80 at 10:06, a delayed 50 from 10:00, then 50 at 10:11: the 80 stands between."""
    book = _book([_bottle("b1", AT_9, 100.0)])
    first = book.process(_read([_bottle("b1", AT_9, 80.0)], at=T0 + timedelta(minutes=6)))
    request = ConfirmRequest(start=START, end=END, not_before=T0 + timedelta(minutes=6) + GAP)
    assert first.confirm == request

    delayed = book.process(_read([_bottle("b1", AT_9, 50.0)], at=T0))
    assert not delayed.confirmed
    assert delayed.confirm == request  # still waiting on the 80, not on the older 50
    assert book.pending_requests() == (request,)

    # A gap after the delayed read, but not after the 80: no confirmation.
    late = book.process(_read([_bottle("b1", AT_9, 50.0)], at=T0 + GAP + timedelta(minutes=1)))
    assert not late.confirmed
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 100.0
    # It disagrees with the 80, so it starts the next round itself.
    assert late.confirm == ConfirmRequest(
        start=START, end=END, not_before=T0 + 2 * GAP + timedelta(minutes=1)
    )


def test_a_read_that_may_not_lower_adds_and_cancels_but_starts_and_confirms_nothing() -> None:
    book = _book([_bottle("b1", AT_9, 90.0), _diaper("d1", AT_10)])
    added = _bottle("b2", AT_10, 30.0)
    # Neither starts a round for the missing diaper...
    outcome = book.process(_read([_bottle("b1", AT_9, 90.0), added], at=T0), may_lower=False)
    assert outcome.accepted == 1
    assert outcome.waiting == 1
    assert outcome.confirm is None
    assert book.pending_requests() == ()
    assert "b2" in book.ledger
    # ...nor confirms one that another read started.
    without = [_bottle("b1", AT_9, 90.0), added]
    book.process(_read(without, at=T0 + timedelta(minutes=1)))
    outcome = book.process(_read(without, at=T0 + GAP + timedelta(minutes=1)), may_lower=False)
    assert not outcome.confirmed
    assert "d1" in book.ledger
    assert len(book.pending_requests()) == 1
    # An ordinary read confirms it.
    assert book.process(_read(without, at=T0 + GAP + timedelta(minutes=2))).confirmed
    assert "d1" not in book.ledger


def test_a_confirming_read_needs_the_same_bounds() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
    book.process(_read([_bottle("b2", AT_10)], at=T0))
    # Wider bounds showing the same absence, later: nothing confirmed, and
    # no second record for the entry already waiting.
    outcome = book.process(_read([_bottle("b2", AT_10)], at=T0 + 2 * GAP, end=END + 3600))
    assert not outcome.confirmed
    assert outcome.confirm is None
    assert outcome.waiting == 1
    assert "b1" in book.ledger
    assert book.pending_requests() == (ConfirmRequest(start=START, end=END, not_before=T0 + GAP),)
    assert book.stats().pending_records == 1


def test_a_move_waits_in_its_old_hour_and_then_counts_once_in_the_new_one() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    moved = [_bottle("b1", AT_10, 90.0)]
    book.process(_read(moved, at=T0))
    assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 1
    assert _amount(book, S.BOTTLE_FEEDS, AT_10) == 0
    outcome = book.process(_read(moved, at=T0 + GAP))
    assert outcome.confirmed
    assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 0
    assert _amount(book, S.BOTTLE_FEEDS, AT_10) == 1
    assert _amount(book, S.BOTTLE_VOLUME, AT_10) == 90.0
    assert outcome.changed_from[S.BOTTLE_FEEDS] == _hour(AT_9)


def test_a_new_entry_is_accepted_at_once_while_a_deletion_in_the_same_read_waits() -> None:
    book = _book([_bottle("b1", AT_9)])
    outcome = book.process(_read([_diaper("d1", AT_10)], at=T0))
    assert outcome.accepted == 1
    assert outcome.waiting == 1
    assert "d1" in book.ledger
    assert "b1" in book.ledger
    assert outcome.changed_from == {S.DIAPER_CHANGES: _hour(AT_10), S.DIAPER_PEE: _hour(AT_10)}


def test_safe_changes_in_the_confirming_read_are_applied_too() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10, 40.0)])
    book.process(_read([_bottle("b2", AT_10, 40.0)], at=T0))
    # A new entry in the hour still running at the first read: they still agree.
    running = datetime(2026, 3, 10, 15, 25, tzinfo=UTC)
    outcome = book.process(_read([_bottle("b2", AT_10, 40.0), _diaper("d9", running)], at=T0 + GAP))
    assert outcome.confirmed
    assert outcome.accepted == 1
    assert outcome.lowered == 1
    assert "d9" in book.ledger
    assert "b1" not in book.ledger


def test_dropped_rows_in_either_read_prevent_confirmation() -> None:
    for first_dropped, second_dropped in ((1, 0), (0, 1)):
        book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
        without = [_bottle("b2", AT_10)]
        book.process(_read(without, at=T0, dropped=first_dropped))
        outcome = book.process(_read(without, at=T0 + GAP, dropped=second_dropped))
        assert not outcome.confirmed
        assert "b1" in book.ledger
        assert outcome.confirm is not None  # the newer read waits instead


def test_an_amount_raised_is_safe_and_one_lowered_or_cleared_waits() -> None:
    book = _book([_bottle("b1", AT_9, 60.0), _bottle("b2", AT_9, 60.0), _bottle("b3", AT_9, 60.0)])
    outcome = book.process(
        _read(
            [_bottle("b1", AT_9, 90.0), _bottle("b2", AT_9, 30.0), _bottle("b3", AT_9, None)],
            at=T0,
        )
    )
    assert outcome.accepted == 1
    assert outcome.waiting == 2
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 210.0
    book.process(
        _read(
            [_bottle("b1", AT_9, 90.0), _bottle("b2", AT_9, 30.0), _bottle("b3", AT_9, None)],
            at=T0 + GAP,
        )
    )
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 120.0
    assert book.ledger.series_hours(S.BOTTLE_FEEDS, START, END) == {_hour(AT_9): 3.0}


def test_a_diaper_type_change_waits_and_is_applied_as_one_unit() -> None:
    book = _book([_diaper("d1", AT_9, "pee")])
    poo = [_diaper("d1", AT_9, "poo")]
    book.process(_read(poo, at=T0))
    assert _amount(book, S.DIAPER_PEE, AT_9) == 1
    assert _amount(book, S.DIAPER_POO, AT_9) == 0
    outcome = book.process(_read(poo, at=T0 + GAP))
    assert outcome.lowered == 1
    assert _amount(book, S.DIAPER_PEE, AT_9) == 0
    assert _amount(book, S.DIAPER_POO, AT_9) == 1
    assert _amount(book, S.DIAPER_CHANGES, AT_9) == 1


def test_a_rejected_amount_on_a_known_entry_is_carried_forward_at_once() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    rejected = _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"}))
    outcome = book.process(_read([rejected], at=T0))
    assert outcome.accepted == 1
    assert outcome.waiting == 0
    assert outcome.confirm is None
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 90.0
    nine = _hour(AT_9)
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == {nine}
    # The valid value back, the same as before: it still waits for a re-read
    # (what is stored for the rejected amount is not known here), then the
    # flag is gone.
    outcome = book.process(_read([_bottle("b1", AT_9, 90.0)], at=T0 + GAP))
    assert outcome.waiting == 1
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == {nine}
    book.process(_read([_bottle("b1", AT_9, 90.0)], at=T0 + 2 * GAP))
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == set()


_REJECTED = _bottle(ml=None, invalid_fields=frozenset({"feed_amount_ml"}))


@pytest.mark.parametrize(
    ("before", "after", "lowers", "needs_confirmation"),
    [
        pytest.param(_REJECTED, _bottle(ml=30.0), False, True, id="rejected-then-lower"),
        pytest.param(_REJECTED, _bottle(ml=500.0), False, True, id="rejected-then-higher"),
        pytest.param(_REJECTED, _bottle(ml=None), False, True, id="rejected-then-absent"),
        pytest.param(_REJECTED, _REJECTED, False, False, id="still-rejected"),
        pytest.param(None, _bottle(ml=30.0), False, False, id="new"),
        pytest.param(_bottle(ml=30.0), _bottle(ml=60.0), False, False, id="raised"),
        pytest.param(_bottle(ml=60.0), _bottle(ml=30.0), True, True, id="lowered"),
        pytest.param(_bottle(ml=60.0), None, True, True, id="removed"),
    ],
)
def test_a_rejected_amount_becoming_valid_needs_confirmation_without_lowering(
    before: CareLog | None, after: CareLog | None, lowers: bool, needs_confirmation: bool
) -> None:
    change = Change(
        uid="u",
        before=None if before is None else _facts(before),
        after=None if after is None else _facts(after),
    )
    assert change.lowers is lowers
    assert change.needs_confirmation is needs_confirmation


@pytest.mark.parametrize("ml", [30.0, 150.0], ids=["lower", "higher"])
def test_an_amount_that_was_rejected_becomes_valid_only_after_a_confirming_read(ml: float) -> None:
    rejected = _bottle("b1", AT_9, None, invalid_fields=frozenset({"feed_amount_ml"}))
    book = _book([rejected])
    nine = _hour(AT_9)
    valid = [_bottle("b1", AT_9, ml)]

    outcome = book.process(_read(valid, at=T0))

    assert outcome.accepted == 0
    assert outcome.waiting == 1
    assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=T0 + GAP)
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == {nine}
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 0.0

    outcome = book.process(_read(valid, at=T0 + GAP))

    assert outcome.confirmed
    assert outcome.lowered == 1
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == set()
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == ml


def test_a_lowering_edit_with_a_rejected_amount_is_not_held_until_it_is_valid() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    rejected = [_bottle("b1", AT_10, None, invalid_fields=frozenset({"feed_amount_ml"}))]
    at = T0
    for _ in range(5):
        outcome = book.process(_read(rejected, at=at))
        assert outcome.lowered == 0
        assert outcome.waiting == 1
        assert outcome.confirm is None
        assert book.pending_requests() == ()
        assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 1
        assert _amount(book, S.BOTTLE_FEEDS, AT_10) == 0
        at += GAP
    valid = [_bottle("b1", AT_10, 60.0)]
    outcome = book.process(_read(valid, at=at))
    assert not outcome.confirmed
    assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=at + GAP)
    outcome = book.process(_read(valid, at=at + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 0
    assert _amount(book, S.BOTTLE_VOLUME, AT_10) == 60.0


RUNNING = datetime(2026, 3, 10, 15, 5, tzinfo=UTC)  # T0 is 15:20
AFTER_HOUR = datetime(2026, 3, 10, 16, 1, tzinfo=UTC)


def test_a_deletion_in_the_running_hour_is_held_once_the_hour_has_finished() -> None:
    book = _book([_bottle("b1", AT_9), _diaper("d1", RUNNING)])
    without = [_bottle("b1", AT_9)]
    for at in (T0, T0 + GAP, T0 + 3 * GAP):  # 15:20, 15:30, 15:50
        outcome = book.process(_read(without, at=at))
        assert outcome.waiting == 1
        assert outcome.confirm is None
        assert book.pending_requests() == ()
        assert "d1" in book.ledger
    outcome = book.process(_read(without, at=AFTER_HOUR))
    assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=AFTER_HOUR + GAP)
    outcome = book.process(_read(without, at=AFTER_HOUR + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert "d1" not in book.ledger


def test_a_move_into_the_running_hour_waits_counted_in_the_old_hour() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    moved = [_bottle("b1", RUNNING, 90.0)]
    for at in (T0, T0 + GAP):
        outcome = book.process(_read(moved, at=at))
        assert outcome.confirm is None
        assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 1
        assert _amount(book, S.BOTTLE_FEEDS, RUNNING) == 0
    assert book.process(_read(moved, at=AFTER_HOUR)).confirm is not None
    outcome = book.process(_read(moved, at=AFTER_HOUR + GAP))
    assert outcome.confirmed
    assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 0
    assert _amount(book, S.BOTTLE_FEEDS, RUNNING) == 1


# ---------------------------------------------------------------------------
# Running: disagreement, giving up, limits
# ---------------------------------------------------------------------------


def test_a_disagreeing_reread_starts_the_next_round_and_the_book_gives_up() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)], max_attempts=3)
    logs = [_bottle("b2", AT_10)]
    first = book.process(_read(logs, at=T0))
    assert first.confirm is not None
    at = T0
    for attempt in (2, 3):
        # Each re-read shows a new entry in a finished hour: they disagree.
        logs = [*logs, _diaper(f"d{attempt}", AT_10)]
        at += GAP
        outcome = book.process(_read(logs, at=at))
        assert not outcome.confirmed
        assert outcome.accepted == 1  # the new entry is still taken at once
        assert outcome.confirm == ConfirmRequest(start=START, end=END, not_before=at + GAP)
        assert "b1" in book.ledger
    logs = [*logs, _diaper("d4", AT_10)]
    at += GAP
    outcome = book.process(_read(logs, at=at))
    assert outcome.gave_up
    assert outcome.confirm is None
    assert book.pending_requests() == ()
    assert "b1" in book.ledger
    # A later read that still implies it starts again at attempt 1.
    at += GAP
    outcome = book.process(_read(logs, at=at))
    assert not outcome.gave_up
    assert outcome.confirm is not None
    outcome = book.process(_read(logs, at=at + GAP))
    assert outcome.confirmed
    assert "b1" not in book.ledger


def test_a_confirmation_re_holds_what_it_could_not_apply_at_attempt_one() -> None:
    # d1 is in the hour running at the first read, finished at the confirming one.
    first_at = datetime(2026, 3, 10, 15, 55, tzinfo=UTC)
    book = _book([_bottle("b1", AT_9), _diaper("d1", RUNNING)], max_attempts=1)
    without: list[CareLog] = []
    assert book.process(_read(without, at=first_at)).waiting == 2
    assert book.stats().waiting_entries == 1  # b1 only
    outcome = book.process(_read(without, at=first_at + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1  # b1
    assert not outcome.gave_up
    assert outcome.confirm is not None  # d1 now waits, at attempt 1
    assert book.stats().waiting_entries == 1
    outcome = book.process(_read(without, at=first_at + 2 * GAP))
    assert outcome.confirmed
    assert "d1" not in book.ledger


# ---------------------------------------------------------------------------
# Running: a later sighting cancels a waiting change
# ---------------------------------------------------------------------------

NINE = int(AT_9.timestamp())
LATER = T0 + timedelta(minutes=5)


def test_a_later_sighting_by_other_bounds_cancels_a_waiting_removal() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10), _bottle("b3", AT_10)])
    book.process(_read([_bottle("b2", AT_10)], at=T0))  # b1 and b3 wait
    assert book.stats().waiting_entries == 2
    # Another read, taken later, returns b1: the first read missed it.
    book.process(_read([_bottle("b1", AT_9)], at=LATER, start=NINE, end=NINE))
    assert book.stats().waiting_entries == 1
    # The re-read misses b1 again: only b3 goes, and b1 starts a fresh round.
    outcome = book.process(_read([_bottle("b2", AT_10)], at=T0 + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert "b3" not in book.ledger
    assert "b1" in book.ledger
    assert outcome.confirm is not None
    outcome = book.process(_read([_bottle("b2", AT_10)], at=T0 + 2 * GAP))
    assert outcome.confirmed
    assert "b1" not in book.ledger


def test_a_record_left_with_nothing_waiting_is_removed_by_a_later_sighting() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
    book.process(_read([_bottle("b2", AT_10)], at=T0))
    book.process(_read([_bottle("b1", AT_9)], at=LATER, start=NINE, end=NINE))
    assert book.pending_requests() == ()
    outcome = book.process(_read([_bottle("b2", AT_10)], at=T0 + GAP))
    assert not outcome.confirmed
    assert "b1" in book.ledger
    assert outcome.confirm is not None  # a fresh round


def test_a_later_sighting_with_the_old_values_cancels_a_waiting_lowering_edit() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0))
    book.process(_read([_bottle("b1", AT_9, 90.0)], at=LATER, start=NINE, end=NINE))
    assert book.pending_requests() == ()
    outcome = book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0 + GAP))
    assert not outcome.confirmed
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 90.0


def test_a_later_sighting_with_the_same_new_values_cancels_nothing() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0))
    book.process(_read([_bottle("b1", AT_9, 60.0)], at=LATER, start=NINE, end=NINE))
    assert book.stats().waiting_entries == 1
    outcome = book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0 + GAP))
    assert outcome.confirmed
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 60.0


def test_an_older_read_of_other_bounds_cancels_nothing() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0))
    older = T0 - timedelta(minutes=5)
    book.process(_read([_bottle("b1", AT_9, 90.0)], at=older, start=NINE, end=NINE))
    assert book.stats().waiting_entries == 1
    outcome = book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0 + GAP))
    assert outcome.confirmed
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 60.0


def test_the_book_holds_at_most_max_pending_records() -> None:
    logs = [_bottle(f"b{i}", AT_9 + timedelta(minutes=i)) for i in range(3)]
    book = _book(logs, max_pending=2)
    for i, log in enumerate(logs):
        second = int(log.timestamp)
        outcome = book.process(_read([], at=T0, start=second, end=second))
        assert (outcome.confirm is not None) is (i < 2)
    assert len(book.pending_requests()) == 2
    assert book.stats().pending_records == 2
    # Room again after one is dropped: the next read of the third is held.
    first = int(logs[0].timestamp)
    assert book.drop_pending(first, first)
    assert not book.drop_pending(first, first)
    third = int(logs[2].timestamp)
    assert book.process(_read([], at=T0, start=third, end=third)).confirm is not None


def test_pending_requests_list_every_record() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
    nine, ten = int(AT_9.timestamp()), int(AT_10.timestamp())
    book.process(_read([], at=T0, start=ten, end=ten))
    book.process(_read([], at=T0 + timedelta(minutes=1), start=nine, end=nine))
    assert book.pending_requests() == (
        ConfirmRequest(start=nine, end=nine, not_before=T0 + timedelta(minutes=1) + GAP),
        ConfirmRequest(start=ten, end=ten, not_before=T0 + GAP),
    )
    stats = book.stats()
    assert (stats.entries, stats.pending_records, stats.waiting_entries) == (2, 2, 2)
    assert stats.established


def test_dropping_every_waiting_record() -> None:
    book = _book([_bottle("b1", AT_9), _diaper("d1", AT_10)])
    book.process(_read([_diaper("d1", AT_10)], at=T0, end=NOON - 1))
    book.process(_read([], at=T0, start=_hour(AT_10)))
    assert len(book.pending_requests()) == 2

    assert book.drop_all_pending() == 2

    assert book.pending_requests() == ()
    assert book.drop_all_pending() == 0
    # Nothing can be confirmed now: the next read starts a new round.
    book.process(_read([_diaper("d1", AT_10)], at=T0 + GAP, end=NOON - 1))
    assert "b1" in book.ledger


def test_a_record_is_removed_when_nothing_lowering_is_left() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 60.0)], at=T0))
    assert book.pending_requests()
    # The edit is undone in the app before the re-read.
    outcome = book.process(_read([_bottle("b1", AT_9, 90.0)], at=T0 + GAP))
    assert outcome.confirm is None
    assert book.pending_requests() == ()
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 90.0


# ---------------------------------------------------------------------------
# Running: an older read processed late
# ---------------------------------------------------------------------------


def test_an_older_read_cannot_add_back_an_entry_a_newer_confirmation_removed() -> None:
    book = _book([_bottle("b1", AT_9), _bottle("b2", AT_10)])
    book.process(_read([_bottle("b2", AT_10)], at=T0))
    book.process(_read([_bottle("b2", AT_10)], at=T0 + GAP + timedelta(minutes=1)))
    assert "b1" not in book.ledger

    # A read taken between the two, of other bounds, arrives late.
    late = book.process(
        _read(
            [_bottle("b1", AT_9), _bottle("b2", AT_10)], at=T0 + timedelta(minutes=5), end=END - 1
        )
    )

    assert "b1" not in book.ledger
    assert (late.accepted, late.waiting) == (0, 0)
    assert late.changed_from == {}


def test_an_older_read_cannot_undo_a_newer_raise() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 150.0)], at=T0 + timedelta(minutes=6)))
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 150.0

    # A read from before the raise arrives late, then a newer read of its
    # bounds says the same, a gap after it: one read after the raise only.
    old = [_bottle("b1", AT_9, 90.0)]
    late = book.process(_read(old, at=T0 + timedelta(minutes=3), end=END - 1))
    assert late.waiting == 0
    book.process(_read(old, at=T0 + timedelta(minutes=16), end=END - 1))

    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 150.0
    assert len(book.pending_requests()) == 1
    # A second read after the raise confirms it, as any lowering.
    book.process(_read(old, at=T0 + timedelta(minutes=27), end=END - 1))
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 90.0


def test_a_read_later_than_the_last_change_behaves_as_before() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 150.0)], at=T0))
    raised = book.process(_read([_bottle("b1", AT_9, 200.0)], at=T0 + timedelta(minutes=1)))
    assert raised.accepted == 1
    assert _amount(book, S.BOTTLE_VOLUME, AT_9) == 200.0
    first = book.process(_read([], at=T0 + timedelta(minutes=2)))
    assert first.waiting == 1
    assert first.confirm is not None
    book.process(_read([], at=first.confirm.not_before))
    assert "b1" not in book.ledger


def test_changes_older_than_a_day_are_forgotten_and_so_is_every_read_that_old() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    book.process(_read([_bottle("b1", AT_9, 150.0)], at=T0))
    assert book._changed_at

    later = T0 + timedelta(days=1, hours=1)
    book.process(_read([_bottle("b1", AT_9, 150.0)], at=later))
    assert book._changed_at == {}
    # A read older than the bound says nothing at all, whatever it holds.
    stale = book.process(_read([_bottle("b9", AT_10)], at=T0 + timedelta(minutes=30)))
    assert "b9" not in book.ledger
    assert stale.accepted == 0
    # A read inside the bound still counts.
    fresh = book.process(_read([_bottle("b9", AT_10)], at=later - timedelta(minutes=1)))
    assert fresh.accepted == 1


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

_SECRET_UID = "MARKER-UID-5e1f"
_SECRET_READER = "MARKER-READER-a07c"


def test_no_uid_reader_or_time_shows_in_any_repr() -> None:
    when = datetime(2026, 3, 10, 9, 17, 41, tzinfo=UTC)
    log = _bottle(_SECRET_UID, when, 90.0)
    start, end = int(when.timestamp()) - 7, int(when.timestamp()) + 11
    book = CareLogBook(reader=_SECRET_READER, min_gap=GAP)
    first = CareLogRead.from_logs(
        [log], start=start, end=end, read_at=T0, dropped=0, reader=_SECRET_READER
    )
    book.adopt([first])
    empty = CareLogRead.from_logs(
        [], start=start, end=end, read_at=T0 + GAP, dropped=0, reader=_SECRET_READER
    )
    outcome = book.process(empty)
    facts = _facts(log)
    objects: list[object] = [
        facts,
        first,
        empty,
        Change(uid=_SECRET_UID, before=None, after=facts),
        Change(uid=_SECRET_UID, before=facts, after=None),
        Change(uid=_SECRET_UID, before=facts, after=facts),
        assess(book.ledger, empty),
        book.ledger,
        book,
        outcome,
        outcome.confirm,
        book.pending_requests(),
        book.stats(),
    ]
    numbers = {
        str(value)
        for value in (
            int(when.timestamp()),
            hour_start(when.timestamp()),
            start,
            end,
            int(T0.timestamp()),
        )
    }
    for obj in objects:
        for text in (repr(obj), str(obj)):
            assert _SECRET_UID not in text
            assert _SECRET_READER not in text
            assert not any(number in text for number in numbers), text
            assert "2026" not in text
    with pytest.raises(ValueError) as caught:
        book.process(
            CareLogRead.from_logs(
                [log], start=start, end=end, read_at=T0, dropped=0, reader="other-" + _SECRET_READER
            )
        )
    assert _SECRET_READER not in str(caught.value)


def test_the_module_needs_no_home_assistant_and_logs_nothing() -> None:
    with open(care_log_ledger.__file__, encoding="utf-8") as file:
        text = file.read()
    assert "homeassistant" not in text
    assert "logging" not in text
    assert "print(" not in text


# ---------------------------------------------------------------------------
# A randomised simulation of the whole rule
# ---------------------------------------------------------------------------

_COUNT_KINDS: dict[CareLogSeries, str] = {
    S.DIAPER_CHANGES: "diaper_change",
    S.BOTTLE_FEEDS: "bottle_feed",
    S.NURSING_SESSIONS: "nursing",
    S.PUMPING_SESSIONS: "pumping",
}


class _World:
    """The true entries, and a reader that is sometimes unreliable."""

    def __init__(self, rng: random.Random, now: datetime) -> None:
        self.rng = rng
        self.now = now
        self.truth: dict[str, CareLog] = {}
        self.stable: set[str] = set()  # never edited; may be omitted once
        self.omitted_once: set[str] = set()
        # Every uid ever truly dated in an hour, and ever of a kind.
        self.ever_in: defaultdict[tuple[str, int], set[str]] = defaultdict(set)
        self.ever_of: defaultdict[str, set[str]] = defaultdict(set)
        self.flaky = True
        self.next_uid = 0
        self.processed: list[CareLogRead] = []  # every read processed, in order

    def _set(self, log: CareLog) -> None:
        self.truth[log.uid] = log
        self.ever_in[(log.log_type, hour_start(log.timestamp))].add(log.uid)
        self.ever_of[log.log_type].add(log.uid)

    def add(self, *, stable: bool) -> None:
        uid = f"e{self.next_uid}"
        self.next_uid += 1
        # Mostly logged as it happens; sometimes late, for an earlier hour.
        late = self.rng.random() < 0.4
        ts = self.now.timestamp() - self.rng.uniform(0, 5 * 3600 if late else 1200)
        log = _random_log(self.rng, uid, ts)
        self._set(dataclasses.replace(log, invalid_fields=frozenset()))
        if stable:
            self.stable.add(uid)

    def mutate(self) -> None:
        rng = self.rng
        if rng.random() < 0.3:
            self.add(stable=rng.random() < 0.3)
        editable = sorted(set(self.truth) - self.stable)
        if not editable or rng.random() < 0.75:
            return
        uid = rng.choice(editable)
        log = self.truth[uid]
        roll = rng.random()
        if roll < 0.15:
            del self.truth[uid]
        elif roll < 0.35:
            # Moved within the window, out of it, or into it from older times.
            self._set(
                dataclasses.replace(log, timestamp=self.now.timestamp() - rng.uniform(0, 10 * 3600))
            )
        elif roll < 0.45:
            nudge = hour_start(log.timestamp) + rng.uniform(0, 3599)
            self._set(dataclasses.replace(log, timestamp=min(nudge, self.now.timestamp())))
        elif log.log_type == "diaper_change":
            kind = rng.choice(["pee", "poo", "mixed", "dry", None])
            self._set(dataclasses.replace(log, change_type=kind))
        elif names := _MEASURES.get(log.log_type):
            name = rng.choice(names)
            old = getattr(log, name)
            if old is None or rng.random() < 0.2:
                new = None if old is not None else round(rng.uniform(0, 200), 1)
            else:
                new = round(old * rng.choice([0.5, 0.9, 1.1, 2.0]), 1)
            self._set(dataclasses.replace(log, **{name: new}))

    def read(self, start: int, end: int) -> tuple[CareLogRead, str | None]:
        rng = self.rng
        present = sorted(
            (log for log in self.truth.values() if start <= log.timestamp <= end),
            key=lambda log: log.uid,
        )
        omitted: str | None = None
        dropped = 0
        returned = present
        if self.flaky:
            candidates = [log.uid for log in present if log.uid in self.stable - self.omitted_once]
            if candidates and rng.random() < 0.2:
                omitted = rng.choice(candidates)
                self.omitted_once.add(omitted)
            returned = [log for log in present if log.uid != omitted]
            if returned and rng.random() < 0.05:
                returned.pop(rng.randrange(len(returned)))
                dropped = 1
            returned = [self._maybe_reject(log) for log in returned]
        read = CareLogRead.from_logs(
            returned, start=start, end=end, read_at=self.now, dropped=dropped, reader=READER
        )
        return read, omitted

    def _maybe_reject(self, log: CareLog) -> CareLog:
        names = _MEASURES.get(log.log_type)
        if not names or self.rng.random() >= 0.005:
            return log
        name = self.rng.choice(names)
        return dataclasses.replace(log, **{name: None}, invalid_fields=frozenset({name}))


def _cells(amounts: HourlyAmounts) -> dict[tuple[int, CareLogSeries], float]:
    return {
        (hour, series): value
        for hour, by_series in amounts.amounts.items()
        for series, value in by_series.items()
    }


def _reads_agree(first: CareLogRead, second: CareLogRead) -> bool:
    """The test's own statement of agreement, written apart from the module."""
    cutoff = hour_start(first.read_at.timestamp())

    def finished(read: CareLogRead) -> dict[str, tuple[int, object, object]]:
        return {
            uid: (facts.hour, facts.raw, facts.invalid)
            for uid, facts in read.entries.items()
            if facts.hour < cutoff
        }

    return first.dropped == 0 and second.dropped == 0 and finished(first) == finished(second)


def _lowers(old: EntryFacts, new: EntryFacts | None) -> bool:
    """The test's own statement of a lowering change, written apart from the module."""
    if new is None or new.hour != old.hour:
        return True
    new_raw = dict(new.raw)
    return any(series not in new_raw or new_raw[series] < value for series, value in old.raw)


def _same_state(a: EntryFacts | None, b: EntryFacts | None) -> bool:
    if a is None or b is None:
        return a is b
    return (a.hour, a.raw, a.invalid) == (b.hour, b.raw, b.invalid)


def _confirmable(ledger: dict[str, EntryFacts], read: CareLogRead) -> set[str]:
    """The uids a re-read of this read could confirm a lowering change for."""
    cutoff = hour_start(read.read_at.timestamp())
    result = {
        uid
        for uid, old in ledger.items()
        if read.start <= old.timestamp <= read.end and uid not in read.entries and old.hour < cutoff
    }
    for uid, seen in read.entries.items():
        old = ledger.get(uid)
        if (
            old is not None
            and not seen.invalid
            and old.hour < cutoff
            and seen.hour < cutoff
            # Lowering, or a rejected amount made valid: either waits.
            and (_lowers(old, seen) or bool(old.invalid - seen.invalid))
        ):
            result.add(uid)
    return result


def _snapshot(book: CareLogBook) -> dict[str, EntryFacts]:
    result: dict[str, EntryFacts] = {}
    for uid in book.ledger.uids_between(-(2**40), 2**40):
        facts = book.ledger.get(uid)
        assert facts is not None
        result[uid] = facts
    return result


def _justified(
    first: CareLogRead,
    read: CareLogRead,
    uid: str,
    old: EntryFacts,
    processed: Sequence[CareLogRead],
    min_gap: timedelta,
) -> bool:
    """Whether a first read, then ``read``, may lower one entry."""
    cutoff = hour_start(first.read_at.timestamp())
    first_said = first.entries.get(uid)
    now_said = read.entries.get(uid)
    return (
        read.read_at - first.read_at >= min_gap
        and _reads_agree(first, read)
        and old.hour < cutoff
        and (now_said is None or now_said.hour < cutoff)
        and _same_state(first_said, now_said)
        and not any(
            later.read_at > first.read_at
            and (seen := later.entries.get(uid)) is not None
            and not _same_state(first_said, seen)
            for later in processed
        )
    )


def _check_process(
    book: CareLogBook,
    world: _World,
    read: CareLogRead,
    omitted: str | None,
    history: defaultdict[tuple[int, int], list[CareLogRead]],
    span: tuple[int, int],
) -> Outcome:
    before = _cells(book.ledger.amounts(*span))
    entries_before = _snapshot(book)
    confirmable = _confirmable(entries_before, read)
    outcome = book.process(read)
    after = _cells(book.ledger.amounts(*span))
    entries_after = _snapshot(book)
    bounds = (read.start, read.end)

    lowered = {cell for cell, value in before.items() if after.get(cell, 0.0) < value}
    if lowered:
        assert outcome.confirmed
        hours = {hour for hour, _ in lowered}
        assert any(
            read.read_at - first.read_at >= book.min_gap
            and _reads_agree(first, read)
            and max(hours) < hour_start(first.read_at.timestamp())
            for first in history[bounds]
        )

    # Per entry: lowered only when a fresh, agreeing first read of the same
    # bounds said the same about it, and no read taken since said otherwise.
    for uid, old in entries_before.items():
        new = entries_after.get(uid)
        if not _lowers(old, new):
            continue
        assert outcome.confirmed
        assert any(
            _justified(first, read, uid, old, world.processed, book.min_gap)
            for first in history[bounds]
        )

    # A record this read made holds something a re-read could confirm.
    if outcome.confirm is not None and outcome.confirm.not_before == read.read_at + book.min_gap:
        still = {
            uid
            for uid in confirmable
            if not _same_state(entries_after.get(uid), read.entries.get(uid))
        }
        assert still

    history[bounds].append(read)
    world.processed.append(read)

    # An entry left out of exactly one answer is never removed.
    if omitted is not None and omitted in entries_before:
        assert omitted in entries_after
    assert (entries_before.keys() & world.stable) <= entries_after.keys()

    # No double counting: per hour, never more entries than ever truly
    # dated in it; in all, never more than the distinct entries ever seen.
    for series, kind in _COUNT_KINDS.items():
        hours = book.ledger.series_hours(series, *span)
        assert sum(hours.values()) <= len(world.ever_of[kind])
        for hour, count in hours.items():
            assert count <= len(world.ever_in[(kind, hour)])
    return outcome


def _step_reads(
    book: CareLogBook,
    world: _World,
    window: timedelta,
    history: defaultdict[tuple[int, int], list[CareLogRead]],
    span: tuple[int, int],
) -> Counter[str]:
    """Make this step's rolling read and every re-read due; count what happened."""
    end = int(world.now.timestamp())
    rolling = [(end - int(window.total_seconds()), end)]
    due = [(r.start, r.end) for r in book.pending_requests() if r.not_before <= world.now]
    counts: Counter[str] = Counter()
    for start, stop in rolling + due:
        read, omitted = world.read(start, stop)
        outcome = _check_process(book, world, read, omitted, history, span)
        counts.update(
            lowered=outcome.lowered,
            confirmed=int(outcome.confirmed),
            gave_up=int(outcome.gave_up),
            omitted=int(omitted is not None),
        )
    return counts


@pytest.mark.parametrize("seed", [3, 17, 2026, 31337, 424242])
def test_a_simulated_history_never_lowers_wrongly_and_converges(seed: int) -> None:
    rng = random.Random(seed)
    window = timedelta(hours=8)
    step = timedelta(minutes=7)
    world = _World(rng, datetime(2026, 3, 10, 6, 3, 11, tzinfo=UTC))
    for _ in range(30):
        world.add(stable=rng.random() < 0.3)
    book = CareLogBook(reader=READER, min_gap=GAP)
    end = int(world.now.timestamp())
    world.flaky = False
    first, _ = world.read(end - int(window.total_seconds()), end)
    world.flaky = True
    book.adopt([first])
    span = (
        hour_start(world.now.timestamp()) - 48 * 3600,
        hour_start(world.now.timestamp()) + 96 * 3600,
    )
    history: defaultdict[tuple[int, int], list[CareLogRead]] = defaultdict(list)
    counts: Counter[str] = Counter()

    for _ in range(500):
        world.now += step
        world.mutate()
        counts += _step_reads(book, world, window, history, span)
    flaky_counts = counts.copy()

    # The truth stops changing and the reader becomes reliable.
    frozen_at = world.now
    world.flaky = False
    for _ in range(24):
        world.now += step
        counts += _step_reads(book, world, window, history, span)

    final_start = int(world.now.timestamp()) - int(window.total_seconds())
    lo = hour_start(final_start) + 3600  # hours wholly inside the last reads
    hi = hour_start(frozen_at.timestamp()) + 3600
    assert lo < hi
    truth = hourly_amounts(world.truth.values(), now=world.now)
    expected = {hour: amounts for hour, amounts in truth.amounts.items() if lo <= hour < hi}
    assert book.ledger.amounts(lo, hi).amounts == expected
    assert book.ledger.amounts(lo, hi).invalid == {}
    assert book.ledger.uids_between(lo, hi - 1) == {
        uid for uid, log in world.truth.items() if lo <= log.timestamp <= hi - 1
    }
    assert book.pending_requests() == ()
    # The simulation did exercise what it is meant to, while still flaky.
    assert flaky_counts["lowered"] >= 15
    assert flaky_counts["confirmed"] >= 15
    assert flaky_counts["gave_up"] >= 1
    assert flaky_counts["omitted"] >= 20
