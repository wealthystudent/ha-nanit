"""Cloud-polled coordinators retry a failed poll before going unavailable."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from aionanit.exceptions import NanitAuthError, NanitConnectionError
from custom_components.nanit.const import DOMAIN
from custom_components.nanit.coordinator import (
    NanitCloudCoordinator,
    NanitNetworkCoordinator,
)

from .conftest import MOCK_EMAIL, mock_entry_data_v2

_models = importlib.import_module("aionanit.models")
Baby = _models.Baby
CloudEvent = _models.CloudEvent
NetworkInfo = _models.NetworkInfo

_ERR = NanitConnectionError("Events fetch failed with HTTP 502")


@pytest.fixture(autouse=True)
def _no_retry_wait() -> Iterator[None]:
    with patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (0.0, 0.0)):
        yield


def _make_entry(hass: HomeAssistant, state: ConfigEntryState | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL
    )
    entry.add_to_hass(hass)
    if state is not None:
        entry.mock_state(hass, state)
    return entry


def _baby(network: object = None) -> object:
    return Baby(uid="baby_1", name="Nursery", camera_uid="cam_1", network=network)


def _cloud(
    hass: HomeAssistant,
    fetch: AsyncMock,
    state: ConfigEntryState | None = None,
) -> NanitCloudCoordinator:
    hub = MagicMock()
    hub.client.async_get_events = fetch
    return NanitCloudCoordinator(hass, _make_entry(hass, state), hub, _baby())


def _network(
    hass: HomeAssistant,
    fetch: AsyncMock,
    state: ConfigEntryState | None = None,
) -> NanitNetworkCoordinator:
    hub = MagicMock()
    hub.failed_camera_uids = set()
    hub.async_get_babies_tolerant = fetch
    return NanitNetworkCoordinator(hass, _make_entry(hass, state), hub, _baby())


async def test_cloud_retry_returns_fresh_data(hass: HomeAssistant) -> None:
    fresh = [CloudEvent(event_type="MOTION", timestamp=2.0, baby_uid="baby_1")]
    fetch = AsyncMock(side_effect=[_ERR, fresh])
    coordinator = _cloud(hass, fetch)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == fresh
    assert fetch.await_count == 2


async def test_cloud_goes_unavailable_after_two_retries(hass: HomeAssistant) -> None:
    good = [CloudEvent(event_type="MOTION", timestamp=1.0, baby_uid="baby_1")]
    fetch = AsyncMock(side_effect=[good, _ERR, _ERR, _ERR])
    coordinator = _cloud(hass, fetch)
    await coordinator.async_refresh()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert fetch.await_count == 4


async def test_cloud_each_poll_retries_on_its_own(hass: HomeAssistant) -> None:
    good = [CloudEvent(event_type="SOUND", timestamp=1.0, baby_uid="baby_1")]
    fetch = AsyncMock(side_effect=[_ERR, _ERR, good, _ERR, _ERR, good])
    coordinator = _cloud(hass, fetch)

    for _ in range(2):
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True
        assert coordinator.data == good


async def test_cloud_retries_wait_5_then_10_seconds(hass: HomeAssistant) -> None:
    good: list[object] = []
    coordinator = _cloud(hass, AsyncMock(side_effect=[_ERR, _ERR, good]))

    with (
        patch("custom_components.nanit.coordinator._POLL_RETRY_DELAYS", (5.0, 10.0)),
        patch("custom_components.nanit.coordinator.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        assert await coordinator._async_update_data() == good

    assert sleep.await_args_list == [call(5.0), call(10.0)]


async def test_cloud_setup_does_not_retry(hass: HomeAssistant) -> None:
    fetch = AsyncMock(side_effect=_ERR)
    coordinator = _cloud(hass, fetch, ConfigEntryState.SETUP_IN_PROGRESS)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert fetch.await_count == 1


async def test_cloud_auth_error_is_not_retried(hass: HomeAssistant) -> None:
    fetch = AsyncMock(side_effect=NanitAuthError("Access token invalid"))
    coordinator = _cloud(hass, fetch)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert fetch.await_count == 1


async def test_network_retry_returns_fresh_info(hass: HomeAssistant) -> None:
    info = NetworkInfo(ssid="home", frequency_mhz=5240, signal_dbm=-50)
    fetch = AsyncMock(side_effect=[_ERR, [_baby(network=info)]])
    coordinator = _network(hass, fetch)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == info
    assert fetch.await_count == 2


async def test_network_goes_unavailable_after_two_retries(hass: HomeAssistant) -> None:
    # The camera is missing from /babies, so the last good data is None.
    fetch = AsyncMock(side_effect=[[], _ERR, _ERR, _ERR])
    coordinator = _network(hass, fetch)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.data is None

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert fetch.await_count == 4


async def test_network_setup_does_not_retry(hass: HomeAssistant) -> None:
    fetch = AsyncMock(side_effect=_ERR)
    coordinator = _network(hass, fetch, ConfigEntryState.SETUP_IN_PROGRESS)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert fetch.await_count == 1
