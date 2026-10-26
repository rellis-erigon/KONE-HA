"""Parsers for KONE payloads (no Home Assistant needed)."""

from __future__ import annotations

from fake_kone import TOPOLOGY

from custom_components.kone.api import LiftState, apply_lift_event, parse_resources, parse_topology
from custom_components.kone.api.auth import TokenManager


def test_resources_with_prefixed_ids():
    res = parse_resources(
        {
            "buildings": [{"id": "building:111", "apiKens": ["ken:1"]}],
            "groups": [{"id": "group:111:1"}, {"id": "group:111:2"}, "group:222:1"],
            "equipments": [{"id": "ken:2", "description": "Escalator"}],
        }
    )
    assert res.buildings["111"].groups == ["1", "2"]
    assert res.buildings["222"].groups == ["1"]
    assert set(res.equipment) == {"ken:1", "ken:2"}
    assert res.equipment["ken:1"].building_id == "111"


def test_resources_tolerates_junk():
    assert parse_resources(None).buildings == {}
    assert parse_resources({"buildings": [None, 3, {}]}).buildings == {}


def test_topology_maps_floors_to_labels():
    topo = parse_topology("building:9", "1", {"data": TOPOLOGY})
    assert set(topo.lifts) == {1, 2}
    assert topo.lifts[1].name == "A"
    assert topo.floor_name(1, 1) == "G"
    assert topo.floor_name(2, 3) == "2"
    assert topo.floor_name(1, 99) is None
    assert topo.area_by_name("1").area_id == 2000


def test_topology_ignores_other_groups():
    data = {"groups": [{"group_id": 2, "lifts": [{"lift_id": 5}]}]}
    assert parse_topology("9", "1", data).lifts == {}


def test_lift_events_merge():
    state = LiftState()
    apply_lift_event(state, "position", {"cur": 4, "adv": 5, "dir": "DOWN", "moving_state": "MOVING"})
    apply_lift_event(state, "doors", {"state": "CLOSING"})
    apply_lift_event(state, "status", {"lift_mode": 0, "fault_active": False})
    assert (state.floor, state.next_floor, state.direction) == (4, 5, "down")
    assert state.door_open is False and state.door_state == "closing"
    assert state.mode == "0" and state.fault is False


def test_hostname_with_scheme():
    tm = TokenManager(None, "a", "b", "http://127.0.0.1:8080")
    assert tm.token_url == "http://127.0.0.1:8080/api/v2/oauth2/token"
    assert tm.ws_base == "ws://127.0.0.1:8080"
    tm = TokenManager(None, "a", "b")
    assert tm.ws_base == "wss://dev.kone.com"


def test_resources_as_the_live_inventory_writes_them():
    """The live inventory: groups as `BUILDING:GROUP`, equipment as bare numbers.

    Scopes need `group:BUILDING:GROUP` and `ken:NNN`; taking these as they
    came made the token request fail (`group:B:B:1`, `rtm/123`).
    """
    res = parse_resources(
        {
            "buildings": [
                {"id": "AbC123", "name": "Tower", "apiKens": ["555"],
                 "groups": [{"id": "AbC123:1", "products": ["callgiving"]}]}
            ],
            "equipments": [{"id": "777", "name": " ", "products": ["rtm", "serviceinfo"]}],
        }
    )
    assert res.buildings["AbC123"].groups == ["1"]
    assert res.buildings["AbC123"].kens == ["ken:555"]
    assert "ken:777" in res.equipment


def test_scopes_from_options_written_either_way():
    from custom_components.kone.hub import buildings_from_options, scopes_for

    buildings = buildings_from_options(
        {"buildings": {"AbC123": {"name": "Tower", "groups": ["AbC123:1", "2"], "kens": ["777", "ken:778"]}}}
    )
    assert scopes_for(buildings, False) == [
        "application/inventory",
        "callgiving/group:AbC123:1",
        "callgiving/group:AbC123:2",
        "rtm/ken:777",
        "rtm/ken:778",
    ]
