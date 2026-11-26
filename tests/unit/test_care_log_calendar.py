"""The care log calendar: privacy, ranges, text, cache and life cycle."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import json
import logging
import sys
import weakref
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
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

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitRateLimited,
    NanitRequestRefused,
)
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
from custom_components.nanit import coordinator as coordinator_module
from custom_components.nanit.baby_code import baby_code
from custom_components.nanit.calendar import (
    CARE_LOG_CACHE_SIZE,
    CARE_LOG_CACHE_TTL,
    CARE_LOG_MAX_PIECE,
    NanitCareLogCalendar,
)
from custom_components.nanit.const import (
    CONF_READ_ONLY,
    CONF_SHOW_CARE_LOG_NOTES,
    DOMAIN,
    is_show_care_log_notes,
)
from custom_components.nanit.coordinator import NanitCareLogCoordinator, care_log_hold
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
    # Rows Nanit wrongly returns with every answer whose range they lie
    # outside, after the rows that belong in it or, if strays_first, before.
    strays: list[CareLogEntry] = field(default_factory=list)
    strays_first: bool = False

    def add(
        self,
        log: CareLog,
        note: str | None = NOTE_MARKER,
        name: str | None = NAME_MARKER,
        relation: str | None = RELATION_MARKER,
    ) -> None:
        detail = CareLogDetail(comment=note, user_name=name, user_relation=relation)
        self.entries.append(CareLogEntry(log=log, detail=detail))

    def add_stray(self, log: CareLog) -> None:
        """Return log, wrongly, with every answer whose range it lies outside."""
        detail = CareLogDetail(
            comment=NOTE_MARKER, user_name=NAME_MARKER, user_relation=RELATION_MARKER
        )
        self.strays.append(CareLogEntry(log=log, detail=detail))

    async def _answer(self, method: str, baby_uid: str, start: int, end: int) -> list[CareLogEntry]:
        self.calls.append((method, baby_uid, start, end))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            # Only the asked baby's entries, as Nanit answers for one baby.
            inside = [
                e
                for e in self.entries
                if e.log.baby_uid == baby_uid and start <= e.log.timestamp <= end
            ]
            strays = [e for e in self.strays if not start <= e.log.timestamp <= end]
            return strays + inside if self.strays_first else inside + strays
        finally:
            self.in_flight -= 1

    async def result(self, baby_uid: str, start: int, end: int) -> CareLogFetch:
        entries = await self._answer("result", baby_uid, start, end)
        return CareLogFetch(logs=tuple(e.log for e in entries), start=start, end=end, dropped=0)

    async def details(self, baby_uid: str, start: int, end: int) -> CareLogDetailFetch:
        entries = await self._answer("details", baby_uid, start, end)
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


def _day_of_entries(fake: FakeNanit, baby_uid: str = BABY_MARKER) -> None:
    """One of each kind on 20 November, with notes, names and relations."""
    fake.add(
        _log(
            "d1",
            "diaper_change",
            _local("2026-11-20T07:00:00"),
            baby_uid=baby_uid,
            change_type="mixed",
        )
    )
    fake.add(
        _log(
            "b1",
            "bottle_feed",
            _local("2026-11-20T08:00:00"),
            baby_uid=baby_uid,
            feed_amount_ml=30.0,
            logged_at=_ts(_local("2026-11-20T08:20:00")),
        )
    )
    fake.add(
        _log(
            "n1",
            "nursing",
            _local("2026-11-20T09:00:00"),
            baby_uid=baby_uid,
            left_duration_s=600.0,
            right_duration_s=300.0,
        )
    )
    fake.add(
        _log(
            "p1",
            "pumping",
            _local("2026-11-20T10:00:00"),
            baby_uid=baby_uid,
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
    entry = await _set_up(hass, client, fake, notes=notes)
    calendar = _calendar(hass, entry)

    surfaces = await _surfaces(hass, hass_client, entry, calendar)
    # No registry entry of the sign-in (care sensors and calendar among them)
    # holds the baby's uid in its unique id, entity id or name.
    registered = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert any(reg.unique_id.endswith("_last_diaper_change") for reg in registered)
    for reg in registered:
        for text in (reg.unique_id, reg.entity_id, reg.name or "", reg.original_name or ""):
            assert BABY_MARKER not in text, text
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
# Refusals and throttling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
@pytest.mark.parametrize(
    "error",
    [NanitRequestRefused("Calendar fetch", 403), NanitRateLimited("Calendar fetch", 600)],
    ids=["refused", "throttled"],
)
async def test_a_refused_calendar_fetch_asks_nothing_more_until_the_hold_ends(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    notes: bool,
    error: NanitRequestRefused,
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=notes)
    fake.error = error

    # A year: five pieces, of which at most the two first ones are asked for.
    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_END - timedelta(days=365), RANGE_END)
    assert 1 <= len(fake.calendar_calls()) <= 2

    # Asked again, for this range or another, while Nanit refuses: no request.
    asked = len(fake.calls)
    fake.error = None
    for start, end in ((RANGE_END - timedelta(days=365), RANGE_END), (RANGE_START, RANGE_END)):
        with pytest.raises(HomeAssistantError):
            await _events(hass, calendar, start, end)
    assert len(fake.calls) == asked

    # Nor does a refresh of the reader, until the hold is over.
    await calendar.coordinator.async_refresh()
    assert not calendar.coordinator.last_update_success
    assert len(fake.calls) == asked
    freezer.tick(care_log_hold(error))

    # Then the reader reads, and the calendar asks again.
    await calendar.coordinator.async_refresh()
    assert calendar.coordinator.last_update_success
    fake.forget_calls()
    events = await _events(hass, calendar, RANGE_START, RANGE_END)
    assert len(events) == 4
    assert len(fake.calendar_calls()) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reader_refusal_holds_the_calendar_back(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.error = NanitRequestRefused("Calendar fetch", 403)
    await calendar.coordinator.async_refresh()
    assert not calendar.coordinator.last_update_success
    # The reader asked once, without retrying.
    assert len(fake.calls) == 1
    fake.forget_calls()

    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    assert fake.calls == []
    assert await hass.config_entries.async_unload(entry.entry_id)


def _other_caller(
    client: Any, reader: NanitCareLogCoordinator
) -> Callable[[int, int], Awaitable[CareLogFetch]]:
    """Return a fetch of the baby's care logs that goes through the reader's hold.

    It stands for any other caller of ``async_request``, as each calendar
    piece is, so a test can have it refused directly.
    """

    async def fetch(start: int, end: int) -> CareLogFetch:
        return await reader.async_request(
            lambda: client.async_get_calendar_result(reader.baby.uid, start, end)
        )

    return fetch


@pytest.mark.parametrize("throttled", [False, True], ids=["refused", "throttled"])
async def test_a_hold_keeps_its_status_and_not_the_error_it_got(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, throttled: bool
) -> None:
    """The error itself, and the traceback it carries, is let go; the hold is not."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    other_fetch = _other_caller(client, calendar.coordinator)
    day = (int(_ts(RANGE_START)), int(_ts(RANGE_END)))
    fake.error = (
        NanitRateLimited("Calendar fetch", 600)
        if throttled
        else NanitRequestRefused("Calendar fetch", 403)
    )
    got = weakref.ref(fake.error)
    with pytest.raises(NanitRequestRefused):
        await other_fetch(*day)
    fake.error = None
    gc.collect()
    assert got() is None

    # Still held, with the same status and wait, asking Nanit nothing.
    with pytest.raises(NanitRequestRefused) as held:
        await other_fetch(*day)
    assert len(fake.calls) == 1
    assert held.value.status == (429 if throttled else 403)
    assert isinstance(held.value, NanitRateLimited) is throttled
    if isinstance(held.value, NanitRateLimited):
        assert held.value.retry_after == 600
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_refusal_shows_on_the_care_sensors_at_once_and_is_told_once(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The calendar is refused: the care sensors say so before the reader polls again.

    Their listeners are told once, then not again by the reader's held poll;
    a refusal of the reader's own poll is told once too.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    reader = calendar.coordinator
    assert STATE_UNAVAILABLE not in _care_sensor_states(hass, entry).values()
    told: list[bool] = []
    unsub = reader.async_add_listener(lambda: told.append(reader.last_update_success))
    caplog.clear()

    fake.error = NanitRequestRefused("Calendar fetch", 403)
    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    await hass.async_block_till_done()
    assert told == [False]
    assert set(_care_sensor_states(hass, entry).values()) == {STATE_UNAVAILABLE}
    await reader.async_refresh()
    assert told == [False]
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert BABY_MARKER not in caplog.text

    # After the hold, the reader reads; then its own poll is refused.
    fake.error = None
    freezer.tick(care_log_hold(NanitRequestRefused("Calendar fetch", 403)))
    await reader.async_refresh()
    assert told == [False, True]
    fake.error = NanitRequestRefused("Calendar fetch", 403)
    await reader.async_refresh()
    assert told == [False, True, False]
    unsub()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "error",
    [NanitRequestRefused("Calendar fetch", 403), NanitRateLimited("Calendar fetch", 7200)],
    ids=["refused", "throttled_two_hours"],
)
async def test_a_hold_stops_every_caller_until_it_ends(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    error: NanitRequestRefused,
) -> None:
    """Another caller is refused; nobody asks Nanit until the hold is over.

    Not the calendar, a refresh of the reader, its scheduled polls (hourly at
    most, even through a two-hour wait) or that caller itself.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    reader = calendar.coordinator
    other_fetch = _other_caller(client, reader)
    day = (int(_ts(RANGE_START)), int(_ts(RANGE_END)))
    fake.error = error
    with pytest.raises(NanitRequestRefused):
        await other_fetch(*day)
    assert len(fake.calls) == 1
    fake.error = None

    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    await reader.async_refresh()
    assert not reader.last_update_success
    with pytest.raises(NanitRequestRefused):
        await other_fetch(*day)
    assert len(fake.calls) == 1

    hold = care_log_hold(error)
    waited = timedelta()
    while waited + reader.update_interval < hold:
        assert reader.update_interval <= timedelta(hours=1)
        waited += reader.update_interval
        freezer.tick(reader.update_interval)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert len(fake.calls) == 1
    assert not reader.last_update_success

    # The first poll after the hold asks Nanit; then everybody may again.
    freezer.tick(hold - waited)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(fake.calls) == 2
    assert reader.last_update_success
    assert len(await _events(hass, calendar, RANGE_START, RANGE_END)) == 4
    await other_fetch(*day)
    assert len(fake.calls) == 4
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_poll_retry_waiting_when_a_refusal_comes_asks_nothing(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """The reader's poll failed once and waits to retry; another caller is refused meanwhile."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    reader = calendar.coordinator
    other_fetch = _other_caller(client, reader)
    waiting = asyncio.Event()
    go_on = asyncio.Event()

    async def retry_wait(_delay: float) -> None:
        waiting.set()
        await go_on.wait()

    fake.error = NanitConnectionError("Calendar fetch failed with HTTP 503")
    with patch.object(coordinator_module.asyncio, "sleep", retry_wait):
        poll = hass.async_create_task(reader.async_refresh())
        await waiting.wait()
        assert len(fake.calls) == 1
        fake.error = NanitRequestRefused("Calendar fetch", 403)
        with pytest.raises(NanitRequestRefused):
            await other_fetch(int(_ts(RANGE_START)), int(_ts(RANGE_END)))
        fake.error = None
        go_on.set()
        await poll

    # The poll's retries asked nothing.
    assert len(fake.calls) == 2
    assert not reader.last_update_success
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_piece_waiting_for_its_turn_asks_nothing_after_a_refusal(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """Two ranges at once: one's pieces hold both turns; a refusal comes for one of them."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()

    year = hass.async_create_task(
        _events(hass, calendar, RANGE_END - timedelta(days=365), RANGE_END)
    )
    await _until(lambda: fake.in_flight == 2)
    day = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    for _ in range(5):
        await asyncio.sleep(0)
    # The day's only piece waits for a turn.
    assert len(fake.calendar_calls()) == 2
    fake.error = NanitRequestRefused("Calendar fetch", 403)
    fake.gate.set()

    for task in (year, day):
        with pytest.raises(HomeAssistantError):
            await task
    assert len(fake.calendar_calls()) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


def _care_sensor_states(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    registry = er.async_get(hass)
    return {
        reg.entity_id: hass.states.get(reg.entity_id).state
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "sensor"
        and reg.unique_id.endswith(
            ("_last_feeding", "_feedings_today", "_bottle_volume_today", "_last_diaper_change")
        )
    }


async def test_an_older_request_succeeding_late_leaves_a_newer_refusal_in_place(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """The reader's poll is on its way when the calendar is refused; the poll then succeeds.

    Its answer, with a feeding never shown before, is dropped: the care
    sensors report unavailable and show nothing of it.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    reader = calendar.coordinator
    before = _care_sensor_states(hass, entry)
    assert len(before) == 4
    assert STATE_UNAVAILABLE not in before.values()
    poll_may_answer = asyncio.Event()
    answer_poll = fake.result

    async def slow_result(baby_uid: str, start: int, end: int) -> CareLogFetch:
        await poll_may_answer.wait()
        return await answer_poll(baby_uid, start, end)

    client.async_get_calendar_result.side_effect = slow_result
    client.async_get_calendar_details.side_effect = NanitRequestRefused("Calendar fetch", 403)
    poll = hass.async_create_task(reader.async_refresh())
    await asyncio.sleep(0)
    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    # Nanit's answer to the poll holds a feeding no sensor has shown.
    fake.add(_log("b_new", "bottle_feed", NOW - timedelta(minutes=5), feed_amount_ml=95.0))
    poll_may_answer.set()
    await poll
    await hass.async_block_till_done()
    after = _care_sensor_states(hass, entry)
    assert set(after.values()) == {STATE_UNAVAILABLE}, after
    assert "b_new" not in repr(reader.data)
    assert not reader.last_update_success

    # The refusal still holds: the calendar asks nothing, and the reader's
    # next poll is held too.
    client.async_get_calendar_details.side_effect = fake.details
    asked = len(fake.calls)
    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, RANGE_START, RANGE_END)
    await reader.async_refresh()
    assert not reader.last_update_success
    assert reader.requests_held
    assert len(fake.calls) == asked
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("error", "warm_shown"),
    [
        (NanitRequestRefused("Calendar fetch", 403), False),
        (NanitRateLimited("Calendar fetch", 600), True),
    ],
    ids=["refused", "throttled"],
)
async def test_a_refusal_drops_kept_and_in_flight_answers(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    error: NanitRequestRefused,
    warm_shown: bool,
) -> None:
    """A range kept, a range on its way, then a third range is refused.

    After a refusal neither the kept range nor the one that was on its way
    is shown, notes or not, and neither is kept. While Nanit throttles, the
    kept range is still shown until its five minutes are up.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    # The same four entries on each of the three days.
    for offset in range(3):
        _day_of_entries(fake)
        fake.entries[-4:] = [
            dataclasses.replace(
                item,
                log=dataclasses.replace(
                    item.log,
                    uid=f"{item.log.uid}-{offset}",
                    timestamp=item.log.timestamp - offset * 86400,
                ),
            )
            for item in fake.entries[-4:]
        ]
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    warm = (RANGE_START, RANGE_END)
    on_its_way = (RANGE_START - timedelta(days=1), RANGE_START)
    refused = (RANGE_START - timedelta(days=2), RANGE_START - timedelta(days=1))
    assert len(await _events(hass, calendar, *warm)) == 4

    way_may_answer = asyncio.Event()
    answer = fake.details
    sent: list[int] = []

    async def details(baby_uid: str, start: int, end: int) -> CareLogDetailFetch:
        sent.append(end)
        if end == int(_ts(on_its_way[1])):
            await way_may_answer.wait()
        if end == int(_ts(refused[1])):
            raise error
        return await answer(baby_uid, start, end)

    client.async_get_calendar_details.side_effect = details
    way = hass.async_create_task(_events(hass, calendar, *on_its_way))
    await _until(lambda: len(sent) == 1)
    with pytest.raises(HomeAssistantError):
        await _events(hass, calendar, *refused)
    assert len(sent) == 2
    way_may_answer.set()

    if warm_shown:
        events = await _events(hass, calendar, *warm)
        assert any(NOTE_MARKER in _event_fields(event) for event in events)
        assert len(await way) == 4
    else:
        with pytest.raises(HomeAssistantError):
            await _events(hass, calendar, *warm)
        with pytest.raises(HomeAssistantError):
            await way
        assert calendar._cache == {}
    # Nothing more was asked of Nanit meanwhile.
    assert len(sent) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_finished_fetch_not_yet_forgotten_is_not_shared_after_a_refusal(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """A range is fetched, notes and all; another request is refused before its task is forgotten.

    A caller who then asks for the range gets the usual error, and no rows.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    held_back: list[tuple[Any, Any]] = []
    forget = calendar._fetch_done
    # A finished task is forgotten in a done callback, which the loop runs
    # later: held back here, to stand for that moment.
    with patch.object(calendar, "_fetch_done", lambda key, task: held_back.append((key, task))):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)
        assert any(NOTE_MARKER in _event_fields(event) for event in events)
        assert len(calendar._in_flight) == 1
        fake.error = NanitRequestRefused("Calendar fetch", 403)
        with pytest.raises(HomeAssistantError):
            await _events(hass, calendar, RANGE_START - timedelta(days=1), RANGE_START)
        fake.error = None
        asked = len(fake.calls)
        with pytest.raises(HomeAssistantError):
            await _events(hass, calendar, RANGE_START, RANGE_END)
        assert len(fake.calls) == asked
    for key, task in held_back:
        forget(key, task)
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


