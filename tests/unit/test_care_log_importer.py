"""The care log statistics importer: history bounds, and what it does without a recorder.

The tests that write statistics are in ``recorder/test_care_log_importer.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from aionanit.models import Baby, CareLogFetch
from custom_components.nanit import care_log_importer
from custom_components.nanit.care_log_importer import (
    HISTORY_FLOOR,
    HISTORY_PIECE,
    CareLogStatisticsImporter,
    care_log_history_pieces,
    care_log_history_start,
)
from custom_components.nanit.const import CARE_LOG_POLL_INTERVAL, DOMAIN
from custom_components.nanit.coordinator import NanitCareLogCoordinator

from .conftest import MOCK_EMAIL, mock_entry_data_v2

DAY = 86400
PIECE = int(HISTORY_PIECE.total_seconds())
NOW = datetime(2026, 11, 20, 18, 10, tzinfo=UTC)


def _baby(birth: date | None = None, created: float | None = None) -> Baby:
    return Baby(
        uid="baby_1", name="Nursery", camera_uid="cam_1", birth_date=birth, created_at=created
    )


# ---------------------------------------------------------------------------
# History start
# ---------------------------------------------------------------------------


def test_history_starts_a_year_before_the_birth_date() -> None:
    start = care_log_history_start(_baby(birth=date(2026, 3, 1)), NOW)

    assert start == int(datetime(2025, 3, 1, tzinfo=UTC).timestamp())


def test_history_starts_a_year_before_the_creation_date_when_that_is_earlier() -> None:
    created = datetime(2025, 12, 24, 15, 30, tzinfo=UTC)
    start = care_log_history_start(_baby(birth=date(2026, 3, 1), created=created.timestamp()), NOW)

    assert start == int((created - timedelta(days=365)).timestamp())


def test_history_uses_the_birth_date_when_it_is_earlier_than_the_creation_date() -> None:
    created = datetime(2026, 5, 1, tzinfo=UTC).timestamp()
    start = care_log_history_start(_baby(birth=date(2026, 3, 1), created=created), NOW)

    assert start == int(datetime(2025, 3, 1, tzinfo=UTC).timestamp())


@pytest.mark.parametrize("created", [None, float("nan"), float("inf"), 0.0, -5.0])
def test_history_starts_three_years_back_on_the_grid_without_a_usable_date(
    created: float | None,
) -> None:
    start = care_log_history_start(_baby(created=created), NOW)

    three_years = int((NOW - timedelta(days=3 * 365)).timestamp())
    assert start % PIECE == 0
    assert three_years - PIECE < start <= three_years
    # The same bounds a day later: the fallback does not move every day.
    assert care_log_history_start(_baby(created=created), NOW + timedelta(days=1)) == start


def test_history_never_starts_before_the_floor() -> None:
    start = care_log_history_start(_baby(birth=date(1950, 1, 1)), NOW)

    assert start == int(HISTORY_FLOOR.timestamp())


# ---------------------------------------------------------------------------
# History pieces
# ---------------------------------------------------------------------------


def _check_pieces(pieces: tuple[tuple[int, int], ...], start: int, end: int) -> None:
    assert pieces[0][0] == start
    assert pieces[-1][1] == end
    for low, high in pieces:
        assert low <= high
        assert high - low + 1 <= PIECE
    for previous, current in pairwise(pieces):
        assert current[0] == previous[1] + 1
        assert current[0] % PIECE == 0


def test_no_pieces_when_the_history_does_not_start_before_the_end() -> None:
    assert care_log_history_pieces(100, 99) == ()
    assert care_log_history_pieces(100, 50) == ()


def test_one_piece_inside_one_grid_cell() -> None:
    start = 3 * PIECE + 10
    assert care_log_history_pieces(start, start + 1000) == ((start, start + 1000),)
    assert care_log_history_pieces(start, start) == ((start, start),)


def test_pieces_break_on_the_grid() -> None:
    start = 3 * PIECE + 10 * DAY
    end = 6 * PIECE + 5 * DAY

    pieces = care_log_history_pieces(start, end)

    assert pieces == (
        (start, 4 * PIECE - 1),
        (4 * PIECE, 5 * PIECE - 1),
        (5 * PIECE, 6 * PIECE - 1),
        (6 * PIECE, end),
    )
    _check_pieces(pieces, start, end)


def test_pieces_starting_and_ending_on_the_grid_are_whole() -> None:
    pieces = care_log_history_pieces(2 * PIECE, 4 * PIECE - 1)

    assert pieces == ((2 * PIECE, 3 * PIECE - 1), (3 * PIECE, 4 * PIECE - 1))


def test_an_end_just_past_the_grid_gets_a_one_second_piece() -> None:
    pieces = care_log_history_pieces(2 * PIECE + 5, 3 * PIECE)

    assert pieces == ((2 * PIECE + 5, 3 * PIECE - 1), (3 * PIECE, 3 * PIECE))


def test_pieces_of_a_real_history_keep_their_bounds_from_day_to_day() -> None:
    start = care_log_history_start(_baby(birth=date(2025, 8, 1)), NOW)
    today = care_log_history_pieces(start, int(NOW.timestamp()) - DAY - 1)
    tomorrow = care_log_history_pieces(start, int(NOW.timestamp()) - 1)

    _check_pieces(today, start, int(NOW.timestamp()) - DAY - 1)
    assert len(today) >= 3
    assert tomorrow[: len(today) - 1] == today[:-1]


# ---------------------------------------------------------------------------
# Without a recorder
# ---------------------------------------------------------------------------


class _Calls:
    """The importer's own fetches, and the reader's."""

    def __init__(self) -> None:
        self.own: list[tuple[int, int]] = []
        self.reader: list[tuple[int, int]] = []

    async def fetch(self, start: int, end: int) -> CareLogFetch:
        self.own.append((start, end))
        return CareLogFetch(logs=(), start=start, end=end, dropped=0)

    async def reader_fetch(self, _uid: str, start: int, end: int) -> CareLogFetch:
        self.reader.append((start, end))
        return CareLogFetch(logs=(), start=start, end=end, dropped=0)


def _importer(
    hass: HomeAssistant, calls: _Calls, **entry_options: bool
) -> tuple[CareLogStatisticsImporter, NanitCareLogCoordinator, MockConfigEntry]:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL, **entry_options
    )
    entry.add_to_hass(hass)
    hub = MagicMock()
    hub.client.async_get_calendar_result = AsyncMock(side_effect=calls.reader_fetch)
    baby = _baby(birth=date(2026, 3, 1))
    coordinator = NanitCareLogCoordinator(hass, entry, hub, baby)
    importer = CareLogStatisticsImporter(
        hass, entry, baby=baby, coordinator=coordinator, fetch=calls.fetch
    )
    return importer, coordinator, entry


async def _settle(
    hass: HomeAssistant, entry: MockConfigEntry, importer: CareLogStatisticsImporter
) -> None:
    """Wait for the reader's polls in flight, then for the importer's worker to idle."""
    await hass.async_block_till_done()
    busy = [task for task in entry._background_tasks if task is not importer._task]
    if busy:
        await asyncio.wait(busy)
    if importer._task is not None:
        await importer._idle.wait()


async def _next_poll(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    importer: CareLogStatisticsImporter,
) -> None:
    freezer.tick(timedelta(seconds=CARE_LOG_POLL_INTERVAL + 1))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await _settle(hass, entry, importer)


async def test_the_listener_keeps_the_reader_polling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """No sensor listens: the importer's listener alone makes the reader poll."""
    freezer.move_to(NOW)
    calls = _Calls()
    importer, _coordinator, entry = _importer(hass, calls)

    await _next_poll(hass, freezer, entry, importer)
    assert calls.reader == []

    importer.async_start()
    await _next_poll(hass, freezer, entry, importer)
    await _next_poll(hass, freezer, entry, importer)
    assert len(calls.reader) == 2
    # Without a recorder, nothing of its own.
    assert calls.own == []
    assert importer.diagnostics()["state"] == "waiting_for_recorder"

    await importer.async_stop()
    await _next_poll(hass, freezer, entry, importer)
    assert len(calls.reader) == 2
    assert importer.diagnostics()["state"] == "stopped"


