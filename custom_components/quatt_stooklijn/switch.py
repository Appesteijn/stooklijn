"""OTGW compensation switch for Quatt Stooklijn integration.

Stuurt de Quatt CiC indirect bij door via een OpenTherm Gateway de
kamertemperatuur te overriden.  Als de MPC-sensor zegt dat de CiC te veel
levert (aanvoertemperatuur te hoog), wordt de kamertemperatuur-override
verhoogd zodat de CiC denkt dat het warmer is en zijn output verlaagt.

Safety:
- Alleen positieve kamertemp offsets (nooit meer output forceren)
- Max offset: configureerbaar, hard max 3.0°C
- Dead band: ±1.0°C (geen actie)
- Rate limit: max 0.5°C per cyclus (5 min)
- Reset naar 0 bij: switch off, HA shutdown, HP uit, MPC unavailable
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_OTGW_ENABLED,
    CONF_OTGW_MAX_OFFSET,
    CONF_OTGW_ROOM_TEMP_OVERRIDE,
    DEFAULT_OTGW_MAX_OFFSET,
    DEFAULT_OTGW_ROOM_TEMP_OVERRIDE,
    DEFAULT_SUPPLY_TEMP_ENTITY,
    DOMAIN,
    MIN_FLOW_LPH,
    OTGW_CYCLE_SECONDS,
    OTGW_DEAD_BAND,
    OTGW_GAIN_FACTOR,
    OTGW_HARD_MAX_OFFSET,
    OTGW_MAX_RATE,
    OTGW_UNAVAILABLE_TIMEOUT,
)
from .coordinator import QuattStooklijnCoordinator
from .helpers import get_device_info, get_float_state

_LOGGER = logging.getLogger(__name__)

# Entity IDs for the sensors we read
_MPC_ENTITY_SUFFIX = "mpc_aanbevolen_aanvoertemperatuur"
_FLOW_ENTITY_DEFAULT = "sensor.heatpump_flowmeter_flowrate"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OTGW switch from config entry."""
    if not entry.data.get(CONF_OTGW_ENABLED, False):
        return

    coordinator: QuattStooklijnCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([QuattOtgwCompensationSwitch(coordinator, entry)])


