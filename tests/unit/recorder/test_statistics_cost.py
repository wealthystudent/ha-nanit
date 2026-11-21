"""What the care log statistics writer costs for a long history: a measurement, not a check.

Skipped in the ordinary run. To measure, from the repository root:

    NANIT_STATISTICS_COST=1 uv run pytest tests/unit/recorder/test_statistics_cost.py \
        -s -p no:cacheprovider --show-capture=no

It prints its numbers. ``NANIT_STATISTICS_COST_YEARS`` (default 5) and
``NANIT_STATISTICS_COST_BABIES`` (default 3) shrink the case for a quick
try. The history is synthetic: about 20 entries a day per baby (diapers,
bottles, nursing and pumping), seeded so every run is the same.

It drives the real writer, ``async_sync_care_log_statistics``, on ledgers
built by the real ``CareLogBook``, into a file-backed SQLite database:

- building each baby's ledger from its history;
- the first import (a full sync of every baby), in batches of at most
  ``BATCH_ROWS`` rows, each read back before the next is sent;
- an unchanged full sync of every baby, which must write nothing;
- an incremental sync of one baby after one late entry a year back;
- throughout, the longest stretch the event loop was held, by a probe that
  sleeps 5 ms at a time;
- peak memory: Python allocations during one baby's unchanged full sync
  (tracemalloc), and the process's peak resident size;
- the size of the database file.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import random
import resource
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from aionanit.models import CareLog
from custom_components.nanit import care_log_recorder
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import hour_start
from custom_components.nanit.care_log_ledger import CareLogBook, CareLogRead
from custom_components.nanit.care_log_recorder import (
    BATCH_ROWS,
    SyncReport,
    async_sync_care_log_statistics,
)
from custom_components.nanit.care_log_statistics import statistic_names

pytestmark = pytest.mark.skipif(
    os.environ.get("NANIT_STATISTICS_COST") != "1",
    reason="a measurement; set NANIT_STATISTICS_COST=1 to run it",
)

YEARS = float(os.environ.get("NANIT_STATISTICS_COST_YEARS", "5"))
BABIES = int(os.environ.get("NANIT_STATISTICS_COST_BABIES", "3"))
PROBE_INTERVAL = 0.005
READER = "reader-cost"
GAP = timedelta(minutes=10)


def _history(seed: int, start: datetime, end: datetime) -> list[CareLog]:
    """About 20 synthetic entries a day: 7 diapers, 6 bottles, 4 nursing, 3 pumping."""
    rng = random.Random(seed)
    logs: list[CareLog] = []
    day = start
    n = 0
    while day < end:
        plan = ["diaper_change"] * 7 + ["bottle_feed"] * 6 + ["nursing"] * 4 + ["pumping"] * 3
        for kind in plan:
            when = day + timedelta(seconds=rng.randrange(86400))
            if when >= end:
                continue
            n += 1
            fields: dict[str, object] = {}
            if kind == "diaper_change":
                fields["change_type"] = rng.choice(["pee", "poo", "mixed", "dry", None])
            elif kind == "bottle_feed":
                if rng.random() < 0.9:
                    fields["feed_amount_ml"] = float(rng.randrange(30, 240, 5))
            elif kind == "nursing":
                fields["left_duration_s"] = float(rng.randrange(60, 1500))
                fields["right_duration_s"] = float(rng.randrange(60, 1500))
            else:
                fields["left_amount_ml"] = float(rng.randrange(10, 150))
                fields["right_amount_ml"] = float(rng.randrange(10, 150))
                fields["duration_s"] = float(rng.randrange(300, 2400))
            logs.append(
                CareLog(
                    uid=f"e{seed}-{n}",
                    log_type=kind,
                    timestamp=when.timestamp(),
                    baby_uid="b",
                    **fields,
                )
            )
        day += timedelta(days=1)
    return logs


@dataclass
class _LoopProbe:
    """Measures how late a 5 ms sleep wakes up: the longest the loop was held."""

    worst: float = 0.0
    task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(PROBE_INTERVAL)
            self.worst = max(self.worst, loop.time() - before - PROBE_INTERVAL)

    def start(self) -> None:
        self.worst = 0.0
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> float:
        assert self.task is not None
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        return self.worst


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _db_path(instance: Recorder) -> Path:
    url = instance.db_url
    assert url.startswith("sqlite:///"), url
    return Path(url.removeprefix("sqlite:///"))


def _read(logs: list[CareLog], start: int, end: int, at: datetime) -> CareLogRead:
    return CareLogRead.from_logs(logs, start=start, end=end, read_at=at, dropped=0, reader=READER)


@pytest.fixture
def quiet_sql() -> Iterator[None]:
    """Turn off the SQL statement logging Home Assistant's test plugin switches on.

    It logs every statement at INFO, which production doesn't, and which
    would make the import look several times slower than it is.
    """
    engine = logging.getLogger("sqlalchemy.engine")
    level = engine.level
    engine.setLevel(logging.WARNING)
    yield
    engine.setLevel(level)


@pytest.mark.usefixtures("quiet_sql")
@pytest.mark.parametrize("persistent_database", [True])
async def test_the_cost_of_the_writer(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    lines: list[str] = []
    rss_start = _rss_mb()
    now = dt_util.utcnow()
    end_hour = hour_start(now.timestamp())
    end = datetime.fromtimestamp(end_hour, UTC)
    start = end - timedelta(days=round(365 * YEARS))
    read_start, read_end = int(start.timestamp()), end_hour - 1
    names = statistic_names({}, "Cost test")
    calls: list[int] = []

    def counting_add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        calls.append(1)
        async_add_external_statistics(hass, metadata, rows)

    # The ledgers, through the real book.
    began = time.perf_counter()
    babies: list[tuple[str, CareLogBook, list[CareLog]]] = []
    for i in range(BABIES):
        logs = _history(i, start, end)
        book = CareLogBook(reader=READER, min_gap=GAP)
        book.adopt([_read(logs, read_start, read_end, now)])
        babies.append((baby_code(f"baby_cost_{i}"), book, logs))
    entries = sum(len(book.ledger) for _code, book, _logs in babies)
    lines.append(f"case: {BABIES} babies, {YEARS:g} years, {entries} entries in the ledgers")
    lines.append(f"ledgers: built in {time.perf_counter() - began:.1f} s")

    async def sync(code: str, book: CareLogBook, **kwargs: Any) -> tuple[SyncReport, float, float]:
        probe = _LoopProbe()
        probe.start()
        began = time.perf_counter()
        report = await async_sync_care_log_statistics(
            hass, code=code, names=names, ledger=book.ledger, end_hour=end_hour, **kwargs
        )
        took = time.perf_counter() - began
        return report, took, await probe.stop()

    with patch.object(care_log_recorder, "async_add_external_statistics", counting_add):
        # The first import.
        total = worst = 0.0
        written = batches = 0
        for code, book, _logs in babies:
            report, took, held = await sync(code, book)
            total += took
            worst = max(worst, held)
            written += report.rows_written
            batches += report.batches
        lines.append(
            f"first import: {total / 60:.1f} min for {written} rows in {batches} batches of at "
            f"most {BATCH_ROWS} ({written / total:.0f} rows/s); event loop held at most "
            f"{worst * 1000:.1f} ms"
        )

        # An unchanged full sync of every baby.
        calls.clear()
        total = worst = 0.0
        read = written = 0
        for code, book, _logs in babies:
            report, took, held = await sync(code, book)
            total += took
            worst = max(worst, held)
            read += report.rows_read
            written += report.rows_written
        assert written == 0
        assert calls == []
        lines.append(
            f"unchanged full sync: {total:.1f} s, {read} rows read, wrote nothing "
            f"({len(calls)} calls); event loop held at most {worst * 1000:.1f} ms"
        )

        # Peak Python allocations during one baby's unchanged full sync.
        gc.collect()
        tracemalloc.start()
        await sync(babies[0][0], babies[0][1])
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        lines.append(
            f"memory: {peak / 2**20:.1f} MB peak Python allocations during one baby's "
            "unchanged full sync"
        )

        # One late entry a year back, for one baby: an incremental sync.
        code, book, logs = babies[0]
        late = CareLog(
            uid="late-entry",
            log_type="bottle_feed",
            timestamp=(end - timedelta(days=365, minutes=-30)).timestamp(),
            baby_uid="b",
            feed_amount_ml=100.0,
        )
        outcome = book.process(_read([*logs, late], read_start, read_end, now + GAP))
        assert outcome.accepted == 1
        calls.clear()
        report, took, held = await sync(code, book, changed_from=outcome.changed_from)
        lines.append(
            f"incremental sync after a late entry a year back: {took:.1f} s, "
            f"{report.rows_read} rows read, {report.rows_written} rows written in "
            f"{report.batches} batches ({len(calls)} calls); event loop held at most "
            f"{held * 1000:.1f} ms"
        )

    await async_wait_recording_done(hass)
    path = _db_path(get_instance(hass))
    size = sum(
        p.stat().st_size for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()
    )
    lines.append(f"database: {size / 2**20:.0f} MB on disk (with WAL)")
    lines.append(
        f"process peak resident size: {_rss_mb():.0f} MB (was {rss_start:.0f} MB at start)"
    )

    print("\n" + "\n".join(f"COST {line}" for line in lines))
