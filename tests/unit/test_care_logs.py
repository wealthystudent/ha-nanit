"""Care Logs: the calendar summary, its coordinator, the sensors and diagnostics."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, Platform, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitRateLimited,
    NanitRequestRefused,
    NanitResponseTooLarge,
)
from aionanit.models import Baby, CareLog, CareLogFetch
from custom_components.nanit import sensor as sensor_module
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.const import CARE_LOG_POLL_INTERVAL, CARE_LOG_SEED_DAYS, DOMAIN
from custom_components.nanit.coordinator import (
    CareLogData,
    CareLogRequestCoverage,
    NanitCareLogCoordinator,
    build_care_log_data,
)
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics
from custom_components.nanit.hub import NanitHub
from custom_components.nanit.sensor import (
    CARE_LOG_SENSORS,
    NanitCareLogSensor,
    NanitCareLogSensorDescription,
)

from .conftest import MOCK_BABY_1, MOCK_BABY_2, MOCK_EMAIL, mock_entry_data_v2

# The five daily totals ("today" sensors).
TODAY = (
    "diaper_changes_today",
    "feedings_today",
    "bottle_volume_today",
    "nursing_time_today",
    "pumped_volume_today",
)

# Local midnight, as an aware datetime. Offsets below are seconds from it.
DAY_START = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
MIDNIGHT = DAY_START.timestamp()
SEED_START = MIDNIGHT - 7 * 86400


def _log(uid: str, log_type: str, offset_s: float, **fields: Any) -> CareLog:
    return CareLog(
        uid=uid, log_type=log_type, timestamp=MIDNIGHT + offset_s, baby_uid="baby_1", **fields
    )


def _summary(
    logs: list[CareLog], window_start: float = SEED_START, previous: CareLogData | None = None
) -> CareLogData:
    return build_care_log_data(
        logs, window_start=window_start, day_start=DAY_START, previous=previous
    )


# ---------------------------------------------------------------------------
# build_care_log_data
# ---------------------------------------------------------------------------


def test_summary_keeps_the_newest_entry_of_each_type() -> None:
    older = _log("d1", "diaper_change", -3600, change_type="pee")
    newer = _log("d2", "diaper_change", 600, change_type="poo")
    bottle = _log("b1", "bottle_feed", 300, feed_amount_ml=120.0)

    data = _summary([newer, bottle, older])

    assert data.latest == {"diaper_change": newer, "bottle_feed": bottle}


def test_summary_today_is_since_local_midnight_oldest_first() -> None:
    yesterday = _log("d1", "diaper_change", -60)
    late = _log("d2", "diaper_change", 7200)
    early = _log("n1", "nursing", 60)

    data = _summary([late, yesterday, early])

    assert data.today == (early, late)
    assert data.day_start == DAY_START


def test_summary_leaves_an_entry_dated_after_midnight_for_tomorrow() -> None:
    """Fetched at 23:30, an entry dated 00:10 is tomorrow's, not also today's."""
    tonight = _log("d1", "diaper_change", 23 * 3600)
    after_midnight = _log("d2", "diaper_change", 24 * 3600 + 600)

    data = _summary([tonight, after_midnight])

    assert data.today == (tonight,)
    assert data.latest["diaper_change"] == after_midnight


def test_summary_counts_the_whole_local_day_when_the_clocks_go_back() -> None:
    """A 25 hour day keeps its last hour: the day ends at the next local midnight."""
    pacific = ZoneInfo("America/Los_Angeles")
    day_start = datetime(2026, 11, 1, tzinfo=pacific)  # clocks go back at 02:00

    def at(day: int, hour: int, minute: int) -> CareLog:
        when = datetime(2026, 11, day, hour, minute, tzinfo=pacific)
        return CareLog(
            uid=f"d{day}{hour}", log_type="diaper_change", timestamp=when.timestamp(), baby_uid="b"
        )

    late_evening = at(1, 23, 30)  # 24.5 hours after that day's midnight
    next_day = at(2, 0, 10)

    data = build_care_log_data(
        [late_evening, next_day],
        window_start=day_start.timestamp() - 86400,
        day_start=day_start,
        previous=None,
    )

    assert data.today == (late_evening,)


def test_summary_leaves_out_sleep() -> None:
    data = _summary([_log("s1", "auto_sleep", 60), _log("s2", "sleep", 120)])

    assert data.latest == {}
    assert data.today == ()


def test_later_poll_falls_back_to_entries_before_its_window() -> None:
    yesterdays_pump = _log("p1", "pumping", -7200)
    seed = _summary([yesterdays_pump, _log("d1", "diaper_change", 600)])

    # The next poll only fetches today, which has no pumping.
    data = _summary([_log("d1", "diaper_change", 600)], window_start=MIDNIGHT, previous=seed)

    assert data.latest["pumping"] == yesterdays_pump
    assert data.today == (_log("d1", "diaper_change", 600),)


def test_entry_deleted_in_the_app_falls_back_to_the_one_before() -> None:
    yesterdays = _log("d1", "diaper_change", -3600)
    todays = _log("d2", "diaper_change", 600)
    seed = _summary([yesterdays, todays])
    second = _summary([todays], window_start=MIDNIGHT, previous=seed)
    assert second.latest["diaper_change"] == todays

    # Today's entry is deleted in the app, so the next fetch doesn't return it.
    third = _summary([], window_start=MIDNIGHT, previous=second)

    assert third.latest["diaper_change"] == yesterdays
    assert third.today == ()


def test_new_day_moves_yesterdays_entries_before_the_window() -> None:
    evening = _log("b1", "bottle_feed", 23 * 3600)
    first_day = _summary([evening], window_start=MIDNIGHT)

    next_midnight = DAY_START + timedelta(days=1)
    data = build_care_log_data(
        [],
        window_start=next_midnight.timestamp(),
        day_start=next_midnight,
        previous=first_day,
    )

    assert data.latest["bottle_feed"] == evening
    assert data.today == ()


def test_summary_counts_a_repeated_row_once() -> None:
    bottle = _log("b1", "bottle_feed", 600, feed_amount_ml=120.0)

    data = _summary([bottle, bottle])

    assert data.today == (bottle,)
    assert data.entries == (bottle,)
    assert _sensor("feedings_today", data).native_value == 1
    assert _sensor("bottle_volume_today", data).native_value == 120.0


def test_summary_breaks_a_tie_in_time_the_same_way_in_any_order() -> None:
    pee = _log("d1", "diaper_change", 600, change_type="pee")
    poo = _log("d2", "diaper_change", 600, change_type="poo")

    forward = _summary([pee, poo])
    backward = _summary([poo, pee])

    assert forward.latest["diaper_change"] == backward.latest["diaper_change"] == poo
    assert forward.today == backward.today == (pee, poo)


def test_redated_entry_does_not_come_back_once_deleted() -> None:
    """An entry moved from Monday to today, then deleted, leaves no stale Monday copy."""
    sunday = _log("p0", "pumping", -4 * 86400)
    monday = _log("p1", "pumping", -3 * 86400)
    seed = _summary([sunday, monday])
    assert seed.needs_seed is False

    # Same uid, date edited to today.
    moved = _log("p1", "pumping", 600)
    second = _summary([moved], window_start=MIDNIGHT, previous=seed)
    assert second.latest["pumping"] == moved
    assert second.before_window["pumping"] == sunday
    assert second.needs_seed is True

    third = _summary([], window_start=MIDNIGHT, previous=second)

    assert third.latest["pumping"] == sunday
    assert third.needs_seed is False


def test_entry_returned_again_unchanged_before_the_window_needs_no_seed() -> None:
    """A fetch may return a row dated just before its start; that is not an edit."""
    evening = _log("b1", "bottle_feed", -600)
    seed = _summary([evening])

    data = _summary([evening], window_start=MIDNIGHT, previous=seed)

    assert data.latest["bottle_feed"] == evening
    assert data.needs_seed is False


def test_summary_keeps_the_four_care_kinds_and_the_request_coverage() -> None:
    nap = _log("s1", "sleep", 3000, duration_s=1800.0, net_duration_s=1500.0)
    camera_sleep = _log("s2", "auto_sleep", -7200, end_timestamp=MIDNIGHT - 3600, status="eligible")
    unknown_kind = _log("x1", "solids", 100)
    bottle = _log("b1", "bottle_feed", 600, feed_amount_ml=90.0)
    coverage = CareLogRequestCoverage(start=int(SEED_START), end=int(MIDNIGHT + 3600), dropped=2)

    data = build_care_log_data(
        [nap, bottle, camera_sleep, unknown_kind, nap],
        window_start=SEED_START,
        day_start=DAY_START,
        previous=None,
        request_coverage=coverage,
    )

    assert data.request_coverage == coverage
    # The sensors' fields hold the four kinds only.
    assert data.entries == (bottle,)
    assert data.latest == {"bottle_feed": bottle}
    assert data.today == (bottle,)


@pytest.mark.parametrize("kind", ["bottle_feed", "sleep"], ids=["same_kind", "sleep_row"])
@pytest.mark.parametrize("later", [True, False], ids=["later_outside", "earlier_outside"])
@pytest.mark.parametrize("reverse", [False, True], ids=["inside_first", "outside_first"])
def test_the_range_is_checked_before_choosing_one_copy_per_uid(
    kind: str, later: bool, reverse: bool
) -> None:
    """A copy dated outside the range asked for, of any kind, is left out and displaces nothing."""
    coverage = CareLogRequestCoverage(start=int(SEED_START), end=int(MIDNIGHT + 3600), dropped=2)
    inside = _log("b1", "bottle_feed", 600, feed_amount_ml=90.0)
    offset = 7200 if later else SEED_START - MIDNIGHT - 3600
    if kind == "sleep":
        outside = _log("b1", "sleep", offset, duration_s=1800.0)
    else:
        outside = _log("b1", "bottle_feed", offset, feed_amount_ml=40.0)
    logs = [inside, outside][:: -1 if reverse else 1]

    data = build_care_log_data(
        logs, window_start=SEED_START, day_start=DAY_START, previous=None, request_coverage=coverage
    )

    assert data.request_coverage == coverage
    # The sensors keep the copy inside.
    assert data.entries == (inside,)
    assert data.latest == {"bottle_feed": inside}
    assert data.today == (inside,)
    assert _sensor("bottle_volume_today", data).native_value == 90.0


def test_a_camera_sleep_session_begun_before_the_range_is_left_out() -> None:
    """Nanit returns a camera sleep session whole when it overlaps the start of the range."""
    coverage = CareLogRequestCoverage(start=int(MIDNIGHT), end=int(MIDNIGHT + 3600))
    session = _log("s1", "auto_sleep", -96, end_timestamp=MIDNIGHT + 1884, status="eligible")
    bottle = _log("b1", "bottle_feed", 600, feed_amount_ml=90.0)

    data = build_care_log_data(
        [session, bottle],
        window_start=MIDNIGHT,
        day_start=DAY_START,
        previous=None,
        request_coverage=coverage,
    )

    assert data.request_coverage == coverage
    assert data.entries == (bottle,)


def test_a_care_sensor_named_after_its_baby_has_the_icon_of_the_one_on_a_camera() -> None:
    """Each ``*_baby`` key (a baby without a camera) has its own key's icon mapping."""
    folder = Path(sensor_module.__file__).parent
    icons = json.loads((folder / "icons.json").read_text(encoding="utf-8"))["entity"]["sensor"]
    names = json.loads((folder / "strings.json").read_text(encoding="utf-8"))["entity"]["sensor"]
    for description in CARE_LOG_SENSORS:
        key = description.translation_key
        assert key is not None
        assert f"{key}_baby" in names
        assert icons[f"{key}_baby"] == icons[key]


