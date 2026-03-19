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

from .const import (
    CONDITION_SOLAR_FRACTION,
    CONF_FLOW_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_RETURN_TEMP_ENTITY,
    DEFAULT_SOLAR_ENTITY,
    DEFAULT_SUPPLY_TEMP_ENTITY,
    DEFAULT_WEATHER_ENTITY,
    DOMAIN,
    MIN_FLOW_LPH,
    NOMINAL_FLOW_LPH,
    MPC_FORECAST_HOURS,
    MPC_SUPPLY_TEMP_MAX,
    MPC_SUPPLY_TEMP_MIN,
    OPEN_METEO_FORECAST_URL,
    SOLAR_RADIATION_DEFAULT_FACTOR,
    SOLAR_TO_HEAT_FACTOR,
)
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData

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
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "stooklijn",
        f"sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
        supply_entity,
    ))
    entities.append(QuattAdviceErrorSensor(
        coordinator, entry, "mpc",
        f"sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
        supply_entity,
    ))

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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }

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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }

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

    def _get_float_state(self, entity_id: str) -> float | None:
        """Read a float value from a HA entity state."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "None", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def native_value(self) -> float | None:
        """Interpolate COP from scatter data at current outdoor temperature."""
        if self.coordinator.data is None:
            return None
        cop_data = self.coordinator.data.stooklijn.cop_scatter_data
        if not cop_data or len(cop_data) < 2:
            return None
        t_outdoor = self._get_float_state(self._outdoor_entity)
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
        t_outdoor = self._get_float_state(self._outdoor_entity)
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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }

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

        entities_to_track = [
            self._outdoor_entity,
            self._flow_entity,
            self._return_temp_entity,
        ]

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                entities_to_track,
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        """Recompute when any input sensor changes."""
        self.async_write_ha_state()

    def _get_float_state(self, entity_id: str) -> float | None:
        """Read a float value from a HA entity state."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "None", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def native_value(self) -> float | None:
        """Calculate recommended supply temperature."""
        if self.coordinator.data is None:
            return None

        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None:
            return None

        t_outdoor = self._get_float_state(self._outdoor_entity)
        t_return = self._get_float_state(self._return_temp_entity)
        flow_lph = self._get_float_state(self._flow_entity)

        if t_outdoor is None or t_return is None:
            return None

        effective_flow = flow_lph if (flow_lph is not None and flow_lph >= MIN_FLOW_LPH) else NOMINAL_FLOW_LPH
        heat_demand_w = max(0.0, heat_loss.slope * t_outdoor + heat_loss.intercept)
        t_supply = t_return + heat_demand_w / (1.16 * effective_flow)
        return round(t_supply, 1)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose formula inputs for transparency."""
        t_outdoor = self._get_float_state(self._outdoor_entity)
        t_return = self._get_float_state(self._return_temp_entity)
        flow_lph = self._get_float_state(self._flow_entity)

        heat_demand_w = None
        if (
            self.coordinator.data is not None
            and self.coordinator.data.heat_loss_hp.slope is not None
            and t_outdoor is not None
        ):
            heat_demand_w = round(
                max(
                    0.0,
                    self.coordinator.data.heat_loss_hp.slope * t_outdoor
                    + self.coordinator.data.heat_loss_hp.intercept,
                ),
                0,
            )

        return {
            "outdoor_temp": t_outdoor,
            "return_temp": t_return,
            "flow_lph": flow_lph,
            "heat_demand_w": heat_demand_w,
        }


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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }
        self._forecast: list[dict] = []
        self._forecast_fetched_at: float | None = None
        self._solar_radiation: list[float] = []  # uurlijkse shortwave W/m² van Open-Meteo

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

    def _get_float_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "None", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

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
                self._async_refresh_forecast,
                timedelta(hours=1),
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_refresh_solar_radiation,
                timedelta(hours=1),
            )
        )
        # Laad forecast direct bij opstarten
        await self._async_refresh_forecast()
        await self._async_refresh_solar_radiation()

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

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
        if self.coordinator.data is None:
            return None
        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None or heat_loss.balance_point is None:
            return None

        t_outdoor = self._get_float_state(self._outdoor_entity)
        t_return = self._get_float_state(self._return_temp_entity)
        flow_lph = self._get_float_state(self._flow_entity)
        solar_w = self._get_float_state(self._solar_entity) or 0.0

        if t_outdoor is None or t_return is None:
            return None

        effective_flow = flow_lph if (flow_lph is not None and flow_lph >= MIN_FLOW_LPH) else NOMINAL_FLOW_LPH
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
        if self.coordinator.data is None:
            return None
        heat_loss = self.coordinator.data.heat_loss_hp
        if heat_loss.slope is None or heat_loss.intercept is None:
            return None

        t_outdoor = self._get_float_state(self._outdoor_entity)
        t_return = self._get_float_state(self._return_temp_entity)
        flow_lph = self._get_float_state(self._flow_entity)
        effective_flow = flow_lph if (flow_lph is not None and flow_lph >= MIN_FLOW_LPH) else NOMINAL_FLOW_LPH
        solar_w = self._get_float_state(self._solar_entity) or 0.0
        solar_gain_w = solar_w * SOLAR_TO_HEAT_FACTOR

        raw_demand = (
            max(0.0, heat_loss.slope * t_outdoor + heat_loss.intercept)
            if t_outdoor is not None
            else None
        )
        net_demand = max(0.0, raw_demand - solar_gain_w) if raw_demand is not None else None

        # Dynamische kalibratie: bereken radiation_factor uit live solar + Open-Meteo uur 0.
        # Als de zon schijnt (solar_w > 50) én Open-Meteo data beschikbaar is:
        #   factor = (solaredge_W × SOLAR_TO_HEAT_FACTOR) / shortwave_radiation_Wm2
        # Dit geeft het effectieve warmtewinst-oppervlak gecalibreerd op het huidige huis.
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

        # Bouw 6-uurs forecast
        forecast_out: list[dict] = []
        for i, point in enumerate(self._forecast[:MPC_FORECAST_HOURS]):
            fc_temp = point.get("temperature")
            if fc_temp is None:
                continue
            condition = point.get("condition", "")
            if i == 0:
                # Uur 0: altijd live solaredge meting (meest nauwkeurig)
                fc_solar_gain = solar_gain_w
                fc_rad_wm2 = None
            else:
                rad_idx = now_hour + i
                if self._solar_radiation and rad_idx < len(self._solar_radiation):
                    fc_rad_wm2 = self._solar_radiation[rad_idx]
                    fc_solar_gain = fc_rad_wm2 * radiation_factor
                else:
                    # Fallback: condition-fractie × huidige solar
                    fc_rad_wm2 = None
                    fraction = CONDITION_SOLAR_FRACTION.get(condition, 0.3)
                    fc_solar_gain = solar_w * fraction * SOLAR_TO_HEAT_FACTOR
            fc_raw = max(0.0, heat_loss.slope * fc_temp + heat_loss.intercept)
            fc_net = max(0.0, fc_raw - fc_solar_gain)
            fc_supply = None
            if t_return is not None:
                fc_flow = effective_flow  # gebruikt fallback van 800 L/h als HP uit staat
                fc_supply = round(
                    max(MPC_SUPPLY_TEMP_MIN, min(MPC_SUPPLY_TEMP_MAX,
                        t_return + fc_net / (1.16 * fc_flow))), 1
                )
            forecast_out.append({
                "hour": i,
                "datetime": point.get("datetime"),
                "condition": condition,
                "temp_forecast": fc_temp,
                "shortwave_wm2": fc_rad_wm2,
                "solar_gain_w": round(fc_solar_gain),
                "heat_demand_w": round(fc_raw),
                "net_demand_w": round(fc_net),
                "supply_temp": fc_supply,
            })

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
            "forecast_6h": forecast_out,
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
        supply_temp_entity: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._advised_entity = advised_entity
        self._supply_temp_entity = supply_temp_entity
        self._attr_unique_id = f"{entry.entry_id}_{mode}_advice_error"
        self._attr_name = (
            "MPC Fout Aanvoertemperatuur"
            if mode == "mpc"
            else "Stooklijn Fout Aanvoertemperatuur"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._advised_entity, self._supply_temp_entity],
                self._handle_state_change,
            )
        )

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

    def _get_float_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "None", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def native_value(self) -> float | None:
        advised = self._get_float_state(self._advised_entity)
        actual = self._get_float_state(self._supply_temp_entity)
        if advised is None or actual is None:
            return None
        return round(advised - actual, 1)
