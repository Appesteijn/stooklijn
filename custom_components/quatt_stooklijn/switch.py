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

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FLOW_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_MAX_DAY,
    CONF_SOUND_LEVEL_MAX_NIGHT,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_SOUND_LEVEL_MAX,
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

# CIC dag/nacht-grens — live uitgelezen uit Quatt integratie-sensoren
_SOUND_NIGHT_START_HOUR_ENTITY = "sensor.cic_sound_night_time_start_hour"
_SOUND_NIGHT_START_MIN_ENTITY = "sensor.cic_sound_night_time_start_min"
_SOUND_NIGHT_END_HOUR_ENTITY = "sensor.cic_sound_night_time_end_hour"
_SOUND_NIGHT_END_MIN_ENTITY = "sensor.cic_sound_night_time_end_min"
# Fallback als de Quatt-sensoren niet beschikbaar zijn
_NIGHT_START_HOUR_DEFAULT = 23
_NIGHT_END_HOUR_DEFAULT = 7

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
    async_add_entities([QuattSoundLevelSwitch(coordinator, entry)])


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

        self._is_on = True  # standaard aan: config-optie = feature actief
        self._current_level_idx: int = _NORMAL_IDX
        self._last_mpc_available: datetime | None = None

        merged = {**entry.data, **entry.options}
        self._supply_entity = DEFAULT_SUPPLY_TEMP_ENTITY
        self._flow_entity = merged.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY)

        # Maximaal geluidsniveau (= max vermogen) dat compensatie mag instellen
        max_day = merged.get(CONF_SOUND_LEVEL_MAX_DAY, DEFAULT_SOUND_LEVEL_MAX)
        max_night = merged.get(CONF_SOUND_LEVEL_MAX_NIGHT, DEFAULT_SOUND_LEVEL_MAX)
        self._max_day_idx = (
            _SOUND_LEVELS.index(max_day) if max_day in _SOUND_LEVELS else _NORMAL_IDX
        )
        self._max_night_idx = (
            _SOUND_LEVELS.index(max_night) if max_night in _SOUND_LEVELS else _NORMAL_IDX
        )

    def _is_night(self) -> bool:
        """Is het nu nacht volgens de CIC dag/nacht-scheiding?

        Leest de nachtvenster-sensoren live uit de Quatt integratie.
        Valt terug op hardcoded standaard als de sensoren niet beschikbaar zijn.
        """
        now = dt_util.now()
        current_minutes = now.hour * 60 + now.minute

        start_h = get_float_state(self.hass, _SOUND_NIGHT_START_HOUR_ENTITY)
        start_m = get_float_state(self.hass, _SOUND_NIGHT_START_MIN_ENTITY)
        end_h = get_float_state(self.hass, _SOUND_NIGHT_END_HOUR_ENTITY)
        end_m = get_float_state(self.hass, _SOUND_NIGHT_END_MIN_ENTITY)

        if start_h is not None and end_h is not None:
            night_start = int(start_h) * 60 + int(start_m or 0)
            night_end = int(end_h) * 60 + int(end_m or 0)
        else:
            night_start = _NIGHT_START_HOUR_DEFAULT * 60
            night_end = _NIGHT_END_HOUR_DEFAULT * 60

        # Nachtvenster kan middernacht overspannen (bijv. 23:00–07:00)
        if night_start > night_end:
            return current_minutes >= night_start or current_minutes < night_end
        else:
            return night_start <= current_minutes < night_end

    def _effective_max_idx(self) -> int:
        """Max niveau-index voor de huidige dagperiode."""
        return self._max_night_idx if self._is_night() else self._max_day_idx

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
        """Herstel vorige state en registreer periodieke cyclus."""
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == "on"
            # Herstel geluidsniveau-index uit opgeslagen attributen
            attrs = last_state.attributes or {}
            saved_level = attrs.get("current_level")
            if saved_level in _SOUND_LEVELS:
                self._current_level_idx = _SOUND_LEVELS.index(saved_level)

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_compensation_cycle,
                timedelta(seconds=OTGW_CYCLE_SECONDS),
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._async_reset_level()

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

    async def _async_do_compensation(self) -> None:
        """Kernlogica: bepaal of het geluidsniveau omhoog of omlaag moet."""

        effective_max = self._effective_max_idx()
        period = "nacht" if self._is_night() else "dag"

        # Clamp interne level bij dag/nacht-overgang
        if self._current_level_idx > effective_max:
            _LOGGER.info(
                "Dag/nacht-overgang (%s): geluidsniveau %s → %s",
                period,
                _SOUND_LEVELS[self._current_level_idx],
                _SOUND_LEVELS[effective_max],
            )
            self._current_level_idx = effective_max
            await self._async_apply_level()

        # 1. HP actief?
        flow = get_float_state(self.hass, self._flow_entity)
        if flow is None or flow < MIN_FLOW_LPH:
            if self._current_level_idx != effective_max:
                _LOGGER.debug(
                    "HP inactief, reset geluidsniveau naar %s-max (%s)",
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_reset_level()
            return

        # 2. Gas actief? → stap omhoog zodat HP meer kan leveren
        boiler_heat = get_float_state(self.hass, _BOILER_HEAT_ENTITY)
        if boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W:
            if self._current_level_idx < effective_max:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Gas actief (%.0f W) → geluidsniveau omhoog naar '%s' (%s-max: %s)",
                    boiler_heat,
                    _SOUND_LEVELS[self._current_level_idx],
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_apply_level()
            else:
                _LOGGER.debug(
                    "Gas actief maar geluidsniveau al op %s-max (%s)",
                    period,
                    _SOUND_LEVELS[effective_max],
                )
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
            if self._current_level_idx < effective_max:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Aanvoer te laag (fout=%.1f°C) → geluidsniveau omhoog naar '%s' (%s-max: %s)",
                    mpc_error,
                    _SOUND_LEVELS[self._current_level_idx],
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_apply_level()

    async def _async_apply_level(self) -> None:
        """Schrijf het huidige geluidsniveau naar dag- en nacht-entiteit, geclampt op max."""
        day_idx = min(self._current_level_idx, self._max_day_idx)
        night_idx = min(self._current_level_idx, self._max_night_idx)
        for entity_id, idx in (
            (_DAY_SOUND_ENTITY, day_idx),
            (_NIGHT_SOUND_ENTITY, night_idx),
        ):
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": _SOUND_LEVELS[idx]},
            )
        _LOGGER.debug(
            "Geluidsniveau ingesteld: dag=%s, nacht=%s",
            _SOUND_LEVELS[day_idx],
            _SOUND_LEVELS[night_idx],
        )

    async def _async_reset_level(self) -> None:
        """Reset dag- en nacht-geluidsniveau naar het geconfigureerde maximum."""
        self._current_level_idx = self._effective_max_idx()
        try:
            for entity_id, max_idx in (
                (_DAY_SOUND_ENTITY, self._max_day_idx),
                (_NIGHT_SOUND_ENTITY, self._max_night_idx),
            ):
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": entity_id, "option": _SOUND_LEVELS[max_idx]},
                )
            _LOGGER.debug(
                "Geluidsniveau gereset: dag=%s, nacht=%s",
                _SOUND_LEVELS[self._max_day_idx],
                _SOUND_LEVELS[self._max_night_idx],
            )
        except Exception:
            _LOGGER.warning("Kon geluidsniveau niet resetten")

    @property
    def extra_state_attributes(self) -> dict:
        mpc_advised = get_float_state(self.hass, _MPC_ENTITY) if self.hass else None
        actual_supply = get_float_state(self.hass, self._supply_entity) if self.hass else None
        flow = get_float_state(self.hass, self._flow_entity) if self.hass else None
        boiler_heat = get_float_state(self.hass, _BOILER_HEAT_ENTITY) if self.hass else None
        is_night = self._is_night()
        effective_max = self._effective_max_idx()

        start_h = get_float_state(self.hass, _SOUND_NIGHT_START_HOUR_ENTITY)
        start_m = get_float_state(self.hass, _SOUND_NIGHT_START_MIN_ENTITY)
        end_h = get_float_state(self.hass, _SOUND_NIGHT_END_HOUR_ENTITY)
        end_m = get_float_state(self.hass, _SOUND_NIGHT_END_MIN_ENTITY)
        night_window = (
            f"{int(start_h):02d}:{int(start_m or 0):02d}–{int(end_h):02d}:{int(end_m or 0):02d}"
            if start_h is not None and end_h is not None
            else f"{_NIGHT_START_HOUR_DEFAULT:02d}:00–{_NIGHT_END_HOUR_DEFAULT:02d}:00 (fallback)"
        )

        return {
            "current_level": _SOUND_LEVELS[self._current_level_idx],
            "period": "nacht" if is_night else "dag",
            "night_window": night_window,
            "effective_max": _SOUND_LEVELS[effective_max],
            "max_day": _SOUND_LEVELS[self._max_day_idx],
            "max_night": _SOUND_LEVELS[self._max_night_idx],
            "effective_day": _SOUND_LEVELS[min(self._current_level_idx, self._max_day_idx)],
            "effective_night": _SOUND_LEVELS[min(self._current_level_idx, self._max_night_idx)],
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
