"""Switch platform for Nanit."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from aionanit import NanitCamera
from aionanit.models import CameraState

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# After a command, ignore contradicting push updates for this many seconds.
# The Nanit camera may echo the *old* state before processing the command;
# empirical data shows the stale-then-confirmed cycle completes within ~12 s.
_COMMAND_GRACE_PERIOD: float = 15.0


@dataclass(frozen=True, kw_only=True)
class NanitSwitchEntityDescription(SwitchEntityDescription):
    """Describe a Nanit switch."""

    value_fn: Callable[[CameraState], bool | None]
    turn_on_fn: Callable[[NanitCamera], Coroutine[Any, Any, Any]]
    turn_off_fn: Callable[[NanitCamera], Coroutine[Any, Any, Any]]


SWITCHES: tuple[NanitSwitchEntityDescription, ...] = (
    NanitSwitchEntityDescription(
        key="camera_power",
        translation_key="camera_power",
        icon="mdi:power",
        entity_registry_enabled_default=True,
        device_class=SwitchDeviceClass.SWITCH,
        value_fn=lambda state: (
            not state.settings.sleep_mode if state.settings.sleep_mode is not None else None
        ),
        turn_on_fn=lambda cam: cam.async_set_settings(sleep_mode=False),
        turn_off_fn=lambda cam: cam.async_set_settings(sleep_mode=True),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit switches for all cameras on the account."""
    entities: list[NanitSwitch] = []
    for cam_data in entry.runtime_data.cameras.values():
        for description in SWITCHES:
            entities.append(NanitSwitch(cam_data.push_coordinator, cam_data.camera, description))
    async_add_entities(entities)


class NanitSwitch(NanitEntity, RestoreEntity, SwitchEntity):
    """Nanit switch entity."""

    entity_description: NanitSwitchEntityDescription

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
        description: NanitSwitchEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._camera = camera
        self._attr_is_on: bool | None = None
        # Track the last command so stale push events are suppressed.
        self._command_state: bool | None = None
        self._command_ts: float = 0.0
        self._attr_unique_id = f"{camera.uid}_{description.key}"
        if coordinator.data is not None:
            self._attr_is_on = self.entity_description.value_fn(coordinator.data)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        if (
            self._attr_is_on is None
            and (last_state := await self.async_get_last_state()) is not None
        ):
            self._attr_is_on = last_state.state == STATE_ON

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on.

        Returns None (unknown) when no live or restored data is available,
        so that the HA frontend does not misleadingly show 'off'.
        """
        return self._attr_is_on

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        After a command, push events that contradict the expected state are
        suppressed for ``_COMMAND_GRACE_PERIOD`` seconds to prevent the
        camera's stale echo from bouncing the HA state.  Once the push
        state matches the command (confirming it was applied) or the grace
        period expires, normal push handling resumes.
        """
        if self.coordinator.data is not None:
            new_value = self.entity_description.value_fn(self.coordinator.data)
            if new_value is not None:
                if self._command_state is not None:
                    elapsed = time.monotonic() - self._command_ts
                    if elapsed < _COMMAND_GRACE_PERIOD:
                        if new_value == self._command_state:
                            # Push confirms the command — accept and clear.
                            self._command_state = None
                            self._attr_is_on = new_value
                        else:
                            # Stale push contradicts the command — skip.
                            _LOGGER.debug(
                                "Ignoring stale push for %s (got %s, expected %s, %.1fs after command)",
                                self.entity_description.key,
                                new_value,
                                self._command_state,
                                elapsed,
                            )
                    else:
                        # Grace period expired — accept whatever the camera says.
                        self._command_state = None
                        self._attr_is_on = new_value
                else:
                    self._attr_is_on = new_value
            # If new_value is None, keep the previous _attr_is_on (last-known).
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        previous = self._attr_is_on
        self._attr_is_on = True
        self._command_state = True
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_on_fn(self._camera)
        except Exception:
            _LOGGER.warning("Failed to turn on %s, reverting state", self.entity_description.key)
            self._attr_is_on = previous
            self._command_state = None
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        previous = self._attr_is_on
        self._attr_is_on = False
        self._command_state = False
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self.entity_description.turn_off_fn(self._camera)
        except Exception:
            _LOGGER.warning("Failed to turn off %s, reverting state", self.entity_description.key)
            self._attr_is_on = previous
            self._command_state = None
            self.async_write_ha_state()
            raise
