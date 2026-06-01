"""Input sanitization — strip XSS vectors from API-provided names.

See docs/SECURITY_AUDIT_CHECKLIST.md §7 (XSS) and §3 (Input Validation).
"""

from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_name(value: str | None) -> str:
    """Strip HTML tags and escape entities from an API-provided name.

    Prevents stored XSS via baby/camera names controlled by the Nanit API.
    """
    if not value:
        return ""
    cleaned = _HTML_TAG_RE.sub("", value)
    # Unescape pre-encoded entities, then re-escape for safe display.
    cleaned = html.escape(html.unescape(cleaned))
    return re.sub(r"\s+", " ", cleaned).strip()


def display_name(value: str | None, uid: str) -> str:
    """Return a sanitized display name with a UID-based fallback for unnamed babies."""
    name = sanitize_name(value)
    return name if name else f"Baby {uid[:8]}"
