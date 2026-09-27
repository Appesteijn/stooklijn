"""Sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
import logging
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
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .analysis.model_benchmark import Comparison, compare
from .analysis.thermal_model import (
    OnlineRCModel,
    simulate_coast_time,
    simulate_forward,
    stored_heat_kwh,
)
from homeassistant.helpers.storage import Store

from .const import (
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
    PRICE_MODE_DYNAMIC,
    PRICE_MODE_NORMAL_DAL,
    PRICE_MODE_OFF,
    PRICE_SHIFT_KEEP_DAYS,
    PRICE_SHIFT_MIN_FORECAST_FRACTION,
    PRICE_SHIFT_RETRIES,
    PRICE_SHIFT_RETRY_DELAY,
    PRICE_SHIFT_RUN_HOUR,
    PRICE_SHIFT_RUN_MINUTE,
    PRICE_SHIFT_STORAGE_KEY,
    PRICE_SHIFT_STORAGE_VERSION,
    CONF_CH_MAX_WATER_ENABLED,
    CONF_COMFORT_FLOOR_TEMP,
    CONF_COMPRESSOR_2_ENTITY,
    CONF_COMPRESSOR_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_QUATT_CLOUD_ENABLED,
    FORECAST_RETRY_DELAYS,
    DEFAULT_QUATT_CLOUD_ENABLED,
    CONF_POWER_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_ROOM_SETPOINT_ENTITY,
    CONF_ROOM_SETPOINT_FALLBACK,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    COAST_MAX_HOURS,
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
    MIN_HEATING_WATTS,
    NOMINAL_FLOW_LPH,
    MPC_FORECAST_HOURS,
    MPC_SUPPLY_TEMP_MAX,
    MPC_SUPPLY_TEMP_MIN,
    OPEN_METEO_FORECAST_URL,
    SOLAR_FORECAST_MAX_AGE_SECONDS,
    ROOM_SETPOINT_MAX,
    ROOM_SETPOINT_MIN,
    SIGNAL_SOUND_LEVEL,
    SOLAR_RADIATION_DEFAULT_FACTOR,
)
from .discovery import (
    ROLE_COMPRESSOR,
    ROLE_COMPRESSOR_2,
    ROLE_FLOW_RATE,
    ROLE_RETURN_TEMP,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_ROOM_SETPOINT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData
from .helpers import get_device_info, get_effective_flow, get_float_state
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
        value_fn=lambda d: (
            round(d.stooklijn.knee_temperature, 2)
            if d.stooklijn.knee_temperature
            else None
        ),
        attr_fn=lambda d: {
            "knee_power": d.stooklijn.knee_power,
        }
        if d.stooklijn.knee_temperature
        else None,
    ),
    QuattSensorDescription(
        key="average_cop",
        translation_key="average_cop",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
        value_fn=lambda d: round(d.average_cop, 2) if d.average_cop else None,
        attr_fn=lambda d: {
            "cop_scatter_data": d.stooklijn.cop_scatter_data,
        }
        if d.stooklijn.cop_scatter_data
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
    # Deelt hetzelfde model en meetlogboek; scoort alleen, stuurt niets aan.
    entities.append(QuattModelBenchmarkSensor(coordinator, entry, mpc_sensor))

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
        entities.append(QuattPriceShiftSensor(hass, coordinator, entry, mpc_sensor))

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
    entities.append(QuattCompressorStartsSensor(hass, entry))
    entities.append(QuattSourceOverviewSensor(hass, entry, registry))

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

    Interpolates from the historically measured COP scatter data.
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
        """Interpolate COP from scatter data at current outdoor temperature."""
        if self.coordinator.data is None:
            return None
        cop_data = self.coordinator.data.stooklijn.cop_scatter_data
        if not cop_data or len(cop_data) < 2:
            return None
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        if t_outdoor is None:
            return None

        import numpy as np  # noqa: PLC0415

        cop_sorted = sorted(cop_data, key=lambda p: p["temp"])
        temps = [p["temp"] for p in cop_sorted]
        cops = [p["cop"] for p in cop_sorted]
        return round(float(np.interp(t_outdoor, temps, cops)), 2)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose inputs for transparency."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        cop_data = (self.coordinator.data.stooklijn.cop_scatter_data if self.coordinator.data else None) or []
        return {
            "outdoor_temp": t_outdoor,
            "data_points": len(cop_data),
        }


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

    async def async_added_to_hass(self) -> None:
        """Register state listeners for live input sensors."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id,
                    (ROLE_OUTDOOR_TEMP, ROLE_FLOW_RATE, ROLE_RETURN_TEMP),
                ),
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        """Recompute when any input sensor changes."""
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
        t_return = get_float_state(self.hass, self._return_temp_entity)
        flow_lph = get_float_state(self.hass, self._flow_entity)

        if t_outdoor is None or t_return is None:
            return None

        from .analysis.utils import calc_heat_demand
        effective_flow = get_effective_flow(flow_lph)
        heat_demand_w = calc_heat_demand(heat_loss.slope, heat_loss.intercept, t_outdoor)
        if heat_demand_w <= 0:
            # Boven het balanspunt valt er niets te adviseren. Teruggeven van de
            # retourtemperatuur suggereert een advies dat er niet is.
            return None
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
        if (
            self.coordinator.data is not None
            and self.coordinator.data.heat_loss_hp.slope is not None
            and t_outdoor is not None
        ):
            from .analysis.utils import calc_heat_demand
            heat_demand_w = round(
                calc_heat_demand(
                    self.coordinator.data.heat_loss_hp.slope,
                    self.coordinator.data.heat_loss_hp.intercept,
                    t_outdoor,
                ),
                0,
            )

        return {
            "outdoor_temp": t_outdoor,
            "return_temp": t_return,
            "flow_lph": flow_lph,
            "heat_demand_w": heat_demand_w,
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
) -> list[dict]:
    """Bereken optimale aanvoertemperatuur bij standaard buitentemperaturen.

    Gebruikt het heat loss model om voor elke buitentemp de benodigde
    aanvoertemperatuur te berekenen. Hergebruikt door Quatt Advies en
    OpenQuatt sensoren.
    """
    from .analysis.utils import calc_heat_demand

    breakpoints = []
    for t_out in outdoor_temps:
        demand = calc_heat_demand(heat_loss_slope, heat_loss_intercept, t_out)
        t_supply = t_return_nominal + demand / (1.16 * flow_nominal)
        t_supply = max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, t_supply))
        breakpoints.append({
            "buiten_temp": t_out,
            "aanvoer_temp": round(t_supply, 1),
        })
    return breakpoints


