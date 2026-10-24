# HomeKit Bridge with linked climate switches

A patched copy of Home Assistant's built-in `homekit` integration (HomeKit Bridge), installed as a custom component so it replaces the built-in one. It adds one thing: switches inside a climate accessory's tile.

HomeKit lets an accessory carry extra Switch services, which is how some air conditioners with native HomeKit show mode switches inside their tile. Stock Home Assistant does this only for fans, with one switch per preset mode. Its climate accessories (Thermostat and HeaterCooler) never get any. With this override, a climate entity whose state carries a `homekit_linked_switches` attribute gets one switch per listed entity, linked into its tile:

```yaml
homekit_linked_switches:
  - switch.bedroom_night_mode
  - input_boolean.bedroom_ac_30_minutes_timer
```

The HomeKit Device Aggregator (`homekit_device`) `air_conditioner` type sets this attribute. Any climate entity that publishes it works the same way, and every other climate entity is bridged exactly as stock HA does it.

## Behaviour

- Each switch is named from the linked entity's registry name, without its device's name in front ("Night mode" rather than "Bedroom Night mode"). Helpers without one use their friendly name.
- Toggling a switch calls `homeassistant.turn_on` / `turn_off`, so `switch` and `input_boolean` entities both work. If the call fails, the switch snaps back to the entity's real state.
- The switch follows its entity's state. While the entity is unavailable, the switch keeps its last value.
- Changing the attribute's list rebuilds the accessory, like a changed set of HVAC or fan modes does upstream.
- Linked entities are read straight from Home Assistant. Exclude them from the bridge so they don't also show as their own tiles.

## Install

1. In HACS, add `https://github.com/jfis223/homekit_linked_switches` as a custom repository of type Integration and download it. Or copy `custom_components/homekit` to `/config/custom_components/homekit` yourself (`make deploy HA_CONFIG=/path/to/config`).
2. Restart Home Assistant. The log warns that a custom integration overrides `homekit`; that is expected. Existing bridges and pairings carry over, because the config entries and storage are unchanged.

To uninstall, delete `/config/custom_components/homekit` and restart. The built-in integration takes over again and the linked switches disappear.

## Keeping up with Home Assistant

The override shadows the built-in integration completely, so **after every Home Assistant update, re-sync it** to the release you are running. A stale copy keeps running old bridge code, or fails to load if the internals it relies on have moved.

```bash
make sync HA_VERSION=2026.11.0   # re-import upstream at that tag and re-apply the patch
# then set the manifest version to 2026.11.0.1, bump requirements-test.txt to
# the pytest-homeassistant-custom-component release that pins 2026.11.0, and:
make install && make check && make patch
```

The whole change lives in `patches/linked-switches.patch`: about 130 lines, mostly in `climate_base.py`, plus one call in each of `type_thermostats.py` and `type_heater_coolers.py`, and the manifest version. Patched spots are marked `[linked-switches patch]`. If a hunk stops applying after an upstream change, port it by hand and regenerate the patch with `make patch`.

The current copy is home-assistant/core **2026.10.2**.

## Development

`make install` (needs [uv](https://docs.astral.sh/uv/); fetches Python 3.14) then `make check`. The tests build real HeaterCooler and Thermostat accessories against a driver with pyhap's networking patched out. Core's own `test_type_thermostats.py` and `test_type_heater_coolers.py` (132 tests) were also run against the patched copy at 2026.10.2 and pass.

## License

The integration code is from [home-assistant/core](https://github.com/home-assistant/core) and is licensed under Apache-2.0 (see `LICENSE.md`). The modifications are marked `[linked-switches patch]` and are listed in `patches/linked-switches.patch`.
