"""Which row shape gives the right period totals, by both of Home Assistant's queries.

The fixture is the counter-example from the plan's review: one count
series with an entry in the hour starting Monday 07:00 UTC and one in the
hour starting Tuesday 12:00 UTC, so the running total is 1, then 2. The
right total for Tuesday (the UTC day) is 1.

Two shapes are written:

- sparse: a row only for an hour that has an entry;
- dense: a row for every hour from the first entry to the last, the
  running total carried forward, with state 0 in an hour with nothing.

Each is read with ``statistics_during_period`` (change per day and per
hour) and with ``statistic_during_period`` (one change over the day). The
dense shape answers both correctly and is what ``statistic_rows`` builds.
The sparse shape is kept as an asserted counter-example:
``statistic_during_period`` reads its Tuesday as 0, and a day with no
entry as missing rather than 0.

Home Assistant's time zone is set to UTC, so a "day" period is the UTC day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistic_during_period,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries
from custom_components.nanit.care_log_statistics import (
    statistic_id,
    statistic_metadata,
)

from ..care_log_rows import statistic_rows

# Synthetic dates, safely in the past: Monday 9 to Wednesday 11 November 2026.
MONDAY = datetime(2026, 11, 9, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY = MONDAY + timedelta(days=3)
MONDAY_7 = MONDAY + timedelta(hours=7)
TUESDAY_12 = TUESDAY + timedelta(hours=12)
WEDNESDAY_12 = WEDNESDAY + timedelta(hours=12)

CODE = baby_code("baby_test_shape")
SERIES = CareLogSeries.DIAPER_CHANGES
STAT_ID = statistic_id(CODE, SERIES)


def _ts(when: datetime) -> int:
    return int(when.timestamp())


def _sparse(*hours: datetime) -> list[StatisticData]:
    """A row only for each hour with an entry (one entry each)."""
    return [
        StatisticData(start=hour, state=1.0, sum=float(total))
        for total, hour in enumerate(hours, start=1)
    ]


def _dense(*hours: datetime) -> list[StatisticData]:
    """A row for every hour from the first entry to the last, as the builder makes it."""
    return statistic_rows(
        {_ts(hour): 1.0 for hour in hours},
        start=_ts(hours[0]),
        end=_ts(hours[-1] + timedelta(hours=1)),
    )


@pytest.fixture
async def utc(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


async def _write(hass: HomeAssistant, rows: list[StatisticData]) -> None:
    async_add_external_statistics(hass, statistic_metadata(CODE, SERIES, "Shape test"), rows)
    await async_wait_recording_done(hass)


async def _periods(
    hass: HomeAssistant, start: datetime, end: datetime, period: str
) -> list[dict[str, Any]]:
    """What statistics_during_period returns for [start, end): start, change, state, sum.

    For a "day" period Home Assistant moves the end to the midnight after
    the end's own date, so an end of Wednesday 00:00 would bring in all of
    Wednesday. The day query is sent an end a microsecond earlier, the last
    moment of the range, which is how the half-open range is meant.
    """
    if period == "day":
        end -= timedelta(microseconds=1)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {STAT_ID},
        period,
        None,
        {"change", "state", "sum"},
    )
    return [
        {
            "start": datetime.fromtimestamp(row["start"], UTC),
            "change": row.get("change"),
            "state": row.get("state"),
            "sum": row.get("sum"),
        }
        for row in result.get(STAT_ID, [])
    ]


async def _change(hass: HomeAssistant, start: datetime, end: datetime) -> Any:
    """What statistic_during_period returns as the change over the period."""
    result = await get_instance(hass).async_add_executor_job(
        statistic_during_period, hass, start, end, STAT_ID, {"change"}, None
    )
    return result["change"]


def _hourly_total(rows: list[dict[str, Any]]) -> float:
    return sum(row["change"] for row in rows if row["change"] is not None)


# ---------------------------------------------------------------------------
# Monday 07:00 and Tuesday 12:00: Tuesday's total is 1
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("utc")
async def test_dense_rows_give_tuesday_a_total_of_1_by_both_queries(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _write(hass, _dense(MONDAY_7, TUESDAY_12))

    assert await _periods(hass, TUESDAY, WEDNESDAY, "day") == [
        {"start": TUESDAY, "change": 1.0, "state": 1.0, "sum": 2.0}
    ]
    hourly = await _periods(hass, TUESDAY, WEDNESDAY, "hour")
    # Every hour of Tuesday up to the last entry has a row: 00:00 to 12:00.
    assert [row["start"] for row in hourly] == [TUESDAY + timedelta(hours=h) for h in range(13)]
    assert [row["change"] for row in hourly] == [0.0] * 12 + [1.0]
    assert _hourly_total(hourly) == 1.0
    assert await _change(hass, TUESDAY, WEDNESDAY) == 1.0

    # Monday reads 1 by both queries too.
    assert [row["change"] for row in await _periods(hass, MONDAY, TUESDAY, "day")] == [1.0]
    assert await _change(hass, MONDAY, TUESDAY) == 1.0


@pytest.mark.usefixtures("utc")
async def test_sparse_rows_get_tuesday_wrong_by_one_of_the_queries(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The counter-example: rows only for hours with an entry."""
    await _write(hass, _sparse(MONDAY_7, TUESDAY_12))

    # statistics_during_period finds the total before the range and gets 1...
    assert await _periods(hass, TUESDAY, WEDNESDAY, "day") == [
        {"start": TUESDAY, "change": 1.0, "state": 1.0, "sum": 2.0}
    ]
    hourly = await _periods(hass, TUESDAY, WEDNESDAY, "hour")
    assert hourly == [{"start": TUESDAY_12, "change": 1.0, "state": 1.0, "sum": 2.0}]
    # ...but statistic_during_period looks for a row in the hour before the
    # range, finds none, takes the first total inside it as the starting
    # point, and reads Tuesday as 0.
    assert await _change(hass, TUESDAY, WEDNESDAY) == 0.0


