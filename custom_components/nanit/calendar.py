"""Care log calendar for Nanit: every care entry of a baby, read on demand.

One read-only calendar per config entry and baby. Nothing is stored: each
range is fetched from Nanit when the calendar is asked for it, and the
answer is kept in memory for a few minutes. The calendar never reports a
current event, so nothing from an entry reaches its state or Home
Assistant's history. Notes and who logged an entry are fetched, and shown,
only when the entry's "Show notes and who logged each entry" option is on.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfVolume
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from aionanit.exceptions import NanitError
from aionanit.models import CareLog, CareLogDetail

from . import NanitConfigEntry
from .baby_code import baby_code, baby_display_name
from .const import DOMAIN, is_show_care_log_notes
from .coordinator import (
    CARE_LOG_BOTTLE,
    CARE_LOG_DIAPER,
    CARE_LOG_NURSING,
    CARE_LOG_PUMPING,
    NanitCareLogCoordinator,
)
from .sanitize import display_name

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Sleep entries are not shown yet: these kinds are skipped entirely.
CARE_LOG_SLEEP_KINDS = frozenset({"sleep", "auto_sleep", "semi_auto_sleep"})

# How long an entry without a usable length (a moment) is shown for, cut
# short at the next local midnight so it never reaches into the next day,
# and just before the clocks go back (see _moment_end).
# Home Assistant's calendar view draws an event of zero length an hour long,
# so a moment late in the evening would reach into the next day.
CARE_LOG_MOMENT_LENGTH = timedelta(minutes=1)
_MICROSECOND = timedelta(microseconds=1)

# Nanit matches a range by an entry's start only, so every request reaches
# this far back to catch an entry that started before the range and runs
# into it. A margin, not a proven maximum.
CARE_LOG_REACH_BACK = timedelta(hours=24)

# Where care logs can exist, so an absurd range costs a few dozen requests
# rather than one per 90 days without end: nothing before 2014 (the floor
# the statistics importer uses for history; Nanit's care logs are younger)
# and nothing more than a year ahead. The ceiling counts from the start of
# today in UTC, so it, and with it a range's cache key, stays put all day.
# A range wholly outside is answered empty without asking Nanit.
CARE_LOG_EARLIEST = datetime(2014, 1, 1, tzinfo=UTC)
CARE_LOG_AHEAD = timedelta(days=366)

# The longest single request, and how many requests may run at once. A
# range is fetched by this many workers taking its pieces in turn, so a
# long range adds no more tasks than a short one.
CARE_LOG_MAX_PIECE = timedelta(days=90)
CARE_LOG_MAX_IN_FLIGHT = 2

# How long a fetched range is answered from memory, and how many are kept.
# An answer is removed when its time is up, even if nothing asks again.
CARE_LOG_CACHE_TTL = timedelta(minutes=5)
CARE_LOG_CACHE_SIZE = 16
_CACHE_TTL_SECONDS = CARE_LOG_CACHE_TTL.total_seconds()

# And how many rows they may hold together. A row takes about 550 bytes in
# memory (measured, notes included), so this is some 15 MB at most: room for
# a three-year range of a busy log (20 entries a day). A single answer above
# it is returned but not kept.
CARE_LOG_CACHE_MAX_ROWS = 25_000

# How many rows an answer is built from before the event loop gets a turn.
# Building one event took about 43 microseconds (measured with the loop's
# debug mode off), so a batch holds the loop for about 10 ms.
CARE_LOG_ROWS_PER_TURN = 200

# How many different ranges one calendar fetches at once: as many as it
# keeps. A request for another range meanwhile gets the usual error.
CARE_LOG_MAX_RANGES_IN_FLIGHT = CARE_LOG_CACHE_SIZE

# A stated length longer than the reach-back can't be trusted to be shown
# whole, and could overflow a date: such an entry is shown as a moment.
_MAX_LENGTH = CARE_LOG_REACH_BACK

_DIAPER_TYPES = ("pee", "poo", "mixed", "dry")

# Where the event text's labels live in the translation files: under the
# calendar entity's own translation key, as state attributes, the one place
# in an entity's translations that holds free text keyed by short tokens.
# hassfest allows no placeholder in a state or state attribute value, so
# these are plain labels, and the text is put together here (see _Formatter).
_TRANSLATION_PREFIX = f"component.{DOMAIN}.entity.calendar.care_log.state_attributes"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one care log calendar per baby that has a care log reader."""
    cameras = entry.runtime_data.cameras
    entities: list[NanitCareLogCalendar] = []
    for coordinator in entry.runtime_data.hub.care_log_coordinators.values():
        # On the camera's device only when that camera was set up; a baby
        # without one, or whose camera failed, gets a calendar of its own.
        camera_uid = coordinator.baby.camera_uid
        entities.append(
            NanitCareLogCalendar(
                coordinator,
                entry_id=entry.entry_id,
                camera_uid=camera_uid if camera_uid and camera_uid in cameras else None,
            )
        )
    # Called once even with no readers, like every other platform.
    async_add_entities(entities)


