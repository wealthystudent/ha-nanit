"""Repair flows for Nanit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_LOCAL_STREAM, DOMAIN
from .local_stream import ISSUE_PROTOCOL, ISSUE_UNREACHABLE

if TYPE_CHECKING:
    from homeassistant.components.repairs import RepairsFlowResult


class LocalStreamRepairFlow(RepairsFlow):
    """Retry local streaming, or turn it off, from its repair issue."""

    def __init__(self, entry_id: str) -> None:
        """Initialize."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Offer the two ways out."""
        # The frontend merges the issue's placeholders into the step text
        # itself. Passing them here too keeps the address visible if that
        # ever changes, since this text is the only place it is shown.
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return self.async_show_menu(
            step_id="init",
            menu_options=["retry", "disable"],
            description_placeholders=dict(issue.translation_placeholders or {})
            if issue is not None
            else None,
        )

    async def async_step_retry(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """Clear the pause so the next viewer tries local again. No reload."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None and entry.domain == DOMAIN:
            data = getattr(entry, "runtime_data", None)
            local = getattr(data, "local_stream", None)
            if local is not None:
                local.async_retry_now()
        return self.async_create_entry(data={})

    async def async_step_disable(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Turn local streaming off (an options change, so the entry reloads)."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None and entry.domain == DOMAIN:
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_LOCAL_STREAM: False}
            )
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow for a Nanit repair issue."""
    if issue_id.startswith((ISSUE_UNREACHABLE, ISSUE_PROTOCOL)) and data:
        entry_id = data.get("entry_id")
        if isinstance(entry_id, str):
            return LocalStreamRepairFlow(entry_id)
    raise ValueError(f"Unknown Nanit repair issue {issue_id}")