# A copy of the bottle b1 that Nanit returns dated outside the range asked
# for: its kind, and how far from the bottle it is dated.
STRAY_COPIES = {
    "later": ("bottle_feed", timedelta(days=5)),
    "earlier": ("bottle_feed", -timedelta(days=5)),
    "other_kind_later": ("diaper_change", timedelta(days=5)),
    "other_kind_earlier": ("diaper_change", -timedelta(days=5)),
    "sleep_later": ("sleep", timedelta(days=5)),
}


@pytest.mark.parametrize("strays_first", [False, True], ids=["stray_last", "stray_first"])
@pytest.mark.parametrize("copy", list(STRAY_COPIES), ids=list(STRAY_COPIES))
async def test_a_copy_dated_outside_its_request_never_hides_the_entry_inside(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    copy: str,
    strays_first: bool,
) -> None:
    """Each row is checked against its request's bounds before one copy per uid is kept.

    Whichever copy is dated later, whatever its kind, and whichever comes first.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    at = _local("2026-11-20T08:00:00")
    fake.add(_log("b1", "bottle_feed", at, feed_amount_ml=30.0))
    entry, calendar = await _one_calendar(hass, client, fake)
    kind, offset = STRAY_COPIES[copy]
    fake.add_stray(_log("b1", kind, at + offset, feed_amount_ml=90.0))
    fake.strays_first = strays_first

    events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert [(e.summary, e.start) for e in events] == [("Bottle, 30 mL", at)]
    ((_method, low, high),) = fake.calendar_calls()
    assert not low <= _ts(at + offset) <= high
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("notes", [False, True], ids=["notes_off", "notes_on"])
async def test_each_piece_of_a_range_is_checked_against_its_own_bounds(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, notes: bool
) -> None:
    """A copy dated inside the range, but outside each piece that returned it, is left out."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    start = RANGE_START - timedelta(days=199)
    early = start + timedelta(days=10)
    fake.add(_log("b1", "bottle_feed", early, feed_amount_ml=30.0))
    entry, calendar = await _one_calendar(hass, client, fake, notes=notes)
    # Dated in the middle piece, so returned only by the first and the last.
    stray = start + timedelta(days=150)
    fake.add_stray(_log("b1", "bottle_feed", stray, feed_amount_ml=90.0))

    events = await _events(hass, calendar, start, RANGE_END)

    assert [(e.summary, e.start) for e in events] == [("Bottle, 30 mL", early)]
    bounds = sorted((low, high) for _m, low, high in fake.calendar_calls())
    assert len(bounds) == 3
    assert [low <= _ts(stray) <= high for low, high in bounds] == [False, True, False]
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
    # From 2014, where care logs begin, to the end of the range.
    assert len(calls) > 50
    assert fake.most_in_flight == 2
    bounds = sorted((s, e) for _m, s, e in calls)
    for (_low, high), (next_low, _next_high) in pairwise(bounds):
        assert high == next_low
    assert await hass.config_entries.async_unload(entry.entry_id)


