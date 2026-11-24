"""A test helper: the dense care log statistics rows, built from hourly amounts.

Production builds its rows in ``care_log_recorder``; the format and shape
tests use these to write rows directly and to compare with what it wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from homeassistant.components.recorder.models import StatisticData

from custom_components.nanit.care_log_hours import HOUR_SECONDS


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
