"""Base class shared by the climate accessory types."""

from collections.abc import Mapping
from functools import partial
import logging
from typing import Any

from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_THERMOSTAT
from pyhap.service import Service
from pyhap.util import callback as pyhap_callback

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_SWING_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN as CLIMATE_DOMAIN,
    FAN_AUTO,
    FAN_OFF,
    FAN_ON,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_SWING_MODE,
    SWING_OFF,
    ClimateEntityCapabilityAttribute,
    ClimateEntityFeature,
    ClimateEntityStateAttribute,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityStateAttribute,
)
from homeassistant.core import (
    DOMAIN as HOMEASSISTANT_DOMAIN,
    Event,
    EventStateChangedData,
    HassJobType,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.percentage import percentage_to_ordered_list_item

from .accessories import HomeAccessory
from .climate_util import (
    fan_mode_to_speed,
    fan_speed_to_mode,
    get_fan_modes_and_speeds,
    get_swing_off_mode,
    get_swing_on_mode,
    get_temperature_range_from_state,
    has_swing_off_mode,
    is_swing_on,
    resolve_target_temp_range,
    temperature_attribute_to_homekit,
)
from .const import (
    CHAR_ACTIVE,
    CHAR_CONFIGURED_NAME,
    CHAR_CURRENT_FAN_STATE,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_NAME,
    CHAR_ON,
    CHAR_ROTATION_SPEED,
    CHAR_SWING_MODE,
    CHAR_TARGET_FAN_STATE,
    MAX_NAME_LENGTH,
    PROP_MAX_VALUE,
    PROP_MIN_STEP,
    PROP_MIN_VALUE,
    SERV_FANV2,
    SERV_SWITCH,
)
from .util import (
    cleanup_name_for_homekit,
    temperature_to_homekit,
    temperature_to_states,
)

_LOGGER = logging.getLogger(__name__)

# [linked-switches patch] The Home app sorts a tile's services by name and
# ignores ServiceLabelIndex there, so the listed order is carried in the names:
# the earlier a service comes, the more zero-width spaces lead its name.
SORT_PREFIX_CHAR = "\u200b"

# [linked-switches patch] Entities listed in this state attribute become switch
# services in the climate accessory, so they share its tile.
ATTR_HOMEKIT_LINKED_SWITCHES = "homekit_linked_switches"

FAN_STATE_INACTIVE = 0
FAN_STATE_IDLE = 1
FAN_STATE_ACTIVE = 2

# States in which a climate entity is inactive rather than idle
CLIMATE_INACTIVE_STATES = frozenset({HVACMode.OFF, STATE_UNAVAILABLE, STATE_UNKNOWN})

HC_HASS_TO_HOMEKIT_FAN_STATE = {
    HVACAction.OFF: FAN_STATE_INACTIVE,
    HVACAction.IDLE: FAN_STATE_IDLE,
    HVACAction.HEATING: FAN_STATE_ACTIVE,
    HVACAction.COOLING: FAN_STATE_ACTIVE,
    HVACAction.DRYING: FAN_STATE_ACTIVE,
    HVACAction.FAN: FAN_STATE_ACTIVE,
    HVACAction.PREHEATING: FAN_STATE_IDLE,
    HVACAction.DEFROSTING: FAN_STATE_IDLE,
}


class HomeKitClimateAccessory(HomeAccessory):
    """Base class for the Thermostat and HeaterCooler accessories."""

    # Configured by subclasses only when the entity exposes the mode.
    char_speed: Characteristic
    char_swing: Characteristic

    char_current_temp: Characteristic

    # Configured by _configure_fan_service when fan_chars is non-empty.
    char_fan_active: Characteristic
    char_target_fan_state: Characteristic
    char_current_fan_state: Characteristic

    def __init__(self, *args: Any) -> None:
        """Initialize the shared climate accessory state."""
        super().__init__(*args, category=CATEGORY_THERMOSTAT)
        self._unit = self.hass.config.units.temperature_unit

        state = self.hass.states.get(self.entity_id)
        assert state
        attributes = state.attributes
        features = attributes.get(EntityStateAttribute.SUPPORTED_FEATURES, 0)

        # ``fan_modes`` maps lowercased names to their original casing;
        # ``ordered_fan_speeds`` holds the predefined speeds in HomeKit order.
        self.fan_modes: dict[str, str] = {}
        self.ordered_fan_speeds: list[str] = []
        if features & ClimateEntityFeature.FAN_MODE:
            self.fan_modes, self.ordered_fan_speeds = get_fan_modes_and_speeds(
                attributes
            )

        self.swing_on_mode: str | None = None
        self.swing_off_mode: str = SWING_OFF
        # The binary swing toggle writes the off mode back, so it is only
        # usable when the entity advertises one.
        if features & ClimateEntityFeature.SWING_MODE and has_swing_off_mode(
            attributes
        ):
            self.swing_on_mode = get_swing_on_mode(attributes)
            self.swing_off_mode = get_swing_off_mode(attributes)

        # Characteristics the subclass places on a linked fan service; which
        # ones, if any, is the subclass's policy.
        self.fan_chars: list[str] = []

        # These attributes drive the characteristic set and valid values, so
        # reload the accessory when any of them change.
        self._reload_on_change_attrs.extend(
            (
                ClimateEntityCapabilityAttribute.MIN_TEMP,
                ClimateEntityCapabilityAttribute.MAX_TEMP,
                ClimateEntityCapabilityAttribute.FAN_MODES,
                ClimateEntityCapabilityAttribute.SWING_MODES,
                ClimateEntityCapabilityAttribute.HVAC_MODES,
                ATTR_HOMEKIT_LINKED_SWITCHES,
            )
        )

        # [linked-switches patch] Read once here like the other service-shaping
        # attributes; a changed list reloads the accessory via the line above.
        linked = attributes.get(ATTR_HOMEKIT_LINKED_SWITCHES)
        self.linked_switches: list[str] = (
            list(dict.fromkeys(e for e in linked if isinstance(e, str)))
            if isinstance(linked, (list, tuple))
            else []
        )
        self._linked_switch_chars: dict[str, Characteristic] = {}

    def get_temperature_range(self, state: State) -> tuple[float, float]:
        """Return the min and max temperature range."""
        return get_temperature_range_from_state(
            state, self._unit, DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP
        )

    def _configure_current_temperature_char(self, serv: Service) -> None:
        """Configure the shared current temperature characteristic."""
        self.char_current_temp = serv.configure_char(
            CHAR_CURRENT_TEMPERATURE, value=21.0
        )

    def _configure_target_mode_char(
        self,
        serv: Service,
        char_name: str,
        value: int,
        valid_values: dict[HVACMode, int],
    ) -> Characteristic:
        """Configure a target mode characteristic scoped to the supported modes.

        The value must be set before ``valid_values`` because pyhap applies the
        valid values first and would reject a default outside that set.
        """
        char = serv.configure_char(char_name, value=value)
        char.override_properties(valid_values=valid_values)
        char.allow_invalid_client_values = True
        return char

    def _reject_char_write(self, char: Characteristic, value: Any) -> None:
        """Flip a characteristic back after rejecting a client write."""
        char.value = value
        char.notify()

    def _dispatch_climate_write(self, service: str, params: dict[str, Any]) -> None:
        """Send a climate write from a characteristic setter.

        Subclasses can override this to serialize their writes.
        """
        self.async_call_service(
            CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: self.entity_id, **params}
        )

    def _update_temperature_char(
        self, char: Characteristic, state: State, attr: str
    ) -> None:
        """Set a temperature characteristic from a state attribute, if present."""
        if (
            value := temperature_attribute_to_homekit(state, attr, self._unit)
        ) is not None:
            char.set_value(value)

    def _update_current_temperature_char(self, state: State) -> None:
        """Update the current temperature characteristic from the entity state."""
        self._update_temperature_char(
            self.char_current_temp,
            state,
            ClimateEntityStateAttribute.CURRENT_TEMPERATURE,
        )

    def _dual_setpoint_params(
        self,
        cool_char: Characteristic,
        heat_char: Characteristic,
        new_high: float | None,
        new_low: float | None,
    ) -> dict[str, float]:
        """Return an ordered high/low target temperature pair for a range write.

        Fills the unchanged side from the current characteristic value and
        enforces the deadband, so the entity always gets a consistent pair.
        """
        high, low = resolve_target_temp_range(
            cool_char.value,
            heat_char.value,
            new_high,
            new_low,
            cool_char.properties[PROP_MIN_VALUE],
            cool_char.properties[PROP_MAX_VALUE],
        )
        return {
            ATTR_TARGET_TEMP_HIGH: self._temperature_to_states(high),
            ATTR_TARGET_TEMP_LOW: self._temperature_to_states(low),
        }

    def _temperature_to_homekit(self, temp: float) -> float:
        """Convert a temperature in the entity's unit to the HomeKit unit."""
        return temperature_to_homekit(temp, self._unit)

    def _temperature_to_states(self, temp: float) -> float:
        """Convert a temperature in the HomeKit unit to the entity's unit."""
        return temperature_to_states(temp, self._unit)

    def _fan_speed_params(self, speed: int) -> dict[str, Any] | None:
        """Return the set_fan_mode data for a HomeKit rotation speed."""
        _LOGGER.debug("%s: Set fan speed to %s", self.entity_id, speed)
        if not self.ordered_fan_speeds or not 0 < speed <= 100:
            return None
        mode = fan_speed_to_mode(self.ordered_fan_speeds, self.fan_modes, speed)
        return {ATTR_FAN_MODE: mode}

    def _set_fan_speed(self, speed: int) -> None:
        """Send the climate fan mode for a HomeKit rotation speed."""
        if (params := self._fan_speed_params(speed)) is not None:
            self._dispatch_climate_write(SERVICE_SET_FAN_MODE, params)

    def _swing_mode_params(self, swing_on: int) -> dict[str, Any] | None:
        """Return the set_swing_mode data for a HomeKit swing toggle."""
        if self.swing_on_mode is None:
            return None
        _LOGGER.debug("%s: Set swing mode to %s", self.entity_id, swing_on)
        return {
            ATTR_SWING_MODE: self.swing_on_mode if swing_on else self.swing_off_mode
        }

    def _set_swing_mode(self, swing_on: int) -> None:
        """Send the climate swing mode for a HomeKit swing toggle."""
        if (params := self._swing_mode_params(swing_on)) is not None:
            self._dispatch_climate_write(SERVICE_SET_SWING_MODE, params)

    def _update_fan_speed_char(self, attributes: Mapping[str, Any]) -> None:
        """Update the rotation speed characteristic from the current fan mode."""
        # Modes with no predefined speed (e.g. fan auto) keep the last value;
        # HomeKit's slider has no position to represent them.
        if (
            self.ordered_fan_speeds
            and (
                speed := fan_mode_to_speed(
                    self.ordered_fan_speeds,
                    attributes.get(ClimateEntityStateAttribute.FAN_MODE),
                )
            )
            is not None
        ):
            self.char_speed.set_value(speed)

    def _update_swing_char(self, attributes: Mapping[str, Any]) -> None:
        """Update the swing characteristic from the current swing mode."""
        # An absent swing mode keeps the last value; there is nothing to show.
        if self.swing_on_mode is not None and (
            swing_mode := attributes.get(ClimateEntityStateAttribute.SWING_MODE)
        ):
            self.char_swing.set_value(1 if is_swing_on(swing_mode) else 0)

    def _configure_linked_switches(self, primary_serv: Service) -> None:
        """[linked-switches patch] Add a switch per listed entity.

        The switches join the accessory but are deliberately not linked to
        `primary_serv`: the Home app draws linked services as inline controls
        of the primary one with no caption, so linked switches show unlabelled.
        Subclasses call this before set_primary_service, so the primary flag
        is set explicitly on every service and the tile still opens on the
        climate service.
        """
        if not self.linked_switches:
            return
        # The fan, when there is one, sorts first (see _configure_fan_service).
        for position, entity_id in enumerate(self.linked_switches):
            serv = self.add_preload_service(
                SERV_SWITCH, [CHAR_NAME, CHAR_CONFIGURED_NAME], unique_id=entity_id
            )
            # Name matches ConfiguredName on purpose. Upstream's fan presets
            # prefix Name with the accessory name and show unlabelled in the
            # Home app; accessories that show captions send the same text in
            # both.
            name = self._sorted_name(self._linked_switch_name(entity_id), position)
            serv.configure_char(CHAR_NAME, value=name)
            serv.configure_char(CHAR_CONFIGURED_NAME, value=name)
            self._linked_switch_chars[entity_id] = serv.configure_char(
                CHAR_ON,
                value=False,
                setter_callback=partial(self._set_linked_switch, entity_id),
            )

    def _sorted_name(self, name: str, position: int) -> str:
        """Lead a name with zero-width spaces so it sorts at `position`.

        Position -1 is the fan, ahead of the first switch at 0.
        """
        prefix = SORT_PREFIX_CHAR * (len(self.linked_switches) - position)
        return prefix + cleanup_name_for_homekit(name)[: MAX_NAME_LENGTH - len(prefix)]

    def _linked_switch_name(self, entity_id: str) -> str:
        """Name a linked switch without its device's name in front.

        Entities using has_entity_name keep that part apart in the registry
        ("Night mode" rather than "Bedroom Night mode"); helpers have only
        their friendly name.
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry and entry.has_entity_name and (entry.name or entry.original_name):
            return entry.name or entry.original_name
        if (state := self.hass.states.get(entity_id)) is not None:
            return state.name
        # Not loaded yet; the name is fixed until the accessory next reloads.
        return split_entity_id(entity_id)[1].replace("_", " ")

    def _set_linked_switch(self, entity_id: str, value: int) -> None:
        """Turn a linked entity on or off from HomeKit."""
        _LOGGER.debug("%s: Set linked switch %s to %s", self.entity_id, entity_id, value)
        self.hass.async_create_task(
            self._async_set_linked_switch(entity_id, bool(value)), eager_start=True
        )

    async def _async_set_linked_switch(self, entity_id: str, on: bool) -> None:
        if not await self.async_call_service_and_wait(
            HOMEASSISTANT_DOMAIN,
            SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            on,
        ):
            # The base resync only covers the climate entity itself, so put
            # this switch back to what the entity actually is.
            self._async_update_linked_switch(entity_id, self.hass.states.get(entity_id))

    @callback
    def _async_update_linked_switch(self, entity_id: str, state: State | None) -> None:
        """Mirror a linked entity onto its switch, keeping the last value if unreadable."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        self._linked_switch_chars[entity_id].set_value(state.state == STATE_ON)

    @callback
    def _async_linked_switch_event(self, event: Event[EventStateChangedData]) -> None:
        self._async_update_linked_switch(
            event.data["entity_id"], event.data["new_state"]
        )

    @callback
    @pyhap_callback  # type: ignore[untyped-decorator]
    def run(self) -> None:
        """Start tracking the linked switches alongside the entity."""
        super().run()
        if not self._linked_switch_chars:
            return
        for entity_id in self._linked_switch_chars:
            self._async_update_linked_switch(entity_id, self.hass.states.get(entity_id))
        self._subscriptions.append(
            async_track_state_change_event(
                self.hass,
                list(self._linked_switch_chars),
                self._async_linked_switch_event,
                job_type=HassJobType.Callback,
            )
        )

    def _configure_fan_service(self, primary_serv: Service) -> None:
        """Create a linked fan service for the chars in ``fan_chars``."""
        # [linked-switches patch] With switches to order, the fan gets a name
        # that sorts ahead of them all.
        named = bool(self.linked_switches)
        chars = self.fan_chars + ([CHAR_NAME, CHAR_CONFIGURED_NAME] if named else [])
        serv_fan = self.add_preload_service(SERV_FANV2, chars)
        if named:
            fan_name = self._sorted_name("Fan", -1)
            serv_fan.configure_char(CHAR_NAME, value=fan_name)
            serv_fan.configure_char(CHAR_CONFIGURED_NAME, value=fan_name)
        primary_serv.add_linked_service(serv_fan)
        self.char_fan_active = serv_fan.configure_char(
            CHAR_ACTIVE, value=1, setter_callback=self._set_fan_active
        )
        if CHAR_SWING_MODE in self.fan_chars:
            self.char_swing = serv_fan.configure_char(
                CHAR_SWING_MODE,
                value=0,
                setter_callback=self._set_swing_mode,
            )
            self.char_swing.display_name = "Swing Mode"
        if CHAR_ROTATION_SPEED in self.fan_chars:
            self.char_speed = serv_fan.configure_char(
                CHAR_ROTATION_SPEED,
                value=100,
                properties={PROP_MIN_STEP: 100 / len(self.ordered_fan_speeds)},
                setter_callback=self._set_fan_speed,
            )
            self.char_speed.display_name = "Fan Mode"
        if CHAR_CURRENT_FAN_STATE in self.fan_chars:
            self.char_current_fan_state = serv_fan.configure_char(
                CHAR_CURRENT_FAN_STATE,
                value=0,
            )
            self.char_current_fan_state.display_name = "Fan State"
        if CHAR_TARGET_FAN_STATE in self.fan_chars:
            self.char_target_fan_state = serv_fan.configure_char(
                CHAR_TARGET_FAN_STATE,
                value=0,
                setter_callback=self._set_fan_auto,
            )
            self.char_target_fan_state.display_name = "Fan Auto"

    def _get_on_mode(self) -> str:
        """Return the fan mode to use when leaving auto or turning the fan on."""
        if self.ordered_fan_speeds:
            speed_key = percentage_to_ordered_list_item(self.ordered_fan_speeds, 50)
            return self.fan_modes[speed_key]
        return self.fan_modes[FAN_ON]

    def _set_fan_active(self, active: int) -> None:
        """Send the climate fan mode for a HomeKit fan active toggle."""
        _LOGGER.debug("%s: Set fan active to %s", self.entity_id, active)
        if FAN_OFF not in self.fan_modes:
            _LOGGER.debug(
                "%s: Fan does not support off, resetting to on", self.entity_id
            )
            self._reject_char_write(self.char_fan_active, 1)
            return
        mode = self._get_on_mode() if active else self.fan_modes[FAN_OFF]
        self._dispatch_climate_write(SERVICE_SET_FAN_MODE, {ATTR_FAN_MODE: mode})

    def _set_fan_auto(self, auto: int) -> None:
        """Send the climate fan mode for a HomeKit fan auto toggle.

        Subclasses must only add CHAR_TARGET_FAN_STATE to ``fan_chars`` when
        FAN_AUTO is in ``fan_modes``; this setter assumes the mode exists.
        """
        _LOGGER.debug("%s: Set fan auto to %s", self.entity_id, auto)
        mode = self.fan_modes[FAN_AUTO] if auto else self._get_on_mode()
        self._dispatch_climate_write(SERVICE_SET_FAN_MODE, {ATTR_FAN_MODE: mode})

    @callback
    def _async_update_fan_service(self, new_state: State) -> None:
        """Update the linked fan service from the entity state."""
        attributes = new_state.attributes

        self._update_swing_char(attributes)
        self._update_fan_speed_char(attributes)

        fan_mode = attributes.get(ClimateEntityStateAttribute.FAN_MODE)
        fan_mode_lower = fan_mode.lower() if isinstance(fan_mode, str) else None
        if CHAR_TARGET_FAN_STATE in self.fan_chars:
            self.char_target_fan_state.set_value(1 if fan_mode_lower == FAN_AUTO else 0)

        if CHAR_CURRENT_FAN_STATE in self.fan_chars and (
            hvac_action := attributes.get(ClimateEntityStateAttribute.HVAC_ACTION)
        ):
            self.char_current_fan_state.set_value(
                HC_HASS_TO_HOMEKIT_FAN_STATE[hvac_action]
            )

        self.char_fan_active.set_value(
            int(
                new_state.state not in CLIMATE_INACTIVE_STATES
                and fan_mode_lower != FAN_OFF
            )
        )