@dataclass(frozen=True)
class _Row:
    """One fetched entry: its numbers, and its personal text when that was asked for."""

    log: CareLog
    detail: CareLogDetail | None


class _Cached:
    """A fetched range kept in memory. Its printed form shows only a count."""

    __slots__ = ("fetched_at", "rows")

    def __init__(self, rows: tuple[_Row, ...], fetched_at: float) -> None:
        self.rows = rows
        self.fetched_at = fetched_at

    def __repr__(self) -> str:
        return f"<care log range: {len(self.rows)} entries>"


type _CacheKey = tuple[int, int, bool]


class _Texts:
    """Event labels in the configured language (Home Assistant fills gaps with English)."""

    def __init__(self, strings: Mapping[str, str]) -> None:
        self._strings = strings

    def get(self, group: str, key: str) -> str:
        """Return one label, or its key when no usable translation is found.

        Home Assistant adds English for any key a language lacks, so the key
        is used only when the integration's own English translation file
        can't be read, or the lookup fails. It holds no text of its own.
        """
        label = self._strings.get(f"{_TRANSLATION_PREFIX}.{group}.state.{key}")
        return label if isinstance(label, str) and label else key


# How the labels and values are put together, one pattern each. Values are
# joined as they are, so text from Nanit is never read as a template.


def _with_detail(label: str, value: str) -> str:
    """Return an event title with its detail: "Bottle, 120 mL"."""
    return f"{label}, {value}"


def _field(label: str, value: str) -> str:
    """Return one line of an event's description: "Left side: 5 min"."""
    return f"{label}: {value}"


def _phrase(label: str, value: str) -> str:
    """Return a label followed by its value: "Logged at ...", "Logged by Sam"."""
    return f"{label} {value}"


def _measured(value: str, unit: str) -> str:
    """Return a number with its unit: "120 mL"."""
    return f"{value} {unit}"


def _with_relation(name: str, relation: str) -> str:
    """Return a person's name with their relation to the baby: "Sam (dad)"."""
    return f"{name} ({relation})"


def _number(value: float, decimals: int = 1) -> str:
    """Return value with at most ``decimals`` decimals, without trailing zeros."""
    rounded = round(value, decimals)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{decimals}f}".rstrip("0")


