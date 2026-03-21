"""Select platform for the Nanit Sound & Light Machine — sound track selection."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aionanit_sl.exceptions import NanitTransportError

from . import NanitConfigEntry
from .const import CONF_CAMERA_UID, DEFAULT_SOUND_MACHINE_SOUNDS
from .coordinator import NanitSoundLightCoordinator
from .entity import NanitSoundLightEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Nanit Sound & Light Machine sound selector."""
    coordinator = entry.runtime_data.sound_light_coordinator
    if coordinator is None:
        return

    async_add_entities([NanitSoundSelect(coordinator, entry)])


class NanitSoundSelect(NanitSoundLightEntity, SelectEntity):
    """Select entity to choose which sound the Sound & Light Machine plays."""

    _attr_translation_key = "sound_machine_sound"
    _attr_icon = "mdi:playlist-music"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        entry: NanitConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.data.get(CONF_CAMERA_UID, entry.entry_id)}"
            "_sound_machine_sound"
        )

    @property
    def options(self) -> list[str]:
        """Return available sound options from device state."""
        if (
            self.coordinator.data is not None
            and self.coordinator.data.available_tracks
        ):
            return list(self.coordinator.data.available_tracks)
        return [s.replace("_", " ").title() for s in DEFAULT_SOUND_MACHINE_SOUNDS]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected sound track."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.current_track

    async def async_select_option(self, option: str) -> None:
        """Change the selected sound track via local WebSocket."""
        try:
            await self.coordinator.sound_light.async_set_track(option)
        except (NanitTransportError, Exception) as err:
            _LOGGER.error("Failed to set sound to %s: %s", option, err)
