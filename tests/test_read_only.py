"""Read-only mode: nothing that changes lift operation can be sent."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.kone.api import KoneControlDisabled, KoneWebSocket, TokenManager
from custom_components.kone.const import DOMAIN, OPT_ALLOW_CONTROL

CALL = "button.test_tower_group_1_call_lift"
CANCEL = "button.test_tower_group_1_cancel_call"
FROM = "select.test_tower_group_1_from_floor"
TO = "select.test_tower_group_1_to_floor"
REFRESH = "button.test_tower_group_1_refresh_floors"
FLOOR_A = "sensor.test_tower_a_floor"


def _lift_calls(kone) -> list[dict]:
    return [m for m in kone.ws_received if str(m.get("type", "")).startswith("lift-call")]


async def _setup(hass, kone, make_entry, allow_control):
    entry = make_entry(kone.url, allow_control=allow_control)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_read_only_has_no_control_entities(hass: HomeAssistant, kone, make_entry) -> None:
    await _setup(hass, kone, make_entry, allow_control=False)
    for entity_id in (CALL, CANCEL, FROM, TO):
        assert hass.states.get(entity_id) is None, entity_id
    # Monitoring and the harmless refresh still work.
    assert hass.states.get(REFRESH) is not None
    await kone.push("lift_1/position", {"cur": 2})
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert hass.states.get(FLOOR_A).state == "1"


async def test_read_only_refuses_services(hass: HomeAssistant, kone, make_entry) -> None:
    await _setup(hass, kone, make_entry, allow_control=False)
    with pytest.raises(ServiceValidationError, match="disabled"):
        await hass.services.async_call(
            DOMAIN, "call_lift", {"from_floor": "G", "to_floor": "2"},
            blocking=True, return_response=True,
        )
    with pytest.raises(ServiceValidationError, match="disabled"):
        await hass.services.async_call(
            DOMAIN, "cancel_call", {"session_id": 1}, blocking=True
        )
    assert _lift_calls(kone) == []


async def test_read_only_client_refuses_even_direct_use(
    hass: HomeAssistant, kone, make_entry
) -> None:
    entry = await _setup(hass, kone, make_entry, allow_control=False)
    ws = entry.runtime_data.ws
    with pytest.raises(KoneControlDisabled):
        await ws.call("9990000951", "1", 1000, 3000)
    with pytest.raises(KoneControlDisabled):
        await ws.cancel("9990000951", "1", 555)
    assert _lift_calls(kone) == []


async def test_turning_control_off_removes_buttons(hass: HomeAssistant, kone, make_entry) -> None:
    entry = await _setup(hass, kone, make_entry, allow_control=True)
    assert hass.states.get(CALL) is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_ALLOW_CONTROL: False}
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for entity_id in (CALL, CANCEL, FROM, TO):
        assert registry.async_get(entity_id) is None, entity_id
        assert hass.states.get(entity_id) is None, entity_id
    assert registry.async_get(REFRESH) is not None
    assert "last_call" in registry.async_get("sensor.test_tower_group_1_last_call").unique_id


async def test_settings_toggle(hass: HomeAssistant, kone, make_entry) -> None:
    entry = await _setup(hass, kone, make_entry, allow_control=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 300, "monitoring": True, "service_info": False, "allow_control": False},
    )
    await hass.async_block_till_done()
    assert entry.options[OPT_ALLOW_CONTROL] is False
    assert entry.runtime_data.ws.read_only is True
    assert hass.states.get(CALL) is None


def test_client_guard_without_home_assistant() -> None:
    ws = KoneWebSocket(None, TokenManager(None, "a", "b"), read_only=True)
    with pytest.raises(KoneControlDisabled):
        ws._guard({"type": "lift-call-api-v2"})
    ws._guard({"type": "site-monitoring"})  # monitoring is never blocked
    ws._guard({"type": "common-api"})
