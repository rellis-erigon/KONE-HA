"""Call and Cancel buttons for a lift group."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KoneConfigEntry
from .entity import KoneGroupEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[ButtonEntity] = []
    for group in hub.groups.values():
        entities += [
            CallButton(hub, group),
            CancelButton(hub, group),
            RefreshButton(hub, group),
        ]
    async_add_entities(entities)


class CallButton(KoneGroupEntity, ButtonEntity):
    """Destination call from the From floor to the To floor."""

    _attr_icon = "mdi:elevator-passenger"

    def __init__(self, hub, group) -> None:
        super().__init__(hub, group, "call")

    async def async_press(self) -> None:
        group = self.group
        if group.selected_from is None or group.selected_to is None:
            raise HomeAssistantError("Choose a From floor and a To floor first")
        if group.selected_from == group.selected_to:
            raise HomeAssistantError("From and To are the same floor")
        await self.hub.async_call(
            group.building_id, group.group_id, group.selected_from, group.selected_to
        )


class CancelButton(KoneGroupEntity, ButtonEntity):
    _attr_icon = "mdi:cancel"

    def __init__(self, hub, group) -> None:
        super().__init__(hub, group, "cancel_call")

    async def async_press(self) -> None:
        await self.hub.async_cancel(self.group.building_id, self.group.group_id)


class RefreshButton(KoneGroupEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"

    def __init__(self, hub, group) -> None:
        super().__init__(hub, group, "refresh_topology")

    async def async_press(self) -> None:
        await self.hub.async_refresh_topology(self.group.building_id, self.group.group_id)
