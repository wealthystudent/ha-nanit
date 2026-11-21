"""Where a baby's care log history starts, and the pieces it is read in.

The care log statistics importer reads a baby's whole history in pieces of
at most ``HISTORY_PIECE``. The boundaries between pieces lie on a fixed grid
(multiples of ``HISTORY_PIECE`` since the Unix epoch), so the same bounds
recur from one pass to the next.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Final

from aionanit.models import Baby

# One request per this much history per pass. The pieces' inner boundaries
# are multiples of it since the Unix epoch, so the same bounds recur.
HISTORY_PIECE: Final = timedelta(days=90)
# The history starts this long before the earlier of birth and creation date.
HISTORY_BEFORE_BIRTH: Final = timedelta(days=365)
# Without either date, this long before now (moved down onto the piece grid,
# so the bounds recur from day to day).
HISTORY_FALLBACK: Final = timedelta(days=3 * 365)
# Never earlier than this, whatever the baby record says: it bounds the
# requests a wrong date could cause (Nanit's care logs are younger).
HISTORY_FLOOR: Final = datetime(2014, 1, 1, tzinfo=UTC)

_PIECE_SECONDS: Final = int(HISTORY_PIECE.total_seconds())

type Bounds = tuple[int, int]


# ---------------------------------------------------------------------------
# History bounds (pure)
# ---------------------------------------------------------------------------


def care_log_history_start(baby: Baby, now: datetime) -> int:
    """Return where a baby's care log history starts, in Unix seconds.

    One year before the earlier of the birth date (its UTC midnight) and the
    record's creation time. When neither is usable, three years before now,
    moved down onto the piece grid. Never before ``HISTORY_FLOOR``.
    """
    known: list[float] = []
    if baby.birth_date is not None:
        birth = baby.birth_date
        known.append(datetime(birth.year, birth.month, birth.day, tzinfo=UTC).timestamp())
    created = baby.created_at
    if created is not None and math.isfinite(created) and created > 0:
        known.append(created)
    if known:
        start = math.floor(min(known) - HISTORY_BEFORE_BIRTH.total_seconds())
    else:
        fallback = math.floor((now - HISTORY_FALLBACK).timestamp())
        start = fallback // _PIECE_SECONDS * _PIECE_SECONDS
    return max(start, int(HISTORY_FLOOR.timestamp()))


def care_log_history_pieces(start: int, end: int) -> tuple[Bounds, ...]:
    """Split ``[start, end]`` (inclusive Unix seconds) into history pieces, oldest first.

    Each piece spans at most ``HISTORY_PIECE``; the boundaries between pieces
    lie on multiples of it since the Unix epoch, so the same bounds recur on
    every pass. Consecutive pieces touch (``next.start == previous.end + 1``)
    and the last ends at ``end``. Empty when ``end < start``.
    """
    pieces: list[Bounds] = []
    low = start
    while low <= end:
        high = min((low // _PIECE_SECONDS + 1) * _PIECE_SECONDS - 1, end)
        pieces.append((low, high))
        low = high + 1
    return tuple(pieces)
