"""Frontend card registration for the Nanit integration.

Serves the compiled nanit-card.js as a static file and registers it as a
Lovelace resource so the card appears in the card picker automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LovelaceData
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_CARD_DIR = Path(__file__).parent
_CARD_FILENAME = "nanit-card.js"
_URL_BASE = "/nanit-card"
_CARD_URL = f"{_URL_BASE}/{_CARD_FILENAME}"

_REGISTERED_KEY = "nanit_card_registered"

_MANIFEST_VERSION: str = "0"
try:
    import json as _json

    _manifest: dict[str, Any] = _json.loads(
        (Path(__file__).parent.parent / "manifest.json").read_text()
    )
    _MANIFEST_VERSION = str(_manifest.get("version", "0"))
except (FileNotFoundError, _json.JSONDecodeError, KeyError):
    pass


async def async_register_card(hass: HomeAssistant) -> None:
    """Register the Nanit companion card as a static Lovelace resource.

    Safe to call multiple times — registration is idempotent. Uses a
    sentinel in ``hass.data`` to skip work on subsequent config entries.
    """
    if hass.data.get(_REGISTERED_KEY):
        return

    hass.data[_REGISTERED_KEY] = True

    card_path = _CARD_DIR / _CARD_FILENAME
    if not card_path.is_file():
        _LOGGER.warning("Nanit card JS not found at %s — skipping card registration", card_path)
        hass.data[_REGISTERED_KEY] = False
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(_URL_BASE, str(_CARD_DIR), cache_headers=False)]
    )

    lovelace_data: LovelaceData = hass.data["lovelace"]
    if lovelace_data.resource_mode == "yaml":
        _LOGGER.debug("Lovelace in YAML mode — skipping automatic card resource registration")
        return

    resource_url = f"{_CARD_URL}?v={_MANIFEST_VERSION}"
    resources = cast(ResourceStorageCollection, lovelace_data.resources)

    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    existing = [r for r in resources.async_items() if _CARD_URL in r["url"]]
    if existing:
        for item in existing:
            if item["url"] != resource_url:
                await resources.async_update_item(item["id"], {"url": resource_url})
                _LOGGER.debug("Updated Nanit card resource URL to %s", resource_url)
        return

    await resources.async_create_item({"res_type": "module", "url": resource_url})
    _LOGGER.debug("Registered Nanit card as Lovelace resource: %s", resource_url)
