"""The care log calendar: privacy, ranges, text, cache and life cycle."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

# Camera imports pull in turbojpeg; stub it like test_read_only.py does.
_ = sys.modules.setdefault("turbojpeg", MagicMock(TurboJPEG=MagicMock()))

from aionanit.exceptions import NanitAuthError, NanitConnectionError
from aionanit.models import (
    Baby,
    CareLog,
    CareLogDetail,
    CareLogDetailFetch,
    CareLogEntry,
    CareLogFetch,
)
from custom_components.nanit import calendar as calendar_module
from custom_components.nanit import config_flow  # noqa: F401  (imported unpatched, see below)
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.calendar import (
    CARE_LOG_CACHE_SIZE,
    CARE_LOG_CACHE_TTL,
    CARE_LOG_MAX_PIECE,
    FALLBACK_TEXT,
    NanitCareLogCalendar,
)
from custom_components.nanit.const import (
    CONF_IMPORT_CARE_LOG_STATISTICS,
    CONF_READ_ONLY,
    CONF_SHOW_CARE_LOG_NOTES,
    DOMAIN,
)
from custom_components.nanit.coordinator import NanitCareLogCoordinator
from custom_components.nanit.diagnostics import async_get_config_entry_diagnostics

from .conftest import MOCK_EMAIL, mock_entry_data_v2
from .test_via_device_e2e import BABY as E2E_BABY
from .test_via_device_e2e import camera_fails, nanit_env  # noqa: F401  (shared fixtures)

# config_flow is imported above before any test patches NanitClient: a setup
# or reload here could otherwise be the first to import it, keeping the mock.

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

# Planted markers. None of them may leak where the tests say it must not.
NOTE_MARKER = "zqnotemarker7731"
NAME_MARKER = "zqnamemarker5512"
RELATION_MARKER = "zqrelationmarker2290"
BABY_MARKER = "zqbabymarker9043"
PRIVATE_MARKERS = (NOTE_MARKER, NAME_MARKER, RELATION_MARKER)

NOW = datetime.fromisoformat("2026-11-20T18:00:00+00:00")
BABY = Baby(uid=BABY_MARKER, name="Nursery", camera_uid="cam_1")


@pytest.fixture
def client(nanit_env: Any) -> Any:  # noqa: F811
    """The faked Nanit client, with cameras, speakers and side coordinators that work."""
    return nanit_env


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)):
        yield


# ---------------------------------------------------------------------------
# A fake Nanit calendar
# ---------------------------------------------------------------------------


def _ts(moment: datetime) -> float:
    return moment.timestamp()


def _log(
    uid: str, kind: str, start: datetime, baby_uid: str = BABY_MARKER, **fields: Any
) -> CareLog:
    return CareLog(uid=uid, log_type=kind, timestamp=_ts(start), baby_uid=baby_uid, **fields)


@dataclass
class FakeNanit:
    """Answers /calendar like Nanit does: by start time, both ends inclusive."""

    entries: list[CareLogEntry] = field(default_factory=list)
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    error: BaseException | None = None
    gate: asyncio.Event | None = None
    in_flight: int = 0
    most_in_flight: int = 0
    # Ranges the reader itself asks for are answered but not recorded.
    reader_calls: int = 0

    def add(
        self,
        log: CareLog,
        note: str | None = NOTE_MARKER,
        name: str | None = NAME_MARKER,
        relation: str | None = RELATION_MARKER,
    ) -> None:
        detail = CareLogDetail(comment=note, user_name=name, user_relation=relation)
        self.entries.append(CareLogEntry(log=log, detail=detail))

    async def _answer(self, method: str, start: int, end: int) -> list[CareLogEntry]:
        self.calls.append((method, "", start, end))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            return [e for e in self.entries if start <= e.log.timestamp <= end]
        finally:
            self.in_flight -= 1

    async def result(self, baby_uid: str, start: int, end: int) -> CareLogFetch:
        entries = await self._answer("result", start, end)
        return CareLogFetch(logs=tuple(e.log for e in entries), start=start, end=end, dropped=0)

    async def details(self, baby_uid: str, start: int, end: int) -> CareLogDetailFetch:
        entries = await self._answer("details", start, end)
        return CareLogDetailFetch(entries=tuple(entries), start=start, end=end, dropped=0)

    def calendar_calls(self) -> list[tuple[str, int, int]]:
        """The calls the calendar made, without the reader's own polls."""
        return [(m, s, e) for m, _b, s, e in self.calls if (m, s, e) not in self._reader_ranges]

    _reader_ranges: set[tuple[str, int, int]] = field(default_factory=set)

    def forget_calls(self) -> None:
        """Treat every call so far as the reader's."""
        self._reader_ranges |= {(m, s, e) for m, _b, s, e in self.calls}
        self.calls.clear()


def _entry(hass: HomeAssistant, *, notes: bool = False, email: str = MOCK_EMAIL) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_entry_data_v2(), "email": email},
        options={CONF_SHOW_CARE_LOG_NOTES: notes},
        version=2,
        minor_version=3,
        unique_id=email,
    )
    entry.add_to_hass(hass)
    return entry


def _wire(client: Any, fake: FakeNanit, babies: list[Baby] | None = None) -> None:
    client.async_get_babies.return_value = babies or [BABY]
    client.async_get_calendar_result = AsyncMock(side_effect=fake.result)
    client.async_get_calendar_details = AsyncMock(side_effect=fake.details)


async def _set_up(
    hass: HomeAssistant,
    client: Any,
    fake: FakeNanit,
    *,
    notes: bool = False,
    babies: list[Baby] | None = None,
    entry: MockConfigEntry | None = None,
) -> MockConfigEntry:
    _wire(client, fake, babies)
    entry = entry or _entry(hass, notes=notes)
    with patch("custom_components.nanit.hub.NanitCareLogCoordinator", NanitCareLogCoordinator):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    fake.forget_calls()
    return entry


def _calendars(hass: HomeAssistant, entry: MockConfigEntry) -> list[NanitCareLogCalendar]:
    return [
        entity
        for platform in async_get_platforms(hass, DOMAIN)
        if platform.domain == "calendar" and platform.config_entry is entry
        for entity in platform.entities.values()
        if isinstance(entity, NanitCareLogCalendar)
    ]


def _calendar(hass: HomeAssistant, entry: MockConfigEntry) -> NanitCareLogCalendar:
    (calendar,) = _calendars(hass, entry)
    return calendar


def _local(text: str) -> datetime:
    """A wall-clock time in Home Assistant's time zone (US/Pacific in tests)."""
    return datetime.fromisoformat(text).replace(tzinfo=dt_util.get_default_time_zone())


