"""Coordinators for the Nanit integration.

NanitPushCoordinator: Push-based coordinator that wraps NanitCamera.subscribe().
    Fires async_set_updated_data on every CameraEvent callback (sensor, settings,
    control, status, connection changes). No polling — all data arrives via
    WebSocket push.

    Entity availability uses a grace period so that brief reconnections (e.g.,
    pre-emptive token refresh) do not surface as "Unavailable" in HA.

NanitCloudCoordinator: Polls the Nanit cloud API for motion/sound events every
    CLOUD_POLL_INTERVAL seconds.

NanitCareLogCoordinator: Polls a baby's Care Logs (diaper changes, feedings,
    pumping) every CARE_LOG_POLL_INTERVAL seconds.

NanitSoundLightCoordinator: Push-based coordinator wrapping NanitSoundLight.subscribe().
    Receives state updates from the S&L device's local WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from aionanit.exceptions import (
    NanitAuthError,
    NanitConnectionError,
    NanitRateLimited,
    NanitRequestRefused,
    NanitResponseTooLarge,
)
from aionanit.models import Baby, CameraEvent, CameraState, CareLog, CloudEvent, NetworkInfo

from .aionanit_sl.models import SoundLightEvent, SoundLightEventKind, SoundLightFullState
from .aionanit_sl.sound_light import NanitSoundLight
from .baby_code import baby_code
from .const import (
    CARE_LOG_POLL_INTERVAL,
    CARE_LOG_SEED_DAYS,
    CLOUD_POLL_INTERVAL,
    DOMAIN,
    NETWORK_POLL_INTERVAL,
)

if TYPE_CHECKING:
    from aionanit import NanitCamera

    from . import NanitConfigEntry
    from .hub import NanitHub

_LOGGER = logging.getLogger(__name__)

# How long to wait before marking entities unavailable after a disconnect.
# If the WebSocket reconnects within this window, entities never go unavailable.
_AVAILABILITY_GRACE_SECONDS: float = 30.0

# Waits before each retry of a cloud poll that failed to connect. Nanit's REST
# API answers a single request with HTTP 5xx or times out several times a day;
# retrying a few seconds later gets fresh data, and the entities stay available
# meanwhile. Setup gets no retries, so it still fails fast.
_POLL_RETRY_DELAYS: tuple[float, ...] = (5.0, 10.0)

# Failures a retry seconds later can't fix: Nanit refused the request, is
# throttling them, or answered with more than the library reads. They end
# the poll at once.
_NOT_RETRIED: tuple[type[NanitConnectionError], ...] = (NanitRequestRefused, NanitResponseTooLarge)


async def _async_fetch_with_retries[T](
    coordinator: DataUpdateCoordinator[Any], fetch: Callable[[], Awaitable[T]]
) -> T:
    """Return ``fetch()``, retrying a NanitConnectionError except during setup.

    A refusal or throttling (``_NOT_RETRIED``) is never retried.
    """
    entry = coordinator.config_entry
    in_setup = entry is not None and entry.state is ConfigEntryState.SETUP_IN_PROGRESS
    delays = () if in_setup else _POLL_RETRY_DELAYS
    for delay in delays:
        try:
            return await fetch()
        except _NOT_RETRIED:
            raise
        except NanitConnectionError as err:
            _LOGGER.debug("%s poll failed, retrying in %.0fs: %s", coordinator.name, delay, err)
            await asyncio.sleep(delay)
    return await fetch()


class NanitPushCoordinator(DataUpdateCoordinator[CameraState]):
    """Push-based coordinator that receives state updates from NanitCamera.subscribe().

    No polling is configured — async_set_updated_data() is called by the camera
    callback on every state change. Entity availability is driven by the
    ``connected`` flag which tracks the WebSocket connection state, debounced
    by a grace period so brief reconnections don't flash "Unavailable".
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        camera: NanitCamera,
        baby: Baby,
    ) -> None:
        """Initialize the push coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{camera.uid}",
        )
        self.camera = camera
        self.baby = baby
        self.connected: bool = False
        self._unsubscribe: Callable[[], None] | None = None
        self._availability_timer: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Start the camera and subscribe to push events.

        Called once from async_setup_entry after the coordinator is created.
        """
        self._unsubscribe = self.camera.subscribe(self._on_camera_event)
        await self.camera.async_start()
        self.connected = self.camera.connected
        self.async_set_updated_data(self.camera.state)

    @callback
    def _on_camera_event(self, event: CameraEvent) -> None:
        """Handle a push event from NanitCamera.subscribe()."""
        transport_connected = self.camera.connected

        if transport_connected:
            # Connection is up — cancel any pending unavailability timer
            # and mark connected immediately.
            self._cancel_availability_timer()
            if not self.connected:
                _LOGGER.info("Camera %s reconnected", self.camera.uid)
            self.connected = True
        elif self.connected:
            # Connection just dropped — start the grace period.
            # Don't mark unavailable yet; give the transport time to reconnect.
            _LOGGER.debug(
                "Camera %s disconnected (grace period %.0fs): %s",
                self.camera.uid,
                _AVAILABILITY_GRACE_SECONDS,
                event.state.connection.last_error,
            )
            self._start_availability_timer()
        # If already disconnected (self.connected is False) and transport is
        # still disconnected, do nothing — timer is already running or fired.

        self.async_set_updated_data(event.state)

    @callback
    def _on_availability_timeout(self, _now: object) -> None:
        """Grace period expired — mark entities unavailable."""
        self._availability_timer = None
        if not self.camera.connected:
            _LOGGER.warning(
                "Camera %s still disconnected after %.0fs grace period",
                self.camera.uid,
                _AVAILABILITY_GRACE_SECONDS,
            )
            self.connected = False
            self.async_update_listeners()

    def _start_availability_timer(self) -> None:
        """Start (or restart) the grace period timer."""
        self._cancel_availability_timer()
        self._availability_timer = async_call_later(
            self.hass, _AVAILABILITY_GRACE_SECONDS, self._on_availability_timeout
        )

    def _cancel_availability_timer(self) -> None:
        """Cancel the grace period timer if running."""
        if self._availability_timer is not None:
            self._availability_timer()
            self._availability_timer = None

    async def async_shutdown(self) -> None:
        """Stop the camera and unsubscribe."""
        self._cancel_availability_timer()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.camera.async_stop()
        await super().async_shutdown()


