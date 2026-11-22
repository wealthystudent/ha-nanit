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

Each stage counts only once it really finished: every importer is
established, running, idle and has no sync waiting, its ledger holds
exactly the made-up history (every counted entry, and the same hourly
amounts), and a comparison of the stored rows with it finds nothing to
write. Those checks run after the stage's clock and probe have stopped. The
made-up history is built before the first stage, and the fake ``fetch``
only slices it (timed with the rest when profiling, below).

For each it reports the wall time, the longest the event loop was held (the
probe of the writer's measurement), the process's peak resident size, and
the peak Python allocations (tracemalloc). Python allocations are measured
in a second, identical run of the restart and the daily pass, because
tracemalloc slows everything it watches; the first import's peak is not
measured that way (a second first import would double the run), so its
line gives the resident size only. The pause between two requests of a
pass (``REQUEST_PAUSE``, 1 s in production) is set to 0, and the minimum
gap between passes to 2 s: the wall times are the work, not the waiting.

With ``NANIT_STATISTICS_COST_PROFILE=1`` it also says what held the loop.
A heartbeat task notes the time every 2 ms; a thread samples the loop
thread's stack every millisecond while the heartbeat is more than 10 ms
late, and every hold of more than 20 ms is kept with its samples. The
garbage collector's runs are timed through ``gc.callbacks``, and the calls
the importer makes on the loop are timed by wrappers. For each stage the
largest holds are printed with the functions most often on top of the
stack (the innermost frame of this integration, and the innermost frame
of all), the wrapped calls and the collections that ran inside them. The
sampling costs little, but the hold lengths come from a run without it.

Home Assistant's test plugin runs every test's event loop in asyncio's
debug mode, which captures a stack for every task and future it creates;
that alone held the loop for tens of milliseconds at a time. Production
runs without it, so the measurement turns it off for its stages.

The made-up history is moved out of the garbage collector's sight
(``gc.freeze``) once built: a real Home Assistant holds none of it, so its
objects must not make every full collection of the measured run longer.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import time
import tracemalloc
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from custom_components.nanit import coordinator as coordinator_module
from custom_components.nanit.care_log_hours import hourly_amounts
from custom_components.nanit.care_log_importer import CareLogStatisticsImporter
from custom_components.nanit.care_log_ledger import CareLogBook, CareLogRead, EntryFacts
from custom_components.nanit.care_log_recorder import (
    async_compare_care_log_statistics,
    coverage_hour,
)
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import NanitCareLogCoordinator

from ..conftest import MOCK_EMAIL, mock_entry_data_v2
from .test_statistics_cost import BABIES, YEARS, _history, _LoopProbe, _rss_mb

pytestmark = pytest.mark.skipif(
    os.environ.get("NANIT_STATISTICS_COST") != "1",
    reason="a measurement; set NANIT_STATISTICS_COST=1 to run it",
)

PROFILE = os.environ.get("NANIT_STATISTICS_COST_PROFILE") == "1"
# How long a stage may take before the measurement gives up on it.
STAGE_TIMEOUT = 3 * 3600.0
# The holds the profile explains, and how many of them per stage it prints.
SLOW_HOLD = 0.020
TOP_HOLDS = 3
HEARTBEAT = 0.002
SAMPLE_EVERY = 0.001
SAMPLE_AFTER = 0.010


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
    began: float = 0.0
    ended: float = 0.0


# ---------------------------------------------------------------------------
# Profiling: what held the loop
# ---------------------------------------------------------------------------


@dataclass
class _Profile:
    """Wrapped calls, collections, holds and stack samples, by perf_counter."""

    spans: list[tuple[str, float, float]] = field(default_factory=list)
    collections: list[tuple[int, float, float]] = field(default_factory=list)
    holds: list[tuple[float, float]] = field(default_factory=list)
    samples: list[tuple[float, str, str]] = field(default_factory=list)
    # What the other threads were doing at each sample: (time, "thread: frame").
    others: list[tuple[float, str]] = field(default_factory=list)
    beat: float = 0.0
    _gc_start: float = 0.0
    _stop: threading.Event = field(default_factory=threading.Event)
    # Spans are kept only for calls on the loop thread, where the profile is made.
    loop_thread: int = field(default_factory=threading.get_ident)

    def on_gc(self, phase: str, info: dict[str, Any]) -> None:
        now = time.perf_counter()
        if phase == "start":
            self._gc_start = now
        else:
            self.collections.append((int(info["generation"]), self._gc_start, now))

    async def heartbeat(self) -> None:
        self.beat = time.perf_counter()
        while True:
            await asyncio.sleep(HEARTBEAT)
            now = time.perf_counter()
            if now - self.beat - HEARTBEAT > SLOW_HOLD:
                self.holds.append((self.beat, now))
            self.beat = now

    def sample(self, thread_id: int) -> None:
        """Run in a thread: sample the loop thread's stack while the heartbeat is late."""
        while not self._stop.wait(SAMPLE_EVERY):
            now = time.perf_counter()
            if now - self.beat < SAMPLE_AFTER:
                continue
            frames = sys._current_frames()
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            mine = threading.get_ident()
            for ident, other in frames.items():
                if ident in (thread_id, mine):
                    continue
                code = other.f_code
                where = f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_qualname}"
                if code.co_qualname.endswith(("wait", "_worker", "select", "acquire", "get")):
                    continue  # waiting, not running
                self.others.append((now, f"{names.get(ident, '?')}: {where}"))
            frame = frames.get(thread_id)
            innermost = ours = ""
            depth = 0
            while frame is not None and depth < 200:
                code = frame.f_code
                where = f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_qualname}"
                if not innermost:
                    innermost = where
                if not ours and "custom_components/nanit" in code.co_filename:
                    ours = where
                frame = frame.f_back
                depth += 1
            self.samples.append((now, ours or "(not in the integration)", innermost))

    def stop(self) -> None:
        self._stop.set()

    def timed[**P, R](self, name: str, target: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            began = time.perf_counter()
            try:
                return target(*args, **kwargs)
            finally:
                if threading.get_ident() == self.loop_thread:  # not executor work
                    self.spans.append((name, began, time.perf_counter()))

        return wrapper

    def timed_iter[**P, T](
        self, name: str, target: Callable[P, Iterator[T]]
    ) -> Callable[P, Iterator[T]]:
        """Time a generator's own runs: each stretch of steps with no pause between them."""

        def wrapper(*args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
            inner = target(*args, **kwargs)
            began = last = time.perf_counter()
            while True:
                step = time.perf_counter()
                if step - last > 0.001:
                    self.spans.append((name, began, last))
                    began = step
                try:
                    item = next(inner)
                except StopIteration:
                    self.spans.append((name, began, time.perf_counter()))
                    return
                last = time.perf_counter()
                yield item

        return wrapper

    def timed_async[**P, R](
        self, name: str, target: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            began = time.perf_counter()
            try:
                return await target(*args, **kwargs)
            finally:
                self.spans.append((name, began, time.perf_counter()))

        return wrapper

    def report(self, stage: _Stage) -> list[str]:
        holds = sorted(
            (hold for hold in self.holds if stage.began <= hold[1] <= stage.ended),
            key=lambda hold: hold[0] - hold[1],
        )
        lines = [f"{stage.name}: {len(holds)} holds over {SLOW_HOLD * 1000:.0f} ms (sampled)"]
        for start, end in holds[:TOP_HOLDS]:
            inside: dict[str, float] = defaultdict(float)
            for name, low, high in self.spans:
                overlap = min(high, end) - max(low, start)
                if overlap > 0:
                    inside[name] += overlap
            collected = ", ".join(
                f"gen{gen} {(min(high, end) - max(low, start)) * 1000:.0f} ms"
                for gen, low, high in self.collections
                if (min(high, end) - max(low, start)) >= 0.001
            )
            ours: dict[str, int] = defaultdict(int)
            innermost: dict[str, int] = defaultdict(int)
            for at, mine, top in self.samples:
                if start <= at <= end:
                    ours[mine] += 1
                    innermost[top] += 1

            def top3(counts: dict[str, int]) -> str:
                best = sorted(counts.items(), key=lambda item: -item[1])[:3]
                return ", ".join(f"{name} x{count}" for name, count in best) or "no samples"

            parts = ", ".join(
                f"{name} {ms * 1000:.0f} ms"
                for name, ms in sorted(inside.items(), key=lambda item: -item[1])
                if ms * 1000 >= 1
            )
            lines.append(f"  {(end - start) * 1000:.0f} ms")
            lines.append(f"    integration frames: {top3(ours)}")
            lines.append(f"    innermost frames: {top3(innermost)}")
            running: dict[str, int] = defaultdict(int)
            for at, what in self.others:
                if start <= at <= end:
                    running[what] += 1
            lines.append(f"    other threads running: {top3(running)}")
            lines.append(f"    wrapped calls: {parts or 'none'}; gc: {collected or 'none'}")
        return lines


def _profiled(profile: _Profile) -> list[Any]:
    """Patches that time the calls the importer and the writer make on the loop."""
    importer = CareLogStatisticsImporter
    return [
        patch.object(CareLogBook, "process", profile.timed("book.process", CareLogBook.process)),
        patch.object(
            CareLogRead,
            "from_logs",
            classmethod(
                profile.timed("CareLogRead.from_logs", CareLogRead.__dict__["from_logs"].__func__)
            ),
        ),
        patch.object(
            importer,
            "_read_from_reader",
            profile.timed("importer._read_from_reader", importer._read_from_reader),
        ),
        patch.object(
            importer,
            "_process_reads",
            profile.timed("importer._process_reads", importer._process_reads),
        ),
        patch.object(
            care_log_recorder,
            "_cells",
            profile.timed_iter("recorder._cells", care_log_recorder._cells),
        ),
        patch.object(
            coordinator_module,
            "build_care_log_data",
            profile.timed(
                "coordinator.build_care_log_data", coordinator_module.build_care_log_data
            ),
        ),
        patch.object(_Nanit, "fetch", profile.timed_async("fake fetch", _Nanit.fetch)),
    ]


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


@pytest.fixture
def quiet_sql() -> Iterator[None]:
    """Turn off the SQL statement logging Home Assistant's test plugin switches on."""
    engine = logging.getLogger("sqlalchemy.engine")
    level = engine.level
    engine.setLevel(logging.WARNING)
    yield
    engine.setLevel(level)


def _tracked() -> str:
    """What the garbage collector tracks, outside the frozen history: in all, and by type."""
    counts: dict[str, int] = defaultdict(int)
    objects = gc.get_objects()
    for one in objects:
        counts[type(one).__qualname__] += 1
    ours = {name: counts[name] for name in ("EntryFacts", "CareLogRead", "CareLog", "mappingproxy")}
    top = sorted(counts.items(), key=lambda item: -item[1])[:6]
    del objects
    return (
        f"{sum(counts.values())} tracked; "
        + ", ".join(f"{name} {count}" for name, count in ours.items())
        + "; largest: "
        + ", ".join(f"{name} {count}" for name, count in top)
    )


def _counted(logs: list[CareLog]) -> set[str]:
    return {log.uid for log in logs if EntryFacts.from_log(log) is not None}


@pytest.mark.usefixtures("quiet_sql")
@pytest.mark.parametrize("persistent_database", [True])
async def test_the_cost_of_the_importer(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")
    lines: list[str] = []
    rows_sent: list[int] = []
    profile = _Profile()

    def counting_add(hass: HomeAssistant, metadata: Any, rows: Any) -> None:
        rows = list(rows)
        rows_sent.append(len(rows))
        async_add_external_statistics(hass, metadata, rows)

    adding = profile.timed("async_add_external_statistics", counting_add)

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
    # The made-up history is no part of a real Home Assistant's heap.
    gc.collect()
    gc.freeze()
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
                one.diagnostics()["state"] == "running"
                and one._idle.is_set()
                and one._book.established
                and not one._sync_wanted
                for one in importers
            ):
                await asyncio.sleep(0.05)

    async def check(importers: list[CareLogStatisticsImporter]) -> None:
        """The stage really finished: ledgers equal the history, the rows equal the ledgers."""
        for importer, nanit in zip(importers, nanits, strict=True):
            ledger = importer._book.ledger
            assert {uid for uid in _counted(nanit.logs) if uid in ledger} == _counted(nanit.logs)
            assert len(ledger) == len(_counted(nanit.logs))
            cov = importer._coverage_start
            assert cov is not None
            end_hour = importer._end_hour()
            finished = [log for log in nanit.logs if log.timestamp < end_hour]
            expected = hourly_amounts(finished, now=datetime.fromtimestamp(end_hour, UTC))
            assert ledger.amounts(coverage_hour(cov), end_hour) == expected
            comparison = await async_compare_care_log_statistics(
                hass, code=importer._code, ledger=ledger, end_hour=end_hour, coverage_start=cov
            )
            assert comparison.rows_to_write == 0, comparison
            assert comparison.lowering_cells == comparison.missing_rows == 0, comparison

    async def stage(name: str, run: Callable[[], Awaitable[None]]) -> _Stage:
        rows_sent.clear()
        requests = sum(nanit.requests for nanit in nanits)
        probe = _LoopProbe()
        probe.start()
        began = time.perf_counter()
        await run()
        ended = time.perf_counter()
        held = await probe.stop()
        if PROFILE:
            lines.append(f"objects, after {name}: {_tracked()}")
        await check(held_importers)
        return _Stage(
            name=name,
            seconds=ended - began,
            held_ms=held * 1000,
            rss_mb=_rss_mb(),
            rows=sum(rows_sent),
            requests=sum(nanit.requests for nanit in nanits) - requests,
            began=began,
            ended=ended,
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
        if PROFILE:
            while not all(
                one._first_pass is not None or one._book.established for one in importers
            ):
                await asyncio.sleep(0.01)
            lines.append(f"objects, waiting for the second pass: {_tracked()}")
        await settled(importers)
        assert all(one.diagnostics()["passes_completed"] == 2 for one in importers)
        return importers

    async def daily(importers: list[CareLogStatisticsImporter]) -> None:
        passes = [one.diagnostics()["passes_completed"] for one in importers]
        for importer in importers:
            importer._history_due = dt_util.utcnow()
            importer._wake()
        await asyncio.sleep(0)
        await settled(importers)
        assert [one.diagnostics()["passes_completed"] for one in importers] == [
            count + 1 for count in passes
        ]

    held_importers: list[CareLogStatisticsImporter] = []
    patches = [
        patch.object(care_log_recorder, "async_add_external_statistics", adding),
        patch.object(care_log_importer, "REQUEST_PAUSE", 0.0),
        patch.object(care_log_importer, "MIN_GAP", timedelta(seconds=2)),
        *(_profiled(profile) if PROFILE else ()),
    ]
    for one in patches:
        one.start()
    loop = asyncio.get_running_loop()
    debug = loop.get_debug()
    loop.set_debug(False)  # as in production; the test plugin turns it on
    heartbeat: asyncio.Task[None] | None = None
    sampler: threading.Thread | None = None
    if PROFILE:
        gc.callbacks.append(profile.on_gc)
        heartbeat = asyncio.get_running_loop().create_task(profile.heartbeat())
        sampler = threading.Thread(
            target=profile.sample, args=(threading.get_ident(),), daemon=True
        )
        sampler.start()
    try:

        async def run_first() -> None:
            held_importers[:] = await first_import()

        async def run_restart() -> None:
            held_importers[:] = await restart(held_importers)

        async def run_daily() -> None:
            await daily(held_importers)

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
        if PROFILE:
            for one in stages:
                lines.extend(profile.report(one))
            gc_ms = sorted((high - low) * 1000 for _gen, low, high in profile.collections)
            lines.append(
                f"garbage collections: {len(gc_ms)}, longest "
                + ", ".join(f"{ms:.1f}" for ms in gc_ms[-3:][::-1])
                + " ms"
            )
        else:
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

        for importer in held_importers:
            await importer.async_stop()
        for reader in readers:
            await reader.async_shutdown()
    finally:
        if PROFILE:
            profile.stop()
            if sampler is not None:
                sampler.join()
            if heartbeat is not None:
                heartbeat.cancel()
            gc.callbacks.remove(profile.on_gc)
        for one in reversed(patches):
            one.stop()
        gc.unfreeze()
        loop.set_debug(debug)

    await async_wait_recording_done(hass)
    print("\n" + "\n".join(f"IMPORTER COST {line}" for line in lines))
