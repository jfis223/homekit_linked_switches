"""Fixtures for testing the patched homekit integration.

`iid_storage` and `hk_driver` are adapted from home-assistant/core's
tests/components/homekit/conftest.py (Apache-2.0): a real HomeDriver with
pyhap's network, zeroconf and persistence patched out.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.homekit.accessories import HomeDriver
from custom_components.homekit.const import BRIDGE_NAME
from custom_components.homekit.iidmanager import AccessoryIIDStorage


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the override in every test."""
    return


@pytest.fixture
def iid_storage(hass: HomeAssistant) -> Generator[AccessoryIIDStorage]:
    """An IID store that never writes to disk."""
    with patch.object(AccessoryIIDStorage, "_async_schedule_save"):
        yield AccessoryIIDStorage(hass, "")


@pytest.fixture
def hk_driver(
    hass: HomeAssistant, iid_storage: AccessoryIIDStorage
) -> Generator[HomeDriver]:
    """A HomeDriver that never touches the network."""
    with (
        patch("pyhap.accessory_driver.AsyncZeroconf"),
        patch("pyhap.accessory_driver.AccessoryEncoder"),
        patch("pyhap.accessory_driver.HAPServer.async_stop"),
        patch("pyhap.accessory_driver.HAPServer.async_start"),
        patch("pyhap.accessory_driver.AccessoryDriver.publish"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        yield HomeDriver(
            hass,
            pincode=b"123-45-678",
            entry_id="",
            entry_title="mock entry",
            bridge_name=BRIDGE_NAME,
            iid_storage=iid_storage,
            address="127.0.0.1",
            loop=asyncio.get_event_loop(),
        )
