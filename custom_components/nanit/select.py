"""Select platform for Nanit — night light timer."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit import NanitCamera

from . import NanitConfigEntry
from .const import DOMAIN
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

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
    async_add_entities(
        NanitNightLightTimer(cam_data.push_coordinator, cam_data.camera)
        for cam_data in entry.runtime_data.cameras.values()
    )


class NanitNightLightTimer(NanitEntity, SelectEntity):
    """Night light auto-off timer.

    Sends ``night_light_timeout`` (seconds) via PUT_CONTROL.
    The camera turns the night light off automatically after the chosen duration.
    A value of 0 means "no auto-off" (the light stays on until manually turned off).
    """

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
