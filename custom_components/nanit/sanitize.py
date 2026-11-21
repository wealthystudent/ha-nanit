"""Input sanitization — strip XSS vectors from API-provided names.

See docs/SECURITY_AUDIT_CHECKLIST.md §7 (XSS) and §3 (Input Validation).
"""

from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_name(value: str | None) -> str:
    """Strip HTML tags from an API-provided or typed name, leaving plain text.

    Prevents stored XSS via baby/camera names controlled by the Nanit API.
    The result is plain text, not HTML: Home Assistant and the card render
    names as text, and markdown surfaces (repairs, flow text) get no tags
    because they are stripped here. Entities are decoded first, so an
    encoded tag such as ``&lt;script&gt;`` is stripped too. A ``<`` with
    no closing ``>`` is not a tag and stays as literal text.
    """
    if not value:
        return ""
    cleaned = value
    # Decode nested encodings (e.g. "&amp;lt;") before stripping tags.
    for _ in range(3):
        decoded = html.unescape(cleaned)
        if decoded == cleaned:
            break
        cleaned = decoded
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def display_name(value: str | None, uid: str) -> str:
    """Return a sanitized display name with a UID-based fallback for unnamed babies."""
    name = sanitize_name(value)
    return name if name else f"Baby {uid[:8]}"


def speaker_display_name(value: str | None, uid: str) -> str:
    """Return a sanitized speaker name with a UID-based fallback for unnamed machines."""
    name = sanitize_name(value)
    return name if name else f"Sound & Light {uid[:8]}"
