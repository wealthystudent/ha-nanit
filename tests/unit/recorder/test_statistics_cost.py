"""What the dense row shape costs for a long history: a measurement, not a check.

Skipped in the ordinary run. To measure, from the repository root:

    NANIT_STATISTICS_COST=1 uv run pytest tests/unit/recorder/test_statistics_cost.py \
        -s -p no:cacheprovider --show-capture=no

It prints its numbers. ``NANIT_STATISTICS_COST_YEARS`` (default 5) and
``NANIT_STATISTICS_COST_BABIES`` (default 3) shrink the case for a quick
try. The history is synthetic: about 20 entries a day per baby (diapers,
bottles, nursing and pumping), seeded so every run is the same.

What it measures, against the limits proposed before measuring:

- the time to work out the hourly amounts, and the number of rows;
- the time to import them in batches of at most 5,000 rows, waiting for the
  recorder after each batch, into a file-backed SQLite database;
- the longest stretch the event loop was held, by a probe that sleeps 5 ms
  at a time throughout, and the longest single call made on the loop;
- the time to read every row back;
- an unchanged comparison (amounts again, read back, compare) that writes
  nothing;
- peak memory: Python allocations while working out and building one
  baby's rows (tracemalloc), and the process's peak resident size;
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
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from aionanit.models import CareLog
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.care_log_hours import (
    HOUR_SECONDS,
    CareLogSeries,
    HourlyAmounts,
    hour_start,
    hourly_amounts,
)
from custom_components.nanit.care_log_statistics import (
    series_amounts,
    statistic_id,
    statistic_metadata,
    statistic_rows,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("NANIT_STATISTICS_COST") != "1",
    reason="a measurement; set NANIT_STATISTICS_COST=1 to run it",
)

YEARS = float(os.environ.get("NANIT_STATISTICS_COST_YEARS", "5"))
BABIES = int(os.environ.get("NANIT_STATISTICS_COST_BABIES", "3"))
BATCH_ROWS = 5000
PROBE_INTERVAL = 0.005


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


@dataclass
class _Report:
    lines: list[str] = field(default_factory=list)
    longest_call: float = 0.0

    def add(self, text: str) -> None:
        self.lines.append(text)

    @contextmanager
    def on_loop(self) -> Iterator[None]:
        """Time a synchronous stretch run on the event loop."""
        began = time.perf_counter()
        try:
            yield
        finally:
            self.longest_call = max(self.longest_call, time.perf_counter() - began)


def _range(amounts: HourlyAmounts) -> tuple[int, int]:
    hours = list(amounts.amounts)
    return hours[0], hours[-1] + HOUR_SECONDS


def _batches(first: int, end: int) -> Iterator[tuple[int, int]]:
    step = BATCH_ROWS * HOUR_SECONDS
    for low in range(first, end, step):
        yield low, min(low + step, end)


async def _import_baby(
    hass: HomeAssistant, report: _Report, code: str, amounts: HourlyAmounts
) -> tuple[int, int]:
    """Import every series of one baby; return the rows and batches written."""
    first, end = _range(amounts)
    rows_written = 0
    batches = 0
    for series in CareLogSeries:
        values = series_amounts(amounts.amounts, series)
        metadata = statistic_metadata(code, series, f"Cost test {series.value}")
        total = 0.0
        for low, high in _batches(first, end):
            with report.on_loop():
                piece = {h: v for h, v in values.items() if low <= h < high}
                rows = statistic_rows(piece, start=low, end=high, sum_before=total)
                async_add_external_statistics(hass, metadata, rows)
            total = rows[-1]["sum"]
            rows_written += len(rows)
            batches += 1
            await async_wait_recording_done(hass)
    return rows_written, batches


def _read_back(hass: HomeAssistant, ids: set[str], start: datetime, end: datetime) -> dict:
    return statistics_during_period(hass, start, end, ids, "hour", None, {"state", "sum"})


def _expected(code: str, amounts: HourlyAmounts) -> dict[str, list[StatisticData]]:
    first, end = _range(amounts)
    return {
        statistic_id(code, series): statistic_rows(
            series_amounts(amounts.amounts, series), start=first, end=end
        )
        for series in CareLogSeries
    }


def _matches(expected: dict[str, list[StatisticData]], stored: dict) -> bool:
    for sid, rows in expected.items():
        got = stored.get(sid, [])
        if len(got) != len(rows):
            return False
        for want, have in zip(rows, got, strict=True):
            if (
                want["start"].timestamp() != have["start"]
                or want["state"] != have["state"]
                or abs(want["sum"] - have["sum"]) > 1e-6
            ):
                return False
    return True


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _db_path(instance: Recorder) -> Path:
    url = instance.db_url
    assert url.startswith("sqlite:///"), url
    return Path(url.removeprefix("sqlite:///"))


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
async def test_the_cost_of_dense_rows(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    report = _Report()
    rss_start = _rss_mb()
    now = dt_util.utcnow()
    end = datetime.fromtimestamp(hour_start(now.timestamp()), UTC)
    start = end - timedelta(days=round(365 * YEARS))
    babies = [(baby_code(f"baby_cost_{i}"), _history(i, start, end)) for i in range(BABIES)]
    entries = sum(len(logs) for _code, logs in babies)
    report.add(f"case: {BABIES} babies, {YEARS:g} years, {entries} entries")

    # Working out the amounts (pure CPU, timed on its own).
    began = time.perf_counter()
    computed: list[tuple[str, HourlyAmounts]] = []
    longest_compute = 0.0
    for code, logs in babies:
        one = time.perf_counter()
        computed.append((code, hourly_amounts(logs, now=now)))
        longest_compute = max(longest_compute, time.perf_counter() - one)
    compute_s = time.perf_counter() - began
    rows_total = sum(
        (_range(amounts)[1] - _range(amounts)[0]) // HOUR_SECONDS * len(CareLogSeries)
        for _code, amounts in computed
    )
    report.add(
        f"compute: {compute_s:.2f} s for all babies "
        f"(longest single baby {longest_compute * 1000:.0f} ms); rows: {rows_total}"
    )

    # Peak Python allocations for one baby: amounts plus every row at once.
    gc.collect()
    tracemalloc.start()
    code, logs = babies[0]
    one_amounts = hourly_amounts(logs, now=now)
    one_rows = _expected(code, one_amounts)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del one_rows, one_amounts
    report.add(
        f"memory: {peak / 2**20:.0f} MB peak Python allocations for one baby's amounts "
        f"and all its rows built at once"
    )

    # The import, in batches, waiting for the recorder after each.
    probe = _LoopProbe()
    probe.start()
    began = time.perf_counter()
    written = 0
    batches = 0
    for code, amounts in computed:
        rows, count = await _import_baby(hass, report, code, amounts)
        written += rows
        batches += count
    import_s = time.perf_counter() - began
    worst_import = await probe.stop()
    assert written == rows_total
    report.add(
        f"import: {import_s:.1f} s for {written} rows in {batches} batches of at most "
        f"{BATCH_ROWS} ({written / import_s:.0f} rows/s)"
    )
    report.add(
        f"event loop during import: held at most {worst_import * 1000:.1f} ms "
        f"(probe); longest single call on the loop {report.longest_call * 1000:.1f} ms"
    )

    # Reading every row back.
    instance = get_instance(hass)
    began = time.perf_counter()
    stored: dict = {}
    for code, amounts in computed:
        first, last = _range(amounts)
        ids = {statistic_id(code, series) for series in CareLogSeries}
        stored.update(
            await instance.async_add_executor_job(
                _read_back,
                hass,
                ids,
                datetime.fromtimestamp(first, UTC),
                datetime.fromtimestamp(last, UTC),
            )
        )
    read_s = time.perf_counter() - began
    assert sum(len(rows) for rows in stored.values()) == rows_total
    report.add(f"read back: {read_s:.1f} s for {rows_total} rows")
    del stored

    # An unchanged comparison: work out, read back, compare; nothing written.
    probe.start()
    began = time.perf_counter()
    all_match = True
    for (code, logs), (_same, _amounts) in zip(babies, computed, strict=True):
        amounts = await hass.async_add_executor_job(lambda logs=logs: hourly_amounts(logs, now=now))
        expected = await hass.async_add_executor_job(_expected, code, amounts)
        first, last = _range(amounts)
        stored_one = await instance.async_add_executor_job(
            _read_back,
            hass,
            set(expected),
            datetime.fromtimestamp(first, UTC),
            datetime.fromtimestamp(last, UTC),
        )
        all_match &= await hass.async_add_executor_job(_matches, expected, stored_one)
    compare_s = time.perf_counter() - began
    worst_compare = await probe.stop()
    assert all_match
    report.add(
        f"unchanged comparison: {compare_s:.1f} s, wrote nothing, "
        f"event loop held at most {worst_compare * 1000:.1f} ms"
    )

    await async_wait_recording_done(hass)
    path = _db_path(instance)
    size = sum(
        p.stat().st_size for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()
    )
    report.add(f"database: {size / 2**20:.0f} MB on disk (with WAL)")
    report.add(f"process peak resident size: {_rss_mb():.0f} MB (was {rss_start:.0f} MB at start)")

    print("\n" + "\n".join(f"COST {line}" for line in report.lines))