class QuattOtgwCompensationSwitch(SwitchEntity):
    """Switch om OTGW kamertemperatuur-compensatie aan/uit te zetten."""

    _attr_has_entity_name = True
    _attr_name = "OTGW Compensatie"
    _attr_icon = "mdi:thermostat"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_otgw_compensation"
        self._attr_device_info = get_device_info(entry.entry_id)

        self._is_on = False
        self._current_offset: float = 0.0
        self._last_mpc_available: datetime | None = None

        # Config
        self._override_entity = entry.data.get(
            CONF_OTGW_ROOM_TEMP_OVERRIDE, DEFAULT_OTGW_ROOM_TEMP_OVERRIDE
        )
        max_offset = entry.data.get(CONF_OTGW_MAX_OFFSET, DEFAULT_OTGW_MAX_OFFSET)
        self._max_offset = min(float(max_offset), OTGW_HARD_MAX_OFFSET)

        # Entity IDs we monitor
        self._mpc_entity = "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur"
        self._supply_entity = DEFAULT_SUPPLY_TEMP_ENTITY
        self._flow_entity = entry.data.get("flow_entity", _FLOW_ENTITY_DEFAULT)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        self._last_mpc_available = datetime.now(timezone.utc)
        self.async_write_ha_state()
        _LOGGER.info("OTGW compensatie ingeschakeld")

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._async_reset_override()
        self.async_write_ha_state()
        _LOGGER.info("OTGW compensatie uitgeschakeld")

    async def async_added_to_hass(self) -> None:
        """Register listeners."""
        # Periodic compensation loop
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_compensation_cycle,
                timedelta(seconds=OTGW_CYCLE_SECONDS),
            )
        )

        # React to MPC and supply temp changes
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._mpc_entity, self._supply_entity],
                self._async_on_state_change,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Reset override when entity is removed."""
        await self._async_reset_override()

    @callback
    async def _async_on_state_change(self, event) -> None:
        """React to MPC or supply temp state changes."""
        if self._is_on:
            await self._async_compensation_cycle()

    async def _async_compensation_cycle(self, _now=None) -> None:
        """Run one compensation cycle."""
        if not self._is_on:
            return

        try:
            await self._async_do_compensation()
        except Exception:
            _LOGGER.exception("OTGW compensatie cyclus gefaald, reset offset")
            await self._async_reset_override()

        self.async_write_ha_state()

    async def _async_do_compensation(self) -> None:
        """Core compensation logic."""
        # Check if HP is active
        flow = get_float_state(self.hass,self._flow_entity)
        if flow is None or flow < MIN_FLOW_LPH:
            if self._current_offset != 0.0:
                _LOGGER.debug("HP inactief, reset offset")
                await self._async_reset_override()
            return

        # Read MPC recommended and actual supply temp
        mpc_advised = get_float_state(self.hass,self._mpc_entity)
        actual_supply = get_float_state(self.hass,self._supply_entity)

        # Watchdog: reset if MPC unavailable too long
        if mpc_advised is not None:
            self._last_mpc_available = datetime.now(timezone.utc)
        elif self._last_mpc_available is not None:
            elapsed = (
                datetime.now(timezone.utc) - self._last_mpc_available
            ).total_seconds()
            if elapsed > OTGW_UNAVAILABLE_TIMEOUT:
                _LOGGER.warning(
                    "MPC sensor >%d sec unavailable, reset offset",
                    OTGW_UNAVAILABLE_TIMEOUT,
                )
                await self._async_reset_override()
                return

        if mpc_advised is None or actual_supply is None:
            return

        # MPC error: negative = CiC overheats (supply too high)
        mpc_error = mpc_advised - actual_supply

        # Within dead band: gradually return to 0
        if abs(mpc_error) <= OTGW_DEAD_BAND:
            if self._current_offset != 0.0:
                self._current_offset = self._move_toward_zero(
                    self._current_offset, 0.1
                )
                await self._async_apply_offset()
            return

        # CiC overheats (mpc_error < 0): increase positive room temp offset
        if mpc_error < -OTGW_DEAD_BAND:
            # Target offset proportional to error (positive = room seems warmer)
            target_offset = min(
                self._max_offset,
                abs(mpc_error) * OTGW_GAIN_FACTOR,
            )
            # Rate limit
            new_offset = min(
                target_offset,
                self._current_offset + OTGW_MAX_RATE,
            )
            new_offset = min(new_offset, self._max_offset)
            self._current_offset = round(new_offset, 1)
            await self._async_apply_offset()
            return

        # CiC underheats (mpc_error > DEAD_BAND): reduce offset toward 0
        # We never make CiC think room is colder — only reduce our override
        if mpc_error > OTGW_DEAD_BAND and self._current_offset > 0:
            self._current_offset = self._move_toward_zero(
                self._current_offset, OTGW_MAX_RATE
            )
            await self._async_apply_offset()

    async def _async_apply_offset(self) -> None:
        """Write the room temperature override to OTGW."""
        if self._current_offset <= 0.0:
            await self._async_reset_override()
            return

        # Read actual room temperature to calculate override value
        room_temp_entity = self._entry.data.get(
            "indoor_temp_entity",
            "sensor.heatpump_thermostat_room_temperature",
        )
        room_temp = get_float_state(self.hass,room_temp_entity)
        if room_temp is None:
            _LOGGER.warning("Kan kamertemperatuur niet lezen, skip override")
            return

        override_value = round(room_temp + self._current_offset, 1)
        _LOGGER.debug(
            "OTGW override: kamer=%.1f + offset=%.1f → override=%.1f",
            room_temp,
            self._current_offset,
            override_value,
        )

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._override_entity, "value": override_value},
        )

    async def _async_reset_override(self) -> None:
        """Reset OTGW room temperature override to 0 (disabled)."""
        self._current_offset = 0.0
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": self._override_entity, "value": 0},
            )
            _LOGGER.debug("OTGW override reset naar 0")
        except Exception:
            _LOGGER.warning("Kon OTGW override niet resetten")

    @staticmethod
    def _move_toward_zero(value: float, step: float) -> float:
        """Move value toward zero by step."""
        if value > 0:
            return round(max(0.0, value - step), 1)
        return round(min(0.0, value + step), 1)

    @property
    def extra_state_attributes(self) -> dict:
        mpc_advised = get_float_state(self.hass, self._mpc_entity) if self.hass else None
        actual_supply = get_float_state(self.hass, self._supply_entity) if self.hass else None
        flow = get_float_state(self.hass, self._flow_entity) if self.hass else None

        return {
            "current_offset": self._current_offset,
            "max_offset": self._max_offset,
            "mpc_advised": mpc_advised,
            "actual_supply": actual_supply,
            "mpc_error": (
                round(mpc_advised - actual_supply, 1)
                if mpc_advised is not None and actual_supply is not None
                else None
            ),
            "hp_active": flow is not None and flow >= MIN_FLOW_LPH,
            "override_entity": self._override_entity,
        }
