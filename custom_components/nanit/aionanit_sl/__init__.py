"""aionanit_sl — Sound & Light Machine extensions for the Nanit integration.

This package contains the S&L-specific WebSocket protocol, models, and
high-level API that are not (yet) part of the external aionanit package.
"""

from .exceptions import NanitTransportError
from .models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
    SoundLightRoutine,
)
from .sound_light import NanitSoundLight

__all__ = [
    "NanitSoundLight",
    "NanitTransportError",
    "SoundLightEvent",
    "SoundLightEventKind",
    "SoundLightFullState",
    "SoundLightRoutine",
]