class NanitCloudCoordinator(DataUpdateCoordinator[list[CloudEvent]]):
    """Polling coordinator for Nanit cloud motion/sound events.

    Polls GET /babies/{uid}/messages every CLOUD_POLL_INTERVAL seconds.
    Entities check event timestamps against a window to determine on/off state.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        hub: NanitHub,
        baby: Baby,
    ) -> None:
        """Initialize the cloud coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{baby.uid}_cloud",
            update_interval=timedelta(seconds=CLOUD_POLL_INTERVAL),
        )
        self._hub = hub
        self.baby = baby

    async def _async_update_data(self) -> list[CloudEvent]:
        """Fetch cloud events from the Nanit API."""
        try:
            client = self._hub.client
            return await _async_fetch_with_retries(
                self, lambda: client.async_get_events(self.baby.uid)
            )
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except NanitConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class NanitNetworkCoordinator(DataUpdateCoordinator[NetworkInfo | None]):
    """Polling coordinator for camera WiFi network diagnostics.

    Polls GET /babies every NETWORK_POLL_INTERVAL seconds and extracts
    the network info for a single camera.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        hub: NanitHub,
        baby: Baby,
    ) -> None:
        """Initialize the network coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{baby.camera_uid}_network",
            update_interval=timedelta(seconds=NETWORK_POLL_INTERVAL),
        )
        self._hub = hub
        self.baby = baby

    async def _async_update_data(self) -> NetworkInfo | None:
        """Fetch network info from the Nanit API."""
        try:
            # The hub's tolerant fetch parses camera-less baby rows (mixed
            # accounts), including on published aionanit wheels whose strict
            # parser raises KeyError on them.
            babies = await _async_fetch_with_retries(self, self._hub.async_get_babies_tolerant)
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except NanitConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cloud_fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        for baby in babies:
            if baby.camera_uid == self.baby.camera_uid:
                return baby.network
        return None


