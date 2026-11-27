"""Sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
import logging
import math
import time
from typing import Any
from urllib.parse import quote

from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .analysis.model_benchmark import Comparison, compare
from .analysis.min_power import MinPowerCurve, learn_min_power
from .analysis.shift_room_temp import TRACK_WINDOW_HOURS
from .analysis.stooklijn import freezing_capacity
from .analysis.thermal_model import (
    AdviceDemand,
    BalanceBias,
    OnlineRCModel,
    advice_demand,
    advice_forward_without_model,
    cap_demand,
    rc_advice_demand,
    simulate_advice_forward,
    simulate_coast_time,
    simulate_forward,
    simulate_recovery_on_min,
    stored_heat_kwh,
    supply_temp_for,
)
from homeassistant.helpers.storage import Store

from .const import (
    SOURCE_NAMES,
    STATS_UNITS,
    CONF_DAL_END_HOUR,
    CONF_DAL_START_HOUR,
    CONF_DAL_WEEKEND,
    CONF_PRICE_DAL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MODE,
    CONF_PRICE_NORMAL,
    DEFAULT_DAL_END_HOUR,
    DEFAULT_DAL_START_HOUR,
    DEFAULT_DAL_WEEKEND,
    DEFAULT_PRICE_DAL,
    DEFAULT_PRICE_MODE,
    DEFAULT_PRICE_NORMAL,
    DEMAND_SHIFT_HOURS,
    DEMAND_SHIFT_MAX_DRIFT_K,
    DEMAND_SHIFT_MAX_OVERSHOOT_K,
    DEMAND_SHIFT_MIN_RUN_MINUTES,
    DEMAND_SHIFT_STEP_MINUTES,
    PRICE_MODE_DYNAMIC,
    PRICE_MODE_NORMAL_DAL,
    PRICE_MODE_OFF,
    PRICE_SHIFT_KEEP_DAYS,
    PRICE_SHIFT_METHOD,
    PRICE_SHIFT_MIN_FORECAST_FRACTION,
    PRICE_SHIFT_RETRIES,
    PRICE_SHIFT_RETRY_DELAY,
    PRICE_SHIFT_RUN_HOUR,
    PRICE_SHIFT_RUN_MINUTE,
    PRICE_SHIFT_STORAGE_KEY,
    PRICE_SHIFT_STORAGE_VERSION,
    CONF_CH_MAX_WATER_ENABLED,
    CONF_CH_MAX_WATER_ENTITY,
    CONF_CIC_RATED_POWER,
    CONF_CIC_ZERO_POWER_TEMP,
    CONF_COMFORT_FLOOR_TEMP,
    CONF_COMPRESSOR_2_ENTITY,
    CONF_COMPRESSOR_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_QUATT_CLOUD_ENABLED,
    FORECAST_RETRY_DELAYS,
    DEFAULT_QUATT_CLOUD_ENABLED,
    CONF_BOILER_HEAT_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_ROOM_SETPOINT_ENTITY,
    CONF_ROOM_SETPOINT_FALLBACK,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    COAST_MAX_HOURS,
    MIN_POWER_RELEARN,
    COAST_STEP_MINUTES,
    COMPRESSOR_REFRESH_INTERVAL,
    COMPRESSOR_STORAGE_KEY,
    COMPRESSOR_STORAGE_VERSION,
    DEFAULT_COMFORT_FLOOR_TEMP,
    DEFAULT_ROOM_SETPOINT,
    DEFAULT_WEATHER_ENTITY,
    DOMAIN,
    MIN_FLOW_LPH,
    MIN_HEAT_OUTPUT_W,
    NOMINAL_FLOW_LPH,
    MPC_FORECAST_HOURS,
    MPC_ADVICE_NO_DEMAND,
    MPC_ADVICE_NO_INPUT,
    MPC_ADVICE_OK,
    MPC_ADVICE_STATUS_ATTR,
    MPC_2R2C_START_TTL_S,
    CONF_PV_POWER_ENTITY,
    CONF_WINDOW_AZIMUTH,
    DEFAULT_ORIENTATION_AZIMUTH,
    PV_ORIENTATION_HISTORY_DAYS,
    PV_ORIENTATION_REFRESH_HOURS,
    WINDOW_AZIMUTH_CANDIDATES,
    SOLAR_FORECAST_LOG_STORAGE_KEY,
    MPC_CATCHUP_FACTOR,
    MPC_SUPPLY_TEMP_MAX,
    MPC_SUPPLY_TEMP_MIN,
    OPEN_METEO_FORECAST_URL,
    SOLAR_FORECAST_MAX_AGE_SECONDS,
    ROOM_SETPOINT_MAX,
    ROOM_SETPOINT_MIN,
    SIGNAL_SOUND_LEVEL,
    SOLAR_RADIATION_DEFAULT_FACTOR,
    DEFROST_POWER_W,
    BALANCE_BIAS_MAX_DEFICIT_K,
    BALANCE_BIAS_MAX_GAP_S,
    BALANCE_BIAS_MAX_K,
    BALANCE_BIAS_SETTLE_HOURS,
    BALANCE_BIAS_TI_HOURS,
)
from .discovery import (
    ROLE_COMPRESSOR,
    ROLE_COMPRESSOR_2,
    ROLE_DEFROST,
    ROLE_DEFROST_2,
    ROLE_FLOW_RATE,
    ROLE_RETURN_TEMP,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_ROOM_SETPOINT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    ROLE_BOILER_HEAT,
    async_config_quatt_hubs,
    async_resolve_candidates,
)
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData
from .helpers import (
    FlowSmoother,
    ReturnHold,
    get_device_info,
    get_effective_flow,
    get_float_state,
)
from .heat_demand import (
    HEARTBEAT_INTERVAL_SECONDS,
    OPENQUATT_CACHE_SECONDS,
    OUTDOOR_MAX_AGE_SECONDS,
    SOURCE_SELECTOR_ENTITY,
)
from .sources import (
    ENTITY_PREFIX,
    MIRROR_SPECS,
    OVERVIEW_SLUG,
    MirrorSpec,
    SourceRegistry,
    async_source_entity,
)
from .cycling import CycleTracker

# Rol → sleutel in de opslag. Vastgelegd: de sleutels staan op schijf.
_COMPRESSOR_STORE_KEYS = {
    ROLE_COMPRESSOR: "runs_hp1",
    ROLE_COMPRESSOR_2: "runs_hp2",
}
from .highres_log import SAMPLE_INTERVAL, HighResLogStore
from .room_temperature import async_room_temperature, reference_entities, room_value
from .analysis.solar_forecast_log import SolarForecastLog
from .analysis.solar_input import SCALE_MEASURED, Orientation
from .thermal_store import ThermalModelStore

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuattSensorDescription(SensorEntityDescription):
    """Describe a Quatt Stooklijn sensor."""

    value_fn: Callable[[QuattStooklijnData], Any] = lambda _: None
    attr_fn: Callable[[QuattStooklijnData], dict | None] = lambda _: None


SENSOR_DESCRIPTIONS: list[QuattSensorDescription] = [
    QuattSensorDescription(
        key="heat_loss_coefficient",
        translation_key="heat_loss_coefficient",
        native_unit_of_measurement="W/K",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-thermometer-outline",
        value_fn=lambda d: (
            round(d.heat_loss_hp.heat_loss_coefficient, 1)
            if d.heat_loss_hp.heat_loss_coefficient
            else None
        ),
        attr_fn=lambda d: {
            "r2": d.heat_loss_hp.r2,
            "slope": d.heat_loss_hp.slope,
            "intercept": d.heat_loss_hp.intercept,
            "balance_point": d.heat_loss_hp.balance_point,
            "scatter_data": d.heat_loss_hp.scatter_data,
            "heat_at_temps": d.heat_loss_hp.heat_at_temps,
        }
        if d.heat_loss_hp.slope
        else None,
    ),
    QuattSensorDescription(
        key="balance_point",
        translation_key="balance_point",
        native_unit_of_measurement="\u00b0C",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-check",
        value_fn=lambda d: (
            round(d.heat_loss_hp.balance_point, 1)
            if d.heat_loss_hp.balance_point
            else None
        ),
    ),
    QuattSensorDescription(
        key="optimal_stooklijn_slope",
        translation_key="optimal_stooklijn_slope",
        native_unit_of_measurement="W/\u00b0C",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line",
        value_fn=lambda d: (
            round(d.stooklijn.slope_optimal, 1)
            if d.stooklijn.slope_optimal
            else None
        ),
        attr_fn=lambda d: {
            "intercept": d.stooklijn.intercept_optimal,
            "r2": d.stooklijn.r2_optimal,
            "balance_temp": d.stooklijn.balance_temp_optimal,
            "scatter_data": d.stooklijn.scatter_data,
            "quatt_slope_ratio": (
                round(d.stooklijn.slope_api_daily / d.stooklijn.slope_optimal, 2)
                if d.stooklijn.slope_api_daily and d.stooklijn.slope_optimal
                else None
            ),
        }
        if d.stooklijn.slope_optimal
        else None,
    ),
    QuattSensorDescription(
        key="quatt_stooklijn_slope",
        translation_key="quatt_stooklijn_slope",
        native_unit_of_measurement="W/\u00b0C",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line-variant",
        value_fn=lambda d: (
            round(d.stooklijn.slope_api, 1) if d.stooklijn.slope_api else None
        ),
        attr_fn=lambda d: {
            "intercept": d.stooklijn.intercept_api,
            "balance_temp_daily": d.stooklijn.balance_temp_api_daily,
            "slope_daily": d.stooklijn.slope_api_daily,
            "intercept_daily": d.stooklijn.intercept_api_daily,
        }
        if d.stooklijn.slope_api
        else None,
    ),
    QuattSensorDescription(
        key="knee_temperature",
        translation_key="knee_temperature",
        native_unit_of_measurement="\u00b0C",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-alert",
        # ``is not None``: 0,0 °C is een geldig knikpunt.
        value_fn=lambda d: (
            round(d.stooklijn.knee_temperature, 2)
            if d.stooklijn.knee_temperature is not None
            else None
        ),
        attr_fn=lambda d: {
            "knee_power": d.stooklijn.knee_power,
        }
        if d.stooklijn.knee_temperature is not None
        else None,
    ),
    QuattSensorDescription(
        key="average_cop",
        translation_key="average_cop",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
        # Totale warmte / totale stroom over de stookdagen van de analyse, niet
        # het gemiddelde van de dag-COP's (zie analysis.utils.cop_energy).
        value_fn=lambda d: round(d.average_cop, 2) if d.average_cop else None,
        attr_fn=lambda d: {
            "cop_scatter_data": d.stooklijn.cop_scatter_data,
            "stookdagen": d.average_cop_days,
        }
        if d.stooklijn.cop_scatter_data or d.average_cop_days
        else None,
    ),
    QuattSensorDescription(
        key="freezing_performance_slope",
        translation_key="freezing_performance_slope",
        native_unit_of_measurement="W/\u00b0C",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:snowflake-thermometer",
        value_fn=lambda d: (
            round(d.stooklijn.slope_local, 1)
            if d.stooklijn.slope_local
            else None
        ),
        attr_fn=lambda d: {
            "intercept": d.stooklijn.intercept_local,
            "r2": d.stooklijn.r2_local,
            "knee_temperature": d.stooklijn.knee_temperature,
            # Kouder dan dit trekt het advies de lijn niet door (geen grens).
            "koudste_meting": (
                round(d.stooklijn.capacity_min_temp, 1)
                if d.stooklijn.capacity_min_temp is not None
                else None
            ),
        }
        if d.stooklijn.slope_local
        else None,
    ),
    QuattSensorDescription(
        key="gas_heat_loss_coefficient",
        translation_key="gas_heat_loss_coefficient",
        native_unit_of_measurement="W/K",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value_fn=lambda d: (
            round(d.heat_loss_gas.heat_loss_coefficient, 1)
            if d.heat_loss_gas.heat_loss_coefficient
            else None
        ),
        attr_fn=lambda d: {
            "r2": d.heat_loss_gas.r2,
            "slope": d.heat_loss_gas.slope,
            "intercept": d.heat_loss_gas.intercept,
            "balance_point": d.heat_loss_gas.balance_point,
            "scatter_data": d.heat_loss_gas.scatter_data,
        }
        if d.heat_loss_gas.slope
        else None,
    ),
    QuattSensorDescription(
        key="last_analysis",
        translation_key="last_analysis",
        icon="mdi:clock-check-outline",
        value_fn=lambda d: (
            d.last_analysis.strftime("%Y-%m-%d") if d.last_analysis else None
        ),
    ),
    QuattSensorDescription(
        key="analysis_status",
        translation_key="analysis_status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.analysis_status,
        attr_fn=lambda _: None,
    ),
    QuattSensorDescription(
        key="data_stats",
        translation_key="data_stats",
        icon="mdi:database-outline",
        value_fn=lambda d: d.data_stats.get("daily_days", 0) if d.data_stats else 0,
        attr_fn=lambda d: d.data_stats if d.data_stats else None,
    ),
    QuattSensorDescription(
        key="openquatt_balance_point",
        translation_key="openquatt_balance_point",
        native_unit_of_measurement="°C",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-thermometer-outline",
        value_fn=lambda d: (
            round(d.stooklijn.balance_temp_optimal, 1)
            if d.stooklijn.balance_temp_optimal is not None
            else None
        ),
        attr_fn=lambda d: {
            "heat_loss_coefficient": d.heat_loss_hp.heat_loss_coefficient,
            "source": "heat_loss_model",
        }
        if d.heat_loss_hp.slope is not None
        else None,
    ),
]


# Vaste object-id per beschrijving.
#
# Sinds de namen via ``translation_key`` lopen leidt HA de entity-id af uit de
# vertaalde naam, en dus uit de taal van de gebruiker: op een Engelse HA zou
# ``sensor.quatt_warmteanalyse_warmtevraag`` ineens ``..._heat_demand`` heten en
# breekt het meegeleverde dashboard. Deze tabel pint wat er nu in het veld staat.
#
# ``quatt_warmteanalyse_quatt_warmteanalyse_slope`` is geen typefout maar de id
# die HA ooit voor "Quatt Stooklijn Slope" heeft gemaakt; hernoemen zou elke
# bestaande verwijzing breken voor niets.
DESCRIPTION_OBJECT_IDS: dict[str, str] = {
    "heat_loss_coefficient": "quatt_warmteanalyse_heat_loss_coefficient",
    "balance_point": "quatt_warmteanalyse_balance_point_temperature",
    "optimal_stooklijn_slope": "quatt_warmteanalyse_optimal_stooklijn_slope",
    "quatt_stooklijn_slope": "quatt_warmteanalyse_quatt_warmteanalyse_slope",
    "knee_temperature": "quatt_warmteanalyse_knee_temperature",
    "average_cop": "quatt_warmteanalyse_average_cop",
    "freezing_performance_slope": "quatt_warmteanalyse_freezing_performance_slope",
    "gas_heat_loss_coefficient": "quatt_warmteanalyse_gas_heat_loss_coefficient",
    "last_analysis": "quatt_warmteanalyse_last_analysis",
    "analysis_status": "quatt_warmteanalyse_analysis_status",
    "data_stats": "quatt_warmteanalyse_data_statistieken",
    "openquatt_balance_point": "quatt_warmteanalyse_openquatt_balance_point",
}


def candidate_entities(
    hass: HomeAssistant,
    entry_id: str,
    roles: tuple[str, ...],
    extra: tuple[str, ...] = (),
) -> list[str]:
    """Alle entity-ID's die voor deze rollen ooit de bron kunnen zijn.

    Bedoeld voor state-change listeners. Alleen de nu actieve bron volgen is te
    weinig: valt die weg, dan komt er per definitie geen state-change meer
    binnen van de entity die het overneemt.
    """
    from .sources import SourceRegistry

    registry: SourceRegistry | None = hass.data.get(DOMAIN, {}).get(
        f"{entry_id}_sources"
    )
    tracked: set[str] = {e for e in extra if e}
    for role in roles:
        source = registry.get(role) if registry else None
        if source:
            tracked.update(c for c in source.candidates if c)
    if ROLE_INDOOR_TEMP in roles:
        # De extra kamersensoren begrenzen de thermostaat: verandert er een,
        # dan kan de kamertemperatuur veranderen (room_temperature.py).
        room = async_room_temperature(hass, entry_id)
        if room is not None:
            tracked.update(room.references)
    return sorted(tracked)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Quatt Stooklijn sensors from config entry."""
    coordinator: QuattStooklijnCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        QuattStooklijnSensor(coordinator, description, entry)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(QuattSupplyTempSensor(coordinator, entry))
    entities.append(QuattEstimatedCopSensor(coordinator, entry))
    mpc_sensor = QuattMpcSensor(coordinator, entry)
    entities.append(mpc_sensor)
    # Coast-time sensor deelt het RC-model + forecast van de MPC-sensor.
    entities.append(QuattCoastTimeSensor(coordinator, entry, mpc_sensor))
    entities.append(QuattStoredHeatSensor(coordinator, entry, mpc_sensor))
    # Aanvoeradvies volgens het 1R1C: de stuurbron "1r1c" (advice_sources).
    entities.append(QuattRcAdviceSensor(coordinator, entry, mpc_sensor))
    # Deelt hetzelfde model en meetlogboek; scoort alleen, stuurt niets aan.
    benchmark_sensor = QuattModelBenchmarkSensor(coordinator, entry, mpc_sensor)
    entities.append(benchmark_sensor)
    mpc_sensor.benchmark = benchmark_sensor
    # Hoe goed de zonverwachting is, per vooruitkijktijd (ZONNEWARMTE_PLAN.md).
    entities.append(QuattSolarForecastSensor(coordinator, entry, mpc_sensor))

    # Geen entity-ID's meer meegeven: die werden hier één keer bij het opstarten
    # bepaald en daarna nooit meer. De sensor zoekt ze nu zelf op via de
    # bronregistry, zodat een bronwissel ook hier doorkomt.
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "stooklijn",
        "sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
    ))
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "mpc",
        "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
    ))
    entities.append(QuattCopPerformanceSensor(hass, coordinator, entry))
    entities.append(QuattAdviceSensor(coordinator, entry))
    entities.append(QuattOpenQuattCurveSensor(coordinator, entry))
    entities.append(QuattPowerHouseCalibrationSensor(hass, coordinator, entry))
    entities.append(QuattHeatDemandSensor(hass, coordinator, entry))

    # Schaduwmeting van de prijsverschuiving — alleen als er een tarief is
    # ingesteld. Zonder tarief valt er niets te meten, en een sensor die altijd
    # "niet ingesteld" zegt kost alleen aandacht.
    cfg = {**entry.data, **entry.options}
    if cfg.get(CONF_PRICE_MODE, DEFAULT_PRICE_MODE) != PRICE_MODE_OFF:
        price_sensor = QuattPriceShiftSensor(
            hass, coordinator, entry, mpc_sensor, benchmark_sensor
        )
        entities.append(price_sensor)
        # Schaduwfase: de stap van nu uit het verschoven plan (stuurt niets).
        entities.append(QuattShiftPlanSensor(hass, entry, price_sensor, mpc_sensor))

    if {**entry.data, **entry.options}.get(CONF_SOUND_LEVEL_ENABLED, False):
        entities.append(QuattSoundLevelSensor(hass, entry))

    if {**entry.data, **entry.options}.get(CONF_CH_MAX_WATER_ENABLED, False):
        entities.append(QuattChMaxWaterSensor(hass, entry))

    # Spiegelsensoren: één stabiel entity-ID per meting, ongeacht of Quatt of
    # OpenQuatt hem levert. Dashboards horen hieraan te hangen.
    registry: SourceRegistry = hass.data[DOMAIN][f"{entry.entry_id}_sources"]
    entities.extend(
        QuattSourceMirrorSensor(hass, entry, registry, spec) for spec in MIRROR_SPECS
    )
    starts_sensor = QuattCompressorStartsSensor(hass, entry)
    entities.append(starts_sensor)
    entities.append(QuattSourceOverviewSensor(hass, entry, registry))
    # Wat de knoppen opleveren, per instelling: comfort, verbruik, starts, gas.
    # Altijd aan, ook zonder knop: "alles uit" is de referentie.
    from .steering_effect import QuattSteeringEffectSensor
    steering_sensor = QuattSteeringEffectSensor(
        hass, coordinator, entry, mpc_sensor, starts_sensor
    )
    entities.append(steering_sensor)
    # Het meetlogboek neemt aanvoer, retour, debiet, opgenomen vermogen,
    # setpoint en de knoppen over uit de meting van het sturingseffect.
    mpc_sensor.steering = steering_sensor

    async_add_entities(entities)


class QuattStooklijnSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Sensor entity for Quatt Stooklijn metrics."""

    entity_description: QuattSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        description: QuattSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            DESCRIPTION_OBJECT_IDS.get(
                description.key, f"quatt_warmteanalyse_{description.key}"
            ),
            hass=coordinator.hass,
        )

    _STATUS_ICONS = {
        "running": "mdi:progress-clock",
        "completed": "mdi:check-circle",
        "no_data": "mdi:timer-sand",
        "error": "mdi:alert-circle",
        "idle": "mdi:information-outline",
    }

    @property
    def icon(self) -> str | None:
        """Return dynamic icon for analysis_status sensor."""
        if self.entity_description.key == "analysis_status" and self.coordinator.data:
            status = self.coordinator.data.analysis_status
            return self._STATUS_ICONS.get(status, "mdi:information-outline")
        return self.entity_description.icon

    @property
    def native_value(self):
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra attributes (scatter data for dashboard)."""
        if self.coordinator.data is None:
            return None
        if self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)


class QuattEstimatedCopSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Live sensor: estimated COP at current outdoor temperature.

    Interpolates within the COP table of the heat-loss analysis.
    Updates whenever the outdoor temperature sensor changes.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "estimated_cop"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge-low"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_estimated_cop"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_geschatte_actuele_cop", hass=coordinator.hass
        )

    @property
    def _outdoor_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_OUTDOOR_TEMP,
            config=cfg, conf_key=CONF_TEMP_ENTITIES,
        )

    async def async_added_to_hass(self) -> None:
        """Register state listener for outdoor temperature."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id, (ROLE_OUTDOOR_TEMP,)
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        """Recompute when outdoor temperature changes."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """COP bij de buitentemperatuur van nu, uit de COP-tabel (warmte / stroom).

        Zelfde pad als de tabel in de warmteverliesanalyse
        (``cop_at_temperature``), zodat sensor en tabel niet uiteenlopen.
        """
        from .analysis.heat_loss import cop_from_table  # noqa: PLC0415

        if self.coordinator.data is None:
            return None
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        if t_outdoor is None:
            return None
        table = getattr(self.coordinator.data.heat_loss_hp, "heat_at_temps", None)
        cop = cop_from_table(table, t_outdoor)
        return round(cop, 2) if cop is not None else None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose inputs for transparency."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        cop_data = (self.coordinator.data.stooklijn.cop_scatter_data if self.coordinator.data else None) or []
        return {
            "outdoor_temp": t_outdoor,
            "data_points": len(cop_data),
        }


def _effective_flow(coordinator, flow_lph: float | None) -> float:
    """Debiet voor een aanvoeradvies: gladgestreken via de gedeelde FlowSmoother.

    Zonder smoother (een coordinator uit een test of van vóór deze versie)
    het momentane debiet, zoals voorheen.
    """
    smoother = getattr(coordinator, "flow_smoother", None)
    if isinstance(smoother, FlowSmoother):
        return smoother.effective(flow_lph)
    return get_effective_flow(flow_lph)


def _feed_flow(coordinator, flow_lph: float | None) -> None:
    """Geef een debietmeting door aan de gedeelde FlowSmoother.

    Beide adviessensoren roepen dit aan bij elke inputwijziging; dezelfde meting
    twee keer op hetzelfde moment verandert niets.
    """
    smoother = getattr(coordinator, "flow_smoother", None)
    if isinstance(smoother, FlowSmoother):
        smoother.update(flow_lph, dt_util.utcnow())


#: Hoe vaak de ontdooivlaggen opnieuw worden opgezocht. Het register doorzoeken
#: bij elke retourmeting is te duur; een nieuwe integratie komt zelden.
DEFROST_ENTITIES_REFRESH_S = 600.0


def _defrost_entities(hass: HomeAssistant, coordinator, entry: ConfigEntry) -> list[str]:
    """De ontdooivlaggen van Quatt en OpenQuatt, beide units, allemaal."""
    cached = getattr(coordinator, "defrost_entities", None)
    if cached is not None and monotonic() - cached[0] < DEFROST_ENTITIES_REFRESH_S:
        return cached[1]
    hubs = async_config_quatt_hubs(hass, {**entry.data, **entry.options})
    entities = [
        entity_id
        for role in (ROLE_DEFROST, ROLE_DEFROST_2)
        for entity_id in async_resolve_candidates(hass, None, role, hubs=hubs)
    ]
    coordinator.defrost_entities = (monotonic(), entities)
    return entities


def _defrosting(hass: HomeAssistant, coordinator, entry: ConfigEntry) -> bool:
    """Ontdooit een van de units nu?

    Een vlag die aan staat, of een thermisch vermogen onder ``DEFROST_POWER_W``.
    Het vermogen omdat een vlag kan ontbreken of niet meekomen: op het testhuis
    stond de Quatt-vlag een week lang uit tijdens 128 ontdooiingen.
    """
    for entity_id in _defrost_entities(hass, coordinator, entry):
        state = hass.states.get(entity_id)
        if state is not None and state.state == "on":
            return True
    cfg = {**entry.data, **entry.options}
    power_entity = async_source_entity(
        hass, entry.entry_id, ROLE_TOTAL_POWER,
        config=cfg, conf_key=CONF_POWER_ENTITY,
    )
    power = get_float_state(hass, power_entity) if power_entity else None
    return power is not None and power < DEFROST_POWER_W


def _advice_return(
    hass: HomeAssistant, coordinator, entry: ConfigEntry, t_return: float | None
) -> float | None:
    """De retour waar een aanvoeradvies mee rekent; zie ReturnHold.

    Zonder ReturnHold (een coordinator uit een test of van vóór deze versie)
    de gemeten retour, zoals voorheen.
    """
    hold = getattr(coordinator, "return_hold", None)
    if not isinstance(hold, ReturnHold):
        return t_return
    hold.update(t_return, _defrosting(hass, coordinator, entry), dt_util.utcnow())
    return hold.effective(t_return)


def _new_balance_bias(
    bias_k: float = 0.0,
    setpoint: float | None = None,
    setpoint_since: float | None = None,
) -> BalanceBias:
    return BalanceBias(
        ti_hours=BALANCE_BIAS_TI_HOURS,
        max_k=BALANCE_BIAS_MAX_K,
        settle_hours=BALANCE_BIAS_SETTLE_HOURS,
        max_deficit_k=BALANCE_BIAS_MAX_DEFICIT_K,
        max_gap_s=BALANCE_BIAS_MAX_GAP_S,
        catchup_factor=MPC_CATCHUP_FACTOR,
        bias_k=bias_k,
        setpoint=setpoint,
        setpoint_since=setpoint_since,
    )


def _finite_or_none(value) -> float | None:
    """Een bewaard getal, of None als het ontbreekt of onbruikbaar is."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _return_held(coordinator) -> bool:
    """Houdt het advies nu een retour van vóór een ontdooiing vast?"""
    hold = getattr(coordinator, "return_hold", None)
    return isinstance(hold, ReturnHold) and hold.held is not None


class QuattSupplyTempSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Live sensor: aanbevolen aanvoertemperatuur op basis van actuele buitentemperatuur.

    Formule: T_aanvoer = T_retour + max(0, slope * T_buiten + intercept) / (1.16 * debiet_lph)
    """

    _attr_has_entity_name = True
    _attr_translation_key = "recommended_supply_temp"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-water"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_recommended_supply_temp"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_aanbevolen_aanvoertemperatuur", hass=coordinator.hass
        )

    @property
    def _outdoor_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_OUTDOOR_TEMP,
            config=cfg, conf_key=CONF_TEMP_ENTITIES,
        )

    @property
    def _flow_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_FLOW_RATE,
            config=cfg, conf_key=CONF_FLOW_ENTITY,
        )

    @property
    def _return_temp_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_RETURN_TEMP,
            config=cfg, conf_key=CONF_RETURN_TEMP_ENTITY,
        )

    def _emitter_when_pump_stopped(self, flow_lph: float | None):
        """Het afgiftemodel als de pomp stilstaat, anders None (dan de retour)."""
        emitter = getattr(self.coordinator, "emitter", None)
        if emitter is None or (flow_lph is not None and flow_lph >= MIN_FLOW_LPH):
            return None
        return emitter

    def _room_temp(self) -> float:
        cfg = {**self._entry.data, **self._entry.options}
        entity = async_source_entity(
            self.hass, self._entry.entry_id, ROLE_INDOOR_TEMP,
            config=cfg, conf_key=CONF_INDOOR_TEMP_ENTITY,
        )
        t_room = room_value(self.hass, self._entry.entry_id, entity)
        if t_room is not None:
            return t_room
        # Geen kamersensor: het kamerdoel, zoals de andere bronnen
        # (QuattMpcSensor._resolve_room_setpoint), niet een vaste 20 °C.
        setpoint_entity = async_source_entity(
            self.hass, self._entry.entry_id, ROLE_ROOM_SETPOINT,
            config=cfg, conf_key=CONF_ROOM_SETPOINT_ENTITY,
        )
        value = get_float_state(self.hass, setpoint_entity) if setpoint_entity else None
        if value is not None and ROOM_SETPOINT_MIN <= value <= ROOM_SETPOINT_MAX:
            return value
        return float(cfg.get(CONF_ROOM_SETPOINT_FALLBACK, DEFAULT_ROOM_SETPOINT))

    async def async_added_to_hass(self) -> None:
        """Register state listeners for live input sensors."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id,
                    # De kamer en het kamerdoel tellen mee zodra de pomp
                    # stilstaat en het afgiftemodel rekent.
                    (ROLE_OUTDOOR_TEMP, ROLE_FLOW_RATE, ROLE_RETURN_TEMP,
                     ROLE_INDOOR_TEMP, ROLE_ROOM_SETPOINT),
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        """Recompute when any input sensor changes."""
        _feed_flow(self.coordinator, get_float_state(self.hass, self._flow_entity))
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Calculate recommended supply temperature."""
        if self.coordinator.data is None:
            return None

        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None:
            return None

        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = _advice_return(
            self.hass, self.coordinator, self._entry,
            get_float_state(self.hass, self._return_temp_entity),
        )
        flow_lph = get_float_state(self.hass, self._flow_entity)

        # De retour is alleen nodig als de pomp draait; stilstaand rekent het
        # afgiftemodel zonder.
        if t_outdoor is None or (
            t_return is None and self._emitter_when_pump_stopped(flow_lph) is None
        ):
            return None

        from .analysis.utils import calc_heat_demand
        effective_flow = _effective_flow(self.coordinator, flow_lph)
        heat_demand_w = calc_heat_demand(heat_loss.slope, heat_loss.intercept, t_outdoor)
        if heat_demand_w <= 0:
            # Boven het balanspunt valt er niets te adviseren. Teruggeven van de
            # retourtemperatuur suggereert een advies dat er niet is.
            return None
        emitter = self._emitter_when_pump_stopped(flow_lph)
        if emitter is not None:
            # Stilstaande pomp: de retour is afgekoeld water. Dan de aanvoer
            # uit het afgiftemodel, zoals de MPC-tak (CLAUDE.md, "Aanvoer uit
            # vermogen").
            t_supply = emitter.supply(heat_demand_w, self._room_temp(), effective_flow)
        else:
            t_supply = t_return + heat_demand_w / (1.16 * effective_flow)
        # Dezelfde grenzen als de MPC-tak en _calc_heating_curve_breakpoints.
        # Zonder clamp schrijft één onzinnige retourtemperatuur — zoals tijdens
        # het bronwissel-venster bij een herstart — een advies van tientallen
        # graden onder nul de langetermijnstatistiek in, en daar komt het nooit
        # meer uit.
        t_supply = max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, t_supply))
        return round(t_supply, 1)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose formula inputs for transparency."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = get_float_state(self.hass, self._return_temp_entity)
        flow_lph = get_float_state(self.hass, self._flow_entity)

        heat_demand_w = None
        heat_demand_raw = None
        if (
            self.coordinator.data is not None
            and self.coordinator.data.heat_loss_hp.slope is not None
            and self.coordinator.data.heat_loss_hp.intercept is not None
            and t_outdoor is not None
        ):
            from .analysis.utils import calc_heat_demand
            heat_demand_raw = calc_heat_demand(
                self.coordinator.data.heat_loss_hp.slope,
                self.coordinator.data.heat_loss_hp.intercept,
                t_outdoor,
            )
            heat_demand_w = round(heat_demand_raw, 0)

        # Waarom de state er wel of niet staat, met dezelfde waarden als de
        # MPC-sensor. Een knop die op dit advies stuurt (zie advice_sources)
        # moet "boven het balanspunt, niets te stoken" kunnen onderscheiden van
        # "er ontbreekt een meting": het eerste betekent dat de aanvoer te warm
        # is, het tweede dat er niets bekend is.
        if (
            self.coordinator.data is None
            or self.coordinator.data.heat_loss_hp.slope is None
            or self.coordinator.data.heat_loss_hp.intercept is None
            or t_outdoor is None
            or (t_return is None and self._emitter_when_pump_stopped(flow_lph) is None)
        ):
            status = MPC_ADVICE_NO_INPUT
        elif heat_demand_raw is not None and heat_demand_raw <= 0:
            status = MPC_ADVICE_NO_DEMAND
        else:
            status = MPC_ADVICE_OK

        return {
            MPC_ADVICE_STATUS_ATTR: status,
            # De naam van deze stuurbron, zoals in de opties (SOURCE_NAMES).
            "bron_naam": SOURCE_NAMES["stooklijn"],
            "outdoor_temp": t_outdoor,
            "return_temp": t_return,
            # True: het advies rekent met de retour van vóór een ontdooiing
            # (ReturnHold), niet met de gemeten return_temp.
            "retour_vastgehouden": _return_held(self.coordinator),
            "flow_lph": flow_lph,
            "heat_demand_w": heat_demand_w,
            "aanvoer_uit": (
                "afgiftemodel" if self._emitter_when_pump_stopped(flow_lph) is not None
                else "retour"
            ),
        }


ADVICE_BREAKPOINT_TEMPS = (-10, -5, 0, 5, 10, 15)
# OpenQuatt hanteert een ander raster dan het generieke advies hierboven: zijn
# zes `Curve Tsupply @ …`-number-entiteiten staan vast op -20/-10/0/5/10/15.
# Die punten worden positioneel overgezet, dus een advies op het advies-raster
# schuift de koude kant een punt op: de waarde voor -10 landt dan op de knop
# voor -20. Wijzig deze reeks alleen als de firmware zijn knoppen wijzigt.
OPENQUATT_BREAKPOINT_TEMPS = (-20, -10, 0, 5, 10, 15)
ADVICE_NOMINAL_RETURN_TEMP = 28.0  # °C — typical return temp for breakpoint calc


def _calc_heating_curve_breakpoints(
    heat_loss_slope: float,
    heat_loss_intercept: float,
    t_return_nominal: float = ADVICE_NOMINAL_RETURN_TEMP,
    flow_nominal: float = NOMINAL_FLOW_LPH,
    outdoor_temps: tuple = ADVICE_BREAKPOINT_TEMPS,
    emitter=None,
    t_room: float | None = None,
) -> list[dict]:
    """Bereken optimale aanvoertemperatuur bij standaard buitentemperaturen.

    Gebruikt het heat loss model om voor elke buitentemp de benodigde
    aanvoertemperatuur te berekenen. Hergebruikt door Quatt Advies en
    OpenQuatt sensoren.

    Met ``emitter`` (afgiftemodel, geleerd uit de recorder) in plaats van een
    vaste retour van 28 °C en 800 l/u. Die vaste retour lag op het gemeten huis
    gemiddeld 1,8 K te hoog (november 2026). De kamer is dan die waarbij het
    afgiftemodel geleerd is (``room_c``), tenzij ``t_room`` gegeven is.
    """
    from .analysis.utils import calc_heat_demand

    if emitter is not None and t_room is None:
        t_room = emitter.room_c

    breakpoints = []
    for t_out in outdoor_temps:
        demand = calc_heat_demand(heat_loss_slope, heat_loss_intercept, t_out)
        if emitter is not None:
            t_supply = emitter.supply(max(0.0, demand), t_room)
        else:
            t_supply = t_return_nominal + demand / (1.16 * flow_nominal)
        t_supply = max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, t_supply))
        breakpoints.append({
            "buiten_temp": t_out,
            "aanvoer_temp": round(t_supply, 1),
        })
    return breakpoints


async def async_pv_hourly(
    hass: HomeAssistant, entity: str, start_ts: float, end_ts: float
) -> list[tuple[float, float]]:
    """Uurgemiddelden van een vermogenssensor uit de recorder, in W.

    ``(begin van het uur in UTC-seconden, gemiddeld W)``. Leeg als de sensor
    geen statistiek heeft (geen ``state_class``) of de recorder niets weet.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    start = dt_util.utc_from_timestamp(start_ts)
    end = dt_util.utc_from_timestamp(end_ts + 3600)

    def _fetch() -> dict:
        return statistics_during_period(
            hass, start_time=start, end_time=end,
            statistic_ids={entity}, period="hour", units=None, types={"mean"},
        )

    stats = await get_instance(hass).async_add_executor_job(_fetch)
    state = hass.states.get(entity)
    unit = str((state.attributes.get("unit_of_measurement") if state else "") or "")
    if _is_energy_unit(unit):
        return []
    factor = _power_unit_factor(unit)
    out: list[tuple[float, float]] = []
    for row in (stats or {}).get(entity, []):
        begin, mean = row.get("start"), row.get("mean")
        if begin is None or mean is None:
            continue
        if not isinstance(begin, (int, float)):
            begin = begin.timestamp()
        out.append((float(begin), float(mean) * factor))
    return out


