"""Tests for linked switches on the climate accessories.

A climate entity lists entities in its `homekit_linked_switches` attribute and
each becomes a switch service in its accessory, so the Home app shows
them inside the same tile. Both climate accessory types share the code, so
every test runs against both.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.climate import (
    ATTR_FAN_MODES,
    ATTR_HVAC_MODES,
    ATTR_SWING_MODES,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pyhap.const import HAP_REPR_AID, HAP_REPR_CHARS, HAP_REPR_IID, HAP_REPR_VALUE
from pyhap.util import is_callback
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.homekit.climate_base import ATTR_HOMEKIT_LINKED_SWITCHES
from custom_components.homekit.const import (
    CHAR_CONFIGURED_NAME,
    CHAR_NAME,
    CHAR_ON,
    SERV_FANV2,
    SERV_HEATER_COOLER,
    SERV_SWITCH,
    SERV_THERMOSTAT,
)
from custom_components.homekit.type_heater_coolers import HeaterCooler
from custom_components.homekit.type_thermostats import Thermostat

CLIMATE = "climate.bedroom_ac"
TIMER = "input_boolean.bedroom_ac_30_minutes_timer"

ACCESSORIES = [
    pytest.param(HeaterCooler, SERV_HEATER_COOLER, id="heater_cooler"),
    pytest.param(Thermostat, SERV_THERMOSTAT, id="thermostat"),
]


def register_night_mode(hass: HomeAssistant) -> str:
    """Register a device-named switch, as hOn's are, and publish its state."""
    entry = er.async_get(hass).async_get_or_create(
        "switch",
        "hon",
        "night",
        suggested_object_id="bedroom_night_mode",
        has_entity_name=True,
        original_name="Night mode",
    )
    hass.states.async_set(
        entry.entity_id, STATE_OFF, {ATTR_FRIENDLY_NAME: "Bedroom Night mode"}
    )
    return entry.entity_id


def set_climate(hass: HomeAssistant, linked=None, **overrides) -> None:
    attributes = {
        ATTR_SUPPORTED_FEATURES: ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE,
        ATTR_HVAC_MODES: [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT],
        ATTR_FAN_MODES: ["off", "auto", "low", "medium", "high"],
        ATTR_SWING_MODES: ["off", "on"],
        "temperature": 22,
        "current_temperature": 22.5,
        **overrides,
    }
    if linked is not None:
        attributes[ATTR_HOMEKIT_LINKED_SWITCHES] = linked
    hass.states.async_set(CLIMATE, HVACMode.COOL, attributes)


async def start(hass, hk_driver, accessory_class):
    acc = accessory_class(hass, hk_driver, "Bedroom AC", CLIMATE, 1, None)
    hk_driver.add_accessory(acc)
    acc.run()
    await hass.async_block_till_done()
    return acc


ZWSP = "\u200b"


def switch_services(acc) -> dict[str, object]:
    """The linked switch services, keyed by their name without sort prefixes."""
    return {
        serv.get_characteristic(CHAR_CONFIGURED_NAME).value.lstrip(ZWSP): serv
        for serv in acc.services
        if serv.display_name == SERV_SWITCH
    }


def write_on(hk_driver, acc, serv, value: bool) -> None:
    """Toggle a switch through the HAP client path, as the Home app does."""
    hk_driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: serv.get_characteristic(CHAR_ON).to_HAP()[HAP_REPR_IID],
                    HAP_REPR_VALUE: value,
                }
            ]
        },
        "mock_addr",
    )


@pytest.fixture
def night(hass):
    return register_night_mode(hass)


@pytest.fixture
def timer(hass):
    hass.states.async_set(TIMER, STATE_ON, {ATTR_FRIENDLY_NAME: "Bedroom AC 30 Minutes Timer"})
    return TIMER


# Building the services


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_one_switch_per_entity_in_the_same_accessory(
    hass, hk_driver, night, timer, accessory_class, primary
):
    set_climate(hass, [night, timer])
    acc = await start(hass, hk_driver, accessory_class)

    switches = switch_services(acc)
    # Registry names drop the device prefix; a helper has only its friendly name.
    assert set(switches) == {"Night mode", "Bedroom AC 30 Minutes Timer"}
    primary_serv = acc.get_service(primary)
    # The Home app captions switches whose Name matches ConfiguredName; an
    # accessory-name prefix on Name is what upstream's unlabelled presets do.
    assert all(
        serv.get_characteristic(CHAR_NAME).value
        == serv.get_characteristic(CHAR_CONFIGURED_NAME).value
        for serv in switches.values()
    )
    # Not linked: the Home app draws linked services as uncaptioned controls.
    assert not any(serv in primary_serv.linked_services for serv in switches.values())
    # The tile must still open on the climate service, not a switch.
    assert primary_serv.is_primary_service
    assert not any(serv.is_primary_service for serv in switches.values())


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_no_attribute_means_no_switches(hass, hk_driver, accessory_class, primary):
    """Every other climate entity is bridged exactly as stock HA does it."""
    set_climate(hass)
    acc = await start(hass, hk_driver, accessory_class)

    assert switch_services(acc) == {}


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_malformed_and_duplicate_entries_are_dropped(
    hass, hk_driver, timer, accessory_class, primary
):
    set_climate(hass, [timer, 42, None, timer])
    acc = await start(hass, hk_driver, accessory_class)

    assert list(switch_services(acc)) == ["Bedroom AC 30 Minutes Timer"]


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_a_bare_string_is_not_split_into_characters(
    hass, hk_driver, timer, accessory_class, primary
):
    set_climate(hass, timer)
    acc = await start(hass, hk_driver, accessory_class)

    assert switch_services(acc) == {}


