"""Diagnostics download: the raw KONE payloads, for checking field names."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import KoneConfigEntry
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET

REDACT = {CONF_CLIENT_SECRET, CONF_CLIENT_ID, "accessToken", "access_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KoneConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
        },
        "scopes": list(hub.tokens.scopes),
        "websocket_connected": hub.ws.connected,
        "allow_control": hub.allow_control,
        "groups": {
            f"{bid}/{gid}": {
                "topology_raw": group.topology.raw,
                "lifts": {lid: asdict(state) for lid, state in group.lifts.items()},
                "actions": group.actions,
                "last_call": asdict(group.last_call) if group.last_call else None,
            }
            for (bid, gid), group in hub.groups.items()
        },
        "equipment": hub.equipment.data if hub.equipment else None,
        "recent_messages": async_redact_data(list(hub.ws.recent), REDACT),
    }