async def async_hourly_means(
    hass: HomeAssistant, entities: list[str], start_ts: float, end_ts: float
) -> dict[str, dict[float, float]]:
    """Uurgemiddelden van meerdere sensoren uit de recorder, in hun eigen eenheid.

    Behalve temperatuur: die altijd in °C (``STATS_UNITS``).

    ``{entity: {begin van het uur (UTC-seconden): gemiddelde}}``.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    start = dt_util.utc_from_timestamp(start_ts)
    end = dt_util.utc_from_timestamp(end_ts + 3600)

    def _fetch() -> dict:
        return statistics_during_period(
            hass, start_time=start, end_time=end,
            statistic_ids=set(entities), period="hour", units=STATS_UNITS,
            types={"mean"},
        )

    stats = await get_instance(hass).async_add_executor_job(_fetch)
    out: dict[str, dict[float, float]] = {e: {} for e in entities}
    for entity in entities:
        for row in (stats or {}).get(entity, []):
            begin, mean = row.get("start"), row.get("mean")
            if begin is None or mean is None:
                continue
            if not isinstance(begin, (int, float)):
                begin = begin.timestamp()
            out[entity][float(begin)] = float(mean)
    return out


def _is_energy_unit(unit: str) -> bool:
    """Wh, kWh, MWh: een tellerstand, geen vermogen (Tweakers 22-11-2026:
    ``today_energy`` en ``lifetime_energy`` werden stilzwijgend als W gelezen)."""
    return unit.strip().lower() in ("wh", "kwh", "mwh")


def _power_unit_factor(unit: str) -> float:
    """Omrekenfactor naar W voor een vermogenseenheid (W, kW, MW)."""
    u = unit.strip().lower()
    if u == "kw":
        return 1000.0
    if u == "mw":
        return 1_000_000.0
    return 1.0


class QuattMpcSensor(CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity):
    """Shadow-mode MPC sensor: aanbevolen aanvoertemperatuur op basis van
    weersvoorspelling + zonnewinst.

    Schrijft niets naar de regelaar of klimaat-entiteiten — puur observatie voor
    vergelijking met de huidige stooklijn.

    Verversing:
    - Weersverwachting: elke uur via timer
    - Aanvoertemp: bij elke state-change van buitentemp / solar / flow / retour
    """

    #: Zie SolarForecastLog.lead_factor; op klasseniveau zodat een sensor die
    #: nog niet geladen is (of in tests via __new__ gemaakt) gewoon None heeft.
    solar_lead_factor: float | None = None
    #: Het sturingseffect; levert de extra kolommen van het meetlogboek.
    steering: Any = None
    #: Afgiftemodel (``analysis.emitter``), eens per etmaal geleerd uit de
    #: recorder; None tot dat lukt.
    emitter: Any = None
    emitter_reason: str = "nog niet geleerd"
    _emitter_at: datetime | None = None
    _emitter_failures: int = 0

    _attr_has_entity_name = True
    _attr_translation_key = "mpc_recommended_supply_temp"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:brain"
    # De vooruitblik is een tabel van MPC_FORECAST_HOURS rijen met ruim tien
    # velden per rij, en deze sensor schrijft bij elke state-change van
    # buitentemp, zon, flow of retour. Dat hoort niet elke keer de recorder in;
    # het dashboard leest het attribuut rechtstreeks. Bij zes uur was dat al
    # verspilling, bij twaalf het dubbele.
    # Afgiftemodel (met tabel per graad) en het voorspellende schaduwadvies
    # (een dict) om dezelfde reden: ze veranderen hooguit per etmaal of uur.
    _unrecorded_attributes = frozenset({"forecast", "afgiftemodel", "advies_voorspellend"})

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mpc_recommended_supply_temp"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur", hass=coordinator.hass
        )
        self._forecast: list[dict] = []
        self._forecast_fetched_at: float | None = None
        # Hoeveel herpogingen er al gedaan zijn na het opstarten, en of er al
        # gewaarschuwd is dat de verwachting structureel uitblijft.
        self._forecast_retry = 0
        self._forecast_warned = False
        self._solar_radiation: list[float] = []  # uurlijkse shortwave W/m² van Open-Meteo
        # De tijdstempels die bij die waarden horen (lokale tijd, uit Open-Meteo
        # zelf) en het moment van ophalen. Zonder die twee is een reeks van
        # gisteren niet te onderscheiden van een van vandaag.
        self._solar_times: list[str] = []
        self._solar_fetched_at = None
        # Online thermal model
        self._thermal_store = ThermalModelStore(coordinator.hass)
        self._highres_store = HighResLogStore(coordinator.hass)
        self._highres_loaded = False
        # Geleerd minimumvermogen, met het moment van leren; zie min_power_curve.
        self._min_power: MinPowerCurve = MinPowerCurve()
        self._min_power_at: datetime | None = None
        self._thermal_loaded = False
        # De modelvergelijking, voor de 2R2C-kolom in de vooruitblik. Wordt na
        # het aanmaken gezet: die sensor krijgt deze als argument, niet andersom.
        self.benchmark: QuattModelBenchmarkSensor | None = None
        # (monotone tijd, parameters, begintoestand); zie _room_2r2c.
        self._start_2r2c: tuple[float, tuple, Any] | None = None
        # Wat Open-Meteo per uur verwachtte, per vooruitkijktijd; zie
        # analysis/solar_forecast_log.py en de zonverwachtingssensor.
        self.solar_forecast_log = SolarForecastLog()
        self._solar_forecast_store = Store(
            coordinator.hass, 1, SOLAR_FORECAST_LOG_STORAGE_KEY
        )
        self._solar_forecast_loaded = False
        # Afgiftemodel: bewaard, zodat het na een herstart of opgeslagen optie
        # meteen weer rekent (zonder sprongen de stooklijnpunten ~1,8 K terug
        # naar de vaste retour tot het eerstvolgende leren).
        self._emitter_store = Store(
            coordinator.hass, 1, f"{DOMAIN}.emitter_{entry.entry_id}"
        )
        self._emitter_at: datetime | None = None
        # Verhouding lopend uur / verwachting 1–6 uur vooruit; None zolang er te
        # weinig zonuren zijn. Zie SolarForecastLog.lead_factor.
        self.solar_lead_factor: float | None = None
        # Geleerd, niet ingesteld: richting van de panelen (uit een jaar
        # PV-statistiek) en van de ramen (nachtelijke modelvergelijking).
        self.pv_azimuth: float | None = None
        self.pv_azimuth_scores: dict[int, float] = {}
        self._pv_orientation_at: datetime | None = None
        self.window_azimuth: float | None = None
        # De laatste PV-schaal die met de zon boven 15° bepaald is, per
        # zonnesensor; zie analysis/solar_input.measured_ghi. Bewaard, want rond
        # de jaarwisseling is hij uit het venster van 30 dagen niet te bepalen.
        self._pv_scale_store = Store(
            coordinator.hass, 1, f"{DOMAIN}.pv_scale_{entry.entry_id}"
        )
        self._pv_scale: dict | None = None
        self._pv_unit_warned: str | None = None
        # Correctie op het balanspunt tegen een blijvend tekort; zie
        # BalanceBias. Bewaard, want hij leert over dagen.
        self.balance_bias = _new_balance_bias()
        self._balance_bias_store = Store(
            coordinator.hass, 1, f"{DOMAIN}.balance_bias_{entry.entry_id}"
        )
        self._balance_bias_saved: float = 0.0
        # Het bewaarde setpoint met zijn tijdstip; een wissel moet ook bewaard
        # worden, anders telt hij na een herstart niet mee.
        self._balance_bias_saved_setpoint: tuple[float | None, float | None] = (None, None)

    # ------------------------------------------------------------------ helpers

    @property
    def _outdoor_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_OUTDOOR_TEMP,
            config=cfg, conf_key=CONF_TEMP_ENTITIES,
        )

    @property
    def _flow_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_FLOW_RATE,
            config=cfg, conf_key=CONF_FLOW_ENTITY,
        )

    @property
    def _return_temp_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_RETURN_TEMP,
            config=cfg, conf_key=CONF_RETURN_TEMP_ENTITY,
        )

    @property
    def _weather_entity(self) -> str:
        return {**self._entry.data, **self._entry.options}.get(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY)

    @property
    def _comfort_floor(self) -> float:
        """Referentie voor de warmtebuffer, gedeeld met de uitlooptijd-sensor.

        Beide gaan over dezelfde vraag — hoeveel ruimte is er tot het te koud
        wordt — dus ze horen dezelfde ondergrens te gebruiken.
        """
        cfg = {**self._entry.data, **self._entry.options}
        return cfg.get(CONF_COMFORT_FLOOR_TEMP, DEFAULT_COMFORT_FLOOR_TEMP)

    @property
    def _indoor_temp_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_INDOOR_TEMP,
            config=cfg, conf_key=CONF_INDOOR_TEMP_ENTITY,
        )

    @property
    def _power_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_TOTAL_POWER,
            config=cfg, conf_key=CONF_POWER_ENTITY,
        )

    @property
    def _boiler_heat_entity(self) -> str | None:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_BOILER_HEAT,
            config=cfg, conf_key=CONF_BOILER_HEAT_ENTITY,
        )

    def _house_heat_w(self) -> float:
        """Warmte die het huis nu krijgt: warmtepomp plus gasketel (W).

        Het huismodel (1R1C) en het meetlogboek (waar het 2R2C op leert)
        rekenden tot v0.10.7-beta.6 alleen met de warmtepomp. De
        warmteverliesanalyse telde de ketel al wel mee. Bij een hybride die
        bijspringt ziet het model de kamer dan opwarmen zonder warmte, en
        dat verklaart het met een negatieve warmtecapaciteit en een veel te
        grote zonnewinst. Nagebootst (21-11-2026, ketel van 8 kW die bijspringt
        bij een kamer 0,4 K onder het setpoint): C negatief in 20 van de 20
        vloerverwarmingshuizen en 9 van de 20 radiatorhuizen, tegen 0 van de 20
        zonder ketel.

        De ketelmeting van Quatt bevat geen tapwater: op het gemeten huis was
        hij de hele zomer van 2025 precies 0. Zonder ketelmeting telt de ketel
        als 0, zoals voorheen.
        """
        q_hp = get_float_state(self.hass, self._power_entity) or 0.0
        boiler = self._boiler_heat_entity
        q_boiler = (get_float_state(self.hass, boiler) or 0.0) if boiler else 0.0
        return max(0.0, q_hp) + max(0.0, q_boiler)

    @property
    def _room_setpoint_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_ROOM_SETPOINT,
            config=cfg, conf_key=CONF_ROOM_SETPOINT_ENTITY,
        )

    def _capacity_w(self, t_outdoor: float) -> float | None:
        """Wat de warmtepomp bij deze buitentemperatuur kan leveren, of None.

        De vriescurve uit de analyse (zelfde lijn als de Power House-kalibratie
        gebruikt), niet doorgetrokken onder het koudste gemeten uur en nooit
        onder wat de pomp aantoonbaar leverde. Zie ``freezing_capacity``.
        """
        data = getattr(self.coordinator, "data", None)
        stooklijn = getattr(data, "stooklijn", None)
        min_temp = getattr(stooklijn, "capacity_min_temp", None)
        measured = getattr(stooklijn, "capacity_measured", None)
        return freezing_capacity(
            t_outdoor,
            getattr(stooklijn, "slope_local", None),
            getattr(stooklijn, "intercept_local", None),
            min_temp if isinstance(min_temp, (int, float)) else None,
            measured if isinstance(measured, list) else None,
        )

    def _resolve_room_setpoint(self) -> tuple[float, str]:
        """Het kamerdoel waar het advies op rekent, en waar het vandaan komt.

        De thermostaat weet dit zelf, dus die is de bron. Een vaste waarde is
        hier geen neutrale keuze maar een aanname over het huis: het advies is
        lineair in ``t_setpoint − t_binnen``, dus wie anders stookt dan de
        aangehouden waarde kreeg elk uur van de dag een advies dat er even ver
        naast zat.

        De terugval is instelbaar en niet hardgecodeerd. Juist de installatie
        die de sensor niet heeft — een thermostaat die niet via OpenTherm aan de
        CiC hangt — zou anders permanent op andermans kamertemperatuur rekenen,
        zonder enige manier om dat te corrigeren.

        Teruggegeven wordt ook waar de waarde vandaan komt. Zonder die
        markering is een advies dat op de terugval draait niet te onderscheiden
        van een advies dat de thermostaat volgt — en dat verschil is precies
        wat je wilt zien als het advies raar oogt.
        """
        value = get_float_state(self.hass, self._room_setpoint_entity)
        if value is not None and ROOM_SETPOINT_MIN <= value <= ROOM_SETPOINT_MAX:
            return value, "thermostaat"
        cfg = {**self._entry.data, **self._entry.options}
        return (
            cfg.get(CONF_ROOM_SETPOINT_FALLBACK, DEFAULT_ROOM_SETPOINT),
            "terugval",
        )

    @property
    def _solar_forecast_is_fresh(self) -> bool:
        """Of de opgehaalde stralingsreeks nog meetelt."""
        if not self._solar_radiation or self._solar_fetched_at is None:
            return False
        age = (dt_util.utcnow() - self._solar_fetched_at).total_seconds()
        return age <= SOLAR_FORECAST_MAX_AGE_SECONDS

    def _solar_radiation_at(self, moment) -> float | None:
        """Straling (W/m²) in het uur waar ``moment`` in valt, of None als onbekend.

        Zoekt op tijdstempel in ``hourly.time``, niet op uur-index. Open-Meteo
        levert die reeks in de tijdzone die we zelf hebben meegegeven, dus een
        gelijke stempel is per definitie hetzelfde uur — ook op de dagen dat er
        een uur verdwijnt of dubbel voorkomt.

        **De stempel is het eind van het uur.** ``shortwave_radiation`` is bij
        Open-Meteo het gemiddelde over het voorafgaande uur: de waarde op 11:00
        hoort bij 10:00–11:00. Tot v0.10.6-beta.12 werd de stempel van het
        begin van het uur gebruikt, en kwam de zon in elk model een uur te laat.
        Gemeten tegen de PV-opbrengst (sep–nov 2026): de oude reeks paste het
        best bij de PV van het uur ervóór (correlatie 0,85 tegen 0,79), en het
        zwaartepunt van de dag lag 0,8 uur te laat. Zie ZONNEWARMTE_PLAN.md.

        Het eind van het uur wordt in UTC opgeteld en dan pas lokaal gemaakt,
        zodat het ook klopt op de nacht van een DST-overgang.
        """
        if not self._solar_forecast_is_fresh:
            return None
        tz = dt_util.now().tzinfo
        start = moment.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        end = (start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(tz)
        stamp = end.strftime("%Y-%m-%dT%H:00")
        try:
            idx = self._solar_times.index(stamp)
        except ValueError:
            return None
        if idx >= len(self._solar_radiation):
            return None
        value = self._solar_radiation[idx]
        return float(value) if value is not None else None

    def _get_current_solar_radiation_wm2(self) -> float | None:
        """Straling van dit uur (W/m²), of None als die niet bekend is.

        Bewust ``None`` en niet ``0.0``. Een nul is een uitspraak — het is donker
        — en die uitspraak ging tot v0.10.1 ook de RC-regressie in zodra
        Open-Meteo onbereikbaar was. Zonnewinst belandt dan in het residu, θ₁ en
        θ₂ verklaren dezelfde trage drift, en het geleerde warmteverlies zakt weg
        terwijl ``converged`` gewoon ``true`` blijft. Precies die fout, alleen
        stil en bij iedereen zonder internet naar Open-Meteo.

        Gebruikt als zonne-invoer voor het RC-model in plaats van PV-opbrengst:
        - W/m² is een directe fysische maat voor inkomende zonne-energie
        - geen collineariteit met buitentemperatuur via paneelkarakteristieken
        - g_solar wordt fysisch betekenisvol: effectief raamoppervlak × ZTA
        """
        return self._solar_radiation_at(dt_util.now())

    @property
    def thermal_model(self) -> OnlineRCModel | None:
        """Het geleerde RC-model, of None tot het geladen is.

        Gedeeld met de coast-time sensor zodat die niet zijn eigen kopie hoeft
        te trainen — beide gebruiken hetzelfde online-geleerde model.
        """
        return self._thermal_store.model if self._thermal_loaded else None

    @property
    def min_power_curve(self) -> MinPowerCurve:
        """Het laagste continue warmtevermogen per buitentemperatuur.

        Geleerd uit het meetlogboek en een uur bewaard: het minimum verandert
        alleen met nieuwe runs, en het logboek doorlopen kost bij een vol
        seizoen een paar milliseconden die niet bij elke state-write horen.
        Leeg tot er genoeg stabiele runs zijn; zie ``analysis.min_power``.
        Gedeeld met de uitlooptijd- en de prijsverschuivingssensor.
        """
        now = dt_util.utcnow()
        if (
            self._min_power_at is None
            or now - self._min_power_at >= MIN_POWER_RELEARN
        ):
            self._min_power = learn_min_power(self.highres_samples)
            self._min_power_at = now
        return self._min_power

    @property
    def highres_samples(self) -> list[list[float]]:
        """Momentopname van het meetlogboek, voor analyse buiten de event loop.

        Nieuwe rijen in de volgorde van ``highres_log.COLUMNS``, onbekend als
        None, dus een executor-thread mag er rustig overheen lopen terwijl er
        hier nieuwe monsters bij komen.
        """
        return self.highres_rows()

    def highres_rows(self, since: float | None = None) -> list[list[float]]:
        """Zoals ``highres_samples``, met ``since`` alleen het recente deel."""
        if not self._highres_loaded:
            return []
        return self._highres_store.log.rows(since)

    @property
    def thermal_params(self) -> dict:
        """Geleerde RC-parameters, of een lege stand tot het model er is.

        Gedeeld met de schaduwsensor voor de warmtevraag, die de thermische
        massa nodig heeft om de kamerdrift te schatten. Zonder ``converged``
        staat er geen bruikbare C in en hoort er niet op gerekend te worden.
        """
        model = self.thermal_model
        return model.params if model is not None else {"converged": False}

    def simulate_heat_need(
        self, fc_temps: list[float], fc_solar_wm2: list[float]
    ) -> tuple[list[float] | None, str | None]:
        """Warmte per uur (W) die het RC-model nodig heeft om het setpoint te houden.

        Gedeeld met de prijsverschuivingssensor, als de basisreeks die daar
        herverdeeld wordt. Zelfde simulatie als de vooruitblik, dus met zon,
        kamertemperatuur en thermische massa — alleen over de lengte van de
        aangeleverde reeks in plaats van ``MPC_FORECAST_HOURS``.

        De simulatie start op ``max(kamer, setpoint)``. Staat de kamer nu onder
        het setpoint, dan vraagt ``calc_required_power`` in uur 0 het hele tekort
        in één keer — bij deze C al snel meer dan het firmwareplafond. Dat
        inhalen moet nú gebeuren en valt niet te verschuiven; het hoort niet in
        de reeks die herverdeeld wordt.

        Retourneert ``(reeks, None)`` of ``(None, reden)``.
        """
        model = self.thermal_model
        if model is None or not model.is_converged:
            return None, "huismodel nog niet gekalibreerd"
        t_indoor = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
        if t_indoor is None:
            return None, "geen kamertemperatuur"
        setpoint, _source = self._resolve_room_setpoint()
        sim = simulate_forward(
            model,
            t_indoor_now=max(t_indoor, setpoint),
            # Alleen voor de aanvoertemperatuur, die hier niet gebruikt wordt.
            t_return=setpoint,
            flow_lph=0.0,
            forecast_t_outdoor=fc_temps,
            forecast_q_solar=fc_solar_wm2,
            t_setpoint=setpoint,
            comfort_floor=self._comfort_floor,
        )
        return [float(step["q_hp_needed_w"]) for step in sim], None

    def build_forecast_arrays(
        self, t_outdoor: float | None, n_hours: int = MPC_FORECAST_HOURS
    ) -> tuple[list[float], list[float], list[dict]]:
        """Bouw tijd-uitgelijnde forecast-arrays voor de komende ``n_hours``.

        Retourneert ``(fc_temps, fc_solar_wm2, fc_meta)``. De HA weather-entity
        kan een forecast leveren die pas over enkele uren begint; we indexeren op
        uren vanaf het lopende hele uur en vallen voor gat-uren terug op de huidige buitentemp.
        Gedeeld door de MPC-sensor en de coast-time sensor.
        """
        # Index i is het uur dat om ``hour_start + i`` begint, voor temperatuur,
        # zon én de afnemers (prijzen, tijdlabels). Vanaf het exacte tijdstip
        # rekenen rondde na :30 het volgende uur af op index 0, terwijl zon en
        # prijs op het lopende uur bleven: alles een uur uit de pas.
        hour_start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        now_local = dt_util.now()

        # Build time-indexed lookup: hours_from_hour_start -> forecast point
        fc_lookup: dict[int, dict] = {}
        for point in self._forecast:
            dt_str = point.get("datetime")
            if dt_str:
                try:
                    fc_dt = datetime.fromisoformat(dt_str)
                    hours_ahead = round((fc_dt - hour_start).total_seconds() / 3600)
                    if 0 <= hours_ahead < n_hours:
                        fc_lookup[hours_ahead] = point
                except (ValueError, TypeError):
                    pass

        fc_temps: list[float] = []
        fc_solar_wm2: list[float] = []   # W/m² for RC model (direct from Open-Meteo)
        fc_meta: list[dict] = []
        for i in range(n_hours):
            # Temperature: use forecast if available, else current outdoor sensor
            if i in fc_lookup:
                fc_temp = fc_lookup[i].get("temperature")
                fc_dt_str = fc_lookup[i].get("datetime")
                fc_condition = fc_lookup[i].get("condition", "")
            elif t_outdoor is not None:
                fc_temp = t_outdoor
                fc_dt_str = None
                fc_condition = "current"
            else:
                break

            if fc_temp is None:
                break

            fc_temps.append(fc_temp)
            # Op tijdstempel opzoeken, niet op ``nu + i`` als index: de reeks
            # begint om middernacht en niet bij dit uur, en op een dag met een
            # DST-overgang klopt uur-rekenwerk sowieso niet.
            rad = self._solar_radiation_at(now_local + timedelta(hours=i))
            # Uren vooruit naar wat Open-Meteo zegt als het uur er is: daarop
            # hebben de huismodellen hun zonfactor geleerd. Niet naar gemeten
            # zon, zie SolarForecastLog.lead_factor.
            if rad is not None and i > 0 and self.solar_lead_factor is not None:
                rad = rad * self.solar_lead_factor
            # Voor de simulatie moet er een getal staan. Nul is hier de veilige
            # kant: geen zon meerekenen vraagt eerder te veel warmte dan te
            # weinig. ``shortwave_wm2`` blijft None zodat in de tabel zichtbaar
            # is dat het een aanname was en geen verwachting.
            fc_solar_wm2.append(rad if rad is not None else 0.0)
            fc_meta.append({
                "datetime": fc_dt_str,
                "condition": fc_condition,
                "shortwave_wm2": rad,
            })
        return fc_temps, fc_solar_wm2, fc_meta

    # ------------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # De extra kamersensoren hebben per uur een helderheid nodig om te zien
        # welke sensor zon krijgt; die zon kent deze sensor.
        room = async_room_temperature(self.hass, self._entry.entry_id)
        if room is not None:
            room.clearness_provider = self.async_hourly_clearness
            room.rows_provider = self.highres_rows

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id,
                    # Het kamerdoel hoort hierbij: verzet de bewoner de
                    # thermostaat, dan verandert het advies mee, en zonder deze
                    # rol zou dat pas zichtbaar worden bij de eerstvolgende
                    # verandering van buitentemp, debiet of retour.
                    (ROLE_OUTDOOR_TEMP, ROLE_FLOW_RATE, ROLE_RETURN_TEMP,
                     ROLE_ROOM_SETPOINT),
                ),
                self._handle_state_change,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_hourly_update,
                timedelta(hours=1),
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_highres_sample,
                SAMPLE_INTERVAL,
            )
        )
        # Laad thermal model + forecast direct bij opstarten
        await self._async_load_thermal_model()
        await self._async_load_highres_log()
        await self._async_load_emitter()
        if self.emitter is None:
            # Geen bewaard model (eerste installatie): niet tot de eerste
            # uurtik wachten, maar ook niet in de drukte van het opstarten.
            self.async_on_remove(async_call_later(
                self.hass, self.EMITTER_FIRST_LEARN_DELAY_S,
                self._async_learn_emitter_first))
        await self._async_refresh_forecast()
        await self._async_refresh_solar_radiation()

    async def async_will_remove_from_hass(self) -> None:
        """Meetlogboek wegschrijven als de integratie herladen of verwijderd wordt.

        Bij het afsluiten van HA zorgt de uitgestelde opslag in
        ``HighResLogStore`` daarvoor; bij herladen wordt de entiteit
        verwijderd zonder dat HA stopt, en dan moet het hier.
        """
        await super().async_will_remove_from_hass()
        if self._highres_loaded:
            try:
                await self._highres_store.async_save()
            except Exception:
                _LOGGER.warning("Meetlogboek opslaan mislukt", exc_info=True)

    async def _handle_state_change(self, event) -> None:
        _feed_flow(self.coordinator, get_float_state(self.hass, self._flow_entity))
        self.async_write_ha_state()

    #: Minimaal zoveel 5-minutenmonsters in het interval voor een uurgemiddelde
    #: (een half uur). Minder, dan de momentopname van de vorige tik.
    INTERVAL_HEAT_MIN_SAMPLES = 6

    def _interval_heat_w(self, now: datetime) -> float | None:
        """Gemiddelde warmte naar het huis sinds de vorige modelupdate, of None.

        Uit het 5-minutenlogboek (warmtepomp + ketel, zie _house_heat_w). None
        als het logboek niet geladen is, als er geen vorige update is, of als
        het interval te weinig monsters heeft; dan leert het model van de
        momentopname, zoals voor v0.10.7-beta.6.
        """
        if not getattr(self, "_highres_loaded", False):
            return None
        since = self._thermal_store.model._prev_timestamp
        if since is None:
            return None
        mean, n = self._highres_store.log.mean_heat_between(since, now)
        return mean if n >= self.INTERVAL_HEAT_MIN_SAMPLES else None

    def _seed_from_batch(self, model: OnlineRCModel) -> None:
        """Zaai een vers model met U uit de warmteverliesanalyse, als die er is."""
        if not self.coordinator.data:
            return
        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is not None:
            # heat_loss.slope is negative (W per °C increase),
            # the heat loss coefficient U = -slope
            model.initialise_from_batch(-heat_loss.slope)

    async def _async_reset_broken_model(self) -> bool:
        """Begin opnieuw als het huismodel ontspoord is (``is_broken``).

        Ook tijdens bedrijf, niet alleen bij het laden: anders blijft een model
        dat ontspoort tot de volgende herstart staan. De ankers (U, P_c) komen
        er direct weer op via ``_refresh_u_prior``. Geeft True als er opnieuw
        is begonnen.
        """
        model = self._thermal_store.model
        if not model.is_broken:
            return False
        _LOGGER.warning(
            "Huismodel ontspoord na %d updates (%s); het begint opnieuw vanaf "
            "de warmteverliesanalyse",
            model._rls.n_updates, model.params,
        )
        fresh = OnlineRCModel()
        self._seed_from_batch(fresh)
        self._thermal_store.model = fresh
        self._refresh_u_prior()
        await self._thermal_store.async_save()
        return True

    async def _async_load_thermal_model(self) -> None:
        """Load persisted thermal model and initialise from batch if needed."""
        await self._thermal_store.async_load()
        self._thermal_loaded = True

        # If model has no updates yet, seed with batch heat loss coefficient
        model = self._thermal_store.model
        if model._rls.n_updates == 0:
            self._seed_from_batch(model)
        self._refresh_u_prior()

        # Prime the model with current sensor values so the first hourly
        # update (1h from now) can already produce an RLS update instead
        # of only storing prev values.
        if model._prev_timestamp is None:
            t_indoor = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = self._house_heat_w()
            q_solar_wm2 = self._get_current_solar_radiation_wm2()
            if t_indoor is not None and t_outdoor is not None and q_solar_wm2 is not None:
                model.update(t_indoor, t_outdoor, q_hp, q_solar_wm2, dt_util.utcnow())
                _LOGGER.info(
                    "RC model primed with initial values: T_in=%.1f, T_out=%.1f",
                    t_indoor, t_outdoor,
                )
            elif q_solar_wm2 is None:
                _LOGGER.info(
                    "RC model niet geprimed: zonnestraling onbekend. Het model "
                    "voeden met nul zou de zonnewinst in het residu duwen en het "
                    "geleerde warmteverlies wegtrekken."
                )
            else:
                _LOGGER.debug(
                    "RC model: cannot prime at startup (sensors not yet available): "
                    "indoor=%s (%s), outdoor=%s (%s) — will update on next hourly tick",
                    t_indoor, self._indoor_temp_entity,
                    t_outdoor, self._outdoor_entity,
                )

    async def _async_load_highres_log(self) -> None:
        """Laad het meetlogboek. Faalt dit, dan loopt de rest gewoon door."""
        try:
            await self._highres_store.async_load()
        except Exception:
            _LOGGER.warning("Meetlogboek laden mislukt", exc_info=True)
        self._highres_loaded = True
        try:
            self.solar_forecast_log = SolarForecastLog.from_dict(
                await self._solar_forecast_store.async_load()
            )
        except Exception:
            _LOGGER.warning("Logboek zonverwachtingen laden mislukt", exc_info=True)
        # Ook na een mislukte lezing: dan begint het opnieuw. Dit zijn
        # verwachtingen, geen metingen; er gaat niets onvervangbaars verloren.
        self._solar_forecast_loaded = True
        try:
            stored = await self._pv_scale_store.async_load()
            if isinstance(stored, dict) and stored.get("entity") and stored.get("scale"):
                self._pv_scale = {
                    "entity": str(stored["entity"]),
                    "scale": float(stored["scale"]),
                    "at": float(stored.get("at") or 0.0),
                }
        except Exception:
            _LOGGER.warning("Bewaarde PV-schaal laden mislukt", exc_info=True)
        try:
            stored = await self._balance_bias_store.async_load()
            if isinstance(stored, dict):
                bias_k = _finite_or_none(stored.get("bias_k"))
                if bias_k is not None:
                    self.balance_bias = _new_balance_bias(
                        bias_k,
                        _finite_or_none(stored.get("setpoint")),
                        _finite_or_none(stored.get("setpoint_since")),
                    )
                    self._balance_bias_saved = self.balance_bias.bias_k
                    self._balance_bias_saved_setpoint = (
                        self.balance_bias._setpoint, self.balance_bias._setpoint_since
                    )
        except Exception:
            _LOGGER.warning("Bewaarde balanspuntcorrectie laden mislukt", exc_info=True)
        self.solar_lead_factor = self.solar_forecast_log.lead_factor(
            dt_util.utcnow().timestamp()
        )

    # -- zon zoals de ramen hem zien ---------------------------------------

    @property
    def pv_power_entity(self) -> str | None:
        """De zonnesensor (PV-vermogen), of None als die niet is ingesteld."""
        cfg = {**self._entry.data, **self._entry.options}
        return cfg.get(CONF_PV_POWER_ENTITY) or None

    @property
    def window_azimuth_configured(self) -> float | None:
        """De opgegeven richting van de ramen, of None (dan wordt hij geleerd)."""
        cfg = {**self._entry.data, **self._entry.options}
        value = cfg.get(CONF_WINDOW_AZIMUTH)
        try:
            return float(value) % 360.0 if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _pv_power_w(self) -> float | None:
        """PV-vermogen nu, in W. None als onbekend (omvormers slapen 's nachts)
        of als de sensor energie meet in plaats van vermogen."""
        entity = self.pv_power_entity
        if not entity:
            return None
        value = get_float_state(self.hass, entity)
        if value is None:
            return None
        state = self.hass.states.get(entity)
        unit = str((state.attributes.get("unit_of_measurement") if state else "") or "")
        if _is_energy_unit(unit):
            self._warn_pv_energy_unit(entity, unit)
            return None
        return value * _power_unit_factor(unit)

    def _warn_pv_energy_unit(self, entity: str, unit: str) -> None:
        """Eén waarschuwing per sensor: hij meet energie, geen vermogen."""
        if getattr(self, "_pv_unit_warned", None) == entity:
            return
        self._pv_unit_warned = entity
        _LOGGER.warning(
            "Zonnesensor %s meet energie (%s), geen vermogen; kies een sensor in "
            "W of kW. Tot dan rekent de zon met Open-Meteo.", entity, unit,
        )

    async def async_hourly_clearness(
        self, start_ts: float, end_ts: float
    ) -> tuple[dict[float, float], str | None]:
        """Helderheid per uur (gemeten / heldere hemel) en waar hij vandaan komt.

        Uit de zonnesensor als die er is en zijn schaal bekend is; anders uit de
        Open-Meteo-verwachting van dat uur (``solar_forecast_log``, 30 dagen).
        Voor de zonuren per kamersensor (room_temperature.py). Uren met weinig
        heldere-hemelzon tellen niet: daar is "bewolkt" geen uitspraak.
        """
        import numpy as np

        from .analysis.solar_geometry import clearsky_ghi, pv_clear_sky, sun_position

        where = self.solar_orientation
        entity = self.pv_power_entity
        if entity:
            scale = self.saved_pv_scale(entity)
            if scale:
                hourly = await async_pv_hourly(self.hass, entity, start_ts, end_ts)
                if hourly:
                    mids = np.array([h for h, _ in hourly], dtype=float) + 1800.0
                    el, az, e0 = sun_position(mids, where.lat, where.lon)
                    cs = pv_clear_sky(el, az, e0, where.pv_tilt, where.pv_azimuth) * scale
                    out = {float(h): w / float(c) for (h, w), c in zip(hourly, cs) if c > 150.0}
                    if out:
                        return out, "zonnesensor"
        log = self.solar_forecast_log
        hours = [h for h in log.hours() if start_ts <= h <= end_ts]
        if not hours:
            return {}, None
        el, _az, _e0 = sun_position(np.array(hours, dtype=float) + 1800.0, where.lat, where.lon)
        out = {}
        for h, c in zip(hours, clearsky_ghi(el)):
            v = log.at(h, 0)
            if v is None:
                v = log.latest(h)
            if v is not None and c > 100.0:
                out[float(h)] = float(v) / float(c)
        return out, ("open-meteo" if out else None)

    def saved_pv_scale(self, entity: str | None) -> float | None:
        """De bewaarde PV-schaal, mits van deze zonnesensor."""
        saved = getattr(self, "_pv_scale", None)
        if not entity or not saved or saved.get("entity") != entity:
            return None
        return saved.get("scale")

    def remember_pv_scale(self, entity: str, scale: float) -> None:
        """Bewaar een schaal die met de zon boven 15° bepaald is.

        Alleen bij een merkbare verandering wegschrijven: hij wordt elk uur
        opnieuw bepaald en schuift dan hooguit een paar procent.
        """
        now = dt_util.utcnow().timestamp()
        old = self.saved_pv_scale(entity)
        self._pv_scale = {"entity": entity, "scale": round(float(scale), 3), "at": now}
        if old is not None and abs(scale - old) <= 0.01 * old:
            return
        self._pv_scale_store.async_delay_save(lambda: dict(self._pv_scale or {}), 60)

    @property
    def solar_orientation(self) -> Orientation:
        """Waar het huis staat en waar ramen en panelen naar wijzen.

        De panelen worden geleerd. De ramen: de opgegeven richting, anders de
        geleerde; zolang geen van beide er is geldt zuid.
        De helling van de panelen staat vast: uit de data nauwelijks te
        bepalen, en op het gemeten huis maakte 15–35° geen verschil.
        """
        from .analysis.solar_geometry import PV_TILT_ASSUMED

        return Orientation(
            lat=float(self.hass.config.latitude),
            lon=float(self.hass.config.longitude),
            window_azimuth=float(
                self.window_azimuth_configured
                if self.window_azimuth_configured is not None
                else getattr(self, "window_azimuth", None) or DEFAULT_ORIENTATION_AZIMUTH
            ),
            pv_tilt=PV_TILT_ASSUMED,
            pv_azimuth=float(
                getattr(self, "pv_azimuth", None) or DEFAULT_ORIENTATION_AZIMUTH
            ),
        )

    async def async_learn_pv_orientation(self) -> None:
        """Leer de richting van de panelen uit een jaar PV-uurstatistiek.

        Eens per etmaal; de sensoren die de zon gebruiken roepen dit aan vóór ze
        rekenen. Mislukt het, of is er te weinig data, dan blijft de vorige
        uitkomst (of zuid) staan.
        """
        from .analysis.solar_geometry import estimate_pv_azimuth

        entity = self.pv_power_entity
        if not entity:
            return
        now = dt_util.utcnow()
        last = getattr(self, "_pv_orientation_at", None)
        if last is not None and now - last < timedelta(hours=PV_ORIENTATION_REFRESH_HOURS):
            return
        self._pv_orientation_at = now
        try:
            hourly = await async_pv_hourly(
                self.hass, entity,
                now.timestamp() - PV_ORIENTATION_HISTORY_DAYS * 86400, now.timestamp(),
            )
            if not hourly:
                return
            ts = [h[0] for h in hourly]
            pv = [h[1] for h in hourly]
            azimuth, scores = await self.hass.async_add_executor_job(
                estimate_pv_azimuth, ts, pv,
                float(self.hass.config.latitude), float(self.hass.config.longitude),
            )
        except Exception:
            _LOGGER.warning("Richting van de panelen leren mislukt", exc_info=True)
            return
        if azimuth is not None:
            self.pv_azimuth = azimuth
            self.pv_azimuth_scores = scores

    def _record_solar_forecast(self, times: list[str], values: list) -> None:
        """Leg een Open-Meteo-ophaling vast in het logboek van zonverwachtingen.

        De stempel is het eind van het uur (zie ``_solar_radiation_at``); het
        logboek rekent met het begin, in UTC.
        """
        if not self._solar_forecast_loaded:
            return
        tz = dt_util.get_default_time_zone()
        hours: list[tuple[float, float | None]] = []
        for stamp, value in zip(times, values):
            try:
                end = datetime.fromisoformat(stamp).replace(tzinfo=tz)
            except (TypeError, ValueError):
                continue
            hours.append((end.timestamp() - 3600.0, value))
        now = dt_util.utcnow().timestamp()
        if self.solar_forecast_log.record(now, hours):
            self.solar_forecast_log.prune(now)
            self._solar_forecast_store.async_delay_save(
                self.solar_forecast_log.to_dict, 60
            )
            self.solar_lead_factor = self.solar_forecast_log.lead_factor(now)

    async def _async_highres_sample(self, _now=None) -> None:
        """Leg elke vijf minuten een monster vast.

        Dit voedt het model niet — het is grondstof voor latere identificatie
        van de snelle tijdconstante, die op uurdata niet te bepalen is. Zie
        ``highres_log`` voor waarom dat een eigen logboek vraagt.
        """
        if not self._highres_loaded:
            return
        self._update_balance_bias()
        t_indoor = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        q_solar_wm2 = self._get_current_solar_radiation_wm2()
        # Zelfde afweging als bij het model: een verzonnen nul voor de zon
        # maakt het monster onbruikbaar voor identificatie, en dan is
        # overslaan eerlijker dan bewaren.
        if t_indoor is None or t_outdoor is None or q_solar_wm2 is None:
            return
        # Warmtepomp plus ketel, zoals het huismodel (zie _house_heat_w). De
        # kolom heet nog q_hp; vóór v0.10.7-beta.6 was het alleen de warmtepomp.
        q_hp = self._house_heat_w()
        now = dt_util.utcnow()
        extra = self._highres_extra(now)
        room = async_room_temperature(self.hass, self._entry.entry_id)
        if room is not None and room.references:
            extra = {**extra, "t_indoor_raw": room.raw()}
        self._highres_store.record(
            now, t_indoor, t_outdoor, q_hp, q_solar_wm2,
            self._pv_power_w(), extra=extra,
        )

    def _steers_max_water(self) -> bool:
        """Stuurt dit advies nu het waterplafond? Zie BalanceBias."""
        knobs = getattr(getattr(self, "steering", None), "last_knob_states", None)
        return bool(knobs) and knobs[0] == "mpc"

    def _update_balance_bias(self) -> None:
        """Eén tik van de balanspuntcorrectie, en bewaren als hij bewoog."""
        bias = getattr(self, "balance_bias", None)
        if bias is None:
            return
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        demand = self._advice_demand(t_outdoor) if t_outdoor is not None else None
        bias.update(
            dt_util.utcnow().timestamp(),
            self._resolve_room_setpoint()[0],
            room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity),
            demand.stationary_w if demand is not None else 0.0,
            demand.capped if demand is not None else False,
            self._steers_max_water(),
        )
        setpoint_now = (bias._setpoint, bias._setpoint_since)
        if (
            abs(bias.bias_k - self._balance_bias_saved) >= 0.01
            or setpoint_now != getattr(self, "_balance_bias_saved_setpoint", None)
        ):
            self._balance_bias_saved = bias.bias_k
            self._balance_bias_saved_setpoint = setpoint_now
            self._balance_bias_store.async_delay_save(bias.state, 60)

    def _highres_extra(self, now: datetime) -> dict[str, float | None]:
        """Aanvoer, retour, debiet, opgenomen vermogen, setpoint en de knoppen
        uit de laatste meting van het sturingseffect, mits vers.

        Niet opnieuw uitgelezen: het sturingseffect meet dezelfde dingen al elke
        vijf minuten. Ouder dan tien minuten (of nog geen meting) is onbekend.
        """
        from .analysis.steering_effect import KNOB_CODES

        steer = self.steering
        sample = getattr(steer, "last_sample", None)
        at = getattr(steer, "last_sample_at", None)
        if sample is None or at is None or abs((now - at).total_seconds()) > 600:
            return {}
        extra: dict[str, float | None] = {
            "t_supply": sample.supply,
            "t_return": sample.t_return,
            "flow_lph": sample.flow_lph,
            "p_el_w": sample.p_el_w,
            "setpoint": sample.setpoint,
            "ceiling": sample.ceiling,
        }
        knobs = getattr(steer, "last_knob_states", None)
        if knobs:
            max_water, sound, demand = knobs
            extra["max_water"] = KNOB_CODES.get(max_water)
            extra["sound"] = KNOB_CODES.get(sound)
            extra["demand"] = 1.0 if demand else 0.0
        return extra

    def _refresh_u_prior(self) -> None:
        """Keep the RC model's U anchor in step with the batch regression.

        Not a one-off seed: the seasonal fit keeps improving as its window
        grows, and the anchor should follow it. Without this the anchor would
        freeze on whatever the regression happened to say the first time the
        model was loaded.
        """
        if not self._thermal_loaded or not self.coordinator.data:
            return
        heat_loss = self.coordinator.data.heat_loss_hp
        slope = heat_loss.slope
        if slope is not None:
            self._thermal_store.model.set_u_prior(-slope)
            # P_c volgt de warmteverliesanalyse, net als U: P_c = U × (T_kamer −
            # T_balans). Vrij geleerd liep hij weg (1.406 W tegen ~970 W uit het
            # balanspunt) en voorspelde het 1R1C de kamer na 12 uur 0,7 °C te
            # warm. Nagespeeld (23-10 t/m 20-11-2026): met anker C stabiel rond
            # 14.000 Wh/K en testfout 0,409 K, zonder 0,662 K. Zie
            # OnlineRCModel.set_p_c_anchor en CLAUDE.md.
            if heat_loss.balance_point is not None:
                self._thermal_store.model.set_p_c_anchor(
                    -slope * (self._indoor_reference() - heat_loss.balance_point)
                )

    def _indoor_reference(self) -> float:
        """Kamertemperatuur waarop de balans rekent: mediaan van 14 dagen logboek.

        Niet het setpoint van dit moment: een nachtverlaging of een tijdelijk
        hoger setpoint zou het anker voor P_c anders laten verspringen. Zonder
        genoeg monsters het kamerdoel.
        """
        import numpy as np

        if getattr(self, "_highres_loaded", False):
            cutoff = dt_util.utcnow().timestamp() - 14 * 86400
            temps = self._highres_store.log.column(1, since=cutoff)
            if len(temps) >= 288:      # minstens een etmaal aan vijfminutenmonsters
                return float(np.median(temps))
        return self._resolve_room_setpoint()[0]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Nieuwe analyse: anker en startwaarde meteen bijwerken.

        Bij het opstarten laadt het model vaak vóór de analyse, en dan deed
        ``_refresh_u_prior`` niets. Zonder deze haak kwam de startwaarde van
        P_c pas bij de eerste uurtik, tot een uur na elke herstart (gezien bij
        de installatie van v0.10.5-beta.6).
        """
        self._refresh_u_prior()
        super()._handle_coordinator_update()

    def _set_emitter(self, model) -> None:
        self.emitter = model
        self.coordinator.emitter = model
        # De stooklijnpunten (Quatt-advies, OpenQuatt-stooklijn) rekenen bij
        # een coordinator-update, en die komt alleen na een analyse. Zonder
        # dit bleven ze na het leren op de vaste retour staan tot de volgende
        # herstart (gezien bij de installatie van beta.9).
        if getattr(self.coordinator, "data", None) is not None:
            self.coordinator.async_update_listeners()

    #: Eerste keer leren na het opstarten, als er nog geen model bewaard is.
    EMITTER_FIRST_LEARN_DELAY_S = 600

    async def _async_learn_emitter_first(self, _now=None) -> None:
        if self.emitter is not None:
            return
        try:
            await self.async_learn_emitter()
        except Exception:  # noqa: BLE001 — het uurwerk probeert het opnieuw
            _LOGGER.debug("Afgiftemodel na het opstarten overgeslagen", exc_info=True)

    async def _async_load_emitter(self) -> None:
        """Het bewaarde afgiftemodel terugzetten. Faalt dat, dan leert het
        uurwerk een nieuw."""
        from .analysis.emitter import EmitterModel

        try:
            stored = await self._emitter_store.async_load() or {}
        except Exception:
            _LOGGER.warning("Afgiftemodel laden mislukt", exc_info=True)
            return
        model = EmitterModel.from_dict(stored.get("model") or {})
        if model is None:
            return
        try:
            at = dt_util.parse_datetime(str(stored.get("geleerd_op")))
        except ValueError:
            at = None
        if at is None:
            return
        self._set_emitter(model)
        self.emitter_reason = "geleerd"
        self._emitter_at = at

    async def async_learn_emitter(self) -> None:
        """Leer het afgiftemodel uit de uurgemiddelden van de recorder.

        Eens per etmaal. Aanvoer, retour, debiet, warmte, kamer en buiten over
        ``emitter.WINDOW_DAYS`` dagen. Lukt het niet, dan blijft het vorige
        model staan. Alleen een poging die tot een fit kwam telt als "vandaag
        geleerd": ontbreken de rollen of faalt de recorder, dan het volgende
        uur opnieuw. Het model wordt ook op de coordinator gezet, voor de
        stooklijnpunten van het Quatt-advies en de OpenQuatt-stooklijn, en
        bewaard.
        """
        from .analysis.emitter import WINDOW_DAYS, fit_emitter

        now = dt_util.utcnow()
        last = self._emitter_at
        if last is not None and now - last < timedelta(hours=24):
            return
        cfg = {**self._entry.data, **self._entry.options}
        roles = (ROLE_SUPPLY_TEMP, ROLE_RETURN_TEMP, ROLE_FLOW_RATE, ROLE_TOTAL_POWER,
                 ROLE_INDOOR_TEMP, ROLE_OUTDOOR_TEMP)
        from .sources import ROLE_CONF_KEYS

        entities = [
            async_source_entity(self.hass, self._entry.entry_id, r,
                                config=cfg, conf_key=ROLE_CONF_KEYS.get(r))
            for r in roles
        ]
        if not all(entities):
            self.emitter_reason = "aanvoer, retour, debiet of warmte niet ingesteld"
            return
        try:
            series = await async_hourly_means(
                self.hass, entities, now.timestamp() - WINDOW_DAYS * 86400, now.timestamp())
            hours = sorted(set.intersection(*[set(v) for v in series.values()]))
            cols = [[series[e][h] for h in hours] for e in entities]
            cols[3] = [v * _power_unit_factor(self._unit(entities[3])) for v in cols[3]]
            model, reason = await self.hass.async_add_executor_job(fit_emitter, *cols)
        except Exception:
            # Eén waarschuwing per reeks mislukkingen; het volgende uur wordt
            # het opnieuw geprobeerd, en een recorder die blijft falen hoort
            # niet elk uur een traceback in het log te zetten.
            self._emitter_failures += 1
            log = _LOGGER.warning if self._emitter_failures == 1 else _LOGGER.debug
            log("Afgiftemodel leren mislukt (%d× op rij); volgend uur opnieuw",
                self._emitter_failures, exc_info=True)
            return
        self._emitter_failures = 0
        self._emitter_at = now
        self.emitter_reason = reason
        if model is not None:
            self._set_emitter(model)
            self._emitter_store.async_delay_save(
                lambda m=model, t=now: {"model": m.to_dict(),
                                        "geleerd_op": t.isoformat()}, 10)
        elif self.emitter is not None:
            self.emitter_reason = f"{reason}; vorig model blijft staan"

    def _unit(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return str((state.attributes.get("unit_of_measurement") if state else "") or "")

    def _pump_running(self, flow_lph: float | None) -> bool:
        return flow_lph is not None and flow_lph >= MIN_FLOW_LPH

    async def _async_hourly_update(self, _now=None) -> None:
        """Hourly: update thermal model with new measurement, then refresh forecast."""
        try:
            await self.async_learn_emitter()
        except Exception:  # noqa: BLE001 — mag het uurwerk niet tegenhouden
            _LOGGER.debug("Afgiftemodel overgeslagen", exc_info=True)
        # Update thermal model
        if self._thermal_loaded:
            self._refresh_u_prior()
            t_indoor = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = self._house_heat_w()
            q_solar_wm2 = self._get_current_solar_radiation_wm2()

            if q_solar_wm2 is None:
                # Geen straling bekend: niet leren. Een uur overslaan kost bijna
                # niets — het RLS-venster is ~500 uur — terwijl doorrekenen met
                # een verzonnen nul de zonnewinst in het residu duwt en U stil
                # laat wegzakken, met ``converged`` nog gewoon op true.
                _LOGGER.info(
                    "RC model update overgeslagen: zonnestraling onbekend "
                    "(Open-Meteo onbereikbaar of reeks verouderd)."
                )
            elif t_indoor is not None and t_outdoor is not None:
                now = dt_util.utcnow()
                updated = self._thermal_store.model.update(
                    t_indoor, t_outdoor, q_hp, q_solar_wm2, now,
                    q_hp_interval_w=self._interval_heat_w(now),
                )
                if updated and await self._async_reset_broken_model():
                    pass  # opnieuw begonnen en al opgeslagen
                elif updated:
                    await self._thermal_store.async_save()
                    _LOGGER.info(
                        "RC model update #%d: %s",
                        self._thermal_store.model._rls.n_updates,
                        self._thermal_store.model.params,
                    )
                else:
                    _LOGGER.info(
                        "RC model update skipped (n=%d, T_in=%.1f, T_out=%.1f, dt_prev=%s)",
                        self._thermal_store.model._rls.n_updates,
                        t_indoor, t_outdoor,
                        self._thermal_store.model._prev_timestamp,
                    )
            else:
                _LOGGER.warning(
                    "RC model: missing sensor data — indoor=%s (%s), outdoor=%s (%s)",
                    t_indoor, self._indoor_temp_entity,
                    t_outdoor, self._outdoor_entity,
                )

        # Meetlogboek wegschrijven: in geheugen verzameld, één keer per uur
        # naar schijf. Bij elk monster schrijven zou een bestand van ongeveer
        # een megabyte 288 keer per dag herschrijven.
        if self._highres_loaded:
            try:
                await self._highres_store.async_save()
            except Exception:
                _LOGGER.warning("Meetlogboek opslaan mislukt", exc_info=True)

        # Refresh forecasts (previously separate timers, now combined)
        await self._async_refresh_forecast()
        await self._async_refresh_solar_radiation()

    async def _async_refresh_forecast(self, _now=None) -> None:
        """Haal hourly weersverwachting op via HA weather service.

        Een mislukte poging laat de vórige verwachting staan. Leegmaken zou een
        tijdelijke storing verergeren: de forecast-arrays vallen dan terug op de
        huidige buitentemperatuur voor élk uur, en een vlakke reeks is voor de
        herverdeling hetzelfde als geen reeks.
        """
        haalde_op = False
        try:
            result = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self._weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            entity_data = result.get(self._weather_entity, {})
            forecast = entity_data.get("forecast", [])
            if forecast:
                if not self._forecast and self._forecast_warned:
                    _LOGGER.info(
                        "MPC: weersverwachting weer beschikbaar (%d uur)",
                        len(forecast),
                    )
                    self._forecast_warned = False
                self._forecast = forecast
                haalde_op = True
        except Exception:
            _LOGGER.debug("MPC: kon weersverwachting niet ophalen", exc_info=True)

        if not haalde_op and not self._forecast:
            self._async_schedule_forecast_retry()

        self.async_write_ha_state()

    @callback
    def _async_schedule_forecast_retry(self) -> None:
        """Probeer het opstartvenster te overbruggen.

        De eerste poging valt in ``async_added_to_hass``; is de weather-integratie
        dan nog niet geladen, dan mislukt hij stil. Zonder deze herpogingen blijft
        de verwachting tot de volgende uurlijkse tik leeg.
        """
        if self._forecast_retry >= len(FORECAST_RETRY_DELAYS):
            if not self._forecast_warned:
                _LOGGER.warning(
                    "MPC: geen weersverwachting van %s na %d pogingen. De "
                    "forecast valt terug op de huidige buitentemperatuur voor "
                    "elk uur; controleer of die weather-entity bestaat en "
                    "hourly forecasts levert.",
                    self._weather_entity,
                    len(FORECAST_RETRY_DELAYS) + 1,
                )
                self._forecast_warned = True
            return

        delay = FORECAST_RETRY_DELAYS[self._forecast_retry]
        self._forecast_retry += 1
        _LOGGER.debug(
            "MPC: nog geen weersverwachting, nieuwe poging over %d s", delay
        )
        self.async_on_remove(
            async_call_later(self.hass, delay, self._async_retry_forecast)
        )

    async def _async_retry_forecast(self, _now) -> None:
        await self._async_refresh_forecast()

    async def _async_refresh_solar_radiation(self, _=None) -> None:
        """Haal shortwave_radiation forecast op van Open-Meteo (gratis, geen API key).

        Lat/lon én tijdzone komen uit de HA-config — geen handmatige instelling
        nodig, en de reeks komt terug in dezelfde zone als waarin hij hier wordt
        uitgelezen.

        Naast de waarden wordt ``hourly.time`` bewaard. Op uur-index rekenen ging
        mis op de twee dagen per jaar dat een dag 23 of 25 uur telt; op tijdstempel
        zoeken kan dat per constructie niet.
        """
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        tz = quote(self.hass.config.time_zone or "UTC", safe="")
        url = OPEN_METEO_FORECAST_URL.format(lat=lat, lon=lon, tz=tz)
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    hourly = data.get("hourly", {})
                    values = hourly.get("shortwave_radiation") or []
                    times = hourly.get("time") or []
                    if values and len(times) == len(values):
                        self._solar_radiation = values
                        self._solar_times = times
                        self._solar_fetched_at = dt_util.utcnow()
                        self._record_solar_forecast(times, values)
                    else:
                        # Een half antwoord is geen antwoord: liever de vorige
                        # reeks laten staan (die verloopt vanzelf) dan hier een
                        # reeks neerzetten waarvan de uren niet vaststaan.
                        _LOGGER.debug(
                            "Open-Meteo: onbruikbaar antwoord (%d waarden, %d tijden)",
                            len(values), len(times),
                        )
                else:
                    _LOGGER.debug("Open-Meteo response %s", resp.status)
        except Exception:
            _LOGGER.debug("Open-Meteo fetch mislukt", exc_info=True)
        self.async_write_ha_state()

    # ------------------------------------------------------------------ value

    @property
    def native_value(self) -> float | None:
        """Aanbevolen aanvoertemp voor het huidige moment."""
        return self._current_advice()[0]

    def _current_advice(self) -> tuple[float | None, str]:
        """Het advies van nu, met de reden als er geen is.

        Waarde en reden komen uit één berekening, zodat het attribuut nooit iets
        anders kan zeggen dan de state ernaast. Wie het advies volgt, zoals de
        geluidsniveau-compensatie, moet "het huis is warm genoeg" kunnen
        onderscheiden van "er ontbreekt een meting": het eerste betekent dat de
        aanvoer te warm is, het tweede dat er niets bekend is.
        """
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = self._advice_return()
        flow_lph = get_float_state(self.hass, self._flow_entity)

        if t_outdoor is None or t_return is None:
            return None, MPC_ADVICE_NO_INPUT

        effective_flow = _effective_flow(self.coordinator, flow_lph)
        if effective_flow < MIN_FLOW_LPH:
            # effective_flow valt bij een stilstaande pomp al terug op het
            # nominale debiet, dus dit komt in de praktijk niet voor — maar
            # mocht dat ooit veranderen, dan is het geen "warm genoeg".
            return None, MPC_ADVICE_NO_INPUT

        demand = self._advice_demand(t_outdoor)
        if demand is None:
            return None, MPC_ADVICE_NO_INPUT
        if demand.total_w <= 0:
            # Kamer warm genoeg of buiten boven het balanspunt: geen vraag, geen
            # advies. De ondergrens hieronder zou anders 20 °C tonen bij nul vraag.
            return None, MPC_ADVICE_NO_DEMAND
        # Heating branch: floor at MPC_SUPPLY_TEMP_MIN (HP is inefficient
        # below ~20°C aanvoer). COOL_MIN (15°C) is reserved for the future
        # cooling branch where aanvoer < retour.
        return supply_temp_for(
            demand.capped_w, t_return, effective_flow,
            MPC_SUPPLY_TEMP_MIN, MPC_SUPPLY_TEMP_MAX,
            **self._emitter_args(flow_lph),
        ), MPC_ADVICE_OK

    def _predictive_advice(
        self, t_outdoor, t_return, flow_lph, effective_flow, fc_temps, fc_solar
    ) -> dict | None:
        """Schaduwadvies: inhaalterm op de kamer over ``PREDICT_HOURS`` uur.

        De kamer over een paar uur volgt uit het goedgekeurde 2R2C met alleen
        het warmteverlies als vermogen, en de verwachte buitentemperatuur en
        zon. In de naspeler op het testhuis (21 dagen, november 2026): kamerfout
        0,10 K tegen 0,14 K met de inhaalterm op de kamer van nu, bij gelijke
        energie. Vooral bedoeld voor zware vloeren, waar achteraf reageren te
        laat is. Stuurt niets.
        """
        from .analysis.replay import PREDICT_HOURS
        from .analysis.thermal_model_2r2c import TwoStateStepper

        two = self.house_model_2r2c()
        loss = self._heat_loss_line()
        if two is None or loss is None or t_outdoor is None or not fc_temps:
            return None
        u, t_bal = loss
        params, start = two
        stepper = TwoStateStepper(params, start.t_mass)
        t_room = start.t_indoor
        for j in range(min(PREDICT_HOURS, len(fc_temps))):
            t_out = t_outdoor if j == 0 else fc_temps[j]
            q_st = u * max(0.0, t_bal - t_out)
            sol = fc_solar[j] if j < len(fc_solar) else 0.0
            t_room = stepper.predict_t_indoor(t_room, t_out, q_st, sol, 1.0)
        t_set, _ = self._resolve_room_setpoint()
        stationary = u * max(0.0, t_bal - t_outdoor)
        if stationary <= 0:
            return {"kamer_over_uren": PREDICT_HOURS, "kamer_verwacht_c": round(t_room, 2),
                    "vraag_w": 0, "aanvoer_c": None}
        demand = max(0.0, cap_demand(
            stationary + MPC_CATCHUP_FACTOR * u * (t_set - t_room),
            stationary, self._capacity_w(t_outdoor), MPC_CATCHUP_FACTOR * u))
        supply = None
        if t_return is not None or self.emitter is not None:
            try:
                supply = supply_temp_for(
                    demand, t_return, effective_flow, MPC_SUPPLY_TEMP_MIN,
                    MPC_SUPPLY_TEMP_MAX, **self._emitter_args(flow_lph))
            except ValueError:
                supply = None
        return {"kamer_over_uren": PREDICT_HOURS, "kamer_verwacht_c": round(t_room, 2),
                "vraag_w": round(demand), "aanvoer_c": supply}

    def _emitter_args(self, flow_lph: float | None) -> dict:
        """Met een stilstaande pomp is de retour afgekoeld water: dan de
        aanvoer uit het afgiftemodel, met de kamer van nu (of het setpoint)."""
        if self.emitter is None or self._pump_running(flow_lph):
            return {}
        t_room = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
        if t_room is None:
            t_room = self._resolve_room_setpoint()[0]
        return {"emitter": self.emitter, "t_room": t_room}

    def _advice_return(self) -> float | None:
        """De gemeten retour, of die van vóór een ontdooiing; zie ReturnHold."""
        return _advice_return(
            self.hass, self.coordinator, self._entry,
            get_float_state(self.hass, self._return_temp_entity),
        )

    def rc_advice(self) -> dict:
        """Het aanvoeradvies volgens het lerende huismodel (1R1C).

        Voor de stuurbron "1r1c" (zie advice_sources). Zolang het model niet
        online is (``is_converged``), valt het terug op het MPC-advies, en zegt
        ``terugval`` dat. Een knop die op deze bron staat stuurt dan gewoon door,
        en het sturingseffect telt zulke uren apart.

        Geeft ``{"value", "status", "terugval", "demand"}``.
        """
        model = self.thermal_model
        if model is None or not model.is_converged:
            value, status = self._current_advice()
            return {"value": value, "status": status, "terugval": True, "demand": None}

        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = self._advice_return()
        flow_lph = get_float_state(self.hass, self._flow_entity)
        if t_outdoor is None or t_return is None:
            return {"value": None, "status": MPC_ADVICE_NO_INPUT, "terugval": False, "demand": None}
        effective_flow = _effective_flow(self.coordinator, flow_lph)
        if effective_flow < MIN_FLOW_LPH:
            return {"value": None, "status": MPC_ADVICE_NO_INPUT, "terugval": False, "demand": None}

        t_setpoint, _ = self._resolve_room_setpoint()
        demand = rc_advice_demand(
            model,
            t_outdoor,
            room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity),
            t_setpoint,
            self._get_current_solar_radiation_wm2(),
            MPC_CATCHUP_FACTOR,
            self._capacity_w(t_outdoor),
        )
        if demand is None:
            return {"value": None, "status": MPC_ADVICE_NO_INPUT, "terugval": False, "demand": None}
        if demand.total_w <= 0:
            return {"value": None, "status": MPC_ADVICE_NO_DEMAND, "terugval": False, "demand": demand}
        value = supply_temp_for(
            demand.capped_w, t_return, effective_flow,
            MPC_SUPPLY_TEMP_MIN, MPC_SUPPLY_TEMP_MAX,
            **self._emitter_args(flow_lph),
        )
        return {"value": value, "status": MPC_ADVICE_OK, "terugval": False, "demand": demand}

    def _advice_demand(self, t_outdoor: float) -> AdviceDemand | None:
        """Het vermogen waar het aanvoeradvies op rekent, of None zonder huismodel.

        Twee delen, en geen van beide komt uit het online 1R1C:

        * **Stationair**: ``U · max(0, T_balans − T_buiten)`` uit de
          warmteverliesregressie. Gemeten (8–18 nov 2026) klopte die op het
          gemiddelde tot op 1% (3.264 W voorspeld, 3.287 W geleverd), terwijl
          het 1R1C 300–400 W te laag uitkwam: zijn P_c is opgeslagen als P_c/C
          en groeide mee toen C opliep. In de K/u-ruimte waarin het model leert
          is dat 0,04 K/u, onder de uurruis van de kamertemperatuur — maar het
          advies vermenigvuldigt met C.

          Geen aparte zonterm: het balanspunt is op daggemiddelden gefit en
          bevat de gemiddelde zon al. De geleerde zonfactor er nog eens van
          aftrekken maakte de zonuren 300 W slechter. Op een zonnige voorjaarsdag
          is dit nog niet getoetst.
        * **Inhaal**: ``MPC_CATCHUP_FACTOR · U · (setpoint − kamer)``. Zie de
          toelichting bij die constante. Nul zonder kamertemperatuur, en nul
          boven het balanspunt.

        Begrensd op de gemeten capaciteit: een advies voor vermogen dat er niet
        is zegt niets.

        Boven het balanspunt geen inhaalvraag: daar stookt de regelaar niet, en
        een kamer die er onder het setpoint zit is geen stookvraag maar een
        zomerdag of een open raam.

        De rekensom zelf staat in ``advice_demand``, die de vooruitblik per uur
        ook gebruikt.
        """
        loss = self._heat_loss_line()
        if loss is None:
            return None
        u_wk, balance_point = loss
        t_setpoint, _ = self._resolve_room_setpoint()
        return advice_demand(
            u_wk,
            balance_point,
            t_outdoor,
            room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity),
            t_setpoint,
            MPC_CATCHUP_FACTOR,
            self._capacity_w(t_outdoor),
        )

    def _heat_loss_line(self) -> tuple[float, float] | None:
        """``(U, T_balans)`` uit de warmteverliesregressie, of None."""
        data = getattr(self.coordinator, "data", None)
        heat_loss = getattr(data, "heat_loss_hp", None)
        slope = getattr(heat_loss, "slope", None)
        balance_point = getattr(heat_loss, "balance_point", None)
        if not isinstance(slope, (int, float)) or not isinstance(balance_point, (int, float)):
            return None
        u_wk = -float(slope)
        if u_wk <= 0:
            return None
        bias = getattr(self, "balance_bias", None)
        return u_wk, float(balance_point) + (bias.bias_k if bias is not None else 0.0)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Attribuut met de vooruitblik (MPC_FORECAST_HOURS uur) + huidige inputs."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return_measured = get_float_state(self.hass, self._return_temp_entity)
        t_return = self._advice_return()
        flow_lph = get_float_state(self.hass, self._flow_entity)
        effective_flow = _effective_flow(self.coordinator, flow_lph)
        room_setpoint, room_setpoint_source = self._resolve_room_setpoint()

        # Thermal model parameters
        model = self._thermal_store.model
        model_params = model.params
        model_source = "online" if model.is_converged else "batch_fallback"

        # Report the factor that the active model actually applies. The online
        # model uses its learned g_solar; only the batch fallback uses the
        # hardcoded default. Reporting the default in both cases made the shown
        # solar gain 2.3x the value the forecast was computed with — a diagnostic
        # that silently contradicts the thing it is supposed to diagnose.
        solar_factor = SOLAR_RADIATION_DEFAULT_FACTOR
        if model.is_converged:
            raw = model.raw_params
            if raw is not None and raw["g"] > 0:
                solar_factor = raw["g"]

        solar_gain_w = (self._get_current_solar_radiation_wm2() or 0.0) * solar_factor

        # Build forecast arrays (shared with the coast-time sensor).
        fc_temps, fc_solar_wm2, fc_meta = self.build_forecast_arrays(t_outdoor)
        fc_solar_gain_w = [wm2 * solar_factor for wm2 in fc_solar_wm2]

        # Bouw de vooruitblik over MPC_FORECAST_HOURS uur. Elk uur het advies,
        # met dezelfde advice_demand als de state. Uur 0 krijgt de gemeten
        # buitentemperatuur, zodat +0 hetzelfde getal is als de state.
        forecast_out: list[dict] = []
        # "huismodel": de kamer loopt mee via het RC-model (simulate_advice_forward).
        # "zonder_huismodel": geen kamer, inhaalterm alleen in uur 0
        # (advice_forward_without_model). None: geen vooruitblik.
        forecast_basis: str | None = None
        loss = self._heat_loss_line()
        if fc_temps and loss is not None:
            t_indoor = room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity)
            temps = list(fc_temps)
            if t_outdoor is not None:
                temps[0] = t_outdoor
            common = dict(
                t_return=t_return if t_return is not None else 28.0,
                flow_lph=effective_flow,
                forecast_t_outdoor=temps,
                u_wk=loss[0],
                balance_point=loss[1],
                # Expliciet, ook al is het de default van de functie: de hele
                # tabel hangt hieraan, en een stil meegenomen default is
                # precies hoe deze waarde eerder aan het zicht ontsnapte.
                t_setpoint=room_setpoint,
                catchup_factor=MPC_CATCHUP_FACTOR,
                capacity_w=self._capacity_w,
                supply_temp_min=MPC_SUPPLY_TEMP_MIN,
                supply_temp_max=MPC_SUPPLY_TEMP_MAX,
                max_hours=MPC_FORECAST_HOURS,
                emitter=self.emitter,
                pump_running=self._pump_running(flow_lph),
            )
            if model.is_converged and t_indoor is not None:
                sim = simulate_advice_forward(
                    model,
                    t_indoor_now=t_indoor,
                    forecast_q_solar=fc_solar_wm2,
                    comfort_floor=self._comfort_floor,
                    **common,
                )
                forecast_basis = "huismodel"
            else:
                # Tot v0.10.7-beta.4 rekende de terugval met de stooklijn, met
                # zon eraf en zonder inhaalterm of capaciteitsgrens: +0 lag dan
                # naast het advies. Zie advice_forward_without_model.
                sim = advice_forward_without_model(t_indoor, **common)
                forecast_basis = "zonder_huismodel"
            fc_temps = temps
            for i, step in enumerate(sim):
                entry = {**step, **fc_meta[i]} if i < len(fc_meta) else step
                entry["temp_forecast"] = fc_temps[i] if i < len(fc_temps) else None
                entry["solar_gain_w"] = round(fc_solar_gain_w[i]) if i < len(fc_solar_gain_w) else None
                forecast_out.append(entry)

        # Onder het minimum levert de warmtepomp in blokken: aan op het minimum,
        # dan uit. Het aandeel aan per uur is vraag / minimum.
        min_curve = self.min_power_curve
        for entry in forecast_out:
            t_fc = entry.get("temp_forecast")
            q_need = entry.get("q_hp_needed_w")
            p_min = min_curve.at(t_fc) if t_fc is not None else None
            entry["min_power_w"] = round(p_min) if p_min is not None else None
            entry["aan_pct"] = (
                round(min(100.0, 100.0 * max(0.0, q_need) / p_min))
                if p_min and q_need is not None
                else None
            )

        # De kamer volgens het 2R2C bij hetzelfde vermogen per uur. Alleen
        # meerekenen: het vermogen blijft uit de 1R1C-kamer komen. Zie _room_2r2c.
        kamer_2r2c, reden_2r2c = self._room_2r2c(forecast_out, fc_solar_wm2)
        for i, entry in enumerate(forecast_out):
            entry["t_indoor_2r2c"] = (
                round(kamer_2r2c[i], 1) if kamer_2r2c is not None else None
            )
        bench_result = self.benchmark.result if self.benchmark is not None else None

        capacity_w = self._capacity_w(t_outdoor) if t_outdoor is not None else None

        # Het vermogen waar het advies van nu op rekent. Bewust dezelfde
        # berekening als de state, zodat de attributen er niet naast kunnen zitten.
        current_rad_wm2 = self._get_current_solar_radiation_wm2()
        demand = self._advice_demand(t_outdoor) if t_outdoor is not None else None

        return {
            # Waarom de state er wel of niet staat; zie MPC_ADVICE_STATUS_ATTR.
            MPC_ADVICE_STATUS_ATTR: self._current_advice()[1],
            # De naam van deze stuurbron, zoals in de opties (SOURCE_NAMES).
            "bron_naam": SOURCE_NAMES["mpc"],
            "outdoor_temp": t_outdoor,
            "return_temp": t_return_measured,
            # De retour waar het advies mee rekent, en of dat de waarde van
            # vóór een ontdooiing is; zie ReturnHold.
            "retour_advies": t_return,
            "retour_vastgehouden": _return_held(self.coordinator),
            "flow_lph": flow_lph,
            # Het debiet waar het advies mee rekent; zie FlowSmoother.
            "flow_effectief_lph": round(effective_flow),
            # Wat de warmtepomp bij deze buitentemperatuur kan leveren (vriescurve),
            # en of de vraag daarop is afgekapt.
            "capaciteit_w": round(capacity_w) if capacity_w is not None else None,
            "advies_begrensd": bool(demand is not None and demand.capped),
            "solar_gain_w": round(solar_gain_w),
            # Welke factor die winst opleverde — anders is niet te zien of je
            # naar het geleerde of het hardgecodeerde getal kijkt.
            "solar_factor_w_per_wm2": round(solar_factor, 3),
            # Waar het advies uit bestaat: warmteverlies bij deze buitentemperatuur
            # (heat_demand_w) plus het inhalen van een kamertekort (inhaal_w).
            # net_demand_w is de som, vóór de capaciteitsgrens. Zie _advice_demand.
            "advies_basis": "warmteverlies + inhaalterm",
            "heat_demand_w": round(demand.stationary_w) if demand is not None else None,
            "inhaal_w": round(demand.catchup_w) if demand is not None else None,
            "inhaalfactor": MPC_CATCHUP_FACTOR,
            # Zoveel K ligt het balanspunt hoger (of lager) dan de analyse zegt,
            # tegen een blijvend tekort; zie BalanceBias. heat_demand_w rekent
            # er al mee.
            "balanspunt_correctie_k": (
                round(self.balance_bias.bias_k, 2)
                if getattr(self, "balance_bias", None) is not None else 0.0
            ),
            "net_demand_w": round(demand.total_w) if demand is not None else None,
            # None als de reeks ontbreekt of verouderd is — een nul zou hier
            # niet te onderscheiden zijn van een echte nacht.
            "solar_radiation_wm2": (
                round(current_rad_wm2) if current_rad_wm2 is not None else None
            ),
            "solar_forecast_fresh": self._solar_forecast_is_fresh,
            "zon_vooruitblik_factor": self.solar_lead_factor,
            # Schaduw: het advies met de inhaalterm op de kamer over een paar
            # uur volgens het goedgekeurde 2R2C, met het weer dat komt. Stuurt
            # niets; zie _predictive_advice en analysis/replay.py.
            "advies_voorspellend": self._predictive_advice(
                t_outdoor, t_return, flow_lph, effective_flow, fc_temps, fc_solar_wm2),
            # Afgiftemodel: aanvoer uit vermogen en kamer, voor de uren vooruit
            # en bij een stilstaande pomp. Zie analysis/emitter.py.
            "afgiftemodel": self.emitter.to_dict() if self.emitter is not None else None,
            "afgiftemodel_reden": self.emitter_reason,
            "stored_heat_kwh": stored_heat_kwh(
                model.raw_params["C"] if model.raw_params else None,
                room_value(self.hass, self._entry.entry_id, self._indoor_temp_entity),
                self._comfort_floor,
            ),
            "comfort_floor": self._comfort_floor,
            # Waar het advies naartoe rekent, en of dat de thermostaat is of de
            # terugval. Zonder dit tweede veld is een advies dat op een vaste
            # 20 °C draait niet te herkennen.
            "room_setpoint": room_setpoint,
            "room_setpoint_source": room_setpoint_source,
            "model_source": model_source,
            **{f"model_{k}": v for k, v in model_params.items()},
            # Horizon-neutrale naam: heette forecast_6h toen de simulatie op zes
            # uur vastzat. Het aantal uren staat ernaast, zodat een template niet
            # opnieuw op de sleutelnaam hoeft te vertrouwen als hij weer wijzigt.
            "forecast": forecast_out,
            "forecast_uren": len(forecast_out),
            # Waarmee de vooruitblik rekent: "huismodel" (kamer loopt mee) of
            # "zonder_huismodel" (inhaalterm alleen in uur 0, geen kamer). De
            # kaart legt het tweede uit; model_source alleen is niet genoeg,
            # want ook een geconvergeerd model valt terug zonder kamertemperatuur.
            "vooruitblik_basis": forecast_basis,
            # De kolom t_indoor_2r2c: waarom hij leeg is, en hoe goed het model
            # twaalf uur vooruit voorspelt (de vooruitblik is twaalf uur lang).
            "kamer_2r2c": {
                "reden": reden_2r2c,
                "fout_12u_k": (
                    bench_result.rmse_k
                    if bench_result is not None and reden_2r2c is None
                    else None
                ),
            },
            "minimumvermogen": min_curve.as_attribute() if min_curve.bins else None,
        }

    def _start_state_2r2c(self, params_dict: dict):
        """Begintoestand van het 2R2C (kamer en massa) uit het logboek, een
        paar minuten bewaard: dit is het dure deel en wordt vaak gevraagd."""
        from .analysis.shift_room_temp import start_state
        from .analysis.thermal_model_2r2c import TwoStateParams

        params = TwoStateParams.from_dict(params_dict)
        key = tuple(sorted(params_dict.items()))
        now = time.monotonic()
        cached = self._start_2r2c
        if cached is None or cached[1] != key or now - cached[0] > MPC_2R2C_START_TTL_S:
            now_ts = dt_util.utcnow().timestamp()
            start = start_state(
                params, self.highres_rows(now_ts - (TRACK_WINDOW_HOURS + 1) * 3600), now_ts)
            self._start_2r2c = (now, key, start)
        else:
            start = cached[2]
        return params, start

    def house_model_2r2c(self):
        """Het 2R2C als huismodel voor afnemers, of None.

        Alleen als de modelvergelijking het goedkeurde voor gebruik (beide
        toetsen en de afkoeling, zie ``model_benchmark.compare``) én er een
        verse begintoestand is. Geeft ``(TwoStateParams, StartState)``. Zonder
        dat rekenen uitlooptijd en driftgrens met het 1R1C, zoals voorheen.
        """
        result = self.benchmark.result if getattr(self, "benchmark", None) else None
        if not _approved_for_use(result, dt_util.utcnow().timestamp()):
            return None
        try:
            params, start = self._start_state_2r2c(result.params_use)
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
            _LOGGER.debug("Begintoestand 2R2C mislukt", exc_info=True)
            return None
        if start.reason is not None or start.t_mass is None:
            return None
        return params, start

    def _room_2r2c(
        self, forecast: list[dict], solar_wm2: list[float]
    ) -> tuple[list[float] | None, str | None]:
        """Kamertemperatuur aan het eind van elk vooruitblik-uur volgens het 2R2C.

        Het 2R2C voorspelt de kamer beter dan het 1R1C (twaalf uur vooruit 0,27
        K, 46% beter, november 2026), maar wordt hier alleen gebruikt om te
        laten zien wat het vermogen uit de tabel met de kamer doet. Het vermogen
        zelf erop laten rekenen leunt op wat het 2R2C het slechtst kent, de
        stationaire balans; zie ``analysis/shift_room_temp.py``.

        Alleen met een fit die de poort van de modelvergelijking haalde. De
        begintoestand (massatemperatuur uit vier dagen logboek) wordt
        ``MPC_2R2C_START_TTL_S`` bewaard: de attributen worden bij elke
        statuswijziging opgebouwd.
        """
        from .analysis.shift_room_temp import room_after_each_step

        if not forecast:
            return None, "geen vooruitblik"
        result = self.benchmark.result if self.benchmark is not None else None
        if result is None:
            return None, "modelvergelijking nog niet gemeten"
        if not result.accepted or not result.params:
            return None, "2R2C voorspelt nog niet beter dan het huidige model"
        try:
            # Goedgekeurd voor gebruik: de fit op het hele logboek, net als
            # uitlooptijd en driftgrens. Anders de fit die getoetst is.
            params, start = self._start_state_2r2c(
                result.params_use
                if _approved_for_use(result, dt_util.utcnow().timestamp())
                else result.params
            )
            if start.reason is not None:
                return None, start.reason
            n = len(forecast)
            return room_after_each_step(
                params,
                start,
                [float(h["temp_forecast"]) for h in forecast],
                [float(v) for v in solar_wm2[:n]] + [0.0] * (n - len(solar_wm2)),
                [float(h.get("q_hp_needed_w") or 0.0) for h in forecast],
                1.0,
            ), None
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
            # Niet als warning: de attributen worden vaak opgebouwd, en de reden
            # staat al in kamer_2r2c.
            _LOGGER.debug("2R2C-kamer doorrekenen mislukt", exc_info=True)
            return None, "doorrekenen mislukt"


