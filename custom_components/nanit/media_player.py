"""Media player platform for Nanit — camera built-in sound machine."""

from __future__ import annotations

import logging
import time

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from aionanit import NanitCamera

from . import NanitConfigEntry
from .coordinator import NanitPushCoordinator
from .entity import NanitEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# After a command, ignore contradicting push updates for this many seconds.
_COMMAND_GRACE_PERIOD: float = 15.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit media player entities for all cameras on the account."""
    entities: list[MediaPlayerEntity] = []
    for cam_data in entry.runtime_data.cameras.values():
        entities.append(NanitMediaPlayer(cam_data.push_coordinator, cam_data.camera))
    async_add_entities(entities)


class NanitMediaPlayer(NanitEntity, MediaPlayerEntity):
    """Media player entity for the Nanit camera's built-in sound machine.

    Controls white noise playback via the camera WebSocket: start/stop,
    track selection, and volume.
    """

    _attr_translation_key = "sound_machine"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        camera: NanitCamera,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._camera = camera
        self._attr_unique_id = f"{camera.uid}_sound_machine"
        # Optimistic command tracking to suppress stale push echoes.
        self._command_playing: bool | None = None
        self._command_track: str | None = None
        self._command_ts: float = 0.0

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the current playback state."""
        if self.coordinator.data is None:
            return None
        pb = self.coordinator.data.playback

        # During grace period, use the optimistic command state.
        if self._command_playing is not None:
            elapsed = time.monotonic() - self._command_ts
            if elapsed < _COMMAND_GRACE_PERIOD:
                return MediaPlayerState.PLAYING if self._command_playing else MediaPlayerState.IDLE
            self._command_playing = None

        return MediaPlayerState.PLAYING if pb.playing else MediaPlayerState.IDLE

    @property
    def source(self) -> str | None:
        """Return the currently playing track."""
        if self.coordinator.data is None:
            return None

        # During grace period, prefer the commanded track.
        if self._command_track is not None:
            elapsed = time.monotonic() - self._command_ts
            if elapsed < _COMMAND_GRACE_PERIOD:
                return self._command_track
            self._command_track = None

        track: str | None = self.coordinator.data.playback.current_track
        return track

    @property
    def source_list(self) -> list[str] | None:
        """Return available sound tracks."""
        if self.coordinator.data is None:
            return None
        tracks = self.coordinator.data.playback.available_tracks
        return list(tracks) if tracks else None

    @property
    def volume_level(self) -> float | None:
        """Return the camera speaker volume as 0.0-1.0."""
        if self.coordinator.data is None:
            return None
        vol = self.coordinator.data.settings.volume
        return vol / 100.0 if vol is not None else None

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the camera speaker volume (0.0-1.0 mapped to 0-100)."""
        await self._camera.async_set_settings(volume=int(volume * 100))
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        """Start sound machine playback."""
        self._command_playing = True
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self._camera.async_start_playback()
        except Exception:
            self._command_playing = None
            self.async_write_ha_state()
            raise

    async def async_media_stop(self) -> None:
        """Stop sound machine playback."""
        self._command_playing = False
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self._camera.async_stop_playback()
        except Exception:
            self._command_playing = None
            self.async_write_ha_state()
            raise

    async def async_select_source(self, source: str) -> None:
        """Select a sound track and start playback."""
        self._command_playing = True
        self._command_track = source
        self._command_ts = time.monotonic()
        self.async_write_ha_state()
        try:
            await self._camera.async_start_playback(track=source)
        except Exception:
            self._command_playing = None
            self._command_track = None
            self.async_write_ha_state()
            raise

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        During the grace period after a command, updates that contradict the
        expected state are suppressed to avoid stale-echo bouncing.
        """
        if self.coordinator.data is not None and self._command_playing is not None:
            elapsed = time.monotonic() - self._command_ts
            pb = self.coordinator.data.playback
            if elapsed < _COMMAND_GRACE_PERIOD:
                if pb.playing == self._command_playing:
                    # Push confirms the command — clear grace.
                    self._command_playing = None
                    self._command_track = None
                # Otherwise suppress the stale push.
            else:
                # Grace period expired — accept whatever the camera says.
                self._command_playing = None
                self._command_track = None
        self.async_write_ha_state()
