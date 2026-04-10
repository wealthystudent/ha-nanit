"""Input sanitization — strip XSS vectors from API-provided names.

See docs/SECURITY_AUDIT_CHECKLIST.md §7 (XSS) and §3 (Input Validation).
"""

from __future__ import annotations

import html
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_name(value: str) -> str:
    """Strip HTML tags and escape entities from an API-provided name.

    Prevents stored XSS via baby/camera names controlled by the Nanit API.
    """
    cleaned = _HTML_TAG_RE.sub("", value)
    # Unescape pre-encoded entities, then re-escape for safe display.
    cleaned = html.escape(html.unescape(cleaned))
    return re.sub(r"\s+", " ", cleaned).strip()