def _sensor_readings(data: CareLogData) -> dict[str, tuple[Any, Any, Any]]:
    readings: dict[str, tuple[Any, Any, Any]] = {}
    for description in CARE_LOG_SENSORS:
        sensor = _sensor(description.key, data)
        readings[description.key] = (
            sensor.native_value,
            sensor.extra_state_attributes,
            sensor.last_reset,
        )
    return readings


def test_other_kinds_and_the_request_coverage_do_not_change_what_the_sensors_show() -> None:
    """Sleep rows, other kinds and the request coverage leave all eight sensors as they were."""
    care_only = list(DAY.entries)
    extra = [
        _log("s1", "sleep", 7000, duration_s=3600.0),
        _log("s2", "auto_sleep", 100, status="eligible", end_timestamp=MIDNIGHT + 900),
        _log("x1", "solids", 9500),
    ]
    whole = build_care_log_data(
        [*care_only, *extra],
        window_start=SEED_START,
        day_start=DAY_START,
        previous=None,
        request_coverage=CareLogRequestCoverage(
            start=int(SEED_START), end=int(MIDNIGHT + 86400), dropped=3
        ),
    )

    assert _sensor_readings(whole) == _sensor_readings(DAY)
    assert _sensor("last_diaper_change", whole).entity_registry_enabled_default is True


# ---------------------------------------------------------------------------
# NanitCareLogCoordinator
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)):
        yield


