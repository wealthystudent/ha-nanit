"""A config entry with the statistics import on, end to end against a real recorder.

The integration is set up through Home Assistant with the faked Nanit
client of the shared fixtures, a real care log reader, and a calendar
answering from a small synthetic history. All data is synthetic, around
Friday 20 November 2026 18:10 UTC.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Camera imports pull in turbojpeg; stub it like test_read_only.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from aionanit.models import CareLog, CareLogFetch
from custom_components.nanit import care_log_importer, care_log_recorder
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import CareLogSeries, hour_start
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.care_log_statistics import statistic_id
from custom_components.nanit.care_log_writers import care_log_writer_registry
from custom_components.nanit.const import (
    CONF_IMPORT_CARE_LOG_STATISTICS,
    CONF_SHOW_CARE_LOG_NOTES,
    DOMAIN,
)
from custom_components.nanit.coordinator import NanitCareLogCoordinator
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics

from ..conftest import MOCK_BABY_1, MOCK_EMAIL, mock_entry_data_v2
from ..test_via_device_e2e import camera_fails, nanit_env  # noqa: F401  (shared fixtures)

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

NOW = datetime(2026, 11, 20, 18, 10, tzinfo=UTC)
UID = MOCK_BABY_1.uid
CODE = baby_code(UID)
FAR_PAST = datetime(2020, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

OLD_BOTTLE = CareLog(
    uid="b_old",
    log_type="bottle_feed",
    timestamp=(NOW - timedelta(days=20, minutes=30)).timestamp(),
    baby_uid=UID,
    feed_amount_ml=90.0,
)
DIAPER = CareLog(
    uid="d1",
    log_type="diaper_change",
    timestamp=(NOW - timedelta(days=2)).timestamp(),
    baby_uid=UID,
    change_type="pee",
)
BOTTLE = CareLog(
    uid="b1",
    log_type="bottle_feed",
    timestamp=(NOW - timedelta(hours=3, minutes=40)).timestamp(),
    baby_uid=UID,
    feed_amount_ml=120.0,
)
HISTORY = (OLD_BOTTLE, DIAPER, BOTTLE)


@pytest.fixture(autouse=True)
def quick() -> Iterator[None]:
    """No real pauses: time stands still under the freezer."""
    with (
        patch.object(care_log_recorder, "VERIFY_RETRY_DELAY", 0.0),
        patch.object(care_log_importer, "REQUEST_PAUSE", 0.0),
        patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)),
    ):
        yield


async def _answer(_baby_uid: str, start: int, end: int) -> CareLogFetch:
    logs = tuple(log for log in HISTORY if start <= log.timestamp <= end)
    return CareLogFetch(logs=logs, start=start, end=end, dropped=0)


async def _settled(hass: HomeAssistant, importer: CareLogStatisticsImporter) -> None:
    """Wait until the importer has nothing to do."""
    for _ in range(100):
        await hass.async_block_till_done()
        if importer._idle.is_set():
            return
        await asyncio.wait_for(importer._idle.wait(), timeout=30)


async def _stored(hass: HomeAssistant, series: CareLogSeries) -> dict[int, tuple[float, float]]:
    """Stored (state, sum) by hour."""
    sid = statistic_id(CODE, series)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        FAR_PAST,
        FAR_FUTURE,
        {sid},
        "hour",
        None,
        {"state", "sum"},
    )
    return {int(row["start"]): (row["state"], row["sum"]) for row in found.get(sid, [])}


async def test_an_entry_with_the_import_on_writes_statistics_and_reports_them(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    nanit_env: Any,  # noqa: F811
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(NOW)
    client = nanit_env
    client.async_get_babies.return_value = [MOCK_BABY_1]
    client.async_get_calendar_result = AsyncMock(side_effect=_answer)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        options={CONF_IMPORT_CARE_LOG_STATISTICS: True, CONF_SHOW_CARE_LOG_NOTES: False},
        version=2,
        minor_version=3,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.nanit.hub.NanitCareLogCoordinator", NanitCareLogCoordinator):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    registry = care_log_writer_registry(hass)
    assert registry is not None
    writer = registry.writer(CODE)
    assert writer is not None
    entry_id, importer = writer
    assert entry_id == entry.entry_id
    await _settled(hass, importer)

    # The reader's fetch and the importer's history pieces, all for this baby.
    assert {call.args[0] for call in client.async_get_calendar_result.await_args_list} == {UID}
    end_hour = hour_start(NOW.timestamp())
    diapers = await _stored(hass, CareLogSeries.DIAPER_CHANGES)
    assert min(diapers) == hour_start(DIAPER.timestamp)
    assert max(diapers) == end_hour - 3600
    assert diapers[hour_start(DIAPER.timestamp)] == (1.0, 1.0)
    assert diapers[end_hour - 3600][1] == 1.0
    volume = await _stored(hass, CareLogSeries.BOTTLE_VOLUME)
    assert min(volume) == hour_start(OLD_BOTTLE.timestamp)
    assert volume[hour_start(BOTTLE.timestamp)][0] == 120.0
    assert volume[end_hour - 3600][1] == 210.0
    # A series nothing was logged for has no statistic.
    assert await _stored(hass, CareLogSeries.PUMPED_VOLUME) == {}

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    section = diagnostics["care_log_statistics"]
    assert section["enabled"] is True
    (item,) = section["babies"].values()
    assert item["writer"] is True
    assert item["state"] == "running"
    assert item["established"] is True
    assert len(importer._book.ledger) == len(HISTORY)
    assert item["passes_completed"] == 1
    assert item["waiting_for_confirmation"] is False
    # Nothing about how many entries or rows there are.
    assert not {"entries", "rows_written", "rows_written_last_sync"} & set(item)
    assert item["minutes_since_last_sync"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert importer.diagnostics()["state"] == "stopped"
    assert care_log_writer_registry(hass) is None
