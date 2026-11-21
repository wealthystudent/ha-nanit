"""Hourly care log amounts for the long-term statistics, on the UTC hour grid.

A pure calculation, kept free of Home Assistant so it is quick to test:
numbers-only care log entries in, the amount each of the sixteen series
gained in each finished UTC hour out.

The rules:

- Everything an entry records goes in the hour it is dated. A nursing or
  pumping session's whole recorded time goes in the hour it started;
  nothing is split across hours.
- A diaper change counts in ``diaper_changes``, and in its type's series
  only when the type is pee, poo, mixed or dry.
- An amount or duration that is absent adds nothing. One the library
  rejected (listed in ``invalid_fields``) adds nothing either, and its
  series is reported for that hour in ``HourlyAmounts.invalid``, so a
  caller can leave that hour's value as it is rather than lower it. Counts
  are not affected by an invalid amount.
- Sleep and unknown kinds are ignored. Entries are de-duplicated by uid
  first.
- Only finished hours are produced: the hour in progress and any later
  hour are left out.

Hours are keyed by the Unix time of their start, which is always a whole
multiple of 3600: Unix time has no leap seconds, so those are exactly the
UTC hours. Arithmetic is in Nanit's units (mL and seconds); durations are
turned into minutes only at the end, without rounding. Sums are exactly
rounded (``math.fsum``), so a total does not depend on the order entries
come in. ``entry_contribution`` and ``sum_contributions`` are the one
definition of what an entry adds; the care log ledger uses them too.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aionanit.models import CareLog

HOUR_SECONDS = 3600

# The kinds counted here. They match coordinator.CARE_LOG_* (checked by a
# test); this module keeps its own copy to stay free of Home Assistant.
DIAPER = "diaper_change"
BOTTLE = "bottle_feed"
NURSING = "nursing"
PUMPING = "pumping"


class SeriesUnit(StrEnum):
    """What a series measures."""

    COUNT = "count"
    MILLILITERS = "mL"
    MINUTES = "min"


class CareLogSeries(StrEnum):
    """The sixteen non-sleep series. The value is the statistic name's suffix."""

    DIAPER_CHANGES = "diaper_changes"
    DIAPER_PEE = "diaper_pee"
    DIAPER_POO = "diaper_poo"
    DIAPER_MIXED = "diaper_mixed"
    DIAPER_DRY = "diaper_dry"
    BOTTLE_FEEDS = "bottle_feeds"
    BOTTLE_VOLUME = "bottle_volume"
    NURSING_SESSIONS = "nursing_sessions"
    NURSING_TIME = "nursing_time"
    NURSING_TIME_LEFT = "nursing_time_left"
    NURSING_TIME_RIGHT = "nursing_time_right"
    PUMPING_SESSIONS = "pumping_sessions"
    PUMPED_VOLUME = "pumped_volume"
    PUMPED_VOLUME_LEFT = "pumped_volume_left"
    PUMPED_VOLUME_RIGHT = "pumped_volume_right"
    PUMPING_TIME = "pumping_time"


SERIES_UNITS: Mapping[CareLogSeries, SeriesUnit] = {
    CareLogSeries.DIAPER_CHANGES: SeriesUnit.COUNT,
    CareLogSeries.DIAPER_PEE: SeriesUnit.COUNT,
    CareLogSeries.DIAPER_POO: SeriesUnit.COUNT,
    CareLogSeries.DIAPER_MIXED: SeriesUnit.COUNT,
    CareLogSeries.DIAPER_DRY: SeriesUnit.COUNT,
    CareLogSeries.BOTTLE_FEEDS: SeriesUnit.COUNT,
    CareLogSeries.BOTTLE_VOLUME: SeriesUnit.MILLILITERS,
    CareLogSeries.NURSING_SESSIONS: SeriesUnit.COUNT,
    CareLogSeries.NURSING_TIME: SeriesUnit.MINUTES,
    CareLogSeries.NURSING_TIME_LEFT: SeriesUnit.MINUTES,
    CareLogSeries.NURSING_TIME_RIGHT: SeriesUnit.MINUTES,
    CareLogSeries.PUMPING_SESSIONS: SeriesUnit.COUNT,
    CareLogSeries.PUMPED_VOLUME: SeriesUnit.MILLILITERS,
    CareLogSeries.PUMPED_VOLUME_LEFT: SeriesUnit.MILLILITERS,
    CareLogSeries.PUMPED_VOLUME_RIGHT: SeriesUnit.MILLILITERS,
    CareLogSeries.PUMPING_TIME: SeriesUnit.MINUTES,
}

_DIAPER_TYPES: Mapping[str, CareLogSeries] = {
    "pee": CareLogSeries.DIAPER_PEE,
    "poo": CareLogSeries.DIAPER_POO,
    "mixed": CareLogSeries.DIAPER_MIXED,
    "dry": CareLogSeries.DIAPER_DRY,
}

# Each measurement field of an entry, by kind, and the series it adds to.
# A total series (nursing time, pumped volume) takes every side.
_MEASUREMENTS: Mapping[str, tuple[tuple[str, tuple[CareLogSeries, ...]], ...]] = {
    BOTTLE: (("feed_amount_ml", (CareLogSeries.BOTTLE_VOLUME,)),),
    NURSING: (
        ("left_duration_s", (CareLogSeries.NURSING_TIME, CareLogSeries.NURSING_TIME_LEFT)),
        ("right_duration_s", (CareLogSeries.NURSING_TIME, CareLogSeries.NURSING_TIME_RIGHT)),
    ),
    PUMPING: (
        ("left_amount_ml", (CareLogSeries.PUMPED_VOLUME, CareLogSeries.PUMPED_VOLUME_LEFT)),
        ("right_amount_ml", (CareLogSeries.PUMPED_VOLUME, CareLogSeries.PUMPED_VOLUME_RIGHT)),
        ("duration_s", (CareLogSeries.PUMPING_TIME,)),
    ),
}