# Care Log types the sensors summarise. The calendar also returns naps
# ("sleep") and the camera's own sleep sessions ("auto_sleep"), which no
# sensor uses yet.
CARE_LOG_DIAPER = "diaper_change"
CARE_LOG_BOTTLE = "bottle_feed"
CARE_LOG_NURSING = "nursing"
CARE_LOG_PUMPING = "pumping"
CARE_LOG_TYPES = frozenset({CARE_LOG_DIAPER, CARE_LOG_BOTTLE, CARE_LOG_NURSING, CARE_LOG_PUMPING})

# The fetch runs a little past now, so an entry logged for a few minutes
# ahead still counts.
_CARE_LOG_END_SLACK = timedelta(hours=1)

# How far back each poll after the first looks. Parents often log after the
# fact (a night feed entered in the morning), and an entry from before
# midnight still has to be seen, as do later edits and deletions of it.
_CARE_LOG_LOOKBACK = timedelta(hours=24)

# How long the reader waits after Nanit refuses its request (HTTP 403)
# before it asks again. A refusal is about the account (its role on the
# baby, or what Nanit offers it), which changes when a person changes it,
# not between two polls: hourly is 12 times fewer requests than the normal
# poll, and a permission given back still shows within the hour (at once
# on a reload).
CARE_LOG_REFUSED_RECHECK = timedelta(hours=1)

# After Nanit throttles a request (HTTP 429): the wait it asks for, and never
# less than the normal poll interval. With no wait given,
# CARE_LOG_THROTTLED_DEFAULT, three normal intervals.
CARE_LOG_THROTTLED_DEFAULT = timedelta(minutes=15)
# The longest the reader goes between two polls while requests are held: a
# longer wait is checked again hourly, and no request goes out before it ends.
CARE_LOG_HELD_POLL_MAX = timedelta(hours=1)


def care_log_hold(err: NanitRequestRefused) -> timedelta:
    """Return how long to wait before asking Nanit for a baby's Care Logs again."""
    normal = timedelta(seconds=CARE_LOG_POLL_INTERVAL)
    if not isinstance(err, NanitRateLimited):
        return CARE_LOG_REFUSED_RECHECK
    if err.retry_after is None:
        return CARE_LOG_THROTTLED_DEFAULT
    return max(timedelta(seconds=err.retry_after), normal)


@dataclass(frozen=True)
class CareLogRequestCoverage:
    """What the last successful Care Log fetch asked Nanit for.

    This is request coverage: the range that was asked for, not a promise
    that Nanit's answer held everything in it (its answer carries no sign
    of being complete). ``start`` and ``end`` are the Unix seconds sent,
    both inclusive, as Nanit matches an entry by its start. ``read_at`` is
    when the answer arrived, ``requested_at`` when the request was sent
    (taken just before it; None when not known), and ``dropped`` how many
    of its rows were skipped as malformed. ``outside`` counts the rows, of
    any kind, that were dated outside the range (``CareLogData`` leaves
    them out of every field).
    """

    start: int
    end: int
    read_at: datetime
    dropped: int = 0
    outside: int = 0
    requested_at: datetime | None = None


