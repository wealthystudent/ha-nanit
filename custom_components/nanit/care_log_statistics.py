"""The long-term statistics format for care logs: ids, names, metadata and rows.

Nothing here writes to the recorder. These builders are the one place the
format is defined, so the importer that comes later, and the tests that pin
the format now, produce exactly the same thing.

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
from datetime import UTC, datetime

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util.unit_conversion import DurationConverter, VolumeConverter

from .care_log_hours import HOUR_SECONDS, SERIES_UNITS, CareLogSeries, SeriesUnit
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

# The series names live in the translation files next to the care log
# calendar's event text: under that entity's state attributes, the one
# place in an entity's translations that holds free text keyed by short
# tokens and is valid for hassfest on every supported version.
_NAME_PREFIX = f"component.{DOMAIN}.entity.calendar.care_log.state_attributes.statistic_name.state"
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


# Home Assistant fills every key a language lacks from the English
# translation, so a series' name is missing, or holds another placeholder,
# only when the translation files themselves are broken (or the lookup
# raised). Then the name is made from the series key: no English text is
# kept here (it lives in the translation files alone).
def fallback_name(series: CareLogSeries) -> str:
    """Return the name template used when no translation can be: the series key."""
    return f"{{baby_name}} {series.value}"


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
    """Return each series' name from translations, falling back to the series key.

    A translation is used only when its only placeholder is ``{baby_name}``.
    The name is substituted plainly, so it is never read as a template. A
    name longer than the recorder's metadata column
    (``STATISTIC_NAME_MAX``) has the baby name cut short, so the series part
    stays whole.
    """
    names: dict[CareLogSeries, str] = {}
    for series in CareLogSeries:
        template = strings.get(f"{_NAME_PREFIX}.{series.value}")
        if not isinstance(template, str) or set(_PLACEHOLDER_RE.findall(template)) != {"baby_name"}:
            template = fallback_name(series)
        room = STATISTIC_NAME_MAX - (len(template) - len("{baby_name}"))
        names[series] = template.replace("{baby_name}", _shortened(baby_name, room))[
            :STATISTIC_NAME_MAX
        ]
    return names


def _shortened(baby_name: str, room: int) -> str:
    """Return the (escaped) baby name cut to ``room`` characters, never inside an entity."""
    if len(baby_name) <= room:
        return baby_name
    cut = baby_name[: max(room, 0)]
    amp = cut.rfind("&")
    if amp >= 0 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut.rstrip()


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


def series_amounts(
    amounts: Mapping[int, Mapping[CareLogSeries, float]], series: CareLogSeries
) -> dict[int, float]:
    """Return one series' amount per hour from ``HourlyAmounts.amounts``."""
    return {hour: values[series] for hour, values in amounts.items() if series in values}


def statistic_rows(
    amounts: Mapping[int, float], *, start: int, end: int, sum_before: float = 0.0
) -> list[StatisticData]:
    """Return a row for every hour from ``start`` up to, not including, ``end``.

    ``start`` and ``end`` are Unix times of UTC hour starts, and every hour
    in ``amounts`` must fall between them. Each row carries the hour's
    amount (0 when it has none) and the running total, which continues from
    ``sum_before``: the total at the end of the hour before ``start``. So a
    long range can be built, and imported, in pieces.
    """
    if start % HOUR_SECONDS or end % HOUR_SECONDS:
        raise ValueError("Rows start and end at the top of a UTC hour")
    if end < start:
        raise ValueError("The range ends before it starts")
    if any(hour < start or hour >= end or hour % HOUR_SECONDS for hour in amounts):
        raise ValueError("An amount falls outside the hours asked for")
    rows: list[StatisticData] = []
    total = sum_before
    for hour in range(start, end, HOUR_SECONDS):
        amount = amounts.get(hour, 0.0)
        total += amount
        rows.append(StatisticData(start=datetime.fromtimestamp(hour, UTC), state=amount, sum=total))
    return rows