def _coordinator(
    hass: HomeAssistant, fetch: AsyncMock, *, dropped: int = 0
) -> NanitCareLogCoordinator:
    """Return a coordinator whose calendar fetch answers with what ``fetch`` returns.

    ``fetch`` is awaited with the baby's uid and the two bounds and returns
    the entries; this wraps them as the library's whole fetch result.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)

    async def fetch_result(baby_uid: str, start: int, end: int) -> CareLogFetch:
        logs = await fetch(baby_uid, start, end)
        return CareLogFetch(logs=tuple(logs), start=start, end=end, dropped=dropped)

    hub = MagicMock()
    hub.client.async_get_calendar_result = AsyncMock(side_effect=fetch_result)
    return NanitCareLogCoordinator(
        hass, entry, hub, Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    )


async def test_first_poll_looks_back_seed_days(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    fetch = AsyncMock(return_value=[])
    coordinator = _coordinator(hass, fetch)

    await coordinator.async_refresh()

    now = dt_util.utcnow()
    assert fetch.await_args is not None
    _, start, end = fetch.await_args.args
    assert start == int((now - timedelta(days=CARE_LOG_SEED_DAYS)).timestamp())
    assert end == int((now + timedelta(hours=1)).timestamp())
    assert coordinator.data.day_start == dt_util.start_of_local_day()


async def test_later_polls_look_back_24_hours(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    fetch = AsyncMock(return_value=[])
    coordinator = _coordinator(hass, fetch)
    await coordinator.async_refresh()

    await coordinator.async_refresh()

    assert fetch.await_args is not None
    assert fetch.await_args.args[1] == int((dt_util.utcnow() - timedelta(hours=24)).timestamp())


def _requested_start(fetch: AsyncMock) -> int:
    assert fetch.await_args is not None
    start: int = fetch.await_args.args[1]
    return start


async def test_poll_after_an_outage_looks_back_seed_days_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """After more than 24 hours without a successful fetch, nothing in between is skipped."""
    freezer.move_to("2026-11-20T18:00:00+00:00")
    fetch = AsyncMock(return_value=[])
    coordinator = _coordinator(hass, fetch)
    await coordinator.async_refresh()

    freezer.tick(timedelta(hours=12))
    fetch.side_effect = NanitConnectionError("Calendar fetch failed with HTTP 503")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    freezer.tick(timedelta(hours=24))
    fetch.side_effect = None
    await coordinator.async_refresh()

    seed_start = dt_util.utcnow() - timedelta(days=CARE_LOG_SEED_DAYS)
    assert _requested_start(fetch) == int(seed_start.timestamp())

    # Back to normal once it has caught up.
    await coordinator.async_refresh()
    assert _requested_start(fetch) == int((dt_util.utcnow() - timedelta(hours=24)).timestamp())


async def test_redated_entry_makes_the_next_poll_look_back_seed_days(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    now = dt_util.utcnow().timestamp()
    monday = CareLog(uid="p1", log_type="pumping", timestamp=now - 3 * 86400, baby_uid="baby_1")
    fetch = AsyncMock(return_value=[monday])
    coordinator = _coordinator(hass, fetch)
    await coordinator.async_refresh()

    # Its date is edited to today, which the 24 hour poll sees.
    fetch.return_value = [CareLog(uid="p1", log_type="pumping", timestamp=now, baby_uid="baby_1")]
    await coordinator.async_refresh()
    day_ago = int((dt_util.utcnow() - timedelta(hours=24)).timestamp())
    assert _requested_start(fetch) == day_ago

    await coordinator.async_refresh()
    seed_start = dt_util.utcnow() - timedelta(days=CARE_LOG_SEED_DAYS)
    assert _requested_start(fetch) == int(seed_start.timestamp())

    await coordinator.async_refresh()
    assert _requested_start(fetch) == day_ago


def _calendar(logs: list[CareLog]) -> AsyncMock:
    """Fake the calendar fetch: only the entries ``logs`` holds inside the requested interval."""

    async def fetch(_baby_uid: str, start: int, end: int) -> list[CareLog]:
        return [log for log in logs if start <= log.timestamp <= end]

    return AsyncMock(side_effect=fetch)


async def test_entry_logged_after_midnight_for_the_evening_before_is_picked_up(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A night feed entered the next morning still becomes the last feeding."""
    freezer.move_to("2026-11-20T08:30:00+00:00")  # 00:30 local (US/Pacific)
    logs: list[CareLog] = []
    coordinator = _coordinator(hass, _calendar(logs))
    await coordinator.async_refresh()
    late_bottle = CareLog(
        uid="b1",
        log_type="bottle_feed",
        timestamp=dt_util.start_of_local_day().timestamp() - 15 * 60,  # 23:45 the evening before
        baby_uid="baby_1",
        feed_amount_ml=90.0,
    )
    logs.append(late_bottle)

    await coordinator.async_refresh()

    assert coordinator.data.latest == {"bottle_feed": late_bottle}
    assert coordinator.data.today == ()


async def test_poll_summarises_the_fetch(hass: HomeAssistant) -> None:
    diaper = CareLog(
        uid="d1",
        log_type="diaper_change",
        timestamp=dt_util.utcnow().timestamp() - 60,
        baby_uid="baby_1",
        change_type="dry",
    )
    coordinator = _coordinator(hass, AsyncMock(return_value=[diaper]))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.latest == {"diaper_change": diaper}


async def test_poll_publishes_its_request_coverage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    now = dt_util.utcnow()
    nap = CareLog(
        uid="s1",
        log_type="sleep",
        timestamp=now.timestamp() - 7200,
        baby_uid="baby_1",
        duration_s=3600.0,
    )
    diaper = CareLog(
        uid="d1",
        log_type="diaper_change",
        timestamp=now.timestamp() - 60,
        baby_uid="baby_1",
        change_type="pee",
    )
    fetch = AsyncMock(return_value=[diaper, nap, nap])
    coordinator = _coordinator(hass, fetch, dropped=2)

    await coordinator.async_refresh()

    data = coordinator.data
    assert data.entries == (diaper,)
    assert data.request_coverage == CareLogRequestCoverage(
        start=int((now - timedelta(days=CARE_LOG_SEED_DAYS)).timestamp()),
        end=int((now + timedelta(hours=1)).timestamp()),
        dropped=2,
    )

    # The next, shorter, poll replaces it with its own range.
    freezer.tick(timedelta(minutes=5))
    fetch.return_value = [diaper]
    await coordinator.async_refresh()

    later = dt_util.utcnow()
    assert coordinator.data.request_coverage == CareLogRequestCoverage(
        start=int((later - timedelta(hours=24)).timestamp()),
        end=int((later + timedelta(hours=1)).timestamp()),
        dropped=2,
    )


async def test_failed_poll_keeps_the_last_successful_request_coverage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    fetch = AsyncMock(return_value=[])
    coordinator = _coordinator(hass, fetch)
    await coordinator.async_refresh()
    coverage = coordinator.data.request_coverage
    assert coverage is not None

    freezer.tick(timedelta(minutes=5))
    fetch.side_effect = NanitConnectionError("Calendar fetch failed with HTTP 503")
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.data.request_coverage == coverage


