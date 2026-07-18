from __future__ import annotations

import asyncio
import importlib
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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


async def test_setup_camera_timeout_treated_as_connection_failure(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """Camera that hangs during setup is treated like a connection failure.

    Regression for issue #80: an unreachable camera (travel camera powered
    off) could block the entire integration indefinitely.
    """
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_1, MOCK_BABY_2]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        patch("custom_components.nanit.hub._CAMERA_SETUP_TIMEOUT", 0.01),
    ):

        async def _hang_forever():
            await asyncio.sleep(3600)

        def push_factory(_hass, _entry, camera, _baby):
            mock = MagicMock()
            if camera.uid == MOCK_BABY_1.camera_uid:
                mock.async_setup = _hang_forever
            else:
                mock.async_setup = AsyncMock()
            return mock

        push_cls.side_effect = push_factory
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        await hub.async_setup()

    assert len(hub.camera_data) == 1
    assert MOCK_BABY_2.camera_uid in hub.camera_data


async def test_failed_camera_uids_populated(hass: HomeAssistant, mock_nanit_client) -> None:
    """Cameras that fail to connect are tracked in failed_camera_uids."""
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

    assert MOCK_BABY_1.camera_uid in hub.failed_camera_uids
    assert MOCK_BABY_2.camera_uid not in hub.failed_camera_uids


async def test_failed_camera_uid_logs_cloud_status(
    hass: HomeAssistant, mock_nanit_client, caplog: pytest.LogCaptureFixture
) -> None:
    """Log message includes cloud connected=False when camera is known offline."""
    from aionanit.models import Baby

    offline_baby = Baby(
        uid=MOCK_BABY_1.uid,
        name=MOCK_BABY_1.name,
        camera_uid=MOCK_BABY_1.camera_uid,
        camera_connected=False,
    )
    mock_nanit_client.async_get_babies.return_value = [offline_baby]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator"),
        patch("custom_components.nanit.hub.NanitNetworkCoordinator"),
        caplog.at_level("WARNING", logger="custom_components.nanit.hub"),
    ):
        push_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )
        with suppress(NanitConnectionError):
            await hub.async_setup()

    assert "cloud reports connected=False" in caplog.text


async def test_failed_camera_logs_unknown_cloud_status_on_legacy_baby(
    hass: HomeAssistant, mock_nanit_client, caplog: pytest.LogCaptureFixture
) -> None:
    """A Baby from an aionanit wheel without camera_connected must not crash setup."""
    legacy_baby = SimpleNamespace(
        uid=MOCK_BABY_1.uid,
        name=MOCK_BABY_1.name,
        camera_uid=MOCK_BABY_1.camera_uid,
    )
    mock_nanit_client.async_get_babies.return_value = [legacy_baby]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator"),
        patch("custom_components.nanit.hub.NanitNetworkCoordinator"),
        caplog.at_level("WARNING", logger="custom_components.nanit.hub"),
    ):
        push_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )
        with suppress(NanitConnectionError):
            await hub.async_setup()

    assert "cloud connected status unknown" in caplog.text


# ---------------------------------------------------------------------------
# Standalone speaker setup (camera optional)
# ---------------------------------------------------------------------------

Baby = importlib.import_module("aionanit.models").Baby

MOCK_BABY_BOTH = Baby(uid="baby_4", name="Nursery", camera_uid="cam_4", speaker_uid="spk_4")
MOCK_BABY_SPEAKER_ONLY = Baby(uid="baby_5", name="Den", camera_uid="", speaker_uid="spk_5")


def _speaker_patches():
    return (
        patch("custom_components.nanit.hub.NanitSoundLight"),
        patch("custom_components.nanit.hub.NanitSoundLightCoordinator"),
    )


async def test_setup_speaker_only_account(hass: HomeAssistant, mock_nanit_client) -> None:
    """An account whose baby has no camera still gets a working S&L entry."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_SPEAKER_ONLY]
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with sl_patch, coord_patch as coord_cls:
        coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert hub.camera_data == {}
    assert set(hub.speaker_data) == {"spk_5"}
    assert hub.speaker_data["spk_5"].baby is MOCK_BABY_SPEAKER_ONLY
    assert coord_cls.call_args.kwargs["via_camera_uid"] is None
    mock_nanit_client.camera.assert_not_called()


async def test_setup_camera_and_speaker(hass: HomeAssistant, mock_nanit_client) -> None:
    """A baby with both devices gets a camera and a speaker, linked via_device."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_BOTH]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        sl_patch,
        coord_patch as sl_coord_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        sl_coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert set(hub.camera_data) == {"cam_4"}
    assert set(hub.speaker_data) == {"spk_4"}
    assert sl_coord_cls.call_args.kwargs["via_camera_uid"] == "cam_4"
    assert hub.speaker_uid_map == {"baby_4": "spk_4"}


