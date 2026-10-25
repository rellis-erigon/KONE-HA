"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_kone import BUILDING, CLIENT_ID, CLIENT_SECRET, GROUP, KEN, FakeKone  # noqa: E402

from custom_components.kone.const import (  # noqa: E402
    B_GROUPS,
    B_KENS,
    B_NAME,
    B_TERMINAL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HOSTNAME,
    DOMAIN,
    OPT_BUILDINGS,
    OPT_MONITORING,
    OPT_SCAN_INTERVAL,
    OPT_SERVICE_INFO,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def kone(socket_enabled):
    server = FakeKone()
    await server.start()
    yield server
    await server.stop()


def entry_options(groups=(GROUP,), kens=(KEN,)) -> dict:
    return {
        OPT_BUILDINGS: {
            BUILDING: {
                B_NAME: "Test Tower",
                B_GROUPS: list(groups),
                B_TERMINAL: 1,
                B_KENS: list(kens),
            }
        },
        OPT_SCAN_INTERVAL: 300,
        OPT_MONITORING: True,
        OPT_SERVICE_INFO: False,
    }


def entry_data(url: str) -> dict:
    return {CONF_CLIENT_ID: CLIENT_ID, CONF_CLIENT_SECRET: CLIENT_SECRET, CONF_HOSTNAME: url}


@pytest.fixture
def make_entry():
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    def make(url: str, **kwargs) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            unique_id=CLIENT_ID,
            title="Test Tower",
            data=entry_data(url),
            options=entry_options(**kwargs),
        )

    return make
