"""An opaque, stable code for a baby, used where a raw baby uid must not appear.

The code names things that outlive a single run: the care log calendar's
unique id and the care log reader's name in debug lines. The same baby gets
the same code on every Home Assistant, so re-adding the integration
continues where it left off. It is a one-way hash of the uid alone: no
camera id, email or display name goes in.

The input is fixed and must never change, or every name built on it moves:

    SHA-256( b"ha-nanit/baby-code/v1" + one NUL byte + uid as UTF-8 )

The prefix keeps the hash specific to this use; the NUL byte separates it
from the uid. The uid is encoded with ``surrogatepass``, so any string a
JSON answer can hold has an encoding. The code is the first 32 characters
of the lowercase hex digest (128 bits).
"""

from __future__ import annotations

import hashlib

from .sanitize import display_name

BABY_CODE_PREFIX = b"ha-nanit/baby-code/v1\x00"
BABY_CODE_LENGTH = 32


def baby_code(uid: str) -> str:
    """Return the 32 hex character code for a baby uid.

    Raises ValueError for an empty uid, which names no baby.
    """
    if not uid:
        raise ValueError("A baby code needs a non-empty uid")
    digest = hashlib.sha256(BABY_CODE_PREFIX + uid.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()[:BABY_CODE_LENGTH]


def baby_display_name(name: str | None, uid: str) -> str:
    """Return the baby's sanitized name, or for an unnamed baby one built on its code.

    For names that outlive a run (an entity name): unlike ``display_name``
    with the uid, the fallback holds no part of the raw uid.
    """
    return display_name(name, baby_code(uid))
