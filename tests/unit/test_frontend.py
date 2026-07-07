"""Tests for the frontend card registration module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.nanit.frontend import (
    _CARD_URL,
    _REGISTERED_KEY,
    _card_resource_version,
    _is_nanit_card_resource,
    _load_manifest_version,
    async_register_card,
)

_FRONTEND_MODULE = "custom_components.nanit.frontend"


@pytest.fixture(autouse=True)
def _reset_card_sentinel(hass: HomeAssistant) -> None:
    """Clear the card-registered sentinel before each test."""
    hass.data.pop(_REGISTERED_KEY, None)


def _mock_resources(items: list[dict] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.loaded = True
    mock.async_items.return_value = items or []
    mock.async_create_item = AsyncMock()
    mock.async_update_item = AsyncMock()
    mock.async_load = AsyncMock()
    return mock


def _mock_lovelace(resources: MagicMock) -> MagicMock:
    lovelace = MagicMock()
    lovelace.resource_mode = "storage"
    lovelace.resources = resources
    return lovelace


def _setup_hass_for_card(hass: HomeAssistant, resources: MagicMock) -> None:
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.data["lovelace"] = _mock_lovelace(resources)


async def test_register_card_creates_resource(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    assert hass.data[_REGISTERED_KEY] is True
    resources.async_create_item.assert_awaited_once()
    call_args = resources.async_create_item.call_args[0][0]
    assert call_args["res_type"] == "module"
    assert call_args["url"].startswith(f"{_CARD_URL}?v=")


async def test_register_card_idempotent(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)
            await async_register_card(hass)

    resources.async_create_item.assert_awaited_once()


async def test_register_card_updates_existing_resource(hass: HomeAssistant) -> None:
    old_url = f"{_CARD_URL}?v=0.0.0"
    resources = _mock_resources([{"id": "res1", "url": old_url}])
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_awaited_once()


async def test_register_card_updates_manual_hacs_resource(hass: HomeAssistant) -> None:
    resources = _mock_resources([{"id": "res1", "url": "/hacsfiles/ha-nanit/conftest.py?v=old"}])
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_awaited_once()


async def test_register_card_updates_only_primary_duplicate(hass: HomeAssistant) -> None:
    resources = _mock_resources(
        [
            {"id": "res1", "url": f"{_CARD_URL}?v=old"},
            {"id": "res2", "url": "/hacsfiles/ha-nanit/nanit-card.js?v=old"},
        ]
    )
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_awaited_once()
    assert resources.async_update_item.call_args.args[0] == "res1"


async def test_register_card_skips_update_when_url_matches(hass: HomeAssistant) -> None:
    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            current_url = f"{_CARD_URL}?v={_card_resource_version()}"
            resources = _mock_resources([{"id": "res1", "url": current_url}])
            _setup_hass_for_card(hass, resources)

            await async_register_card(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_not_awaited()


async def test_register_card_missing_js_skips(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path("/nonexistent")):
        await async_register_card(hass)

    assert hass.data[_REGISTERED_KEY] is False
    resources.async_create_item.assert_not_awaited()


async def test_register_card_yaml_mode_skips_resource(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    _setup_hass_for_card(hass, resources)
    hass.data["lovelace"].resource_mode = "yaml"

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    resources.async_create_item.assert_not_awaited()


def test_manifest_version_is_loaded() -> None:
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parent.parent.parent
            / "custom_components"
            / "nanit"
            / "manifest.json"
        ).read_text()
    )
    assert manifest["version"] == _load_manifest_version()


async def test_register_card_loads_resources_when_not_loaded(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    resources.loaded = False
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    resources.async_load.assert_awaited_once()
    resources.async_create_item.assert_awaited_once()


def test_is_nanit_card_resource_matches_known_paths() -> None:
    assert _is_nanit_card_resource(f"{_CARD_URL}?v=123")
    assert _is_nanit_card_resource("/hacsfiles/ha-nanit/nanit-card.js?v=123")
    assert _is_nanit_card_resource("/local/community/nanit/nanit-card.js")
    assert not _is_nanit_card_resource("/local/community/other-card.js")
