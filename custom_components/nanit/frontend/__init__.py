"""Frontend card registration for the Nanit integration.

Serves the compiled nanit-card.js as a static file and registers it as a
Lovelace resource so the card appears in the card picker automatically.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

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


def _is_nanit_card_resource(url: str) -> bool:
    """Return true if a Lovelace resource points at a Nanit card bundle."""
    path = urlparse(url).path
    return path == _CARD_URL or (path.endswith(f"/{_CARD_FILENAME}") and "nanit" in path.lower())


# Pre-compute at import time — these files are static (part of the installed
# package) and never change until the next HACS update/reinstall. Reading them
# here avoids blocking I/O inside the async event loop.
_MANIFEST_VERSION: str = "0"
try:
    _manifest: dict[str, Any] = json.loads(
        (Path(__file__).parent.parent / "manifest.json").read_text()
    )
    _MANIFEST_VERSION = str(_manifest.get("version", "0"))
except (FileNotFoundError, json.JSONDecodeError, KeyError):
    pass

_CARD_HASH: str = ""
with contextlib.suppress(FileNotFoundError):
    _CARD_HASH = hashlib.sha256((_CARD_DIR / _CARD_FILENAME).read_bytes()).hexdigest()[:12]

_CARD_RESOURCE_VERSION: str = (
    f"{_MANIFEST_VERSION}-{_CARD_HASH}" if _CARD_HASH else _MANIFEST_VERSION
)


async def async_register_card(hass: HomeAssistant) -> None:
    """Register the Nanit companion card as a static Lovelace resource.

    Safe to call multiple times — registration is idempotent. Uses a
    sentinel in ``hass.data`` to skip work on subsequent config entries.
    """
    if hass.data.get(_REGISTERED_KEY):
        return

    card_path = _CARD_DIR / _CARD_FILENAME
    if not await hass.async_add_executor_job(card_path.is_file):
        _LOGGER.warning("Nanit card JS not found at %s — skipping card registration", card_path)
        hass.data[_REGISTERED_KEY] = False
        return

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_URL_BASE, str(_CARD_DIR), cache_headers=True)]
        )

        lovelace_data: LovelaceData = hass.data["lovelace"]
        if lovelace_data.resource_mode == "yaml":
            _LOGGER.debug("Lovelace in YAML mode — skipping automatic card resource registration")
            hass.data[_REGISTERED_KEY] = True
            return

        resource_url = f"{_CARD_URL}?v={_CARD_RESOURCE_VERSION}"
        resources = cast(ResourceStorageCollection, lovelace_data.resources)

        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True

        existing = [
            item
            for item in resources.async_items()
            if _is_nanit_card_resource(str(item.get("url", "")))
        ]
        if existing:
            primary = existing[0]
            if primary.get("url") != resource_url:
                await resources.async_update_item(primary["id"], {"url": resource_url})
                _LOGGER.debug("Updated Nanit card resource URL to %s", resource_url)
            if len(existing) > 1:
                _LOGGER.warning(
                    "Found %d Nanit card Lovelace resources; keeping %s and leaving duplicates unchanged",
                    len(existing),
                    primary.get("id"),
                )
            hass.data[_REGISTERED_KEY] = True
            return

        await resources.async_create_item({"res_type": "module", "url": resource_url})
        _LOGGER.debug("Registered Nanit card as Lovelace resource: %s", resource_url)
        hass.data[_REGISTERED_KEY] = True
    except Exception:
        hass.data[_REGISTERED_KEY] = False
        raise