def _calc_mpc_supply_temp(
    heat_loss_slope: float,
    heat_loss_intercept: float,
    balance_point: float,
    t_outdoor: float,
    t_return: float,
    flow_lph: float,
    solar_gain_w: float,
) -> float | None:
    """Bereken MPC aanvoertemperatuur.

    warmtevraag = UA × max(0, T_balance - T_buiten) − Q_zon
    T_aanvoer   = T_retour + max(0, warmtevraag) / (1.16 × debiet)
    """
    if flow_lph < MIN_FLOW_LPH:
        return None
    raw_demand = heat_loss_slope * t_outdoor + heat_loss_intercept
    net_demand = max(0.0, raw_demand - solar_gain_w)
    if net_demand <= 0:
        # Geen warmtevraag, dus geen aanvoeradvies. Zonder deze afslag zou de
        # ondergrens hieronder een advies van 20 °C tonen terwijl er niets te
        # adviseren valt — precies zoals de foutsensoren zwijgen bij stilstand.
        return None
    t_supply = t_return + net_demand / (1.16 * flow_lph)
    return max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, t_supply))


class QuattMpcSensor(CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity):
    """Shadow-mode MPC sensor: aanbevolen aanvoertemperatuur op basis van
    weersvoorspelling + zonnewinst.

    Schrijft NIKS naar OTGW of klimaat-entiteiten — puur observatie voor
    vergelijking met de huidige stooklijn.

    Verversing:
    - Weersverwachting: elke uur via timer
    - Aanvoertemp: bij elke state-change van buitentemp / solar / flow / retour
    """

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
    _unrecorded_attributes = frozenset({"forecast"})

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
        self._thermal_loaded = False

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
    def _room_setpoint_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_ROOM_SETPOINT,
            config=cfg, conf_key=CONF_ROOM_SETPOINT_ENTITY,
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
        """Straling (W/m²) op het hele uur van ``moment``, of None als onbekend.

        Zoekt op tijdstempel in ``hourly.time``, niet op uur-index. Open-Meteo
        levert die reeks in de tijdzone die we zelf hebben meegegeven, dus een
        gelijke stempel is per definitie hetzelfde uur — ook op de dagen dat er
        een uur verdwijnt of dubbel voorkomt.
        """
        if not self._solar_forecast_is_fresh:
            return None
        stamp = moment.astimezone(dt_util.now().tzinfo).strftime("%Y-%m-%dT%H:00")
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
    def highres_samples(self) -> list[list[float]]:
        """Momentopname van het meetlogboek, voor analyse buiten de event loop.

        Een kopie van de lijst, geen kopie van de rijen — die worden na het
        vastleggen niet meer aangeraakt, dus een executor-thread mag er
        rustig overheen lopen terwijl er hier nieuwe monsters bij komen.
        """
        if not self._highres_loaded:
            return []
        return list(self._highres_store.log.to_dict()["samples"])

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
        t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
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
        uren-vanaf-nu en vallen voor gat-uren terug op de huidige buitentemp.
        Gedeeld door de MPC-sensor en de coast-time sensor.
        """
        now_utc = dt_util.utcnow()
        now_local = dt_util.now()

        # Build time-indexed lookup: hours_from_now -> forecast point
        fc_lookup: dict[int, dict] = {}
        for point in self._forecast:
            dt_str = point.get("datetime")
            if dt_str:
                try:
                    fc_dt = datetime.fromisoformat(dt_str)
                    hours_ahead = round((fc_dt - now_utc).total_seconds() / 3600)
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
        await self._async_refresh_forecast()
        await self._async_refresh_solar_radiation()

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

    async def _async_load_thermal_model(self) -> None:
        """Load persisted thermal model and initialise from batch if needed."""
        await self._thermal_store.async_load()
        self._thermal_loaded = True

        # If model has no updates yet, seed with batch heat loss coefficient
        model = self._thermal_store.model
        if model._rls.n_updates == 0 and self.coordinator.data:
            heat_loss = self.coordinator.data.heat_loss_hp
            if heat_loss.slope is not None:
                # heat_loss.slope is negative (W per °C increase),
                # the heat loss coefficient U = -slope
                model.initialise_from_batch(-heat_loss.slope)
        self._refresh_u_prior()

        # Prime the model with current sensor values so the first hourly
        # update (1h from now) can already produce an RLS update instead
        # of only storing prev values.
        if model._prev_timestamp is None:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = get_float_state(self.hass, self._power_entity) or 0.0
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

    async def _async_highres_sample(self, _now=None) -> None:
        """Leg elke vijf minuten een monster vast.

        Dit voedt het model niet — het is grondstof voor latere identificatie
        van de snelle tijdconstante, die op uurdata niet te bepalen is. Zie
        ``highres_log`` voor waarom dat een eigen logboek vraagt.
        """
        if not self._highres_loaded:
            return
        t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        q_solar_wm2 = self._get_current_solar_radiation_wm2()
        # Zelfde afweging als bij het model: een verzonnen nul voor de zon
        # maakt het monster onbruikbaar voor identificatie, en dan is
        # overslaan eerlijker dan bewaren.
        if t_indoor is None or t_outdoor is None or q_solar_wm2 is None:
            return
        q_hp = get_float_state(self.hass, self._power_entity) or 0.0
        self._highres_store.record(
            dt_util.utcnow(), t_indoor, t_outdoor, q_hp, q_solar_wm2
        )

    def _refresh_u_prior(self) -> None:
        """Keep the RC model's U anchor in step with the batch regression.

        Not a one-off seed: the seasonal fit keeps improving as its window
        grows, and the anchor should follow it. Without this the anchor would
        freeze on whatever the regression happened to say the first time the
        model was loaded.
        """
        if not self._thermal_loaded or not self.coordinator.data:
            return
        slope = self.coordinator.data.heat_loss_hp.slope
        if slope is not None:
            self._thermal_store.model.set_u_prior(-slope)

    async def _async_hourly_update(self, _now=None) -> None:
        """Hourly: update thermal model with new measurement, then refresh forecast."""
        # Update thermal model
        if self._thermal_loaded:
            self._refresh_u_prior()
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = get_float_state(self.hass, self._power_entity) or 0.0
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
                updated = self._thermal_store.model.update(
                    t_indoor, t_outdoor, q_hp, q_solar_wm2, dt_util.utcnow()
                )
                if updated:
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
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = get_float_state(self.hass, self._return_temp_entity)
        flow_lph = get_float_state(self.hass, self._flow_entity)

        if t_outdoor is None or t_return is None:
            return None

        effective_flow = get_effective_flow(flow_lph)

        # Online RC model: use learned parameters when converged
        model = self._thermal_store.model
        if self._thermal_loaded and model.is_converged:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                # Voor een advies moet er een getal staan. Onbekend telt hier als
                # geen zon: dat vraagt eerder te veel warmte dan te weinig, en dat
                # is de goede kant om op te falen. Leren gebeurt hier niet — daar
                # wordt een onbekende straling wél overgeslagen.
                q_solar_wm2 = self._get_current_solar_radiation_wm2() or 0.0
                t_setpoint, _ = self._resolve_room_setpoint()
                q_needed = model.calc_required_power(
                    t_indoor, t_outdoor, q_solar_wm2, t_setpoint=t_setpoint,
                )
                if q_needed <= 0:
                    # Kamer op of boven setpoint: geen vraag, geen advies. De
                    # ondergrens hieronder zou anders 20 °C tonen bij nul vraag.
                    return None
                t_supply = t_return + q_needed / (1.16 * effective_flow)
                # Heating branch: floor at MPC_SUPPLY_TEMP_MIN (HP is inefficient
                # below ~20°C aanvoer). COOL_MIN (15°C) is reserved for the future
                # cooling branch where aanvoer < retour.
                return round(
                    max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, t_supply)),
                    1,
                )

        # Fallback: batch heat loss model
        if self.coordinator.data is None:
            return None
        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None or heat_loss.balance_point is None:
            return None

        solar_gain_w = (self._get_current_solar_radiation_wm2() or 0.0) * SOLAR_RADIATION_DEFAULT_FACTOR
        return _calc_mpc_supply_temp(
            heat_loss.slope,
            heat_loss.intercept,
            heat_loss.balance_point,
            t_outdoor,
            t_return,
            effective_flow,
            solar_gain_w,
        )

    @property
    def extra_state_attributes(self) -> dict | None:
        """Attribuut met de vooruitblik (MPC_FORECAST_HOURS uur) + huidige inputs."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = get_float_state(self.hass, self._return_temp_entity)
        flow_lph = get_float_state(self.hass, self._flow_entity)
        effective_flow = get_effective_flow(flow_lph)
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
        # Solar gain in W: the batch fallback consumes this directly, so it must
        # keep using the default factor; display follows the active model.
        fc_solar_gain_batch_w = [
            wm2 * SOLAR_RADIATION_DEFAULT_FACTOR for wm2 in fc_solar_wm2
        ]
        fc_solar_gain_w = [wm2 * solar_factor for wm2 in fc_solar_wm2]

        # Bouw de vooruitblik over MPC_FORECAST_HOURS uur
        forecast_out: list[dict] = []
        if model.is_converged and fc_temps:
            # Online model: forward simulation (input = W/m², model applies g_solar internally)
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                sim = simulate_forward(
                    model,
                    t_indoor_now=t_indoor,
                    t_return=t_return or 28.0,
                    flow_lph=effective_flow,
                    forecast_t_outdoor=fc_temps,
                    forecast_q_solar=fc_solar_wm2,
                    # Expliciet, ook al is het de default van de functie: de hele
                    # tabel hangt hieraan, en een stil meegenomen default is
                    # precies hoe deze waarde eerder aan het zicht ontsnapte.
                    t_setpoint=room_setpoint,
                    max_hours=MPC_FORECAST_HOURS,
                    comfort_floor=self._comfort_floor,
                )
                for i, step in enumerate(sim):
                    entry = {**step, **fc_meta[i]} if i < len(fc_meta) else step
                    entry["temp_forecast"] = fc_temps[i] if i < len(fc_temps) else None
                    entry["solar_gain_w"] = round(fc_solar_gain_w[i]) if i < len(fc_solar_gain_w) else None
                    forecast_out.append(entry)

        if not forecast_out:
            # Fallback: batch stooklijn-based forecast (needs solar gain in W)
            forecast_out = self._build_batch_forecast(
                effective_flow, t_return, fc_temps, fc_solar_gain_batch_w, fc_meta,
            )

        # Current demand (from whichever model is active)
        raw_demand = None
        net_demand = None
        current_rad_wm2 = self._get_current_solar_radiation_wm2()
        if model.is_converged and t_outdoor is not None:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                raw_demand = model.calc_required_power(
                    t_indoor, t_outdoor, 0.0, t_setpoint=room_setpoint,
                )
                net_demand = model.calc_required_power(
                    t_indoor, t_outdoor, current_rad_wm2 or 0.0,
                    t_setpoint=room_setpoint,
                )
        elif self.coordinator.data is not None:
            heat_loss = self.coordinator.data.heat_loss_hp
            if heat_loss.slope is not None and t_outdoor is not None:
                from .analysis.utils import calc_heat_demand
                raw_demand = calc_heat_demand(
                    heat_loss.slope, heat_loss.intercept, t_outdoor,
                )
                net_demand = max(0.0, raw_demand - solar_gain_w)

        return {
            "outdoor_temp": t_outdoor,
            "return_temp": t_return,
            "flow_lph": flow_lph,
            "solar_gain_w": round(solar_gain_w),
            # Welke factor die winst opleverde — anders is niet te zien of je
            # naar het geleerde of het hardgecodeerde getal kijkt.
            "solar_factor_w_per_wm2": round(solar_factor, 3),
            "heat_demand_w": round(raw_demand) if raw_demand is not None else None,
            "net_demand_w": round(net_demand) if net_demand is not None else None,
            # None als de reeks ontbreekt of verouderd is — een nul zou hier
            # niet te onderscheiden zijn van een echte nacht.
            "solar_radiation_wm2": (
                round(current_rad_wm2) if current_rad_wm2 is not None else None
            ),
            "solar_forecast_fresh": self._solar_forecast_is_fresh,
            "stored_heat_kwh": stored_heat_kwh(
                model.raw_params["C"] if model.raw_params else None,
                get_float_state(self.hass, self._indoor_temp_entity),
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
        }

    def _build_batch_forecast(
        self,
        effective_flow: float,
        t_return: float | None,
        fc_temps: list[float],
        fc_solar: list[float],
        fc_meta: list[dict],
    ) -> list[dict]:
        """Vooruitblik uit de batch-stooklijn — terugval als het RC-model nog
        niet geconvergeerd is. Volgt de lengte van ``fc_temps``."""
        if self.coordinator.data is None:
            return []
        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None:
            return []

        from .analysis.utils import calc_heat_demand

        sl = self.coordinator.data.stooklijn
        if sl.slope_optimal is not None and sl.intercept_optimal is not None:
            sl_slope, sl_intercept = sl.slope_optimal, sl.intercept_optimal
        else:
            sl_slope = sl.slope_api
            sl_intercept = sl.intercept_api

        forecast_out: list[dict] = []
        for i, fc_temp in enumerate(fc_temps):
            fc_sg = fc_solar[i] if i < len(fc_solar) else 0.0
            fc_raw = calc_heat_demand(heat_loss.slope, heat_loss.intercept, fc_temp)
            fc_net = max(0.0, fc_raw - fc_sg)

            fc_supply = None
            fc_supply_no_solar = None
            if sl_slope is not None and sl_intercept is not None and t_return is not None:
                fc_sl_demand = max(0.0, sl_slope * fc_temp + sl_intercept - fc_sg)
                if fc_sl_demand > MIN_HEATING_WATTS:
                    raw_supply = t_return + fc_sl_demand / (1.16 * effective_flow)
                    fc_supply = round(
                        max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, raw_supply)), 1
                    )
                fc_sl_demand_ns = max(0.0, sl_slope * fc_temp + sl_intercept)
                if fc_sl_demand_ns > MIN_HEATING_WATTS:
                    raw_supply_ns = t_return + fc_sl_demand_ns / (1.16 * effective_flow)
                    fc_supply_no_solar = round(
                        max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX, raw_supply_ns)), 1
                    )

            entry = {
                "hour": i,
                "temp_forecast": fc_temp,
                "solar_gain_w": round(fc_sg),
                "heat_demand_w": round(fc_raw),
                "net_demand_w": round(fc_net),
                "q_hp_needed_w": round(fc_net),
                "hp_needed": bool(fc_net > MIN_HEATING_WATTS),
                "supply_temp": fc_supply,
                "supply_temp_no_solar": fc_supply_no_solar,
            }
            if i < len(fc_meta):
                entry.update(fc_meta[i])
            forecast_out.append(entry)

        return forecast_out


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
        """Run the free-cooldown simulation, or None if the model isn't ready."""
        model = self._mpc.thermal_model
        if model is None or not model.is_converged:
            return None

        t_indoor = get_float_state(self.hass, self._mpc._indoor_temp_entity)
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

        return simulate_coast_time(
            model,
            t_indoor_now=t_indoor,
            comfort_floor=self._comfort_floor,
            forecast_t_outdoor=fc_temps,
            forecast_q_solar=fc_solar_wm2,
            step_minutes=COAST_STEP_MINUTES,
            max_hours=COAST_MAX_HOURS,
        )

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
            "model_source": "online",
            "trajectory": result["trajectory"],
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
        return get_float_state(self.hass, self._mpc._indoor_temp_entity)

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
    en horen niet in de event loop. Na een herstart staat hij op "nog niet
    gemeten" tot de eerste meting; het resultaat wordt bewust niet bewaard,
    want een score van gisteren op de data van gisteren is geen antwoord op de
    vraag van vandaag.
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
        return {"gemeten": True, **self._result.to_dict()}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
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
        self._running = True
        try:
            result = await self.hass.async_add_executor_job(compare, rows, model)
        except Exception:
            _LOGGER.warning("Modelvergelijking mislukt", exc_info=True)
            return
        finally:
            self._running = False
        self._result = result
        _LOGGER.info(
            "Modelvergelijking: 2R2C %s K, huidig %s K, niets doen %s K — %s",
            result.rmse_k, result.reference_rmse_k, result.persistence_rmse_k,
            result.reason,
        )
        self.async_write_ha_state()


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
        return round(advised - actual, 1)


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
            # De gemeten dagspreiding, en of het verschil daarbovenuit komt.
            # Zonder dit zou elke toevallige uitschieting als resultaat lezen.
            "dagspreiding_pct": perf.spread_pct,
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
ADVICE_SOURCE_OPENQUATT = "openquatt"
ADVICE_SOURCE_UNKNOWN = "onbekend"