# State


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_switches_follow_their_entities(
    hass, hk_driver, night, timer, accessory_class, primary
):
    set_climate(hass, [night, timer])
    acc = await start(hass, hk_driver, accessory_class)
    switches = switch_services(acc)
    night_on = switches["Night mode"].get_characteristic(CHAR_ON)
    timer_on = switches["Bedroom AC 30 Minutes Timer"].get_characteristic(CHAR_ON)
    assert (night_on.value, timer_on.value) == (False, True)

    hass.states.async_set(night, STATE_ON)
    await hass.async_block_till_done()
    assert night_on.value is True

    # Unreadable keeps the last value rather than flashing the switch off.
    hass.states.async_set(night, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert night_on.value is True


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_a_missing_entity_does_not_break_the_accessory(
    hass, hk_driver, timer, accessory_class, primary
):
    set_climate(hass, ["switch.not_there_yet", timer])
    acc = await start(hass, hk_driver, accessory_class)

    switches = switch_services(acc)
    assert switches["not there yet"].get_characteristic(CHAR_ON).value is False

    hass.states.async_set("switch.not_there_yet", STATE_ON)
    await hass.async_block_till_done()
    assert switches["not there yet"].get_characteristic(CHAR_ON).value is True


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_a_changed_list_reloads_the_accessory(
    hass, hk_driver, night, timer, accessory_class, primary
):
    """The services are fixed at build time, so a new list needs a rebuild."""
    set_climate(hass, [night])
    acc = await start(hass, hk_driver, accessory_class)

    with patch.object(acc, "async_reload") as reload:
        set_climate(hass, [night, timer])
        await hass.async_block_till_done()

    reload.assert_called_once()


# Commands


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_toggling_calls_the_generic_services(
    hass, hk_driver, night, accessory_class, primary
):
    """Generic turn_on/off, so switch and input_boolean entities both work."""
    turn_on = async_mock_service(hass, "homeassistant", "turn_on")
    turn_off = async_mock_service(hass, "homeassistant", "turn_off")
    set_climate(hass, [night])
    acc = await start(hass, hk_driver, accessory_class)
    serv = switch_services(acc)["Night mode"]

    write_on(hk_driver, acc, serv, True)
    await hass.async_block_till_done()
    write_on(hk_driver, acc, serv, False)
    await hass.async_block_till_done()

    assert [call.data[ATTR_ENTITY_ID] for call in turn_on] == [night]
    assert [call.data[ATTR_ENTITY_ID] for call in turn_off] == [night]


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_a_failed_toggle_snaps_back(hass, hk_driver, night, accessory_class, primary):
    """pyhap keeps the written value, so a failure must restore the real one."""

    def _fail(call: ServiceCall) -> None:
        raise HomeAssistantError("unit offline")

    hass.services.async_register("homeassistant", "turn_on", _fail)
    set_climate(hass, [night])
    acc = await start(hass, hk_driver, accessory_class)
    serv = switch_services(acc)["Night mode"]

    write_on(hk_driver, acc, serv, True)
    await hass.async_block_till_done()

    assert serv.get_characteristic(CHAR_ON).value is False


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_run_stays_a_loop_callback(hass, hk_driver, accessory_class, primary):
    """Without pyhap's marker the driver would call run() from a worker thread."""
    set_climate(hass)
    acc = accessory_class(hass, hk_driver, "Bedroom AC", CLIMATE, 1, None)

    assert is_callback(acc.run)


# Order


def sort_prefixes_of(name: str) -> int:
    return len(name) - len(name.lstrip(ZWSP))


def sort_prefixes(serv) -> int:
    return sort_prefixes_of(serv.get_characteristic(CHAR_CONFIGURED_NAME).value)


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_names_carry_the_listed_order(
    hass, hk_driver, night, timer, accessory_class, primary
):
    """The Home app sorts by name, so later services get more leading prefixes."""
    set_climate(hass, [timer, night])
    acc = await start(hass, hk_driver, accessory_class)

    switches = switch_services(acc)
    fan = acc.get_service(SERV_FANV2)
    assert fan.get_characteristic(CHAR_CONFIGURED_NAME).value.lstrip(ZWSP) == "Fan"
    assert [
        sort_prefixes(fan),
        sort_prefixes(switches["Bedroom AC 30 Minutes Timer"]),
        sort_prefixes(switches["Night mode"]),
    ] == [1, 2, 3]
    # Sorted the way the Home app does, the names come out in the listed order.
    names = [s.get_characteristic(CHAR_CONFIGURED_NAME).value for s in (fan, *switches.values())]
    assert [n.lstrip(ZWSP) for n in sorted(names, key=lambda n: (sort_prefixes_of(n), n))] == [
        "Fan",
        "Bedroom AC 30 Minutes Timer",
        "Night mode",
    ]


@pytest.mark.parametrize(("accessory_class", "primary"), ACCESSORIES)
async def test_no_prefixes_or_fan_name_without_switches(
    hass, hk_driver, accessory_class, primary
):
    """A stock climate keeps upstream's services exactly."""
    set_climate(hass)
    acc = await start(hass, hk_driver, accessory_class)

    fan = acc.get_service(SERV_FANV2)
    assert fan is None or all(
        char.display_name != CHAR_CONFIGURED_NAME for char in fan.characteristics
    )
