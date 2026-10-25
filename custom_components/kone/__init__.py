"""The KONE integration.

Connects one KONE API Portal application (client id + secret) to Home
Assistant. Each configured building becomes a device; under it, each lift
group, each lift in the group, and each piece of equipment polled through
the Equipment Status API become devices of their own.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .api import ACTION_DESTINATION, KoneError
from .const import (
    DOMAIN,
    MANUFACTURER,
    SERVICE_CALL_LIFT,
    SERVICE_CANCEL_CALL,
    SERVICE_REFRESH_TOPOLOGY,
)
from .hub import KoneHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type KoneConfigEntry = ConfigEntry[KoneHub]

ATTR_BUILDING_ID = "building_id"
ATTR_GROUP_ID = "group_id"
ATTR_FROM = "from_floor"
ATTR_TO = "to_floor"
ATTR_ACTION = "action"
ATTR_TERMINAL = "terminal"
ATTR_SESSION_ID = "session_id"

_TARGET = {
    vol.Optional(ATTR_DEVICE_ID): cv.string,
    vol.Optional(ATTR_BUILDING_ID): cv.string,
    vol.Optional(ATTR_GROUP_ID): cv.string,
}

CALL_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Required(ATTR_FROM): vol.Any(cv.positive_int, cv.string),
        vol.Optional(ATTR_TO): vol.Any(cv.positive_int, cv.string),
        vol.Optional(ATTR_ACTION, default=ACTION_DESTINATION): cv.positive_int,
        vol.Optional(ATTR_TERMINAL): cv.positive_int,
    }
)
CANCEL_SCHEMA = vol.Schema({**_TARGET, vol.Optional(ATTR_SESSION_ID): cv.positive_int})
REFRESH_SCHEMA = vol.Schema(_TARGET)


def group_device_identifier(entry_id: str, building_id: str, group_id: str) -> str:
    return f"{entry_id}_group_{building_id}_{group_id}"


def building_device_identifier(entry_id: str, building_id: str) -> str:
    return f"{entry_id}_building_{building_id}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KoneConfigEntry) -> bool:
    hub = KoneHub(hass, entry)
    try:
        await hub.async_start()
    except ConfigEntryAuthFailed:
        await hub.async_stop()
        raise
    except KoneError as err:
        await hub.async_stop()
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = hub

    registry = dr.async_get(hass)
    for building in hub.buildings.values():
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, building_device_identifier(entry.entry_id, building.building_id))},
            manufacturer=MANUFACTURER,
            model="Building",
            name=building.name,
            serial_number=building.building_id,
        )

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KoneConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: KoneConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: KoneConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing devices for buildings that are no longer configured."""
    hub: KoneHub = entry.runtime_data
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        for building_id in hub.buildings:
            if building_id in identifier:
                return False
    return True


# Services -----------------------------------------------------------------


def _resolve(hass: HomeAssistant, call: ServiceCall) -> tuple[KoneHub, str, str]:
    """Find the hub, building and group a service call is aimed at."""
    hubs: list[KoneHub] = [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
    device_id = call.data.get(ATTR_DEVICE_ID)
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"Unknown device {device_id}")
        for hub in hubs:
            for key in hub.groups:
                ident = group_device_identifier(hub.entry.entry_id, *key)
                if (DOMAIN, ident) in device.identifiers:
                    return hub, key[0], key[1]
        raise ServiceValidationError("That device is not a KONE lift group")

    building_id = call.data.get(ATTR_BUILDING_ID)
    group_id = call.data.get(ATTR_GROUP_ID)
    matches = [
        (hub, key)
        for hub in hubs
        for key in hub.groups
        if (building_id is None or key[0] == str(building_id))
        and (group_id is None or key[1] == str(group_id))
    ]
    if len(matches) == 1:
        hub, (bid, gid) = matches[0]
        return hub, bid, gid
    if not matches:
        raise ServiceValidationError("No configured KONE lift group matches")
    raise ServiceValidationError(
        "More than one KONE lift group matches; give device_id, or building_id and group_id"
    )


def _area(hub: KoneHub, building_id: str, group_id: str, value: Any) -> int:
    """An area id from either an id or a landing name such as `G`."""
    topology = hub.groups[(building_id, group_id)].topology
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        number = int(value)
        if not topology.areas or number in topology.areas:
            return number
    area = topology.area_by_name(str(value))
    if area is None:
        raise ServiceValidationError(f"No floor called {value!r} in group {group_id}")
    return area.area_id


def _register_services(hass: HomeAssistant) -> None:
    async def call_lift(call: ServiceCall) -> ServiceResponse:
        hub, bid, gid = _resolve(hass, call)
        source = _area(hub, bid, gid, call.data[ATTR_FROM])
        dest = (
            _area(hub, bid, gid, call.data[ATTR_TO])
            if call.data.get(ATTR_TO) is not None
            else None
        )
        state = await hub.async_call(
            bid,
            gid,
            source,
            dest,
            action=call.data[ATTR_ACTION],
            terminal=call.data.get(ATTR_TERMINAL),
        )
        return {
            "session_id": state.session_id,
            "building_id": bid,
            "group_id": gid,
            "from_area": source,
            "to_area": dest,
        }

    async def cancel_call(call: ServiceCall) -> None:
        hub, bid, gid = _resolve(hass, call)
        await hub.async_cancel(bid, gid, call.data.get(ATTR_SESSION_ID))

    async def refresh_topology(call: ServiceCall) -> None:
        hub, bid, gid = _resolve(hass, call)
        await hub.async_refresh_topology(bid, gid)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALL_LIFT,
        call_lift,
        schema=CALL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_CALL, cancel_call, schema=CANCEL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_TOPOLOGY, refresh_topology, schema=REFRESH_SCHEMA
    )