def _valid(log: CareLog, name: str) -> float | None:
    """Return a measurement that is present, valid and finite, else None."""
    if name in log.invalid_fields:
        return None
    value = getattr(log, name)
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _total(*values: float | None) -> float | None:
    """Return the sum of the values present, or None when none is or the sum isn't finite."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    total = sum(present)
    return total if math.isfinite(total) else None


def _length(log: CareLog) -> float | None:
    """Return how long the entry runs in seconds, or None when it is a moment."""
    if log.log_type == CARE_LOG_NURSING:
        seconds = _total(_valid(log, "left_duration_s"), _valid(log, "right_duration_s"))
    elif log.log_type in (CARE_LOG_DIAPER, CARE_LOG_BOTTLE):
        return None
    else:
        seconds = _valid(log, "duration_s")
    if seconds is None or seconds <= 0 or seconds > _MAX_LENGTH.total_seconds():
        return None
    return seconds


class _Formatter:
    """Turns an entry into event text, in the current language and units."""

    def __init__(self, hass: HomeAssistant, texts: _Texts) -> None:
        self._texts = texts
        converted = hass.config.units.get_converted_unit(
            SensorDeviceClass.VOLUME, UnitOfVolume.MILLILITERS
        )
        self._fluid_ounces = converted == UnitOfVolume.FLUID_OUNCES

    def amount(self, milliliters: float) -> str:
        if self._fluid_ounces:
            ounces = VolumeConverter.convert(
                milliliters, UnitOfVolume.MILLILITERS, UnitOfVolume.FLUID_OUNCES
            )
            # Two decimals: parents log quarter ounces, which one decimal mangles.
            return _measured(_number(ounces, 2), self._texts.get("measurement", "fluid_ounces"))
        return _measured(_number(milliliters), self._texts.get("measurement", "milliliters"))

    def duration(self, seconds: float) -> str:
        return _measured(_number(seconds / 60), self._texts.get("measurement", "minutes"))

    def summary(self, log: CareLog) -> str:
        kind = log.log_type
        if kind not in (CARE_LOG_DIAPER, CARE_LOG_BOTTLE, CARE_LOG_NURSING, CARE_LOG_PUMPING):
            # The kind is a short lowercase token (the library guarantees it).
            return kind.replace("_", " ").capitalize()
        label = self._texts.get("event_title", kind)
        detail: str | None = None
        if kind == CARE_LOG_DIAPER:
            if log.change_type in _DIAPER_TYPES:
                detail = self._texts.get("diaper_type", log.change_type)
        elif kind == CARE_LOG_BOTTLE:
            if (amount := _valid(log, "feed_amount_ml")) is not None:
                detail = self.amount(amount)
        elif kind == CARE_LOG_NURSING:
            seconds = _total(_valid(log, "left_duration_s"), _valid(log, "right_duration_s"))
            if seconds is not None:
                detail = self.duration(seconds)
        else:
            amount = _total(_valid(log, "left_amount_ml"), _valid(log, "right_amount_ml"))
            if amount is not None:
                detail = self.amount(amount)
        return label if detail is None else _with_detail(label, detail)

    def description(self, row: _Row, show_notes: bool) -> str | None:
        texts = self._texts
        log = row.log
        lines: list[str] = []
        if log.log_type == CARE_LOG_NURSING:
            for side in ("left", "right"):
                if (seconds := _valid(log, f"{side}_duration_s")) is not None:
                    lines.append(
                        _field(texts.get("event_detail", f"{side}_side"), self.duration(seconds))
                    )
        elif log.log_type == CARE_LOG_PUMPING:
            for side in ("left", "right"):
                if (amount := _valid(log, f"{side}_amount_ml")) is not None:
                    lines.append(
                        _field(texts.get("event_detail", f"{side}_side"), self.amount(amount))
                    )
            if (seconds := _valid(log, "duration_s")) is not None:
                lines.append(_field(texts.get("event_detail", "duration"), self.duration(seconds)))
        elif (
            log.log_type not in (CARE_LOG_DIAPER, CARE_LOG_BOTTLE)
            and (seconds := _valid(log, "duration_s")) is not None
        ):
            lines.append(_field(texts.get("event_detail", "duration"), self.duration(seconds)))
        if (logged_at := _moment(log.logged_at)) is not None:
            time = dt_util.as_local(logged_at).strftime("%Y-%m-%d %H:%M")
            lines.append(_phrase(texts.get("event_detail", "logged_at"), time))
        detail = row.detail
        if show_notes and detail is not None:
            if detail.comment:
                lines.append(_field(texts.get("event_detail", "note"), detail.comment))
            if detail.user_name:
                name = detail.user_name
                if detail.user_relation:
                    name = _with_relation(name, detail.user_relation)
                lines.append(_phrase(texts.get("event_detail", "logged_by"), name))
        return "\n".join(lines) if lines else None


def _moment(timestamp: float | None) -> datetime | None:
    """Return a Unix time as an aware UTC datetime, or None when it isn't usable."""
    if not isinstance(timestamp, int | float) or not math.isfinite(timestamp):
        return None
    try:
        return dt_util.utc_from_timestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None


def _wall_clock(moment: datetime) -> datetime:
    """Return what Home Assistant's local clock reads at a moment, as a naive time."""
    return dt_util.as_local(moment).replace(tzinfo=None)