async def test_setup_speaker_only_failure_raises(hass: HomeAssistant, mock_nanit_client) -> None:
    """When the only device on the account fails, setup raises so HA retries."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_SPEAKER_ONLY]
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with sl_patch, coord_patch as coord_cls:
        coord_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )
        with pytest.raises(NanitConnectionError):
            await hub.async_setup()

    assert hub.speaker_data == {}


async def test_setup_speaker_failure_keeps_camera(hass: HomeAssistant, mock_nanit_client) -> None:
    """A failing speaker must not take down a working camera."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_BOTH]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        sl_patch,
        coord_patch as sl_coord_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        sl_coord_cls.return_value = MagicMock(async_setup=AsyncMock(side_effect=ValueError("boom")))
        await hub.async_setup()

    assert set(hub.camera_data) == {"cam_4"}
    assert hub.speaker_data == {}


async def test_setup_camera_failure_keeps_speaker(hass: HomeAssistant, mock_nanit_client) -> None:
    """A failing camera must not take down a working speaker (partial failure)."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_BOTH]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])
    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        sl_patch,
        coord_patch as sl_coord_cls,
    ):
        push_cls.return_value = MagicMock(
            async_setup=AsyncMock(side_effect=NanitConnectionError("unreachable"))
        )
        sl_coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert hub.camera_data == {}
    assert set(hub.speaker_data) == {"spk_4"}
    assert hub.failed_camera_uids == {"cam_4"}


async def test_get_babies_falls_back_to_raw_parse_on_keyerror(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """A /babies row without camera_uid breaks aionanit's parser; the raw parse covers it."""
    mock_nanit_client.async_get_babies.side_effect = KeyError("camera_uid")

    resp = MagicMock()
    resp.status = 200
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(
        return_value={
            "babies": [
                {
                    "uid": "baby_5",
                    "name": "Den",
                    "speaker": {"speaker": {"uid": "spk_5"}},
                }
            ]
        }
    )
    mock_nanit_client.rest_client.base_url = "https://api.example.invalid"
    mock_nanit_client.rest_client.session.get = AsyncMock(return_value=resp)

    entry = _make_entry(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with sl_patch, coord_patch as coord_cls:
        coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert hub.camera_data == {}
    assert set(hub.speaker_data) == {"spk_5"}
    assert hub.babies[0].camera_uid == ""
    assert hub.babies[0].speaker_uid == "spk_5"


async def test_legacy_camera_keyed_speaker_map_translates(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """A persisted speaker map keyed by camera_uid (old shape) still resolves."""
    baby = Baby(uid="baby_4", name="Nursery", camera_uid="cam_4", speaker_uid=None)
    mock_nanit_client.async_get_babies.return_value = [baby]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_entry_data_v2(), "speaker_uid_map": {"cam_4": "spk_4"}},
        version=2,
        unique_id=MOCK_EMAIL,
    )
    entry.add_to_hass(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        sl_patch,
        coord_patch as sl_coord_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        sl_coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert hub.speaker_uid_map == {"baby_4": "spk_4"}
    assert set(hub.speaker_data) == {"spk_4"}
    # The persisted map is re-keyed by baby uid
    assert entry.data["speaker_uid_map"] == {"baby_4": "spk_4"}


async def test_legacy_camera_keyed_speaker_ip_still_applies(
    hass: HomeAssistant, mock_nanit_client
) -> None:
    """A speaker IP stored under the camera_uid (old shape) reaches the facade."""
    mock_nanit_client.async_get_babies.return_value = [MOCK_BABY_BOTH]
    mock_nanit_client.camera.side_effect = lambda **kw: _make_mock_camera(kw["uid"], kw["baby_uid"])
    entry = _make_entry(hass, options={"speaker_ips": {"cam_4": "192.168.1.60"}})
    hub = NanitHub(hass, MagicMock(), entry)

    sl_patch, coord_patch = _speaker_patches()
    with (
        patch("custom_components.nanit.hub.NanitPushCoordinator") as push_cls,
        patch("custom_components.nanit.hub.NanitCloudCoordinator") as cloud_cls,
        patch("custom_components.nanit.hub.NanitNetworkCoordinator") as net_cls,
        sl_patch as sl_cls,
        coord_patch as sl_coord_cls,
    ):
        push_cls.return_value = MagicMock(async_setup=AsyncMock())
        cloud_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        net_cls.return_value = MagicMock(async_config_entry_first_refresh=AsyncMock())
        sl_coord_cls.return_value = MagicMock(async_setup=AsyncMock())
        await hub.async_setup()

    assert sl_cls.call_args.kwargs["device_ip"] == "192.168.1.60"
