"""Data shapes for the KONE APIs, and tolerant parsers for them.

KONE's identifiers come in two spellings: bare (`9990000951`, group `1`) in
the Elevator Call API v2 payloads, and prefixed (`building:9990000951`,
`group:9990000951:1`) in the Application API and in v1 payloads. Everything
here is stored bare and prefixed only on the way out.

The parsers accept the field names KONE's examples and documentation use,
plus the obvious variants, and keep the raw payload beside the parsed value.
Anything they cannot make sense of is left as `None` rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def bare_building_id(value: str) -> str:
    """`building:123` -> `123`; `123` -> `123`."""
    value = str(value).strip()
    return value.split(":", 1)[1] if value.startswith("building:") else value


def prefixed_building_id(value: str) -> str:
    return f"building:{bare_building_id(value)}"


def split_group_id(value: str) -> tuple[str, str] | None:
    """`group:123:1` or `123:1` -> (`123`, `1`); anything else -> None.

    The live inventory lists a building's groups as `BUILDING:GROUP`, with
    no `group:` prefix; scopes and the docs write `group:BUILDING:GROUP`.
    """
    parts = str(value).strip().split(":")
    if len(parts) == 3 and parts[0] == "group":
        return parts[1], parts[2]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


def ken_id(value: str) -> str:
    """`123` or `ken:123` -> `ken:123`.

    The live inventory lists equipment as bare numbers, but scopes
    (`rtm/ken:123`) and the Equipment Status API (`equipmentIds` must match
    `^ken:.+$`) both need the prefix.
    """
    value = str(value).strip()
    return value if value.startswith("ken:") else f"ken:{value}"


# --------------------------------------------------------------------------
# Application API: what this application may see
# --------------------------------------------------------------------------


@dataclass
class BuildingResource:
    building_id: str
    name: str | None = None
    groups: list[str] = field(default_factory=list)
    kens: list[str] = field(default_factory=list)


@dataclass
class EquipmentResource:
    ken: str
    building_id: str | None = None
    description: str | None = None
    equipment_type: str | None = None
    #: The APIs KONE lists for it (`rtm`, `serviceinfo`); None when it was
    #: only named under a building and the inventory said nothing.
    products: list[str] | None = None

    @property
    def pollable(self) -> bool:
        """Whether Equipment Status (`rtm`) can be asked about it."""
        return self.products is None or "rtm" in self.products


@dataclass
class Resources:
    buildings: dict[str, BuildingResource] = field(default_factory=dict)
    equipment: dict[str, EquipmentResource] = field(default_factory=dict)
    raw: Any = None


def parse_resources(body: Any) -> Resources:
    res = Resources(raw=body)
    if not isinstance(body, dict):
        return res

    for item in body.get("buildings") or []:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not item.get("id"):
            continue
        bid = bare_building_id(item["id"])
        building = res.buildings.setdefault(bid, BuildingResource(bid))
        building.name = item.get("name") or item.get("description") or building.name
        for ken in item.get("apiKens") or item.get("kens") or []:
            ken = ken_id(ken)
            if ken not in building.kens:
                building.kens.append(ken)
        for group in item.get("groups") or []:
            gid = group.get("id") if isinstance(group, dict) else group
            _add_group(res, gid, bid)

    for item in body.get("groups") or []:
        gid = item.get("id") if isinstance(item, dict) else item
        _add_group(res, gid, None)

    for item in body.get("equipments") or body.get("equipment") or []:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not item.get("id"):
            continue
        ken = ken_id(item["id"])
        bid = item.get("buildingId") or item.get("building")
        res.equipment[ken] = EquipmentResource(
            ken=ken,
            building_id=bare_building_id(bid) if bid else None,
            description=_text(item.get("description") or item.get("desc") or item.get("name")),
            equipment_type=item.get("type") or item.get("equipmentType"),
            products=[str(p) for p in item["products"]] if isinstance(item.get("products"), list) else None,
        )

    # Building kens are equipment too, even when KONE only lists them there.
    # When the inventory says which APIs its equipment has, a ken it does not
    # list as equipment has none of them: the live sandbox names each
    # building's own number under `apiKens`, and refuses `rtm` for it.
    described = any(eq.products is not None for eq in res.equipment.values())
    for building in res.buildings.values():
        for ken in building.kens:
            res.equipment.setdefault(
                ken,
                EquipmentResource(
                    ken=ken, building_id=building.building_id,
                    products=[] if described else None,
                ),
            )
    return res


def _text(value: Any) -> str | None:
    """A description, or None for the blanks the live inventory sends (`" "`)."""
    text = str(value).strip() if value is not None else ""
    return text or None


def _add_group(res: Resources, gid: Any, fallback_building: str | None) -> None:
    if gid is None:
        return
    split = split_group_id(str(gid))
    if split is not None:
        bid, group = split
    elif fallback_building is not None:
        bid, group = fallback_building, str(gid)
    else:
        return
    building = res.buildings.setdefault(bid, BuildingResource(bid))
    if group not in building.groups:
        building.groups.append(group)


# --------------------------------------------------------------------------
# Elevator Call API v2 `config`: the group's topology
# --------------------------------------------------------------------------


@dataclass
class Area:
    """A place a passenger can be: a floor landing, usually."""

    area_id: int
    short_name: str
    exit: bool = False
    #: (lift_id, floor) pairs that serve this area, where KONE says so.
    lift_floors: list[tuple[int, int]] = field(default_factory=list)
    #: The live config's way of placing an area: the group's floor and side
    #: (side 1 is the front, 2 the rear: `1` and `1R`).
    group_floor_id: int | None = None
    group_side: int | None = None


@dataclass
class Lift:
    lift_id: int
    name: str
    floors: list[int] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    #: lift_floor_id -> group_floor_id, from the live config's floors[].
    floor_groups: dict[int, int] = field(default_factory=dict)


@dataclass
class Topology:
    building_id: str
    group_id: str
    lifts: dict[int, Lift] = field(default_factory=dict)
    areas: dict[int, Area] = field(default_factory=dict)
    raw: Any = None

    def floor_name(self, lift_id: int, floor: int | None) -> str | None:
        """Landing label (e.g. `G`, `12`) for a lift's floor index."""
        if floor is None:
            return None
        for area in self.areas.values():
            if (lift_id, floor) in area.lift_floors:
                return area.short_name
        # The live config places areas by group floor and side instead.
        lift = self.lifts.get(lift_id)
        group_floor = lift.floor_groups.get(floor) if lift else None
        if group_floor is not None:
            area = self._front_area(group_floor)
            if area is not None:
                return area.short_name
        return None

    def _front_area(self, group_floor: int) -> Area | None:
        """The front (lowest side) area of a group floor."""
        found = [a for a in self.areas.values() if a.group_floor_id == group_floor]
        return min(found, key=lambda a: (a.group_side or 0, a.area_id)) if found else None

    def lift_of_deck(self, area_id: int | None) -> int | None:
        """The lift whose deck has this area id (config `lifts[].decks[]`)."""
        if area_id is None:
            return None
        for lift in self.lifts.values():
            for deck in lift.raw.get("decks") or []:
                if isinstance(deck, dict) and _int(deck.get("area_id")) == area_id:
                    return lift.lift_id
        return None

    def floor_labels(self, lift_id: int) -> list[str]:
        """The landings a lift serves, bottom to top, by their labels."""
        lift = self.lifts.get(lift_id)
        if lift is None:
            return []
        labels = []
        for floor in sorted(lift.floors, key=lambda f: lift.floor_groups.get(f, f)):
            name = self.floor_name(lift_id, floor)
            if name is not None and name not in labels:
                labels.append(name)
        return labels

    def area_by_name(self, name: str) -> Area | None:
        for area in self.areas.values():
            if area.short_name == name:
                return area
        return None

    def sorted_areas(self) -> list[Area]:
        return sorted(self.areas.values(), key=lambda a: a.area_id)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_topology(building_id: str, group_id: str, body: Any) -> Topology:
    """Parse the `data` of a `config` response.

    Accepts either the whole message or just its `data`.
    """
    topo = Topology(bare_building_id(building_id), str(group_id), raw=body)
    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        return topo

    # Lifts are listed under groups[].lifts[] (v2) or directly under lifts[].
    lifts: list[dict] = []
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        gid = group.get("group_id", group.get("groupId"))
        if gid is not None and str(gid) != str(group_id):
            continue
        lifts.extend(lift for lift in group.get("lifts") or [] if isinstance(lift, dict))
    if not lifts:
        lifts = [lift for lift in data.get("lifts") or [] if isinstance(lift, dict)]

    for index, lift in enumerate(lifts, start=1):
        lift_id = _int(lift.get("lift_id", lift.get("liftId", lift.get("id"))))
        if lift_id is None:
            lift_id = index
        floors = []
        floor_groups: dict[int, int] = {}
        for floor in lift.get("floors") or []:
            if isinstance(floor, dict):
                # The live config writes lift_floor_id and group_floor_id.
                number = _int(floor.get("floor", floor.get("floor_id", floor.get("lift_floor_id"))))
                group_floor = _int(floor.get("group_floor_id"))
                if number is not None and group_floor is not None:
                    floor_groups[number] = group_floor
            else:
                number = _int(floor)
            if number is not None:
                floors.append(number)
        topo.lifts[lift_id] = Lift(
            lift_id=lift_id,
            name=str(lift.get("lift_name") or lift.get("liftName") or lift.get("name") or f"Lift {lift_id}"),
            floors=floors,
            raw=lift,
            floor_groups=floor_groups,
        )

    for dest in data.get("destinations") or data.get("areas") or []:
        if not isinstance(dest, dict):
            continue
        area_id = _int(dest.get("area_id", dest.get("areaId", dest.get("id"))))
        if area_id is None:
            continue
        area = Area(
            area_id=area_id,
            short_name=str(dest.get("short_name") or dest.get("shortName") or area_id),
            exit=bool(dest.get("exit") or dest.get("exit_floor") or dest.get("exitFloor")),
            group_floor_id=_int(dest.get("group_floor_id")),
            group_side=_int(dest.get("group_side")),
        )
        for served in dest.get("lifts") or []:
            if not isinstance(served, dict):
                continue
            lid = _int(served.get("lift_id", served.get("liftId")))
            floor = _int(served.get("floor", served.get("floor_id")))
            if lid is not None and floor is not None:
                area.lift_floors.append((lid, floor))
        topo.areas[area_id] = area
    return topo