def _clock_change(start: datetime, end: datetime) -> datetime:
    """Return the first instant after start, up to end, with another UTC offset.

    The local offset at end must differ from the one at start. Found by
    halving, to the microsecond, on UTC instants.
    """
    offset = dt_util.as_local(start).utcoffset()
    low, high = start, end
    while high - low > _MICROSECOND:
        middle = low + (high - low) / 2
        if dt_util.as_local(middle).utcoffset() == offset:
            low = middle
        else:
            high = middle
    return high


def _moment_end(start: datetime) -> datetime:
    """Return where a moment's nominal length ends: a minute on, or sooner.

    Worked out on UTC instants. The minute is cut short at the next local
    midnight, so the moment never reaches into the next day, and just
    before the clocks go back, since Home Assistant checks an event's span
    on the local wall clock, where the end would read before the start. Its
    length is more than zero and at most a minute.
    """
    try:
        end = start + CARE_LOG_MOMENT_LENGTH
        next_day = dt_util.as_local(start).date() + timedelta(days=1)
    except OverflowError:
        return start
    midnight = dt_util.as_utc(dt_util.start_of_local_day(next_day))
    if start < midnight < end:
        end = midnight
    if _wall_clock(end) <= _wall_clock(start):
        end = max(start, _clock_change(start, end) - _MICROSECOND)
    return end


def _span(log: CareLog) -> tuple[datetime, datetime, bool] | None:
    """Return when the entry starts and ends (UTC) and whether it is a moment.

    None when its start is unusable.
    """
    start = _moment(log.timestamp)
    if start is None:
        return None
    seconds = _length(log)
    try:
        if seconds is None:
            return start, _moment_end(start), True
        return start, start + timedelta(seconds=seconds), False
    except OverflowError:
        return start, start, True


def _overlaps(
    start: datetime, end: datetime, moment: bool, range_start: datetime, range_end: datetime
) -> bool:
    """Return whether an event falls in the half-open range [range_start, range_end).

    A moment counts when it starts at or after the start and before the end,
    whatever its nominal length; an interval counts when any part of it lies
    inside.
    """
    if moment or end <= start:
        return range_start <= start < range_end
    return start < range_end and end > range_start


def _event(
    start: datetime, end: datetime, moment: bool, summary: str, description: str | None
) -> tuple[CalendarEvent, datetime, bool] | None:
    """Build the event in local time from UTC instants, as a moment if the span is rejected.

    Home Assistant checks the span on local wall-clock times, so an entry
    across a repeated hour when the clocks go back can look reversed. It is
    then shown as a moment at its start rather than dropped. Returns the
    event with the end (UTC) it is shown with, and whether it is shown as a
    moment, so a range can be checked against what is shown.
    """
    for event_end, shown_as_moment in ((end, moment), (_moment_end(start), True), (start, True)):
        try:
            event = CalendarEvent(
                start=dt_util.as_local(start),
                end=dt_util.as_local(event_end),
                summary=summary,
                description=description,
            )
        except HomeAssistantError:
            continue
        return event, event_end, shown_as_moment
    return None


def _fetch_bounds() -> tuple[int, int]:
    """Return the first and the last Unix second a fetch may ask Nanit for."""
    today = dt_util.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(CARE_LOG_EARLIEST.timestamp()), int((today + CARE_LOG_AHEAD).timestamp())


def _pieces(start: int, end: int) -> Iterator[tuple[int, int]]:
    """Split an inclusive range into requests of at most CARE_LOG_MAX_PIECE, in order."""
    step = int(CARE_LOG_MAX_PIECE.total_seconds())
    low = start
    while True:
        high = min(low + step, end)
        yield low, high
        if high >= end:
            return
        # Nanit includes both ends, so neighbouring pieces share one second;
        # an entry at it is returned twice and removed by its uid.
        low = high


def _asked(rows: Iterable[_Row], low: int, high: int) -> list[_Row]:
    """Keep the rows of one request that are dated inside its inclusive bounds.

    Every kind: a row dated outside is not what was asked for.
    """
    return [row for row in rows if low <= row.log.timestamp <= high]