async def test_poll_retries_before_going_unavailable(hass: HomeAssistant) -> None:
    err = NanitConnectionError("Calendar fetch failed with HTTP 502")
    fetch = AsyncMock(side_effect=[err, err, err])
    coordinator = _coordinator(hass, fetch)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert fetch.await_count == 3


async def test_poll_does_not_retry_an_answer_too_large_to_read(hass: HomeAssistant) -> None:
    fetch = AsyncMock(side_effect=NanitResponseTooLarge("Calendar response too large"))
    coordinator = _coordinator(hass, fetch)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert fetch.await_count == 1


async def test_auth_error_starts_reauth(hass: HomeAssistant) -> None:
    fetch = AsyncMock(side_effect=NanitAuthError("Access token invalid"))
    coordinator = _coordinator(hass, fetch)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert fetch.await_count == 1


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


def _sensor(key: str, data: CareLogData | None) -> NanitCareLogSensor:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.baby = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
    description = next(d for d in CARE_LOG_SENSORS if d.key == key)
    return NanitCareLogSensor(coordinator, description, entry_id="entry_1", camera_uid="cam_1")


DAY = _summary(
    [
        _log("d0", "diaper_change", -600, change_type="poo"),  # yesterday
        _log("d1", "diaper_change", 600, change_type="pee"),
        _log("d2", "diaper_change", 4000, change_type="mixed"),
        _log("d3", "diaper_change", 9000, change_type="pee"),
        _log("b1", "bottle_feed", 1000, feed_amount_ml=118.294),
        _log("b2", "bottle_feed", 5000, feed_amount_ml=59.147),
        _log("b3", "bottle_feed", 5500),  # no amount logged
        _log("n1", "nursing", 2000, left_duration_s=610.0, right_duration_s=893.0),
        _log("n2", "nursing", 8000, left_duration_s=320.0),
        _log("p1", "pumping", 3000, left_amount_ml=7.39, right_amount_ml=7.39, duration_s=900.0),
    ]
)


def test_last_diaper_change() -> None:
    sensor = _sensor("last_diaper_change", DAY)

    assert sensor.native_value == dt_util.utc_from_timestamp(MIDNIGHT + 9000)
    assert sensor.extra_state_attributes == {"change_type": "pee"}
    assert sensor.entity_description.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.last_reset is None
    assert sensor.unique_id == f"entry_1_{baby_code('baby_1')}_last_diaper_change"


def test_last_diaper_change_hides_an_unknown_type() -> None:
    data = _summary([_log("d1", "diaper_change", 600, change_type="<b>wet</b>")])

    assert _sensor("last_diaper_change", data).extra_state_attributes == {"change_type": None}


def test_last_feeding_is_the_newer_of_bottle_and_nursing() -> None:
    sensor = _sensor("last_feeding", DAY)

    assert sensor.native_value == dt_util.utc_from_timestamp(MIDNIGHT + 8000)
    assert sensor.extra_state_attributes == {
        "feeding_type": "nursing",
        "left_minutes": 5.3,
        "right_minutes": None,
    }


def test_last_feeding_reports_a_bottle_amount() -> None:
    data = _summary([_log("b1", "bottle_feed", 1000, feed_amount_ml=118.294)])

    sensor = _sensor("last_feeding", data)

    assert sensor.extra_state_attributes == {"feeding_type": "bottle", "amount_ml": 118.3}


def test_last_pumping() -> None:
    sensor = _sensor("last_pumping", DAY)

    assert sensor.native_value == dt_util.utc_from_timestamp(MIDNIGHT + 3000)
    assert sensor.extra_state_attributes == {"amount_ml": 14.8, "duration_minutes": 15.0}


def test_diaper_changes_today_counts_each_type() -> None:
    sensor = _sensor("diaper_changes_today", DAY)

    assert sensor.native_value == 3
    assert sensor.extra_state_attributes == {"pee": 2, "poo": 0, "mixed": 1, "dry": 0}


def test_feedings_today_counts_bottles_and_nursing() -> None:
    sensor = _sensor("feedings_today", DAY)

    assert sensor.native_value == 5
    assert sensor.extra_state_attributes == {"bottle": 3, "nursing": 2}


def test_bottle_volume_today() -> None:
    sensor = _sensor("bottle_volume_today", DAY)

    assert sensor.native_value == 177.4
    assert sensor.entity_description.device_class == SensorDeviceClass.VOLUME
    assert sensor.entity_description.native_unit_of_measurement == UnitOfVolume.MILLILITERS


def test_nursing_time_today() -> None:
    sensor = _sensor("nursing_time_today", DAY)

    assert sensor.native_value == 30.4  # (610 + 893 + 320) s
    assert sensor.entity_description.native_unit_of_measurement == UnitOfTime.MINUTES


def test_pumped_volume_today() -> None:
    assert _sensor("pumped_volume_today", DAY).native_value == 14.8


