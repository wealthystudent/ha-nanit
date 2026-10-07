"""Tests for the device registry lookups across the HA 2026.8 API change."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit import device_links
from custom_components.nanit.const import DOMAIN


def _entries(hass: HomeAssistant) -> tuple[MockConfigEntry, MockConfigEntry]:
    ours = MockConfigEntry(domain=DOMAIN)
    other = MockConfigEntry(domain=DOMAIN)
    ours.add_to_hass(hass)
    other.add_to_hass(hass)
    return ours, other


async def test_finds_own_device(hass: HomeAssistant) -> None:
    ours, _ = _entries(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(config_entry_id=ours.entry_id, identifiers={(DOMAIN, "a")})

    assert device_links.async_get_device(dev_reg, (DOMAIN, "a"), ours.entry_id) == device
    assert device_links.async_get_device(dev_reg, (DOMAIN, "b"), ours.entry_id) is None


async def test_legacy_lookup_finds_own_device_and_ignores_other_entries(
    hass: HomeAssistant,
) -> None:
    ours, other = _entries(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(config_entry_id=ours.entry_id, identifiers={(DOMAIN, "a")})
    dev_reg.async_get_or_create(config_entry_id=other.entry_id, identifiers={(DOMAIN, "b")})

    with patch.object(device_links, "HAS_DEVICE_ID_LINKS", False):
        assert device_links.async_get_device(dev_reg, (DOMAIN, "a"), ours.entry_id) == device
        # Matches the new API, which only searches the given entry's devices.
        assert device_links.async_get_device(dev_reg, (DOMAIN, "b"), ours.entry_id) is None


def test_running_ha_has_device_id_links() -> None:
    # The dev environment pins HA 2026.8 or later; this guards the detection.
    assert device_links.HAS_DEVICE_ID_LINKS
