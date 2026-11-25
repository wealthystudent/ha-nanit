"""The long-term statistics format for care logs: ids, names, metadata and rows.

Nothing here writes to the recorder: the writer (``care_log_recorder``)
builds the rows from the ledger, and the tests that pin the format build
theirs with a helper of their own.

The format, as Home Assistant's recorder checks it:

- One external statistic per baby and series, ``nanit:<code>_<series>``,
  where ``<code>`` is the baby code (never a raw uid) and ``<series>`` one
  of ``CareLogSeries``. The source is ``nanit``.
- A running total with no mean, and an explicit unit and unit class:
  none for counts, volume in mL, duration in minutes.
- A row for every hour from the first to the last, each carrying both the
  hour's own amount (``state``, 0 for an hour with nothing) and the running
  total (``sum``). The storage-shape test shows why an hour with nothing
  still needs its row: without one, ``statistic_during_period`` reads a
  day after an empty stretch as 0, and an empty day as missing. A row
  written again replaces the stored one, and a key left out becomes empty,
  so both keys are always sent.
- Each row starts at the top of a UTC hour, given as an aware datetime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.unit_conversion import DurationConverter, VolumeConverter

from .care_log_hours import SERIES_UNITS, CareLogSeries, SeriesUnit
from .const import DOMAIN
from .sanitize import display_name

STATISTIC_SOURCE = DOMAIN

# The unit class and unit each kind of series is stored in.
STATISTIC_UNITS: Mapping[SeriesUnit, tuple[str | None, str | None]] = {
    SeriesUnit.COUNT: (None, None),
    SeriesUnit.MILLILITERS: (VolumeConverter.UNIT_CLASS, UnitOfVolume.MILLILITERS),
    SeriesUnit.MINUTES: (DurationConverter.UNIT_CLASS, UnitOfTime.MINUTES),
}

# The recorder stores a statistic's name in a column of this many
# characters; a longer one can be refused by PostgreSQL or a strict MariaDB.
STATISTIC_NAME_MAX = 255

_CODE_RE = re.compile(r"[0-9a-f]{32}")

# The series labels live in the translation files next to the care log
# calendar's event labels: under that entity's state attributes, the one
# place in an entity's translations that holds free text keyed by short
# tokens. hassfest allows no placeholder there, so each is a plain label
# and the baby's name is put before it here (see ``statistic_names``).
_NAME_PREFIX = f"component.{DOMAIN}.entity.calendar.care_log.state_attributes.statistic_name.state"


# Home Assistant fills every key a language lacks from the English
# translation, so a series' label is missing only when the translation
# files themselves are broken (or the lookup raised). Then the series key
# stands in: no English text is kept here (it lives in the translation
# files alone).
def fallback_label(series: CareLogSeries) -> str:
    """Return the label used when no translation can be: the series key."""
    return series.value


def statistic_id(code: str, series: CareLogSeries) -> str:
    """Return the statistic id of one baby's series.

    Raises ValueError unless ``code`` is a baby code (32 lowercase hex
    characters), so a raw uid can never end up in an id.
    """
    if not _CODE_RE.fullmatch(code):
        raise ValueError("Not a baby code")
    return f"{DOMAIN}:{code}_{series.value}"


def statistic_baby_name(name: str | None, code: str) -> str:
    """Return the baby's name for statistic names: sanitized, or one made from its code."""
    return display_name(name, code)


def statistic_names(strings: Mapping[str, str], baby_name: str) -> dict[CareLogSeries, str]:
    """Return each series' name: the baby name, then the series' translated label.

    A label that is missing or empty falls back to the series key. The two
    are joined as they are, so neither is read as a template. A name longer
    than the recorder's metadata column (``STATISTIC_NAME_MAX``) has the baby
    name cut short, so the series part stays whole.
    """
    names: dict[CareLogSeries, str] = {}
    for series in CareLogSeries:
        label = strings.get(f"{_NAME_PREFIX}.{series.value}")
        if not isinstance(label, str) or not label:
            label = fallback_label(series)
        room = STATISTIC_NAME_MAX - len(_named("", label))
        names[series] = _named(_shortened(baby_name, room), label)[:STATISTIC_NAME_MAX]
    return names


def _named(baby_name: str, label: str) -> str:
    """Return a series' name from the baby's name and the series' label."""
    return f"{baby_name} {label}"


def _shortened(baby_name: str, room: int) -> str:
    """Return the baby name cut to ``room`` characters."""
    if len(baby_name) <= room:
        return baby_name
    return baby_name[: max(room, 0)].rstrip()


async def async_statistic_names(hass: HomeAssistant, baby_name: str) -> dict[CareLogSeries, str]:
    """Return each series' name in Home Assistant's language (English for a missing key)."""
    try:
        strings = await async_get_translations(hass, hass.config.language, "entity", {DOMAIN})
    except Exception:  # noqa: BLE001
        strings = {}
    return statistic_names(strings, baby_name)


def statistic_metadata(code: str, series: CareLogSeries, name: str) -> StatisticMetaData:
    """Return the metadata of one baby's series."""
    unit_class, unit = STATISTIC_UNITS[SERIES_UNITS[series]]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=STATISTIC_SOURCE,
        statistic_id=statistic_id(code, series),
        unit_class=unit_class,
        unit_of_measurement=unit,
    )
