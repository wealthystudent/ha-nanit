"""Shared fixtures for Nanit integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from aionanit.models import Baby
from custom_components.nanit.const import (
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_UID,
    CONF_REFRESH_TOKEN,
    CONF_STORE_CREDENTIALS,
)

MOCK_EMAIL = "test@example.com"
MOCK_PASSWORD = "secret123"
MOCK_ACCESS_TOKEN = "mock_access_token"
MOCK_REFRESH_TOKEN = "mock_refresh_token"
MOCK_MFA_TOKEN = "mock_mfa_token"

MOCK_BABY_1 = Baby(uid="baby_1", name="Nursery", camera_uid="cam_1")
MOCK_BABY_2 = Baby(uid="baby_2", name="Bedroom", camera_uid="cam_2")
MOCK_BABY_3 = Baby(uid="baby_3", name="Playroom", camera_uid="cam_3")


def mock_entry_data_v2(
    *,
    store_credentials: bool = True,
) -> dict:
    data = {
        CONF_ACCESS_TOKEN: MOCK_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN: MOCK_REFRESH_TOKEN,
        CONF_STORE_CREDENTIALS: store_credentials,
        CONF_EMAIL: MOCK_EMAIL,
    }
    if store_credentials:
        data[CONF_PASSWORD] = MOCK_PASSWORD
    return data


def mock_entry_data_v1() -> dict:
    return {
        CONF_ACCESS_TOKEN: MOCK_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN: MOCK_REFRESH_TOKEN,
        CONF_STORE_CREDENTIALS: True,
        CONF_EMAIL: MOCK_EMAIL,
        CONF_PASSWORD: MOCK_PASSWORD,
        CONF_BABY_UID: MOCK_BABY_1.uid,
        CONF_CAMERA_UID: MOCK_BABY_1.camera_uid,
        CONF_BABY_NAME: MOCK_BABY_1.name,
        CONF_CAMERA_IP: "192.168.1.10",
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return snapshot assertion fixture with the Home Assistant extension.

    Explicitly override to ensure consistent snapshot directory (``snapshots/``)
    regardless of plugin load order between syrupy and
    pytest-homeassistant-custom-component.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def mock_nanit_client():
    """Patch NanitClient and NanitCloudCoordinator for the entire integration."""
    with (
        patch("custom_components.nanit.hub.NanitClient", autospec=True) as mock_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as mock_cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as mock_net_cls,
    ):
        client = mock_cls.return_value

        mock_tm = MagicMock()
        mock_tm.on_tokens_refreshed.return_value = lambda: None
        mock_tm.async_get_access_token = AsyncMock(return_value=MOCK_ACCESS_TOKEN)
        client.token_manager = mock_tm

        client.restore_tokens = MagicMock()
        client.async_get_babies = AsyncMock(return_value=[MOCK_BABY_1])
        client.async_close = AsyncMock()

        mock_camera = MagicMock()
        mock_camera.uid = MOCK_BABY_1.camera_uid
        mock_camera.baby_uid = MOCK_BABY_1.uid
        mock_camera.connected = True
        mock_camera.state = MagicMock()
        mock_camera.subscribe = MagicMock(return_value=lambda: None)
        mock_camera.async_start = AsyncMock()
        mock_camera.async_stop = AsyncMock()
        client.camera.return_value = mock_camera

        client.rest_client = MagicMock()
        client.rest_client.async_get_events = AsyncMock(return_value=[])

        mock_cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        mock_net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())

        yield client


@pytest.fixture
def mock_config_flow_client():
    """Patch NanitClient for config flow tests (different import path)."""
    with (
        patch("custom_components.nanit.config_flow.NanitClient", autospec=True) as mock_cls,
        patch("custom_components.nanit.async_setup_entry", return_value=True),
        patch("custom_components.nanit.async_unload_entry", return_value=True),
    ):
        client = mock_cls.return_value
        client.async_login = AsyncMock(
            return_value={
                "access_token": MOCK_ACCESS_TOKEN,
                "refresh_token": MOCK_REFRESH_TOKEN,
            }
        )
        client.async_verify_mfa = AsyncMock(
            return_value={
                "access_token": MOCK_ACCESS_TOKEN,
                "refresh_token": MOCK_REFRESH_TOKEN,
            }
        )
        client.restore_tokens = MagicMock()
        client.async_get_babies = AsyncMock(return_value=[MOCK_BABY_1])
        yield client
