"""Sensors: lift position and motion, the last call, equipment status."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KoneConfigEntry
from .entity import (
    KoneEquipmentEntity,
    KoneGroupEntity,
    KoneLiftEntity,
    equipment_pairs,
    track_lifts,
)
from .hub import GroupState, KoneHub

DIRECTIONS = ["up", "down", "none"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SensorEntity] = [
        LastCallSensor(hub, group) for group in hub.groups.values()
    ]
    if hub.equipment is not None:
        for building_id, ken in equipment_pairs(hub):
            entities += [
                AvailabilitySensor(hub.equipment, building_id, ken),
                MaintenanceSensor(hub.equipment, building_id, ken),
            ]
            if hub.service_info:
                entities.append(ServiceOrdersSensor(hub.equipment, building_id, ken))
    async_add_entities(entities)

    def lift_entities(hub: KoneHub, group: GroupState, lift_id: int):
        return [
            FloorSensor(hub, group, lift_id),
            DirectionSensor(hub, group, lift_id),
            MotionSensor(hub, group, lift_id),
            ModeSensor(hub, group, lift_id),
        ]

    track_lifts(hub, lift_entities, async_add_entities)


# Lifts --------------------------------------------------------------------


class FloorSensor(KoneLiftEntity, SensorEntity):
    """The landing the car is at, by its button label where known."""

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "floor")
        self._attr_icon = "mdi:elevator"

    @property
    def native_value(self) -> str | None:
        floor = self.lift.floor
        if floor is None:
            return None
        return self.group.topology.floor_name(self.lift_id, floor) or str(floor)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lift = self.lift
        return {
            "floor_index": lift.floor,
            "next_floor_index": lift.next_floor,
            "next_floor": self.group.topology.floor_name(self.lift_id, lift.next_floor),
            "last_update": lift.last_update,
        }


class DirectionSensor(KoneLiftEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DIRECTIONS

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "direction")

    @property
    def native_value(self) -> str | None:
        direction = self.lift.direction
        return direction if direction in DIRECTIONS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"raw": self.lift.direction}


class MotionSensor(KoneLiftEntity, SensorEntity):
    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "motion")

    @property
    def native_value(self) -> str | None:
        return self.lift.moving_state


class ModeSensor(KoneLiftEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "mode")

    @property
    def native_value(self) -> str | None:
        return self.lift.mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self.lift.raw.get("status")}


# Groups -------------------------------------------------------------------


class LastCallSensor(KoneGroupEntity, SensorEntity):
    def __init__(self, hub, group) -> None:
        super().__init__(hub, group, "last_call")
        self._attr_icon = "mdi:gesture-tap-button"

    @property
    def native_value(self) -> str | None:
        call = self.group.last_call
        return call.status if call else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        call = self.group.last_call
        topo = self.group.topology
        attrs: dict[str, Any] = {
            "lifts": len(topo.lifts),
            "floors": [a.short_name for a in topo.sorted_areas()],
            "actions": self.group.actions,
        }
        if call is not None:
            src = topo.areas.get(call.source) if call.source is not None else None
            dst = topo.areas.get(call.destination) if call.destination is not None else None
            attrs.update(
                session_id=call.session_id,
                from_floor=src.short_name if src else call.source,
                to_floor=dst.short_name if dst else call.destination,
                lift_id=call.lift_id,
            )
        return attrs


# Equipment ----------------------------------------------------------------


class AvailabilitySensor(KoneEquipmentEntity, SensorEntity):
    def __init__(self, coordinator, building_id, ken) -> None:
        super().__init__(coordinator, building_id, ken, "availability")

    @property
    def native_value(self) -> str | None:
        state = self.availability_data.get("state")
        return str(state).lower() if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.availability_data
        return {
            "previous_state": data.get("previousState"),
            "service_mode": data.get("serviceMode"),
            "active_alerts": data.get("activeAlertCount"),
            "last_update": data.get("lastUpdate"),
        }


class MaintenanceSensor(KoneEquipmentEntity, SensorEntity):
    def __init__(self, coordinator, building_id, ken) -> None:
        super().__init__(coordinator, building_id, ken, "maintenance")

    @property
    def native_value(self) -> str | None:
        maintenance = self.status_data.get("maintenance") or {}
        return maintenance.get("status") or self.status_data.get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.status_data
        return {
            "equipment_status": data.get("equipmentStatus"),
            "out_of_order_since": data.get("equipmentOutOfOrderDate"),
            "back_in_order": data.get("equipmentBackInOrderDate"),
            "location": data.get("equipmentLocationDescription"),
            "equipment_type": data.get("equipmentType"),
        }


class ServiceOrdersSensor(KoneEquipmentEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, building_id, ken) -> None:
        super().__init__(coordinator, building_id, ken, "service_orders")
        self._attr_icon = "mdi:wrench-clock"

    @property
    def native_value(self) -> int | None:
        orders = self.service_orders
        return len(orders) if orders is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"orders": (self.service_orders or [])[:10]}
