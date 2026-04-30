from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit.const import CONF_CAMERA_IPS, CONF_REFRESH_TOKEN, DOMAIN
from custom_components.nanit.hub import NanitHub

from .conftest import (
    MOCK_BABY_1,
    MOCK_BABY_2,
    MOCK_BABY_3,
    MOCK_EMAIL,
    mock_entry_data_v2,
)


def _make_entry(hass: HomeAssistant, options: dict[str, object] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_entry_data_v2(),
        version=2,
        unique_id=MOCK_EMAIL,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _make_mock_camera(uid: str, baby_uid: str) -> MagicMock:
    cam = MagicMock()
    cam.uid = uid
    cam.baby_uid = baby_uid
    cam.connected = True
    cam.state = MagicMock()
    cam.subscribe = MagicMock(return_value=lambda: None)
    cam.async_start = AsyncMock()
    cam.async_stop = AsyncMock()
    return cam


aionanit = importlib.import_module("aionanit")
NanitAuthError = aionanit.NanitAuthError
NanitConnectionError = aionanit.NanitConnectionError


async def test_setup_single_baby(hass: HomeAssistant, mock_nanit_client) -> None:
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    assert len(hub.camera_data) == 1
    assert MOCK_BABY_1.camera_uid in hub.camera_data


async def test_setup_multiple_babies(hass: HomeAssistant, mock_nanit_client) -> None:
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2, MOCK_BABY_3]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    assert len(hub.camera_data) == 3
    assert set(hub.camera_data) == {"cam_1", "cam_2", "cam_3"}


async def test_setup_zero_babies(hass: HomeAssistant, mock_nanit_client) -> None:
    mock_nanit_client.async_get_babies.return_value = []
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    await hub.async_setup()
    assert hub.camera_data == {}


async def test_setup_auth_error_propagates(hass: HomeAssistant, mock_nanit_client) -> None:
    mock_nanit_client.async_get_babies.side_effect = NanitAuthError("expired")
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    try:
        await hub.async_setup()
        raise AssertionError("Expected NanitAuthError")
    except NanitAuthError:
        pass


async def test_setup_partial_failure(hass: HomeAssistant, mock_nanit_client) -> None:
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):

        def push_factory(_hass, _entry, camera, _baby):
            mock = MagicMock()
            if camera.uid == MOCK_BABY_1.camera_uid:
                mock.async_setup = AsyncMock(side_effect=NanitConnectionError("unreachable"))
            else:
                mock.async_setup = AsyncMock()
            return mock

        push_cls.side_effect = push_factory
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    assert len(hub.camera_data) == 1
    assert MOCK_BABY_2.camera_uid in hub.camera_data


async def test_setup_all_cameras_fail_raises(hass: HomeAssistant, mock_nanit_client) -> None:
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls:
        push_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )
        try:
            await hub.async_setup()
            raise AssertionError("Expected NanitConnectionError")
        except NanitConnectionError:
            pass


async def test_setup_reads_camera_ips_from_options(hass: HomeAssistant, mock_nanit_client) -> None:
    entry = _make_entry(hass, options={CONF_CAMERA_IPS: {"cam_1": "10.0.0.8"}})
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    mock_nanit_client.camera.assert_called_once_with(
        uid="cam_1", baby_uid="baby_1", prefer_local=True, local_ip="10.0.0.8"
    )


async def test_camera_connection_failure_creates_repair_issue(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.ir.async_create_issue") as mock_create_issue,
    ):
        push_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )

        try:
            await hub.async_setup()
            raise AssertionError("Expected NanitConnectionError")
        except NanitConnectionError:
            pass

    mock_create_issue.assert_called_once()
    call_args = mock_create_issue.call_args
    assert call_args.args[0] is hass
    assert call_args.args[1] == DOMAIN
    assert call_args.args[2] == f"camera_connection_failed_{MOCK_BABY_1.camera_uid}"
    assert call_args.kwargs["is_fixable"] is False
    assert call_args.kwargs["is_persistent"] is False
    assert call_args.kwargs["translation_key"] == "camera_connection_failed"
    assert call_args.kwargs["translation_placeholders"] == {
        "camera_name": MOCK_BABY_1.name,
        "error": "unreachable",
    }


async def test_successful_camera_setup_deletes_repair_issue(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        patch("custom_components.nanit.hub.ir.async_delete_issue") as mock_delete_issue,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    mock_delete_issue.assert_called_once_with(
        hass, DOMAIN, f"camera_connection_failed_{MOCK_BABY_1.camera_uid}"
    )


async def test_async_close(hass: HomeAssistant, mock_nanit_client) -> None:
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    assert hub.camera_data
    await hub.async_close()
    mock_nanit_client.async_close.assert_awaited_once()
    assert hub.camera_data == {}


async def test_token_refresh_callback(hass: HomeAssistant, mock_nanit_client) -> None:
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    callback = mock_nanit_client.token_manager.on_tokens_refreshed.call_args.args[0]
    callback("new_access", "new_refresh")

    assert entry.data[CONF_ACCESS_TOKEN] == "new_access"
    assert entry.data[CONF_REFRESH_TOKEN] == "new_refresh"
