"""Sound level compensation switch for Quatt Stooklijn integration.

Beïnvloedt het maximale geluidsniveau (en daarmee het vermogen) van de
warmtepomp op basis van de MPC-aanbeveling en gasketelactiviteit.

Logica (elke 5 min):
1. HP inactief (debiet < MIN_FLOW_LPH)
   → reset naar 'normal', stop
2. Gas actief (sensor.heatpump_boiler_heat_power > drempel)
   → één stap omhoog (HP mag harder draaien zodat ketel minder nodig is)
3. Aanvoertemp > MPC-advies + dead band
   → één stap omlaag (te veel warmte)
4. Aanvoertemp < MPC-advies − dead band
   → één stap omhoog (te weinig warmte)
5. Binnen dead band → geen actie

Niveauvolgorde (zwak → sterk): building87 → silent → library → normal
Reset naar 'normal' bij: switch off, HA shutdown, HP inactief.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FLOW_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_SUPPLY_TEMP_ENTITY,
    DOMAIN,
    MIN_FLOW_LPH,
    OTGW_CYCLE_SECONDS,
    OTGW_UNAVAILABLE_TIMEOUT,
)
from .coordinator import QuattStooklijnCoordinator
from .helpers import get_device_info, get_float_state

_LOGGER = logging.getLogger(__name__)

# Niveauvolgorde: index 0 = zwakst, index 3 = sterkst
_SOUND_LEVELS = ["building87", "silent", "library", "normal"]
_NORMAL_IDX = len(_SOUND_LEVELS) - 1

_MPC_ENTITY = "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur"
_DAY_SOUND_ENTITY = "select.cic_day_max_sound_level"
_NIGHT_SOUND_ENTITY = "select.cic_night_max_sound_level"
_BOILER_HEAT_ENTITY = "sensor.heatpump_boiler_heat_power"

_GAS_THRESHOLD_W = 200.0  # W: boven deze waarde is de gasketel actief
_DEAD_BAND = 2.0           # °C: geen actie binnen deze marge rond MPC-advies


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sound level switch from config entry."""
    if not {**entry.data, **entry.options}.get(CONF_SOUND_LEVEL_ENABLED, False):
        return

    coordinator: QuattStooklijnCoordinator = hass.data[DOMAIN][entry.entry_id]
    switch = QuattSoundLevelSwitch(coordinator, entry)
    level_sensor = QuattSoundLevelSensor(switch, entry)
    gas_sensor = QuattGasActiveSensor(switch, entry)
    switch._companions = [level_sensor, gas_sensor]
    async_add_entities([switch, level_sensor, gas_sensor])


