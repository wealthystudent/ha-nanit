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
owner code is a one-way code of the config entry's unique id (the Nanit
account), or of its entry id when it has none (``owner_code``), so removing
and adding the same account again keeps the ownership. Nothing readable
about the account or the baby is stored, and nothing about the entries.

- An entry writes a baby's statistics only when the marker names its owner
  code. It resumes whenever it offers again (a reload, or the account
  added again).
- When the marker names no owner for the baby, or names one that does not
  offer and the baby has no stored statistics rows at all (the user
  deleted them, so the marker is void), the offering entry whose
  ``entry_id`` sorts first takes ownership, and the marker is saved before
  its importer starts. Taking ownership waits until Home Assistant has
  started, so every entry that loads at start-up has offered and the
  choice does not depend on the order they load in.
- Every other entry stays a standby and never writes, whatever happens to
  the owner: unloaded, removed, the import switched off, stopped by a
  refused sign-in, or polling disabled. There is no handover. While a baby
  has offers but no importer, the registry looks again every
  ``OWNER_CHECK_INTERVAL`` (and once Home Assistant has started), so a
  void marker is noticed without a restart.

Every change runs under one lock, so an entry unloading while another sets
up is handled one step at a time. The registry leaves ``hass.data[DOMAIN]``,
with its timers cancelled, once no baby has an entry and nothing waits for
the lock. Starting an importer only starts its background task, so offering
returns at once.

``EntryCareLogWriters`` is one config entry's side: it offers a writer for
every baby the entry has a care log reader for, when the entry's "Import
care log statistics" option is on, and withdraws them all on unload.

Privacy: no uid, baby name, baby code or owner code appears in a log line
here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .baby_code import baby_code, one_way_code
from .care_log_importer import CareLogStatisticsImporter
from .care_log_recorder import async_has_care_log_statistics
from .const import DOMAIN, is_import_care_log_statistics

if TYPE_CHECKING:
    from aionanit.client import NanitClient
    from aionanit.models import CareLogFetch

    from . import NanitConfigEntry
    from .coordinator import NanitCareLogCoordinator

_LOGGER = logging.getLogger(__name__)

# The registry's key in hass.data[DOMAIN].
WRITERS_KEY: Final = "care_log_writers"

# The owner marker's storage key and version.
STORE_KEY: Final = f"{DOMAIN}.care_log_statistics"
STORE_VERSION: Final = 1

# The input of the owner code: like the baby code, with its own prefix.
OWNER_CODE_PREFIX: Final = b"ha-nanit/statistics-owner/v1\x00"

# How often a baby with offers but no importer is looked at again.
OWNER_CHECK_INTERVAL: Final = timedelta(minutes=15)

# The state an entry that is not the owner reports in diagnostics.
STATE_NOT_OWNER: Final = "not_owner"

type ImporterFactory = Callable[[], CareLogStatisticsImporter]


def owner_code(entry: NanitConfigEntry) -> str:
    """Return the owner code of a config entry: a one-way code of its account."""
    return one_way_code(OWNER_CODE_PREFIX, entry.unique_id or entry.entry_id)


@dataclass(frozen=True, slots=True)
class _Offer:
    owner: str
    factory: ImporterFactory


@dataclass
class _Baby:
    """The entries offering to write one baby's statistics, and the one that does."""

    offers: dict[str, _Offer] = field(default_factory=dict)
    writer: str | None = None
    importer: CareLogStatisticsImporter | None = None


class CareLogWriterRegistry:
    """Which config entry writes each baby's care log statistics."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start with no babies; the marker is read when first needed."""
        self._hass = hass
        self._lock = asyncio.Lock()
        self._babies: dict[str, _Baby] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._owners: dict[str, str] | None = None
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._check_task: asyncio.Task[None] | None = None
        # Callers inside or waiting for the lock: the registry stays in
        # hass.data while there are any.
        self._users = 0

    @property
    def unused(self) -> bool:
        """Return whether no baby has an entry and nobody is using the registry."""
        return not self._babies and self._users == 0

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

    async def async_register(
        self, code: str, entry_id: str, owner: str, factory: ImporterFactory
    ) -> None:
        """Offer an entry as a baby's writer; it writes when it is, or becomes, the owner."""
        self._users += 1
        try:
            async with self._lock:
                baby = self._babies.setdefault(code, _Baby())
                baby.offers[entry_id] = _Offer(owner=owner, factory=factory)
                try:
                    await self._async_settle(code, baby)
                finally:
                    self._update_watch()
        finally:
            self._users -= 1

    async def async_unregister(self, code: str, entry_id: str) -> None:
        """Withdraw an entry, stopping its importer; nobody else takes over."""
        self._users += 1
        try:
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
                            await importer.async_stop()
                finally:
                    if not baby.offers:
                        del self._babies[code]
                    self._update_watch()
        finally:
            self._users -= 1

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

    async def _async_owners(self) -> dict[str, str]:
        """Return the marker (baby code to owner code), read from disk the first time."""
        if self._owners is None:
            owners: dict[str, str] = {}
            data = await self._store.async_load()
            stored = data.get("owners") if isinstance(data, dict) else None
            if isinstance(stored, dict):
                owners = {
                    key: value
                    for key, value in stored.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            self._owners = owners
        return self._owners

    async def _async_settle(self, code: str, baby: _Baby) -> None:
        """Start the owner's importer for a baby that has none, taking ownership if free."""
        hass = self._hass
        if baby.importer is not None or not baby.offers or hass.is_stopping:
            return
        owners = await self._async_owners()
        owner = owners.get(code)
        if owner is not None:
            for entry_id in sorted(baby.offers):
                if baby.offers[entry_id].owner == owner:
                    self._start(baby, entry_id)
                    return
            # The owner does not offer: it keeps the statistics unless they
            # were deleted (an unknown answer keeps them too).
            if await async_has_care_log_statistics(hass, code) is not False:
                return
        if hass.state is not CoreState.running:
            # Taken once every entry that loads at start-up has offered.
            return
        entry_id = min(baby.offers)
        owners[code] = baby.offers[entry_id].owner
        await self._store.async_save({"owners": dict(owners)})
        self._start(baby, entry_id)

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
    hass: HomeAssistant, code: str, entry_id: str, owner: str, factory: ImporterFactory
) -> None:
    """Offer an entry as the writer of a baby's statistics (see the module docstring)."""
    registry = _registry(hass)
    try:
        await registry.async_register(code, entry_id, owner, factory)
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

    @property
    def codes(self) -> tuple[str, ...]:
        """Return the codes of the babies this entry offers to write, sorted."""
        return tuple(sorted(self._codes))

    async def async_start(self) -> None:
        """Offer a writer for every baby with a reader, when the option is on."""
        if not is_import_care_log_statistics(self._entry):
            return
        owner = owner_code(self._entry)
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
            await async_register_writer(self._hass, code, self._entry.entry_id, owner, factory)

    async def async_stop(self) -> None:
        """Withdraw every offer, stopping this entry's importers. Safe to call again."""
        self._stopped = True
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
                babies[f"baby_{index}"] = {"writer": False, "state": STATE_NOT_OWNER}
        return {"enabled": is_import_care_log_statistics(self._entry), "babies": babies}
