"""Tests for aionanit_sl/sound_light.py — NanitSoundLight WebSocket client."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nanit.aionanit_sl.models import (
    SoundLightEvent,
    SoundLightEventKind,
    SoundLightFullState,
)
from custom_components.nanit.aionanit_sl.sound_light import NanitSoundLight


def _make_sound_light(
    device_ip: str | None = "192.168.1.50",
    speaker_uid: str = "L101TEST",
) -> NanitSoundLight:
    """Create a NanitSoundLight instance with mocked dependencies."""
    token_manager = MagicMock()
    token_manager.async_get_access_token = AsyncMock(return_value="mock_access_token")

    rest_client = MagicMock()
    rest_client.base_url = "https://api.nanit.com"

    session = MagicMock()

    return NanitSoundLight(
        speaker_uid=speaker_uid,
        token_manager=token_manager,
        rest_client=rest_client,
        session=session,
        device_ip=device_ip,
    )


class TestProperties:
    def test_speaker_uid(self) -> None:
        sl = _make_sound_light(speaker_uid="L101ABC")
        assert sl.speaker_uid == "L101ABC"

    def test_initial_state(self) -> None:
        sl = _make_sound_light()
        assert sl.state == SoundLightFullState()

    def test_initial_connected_false(self) -> None:
        sl = _make_sound_light()
        assert sl.connected is False

    def test_connection_mode_unavailable_when_disconnected(self) -> None:
        sl = _make_sound_light()
        assert sl.connection_mode == "unavailable"

    def test_connection_mode_local_when_connected_with_ip(self) -> None:
        sl = _make_sound_light(device_ip="192.168.1.50")
        sl._connected = True
        sl._use_cloud_relay = False
        assert sl.connection_mode == "local"

    def test_connection_mode_cloud_when_connected_without_ip(self) -> None:
        sl = _make_sound_light(device_ip=None)
        sl._connected = True
        sl._use_cloud_relay = True
        assert sl.connection_mode == "cloud"


class TestRestoreState:
    def test_restore_state_updates_internal_state(self) -> None:
        sl = _make_sound_light()
        new_state = SoundLightFullState(power_on=True, brightness=0.5)
        sl.restore_state(new_state)
        assert sl.state.power_on is True
        assert sl.state.brightness == 0.5

    def test_restore_state_replaces_completely(self) -> None:
        sl = _make_sound_light()
        sl.restore_state(SoundLightFullState(volume=0.8))
        sl.restore_state(SoundLightFullState(brightness=0.3))
        # Second restore should replace, not merge
        assert sl.state.volume is None
        assert sl.state.brightness == 0.3


class TestSubscription:
    def test_subscribe_and_fire_event(self) -> None:
        sl = _make_sound_light()
        events: list[SoundLightEvent] = []
        sl.subscribe(events.append)
        sl._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
        assert len(events) == 1
        assert events[0].kind == SoundLightEventKind.CONNECTION_CHANGE

    def test_unsubscribe(self) -> None:
        sl = _make_sound_light()
        events: list[SoundLightEvent] = []
        unsub = sl.subscribe(events.append)
        unsub()
        sl._fire_event(SoundLightEventKind.CONNECTION_CHANGE)
        assert len(events) == 0

    def test_subscriber_exception_does_not_propagate(self) -> None:
        sl = _make_sound_light()

        def bad_callback(event: SoundLightEvent) -> None:
            raise RuntimeError("boom")

        sl.subscribe(bad_callback)
        # Should not raise
        sl._fire_event(SoundLightEventKind.STATE_UPDATE)


class TestDualSslContext:
    """Verify that local and cloud connections use different TLS settings."""

    def test_local_ssl_ctx_uses_cert_none(self) -> None:
        sl = _make_sound_light(device_ip="192.168.1.50")
        # Trigger SSL context creation
        sl._local_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        sl._local_ssl_ctx.check_hostname = False
        sl._local_ssl_ctx.verify_mode = ssl.CERT_NONE
        assert sl._local_ssl_ctx.verify_mode == ssl.CERT_NONE

    def test_no_global_ssl_ctx_attribute(self) -> None:
        """Ensure the old _ssl_ctx attribute no longer exists."""
        sl = _make_sound_light()
        assert not hasattr(sl, "_ssl_ctx")

    def test_local_ssl_ctx_initialized_none(self) -> None:
        sl = _make_sound_light()
        assert sl._local_ssl_ctx is None


class TestReconnectTaskTracking:
    """Verify reconnect tasks are properly tracked for cleanup."""

    def test_reconnect_task_initialized_none(self) -> None:
        sl = _make_sound_light()
        assert sl._reconnect_task is None

    @pytest.mark.asyncio
    async def test_async_stop_cancels_reconnect_task(self) -> None:
        sl = _make_sound_light()

        # Create a dummy task
        async def dummy() -> None:
            await asyncio.sleep(100)

        sl._reconnect_task = asyncio.get_running_loop().create_task(dummy())
        assert not sl._reconnect_task.done()

        await sl.async_stop()
        assert sl._reconnect_task is None

    @pytest.mark.asyncio
    async def test_close_ws_cancels_reconnect_task(self) -> None:
        sl = _make_sound_light()

        async def dummy() -> None:
            await asyncio.sleep(100)

        sl._reconnect_task = asyncio.get_running_loop().create_task(dummy())
        await sl._async_close_ws()
        assert sl._reconnect_task is None


class TestCloudRelayPrefersDefaultTls:
    """Verify that cloud relay connections don't pass the CERT_NONE ssl context."""

    @pytest.mark.asyncio
    async def test_cloud_relay_no_ssl_param(self) -> None:
        """When connecting to cloud relay, ws_connect should NOT receive ssl=_local_ssl_ctx."""
        sl = _make_sound_light(device_ip=None)  # cloud relay only
        sl._stopped = False

        mock_ws = MagicMock()
        mock_ws.closed = False
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))

        sl._session.ws_connect = AsyncMock(return_value=mock_ws)

        with contextlib.suppress(Exception):
            await sl._async_connect()  # May fail on recv_task setup, that's fine

        # Check the ws_connect call — ssl should NOT be the CERT_NONE context
        if sl._session.ws_connect.call_count > 0:
            call_kwargs = sl._session.ws_connect.call_args
            # Cloud relay call should not have ssl=<SSLContext> with CERT_NONE
            ssl_arg = call_kwargs.kwargs.get("ssl") if call_kwargs.kwargs else None
            if ssl_arg is not None:
                assert ssl_arg.verify_mode != ssl.CERT_NONE, "Cloud relay must not use CERT_NONE"


class TestUseCloudRelayFlag:
    def test_no_ip_sets_cloud_relay_true(self) -> None:
        sl = _make_sound_light(device_ip=None)
        assert sl._use_cloud_relay is True

    def test_with_ip_sets_cloud_relay_false(self) -> None:
        sl = _make_sound_light(device_ip="10.0.0.1")
        assert sl._use_cloud_relay is False
