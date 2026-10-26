"""Binary sensors: doors and faults per lift, connection, equipment alarms."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KoneConfigEntry
from .api.models import FAULT_MODE, IN_SERVICE_MODES, LIFT_MODES, MODE_OTHER, mode_code
from .entity import (
    KoneEquipmentEntity,
    KoneGroupEntity,
    KoneLiftEntity,
    equipment_pairs,
    track_lifts,
)
from .hub import GroupState, KoneHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        ConnectedSensor(hub, group) for group in hub.groups.values()
    ]
    if hub.equipment is not None:
        for building_id, ken in equipment_pairs(hub):
            entities += [
                EntrapmentSensor(hub.equipment, building_id, ken),
                OutOfOrderSensor(hub.equipment, building_id, ken),
            ]
    async_add_entities(entities)

    def lift_entities(hub: KoneHub, group: GroupState, lift_id: int):
        return [
            DoorSensor(hub, group, lift_id),
            FaultSensor(hub, group, lift_id),
            InServiceSensor(hub, group, lift_id),
        ]

    track_lifts(hub, lift_entities, async_add_entities)


class DoorSensor(KoneLiftEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "door")

    @property
    def is_on(self) -> bool | None:
        return self.lift.door_open

    @property
    def extra_state_attributes(self):
        return {"door_state": self.lift.door_state}


class FaultSensor(KoneLiftEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "fault")

    @property
    def is_on(self) -> bool | None:
        if self.lift.fault is not None:
            return self.lift.fault
        # No fault field from KONE: the mode says whether it has tripped.
        code = mode_code(self.lift.mode)
        return None if code is None else code == FAULT_MODE


class InServiceSensor(KoneLiftEntity, BinarySensorEntity):
    """On while the car is in normal service (mode 0); off for any other mode."""

    def __init__(self, hub, group, lift_id) -> None:
        super().__init__(hub, group, lift_id, "in_service")

    @property
    def is_on(self) -> bool | None:
        code = mode_code(self.lift.mode)
        return None if code is None else code in IN_SERVICE_MODES

    @property
    def extra_state_attributes(self):
        code = mode_code(self.lift.mode)
        return {"mode": LIFT_MODES.get(code, MODE_OTHER) if code is not None else None,
                "code": code}


class ConnectedSensor(KoneGroupEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub, group) -> None:
        super().__init__(hub, group, "connected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.hub.ws.connected


class EntrapmentSensor(KoneEquipmentEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, building_id, ken) -> None:
        super().__init__(coordinator, building_id, ken, "entrapment")

    @property
    def is_on(self) -> bool | None:
        value = self.status_data.get("entrapment")
        return value if isinstance(value, bool) else None


class OutOfOrderSensor(KoneEquipmentEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, building_id, ken) -> None:
        super().__init__(coordinator, building_id, ken, "out_of_order")

    @property
    def is_on(self) -> bool | None:
        value = self.status_data.get("outOfOrder")
        if isinstance(value, bool):
            return value
        state = self.availability_data.get("state")
        if state is None:
            return None
        return str(state).lower() not in ("in_service", "inservice", "available", "normal")