def _calc_quatt_advice(
    data: QuattStooklijnData,
    stookgrens_setting: float | None,
    openquatt: bool,
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
    getoond; alleen een verschil met een uitgelezen instelling telt als aanpassing.
    """
    from .analysis.utils import calc_heat_demand
    from .power_house import advise_zero_power_temp

    attrs: dict[str, Any] = {}
    changes = 0
    where = "in OpenQuatt" if openquatt else "in de Quatt-app"

    # --- Stookgrens ---
    opt = data.stooklijn.balance_temp_optimal
    attrs["stookgrens_huidig"] = stookgrens_setting
    attrs["stookgrens_optimaal"] = round(opt, 1) if opt is not None else None
    attrs["stookgrens_bron"] = (
        ADVICE_SOURCE_OPENQUATT
        if stookgrens_setting is not None
        else ADVICE_SOURCE_UNKNOWN
    )
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
            )
    else:
        attrs["stookgrens_advies"] = (
            f"Aanbevolen stookgrens: {opt:.1f}°C (gemeten balanspunt van je "
            f"huis). Controleer {where} wat er nu staat."
        )

    # --- Nominaal vermogen bij -10°C ---
    hl = data.heat_loss_hp
    vermogen_opt = None
    if hl.slope is not None and hl.intercept is not None:
        vermogen_opt = round(calc_heat_demand(hl.slope, hl.intercept, -10))
    # Nooit uit te lezen; het attribuut blijft voor bestaande dashboards.
    attrs["nominaal_vermogen_huidig_w"] = None
    attrs["nominaal_vermogen_optimaal_w"] = vermogen_opt
    attrs["nominaal_vermogen_bron"] = (
        ADVICE_SOURCE_OPENQUATT if openquatt else ADVICE_SOURCE_UNKNOWN
    )
    if openquatt:
        # Power House heeft geen vermogen bij -10°C maar Tc en Pr, en die
        # hangen samen met T0 — dat rekent de kalibratiesensor als drietal uit.
        attrs["nominaal_vermogen_advies"] = (
            "OpenQuatt kent geen nominaal vermogen bij -10°C; "
            "zie de Power House-kalibratie"
        )
    elif vermogen_opt is not None:
        attrs["nominaal_vermogen_advies"] = (
            f"Aanbevolen nominaal vermogen: {vermogen_opt} W bij -10°C. "
            f"Controleer {where} wat er nu staat."
        )
    else:
        attrs["nominaal_vermogen_advies"] = None

    # --- Stooklijn breakpoints ---
    if hl.slope is not None and hl.intercept is not None:
        breakpoints = _calc_heating_curve_breakpoints(hl.slope, hl.intercept)
        attrs["stooklijn_punten"] = breakpoints
        punten_str = ", ".join(
            f"{bp['buiten_temp']}°C→{bp['aanvoer_temp']}°C"
            for bp in breakpoints
        )
        attrs["stooklijn_advies"] = f"Stel stooklijn in op: {punten_str}"
    else:
        attrs["stooklijn_punten"] = None
        attrs["stooklijn_advies"] = None

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

    def _advice(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None
        openquatt, setting = self._openquatt_stookgrens()
        return _calc_quatt_advice(data, setting, openquatt)

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
        if candidates:
            self._remove_tracker = async_track_state_change_event(
                self.hass, candidates, self._handle_source_change
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
        return get_float_state(self.hass, entity_id)

    @property
    def available(self) -> bool:
        return self._registry.active_entity(self._spec.role) is not None

    @property
    def extra_state_attributes(self) -> dict:
        source = self._registry.get(self._spec.role)
        if source is None:
            return {}
        return {
            "source_entity": source.active,
            "source_integration": source.integration,
            "candidates": list(source.candidates),
            "switched_at": (
                source.switched_at.isoformat() if source.switched_at else None
            ),
        }


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

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        mpc_sensor: "QuattMpcSensor",
    ) -> None:
        super().__init__(hass, coordinator, entry)
        self._mpc = mpc_sensor
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
        """Prijs per uur vanaf ``start``; leeg als er geen bruikbare reeks is."""
        from .analysis.tariff import hourly_prices_from_attributes, normal_dal_prices

        cfg = self._cfg
        if self._mode == PRICE_MODE_NORMAL_DAL:
            return normal_dal_prices(
                start,
                n_hours,
                float(cfg.get(CONF_PRICE_NORMAL, DEFAULT_PRICE_NORMAL)),
                float(cfg.get(CONF_PRICE_DAL, DEFAULT_PRICE_DAL)),
                int(cfg.get(CONF_DAL_START_HOUR, DEFAULT_DAL_START_HOUR)),
                int(cfg.get(CONF_DAL_END_HOUR, DEFAULT_DAL_END_HOUR)),
                bool(cfg.get(CONF_DAL_WEEKEND, DEFAULT_DAL_WEEKEND)),
                tz=start.tzinfo,
            )
        if self._mode == PRICE_MODE_DYNAMIC:
            entity_id = cfg.get(CONF_PRICE_ENTITY)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None:
                return []
            return hourly_prices_from_attributes(
                state.attributes,
                start,
                n_hours,
                unit=state.attributes.get("unit_of_measurement"),
                tz=start.tzinfo,
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
        fc_temps = fc_temps[: len(prices)]
        fc_solar = fc_solar[: len(prices)]

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
        ceiling = get_float_state(
            self.hass, openquatt.get(ROLE_PH_RATED_POWER) or ""
        ) if openquatt else None

        common = {
            "ceiling_w": ceiling,
            "thermal_mass_wh_k": c_whk,
            "max_drift_k": DEMAND_SHIFT_MAX_DRIFT_K,
            "max_overshoot_k": DEMAND_SHIFT_MAX_OVERSHOOT_K,
            "baseline": baseline,
        }
        # Nulpunt ``None``: met een basisreeks rekent demand_shift er niet mee.
        args = (fc_temps, curve, float(hlc), None)

        # Begrensde punten tellen mee: een teruggeschaalde verschuiving blijft
        # binnen de comfortgrens en is dus een geldig plan. Zie scan_gamma.
        scan_p = scan_gamma(
            *args, prices=prices, begrenzing_toegestaan=True, **common
        )
        gamma_p = scan_p.advies or 0.0
        r_p = calculate_demand_shift(*args, gamma_p, prices=prices, **common)

        # Dezelfde dag met alleen de COP-weging, geprijsd tegen dezelfde tarieven.
        scan_c = scan_gamma(*args, begrenzing_toegestaan=True, **common)
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

        return {
            "datum": start.date().isoformat(),
            "tariefbron": self._mode,
            "venster_uren": len(prices),
            "gamma": gamma_p,
            "kosten_vlak_eur": r_p.cost_flat_eur,
            "besparing_eur": _eur(r_p),
            "verlies_eur": r_p.drift_loss_eur,
            "stroom_vlak_kwh": r_p.elec_flat_kwh,
            "stroom_verschoven_kwh": r_p.elec_shifted_kwh,
            "drift_k": r_p.worst_drift_k,
            "overshoot_k": r_p.peak_drift_k,
            "uren_boven_plafond": r_p.hours_above_ceiling,
            "gamma_alleen_cop": gamma_c,
            "besparing_alleen_cop_eur": _eur(r_c),
            # Per uur, alleen voor de voorvertoning; gaat niet de store in.
            # Met tijdstempel, zodat het dashboard er een tijdas van kan maken.
            # Doorgeteld in UTC, net als de prijsreeks: in een DST-nacht klopt
            # uur-rekenwerk op lokale tijd niet.
            "_uren": [
                {
                    "tijd": (first_utc + timedelta(hours=i)).isoformat(),
                    "prijs": round(p, 4),
                    "vlak_w": round(f),
                    "verschoven_w": round(v),
                }
                for i, (p, f, v) in enumerate(
                    zip(prices, r_p.flat, r_p.shifted)
                )
            ],
        }

    # -- lifecycle ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        # Bewust niet de listeners van QuattHeatDemandSensor: die schrijven bij
        # elke buitentemperatuur, en deze sensor verandert één keer per uur.
        await super(QuattHeatDemandSensor, self).async_added_to_hass()

        stored = await self._store.async_load() or {}
        self._days = list(stored.get("dagen", []))
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
            "reden_geen_uitkomst": self._reason,
        }
        if self._preview is not None:
            attrs["komend_etmaal"] = {
                k.lstrip("_"): v for k, v in self._preview.items()
            }
        return attrs


class QuattChMaxWaterSensor(SensorEntity):
    """Diagnostische sensor: laatste waarde + tijdstip van chMaxWaterTemperatuur schrijfactie."""

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

    @property
    def _controller(self):
        from .ch_max_water import ChMaxWaterController
        return self.hass.data.get(DOMAIN, {}).get(f"{self._entry.entry_id}_ch_max_water")

    @property
    def native_value(self) -> float | None:
        ctrl = self._controller
        return ctrl.last_written if ctrl else None

    @property
    def extra_state_attributes(self) -> dict | None:
        ctrl = self._controller
        if ctrl is None:
            return None
        # Vóór de eerste schrijfactie is ``target_entity`` nog leeg; dan alsnog
        # resolven, zodat de kaart meteen na inschakelen iets te tonen heeft.
        limit_entity = ctrl.target_entity or ctrl._resolve_number_entity()
        return {
            "last_written_at": ctrl.last_written_at.isoformat() if ctrl.last_written_at else None,
            "source": ctrl._source,
            "source_entity": ctrl.source_entity,
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

    Beide units tellen apart mee. Een duo wisselt ze slim om en om af, dus ze
    starten en stoppen onafhankelijk; wie alleen hp1 volgt telt ruwweg de helft
    en ziet een installatie die om beurten kortcyclet aan voor een rustig
    draaiende. Op een solo blijft de tweede tracker leeg.

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
            # Per unit, zodat zichtbaar is of er één de dienst uitmaakt of dat
            # ze netjes afwisselen.
            "per_unit": {
                "hp1": {
                    "starts_laatste_etmaal": self._trackers[
                        ROLE_COMPRESSOR
                    ].starts_in_last(24, now),
                    "bron": self._source_for(ROLE_COMPRESSOR),
                },
                "hp2": {
                    "starts_laatste_etmaal": self._trackers[
                        ROLE_COMPRESSOR_2
                    ].starts_in_last(24, now),
                    "bron": self._source_for(ROLE_COMPRESSOR_2),
                },
            },
        }
