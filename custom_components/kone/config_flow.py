"""Config flow: credentials first, then the buildings to add.

Anyone with a KONE API Portal application can use this with their own
client id and secret. After the credentials are checked, the buildings the
application has been granted are offered; for each one chosen, its lift
groups and the equipment to poll are confirmed. Buildings can be added,
edited and removed later from the integration's Configure menu.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DEFAULT_HOSTNAME,
    SCOPE_INVENTORY,
    KoneAuthError,
    KoneError,
    KoneRestClient,
    Resources,
    TokenManager,
)
from .api.models import BuildingResource, bare_building_id
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
    MIN_SCAN_INTERVAL,
    OPT_ALLOW_CONTROL,
    OPT_BUILDINGS,
    OPT_MONITORING,
    OPT_SCAN_INTERVAL,
    OPT_SERVICE_INFO,
)
from .hub import buildings_from_options, scopes_for

_LOGGER = logging.getLogger(__name__)

F_BUILDINGS = "buildings"
F_MANUAL = "manual_building_ids"


def _credentials_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CLIENT_ID, default=defaults.get(CONF_CLIENT_ID, "")): str,
            vol.Required(CONF_CLIENT_SECRET): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_HOSTNAME, default=defaults.get(CONF_HOSTNAME, DEFAULT_HOSTNAME)
            ): str,
        }
    )


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    # Read-only unless the user opts in: a mistyped automation should not be
    # able to move a lift in an occupied building.
    return _credentials_schema(defaults).extend(
        {vol.Required(OPT_ALLOW_CONTROL, default=defaults.get(OPT_ALLOW_CONTROL, False)): bool}
    )


async def _discover(hass, data: Mapping[str, Any]) -> Resources:
    """Check credentials and list what the application may see."""
    session = async_get_clientsession(hass)
    tokens = TokenManager(
        session,
        data[CONF_CLIENT_ID],
        data[CONF_CLIENT_SECRET],
        data.get(CONF_HOSTNAME) or DEFAULT_HOSTNAME,
        [SCOPE_INVENTORY],
    )
    return await KoneRestClient(session, tokens).resources()


class ScopeRefused(KoneAuthError):
    """KONE would not grant a token for the configuration; `refused` says what."""

    def __init__(self, refused: list[str]) -> None:
        super().__init__("Refused: " + (", ".join(refused) or "the token request"))
        self.refused = refused

    @property
    def text(self) -> str:
        return ", ".join(self.refused) or "the whole request (check the client ID and secret)"


async def _check_scopes(hass, data: Mapping[str, Any], options: Mapping[str, Any]) -> None:
    """Ask for a token with every scope the configuration needs.

    KONE refuses the whole request when one scope is not granted, without
    saying which. So when it does, each scope is asked for on its own, and
    the ones refused are named (ScopeRefused), for the form to show.
    """
    buildings = buildings_from_options(dict(options))
    scopes = scopes_for(buildings, bool(options.get(OPT_SERVICE_INFO)))

    def tokens(wanted) -> TokenManager:
        return TokenManager(
            async_get_clientsession(hass),
            data[CONF_CLIENT_ID],
            data[CONF_CLIENT_SECRET],
            data.get(CONF_HOSTNAME) or DEFAULT_HOSTNAME,
            wanted,
        )

    try:
        await tokens(scopes).token(force=True)
    except KoneAuthError:
        refused = []
        for scope in scopes:
            if scope == SCOPE_INVENTORY:
                continue
            try:
                await tokens((SCOPE_INVENTORY, scope)).token(force=True)
            except KoneAuthError:
                refused.append(scope)
        raise ScopeRefused(refused) from None


def _parse_list(text: str) -> list[str]:
    return [part.strip() for part in str(text or "").replace(";", ",").split(",") if part.strip()]


def _building_label(building: BuildingResource) -> str:
    return f"{building.name} ({building.building_id})" if building.name else building.building_id


def _building_schema(
    resources: Resources | None,
    building_id: str,
    current: Mapping[str, Any] | None,
) -> vol.Schema:
    discovered = resources.buildings.get(building_id) if resources else None
    current = current or {}
    groups = current.get(B_GROUPS) or (
        discovered.groups if discovered and discovered.groups else ["1"]
    )
    # A re-shown form hands back the text the user typed, not a list.
    groups_default = groups if isinstance(groups, str) else ",".join(groups)
    ken_options: list[str] = []
    if resources:
        # Only what Equipment Status can be asked about: offering the rest
        # makes the scope check fail for the whole configuration.
        ken_options = [
            ken
            for ken, eq in resources.equipment.items()
            if eq.building_id in (None, building_id) and eq.pollable
        ]
    for ken in current.get(B_KENS) or []:
        if ken not in ken_options:
            ken_options.append(ken)
    labels = []
    for ken in ken_options:
        eq = resources.equipment.get(ken) if resources else None
        desc = (eq.description or eq.equipment_type) if eq else None
        labels.append(SelectOptionDict(value=ken, label=f"{ken} – {desc}" if desc else ken))

    return vol.Schema(
        {
            vol.Required(
                B_NAME,
                default=current.get(B_NAME)
                or (discovered.name if discovered and discovered.name else f"Building {building_id}"),
            ): str,
            vol.Optional(B_GROUPS, default=groups_default): str,
            vol.Required(
                B_TERMINAL, default=int(current.get(B_TERMINAL) or DEFAULT_TERMINAL)
            ): NumberSelector(NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)),
            vol.Optional(B_KENS, default=list(current.get(B_KENS) or [])): SelectSelector(
                SelectSelectorConfig(
                    options=labels, multiple=True, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


def _building_from_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        B_NAME: user_input[B_NAME].strip(),
        B_GROUPS: _parse_list(user_input.get(B_GROUPS, "")),
        B_TERMINAL: int(user_input[B_TERMINAL]),
        B_KENS: [k if k.startswith("ken:") else f"ken:{k}" for k in user_input.get(B_KENS) or []],
    }


def _choose_schema(resources: Resources | None, exclude: set[str]) -> vol.Schema:
    options = [
        SelectOptionDict(value=b.building_id, label=_building_label(b))
        for b in (resources.buildings.values() if resources else [])
        if b.building_id not in exclude
    ]
    schema: dict[Any, Any] = {}
    if options:
        schema[vol.Optional(F_BUILDINGS, default=[])] = SelectSelector(
            SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
        )
    schema[vol.Optional(F_MANUAL, default="")] = str
    return vol.Schema(schema)


def _chosen(user_input: Mapping[str, Any]) -> list[str]:
    ids = list(user_input.get(F_BUILDINGS) or [])
    ids += [bare_building_id(b) for b in _parse_list(user_input.get(F_MANUAL, ""))]
    return list(dict.fromkeys(ids))


class KoneConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._resources: Resources | None = None
        self._queue: list[str] = []
        self._buildings: dict[str, dict[str, Any]] = {}
        self._allow_control = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return KoneOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_CLIENT_ID] = user_input[CONF_CLIENT_ID].strip()
            await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
            self._abort_if_unique_id_configured()
            try:
                self._resources = await _discover(self.hass, user_input)
            except KoneAuthError:
                errors["base"] = "invalid_auth"
            except KoneError as err:
                _LOGGER.warning("KONE discovery failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._allow_control = bool(user_input.pop(OPT_ALLOW_CONTROL, False))
                self._data = user_input
                return await self.async_step_choose()
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_choose(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._queue = _chosen(user_input)
            if self._queue:
                return await self.async_step_building()
            errors["base"] = "no_buildings"
        found = len(self._resources.buildings) if self._resources else 0
        return self.async_show_form(
            step_id="choose",
            data_schema=_choose_schema(self._resources, set()),
            errors=errors,
            description_placeholders={"found": str(found)},
        )

    async def async_step_building(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        building_id = self._queue[0]
        errors: dict[str, str] = {}
        if user_input is not None:
            building = _building_from_input(user_input)
            if not building[B_GROUPS] and not building[B_KENS]:
                errors["base"] = "nothing_selected"
            else:
                self._buildings[building_id] = building
                self._queue.pop(0)
                if self._queue:
                    return await self.async_step_building()
                return await self._async_finish()
        return self.async_show_form(
            step_id="building",
            data_schema=_building_schema(self._resources, building_id, user_input),
            errors=errors,
            description_placeholders={"building_id": building_id},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        options = {
            OPT_BUILDINGS: self._buildings,
            OPT_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            OPT_MONITORING: True,
            OPT_SERVICE_INFO: False,
            OPT_ALLOW_CONTROL: self._allow_control,
        }
        try:
            await _check_scopes(self.hass, self._data, options)
        except ScopeRefused as err:
            # Start over on the building list so the user can drop the one
            # the application has not been granted.
            self._buildings = {}
            return self.async_show_form(
                step_id="choose",
                data_schema=_choose_schema(self._resources, set()),
                errors={"base": "scope_refused"},
                description_placeholders={
                    "found": str(len(self._resources.buildings) if self._resources else 0),
                    "refused": err.text,
                },
            )
        except KoneError:
            return self.async_abort(reason="cannot_connect")
        title = ", ".join(b[B_NAME] for b in self._buildings.values()) or "KONE"
        return self.async_create_entry(title=title, data=self._data, options=options)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            try:
                await _check_scopes(self.hass, data, entry.options)
            except KoneAuthError:
                errors["base"] = "invalid_auth"
            except KoneError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(entry.data),
            errors=errors,
        )


class KoneOptionsFlow(OptionsFlow):
    """Add, edit and remove buildings; tune polling."""

    def __init__(self) -> None:
        self._resources: Resources | None = None
        self._queue: list[str] = []
        self._buildings: dict[str, dict[str, Any]] | None = None

    @property
    def _current(self) -> dict[str, dict[str, Any]]:
        if self._buildings is None:
            self._buildings = {
                k: dict(v) for k, v in (self.config_entry.options.get(OPT_BUILDINGS) or {}).items()
            }
        return self._buildings

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add_building", "settings"]
        if self._current:
            menu[1:1] = ["edit_building", "remove_building"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def _save(self, **changes: Any) -> ConfigFlowResult:
        options = {**self.config_entry.options, OPT_BUILDINGS: self._current, **changes}
        return self.async_create_entry(data=options)

    # Add ------------------------------------------------------------------

    async def async_step_add_building(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._resources is None:
            try:
                self._resources = await _discover(self.hass, self.config_entry.data)
            except KoneError as err:
                _LOGGER.warning("KONE discovery failed: %s", err)
                self._resources = Resources()
        if user_input is not None:
            self._queue = [b for b in _chosen(user_input) if b not in self._current]
            if self._queue:
                return await self.async_step_building()
            errors["base"] = "no_buildings"
        return self.async_show_form(
            step_id="add_building",
            data_schema=_choose_schema(self._resources, set(self._current)),
            errors=errors,
        )

    async def async_step_building(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        building_id = self._queue[0]
        errors: dict[str, str] = {}
        refused = ""
        if user_input is not None:
            building = _building_from_input(user_input)
            if not building[B_GROUPS] and not building[B_KENS]:
                errors["base"] = "nothing_selected"
            else:
                previous = self._current.get(building_id)
                self._current[building_id] = building
                try:
                    await _check_scopes(
                        self.hass,
                        self.config_entry.data,
                        {**self.config_entry.options, OPT_BUILDINGS: self._current},
                    )
                except ScopeRefused as err:
                    if previous is None:
                        self._current.pop(building_id)
                    else:
                        self._current[building_id] = previous
                    errors["base"] = "scope_refused"
                    refused = err.text
                except KoneError:
                    errors["base"] = "cannot_connect"
                else:
                    self._queue.pop(0)
                    if self._queue:
                        return await self.async_step_building()
                    return await self._save()
        return self.async_show_form(
            step_id="building",
            data_schema=_building_schema(
                self._resources, building_id, user_input or self._current.get(building_id)
            ),
            errors=errors,
            description_placeholders={"building_id": building_id, "refused": refused},
        )

    # Edit / remove --------------------------------------------------------

    def _pick_schema(self, multiple: bool) -> vol.Schema:
        options = [
            SelectOptionDict(value=bid, label=f"{conf.get(B_NAME)} ({bid})")
            for bid, conf in self._current.items()
        ]
        return vol.Schema(
            {
                vol.Required(F_BUILDINGS): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=multiple, mode=SelectSelectorMode.LIST)
                )
            }
        )

    async def async_step_edit_building(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._queue = [user_input[F_BUILDINGS]]
            if self._resources is None:
                try:
                    self._resources = await _discover(self.hass, self.config_entry.data)
                except KoneError:
                    self._resources = Resources()
            return await self.async_step_building()
        return self.async_show_form(step_id="edit_building", data_schema=self._pick_schema(False))

    async def async_step_remove_building(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            for bid in user_input[F_BUILDINGS]:
                self._current.pop(bid, None)
            return await self._save()
        return self.async_show_form(step_id="remove_building", data_schema=self._pick_schema(True))

    # Settings -------------------------------------------------------------

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        refused = ""
        if user_input is not None:
            changes = {
                OPT_SCAN_INTERVAL: int(user_input[OPT_SCAN_INTERVAL]),
                OPT_MONITORING: bool(user_input[OPT_MONITORING]),
                OPT_SERVICE_INFO: bool(user_input[OPT_SERVICE_INFO]),
                OPT_ALLOW_CONTROL: bool(user_input[OPT_ALLOW_CONTROL]),
            }
            try:
                await _check_scopes(
                    self.hass, self.config_entry.data, {**options, **changes, OPT_BUILDINGS: self._current}
                )
            except ScopeRefused as err:
                errors["base"] = "scope_refused"
                refused = err.text
            except KoneError:
                errors["base"] = "cannot_connect"
            else:
                return await self._save(**changes)
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_SCAN_INTERVAL,
                        default=int(options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL, max=3600, step=30, unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(OPT_MONITORING, default=options.get(OPT_MONITORING, True)): bool,
                    vol.Required(OPT_SERVICE_INFO, default=options.get(OPT_SERVICE_INFO, False)): bool,
                    vol.Required(
                        OPT_ALLOW_CONTROL, default=options.get(OPT_ALLOW_CONTROL, True)
                    ): bool,
                }
            ),
            errors=errors,
            description_placeholders={"refused": refused},
        )
