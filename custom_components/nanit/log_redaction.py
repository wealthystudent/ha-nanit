"""Keep the Nanit access token out of Home Assistant's stream logs.

The camera's stream source is ``rtmps://media-secured.nanit.com/nanit/<baby uid>.<access token>``,
so the token sits in the URL path. Home Assistant's ``stream`` and ``go2rtc`` components redact
only usernames, passwords and ``auth``-style query parameters before logging a source URL, so
a stream error writes the full token to the log. The ``stream`` worker logs it at ERROR, and
go2rtc's own warnings, which Home Assistant relays at WARNING, carry the URL too.

A filter masks the token and keeps the rest of the line, so the error stays useful. A filter on
a logger does not apply to records from its child loggers, so it has to sit on the exact logger
that prints the URL. For the stream worker that is a per-camera logger: Home Assistant creates
each camera's stream with ``stream_label=<entity_id>`` and logs it under
``homeassistant.components.stream.stream.<entity_id>``, so each camera entity attaches the filter
to its own stream logger. go2rtc relays its output through one fixed logger, covered globally.
"""

from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

# Loggers that print the URL regardless of which camera it belongs to. The
# stream component's own logger is used for streams created without a label.
STREAM_LOGGERS = (
    "homeassistant.components.stream",
    "homeassistant.components.go2rtc.server",
)

# The baby uid has no dots, and the token is a JWT: base64url segments joined by
# dots. The host may carry a port, and the slashes may be percent-encoded.
_STREAM_TOKEN = re.compile(
    r"(media-secured\.nanit\.com(?::\d+)?(?:/|%2F)nanit(?:/|%2F)[^/.\s%]+\.)[A-Za-z0-9_\-.]+",
    re.IGNORECASE,
)
_REDACTED = "***"
_FORMATTER = logging.Formatter()

_DATA_KEY = f"{DOMAIN}_stream_token_filter"


def redact_stream_token(text: str) -> str:
    """Mask the access token in any Nanit stream URL in ``text``."""
    return _STREAM_TOKEN.sub(rf"\g<1>{_REDACTED}", text)


class StreamTokenFilter(logging.Filter):
    """Rewrite log records so a Nanit stream URL never carries its token."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask the token in the message and any traceback, and keep the record."""
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            # A malformed record is the logging call's problem, not ours to break.
            return True
        redacted = redact_stream_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            # Format the traceback now, so handlers reuse the redacted copy.
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_stream_token(record.exc_text)
        return True


# One shared instance: loggers are process-wide and addFilter() ignores a filter
# that is already attached, so installing twice can never stack copies.
_FILTER = StreamTokenFilter()


@callback
def async_install_stream_token_filter(hass: HomeAssistant, entry_id: str) -> None:
    """Install the filter, and record that this entry needs it."""
    for name in STREAM_LOGGERS:
        logging.getLogger(name).addFilter(_FILTER)
    hass.data.setdefault(_DATA_KEY, set()).add(entry_id)


@callback
def async_remove_stream_token_filter(hass: HomeAssistant, entry_id: str) -> None:
    """Drop this entry's need for the filter, removing it with the last entry."""
    entries: set[str] = hass.data.get(_DATA_KEY, set())
    entries.discard(entry_id)
    if entries:
        return
    hass.data.pop(_DATA_KEY, None)
    for name in STREAM_LOGGERS:
        logging.getLogger(name).removeFilter(_FILTER)


def camera_stream_logger(entity_id: str) -> str:
    """Return the logger Home Assistant's stream worker uses for this camera."""
    return f"homeassistant.components.stream.stream.{entity_id}"


@callback
def async_attach_to_camera_stream(entity_id: str) -> None:
    """Mask the token in this camera's stream log lines."""
    logging.getLogger(camera_stream_logger(entity_id)).addFilter(_FILTER)


@callback
def async_detach_from_camera_stream(entity_id: str) -> None:
    """Stop filtering this camera's stream logger, when the entity goes away."""
    logging.getLogger(camera_stream_logger(entity_id)).removeFilter(_FILTER)