@dataclass(frozen=True)
class CareLogData:
    """A summary of one baby's Care Logs.

    ``latest`` holds the newest entry of each type and ``today`` every entry
    on the local day that starts at ``day_start`` (local midnight), oldest
    first. ``entries`` is every entry the last fetch returned, and
    ``before_window`` the newest entry of each type older than that fetch's
    start: the next summary needs both to know what came before its own,
    shorter, fetch. ``needs_seed`` is set when an entry from before the
    window turned up again inside it (its date was edited), so what came
    before it is no longer known and the next fetch has to look back as far
    as the first one did.

    The sensors use only the fields above, which hold the kinds in
    ``CARE_LOG_TYPES``. ``all_entries`` is the whole numeric answer of the
    last fetch, every kind (sleep included), each uid once, oldest first,
    with the range that fetch asked for in ``request_coverage``. Neither
    carries anything forward from earlier fetches. When the range is known,
    every row, of any kind, is checked against it before a copy is chosen
    per uid, for the sensors' fields and ``all_entries`` alike: one dated
    outside it is left out and counted in ``request_coverage.outside``, so
    it neither displaces a copy inside the range nor goes unnoticed.
    """

    latest: Mapping[str, CareLog]
    today: tuple[CareLog, ...]
    day_start: datetime
    entries: tuple[CareLog, ...] = ()
    before_window: Mapping[str, CareLog] = field(default_factory=dict)
    needs_seed: bool = False
    all_entries: tuple[CareLog, ...] = ()
    request_coverage: CareLogRequestCoverage | None = None


def _order(log: CareLog) -> tuple[float, str]:
    """Sort key for entries: by time, and by uid between entries at the same time."""
    return (log.timestamp, log.uid)


def _keep_newest(newest: dict[str, CareLog], log: CareLog) -> None:
    """Store ``log`` under its type unless a newer entry of that type is there."""
    current = newest.get(log.log_type)
    if current is None or _order(log) > _order(current):
        newest[log.log_type] = log


def _by_uid(logs: Iterable[CareLog]) -> dict[str, CareLog]:
    """Index entries by uid, so a row the API repeats counts only once."""
    unique: dict[str, CareLog] = {}
    for log in logs:
        current = unique.get(log.uid)
        if current is None or log.timestamp > current.timestamp:
            unique[log.uid] = log
    return unique


def build_care_log_data(
    logs: Iterable[CareLog],
    *,
    window_start: float,
    day_start: datetime,
    previous: CareLogData | None,
    request_coverage: CareLogRequestCoverage | None = None,
) -> CareLogData:
    """Summarise one calendar fetch covering ``window_start`` up to now.

    Later polls fetch a shorter window than the first, so a type with no
    entry since ``window_start`` falls back to the newest older entry an
    earlier fetch returned. An entry inside the window that the API no
    longer returns was deleted in the app, so it no longer counts.
    """
    logs = tuple(logs)
    if request_coverage is not None:
        # Every row is checked against the range asked for before one copy
        # per uid is chosen (see ``CareLogData``).
        low, high = request_coverage.start, request_coverage.end
        in_range = [log for log in logs if low <= log.timestamp <= high]
        if outside := len(logs) - len(in_range):
            request_coverage = replace(request_coverage, outside=outside)
        logs = tuple(in_range)
    fetched = _by_uid(logs)
    all_entries = tuple(sorted(fetched.values(), key=_order))
    entries = tuple(log for log in fetched.values() if log.log_type in CARE_LOG_TYPES)

    before_window: dict[str, CareLog] = {}
    needs_seed = False
    if previous is not None:
        for log in (*previous.before_window.values(), *previous.entries):
            if log.timestamp >= window_start:
                continue
            current = fetched.get(log.uid)
            if current is None:
                _keep_newest(before_window, log)
            elif current != log:
                # The same entry, edited since: the older copy is stale, and
                # must not come back if the entry is deleted later.
                needs_seed = True

    latest = dict(before_window)
    for log in entries:
        _keep_newest(latest, log)

    # Only the local day itself. The fetch runs a little past now, so close
    # to midnight it can return an entry dated just after it; that entry
    # belongs to tomorrow's totals and must not count in today's as well.
    # Adding a day to the local midnight keeps a 23 or 25 hour day whole.
    day_start_ts = day_start.timestamp()
    day_end_ts = (day_start + timedelta(days=1)).timestamp()
    today = tuple(
        sorted(
            (log for log in entries if day_start_ts <= log.timestamp < day_end_ts),
            key=_order,
        )
    )
    return CareLogData(
        latest=latest,
        today=today,
        day_start=day_start,
        entries=entries,
        before_window=before_window,
        needs_seed=needs_seed,
        all_entries=all_entries,
        request_coverage=request_coverage,
    )


