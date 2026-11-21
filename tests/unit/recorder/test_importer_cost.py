"""What the care log statistics importer costs end to end: a measurement, not a check.

Skipped in the ordinary run. To measure, from the repository root:

    NANIT_STATISTICS_COST=1 uv run pytest tests/unit/recorder/test_importer_cost.py \\
        -s -p no:cacheprovider --show-capture=no

It prints its numbers. ``NANIT_STATISTICS_COST_YEARS`` (default 5) and
``NANIT_STATISTICS_COST_BABIES`` (default 3) shrink the case, as for the
writer's measurement in ``test_statistics_cost.py``, whose synthetic history
(about 20 entries a day per baby, seeded) it shares.

Unlike that one, this drives real ``CareLogStatisticsImporter`` workers, all
babies at once, each with a real care log reader, answering from the
synthetic history through a fake ``fetch``, into a file-backed SQLite
database, in real time:

- the first import: each importer's first pass (the history in 90-day
  pieces, the reader's answer, the ledger, the comparison) and its full sync;
- a restart: new importers, after one entry per baby was deleted, so the
  first pass would lower a stored cell and a second pass must agree before
  anything is written;
- an unchanged daily pass: every importer reads the whole history again and
  checks every stored row twice, writing nothing.

For each it reports the wall time, the longest the event loop was held (the
probe of the writer's measurement), the process's peak resident size, and
the peak Python allocations (tracemalloc). Python allocations are measured
in a second, identical run of the restart and the daily pass, because
tracemalloc slows everything it watches; the first import's peak is not
measured that way (a second first import would double the run), so its
line gives the resident size only. The pause between two requests of a
pass (``REQUEST_PAUSE``, 1 s in production) is set to 0, and the minimum
gap between passes to 2 s: the wall times are the work, not the waiting.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import time
import tracemalloc
from bisect import bisect_left, bisect_right
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from aionanit.models import Baby, CareLog, CareLogFetch
from custom_components.nanit import care_log_importer, care_log_recorder
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import NanitCareLogCoordinator

from ..conftest import MOCK_EMAIL, mock_entry_data_v2
from .test_statistics_cost import BABIES, YEARS, _history, _LoopProbe, _rss_mb

pytestmark = pytest.mark.skipif(
    os.environ.get("NANIT_STATISTICS_COST") != "1",
    reason="a measurement; set NANIT_STATISTICS_COST=1 to run it",
)

# How long a stage may take before the measurement gives up on it.
STAGE_TIMEOUT = 3 * 3600.0


class _Nanit:
    """One baby's synthetic history, answered by time range like Nanit's calendar."""

    def __init__(self, logs: list[CareLog]) -> None:
        self.logs = sorted(logs, key=lambda log: log.timestamp)
        self.times = [log.timestamp for log in self.logs]
        self.requests = 0

    def delete_one(self) -> None:
        """Delete the entry in the middle of the history."""
        index = len(self.logs) // 2
        del self.logs[index]
        del self.times[index]

    async def fetch(self, start: int, end: int) -> CareLogFetch:
        self.requests += 1
        low = bisect_left(self.times, start)
        high = bisect_right(self.times, end)
        return CareLogFetch(logs=tuple(self.logs[low:high]), start=start, end=end, dropped=0)

    async def reader_fetch(self, _uid: str, start: int, end: int) -> CareLogFetch:
        return await self.fetch(start, end)


@dataclass
class _Stage:
    name: str
    seconds: float
    held_ms: float
    rss_mb: float
    rows: int
    requests: int


@pytest.fixture
def quiet_sql() -> Iterator[None]:
    """Turn off the SQL statement logging Home Assistant's test plugin switches on."""
    engine = logging.getLogger("sqlalchemy.engine")
    level = engine.level
    engine.setLevel(logging.WARNING)
    yield
    engine.setLevel(level)


