"""Tests for coordinator.py — NanitSoundLightCoordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit.aionanit_sl.models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
)
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import NanitSoundLightCoordinator

from .conftest import MOCK_EMAIL, mock_entry_data_v2


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)
    return entry


def _make_mock_sound_light(
    speaker_uid: str = "L101TEST",
    connected: bool = False,
) -> MagicMock:
    sl = MagicMock()
    sl.speaker_uid = speaker_uid
    sl.connected = connected
    sl.state = SoundLightFullState()
    sl.subscribe = MagicMock(return_value=lambda: None)
    sl.async_start = AsyncMock()
    sl.async_stop = AsyncMock()
    sl.restore_state = MagicMock()
    return sl


class TestGracePeriod:
    """Test the 30-second availability grace period."""

    @pytest.mark.asyncio
    async def test_disconnect_starts_grace_timer(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light(connected=True)
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        # Simulate initial setup
        coord._sl_connected = True

        # Simulate disconnect event
        sl.connected = False
        event = SoundLightEvent(
            kind=SoundLightEventKind.CONNECTION_CHANGE,
            state=SoundLightFullState(),
        )
        coord._on_sl_event(event)

        # Should still be "connected" due to grace period
        assert coord.connected is True
        assert coord._availability_timer is not None

    @pytest.mark.asyncio
    async def test_reconnect_cancels_grace_timer(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light(connected=True)
        coord = NanitSoundLightCoordinator(hass, entry, sl)
        coord._sl_connected = True

        # Simulate disconnect
        sl.connected = False
        disconnect_event = SoundLightEvent(
            kind=SoundLightEventKind.CONNECTION_CHANGE,
            state=SoundLightFullState(),
        )
        coord._on_sl_event(disconnect_event)
        assert coord._availability_timer is not None

        # Simulate reconnect
        sl.connected = True
        reconnect_event = SoundLightEvent(
            kind=SoundLightEventKind.CONNECTION_CHANGE,
            state=SoundLightFullState(),
        )
        coord._on_sl_event(reconnect_event)

        # Timer should be cancelled, still connected
        assert coord._availability_timer is None
        assert coord.connected is True


class TestDebouncedSave:
    """Test debounced state persistence."""

    @pytest.mark.asyncio
    async def test_state_update_schedules_save(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        state = SoundLightFullState(power_on=True, brightness=0.5)
        event = SoundLightEvent(
            kind=SoundLightEventKind.STATE_UPDATE,
            state=state,
        )
        coord._on_sl_event(event)

        # Save timer should be running
        assert coord._save_timer is not None
        assert coord._pending_save_state is state

    @pytest.mark.asyncio
    async def test_rapid_updates_only_schedule_one_timer(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        state1 = SoundLightFullState(brightness=0.3)
        state2 = SoundLightFullState(brightness=0.6)
        state3 = SoundLightFullState(brightness=0.9)

        for state in (state1, state2, state3):
            coord._on_sl_event(
                SoundLightEvent(
                    kind=SoundLightEventKind.STATE_UPDATE,
                    state=state,
                )
            )

        # Only the latest state should be pending
        assert coord._pending_save_state is state3
        # Timer should be set only once (not reset on each update)
        assert coord._save_timer is not None

    @pytest.mark.asyncio
    async def test_connection_change_does_not_schedule_save(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        event = SoundLightEvent(
            kind=SoundLightEventKind.CONNECTION_CHANGE,
            state=SoundLightFullState(),
        )
        coord._on_sl_event(event)

        assert coord._save_timer is None
        assert coord._pending_save_state is None

    @pytest.mark.asyncio
    async def test_shutdown_cancels_save_timer(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        # Schedule a save
        state = SoundLightFullState(power_on=True)
        coord._on_sl_event(
            SoundLightEvent(
                kind=SoundLightEventKind.STATE_UPDATE,
                state=state,
            )
        )
        assert coord._save_timer is not None

        # Shutdown should cancel timer and clear pending state
        await coord.async_shutdown()
        assert coord._save_timer is None
        assert coord._pending_save_state is None


class TestRestoreState:
    """Test state restoration using public restore_state() method."""

    @pytest.mark.asyncio
    async def test_setup_calls_restore_state_when_no_initial_state(
        self, hass: HomeAssistant
    ) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        # power_on=None means no initial state received yet
        sl.state = SoundLightFullState(power_on=None)
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        restored = SoundLightFullState(power_on=True, brightness=0.7)
        with patch.object(
            coord, "_async_restore_state", new_callable=AsyncMock, return_value=restored
        ):
            await coord.async_setup()

        # Should call restore_state (public method) not _state directly
        sl.restore_state.assert_called_once_with(restored)

    @pytest.mark.asyncio
    async def test_setup_skips_restore_when_state_exists(self, hass: HomeAssistant) -> None:
        entry = _make_entry(hass)
        sl = _make_mock_sound_light()
        sl.state = SoundLightFullState(power_on=True)  # already has state
        coord = NanitSoundLightCoordinator(hass, entry, sl)

        with patch.object(coord, "_async_restore_state", new_callable=AsyncMock) as mock_restore:
            await coord.async_setup()

        # Should NOT attempt to restore
        mock_restore.assert_not_called()
        sl.restore_state.assert_not_called()