class QuattCoastTimeSensor(CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity):
    """Veilige uitlooptijd: hoeveel minuten het huis met de warmtepomp UIT kan
    uitlopen op zijn thermische massa vóór de binnentemp de comfort-vloer raakt.

    Bedoeld als comfort-grens voor sturing van buitenaf: bij een duur tarief
    mag de WP geknepen worden — maar alleen zolang het huis veilig kan uitlopen.
    De Open-Meteo zon-forecast gaat mee in de simulatie, dus voorspelde zon
    verlengt de coast-tijd (de geleerde g·Q_solar-term remt de afkoeling).

    Hergebruikt het online RC-model én de forecast van de MPC-sensor, zodat er
    geen tweede model getraind of forecast opgehaald hoeft te worden.

    Niet beschikbaar tot het RC-model geconvergeerd is (≈2 dagen data); sturing
    die hierop leunt moet zolang op een eigen terugval draaien.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "coast_time"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-clock-outline"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: QuattMpcSensor,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mpc = mpc_sensor
        self._attr_unique_id = f"{entry.entry_id}_coast_time_min"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Pin een deterministische entity-id, los van de device-naam/area.
        # Anders bouwt HA de id voor deze (nieuwe) entity op uit de area van
        # het device (bijv. "Bijkeuken") → sensor.bijkeuken_quatt_warmteanalyse_…,
        # terwijl het dashboard en externe automations de schone id verwachten.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_veilige_uitlooptijd",
            hass=coordinator.hass,
        )

    @property
    def _comfort_floor(self) -> float:
        cfg = {**self._entry.data, **self._entry.options}
        return cfg.get(CONF_COMFORT_FLOOR_TEMP, DEFAULT_COMFORT_FLOOR_TEMP)

    def _compute(self) -> dict | None:
        """Run the free-cooldown simulation, or None if the model isn't ready.

        Met beide huismodellen als het 2R2C goedgekeurd is voor gebruik, en
        dan de kortste uitkomst: dit getal heet "veilig". Op het gemeten huis
        voorspelde het 2R2C de afkoeling bij een stilstaande pomp ruim een
        kwart te traag (+0,068 K op −0,248 K in vier uur), het 1R1C iets te
        snel (−0,041 K); alleen het 2R2C gaf live 237 minuten tegen 145
        (22-11-2026). Zonder goedgekeurd 2R2C alleen het 1R1C.
        """
        from .analysis.thermal_model_2r2c import TwoStateStepper

        candidates: list[tuple[str, Any]] = []
        one = self._mpc.thermal_model
        if one is not None and one.is_converged:
            candidates.append(("online", one))
        two = self._mpc.house_model_2r2c()
        if two is not None:
            params, start = two
            candidates.append(("2r2c", TwoStateStepper(params, start.t_mass)))
        if not candidates:
            return None

        t_indoor = room_value(self.hass, self._mpc._entry.entry_id, self._mpc._indoor_temp_entity)
        t_outdoor = get_float_state(self.hass, self._mpc._outdoor_entity)
        if t_indoor is None or t_outdoor is None:
            return None

        fc_temps, fc_solar_wm2, _ = self._mpc.build_forecast_arrays(
            t_outdoor, n_hours=COAST_MAX_HOURS
        )
        if not fc_temps:
            # No forecast yet → persist current outdoor reading, no solar.
            fc_temps = [t_outdoor]
            fc_solar_wm2 = [0.0]

        per_model: dict[str, dict] = {}
        for name, candidate in candidates:
            per_model[name] = simulate_coast_time(
                candidate,
                t_indoor_now=t_indoor,
                comfort_floor=self._comfort_floor,
                forecast_t_outdoor=fc_temps,
                forecast_q_solar=fc_solar_wm2,
                step_minutes=COAST_STEP_MINUTES,
                max_hours=COAST_MAX_HOURS,
            )
        usable = [(n, m) for n, m in candidates
                  if per_model[n]["coast_minutes"] is not None]
        if not usable:
            self._model_source = candidates[0][0]
            result = per_model[candidates[0][0]]
            model = candidates[0][1]
        else:
            self._model_source, model = min(
                usable, key=lambda nm: per_model[nm[0]]["coast_minutes"])
            result = per_model[self._model_source]
        result = dict(result)
        result["per_model"] = {
            n: r["coast_minutes"] for n, r in per_model.items()
        }

        # Het vervolg: vanaf de vloer terug naar het setpoint met de warmtepomp
        # op zijn minimum. Alleen als de vloer gehaald wordt en het minimum
        # geleerd is; anders valt er niets terug te betalen of niets te rekenen.
        result["recovery_on_min_minutes"] = None
        result["recovery_target"] = None
        curve = self._mpc.min_power_curve
        if result["reaches_floor"] and curve.bins:
            target, _ = self._mpc._resolve_room_setpoint()
            result["recovery_target"] = target
            result["recovery_on_min_minutes"] = simulate_recovery_on_min(
                model,
                t_start=self._comfort_floor,
                t_target=target,
                forecast_t_outdoor=fc_temps,
                forecast_q_solar=fc_solar_wm2,
                min_power_at=curve.at,
                start_minute=result["coast_minutes"] or 0,
                step_minutes=COAST_STEP_MINUTES,
                max_hours=COAST_MAX_HOURS,
            )
        return result

    @property
    def native_value(self) -> int | None:
        result = self._compute()
        return result["coast_minutes"] if result else None

    @property
    def extra_state_attributes(self) -> dict | None:
        result = self._compute()
        if result is None:
            return {
                "comfort_floor": self._comfort_floor,
                "model_source": "unavailable",
            }
        return {
            "comfort_floor": self._comfort_floor,
            "comfort_at_risk": result["comfort_at_risk"],
            "reaches_floor": result["reaches_floor"],
            # Het model met de kortste uitlooptijd: "2r2c" of "online" (1R1C).
            "model_source": getattr(self, "_model_source", "online"),
            # Beide uitkomsten in minuten, als het 2R2C meedoet.
            "per_model": result.get("per_model"),
            "trajectory": result["trajectory"],
            # None = niet binnen het venster op het minimum, of geen minimum
            # geleerd; recovery_target zegt welke van de twee (None = geen).
            "recovery_on_min_minutes": result["recovery_on_min_minutes"],
            "recovery_target": result["recovery_target"],
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id,
                    (ROLE_OUTDOOR_TEMP, ROLE_INDOOR_TEMP),
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()


class QuattStoredHeatSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Warmte die nu in de bouwmassa zit, gerekend boven de comfortgrens.

    E = C × (T_binnen − T_comfortgrens), met C de geleerde warmtecapaciteit van
    het huis. Dit is de buffer waarop je kunt teren met de warmtepomp uit: wat
    het huis moet verliezen voordat het van de huidige temperatuur naar de
    comfortgrens zakt. Het is nadrukkelijk geen warmte die je eruit kunt halen
    en elders in kunt stoppen.

    Zelfde grens als de uitlooptijd-sensor, want het is dezelfde vraag in een
    andere eenheid: die zegt hoe lang, deze hoeveel. Staat het huis onder de
    grens, dan is het getal negatief — dat blijft staan, want een tekort is
    informatie.

    Niet beschikbaar tot het RC-model geconvergeerd is (≈2 dagen data): zonder
    betrouwbare C is elk getal hier verzonnen.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "stored_heat"
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-thermometer"
    # Bewust geen device_class ENERGY: dat hoort bij een meterstand die oploopt
    # en zou deze sensor het energiedashboard in trekken. Dit is een voorraad op
    # dit moment, geen verbruik.

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: QuattMpcSensor,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mpc = mpc_sensor
        self._attr_unique_id = f"{entry.entry_id}_stored_heat_kwh"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Deterministische entity-id, net als bij de uitlooptijd-sensor: anders
        # bouwt HA hem op uit de area van het device en verwijst het dashboard
        # naar een id die niet bestaat.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_opgeslagen_warmte",
            hass=coordinator.hass,
        )

    @property
    def _comfort_floor(self) -> float:
        return self._mpc._comfort_floor

    @property
    def _capacity_whk(self) -> float | None:
        model = self._mpc.thermal_model
        if model is None or not model.is_converged:
            return None
        raw = model.raw_params
        return raw["C"] if raw else None

    @property
    def _t_indoor(self) -> float | None:
        return room_value(self.hass, self._mpc._entry.entry_id, self._mpc._indoor_temp_entity)

    @property
    def native_value(self) -> float | None:
        return stored_heat_kwh(
            self._capacity_whk, self._t_indoor, self._comfort_floor
        )

    @property
    def extra_state_attributes(self) -> dict:
        capacity = self._capacity_whk
        t_indoor = self._t_indoor
        return {
            "comfort_floor": self._comfort_floor,
            "kamertemperatuur": t_indoor,
            # De marge in graden staat er los bij: dat getal is direct te
            # controleren aan de thermostaat, de kWh niet.
            "marge_k": (
                round(t_indoor - self._comfort_floor, 1)
                if t_indoor is not None
                else None
            ),
            "capaciteit_wh_k": round(capacity) if capacity else None,
            "model_source": "online" if capacity else "unavailable",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id, (ROLE_INDOOR_TEMP,)
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()


class QuattRcAdviceSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Aanvoeradvies volgens het lerende huismodel (1R1C): de stuurbron "1r1c".

    Zelfde opbouw als het MPC-advies — basis plus inhaalterm, begrensd op de
    capaciteit, omgerekend met retour en debiet — maar de basis komt uit het
    1R1C in plaats van uit de rechte lijn door de stookdagen. Zie
    ``rc_advice_demand`` voor de rekensom en waarom deze bron bestaat.

    Zolang het model niet online is, geeft hij het MPC-advies door, met
    ``terugval: true``. Zo valt een knop op deze bron nooit stil.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "rc_recommended_supply_temp"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-thermometer-outline"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: QuattMpcSensor,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mpc = mpc_sensor
        self._attr_unique_id = f"{entry.entry_id}_rc_recommended_supply_temp"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_1r1c_aanbevolen_aanvoertemperatuur",
            hass=coordinator.hass,
        )

    @property
    def native_value(self) -> float | None:
        return self._mpc.rc_advice()["value"]

    @property
    def extra_state_attributes(self) -> dict:
        advice = self._mpc.rc_advice()
        demand = advice["demand"]
        return {
            MPC_ADVICE_STATUS_ATTR: advice["status"],
            # True: het huismodel is nog niet online en dit is het advies van
            # "Buitentemperatuur + kamer".
            "terugval": advice["terugval"],
            "bron_naam": (
                f"{SOURCE_NAMES['1r1c']}, leert nog" if advice["terugval"]
                else SOURCE_NAMES["1r1c"]
            ),
            "stationair_w": round(demand.stationary_w) if demand else None,
            "inhaal_w": round(demand.catchup_w) if demand else None,
            "vraag_w": round(demand.capped_w) if demand else None,
            "begrensd": bool(demand.capped) if demand else False,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id,
                    (ROLE_OUTDOOR_TEMP, ROLE_RETURN_TEMP, ROLE_FLOW_RATE,
                     ROLE_INDOOR_TEMP, ROLE_ROOM_SETPOINT),
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()


#: Hoe lang een goedkeuring voor gebruik geldt zonder nieuwe meting. Gelijk
#: aan wat een bewaarde uitkomst na een herstart mag zijn: mislukken de
#: nachtelijke metingen daarna, dan valt alles terug op het 1R1C.
APPROVAL_MAX_AGE = timedelta(hours=48)


def _approved_for_use(result, now_ts: float) -> bool:
    """Is het 2R2C goedgekeurd voor gebruik, met een meting die niet te oud is?"""
    if (
        result is None
        or not getattr(result, "approved", False)
        or not getattr(result, "params_use", None)
    ):
        return False
    measured = getattr(result, "measured_ts", None)
    return (
        isinstance(measured, (int, float))
        and now_ts - measured <= APPROVAL_MAX_AGE.total_seconds()
    )


class QuattModelBenchmarkSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Scoort het tweetoestandsmodel tegen het model dat nu draait.

    Het 2R2C-model staat uit tot het op weggehouden data aantoonbaar beter
    voorspelt dan het 1R1C dat er nu is. Die poort zat tot nu toe onzichtbaar
    in de code: je kon niet zien of het model nog niet aan de beurt was, was
    afgewezen, of nooit had gedraaid. Deze sensor maakt hem zichtbaar.

    Hij verandert niets aan de regeling. Er wordt gefit op een kopie van het
    meetlogboek en gescoord op de helft die niet in de fit zat; het draaiende
    model wordt alleen uitgelezen.

    Draait 's nachts, want de fit plus de vrijloop-evaluatie kosten seconden
    en horen niet in de event loop.

    Sinds v0.10.7-beta.9 wordt de uitkomst bewaard (hoogstens
    ``KEEP_RESULT`` oud bij het laden): uitlooptijd en driftgrens rekenen op
    het goedgekeurde 2R2C, en zouden anders na elke herstart tien minuten
    terugvallen op het 1R1C.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "model_benchmark"
    _attr_icon = "mdi:scale-balance"

    #: Eén keer per etmaal. Vaker heeft geen zin: het logboek groeit met een
    #: dag per dag, en de uitkomst verschuift navenant traag.
    INTERVAL = timedelta(hours=24)

    #: Wachttijd na het opstarten. Niet meteen: bij een herstart heeft HA het
    #: eerst druk genoeg, en op een Pi met een SD-kaart is dit precies het
    #: soort werk dat je daar niet doorheen wil duwen.
    STARTUP_DELAY = timedelta(minutes=10)

    #: Hoe oud een bewaarde uitkomst mag zijn om na een herstart te gelden.
    #: Tijdens bedrijf geldt dezelfde grens voor de goedkeuring
    #: (``_approved_for_use``).
    KEEP_RESULT = APPROVAL_MAX_AGE

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: QuattMpcSensor,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mpc = mpc_sensor
        self._result: Comparison | None = None
        # De naspeler (analysis/replay.py), alleen met een goedgekeurd 2R2C.
        self._replay: dict | None = None
        self._store = Store(
            coordinator.hass, 1, f"{DOMAIN}.model_benchmark_{entry.entry_id}"
        )
        # Het 2R2C met de zon op de ramen (zonnesensor), naast de hoofduitkomst.
        # None zonder zonnesensor of zolang er niet gemeten is.
        self._window: dict | None = None
        self._running = False
        self._attr_unique_id = f"{entry.entry_id}_model_benchmark"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vastgepind, net als bij de andere sensoren hier: zonder dit leidt HA
        # de id af uit het gebied van het device en wijst het dashboard naar
        # een entiteit die niet bestaat.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_modelvergelijking",
            hass=coordinator.hass,
        )

    @property
    def result(self) -> Comparison | None:
        """De laatste uitkomst, of ``None`` zolang er niet gemeten is."""
        return self._result

    @property
    def native_value(self) -> str:
        if self._result is None:
            return "nog niet gemeten"
        if self._result.accepted:
            return "beter"
        reden = self._result.reason
        if reden.startswith(("te weinig", "nog geen", "geen bruikbare")):
            return "te weinig data"
        if reden.startswith("fit "):
            return "fit mislukt"
        return "niet beter"

    @property
    def extra_state_attributes(self) -> dict:
        if self._result is None:
            return {"gemeten": False}
        attrs = {"gemeten": True, **self._result.to_dict()}
        if self._result.approved and not _approved_for_use(
            self._result, dt_util.utcnow().timestamp()
        ):
            # Zelfde grens als de afnemers; anders zegt de kaart "In gebruik"
            # terwijl alles al op het 1R1C rekent.
            attrs["goedgekeurd_voor_gebruik"] = False
            attrs["goedkeuring"] = (
                "verlopen: geen nieuwe meting in "
                f"{int(APPROVAL_MAX_AGE.total_seconds() // 3600)} uur")
        # Gevelzon (ZONNEWARMTE_PLAN.md). De zonfactoren ook los, zodat een
        # grafiek per meting kan laten zien of ze stabiliseren.
        window = getattr(self, "_window", None)
        attrs["zonfactor_horizontaal"] = (self._result.params or {}).get("g")
        attrs["gevelzon"] = window
        attrs["zonfactor_gevel"] = ((window or {}).get("params") or {}).get("g")
        attrs["gevelzon_rmse_k"] = (window or {}).get("rmse_k")
        attrs["richting_ramen"] = (window or {}).get("richting_ramen")
        attrs["richting_ramen_bron"] = (window or {}).get("richting_ramen_bron")
        attrs["richting_panelen"] = (window or {}).get("richting_panelen")
        attrs["naspeler"] = getattr(self, "_replay", None)
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 — een kapotte opslag is geen ramp
            _LOGGER.warning("Bewaarde modelvergelijking onleesbaar", exc_info=True)
            stored = None
        self._restore(stored, dt_util.utcnow().timestamp())
        self._schedule_expiry()
        self.async_on_remove(self._cancel_expiry)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_run, self.INTERVAL
            )
        )
        self.async_on_remove(
            async_call_later(
                self.hass, self.STARTUP_DELAY.total_seconds(), self._async_run
            )
        )

    def _restore(self, stored, now_ts: float) -> None:
        """Een bewaarde uitkomst terugzetten, als hij niet te oud is."""
        restored = Comparison.from_storage(stored) if isinstance(stored, dict) else None
        if (
            restored is not None
            and restored.measured_ts is not None
            and now_ts - restored.measured_ts <= self.KEEP_RESULT.total_seconds()
        ):
            self._result = restored
            naspeler = stored.get("naspeler")
            self._replay = naspeler if isinstance(naspeler, dict) else None

    def _storage(self) -> dict:
        return {**self._result.to_storage(), "naspeler": self._replay}

    _expiry_unsub = None

    def _cancel_expiry(self) -> None:
        if self._expiry_unsub is not None:
            self._expiry_unsub()
            self._expiry_unsub = None

    def _schedule_expiry(self) -> None:
        """De state opnieuw schrijven zodra de goedkeuring verloopt. Zonder
        nieuwe meting schrijft deze sensor anders niets, en blijft de kaart
        "In gebruik" zeggen."""
        self._cancel_expiry()
        result = self._result
        if result is None or not _approved_for_use(result, dt_util.utcnow().timestamp()):
            return
        delay = (result.measured_ts + APPROVAL_MAX_AGE.total_seconds()
                 - dt_util.utcnow().timestamp() + 1.0)

        @callback
        def _expired(_now) -> None:
            self._expiry_unsub = None
            self.async_write_ha_state()

        self._expiry_unsub = async_call_later(self.hass, max(1.0, delay), _expired)

    async def _async_run(self, _now=None) -> None:
        """Meet opnieuw. Een mislukte meting laat de vorige uitkomst staan."""
        if self._running:
            # De vorige ronde loopt nog. Dat kan op trage hardware met een vol
            # logboek; er dan een tweede executor-thread naast zetten maakt het
            # alleen erger.
            return
        rows = self._mpc.highres_samples
        if not rows:
            return
        model = self._mpc.thermal_model
        heat_loss = getattr(
            getattr(getattr(self, "coordinator", None), "data", None), "heat_loss_hp", None
        )
        slope = getattr(heat_loss, "slope", None)
        u = -slope if isinstance(slope, (int, float)) else None
        bp = getattr(heat_loss, "balance_point", None)
        now_ts = dt_util.utcnow().timestamp()
        self._running = True
        try:
            result = await self.hass.async_add_executor_job(
                _compare_anchored, rows, model, u, bp, now_ts
            )
            replay_out = None
            if result.approved and result.params_use and u is not None and bp is not None:
                replay_out = await self.hass.async_add_executor_job(
                    _run_replay, result.params_use, rows, u, bp
                )
            window = await self._async_window_variant(rows, u, bp)
        except Exception:
            _LOGGER.warning("Modelvergelijking mislukt", exc_info=True)
            return
        finally:
            self._running = False
        self._result = result
        self._replay = replay_out
        self._window = window
        self._schedule_expiry()
        self._store.async_delay_save(self._storage, 30)
        _LOGGER.info(
            "Modelvergelijking: 2R2C %s K, huidig %s K, niets doen %s K — %s; "
            "gevelzon %s K",
            result.rmse_k, result.reference_rmse_k, result.persistence_rmse_k,
            result.reason, (window or {}).get("rmse_k"),
        )
        self.async_write_ha_state()

    async def _async_window_variant(
        self, rows: list[list[float]], u: float | None = None, bp: float | None = None,
    ) -> dict | None:
        """Het 2R2C met de zon op de ramen, gemeten met de zonnesensor.

        Met dezelfde ankers als de hoofdvergelijking (U en de totale gratis
        warmte, de gemiddelde zon uit de eigen zonkolom), anders zegt het
        verschil tussen beide ook iets over de ankers en niet alleen over de zon.

        PV staat pas sinds v0.10.6-beta.12 in het meetlogboek. Voor de oudere
        monsters wordt hij aangevuld uit de uurstatistiek van de recorder,
        zodat er meteen een uitkomst is in plaats van over vier weken. Een fout
        hier mag de hoofduitkomst niet meenemen.
        """
        from .analysis.model_benchmark import compare_variant
        from .analysis.solar_input import window_rows

        entity = self._mpc.pv_power_entity
        if not entity:
            return None
        try:
            await self._mpc.async_learn_pv_orientation()
            hourly = await async_pv_hourly(
                self.hass, entity, min(r[0] for r in rows), max(r[0] for r in rows)
            )
            where = self._mpc.solar_orientation
            configured = self._mpc.window_azimuth_configured
            candidates = (
                (configured,) if configured is not None else WINDOW_AZIMUTH_CANDIDATES
            )
            result = await self.hass.async_add_executor_job(
                _window_variant_with_learned_direction, rows, where, hourly, u, bp,
                self._mpc.saved_pv_scale(entity), candidates,
            )
        except Exception:
            _LOGGER.warning("Modelvergelijking met gevelzon mislukt", exc_info=True)
            return {"reden": "berekening mislukt"}
        if result.get("richting_ramen") is not None:
            result["richting_ramen_bron"] = "ingesteld" if configured is not None else "geleerd"
            if configured is None:
                self._mpc.window_azimuth = result["richting_ramen"]
        result["richting_panelen"] = self._mpc.pv_azimuth
        return result



def _run_replay(params_dict, rows, u, balance_point) -> dict | None:
    """De naspeler op het goedgekeurde 2R2C; executor. Een fout kost alleen
    de naspeler, niet de vergelijking."""
    from .analysis.replay import replay
    from .analysis.thermal_model_2r2c import TwoStateParams

    try:
        return replay(TwoStateParams.from_dict(params_dict), rows, u, balance_point)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Naspeler mislukt", exc_info=True)
        return None


def _compare_anchored(rows, model, u, balance_point, now_ts):
    """De modelvergelijking met ankers uit de warmteverliesanalyse; executor."""
    from .analysis.model_benchmark import anchors_from_rows

    return compare(rows, model, anchors=anchors_from_rows(rows, u, balance_point),
                   now_ts=now_ts)


def _window_variant_with_learned_direction(
    rows, where, hourly, u=None, balance_point=None, saved_scale=None,
    candidates=WINDOW_AZIMUTH_CANDIDATES,
) -> dict:
    """De gevelzonvariant voor elke kandidaat-raamrichting; de beste wint.

    Met een opgegeven richting is er één kandidaat. Dat is ook beter: op het
    gemeten huis lagen 135–270° binnen 0,01 K van elkaar en won op 20-11 225°,
    op 27-11 135°, terwijl de ramen op ~180° staan.

    Gekozen op het gemiddelde van beide week-splitsingen: met één splitsing
    sprong het beste punt op het gemeten huis tussen 135° en 250°. De getoonde
    uitkomst is die van de gewone splitsing, net als de hoofdvergelijking.
    Draait in de executor.
    """
    from dataclasses import replace

    from .analysis import model_benchmark as mb
    from .analysis.solar_input import window_rows

    per_richting: dict[int, float] = {}
    gewoon: dict[int, dict] = {}
    info: dict = {}
    for cand in candidates:
        variant, info = window_rows(
            rows, replace(where, window_azimuth=float(cand)), hourly, saved_scale)
        if variant is None:
            return {**info, "reden": "te weinig PV-data in het meetlogboek"}
        # Per richting eigen ankers: de gemiddelde zon hangt aan de zonkolom.
        anchors = mb.anchors_from_rows(variant, u, balance_point)
        a = mb.compare_variant(variant, anchors=anchors)
        b = mb.compare_variant(variant, reverse=True, anchors=anchors)
        if a.get("rmse_k") is None or b.get("rmse_k") is None:
            return {**info, **a}
        cand = int(round(cand))
        per_richting[cand] = round((a["rmse_k"] + b["rmse_k"]) / 2, 3)
        gewoon[cand] = a
    best = min(per_richting, key=per_richting.get)
    return {
        **info,
        **gewoon[best],
        "geankerd": anchors is not None,
        "richting_ramen": float(best),
        "rmse_per_richting": {str(k): v for k, v in per_richting.items()},
    }


class QuattSolarForecastSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Hoe goed is de zonverwachting, per vooruitkijktijd?

    Bij elke Open-Meteo-ophaling legt de MPC-sensor vast wat er voor de
    komende twaalf uur verwacht werd (``SolarForecastLog``). Deze sensor zet
    dat elk uur naast wat er kwam: gemeten met de zonnesensor als die is
    ingesteld, anders de verwachting van vlak ervóór.

    De stand is de gemiddelde fout op drie uur vooruit, de horizon waarop een
    aansturing met de zon rekent. Die gaat de langetermijnstatistiek in, zodat
    te zien is of het getal stabiliseert. Zie ZONNEWARMTE_PLAN.md.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "solar_forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_native_unit_of_measurement = "W/m²"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    # Het uuroverzicht van vandaag is voor de grafiek, niet voor de recorder.
    _unrecorded_attributes = frozenset({"vandaag"})

    INTERVAL = timedelta(hours=1)
    STARTUP_DELAY = timedelta(minutes=3)
    #: De vooruitkijktijd waarvan de fout de stand is.
    STATE_LEAD = "3"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: QuattMpcSensor,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._mpc = mpc_sensor
        self._score: dict | None = None
        self._running = False
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_zonverwachting",
            hass=coordinator.hass,
        )

    @property
    def native_value(self) -> float | None:
        if not self._score:
            return None
        return self._score["per_vooruitkijktijd"].get(self.STATE_LEAD, {}).get("mae")

    @property
    def extra_state_attributes(self) -> dict:
        if not self._score:
            return {"gemeten": False}
        return {"gemeten": True, **self._score}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_run, self.INTERVAL)
        )
        self.async_on_remove(
            async_call_later(
                self.hass, self.STARTUP_DELAY.total_seconds(), self._async_run
            )
        )

    async def _async_run(self, _now=None) -> None:
        if self._running:
            return
        from .analysis.solar_forecast_log import KEEP_HOURS, SolarForecastLog

        now = dt_util.utcnow().timestamp()
        log_dict = self._mpc.solar_forecast_log.to_dict()
        rows = self._mpc.highres_rows(now - KEEP_HOURS * 3600)
        entity = self._mpc.pv_power_entity
        self._running = True
        await self._mpc.async_learn_pv_orientation()
        where = self._mpc.solar_orientation
        tz = dt_util.get_default_time_zone()
        try:
            hourly = (
                await async_pv_hourly(self.hass, entity, now - KEEP_HOURS * 3600, now)
                if entity else []
            )
            self._score = await self.hass.async_add_executor_job(
                _score_solar_forecast,
                SolarForecastLog.from_dict(log_dict), rows, entity, where, hourly, now, tz,
                self._mpc.saved_pv_scale(entity),
            )
        except Exception:
            _LOGGER.warning("Zonverwachting scoren mislukt", exc_info=True)
            return
        finally:
            self._running = False
        if (entity and self._score.get("pv_schaal_bron") == SCALE_MEASURED
                and self._score.get("pv_schaal") is not None):
            self._mpc.remember_pv_scale(entity, self._score["pv_schaal"])
        state = self.hass.states.get(entity) if entity else None
        unit = str((state.attributes.get("unit_of_measurement") if state else "") or "")
        if _is_energy_unit(unit):
            self._score["reden"] = (
                f"zonnesensor meet energie ({unit}), geen vermogen: kies een sensor in W of kW"
            )
        self.async_write_ha_state()


def _score_solar_forecast(log, rows, entity, where, hourly, now, tz, saved_scale=None) -> dict:
    """Rekenwerk van de zonverwachtingssensor; draait in de executor."""
    import numpy as np

    from .analysis.solar_geometry import clearsky_ghi, sun_position
    from .analysis.solar_input import hourly_measured, measured_ghi

    measured = None
    scale = None
    source = None
    reden = None
    if entity:
        ts, ghi, scale, source = measured_ghi(rows, where, hourly, saved_scale)
        if scale is None:
            reden = "zonnesensor: nog te weinig zon gezien om de installatie te ijken"
        else:
            measured = hourly_measured(ts, ghi, where)
    hours = sorted(set(log.hours()) | set(measured or {}))
    clear = {}
    if hours:
        mids = np.array(hours, dtype=float) + 1800.0
        el, _az, _e0 = sun_position(mids, where.lat, where.lon)
        clear = {h: float(c) for h, c in zip(hours, clearsky_ghi(el))}
    score = log.score(now, measured or None, clear)
    # De factor die de vooruitblik gebruikt; ``correctiefactor`` (gemeten /
    # verwacht) is alleen ter informatie, zie SolarForecastLog.lead_factor.
    score["correctiefactor_vooruitblik"] = log.lead_factor(now)

    # Vandaag, voor de grafiek: wat er een uur vooruit verwacht werd, wat de
    # laatste verwachting was, en wat er gemeten is.
    today = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = int(today.timestamp())
    vandaag = []
    for h in range(first, first + 36 * 3600, 3600):
        latest = log.latest(h)
        m = (measured or {}).get(h)
        if latest is None and m is None:
            continue
        vandaag.append({
            "tijd": datetime.fromtimestamp(h, tz).isoformat(),
            "verwacht_1u": log.at(h, 1),
            "verwacht_laatste": latest,
            "gemeten": round(m[0], 1) if m else None,
        })
    return {
        **score,
        "zonnesensor": entity,
        "pv_schaal": round(scale, 2) if scale is not None else None,
        "pv_schaal_bron": source,
        "richting_panelen": where.pv_azimuth if entity else None,
        "reden": reden,
        "vandaag": vandaag,
    }


class QuattAdviceErrorSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Fout sensor: advies − werkelijke aanvoertemperatuur.

    Positief = advies te hoog, negatief = advies te laag t.o.v. werkelijk.
    Alleen beschikbaar als beide bronnen een geldige waarde hebben.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-check"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mode: str,
        advised_entity: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._advised_entity = advised_entity
        self._attr_unique_id = f"{entry.entry_id}_{mode}_advice_error"
        self._attr_translation_key = (
            "mpc_supply_temp_error"
            if mode == "mpc"
            else "stooklijn_supply_temp_error"
        )
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"quatt_warmteanalyse_{mode}_fout_aanvoertemperatuur",
            hass=coordinator.hass,
        )

    @property
    def _supply_temp_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_SUPPLY_TEMP,
            config=cfg, conf_key=CONF_SUPPLY_TEMP_ENTITY,
        )

    @property
    def _flow_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_FLOW_RATE,
            config=cfg, conf_key=CONF_FLOW_ENTITY,
        )

    @property
    def _heat_output_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_TOTAL_POWER,
            config=cfg, conf_key=CONF_POWER_ENTITY,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Volg álle kandidaten, niet alleen de nu actieve: anders komt een
        # bronwissel niet binnen (zie de spiegelsensoren, zelfde reden).
        registry = self.hass.data.get(DOMAIN, {}).get(
            f"{self._entry.entry_id}_sources"
        )
        tracked = {self._advised_entity}
        for role in (ROLE_SUPPLY_TEMP, ROLE_FLOW_RATE, ROLE_TOTAL_POWER):
            source = registry.get(role) if registry else None
            tracked.update(source.candidates if source else ())
        tracked.discard(None)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, sorted(tracked), self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        # Fout is alleen zinvol als de HP draait
        flow = get_float_state(self.hass, self._flow_entity)
        if flow is None or flow < MIN_FLOW_LPH:
            return None
        # ... én als er warmte het huis in gaat. Debiet alleen is niet genoeg:
        # buiten het stookseizoen circuleert de pomp met 0 W productie, terwijl
        # de adviessensor op een zomernacht onder het balanspunt wél een getal
        # geeft. Het verschil daartussen is geen voorspelfout maar ruis, en het
        # trok de maandgemiddelden richting −14 °C.
        heat_out = get_float_state(self.hass, self._heat_output_entity)
        if heat_out is None or heat_out < MIN_HEAT_OUTPUT_W:
            return None
        advised = get_float_state(self.hass, self._advised_entity)
        actual = get_float_state(self.hass, self._supply_temp_entity)
        if advised is None or actual is None:
            return None
        return round(advised - (actual - (self._supply_correction() or 0.0)), 1)

    def _supply_correction(self) -> float | None:
        """Wat de aanvoersensorcontrole als afwijking meldt (supply_check.py).

        Alleen hier, in een sensor die niets stuurt: anders toont de afwijking
        de meetfout van een niet-gekalibreerde sensor als fout in het advies.
        """
        check = self.hass.data.get(DOMAIN, {}).get(f"{self._entry.entry_id}_supply_check")
        if check is None:
            return None
        offset = check.correction_for(self._supply_temp_entity)
        return round(offset, 2) if isinstance(offset, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "aanvoersensor": self._supply_temp_entity,
            # Afgetrokken van de gemeten aanvoer; None = geen afwijking gemeld.
            "aanvoer_correctie_k": self._supply_correction(),
        }


class QuattCopPerformanceSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Rendement afgezet tegen de eigen norm bij dezelfde buitentemperatuur.

    De kale dag-COP volgt vooral het weer — op deze installatie 1,85 bij −5 °C
    en 4,55 bij +13 °C — en is daarom onbruikbaar om een regelwijziging aan te
    toetsen. Deze sensor deelt de gemeten dag-COP door wat de installatie bij
    díe temperatuur en in díe seizoenshelft normaal presteerde. 1,00 is zoals
    altijd, hoger is beter.

    Bedoeld om over een heel seizoen op te sturen, niet per dag: de dag-tot-dag
    spreiding is ongeveer ±12%. De losse dag staat daarom niet meer in de
    attributen — een getal tonen met "dit betekent niets" eronder is netto
    negatief. Wat er wél staat is het venster, en het verschil vóór en ná de
    norm-grens met de toets of dat verschil boven de ruis uitkomt.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "cop_performance"
    _attr_icon = "mdi:gauge-full"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # De dagreeks is honderden regels en verandert alleen bij een analyse. Zonder
    # dit gaat hij bij elke state-write mee de recorder in. Het meegeleverde
    # dashboard tekent hem niet meer — de reeks bleek als plaatje niets toe te
    # voegen aan het getal — maar hij blijft gepubliceerd als ruwe data voor wie
    # er zelf een grafiek op wil zetten.
    _unrecorded_attributes = frozenset(
        {"stookdagen", "recente_dagen", "referentiecurve"}
    )

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cop_performance"
        # Entity-ID vastpinnen. HA leidt de ID van een nieuwe entity af uit het
        # *gebied* van het device; zonder dit wordt het
        # sensor.bijkeuken_quatt_warmteanalyse_cop_prestatie. Dat ging in v0.8.8
        # bij de spiegelsensoren mis en in v0.8.11 nog een keer bij de
        # kalibratiesensor — zie de toelichting bij MirrorSpec.slug.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"{ENTITY_PREFIX}_cop_prestatie",
            hass=hass,
        )
        self._attr_device_info = get_device_info(entry.entry_id)

    @property
    def native_value(self) -> float | None:
        """Voortschrijdend gemiddelde over 30 dagen, niet de losse dag.

        Eén dag is te ruis-gevoelig om als state te tonen: wie ernaar kijkt zou
        een toevallige uitschieter voor een trend aanzien.
        """
        data = self.coordinator.data
        return data.cop_performance.rolling_30d if data else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.cop_performance.rolling_30d is not None)

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None:
            return None
        perf = data.cop_performance
        if perf.rolling_30d is None:
            return None

        venster = perf.daily[-30:]
        sinds = None
        if perf.latest_date:
            try:
                laatste = datetime.fromisoformat(perf.latest_date).date()
                sinds = (dt_util.now().date() - laatste).days
            except (TypeError, ValueError):
                sinds = None

        return {
            "laatste_dag": perf.latest_date,
            "rolling_30d": perf.rolling_30d,
            # De norm zelf, zodat te zien is waar hij tegen afgezet wordt. Per
            # seizoenshelft, want najaar en voorjaar hebben elk hun eigen bins.
            "referentiecurve": {
                seizoen: {str(k): v for k, v in sorted(bins.items())}
                for seizoen, bins in perf.reference.items()
            },
            "referentie_stookdagen": perf.reference_days,
            "beoordeelde_dagen": len(perf.daily),
            # False = de beoordeelde dagen zitten zelf in de norm en een
            # verbetering wordt deels tegen zichzelf afgezet. Zonder deze vlag
            # leest het getal betrouwbaarder dan het is.
            "norm_bevroren": perf.norm_frozen,
            "norm_grens": perf.baseline_date,
            "norm_grens_handmatig": perf.baseline_explicit,
            "norm_grens_gevraagd": perf.baseline_requested,
            # Vóór en ná de grens — het eigenlijke antwoord op "heeft die
            # aanpassing geholpen".
            # voor_dagen/na_dagen tellen alleen de dagen die op seizoenspositie
            # tegen elkaar te zetten zijn; na_dagen_totaal is alles ná de grens.
            # Het verschil tussen die twee is het antwoord op "waarom staat er
            # nog geen oordeel terwijl er wel gestookt is".
            "voor_dagen": perf.before.days,
            "voor_gemiddelde": perf.before.mean,
            "na_dagen": perf.after.days,
            "na_dagen_totaal": perf.after_days_total,
            "na_gemiddelde": perf.after.mean,
            "verschil_pct": perf.delta_pct,
            # De spreiding van één dag, de grens voor het gemiddelde verschil,
            # en of het verschil daarbovenuit komt.
            # Zonder dit zou elke toevallige uitschieting als resultaat lezen.
            "dagspreiding_pct": perf.spread_pct,
            "verschil_grens_pct": perf.delta_threshold_pct,
            "verschil_significant": perf.delta_significant,
            # Liggen vóór en ná in hetzelfde deel van het stookseizoen? Zo niet,
            # dan zit er seizoen in het verschil en is het geen oordeel over de
            # aanpassing — zie SEASON_MATCH_DAYS in cop_performance.py.
            "verschil_vergelijkbaar": perf.delta_comparable,
            "voor_van": perf.before.date_from,
            "voor_tot": perf.before.date_to,
            "na_van": perf.after.date_from,
            "na_tot": perf.after.date_to,
            # Het venster achter rolling_30d, expliciet. Dat zijn de laatste 30
            # *stookdagen* en niet de laatste 30 kalenderdagen: buiten het
            # stookseizoen staat dit getal maanden stil, en zonder deze datums
            # leest het als een actuele maand.
            "venster_van": venster[0]["date"] if venster else None,
            "venster_tot": venster[-1]["date"] if venster else None,
            "venster_stookdagen": len(venster),
            "dagen_sinds_laatste_stookdag": sinds,
            # De volledige reeks voor de grafiek. Staat in
            # _unrecorded_attributes, dus dit kost de recorder niets.
            "stookdagen": perf.daily,
            # Alleen de recente dagen: de volledige reeks is honderden dagen en
            # hoort niet elke state-write mee de recorder in.
            "recente_dagen": perf.daily[-14:],
        }


# Waar het advies zijn "huidige" waarde vandaan heeft. Alleen OpenQuatt laat zijn
# stookgrens zien; de Quatt-integratie heeft geen entity voor stookgrens of
# nominaal vermogen, dus daar is de huidige instelling onbekend.
#
# Ze staan ook niet in de Quatt-app: de cloud-API achter die app (dezelfde die
# de Quatt-integratie gebruikt) kent als instellingen alleen de maximale
# watertemperatuur, de geluidsniveaus, prijssturing, all-electric boost en het
# zonnevermogen. Stookgrens en vermogen zet de installateur; een stooklijn met
# punten heeft de CiC niet, die regelt op het warmteverliesmodel.
ADVICE_SOURCE_OPENQUATT = "openquatt"
ADVICE_SOURCE_UNKNOWN = "onbekend"
# Door de gebruiker in de opties ingevuld: bekend, maar niet uitgelezen. Kan dus
# verouderen als de installateur iets aanpast — daarom staat de herkomst erbij.
ADVICE_SOURCE_MANUAL = "handmatig"


def _optional_float(value) -> float | None:
    """Een optioneel optieveld als getal; leeg of onleesbaar is ``None``."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _calc_quatt_advice(
    data: QuattStooklijnData,
    stookgrens_setting: float | None,
    openquatt: bool,
    max_water_setting: float | None = None,
    max_water_auto: bool = False,
    manual_stookgrens: float | None = None,
    manual_vermogen: float | None = None,
    emitter=None,
) -> dict[str, Any]:
    """Advies-attributen voor stookgrens, nominaal vermogen en stooklijnpunten.

    Er wordt alleen "van X naar Y" geadviseerd als X echt is uitgelezen. Tot en
    met v0.10.4 kwam X uit een regressie op de daggemiddelden tussen de knie en
    het punt waar de vraag onder de minimale modulatie zakt. Dat venster is smal
    (bij een Quatt-gebruiker ~0–6,5 °C), en het nulpunt daarvan was een
    extrapolatie over ruim 6 K, geen instelling: die kwam op 13,0 °C uit terwijl
    Quatt 16 bevestigde, met als advies "verhoog van 13 naar 16". Dagdata meten
    bovendien wat het huis vraagt — de kamerthermostaat regelt bij — en niet hoe
    de stooklijn staat. Voor het nominaal vermogen gold hetzelfde.

    Wat wél uit de meting volgt is het aanbevolen getal zelf. Dat wordt altijd
    getoond; alleen een verschil met een bekende instelling telt als aanpassing.

    Bekend is: uitgelezen uit OpenQuatt, of door de gebruiker van een CiC zelf
    ingevuld in de opties (``manual_*``). Een uitgelezen waarde gaat voor; bij
    OpenQuatt worden de handmatige velden genegeerd, die zijn voor de CiC.
    """
    from .analysis.utils import calc_heat_demand
    from .power_house import RATED_POWER_THRESHOLD, advise_zero_power_temp

    stookgrens_bron = ADVICE_SOURCE_UNKNOWN
    if stookgrens_setting is not None:
        stookgrens_bron = ADVICE_SOURCE_OPENQUATT
    elif not openquatt and manual_stookgrens is not None:
        stookgrens_setting = manual_stookgrens
        stookgrens_bron = ADVICE_SOURCE_MANUAL
    if openquatt:
        manual_vermogen = None

    attrs: dict[str, Any] = {}
    changes = 0
    # Stookgrens en vermogen staan niet in de Quatt-app (zie hierboven); bij
    # een CiC kan alleen de installateur of Quatt ze inzien en aanpassen.
    ask = (
        "in OpenQuatt"
        if openquatt
        else "Die staat niet in de Quatt-app; je installateur of Quatt-support "
        "kan hem controleren en aanpassen."
    )

    # --- Stookgrens ---
    opt = data.stooklijn.balance_temp_optimal
    attrs["stookgrens_huidig"] = stookgrens_setting
    attrs["stookgrens_optimaal"] = round(opt, 1) if opt is not None else None
    attrs["stookgrens_bron"] = stookgrens_bron
    if opt is None:
        attrs["stookgrens_advies"] = None
    elif stookgrens_setting is not None:
        target = advise_zero_power_temp(opt, stookgrens_setting)
        if target is None:
            attrs["stookgrens_advies"] = "Stookgrens is goed ingesteld"
        else:
            changes += 1
            verb = "Verhoog" if target > stookgrens_setting else "Verlaag"
            attrs["stookgrens_advies"] = (
                f"{verb} stookgrens van {stookgrens_setting:.1f} "
                f"naar {target:.1f}°C"
                # Bij de CiC kan de gebruiker dit niet zelf; zeg bij wie het kan.
                + (f". {ask}" if stookgrens_bron == ADVICE_SOURCE_MANUAL else "")
            )
    else:
        attrs["stookgrens_advies"] = (
            f"Aanbevolen stookgrens: {opt:.1f}°C (gemeten balanspunt van je "
            f"huis). "
            + (f"Controleer {ask} wat er nu staat." if openquatt else ask)
        )

    # --- Nominaal vermogen bij -10°C ---
    hl = data.heat_loss_hp
    vermogen_opt = None
    if hl.slope is not None and hl.intercept is not None:
        vermogen_opt = round(calc_heat_demand(hl.slope, hl.intercept, -10))
    # Nooit uit te lezen; alleen bekend als de gebruiker hem zelf invulde.
    attrs["nominaal_vermogen_huidig_w"] = (
        round(manual_vermogen) if manual_vermogen is not None else None
    )
    attrs["nominaal_vermogen_optimaal_w"] = vermogen_opt
    attrs["nominaal_vermogen_bron"] = (
        ADVICE_SOURCE_OPENQUATT
        if openquatt
        else ADVICE_SOURCE_MANUAL
        if manual_vermogen is not None
        else ADVICE_SOURCE_UNKNOWN
    )
    if openquatt:
        # Power House heeft geen vermogen bij -10°C maar Tc en Pr, en die
        # hangen samen met T0 — dat rekent de kalibratiesensor als drietal uit.
        attrs["nominaal_vermogen_advies"] = (
            "OpenQuatt kent geen nominaal vermogen bij -10°C; "
            "zie de Power House-kalibratie"
        )
    elif vermogen_opt is not None and manual_vermogen is not None:
        # Zelfde drempel als de Power House-kalibratie voor Pr, zodat beide
        # kaarten het over "wijkt genoeg af" niet oneens zijn.
        if abs(vermogen_opt - manual_vermogen) < RATED_POWER_THRESHOLD:
            attrs["nominaal_vermogen_advies"] = "Nominaal vermogen is goed ingesteld"
        else:
            changes += 1
            verb = "Verhoog" if vermogen_opt > manual_vermogen else "Verlaag"
            attrs["nominaal_vermogen_advies"] = (
                f"{verb} nominaal vermogen van {manual_vermogen:.0f} naar "
                f"{vermogen_opt} W bij -10°C. {ask}"
            )
    elif vermogen_opt is not None:
        attrs["nominaal_vermogen_advies"] = (
            f"Aanbevolen nominaal vermogen: {vermogen_opt} W bij -10°C. {ask}"
        )
    else:
        attrs["nominaal_vermogen_advies"] = None

    # --- Stooklijn breakpoints ---
    # Alleen OpenQuatt kan op een stooklijn draaien (Heating Curve-modus met
    # zes ``Curve Tsupply``-knoppen); de CiC kent geen stooklijn. Voor OpenQuatt
    # dus zijn eigen raster, anders landen de punten op de verkeerde knoppen.
    # De punten zelf blijven altijd beschikbaar, ook voor het max-wateradvies.
    if hl.slope is not None and hl.intercept is not None:
        breakpoints = _calc_heating_curve_breakpoints(hl.slope, hl.intercept, emitter=emitter)
        attrs["stooklijn_punten"] = breakpoints
        attrs["stooklijn_punten_bron"] = "afgiftemodel" if emitter is not None else "vaste retour"
        if openquatt:
            oq_points = _calc_heating_curve_breakpoints(
                hl.slope, hl.intercept, outdoor_temps=OPENQUATT_BREAKPOINT_TEMPS,
                emitter=emitter,
            )
            punten_str = ", ".join(
                f"{bp['buiten_temp']}°C→{bp['aanvoer_temp']}°C"
                for bp in oq_points
            )
            attrs["stooklijn_advies"] = (
                "In Heating Curve-modus: zet de Curve Tsupply-punten op "
                f"{punten_str}"
            )
        else:
            attrs["stooklijn_advies"] = None
    else:
        breakpoints = None
        attrs["stooklijn_punten"] = None
        attrs["stooklijn_advies"] = None

    # --- Maximale watertemperatuur ---
    # De enige van deze instellingen die een CiC-gebruiker zelf in de app kan
    # zetten. Het advies is bewust een ondergrens: wat het huis bij -10°C
    # volgens het model nodig heeft. Omlaag adviseren doen we niet: een te
    # krap plafond kost comfort. Daarom het hoogste van afgiftemodel en vaste
    # retour. Het afgiftemodel is geleerd op de uren die er waren (in november
    # tot ~6 kW) en wordt hier doorgetrokken naar het vermogen bij -10 °C;
    # dat is niet getoetst, en hier mag het niet ~1,8 K lager uitkomen dan
    # voorheen (review 22-11).
    def _bij_min10(punten) -> float | None:
        return next(
            (bp["aanvoer_temp"] for bp in punten or () if bp["buiten_temp"] == -10),
            None,
        )

    nodig = _bij_min10(breakpoints)
    if nodig is not None and emitter is not None:
        vast = _bij_min10(_calc_heating_curve_breakpoints(
            hl.slope, hl.intercept, outdoor_temps=(-10,)))
        if vast is not None:
            nodig = max(nodig, vast)
    attrs["max_water_huidig"] = max_water_setting
    attrs["max_water_nodig_bij_min10"] = nodig
    attrs["max_water_automatisch"] = max_water_auto
    plek = "in OpenQuatt" if openquatt else "in de Quatt-app"
    if nodig is None:
        attrs["max_water_advies"] = None
    elif max_water_auto:
        attrs["max_water_advies"] = (
            "Wordt door deze integratie automatisch bijgestuurd"
        )
    elif max_water_setting is None:
        attrs["max_water_advies"] = (
            f"Je huis heeft bij -10°C ongeveer {nodig:.1f}°C aanvoer nodig. "
            f"Zet de maximale watertemperatuur {plek} niet lager dan "
            f"{math.ceil(nodig)}°C, of laat deze integratie hem bijsturen "
            f"(opties)."
        )
    elif max_water_setting < nodig:
        changes += 1
        attrs["max_water_advies"] = (
            f"Verhoog de maximale watertemperatuur {plek} van "
            f"{max_water_setting:.0f} naar {math.ceil(nodig)}°C: bij -10°C "
            f"heeft je huis ongeveer {nodig:.1f}°C aanvoer nodig"
        )
    else:
        attrs["max_water_advies"] = (
            f"Maximale watertemperatuur ({max_water_setting:.0f}°C) is hoog "
            f"genoeg; bij -10°C is ongeveer {nodig:.1f}°C nodig"
        )

    # Stooklijn breakpoints zijn informatief, niet meegeteld.
    attrs["aantal_aanpassingen"] = changes
    return attrs


class QuattAdviceSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Statische advies-sensor: welke parameters moet Quatt aanpassen.

    State is het aantal aanbevolen aanpassingen, of dat er alleen aanbevolen
    waarden zijn als de huidige instellingen niet uit te lezen zijn. Details
    staan in de attributen; zie ``_calc_quatt_advice``.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "advice_parameters"
    _attr_icon = "mdi:tune"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_quatt_advice"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_quatt_advies_parameters", hass=coordinator.hass
        )

    def _openquatt_stookgrens(self) -> tuple[bool, float | None]:
        """(OpenQuatt aanwezig, ingestelde stookgrens) — de enige uitleesbare instelling."""
        from .discovery import ROLE_PH_ZERO_POWER_TEMP, async_discover_openquatt_entities

        found = async_discover_openquatt_entities(self.hass)
        entity_id = found.get(ROLE_PH_ZERO_POWER_TEMP)
        setting = get_float_state(self.hass, entity_id) if entity_id else None
        return bool(found), setting

    def _max_water(self) -> tuple[float | None, bool]:
        """(ingestelde max. watertemperatuur, bijsturing door deze integratie aan)."""
        from .discovery import ROLE_CH_MAX_WATER, async_resolve_setting_entity

        cfg = {**self._entry.data, **self._entry.options}
        entity_id = async_resolve_setting_entity(
            self.hass, cfg, CONF_CH_MAX_WATER_ENTITY, ROLE_CH_MAX_WATER
        )
        setting = get_float_state(self.hass, entity_id) if entity_id else None
        auto = (
            self.hass.data.get(DOMAIN, {}).get(f"{self._entry.entry_id}_ch_max_water")
            is not None
        )
        return setting, auto

    def _advice(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None
        openquatt, setting = self._openquatt_stookgrens()
        max_water, auto = self._max_water()
        cfg = {**self._entry.data, **self._entry.options}
        return _calc_quatt_advice(
            data, setting, openquatt, max_water, auto,
            manual_stookgrens=_optional_float(cfg.get(CONF_CIC_ZERO_POWER_TEMP)),
            manual_vermogen=_optional_float(cfg.get(CONF_CIC_RATED_POWER)),
            emitter=getattr(self.coordinator, "emitter", None),
        )

    @property
    def native_value(self) -> str | None:
        advice = self._advice()
        if advice is None:
            return None
        changes = advice["aantal_aanpassingen"]
        if changes:
            return f"{changes} aanpassing{'en' if changes != 1 else ''} aanbevolen"
        if advice["stookgrens_bron"] == ADVICE_SOURCE_OPENQUATT:
            return "Geen afwijking gevonden"
        # CiC: pas "geen afwijking" als beide instellingen bekend zijn. Met één
        # ingevuld is over de andere nog niets gezegd.
        if (
            advice["stookgrens_bron"] == ADVICE_SOURCE_MANUAL
            and advice["nominaal_vermogen_bron"] == ADVICE_SOURCE_MANUAL
        ):
            return "Geen afwijking gevonden"
        return "Aanbevolen instellingen beschikbaar"

    @property
    def extra_state_attributes(self) -> dict | None:
        return self._advice()


class QuattOpenQuattCurveSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Passieve output sensor: optimale stooklijn breakpoints voor OpenQuatt.

    State = aantal breakpoints (6).  Attributen bevatten de individuele punten
    zodat HA-automations ze naar OpenQuatt number-entiteiten kunnen schrijven.

    Gebruikt bewust ``OPENQUATT_BREAKPOINT_TEMPS`` en niet het advies-raster:
    de punten worden op volgorde naar zes vaste number-entiteiten geschreven,
    dus de buitentemperaturen moeten één-op-één matchen met wat de firmware
    daar aanbiedt. Elk attribuut draagt zijn buitentemperatuur mee (``bp_N_buiten``),
    zodat een automation op waarde kan controleren in plaats van op positie.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "openquatt_curve"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_openquatt_curve"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id, los van de vertaalde naam — zie DESCRIPTION_OBJECT_IDS.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_openquatt_stooklijn", hass=coordinator.hass
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None
        return len(OPENQUATT_BREAKPOINT_TEMPS)

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None

        breakpoints = _calc_heating_curve_breakpoints(
            data.heat_loss_hp.slope,
            data.heat_loss_hp.intercept,
            outdoor_temps=OPENQUATT_BREAKPOINT_TEMPS,
            emitter=getattr(self.coordinator, "emitter", None),
        )

        attrs: dict[str, Any] = {"breakpoints": breakpoints}
        for i, bp in enumerate(breakpoints, 1):
            attrs[f"bp_{i}_buiten"] = bp["buiten_temp"]
            attrs[f"bp_{i}_aanvoer"] = bp["aanvoer_temp"]
        return attrs


class QuattSourceMirrorSensor(SensorEntity):
    """Spiegelt één meting, ongeacht welke integratie hem levert.

    Het bestaansrecht: een dashboard kan niet resolven. Het hardcodeert een
    entity-ID, en als die bron wegvalt blijft de kaart leeg zonder uitleg. Deze
    sensor heeft een stabiel entity-ID dat blijft werken terwijl de onderliggende
    bron wisselt, en zet in zijn attributen wie er op dit moment levert.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        registry: SourceRegistry,
        spec: MirrorSpec,
    ) -> None:
        self._entry = entry
        self._registry = registry
        self._spec = spec
        # De slug is ook de vertaalsleutel: één naam per rol, en de
        # entity-id die eruit volgt staat toch al vast.
        self._attr_translation_key = spec.slug
        self._attr_unique_id = f"{entry.entry_id}_source_{spec.role}"
        # Deterministische entity-id — zie de toelichting bij MirrorSpec.slug.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, f"{ENTITY_PREFIX}_{spec.slug}", hass=hass
        )
        self._attr_icon = spec.icon
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = get_device_info(entry.entry_id)
        self._tracked: list[str] = []
        self._remove_tracker = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resubscribe()
        # Ook op de klok meelopen: de kandidatenlijst zelf kan veranderen als er
        # een integratie bijkomt, en daar is geen state-change van een entity
        # die we al volgen.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(minutes=1)
            )
        )

    def _resubscribe(self) -> None:
        """Volg álle kandidaten, niet alleen de actieve.

        Dit is precies waar de oude opzet op stukliep: die abonneerde zich één
        keer op de bij het opstarten gekozen entity. Kwam een betere bron later
        terug, dan kwam die state-change nooit binnen.
        """
        source = self._registry.get(self._spec.role)
        candidates = list(source.candidates) if source else []
        if candidates == self._tracked:
            return

        if self._remove_tracker is not None:
            self._remove_tracker()
            self._remove_tracker = None

        self._tracked = candidates
        tracked = list(candidates)
        if self._spec.role == ROLE_INDOOR_TEMP:
            room = async_room_temperature(self.hass, self._entry.entry_id)
            if room is not None:
                tracked += [e for e in room.references if e not in tracked]
        if tracked:
            self._remove_tracker = async_track_state_change_event(
                self.hass, tracked, self._handle_source_change
            )

    @callback
    def _handle_source_change(self, _event) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_tick(self, _now) -> None:
        self._resubscribe()
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        entity_id = self._registry.active_entity(self._spec.role)
        if entity_id is None:
            return None
        if self._spec.role == ROLE_INDOOR_TEMP:
            # De kamertemperatuur zoals de integratie hem gebruikt: begrensd
            # op de extra kamersensoren, als die er zijn.
            return room_value(self.hass, self._entry.entry_id, entity_id)
        return get_float_state(self.hass, entity_id)

    @property
    def available(self) -> bool:
        return self._registry.active_entity(self._spec.role) is not None

    @property
    def extra_state_attributes(self) -> dict:
        source = self._registry.get(self._spec.role)
        if source is None:
            return {}
        attrs = {
            "source_entity": source.active,
            "source_integration": source.integration,
            "candidates": list(source.candidates),
            "switched_at": (
                source.switched_at.isoformat() if source.switched_at else None
            ),
        }
        if self._spec.role == ROLE_INDOOR_TEMP:
            room = async_room_temperature(self.hass, self._entry.entry_id)
            if room is not None:
                attrs.update(room.attributes())
        return attrs


class QuattSourceOverviewSensor(SensorEntity):
    """Overzicht: welke integratie levert welke meting.

    State is de lijst integraties die op dit moment iets leveren; de volledige
    rol-naar-entity kaart staat in de attributen.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "source_overview"
    _attr_icon = "mdi:source-branch"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, registry: SourceRegistry
    ) -> None:
        self._entry = entry
        self._registry = registry
        self._attr_unique_id = f"{entry.entry_id}_source_overview"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, f"{ENTITY_PREFIX}_{OVERVIEW_SLUG}", hass=hass
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(minutes=1)
            )
        )

    @callback
    def _handle_tick(self, _now) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        in_use = self._registry.integrations_in_use()
        return " + ".join(in_use) if in_use else "geen"

    @property
    def extra_state_attributes(self) -> dict:
        summary = self._registry.summary()
        # Een rol zonder ook maar één kandidaat is niet "gemist" maar afwezig:
        # op een solo-installatie bestaat hp2 domweg niet, en die als gat tonen
        # stuurt iedere solo-eigenaar op zoek naar een bron die er niet hoort te
        # zijn. Zo'n rol telt daarom in het geheel niet mee — ook niet als
        # opgelost, want dan zou het aantal juist te rooskleurig worden.
        van_toepassing = {
            role: info
            for role, info in summary.items()
            if info["entity"] is not None or info["candidates"]
        }
        missing = [
            role for role, info in van_toepassing.items() if info["entity"] is None
        ]
        cfg = {**self._entry.data, **self._entry.options}
        return {
            "roles": summary,
            "missing_roles": missing,
            "roles_total": len(van_toepassing),
            "roles_resolved": len(van_toepassing) - len(missing),
            # Hoort hier omdat het dezelfde vraag beantwoordt als de rest van
            # deze sensor: waar komt de data vandaan. Staat dit uit, dan komen
            # nieuwe dagen uit de recorder en groeit de insights-cache niet meer.
            "cloud_enabled": bool(
                cfg.get(CONF_QUATT_CLOUD_ENABLED, DEFAULT_QUATT_CLOUD_ENABLED)
            ),
        }


class QuattPowerHouseCalibrationSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Passieve output sensor: het gekalibreerde Power House-huismodel.

    OpenQuatt's Power House-strategie draagt hetzelfde lineaire warmteverlies-
    model in zich dat deze integratie meet. Deze sensor vertaalt de meting naar
    de drie number-entiteiten die dat model in de firmware vastleggen, en zet er
    de huidige waarden naast zodat te zien is of bijstellen zin heeft.

    Bewust géén schrijfactie: dit is kalibratie, geen regeling. De waarden
    veranderen hooguit één keer per analyse en horen bij een bewuste stap, niet
    bij een tikkende timer. De ``*_entity``-attributen dragen de opgezochte
    entity-ID mee, zodat een automation niet op naam hoeft te gokken — de
    firmware heeft die namen al eens onder de voet gelopen.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "power_house_calibration"
    _attr_icon = "mdi:home-search-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_power_house_calibration"
        # Entity-ID vastpinnen, net als de spiegelsensoren. HA bouwt de ID voor
        # een nieuwe entity op uit het *gebied* van het device, dus zonder dit
        # wordt het sensor.bijkeuken_quatt_warmteanalyse_… en breekt elke
        # dashboardverwijzing. Zie de toelichting bij MirrorSpec.slug.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"{ENTITY_PREFIX}_openquatt_power_house_kalibratie",
            hass=hass,
        )
        self._attr_device_info = get_device_info(entry.entry_id)

    def _calibration(self, targets: dict[str, str | None] | None = None):
        from .power_house import calc_power_house_calibration

        data = self.coordinator.data
        if data is None:
            return None
        if targets is None:
            targets = self._targets()
        sl = data.stooklijn
        return calc_power_house_calibration(
            data.heat_loss_hp.heat_loss_coefficient,
            sl.balance_temp_optimal,
            capability_slope=sl.slope_local,
            capability_intercept=sl.intercept_local,
            knee_power=sl.knee_power,
            # Tc en Pr worden tegen de T0 van de regelaar uitgerekend, niet tegen
            # het gemeten balanspunt — zie de toelichting in power_house.py.
            controller_zero_power_temp=self._current(targets["zero_power_temp"]),
        )

    def _targets(self) -> dict[str, str | None]:
        """Rol → entity-ID van de bijbehorende OpenQuatt number-entity."""
        from .discovery import (
            ROLE_PH_COLD_TEMP,
            ROLE_PH_RATED_POWER,
            ROLE_PH_ZERO_POWER_TEMP,
            async_discover_openquatt_entities,
        )

        found = async_discover_openquatt_entities(self.hass)
        return {
            "zero_power_temp": found.get(ROLE_PH_ZERO_POWER_TEMP),
            "cold_temp": found.get(ROLE_PH_COLD_TEMP),
            "rated_power": found.get(ROLE_PH_RATED_POWER),
        }

    def _current(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        return get_float_state(self.hass, entity_id)

    @property
    def native_value(self) -> str | None:
        from .power_house import (
            COLD_TEMP_THRESHOLD,
            RATED_POWER_THRESHOLD,
            ZERO_POWER_TEMP_THRESHOLD,
        )

        targets = self._targets()
        cal = self._calibration(targets)
        if cal is None:
            return "onvoldoende data"

        if not any(targets.values()):
            return "OpenQuatt niet gevonden"

        # T0 telt alleen mee als de meting er iets over te zeggen heeft. Normaal
        # wordt hij overgenomen van de regelaar — boven de stookgrens is geen
        # data — maar staat hij zó ver weg dat de feedforward er structureel te
        # weinig door vraagt, dan is dat wél te meten. Zie power_house.py.
        pairs = [
            (cal.cold_temp, targets["cold_temp"], COLD_TEMP_THRESHOLD),
            (cal.rated_power, targets["rated_power"], RATED_POWER_THRESHOLD),
        ]
        if cal.zero_power_temp_advised:
            pairs.append(
                (
                    cal.zero_power_temp,
                    targets["zero_power_temp"],
                    ZERO_POWER_TEMP_THRESHOLD,
                )
            )
        changes = 0
        for advised, entity_id, threshold in pairs:
            current = self._current(entity_id)
            # Een onbekende huidige waarde telt niet als afwijking: dan is er
            # niets om mee te vergelijken, en "aanpassing nodig" roepen op basis
            # van een lege state is misleidend.
            if current is not None and abs(advised - current) >= threshold:
                changes += 1

        if changes == 0:
            return "model is gekalibreerd"
        return f"{changes} aanpassing{'en' if changes > 1 else ''} aanbevolen"

    @property
    def extra_state_attributes(self) -> dict | None:
        targets = self._targets()
        cal = self._calibration(targets)
        if cal is None:
            return None

        attrs: dict[str, Any] = {
            "zero_power_temp": cal.zero_power_temp,
            "cold_temp": cal.cold_temp,
            "rated_power": cal.rated_power,
            "zero_power_temp_entity": targets["zero_power_temp"],
            "cold_temp_entity": targets["cold_temp"],
            "rated_power_entity": targets["rated_power"],
            "zero_power_temp_huidig": self._current(targets["zero_power_temp"]),
            "cold_temp_huidig": self._current(targets["cold_temp"]),
            "rated_power_huidig": self._current(targets["rated_power"]),
            "capaciteitsbron": cal.capacity_source,
            "vollast_vermogen_w": cal.full_output_power,
            # T0 wordt overgenomen, niet geadviseerd. Het gemeten balanspunt
            # staat er los naast: informatief, maar te zwak onderbouwd om naar
            # te schrijven — de regressie ziet geen enkele dag boven 16 °C.
            "zero_power_temp_bron": cal.zero_power_temp_source,
            "balanspunt_gemeten": cal.balance_point_measured,
            "zero_power_temp_geadviseerd": cal.zero_power_temp_advised,
            # Wat de ingestelde stookgrens kost aan structureel te weinig vraag.
            # Positief = het huis vraagt meer dan de feedforward aanbiedt.
            "stookgrens_afwijking_w": cal.zero_power_temp_bias_w,
        }
        basis = (
            f"Bij {cal.cold_temp:.1f}°C buiten heeft het huis "
            f"{cal.rated_power:.0f} W nodig en draaien de warmtepompen vollast."
        )
        if cal.zero_power_temp_advised:
            attrs["toelichting"] = (
                f"{basis} De ingestelde stookgrens staat "
                f"{cal.zero_power_temp_bias_w:.0f} W van de meting af: zo veel "
                f"vraagt de feedforward elke stookdag te weinig. Geadviseerd "
                f"wordt het gemeten balanspunt van "
                f"{cal.balance_point_measured:.1f}°C — dat is extrapolatie "
                f"boven de warmste meetdag, maar wel dichter bij de meting dan "
                f"de huidige stand. Tc en Pr hierboven horen bij dat nieuwe "
                f"nulpunt; pas ze samen aan."
            )
        else:
            attrs["toelichting"] = (
                f"{basis} Tc en Pr zijn uitgerekend tegen de ingestelde "
                f"stookgrens van {cal.zero_power_temp:.1f}°C; die wordt niet "
                f"geadviseerd omdat de meting boven de stookgrens geen data "
                f"heeft (regressie zegt {cal.balance_point_measured:.1f}°C, "
                f"maar dat is extrapolatie)."
            )
        return attrs


# De hartslag als timedelta, één keer opgebouwd.
HEARTBEAT_INTERVAL = timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS)


class QuattHeatDemandSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """De warmtevraag van het huis in W — het koppelvlak naar Power House.

    Publiceert ``P = UA · (T_balans − T_buiten)``, begrensd op nul, uit de
    seizoensregressie over een jaar meetdata. Wie de OpenQuatt-bronhelper
    hiernaar laat wijzen vervangt daarmee de feedforward van Power House; de
    comfortterm, de clamp op ``Pr``, de slew-limiter en de waterbegrenzer
    blijven van de firmware. Zie ``heat_demand.py`` voor waarom hier bewust
    niets van wordt afgetrokken.

    Bewust géén schrijfactie, ook niet naar de bronhelper: de gebruiker wijst
    hem één keer aan, en het leegmaken van dat ene veld is de noodrem.

    Zonder analysedata geeft deze sensor ``None``. De proxy in het HA-package
    maakt daar 0 W van, maar zet zijn ``…_valid``-vlag op ``off``, en de
    firmware houdt dan 300 s de laatste geldige waarde vast en valt daarna
    terug op haar eigen huismodel. Dat vervalgedrag is van de firmware — hier
    hoeft niets te worden nagebouwd.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "heat_demand"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_heat_demand"
        # Entity-ID vastpinnen: HA leidt de ID van een nieuwe entity af uit het
        # *gebied* van het device, en dit device staat in de bijkeuken. Zie de
        # toelichting bij MirrorSpec.slug — en bij de kalibratiesensor, waar het
        # in v0.8.11 alsnog misging.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"{ENTITY_PREFIX}_warmtevraag",
            hass=hass,
        )
        self._attr_device_info = get_device_info(entry.entry_id)
        self._openquatt_cache: tuple[float, dict[str, str]] | None = None
        # Eén melding per keer dat de bron bevriest, niet per uitgelezen veld.
        self._stale_logged = False

    @property
    def _outdoor_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_OUTDOOR_TEMP,
            config=cfg, conf_key=CONF_TEMP_ENTITIES,
        )

    async def async_added_to_hass(self) -> None:
        """Volg de buitentemperatuur, niet alleen de analysecyclus.

        Het huismodel verandert hooguit één keer per analyse, maar de vraag die
        eruit volgt beweegt met het weer mee. Zonder deze listener zou de
        regelaar een uur op een verouderde vraag lopen.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id, (ROLE_OUTDOOR_TEMP,)
                ),
                self._handle_state_change,
            )
        )
        # Zonder deze hartslag zou de versheidscontrole nooit kunnen afgaan:
        # de coordinator ververst alleen op verzoek (``update_interval=None``),
        # dus de listener hierboven is de enige trigger — en juist een bevroren
        # bronsensor stuurt geen enkel event. De gepubliceerde vraag zou dan tot
        # in lengte van dagen op zijn laatste waarde blijven staan.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_heartbeat, HEARTBEAT_INTERVAL
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_heartbeat(self, _now) -> None:
        self.async_write_ha_state()

    def _openquatt(self) -> dict[str, str]:
        """De OpenQuatt-detectie, hooguit één keer per state-write.

        ``async_discover_openquatt_entities`` loopt het hele entity-register
        langs, en HA vraagt bij elke write zowel ``native_value`` als
        ``extra_state_attributes`` op — die samen vier keer een nulpunt of een
        rol nodig hebben. Een kort geheugen vouwt dat terug naar één scan,
        zonder de detectie vast te zetten: hernoemt de firmware een entiteit,
        dan is dat binnen enkele seconden weer zichtbaar.
        """
        now = monotonic()
        cached = self._openquatt_cache
        if cached is not None and now - cached[0] < OPENQUATT_CACHE_SECONDS:
            return cached[1]

        from .discovery import async_discover_openquatt_entities

        found = async_discover_openquatt_entities(self.hass)
        self._openquatt_cache = (now, found)
        return found

    def _zero_point(self, openquatt: dict[str, str] | None = None) -> tuple[float, str] | None:
        """De buitentemperatuur waarbij de warmtevraag nul wordt, plus zijn bron.

        **De stookgrens van de regelaar gaat vóór het gemeten balanspunt**, om
        dezelfde reden die in ``power_house.py`` uitgebreid staat: boven de
        stookgrens wordt er niet gestookt, dus daar heeft de regressie geen
        data en is haar nulpunt extrapolatie. Bij deze woning ligt de warmste
        waarneming op 15,2 °C terwijl de fit het nulpunt op 16,7 legt.

        Zonder deze voorrang lopen twee dingen uiteen die deze integratie over
        hetzelfde huis publiceert: de kalibratiesensor rekent Tc en Pr al tegen
        de stookgrens van de regelaar uit. En het verschil is niet alleen
        cosmetisch — tussen die twee nulpunten zouden we een vraag publiceren
        waar de firmware zelf nul zegt, en de installatie dus boven haar eigen
        stookgrens laten stoken.

        Zonder regelaar valt hij terug op de meting; dan is dat het enige
        nulpunt dat er is.
        """
        from .discovery import ROLE_PH_ZERO_POWER_TEMP
        from .power_house import T0_FROM_CONTROLLER, T0_FROM_MEASUREMENT

        if openquatt is None:
            openquatt = self._openquatt()
        controller_t0 = get_float_state(
            self.hass, openquatt.get(ROLE_PH_ZERO_POWER_TEMP) or ""
        )
        if controller_t0 is not None:
            return controller_t0, T0_FROM_CONTROLLER

        data = self.coordinator.data
        if data is None or data.heat_loss_hp.balance_point is None:
            return None
        return float(data.heat_loss_hp.balance_point), T0_FROM_MEASUREMENT

    def _outdoor_temp(self) -> float | None:
        """De buitentemperatuur, mits vers genoeg om op te regelen.

        Een bronsensor die blijft hangen op een oude waarde wordt nergens
        anders opgemerkt: hij levert nog steeds een geldig getal, dus de proxy
        blijft ``valid`` en de firmware ziet geen reden om terug te vallen op
        haar eigen model. Zonder deze controle zouden we met overtuiging een
        vraag blijven publiceren die bij een bevroren meting hoort.
        """
        state = self.hass.states.get(self._outdoor_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None

        # ``last_reported`` telt élke melding mee, ook als de waarde gelijk
        # bleef; ``last_changed`` doet dat niet en zou een stabiele
        # buitentemperatuur ten onrechte als bevroren aanmerken.
        reported = getattr(state, "last_reported", None) or state.last_updated
        if reported is not None:
            age = (dt_util.utcnow() - reported).total_seconds()
            if age > OUTDOOR_MAX_AGE_SECONDS:
                # HA leest bij elke write zowel de waarde als de attributen uit,
                # dus zonder deze vlag komt dezelfde melding meermaals per write
                # in het log — en blijft dat doen zolang de bron stilstaat.
                if not self._stale_logged:
                    _LOGGER.warning(
                        "Warmtevraag: buitentemperatuur van '%s' is %.0f min "
                        "oud (grens %.0f min) — geen vraag gepubliceerd",
                        self._outdoor_entity,
                        age / 60,
                        OUTDOOR_MAX_AGE_SECONDS / 60,
                    )
                    self._stale_logged = True
                return None
        self._stale_logged = False

        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _demand(self, openquatt: dict[str, str] | None = None) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        hlc = data.heat_loss_hp.heat_loss_coefficient
        if not hlc or hlc <= 0:
            return None

        zero_point = self._zero_point(openquatt)
        t_outdoor = self._outdoor_temp()
        if zero_point is None or t_outdoor is None:
            return None

        t_zero, _source = zero_point
        return round(hlc * max(0.0, t_zero - t_outdoor))

    @property
    def native_value(self) -> float | None:
        return self._demand(self._openquatt())

    @property
    def extra_state_attributes(self) -> dict | None:
        from .discovery import ROLE_PH_RATED_POWER, async_heat_demand_link

        openquatt = self._openquatt()
        link = async_heat_demand_link(self.hass, self.entity_id, openquatt=openquatt)
        data = self.coordinator.data
        heat_loss = data.heat_loss_hp if data is not None else None

        zero_point = self._zero_point(openquatt)
        attrs: dict[str, Any] = {
            "buiten_temp": self._outdoor_temp(),
            "warmteverliescoefficient": (
                round(heat_loss.heat_loss_coefficient, 1)
                if heat_loss and heat_loss.heat_loss_coefficient is not None
                else None
            ),
            "balanspunt_gemeten": (
                round(heat_loss.balance_point, 2)
                if heat_loss and heat_loss.balance_point is not None
                else None
            ),
            "nulpunt": round(zero_point[0], 2) if zero_point else None,
            "nulpunt_bron": zero_point[1] if zero_point else None,
            "formule": "UA × max(0, T_nulpunt − T_buiten)",
            "koppeling": link.status,
            "koppeling_actief": link.active,
            "koppeling_ingesteld": link.wired,
            "firmware_bevestigt": link.confirmed,
            "firmware_feedforward": link.firmware_feedforward,
            "bronhelper": SOURCE_SELECTOR_ENTITY,
            "bronhelper_wijst_naar": link.selector,
            "proxy_entity": link.proxy_entity,
            "firmware_bron": link.firmware_source,
            # Leeg zolang er geen OpenQuatt-node gevonden is. Het dashboard
            # hangt hieraan of het de koppelinstructie toont: die is zinloos —
            # en verwarrend — voor een installatie die alleen een CiC heeft.
            "keuzeknop_entity": link.select_entity,
        }

        # Het plafond van de firmware erbij: die klemt een externe vraag op
        # ``Rated maximum house power``, en dat gebeurt zonder melding. Wie de
        # vraag boven Pr ziet uitkomen weet dan meteen dat de regelaar hem
        # afkapt en dat Pr aan bijstelling toe is.
        rated_entity = openquatt.get(ROLE_PH_RATED_POWER)
        rated = get_float_state(self.hass, rated_entity) if rated_entity else None
        attrs["firmware_plafond_w"] = rated
        value = self._demand(openquatt)
        attrs["boven_firmware_plafond"] = (
            bool(rated is not None and value is not None and value > rated)
        )
        return attrs


class QuattPriceShiftSensor(QuattHeatDemandSensor):
    """Schaduwmeting: wat had verschuiven naar goedkope uren opgeleverd?

    **Deze sensor stuurt niets aan.** Elke nacht rekent hij het komende etmaal
    door: de warmte die het RC-model nodig heeft om het setpoint te houden,
    herverdeeld naar de uren met de meeste warmte per euro — ``(COP / prijs)^γ``
    — binnen dezelfde kamerdrift-grenzen. De voorspelde besparing van die dag
    gaat de teller in. De state is het totaal sinds het begin van de meting, in
    euro's.

    Wat het is en wat niet:

    * Een **modelvoorspelling**, geen meting van werkelijk verbruik. De vlakke
      reeks komt uit ``simulate_forward`` van de MPC-sensor: zon,
      kamertemperatuur, setpoint en thermische massa doen mee. Tot en met
      v0.10.4-beta.3 was het ``UA · (T0 − T_buiten)``, de reeks van
      ``warmtevraag``. Die is voor de firmware bedoeld, die er zelf de kamerfout
      en de zonnewinst vanaf haalt; als bedrag telde hij warmte die nooit
      geleverd wordt — op 27-09-2026 9 kWh voor een etmaal waarin het RC-model
      0 W gaf, in een maand waarin de warmtepomp niets leverde. De COP komt uit
      de gemeten referentiecurve.
    * Het **extra warmteverlies** van voorverwarmen wordt wel meegerekend
      (``UA · drift`` per uur). Zonder die term is een warmer huis gratis.
    * γ wordt **per dag gekozen** met ``scan_gamma``: de rustigste die vrijwel
      de volle winst pakt zonder dat het firmwareplafond of de driftgrens
      ingrijpt. Geen knop — die kwam in v0.9.14 juist weg omdat hij op elke
      stand hetzelfde deed.
    * Ter vergelijking loopt een **pure COP-verschuiving** mee, in euro's tegen
      dezelfde prijzen. Het verschil tussen de twee is wat de prijs toevoegt.

    Erft UA, versheidsbewaking en OpenQuatt-detectie van
    ``QuattHeatDemandSensor``.
    """

    _attr_translation_key = "price_shift"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash-clock"
    _attr_suggested_display_precision = 2
    # Het uur-voor-uur-overzicht van het komende etmaal is voor het dashboard;
    # het verandert elk uur en hoort niet elke keer de recorder in.
    _unrecorded_attributes = frozenset({"komend_etmaal"})

    _REASON_NO_FORECAST = "weersverwachting nog niet geladen"
    _STEPS_PER_HOUR = 60 // DEMAND_SHIFT_STEP_MINUTES

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: "QuattMpcSensor",
        benchmark_sensor: QuattModelBenchmarkSensor,
    ) -> None:
        super().__init__(hass, coordinator, entry)
        self._mpc = mpc_sensor
        self._benchmark = benchmark_sensor
        self._attr_unique_id = f"{entry.entry_id}_price_shift"
        # Entity-ID vastpinnen, zie de toelichting bij MirrorSpec.slug.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"{ENTITY_PREFIX}_prijsverschuiving",
            hass=hass,
        )
        self._store = Store(
            hass, PRICE_SHIFT_STORAGE_VERSION, PRICE_SHIFT_STORAGE_KEY
        )
        self._days: list[dict] = []
        self._loaded = False
        # Het komende etmaal, elk uur ververst. Los van de dagtelling: dit is
        # wat de sensor nú zou kiezen, de telling is wat hij om middernacht koos.
        self._preview: dict | None = None
        # Waarom er geen uitkomst is, als die er niet is. Een meting die stil
        # wegvalt is niet te onderscheiden van een dag zonder winst.
        self._reason: str | None = None
        # Hoeveelste poging van de nachtmeting; zie PRICE_SHIFT_RETRIES.
        self._daily_attempt = 0

    # -- configuratie ------------------------------------------------------

    @property
    def _cfg(self) -> dict:
        return {**self._entry.data, **self._entry.options}

    @property
    def _mode(self) -> str:
        return self._cfg.get(CONF_PRICE_MODE, DEFAULT_PRICE_MODE)

    def _prices(self, start: datetime, n_hours: int) -> list[float]:
        """Prijs per kwartier vanaf ``start``; leeg zonder bruikbare reeks."""
        from .analysis.tariff import normal_dal_prices, prices_from_attributes

        cfg = self._cfg
        if self._mode == PRICE_MODE_NORMAL_DAL:
            per_uur = normal_dal_prices(
                start,
                n_hours,
                float(cfg.get(CONF_PRICE_NORMAL, DEFAULT_PRICE_NORMAL)),
                float(cfg.get(CONF_PRICE_DAL, DEFAULT_PRICE_DAL)),
                int(cfg.get(CONF_DAL_START_HOUR, DEFAULT_DAL_START_HOUR)),
                int(cfg.get(CONF_DAL_END_HOUR, DEFAULT_DAL_END_HOUR)),
                bool(cfg.get(CONF_DAL_WEEKEND, DEFAULT_DAL_WEEKEND)),
                tz=start.tzinfo,
            )
            return [p for p in per_uur for _ in range(self._STEPS_PER_HOUR)]
        if self._mode == PRICE_MODE_DYNAMIC:
            entity_id = cfg.get(CONF_PRICE_ENTITY)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None:
                return []
            try:
                current_price = float(state.state)
            except (TypeError, ValueError):
                current_price = None
            return prices_from_attributes(
                state.attributes,
                start,
                n_hours,
                step_minutes=DEMAND_SHIFT_STEP_MINUTES,
                unit=state.attributes.get("unit_of_measurement"),
                tz=start.tzinfo,
                current_price=current_price,
                now=dt_util.utcnow(),
            )
        return []

    # -- rekenen -----------------------------------------------------------

    def _evaluate(self, start: datetime) -> dict | None:
        """Reken het venster vanaf ``start`` door, of ``None`` met een reden."""
        from .analysis.cop_performance import (
            SEASON_AUTUMN,
            SEASON_SPRING,
            season_of,
        )
        from .analysis.demand_shift import calculate_demand_shift, scan_gamma
        from .discovery import ROLE_PH_RATED_POWER

        data = self.coordinator.data
        if data is None:
            self._reason = "geen analysedata"
            return None
        hlc = data.heat_loss_hp.heat_loss_coefficient
        if not hlc or hlc <= 0:
            self._reason = "geen warmteverliescoëfficiënt"
            return None

        openquatt = self._openquatt()
        t_outdoor = self._outdoor_temp()
        if t_outdoor is None:
            self._reason = "geen verse buitentemperatuur"
            return None

        fc_temps, fc_solar, fc_meta = self._mpc.build_forecast_arrays(
            t_outdoor, n_hours=DEMAND_SHIFT_HOURS
        )
        # Uren zonder verwachting krijgen de huidige buitentemperatuur, en zijn
        # te herkennen aan een ontbrekend tijdstip. Te veel daarvan en de dag is
        # verzonnen: geen uitkomst, in plaats van stil op een vlakke reeks.
        echt = sum(1 for m in fc_meta if m.get("datetime"))
        if not fc_temps or echt < PRICE_SHIFT_MIN_FORECAST_FRACTION * len(fc_temps):
            self._reason = self._REASON_NO_FORECAST
            return None
        prices = self._prices(start, len(fc_temps))
        if not prices:
            self._reason = "geen prijzen voor het venster"
            return None
        # Een dynamische reeks kan korter zijn dan de weersverwachting (morgen
        # nog niet bekend). Dan het venster inkorten, niet de prijs verzinnen.
        venster_uren = len(prices) // self._STEPS_PER_HOUR
        fc_temps = fc_temps[:venster_uren]
        fc_solar = fc_solar[:venster_uren]

        baseline, reden = self._mpc.simulate_heat_need(fc_temps, fc_solar)
        if baseline is None or len(baseline) != len(fc_temps):
            self._reason = reden or "huismodel gaf geen reeks"
            return None

        # De COP-curve van de seizoenshelft waarin het venster valt. Ontbreekt
        # die helft nog, dan de andere: voor een weging tellen de verhoudingen
        # tussen de uren, en die liggen in beide helften dicht bij elkaar.
        reference = data.cop_performance.reference or {}
        season = season_of(start.month)
        other = SEASON_SPRING if season == SEASON_AUTUMN else SEASON_AUTUMN
        curve = reference.get(season) or reference.get(other) or {}

        params = self._mpc.thermal_params
        c_whk = params.get("C_whk") if params.get("converged") else None
        # De kamerdrift met het 2R2C als dat goedgekeurd is voor gebruik: één
        # capaciteit voor het hele huis overschat hoeveel een kort tekort de
        # lucht laat zakken niet, maar wel hoe traag hij terugkomt.
        drift_response = None
        two = self._mpc.house_model_2r2c()
        if two is not None:
            from .analysis.thermal_model_2r2c import room_response

            p2 = two[0]
            drift_response = lambda delta, dt, _p=p2: room_response(_p, delta, dt)  # noqa: E731
        self._drift_model = "2r2c" if drift_response is not None else (
            "1r1c" if c_whk else None)
        # COP van de verschoven reeks met de hogere aanvoer van een voller uur.
        emitter = getattr(self._mpc, "emitter", None)
        supply_of = None
        if emitter is not None:
            t_room = self._mpc._resolve_room_setpoint()[0]
            supply_of = lambda p, _e=emitter, _t=t_room: _e.supply(p, _t)  # noqa: E731
        ceiling = get_float_state(
            self.hass, openquatt.get(ROLE_PH_RATED_POWER) or ""
        ) if openquatt else None

        # Aan/uit per uur zodra het minimumvermogen geleerd is; daarvoor None
        # en rekent de verschuiving zonder, zoals voorheen.
        min_power = self._mpc.min_power_curve.for_temps(fc_temps)

        # De verschuiving rekent per kwartier. Temperatuur, vraag en minimum zijn
        # uurwaarden en blijven binnen het uur gelijk, zodat er geen energie bij
        # komt of verdwijnt.
        def per_stap(reeks: list[float]) -> list[float]:
            return [v for v in reeks for _ in range(self._STEPS_PER_HOUR)]

        baseline = per_stap(baseline)
        min_power = per_stap(min_power) if min_power is not None else None
        common = {
            "ceiling_w": ceiling,
            "thermal_mass_wh_k": c_whk,
            "drift_response": drift_response,
            "supply_of": supply_of,
            "max_drift_k": DEMAND_SHIFT_MAX_DRIFT_K,
            "max_overshoot_k": DEMAND_SHIFT_MAX_OVERSHOOT_K,
            "baseline": baseline,
            "min_power_w": min_power,
            "dt_hours": DEMAND_SHIFT_STEP_MINUTES / 60,
            "min_run_steps": (
                DEMAND_SHIFT_MIN_RUN_MINUTES // DEMAND_SHIFT_STEP_MINUTES
            ),
        }
        # Nulpunt ``None``: met een basisreeks rekent demand_shift er niet mee.
        args = (per_stap(fc_temps), curve, float(hlc), None)

        # Begrensde punten tellen mee: een teruggeschaalde verschuiving blijft
        # binnen de comfortgrens en is dus een geldig plan. Zie scan_gamma.
        scan_p = scan_gamma(
            *args, prices=prices, begrenzing_toegestaan=True, **common
        )
        gamma_p = scan_p.advies or 0.0
        r_p = calculate_demand_shift(*args, gamma_p, prices=prices, **common)

        # Dezelfde dag met alleen de COP-weging, geprijsd tegen dezelfde tarieven.
        # Ook gekozen op euro's, met het warmteverlies van voorverwarmen: anders
        # wint een γ die kWh bespaart maar na het prijzen geld kost.
        scan_c = scan_gamma(
            *args, prices=prices, weigh_prices=False,
            begrenzing_toegestaan=True, **common,
        )
        gamma_c = scan_c.advies or 0.0
        r_c = calculate_demand_shift(
            *args, gamma_c, prices=prices, weigh_prices=False, **common
        )

        if r_p.cost_flat_eur is None:
            self._reason = "geen bruikbare COP-curve"
            return None

        self._reason = None

        first_utc = start.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )

        def _eur(r) -> float:
            return round((r.cost_flat_eur or 0.0) - (r.cost_shifted_eur or 0.0), 4)

        kamer, kamer_uit = self._room_forecast(
            first_utc,
            per_stap(fc_temps),
            per_stap(fc_solar),
            r_p.flat,
            r_p.shifted,
        )

        return {
            "datum": start.date().isoformat(),
            "methode": PRICE_SHIFT_METHOD,
            "tariefbron": self._mode,
            "venster_uren": venster_uren,
            "stap_minuten": DEMAND_SHIFT_STEP_MINUTES,
            "gamma": gamma_p,
            "kosten_vlak_eur": r_p.cost_flat_eur,
            "besparing_eur": _eur(r_p),
            "verlies_eur": r_p.drift_loss_eur,
            "stroom_vlak_kwh": r_p.elec_flat_kwh,
            "stroom_verschoven_kwh": r_p.elec_shifted_kwh,
            "drift_k": r_p.worst_drift_k,
            "driftmodel": self._drift_model,
            "cop_met_aanvoer": r_p.supply_cop_applied,
            "overshoot_k": r_p.peak_drift_k,
            "uren_boven_plafond": r_p.hours_above_ceiling,
            "minimumvermogen": r_p.min_power_applied,
            "uren_onder_minimum": r_p.hours_below_min,
            "gamma_alleen_cop": gamma_c,
            "besparing_alleen_cop_eur": _eur(r_c),
            # Alleen voor de voorvertoning, net als "_uren".
            "_kamer": kamer_uit,
            # Per kwartier, alleen voor de voorvertoning; gaat niet de store in.
            # De sleutel heet historisch "uren": eigen dashboards lezen hem.
            # Met tijdstempel, zodat het dashboard er een tijdas van kan maken.
            # Doorgeteld in UTC, net als de prijsreeks: in een DST-nacht klopt
            # uur-rekenwerk op lokale tijd niet.
            "_uren": [
                {
                    "tijd": (
                        first_utc + timedelta(minutes=DEMAND_SHIFT_STEP_MINUTES * i)
                    ).isoformat(),
                    "prijs": round(p, 4),
                    "vlak_w": round(f),
                    "verschoven_w": round(v),
                    "min_w": round(m) if m is not None else None,
                    "kamer_vlak_c": kamer.flat[i] if kamer.flat else None,
                    "kamer_verschoven_c": kamer.shifted[i] if kamer.shifted else None,
                }
                for i, (p, f, v, m) in enumerate(
                    zip(prices, r_p.flat, r_p.shifted, min_power or [None] * len(prices))
                )
            ],
        }

    def _room_forecast(
        self,
        first_utc: datetime,
        temps: list[float],
        solar: list[float],
        flat: list[float],
        shifted: list[float],
    ) -> tuple[Any, dict]:
        """Kamertemperatuur volgens het 2R2C, plus de samenvatting ervan.

        Alleen met een fit die de poort van de modelvergelijking haalde. Is
        het 2R2C goedgekeurd voor gebruik, dan met dezelfde parameters als de
        driftgrens (``params_use``), zodat de getoonde afwijking en de grens
        uit één model komen. Anders de fit van de poort, ter informatie; de
        driftgrens rekent dan met het 1R1C (``driftmodel``). Een fout hier
        mag de dagmeting niet meenemen: dit is een grafiek, de
        besparingsteller loopt erlangs.
        """
        from .analysis.shift_room_temp import RoomForecast, forecast_room_temperature
        from .analysis.thermal_model_2r2c import TwoStateParams

        result = self._benchmark.result
        approved = _approved_for_use(result, dt_util.utcnow().timestamp())
        if result is None:
            reden = "modelvergelijking nog niet gemeten"
        elif not approved and (not result.accepted or not result.params):
            reden = "2R2C voorspelt nog niet beter dan het huidige model"
        else:
            try:
                kamer = forecast_room_temperature(
                    TwoStateParams.from_dict(
                        result.params_use if approved else result.params),
                    self._mpc.highres_rows(
                        first_utc.timestamp() - (TRACK_WINDOW_HOURS + 1) * 3600),
                    first_utc.timestamp(),
                    temps,
                    solar,
                    flat,
                    shifted,
                    DEMAND_SHIFT_STEP_MINUTES / 60,
                )
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
                _LOGGER.warning("Kamertemperatuur doorrekenen mislukt", exc_info=True)
                kamer = RoomForecast(reason="doorrekenen mislukt")
            if kamer.reason is None:
                verschil = [s - f for f, s in zip(kamer.flat, kamer.shifted)]
                return kamer, {
                    "model": "2R2C",
                    "goedgekeurd": approved,
                    "fout_12u_k": result.rmse_k,
                    "start_c": kamer.t_indoor0,
                    "massa_c": kamer.t_mass0,
                    "afwijking_min_k": round(min(verschil), 2),
                    "afwijking_max_k": round(max(verschil), 2),
                    "reden": None,
                }
            reden = kamer.reason
        return RoomForecast(reason=reden), {"model": "2R2C", "reden": reden}

    # -- lifecycle ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        # Bewust niet de listeners van QuattHeatDemandSensor: die schrijven bij
        # elke buitentemperatuur, en deze sensor verandert één keer per uur.
        await super(QuattHeatDemandSensor, self).async_added_to_hass()

        stored = await self._store.async_load() or {}
        # Dagen van een eerdere rekenwijze tellen niet mee; zie PRICE_SHIFT_METHOD.
        # Ze verdwijnen bij de eerstvolgende opslag ook uit de store.
        self._days = [
            d
            for d in stored.get("dagen", [])
            if d.get("methode", 1) == PRICE_SHIFT_METHOD
        ]
        self._loaded = True

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._handle_daily,
                hour=PRICE_SHIFT_RUN_HOUR,
                minute=PRICE_SHIFT_RUN_MINUTE,
                second=0,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_preview, timedelta(hours=1)
            )
        )
        # De MPC-sensor schrijft zijn state na elke keer dat hij de
        # weersverwachting ophaalt. Wachtte de voorvertoning daarop, dan nu
        # opnieuw rekenen in plaats van tot het volgende uur.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._mpc.entity_id], self._handle_mpc_update
            )
        )
        # De modelvergelijking meet tien minuten na een herstart en daarna één
        # keer per etmaal. Zonder dit bleef de kamertemperatuur tot het volgende
        # hele uur leeg.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._benchmark.entity_id], self._handle_preview
            )
        )
        await self._handle_preview()

    async def _handle_mpc_update(self, _event) -> None:
        # Alleen als er op de verwachting gewacht werd: de MPC-sensor schrijft
        # ook bij elke buitentemperatuur, en dan is er niets nieuws te rekenen.
        if self._reason == self._REASON_NO_FORECAST:
            await self._handle_preview()

    async def _handle_preview(self, _now=None) -> None:
        self._preview = self._evaluate(dt_util.now())
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Nieuwe analysedata: de voorvertoning meteen opnieuw rekenen.

        Zonder dit bleef na een herstart of herlaad de eerste voorvertoning een
        uur staan — en die draait vóór de analyse klaar is, dus met "geen
        warmteverliescoëfficiënt" als reden. Gezien op 27-09-2026: de sensor
        rekende om 10:20:10, de data kwam om 10:20:14.
        """
        self._preview = self._evaluate(dt_util.now())
        self.async_write_ha_state()

    async def _handle_daily(self, _now=None) -> None:
        """Leg het komende etmaal vast en tel de voorspelde besparing op."""
        self._daily_attempt = 0
        await self._async_daily_attempt()

    async def _handle_daily_retry(self, _now=None) -> None:
        await self._async_daily_attempt()

    async def _async_daily_attempt(self) -> None:
        if not self._loaded:
            return
        now = dt_util.now()
        record = self._evaluate(now)
        self._preview = record
        if record is None and self._daily_attempt < PRICE_SHIFT_RETRIES:
            # Net na een herstart ontbreken analyse of verwachting nog. Straks
            # opnieuw; het venster schuift dan een kwartier op, maar valt nog in
            # dezelfde dag.
            self._daily_attempt += 1
            self.async_on_remove(
                async_call_later(
                    self.hass,
                    PRICE_SHIFT_RETRY_DELAY,
                    self._handle_daily_retry,
                )
            )
        # Zonder warmtevraag valt er niets te verschuiven. Zo'n dag telt niet
        # mee: anders loopt het aantal meetdagen de hele zomer door op zonder
        # dat er iets gemeten is.
        if record is not None and (record["kosten_vlak_eur"] or 0) > 0:
            opgeslagen = {k: v for k, v in record.items() if not k.startswith("_")}
            self._days = [d for d in self._days if d.get("datum") != record["datum"]]
            self._days.append(opgeslagen)
            grens = (now.date() - timedelta(days=PRICE_SHIFT_KEEP_DAYS)).isoformat()
            self._days = [d for d in self._days if d.get("datum", "") >= grens]
            await self._store.async_save({"dagen": self._days})
        self.async_write_ha_state()

    # -- weergave ----------------------------------------------------------

    @property
    def native_value(self) -> float | None:
        if not self._loaded:
            return None
        return round(sum(d.get("besparing_eur", 0.0) for d in self._days), 2)

    @property
    def extra_state_attributes(self) -> dict | None:
        dagen = self._days
        kosten = sum(d.get("kosten_vlak_eur", 0.0) for d in dagen)
        besparing = sum(d.get("besparing_eur", 0.0) for d in dagen)
        alleen_cop = sum(d.get("besparing_alleen_cop_eur", 0.0) for d in dagen)
        stroom_extra = sum(
            (d.get("stroom_verschoven_kwh") or 0.0) - (d.get("stroom_vlak_kwh") or 0.0)
            for d in dagen
        )
        attrs: dict[str, Any] = {
            "tariefbron": self._mode,
            "gemeten_dagen": len(dagen),
            "eerste_dag": dagen[0]["datum"] if dagen else None,
            "kosten_vlak_eur": round(kosten, 2),
            "besparing_pct": round(100 * besparing / kosten, 2) if kosten > 0 else None,
            # Dezelfde dagen met alleen de COP-weging. Het verschil met de state
            # is wat de prijs toevoegt bovenop verschuiven naar warme uren.
            "besparing_alleen_cop_eur": round(alleen_cop, 2),
            # Positief = verschuiven kost méér stroom (goedkoop maar koud uur).
            "extra_stroom_kwh": round(stroom_extra, 1),
            "laatste_dag": dagen[-1] if dagen else None,
            # Binnen rekenwijze 3 verschilt per dag nog het driftmodel (geen,
            # 1R1C of het goedgekeurde 2R2C) en of de aanvoer-COP meetelde.
            # Het totaal telt die dagen op, net als vroeger de modelwissels
            # van het 1R1C; deze verdeling laat zien waar het uit bestaat.
            "per_model": self._per_model(dagen),
            "reden_geen_uitkomst": self._reason,
        }
        if self._preview is not None:
            attrs["komend_etmaal"] = {
                k.lstrip("_"): v for k, v in self._preview.items()
            }
        return attrs


    @staticmethod
    def _per_model(dagen: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for d in dagen:
            sleutel = (
                f"drift={d.get('driftmodel') or 'geen'};"
                f"aanvoer_cop={'ja' if d.get('cop_met_aanvoer') else 'nee'}"
            )
            g = out.setdefault(sleutel, {"dagen": 0, "besparing_eur": 0.0})
            g["dagen"] += 1
            g["besparing_eur"] = round(g["besparing_eur"] + d.get("besparing_eur", 0.0), 4)
        return out


class QuattShiftPlanSensor(SensorEntity):
    """Schaduwfase: wat het verschoven plan nú zou vragen. Stuurt niets.

    Leest de voorvertoning van de prijsverschuiving (per kwartier) en geeft
    het vermogen van dit kwartier, het vlakke vermogen ernaast, en de
    setpointverschuiving die erbij hoort: hoeveel de kamer volgens het
    huismodel boven of onder de vlakke baan komt te staan. Met het
    afgiftemodel ook de aanvoer die dat vermogen vraagt.

    Uitvoerbaar maken is een aparte stap (analyse 22-11-2026, punt 9):
    OpenQuatt in Power House neemt het vermogen als warmtevraag; een CiC kan
    met de max. aanvoertemperatuur alleen knijpen in dure uren, extra stoken in
    goedkope uren gaat daar alleen via het thermostaatsetpoint. Wat de
    firmware van OpenQuatt binnen de comfortband met de kamerfout doet, is nog
    niet getoetst.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "shift_plan"
    _attr_icon = "mdi:calendar-clock"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"komende_stappen"})

    INTERVAL = timedelta(minutes=5)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, price_sensor, mpc_sensor) -> None:
        self.hass = hass
        self._entry = entry
        self._price = price_sensor
        self._mpc = mpc_sensor
        self._attr_unique_id = f"{entry.entry_id}_shift_plan"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_plan_nu", hass=hass
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, self.INTERVAL)
        )

    async def _tick(self, _now=None) -> None:
        self.async_write_ha_state()

    def _steps(self) -> list[dict]:
        preview = getattr(self._price, "_preview", None) or {}
        return list(preview.get("_uren") or [])

    def _current(self, now: datetime | None = None) -> tuple[int, list[dict]] | None:
        steps = self._steps()
        if not steps:
            return None
        now = now or dt_util.utcnow()
        best = None
        for i, step in enumerate(steps):
            try:
                t = datetime.fromisoformat(step["tijd"])
            except (KeyError, TypeError, ValueError):
                continue
            if t <= now:
                best = i
        if best is None:
            return None
        start = datetime.fromisoformat(steps[best]["tijd"])
        if now - start > timedelta(minutes=DEMAND_SHIFT_STEP_MINUTES):
            return None
        return best, steps

    @property
    def native_value(self) -> int | None:
        cur = self._current()
        if cur is None:
            return None
        i, steps = cur
        return steps[i].get("verschoven_w")

    @property
    def extra_state_attributes(self) -> dict:
        cur = self._current()
        if cur is None:
            return {"plan": False, "reden": getattr(self._price, "_reason", None)
                    or "geen plan voor dit kwartier"}
        i, steps = cur
        step = steps[i]
        vlak, verschoven = step.get("vlak_w"), step.get("verschoven_w")
        kv, kf = step.get("kamer_verschoven_c"), step.get("kamer_vlak_c")
        emitter = getattr(self._mpc, "emitter", None)
        aanvoer = None
        if emitter is not None and verschoven:
            t_room = self._mpc._resolve_room_setpoint()[0]
            aanvoer = round(emitter.supply(float(verschoven), t_room), 1)
        return {
            "plan": True,
            "stap": step.get("tijd"),
            "vlak_w": vlak,
            "verschil_w": (verschoven - vlak) if vlak is not None and verschoven is not None else None,
            # Hoeveel het kamersetpoint zou moeten schuiven om dit plan te
            # volgen: de kamer volgens het plan min de vlakke baan.
            "setpoint_verschuiving_k": (
                round(kv - kf, 2) if kv is not None and kf is not None else None),
            "aanvoer_plan_c": aanvoer,
            "prijs": step.get("prijs"),
            "komende_stappen": [
                {"tijd": s.get("tijd"), "verschoven_w": s.get("verschoven_w"),
                 "vlak_w": s.get("vlak_w")}
                for s in steps[i:i + 16]
            ],
        }


