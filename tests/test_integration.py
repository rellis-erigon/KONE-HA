"""End-to-end: set up against the fake KONE server and drive the entities."""

from __future__ import annotations

import asyncio

from fake_kone import BUILDING, GROUP, KEN

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.kone.api import callgiving_scope, equipment_status_scope
from custom_components.kone.const import DOMAIN

FLOOR_A = "sensor.test_tower_a_floor"
DIR_A = "sensor.test_tower_a_direction"
DOOR_A = "binary_sensor.test_tower_a_door"
FROM = "select.test_tower_group_1_from_floor"
TO = "select.test_tower_group_1_to_floor"
CALL = "button.test_tower_group_1_call_lift"
LAST_CALL = "sensor.test_tower_group_1_last_call"
AVAIL = "sensor.test_tower_ken_100001_availability"
ENTRAP = "binary_sensor.test_tower_ken_100001_entrapment"


async def _setup(hass: HomeAssistant, kone, make_entry, **kwargs):
    entry = make_entry(kone.url, **kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _settle(hass: HomeAssistant) -> None:
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()


async def test_setup_builds_device_tree(hass: HomeAssistant, kone, make_entry) -> None:
    entry = await _setup(hass, kone, make_entry)
    assert entry.state is ConfigEntryState.LOADED

    # One token covering inventory, the group and the equipment.
    scopes = kone.token_requests[-1]
    assert callgiving_scope(BUILDING, GROUP) in scopes
    assert equipment_status_scope(KEN) in scopes

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    names = {d.name for d in devices}
    assert {"Test Tower", "Test Tower group 1", "Test Tower A", "Test Tower B",
            f"Test Tower {KEN}"} <= names

    # Topology was asked for, and monitoring subscribed for the group.
    await _settle(hass)
    kinds = [(m.get("type"), m.get("callType")) for m in kone.ws_received]
    assert ("common-api", "config") in kinds
    sub = next(m for m in kone.ws_received if m.get("type") == "site-monitoring")
    assert sub["buildingId"] == f"building:{BUILDING}"
    assert "+/position" in sub["payload"]["subtopics"]

    assert hass.states.get(FROM).attributes["options"] == ["G", "1", "2"]


async def test_monitoring_updates_lift_entities(hass: HomeAssistant, kone, make_entry) -> None:
    await _setup(hass, kone, make_entry)
    await kone.push("lift_1/position", {"cur": 3, "adv": 3, "dir": "UP", "moving_state": "MOVING"})
    await kone.push("lift_1/doors", {"state": "OPENED", "landing": 3})
    await _settle(hass)

    assert hass.states.get(FLOOR_A).state == "2"  # floor index 3 is labelled "2"
    assert hass.states.get(DIR_A).state == "up"
    assert hass.states.get(DOOR_A).state == "on"


async def test_unknown_lift_from_monitoring_gets_entities(
    hass: HomeAssistant, kone, make_entry
) -> None:
    await _setup(hass, kone, make_entry)
    await kone.push("lift_7/position", {"cur": 2})
    await _settle(hass)
    state = hass.states.get("sensor.test_tower_lift_7_floor")
    # Not in the topology, so there is no label: the raw index is shown.
    assert state is not None and state.state == "2"


async def test_call_button_places_destination_call(hass: HomeAssistant, kone, make_entry) -> None:
    await _setup(hass, kone, make_entry)
    await hass.services.async_call("select", "select_option", {"entity_id": FROM, "option": "G"}, blocking=True)
    await hass.services.async_call("select", "select_option", {"entity_id": TO, "option": "2"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": CALL}, blocking=True)
    await _settle(hass)

    call = next(m for m in kone.ws_received if m.get("type") == "lift-call-api-v2")
    assert call["payload"]["area"] == 1000
    assert call["payload"]["call"] == {"action": 2, "destination": 3000}
    assert call["payload"]["terminal"] == 1

    assert hass.states.get(LAST_CALL).attributes["session_id"] == 555
    # The call's own state is then monitored.
    assert any(
        m.get("type") == "site-monitoring" and m["payload"]["subtopics"] == ["call_state/555/+"]
        for m in kone.ws_received
    )

    await kone.push("call_state/555/being_served", {"lift_id": 2})
    await _settle(hass)
    state = hass.states.get(LAST_CALL)
    assert state.state == "being_served"
    assert state.attributes["lift_id"] == 2


async def test_call_lift_service_by_floor_name(hass: HomeAssistant, kone, make_entry) -> None:
    await _setup(hass, kone, make_entry)
    result = await hass.services.async_call(
        DOMAIN, "call_lift", {"from_floor": "1", "to_floor": "G"},
        blocking=True, return_response=True,
    )
    assert result["session_id"] == 555
    assert result["from_area"] == 2000 and result["to_area"] == 1000

    await hass.services.async_call(DOMAIN, "cancel_call", {}, blocking=True)
    await _settle(hass)
    delete = next(m for m in kone.ws_received if m.get("callType") == "delete")
    assert delete["payload"] == {"session_id": 555}


async def test_equipment_status(hass: HomeAssistant, kone, make_entry) -> None:
    kone.entrapment = True
    await _setup(hass, kone, make_entry)
    assert hass.states.get(AVAIL).state == "in_service"
    assert hass.states.get(ENTRAP).state == "on"


async def test_equipment_needing_a_webhook_is_reported_once(
    hass: HomeAssistant, kone, make_entry
) -> None:
    """Without a webhook subscription, KONE refuses Equipment Status: say so in Repairs."""
    from homeassistant.helpers import issue_registry as ir

    kone.require_webhook = True
    entry = await _setup(hass, kone, make_entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"equipment_needs_webhook_{entry.entry_id}")
    assert issue is not None and issue.translation_key == "equipment_needs_webhook"
    # Lift monitoring is not affected.
    assert entry.state is ConfigEntryState.LOADED


async def test_equipment_only_building_needs_no_websocket(
    hass: HomeAssistant, kone, make_entry
) -> None:
    entry = await _setup(hass, kone, make_entry, groups=())
    assert entry.state is ConfigEntryState.LOADED
    assert not kone.ws_received
    assert hass.states.get(AVAIL).state == "in_service"


async def test_reconnects_and_resubscribes(hass: HomeAssistant, kone, make_entry) -> None:
    entry = await _setup(hass, kone, make_entry)
    hub = entry.runtime_data
    subs_before = sum(1 for m in kone.ws_received if m.get("type") == "site-monitoring")
    for ws in list(kone.sockets):
        await ws.close()
    await _settle(hass)
    assert not hub.ws.connected
    await hub.ws.wait_connected(10)
    await asyncio.sleep(0.2)
    subs_after = sum(1 for m in kone.ws_received if m.get("type") == "site-monitoring")
    assert subs_after > subs_before


async def test_a_short_drop_does_not_take_the_lifts_offline(
    hass: HomeAssistant, kone, make_entry, monkeypatch
) -> None:
    # The connection is reopened every hour for a new token; that second
    # must not show every lift going unavailable.
    from custom_components.kone.api import websocket

    entry = await _setup(hass, kone, make_entry)
    hub = entry.runtime_data
    for ws in list(kone.sockets):
        await ws.close()
    await _settle(hass)
    assert not hub.ws.connected
    assert hass.states.get(FLOOR_A).state != "unavailable"
    # A drop longer than the grace does show.
    monkeypatch.setattr(websocket, "OUTAGE_GRACE", 0.0)
    hub._notify()
    await hass.async_block_till_done()
    assert hass.states.get(FLOOR_A).state == "unavailable"


async def test_unload(hass: HomeAssistant, kone, make_entry) -> None:
    entry = await _setup(hass, kone, make_entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_bad_secret_starts_reauth(hass: HomeAssistant, kone, make_entry) -> None:
    entry = make_entry(kone.url)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "client_secret": "wrong"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())