class NanitCareLogCoordinator(DataUpdateCoordinator[CareLogData]):
    """Polling coordinator for a baby's Care Logs.

    Polls GET /babies/{uid}/calendar every CARE_LOG_POLL_INTERVAL seconds.
    The first poll looks back CARE_LOG_SEED_DAYS days to find the newest
    entry of each type; later polls fetch the last 24 hours (and at least
    since midnight) and carry older entries forward, which keeps each poll
    small. A poll looks back the full CARE_LOG_SEED_DAYS again when the last
    successful one is more than 24 hours old, so entries logged during an
    outage are not skipped, and when the last summary asks for it
    (``CareLogData.needs_seed``).
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        hub: NanitHub,
        baby: Baby,
    ) -> None:
        """Initialize the care log coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # Home Assistant prints the name in its debug lines: the baby's
            # code, never its raw uid.
            name=f"{DOMAIN}_{baby_code(baby.uid)}_care_logs",
            update_interval=timedelta(seconds=CARE_LOG_POLL_INTERVAL),
        )
        self._hub = hub
        self.baby = baby
        # When the last successful fetch started.
        self._last_fetch: datetime | None = None
        # The hold every Care Log request of this sign-in for this baby goes
        # through (``async_request``). Until when, on the loop's monotonic
        # clock, no request may be sent; None when one may.
        self._held_until: float | None = None
        # The newest refusal or throttling, until a request sent after it
        # succeeds.
        self._held_by: NanitRequestRefused | None = None
        # Every refusal or throttling the hold saw, and every refusal (403).
        self._holds = 0
        self._refusals = 0
        # A refusal (403) that no request sent after it has answered yet.
        self._refused = False
        # Whether the reader's own poll is under way.
        self._polling = False

    @property
    def requests_held(self) -> bool:
        """Return whether Nanit refused or throttled this baby's Care Logs a moment ago.

        For as long as ``care_log_hold`` says after the newest refusal or
        throttling, unless a request sent after it succeeded.
        """
        return self._held_until is not None and self.hass.loop.time() < self._held_until

    @property
    def refusals(self) -> int:
        """Return how many refusals (HTTP 403) this sign-in got for this baby so far.

        A calendar compares it before and after a fetch: one that changed
        means what it fetched or kept may no longer be shown.
        """
        return self._refusals

    @property
    def refused(self) -> bool:
        """Return whether Nanit refused (HTTP 403) a request no later one has succeeded since."""
        return self._refused

    async def async_request[T](self, request: Callable[[], Awaitable[T]]) -> T:
        """Send one Care Log request of this sign-in for this baby, unless requests are held.

        Every request for them goes through here just before it is sent: the
        reader's polls and their retries, each calendar piece and each of
        the statistics importer's own. While requests are held it raises the
        refusal or throttling again, asking Nanit nothing. A refusal or
        throttling a request gets holds every request (``care_log_hold``),
        and a success clears the hold only when no refusal or throttling
        arrived while it was on its way: an older request finishing late
        leaves a newer hold in place. One that a refusal (not a throttling)
        arrived during raises that refusal instead of returning its answer.
        """
        if self.requests_held and self._held_by is not None:
            held = self._held_by
            if isinstance(held, NanitRateLimited):
                raise NanitRateLimited("Calendar fetch", held.retry_after)
            raise NanitRequestRefused("Calendar fetch", held.status)
        holds = self._holds
        refusals = self._refusals
        try:
            result = await request()
        except NanitRequestRefused as err:
            self._hold(err)
            raise
        if self._refusals != refusals:
            # Nanit refused another request while this one was on its way:
            # its answer reaches nobody, as if it had been refused too.
            del result
            raise NanitRequestRefused("Calendar fetch", 403)
        if self._holds == holds:
            self._held_until = None
            self._held_by = None
            self._refused = False
        return result

    def _hold(self, err: NanitRequestRefused) -> None:
        """Hold every request after a refusal or throttling, for as long as it asks."""
        until = self.hass.loop.time() + care_log_hold(err).total_seconds()
        if self._held_until is None or until > self._held_until:
            self._held_until = until
        self._held_by = err
        self._holds += 1
        if not isinstance(err, NanitRateLimited):
            self._refusals += 1
            self._refused = True
            if not self._polling:
                # Another caller was refused: the care sensors say so now,
                # not at the next poll (a poll under way reports it itself).
                self.async_set_update_error(
                    UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="care_log_fetch_failed",
                        translation_placeholders={"error": "Calendar fetch failed with HTTP 403"},
                    )
                )

    def _held_poll_interval(self) -> timedelta:
        """Return when the reader polls next while requests are held."""
        normal = timedelta(seconds=CARE_LOG_POLL_INTERVAL)
        if self._held_until is None:
            return normal
        left = timedelta(seconds=self._held_until - self.hass.loop.time())
        return min(max(left, normal), CARE_LOG_HELD_POLL_MAX)

    async def _async_update_data(self) -> CareLogData:
        """Fetch Care Logs from the Nanit API and summarise them."""
        now = dt_util.utcnow()
        day_start = dt_util.start_of_local_day()
        seed = (
            self.data is None
            or self.data.needs_seed
            or self._last_fetch is None
            or now - self._last_fetch > _CARE_LOG_LOOKBACK
        )
        lookback = timedelta(days=CARE_LOG_SEED_DAYS) if seed else _CARE_LOG_LOOKBACK
        window_start = min(day_start, now - lookback)
        start = int(window_start.timestamp())
        end = int((now + _CARE_LOG_END_SLACK).timestamp())
        # Just before the request (and its retries): an hour that had not
        # finished by then may gain entries the answer lacks.
        requested_at = dt_util.utcnow()
        self._polling = True
        try:
            client = self._hub.client
            fetch = await _async_fetch_with_retries(
                self,
                lambda: self.async_request(
                    lambda: client.async_get_calendar_result(self.baby.uid, start, end)
                ),
            )
        except NanitAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except NanitRequestRefused as err:
            # Not retried, and the next poll waits while requests are held
            # (async_request): asking again in five minutes would get the
            # same answer. Reported like any failure.
            self.update_interval = self._held_poll_interval()
            _LOGGER.debug(
                "%s: Nanit answered HTTP %s, next poll in %.0fs",
                self.name,
                err.status,
                self.update_interval.total_seconds(),
            )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="care_log_fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except NanitConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="care_log_fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            self._polling = False

        self.update_interval = timedelta(seconds=CARE_LOG_POLL_INTERVAL)
        self._last_fetch = now
        coverage = CareLogRequestCoverage(
            start=fetch.start,
            end=fetch.end,
            read_at=dt_util.utcnow(),
            dropped=fetch.dropped,
            requested_at=requested_at,
        )
        return build_care_log_data(
            fetch.logs,
            window_start=start,
            day_start=day_start,
            previous=self.data,
            request_coverage=coverage,
        )