async def test_daily_totals_reset_at_local_midnight(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Two polls either side of midnight, with a bottle logged between them."""
    local = dt_util.get_default_time_zone()
    assert local == ZoneInfo("US/Pacific")
    freezer.move_to(datetime(2026, 11, 19, 23, 58, tzinfo=local))
    evening = CareLog(
        uid="b1",
        log_type="bottle_feed",
        timestamp=datetime(2026, 11, 19, 23, 40, tzinfo=local).timestamp(),
        baby_uid="baby_1",
        feed_amount_ml=90.0,
    )
    logs = [evening]
    coordinator = _coordinator(hass, _calendar(logs))
    await coordinator.async_refresh()
    daily = {
        d.key: NanitCareLogSensor(coordinator, d, entry_id="entry_1", camera_uid="cam_1")
        for d in CARE_LOG_SENSORS
        if d.key in TODAY
    }
    last_feeding = NanitCareLogSensor(
        coordinator,
        next(d for d in CARE_LOG_SENSORS if d.key == "last_feeding"),
        entry_id="entry_1",
        camera_uid="cam_1",
    )

    assert len(daily) == 5
    assert daily["feedings_today"].native_value == 1
    assert daily["bottle_volume_today"].native_value == 90.0
    for sensor in daily.values():
        # No long-term statistics of their own: no state class, no reset.
        assert sensor.entity_description.state_class is None
        assert sensor.state_class is None
        assert sensor.last_reset is None

    after_midnight = CareLog(
        uid="b2",
        log_type="bottle_feed",
        timestamp=datetime(2026, 11, 20, 0, 1, tzinfo=local).timestamp(),
        baby_uid="baby_1",
        feed_amount_ml=60.0,
    )
    logs.append(after_midnight)
    freezer.move_to(datetime(2026, 11, 20, 0, 3, tzinfo=local))
    await coordinator.async_refresh()

    assert daily["feedings_today"].native_value == 1
    assert daily["bottle_volume_today"].native_value == 60.0
    assert daily["diaper_changes_today"].native_value == 0
    for sensor in daily.values():
        assert sensor.last_reset is None
    assert last_feeding.native_value == dt_util.utc_from_timestamp(after_midnight.timestamp)


@pytest.mark.parametrize("key", TODAY)
def test_the_today_sensors_record_no_long_term_statistics_of_their_own(key: str) -> None:
    """They would run low: an entry logged after midnight for the day before never counts."""
    sensor = _sensor(key, DAY)

    assert sensor.entity_description.state_class is None
    assert sensor.state_class is None
    assert sensor.last_reset is None
    assert sensor.capability_attributes is None


def test_the_other_sensors_have_no_state_class_either() -> None:
    for description in CARE_LOG_SENSORS:
        assert description.state_class is None, description.key


def test_an_empty_day_reads_zero_and_unknown() -> None:
    data = _summary([])

    assert _sensor("diaper_changes_today", data).native_value == 0
    assert _sensor("bottle_volume_today", data).native_value == 0
    assert _sensor("last_diaper_change", data).native_value is None
    assert _sensor("last_diaper_change", data).extra_state_attributes == {}
    assert _sensor("last_feeding", data).extra_state_attributes == {}
    assert _sensor("last_pumping", data).extra_state_attributes == {}


@pytest.mark.parametrize("description", CARE_LOG_SENSORS, ids=lambda d: d.key)
def test_sensors_without_data(description: NanitCareLogSensorDescription) -> None:
    sensor = _sensor(description.key, None)

    assert sensor.native_value is None
    assert sensor.last_reset is None
    assert sensor.extra_state_attributes is None


def test_sensors_are_enabled_for_an_account_with_care_logs() -> None:
    assert _sensor("last_diaper_change", DAY).entity_registry_enabled_default is True


def test_sensors_are_disabled_for_an_account_without_care_logs() -> None:
    assert _sensor("last_diaper_change", _summary([])).entity_registry_enabled_default is False


# ---------------------------------------------------------------------------
# Hub and diagnostics
# ---------------------------------------------------------------------------


def test_sensors_are_enabled_when_the_first_fetch_failed() -> None:
    """No data means nothing was read yet, not that the account has no Care Logs."""
    for description in CARE_LOG_SENSORS:
        assert _sensor(description.key, None).entity_registry_enabled_default is True


def _hub_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    return entry


def _mock_reader(first_refresh: AsyncMock | None = None) -> Any:
    """A factory for mock readers, each keeping the baby it was created for."""

    def create(_hass: HomeAssistant, _entry: Any, _hub: Any, baby: Baby) -> MagicMock:
        return MagicMock(
            async_config_entry_first_refresh=first_refresh or AsyncMock(),
            async_shutdown=AsyncMock(),
            baby=baby,
            data=None,
        )

    return create


def _mock_camera(uid: str, baby_uid: str) -> MagicMock:
    camera = MagicMock(uid=uid, baby_uid=baby_uid, connected=True)
    camera.subscribe = MagicMock(return_value=lambda: None)
    camera.async_start = AsyncMock()
    camera.async_stop = AsyncMock()
    return camera


def _push_factory(failing_camera_uids: set[str]) -> Any:
    def create(_hass: HomeAssistant, _entry: Any, camera: Any, _baby: Baby) -> MagicMock:
        if camera.uid in failing_camera_uids:
            return MagicMock(async_setup=AsyncMock(side_effect=NanitConnectionError("offline")))
        return MagicMock(async_setup=AsyncMock())

    return create


async def test_care_log_first_fetch_failure_keeps_the_reader(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """A failing Care Logs fetch at setup keeps its reader, and the entry sets up."""
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))
    first_refresh = AsyncMock(side_effect=ConfigEntryNotReady())

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch(
            "custom_components.nanit.hub.NanitCareLogCoordinator",
            side_effect=_mock_reader(first_refresh),
        ),
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    data = hub.camera_data[MOCK_BABY_1.camera_uid]
    reader = hub.care_log_coordinators[MOCK_BABY_1.uid]
    assert data.care_log_coordinator is reader
    assert reader.baby is MOCK_BABY_1
    assert data.cloud_coordinator is not None
    first_refresh.assert_awaited_once()
    await hub.async_close()


async def test_care_log_coordinator_auth_failure_propagates(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch(
            "custom_components.nanit.hub.NanitCareLogCoordinator",
            side_effect=_mock_reader(AsyncMock(side_effect=ConfigEntryAuthFailed())),
        ),
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        with pytest.raises(ConfigEntryAuthFailed):
            await hub.async_setup()
    reader = hub.care_log_coordinators[MOCK_BABY_1.uid]
    await hub.async_close()
    reader.async_shutdown.assert_awaited_once()


async def test_every_baby_gets_a_reader_even_without_a_working_camera(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """Care Logs belong to the baby: a failed camera or no camera still gets a reader."""
    no_camera = Baby(uid="baby_4", name="Spare room", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2, no_camera]
    mock_nanit_client.camera.side_effect = lambda **kw: _mock_camera(kw["uid"], kw["baby_uid"])
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))

    with (
        patch(
            "custom_components.nanit.hub.NanitPushCoordinator",
            side_effect=_push_factory({MOCK_BABY_1.camera_uid}),
        ),
        patch(
            "custom_components.nanit.hub.NanitCareLogCoordinator", side_effect=_mock_reader()
        ) as care_cls,
    ):
        await hub.async_setup()

    readers = hub.care_log_coordinators
    assert set(readers) == {"baby_1", "baby_2", "baby_4"}
    assert {uid: reader.baby for uid, reader in readers.items()} == {
        "baby_1": MOCK_BABY_1,
        "baby_2": MOCK_BABY_2,
        "baby_4": no_camera,
    }
    for reader in readers.values():
        reader.async_config_entry_first_refresh.assert_awaited_once()
    assert care_cls.call_count == 3
    # Sensors stay on the camera's device, so only the working camera has them.
    assert set(hub.camera_data) == {MOCK_BABY_2.camera_uid}
    assert hub.camera_data[MOCK_BABY_2.camera_uid].care_log_coordinator is readers["baby_2"]
    await hub.async_close()


async def test_baby_record_without_a_uid_gets_no_reader(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    placeholder = Baby(uid="", name="Unpaired speaker", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, placeholder, MOCK_BABY_1]
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch(
            "custom_components.nanit.hub.NanitCareLogCoordinator", side_effect=_mock_reader()
        ) as care_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert set(hub.care_log_coordinators) == {"baby_1"}
    assert care_cls.call_count == 1
    await hub.async_close()


async def test_no_reader_is_started_when_no_device_could_be_set_up(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """The readers never change when setup counts as successful."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2]
    mock_nanit_client.camera.side_effect = lambda **kw: _mock_camera(kw["uid"], kw["baby_uid"])
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))

    with (
        patch(
            "custom_components.nanit.hub.NanitPushCoordinator",
            side_effect=_push_factory({MOCK_BABY_1.camera_uid, MOCK_BABY_2.camera_uid}),
        ),
        patch(
            "custom_components.nanit.hub.NanitCareLogCoordinator", side_effect=_mock_reader()
        ) as care_cls,
        pytest.raises(NanitConnectionError, match="No devices could be set up"),
    ):
        await hub.async_setup()

    care_cls.assert_not_called()
    assert hub.care_log_coordinators == {}
    await hub.async_close()


