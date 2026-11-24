"""One care log statistics writer per baby: the sign-in that started its statistics.

``CareLogStatisticsImporter`` assumes it is the only writer of a baby's
statistics, but two config entries (two Nanit sign-ins) can see the same
baby, and they need not see the same entries. So a baby's statistics are
kept by one owner, the sign-in that started them, and by no other. The
registry here, kept in ``hass.data[DOMAIN]``, holds for each baby code every
entry that offers to write (an importer factory per entry, and the entry's
owner code) and the importer running, if any.

The owner is remembered on disk, with Home Assistant's storage helper, in
one file for the integration (``STORE_KEY``): baby code to owner code. The
owner code is a one-way code of the Nanit account (``owner_code``): the
entry's email, stripped and lower-cased, or its unique id lower-cased, or
its entry id when it has neither. So removing and adding the same account
again keeps the ownership, whatever the letter case it was added with.
Nothing readable about the account or the baby is stored, and nothing
about the entries.

Ownership is proved, never assumed:

- An entry writes a baby's statistics only when the marker names its owner
  code. It resumes whenever it offers again (a reload, or the account
  added again), whatever its polling setting.
- A fresh claim needs a database with no stored row of any of the baby's
  series (``async_has_care_log_statistics`` says False). That is when the
  marker names no owner for the baby (it is absent, malformed or lacks the
  baby), or names one that does not offer and the rows are gone (the user
  deleted them, so the marker is void). Then the offering entry whose
  ``entry_id`` sorts first among those with polling enabled (an entry with
  polling disabled could never import) and whose care log reader has read
  the baby (it holds a read and its last update succeeded: a sign-in Nanit
  refuses would hold the baby and never import) takes ownership. An entry
  whose reader has not read yet listens to it, which keeps it polling, and
  has the registry look again as soon as it has read. Taking ownership
  waits until Home Assistant has started, so every entry that loads at
  start-up has offered and the choice does not depend on the order they
  load in.
- When the marker names no owner but the baby has stored rows, nobody
  writes: the baby is unowned (``STATE_UNOWNED`` in diagnostics, and one
  warning per Home Assistant run). When the database cannot tell, nothing
  is decided. It cannot while the baby is paused because a batch sent in
  this run is unverified (``care_log_recorder``): then nothing is decided
  until Home Assistant restarts (``STATE_PAUSED_UNTIL_RESTART``).
- The marker is saved (owner-only on disk, and written to a temporary file,
  synced and moved into place, so a power loss leaves the old marker or
  the new one), and read back from disk with a store of its own
  (Home Assistant's store logs a failed write and returns normally), before
  the owner is noted in memory and its importer starts. Any failure leaves
  everything as it was, and the next look tries again.
- A marker that cannot be loaded never fails the entry's setup: nobody
  writes, every offering entry reports ``STATE_OWNER_UNKNOWN``, one error is
  logged by type only, and the next look tries again. Home Assistant's
  store raises for a file it cannot read or one of a newer major version;
  a newer minor version it loads, and saves again in this one's. A file
  that is not valid JSON it moves aside and loads as nothing: then no owner
  is on record (see above). A file whose data has another shape names no
  owner either.
- Every other entry stays a standby and never writes, whatever happens to
  the owner: unloaded, removed, the import switched off, stopped by a
  refused sign-in, or polling disabled. There is no handover. While a baby
  has offers but no importer, the registry looks again every
  ``OWNER_CHECK_INTERVAL`` (and once Home Assistant has started), so a
  void marker is noticed without a restart.

Every change runs under one lock, so an entry unloading while another sets
up is handled one step at a time. Stopping an importer is not awaited under
it: the importer is detached under the lock and the baby marked as
stopping, so no importer starts for that baby until the stop has finished,
while other babies and entries go on. The registry leaves
``hass.data[DOMAIN]``, with its timers cancelled, once no baby has an entry,
no stop runs and nothing waits for the lock. Starting an importer only
starts its background task, so offering returns at once.

``EntryCareLogWriters`` is one config entry's side: it offers a writer for
every baby the entry has a care log reader for, when the entry's "Import
care log statistics" option is on, and withdraws them all on unload.

Privacy: no uid, baby name, baby code or owner code appears in a log line
here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import CONF_EMAIL
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .baby_code import baby_code, one_way_code
from .care_log_importer import STATE_PAUSED_UNTIL_RESTART, CareLogStatisticsImporter
from .care_log_recorder import async_has_care_log_statistics, care_log_paused
from .const import DOMAIN, is_import_care_log_statistics

if TYPE_CHECKING:
    from aionanit.client import NanitClient
    from aionanit.models import CareLogFetch

    from . import NanitConfigEntry
    from .coordinator import NanitCareLogCoordinator

_LOGGER = logging.getLogger(__name__)

# The registry's key in hass.data[DOMAIN].
WRITERS_KEY: Final = "care_log_writers"
# The messages already logged in this Home Assistant run, in hass.data[DOMAIN]:
# kept apart from the registry, which leaves when unused.
LOGGED_KEY: Final = "care_log_writers_logged"

# The owner marker's storage key and version.
STORE_KEY: Final = f"{DOMAIN}.care_log_statistics"
STORE_VERSION: Final = 1

# The input of the owner code: like the baby code, with its own prefix.
OWNER_CODE_PREFIX: Final = b"ha-nanit/statistics-owner/v1\x00"

# How often a baby with offers but no importer is looked at again.
OWNER_CHECK_INTERVAL: Final = timedelta(minutes=15)

# The states an entry that does not write reports in diagnostics: another
# sign-in is known to own the statistics; nothing is decided yet;
# statistics exist that no sign-in is known to own; the marker cannot be
# read. Nobody writes and nothing can be decided while the baby is paused
# (``STATE_PAUSED_UNTIL_RESTART``, from the importer).
STATE_NOT_OWNER: Final = "not_owner"
STATE_WAITING_FOR_OWNER_CHECK: Final = "waiting_for_owner_check"
STATE_UNOWNED: Final = "unowned"
STATE_OWNER_UNKNOWN: Final = "owner_unknown"

type ImporterFactory = Callable[[], CareLogStatisticsImporter]


def owner_code(entry: NanitConfigEntry) -> str:
    """Return the owner code of a config entry: a one-way code of its account.

    The account is the email, stripped and lower-cased, as the config flow
    matches it; else the unique id, lower-cased (a legacy entry may carry a
    mixed-case one); else the entry id.
    """
    email = entry.data.get(CONF_EMAIL)
    account = email.strip().lower() if isinstance(email, str) else ""
    if not account and entry.unique_id:
        account = entry.unique_id.lower()
    return one_way_code(OWNER_CODE_PREFIX, account or entry.entry_id)


def _owners_from(data: object) -> dict[str, str]:
    """Return the marker's baby code to owner code pairs; nothing for any other shape."""
    stored = data.get("owners") if isinstance(data, Mapping) else None
    if not isinstance(stored, Mapping):
        return {}
    return {
        key: value
        for key, value in stored.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _first_time(hass: HomeAssistant, what: str) -> bool:
    """Return True the first time ``what`` is asked about in this Home Assistant run."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    logged: set[str] = domain_data.setdefault(LOGGED_KEY, set())
    if what in logged:
        return False
    logged.add(what)
    return True


def _always() -> bool:
    return True


def _marker_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """Return a store for the owner marker: owner-only, and replaced atomically (fsync'd)."""
    return Store(hass, STORE_VERSION, STORE_KEY, private=True, atomic_writes=True)


@dataclass(frozen=True, slots=True)
class _Offer:
    owner: str
    factory: ImporterFactory
    # Whether the entry may take a first ownership (it polls).
    can_claim: bool = True
    # Whether its reader has read the baby's care logs: asked at each look.
    has_read: Callable[[], bool] = _always


@dataclass
class _Baby:
    """The entries offering to write one baby's statistics, and the one that does."""

    offers: dict[str, _Offer] = field(default_factory=dict)
    writer: str | None = None
    importer: CareLogStatisticsImporter | None = None
    # Stored rows, and no owner on record.
    unowned: bool = False


class CareLogWriterRegistry:
    """Which config entry writes each baby's care log statistics."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start with no babies; the marker is read when first needed."""
        self._hass = hass
        self._lock = asyncio.Lock()
        self._babies: dict[str, _Baby] = {}
        self._store: Store[dict[str, Any]] = _marker_store(hass)
        self._owners: dict[str, str] | None = None
        self._marker_unreadable = False
        # The stops in progress, by baby code: no importer starts for a baby
        # while its previous one is stopping.
        self._stopping: dict[str, asyncio.Task[None]] = {}
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._check_task: asyncio.Task[None] | None = None
        # Callers inside or waiting for the lock: the registry stays in
        # hass.data while there are any.
        self._users = 0

    @property
    def unused(self) -> bool:
        """Return whether no baby has an entry, no stop runs and nobody uses the registry."""
        return not self._babies and self._users == 0 and not self._stopping

    def writer(self, code: str) -> tuple[str, CareLogStatisticsImporter] | None:
        """Return the writing entry's id and its importer for a baby, if one runs."""
        baby = self._babies.get(code)
        if baby is None or baby.writer is None or baby.importer is None:
            return None
        return baby.writer, baby.importer

    def entries(self, code: str) -> tuple[str, ...]:
        """Return the ids of the entries offering to write a baby's statistics, sorted."""
        baby = self._babies.get(code)
        return tuple(sorted(baby.offers)) if baby is not None else ()

    def standby_state(self, code: str, entry_id: str) -> str:
        """Return the state an entry that does not write a baby's statistics reports.

        ``STATE_PAUSED_UNTIL_RESTART`` first, whatever else holds, while the
        baby is paused: a write may still land, so the pause is what every
        entry that sees the baby shows. Otherwise ``STATE_NOT_OWNER`` only
        for a known different owner: another entry writes, or the marker
        names another sign-in. While nothing is decided,
        ``STATE_WAITING_FOR_OWNER_CHECK``.
        """
        if care_log_paused(self._hass, code):
            return STATE_PAUSED_UNTIL_RESTART
        if self._marker_unreadable:
            return STATE_OWNER_UNKNOWN
        baby = self._babies.get(code)
        if baby is not None and baby.unowned:
            return STATE_UNOWNED
        if baby is not None and baby.writer is not None and baby.writer != entry_id:
            return STATE_NOT_OWNER
        owner = None if self._owners is None else self._owners.get(code)
        offer = None if baby is None else baby.offers.get(entry_id)
        if owner is not None and (offer is None or offer.owner != owner):
            return STATE_NOT_OWNER
        return STATE_WAITING_FOR_OWNER_CHECK

    async def async_register(
        self,
        code: str,
        entry_id: str,
        owner: str,
        factory: ImporterFactory,
        *,
        can_claim: bool = True,
        has_read: Callable[[], bool] = _always,
    ) -> None:
        """Offer an entry as a baby's writer; it writes when it is, or becomes, the owner.

        Never raises for a failed decision: setup goes on, and the next look
        tries again.
        """
        self._users += 1
        try:
            async with self._lock:
                baby = self._babies.setdefault(code, _Baby())
                baby.offers[entry_id] = _Offer(
                    owner=owner, factory=factory, can_claim=can_claim, has_read=has_read
                )
                try:
                    await self._async_settle(code, baby)
                except Exception as err:  # noqa: BLE001
                    # Logged by type only: the text could hold anything.
                    _LOGGER.error(
                        "Could not decide who writes the care log statistics (%s); "
                        "looking again later",
                        type(err).__name__,
                    )
                finally:
                    self._update_watch()
        finally:
            self._users -= 1

    async def async_unregister(self, code: str, entry_id: str) -> None:
        """Withdraw an entry, stopping its importer; nobody else takes over.

        The importer is detached under the lock and stopped outside it.
        """
        self._users += 1
        try:
            stop: asyncio.Task[None] | None = None
            async with self._lock:
                baby = self._babies.get(code)
                if baby is None or baby.offers.pop(entry_id, None) is None:
                    return
                try:
                    if baby.writer == entry_id:
                        importer = baby.importer
                        baby.writer = None
                        baby.importer = None
                        if importer is not None:
                            stop = self._begin_stop(code, importer)
                finally:
                    if not baby.offers:
                        del self._babies[code]
                    self._update_watch()
            if stop is not None:
                # Shielded: a cancelled caller leaves the stop running, and
                # the baby stopping, until it has finished (``_stop_done``
                # runs then too).
                await asyncio.shield(stop)
                self._stop_done(code, stop)
        finally:
            self._users -= 1

    def _begin_stop(self, code: str, importer: CareLogStatisticsImporter) -> asyncio.Task[None]:
        stop = self._hass.async_create_background_task(
            importer.async_stop(), "nanit care log statistics importer stop"
        )
        self._stopping[code] = stop

        def done(_task: asyncio.Task[None]) -> None:
            self._stop_done(code, stop)

        stop.add_done_callback(done)
        return stop

    @callback
    def _stop_done(self, code: str, stop: asyncio.Task[None]) -> None:
        if self._stopping.get(code) is stop:
            del self._stopping[code]
        if not stop.cancelled():
            stop.exception()  # Retrieved: async_stop does not raise.
        _forget_if_unused(self._hass, self)

    async def async_check(self) -> None:
        """Look again at every baby that has offers but no importer."""
        self._users += 1
        try:
            async with self._lock:
                try:
                    for code, baby in list(self._babies.items()):
                        if baby.importer is None and baby.offers:
                            await self._async_settle(code, baby)
                finally:
                    self._update_watch()
        finally:
            self._users -= 1

    # -- deciding -------------------------------------------------------------

    async def _async_owners(self) -> dict[str, str] | None:
        """Return the marker (baby code to owner code), read from disk the first time.

        None while it cannot be loaded: nothing is decided then.
        """
        if self._owners is None:
            try:
                data = await self._disk_store().async_load()
            except Exception as err:  # noqa: BLE001
                self._marker_unreadable = True
                if _first_time(self._hass, "marker_unreadable"):
                    # Logged by type only: the text could hold a path or the data.
                    _LOGGER.error(
                        "Could not read who writes the care log statistics (%s); writing "
                        "none of them until it can be read",
                        type(err).__name__,
                    )
                return None
            self._marker_unreadable = False
            self._owners = _owners_from(data)
        return self._owners

    async def _async_settle(self, code: str, baby: _Baby) -> None:
        """Start the owner's importer for a baby that has none, taking ownership if free."""
        hass = self._hass
        if (
            baby.importer is not None
            or not baby.offers
            or hass.is_stopping
            or code in self._stopping
        ):
            return
        owners = await self._async_owners()
        if owners is None:
            return
        owner = owners.get(code)
        if owner is not None:
            for entry_id in sorted(baby.offers):
                if baby.offers[entry_id].owner == owner:
                    baby.unowned = False
                    self._start(baby, entry_id)
                    return
        if hass.state is not CoreState.running:
            # Taken once every entry that loads at start-up has offered.
            return
        # The owner does not offer, or none is on record: only a database
        # with none of the baby's rows lets anyone take it (an unknown
        # answer decides nothing).
        has_rows = await async_has_care_log_statistics(hass, code)
        if has_rows is None:
            return
        baby.unowned = has_rows and owner is None
        if has_rows:
            if owner is None and _first_time(hass, "unowned"):
                _LOGGER.warning(
                    "Care log statistics exist for a baby, but this installation has no "
                    "record of which sign-in owns them, so none of its sign-ins writes "
                    "them; see 'Care log statistics' in the README to choose one"
                )
            return
        # Only a sign-in that has read the baby's care logs: one that cannot
        # (refused, say) would hold the baby and never import. It is looked
        # at again once its reader reads (``EntryCareLogWriters``).
        claimers = [
            one
            for one in sorted(baby.offers)
            if baby.offers[one].can_claim and baby.offers[one].has_read()
        ]
        if not claimers:
            return
        entry_id = claimers[0]
        claimed = {**owners, code: baby.offers[entry_id].owner}
        if not await self._async_save_owners(claimed, code):
            return
        self._owners = claimed
        self._start(baby, entry_id)

    async def _async_save_owners(self, owners: dict[str, str], code: str) -> bool:
        """Save the marker and read it back from disk; return whether it names the owner.

        Home Assistant's store logs a failed write and returns normally, and
        while it is stopping it only queues the write: so the file is read
        again with a new store (``_disk_store``).
        """
        try:
            await self._store.async_save({"owners": dict(owners)})
            on_disk = await self._disk_store().async_load()
        except Exception as err:  # noqa: BLE001
            if _first_time(self._hass, "marker_not_saved"):
                _LOGGER.error(
                    "Could not save who writes the care log statistics (%s); trying again later",
                    type(err).__name__,
                )
            return False
        if _owners_from(on_disk).get(code) != owners[code]:
            if _first_time(self._hass, "marker_not_saved"):
                _LOGGER.error(
                    "Could not save who writes the care log statistics; trying again later"
                )
            return False
        return True

    def _disk_store(self) -> Store[dict[str, Any]]:
        """Return a new store for the marker: it has no copy in memory, so it reads the file."""
        return _marker_store(self._hass)

    def _start(self, baby: _Baby, entry_id: str) -> None:
        importer = baby.offers[entry_id].factory()
        baby.writer = entry_id
        baby.importer = importer
        importer.async_start()

    # -- looking again --------------------------------------------------------

    @callback
    def _update_watch(self) -> None:
        """Watch while a baby has offers but no importer; stop watching otherwise."""
        hass = self._hass
        needed = not hass.is_stopping and any(
            baby.offers and baby.importer is None for baby in self._babies.values()
        )
        if not needed:
            self._stop_watching()
            return
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                hass, self._on_interval, OWNER_CHECK_INTERVAL
            )
        if self._unsub_started is None and hass.state is not CoreState.running:
            self._unsub_started = async_at_started(hass, self._on_started)

    @callback
    def _stop_watching(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._unsub_started = None
        self._schedule_check()

    @callback
    def _on_interval(self, _now: datetime) -> None:
        self._schedule_check()

    @callback
    def async_look_again(self) -> None:
        """Look again soon at every baby that has offers but no importer."""
        self._schedule_check()

    @callback
    def _schedule_check(self) -> None:
        if self._check_task is not None and not self._check_task.done():
            return
        self._check_task = self._hass.async_create_background_task(
            self._async_run_check(), "nanit care log statistics owner check"
        )

    async def _async_run_check(self) -> None:
        try:
            await self.async_check()
        except Exception as err:  # noqa: BLE001
            # Logged by type only: the text could hold anything.
            _LOGGER.error(
                "Could not check who writes the care log statistics (%s)", type(err).__name__
            )
        finally:
            _forget_if_unused(self._hass, self)


def _registry(hass: HomeAssistant) -> CareLogWriterRegistry:
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get(WRITERS_KEY)
    if not isinstance(registry, CareLogWriterRegistry):
        registry = CareLogWriterRegistry(hass)
        domain_data[WRITERS_KEY] = registry
    return registry


def care_log_writer_registry(hass: HomeAssistant) -> CareLogWriterRegistry | None:
    """Return the registry, or None when no entry offers to write."""
    registry = hass.data.get(DOMAIN, {}).get(WRITERS_KEY)
    return registry if isinstance(registry, CareLogWriterRegistry) else None


def _forget_if_unused(hass: HomeAssistant, registry: CareLogWriterRegistry) -> None:
    domain_data = hass.data.get(DOMAIN)
    if registry.unused and domain_data is not None and domain_data.get(WRITERS_KEY) is registry:
        del domain_data[WRITERS_KEY]


async def async_register_writer(
    hass: HomeAssistant,
    code: str,
    entry_id: str,
    owner: str,
    factory: ImporterFactory,
    *,
    can_claim: bool = True,
    has_read: Callable[[], bool] = _always,
) -> None:
    """Offer an entry as the writer of a baby's statistics (see the module docstring).

    ``can_claim`` False (polling disabled) keeps the entry from taking a
    first ownership, and so does ``has_read`` returning False when asked (its
    reader has not read the baby's care logs); it still writes when it is
    the owner.
    """
    registry = _registry(hass)
    try:
        await registry.async_register(
            code, entry_id, owner, factory, can_claim=can_claim, has_read=has_read
        )
    finally:
        _forget_if_unused(hass, registry)


async def async_unregister_writer(hass: HomeAssistant, code: str, entry_id: str) -> None:
    """Withdraw an entry as a writer of a baby's statistics (see the module docstring)."""
    registry = care_log_writer_registry(hass)
    if registry is None:
        return
    try:
        await registry.async_unregister(code, entry_id)
    finally:
        _forget_if_unused(hass, registry)


def _importer_factory(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    client: NanitClient,
    coordinator: NanitCareLogCoordinator,
) -> ImporterFactory:
    """Return a factory of importers for one baby, reading through the entry's client."""
    baby = coordinator.baby
    uid = baby.uid

    async def fetch(start: int, end: int) -> CareLogFetch:
        return await client.async_get_calendar_result(uid, start, end)

    def make() -> CareLogStatisticsImporter:
        return CareLogStatisticsImporter(
            hass, entry, baby=baby, coordinator=coordinator, fetch=fetch
        )

    return make


def _has_read(coordinator: NanitCareLogCoordinator) -> bool:
    """Whether a reader holds a read of the baby's care logs and its last update succeeded."""
    return coordinator.last_update_success and coordinator.data is not None


class EntryCareLogWriters:
    """One config entry's offers to write its babies' care log statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NanitConfigEntry,
        client: NanitClient,
        coordinators: dict[str, NanitCareLogCoordinator],
    ) -> None:
        """Prepare the offers; ``coordinators`` are the entry's readers by baby uid."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinators = coordinators
        self._codes: list[str] = []
        self._stopped = False
        # Per baby code, the listener on a reader that has not read yet.
        self._waiting: dict[str, CALLBACK_TYPE] = {}

    @property
    def codes(self) -> tuple[str, ...]:
        """Return the codes of the babies this entry offers to write, sorted."""
        return tuple(sorted(self._codes))

    async def async_start(self) -> None:
        """Offer a writer for every baby with a reader, when the option is on."""
        if not is_import_care_log_statistics(self._entry):
            return
        owner = owner_code(self._entry)
        # An entry with polling disabled could never import: it takes no
        # first ownership.
        can_claim = not self._entry.pref_disable_polling
        for uid, coordinator in list(self._coordinators.items()):
            if self._stopped:
                return
            # The hub keeps readers only for babies with a uid.
            if not uid:
                continue
            code = baby_code(uid)
            if code in self._codes:
                continue
            # Noted first, so a failing offer is still withdrawn on unload.
            self._codes.append(code)
            factory = _importer_factory(self._hass, self._entry, self._client, coordinator)
            await async_register_writer(
                self._hass,
                code,
                self._entry.entry_id,
                owner,
                factory,
                can_claim=can_claim,
                has_read=partial(_has_read, coordinator),
            )
            if can_claim and not self._stopped and not _has_read(coordinator):
                self._wait_for_read(code, coordinator)

    @callback
    def _wait_for_read(self, code: str, coordinator: NanitCareLogCoordinator) -> None:
        """Look again at the baby once a reader that has not read yet does.

        The listener keeps the reader polling, as the importer would. It
        goes once the reader has read, or once some entry writes the baby.
        """

        @callback
        def on_update() -> None:
            registry = care_log_writer_registry(self._hass)
            done = registry is None or registry.writer(code) is not None
            if not done and not _has_read(coordinator):
                return
            unsub = self._waiting.pop(code, None)
            if unsub is not None:
                unsub()
            if not done and registry is not None:
                registry.async_look_again()

        self._waiting[code] = coordinator.async_add_listener(on_update)

    async def async_stop(self) -> None:
        """Withdraw every offer, stopping this entry's importers. Safe to call again."""
        self._stopped = True
        while self._waiting:
            self._waiting.popitem()[1]()
        while self._codes:
            code = self._codes.pop()
            await async_unregister_writer(self._hass, code, self._entry.entry_id)

    def diagnostics(self) -> dict[str, object]:
        """Return whether the option is on and, per baby, whether this entry writes.

        Babies are keyed by position (``baby_0``, ...), in the order of their
        codes, never by a uid, code or name.
        """
        babies: dict[str, object] = {}
        registry = care_log_writer_registry(self._hass)
        for index, code in enumerate(self.codes):
            writer = registry.writer(code) if registry is not None else None
            if writer is not None and writer[0] == self._entry.entry_id:
                babies[f"baby_{index}"] = {"writer": True, **writer[1].diagnostics()}
            else:
                state = (
                    STATE_WAITING_FOR_OWNER_CHECK
                    if registry is None
                    else registry.standby_state(code, self._entry.entry_id)
                )
                babies[f"baby_{index}"] = {"writer": False, "state": state}
        return {"enabled": is_import_care_log_statistics(self._entry), "babies": babies}
