"""Base entities and the device tree.

    Building
    ├── Lift group  (call buttons, floor selects, last call)
    │   └── Lift    (floor, direction, doors, mode)
    └── Equipment   (availability, entrapment, maintenance)
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import building_device_identifier, group_device_identifier
from .api import LiftState
from .const import DOMAIN, MANUFACTURER
from .hub import EquipmentCoordinator, GroupState, KoneHub


class KoneHubEntity(Entity):
    """An entity fed by the WebSocket, refreshed on the hub's signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: KoneHub) -> None:
        self.hub = hub

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.hub.signal, self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self.hub.ws.connected


class KoneGroupEntity(KoneHubEntity):
    def __init__(self, hub: KoneHub, group: GroupState, key: str) -> None:
        super().__init__(hub)
        self.group_key = (group.building_id, group.group_id)
        entry_id = hub.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{group.building_id}_{group.group_id}_{key}"
        self._attr_translation_key = key
        building = hub.buildings[group.building_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, group_device_identifier(entry_id, *self.group_key))},
            manufacturer=MANUFACTURER,
            model="Lift group",
            name=f"{building.name} group {group.group_id}",
            via_device_id=hub.device_ids.get(building_device_identifier(entry_id, group.building_id)),
        )

    @property
    def group(self) -> GroupState:
        return self.hub.groups[self.group_key]


class KoneLiftEntity(KoneHubEntity):
    def __init__(self, hub: KoneHub, group: GroupState, lift_id: int, key: str) -> None:
        super().__init__(hub)
        self.group_key = (group.building_id, group.group_id)
        self.lift_id = lift_id
        entry_id = hub.entry.entry_id
        base = f"{entry_id}_{group.building_id}_{group.group_id}_lift{lift_id}"
        self._attr_unique_id = f"{base}_{key}"
        self._attr_translation_key = key
        lift = group.topology.lifts.get(lift_id)
        building = hub.buildings[group.building_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            manufacturer=MANUFACTURER,
            model="Elevator",
            name=f"{building.name} {lift.name if lift else f'Lift {lift_id}'}",
            via_device_id=hub.device_ids.get(group_device_identifier(entry_id, *self.group_key)),
        )

    @property
    def group(self) -> GroupState:
        return self.hub.groups[self.group_key]

    @property
    def lift(self) -> LiftState:
        return self.group.lifts.setdefault(self.lift_id, LiftState())


class KoneEquipmentEntity(CoordinatorEntity[EquipmentCoordinator]):
    """An entity fed by Equipment Status polling, for one `ken:`."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EquipmentCoordinator, building_id: str, ken: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self.ken = ken
        entry_id = coordinator.hub.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{ken}_{key}"
        self._attr_translation_key = key
        building = coordinator.hub.buildings[building_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{ken}")},
            manufacturer=MANUFACTURER,
            model="Equipment",
            name=f"{building.name} {ken}",
            serial_number=ken.removeprefix("ken:"),
            via_device_id=coordinator.hub.device_ids.get(
                building_device_identifier(entry_id, building_id)
            ),
        )

    @property
    def availability_data(self) -> dict:
        return (self.coordinator.data or {}).get(self.ken, {}).get("availability") or {}

    @property
    def status_data(self) -> dict:
        return (self.coordinator.data or {}).get(self.ken, {}).get("status") or {}

    @property
    def service_orders(self) -> list[dict] | None:
        return (self.coordinator.data or {}).get(self.ken, {}).get("service_orders")


def track_lifts(
    hub: KoneHub,
    factory: Callable[[KoneHub, GroupState, int], list[Entity]],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add entities for every lift now, and for lifts that appear later.

    Lifts normally come from the group's topology, but when KONE does not
    answer the `config` request they first show up in monitoring events.
    """
    seen: set[tuple[str, str, int]] = set()

    @callback
    def add_new() -> None:
        new: list[Entity] = []
        for group in hub.groups.values():
            for lift_id in list(group.lifts):
                key = (group.building_id, group.group_id, lift_id)
                if key not in seen:
                    seen.add(key)
                    new.extend(factory(hub, group, lift_id))
        if new:
            async_add_entities(new)

    add_new()
    hub.entry.async_on_unload(async_dispatcher_connect(hub.hass, hub.signal, add_new))


def equipment_pairs(hub: KoneHub) -> list[tuple[str, str]]:
    """(building_id, ken) for every piece of polled equipment."""
    return [(b.building_id, ken) for b in hub.buildings.values() for ken in b.kens]