async def test_close_shuts_down_every_reader(hass: HomeAssistant, mock_nanit_client) -> None:
    no_camera = Baby(uid="baby_4", name="Spare room", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, no_camera]
    hub = NanitHub(hass, MagicMock(), _hub_entry(hass))
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCareLogCoordinator", side_effect=_mock_reader()),
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()
    readers = list(hub.care_log_coordinators.values())
    assert len(readers) == 2

    await hub.async_close()

    for reader in readers:
        reader.async_shutdown.assert_awaited_once()
    assert hub.care_log_coordinators == {}


# ---------------------------------------------------------------------------
# End to end: real readers, real sensors, a faked Nanit answer
# ---------------------------------------------------------------------------


def _answers(answers: dict[str, list[Any]]) -> AsyncMock:
    """Fake the whole calendar fetch, per baby.

    Each call takes that baby's next answer, and the last one repeats. An
    answer is a list of entries, or an exception to raise.
    """

    async def fetch(baby_uid: str, start: int, end: int) -> CareLogFetch:
        queue = answers[baby_uid]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return CareLogFetch(logs=tuple(answer), start=start, end=end, dropped=0)

    return AsyncMock(side_effect=fetch)


async def _set_up(
    hass: HomeAssistant, entry: MockConfigEntry, client: Any, calendar: AsyncMock
) -> bool:
    client.async_get_calendar_result = calendar
    # Only the sensor platform: the Care Log sensors are all this checks.
    with (
        patch("custom_components.nanit.hub.NanitCareLogCoordinator", NanitCareLogCoordinator),
        patch("custom_components.nanit.PLATFORMS", [Platform.SENSOR]),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return result


def _care_entity_id(hass: HomeAssistant, baby_uid: str, key: str) -> str:
    """The entity id of a baby's care log sensor, on the only entry that has one."""
    suffix = f"_{baby_code(baby_uid)}_{key}"
    (entity_id,) = (
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
        and entry.domain == "sensor"
        and entry.unique_id.endswith(suffix)
    )
    return entity_id


async def _next_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=CARE_LOG_POLL_INTERVAL))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _diaper(baby_uid: str, uid: str = "d1") -> CareLog:
    return CareLog(
        uid=uid,
        log_type="diaper_change",
        timestamp=dt_util.utcnow().timestamp() - 60,
        baby_uid=baby_uid,
        change_type="pee",
    )


