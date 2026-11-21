"""One care log statistics writer per baby, whichever sign-ins can see the baby.

``CareLogStatisticsImporter`` assumes it is the only writer of a baby's
statistics, but two config entries (two Nanit sign-ins) can see the same
baby. The registry here, kept in ``hass.data[DOMAIN]``, lets exactly one of
them write. For each baby code it holds every entry that offers to write
(an importer factory per entry) and the importer running, if any.

The writer is the entry whose ``entry_id`` sorts first among those that
offer, so the choice does not depend on the order entries load in:

- Offering (``async_register_writer``): when the new entry should be the
  writer, the current writer's importer is stopped (awaited) and a new one
  made and started; otherwise the entry waits as a standby.
- Withdrawing (``async_unregister_writer``): when the entry was the writer,
  its importer is stopped (awaited) and the entry that now sorts first gets
  a new importer, which starts from scratch. Nothing is handed over while
  Home Assistant is stopping.

Every change runs under one lock, so an entry unloading while another sets
up is handled one step at a time. The registry leaves ``hass.data[DOMAIN]``
once no baby has an entry and nothing waits for the lock. Starting an
importer only starts its background task, so offering returns at once.

``EntryCareLogWriters`` is one config entry's side: it offers a writer for
every baby the entry has a care log reader for, when the entry's "Import
care log statistics" option is on, and withdraws them all on unload.

Privacy: no uid, baby name or baby code appears in a log line here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant

from .baby_code import baby_code
from .care_log_importer import CareLogStatisticsImporter
from .const import DOMAIN, is_import_care_log_statistics

if TYPE_CHECKING:
    from aionanit.client import NanitClient
    from aionanit.models import CareLogFetch

    from . import NanitConfigEntry
    from .coordinator import NanitCareLogCoordinator

_LOGGER = logging.getLogger(__name__)

# The registry's key in hass.data[DOMAIN].
WRITERS_KEY: Final = "care_log_writers"

# The state a standby reports in diagnostics.
STATE_STANDBY: Final = "standby"

type ImporterFactory = Callable[[], CareLogStatisticsImporter]


@dataclass
class _Baby:
    """The entries offering to write one baby's statistics, and the one that does."""

    factories: dict[str, ImporterFactory] = field(default_factory=dict)
    writer: str | None = None
    importer: CareLogStatisticsImporter | None = None


class CareLogWriterRegistry:
    """Which config entry writes each baby's care log statistics."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start with no babies."""
        self._hass = hass
        self._lock = asyncio.Lock()
        self._babies: dict[str, _Baby] = {}
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
        return tuple(sorted(baby.factories)) if baby is not None else ()

    async def async_register(self, code: str, entry_id: str, factory: ImporterFactory) -> None:
        """Offer an entry as a baby's writer; it writes when its id sorts first."""
        self._users += 1
        try:
            async with self._lock:
                baby = self._babies.setdefault(code, _Baby())
                baby.factories[entry_id] = factory
                if min(baby.factories) != entry_id or baby.writer == entry_id:
                    return
                await self._async_stop_writer(baby)
                self._start_first(baby)
        finally:
            self._users -= 1

    async def async_unregister(self, code: str, entry_id: str) -> None:
        """Withdraw an entry; when it was the writer, the next entry takes over."""
        self._users += 1
        try:
            async with self._lock:
                baby = self._babies.get(code)
                if baby is None or baby.factories.pop(entry_id, None) is None:
                    return
                if baby.writer == entry_id:
                    await self._async_stop_writer(baby)
                    try:
                        self._start_first(baby)
                    except Exception as err:  # noqa: BLE001
                        # The entry going away must still unload. Logged by
                        # type only: the text could hold anything.
                        _LOGGER.error(
                            "Could not start the care log statistics import for another "
                            "sign-in (%s)",
                            type(err).__name__,
                        )
                if not baby.factories:
                    del self._babies[code]
        finally:
            self._users -= 1

    async def _async_stop_writer(self, baby: _Baby) -> None:
        importer = baby.importer
        baby.writer = None
        baby.importer = None
        if importer is not None:
            await importer.async_stop()

    def _start_first(self, baby: _Baby) -> None:
        """Make and start an importer for the entry that sorts first (none is running)."""
        if not baby.factories or self._hass.is_stopping:
            return
        entry_id = min(baby.factories)
        importer = baby.factories[entry_id]()
        baby.writer = entry_id
        baby.importer = importer
        importer.async_start()


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
    hass: HomeAssistant, code: str, entry_id: str, factory: ImporterFactory
) -> None:
    """Offer an entry as the writer of a baby's statistics (see the module docstring)."""
    registry = _registry(hass)
    try:
        await registry.async_register(code, entry_id, factory)
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
            await async_register_writer(self._hass, code, self._entry.entry_id, factory)

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
                babies[f"baby_{index}"] = {"writer": False, "state": STATE_STANDBY}
        return {"enabled": is_import_care_log_statistics(self._entry), "babies": babies}
