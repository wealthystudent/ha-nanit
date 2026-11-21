"""The care log statistics format: ids, names, metadata and rows (no recorder)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from custom_components.nanit import care_log_statistics
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries
from custom_components.nanit.care_log_statistics import (
    FALLBACK_NAMES,
    async_statistic_names,
    series_amounts,
    statistic_baby_name,
    statistic_id,
    statistic_metadata,
    statistic_names,
    statistic_rows,
)

S = CareLogSeries
UID = "baby_test_0001"
CODE = baby_code(UID)
H0 = int(datetime(2026, 11, 9, 7, tzinfo=UTC).timestamp())
HOUR = 3600

COUNTS = {
    S.DIAPER_CHANGES,
    S.DIAPER_PEE,
    S.DIAPER_POO,
    S.DIAPER_MIXED,
    S.DIAPER_DRY,
    S.BOTTLE_FEEDS,
    S.NURSING_SESSIONS,
    S.PUMPING_SESSIONS,
}
VOLUMES = {S.BOTTLE_VOLUME, S.PUMPED_VOLUME, S.PUMPED_VOLUME_LEFT, S.PUMPED_VOLUME_RIGHT}
TIMES = {S.NURSING_TIME, S.NURSING_TIME_LEFT, S.NURSING_TIME_RIGHT, S.PUMPING_TIME}


# ---------------------------------------------------------------------------
# Ids and metadata
# ---------------------------------------------------------------------------


def test_every_series_has_a_valid_id_made_of_the_code() -> None:
    ids = {statistic_id(CODE, series) for series in CareLogSeries}
    assert len(ids) == 16
    for series in CareLogSeries:
        sid = statistic_id(CODE, series)
        assert sid == f"nanit:{CODE}_{series.value}"
        assert valid_statistic_id(sid)
        assert UID not in sid
    assert statistic_id(CODE, S.PUMPED_VOLUME_LEFT) == (
        "nanit:71dc386253a3e9dd0777854a0c04fc5a_pumped_volume_left"
    )


@pytest.mark.parametrize("code", ["baby_1", CODE.upper(), CODE[:31], CODE + "0", ""])
def test_only_a_baby_code_makes_an_id(code: str) -> None:
    with pytest.raises(ValueError):
        statistic_id(code, S.DIAPER_CHANGES)


def test_the_metadata_of_every_series() -> None:
    assert set(CareLogSeries) == COUNTS | VOLUMES | TIMES
    for series in CareLogSeries:
        metadata = statistic_metadata(CODE, series, "A name")
        assert metadata["statistic_id"] == statistic_id(CODE, series)
        assert metadata["source"] == "nanit"
        assert metadata["name"] == "A name"
        assert metadata["has_sum"] is True
        assert metadata["mean_type"] is StatisticMeanType.NONE
        assert "has_mean" not in metadata
        expected = (
            (None, None)
            if series in COUNTS
            else ("volume", "mL")
            if series in VOLUMES
            else ("duration", "min")
        )
        assert (metadata["unit_class"], metadata["unit_of_measurement"]) == expected


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_the_english_names_match_strings_json() -> None:
    strings = json.loads(
        (Path(care_log_statistics.__file__).parent / "strings.json").read_text(encoding="utf-8")
    )
    group = strings["entity"]["calendar"]["care_log"]["state_attributes"]["statistic_name"]
    assert group["state"] == {series.value: name for series, name in FALLBACK_NAMES.items()}


def test_names_carry_the_baby_name_and_no_raw_id() -> None:
    names = statistic_names({}, statistic_baby_name("Nursery", CODE))
    assert names[S.DIAPER_CHANGES] == "Nursery diaper changes"
    assert names[S.PUMPED_VOLUME_RIGHT] == "Nursery pumped volume, right"
    assert len(set(names.values())) == 16
    unnamed = statistic_names({}, statistic_baby_name(None, CODE))
    assert unnamed[S.BOTTLE_FEEDS] == f"Baby {CODE[:8]} bottle feeds"
    assert all(UID not in name for name in (*names.values(), *unnamed.values()))


def test_the_baby_name_is_sanitized() -> None:
    assert statistic_baby_name("<b>Ada</b>  & co", CODE) == "Ada &amp; co"


def test_a_translation_is_used_when_it_has_only_the_baby_name() -> None:
    prefix = "component.nanit.entity.calendar.care_log.state_attributes.statistic_name.state"
    strings = {
        f"{prefix}.diaper_changes": "Windeln {baby_name}",
        f"{prefix}.diaper_pee": "Nass {baby_name} {other}",
        f"{prefix}.diaper_poo": "No placeholder",
    }
    names = statistic_names(strings, "Ada")
    assert names[S.DIAPER_CHANGES] == "Windeln Ada"
    assert names[S.DIAPER_PEE] == "Ada pee diapers"
    assert names[S.DIAPER_POO] == "Ada poo diapers"


def test_a_name_with_braces_is_not_read_as_a_template() -> None:
    assert statistic_names({}, "{baby_name}")[S.DIAPER_DRY] == "{baby_name} dry diapers"


async def test_names_come_from_the_translation_files(hass: HomeAssistant) -> None:
    strings = await async_get_translations(hass, "en", "entity", {"nanit"})
    prefix = "component.nanit.entity.calendar.care_log.state_attributes.statistic_name.state"
    assert strings[f"{prefix}.diaper_changes"] == "{baby_name} diaper changes"
    names = await async_statistic_names(hass, "Nursery")
    assert names == statistic_names({}, "Nursery")


async def test_names_fall_back_to_english_when_translations_fail(hass: HomeAssistant) -> None:
    with patch.object(
        care_log_statistics, "async_get_translations", AsyncMock(side_effect=RuntimeError)
    ):
        names = await async_statistic_names(hass, "Nursery")
    assert names[S.NURSING_TIME] == "Nursery nursing time"


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def test_series_amounts_picks_one_series() -> None:
    amounts = {
        H0: {S.BOTTLE_FEEDS: 1.0, S.BOTTLE_VOLUME: 30.0},
        H0 + HOUR: {S.BOTTLE_FEEDS: 2.0},
    }
    assert series_amounts(amounts, S.BOTTLE_VOLUME) == {H0: 30.0}
    assert series_amounts(amounts, S.BOTTLE_FEEDS) == {H0: 1.0, H0 + HOUR: 2.0}
    assert series_amounts(amounts, S.PUMPING_TIME) == {}


def test_every_hour_gets_a_row_with_its_amount_and_the_running_total() -> None:
    rows = statistic_rows({H0: 2.0, H0 + 3 * HOUR: 1.5}, start=H0, end=H0 + 4 * HOUR)
    start = datetime.fromtimestamp(H0, UTC)
    assert rows == [
        {"start": start, "state": 2.0, "sum": 2.0},
        {"start": start + timedelta(hours=1), "state": 0.0, "sum": 2.0},
        {"start": start + timedelta(hours=2), "state": 0.0, "sum": 2.0},
        {"start": start + timedelta(hours=3), "state": 1.5, "sum": 3.5},
    ]
    for row in rows:
        assert row["start"].tzinfo is UTC
        assert (row["start"].minute, row["start"].second, row["start"].microsecond) == (0, 0, 0)


def test_rows_built_in_pieces_match_rows_built_at_once() -> None:
    amounts = {H0 + h * HOUR: float(h % 3) for h in range(10)}
    whole = statistic_rows(amounts, start=H0, end=H0 + 10 * HOUR)
    first = statistic_rows(
        {h: v for h, v in amounts.items() if h < H0 + 4 * HOUR}, start=H0, end=H0 + 4 * HOUR
    )
    second = statistic_rows(
        {h: v for h, v in amounts.items() if h >= H0 + 4 * HOUR},
        start=H0 + 4 * HOUR,
        end=H0 + 10 * HOUR,
        sum_before=first[-1]["sum"],
    )
    assert first + second == whole


def test_an_empty_range_has_no_rows() -> None:
    assert statistic_rows({}, start=H0, end=H0) == []


@pytest.mark.parametrize(
    ("amounts", "start", "end"),
    [
        ({}, H0 + 1, H0 + HOUR),
        ({}, H0, H0 + HOUR + 1800),
        ({}, H0 + HOUR, H0),
        ({H0 - HOUR: 1.0}, H0, H0 + HOUR),
        ({H0 + HOUR: 1.0}, H0, H0 + HOUR),
        ({H0 + 60: 1.0}, H0, H0 + HOUR),
    ],
)
def test_rows_are_only_built_on_the_utc_hour_grid(
    amounts: dict[int, float], start: int, end: int
) -> None:
    with pytest.raises(ValueError):
        statistic_rows(amounts, start=start, end=end)