async def test_failed_first_fetch_shows_unavailable_sensors_until_a_poll_succeeds(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    diaper = _diaper("baby_1")
    calendar = _answers({"baby_1": [NanitConnectionError("Calendar fetch failed"), [diaper]]})
    entry = _hub_entry(hass)

    assert await _set_up(hass, entry, mock_nanit_client, calendar)

    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    for description in CARE_LOG_SENSORS:
        entity_id = _care_entity_id(hass, "baby_1", description.key)
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None
        assert registry_entry.disabled_by is None, description.key
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, description.key

    await _next_poll(hass, freezer)

    last_diaper = hass.states.get(_care_entity_id(hass, "baby_1", "last_diaper_change"))
    assert last_diaper.state == dt_util.utc_from_timestamp(diaper.timestamp).isoformat()
    assert last_diaper.attributes["change_type"] == "pee"
    diapers_today = hass.states.get(_care_entity_id(hass, "baby_1", "diaper_changes_today"))
    assert diapers_today.state == "1"
    assert diapers_today.attributes["pee"] == 1
    for key in TODAY:
        attributes = hass.states.get(_care_entity_id(hass, "baby_1", key)).attributes
        assert "state_class" not in attributes, key
        assert "last_reset" not in attributes, key
    feedings = hass.states.get(_care_entity_id(hass, "baby_1", "feedings_today"))
    assert feedings.state == "0"
    assert calendar.await_count == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sensors_without_care_logs_still_register_disabled(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    entry = _hub_entry(hass)

    assert await _set_up(hass, entry, mock_nanit_client, _answers({"baby_1": [[]]}))

    registry = er.async_get(hass)
    for description in CARE_LOG_SENSORS:
        registry_entry = registry.async_get(_care_entity_id(hass, "baby_1", description.key))
        assert registry_entry is not None
        assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_sensor_the_user_disabled_stays_disabled_after_a_failed_fetch(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    entry = _hub_entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{baby_code('baby_1')}_last_feeding",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    calendar = _answers({"baby_1": [NanitConnectionError("Calendar fetch failed")]})

    assert await _set_up(hass, entry, mock_nanit_client, calendar)

    disabled = registry.async_get(_care_entity_id(hass, "baby_1", "last_feeding"))
    assert disabled is not None
    assert disabled.disabled_by is er.RegistryEntryDisabler.USER
    other = registry.async_get(_care_entity_id(hass, "baby_1", "last_pumping"))
    assert other is not None
    assert other.disabled_by is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_first_fetch_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    entry = _hub_entry(hass)
    calendar = _answers({"baby_1": [NanitAuthError("Access token invalid")]})

    assert not await _set_up(hass, entry, mock_nanit_client, calendar)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    mock_nanit_client.async_close.assert_awaited()


async def test_two_babies_one_failing_one_healthy(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2]
    mock_nanit_client.camera.side_effect = lambda **kw: _mock_camera(kw["uid"], kw["baby_uid"])
    diaper = _diaper("baby_2", uid="d2")
    answers: dict[str, list[Any]] = {
        "baby_1": [NanitConnectionError("Calendar fetch failed")],
        "baby_2": [[diaper]],
    }
    calendar = _answers(answers)
    entry = _hub_entry(hass)

    assert await _set_up(hass, entry, mock_nanit_client, calendar)

    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.hub.care_log_coordinators) == {"baby_1", "baby_2"}
    failing = hass.states.get(_care_entity_id(hass, "baby_1", "last_diaper_change"))
    healthy = hass.states.get(_care_entity_id(hass, "baby_2", "last_diaper_change"))
    assert failing.state == STATE_UNAVAILABLE
    assert healthy.state == dt_util.utc_from_timestamp(diaper.timestamp).isoformat()

    # At the next poll both ask again: the failing one keeps failing (and
    # retries), the healthy one picks up a newer diaper change.
    calendar.reset_mock()
    newer = dataclasses.replace(diaper, uid="d3", timestamp=diaper.timestamp + 30)
    answers["baby_2"] = [[diaper, newer]]
    await _next_poll(hass, freezer)
    # The polls run as background tasks: wait for their retries too.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert Counter(call.args[0] for call in calendar.await_args_list) == {
        "baby_1": 3,
        "baby_2": 1,
    }
    assert hass.states.get(failing.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(healthy.entity_id).state == (
        dt_util.utc_from_timestamp(newer.timestamp).isoformat()
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


def _scripted(answers: list[Any]) -> AsyncMock:
    """Fake the whole calendar fetch: each call takes the next answer, the last repeats."""

    async def fetch(baby_uid: str, start: int, end: int) -> CareLogFetch:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return CareLogFetch(logs=tuple(answer), start=start, end=end, dropped=0)

    return AsyncMock(side_effect=fetch)


async def _polls(hass: HomeAssistant, freezer: FrozenDateTimeFactory, count: int) -> None:
    for _ in range(count):
        await _next_poll(hass, freezer)


@pytest.mark.parametrize("at_setup", [False, True], ids=["after_setup", "at_setup"])
async def test_a_refusal_is_not_retried_and_is_asked_again_hourly(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory, at_setup: bool
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    refused = NanitRequestRefused("Calendar fetch", 403)
    diaper = _diaper("baby_1")
    answers: list[Any] = [refused] if at_setup else [[diaper], refused]
    calendar = _scripted(answers)
    entry = _hub_entry(hass)
    assert await _set_up(hass, entry, mock_nanit_client, calendar)
    reader = entry.runtime_data.hub.care_log_coordinators["baby_1"]
    entity_id = _care_entity_id(hass, "baby_1", "last_diaper_change")
    base = 0
    if not at_setup:
        assert calendar.await_count == 1
        base = 1
        await _next_poll(hass, freezer)
    # One request, not three: a refusal is not retried within the poll.
    assert calendar.await_count == base + 1
    assert reader.last_update_success is False
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    # Nothing more for the rest of the hour, at the normal five-minute polls.
    await _polls(hass, freezer, 11)
    assert calendar.await_count == base + 1
    # An hour after the refusal, one more request, refused again.
    await _next_poll(hass, freezer)
    assert calendar.await_count == base + 2
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    # The next hourly check succeeds, and the reader is back to its normal pace.
    answers[:] = [[diaper]]
    await _polls(hass, freezer, 11)
    assert calendar.await_count == base + 2
    await _next_poll(hass, freezer)
    assert calendar.await_count == base + 3
    assert reader.last_update_success is True
    assert (
        hass.states.get(entity_id).state == dt_util.utc_from_timestamp(diaper.timestamp).isoformat()
    )
    await _next_poll(hass, freezer)
    assert calendar.await_count == base + 4
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("retry_after", "quiet_polls"),
    [(1200, 3), (None, 2), (30, 0), (7200, 23)],
    ids=["twenty_minutes", "no_wait_given", "less_than_a_poll", "two_hours"],
)
async def test_throttling_waits_as_asked_and_at_least_a_poll(
    hass: HomeAssistant,
    mock_nanit_client,
    freezer: FrozenDateTimeFactory,
    retry_after: int | None,
    quiet_polls: int,
) -> None:
    """``quiet_polls`` normal polls pass without a request before the next one is made.

    A wait of more than an hour is checked hourly, and nothing is asked before it ends.
    """
    freezer.move_to("2026-11-20T18:00:00+00:00")
    bounded = NanitRateLimited("Calendar fetch", retry_after)
    answers: list[Any] = [[_diaper("baby_1")], bounded, []]
    calendar = _scripted(answers)
    entry = _hub_entry(hass)
    assert await _set_up(hass, entry, mock_nanit_client, calendar)
    reader = entry.runtime_data.hub.care_log_coordinators["baby_1"]

    await _next_poll(hass, freezer)
    assert calendar.await_count == 2
    assert reader.last_update_success is False
    assert hass.states.get(_care_entity_id(hass, "baby_1", "last_diaper_change")).state == (
        STATE_UNAVAILABLE
    )

    await _polls(hass, freezer, quiet_polls)
    assert calendar.await_count == 2
    await _next_poll(hass, freezer)
    assert calendar.await_count == 3
    assert reader.last_update_success is True
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_care_log_hold_bounds() -> None:
    # Imported here, so the rest of this file also runs against code without it.
    from custom_components.nanit.coordinator import care_log_hold

    poll = timedelta(seconds=CARE_LOG_POLL_INTERVAL)
    assert care_log_hold(NanitRequestRefused("x", 403)) == timedelta(hours=1)
    assert care_log_hold(NanitRateLimited("x", None)) == timedelta(minutes=15)
    assert care_log_hold(NanitRateLimited("x", 0)) == poll
    assert care_log_hold(NanitRateLimited("x", 1200)) == timedelta(minutes=20)
    assert care_log_hold(NanitRateLimited("x", 86400)) == timedelta(days=1)


def _care_registry_entries(
    hass: HomeAssistant, entry: MockConfigEntry, baby_uid: str
) -> dict[str, er.RegistryEntry]:
    """The care log sensors registered for a baby on an entry, by unique id."""
    code = baby_code(baby_uid)
    return {
        registered.unique_id: registered
        for registered in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registered.domain == "sensor" and code in registered.unique_id
    }


async def test_a_camera_moved_to_another_baby_does_not_continue_the_first_babys_sensors(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    entry = _hub_entry(hass)
    calendar = _answers({"baby_1": [[_diaper("baby_1")]], "baby_9": [[_diaper("baby_9")]]})
    assert await _set_up(hass, entry, mock_nanit_client, calendar)
    first = {
        uid: reg.entity_id for uid, reg in _care_registry_entries(hass, entry, "baby_1").items()
    }
    assert len(first) == len(CARE_LOG_SENSORS)
    assert await hass.config_entries.async_unload(entry.entry_id)

    # The same camera, now under a sibling.
    mock_nanit_client.async_get_babies.return_value = [
        Baby(uid="baby_9", name="Nursery", camera_uid="cam_1")
    ]
    assert await _set_up(hass, entry, mock_nanit_client, calendar)

    second = _care_registry_entries(hass, entry, "baby_9")
    assert len(second) == len(CARE_LOG_SENSORS)
    assert not set(first) & set(second)
    assert not set(first.values()) & {reg.entity_id for reg in second.values()}
    # The first baby's registry entries are left as they were, not reused.
    registry = er.async_get(hass)
    for unique_id, entity_id in first.items():
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
    # The sibling's sensors are on the camera's device and show the sibling's entry.
    last = second[next(uid for uid in second if uid.endswith("_last_diaper_change"))]
    assert last.device_id is not None
    assert hass.states.get(last.entity_id).state != STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_baby_without_a_camera_gets_sensors_that_update(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    """On an account whose other baby's camera works; named after the baby, on no device."""
    freezer.move_to("2026-11-20T18:00:00+00:00")
    no_camera = Baby(uid="baby_4", name="Spare room", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, no_camera]
    diaper = _diaper("baby_4", uid="d4")
    answers: dict[str, list[Any]] = {"baby_1": [[]], "baby_4": [[diaper]]}
    entry = _hub_entry(hass)
    assert await _set_up(hass, entry, mock_nanit_client, _answers(answers))

    registered = _care_registry_entries(hass, entry, "baby_4")
    assert len(registered) == len(CARE_LOG_SENSORS)
    assert all(reg.device_id is None for reg in registered.values())
    entity_id = _care_entity_id(hass, "baby_4", "last_diaper_change")
    assert entity_id == "sensor.spare_room_last_diaper_change"
    state = hass.states.get(entity_id)
    assert state.name == "Spare room last diaper change"
    assert state.state == dt_util.utc_from_timestamp(diaper.timestamp).isoformat()
    assert hass.states.get(_care_entity_id(hass, "baby_4", "diaper_changes_today")).state == "1"

    newer = dataclasses.replace(diaper, uid="d5", timestamp=diaper.timestamp + 30)
    answers["baby_4"] = [[diaper, newer]]
    await _next_poll(hass, freezer)
    assert hass.states.get(entity_id).state == (
        dt_util.utc_from_timestamp(newer.timestamp).isoformat()
    )
    assert hass.states.get(_care_entity_id(hass, "baby_4", "diaper_changes_today")).state == "2"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_sign_ins_seeing_one_baby_each_keep_working_sensors(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-20T18:00:00+00:00")
    diaper = _diaper("baby_1")
    answers: dict[str, list[Any]] = {"baby_1": [[diaper]]}
    calendar = _answers(answers)
    first = _hub_entry(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_entry_data_v2(), "email": "other@example.com"},
        version=2,
        unique_id="other@example.com",
    )
    second.add_to_hass(hass)
    # Setting the integration up loads both entries.
    assert await _set_up(hass, first, mock_nanit_client, calendar)
    assert first.state is second.state is ConfigEntryState.LOADED

    ones = _care_registry_entries(hass, first, "baby_1")
    twos = _care_registry_entries(hass, second, "baby_1")
    assert len(ones) == len(twos) == len(CARE_LOG_SENSORS)
    assert not set(ones) & set(twos)
    (last_two,) = (reg for uid, reg in twos.items() if uid.endswith("_last_diaper_change"))
    for registered in (*ones.values(), *twos.values()):
        assert hass.states.get(registered.entity_id).state != STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(first.entry_id)
    newer = dataclasses.replace(diaper, uid="d9", timestamp=diaper.timestamp + 30)
    answers["baby_1"] = [[diaper, newer]]
    await _next_poll(hass, freezer)

    assert hass.states.get(last_two.entity_id).state == (
        dt_util.utc_from_timestamp(newer.timestamp).isoformat()
    )
    assert await hass.config_entries.async_unload(second.entry_id)


async def test_no_care_sensor_id_or_name_holds_the_baby_uid(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    """Planted uids, one with a camera and one unnamed without: not even 8 characters of either."""
    freezer.move_to("2026-11-20T18:00:00+00:00")
    with_camera = Baby(uid="zqcamerababy5521", name="Nursery", camera_uid="cam_1")
    unnamed = Baby(uid="zqunnamedbaby8834", name="", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [with_camera, unnamed]
    calendar = _answers(
        {
            with_camera.uid: [[_diaper(with_camera.uid)]],
            unnamed.uid: [[_diaper(unnamed.uid, uid="d2")]],
        }
    )
    entry = _hub_entry(hass)
    assert await _set_up(hass, entry, mock_nanit_client, calendar)

    shown: list[str] = []
    for baby in (with_camera, unnamed):
        registered = _care_registry_entries(hass, entry, baby.uid)
        assert len(registered) == len(CARE_LOG_SENSORS)
        for reg in registered.values():
            state = hass.states.get(reg.entity_id)
            shown += [reg.unique_id, reg.entity_id, reg.name or "", reg.original_name or ""]
            shown += [state.name, str(state.attributes.get("friendly_name"))]
    text = " ".join(shown)
    for uid in (with_camera.uid, unnamed.uid):
        for i in range(len(uid) - 7):
            assert uid[i : i + 8] not in text, uid[i : i + 8]
    assert f"Baby {baby_code(unnamed.uid)[:8]} last diaper change" in text
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_stops_every_reader(
    hass: HomeAssistant, mock_nanit_client, freezer: FrozenDateTimeFactory
) -> None:
    """Including the reader of a baby with no camera, which no sensor listens to."""
    freezer.move_to("2026-11-20T18:00:00+00:00")
    no_camera = Baby(uid="baby_4", name="Spare room", camera_uid="")
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, no_camera]
    calendar = _answers({"baby_1": [[_diaper("baby_1")]], "baby_4": [[]]})
    entry = _hub_entry(hass)
    assert await _set_up(hass, entry, mock_nanit_client, calendar)
    hub = entry.runtime_data.hub
    readers = list(hub.care_log_coordinators.values())
    assert {reader.baby.uid for reader in readers} == {"baby_1", "baby_4"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    fetches = calendar.await_count
    for reader in readers:
        await reader.async_refresh()
    await _next_poll(hass, freezer)

    assert calendar.await_count == fetches
    assert hub.care_log_coordinators == {}


_WITH_DROPPED_ROWS = build_care_log_data(
    [_log("s1", "sleep", 60), _log("d1", "diaper_change", 600)],
    window_start=SEED_START,
    day_start=DAY_START,
    previous=None,
    # Up to an hour ahead, as the reader asks: both rows are in the range.
    request_coverage=CareLogRequestCoverage(
        start=int(SEED_START), end=int(MIDNIGHT + 3600), dropped=4
    ),
)


@pytest.mark.parametrize(
    ("data", "has_data", "dropped_rows"),
    [
        (DAY, True, None),
        (_summary([]), False, None),
        (None, False, None),
        (_WITH_DROPPED_ROWS, True, 4),
    ],
)
async def test_diagnostics_report_only_whether_there_are_care_logs(
    data: CareLogData | None, has_data: bool, dropped_rows: int | None
) -> None:
    care = MagicMock(last_update_success=True, last_exception=None, data=data)
    cam_data = MagicMock(
        baby=Baby(uid="baby_1", name="Nursery", camera_uid="cam_1"),
        push_coordinator=MagicMock(
            last_update_success=True, last_exception=None, connected=True, data=None
        ),
        cloud_coordinator=None,
        care_log_coordinator=care,
    )
    entry = MagicMock(data={}, options={}, runtime_data=MagicMock(cameras={"cam_1": cam_data}))

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["cameras"]["camera_0"]["care_log_coordinator"] == {
        "last_update_success": True,
        "last_exception": None,
        "has_data": has_data,
        "dropped_rows": dropped_rows,
    }