class QuattChMaxWaterSensor(SensorEntity, RestoreEntity):
    """Diagnostische sensor: laatste waarde + tijdstip van chMaxWaterTemperatuur schrijfactie.

    De controller begint na elke herstart of reload met een lege geschiedenis
    en schrijft pas bij de eerste tick, tot een interval later. Tot die tijd
    toont de sensor de vorige waarde uit de restore-state in plaats van
    Unknown. Unknown verborg de dashboardkaart, terwijl de regelaar al die tijd
    gewoon op die vorige waarde stond. De controller zelf krijgt de herstelde
    waarde bewust níét: na een reload kunnen bron of marge gewijzigd zijn, en
    dan hoort de eerste tick onvoorwaardelijk te schrijven.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "ch_max_water_setting"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-high"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ch_max_water_setting"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_max_aanvoertemperatuur_instelling",
            hass=hass,
        )
        self._restored_value: float | None = None
        self._restored_at: str | None = None

    async def async_added_to_hass(self) -> None:
        """Haal de laatst geschreven waarde terug van vóór de herstart."""
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        try:
            self._restored_value = float(last_state.state)
        except (TypeError, ValueError):
            # unknown/unavailable: niets om terug te zetten.
            return
        self._restored_at = (last_state.attributes or {}).get("last_written_at")

    @property
    def _controller(self):
        from .ch_max_water import ChMaxWaterController
        return self.hass.data.get(DOMAIN, {}).get(f"{self._entry.entry_id}_ch_max_water")

    @property
    def native_value(self) -> float | None:
        ctrl = self._controller
        if ctrl is not None and ctrl.last_written is not None:
            return ctrl.last_written
        return self._restored_value

    @property
    def extra_state_attributes(self) -> dict | None:
        ctrl = self._controller
        if ctrl is None:
            return None
        # Vóór de eerste schrijfactie is ``target_entity`` nog leeg; dan alsnog
        # resolven, zodat de kaart meteen na inschakelen iets te tonen heeft.
        limit_entity = ctrl.target_entity or ctrl._resolve_number_entity()
        written_now = ctrl.last_written is not None
        return {
            "last_written_at": (
                ctrl.last_written_at.isoformat()
                if written_now and ctrl.last_written_at
                else self._restored_at
            ),
            # True zolang de waarde van vóór de herstart komt en de controller
            # in deze sessie nog niets geschreven heeft.
            "hersteld": not written_now and self._restored_value is not None,
            "source": ctrl._source,
            # De naam van die bron voor de kaart; ``source`` blijft de interne
            # waarde, zodat automations er niet op breken.
            "bron_naam": SOURCE_NAMES.get(ctrl._source, ctrl._source),
            "source_entity": ctrl.source_entity,
            # Graden bovenop het advies. Zonder dit veld lijkt een limiet die
            # twee graden boven het advies staat een fout in de schrijfactie.
            "margin_c": ctrl.margin,
            # Naar wélke knop geschreven is. Zonder dit is van buitenaf niet te
            # zien of de schrijfactie bij de regelaar landt die ook stuurt.
            "target_entity": ctrl.target_entity,
            # De limiet die er nú op de regelaar staat, plus de knop waar hij
            # vandaan komt. Het dashboard las die knop hiervoor onder een vaste
            # naam (``number.cic_max_water_temperature``) — de ná-migratievorm,
            # dus op oudere Quatt-installaties bleef die rij leeg. De resolutie
            # hoort hier thuis, niet in de kaart.
            "limit_entity": limit_entity,
            "limit_c": (
                get_float_state(self.hass, limit_entity) if limit_entity else None
            ),
            "interval_minutes": int(ctrl._interval.total_seconds() // 60),
        }


class QuattSoundLevelSensor(SensorEntity):
    """Sensor met het actieve geluidsniveau — spiegelt current_level van de compensatie-switch.

    De switch publiceert zijn niveau via een dispatcher-signaal en legt de
    laatste waarde vast in hass.data. Zo hangt deze sensor niet aan een vaste
    entity-ID van de switch (die de gebruiker kan hernoemen) en maakt het niet
    uit welke van de twee platforms als eerste wordt opgezet.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "sound_level"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_sound_level_sensor"
        self._attr_device_info = get_device_info(entry.entry_id)
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, "quatt_warmteanalyse_geluidsniveau", hass=hass
        )
        self._level: str | None = None

    @property
    def state(self) -> str | None:
        return self._level

    async def async_added_to_hass(self) -> None:
        self._level = self.hass.data.get(DOMAIN, {}).get(
            f"{self._entry_id}_sound_level"
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SOUND_LEVEL.format(self._entry_id),
                self._handle_level,
            )
        )

    @callback
    def _handle_level(self, level: str) -> None:
        self._level = level
        self.async_write_ha_state()


class QuattCompressorStartsSensor(SensorEntity):
    """Compressorstarts per uur — de maat voor kortcyclen.

    De state is het aantal starts in het afgelopen uur, opgeteld over beide
    warmtepompen en bewust voortschrijdend in plaats van per kalenderdag. Een
    teller die om middernacht op nul gaat zegt om half één niets, terwijl juist
    de nacht — lage vraag, hoge aanvoertemperatuur — de periode is waarin het
    kortcyclen begint.

    Beide units tellen apart mee. Een duo verdeelt het werk over beide units,
    dus ze starten en stoppen onafhankelijk; wie alleen hp1 volgt mist de
    starts van hp2. Op een solo blijft de tweede tracker leeg.

    Als grafiek naast de buitentemperatuur beantwoordt deze sensor de vraag
    waarvoor hij bestaat: veel starts terwijl het buiten niet warm is, betekent
    dat de warmtepomp meer levert dan het huis vraagt en zichzelf uitzet. Dan
    staat de stooklijn te hoog.

    De geschiedenis wordt in een eigen store bewaard. De recorder gooit ruwe
    states na tien dagen weg, en de vraag of een ingreep geholpen heeft
    beantwoord je door twee koudeperioden te vergelijken die maanden uit elkaar
    liggen.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "compressor_starts"
    _attr_icon = "mdi:restart"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "starts/uur"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_compressor_starts"
        # Entity-ID vastpinnen — zie de toelichting bij MirrorSpec.slug.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            f"{ENTITY_PREFIX}_compressorstarts",
            hass=hass,
        )
        self._attr_device_info = get_device_info(entry.entry_id)
        self._store = Store(
            hass, COMPRESSOR_STORAGE_VERSION, COMPRESSOR_STORAGE_KEY
        )
        # Eén tracker per unit. Ze delen niets: een start van hp2 terwijl hp1
        # draait is een eigen start, geen voortzetting.
        self._trackers: dict[str, CycleTracker] = {
            ROLE_COMPRESSOR: CycleTracker(),
            ROLE_COMPRESSOR_2: CycleTracker(),
        }
        self._loaded = False

    def starts_between(self, since: datetime, until: datetime) -> int | None:
        """Starts van beide units samen in [since, until); None vóór het laden.

        Voor het sturingseffect, dat per kalenderdag telt.
        """
        if not getattr(self, "_loaded", False):
            return None
        return sum(t.starts_between(since, until) for t in self._trackers.values())

    # -- bron --------------------------------------------------------------

    def _source_for(self, role: str) -> str | None:
        cfg = {**self._entry.data, **self._entry.options}
        conf_key = (
            CONF_COMPRESSOR_ENTITY
            if role == ROLE_COMPRESSOR
            else CONF_COMPRESSOR_2_ENTITY
        )
        return async_source_entity(
            self.hass, self._entry.entry_id, role,
            config=cfg, conf_key=conf_key,
        )

    @property
    def _source_entity(self) -> str | None:
        return self._source_for(ROLE_COMPRESSOR)

    def _meting_van(self, role: str) -> tuple[float | None, datetime | None]:
        """De frequentie én het moment waarop die waarde ging gelden.

        De tijd hoort erbij omdat de tracker beurtgrenzen zet op het tijdstip
        van de meting, niet van het verwerken. ``last_changed`` is precies dat:
        het moment waarop de bron van waarde wisselde. Zonder die tijd werd een
        beurt afgemeten vanaf het moment dat wij ernaar keken, en dat is bij een
        late melding — na een herstart, of op de tick in plaats van op een
        state-change — te laat.

        Het blijft een ondergrens: moduleert de compressor van 30 naar 45 Hz,
        dan schuift ``last_changed`` mee. Zagen we de beurt daarvóór al, dan
        maakt dat niets uit; is deze meting de eerste, dan telt de beurt vanaf
        de modulatie. Beter dan de waarnemingstijd, niet perfect.
        """
        entity_id = self._source_for(role)
        if not entity_id:
            return None, None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None, None
        try:
            return float(state.state), getattr(state, "last_changed", None)
        except (TypeError, ValueError):
            return None, None

    # -- lifecycle ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        stored = await self._store.async_load() or {}
        for role, sleutel in _COMPRESSOR_STORE_KEYS.items():
            tracker = CycleTracker.from_list(stored.get(sleutel))
            tracker.prune(dt_util.utcnow())
            self._trackers[role] = tracker
        self._loaded = True

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass,
                    self._entry.entry_id,
                    (ROLE_COMPRESSOR, ROLE_COMPRESSOR_2),
                ),
                self._handle_state_change,
            )
        )
        # Ook zonder toestandswisseling opnieuw rekenen: het uursvenster
        # schuift door, dus zonder tik blijft de state hangen op het aantal van
        # het moment waarop de compressor voor het laatst iets deed.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, COMPRESSOR_REFRESH_INTERVAL
            )
        )
        self.async_write_ha_state()

    async def _handle_state_change(self, event) -> None:
        await self._async_process()

    async def _handle_tick(self, _now=None) -> None:
        await self._async_process()

    @staticmethod
    def _grenzen(tracker) -> tuple[int, bool]:
        """Vingerafdruk van de beurtgrenzen: aantal beurten en of er één loopt.

        Elke mutatie die update() kan doen verandert precies één van de twee: een
        start voegt een beurt toe, een stop sluit de lopende, en het hervatten na
        een meethiaat opent de laatste weer.
        """
        return len(tracker.runs), tracker.running

    async def _async_process(self) -> None:
        """Verwerk een meting; schrijf alleen weg als er een grens verschoof.

        Eerder ging er een opslagschrijfactie uit bij élke state-change van de
        bron. Dat viel niet op zolang de bron per beurt twee of drie keer van
        waarde wisselde, maar een bron die de modulatie meldt — OpenQuatt doet
        dat elke tien seconden — maakt daar tien tot twintig schrijfacties van,
        allemaal met identieke inhoud. Wat bewaard moet blijven zijn de
        beurtgrenzen, dus daar hangt het schrijven nu aan.
        """
        if not self._loaded:
            return
        now = dt_util.utcnow()
        gewijzigd = False
        for role, tracker in self._trackers.items():
            voor = self._grenzen(tracker)
            waarde, gemeten_op = self._meting_van(role)
            tracker.update(waarde, gemeten_op or now)
            gewijzigd |= self._grenzen(tracker) != voor
            # Opruimen en uitlezen gaan wél op de echte klok: het venster van
            # "laatste 24 uur" hangt aan nu, niet aan wanneer de bron voor het
            # laatst iets deed.
            tracker.prune(now)
        if gewijzigd:
            await self._store.async_save(
                {
                    sleutel: self._trackers[role].to_list()
                    for role, sleutel in _COMPRESSOR_STORE_KEYS.items()
                }
            )
        self.async_write_ha_state()

    # -- weergave ----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._source_entity is not None

    def _sum(self, fn) -> int:
        return sum(fn(t) for t in self._trackers.values())

    @property
    def native_value(self) -> int | None:
        if not self._loaded:
            return None
        now = dt_util.utcnow()
        return self._sum(lambda t: t.starts_in_last(1, now))

    @property
    def extra_state_attributes(self) -> dict:
        now = dt_util.utcnow()
        alle = list(self._trackers.values())

        # Gemiddelde looptijd over beide units samen, gewogen naar het aantal
        # beurten. Los middelen van twee gemiddelden zou een unit die één keer
        # draaide even zwaar laten wegen als een die er vijftig deed.
        duren = [
            r.minutes
            for t in alle
            for r in t.runs
            if r.start >= now - timedelta(hours=24) and r.minutes is not None
        ]
        looptijd = round(sum(duren) / len(duren), 1) if duren else None

        starts = [t.last_start for t in alle if t.last_start]
        per_dag = [t.starts_per_day(7, now) for t in alle]

        return {
            "starts_laatste_uur": self._sum(lambda t: t.starts_in_last(1, now)),
            "starts_laatste_etmaal": self._sum(lambda t: t.starts_in_last(24, now)),
            "starts_per_dag_7d": (
                round(sum(p for p in per_dag if p is not None), 1)
                if any(p is not None for p in per_dag)
                else None
            ),
            "gemiddelde_looptijd_min": looptijd,
            "draait_nu": any(t.running for t in alle),
            "laatste_start": max(starts).isoformat() if starts else None,
            "beurten_bewaard": self._sum(lambda t: len(t.runs)),
            # Per unit, zodat zichtbaar is of er één de dienst uitmaakt. Het
            # duo neemt het werk in blokken van elkaar over, niet per start:
            # een etmaal met alles op één unit is normaal. Pas de draaiuren
            # over een week zeggen iets over de verdeling.
            "per_unit": {
                unit: {
                    "starts_laatste_etmaal": self._trackers[role].starts_in_last(
                        24, now
                    ),
                    "starts_7d": self._trackers[role].starts_in_last(24 * 7, now),
                    "draaiuren_7d": self._trackers[role].runtime_hours_in_last(
                        24 * 7, now
                    ),
                    "bron": self._source_for(role),
                }
                for unit, role in (
                    ("hp1", ROLE_COMPRESSOR),
                    ("hp2", ROLE_COMPRESSOR_2),
                )
            },
        }