async def _events(
    hass: HomeAssistant, calendar: NanitCareLogCalendar, start: datetime, end: datetime
) -> list[Any]:
    return await calendar.async_get_events(hass, start, end)


def _summaries(events: list[Any]) -> list[str]:
    return [event.summary for event in events]


RANGE_START = _local("2026-11-20T00:00:00")
RANGE_END = _local("2026-11-21T00:00:00")


def _day_of_entries(fake: FakeNanit) -> None:
    """One of each kind on 20 November, with notes, names and relations."""
    fake.add(_log("d1", "diaper_change", _local("2026-11-20T07:00:00"), change_type="mixed"))
    fake.add(
        _log(
            "b1",
            "bottle_feed",
            _local("2026-11-20T08:00:00"),
            feed_amount_ml=30.0,
            logged_at=_ts(_local("2026-11-20T08:20:00")),
        )
    )
    fake.add(
        _log(
            "n1",
            "nursing",
            _local("2026-11-20T09:00:00"),
            left_duration_s=600.0,
            right_duration_s=300.0,
        )
    )
    fake.add(
        _log(
            "p1",
            "pumping",
            _local("2026-11-20T10:00:00"),
            left_amount_ml=20.0,
            right_amount_ml=10.0,
            duration_s=1200.0,
        )
    )


# ---------------------------------------------------------------------------
# Privacy markers (written first)
# ---------------------------------------------------------------------------


