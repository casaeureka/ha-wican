"""Diagnostics platform for WiCAN integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import WiCANConfigEntry

#: What would let anyone post to the entry's webhook: its id, and the URL that carries it.
TO_REDACT = {CONF_WEBHOOK_ID, "webhook_url"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: WiCANConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = config_entry.runtime_data.coordinator

    entry_data = async_redact_data(dict(config_entry.data), TO_REDACT)

    # The entry's own entities, whatever their entity_id became.
    wican_entities = {}
    for entity in er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id):
        state = hass.states.get(entity.entity_id)
        wican_entities[entity.entity_id] = {
            "state": state.state if state else None,
            "attributes": dict(state.attributes) if state else {},
        }

    return {
        "entry": {
            "title": config_entry.title,
            "entry_id": config_entry.entry_id,
            "unique_id": config_entry.unique_id,
            "data": entry_data,
            "options": dict(config_entry.options),
        },
        "device_info": {
            "fw_version": config_entry.data.get("fw_version"),
            "hw_version": config_entry.data.get("hw_version"),
            "device_id": config_entry.data.get("device_id"),
            "git_version": config_entry.data.get("git_version"),
            "mdns": config_entry.data.get("mdns"),
            "host": config_entry.data.get("host"),
            "ip": config_entry.data.get("ip"),
        },
        "runtime_data": {
            "webhook_id": REDACTED,
            "post_interval": config_entry.runtime_data.post_interval,
            "device_host": config_entry.runtime_data.device_host,
            "device_ip": config_entry.runtime_data.device_ip,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval)
            if coordinator.update_interval
            else None,
            "data_keys": list(coordinator.data.keys()) if coordinator.data else [],
        },
        "entities": wican_entities,
        "entity_count": len(wican_entities),
    }