_SL_STORE_VERSION = 1
# Fields from SoundLightFullState that we persist across restarts.
_SL_PERSIST_FIELDS = (
    "brightness",
    "light_enabled",
    "clock_enabled",
    "clock_brightness",
    "color_r",
    "color_g",
    "sound_on",
    "current_track",
    "volume",
    "power_on",
    "temperature_c",
    "humidity_pct",
)

# Fields that must be float in [0.0, 1.0].
_UNIT_FLOAT_FIELDS = frozenset({"brightness", "color_r", "color_g", "volume"})
# Fields that must be bool.
_BOOL_FIELDS = frozenset({"light_enabled", "sound_on", "power_on", "clock_enabled"})
# Fields that must be finite float (no range constraint).
_FINITE_FLOAT_FIELDS = frozenset({"temperature_c", "humidity_pct"})


def _clamp_restored_value(field: str, value: Any) -> Any:
    """Validate/clamp a restored value, returning ``None`` if invalid."""
    if field in _UNIT_FLOAT_FIELDS:
        if not isinstance(value, int | float):
            return None
        fval = float(value)
        if not math.isfinite(fval):
            return None
        return max(0.0, min(1.0, fval))

    if field in _BOOL_FIELDS:
        return value if isinstance(value, bool) else None

    if field == "clock_brightness":
        return value if type(value) is int and 0 <= value <= 8 else None

    if field == "current_track":
        return value if isinstance(value, str) else None

    if field in _FINITE_FLOAT_FIELDS:
        if not isinstance(value, int | float):
            return None
        fval = float(value)
        return fval if math.isfinite(fval) else None

    return None