# --------------------------------------------------------------------------
# Site Monitoring events
# --------------------------------------------------------------------------


#: `lift_mode` in Site Monitoring `status` events, from KONE's Elevator
#: WebSocket API v2 schema (API Portal, "Possible values and their
#: meanings"; -1 is "out of service"). Codes 24 and 25 are not listed
#: there; any code not here reads as "other", with the code kept beside it.
LIFT_MODES: dict[int, str] = {
    -1: "out_of_service",
    0: "normal",
    1: "inspection",
    2: "fireman_service",
    3: "emergency_power",
    4: "attendant",
    5: "freight",
    6: "vip_service",
    7: "out_of_service",
    8: "independent",
    9: "hospital_emergency",
    10: "hospital_bed",
    11: "priority_call",
    12: "priority_call_empty_car",
    13: "separate_collective",
    14: "failed",
    15: "special_parking",
    16: "abnormal_dispatch",
    17: "landing_calls_disconnected",
    18: "proxy_disconnected",
    19: "safety_chain_break",
    20: "earthquake",
    21: "parked_doors_closed",
    22: "car_hospital",
    23: "reserve_dispatch",
    26: "agv",
    27: "wind_reduced_speed",
    28: "storm_stopped",
    29: "reduced_speed",
    30: "evacuation",
    31: "cleaning_pending",
    32: "cleaning",
    33: "maintenance_pending",
    34: "invasion",
    35: "setup_drive",
    36: "substance_release",
    37: "fire_detection",
    38: "fire_machine_room",
    39: "penthouse_visitor",
    40: "priority_goods",
    41: "recover",
    42: "reserved",
    43: "car_to_lobby",
    44: "pet",
    45: "empty_car",
    46: "medical_emergency",
    47: "retail",
    48: "vip_low",
    49: "vip_high",
    50: "vip_only",
    51: "pandemic",
    52: "transformer",
    53: "reserved",
    54: "reserved",
    55: "pit_inspection_failure",
    56: "luggage",
    57: "roof_access",
    58: "pit_access",
}
MODE_OTHER = "other"
#: Modes in which the car serves ordinary passengers: normal, and the ones
#: the schema says still drive normally (special parking: "it drives
#: normally"; strong wind and reduced speed: running, only slower).
IN_SERVICE_MODES = frozenset({0, 15, 27, 29})
#: Modes that are a failure: "lift has failed and is unable to drive",
#: a break in the safety chain, a pit inspection failure.
FAULT_MODES = frozenset({14, 19, 55})


