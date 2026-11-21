"""The care log ledger: when a long-term chart is allowed to go down (pure, no recorder)."""

from __future__ import annotations

import dataclasses
import operator
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
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
        # Outside the bounds: kept anyway, Nanit decides what is in range.
        _diaper("d1", DAY - timedelta(days=3)),
    ]
    read = _read(logs, at=T0)
    assert set(read.entries) == {"b1", "d1"}
    assert read.entries["b1"].hour == _hour(AT_10)
    assert read.entries["b1"].amount(S.BOTTLE_VOLUME) == 50.0


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
        pytest.param(_reads_at((START, NOON), (NOON, END)), id="overlap"),
    ],
)
def test_a_candidate_needs_contiguous_reads_by_this_reader(reads: list[CareLogRead]) -> None:
    book = CareLogBook(reader=READER, min_gap=GAP)
    with pytest.raises(ValueError):
        book.candidate(reads)


def test_an_entry_in_two_reads_of_a_pass_keeps_the_later_read() -> None:
    # The entry was moved from the morning to the afternoon between requests.
    book = CareLogBook(reader=READER, min_gap=GAP)
    afternoon = datetime(2026, 3, 10, 13, 5, tzinfo=UTC)
    reads = [
        _read(
            [_bottle("b1", afternoon, 50.0)],
            at=T0 + timedelta(seconds=5),
            start=START,
            end=NOON - 1,
        ),
        _read([_bottle("b1", AT_9, 90.0)], at=T0, start=NOON, end=END),
    ]
    ledger = book.candidate(reads)
    assert ledger.get("b1") == _facts(_bottle("b1", afternoon, 50.0))


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
    # The valid value back, the same as before: safe, and the flag is gone.
    book.process(_read([_bottle("b1", AT_9, 90.0)], at=T0 + GAP))
    assert book.ledger.keep_hours(S.BOTTLE_VOLUME, nine, nine + 3600) == set()


def test_a_lowering_edit_with_a_rejected_amount_waits_until_it_is_valid() -> None:
    book = _book([_bottle("b1", AT_9, 90.0)])
    rejected = [_bottle("b1", AT_10, None, invalid_fields=frozenset({"feed_amount_ml"}))]
    at = T0
    for _ in range(5):
        outcome = book.process(_read(rejected, at=at))
        assert outcome.lowered == 0
        assert outcome.confirm is not None
        assert not outcome.gave_up
        assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 1
        assert _amount(book, S.BOTTLE_FEEDS, AT_10) == 0
        at += GAP
    valid = [_bottle("b1", AT_10, 60.0)]
    assert not book.process(_read(valid, at=at)).confirmed  # disagrees with the rejected read
    outcome = book.process(_read(valid, at=at + GAP))
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert _amount(book, S.BOTTLE_FEEDS, AT_9) == 0
    assert _amount(book, S.BOTTLE_VOLUME, AT_10) == 60.0


def test_a_deletion_in_the_hour_running_at_the_first_read_waits_another_round() -> None:
    running = datetime(2026, 3, 10, 15, 5, tzinfo=UTC)  # T0 is 15:20
    book = _book([_bottle("b1", AT_9), _diaper("d1", running)])
    without = [_bottle("b1", AT_9)]
    book.process(_read(without, at=T0))
    outcome = book.process(_read(without, at=T0 + GAP))  # 15:30, cutoff 15:00
    assert outcome.confirmed
    assert outcome.lowered == 0
    assert "d1" in book.ledger
    assert outcome.confirm is not None
    after_hour = datetime(2026, 3, 10, 16, 1, tzinfo=UTC)
    outcome = book.process(_read(without, at=after_hour))  # cutoff still 15:00
    assert outcome.confirmed
    assert "d1" in book.ledger
    outcome = book.process(_read(without, at=after_hour + GAP))  # cutoff 16:00
    assert outcome.confirmed
    assert outcome.lowered == 1
    assert "d1" not in book.ledger


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


def test_a_confirmation_restarts_the_count_for_what_it_could_not_apply() -> None:
    running = datetime(2026, 3, 10, 15, 5, tzinfo=UTC)
    book = _book([_bottle("b1", AT_9), _diaper("d1", running)], max_attempts=1)
    without: list[CareLog] = []
    book.process(_read(without, at=T0))
    outcome = book.process(_read(without, at=T0 + GAP))
    assert outcome.lowered == 1  # b1
    assert not outcome.gave_up
    assert outcome.confirm is not None  # d1 waits again, at attempt 1


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


def _check_process(
    book: CareLogBook,
    world: _World,
    read: CareLogRead,
    omitted: str | None,
    history: defaultdict[tuple[int, int], list[CareLogRead]],
    span: tuple[int, int],
) -> Outcome:
    before = _cells(book.ledger.amounts(*span))
    before_uids = set(book.ledger.uids_between(-(2**40), 2**40))
    outcome = book.process(read)
    after = _cells(book.ledger.amounts(*span))
    after_uids = set(book.ledger.uids_between(-(2**40), 2**40))

    lowered = {cell for cell, value in before.items() if after.get(cell, 0.0) < value}
    if lowered:
        assert outcome.confirmed
        hours = {hour for hour, _ in lowered}
        earlier = history[(read.start, read.end)]
        assert any(
            read.read_at - first.read_at >= book.min_gap
            and _reads_agree(first, read)
            and max(hours) < hour_start(first.read_at.timestamp())
            for first in earlier
        )
    history[(read.start, read.end)].append(read)

    # An entry left out of exactly one answer is never removed.
    if omitted is not None and omitted in before_uids:
        assert omitted in after_uids
    assert (before_uids & world.stable) <= after_uids

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