_COUNTS: Mapping[str, CareLogSeries] = {
    DIAPER: CareLogSeries.DIAPER_CHANGES,
    BOTTLE: CareLogSeries.BOTTLE_FEEDS,
    NURSING: CareLogSeries.NURSING_SESSIONS,
    PUMPING: CareLogSeries.PUMPING_SESSIONS,
}


@dataclass(frozen=True)
class HourlyAmounts:
    """What each finished UTC hour gained, per series.

    ``amounts`` maps an hour's start (Unix seconds) to the series an entry
    added to in that hour; a series that is not there gained nothing. Counts
    are entries, volumes mL and times minutes. ``invalid`` maps an hour to
    the series for which an entry in it carried a rejected value: that
    hour's amount for them is incomplete, and should not lower a stored one.
    """

    amounts: Mapping[int, Mapping[CareLogSeries, float]]
    invalid: Mapping[int, frozenset[CareLogSeries]]


@dataclass(frozen=True, slots=True)
class EntryContribution:
    """What one entry adds, all of it in the hour it is dated.

    ``raw`` holds, sorted by series, the amount the entry adds to each series
    it adds to, in Nanit's units (1 for a count, mL, seconds). ``invalid``
    holds the series for which one of its measurements was rejected; such a
    measurement adds nothing to ``raw``.
    """

    hour: int
    raw: tuple[tuple[CareLogSeries, float], ...]
    invalid: frozenset[CareLogSeries]


def hour_start(timestamp: float) -> int:
    """Return the start of the UTC hour a Unix time falls in."""
    return math.floor(timestamp / HOUR_SECONDS) * HOUR_SECONDS


def latest_by_uid(logs: Iterable[CareLog]) -> list[CareLog]:
    """Keep one entry per uid: the later-dated copy, as the sensors do."""
    unique: dict[str, CareLog] = {}
    for log in logs:
        current = unique.get(log.uid)
        if current is None or log.timestamp > current.timestamp:
            unique[log.uid] = log
    return list(unique.values())


def _measurement(log: CareLog, name: str) -> tuple[float | None, bool]:
    """Return a measurement and whether it was rejected.

    The library already turns a rejected value into None and lists it in
    ``invalid_fields``; a value that is somehow not a finite, non-negative
    number is treated the same way.
    """
    if name in log.invalid_fields:
        return None, True
    value = getattr(log, name)
    if value is None:
        return None, False
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None, True
    return float(value), False


def entry_contribution(log: CareLog) -> EntryContribution | None:
    """Return what one entry adds, or None when it adds nothing.

    Sleep and unknown kinds, and an entry without a finite time, add nothing.
    """
    count_series = _COUNTS.get(log.log_type)
    if count_series is None or not math.isfinite(log.timestamp):
        return None
    parts: defaultdict[CareLogSeries, list[float]] = defaultdict(list)
    invalid: set[CareLogSeries] = set()
    parts[count_series].append(1.0)
    if log.log_type == DIAPER and (type_series := _DIAPER_TYPES.get(log.change_type or "")):
        parts[type_series].append(1.0)
    for name, targets in _MEASUREMENTS.get(log.log_type, ()):
        value, rejected = _measurement(log, name)
        if rejected:
            invalid.update(targets)
        elif value is not None:
            for series in targets:
                parts[series].append(value)
    raw = tuple(
        sorted(
            ((series, math.fsum(values)) for series, values in parts.items()),
            key=lambda item: item[0].value,
        )
    )
    return EntryContribution(hour=hour_start(log.timestamp), raw=raw, invalid=frozenset(invalid))


def to_series_units(series: CareLogSeries, raw_total: float) -> float:
    """Turn a raw total (count, mL or seconds) into the series' unit.

    Seconds become minutes, without rounding; anything else is unchanged.
    """
    return raw_total / 60 if SERIES_UNITS[series] is SeriesUnit.MINUTES else raw_total


def series_total(series: CareLogSeries, raw_amounts: Iterable[float]) -> float:
    """Return the total of some raw amounts of one series, in the series' unit.

    The sum is exactly rounded (``math.fsum``), so it does not depend on the
    order the amounts come in.
    """
    return to_series_units(series, math.fsum(raw_amounts))


def sum_contributions(contributions: Iterable[EntryContribution]) -> HourlyAmounts:
    """Add up entries' contributions per hour and series, oldest hour first."""
    raw: defaultdict[int, defaultdict[CareLogSeries, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    invalid: defaultdict[int, set[CareLogSeries]] = defaultdict(set)
    for contribution in contributions:
        hour_raw = raw[contribution.hour]
        for series, amount in contribution.raw:
            hour_raw[series].append(amount)
        if contribution.invalid:
            invalid[contribution.hour].update(contribution.invalid)
    amounts = {
        hour: {series: series_total(series, values) for series, values in hour_raw.items()}
        for hour, hour_raw in sorted(raw.items())
    }
    return HourlyAmounts(
        amounts=amounts,
        invalid={hour: frozenset(series) for hour, series in sorted(invalid.items())},
    )


def hourly_amounts(logs: Iterable[CareLog], *, now: datetime) -> HourlyAmounts:
    """Return what each finished UTC hour gained, from numbers-only entries.

    ``now`` must be timezone-aware. An hour is finished when it ends at or
    before ``now``.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    first_unfinished = hour_start(now.timestamp())
    contributions = (entry_contribution(log) for log in latest_by_uid(logs))
    return sum_contributions(
        contribution
        for contribution in contributions
        if contribution is not None and contribution.hour < first_unfinished
    )
