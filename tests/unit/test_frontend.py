"""Tests for the frontend card registration module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.nanit.frontend import (
    _CARD_URL,
    _MANIFEST_VERSION,
    _REGISTERED_KEY,
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
    assert _CARD_URL in call_args["url"]


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


async def test_register_card_skips_update_when_url_matches(hass: HomeAssistant) -> None:
    current_url = f"{_CARD_URL}?v={_MANIFEST_VERSION}"
    resources = _mock_resources([{"id": "res1", "url": current_url}])
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
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
    assert _MANIFEST_VERSION == "1.4.0"


async def test_register_card_loads_resources_when_not_loaded(hass: HomeAssistant) -> None:
    resources = _mock_resources()
    resources.loaded = False
    _setup_hass_for_card(hass, resources)

    with patch(f"{_FRONTEND_MODULE}._CARD_DIR", Path(__file__).parent):
        with patch(f"{_FRONTEND_MODULE}._CARD_FILENAME", "conftest.py"):
            await async_register_card(hass)

    resources.async_load.assert_awaited_once()
    resources.async_create_item.assert_awaited_once()
