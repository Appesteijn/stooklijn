"""Sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .analysis.thermal_model import OnlineRCModel, simulate_6h
from .const import (
    CONDITION_SOLAR_FRACTION,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_INDOOR_TEMP_ENTITY,
    DEFAULT_POWER_ENTITY,
    DEFAULT_RETURN_TEMP_ENTITY,
    DEFAULT_SOLAR_ENTITY,
    DEFAULT_SUPPLY_TEMP_ENTITY,
    DEFAULT_WEATHER_ENTITY,
    DOMAIN,
    MIN_FLOW_LPH,
    MIN_HEATING_WATTS,
    NOMINAL_FLOW_LPH,
    MPC_FORECAST_HOURS,
    MPC_SUPPLY_TEMP_MAX,
    MPC_SUPPLY_TEMP_MIN,
    OPEN_METEO_FORECAST_URL,
    SOLAR_RADIATION_DEFAULT_FACTOR,
    SOLAR_TO_HEAT_FACTOR,
)
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData
from .helpers import get_device_info, get_effective_flow, get_float_state
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
        name="Heat Loss Coefficient",
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
        name="Balance Point Temperature",
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
        name="Optimal Stooklijn Slope",
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
        name="Quatt Stooklijn Slope",
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
        name="Knee Temperature",
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
        name="Average COP",
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
        name="Freezing Performance Slope",
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
        name="Gas Heat Loss Coefficient",
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
        name="Last Analysis",
        icon="mdi:clock-check-outline",
        value_fn=lambda d: (
            d.last_analysis.strftime("%Y-%m-%d") if d.last_analysis else None
        ),
    ),
    QuattSensorDescription(
        key="analysis_status",
        name="Analysis Status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.analysis_status,
        attr_fn=lambda _: None,
    ),
    QuattSensorDescription(
        key="data_stats",
        name="Data Statistieken",
        icon="mdi:database-outline",
        value_fn=lambda d: d.data_stats.get("daily_days", 0) if d.data_stats else 0,
        attr_fn=lambda d: d.data_stats if d.data_stats else None,
    ),
    QuattSensorDescription(
        key="actual_stooklijn",
        name="Actual Stooklijn Setting",
        native_unit_of_measurement="W/°C",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tune-vertical",
        value_fn=lambda d: (
            round(d.actual_stooklijn_slope, 1)
            if d.actual_stooklijn_slope is not None
            else None
        ),
        attr_fn=lambda d: {
            "intercept": d.actual_stooklijn_intercept,
        }
        if d.actual_stooklijn_slope is not None
        else None,
    ),
    QuattSensorDescription(
        key="openquatt_balance_point",
        name="OpenQuatt Balance Point",
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
    entities.append(QuattMpcSensor(coordinator, entry))

    supply_entity = DEFAULT_SUPPLY_TEMP_ENTITY
    entry_slug = entry.entry_id
    flow_entity = entry.data.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY)
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "stooklijn",
        f"sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
        supply_entity, flow_entity,
    ))
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "mpc",
        f"sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
        supply_entity, flow_entity,
    ))
    entities.append(QuattAdviceSensor(coordinator, entry))
    entities.append(QuattOpenQuattCurveSensor(coordinator, entry))

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

    _STATUS_ICONS = {
        "running": "mdi:progress-clock",
        "completed": "mdi:check-circle",
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
    _attr_name = "Geschatte Actuele COP"
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

    @property
    def _outdoor_entity(self) -> str:
        temp_entities = self._entry.data.get(CONF_TEMP_ENTITIES, [])
        return temp_entities[0] if temp_entities else "sensor.heatpump_hp1_temperature_outside"

    async def async_added_to_hass(self) -> None:
        """Register state listener for outdoor temperature."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._outdoor_entity],
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
    _attr_name = "Aanbevolen Aanvoertemperatuur"
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

    @property
    def _outdoor_entity(self) -> str:
        temp_entities = self._entry.data.get(CONF_TEMP_ENTITIES, [])
        return temp_entities[0] if temp_entities else "sensor.heatpump_hp1_temperature_outside"

    @property
    def _flow_entity(self) -> str:
        return self._entry.data.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY)

    @property
    def _return_temp_entity(self) -> str:
        return self._entry.data.get(CONF_RETURN_TEMP_ENTITY, DEFAULT_RETURN_TEMP_ENTITY)

    async def async_added_to_hass(self) -> None:
        """Register state listeners for live input sensors."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._outdoor_entity, self._flow_entity, self._return_temp_entity],
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
        t_supply = t_return + heat_demand_w / (1.16 * effective_flow)
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
ADVICE_NOMINAL_RETURN_TEMP = 28.0  # °C — typical return temp for breakpoint calc
ADVICE_STOOKGRENS_THRESHOLD = 1.0  # °C — significant difference threshold
ADVICE_VERMOGEN_THRESHOLD = 500  # W — significant difference threshold


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
    _attr_name = "MPC Aanbevolen Aanvoertemperatuur"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:brain"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mpc_recommended_supply_temp"
        self._attr_device_info = get_device_info(entry.entry_id)
        self._forecast: list[dict] = []
        self._forecast_fetched_at: float | None = None
        self._solar_radiation: list[float] = []  # uurlijkse shortwave W/m² van Open-Meteo
        # Online thermal model
        self._thermal_store = ThermalModelStore(coordinator.hass)
        self._thermal_loaded = False

    # ------------------------------------------------------------------ helpers

    @property
    def _outdoor_entity(self) -> str:
        temp_entities = self._entry.data.get(CONF_TEMP_ENTITIES, [])
        return temp_entities[0] if temp_entities else "sensor.heatpump_hp1_temperature_outside"

    @property
    def _flow_entity(self) -> str:
        return self._entry.data.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY)

    @property
    def _return_temp_entity(self) -> str:
        return self._entry.data.get(CONF_RETURN_TEMP_ENTITY, DEFAULT_RETURN_TEMP_ENTITY)

    @property
    def _solar_entity(self) -> str:
        return self._entry.data.get(CONF_SOLAR_ENTITY, DEFAULT_SOLAR_ENTITY)

    @property
    def _weather_entity(self) -> str:
        return self._entry.data.get(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY)

    @property
    def _indoor_temp_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return cfg.get(CONF_INDOOR_TEMP_ENTITY, DEFAULT_INDOOR_TEMP_ENTITY)

    @property
    def _power_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return cfg.get(CONF_POWER_ENTITY, DEFAULT_POWER_ENTITY)

    # ------------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._outdoor_entity, self._flow_entity,
                 self._return_temp_entity, self._solar_entity],
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
        # Laad thermal model + forecast direct bij opstarten
        await self._async_load_thermal_model()
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

        # Prime the model with current sensor values so the first hourly
        # update (1h from now) can already produce an RLS update instead
        # of only storing prev values.
        if model._prev_timestamp is None:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = get_float_state(self.hass, self._power_entity) or 0.0
            solar_w = get_float_state(self.hass, self._solar_entity) or 0.0
            if t_indoor is not None and t_outdoor is not None:
                model.update(t_indoor, t_outdoor, q_hp, solar_w, dt_util.utcnow())
                _LOGGER.info(
                    "RC model primed with initial values: T_in=%.1f, T_out=%.1f",
                    t_indoor, t_outdoor,
                )
            else:
                _LOGGER.warning(
                    "RC model: cannot prime, missing sensors: indoor=%s (%s), outdoor=%s (%s)",
                    t_indoor, self._indoor_temp_entity,
                    t_outdoor, self._outdoor_entity,
                )

    async def _async_hourly_update(self, _now=None) -> None:
        """Hourly: update thermal model with new measurement, then refresh forecast."""
        # Update thermal model
        if self._thermal_loaded:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = get_float_state(self.hass, self._power_entity) or 0.0
            solar_w = get_float_state(self.hass, self._solar_entity) or 0.0

            if t_indoor is not None and t_outdoor is not None:
                updated = self._thermal_store.model.update(
                    t_indoor, t_outdoor, q_hp, solar_w, dt_util.utcnow()
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

        # Refresh forecasts (previously separate timers, now combined)
        await self._async_refresh_forecast()
        await self._async_refresh_solar_radiation()

    async def _async_refresh_forecast(self, _now=None) -> None:
        """Haal hourly weersverwachting op via HA weather service."""
        try:
            result = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self._weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            entity_data = result.get(self._weather_entity, {})
            self._forecast = entity_data.get("forecast", [])
        except Exception:
            _LOGGER.debug("MPC: kon weersverwachting niet ophalen", exc_info=True)
            self._forecast = []

        self.async_write_ha_state()

    async def _async_refresh_solar_radiation(self, _=None) -> None:
        """Haal shortwave_radiation forecast op van Open-Meteo (gratis, geen API key).

        Gebruikt lat/lon uit HA config — geen handmatige instelling nodig.
        Slaat 48 uurlijkse W/m² waarden op in self._solar_radiation.
        """
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        url = OPEN_METEO_FORECAST_URL.format(lat=lat, lon=lon)
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._solar_radiation = data.get("hourly", {}).get("shortwave_radiation", [])
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
        solar_w = get_float_state(self.hass, self._solar_entity) or 0.0

        if t_outdoor is None or t_return is None:
            return None

        effective_flow = get_effective_flow(flow_lph)

        # Online RC model: use learned parameters when converged
        model = self._thermal_store.model
        if self._thermal_loaded and model.is_converged:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                q_needed = model.calc_required_power(
                    t_indoor, t_outdoor, solar_w, t_setpoint=20.0,
                )
                if q_needed <= 0:
                    return None  # no heating needed
                t_supply = t_return + q_needed / (1.16 * effective_flow)
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

        solar_gain_w = solar_w * SOLAR_TO_HEAT_FACTOR
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
        """Attribuut met 6-uurs voorspelling + huidige inputs."""
        t_outdoor = get_float_state(self.hass, self._outdoor_entity)
        t_return = get_float_state(self.hass, self._return_temp_entity)
        flow_lph = get_float_state(self.hass, self._flow_entity)
        effective_flow = get_effective_flow(flow_lph)
        solar_w = get_float_state(self.hass, self._solar_entity) or 0.0
        solar_gain_w = solar_w * SOLAR_TO_HEAT_FACTOR

        # Thermal model parameters
        model = self._thermal_store.model
        model_params = model.params
        model_source = "online" if model.is_converged else "batch_fallback"

        # Dynamische kalibratie: radiation_factor uit live solar + Open-Meteo
        now_hour = dt_util.now().hour
        radiation_factor = SOLAR_RADIATION_DEFAULT_FACTOR
        radiation_source = "default"
        if self._solar_radiation:
            rad_now = (
                self._solar_radiation[now_hour]
                if now_hour < len(self._solar_radiation)
                else 0.0
            )
            if solar_w > 50 and rad_now > 50:
                radiation_factor = (solar_w * SOLAR_TO_HEAT_FACTOR) / rad_now
                radiation_source = "calibrated"
            elif rad_now <= 0:
                radiation_source = "open_meteo_night"
            else:
                radiation_source = "open_meteo_default"

        # Build solar gain forecasts for simulate_6h
        fc_temps: list[float] = []
        fc_solar: list[float] = []
        fc_meta: list[dict] = []  # condition, datetime, shortwave per hour
        for i, point in enumerate(self._forecast[:MPC_FORECAST_HOURS]):
            fc_temp = point.get("temperature")
            if fc_temp is None:
                break
            fc_temps.append(fc_temp)
            condition = point.get("condition", "")
            if i == 0:
                fc_solar_gain = solar_w  # live measurement (raw W, not × factor)
                fc_rad_wm2 = None
            else:
                rad_idx = now_hour + i
                if self._solar_radiation and rad_idx < len(self._solar_radiation):
                    fc_rad_wm2 = self._solar_radiation[rad_idx]
                    fc_solar_gain = fc_rad_wm2 * radiation_factor
                else:
                    fc_rad_wm2 = None
                    fraction = CONDITION_SOLAR_FRACTION.get(condition, 0.3)
                    fc_solar_gain = solar_w * fraction * SOLAR_TO_HEAT_FACTOR
            fc_solar.append(fc_solar_gain)
            fc_meta.append({
                "datetime": point.get("datetime"),
                "condition": condition,
                "shortwave_wm2": fc_rad_wm2,
            })

        # Build 6-hour forecast
        forecast_out: list[dict] = []
        if model.is_converged and fc_temps:
            # Online model: forward simulation
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                sim = simulate_6h(
                    model,
                    t_indoor_now=t_indoor,
                    t_return=t_return or 28.0,
                    flow_lph=effective_flow,
                    forecast_t_outdoor=fc_temps,
                    forecast_q_solar=fc_solar,
                )
                for i, step in enumerate(sim):
                    entry = {**step, **fc_meta[i]} if i < len(fc_meta) else step
                    entry["temp_forecast"] = fc_temps[i] if i < len(fc_temps) else None
                    entry["solar_gain_w"] = round(fc_solar[i]) if i < len(fc_solar) else None
                    forecast_out.append(entry)

        if not forecast_out:
            # Fallback: batch stooklijn-based forecast
            forecast_out = self._build_batch_forecast(
                effective_flow, solar_w, solar_gain_w,
                radiation_factor, fc_temps, fc_solar, fc_meta,
            )

        # Current demand (from whichever model is active)
        raw_demand = None
        net_demand = None
        if model.is_converged and t_outdoor is not None:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                raw_demand = model.calc_required_power(
                    t_indoor, t_outdoor, 0.0, t_setpoint=20.0,
                )
                net_demand = model.calc_required_power(
                    t_indoor, t_outdoor, solar_w, t_setpoint=20.0,
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
            "solar_power_w": round(solar_w),
            "solar_gain_w": round(solar_gain_w),
            "heat_demand_w": round(raw_demand) if raw_demand is not None else None,
            "net_demand_w": round(net_demand) if net_demand is not None else None,
            "radiation_factor": round(radiation_factor, 3),
            "radiation_source": radiation_source,
            "model_source": model_source,
            **{f"model_{k}": v for k, v in model_params.items()},
            "forecast_6h": forecast_out,
        }

    def _build_batch_forecast(
        self,
        effective_flow: float,
        solar_w: float,
        solar_gain_w: float,
        radiation_factor: float,
        fc_temps: list[float],
        fc_solar: list[float],
        fc_meta: list[dict],
    ) -> list[dict]:
        """Build 6h forecast using batch stooklijn model (fallback)."""
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
            sl_slope = self.coordinator.data.actual_stooklijn_slope
            sl_intercept = self.coordinator.data.actual_stooklijn_intercept

        forecast_out: list[dict] = []
        for i, fc_temp in enumerate(fc_temps):
            fc_sg = fc_solar[i] if i < len(fc_solar) else 0.0
            fc_raw = calc_heat_demand(heat_loss.slope, heat_loss.intercept, fc_temp)
            fc_net = max(0.0, fc_raw - fc_sg)

            fc_supply = None
            if sl_slope is not None and sl_intercept is not None:
                fc_stooklijn = sl_slope * fc_temp + sl_intercept
                fc_solar_correction = fc_sg / (1.16 * effective_flow)
                fc_supply = round(
                    max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX,
                        fc_stooklijn - fc_solar_correction)), 1
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
            }
            if i < len(fc_meta):
                entry.update(fc_meta[i])
            forecast_out.append(entry)

        return forecast_out


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
        supply_temp_entity: str,
        flow_entity: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._advised_entity = advised_entity
        self._supply_temp_entity = supply_temp_entity
        self._flow_entity = flow_entity
        self._attr_unique_id = f"{entry.entry_id}_{mode}_advice_error"
        self._attr_name = (
            "MPC Fout Aanvoertemperatuur"
            if mode == "mpc"
            else "Stooklijn Fout Aanvoertemperatuur"
        )
        self._attr_device_info = get_device_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._advised_entity, self._supply_temp_entity,
                 self._flow_entity],
                self._handle_state_change,
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
        advised = get_float_state(self.hass, self._advised_entity)
        actual = get_float_state(self.hass, self._supply_temp_entity)
        if advised is None or actual is None:
            return None
        return round(advised - actual, 1)


class QuattAdviceSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Statische advies-sensor: welke parameters moet Quatt aanpassen.

    Toont het aantal aanbevolen aanpassingen als state, met gedetailleerde
    advies-attributen voor stookgrens, nominaal vermogen, en stooklijnpunten.
    Bedoeld om één keer per jaar aan Quatt door te geven.
    """

    _attr_has_entity_name = True
    _attr_name = "Quatt Advies Parameters"
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

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None

        changes = self._count_changes(data)
        if changes == 0:
            return "Instellingen optimaal"
        return f"{changes} aanpassing{'en' if changes != 1 else ''} aanbevolen"

    def _count_changes(self, data: QuattStooklijnData) -> int:
        """Tel het aantal significante afwijkingen."""
        changes = 0

        # Stookgrens
        stookgrens_cur = data.stooklijn.balance_temp_api_daily
        stookgrens_opt = data.stooklijn.balance_temp_optimal
        if (
            stookgrens_cur is not None
            and stookgrens_opt is not None
            and abs(stookgrens_cur - stookgrens_opt) > ADVICE_STOOKGRENS_THRESHOLD
        ):
            changes += 1

        # Nominaal vermogen
        vermogen_cur, vermogen_opt = self._calc_vermogen(data)
        if (
            vermogen_cur is not None
            and vermogen_opt is not None
            and abs(vermogen_cur - vermogen_opt) > ADVICE_VERMOGEN_THRESHOLD
        ):
            changes += 1

        # Stooklijn breakpoints — always recommend if heat loss model available
        if data.heat_loss_hp.slope is not None:
            changes += 1

        return changes

    def _calc_vermogen(
        self, data: QuattStooklijnData
    ) -> tuple[float | None, float | None]:
        """Bereken huidig en optimaal vermogen bij -10°C."""
        from .analysis.utils import calc_heat_demand

        # Huidig: uit de Quatt stooklijn config
        vermogen_cur = None
        if (
            data.actual_stooklijn_slope is not None
            and data.actual_stooklijn_intercept is not None
        ):
            vermogen_cur = round(
                data.actual_stooklijn_slope * -10
                + data.actual_stooklijn_intercept
            )

        # Optimaal: uit het heat loss model
        vermogen_opt = None
        if data.heat_loss_hp.slope is not None and data.heat_loss_hp.intercept is not None:
            vermogen_opt = round(
                calc_heat_demand(data.heat_loss_hp.slope, data.heat_loss_hp.intercept, -10)
            )

        return vermogen_cur, vermogen_opt

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None

        attrs: dict[str, Any] = {}

        # --- Stookgrens ---
        stookgrens_cur = data.stooklijn.balance_temp_api_daily
        stookgrens_opt = data.stooklijn.balance_temp_optimal
        attrs["stookgrens_huidig"] = (
            round(stookgrens_cur, 1) if stookgrens_cur is not None else None
        )
        attrs["stookgrens_optimaal"] = (
            round(stookgrens_opt, 1) if stookgrens_opt is not None else None
        )
        if stookgrens_cur is not None and stookgrens_opt is not None:
            diff = stookgrens_opt - stookgrens_cur
            if abs(diff) > ADVICE_STOOKGRENS_THRESHOLD:
                verb = "Verhoog" if diff > 0 else "Verlaag"
                attrs["stookgrens_advies"] = (
                    f"{verb} stookgrens van {stookgrens_cur:.1f} naar {stookgrens_opt:.1f}°C"
                )
            else:
                attrs["stookgrens_advies"] = "Stookgrens is goed ingesteld"
        else:
            attrs["stookgrens_advies"] = None

        # --- Nominaal vermogen bij -10°C ---
        vermogen_cur, vermogen_opt = self._calc_vermogen(data)
        attrs["nominaal_vermogen_huidig_w"] = vermogen_cur
        attrs["nominaal_vermogen_optimaal_w"] = vermogen_opt
        if vermogen_cur is not None and vermogen_opt is not None:
            diff = vermogen_opt - vermogen_cur
            if abs(diff) > ADVICE_VERMOGEN_THRESHOLD:
                verb = "Verhoog" if diff > 0 else "Verlaag"
                attrs["nominaal_vermogen_advies"] = (
                    f"{verb} nominaal vermogen naar {vermogen_opt} W"
                )
            else:
                attrs["nominaal_vermogen_advies"] = "Nominaal vermogen is goed ingesteld"
        else:
            attrs["nominaal_vermogen_advies"] = (
                "Vul de huidige Quatt stooklijn in bij de integratie-instellingen"
                if vermogen_cur is None
                else None
            )

        # --- Stooklijn breakpoints ---
        if data.heat_loss_hp.slope is not None and data.heat_loss_hp.intercept is not None:
            breakpoints = _calc_heating_curve_breakpoints(
                data.heat_loss_hp.slope,
                data.heat_loss_hp.intercept,
            )
            attrs["stooklijn_punten"] = breakpoints
            punten_str = ", ".join(
                f"{bp['buiten_temp']}°C→{bp['aanvoer_temp']}°C"
                for bp in breakpoints
            )
            attrs["stooklijn_advies"] = f"Stel stooklijn in op: {punten_str}"
        else:
            attrs["stooklijn_punten"] = None
            attrs["stooklijn_advies"] = None

        attrs["aantal_aanpassingen"] = self._count_changes(data)
        return attrs


class QuattOpenQuattCurveSensor(
    CoordinatorEntity[QuattStooklijnCoordinator], SensorEntity
):
    """Passieve output sensor: optimale stooklijn breakpoints voor OpenQuatt.

    State = aantal breakpoints (6).  Attributen bevatten de individuele punten
    zodat HA-automations ze naar OpenQuatt number-entiteiten kunnen schrijven.
    """

    _attr_has_entity_name = True
    _attr_name = "OpenQuatt Stooklijn"
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

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None
        return len(ADVICE_BREAKPOINT_TEMPS)

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None or data.heat_loss_hp.slope is None:
            return None

        breakpoints = _calc_heating_curve_breakpoints(
            data.heat_loss_hp.slope,
            data.heat_loss_hp.intercept,
        )

        attrs: dict[str, Any] = {"breakpoints": breakpoints}
        for i, bp in enumerate(breakpoints, 1):
            attrs[f"bp_{i}_buiten"] = bp["buiten_temp"]
            attrs[f"bp_{i}_aanvoer"] = bp["aanvoer_temp"]
        return attrs
