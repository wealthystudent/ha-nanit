"""Select platform for Nanit — night light timer and Sound & Light Machine sound."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit import NanitCamera

from . import NanitConfigEntry
from .aionanit_sl.exceptions import NanitTransportError
from .const import DEFAULT_SOUND_MACHINE_SOUNDS, DOMAIN
from .coordinator import NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitEntity, NanitSoundLightEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_OPTION_TO_SECONDS: dict[str, int] = {
    "off": 0,
    "15_minutes": 900,
    "30_minutes": 1800,
    "1_hour": 3600,
    "2_hours": 7200,
    "4_hours": 14400,
}
_SECONDS_TO_OPTION: dict[int, str] = {v: k for k, v in _OPTION_TO_SECONDS.items()}

_OPTIONS: list[str] = list(_OPTION_TO_SECONDS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit select entities for all cameras on the account."""
    entities: list[SelectEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        entities.append(NanitNightLightTimer(cam_data.push_coordinator, cam_data.camera))
        sl_coordinator = cam_data.sound_light_coordinator
        if sl_coordinator is not None:
            entities.append(NanitSoundSelect(sl_coordinator))
    async_add_entities(entities)


class NanitNightLightTimer(NanitEntity, SelectEntity):
    """Night light auto-off timer."""

    _attr_translation_key = "night_light_timer"
    _attr_options = _OPTIONS

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._camera = camera
        self._attr_unique_id = f"{camera.uid}_night_light_timer"

    @property
    def current_option(self) -> str | None:
        """Return the currently selected timer option."""
        if self.coordinator.data is None:
            return None
        timeout = self.coordinator.data.control.night_light_timeout
        if timeout is None:
            return "off"
        return _SECONDS_TO_OPTION.get(timeout, "off")

    async def async_select_option(self, option: str) -> None:
        """Set the night light timer."""
        if option not in _OPTION_TO_SECONDS:
            raise ServiceValidationError(
                f"Invalid option '{option}'",
                translation_domain=DOMAIN,
                translation_key="invalid_option",
                translation_placeholders={"option": option},
            )
        seconds = _OPTION_TO_SECONDS[option]
        await self._camera.async_set_control(night_light_timeout=seconds)
        self.async_write_ha_state()


class NanitSoundSelect(NanitSoundLightEntity, SelectEntity):
    """Select entity to choose which sound the Sound & Light Machine plays."""

    _attr_translation_key = "sound_machine_sound"
    _attr_icon = "mdi:playlist-music"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        baby = coordinator.baby
        self._attr_unique_id = f"{baby.camera_uid}_sound_machine_sound"

    @property
    def options(self) -> list[str]:
        """Return available sound options from device state."""
        if self.coordinator.data is not None and self.coordinator.data.available_tracks:
            return list(self.coordinator.data.available_tracks)
        return [s.replace("_", " ").title() for s in DEFAULT_SOUND_MACHINE_SOUNDS]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected sound track."""
        if self.coordinator.data is None:
            return None
        result: str | None = self.coordinator.data.current_track
        return result

    async def async_select_option(self, option: str) -> None:
        """Change the selected sound track via local WebSocket."""
        try:
            await self.coordinator.sound_light.async_set_track(option)
        except NanitTransportError as err:
            _LOGGER.error("Failed to set sound to %s: %s", option, err)
