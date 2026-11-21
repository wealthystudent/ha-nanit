"""The care log statistics format, written to and read back from a real recorder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import SERIES_UNITS, CareLogSeries, SeriesUnit
from custom_components.nanit.care_log_statistics import (
    async_statistic_names,
    statistic_baby_name,
    statistic_id,
    statistic_metadata,
    statistic_rows,
)

S = CareLogSeries
UID = "baby_test_format"
CODE = baby_code(UID)
# A synthetic hour, safely in the past.
HOUR_START = datetime(2026, 11, 9, 7, tzinfo=UTC)
H0 = int(HOUR_START.timestamp())
HOUR = 3600

AMOUNT = {SeriesUnit.COUNT: 2.0, SeriesUnit.MILLILITERS: 120.5, SeriesUnit.MINUTES: 12.25}


async def _metadata(hass: HomeAssistant, ids: set[str]) -> dict[str, dict[str, Any]]:
    found = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=ids)
    )
    return {sid: dict(meta) for sid, (_id, meta) in found.items()}


async def _rows(hass: HomeAssistant, ids: set[str], start: datetime, end: datetime) -> Any:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "hour", None, {"state", "sum"}
    )


async def test_one_row_of_every_series_reads_back(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    names = await async_statistic_names(hass, statistic_baby_name("Nursery", CODE))
    for series in CareLogSeries:
        amount = AMOUNT[SERIES_UNITS[series]]
        async_add_external_statistics(
            hass,
            statistic_metadata(CODE, series, names[series]),
            statistic_rows({H0: amount}, start=H0, end=H0 + HOUR),
        )
    await async_wait_recording_done(hass)

    ids = {statistic_id(CODE, series) for series in CareLogSeries}
    stored = await _metadata(hass, ids)
    assert set(stored) == ids
    rows = await _rows(hass, ids, HOUR_START, HOUR_START + timedelta(hours=1))
    for series in CareLogSeries:
        sid = statistic_id(CODE, series)
        unit_class, unit = {
            SeriesUnit.COUNT: (None, None),
            SeriesUnit.MILLILITERS: ("volume", "mL"),
            SeriesUnit.MINUTES: ("duration", "min"),
        }[SERIES_UNITS[series]]
        meta = stored[sid]
        assert meta["source"] == "nanit"
        assert meta["name"] == names[series]
        assert meta["name"].startswith("Nursery ")
        assert UID not in meta["name"]
        assert meta["has_sum"] is True
        assert meta["mean_type"] == 0
        assert (meta["unit_class"], meta["unit_of_measurement"]) == (unit_class, unit)
        amount = AMOUNT[SERIES_UNITS[series]]
        assert [(r["start"], r["state"], r["sum"]) for r in rows[sid]] == [
            (HOUR_START.timestamp(), amount, amount)
        ]

    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id(CODE, S.BOTTLE_VOLUME),
        True,
        {"state", "sum"},
    )
    assert [(r["state"], r["sum"]) for r in last[statistic_id(CODE, S.BOTTLE_VOLUME)]] == [
        (120.5, 120.5)
    ]


async def test_writing_an_hour_again_replaces_it(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    metadata = statistic_metadata(CODE, S.DIAPER_CHANGES, "Rewrite test")
    sid = metadata["statistic_id"]
    end = HOUR_START + timedelta(hours=3)

    async_add_external_statistics(
        hass, metadata, statistic_rows({H0: 1.0, H0 + HOUR: 2.0}, start=H0, end=H0 + 3 * HOUR)
    )
    await async_wait_recording_done(hass)
    # The second hour is corrected, and every later total with it.
    async_add_external_statistics(
        hass,
        metadata,
        statistic_rows({H0 + HOUR: 4.0}, start=H0 + HOUR, end=H0 + 3 * HOUR, sum_before=1.0),
    )
    await async_wait_recording_done(hass)

    rows = (await _rows(hass, {sid}, HOUR_START, end))[sid]
    assert [(r["state"], r["sum"]) for r in rows] == [(1.0, 1.0), (4.0, 5.0), (0.0, 5.0)]


async def test_a_key_left_out_of_a_rewritten_row_is_emptied(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Why every row carries both the hour's amount and the running total."""
    metadata = statistic_metadata(CODE, S.DIAPER_CHANGES, "Rewrite test")
    sid = metadata["statistic_id"]
    async_add_external_statistics(hass, metadata, [{"start": HOUR_START, "state": 1, "sum": 1}])
    await async_wait_recording_done(hass)
    async_add_external_statistics(hass, metadata, [{"start": HOUR_START, "state": 3}])
    await async_wait_recording_done(hass)

    rows = (await _rows(hass, {sid}, HOUR_START, HOUR_START + timedelta(hours=1)))[sid]
    assert [(r["state"], r["sum"]) for r in rows] == [(3.0, None)]


def _changed(metadata: StatisticMetaData, **changes: Any) -> dict[str, Any]:
    """The builder's metadata with fields it never sets changed."""
    return {**metadata, **changes}


VOLUME = statistic_metadata(CODE, S.BOTTLE_VOLUME, "Volume")
TIME = statistic_metadata(CODE, S.NURSING_TIME, "Time")
COUNT = statistic_metadata(CODE, S.BOTTLE_FEEDS, "Count")
GOOD_ROW = StatisticData(start=HOUR_START, state=1.0, sum=1.0)


@pytest.mark.parametrize(
    ("metadata", "rows", "message"),
    [
        (_changed(VOLUME, unit_class="duration"), [GOOD_ROW], "Unsupported unit_of_measurement"),
        (_changed(TIME, unit_class="volume"), [GOOD_ROW], "Unsupported unit_of_measurement"),
        (_changed(COUNT, unit_class="volume"), [GOOD_ROW], "Unsupported unit_of_measurement"),
        (_changed(VOLUME, unit_class="milk"), [GOOD_ROW], "Unsupported unit_class"),
        (_changed(VOLUME, statistic_id=f"nanit:{UID}_Bottle"), [GOOD_ROW], "Invalid statistic_id"),
        (_changed(VOLUME, source="recorder"), [GOOD_ROW], "Invalid source"),
        (VOLUME, [{"start": datetime(2026, 11, 9, 7), "state": 1, "sum": 1}], "Naive timestamp"),
        (VOLUME, [{"start": HOUR_START + timedelta(minutes=30), "state": 1, "sum": 1}], "top"),
        (VOLUME, [{"start": HOUR_START + timedelta(seconds=1), "state": 1, "sum": 1}], "top"),
    ],
    ids=[
        "volume-as-duration",
        "time-as-volume",
        "count-as-volume",
        "unknown-unit-class",
        "raw-id",
        "wrong-source",
        "naive",
        "half-past",
        "one-second-past",
    ],
)
async def test_home_assistant_rejects_what_the_builder_never_makes(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    message: str,
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        async_add_external_statistics(hass, metadata, [dict(row) for row in rows])
    await async_wait_recording_done(hass)
    assert await _metadata(hass, {metadata["statistic_id"]}) == {}