class QuattSoundLevelSwitch(SwitchEntity, RestoreEntity):
    """Switch om geluidsniveau-compensatie aan/uit te zetten."""

    _attr_has_entity_name = True
    _attr_name = "Geluidsniveau Compensatie"
    _attr_icon = "mdi:volume-high"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sound_level_compensation"
        self._attr_device_info = get_device_info(entry.entry_id)

        self._is_on = False
        self._current_level_idx: int = _NORMAL_IDX
        self._last_mpc_available: datetime | None = None
        self._companions: list = []

        self._supply_entity = DEFAULT_SUPPLY_TEMP_ENTITY
        self._flow_entity = {**entry.data, **entry.options}.get(
            CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY
        )

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        self._last_mpc_available = datetime.now(timezone.utc)
        self.async_write_ha_state()
        _LOGGER.info("Geluidsniveau compensatie ingeschakeld")

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._async_reset_level()
        self.async_write_ha_state()
        _LOGGER.info("Geluidsniveau compensatie uitgeschakeld")

    async def async_added_to_hass(self) -> None:
        """Herstel vorige staat en registreer periodieke cyclus."""
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == "on"
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_compensation_cycle,
                timedelta(seconds=OTGW_CYCLE_SECONDS),
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._async_reset_level()

    def _notify_companions(self) -> None:
        """Stuur companions een state-update (voor Recorder-tracking)."""
        for companion in self._companions:
            companion.async_write_ha_state()

    async def _async_compensation_cycle(self, _now=None) -> None:
        """Voer één compensatiecyclus uit."""
        if not self._is_on:
            return
        try:
            await self._async_do_compensation()
        except Exception:
            _LOGGER.exception("Geluidsniveau compensatie cyclus gefaald, reset")
            await self._async_reset_level()
        self.async_write_ha_state()
        self._notify_companions()

    async def _async_do_compensation(self) -> None:
        """Kernlogica: bepaal of het geluidsniveau omhoog of omlaag moet."""

        # 1. HP actief?
        flow = get_float_state(self.hass, self._flow_entity)
        if flow is None or flow < MIN_FLOW_LPH:
            if self._current_level_idx != _NORMAL_IDX:
                _LOGGER.debug("HP inactief, reset geluidsniveau naar 'normal'")
                await self._async_reset_level()
            return

        # 2. Gas actief? → stap omhoog zodat HP meer kan leveren
        boiler_heat = get_float_state(self.hass, _BOILER_HEAT_ENTITY)
        if boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W:
            if self._current_level_idx < _NORMAL_IDX:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Gas actief (%.0f W) → geluidsniveau omhoog naar '%s'",
                    boiler_heat,
                    _SOUND_LEVELS[self._current_level_idx],
                )
                await self._async_apply_level()
            else:
                _LOGGER.debug("Gas actief maar geluidsniveau al op 'normal'")
            return

        # 3. MPC feedback
        mpc_advised = get_float_state(self.hass, _MPC_ENTITY)
        actual_supply = get_float_state(self.hass, self._supply_entity)

        # Watchdog: reset als MPC te lang unavailable
        if mpc_advised is not None:
            self._last_mpc_available = datetime.now(timezone.utc)
        elif self._last_mpc_available is not None:
            elapsed = (
                datetime.now(timezone.utc) - self._last_mpc_available
            ).total_seconds()
            if elapsed > OTGW_UNAVAILABLE_TIMEOUT:
                _LOGGER.warning(
                    "MPC sensor >%d sec unavailable, reset geluidsniveau",
                    OTGW_UNAVAILABLE_TIMEOUT,
                )
                await self._async_reset_level()
                return

        if mpc_advised is None or actual_supply is None:
            return

        mpc_error = mpc_advised - actual_supply  # negatief = te heet

        if mpc_error < -_DEAD_BAND:
            # Aanvoer te hoog → stap omlaag
            if self._current_level_idx > 0:
                self._current_level_idx -= 1
                _LOGGER.info(
                    "Aanvoer te hoog (fout=%.1f°C) → geluidsniveau omlaag naar '%s'",
                    mpc_error,
                    _SOUND_LEVELS[self._current_level_idx],
                )
                await self._async_apply_level()

        elif mpc_error > _DEAD_BAND:
            # Aanvoer te laag → stap omhoog
            if self._current_level_idx < _NORMAL_IDX:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Aanvoer te laag (fout=%.1f°C) → geluidsniveau omhoog naar '%s'",
                    mpc_error,
                    _SOUND_LEVELS[self._current_level_idx],
                )
                await self._async_apply_level()

    async def _async_apply_level(self) -> None:
        """Schrijf het huidige geluidsniveau naar dag- en nacht-entiteit."""
        level = _SOUND_LEVELS[self._current_level_idx]
        for entity_id in (_DAY_SOUND_ENTITY, _NIGHT_SOUND_ENTITY):
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": level},
            )
        _LOGGER.debug("Geluidsniveau ingesteld: %s", level)

    async def _async_reset_level(self) -> None:
        """Reset dag- en nacht-geluidsniveau naar 'normal'."""
        self._current_level_idx = _NORMAL_IDX
        try:
            for entity_id in (_DAY_SOUND_ENTITY, _NIGHT_SOUND_ENTITY):
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": entity_id, "option": "normal"},
                )
            _LOGGER.debug("Geluidsniveau gereset naar 'normal'")
        except Exception:
            _LOGGER.warning("Kon geluidsniveau niet resetten naar 'normal'")

    @property
    def current_sound_level(self) -> str:
        """Huidig geluidsniveau als string (voor companion sensor)."""
        return _SOUND_LEVELS[self._current_level_idx]

    @property
    def extra_state_attributes(self) -> dict:
        mpc_advised = get_float_state(self.hass, _MPC_ENTITY) if self.hass else None
        actual_supply = get_float_state(self.hass, self._supply_entity) if self.hass else None
        flow = get_float_state(self.hass, self._flow_entity) if self.hass else None
        boiler_heat = get_float_state(self.hass, _BOILER_HEAT_ENTITY) if self.hass else None

        return {
            "current_level": _SOUND_LEVELS[self._current_level_idx],
            "mpc_advised": mpc_advised,
            "actual_supply": actual_supply,
            "mpc_error": (
                round(mpc_advised - actual_supply, 1)
                if mpc_advised is not None and actual_supply is not None
                else None
            ),
            "hp_active": flow is not None and flow >= MIN_FLOW_LPH,
            "gas_active": boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W,
            "boiler_heat_w": round(boiler_heat) if boiler_heat is not None else None,
        }


class QuattSoundLevelSensor(SensorEntity):
    """Sensor met het huidige geluidsniveau — wordt bijgehouden door HA Recorder."""

    _attr_has_entity_name = True
    _attr_name = "Geluidsniveau"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, switch: QuattSoundLevelSwitch, entry: ConfigEntry) -> None:
        self._switch = switch
        self._attr_unique_id = f"{entry.entry_id}_sound_level_sensor"
        self._attr_device_info = get_device_info(entry.entry_id)

    @property
    def state(self) -> str:
        return self._switch.current_sound_level

    @property
    def available(self) -> bool:
        return self._switch.is_on


class QuattGasActiveSensor(BinarySensorEntity):
    """Binary sensor: gasketel actief als aanvulling op de warmtepomp."""

    _attr_has_entity_name = True
    _attr_name = "Gasketel Actief"
    _attr_icon = "mdi:fire"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, switch: QuattSoundLevelSwitch, entry: ConfigEntry) -> None:
        self._switch = switch
        self._attr_unique_id = f"{entry.entry_id}_gas_boiler_active"
        self._attr_device_info = get_device_info(entry.entry_id)

    @property
    def is_on(self) -> bool:
        if self._switch.hass is None:
            return False
        boiler_heat = get_float_state(self._switch.hass, _BOILER_HEAT_ENTITY)
        return boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W

    @property
    def extra_state_attributes(self) -> dict:
        if self._switch.hass is None:
            return {}
        boiler_heat = get_float_state(self._switch.hass, _BOILER_HEAT_ENTITY)
        return {"boiler_heat_w": round(boiler_heat) if boiler_heat is not None else None}
