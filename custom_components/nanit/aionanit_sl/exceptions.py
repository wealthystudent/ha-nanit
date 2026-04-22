"""S&L-specific exceptions."""

from aionanit import NanitError


class NanitTransportError(NanitError):
    """WebSocket transport error for the S&L device."""