def _chain(err: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        yield err
        err = err.__cause__ or err.__context__


def _event_fields(event: Any) -> str:
    return json.dumps(event.as_dict(), default=str)


async def _surfaces(
    hass: HomeAssistant,
    hass_client: Any,
    entry: MockConfigEntry,
    calendar: NanitCareLogCalendar,
) -> dict[str, str]:
    """Every surface the markers are checked on, as text."""
    events = await _events(hass, calendar, RANGE_START, RANGE_END)
    http = await hass_client()
    response = await http.get(
        f"/api/calendars/{calendar.entity_id}",
        params={"start": RANGE_START.isoformat(), "end": RANGE_END.isoformat()},
    )
    assert response.status == 200
    rest = await response.text()
    action = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": calendar.entity_id,
            "start_date_time": RANGE_START.isoformat(),
            "end_date_time": RANGE_END.isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    states = json.dumps(
        [(s.entity_id, s.state, dict(s.attributes)) for s in hass.states.async_all()],
        default=str,
    )
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    reprs = " ".join(
        [
            repr(calendar),
            repr(calendar._cache),
            *(repr(cached.rows) for cached in calendar._cache.values()),
            repr(calendar._in_flight),
        ]
    )
    return {
        "events": " ".join(_event_fields(event) for event in events),
        "rest": rest,
        "action": json.dumps(action, default=str),
        "states": states,
        "diagnostics": diagnostics,
        "reprs": reprs,
    }


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
async def test_privacy_markers(
    hass: HomeAssistant,
    hass_client: Any,
    client: Any,
    caplog: pytest.LogCaptureFixture,
    notes: bool,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeNanit()
    _day_of_entries(fake)
    # With the statistics import on, so diagnostics has its section too.
    entry = _entry(hass, notes=notes)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_IMPORT_CARE_LOG_STATISTICS: True}
    )
    entry = await _set_up(hass, client, fake, notes=notes, entry=entry)
    calendar = _calendar(hass, entry)

    surfaces = await _surfaces(hass, hass_client, entry, calendar)
    statistics = (await async_get_config_entry_diagnostics(hass, entry))["care_log_statistics"]
    assert statistics["enabled"] is True
    assert statistics["babies"]["baby_0"]["writer"] is True
    section = json.dumps(statistics)
    for marker in (BABY_MARKER, baby_code(BABY_MARKER), BABY.name, entry.entry_id):
        assert marker not in section, marker
    assert baby_code(BABY_MARKER) not in surfaces["diagnostics"]

    # Only the calendar answer and the action response may carry notes and
    # names, and only with the option on.
    answers = ("events", "rest", "action")
    for surface, text in surfaces.items():
        assert BABY_MARKER not in text or surface in ("states", "diagnostics", "reprs"), surface
        for marker in PRIVATE_MARKERS:
            if notes and surface in answers:
                assert marker in text, (surface, marker)
            else:
                assert marker not in text, (surface, marker)
    for marker in PRIVATE_MARKERS:
        assert marker not in caplog.text, marker
    if notes:
        client.async_get_calendar_details.assert_awaited()
    else:
        client.async_get_calendar_details.assert_not_awaited()
    assert hass.states.get(calendar.entity_id).state == STATE_OFF
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
async def test_the_baby_uid_never_reaches_an_event_field(
    hass: HomeAssistant,
    hass_client: Any,
    client: Any,
    notes: bool,
) -> None:
    fake = FakeNanit()
    _day_of_entries(fake)
    fake.add(_log("x1", "temperature", _local("2026-11-20T11:00:00"), duration_s=60.0))
    entry = await _set_up(hass, client, fake, notes=notes)
    calendar = _calendar(hass, entry)

    surfaces = await _surfaces(hass, hass_client, entry, calendar)

    for surface in ("events", "rest", "action"):
        assert BABY_MARKER not in surfaces[surface], surface
        # Nor the entry uids or the camera uid.
        for uid in ("d1", "b1", "n1", "p1", "x1", "cam_1"):
            assert f'"{uid}"' not in surfaces[surface], (surface, uid)
        assert "cam_1" not in surfaces[surface], surface
    # The cache does not hold the baby's uid either.
    assert BABY_MARKER not in surfaces["reprs"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
@pytest.mark.parametrize(
    "error",
    [
        NanitConnectionError("Calendar fetch failed: ClientError"),
        NanitAuthError("Access token invalid"),
    ],
    ids=["connection", "auth"],
)
async def test_privacy_markers_on_a_failed_fetch(
    hass: HomeAssistant,
    hass_client: Any,
    client: Any,
    caplog: pytest.LogCaptureFixture,
    notes: bool,
    error: Exception,
) -> None:
    """A failure carrying the markers in its chain lets none of them through."""
    caplog.set_level(logging.DEBUG)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry = await _set_up(hass, client, fake, notes=notes)
    calendar = _calendar(hass, entry)
    inner = RuntimeError(f"{BABY_MARKER} {NOTE_MARKER} {NAME_MARKER} {RELATION_MARKER}")
    error.__cause__ = inner
    fake.error = error

    with pytest.raises(HomeAssistantError) as raised:
        await _events(hass, calendar, RANGE_START, RANGE_END)

    for err in _chain(raised.value):
        assert all(marker not in str(err) for marker in (*PRIVATE_MARKERS, BABY_MARKER))
        assert not isinstance(err, (NanitConnectionError, NanitAuthError, RuntimeError))
    assert raised.value.translation_key == "care_log_calendar_unavailable"
    assert "Could not read the care log from Nanit" in str(raised.value)

    http = await hass_client()
    response = await http.get(
        f"/api/calendars/{calendar.entity_id}",
        params={"start": RANGE_START.isoformat(), "end": RANGE_END.isoformat()},
    )
    assert response.status == 500
    body = await response.text()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": calendar.entity_id,
                "start_date_time": RANGE_START.isoformat(),
                "end_date_time": RANGE_END.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
    # The reader's own debug lines name it by the baby's uid (as they did
    # before the calendar), so the uid is checked in the calendar's lines.
    calendar_lines = " ".join(
        record.getMessage() + (record.exc_text or "")
        for record in caplog.records
        if record.name == calendar_module.__name__
    )
    assert calendar_lines
    for marker in (*PRIVATE_MARKERS, BABY_MARKER):
        assert marker not in body
        assert marker not in calendar_lines
        assert marker not in repr(calendar._cache)
    for marker in PRIVATE_MARKERS:
        assert marker not in caplog.text
    for marker in PRIVATE_MARKERS:
        assert marker not in diagnostics
    assert calendar._cache == {}
    if not notes:
        client.async_get_calendar_details.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


async def _one_calendar(
    hass: HomeAssistant, client: Any, fake: FakeNanit, *, notes: bool = False
) -> tuple[MockConfigEntry, NanitCareLogCalendar]:
    entry = await _set_up(hass, client, fake, notes=notes)
    return entry, _calendar(hass, entry)


async def test_half_open_range_and_boundaries(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    start, end = RANGE_START, RANGE_END
    fake.add(_log("at_start", "diaper_change", start))
    fake.add(_log("at_end", "diaper_change", end))
    fake.add(_log("before", "diaper_change", start - timedelta(seconds=1)))
    # Ends exactly at the start: not in the range.
    fake.add(_log("ends_at_start", "nursing", start - timedelta(minutes=10), left_duration_s=600.0))
    # Runs into the range: in it.
    fake.add(_log("runs_in", "pumping", start - timedelta(minutes=10), duration_s=601.0))
    # Starts at the end: not in it.
    fake.add(_log("starts_at_end", "nursing", end, left_duration_s=600.0))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(hass, calendar, start, end)

    assert [(e.start, e.end) for e in events] == [
        (start - timedelta(minutes=10), start - timedelta(minutes=10) + timedelta(seconds=601)),
        (start, start + timedelta(minutes=1)),
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reaches_back_24_hours_in_whole_seconds(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    start = RANGE_START + timedelta(microseconds=250_000)
    end = RANGE_END + timedelta(microseconds=250_000)
    # Started 23 hours before the range and still running into it.
    fake.add(
        _log(
            "long",
            "nursing",
            RANGE_START - timedelta(hours=23),
            left_duration_s=12 * 3600.0,
            right_duration_s=11 * 3600.0 + 1800.0,
        )
    )
    # Started 25 hours before: outside the reach-back, never asked for.
    fake.add(_log("old", "pumping", RANGE_START - timedelta(hours=25), duration_s=23 * 3600.0))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(hass, calendar, start, end)

    assert _summaries(events) == ["Nursing, 1410 min"]
    assert fake.calendar_calls() == [
        (
            "result",
            int(_ts(RANGE_START - timedelta(hours=24))),
            int(_ts(RANGE_END)) + 1,
        )
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_repeats_are_removed_and_events_sorted_by_start_then_uid(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    at = _local("2026-11-20T12:00:00")
    fake.add(_log("b", "bottle_feed", at, feed_amount_ml=10.0))
    fake.add(_log("a", "diaper_change", at, change_type="pee"))
    fake.add(_log("c", "pumping", at - timedelta(hours=1)))
    fake.add(_log("a", "diaper_change", at, change_type="pee"))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(events) == ["Pumping", "Diaper, pee", "Bottle, 10 mL"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_long_range_is_fetched_in_pieces_two_at_a_time(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    start = RANGE_START - timedelta(days=299)
    entry, calendar = await _one_calendar(hass, client, fake)
    fetch_start = int(_ts(start - timedelta(hours=24)))
    step = int(CARE_LOG_MAX_PIECE.total_seconds())
    # An entry at a piece boundary comes back twice and is shown once.
    fake.add(_log("edge", "diaper_change", dt_util.utc_from_timestamp(fetch_start + step)))
    fake.gate = asyncio.Event()

    task = hass.async_create_task(_events(hass, calendar, start, RANGE_END))
    await asyncio.sleep(0)
    for _ in range(5):
        await asyncio.sleep(0)
    assert fake.in_flight == 2
    fake.gate.set()
    events = await task

    calls = fake.calendar_calls()
    assert len(calls) == 4
    assert fake.most_in_flight == 2
    bounds = sorted((s, e) for _m, s, e in calls)
    assert bounds[0][0] == fetch_start
    assert bounds[-1][1] == int(_ts(RANGE_END))
    for (_low, high), (next_low, _next_high) in pairwise(bounds):
        assert high == next_low
    assert all(high - low <= step for low, high in bounds)
    assert _summaries(events) == ["Diaper"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _until(condition: Any) -> None:
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never met")


async def test_a_30_year_range_adds_no_more_tasks_than_its_workers(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()
    start = RANGE_START - timedelta(days=30 * 365)
    before = len(asyncio.all_tasks())

    pending = hass.async_create_task(_events(hass, calendar, start, RANGE_END))
    await _until(lambda: fake.in_flight == 2)
    for _ in range(5):
        await asyncio.sleep(0)

    # The caller, the range's one fetch, and its two workers.
    assert len(asyncio.all_tasks()) - before <= 4
    fake.gate.set()
    assert await pending == []
    calls = fake.calendar_calls()
    assert len(calls) > 120
    assert fake.most_in_flight == 2
    bounds = sorted((s, e) for _m, s, e in calls)
    for (_low, high), (next_low, _next_high) in pairwise(bounds):
        assert high == next_low
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_sole_caller_giving_up_stops_the_fetch(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()

    pending = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    await _until(lambda: fake.in_flight == 1)
    (task,) = calendar._in_flight.values()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await _until(task.done)

    assert task.cancelled()
    assert fake.in_flight == 0
    assert calendar._in_flight == {}
    assert calendar._cache == {}
    # The next caller starts a fetch of its own and gets its answer.
    fake.gate.set()
    assert len(await _events(hass, calendar, RANGE_START, RANGE_END)) == 4
    assert len(fake.calendar_calls()) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_calendar_fetches_a_bounded_number_of_ranges_at_once(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()
    limit = CARE_LOG_CACHE_SIZE

    def day(index: int) -> tuple[datetime, datetime]:
        return RANGE_START - timedelta(days=index), RANGE_END - timedelta(days=index)

    pending = [hass.async_create_task(_events(hass, calendar, *day(i))) for i in range(limit)]
    await _until(lambda: len(calendar._in_flight) == limit)

    # Another range is refused at once; one already in flight is still shared.
    refused = hass.async_create_task(_events(hass, calendar, *day(limit)))
    await _until(refused.done)
    with pytest.raises(HomeAssistantError):
        refused.result()
    shared = hass.async_create_task(_events(hass, calendar, *day(0)))
    fake.gate.set()

    answers = await asyncio.gather(*pending)
    assert len(answers[0]) == 4
    assert len(await shared) == 4
    assert len(fake.calendar_calls()) == limit
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Kinds, text and units
# ---------------------------------------------------------------------------


async def _day(
    hass: HomeAssistant, client: Any, fake: FakeNanit, *, notes: bool = False
) -> list[Any]:
    entry, calendar = await _one_calendar(hass, client, fake, notes=notes)
    events = await _events(hass, calendar, RANGE_START, RANGE_END)
    assert await hass.config_entries.async_unload(entry.entry_id)
    return events


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (
            METRIC_SYSTEM,
            [
                ("Diaper, mixed", None, 60),
                ("Bottle, 30 mL", "Logged at 2026-11-20 08:20", 60),
                ("Nursing, 15 min", "Left side: 10 min\nRight side: 5 min", 900),
                (
                    "Pumping, 30 mL",
                    "Left side: 20 mL\nRight side: 10 mL\nDuration: 20 min",
                    1200,
                ),
            ],
        ),
        (
            US_CUSTOMARY_SYSTEM,
            [
                ("Diaper, mixed", None, 60),
                ("Bottle, 1.01 fl oz", "Logged at 2026-11-20 08:20", 60),
                ("Nursing, 15 min", "Left side: 10 min\nRight side: 5 min", 900),
                (
                    "Pumping, 1.01 fl oz",
                    "Left side: 0.68 fl oz\nRight side: 0.34 fl oz\nDuration: 20 min",
                    1200,
                ),
            ],
        ),
    ],
    ids=["metric", "us_customary"],
)
async def test_each_kind_title_description_and_length(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    units: Any,
    expected: list[tuple[str, str | None, int]],
) -> None:
    freezer.move_to(NOW)
    hass.config.units = units
    fake = FakeNanit()
    _day_of_entries(fake)

    events = await _day(hass, client, fake)

    assert [
        (e.summary, e.description, int((e.end - e.start).total_seconds())) for e in events
    ] == expected
    for event in events:
        assert event.start.tzinfo is not None
        assert event.end.tzinfo is not None
        assert event.uid is None
        assert event.location is None


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (
            US_CUSTOMARY_SYSTEM,
            [
                ("Pumping, 1.75 fl oz", "Left side: 0.75 fl oz\nRight side: 1 fl oz"),
                ("Pumping, 0.5 fl oz", "Left side: 0.25 fl oz\nRight side: 0.25 fl oz"),
                ("Bottle, 1.75 fl oz", None),
                ("Bottle, 0.25 fl oz", None),
            ],
        ),
        (
            METRIC_SYSTEM,
            [
                ("Pumping, 51.8 mL", "Left side: 22.2 mL\nRight side: 29.6 mL"),
                ("Pumping, 14.8 mL", "Left side: 7.4 mL\nRight side: 7.4 mL"),
                ("Bottle, 51.8 mL", None),
                ("Bottle, 7.4 mL", None),
            ],
        ),
    ],
    ids=["us_customary", "metric"],
)
async def test_quarter_ounces_keep_two_decimals_and_millilitres_one(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    units: Any,
    expected: list[tuple[str, str | None]],
) -> None:
    """Stored mL a hair under a quarter ounce still show as that quarter."""
    freezer.move_to(NOW)
    hass.config.units = units
    fake = FakeNanit()
    at = _local("2026-11-20T12:00:00")
    # 22.18 mL is 0.74999 fl oz, 7.39 mL 0.24989, 29.57 mL 0.99986, 51.75 mL 1.74987.
    fake.add(_log("p1", "pumping", at, left_amount_ml=22.18, right_amount_ml=29.57))
    fake.add(
        _log("p2", "pumping", at + timedelta(minutes=1), left_amount_ml=7.39, right_amount_ml=7.39)
    )
    fake.add(_log("b1", "bottle_feed", at + timedelta(minutes=2), feed_amount_ml=51.75))
    fake.add(_log("b2", "bottle_feed", at + timedelta(minutes=3), feed_amount_ml=7.39))

    events = await _day(hass, client, fake)

    assert [(e.summary, e.description) for e in events] == expected


def test_numbers_drop_trailing_zeros() -> None:
    assert [calendar_module._number(value) for value in (0.0, 1.0, 15.5, 22.18, 29.96, 7.04)] == [
        "0",
        "1",
        "15.5",
        "22.2",
        "30",
        "7",
    ]
    assert [
        calendar_module._number(value, 2) for value in (0.74999, 1.74987, 0.24989, 0.49977, 0.99986)
    ] == [
        "0.75",
        "1.75",
        "0.25",
        "0.5",
        "1",
    ]
    assert calendar_module._number(1.014, 2) == "1.01"
    assert calendar_module._number(0.104, 2) == "0.1"


async def test_notes_and_who_logged_with_the_option_on(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    fake.add(_log("d1", "diaper_change", _local("2026-11-20T07:00:00"), change_type="dry"))
    fake.add(
        _log("d2", "diaper_change", _local("2026-11-20T08:00:00")),
        note=None,
        name="Sam",
        relation=None,
    )
    fake.add(_log("d3", "diaper_change", _local("2026-11-20T09:00:00")), note=None, name=None)

    events = await _day(hass, client, fake, notes=True)

    assert [(e.summary, e.description) for e in events] == [
        (
            "Diaper, dry",
            f"Note: {NOTE_MARKER}\nLogged by {NAME_MARKER} ({RELATION_MARKER})",
        ),
        ("Diaper", "Logged by Sam"),
        ("Diaper", None),
    ]


async def test_absent_and_invalid_measurements_are_left_out(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    at = _local("2026-11-20T12:00:00")
    fake.add(_log("b1", "bottle_feed", at, invalid_fields=frozenset({"feed_amount_ml"})))
    fake.add(_log("b2", "bottle_feed", at + timedelta(minutes=1)))
    fake.add(
        _log(
            "n1",
            "nursing",
            at + timedelta(minutes=2),
            left_duration_s=None,
            right_duration_s=120.0,
            invalid_fields=frozenset({"left_duration_s"}),
        )
    )
    fake.add(_log("n2", "nursing", at + timedelta(minutes=3)))
    fake.add(
        _log(
            "p1",
            "pumping",
            at + timedelta(minutes=4),
            left_amount_ml=15.5,
            invalid_fields=frozenset({"right_amount_ml", "duration_s"}),
        )
    )
    fake.add(_log("p2", "pumping", at + timedelta(minutes=5)))
    fake.add(_log("d1", "diaper_change", at + timedelta(minutes=6), change_type="unknown"))

    events = await _day(hass, client, fake)

    assert [(e.summary, e.description, e.end - e.start) for e in events] == [
        ("Bottle", None, timedelta(minutes=1)),
        ("Bottle", None, timedelta(minutes=1)),
        ("Nursing, 2 min", "Right side: 2 min", timedelta(minutes=2)),
        ("Nursing", None, timedelta(minutes=1)),
        ("Pumping, 15.5 mL", "Left side: 15.5 mL", timedelta(minutes=1)),
        ("Pumping", None, timedelta(minutes=1)),
        ("Diaper", None, timedelta(minutes=1)),
    ]


async def test_measurements_whose_sum_overflows_leave_the_rest_of_the_range(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """Two finite sides that add up to infinity show no total, and nothing else is lost."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    at = _local("2026-11-20T12:00:00")
    fake.add(_log("p9", "pumping", at, left_amount_ml=1e308, right_amount_ml=1e308))
    fake.add(
        _log(
            "n9",
            "nursing",
            at + timedelta(minutes=1),
            left_duration_s=1e308,
            right_duration_s=1e308,
        )
    )

    events = await _day(hass, client, fake)

    assert _summaries(events) == [
        "Diaper, mixed",
        "Bottle, 30 mL",
        "Nursing, 15 min",
        "Pumping, 30 mL",
        "Pumping",
        "Nursing",
    ]
    for event in events[-2:]:
        assert event.end - event.start == timedelta(minutes=1)


async def test_an_unknown_kind_is_shown_generically_and_sleep_is_skipped(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    at = _local("2026-11-20T12:00:00")
    fake.add(_log("x1", "solid_food", at, duration_s=300.0))
    fake.add(_log("x2", "medicine", at + timedelta(minutes=10)))
    for index, kind in enumerate(("sleep", "auto_sleep", "semi_auto_sleep")):
        fake.add(
            _log(
                f"s{index}",
                kind,
                at + timedelta(minutes=20 + index),
                duration_s=3600.0,
                status="eligible",
            )
        )

    events = await _day(hass, client, fake)

    assert [(e.summary, e.description, e.end - e.start) for e in events] == [
        ("Solid food", "Duration: 5 min", timedelta(minutes=5)),
        ("Medicine", None, timedelta(minutes=1)),
    ]


async def test_an_unusable_length_falls_back_to_a_moment(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    at = _local("2026-11-20T12:00:00")
    fake.add(_log("p1", "pumping", at, duration_s=0.0))
    fake.add(_log("p2", "pumping", at + timedelta(minutes=1), duration_s=float("inf")))
    fake.add(_log("p3", "pumping", at + timedelta(minutes=2), duration_s=1e15))
    fake.add(_log("p4", "pumping", at + timedelta(minutes=3), duration_s=-5.0))

    events = await _day(hass, client, fake)

    assert len(events) == 4
    assert all(e.end - e.start == timedelta(minutes=1) for e in events)


async def test_clocks_going_back_fall_back_to_a_moment_and_keep_the_rest(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """1 November 2026: US/Pacific repeats 01:00 to 02:00."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    # 01:50 PDT, running 20 minutes to 01:10 PST: reversed on the wall clock.
    across = datetime.fromisoformat("2026-11-01T08:50:00+00:00")
    fake.add(_log("n1", "nursing", across, left_duration_s=1200.0))
    # Inside the second 01:00 hour: an ordinary interval.
    inside = datetime.fromisoformat("2026-11-01T09:15:00+00:00")
    fake.add(_log("n2", "nursing", inside, right_duration_s=600.0))
    fake.add(_log("d1", "diaper_change", across + timedelta(minutes=5)))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(
        hass, calendar, _local("2026-11-01T00:00:00"), _local("2026-11-02T00:00:00")
    )

    assert _summaries(events) == ["Nursing, 20 min", "Diaper", "Nursing, 10 min"]
    first, diaper, second = events
    # Shown as a moment at its start: one minute, like any other moment.
    assert first.start == dt_util.as_local(across)
    assert first.end - first.start == timedelta(minutes=1)
    assert diaper.end - diaper.start == timedelta(minutes=1)
    assert second.end - second.start == timedelta(minutes=10)
    assert await hass.config_entries.async_unload(entry.entry_id)


def _local_day() -> tuple[datetime, datetime]:
    """20 November 2026 in Home Assistant's time zone, read when the test runs."""
    return _local("2026-11-20T00:00:00"), _local("2026-11-21T00:00:00")


async def _moments(
    hass: HomeAssistant,
    client: Any,
    times: list[datetime],
    start: datetime,
    end: datetime,
    kinds: tuple[str, ...] = ("diaper_change", "bottle_feed", "medicine"),
) -> list[Any]:
    """The events of one moment of each kind at each time, in [start, end)."""
    fake = FakeNanit()
    for index, at in enumerate(times):
        for kind in kinds:
            fake.add(_log(f"{kind}_{index}", kind, at))
    entry, calendar = await _one_calendar(hass, client, fake)
    events = await _events(hass, calendar, start, end)
    assert await hass.config_entries.async_unload(entry.entry_id)
    return events


def _spans(events: list[Any]) -> set[tuple[datetime, datetime]]:
    return {(dt_util.as_utc(e.start), dt_util.as_utc(e.end)) for e in events}


async def test_a_moment_lasts_one_minute(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """A diaper change, a bottle and any other kind without a length alike."""
    freezer.move_to(NOW)
    at = _local("2026-11-20T14:00:00")

    events = await _moments(hass, client, [at], *_local_day())

    assert len(events) == 3
    assert {(e.start, e.end) for e in events} == {(at, _local("2026-11-20T14:01:00"))}


@pytest.mark.parametrize(
    ("moment", "length"),
    [("2026-11-20T23:59:30", timedelta(seconds=30)), ("2026-11-20T23:59:59", timedelta(seconds=1))],
)
async def test_a_moment_just_before_midnight_ends_at_midnight(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    moment: str,
    length: timedelta,
) -> None:
    freezer.move_to(NOW)
    at = _local(moment)
    start, end = _local_day()

    events = await _moments(hass, client, [at], start, end)

    assert len(events) == 3
    assert {(e.start, e.end) for e in events} == {(at, end)}
    assert all(e.end - e.start == length for e in events)


@pytest.mark.parametrize(
    ("day", "next_day"),
    [("2026-03-08", "2026-03-09"), ("2026-11-01", "2026-11-02")],
    ids=["clocks_forward", "clocks_back"],
)
async def test_moments_on_a_day_the_clocks_change(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, day: str, next_day: str
) -> None:
    """US/Pacific: 8 March has 23 hours, 1 November 25."""
    freezer.move_to(NOW)
    start, end = _local(f"{day}T00:00:00"), _local(f"{next_day}T00:00:00")
    before_change = _local(f"{day}T00:59:30")
    late = _local(f"{day}T23:59:30")

    events = await _moments(hass, client, [before_change, late], start, end, kinds=("bottle_feed",))

    utc = dt_util.as_utc
    assert _spans(events) == {
        (utc(before_change), utc(before_change) + timedelta(minutes=1)),
        (utc(late), utc(end)),
    }
    assert utc(end) - utc(late) == timedelta(seconds=30)


async def test_moments_in_a_time_zone_offset_by_30_minutes(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """Asia/Kolkata (UTC+05:30): midnight there falls at half past a UTC hour."""
    await hass.config.async_set_time_zone("Asia/Kolkata")
    freezer.move_to(NOW)
    start, end = _local("2026-11-20T00:00:00"), _local("2026-11-21T00:00:00")
    assert dt_util.as_utc(end) == datetime.fromisoformat("2026-11-20T18:30:00+00:00")
    afternoon = _local("2026-11-20T14:00:00")
    late = _local("2026-11-20T23:59:30")

    events = await _moments(hass, client, [afternoon, late], start, end, kinds=("diaper_change",))

    assert {(e.start, e.end) for e in events} == {
        (afternoon, afternoon + timedelta(minutes=1)),
        (late, end),
    }
    assert all(e.start.utcoffset() == timedelta(hours=5, minutes=30) for e in events)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


# Each zone's clock changes in 2026 (UTC instants), and an ordinary day.
LOS_ANGELES = "America/Los_Angeles"
LORD_HOWE = "Australia/Lord_Howe"  # +10:30, and +11:00 in summer: a 30-minute change
CLOCK_DAYS = {
    # zone, local day: the instant the clocks change that day, or None
    "la_forward": (LOS_ANGELES, "2026-03-08", _utc("2026-03-08T10:00:00+00:00")),
    "la_back": (LOS_ANGELES, "2026-11-01", _utc("2026-11-01T09:00:00+00:00")),
    "la_ordinary": (LOS_ANGELES, "2026-11-20", None),
    "lord_howe_back": (LORD_HOWE, "2026-04-05", _utc("2026-04-04T15:00:00+00:00")),
    "lord_howe_forward": (LORD_HOWE, "2026-10-04", _utc("2026-10-03T15:30:00+00:00")),
    "lord_howe_ordinary": (LORD_HOWE, "2026-11-20", None),
}


@pytest.mark.parametrize(
    ("zone", "range_start"),
    [
        # The brief's example: 04:00 PDT, after the clocks went forward.
        (LOS_ANGELES, _utc("2026-03-08T11:00:00+00:00")),
        # 04:00 PST, after they went back.
        (LOS_ANGELES, _utc("2026-11-01T12:00:00+00:00")),
        # Midnight on the day after each change, and an ordinary midnight.
        (LOS_ANGELES, _utc("2026-03-09T07:00:00+00:00")),
        (LOS_ANGELES, _utc("2026-11-02T08:00:00+00:00")),
        (LOS_ANGELES, _utc("2026-11-20T08:00:00+00:00")),
        # 04:00 on each Lord Howe change day, and an ordinary midnight.
        (LORD_HOWE, _utc("2026-04-04T17:30:00+00:00")),
        (LORD_HOWE, _utc("2026-10-03T17:00:00+00:00")),
        (LORD_HOWE, _utc("2026-11-19T13:00:00+00:00")),
    ],
)
async def test_the_reach_back_is_24_elapsed_hours_whatever_the_clocks_do(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    zone: str,
    range_start: datetime,
) -> None:
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(NOW)
    fake = FakeNanit()
    entry, calendar = await _one_calendar(hass, client, fake)
    start = dt_util.as_local(range_start)
    end = dt_util.as_local(range_start + timedelta(days=1))

    await _events(hass, calendar, start, end)

    ((_method, fetch_start, fetch_end),) = fake.calendar_calls()
    assert int(_ts(range_start)) - fetch_start == 24 * 3600
    assert fetch_end == int(_ts(range_start + timedelta(days=1)))
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("day", list(CLOCK_DAYS), ids=list(CLOCK_DAYS))
async def test_every_moment_lasts_up_to_a_minute_and_stays_on_its_day(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, day: str
) -> None:
    """Moments around midnight and the clock changes, checked on UTC instants."""
    zone, local_day, change = CLOCK_DAYS[day]
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(NOW)
    day_start = dt_util.start_of_local_day(datetime.fromisoformat(local_day).date())
    next_midnight = dt_util.as_utc(dt_util.start_of_local_day(day_start.date() + timedelta(days=1)))
    noon = dt_util.as_utc(day_start) + timedelta(hours=12)
    around = change or noon
    seconds = (-60, -59, -30, -1, 0, 1, 30)
    times = sorted(
        {dt_util.as_utc(day_start) + timedelta(seconds=s) for s in (0, 1, 30)}
        | {around + timedelta(seconds=s) for s in seconds}
        | {next_midnight + timedelta(seconds=s) for s in (-60, -59, -30, -1)}
    )

    events = await _moments(
        hass, client, times, day_start, dt_util.as_local(next_midnight), kinds=("diaper_change",)
    )

    assert sorted(dt_util.as_utc(e.start) for e in events) == times
    for event in events:
        start, end = dt_util.as_utc(event.start), dt_util.as_utc(event.end)
        assert timedelta(0) < end - start <= timedelta(minutes=1), (start, end)
        assert end <= next_midnight, (start, end)
        # Shown in local time, with the offset in force at each instant.
        assert event.start.utcoffset() == dt_util.as_local(start).utcoffset()
        assert event.end.utcoffset() == dt_util.as_local(end).utcoffset()
    if change is not None:
        # The last minute before the clocks go back ends just before they do.
        last = next(e for e in events if dt_util.as_utc(e.start) == change - timedelta(seconds=30))
        offset_before = dt_util.as_local(change - timedelta(seconds=1)).utcoffset()
        offset_after = dt_util.as_local(change).utcoffset()
        assert offset_before is not None and offset_after is not None
        if offset_after < offset_before:
            assert dt_util.as_utc(last.end) == change - timedelta(microseconds=1)
        else:
            assert dt_util.as_utc(last.end) == change + timedelta(seconds=30)


async def test_a_range_takes_a_moment_by_its_start_as_before(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """The nominal minute changes no range: at the start is in, at the end is out."""
    freezer.move_to(NOW)
    start, end = _local("2026-11-20T12:00:00"), _local("2026-11-20T13:00:00")
    fake = FakeNanit()
    fake.add(_log("at_start", "diaper_change", start))
    # Its minute runs into the range, but it happened before it.
    fake.add(_log("just_before", "diaper_change", start - timedelta(seconds=30)))
    # Its minute runs past the end, but it happened inside.
    fake.add(_log("just_inside", "bottle_feed", end - timedelta(seconds=30)))
    fake.add(_log("at_end", "diaper_change", end))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(hass, calendar, start, end)

    assert [(e.start, e.end) for e in events] == [
        (start, start + timedelta(minutes=1)),
        (end - timedelta(seconds=30), end + timedelta(seconds=30)),
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_event_the_validator_rejects_outright_is_skipped(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    real = calendar_module.CalendarEvent

    def picky(**kwargs: Any) -> Any:
        if kwargs["summary"].startswith("Bottle"):
            raise HomeAssistantError("Failed to validate CalendarEvent")
        return real(**kwargs)

    with patch.object(calendar_module, "CalendarEvent", side_effect=picky):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(events) == ["Diaper, mixed", "Nursing, 15 min", "Pumping, 30 mL"]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_the_english_fallback_matches_strings_json() -> None:
    strings = json.loads(
        (Path(calendar_module.__file__).parent / "strings.json").read_text(encoding="utf-8")
    )
    attributes = strings["entity"]["calendar"]["care_log"]["state_attributes"]
    # The statistics' names share the block; care_log_statistics checks those.
    calendar_groups = {g: v["state"] for g, v in attributes.items() if g != "statistic_name"}
    assert calendar_groups == FALLBACK_TEXT


async def test_text_falls_back_to_english_when_translations_fail(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)

    with patch.object(
        calendar_module, "async_get_translations", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(events) == [
        "Diaper, mixed",
        "Bottle, 30 mL",
        "Nursing, 15 min",
        "Pumping, 30 mL",
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_text_comes_from_the_translation_files(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    prefix = "component.nanit.entity.calendar.care_log.state_attributes"
    translated = {
        f"{prefix}.event_title.state.bottle_feed_amount": "Biberon, {amount}",
        f"{prefix}.measurement.state.milliliters": "{value} ml",
        # A broken translation (unknown placeholder) falls back to English.
        f"{prefix}.event_title.state.nursing_duration": "Stillen, {dauer}",
    }

    with patch.object(
        calendar_module, "async_get_translations", AsyncMock(return_value=translated)
    ):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(events) == [
        "Diaper, mixed",
        "Biberon, 30 ml",
        "Nursing, 15 min",
        "Pumping, 30 ml",
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


async def test_simultaneous_requests_share_one_fetch(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()

    first = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    second = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    fake.gate.set()

    assert _summaries(await first) == _summaries(await second)
    assert len(fake.calendar_calls()) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_caller_giving_up_does_not_stop_the_shared_fetch(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()

    first = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    second = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    fake.gate.set()

    assert len(await second) == 4
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(fake.calendar_calls()) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cached_answers_expire_after_five_minutes(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)

    await _events(hass, calendar, RANGE_START, RANGE_END)
    freezer.tick(CARE_LOG_CACHE_TTL - timedelta(seconds=1))
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert len(fake.calendar_calls()) == 1

    freezer.tick(timedelta(seconds=1))
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert len(fake.calendar_calls()) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
async def test_an_idle_calendar_drops_its_answers_when_they_expire(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, notes: bool
) -> None:
    """With no further request, nothing fetched stays in memory past five minutes."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=notes)

    await _events(hass, calendar, RANGE_START - timedelta(days=1), RANGE_START)
    freezer.tick(timedelta(minutes=2))
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert len(calendar._cache) == 2

    # The first answer goes at its five minutes, the second stays until its own.
    freezer.tick(CARE_LOG_CACHE_TTL - timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calendar._cache) == 1
    (rows,) = (cached.rows for cached in calendar._cache.values())
    assert rows

    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calendar._cache == {}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_expiry_timer_is_cancelled_on_unload(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """Unloaded with an answer still fresh: no timer is left behind (the harness checks)."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert calendar._cache

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert calendar._cache == {}
    freezer.tick(CARE_LOG_CACHE_TTL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calendar._cache == {}


async def test_text_is_formatted_after_the_cache(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """A change of units needs no new fetch."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)

    metric = await _events(hass, calendar, RANGE_START, RANGE_END)
    hass.config.units = US_CUSTOMARY_SYSTEM
    customary = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(metric)[1] == "Bottle, 30 mL"
    assert _summaries(customary)[1] == "Bottle, 1.01 fl oz"
    assert len(fake.calendar_calls()) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failure_is_not_cached(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.error = NanitConnectionError("Calendar fetch failed: TimeoutError")

    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    assert calendar._cache == {}
    fake.error = None

    assert len(await _events(hass, calendar, RANGE_START, RANGE_END)) == 4
    assert len(fake.calendar_calls()) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_notes_on_and_off_are_cached_apart(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)

    with patch.object(calendar_module, "is_show_care_log_notes", return_value=True):
        with_notes = await _events(hass, calendar, RANGE_START, RANGE_END)
    without = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert NOTE_MARKER in (with_notes[0].description or "")
    assert all(NOTE_MARKER not in (e.description or "") for e in without)
    assert [m for m, _s, _e in fake.calendar_calls()] == ["details", "result"]
    assert sorted(key[2] for key in calendar._cache) == [False, True]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_answer_is_not_delivered_when_the_notes_option_changed_meanwhile(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    answers = iter([True, False])

    with (
        patch.object(
            calendar_module, "is_show_care_log_notes", side_effect=lambda _e: next(answers)
        ),
        pytest.raises(HomeAssistantError),
    ):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("change", ["option_off", "removed"])
async def test_notes_are_not_returned_when_consent_ends_during_the_text_lookup(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, change: str
) -> None:
    """The last await before the answer is the translation lookup: the check comes after it."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    looking_up = asyncio.Event()
    release = asyncio.Event()

    async def slow_translations(*_args: Any) -> dict[str, str]:
        looking_up.set()
        await release.wait()
        return {}

    with patch.object(calendar_module, "async_get_translations", side_effect=slow_translations):
        pending = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
        await looking_up.wait()
        if change == "option_off":
            # Only the option changes: the reload it would start is held back,
            # so the calendar is still there when the answer is built.
            with patch.object(entry, "update_listeners", []):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_SHOW_CARE_LOG_NOTES: False}
                )
            assert not calendar._removed
        else:
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert calendar._removed
        release.set()
        with pytest.raises(HomeAssistantError) as raised:
            await pending

    assert raised.value.translation_key == "care_log_calendar_unavailable"
    for marker in PRIVATE_MARKERS:
        assert marker not in str(raised.value)
    if change == "option_off":
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_cache_is_bounded(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    entry, calendar = await _one_calendar(hass, client, fake)

    for day in range(CARE_LOG_CACHE_SIZE + 3):
        freezer.tick(timedelta(seconds=1))
        start = RANGE_START - timedelta(days=day)
        await _events(hass, calendar, start, start + timedelta(days=1))

    assert len(calendar._cache) == CARE_LOG_CACHE_SIZE
    fetch_starts = {key[0] for key in calendar._cache}
    newest = RANGE_START - timedelta(days=CARE_LOG_CACHE_SIZE + 2)
    oldest = RANGE_START
    assert int(_ts(newest - timedelta(hours=24))) in fetch_starts
    assert int(_ts(oldest - timedelta(hours=24))) not in fetch_starts
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_answer_arriving_after_removal_is_discarded(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()
    pending = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    (task,) = calendar._in_flight.values()

    assert await hass.config_entries.async_unload(entry.entry_id)
    fake.gate.set()

    with pytest.raises(HomeAssistantError):
        await pending
    assert task.cancelled()
    assert calendar._cache == {}
    assert calendar._in_flight == {}


async def test_a_result_landing_after_removal_is_not_kept(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """Even a fetch that finishes as the calendar goes is not kept."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    key = (0, 10, False)
    calendar._removed = True

    rows = await calendar._async_fetch_and_keep(key)

    assert rows == ()
    assert calendar._cache == {}
    calendar._removed = False
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# The entity
# ---------------------------------------------------------------------------


async def test_calendar_entity_basics(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)

    registry_entry = er.async_get(hass).async_get(calendar.entity_id)
    assert registry_entry is not None
    assert (
        registry_entry.unique_id == f"{entry.entry_id}_{baby_code(BABY_MARKER)}_care_log_calendar"
    )
    assert BABY_MARKER not in registry_entry.unique_id
    assert registry_entry.disabled_by is None
    assert calendar.entity_id == "calendar.nursery_care_log"
    state = hass.states.get(calendar.entity_id)
    assert state.state == STATE_OFF
    assert set(state.attributes) <= {"friendly_name", "icon", "supported_features"}
    assert calendar.supported_features in (None, 0)
    assert calendar.event is None
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    assert device is not None
    assert (DOMAIN, "cam_1") in device.identifiers
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_baby_without_a_camera_gets_a_calendar_of_its_own(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    no_camera = Baby(uid="baby_4", name="Spare room", camera_uid="")
    fake = FakeNanit()
    fake.add(_log("d4", "diaper_change", _local("2026-11-20T07:00:00"), baby_uid="baby_4"))
    entry = await _set_up(hass, client, fake, babies=[BABY, no_camera])

    calendars = {c.coordinator.baby.uid: c for c in _calendars(hass, entry)}
    assert set(calendars) == {BABY_MARKER, "baby_4"}
    spare = calendars["baby_4"]
    assert spare.entity_id == "calendar.spare_room_care_log"
    assert er.async_get(hass).async_get(spare.entity_id).device_id is None
    assert hass.states.get(spare.entity_id).state == STATE_OFF
    assert _summaries(await _events(hass, spare, RANGE_START, RANGE_END)) == ["Diaper"]
    assert fake.calendar_calls()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_sign_ins_seeing_the_same_baby_get_two_calendars(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    first = await _set_up(hass, client, fake)
    second = await _set_up(hass, client, fake, entry=_entry(hass, email="other@example.com"))

    one, two = _calendar(hass, first), _calendar(hass, second)
    assert one.unique_id != two.unique_id
    assert one.entity_id != two.entity_id
    assert len(await _events(hass, one, RANGE_START, RANGE_END)) == 4
    assert len(await _events(hass, two, RANGE_START, RANGE_END)) == 4
    # Each has its own cache, so each fetched.
    assert len(fake.calendar_calls()) == 2
    assert await hass.config_entries.async_unload(first.entry_id)
    assert await hass.config_entries.async_unload(second.entry_id)


async def test_unavailable_until_the_reader_succeeds_and_written_on_each_poll(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    fake.error = NanitConnectionError("Calendar fetch failed: TimeoutError")
    entry, calendar = await _one_calendar(hass, client, fake)
    assert hass.states.get(calendar.entity_id).state == STATE_UNAVAILABLE

    fake.error = None
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(calendar.entity_id)
    assert state.state == STATE_OFF
    assert state.last_reported >= NOW + timedelta(minutes=5)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_removes_the_calendar_and_stops_its_reader(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert calendar._cache

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert calendar._cache == {}
    assert hass.states.get(calendar.entity_id).state == STATE_UNAVAILABLE
    assert _calendars(hass, entry) == []


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


async def test_read_only_mode_keeps_a_filled_calendar(
    hass: HomeAssistant,
    nanit_env: Any,  # noqa: F811
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switched on and off, the calendar stays registered, enabled and readable."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    _wire(nanit_env, fake, [E2E_BABY])
    entry = _entry(hass)
    registry = er.async_get(hass)

    with patch("custom_components.nanit.hub.NanitCareLogCoordinator", NanitCareLogCoordinator):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = _calendar(hass, entry).entity_id
        for read_only in (True, False):
            hass.config_entries.async_update_entry(entry, options={CONF_READ_ONLY: read_only})
            await hass.async_block_till_done()

            assert entry.state is ConfigEntryState.LOADED
            registry_entry = registry.async_get(entity_id)
            assert registry_entry is not None, read_only
            assert registry_entry.disabled_by is None
            calendar = _calendar(hass, entry)
            assert calendar.entity_id == entity_id
            assert hass.states.get(entity_id).state == STATE_OFF
            assert len(await _events(hass, calendar, RANGE_START, RANGE_END)) == 4
    assert await hass.config_entries.async_unload(entry.entry_id)
