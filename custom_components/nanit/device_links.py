"""Device registry lookups across the Home Assistant 2026.8 API change.

HA 2026.8 added `DeviceRegistry.async_get_device_by_identifier` and the
`via_device_id` key of DeviceInfo, and HA 2026.9 deprecated `async_get_device`
and `via_device` (both stop working in 2027.8). HA before 2026.8 has neither
replacement and rejects device info carrying `via_device_id`, so the
integration picks the API by what the running HA provides.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr

HAS_DEVICE_ID_LINKS = hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")


def async_get_device(
    dev_reg: dr.DeviceRegistry, identifier: tuple[str, str], config_entry_id: str
) -> dr.DeviceEntry | None:
    """Return this config entry's device with the given identifier, if any."""
    if HAS_DEVICE_ID_LINKS:
        return dev_reg.async_get_device_by_identifier(identifier, config_entry_id)
    device = dev_reg.async_get_device(identifiers={identifier})
    if device is None or config_entry_id not in device.config_entries:
        return None
    return device