class NanitSoundLightCoordinator(DataUpdateCoordinator[SoundLightFullState]):
    """Push-based coordinator for the Nanit Sound & Light Machine.

    Wraps NanitSoundLight.subscribe() — receives state updates from
    the S&L device via WebSocket (cloud relay or local).
    No polling — all state is pushed by the device.

    Persists the last known state to HA storage so that entities show
    their previous values on restart (instead of "unknown") until the
    first live update arrives from the device.

    Uses a grace period for disconnections so brief reconnections
    (e.g. during hourly access-token refresh) do not flash entities
    as "Unavailable" in HA.
    """

    config_entry: NanitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        sound_light: NanitSoundLight,
        baby: Baby,
        via_camera_uid: str | None = None,
    ) -> None:
        """Initialize the Sound & Light coordinator.

        via_camera_uid links the S&L device to the baby's camera device in
        the registry; None when the baby has no camera (standalone speaker).
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{sound_light.speaker_uid}_sound_light",
        )
        self.sound_light = sound_light
        self.baby = baby
        self.via_camera_uid = via_camera_uid
        self._unsubscribe: Callable[[], None] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _SL_STORE_VERSION,
            f"{DOMAIN}_sl_state_{sound_light.speaker_uid}",
        )
        self._sl_connected: bool = False
        self._availability_timer: CALLBACK_TYPE | None = None
        self._save_timer: CALLBACK_TYPE | None = None
        self._pending_save_state: SoundLightFullState | None = None

    @property
    def connected(self) -> bool:
        """Return debounced connection state (survives brief reconnections)."""
        return self._sl_connected

    async def async_setup(self) -> None:
        """Start the S&L device and subscribe to push events."""
        self._unsubscribe = self.sound_light.subscribe(self._on_sl_event)
        await self.sound_light.async_start()
        self._sl_connected = self.sound_light.connected

        # If the device hasn't sent initial state yet (cloud relay),
        # restore the last known state from disk so entities aren't "unknown".
        state = self.sound_light.state
        if state.power_on is None:
            restored = await self._async_restore_state()
            if restored is not None:
                # Feed restored state into the sound_light instance so
                # entities and coordinator data are consistent.
                self.sound_light.restore_state(restored)
                state = restored
                _LOGGER.debug(
                    "S&L %s: restored saved state (power=%s, track=%s, vol=%s)",
                    self.sound_light.speaker_uid,
                    restored.power_on,
                    restored.current_track,
                    restored.volume,
                )

        self.async_set_updated_data(state)

    async def _async_restore_state(self) -> SoundLightFullState | None:
        """Load persisted S&L state from HA storage."""
        try:
            data = await self._store.async_load()
            if not data or not isinstance(data, dict):
                return None
            kwargs = {}
            for field in _SL_PERSIST_FIELDS:
                if field in data and data[field] is not None:
                    value = data[field]
                    clamped = _clamp_restored_value(field, value)
                    if clamped is not None:
                        kwargs[field] = clamped
            if not kwargs:
                return None
            if data.get("available_tracks"):
                tracks = data["available_tracks"]
                if isinstance(tracks, list) and all(isinstance(t, str) for t in tracks):
                    kwargs["available_tracks"] = tuple(tracks)
            return SoundLightFullState(**kwargs)
        except Exception:
            _LOGGER.debug("Failed to restore S&L state", exc_info=True)
            return None

    async def _async_save_state(self, state: SoundLightFullState) -> None:
        """Persist current S&L state to HA storage."""
        try:
            data = {}
            for field in _SL_PERSIST_FIELDS:
                val = getattr(state, field, None)
                if val is not None:
                    data[field] = val
            # Also save available_tracks
            if state.available_tracks:
                data["available_tracks"] = list(state.available_tracks)
            if data:
                await self._store.async_save(data)
        except Exception:
            _LOGGER.debug("Failed to save S&L state", exc_info=True)

    @callback
    def _on_sl_event(self, event: SoundLightEvent) -> None:
        """Handle a push event from NanitSoundLight.subscribe()."""
        if event.kind == SoundLightEventKind.CONNECTION_CHANGE:
            transport_connected = self.sound_light.connected
            if transport_connected:
                # Connection is up — cancel any pending unavailability timer
                # and mark connected immediately.
                self._cancel_availability_timer()
                if not self._sl_connected:
                    _LOGGER.info(
                        "S&L %s reconnected",
                        self.sound_light.speaker_uid,
                    )
                self._sl_connected = True
            elif self._sl_connected:
                # Connection just dropped — start the grace period.
                # Don't mark unavailable yet; give the transport time to reconnect.
                _LOGGER.debug(
                    "S&L %s disconnected (grace period %.0fs)",
                    self.sound_light.speaker_uid,
                    _AVAILABILITY_GRACE_SECONDS,
                )
                self._start_availability_timer()
            # If already disconnected and transport still disconnected,
            # do nothing — timer is already running or fired.

        self.async_set_updated_data(event.state)

        # Debounce state saves — at most every 5 seconds to avoid
        # overlapping writes from rapid state/sensor updates.
        if event.kind in (
            SoundLightEventKind.STATE_UPDATE,
            SoundLightEventKind.SENSOR_UPDATE,
        ):
            self._schedule_save(event.state)

    @callback
    def _schedule_save(self, state: SoundLightFullState) -> None:
        """Schedule a debounced state save (at most every 5 seconds)."""
        self._pending_save_state = state
        if self._save_timer is not None:
            # Timer already running — it will pick up the latest state
            return
        self._save_timer = async_call_later(self.hass, 5, self._do_save)

    @callback
    def _do_save(self, _now: object) -> None:
        """Execute the debounced state save."""
        self._save_timer = None
        if self._pending_save_state is not None:
            state = self._pending_save_state
            self._pending_save_state = None
            self.hass.async_create_task(self._async_save_state(state))

    @callback
    def _on_availability_timeout(self, _now: object) -> None:
        """Grace period expired — mark S&L entities unavailable."""
        self._availability_timer = None
        if not self.sound_light.connected:
            _LOGGER.warning(
                "S&L %s still disconnected after %.0fs grace period",
                self.sound_light.speaker_uid,
                _AVAILABILITY_GRACE_SECONDS,
            )
            self._sl_connected = False
            self.async_update_listeners()

    def _start_availability_timer(self) -> None:
        """Start (or restart) the grace period timer."""
        self._cancel_availability_timer()
        self._availability_timer = async_call_later(
            self.hass, _AVAILABILITY_GRACE_SECONDS, self._on_availability_timeout
        )

    def _cancel_availability_timer(self) -> None:
        """Cancel the grace period timer if running."""
        if self._availability_timer is not None:
            self._availability_timer()
            self._availability_timer = None

    async def async_shutdown(self) -> None:
        """Stop the S&L device and unsubscribe."""
        self._cancel_availability_timer()
        # Cancel debounced save timer and flush final state
        if self._save_timer is not None:
            self._save_timer()
            self._save_timer = None
        self._pending_save_state = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        # Save final state before stopping
        await self._async_save_state(self.sound_light.state)
        await self.sound_light.async_stop()
        await super().async_shutdown()
