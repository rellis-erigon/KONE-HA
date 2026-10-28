"""The running connection for one KONE application (one config entry).

The hub owns the token, the WebSocket and the REST client, holds what is
known about each configured group (topology, per-lift state, the last call)
and tells entities when it changes. Equipment status is polled separately
by `EquipmentCoordinator`, because it is REST and rate limited.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ACTION_DESTINATION,
    SCOPE_INVENTORY,
    CallState,
    KoneAuthError,
    KoneError,
    KoneRestClient,
    KoneWebSocket,
    LiftState,
    Topology,
    TokenManager,
    apply_lift_event,
    callgiving_scope,
    equipment_status_scope,
    lift_id_from_topic,
    parse_topology,
    service_info_scope,
)
from .api.websocket import OUTAGE_GRACE
from .api.models import bare_building_id, ken_id, split_group_id
from .const import (
    B_GROUPS,
    B_KENS,
    B_NAME,
    B_TERMINAL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HOSTNAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TERMINAL,
    DOMAIN,
    OPT_ALLOW_CONTROL,
    OPT_BUILDINGS,
    OPT_MONITORING,
    OPT_SCAN_INTERVAL,
    OPT_SERVICE_INFO,
)

_LOGGER = logging.getLogger(__name__)

GroupKey = tuple[str, str]


@dataclass
class BuildingConfig:
    building_id: str
    name: str
    groups: list[str]
    terminal: int
    kens: list[str]


@dataclass
class GroupState:
    """Everything known about one lift group."""

    building_id: str
    group_id: str
    topology: Topology
    lifts: dict[int, LiftState] = field(default_factory=dict)
    last_call: CallState | None = None
    #: The floors chosen in the group's From/To selects, as area ids.
    selected_from: int | None = None
    selected_to: int | None = None
    actions: Any = None


def buildings_from_options(options: dict[str, Any]) -> dict[str, BuildingConfig]:
    out: dict[str, BuildingConfig] = {}
    for bid, conf in (options.get(OPT_BUILDINGS) or {}).items():
        bid = bare_building_id(bid)
        out[bid] = BuildingConfig(
            building_id=bid,
            name=conf.get(B_NAME) or f"Building {bid}",
            groups=[_group_of(bid, g) for g in conf.get(B_GROUPS) or []],
            terminal=int(conf.get(B_TERMINAL) or DEFAULT_TERMINAL),
            kens=[ken_id(k) for k in conf.get(B_KENS) or []],
        )
    return out


def _group_of(building_id: str, value: object) -> str:
    """A group as its own number, however it was written.

    The inventory, and so the form's default, writes `BUILDING:GROUP`; the
    scope and the WebSocket want the group alone, under its building.
    """
    split = split_group_id(str(value))
    if split is not None and split[0] == building_id:
        return split[1]
    return str(value).strip()


def scopes_for(
    buildings: dict[str, BuildingConfig], service_info: bool
) -> list[str]:
    """The token scopes needed for what has been configured."""
    scopes = [SCOPE_INVENTORY]
    for building in buildings.values():
        scopes += [callgiving_scope(building.building_id, g) for g in building.groups]
        scopes += [equipment_status_scope(ken) for ken in building.kens]
        if service_info:
            scopes += [service_info_scope(ken) for ken in building.kens]
    return scopes


class KoneHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.buildings = buildings_from_options(dict(entry.options))
        self.monitoring = entry.options.get(OPT_MONITORING, True)
        self.service_info = entry.options.get(OPT_SERVICE_INFO, False)
        # Entries made before this option existed were never asked; they
        # keep control rather than silently losing their buttons.
        self.allow_control = bool(entry.options.get(OPT_ALLOW_CONTROL, True))

        session = async_get_clientsession(hass)
        self.tokens = TokenManager(
            session,
            entry.data[CONF_CLIENT_ID],
            entry.data[CONF_CLIENT_SECRET],
            entry.data.get(CONF_HOSTNAME) or "dev.kone.com",
            scopes_for(self.buildings, self.service_info),
        )
        self.rest = KoneRestClient(session, self.tokens)
        self._grace_check: Any = None
        """Cancels the look at availability after a drop's grace period."""
        self.ws = KoneWebSocket(
            session,
            self.tokens,
            on_event=self._on_event,
            on_state=self._on_ws_state,
            client_name=f"ha-{entry.entry_id[:8]}",
            read_only=not self.allow_control,
        )
        self.groups: dict[GroupKey, GroupState] = {}
        # Device registry ids of the building and group devices, by identifier.
        self.device_ids: dict[str, str] = {}
        self.equipment: EquipmentCoordinator | None = None
        self._ws_task: asyncio.Task | None = None

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_update"

    @property
    def has_groups(self) -> bool:
        return any(b.groups for b in self.buildings.values())

    @property
    def all_kens(self) -> list[str]:
        return [ken for b in self.buildings.values() for ken in b.kens]

    # Lifecycle ------------------------------------------------------------

    async def async_start(self) -> None:
        """Connect and learn each group's topology.

        Raises ConfigEntryAuthFailed for bad credentials and KoneError when
        KONE cannot be reached, so setup can retry later.
        """
        try:
            await self.tokens.token(force=True)
        except KoneAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        for building in self.buildings.values():
            for group in building.groups:
                key = (building.building_id, group)
                self.groups[key] = GroupState(
                    building.building_id, group, Topology(building.building_id, group)
                )

        if self.has_groups:
            self._ws_task = self.entry.async_create_background_task(
                self.hass, self.ws.run(), f"{DOMAIN} websocket"
            )
            await self.ws.wait_connected(30)
            for key in self.groups:
                await self.async_refresh_topology(*key)
                if self.monitoring:
                    self.ws.subscribe(*key)

        if self.all_kens:
            self.equipment = EquipmentCoordinator(self.hass, self)
            # Not async_config_entry_first_refresh: equipment that fails to
            # answer (no webhook subscription, an API not subscribed) must not
            # hold back the lifts. Its entities show unavailable instead, and
            # a refused token still starts reauthentication.
            await self.equipment.async_refresh()

    async def async_stop(self) -> None:
        if self._grace_check is not None:
            self._grace_check()
            self._grace_check = None
        await self.ws.stop()
        if self._ws_task is not None:
            self._ws_task.cancel()

    async def async_refresh_topology(self, building_id: str, group_id: str) -> None:
        state = self.groups[(building_id, group_id)]
        try:
            msg = await self.ws.config(building_id, group_id)
            state.topology = parse_topology(building_id, group_id, msg)
        except KoneError as err:
            _LOGGER.warning(
                "No topology for building %s group %s: %s", building_id, group_id, err
            )
        try:
            state.actions = (await self.ws.actions(building_id, group_id)).get("data")
        except KoneError as err:
            _LOGGER.debug("No action list for %s/%s: %s", building_id, group_id, err)
        for lift_id in state.topology.lifts:
            state.lifts.setdefault(lift_id, LiftState())
        self._notify()

    # Events ---------------------------------------------------------------

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def _on_ws_state(self, connected: bool) -> None:
        self._notify()
        if self._grace_check is not None:
            self._grace_check()
            self._grace_check = None
        if not connected:
            # Still available for now; look again once the grace is over.
            @callback
            def _recheck(_now) -> None:
                self._grace_check = None
                self._notify()

            self._grace_check = async_call_later(
                self.hass, OUTAGE_GRACE + 1, _recheck
            )

    def _group_for(self, msg: dict[str, Any]) -> GroupState | None:
        bid = msg.get("buildingId")
        gid = msg.get("groupId")
        if bid is not None and gid is not None:
            return self.groups.get((bare_building_id(bid), str(gid)))
        if len(self.groups) == 1:
            return next(iter(self.groups.values()))
        return None

    @callback
    def _on_event(self, msg: dict[str, Any]) -> None:
        subtopic = msg.get("subtopic")
        data = msg.get("data")
        if not isinstance(data, dict):
            data = {}
        if subtopic is None and isinstance(data.get("subtopic"), str):
            subtopic = data["subtopic"]
        group = self._group_for(msg)
        if group is None or not isinstance(subtopic, str):
            _LOGGER.debug("Unrouted KONE message: %s", msg)
            return

        parts = subtopic.split("/")
        if parts[0] == "call_state" and len(parts) >= 3:
            self._apply_call_state(group, parts[1], parts[2], data)
        elif len(parts) == 2:
            lift_id = lift_id_from_topic(parts[0])
            if lift_id is None:
                return
            lift = group.lifts.setdefault(lift_id, LiftState())
            apply_lift_event(lift, parts[1], data)
        else:
            return
        self._notify()

    def _apply_call_state(
        self, group: GroupState, session: str, status: str, data: dict
    ) -> None:
        call = group.last_call
        if call is None or str(call.session_id) != session:
            # A call placed elsewhere, or before a restart; track it anyway.
            call = CallState(session_id=_int(session))
            group.last_call = call
        call.status = status
        call.raw = data
        lift = next((data[k] for k in ("lift_id", "liftId", "allocated_lift", "lift") if data.get(k) is not None), None)
        if lift is None:
            # Or the car's deck, as an area id: the topology says whose it is.
            for key, value in data.items():
                if "deck" in key.lower():
                    for area in value if isinstance(value, list) else [value]:
                        lift = group.topology.lift_of_deck(_int(area))
                        if lift is not None:
                            break
                if lift is not None:
                    break
        if lift is not None:
            call.lift_id = _int(lift)

    # Calls ----------------------------------------------------------------

    def _require_control(self) -> None:
        if not self.allow_control:
            raise ServiceValidationError(
                "Lift control is disabled for this KONE connection. "
                "Turn on 'Allow lift control' in the integration's settings."
            )

    def building_terminal(self, building_id: str) -> int:
        building = self.buildings.get(building_id)
        return building.terminal if building else DEFAULT_TERMINAL

    async def async_call(
        self,
        building_id: str,
        group_id: str,
        source: int,
        destination: int | None,
        action: int = ACTION_DESTINATION,
        terminal: int | None = None,
    ) -> CallState:
        self._require_control()
        group = self.groups.get((building_id, group_id))
        if group is None:
            raise HomeAssistantError(f"Group {group_id} of building {building_id} is not configured")
        try:
            msg = await self.ws.call(
                building_id,
                group_id,
                source,
                destination,
                action=action,
                terminal=terminal or self.building_terminal(building_id),
            )
        except KoneError as err:
            raise HomeAssistantError(f"KONE refused the call: {err}") from err

        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        call = CallState(
            session_id=_int(data.get("session_id")),
            request_id=_int(msg.get("requestId")),
            status="requested",
            source=source,
            destination=destination,
            raw=msg,
        )
        group.last_call = call
        self._notify()
        if call.session_id is not None:
            try:
                await self.ws.monitor_once(
                    building_id, group_id, [f"call_state/{call.session_id}/+"]
                )
            except KoneError as err:
                _LOGGER.debug("Could not monitor call %s: %s", call.session_id, err)
        return call

    async def async_cancel(
        self, building_id: str, group_id: str, session_id: int | None = None
    ) -> None:
        self._require_control()
        group = self.groups.get((building_id, group_id))
        if session_id is None and group is not None and group.last_call:
            session_id = group.last_call.session_id
        if session_id is None:
            raise HomeAssistantError("There is no call to cancel")
        try:
            await self.ws.cancel(building_id, group_id, session_id)
        except KoneError as err:
            raise HomeAssistantError(f"Cancel failed: {err}") from err
        if group is not None and group.last_call and group.last_call.session_id == session_id:
            group.last_call.status = "cancel_requested"
            self._notify()


class EquipmentCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls Equipment Status 2.0 (and optionally Service Info 2.0).

    Data is {ken: {"availability": {...}, "status": {...}, "service_orders": [...]}}.
    """

    def __init__(self, hass: HomeAssistant, hub: KoneHub) -> None:
        interval = int(hub.entry.options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=hub.entry,
            name=f"{DOMAIN} equipment",
            update_interval=timedelta(seconds=interval),
        )
        self.hub = hub

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        kens = self.hub.all_kens
        out: dict[str, dict[str, Any]] = {ken: {} for ken in kens}
        webhook_refused = False
        try:
            for kind, fetch in (
                ("availability", self.hub.rest.availability),
                ("status", self.hub.rest.status),
            ):
                try:
                    for item in await fetch(kens):
                        ken = ken_id(item["equipmentId"]) if item.get("equipmentId") else None
                        if ken in out:
                            out[ken][kind] = item
                except KoneAuthError:
                    raise
                except KoneError as err:
                    # One API not being subscribed should not hide the other.
                    webhook_refused = webhook_refused or needs_webhook(err)
                    _LOGGER.debug("Equipment %s unavailable: %s", kind, err)
            if self.hub.service_info:
                for ken in kens:
                    try:
                        out[ken]["service_orders"] = await self.hub.rest.service_orders(ken)
                    except KoneError as err:
                        _LOGGER.debug("Service orders for %s: %s", ken, err)
        except KoneAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except KoneError as err:
            raise UpdateFailed(str(err)) from err
        if kens and not any(out.values()):
            if webhook_refused:
                # Said once, in Repairs, rather than as an error every poll.
                ir.async_create_issue(
                    self.hass, DOMAIN, f"{ISSUE_EQUIPMENT_WEBHOOK}_{self.hub.entry.entry_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_EQUIPMENT_WEBHOOK,
                    translation_placeholders={"title": self.hub.entry.title},
                )
                raise UpdateFailed(
                    "KONE answers Equipment Status only once the application has a "
                    "webhook subscription; this integration does not create one"
                )
            raise UpdateFailed("No equipment data returned")
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_EQUIPMENT_WEBHOOK}_{self.hub.entry.entry_id}")
        return out


#: What KONE answers Equipment Status with until the application has a
#: webhook subscription (seen on the live sandbox). This integration never
#: creates one: it would need Home Assistant reachable from the internet.
WEBHOOK_REQUIRED = "webhook subscription"
ISSUE_EQUIPMENT_WEBHOOK = "equipment_needs_webhook"


def needs_webhook(err: Exception) -> bool:
    return WEBHOOK_REQUIRED in str(err).lower()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