# Where care logs can exist, from NOW: 2014 on, and a year ahead counted from
# the start of NOW's UTC day.
EARLIEST = datetime.fromisoformat("2014-01-01T00:00:00+00:00")
LATEST = datetime.fromisoformat("2026-11-20T00:00:00+00:00") + timedelta(days=366)


async def test_a_thousand_year_range_asks_only_where_care_logs_can_exist(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    fake.add(_log("early", "diaper_change", EARLIEST))
    fake.add(_log("now", "diaper_change", NOW))
    fake.add(_log("latest", "diaper_change", LATEST))
    entry, calendar = await _one_calendar(hass, client, fake)
    start = datetime.fromisoformat("1500-01-01T00:00:00+00:00")
    end = datetime.fromisoformat("2500-01-01T00:00:00+00:00")

    events = await _events(hass, calendar, start, end)

    calls = fake.calendar_calls()
    bounds = sorted((s, e) for _m, s, e in calls)
    step = int(CARE_LOG_MAX_PIECE.total_seconds())
    span = int(_ts(LATEST)) - int(_ts(EARLIEST))
    assert len(calls) == -(-span // step)
    assert len(calls) < 60
    assert bounds[0][0] == int(_ts(EARLIEST))
    assert bounds[-1][1] == int(_ts(LATEST))
    assert len(events) == 3
    # The same range a minute later is answered from memory: its key is the
    # clamped one, and the ceiling only moves once a day.
    fake.forget_calls()
    freezer.tick(timedelta(minutes=1))
    assert len(await _events(hass, calendar, start, end)) == 3
    assert fake.calls == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2000-01-01T00:00:00+00:00", "2000-02-01T00:00:00+00:00"),
        ("1000-01-01T00:00:00+00:00", "2013-12-31T00:00:00+00:00"),
        ("2028-01-01T00:00:00+00:00", "2028-02-01T00:00:00+00:00"),
        ("2030-01-01T00:00:00+00:00", "3000-01-01T00:00:00+00:00"),
        # Ends a second before 2014, and starts a second after a year ahead:
        # the range asked for is checked, not the 24 hours before it.
        ("2013-12-30T00:00:00+00:00", (EARLIEST - timedelta(seconds=1)).isoformat()),
        ((LATEST + timedelta(seconds=1)).isoformat(), (LATEST + timedelta(days=1)).isoformat()),
    ],
    ids=["before", "long_before", "after", "long_after", "just_before", "just_after"],
)
async def test_a_range_wholly_outside_asks_nothing(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, start: str, end: str
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(
        hass, calendar, datetime.fromisoformat(start), datetime.fromisoformat(end)
    )

    assert events == []
    assert fake.calendar_calls() == []
    assert await hass.config_entries.async_unload(entry.entry_id)


# Starts at the very beginning of the year 1, where 24 hours earlier is no
# date at all.
YEAR_ONE_STARTS = {
    "min_utc": datetime.min.replace(tzinfo=UTC),
    "day_2_utc": datetime(1, 1, 2, tzinfo=UTC),
    # As Home Assistant passes it in a time zone east of UTC: 1 January in UTC.
    "day_2_east": datetime(1, 1, 2, tzinfo=timezone(timedelta(hours=9))),
}


@pytest.mark.parametrize("to_now", [False, True], ids=["ends_before_2014", "ends_now"])
@pytest.mark.parametrize("start", list(YEAR_ONE_STARTS), ids=list(YEAR_ONE_STARTS))
async def test_a_range_from_the_year_1_asks_only_from_2014(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, start: str, to_now: bool
) -> None:
    """The range asked for is checked before the reach-back is taken off its start."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    fake.add(_log("early", "diaper_change", EARLIEST))
    fake.add(_log("now", "bottle_feed", NOW, feed_amount_ml=30.0))
    entry, calendar = await _one_calendar(hass, client, fake)
    range_start = YEAR_ONE_STARTS[start]
    range_end = RANGE_END if to_now else range_start + timedelta(days=1)

    events = await _events(hass, calendar, range_start, range_end)

    calls = fake.calendar_calls()
    if to_now:
        assert _summaries(events) == ["Diaper", "Bottle, 30 mL"]
        # Clamped from the floor: nothing before 2014 is asked for.
        assert min(low for _m, low, _high in calls) == int(_ts(EARLIEST))
        assert max(high for _m, _low, high in calls) == int(_ts(RANGE_END))
    else:
        assert events == []
        assert calls == []
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


def _sent(event: Any) -> tuple[datetime, datetime]:
    """The start and end the frontend is sent for an event, as UTC instants.

    Home Assistant 2025.12 has no public helper for this, so the tests that
    use it are skipped there and the rest of this file still runs.
    """
    helper = pytest.importorskip("homeassistant.components.calendar.helper")
    sent = dataclasses.asdict(event, dict_factory=helper.api_event_dict_factory)
    start = datetime.fromisoformat(sent["start"]["dateTime"])
    end = datetime.fromisoformat(sent["end"]["dateTime"])
    return dt_util.as_utc(start), dt_util.as_utc(end)


async def test_a_half_hour_session_across_the_clocks_going_back_keeps_its_length_in_its_text(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """US/Pacific, 1 November 2026: 30 minutes from 01:45 PDT end at 01:15 PST.

    Home Assistant checks the span on the wall clock, where it reads reversed,
    and turns every datetime into local time before it does, so no form of
    the start and end can pass. The session is shown as a moment, and its
    text still gives its real length.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    nursing = datetime.fromisoformat("2026-11-01T08:45:00+00:00")
    fake.add(_log("n1", "nursing", nursing, left_duration_s=1200.0, right_duration_s=600.0))
    pumping = datetime.fromisoformat("2026-11-01T08:40:00+00:00")
    fake.add(_log("p1", "pumping", pumping, duration_s=1800.0, left_amount_ml=30.0))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(
        hass, calendar, _local("2026-11-01T00:00:00"), _local("2026-11-02T00:00:00")
    )

    pumped, nursed = events
    assert _sent(nursed) == (nursing, nursing + timedelta(minutes=1))
    assert nursed.summary == "Nursing, 30 min"
    assert nursed.description == "Left side: 20 min\nRight side: 10 min"
    assert _sent(pumped) == (pumping, pumping + timedelta(minutes=1))
    # A pumping title holds its amount, as it always does: the length is in
    # the description.
    assert pumped.summary == "Pumping, 30 mL"
    assert pumped.description == "Left side: 30 mL\nDuration: 30 min"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_session_shown_as_a_moment_is_checked_against_the_range_as_shown(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """US/Pacific, 1 November 2026: 20 minutes from 01:50 PDT end at 01:10 PST.

    The session overlaps 01:05 to 01:08 PST, but it is shown as a minute at
    01:50 PDT, an hour before that range: it is not in it. A range holding
    its start still gets it.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    start = datetime.fromisoformat("2026-11-01T08:50:00+00:00")  # 01:50 PDT
    fake.add(_log("n1", "nursing", start, left_duration_s=1200.0))
    entry, calendar = await _one_calendar(hass, client, fake)
    pst = timezone(timedelta(hours=-8))

    inside_the_session = await _events(
        hass,
        calendar,
        datetime(2026, 11, 1, 1, 5, tzinfo=pst),
        datetime(2026, 11, 1, 1, 8, tzinfo=pst),
    )
    around_its_start = await _events(
        hass, calendar, start - timedelta(minutes=5), start + timedelta(minutes=5)
    )

    assert inside_the_session == []
    (event,) = around_its_start
    assert _sent(event) == (start, start + timedelta(minutes=1))
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sessions_across_a_30_minute_clock_change(hass: HomeAssistant, client: Any) -> None:
    """Australia/Lord_Howe, 5 April 2026: 02:00 (+11:00) goes back to 01:30 (+10:30).

    Run on the real clock: freezegun's datetimes drop ``fold`` on a time
    zone conversion, and with it which of two repeated wall-clock times is
    meant, so under the frozen clock the end would be sent as the start.
    """
    await hass.config.async_set_time_zone("Australia/Lord_Howe")
    fake = FakeNanit()
    # 30 minutes from 01:45 (+11:00) end at 01:45 (+10:30): no time at all on
    # the wall clock, which Home Assistant accepts, and sent with both
    # offsets, so the frontend gets the real half hour.
    whole = datetime.fromisoformat("2026-04-04T14:45:00+00:00")
    fake.add(_log("n1", "nursing", whole, left_duration_s=1800.0))
    # 20 minutes from 01:50 (+11:00) end at 01:40 (+10:30): reversed, so a moment.
    reversed_start = datetime.fromisoformat("2026-04-04T14:50:00+00:00")
    fake.add(_log("n2", "nursing", reversed_start, right_duration_s=1200.0))
    entry, calendar = await _one_calendar(hass, client, fake)

    events = await _events(
        hass, calendar, _local("2026-04-05T00:00:00"), _local("2026-04-06T00:00:00")
    )

    shown, moment = events
    assert _sent(shown) == (whole, whole + timedelta(minutes=30))
    assert shown.start.utcoffset() == timedelta(hours=11)
    assert shown.end.utcoffset() == timedelta(hours=10, minutes=30)
    assert shown.summary == "Nursing, 30 min"
    assert _sent(moment) == (reversed_start, reversed_start + timedelta(minutes=1))
    assert moment.summary == "Nursing, 20 min"
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


def _english_strings() -> dict[str, str]:
    """The calendar's English text as Home Assistant's translation lookup returns it."""
    english = json.loads(
        (Path(calendar_module.__file__).parent / "translations" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    attributes = english["entity"]["calendar"]["care_log"]["state_attributes"]
    prefix = "component.nanit.entity.calendar.care_log.state_attributes"
    return {
        f"{prefix}.{group}.state.{key}": text
        for group, value in attributes.items()
        for key, text in value["state"].items()
    }


def _labels(name: str) -> dict[str, dict[str, str]]:
    """The calendar's label groups in one translation file."""
    folder = Path(calendar_module.__file__).parent
    path = folder / name if name == "strings.json" else folder / "translations" / name
    strings = json.loads(path.read_text(encoding="utf-8"))
    attributes = strings["entity"]["calendar"]["care_log"]["state_attributes"]
    return {group: value["state"] for group, value in attributes.items()}


def test_the_labels_are_plain_text_and_the_same_in_both_files() -> None:
    labels = _labels("strings.json")
    assert _labels("en.json") == labels
    assert {group: set(keys) for group, keys in labels.items()} == {
        "event_title": {"diaper_change", "bottle_feed", "nursing", "pumping"},
        "event_detail": {"left_side", "right_side", "duration", "logged_at", "note", "logged_by"},
        "diaper_type": {"pee", "poo", "mixed", "dry"},
        "measurement": {"milliliters", "fluid_ounces", "minutes"},
    }
    for group, keys in labels.items():
        for key, label in keys.items():
            assert label, (group, key)
            assert label == label.strip(), (group, key)
            assert "{" not in label, (group, key)
            assert "}" not in label, (group, key)


async def test_the_english_text_is_put_together_from_the_labels(hass: HomeAssistant) -> None:
    """The exact English a user sees, for every pattern."""
    await hass.config.async_set_time_zone("UTC")
    hass.config.units = METRIC_SYSTEM
    metric = calendar_module._Formatter(hass, calendar_module._Texts(_english_strings()))
    hass.config.units = US_CUSTOMARY_SYSTEM
    customary = calendar_module._Formatter(hass, calendar_module._Texts(_english_strings()))
    at = datetime(2026, 11, 20, 15, 5, tzinfo=UTC)

    def entry(kind: str, **fields: Any) -> CareLog:
        return _log("e1", kind, at, **fields)

    assert [
        metric.summary(entry("diaper_change")),
        metric.summary(entry("diaper_change", change_type="pee")),
        metric.summary(entry("diaper_change", change_type="unknown")),
        metric.summary(entry("bottle_feed")),
        metric.summary(entry("bottle_feed", feed_amount_ml=120.0)),
        customary.summary(entry("bottle_feed", feed_amount_ml=125.6862)),
        metric.summary(entry("nursing")),
        metric.summary(entry("nursing", left_duration_s=420.0, right_duration_s=300.0)),
        metric.summary(entry("pumping")),
        metric.summary(entry("pumping", left_amount_ml=60.0, right_amount_ml=30.0)),
        metric.summary(entry("solid_food")),
    ] == [
        "Diaper",
        "Diaper, pee",
        "Diaper",
        "Bottle",
        "Bottle, 120 mL",
        "Bottle, 4.25 fl oz",
        "Nursing",
        "Nursing, 12 min",
        "Pumping",
        "Pumping, 90 mL",
        "Solid food",
    ]
    assert metric.amount(120.0) == "120 mL"
    assert customary.amount(125.6862) == "4.25 fl oz"
    assert metric.duration(720.0) == "12 min"

    nursed = calendar_module._Row(
        entry("nursing", left_duration_s=300.0, right_duration_s=720.0, logged_at=_ts(at)),
        CareLogDetail(comment="Sleepy", user_name="Sam", user_relation="dad"),
    )
    assert metric.description(nursed, show_notes=True) == (
        "Left side: 5 min\nRight side: 12 min\nLogged at 2026-11-20 15:05\n"
        "Note: Sleepy\nLogged by Sam (dad)"
    )
    assert metric.description(nursed, show_notes=False) == (
        "Left side: 5 min\nRight side: 12 min\nLogged at 2026-11-20 15:05"
    )
    pumped = calendar_module._Row(
        entry("pumping", right_amount_ml=30.0, duration_s=720.0),
        CareLogDetail(user_name="Sam"),
    )
    assert metric.description(pumped, show_notes=True) == (
        "Right side: 30 mL\nDuration: 12 min\nLogged by Sam"
    )


def test_the_fallback_holds_no_english_text() -> None:
    """Only each label's key: no text of strings.json comes back."""
    texts = calendar_module._Texts({})
    for group, keys in _labels("strings.json").items():
        for key in keys:
            assert texts.get(group, key) == key, (group, key)


async def test_text_shows_keys_and_values_when_translations_fail(
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
        "diaper_change, mixed",
        "bottle_feed, 30 milliliters",
        "nursing, 15 minutes",
        "pumping, 30 milliliters",
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
    # As Home Assistant returns a language: English for every key it lacks.
    translated = {
        **_english_strings(),
        f"{prefix}.event_title.state.bottle_feed": "Biberon",
        f"{prefix}.measurement.state.milliliters": "ml",
        # Empty: Home Assistant would have filled in English, so it can only
        # be a broken file; the key shows instead.
        f"{prefix}.event_title.state.nursing": "",
    }

    with patch.object(
        calendar_module, "async_get_translations", AsyncMock(return_value=translated)
    ):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)

    assert _summaries(events) == [
        "Diaper, mixed",
        "Biberon, 30 ml",
        "nursing, 15 min",
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


async def test_a_shared_fetch_that_fails_after_a_caller_gave_up_is_not_reported(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    """The caller still waiting gets the failure, and nobody else does.

    Some Python versions report a shielded future's failure to the event
    loop's exception handler once its waiter was cancelled. The ``hass``
    fixture fails a test in which anything reaches that handler.
    """
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake)
    fake.gate = asyncio.Event()
    fake.error = NanitConnectionError("Calendar fetch failed: TimeoutError")

    first = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    second = hass.async_create_task(_events(hass, calendar, RANGE_START, RANGE_END))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    fake.gate.set()

    with pytest.raises(HomeAssistantError):
        await second
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(fake.calendar_calls()) == 1
    fake.error = None
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


@pytest.mark.parametrize("idle", [False, True], ids=["asked_again", "idle"])
async def test_a_clock_set_back_keeps_no_answer_past_five_minutes(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, idle: bool
) -> None:
    """Five minutes pass while the wall clock is set back two hours: the notes still go."""
    freezer.move_to(NOW)
    fake = FakeNanit()
    _day_of_entries(fake)
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    await _events(hass, calendar, RANGE_START, RANGE_END)
    assert calendar._cache

    freezer.tick(CARE_LOG_CACHE_TTL)
    set_back = dt_util.utcnow() - timedelta(hours=2)
    with patch.object(dt_util, "utcnow", return_value=set_back):
        if idle:
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
            assert calendar._cache == {}
        else:
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


def _many_bottles(fake: FakeNanit, count: int) -> None:
    """``count`` bottles spread over the day of RANGE_START."""
    step = (RANGE_END - RANGE_START) / (count + 1)
    for i in range(count):
        fake.add(
            _log(f"b{i:05d}", "bottle_feed", RANGE_START + step * (i + 1), feed_amount_ml=30.0)
        )


async def _turns_while(awaitable: Any, on_turn: Any = None) -> tuple[Any, int]:
    """Await ``awaitable`` in its own task, counting the turns another task gets meanwhile."""
    task = asyncio.ensure_future(awaitable)
    turns = 0
    while not task.done():
        await asyncio.sleep(0)
        turns += 1
        if on_turn is not None:
            on_turn(turns)
    return await task, turns


@pytest.mark.parametrize("cached", [False, True], ids=["fetched", "from_memory"])
async def test_a_long_answer_lets_the_event_loop_run_while_it_is_built(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory, cached: bool
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _many_bottles(fake, 1000)
    entry, calendar = await _one_calendar(hass, client, fake)
    if cached:
        await _events(hass, calendar, RANGE_START, RANGE_END)

    events, turns = await _turns_while(_events(hass, calendar, RANGE_START, RANGE_END))

    assert len(events) == 1000
    assert [event.start for event in events] == sorted(event.start for event in events)
    # 1,000 rows, 200 to a batch: the loop ran between every two.
    assert turns >= 4
    assert len(fake.calendar_calls()) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_notes_are_not_returned_when_the_option_goes_off_while_the_answer_is_built(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake = FakeNanit()
    _many_bottles(fake, 1000)
    entry, calendar = await _one_calendar(hass, client, fake, notes=True)
    await _events(hass, calendar, RANGE_START, RANGE_END)

    def option_off(turn: int) -> None:
        if turn == 3:
            # Only the option changes: the reload it would start is held back.
            with patch.object(entry, "update_listeners", []):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_SHOW_CARE_LOG_NOTES: False}
                )

    with pytest.raises(HomeAssistantError) as raised:
        await _turns_while(_events(hass, calendar, RANGE_START, RANGE_END), option_off)

    assert raised.value.translation_key == "care_log_calendar_unavailable"
    assert not is_show_care_log_notes(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_cache_is_bounded_by_rows(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to(NOW)
    monkeypatch.setattr(calendar_module, "CARE_LOG_CACHE_MAX_ROWS", 6, raising=False)
    fake = FakeNanit()
    _day_of_entries(fake)  # four entries on the day of RANGE_START
    for i in range(3):
        fake.add(_log(f"x{i}", "diaper_change", RANGE_START - timedelta(days=3, hours=-i)))
    entry, calendar = await _one_calendar(hass, client, fake)
    day_before = (RANGE_START - timedelta(days=3), RANGE_START - timedelta(days=2))

    assert len(await _events(hass, calendar, RANGE_START, RANGE_END)) == 4
    freezer.tick(timedelta(seconds=1))
    assert len(await _events(hass, calendar, *day_before)) == 3
    # Four rows and three would be seven: the older range is dropped.
    assert [len(cached.rows) for cached in calendar._cache.values()] == [3]

    # A week holds all seven rows: answered, and not kept.
    freezer.tick(timedelta(seconds=1))
    week = await _events(hass, calendar, RANGE_START - timedelta(days=6), RANGE_END)
    assert len(week) == 7
    assert [len(cached.rows) for cached in calendar._cache.values()] == [3]
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
    spare_note, nursery_note = "zqsparenote1180", "zqnurserynote6604"
    fake.add(
        _log("d4", "diaper_change", _local("2026-11-20T07:00:00"), baby_uid="baby_4"),
        note=spare_note,
    )
    fake.add(
        _log("b1", "bottle_feed", _local("2026-11-20T08:00:00"), feed_amount_ml=30.0),
        note=nursery_note,
    )
    entry = await _set_up(hass, client, fake, notes=True, babies=[BABY, no_camera])

    calendars = {c.coordinator.baby.uid: c for c in _calendars(hass, entry)}
    assert set(calendars) == {BABY_MARKER, "baby_4"}
    spare = calendars["baby_4"]
    assert spare.entity_id == "calendar.spare_room_care_log"
    assert er.async_get(hass).async_get(spare.entity_id).device_id is None
    assert hass.states.get(spare.entity_id).state == STATE_OFF
    # Each calendar asks for its own baby and shows only that baby's entries.
    spare_events = await _events(hass, spare, RANGE_START, RANGE_END)
    nursery_events = await _events(hass, calendars[BABY_MARKER], RANGE_START, RANGE_END)
    assert _summaries(spare_events) == ["Diaper"]
    assert _summaries(nursery_events) == ["Bottle, 30 mL"]
    spare_text = " ".join(_event_fields(event) for event in spare_events)
    nursery_text = " ".join(_event_fields(event) for event in nursery_events)
    assert spare_note in spare_text and nursery_note not in spare_text
    assert nursery_note in nursery_text and spare_note not in nursery_text
    assert {baby_uid for _m, baby_uid, _s, _e in fake.calls} == {BABY_MARKER, "baby_4"}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unnamed_baby_without_a_camera_gets_a_name_without_its_uid(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    unnamed_uid = "zqunnamedbaby4471"
    unnamed = Baby(uid=unnamed_uid, name="", camera_uid="")
    fake = FakeNanit()
    entry = await _set_up(hass, client, fake, babies=[BABY, unnamed])

    (calendar,) = (c for c in _calendars(hass, entry) if c.coordinator.baby.uid == unnamed_uid)
    registered = er.async_get(hass).async_get(calendar.entity_id)
    assert registered is not None
    state = hass.states.get(calendar.entity_id)
    shown = " ".join([calendar.entity_id, registered.unique_id, str(calendar.name), state.name])
    # Not the uid, nor any piece of it as long as the old fallback's eight characters.
    for i in range(len(unnamed_uid) - 7):
        assert unnamed_uid[i : i + 8] not in shown, shown
    assert state.name == f"Baby {baby_code(unnamed_uid)[:8]} care log"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_sign_ins_seeing_the_same_baby_get_two_calendars(
    hass: HomeAssistant, client: Any, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    # Each sign-in has a client of its own, answering with what that account
    # sees of the same baby: one note each.
    fakes = {"first": FakeNanit(), "second": FakeNanit()}
    notes = {"first": "zqfirstsignin3318", "second": "zqsecondsignin7720"}
    for name, fake in fakes.items():
        fake.add(_log("d1", "diaper_change", _local("2026-11-20T07:00:00")), note=notes[name])
    first = await _set_up(hass, client, fakes["first"], notes=True)
    second = await _set_up(
        hass,
        client,
        fakes["second"],
        notes=True,
        entry=_entry(hass, notes=True, email="other@example.com"),
    )
    for entry, name in ((first, "first"), (second, "second")):
        own = MagicMock(async_close=AsyncMock())
        own.async_get_calendar_result = AsyncMock(side_effect=fakes[name].result)
        own.async_get_calendar_details = AsyncMock(side_effect=fakes[name].details)
        entry.runtime_data.hub._client = own
        fakes[name].forget_calls()

    one, two = _calendar(hass, first), _calendar(hass, second)
    assert one.unique_id != two.unique_id
    assert one.entity_id != two.entity_id
    for calendar, name, other in ((one, "first", "second"), (two, "second", "first")):
        events = await _events(hass, calendar, RANGE_START, RANGE_END)
        text = " ".join(_event_fields(event) for event in events)
        assert notes[name] in text
        assert notes[other] not in text
        # Each has its own client and cache, so each fetched once, for itself.
        assert len(fakes[name].calendar_calls()) == 1
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


async def test_a_failed_first_fetch_logs_neither_the_baby_name_nor_its_uid(
    hass: HomeAssistant,
    client: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    freezer.move_to(NOW)
    baby = Baby(uid=BABY_MARKER, name=NAME_MARKER, camera_uid="cam_1")
    fake = FakeNanit()
    fake.error = NanitConnectionError("Calendar fetch failed: TimeoutError")

    entry = await _set_up(hass, client, fake, babies=[baby])

    warnings = [
        record
        for record in caplog.records
        if record.name == "custom_components.nanit.hub" and "failed at setup" in record.getMessage()
    ]
    assert len(warnings) == 1
    for record in caplog.records:
        if record.levelno < logging.WARNING and record.name != "custom_components.nanit.hub":
            continue
        shown = record.getMessage() + (record.exc_text or "")
        assert NAME_MARKER not in shown, record.name
        assert BABY_MARKER not in shown, record.name
    # Named by its code instead, so two babies' warnings can be told apart.
    assert baby_code(BABY_MARKER) in warnings[0].getMessage()
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
    _day_of_entries(fake, E2E_BABY.uid)
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