# ---------------------------------------------------------------------------
# Monday 07:00 and Wednesday 12:00: an empty Tuesday inside the covered period
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("utc")
async def test_dense_rows_read_an_empty_day_as_zero(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _write(hass, _dense(MONDAY_7, WEDNESDAY_12))

    assert await _periods(hass, TUESDAY, WEDNESDAY, "day") == [
        {"start": TUESDAY, "change": 0.0, "state": 0.0, "sum": 1.0}
    ]
    hourly = await _periods(hass, TUESDAY, WEDNESDAY, "hour")
    assert len(hourly) == 24
    assert {row["change"] for row in hourly} == {0.0}
    assert await _change(hass, TUESDAY, WEDNESDAY) == 0.0
    # And the days either side are still right.
    assert await _change(hass, MONDAY, TUESDAY) == 1.0
    assert await _change(hass, WEDNESDAY, THURSDAY) == 1.0
    days = await _periods(hass, MONDAY, THURSDAY, "day")
    assert [(row["start"], row["change"]) for row in days] == [
        (MONDAY, 1.0),
        (TUESDAY, 0.0),
        (WEDNESDAY, 1.0),
    ]


@pytest.mark.usefixtures("utc")
async def test_sparse_rows_read_an_empty_day_as_missing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The counter-example again: an empty day has no value at all."""
    await _write(hass, _sparse(MONDAY_7, WEDNESDAY_12))

    assert await _periods(hass, TUESDAY, WEDNESDAY, "day") == []
    assert await _periods(hass, TUESDAY, WEDNESDAY, "hour") == []
    assert await _change(hass, TUESDAY, WEDNESDAY) is None
    # The day after it is read wrong too, for the same reason as above.
    assert await _change(hass, WEDNESDAY, THURSDAY) == 0.0
    days = await _periods(hass, MONDAY, THURSDAY, "day")
    assert [(row["start"], row["change"]) for row in days] == [(MONDAY, 1.0), (WEDNESDAY, 1.0)]