def _unique(rows: Iterable[_Row]) -> tuple[_Row, ...]:
    """Keep one row per entry uid, sleep kinds left out.

    Each piece's rows are first checked against its bounds (see _asked), so
    a copy dated outside the request that returned it never displaces one
    inside.
    """
    unique: dict[str, _Row] = {}
    for row in rows:
        if row.log.log_type in CARE_LOG_SLEEP_KINDS:
            continue
        current = unique.get(row.log.uid)
        if current is None or row.log.timestamp > current.log.timestamp:
            unique[row.log.uid] = row
    return tuple(unique.values())


class NanitCareLogCalendar(CoordinatorEntity[NanitCareLogCoordinator], CalendarEntity):
    """A read-only calendar of one baby's care log, fetched on demand."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanitCareLogCoordinator,
        *,
        entry_id: str,
        camera_uid: str | None,
    ) -> None:
        """Initialize the calendar for the reader's baby."""
        super().__init__(coordinator)
        baby = coordinator.baby
        # Per config entry and baby: two sign-ins that see the same baby get
        # two calendars, each with its own credentials, option and cache.
        # The baby is named by its code, so the raw uid never reaches the
        # entity registry.
        self._attr_unique_id = f"{entry_id}_{baby_code(baby.uid)}_care_log_calendar"
        self._camera_uid = camera_uid
        if camera_uid:
            self._attr_translation_key = "care_log"
        else:
            # No device to carry the baby's name, so the entity name does.
            self._attr_translation_key = "care_log_baby"
            self._attr_translation_placeholders = {
                "baby_name": baby_display_name(baby.name, baby.uid)
            }
        self._cache: dict[_CacheKey, _Cached] = {}
        self._in_flight: dict[_CacheKey, asyncio.Task[tuple[_Row, ...]]] = {}
        # How many callers wait for each fetch in flight.
        self._waiting: dict[_CacheKey, int] = {}
        self._requests = asyncio.Semaphore(CARE_LOG_MAX_IN_FLIGHT)
        # The reader's refusal count when the answers kept were fetched.
        self._kept_refusals = coordinator.refusals
        self._removed = False
        self._cancel_expiry: CALLBACK_TYPE | None = None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the camera's device when the baby has a camera that was set up."""
        if not self._camera_uid:
            return None
        baby = self.coordinator.baby
        return DeviceInfo(
            identifiers={(DOMAIN, self._camera_uid)},
            name=display_name(baby.name, baby.uid),
            manufacturer="Nanit",
        )

    @property
    def available(self) -> bool:
        """Return whether the baby's care log reader has succeeded at least once."""
        return self.coordinator.data is not None

    @property
    def event(self) -> CalendarEvent | None:
        """Return no current event, ever.

        Deliberate: the state stays "off", so nothing from an entry reaches
        the entity's state, its attributes or Home Assistant's history.
        """
        return None

    async def async_will_remove_from_hass(self) -> None:
        """Drop everything kept in memory and stop fetches still running."""
        self._removed = True
        self._cache.clear()
        self._stop_expiry()
        for task in self._in_flight.values():
            task.cancel()
        self._in_flight.clear()
        self._waiting.clear()
        await super().async_will_remove_from_hass()

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the care entries that fall in [start_date, end_date)."""
        entry = self.coordinator.config_entry
        show_notes = is_show_care_log_notes(entry)
        # The range asked for is checked against where care logs can exist
        # before the reach-back is taken off its start: 24 hours before a
        # start early in the year 1 is no date at all.
        range_start = math.floor(start_date.timestamp())
        fetch_end = math.ceil(end_date.timestamp())
        earliest, latest = _fetch_bounds()
        if fetch_end < earliest or range_start > latest:
            return []
        fetch_start = earliest
        if range_start >= earliest:
            # Elapsed time, on UTC: on a day the clocks change, 24 hours back
            # on the local clock would be 23 or 25 real ones.
            reach_back = dt_util.as_utc(start_date) - CARE_LOG_REACH_BACK
            fetch_start = max(math.floor(reach_back.timestamp()), earliest)
        fetch_end = min(fetch_end, latest)
        refusals = self.coordinator.refusals
        rows = await self._async_rows((fetch_start, max(fetch_start, fetch_end), show_notes))
        texts = await _async_texts(hass)

        formatter = _Formatter(hass, texts)
        found: list[tuple[datetime, str, CalendarEvent]] = []
        for index, row in enumerate(rows):
            if index and not index % CARE_LOG_ROWS_PER_TURN:
                # A long range has thousands of rows: let the loop run between
                # batches rather than hold it for the whole answer.
                await asyncio.sleep(0)
            if (span := _span(row.log)) is None:
                continue
            start, end, moment = span
            if not _overlaps(start, end, moment, start_date, end_date):
                continue
            built = _event(
                start,
                end,
                moment,
                formatter.summary(row.log),
                formatter.description(row, show_notes),
            )
            if built is None:
                continue
            event, shown_end, shown_as_moment = built
            # Checked again against the span the event is shown with: one
            # shortened across the clocks going back can fall outside the range.
            if _overlaps(start, shown_end, shown_as_moment, start_date, end_date):
                found.append((start, row.log.uid, event))
        # The option may have changed, the calendar gone, or Nanit refused
        # this baby's care logs, while the answer was on its way or being
        # built. Checked after the last await, so nothing can change between
        # this and the answer being returned.
        if (
            self._removed
            or is_show_care_log_notes(entry) != show_notes
            or self.coordinator.refusals != refusals
        ):
            raise _unavailable()
        found.sort(key=lambda item: (item[0], item[1]))
        return [event for _start, _uid, event in found]

    async def _async_rows(self, key: _CacheKey) -> tuple[_Row, ...]:
        """Return a fetched range from memory, a fetch already running, or a new fetch."""
        if self._kept_refusals != self.coordinator.refusals:
            # Nanit refused this baby's care logs since these were kept: none
            # of them is shown again. Throttling keeps them (they stay their
            # five minutes). Nor is a fetch started before the refusal shared,
            # even one that has finished but not yet been forgotten.
            self._cache.clear()
            for stale in self._in_flight.values():
                stale.cancel()
            self._in_flight.clear()
            self._waiting.clear()
            self._stop_expiry()
            self._kept_refusals = self.coordinator.refusals
        cached = self._cache.get(key)
        if cached is not None and self._age(cached) < _CACHE_TTL_SECONDS:
            return cached.rows
        task = self._in_flight.get(key)
        if task is None:
            if len(self._in_flight) >= CARE_LOG_MAX_RANGES_IN_FLIGHT:
                raise _unavailable()
            task = self.coordinator.config_entry.async_create_background_task(
                self.hass, self._async_fetch_and_keep(key), f"{DOMAIN} care log calendar fetch"
            )
            self._in_flight[key] = task
            task.add_done_callback(lambda done: self._fetch_done(key, done))
        self._waiting[key] = self._waiting.get(key, 0) + 1
        failed = False
        rows: tuple[_Row, ...] = ()
        try:
            # Shielded: a caller giving up doesn't cancel the fetch others share.
            rows = await asyncio.shield(task)
        except HomeAssistantError:
            failed = True
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not task.cancelled() or (current is not None and current.cancelling()):
                raise
            # The fetch was stopped because the calendar was removed.
            failed = True
        finally:
            self._stop_waiting(key, task)
        if failed:
            # Raised here, outside the handler, so nothing of the failure is chained.
            raise _unavailable()
        return rows

    def _stop_waiting(self, key: _CacheKey, task: asyncio.Task[tuple[_Row, ...]]) -> None:
        """Count a caller out, and stop the fetch when nobody waits for it any more."""
        if self._in_flight.get(key) is not task:
            return
        waiting = self._waiting.get(key, 0) - 1
        if waiting > 0:
            self._waiting[key] = waiting
            return
        self._waiting.pop(key, None)
        if not task.done():
            # Every caller gave up. Forgotten at once, so a caller arriving
            # next starts a fetch of its own instead of sharing a stopped one.
            del self._in_flight[key]
            task.cancel()

    def _fetch_done(self, key: _CacheKey, task: asyncio.Task[tuple[_Row, ...]]) -> None:
        if self._in_flight.get(key) is task:
            del self._in_flight[key]
            self._waiting.pop(key, None)
        if not task.cancelled():
            # Marks a failure as seen when every caller has already gone.
            task.exception()

    async def _async_fetch_and_keep(self, key: _CacheKey) -> tuple[_Row, ...]:
        """Fetch a range and keep it in memory, unless the calendar is gone.

        A fetch during which Nanit refused this baby's care logs (to this or
        another request) fails: what it got is neither kept nor returned.
        """
        refusals = self.coordinator.refusals
        rows = await self._async_fetch(*key)
        if self.coordinator.refusals != refusals:
            raise _unavailable()
        if not self._removed and len(rows) <= CARE_LOG_CACHE_MAX_ROWS:
            self._cache[key] = _Cached(rows, self.hass.loop.time())
            # Oldest first, until both the ranges and their rows fit.
            while (
                len(self._cache) > CARE_LOG_CACHE_SIZE
                or sum(len(cached.rows) for cached in self._cache.values())
                > CARE_LOG_CACHE_MAX_ROWS
            ):
                oldest = min(self._cache, key=lambda k: self._cache[k].fetched_at)
                del self._cache[oldest]
            self._schedule_expiry()
        return rows

    def _schedule_expiry(self) -> None:
        """Set the timer for when the oldest answer kept runs out, or stop it if none is."""
        self._stop_expiry()
        if not self._cache:
            return
        oldest = max(self._age(cached) for cached in self._cache.values())
        self._cancel_expiry = async_call_later(
            self.hass, max(_CACHE_TTL_SECONDS - oldest, 0.0), self._async_expire
        )

    def _age(self, cached: _Cached) -> float:
        """Return how many seconds ago an answer was fetched, on a clock that never goes back.

        The loop's monotonic clock, not the wall clock: setting the clock back
        must not keep an answer (a note among them) past its five minutes.
        """
        return self.hass.loop.time() - cached.fetched_at

    def _stop_expiry(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _async_expire(self, _now: datetime) -> None:
        """Drop every answer whose time is up, then wait for the next one."""
        self._cancel_expiry = None
        for key in [
            k for k, cached in self._cache.items() if self._age(cached) >= _CACHE_TTL_SECONDS
        ]:
            del self._cache[key]
        self._schedule_expiry()

    async def _async_fetch(self, start: int, end: int, show_notes: bool) -> tuple[_Row, ...]:
        """Fetch an inclusive range from Nanit, in pieces."""
        coordinator = self.coordinator
        client = coordinator.config_entry.runtime_data.hub.client
        baby_uid = coordinator.baby.uid

        async def piece(low: int, high: int) -> list[_Row]:
            # Through the reader's hold once a permit is had, just before the
            # request: a piece that waited is held back by a refusal meanwhile.
            async with self._requests:
                if show_notes:
                    detail_fetch = await coordinator.async_request(
                        lambda: client.async_get_calendar_details(baby_uid, low, high)
                    )
                    fetched = [_Row(_stripped(e.log), e.detail) for e in detail_fetch.entries]
                else:
                    fetch = await coordinator.async_request(
                        lambda: client.async_get_calendar_result(baby_uid, low, high)
                    )
                    fetched = [_Row(_stripped(log), None) for log in fetch.logs]
            # Checked here, against this piece's own bounds, before a copy
            # is chosen per uid across the pieces.
            return _asked(fetched, low, high)

        pieces = _pieces(start, end)
        rows: list[_Row] = []

        async def worker() -> None:
            # The workers share one iterator, so each piece is fetched once.
            for low, high in pieces:
                rows.extend(await piece(low, high))

        failure: str | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(CARE_LOG_MAX_IN_FLIGHT):
                    group.create_task(worker())
        except* NanitError as errors:
            # The first error stops every piece still to come; a refusal or
            # throttling also holds every request back (async_request).
            failure = type(errors.exceptions[0]).__name__
        if failure is not None:
            # Only the error's type: the library's text is fixed, but nothing
            # here needs more, and the baby's uid never goes in a log line.
            _LOGGER.debug("Care log calendar fetch failed (%s)", failure)
            raise _unavailable()
        return _unique(rows)


def _stripped(log: CareLog) -> CareLog:
    """Return the entry without the baby's uid, which nothing here needs."""
    return dataclasses.replace(log, baby_uid="")


def _unavailable() -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key="care_log_calendar_unavailable"
    )


async def _async_texts(hass: HomeAssistant) -> _Texts:
    """Return the event text in Home Assistant's language, English where it has none."""
    try:
        strings = await async_get_translations(hass, hass.config.language, "entity", {DOMAIN})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Care log calendar text could not be loaded; using its keys")
        strings = {}
    return _Texts(strings)
