"""The setup and options flows."""

from __future__ import annotations

from fake_kone import BUILDING, CLIENT_ID, CLIENT_SECRET, KEN

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.kone.api import callgiving_scope
from custom_components.kone.const import DOMAIN, OPT_BUILDINGS


async def _start(hass, url, secret=CLIENT_SECRET):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": CLIENT_ID, "client_secret": secret, "hostname": url},
    )


async def test_full_flow(hass: HomeAssistant, kone) -> None:
    result = await _start(hass, kone.url)
    assert result["step_id"] == "choose"
    assert result["description_placeholders"]["found"] == "1"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"buildings": [BUILDING], "manual_building_ids": ""}
    )
    assert result["step_id"] == "building"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "HQ", "groups": "1", "terminal": 1, "kens": [KEN]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HQ"
    assert result["options"][OPT_BUILDINGS][BUILDING]["groups"] == ["1"]
    assert result["options"][OPT_BUILDINGS][BUILDING]["kens"] == [KEN]
    assert "client_secret" in result["data"]
    # Read-only unless the user ticks the box, and the flag is an option,
    # not stored with the credentials.
    assert result["options"]["allow_control"] is False
    assert "allow_control" not in result["data"]


async def test_bad_credentials(hass: HomeAssistant, kone) -> None:
    result = await _start(hass, kone.url, secret="nope")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unreachable(hass: HomeAssistant, socket_enabled) -> None:
    result = await _start(hass, "http://127.0.0.1:9")
    assert result["errors"] == {"base": "cannot_connect"}


async def test_refused_group_scope_sends_user_back(hass: HomeAssistant, kone) -> None:
    kone.denied_scopes = {callgiving_scope(BUILDING, "9")}
    result = await _start(hass, kone.url)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"buildings": [BUILDING], "manual_building_ids": ""}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "HQ", "groups": "9", "terminal": 1, "kens": []}
    )
    assert result["step_id"] == "choose"
    assert result["errors"] == {"base": "scope_refused"}


async def test_options_add_and_remove_building(hass: HomeAssistant, kone, make_entry) -> None:
    entry = make_entry(kone.url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_building"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"manual_building_ids": "building:12345"}
    )
    assert result["step_id"] == "building"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Annex", "groups": "", "terminal": 1, "kens": ["200002"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[OPT_BUILDINGS]["12345"]["kens"] == ["ken:200002"]
    assert BUILDING in entry.options[OPT_BUILDINGS]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_building"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"buildings": ["12345"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert list(entry.options[OPT_BUILDINGS]) == [BUILDING]


async def test_control_can_be_enabled_at_setup(hass: HomeAssistant, kone) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
         "hostname": kone.url, "allow_control": True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"buildings": [BUILDING], "manual_building_ids": ""}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "HQ", "groups": "1", "terminal": 1, "kens": []}
    )
    assert result["options"]["allow_control"] is True
