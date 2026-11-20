"""Care Logs: the calendar summary, its coordinator, the sensors and diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from aionanit.exceptions import NanitAuthError, NanitConnectionError
from aionanit.models import Baby, CareLog
from custom_components.nanit.const import CARE_LOG_SEED_DAYS, DOMAIN
from custom_components.nanit.coordinator import (
    CareLogData,
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

from .conftest import MOCK_BABY_1, MOCK_EMAIL, mock_entry_data_v2

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


# ---------------------------------------------------------------------------
# NanitCareLogCoordinator
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)):
        yield


def _coordinator(hass: HomeAssistant, fetch: AsyncMock) -> NanitCareLogCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    hub = MagicMock()
    hub.client.async_get_calendar = fetch
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


async def test_entry_logged_after_midnight_for_the_evening_before_is_picked_up(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A night feed entered the next morning still becomes the last feeding."""
    freezer.move_to("2026-11-20T08:30:00+00:00")  # 00:30 local (US/Pacific)
    fetch = AsyncMock(return_value=[])
    coordinator = _coordinator(hass, fetch)
    await coordinator.async_refresh()
    late_bottle = CareLog(
        uid="b1",
        log_type="bottle_feed",
        timestamp=dt_util.start_of_local_day().timestamp() - 15 * 60,  # 23:45 the evening before
        baby_uid="baby_1",
        feed_amount_ml=90.0,
    )
    fetch.return_value = [late_bottle]

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


async def test_poll_retries_before_going_unavailable(hass: HomeAssistant) -> None:
    err = NanitConnectionError("Calendar fetch failed with HTTP 502")
    fetch = AsyncMock(side_effect=[err, err, err])
    coordinator = _coordinator(hass, fetch)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert fetch.await_count == 3


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
    return NanitCareLogSensor(coordinator, description)


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
    assert sensor.unique_id == "cam_1_last_diaper_change"


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


@pytest.mark.parametrize(
    "key",
    [
        "diaper_changes_today",
        "feedings_today",
        "bottle_volume_today",
        "nursing_time_today",
        "pumped_volume_today",
    ],
)
def test_daily_totals_reset_at_local_midnight(key: str) -> None:
    sensor = _sensor(key, DAY)

    assert sensor.entity_description.state_class == SensorStateClass.TOTAL
    assert sensor.last_reset == DAY_START


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


async def test_care_log_coordinator_failure_degrades_not_blocks(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """A failing Care Logs fetch at setup disables its sensors, not the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCareLogCoordinator") as care_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        care_cls.return_value = MagicMock(
            async_config_entry_first_refresh=AsyncMock(side_effect=ConfigEntryNotReady())
        )
        await hub.async_setup()

    data = hub.camera_data[MOCK_BABY_1.camera_uid]
    assert data.care_log_coordinator is None
    assert data.cloud_coordinator is not None
    await hub.async_close()


async def test_care_log_coordinator_auth_failure_propagates(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCareLogCoordinator") as care_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        care_cls.return_value = MagicMock(
            async_config_entry_first_refresh=AsyncMock(side_effect=ConfigEntryAuthFailed())
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await hub.async_setup()
    await hub.async_close()


async def test_diagnostics_report_care_log_counts_only() -> None:
    care = MagicMock(last_update_success=True, last_exception=None, data=DAY)
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
        "latest_types": ["bottle_feed", "diaper_change", "nursing", "pumping"],
        "today_count": 9,
    }
