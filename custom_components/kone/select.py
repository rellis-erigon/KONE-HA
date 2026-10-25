"""From/To floor pickers for a lift group, used by its Call button."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import KoneConfigEntry
from .entity import KoneGroupEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(
        entity
        for group in hub.groups.values()
        for entity in (
            FloorSelect(hub, group, "from_floor"),
            FloorSelect(hub, group, "to_floor"),
        )
    )


class FloorSelect(KoneGroupEntity, SelectEntity, RestoreEntity):
    _attr_icon = "mdi:stairs"

    def __init__(self, hub, group, key: str) -> None:
        super().__init__(hub, group, key)
        self._key = key

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and self.current_option is None:
            area = self.group.topology.area_by_name(last.state)
            if area is not None:
                self._set(area.area_id)

    @property
    def options(self) -> list[str]:
        return [a.short_name for a in self.group.topology.sorted_areas()]

    @property
    def current_option(self) -> str | None:
        area_id = (
            self.group.selected_from if self._key == "from_floor" else self.group.selected_to
        )
        area = self.group.topology.areas.get(area_id) if area_id is not None else None
        return area.short_name if area else None

    def _set(self, area_id: int) -> None:
        if self._key == "from_floor":
            self.group.selected_from = area_id
        else:
            self.group.selected_to = area_id

    async def async_select_option(self, option: str) -> None:
        area = self.group.topology.area_by_name(option)
        if area is not None:
            self._set(area.area_id)
            self.async_write_ha_state()
