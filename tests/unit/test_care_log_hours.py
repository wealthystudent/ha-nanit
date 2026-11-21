"""The hourly care log amounts behind the long-term statistics (pure, no recorder)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aionanit.models import CareLog
from custom_components.nanit import care_log_hours, coordinator
from custom_components.nanit.care_log_hours import (
    SERIES_UNITS,
    CareLogSeries,
    EntryContribution,
    SeriesUnit,
    entry_contribution,
    hour_start,
    hourly_amounts,
    series_total,
    to_series_units,
)

S = CareLogSeries
# Synthetic: a Tuesday afternoon, long after every entry below.
NOW = datetime(2026, 3, 10, 15, 20, tzinfo=UTC)


def _ts(when: datetime) -> float:
    return when.timestamp()


def _log(uid: str, kind: str, when: datetime, **fields: Any) -> CareLog:
    return CareLog(uid=uid, log_type=kind, timestamp=_ts(when), baby_uid="b", **fields)


def _hour(when: datetime) -> int:
    return int(when.replace(minute=0, second=0, microsecond=0).timestamp())


AT_9 = datetime(2026, 3, 10, 9, 10, tzinfo=UTC)
AT_10 = datetime(2026, 3, 10, 10, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The series
# ---------------------------------------------------------------------------


def test_the_sixteen_series_and_their_units() -> None:
    assert [series.value for series in CareLogSeries] == [
        "diaper_changes",
        "diaper_pee",
        "diaper_poo",
        "diaper_mixed",
        "diaper_dry",
        "bottle_feeds",
        "bottle_volume",
        "nursing_sessions",
        "nursing_time",
        "nursing_time_left",
        "nursing_time_right",
        "pumping_sessions",
        "pumped_volume",
        "pumped_volume_left",
        "pumped_volume_right",
        "pumping_time",
    ]
    assert set(SERIES_UNITS) == set(CareLogSeries)
    volumes = {s for s, unit in SERIES_UNITS.items() if unit is SeriesUnit.MILLILITERS}
    times = {s for s, unit in SERIES_UNITS.items() if unit is SeriesUnit.MINUTES}
    assert volumes == {
        S.BOTTLE_VOLUME,
        S.PUMPED_VOLUME,
        S.PUMPED_VOLUME_LEFT,
        S.PUMPED_VOLUME_RIGHT,
    }
    assert times == {S.NURSING_TIME, S.NURSING_TIME_LEFT, S.NURSING_TIME_RIGHT, S.PUMPING_TIME}


def test_the_kinds_match_the_coordinator() -> None:
    assert care_log_hours.DIAPER == coordinator.CARE_LOG_DIAPER
    assert care_log_hours.BOTTLE == coordinator.CARE_LOG_BOTTLE
    assert care_log_hours.NURSING == coordinator.CARE_LOG_NURSING
    assert care_log_hours.PUMPING == coordinator.CARE_LOG_PUMPING
    assert {
        care_log_hours.DIAPER,
        care_log_hours.BOTTLE,
        care_log_hours.NURSING,
        care_log_hours.PUMPING,
    } == coordinator.CARE_LOG_TYPES


def test_hour_start_is_the_utc_hour() -> None:
    assert hour_start(_ts(AT_9)) == _hour(AT_9)
    assert hour_start(_ts(datetime(2026, 3, 10, 9, 0, tzinfo=UTC))) == _hour(AT_9)
    assert hour_start(_ts(datetime(2026, 3, 10, 9, 59, 59, 999000, tzinfo=UTC))) == _hour(AT_9)
    assert hour_start(-1) == -3600


# ---------------------------------------------------------------------------
# Each kind
# ---------------------------------------------------------------------------


def test_every_kind_lands_in_the_hour_it_is_dated() -> None:
    logs = [
        _log("d1", "diaper_change", AT_9, change_type="pee"),
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=90.0),
        _log("n1", "nursing", AT_10, left_duration_s=600.0, right_duration_s=300.0),
        _log(
            "p1",
            "pumping",
            AT_10,
            left_amount_ml=40.0,
            right_amount_ml=35.5,
            duration_s=900.0,
        ),
    ]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {
        _hour(AT_9): {
            S.DIAPER_CHANGES: 1,
            S.DIAPER_PEE: 1,
            S.BOTTLE_FEEDS: 1,
            S.BOTTLE_VOLUME: 90.0,
        },
        _hour(AT_10): {
            S.NURSING_SESSIONS: 1,
            S.NURSING_TIME: 15.0,
            S.NURSING_TIME_LEFT: 10.0,
            S.NURSING_TIME_RIGHT: 5.0,
            S.PUMPING_SESSIONS: 1,
            S.PUMPED_VOLUME: 75.5,
            S.PUMPED_VOLUME_LEFT: 40.0,
            S.PUMPED_VOLUME_RIGHT: 35.5,
            S.PUMPING_TIME: 15.0,
        },
    }
    assert result.invalid == {}


def test_amounts_in_one_hour_add_up() -> None:
    logs = [
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=60.0),
        _log("b2", "bottle_feed", AT_9 + timedelta(minutes=20), feed_amount_ml=30.0),
        _log("d1", "diaper_change", AT_9, change_type="poo"),
        _log("d2", "diaper_change", AT_9, change_type="mixed"),
        _log("d3", "diaper_change", AT_9, change_type="dry"),
    ]
    assert hourly_amounts(logs, now=NOW).amounts == {
        _hour(AT_9): {
            S.BOTTLE_FEEDS: 2,
            S.BOTTLE_VOLUME: 90.0,
            S.DIAPER_CHANGES: 3,
            S.DIAPER_POO: 1,
            S.DIAPER_MIXED: 1,
            S.DIAPER_DRY: 1,
        }
    }


@pytest.mark.parametrize("change_type", [None, "wet", "", "PEE"])
def test_a_diaper_of_another_type_counts_only_as_a_change(change_type: str | None) -> None:
    logs = [_log("d1", "diaper_change", AT_9, change_type=change_type)]
    assert hourly_amounts(logs, now=NOW).amounts == {_hour(AT_9): {S.DIAPER_CHANGES: 1}}


def test_a_session_starting_at_11_55_puts_its_whole_time_in_the_11_00_hour() -> None:
    start = datetime(2026, 3, 10, 11, 55, tzinfo=UTC)
    logs = [
        _log("n1", "nursing", start, left_duration_s=1500.0, right_duration_s=900.0),
        _log("p1", "pumping", start, left_amount_ml=50.0, duration_s=2400.0),
    ]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {
        _hour(start): {
            S.NURSING_SESSIONS: 1,
            S.NURSING_TIME: 40.0,
            S.NURSING_TIME_LEFT: 25.0,
            S.NURSING_TIME_RIGHT: 15.0,
            S.PUMPING_SESSIONS: 1,
            S.PUMPED_VOLUME: 50.0,
            S.PUMPED_VOLUME_LEFT: 50.0,
            S.PUMPING_TIME: 40.0,
        }
    }
    assert _hour(start) == int(datetime(2026, 3, 10, 11, tzinfo=UTC).timestamp())


def test_sleep_and_unknown_kinds_are_ignored() -> None:
    logs = [
        _log("s1", "sleep", AT_9, duration_s=3600.0),
        _log("s2", "auto_sleep", AT_9, duration_s=3600.0, status="eligible"),
        _log("s3", "semi_auto_sleep", AT_9, duration_s=3600.0),
        _log("x1", "temperature", AT_9),
        _log("x2", "medicine", AT_9, duration_s=60.0),
    ]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {}
    assert result.invalid == {}


# ---------------------------------------------------------------------------
# Absent and invalid values
# ---------------------------------------------------------------------------


def test_an_absent_amount_adds_nothing_and_is_not_reported() -> None:
    logs = [
        _log("b1", "bottle_feed", AT_9),
        _log("n1", "nursing", AT_9, left_duration_s=300.0),
        _log("p1", "pumping", AT_9),
    ]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {
        _hour(AT_9): {
            S.BOTTLE_FEEDS: 1,
            S.NURSING_SESSIONS: 1,
            S.NURSING_TIME: 5.0,
            S.NURSING_TIME_LEFT: 5.0,
            S.PUMPING_SESSIONS: 1,
        }
    }
    assert result.invalid == {}


def test_an_invalid_amount_adds_nothing_and_is_reported_for_its_hour() -> None:
    logs = [
        _log("b1", "bottle_feed", AT_9, invalid_fields=frozenset({"feed_amount_ml"})),
        _log("b2", "bottle_feed", AT_9, feed_amount_ml=30.0),
        _log(
            "n1",
            "nursing",
            AT_10,
            right_duration_s=300.0,
            invalid_fields=frozenset({"left_duration_s"}),
        ),
        _log(
            "p1",
            "pumping",
            AT_10,
            left_amount_ml=20.0,
            invalid_fields=frozenset({"right_amount_ml", "duration_s"}),
        ),
    ]
    result = hourly_amounts(logs, now=NOW)
    # Counts are still counted; the rest of the hour's amounts still add up.
    assert result.amounts == {
        _hour(AT_9): {S.BOTTLE_FEEDS: 2, S.BOTTLE_VOLUME: 30.0},
        _hour(AT_10): {
            S.NURSING_SESSIONS: 1,
            S.NURSING_TIME: 5.0,
            S.NURSING_TIME_RIGHT: 5.0,
            S.PUMPING_SESSIONS: 1,
            S.PUMPED_VOLUME: 20.0,
            S.PUMPED_VOLUME_LEFT: 20.0,
        },
    }
    assert result.invalid == {
        _hour(AT_9): frozenset({S.BOTTLE_VOLUME}),
        _hour(AT_10): frozenset(
            {
                S.NURSING_TIME,
                S.NURSING_TIME_LEFT,
                S.PUMPED_VOLUME,
                S.PUMPED_VOLUME_RIGHT,
                S.PUMPING_TIME,
            }
        ),
    }


def test_an_invalid_amount_alone_still_reports_its_hour() -> None:
    logs = [_log("b1", "bottle_feed", AT_9, invalid_fields=frozenset({"feed_amount_ml"}))]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {_hour(AT_9): {S.BOTTLE_FEEDS: 1}}
    assert result.invalid == {_hour(AT_9): frozenset({S.BOTTLE_VOLUME})}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_an_unusable_number_the_library_let_through_counts_as_invalid(value: float) -> None:
    logs = [_log("b1", "bottle_feed", AT_9, feed_amount_ml=value)]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {_hour(AT_9): {S.BOTTLE_FEEDS: 1}}
    assert result.invalid == {_hour(AT_9): frozenset({S.BOTTLE_VOLUME})}


def test_an_entry_without_a_usable_time_is_left_out() -> None:
    logs = [
        CareLog(uid="d1", log_type="diaper_change", timestamp=float("nan"), baby_uid="b"),
        CareLog(uid="d2", log_type="diaper_change", timestamp=float("inf"), baby_uid="b"),
    ]
    assert hourly_amounts(logs, now=NOW).amounts == {}


def test_a_zero_amount_is_a_recorded_amount() -> None:
    logs = [_log("b1", "bottle_feed", AT_9, feed_amount_ml=0.0)]
    result = hourly_amounts(logs, now=NOW)
    assert result.amounts == {_hour(AT_9): {S.BOTTLE_FEEDS: 1, S.BOTTLE_VOLUME: 0.0}}
    assert result.invalid == {}


# ---------------------------------------------------------------------------
# Duplicates, arithmetic
# ---------------------------------------------------------------------------


def test_entries_are_counted_once_per_uid() -> None:
    logs = [
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=60.0),
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=60.0),
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=60.0),
    ]
    assert hourly_amounts(logs, now=NOW).amounts == {
        _hour(AT_9): {S.BOTTLE_FEEDS: 1, S.BOTTLE_VOLUME: 60.0}
    }


def test_of_two_copies_of_an_entry_the_later_dated_one_counts() -> None:
    # The same entry seen at two times (its date was edited between pieces).
    logs = [
        _log("b1", "bottle_feed", AT_10, feed_amount_ml=50.0),
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=60.0),
    ]
    assert hourly_amounts(logs, now=NOW).amounts == {
        _hour(AT_10): {S.BOTTLE_FEEDS: 1, S.BOTTLE_VOLUME: 50.0}
    }


def test_seconds_are_summed_before_turning_into_minutes() -> None:
    # Three 20-second sessions are one minute; rounding each to whole
    # minutes first would make them none.
    logs = [
        _log(f"n{i}", "nursing", AT_9 + timedelta(minutes=i), left_duration_s=20.0)
        for i in range(3)
    ]
    amounts = hourly_amounts(logs, now=NOW).amounts[_hour(AT_9)]
    assert amounts[S.NURSING_TIME] == 1.0
    assert amounts[S.NURSING_TIME_LEFT] == 1.0
    assert amounts[S.NURSING_SESSIONS] == 3


def test_fractional_amounts_are_kept_whole() -> None:
    logs = [
        _log("n1", "nursing", AT_9, left_duration_s=61.5),
        _log("b1", "bottle_feed", AT_9, feed_amount_ml=29.5735),
    ]
    amounts = hourly_amounts(logs, now=NOW).amounts[_hour(AT_9)]
    assert amounts[S.NURSING_TIME] == 61.5 / 60
    assert amounts[S.BOTTLE_VOLUME] == 29.5735


# ---------------------------------------------------------------------------
# Only finished hours
# ---------------------------------------------------------------------------


def test_the_hour_in_progress_and_later_hours_are_left_out() -> None:
    now = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)
    logs = [
        _log("d1", "diaper_change", datetime(2026, 3, 10, 11, 59, 59, tzinfo=UTC)),
        _log("d2", "diaper_change", datetime(2026, 3, 10, 12, 0, tzinfo=UTC)),
        _log("d3", "diaper_change", datetime(2026, 3, 10, 12, 29, tzinfo=UTC)),
        _log("d4", "diaper_change", datetime(2026, 3, 10, 13, 5, tzinfo=UTC)),
        _log(
            "b1",
            "bottle_feed",
            datetime(2026, 3, 10, 12, 10, tzinfo=UTC),
            invalid_fields=frozenset({"feed_amount_ml"}),
        ),
    ]
    result = hourly_amounts(logs, now=now)
    assert result.amounts == {
        int(datetime(2026, 3, 10, 11, tzinfo=UTC).timestamp()): {S.DIAPER_CHANGES: 1}
    }
    assert result.invalid == {}


def test_an_hour_is_finished_the_moment_it_ends() -> None:
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    logs = [
        _log("d1", "diaper_change", datetime(2026, 3, 10, 11, 30, tzinfo=UTC)),
        _log("d2", "diaper_change", now),
    ]
    assert list(hourly_amounts(logs, now=now).amounts) == [
        int(datetime(2026, 3, 10, 11, tzinfo=UTC).timestamp())
    ]


def test_now_may_be_in_any_time_zone_but_not_naive() -> None:
    logs = [_log("d1", "diaper_change", datetime(2026, 3, 10, 11, 30, tzinfo=UTC))]
    local_now = datetime(2026, 3, 10, 12, 10, tzinfo=UTC).astimezone(ZoneInfo("Asia/Kolkata"))
    assert len(hourly_amounts(logs, now=local_now).amounts) == 1
    with pytest.raises(ValueError):
        hourly_amounts(logs, now=datetime(2026, 3, 10, 12, 10))


def test_hours_come_out_oldest_first() -> None:
    logs = [
        _log("d3", "diaper_change", AT_10),
        _log("d1", "diaper_change", AT_9 - timedelta(days=400)),
        _log("d2", "diaper_change", AT_9),
    ]
    hours = list(hourly_amounts(logs, now=NOW).amounts)
    assert hours == sorted(hours)
    assert all(hour % 3600 == 0 for hour in hours)


# ---------------------------------------------------------------------------
# Rows follow UTC hours, not local ones
# ---------------------------------------------------------------------------


def test_a_half_hour_zone_midnight_falls_inside_one_utc_hour() -> None:
    """In India (UTC+5:30) local midnight is 18:30 UTC.

    Entries a quarter of an hour either side of it land in the same UTC
    hour, so a daily chart there can include a slice of the neighbouring
    day: documented, not a bug.
    """
    kolkata = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 3, 9, 23, 45, tzinfo=kolkata)
    after = datetime(2026, 3, 10, 0, 15, tzinfo=kolkata)
    logs = [
        _log("d1", "diaper_change", before),
        _log("d2", "diaper_change", after),
    ]
    utc_hour = int(datetime(2026, 3, 9, 18, tzinfo=UTC).timestamp())
    assert hourly_amounts(logs, now=NOW).amounts == {utc_hour: {S.DIAPER_CHANGES: 2}}


def test_the_repeated_hour_when_clocks_go_back_is_two_utc_hours() -> None:
    """In London on 2026-10-25 the hour from 01:00 to 02:00 happens twice."""
    london = ZoneInfo("Europe/London")
    first = datetime(2026, 10, 25, 1, 30, tzinfo=london, fold=0)  # 00:30 UTC
    second = datetime(2026, 10, 25, 1, 30, tzinfo=london, fold=1)  # 01:30 UTC
    logs = [
        _log("d1", "diaper_change", first),
        _log("b1", "bottle_feed", second, feed_amount_ml=30.0),
    ]
    now = datetime(2026, 10, 26, tzinfo=UTC)
    assert hourly_amounts(logs, now=now).amounts == {
        int(datetime(2026, 10, 25, 0, tzinfo=UTC).timestamp()): {S.DIAPER_CHANGES: 1},
        int(datetime(2026, 10, 25, 1, tzinfo=UTC).timestamp()): {
            S.BOTTLE_FEEDS: 1,
            S.BOTTLE_VOLUME: 30.0,
        },
    }


def test_the_missing_hour_when_clocks_go_forward_changes_nothing() -> None:
    """In London on 2026-03-29 local time jumps from 01:00 to 02:00.

    Either side of the jump is one UTC hour apart, as it is in real time.
    """
    london = ZoneInfo("Europe/London")
    before = datetime(2026, 3, 29, 0, 50, tzinfo=london)  # 00:50 UTC
    after = datetime(2026, 3, 29, 2, 10, tzinfo=london)  # 01:10 UTC
    logs = [
        _log("d1", "diaper_change", before),
        _log("d2", "diaper_change", after),
    ]
    now = datetime(2026, 3, 30, tzinfo=UTC)
    assert hourly_amounts(logs, now=now).amounts == {
        int(datetime(2026, 3, 29, 0, tzinfo=UTC).timestamp()): {S.DIAPER_CHANGES: 1},
        int(datetime(2026, 3, 29, 1, tzinfo=UTC).timestamp()): {S.DIAPER_CHANGES: 1},
    }


# ---------------------------------------------------------------------------
# One entry's contribution, and the shared summing
# ---------------------------------------------------------------------------


def test_an_entry_contribution_holds_its_hour_and_raw_amounts() -> None:
    nursing = _log("n1", "nursing", AT_10, left_duration_s=600.0, right_duration_s=300.0)
    assert entry_contribution(nursing) == EntryContribution(
        hour=_hour(AT_10),
        raw=(
            (S.NURSING_SESSIONS, 1.0),
            (S.NURSING_TIME, 900.0),
            (S.NURSING_TIME_LEFT, 600.0),
            (S.NURSING_TIME_RIGHT, 300.0),
        ),
        invalid=frozenset(),
    )
    diaper = _log("d1", "diaper_change", AT_9, change_type="poo")
    assert entry_contribution(diaper) == EntryContribution(
        hour=_hour(AT_9),
        raw=((S.DIAPER_CHANGES, 1.0), (S.DIAPER_POO, 1.0)),
        invalid=frozenset(),
    )


def test_an_entry_contribution_is_sorted_by_series() -> None:
    pumping = _log(
        "p1", "pumping", AT_9, left_amount_ml=40.0, right_amount_ml=35.5, duration_s=900.0
    )
    contribution = entry_contribution(pumping)
    assert contribution is not None
    names = [series.value for series, _ in contribution.raw]
    assert names == sorted(names)


def test_an_entry_contribution_flags_a_rejected_amount_and_adds_nothing_for_it() -> None:
    log = _log(
        "n1",
        "nursing",
        AT_9,
        right_duration_s=300.0,
        invalid_fields=frozenset({"left_duration_s"}),
    )
    assert entry_contribution(log) == EntryContribution(
        hour=_hour(AT_9),
        raw=(
            (S.NURSING_SESSIONS, 1.0),
            (S.NURSING_TIME, 300.0),
            (S.NURSING_TIME_RIGHT, 300.0),
        ),
        invalid=frozenset({S.NURSING_TIME, S.NURSING_TIME_LEFT}),
    )


def test_an_uncounted_kind_or_an_unusable_time_contributes_nothing() -> None:
    assert entry_contribution(_log("s1", "sleep", AT_9, duration_s=60.0)) is None
    assert entry_contribution(_log("x1", "medicine", AT_9)) is None
    no_time = CareLog(uid="d1", log_type="diaper_change", timestamp=float("nan"), baby_uid="b")
    assert entry_contribution(no_time) is None


def test_raw_totals_become_series_units_without_rounding() -> None:
    assert to_series_units(S.NURSING_TIME, 61.5) == 61.5 / 60
    assert to_series_units(S.PUMPING_TIME, 90.0) == 1.5
    assert to_series_units(S.BOTTLE_VOLUME, 29.5735) == 29.5735
    assert to_series_units(S.DIAPER_CHANGES, 3.0) == 3.0
    assert series_total(S.NURSING_TIME_LEFT, [20.0, 20.0, 20.0]) == 1.0


def test_totals_do_not_depend_on_the_order_entries_come_in() -> None:
    # One-decimal amounts: adding them up one by one gives a total that
    # changes in the last bit with the order.
    rng = random.Random(20261121)
    logs = [
        _log(
            f"b{i}",
            "bottle_feed",
            AT_9 + timedelta(seconds=i),
            feed_amount_ml=round(rng.uniform(0, 250), 1),
        )
        for i in range(60)
    ] + [
        _log(
            f"n{i}",
            "nursing",
            AT_9 + timedelta(seconds=i),
            left_duration_s=round(rng.uniform(0, 1800), 1),
            right_duration_s=round(rng.uniform(0, 1800), 1),
        )
        for i in range(60)
    ]
    expected = hourly_amounts(logs, now=NOW)
    for _ in range(20):
        shuffled = logs[:]
        rng.shuffle(shuffled)
        result = hourly_amounts(shuffled, now=NOW)
        assert result == expected
        # Bit for bit, not just equal within a tolerance.
        for hour, amounts in expected.amounts.items():
            for series, value in amounts.items():
                assert result.amounts[hour][series].hex() == value.hex()


def test_nothing_in_the_module_needs_home_assistant() -> None:
    source = care_log_hours.__file__
    with open(source, encoding="utf-8") as file:
        text = file.read()
    assert "homeassistant" not in text
