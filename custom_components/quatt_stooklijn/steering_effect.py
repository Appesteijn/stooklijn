"""Sensor "Sturingseffect": meet per instelling van de knoppen wat sturen oplevert.

Het rekenwerk staat in ``analysis/steering_effect.py``; hier alleen het
bemonsteren, het afsluiten van een dag en het bewaren.

Elke vijf minuten wordt vastgelegd hoe de knoppen staan — de max.
aanvoertemperatuur (uit, welke bron, of gepauzeerd omdat de warmtevraag Power
House aanstuurt), de geluidsniveaucompensatie (uit, of welke bron) en de
warmtevraagkoppeling naar OpenQuatt Power House — samen met kamer, setpoint,
buiten, stroom, warmte, gas, aanvoer en de ingestelde max. aanvoertemperatuur. Om middernacht wordt
de dag afgesloten met het aantal compressorstarts uit de starttelling.

De sensor stuurt niets. Hij is er om een keuze tussen bronnen te maken op
comfort en verbruik, niet op gelijkenis met wat de CiC zelf deed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import ENTITY_ID_FORMAT, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .advice_sources import is_fallback, valid_source
from .analysis.steering_effect import (
    CEILING_TOLERANCE_K,
    COLD_K,
    DEFAULT_BASE_TEMP,
    FALLBACK_1R1C,
    KEEP_DAYS,
    PAUSED,
    MIN_DEGREE_DAYS,
    WARM_K,
    Day,
    Sample,
    config_label,
    counts_for_comparison,
    summarize,
)
from .ch_max_water import heat_demand_entity
from .const import (
    CONF_CH_MAX_WATER_ENABLED,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_SOURCE,
    DEFAULT_SOUND_LEVEL_SOURCE,
    DOMAIN,
    MIN_FLOW_LPH,
)
from .discovery import (
    ROLE_BOILER_HEAT,
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    async_entity_has_value,
    async_heat_demand_link,
)
from .helpers import get_device_info, get_float_state, resolve_own_entity_id
from .sources import ROLE_CONF_KEYS, async_source_entity

_LOGGER = logging.getLogger(__name__)

SAMPLE_INTERVAL = timedelta(minutes=5)
STORAGE_VERSION = 1
#: Uiterlijk zo lang na de eerste nieuwe meting wordt weggeschreven; bij het
#: afsluiten van HA meteen (final write van de Store).
SAVE_DELAY_S = 30 * 60
#: Zoveel afgesloten dagen staan in het attribuut ``dagen``.
DAYS_SHOWN = 14
#: Een balanspunt buiten dit bereik is geen huis maar een mislukte fit.
BASE_TEMP_RANGE = (8.0, 24.0)


class QuattSteeringEffectSensor(SensorEntity):
    """Wat de knoppen opleveren, per instelling. State: de instelling van nu."""

    _attr_has_entity_name = True
    _attr_translation_key = "steering_effect"
    _attr_icon = "mdi:scale-balance"
    # De lijsten zijn voor de kaart, niet voor de recorder: ze veranderen elke
    # vijf minuten en zouden de database bij elke meting een kopie geven.
    _unrecorded_attributes = frozenset({"vandaag", "dagen", "per_instelling"})

    def __init__(self, hass: HomeAssistant, coordinator, entry: ConfigEntry,
                 mpc_sensor=None, starts_sensor=None) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._mpc = mpc_sensor
        self._starts_sensor = starts_sensor
        self._attr_unique_id = f"{entry.entry_id}_steering_effect"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_sturingseffect", hass=hass
        )
        self._store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.steering_effect_{entry.entry_id}"
        )
        self._days: list[dict] = []
        self._day: Day | None = None
        self._last_ts: datetime | None = None
        self._config_now: str | None = None
        #: Opslaan pas na een geslaagde lezing, anders overschrijft de eerste
        #: opslag na een mislukte lezing maanden aan dagen met niets.
        self._loaded = False
        #: Er staat al een uitgestelde opslag klaar. Opnieuw inplannen zou de
        #: timer opschuiven, waardoor hij nooit afgaat.
        self._save_pending = False

    # -- levenscyclus ------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.warning(
                "Sturingseffect: opslag onleesbaar; er wordt niets weggeschreven",
                exc_info=True,
            )
            data = None
        else:
            self._loaded = True
        if isinstance(data, dict):
            self._days = list(data.get("days") or [])[-KEEP_DAYS:]
            if data.get("current"):
                try:
                    self._day = Day.from_dict(data["current"])
                except (KeyError, TypeError, ValueError):
                    self._day = None
            ts = data.get("last_ts")
            self._last_ts = dt_util.parse_datetime(ts) if ts else None

        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_sample, SAMPLE_INTERVAL)
        )

    # -- meten -------------------------------------------------------------

    async def _async_sample(self, _now=None) -> None:
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date().isoformat()
        minutes = SAMPLE_INTERVAL.total_seconds() / 60.0
        if self._day is None:
            self._day = Day(date=today)
        elif self._day.date != today:
            await self._async_close_day()
            self._day = Day(date=today)
        elif self._last_ts is not None:
            minutes = max(0.0, (now - self._last_ts).total_seconds() / 60.0)

        sample = self._sample()
        self._config_now = sample.config
        self._day.add(sample, minutes, MIN_FLOW_LPH)
        self._last_ts = now
        self._schedule_save()
        self.async_write_ha_state()

    def _sample(self) -> Sample:
        cfg = {**self._entry.data, **self._entry.options}
        demand = self._demand_link_active()
        max_water, ceiling = self._ceiling_state(cfg, demand)
        setpoint = None
        if self._mpc is not None:
            try:
                setpoint = self._mpc._resolve_room_setpoint()[0]
            except Exception:  # noqa: BLE001 — meten mag nooit de sensor breken
                setpoint = None
        return Sample(
            config=config_label(max_water, self._sound_state(cfg), demand),
            t_indoor=self._value(ROLE_INDOOR_TEMP, cfg),
            setpoint=setpoint,
            t_outdoor=self._value(ROLE_OUTDOOR_TEMP, cfg),
            p_el_w=self._value(ROLE_POWER_INPUT, cfg),
            p_heat_w=self._value(ROLE_TOTAL_POWER, cfg),
            p_gas_w=self._value(ROLE_BOILER_HEAT, cfg),
            supply=self._value(ROLE_SUPPLY_TEMP, cfg),
            ceiling=ceiling,
            flow_lph=self._value(ROLE_FLOW_RATE, cfg),
            base_temp=self._base_temp(),
        )

    def _value(self, role: str, cfg: dict) -> float | None:
        entity_id = async_source_entity(
            self.hass, self._entry.entry_id, role,
            config=cfg, conf_key=ROLE_CONF_KEYS.get(role),
        )
        return get_float_state(self.hass, entity_id) if entity_id else None

    def _base_temp(self) -> float:
        """Graaddagbasis: het balanspunt van het huis, anders 18 °C."""
        data = getattr(self._coordinator, "data", None)
        bp = getattr(getattr(data, "heat_loss_hp", None), "balance_point", None)
        if isinstance(bp, (int, float)) and BASE_TEMP_RANGE[0] <= bp <= BASE_TEMP_RANGE[1]:
            return float(bp)
        return DEFAULT_BASE_TEMP

    # -- stand van de knoppen ---------------------------------------------

    def _demand_link_active(self) -> bool:
        """Stuurt de warmtevraag OpenQuatt Power House nu rechtstreeks aan?"""
        entity_id = heat_demand_entity(self.hass, self._entry.entry_id)
        if not async_entity_has_value(self.hass, entity_id):
            return False
        return async_heat_demand_link(self.hass, entity_id).active

    def _ceiling_state(self, cfg: dict, demand: bool) -> tuple[str, float | None]:
        """Stand van de max. aanvoertemperatuur, en wat er nu op de regelaar staat."""
        if not cfg.get(CONF_CH_MAX_WATER_ENABLED, False):
            return "uit", None
        ctrl = self.hass.data.get(DOMAIN, {}).get(f"{self._entry.entry_id}_ch_max_water")
        if ctrl is None:
            return "uit", None
        if demand:
            # De controller treedt dan terug (zie ChMaxWaterController._async_tick).
            return PAUSED, None
        limit_entity = ctrl.target_entity or ctrl._resolve_number_entity()
        ceiling = get_float_state(self.hass, limit_entity) if limit_entity else None
        return self._effective(ctrl._source), ceiling

    def _sound_state(self, cfg: dict) -> str:
        """Uit, of de bron van de compensatie. Alleen als de switch echt aan staat."""
        if not cfg.get(CONF_SOUND_LEVEL_ENABLED, False):
            return "uit"
        switch_id = resolve_own_entity_id(
            self.hass, "switch", self._entry.entry_id, "sound_level_compensation"
        )
        state = self.hass.states.get(switch_id) if isinstance(switch_id, str) else None
        if state is None or state.state != "on":
            return "uit"
        return self._effective(
            valid_source(cfg.get(CONF_SOUND_LEVEL_SOURCE), DEFAULT_SOUND_LEVEL_SOURCE)
        )

    def _effective(self, source: str) -> str:
        """De bron zoals hij nu werkelijk stuurt: 1R1C kan op MPC terugvallen."""
        if source == "1r1c" and is_fallback(self.hass, self._entry.entry_id, source):
            return FALLBACK_1R1C
        return source

    # -- dag afsluiten en bewaren -----------------------------------------

    def _starts(self, date: str, until: datetime | None = None) -> int | None:
        if self._starts_sensor is None:
            return None
        start = dt_util.start_of_local_day(datetime.fromisoformat(date).date())
        end = until or start + timedelta(days=1)
        try:
            return self._starts_sensor.starts_between(start, end)
        except Exception:  # noqa: BLE001
            return None

    async def _async_close_day(self) -> None:
        day = self._day
        if day is None or day.minutes <= 0:
            return
        self._days.append(day.record(self._starts(day.date)))
        self._days = self._days[-KEEP_DAYS:]
        _LOGGER.debug("Sturingseffect: dag %s afgesloten", day.date)
        if self._loaded:
            await self._store.async_save(self._snapshot())
            self._save_pending = False

    def _snapshot(self) -> dict:
        self._save_pending = False
        return {
            "days": self._days,
            "current": self._day.to_dict() if self._day else None,
            "last_ts": self._last_ts.isoformat() if self._last_ts else None,
        }

    def _schedule_save(self) -> None:
        if self._loaded and not self._save_pending:
            self._save_pending = True
            self._store.async_delay_save(self._snapshot, SAVE_DELAY_S)

    # -- tonen -------------------------------------------------------------

    @property
    def native_value(self) -> str | None:
        return self._config_now

    @property
    def extra_state_attributes(self) -> dict:
        vandaag = None
        if self._day is not None:
            vandaag = self._day.record(self._starts(self._day.date, dt_util.utcnow()))
        return {
            "per_instelling": summarize(self._days),
            "vandaag": vandaag,
            "dagen": self._days[-DAYS_SHOWN:],
            "dagen_gemeten": len(self._days),
            "dagen_in_vergelijk": sum(1 for r in self._days if counts_for_comparison(r)),
            "drempels": {
                "te_koud_k": COLD_K,
                "te_warm_k": WARM_K,
                "max_aanvoer_marge_k": CEILING_TOLERANCE_K,
                "min_graaddagen": MIN_DEGREE_DAYS,
                "graaddag_basis_c": round(self._base_temp(), 1),
            },
        }