def mode_code(mode: str | None) -> int | None:
    """The numeric `lift_mode`, from the text LiftState keeps."""
    try:
        return int(mode) if mode is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class LiftState:
    """What is known about one lift, merged from monitoring events."""

    floor: int | None = None
    next_floor: int | None = None
    direction: str | None = None
    moving_state: str | None = None
    door_state: str | None = None
    door_open: bool | None = None
    mode: str | None = None
    fault: bool | None = None
    next_stop_eta: Any = None
    last_update: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallState:
    session_id: int | None = None
    request_id: int | None = None
    status: str | None = None
    source: int | None = None
    destination: int | None = None
    lift_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


_DOOR_OPEN = {"OPENED", "OPEN", "OPENING"}
_DOOR_CLOSED = {"CLOSED", "CLOSING", "LOCKED"}


def lift_id_from_topic(target: str) -> int | None:
    """`lift_3` -> 3."""
    if target.startswith("lift_"):
        return _int(target[5:])
    return _int(target)


def apply_lift_event(state: LiftState, kind: str, data: dict) -> None:
    """Merge one `lift_N/<kind>` monitoring event into `state`."""
    state.raw[kind] = data
    state.last_update = data.get("time") or state.last_update

    if kind == "position":
        state.floor = _int(data.get("cur", data.get("floor", state.floor)))
        state.next_floor = _int(data.get("adv", data.get("advanced_floor")))
        if "dir" in data:
            state.direction = _direction(data["dir"])
        if "moving_state" in data:
            state.moving_state = str(data["moving_state"]).lower()
        if isinstance(data.get("door"), bool):
            state.door_open = data["door"]
    elif kind == "doors":
        raw = data.get("state", data.get("door_state"))
        if raw is not None:
            state.door_state = str(raw).lower()
            upper = str(raw).upper()
            if upper in _DOOR_OPEN:
                state.door_open = True
            elif upper in _DOOR_CLOSED:
                state.door_open = False
    elif kind == "status":
        mode = data.get("lift_mode", data.get("mode"))
        if mode is not None:
            state.mode = str(mode)
        fault = data.get("fault_active", data.get("fault"))
        if isinstance(fault, bool):
            state.fault = fault
    elif kind in ("next_stop_eta", "stopping"):
        state.next_stop_eta = data.get("eta", data.get("time", data))


def _direction(value: Any) -> str:
    text = str(value).strip().lower()
    return {"u": "up", "d": "down", "n": "none", "": "none", "0": "none"}.get(text, text)