@pytest.mark.usefixtures("quiet_sql")
@pytest.mark.parametrize("persistent_database", [True])
async def test_the_cost_of_the_importer(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")
    lines: list[str] = []
    rows_sent: list[int] = []

    def counting_add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        rows = list(rows)
        rows_sent.append(len(rows))
        async_add_external_statistics(hass, metadata, rows)

    now = dt_util.utcnow()
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=round(365 * YEARS))
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)

    nanits: list[_Nanit] = []
    babies: list[Baby] = []
    readers: list[NanitCareLogCoordinator] = []
    for i in range(BABIES):
        nanit = _Nanit(_history(i, start, now))
        nanits.append(nanit)
        # The history starts a year before birth: here, at ``start``.
        birth = (start + timedelta(days=365)).date()
        babies.append(
            Baby(
                uid=f"baby_importer_cost_{i}",
                name=f"Cost {i}",
                camera_uid="",
                birth_date=date(birth.year, birth.month, birth.day),
            )
        )
        hub = MagicMock()
        hub.client.async_get_calendar_result = AsyncMock(side_effect=nanit.reader_fetch)
        readers.append(NanitCareLogCoordinator(hass, entry, hub, babies[i]))
    entries = sum(len(nanit.logs) for nanit in nanits)
    lines.append(f"case: {BABIES} babies at once, {YEARS:g} years, {entries} entries")

    def make() -> list[CareLogStatisticsImporter]:
        return [
            CareLogStatisticsImporter(
                hass, entry, baby=babies[i], coordinator=readers[i], fetch=nanits[i].fetch
            )
            for i in range(BABIES)
        ]

    async def settled(importers: list[CareLogStatisticsImporter]) -> None:
        async with asyncio.timeout(STAGE_TIMEOUT):
            while not all(
                one.diagnostics()["state"] == "running" and one._idle.is_set() for one in importers
            ):
                await asyncio.sleep(0.05)

    async def stage(name: str, run: Callable[[], Awaitable[None]]) -> _Stage:
        rows_sent.clear()
        requests = sum(nanit.requests for nanit in nanits)
        probe = _LoopProbe()
        probe.start()
        began = time.perf_counter()
        await run()
        took = time.perf_counter() - began
        held = await probe.stop()
        return _Stage(
            name=name,
            seconds=took,
            held_ms=held * 1000,
            rss_mb=_rss_mb(),
            rows=sum(rows_sent),
            requests=sum(nanit.requests for nanit in nanits) - requests,
        )

    async def first_import() -> list[CareLogStatisticsImporter]:
        importers = make()
        for importer, reader in zip(importers, readers, strict=True):
            importer.async_start()
            hass.async_create_task(reader.async_refresh())
        await settled(importers)
        return importers

    async def restart(old: list[CareLogStatisticsImporter]) -> list[CareLogStatisticsImporter]:
        for importer in old:
            await importer.async_stop()
        for nanit in nanits:
            nanit.delete_one()  # while stopped: the first pass would lower a cell
        importers = make()
        for importer, reader in zip(importers, readers, strict=True):
            importer.async_start()
            hass.async_create_task(reader.async_refresh())
        await settled(importers)
        assert all(one.diagnostics()["passes_completed"] == 2 for one in importers)
        return importers

    async def daily(importers: list[CareLogStatisticsImporter]) -> None:
        for importer in importers:
            importer._history_due = dt_util.utcnow()
            importer._wake()
        await asyncio.sleep(0)
        await settled(importers)

    held: list[CareLogStatisticsImporter] = []
    with (
        patch.object(care_log_recorder, "async_add_external_statistics", counting_add),
        patch.object(care_log_importer, "REQUEST_PAUSE", 0.0),
        patch.object(care_log_importer, "MIN_GAP", timedelta(seconds=2)),
    ):

        async def run_first() -> None:
            held[:] = await first_import()

        async def run_restart() -> None:
            held[:] = await restart(held)

        async def run_daily() -> None:
            await daily(held)

        stages = [
            await stage("first import", run_first),
            await stage("restart, a second pass needed", run_restart),
            await stage("unchanged daily pass", run_daily),
        ]
        for one in stages:
            lines.append(
                f"{one.name}: {one.seconds / 60:.1f} min ({one.seconds:.0f} s), "
                f"{one.rows} rows sent, {one.requests} own requests; event loop held at "
                f"most {one.held_ms:.1f} ms; peak resident size so far {one.rss_mb:.0f} MB"
            )
        assert stages[2].rows == 0

        # Peak Python allocations: the restart and the daily pass again, traced.
        for name, run in (("restart", run_restart), ("unchanged daily pass", run_daily)):
            gc.collect()
            tracemalloc.start()
            began = time.perf_counter()
            await run()
            took = time.perf_counter() - began
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            lines.append(
                f"memory, {name} again under tracemalloc ({took:.0f} s): "
                f"{peak / 2**20:.1f} MB peak Python allocations"
            )

        for importer in held:
            await importer.async_stop()
        for reader in readers:
            await reader.async_shutdown()

    await async_wait_recording_done(hass)
    print("\n" + "\n".join(f"IMPORTER COST {line}" for line in lines))