async def test_polling_disabled_makes_no_request_of_its_own(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    calls = _Calls()
    importer, coordinator, entry = _importer(hass, calls, pref_disable_polling=True)

    with patch.object(care_log_importer, "care_log_recorder_ready", return_value=True):
        importer.async_start()
        # The reader does not poll by itself either; an update asked for does.
        await coordinator.async_refresh()
        await _settle(hass, entry, importer)
        freezer.tick(timedelta(days=1))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await coordinator.async_refresh()
        await _settle(hass, entry, importer)

    assert len(calls.reader) == 2
    assert calls.own == []
    assert importer.diagnostics()["state"] == "polling_disabled"
    await importer.async_stop()


async def test_diagnostics_before_anything_happened(hass: HomeAssistant) -> None:
    importer, _, _ = _importer(hass, _Calls())

    assert importer.diagnostics() == {
        "state": "idle",
        "entries": 0,
        "pending_records": 0,
        "waiting_entries": 0,
        "established": False,
        "passes_completed": 0,
        "confirm_fetches": 0,
        "give_ups": 0,
        "sync_failures": 0,
        "queue_overflows": 0,
        "rows_written": 0,
        "rows_written_last_sync": 0,
        "minutes_since_last_sync": None,
        "minutes_since_last_history_pass": None,
    }
    # Stopping one that never started is fine.
    await importer.async_stop()
