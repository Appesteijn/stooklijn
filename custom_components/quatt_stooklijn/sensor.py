"""Sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

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
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .analysis.thermal_model import OnlineRCModel, simulate_6h, simulate_coast_time
from .const import (
    CONF_CH_MAX_WATER_ENABLED,
    CONF_COMFORT_FLOOR_TEMP,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    COAST_MAX_HOURS,
    COAST_STEP_MINUTES,
    DEFAULT_COMFORT_FLOOR_TEMP,
    DEFAULT_SOLAR_ENTITY,
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
)
from .discovery import (
    ROLE_FLOW_RATE,
    ROLE_RETURN_TEMP,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData
from .helpers import get_device_info, get_effective_flow, get_float_state
from .heat_demand import OUTDOOR_MAX_AGE_SECONDS, SOURCE_SELECTOR_ENTITY
from .sources import (
    ENTITY_PREFIX,
    MIRROR_SPECS,
    OVERVIEW_SLUG,
    MirrorSpec,
    SourceRegistry,
    async_source_entity,
)
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
        translation_key="analysis_status",
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
    entities.append(QuattAdviceSensor(coordinator, entry))
    entities.append(QuattOpenQuattCurveSensor(coordinator, entry))
    entities.append(QuattPowerHouseCalibrationSensor(hass, coordinator, entry))
    entities.append(QuattHeatDemandSensor(hass, coordinator, entry))

    if {**entry.data, **entry.options}.get(CONF_SOUND_LEVEL_ENABLED, False):
        entities.append(QuattSoundLevelSensor(entry))

    if {**entry.data, **entry.options}.get(CONF_CH_MAX_WATER_ENABLED, False):
        entities.append(QuattChMaxWaterSensor(entry))

    # Spiegelsensoren: één stabiel entity-ID per meting, ongeacht of Quatt of
    # OpenQuatt hem levert. Dashboards horen hieraan te hangen.
    registry: SourceRegistry = hass.data[DOMAIN][f"{entry.entry_id}_sources"]
    entities.extend(
        QuattSourceMirrorSensor(hass, entry, registry, spec) for spec in MIRROR_SPECS
    )
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
# OpenQuatt hanteert een ander raster dan het generieke advies hierboven: zijn
# zes `Curve Tsupply @ …`-number-entiteiten staan vast op -20/-10/0/5/10/15.
# Die punten worden positioneel overgezet, dus een advies op het advies-raster
# schuift de koude kant een punt op: de waarde voor -10 landt dan op de knop
# voor -20. Wijzig deze reeks alleen als de firmware zijn knoppen wijzigt.
OPENQUATT_BREAKPOINT_TEMPS = (-20, -10, 0, 5, 10, 15)
ADVICE_NOMINAL_RETURN_TEMP = 28.0  # °C — typical return temp for breakpoint calc
ADVICE_STOOKGRENS_THRESHOLD = 1.0  # °C — significant difference threshold
ADVICE_VERMOGEN_THRESHOLD = 500  # W — significant difference threshold
# Warm-side regression is unreliable when the fitted balance point is above this
# temperature: only mild-weather data available, extrapolation to -10°C is invalid.
ADVICE_MAX_RELIABLE_BALANCE_TEMP = 20.0  # °C


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
    def _solar_entity(self) -> str:
        return {**self._entry.data, **self._entry.options}.get(CONF_SOLAR_ENTITY, DEFAULT_SOLAR_ENTITY)

    @property
    def _weather_entity(self) -> str:
        return {**self._entry.data, **self._entry.options}.get(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY)

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

    def _get_current_solar_radiation_wm2(self) -> float:
        """Return current hour's shortwave radiation from Open-Meteo (W/m²).

        Used as solar input for the RC model instead of PV output, because:
        - W/m² is a direct physical measure of incoming solar energy
        - No collinearity with outdoor temperature through PV panel characteristics
        - g_solar becomes physically meaningful: effective_window_area × SHGC
        """
        now_hour = dt_util.now().hour
        if self._solar_radiation and now_hour < len(self._solar_radiation):
            return float(self._solar_radiation[now_hour])
        return 0.0

    @property
    def thermal_model(self) -> OnlineRCModel | None:
        """Het geleerde RC-model, of None tot het geladen is.

        Gedeeld met de coast-time sensor zodat die niet zijn eigen kopie hoeft
        te trainen — beide gebruiken hetzelfde online-geleerde model.
        """
        return self._thermal_store.model if self._thermal_loaded else None

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
        now_hour = dt_util.now().hour

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
            rad_idx = now_hour + i
            rad_wm2 = 0.0
            if self._solar_radiation and rad_idx < len(self._solar_radiation):
                rad_wm2 = self._solar_radiation[rad_idx]
            fc_solar_wm2.append(rad_wm2)
            fc_meta.append({
                "datetime": fc_dt_str,
                "condition": fc_condition,
                "shortwave_wm2": rad_wm2,
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
                    (ROLE_OUTDOOR_TEMP, ROLE_FLOW_RATE, ROLE_RETURN_TEMP),
                    extra=(self._solar_entity,),
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
        self._refresh_u_prior()

        # Prime the model with current sensor values so the first hourly
        # update (1h from now) can already produce an RLS update instead
        # of only storing prev values.
        if model._prev_timestamp is None:
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            t_outdoor = get_float_state(self.hass, self._outdoor_entity)
            q_hp = get_float_state(self.hass, self._power_entity) or 0.0
            q_solar_wm2 = self._get_current_solar_radiation_wm2()
            if t_indoor is not None and t_outdoor is not None:
                model.update(t_indoor, t_outdoor, q_hp, q_solar_wm2, dt_util.utcnow())
                _LOGGER.info(
                    "RC model primed with initial values: T_in=%.1f, T_out=%.1f",
                    t_indoor, t_outdoor,
                )
            else:
                _LOGGER.debug(
                    "RC model: cannot prime at startup (sensors not yet available): "
                    "indoor=%s (%s), outdoor=%s (%s) — will update on next hourly tick",
                    t_indoor, self._indoor_temp_entity,
                    t_outdoor, self._outdoor_entity,
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

            if t_indoor is not None and t_outdoor is not None:
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
                q_solar_wm2 = self._get_current_solar_radiation_wm2()
                q_needed = model.calc_required_power(
                    t_indoor, t_outdoor, q_solar_wm2, t_setpoint=20.0,
                )
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

        solar_gain_w = self._get_current_solar_radiation_wm2() * SOLAR_RADIATION_DEFAULT_FACTOR
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

        solar_gain_w = self._get_current_solar_radiation_wm2() * solar_factor

        # Build forecast arrays (shared with the coast-time sensor).
        fc_temps, fc_solar_wm2, fc_meta = self.build_forecast_arrays(t_outdoor)
        # Solar gain in W: the batch fallback consumes this directly, so it must
        # keep using the default factor; display follows the active model.
        fc_solar_gain_batch_w = [
            wm2 * SOLAR_RADIATION_DEFAULT_FACTOR for wm2 in fc_solar_wm2
        ]
        fc_solar_gain_w = [wm2 * solar_factor for wm2 in fc_solar_wm2]

        # Build 6-hour forecast
        forecast_out: list[dict] = []
        if model.is_converged and fc_temps:
            # Online model: forward simulation (input = W/m², model applies g_solar internally)
            t_indoor = get_float_state(self.hass, self._indoor_temp_entity)
            if t_indoor is not None:
                sim = simulate_6h(
                    model,
                    t_indoor_now=t_indoor,
                    t_return=t_return or 28.0,
                    flow_lph=effective_flow,
                    forecast_t_outdoor=fc_temps,
                    forecast_q_solar=fc_solar_wm2,
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
                    t_indoor, t_outdoor, 0.0, t_setpoint=20.0,
                )
                net_demand = model.calc_required_power(
                    t_indoor, t_outdoor, current_rad_wm2, t_setpoint=20.0,
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
            # Welke factor die winst opleverde — anders is niet te zien of je
            # naar het geleerde of het hardgecodeerde getal kijkt.
            "solar_factor_w_per_wm2": round(solar_factor, 3),
            "heat_demand_w": round(raw_demand) if raw_demand is not None else None,
            "net_demand_w": round(net_demand) if net_demand is not None else None,
            "solar_radiation_wm2": round(current_rad_wm2),
            "model_source": model_source,
            **{f"model_{k}": v for k, v in model_params.items()},
            "forecast_6h": forecast_out,
        }

    def _build_batch_forecast(
        self,
        effective_flow: float,
        t_return: float | None,
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

    Voedt energy-os: bij een duur tarief mag de WP geknepen worden en draagt de
    batterij de last — maar alleen zolang het huis veilig kan uitlopen. De
    Open-Meteo zon-forecast gaat mee in de simulatie, dus voorspelde zon
    verlengt de coast-tijd (de geleerde g·Q_solar-term remt de afkoeling).

    Hergebruikt het online RC-model én de forecast van de MPC-sensor, zodat er
    geen tweede model getraind of forecast opgehaald hoeft te worden.

    Niet beschikbaar tot het RC-model geconvergeerd is (≈2 dagen data); energy-os
    valt dan terug op zijn eigen heuristiek.
    """

    _attr_has_entity_name = True
    _attr_name = "Veilige Uitlooptijd"
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
        # terwijl het dashboard en energy-os de schone id verwachten.
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
        self._attr_name = (
            "MPC Fout Aanvoertemperatuur"
            if mode == "mpc"
            else "Stooklijn Fout Aanvoertemperatuur"
        )
        self._attr_device_info = get_device_info(entry.entry_id)

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Volg álle kandidaten, niet alleen de nu actieve: anders komt een
        # bronwissel niet binnen (zie de spiegelsensoren, zelfde reden).
        registry = self.hass.data.get(DOMAIN, {}).get(
            f"{self._entry.entry_id}_sources"
        )
        tracked = {self._advised_entity}
        for role in (ROLE_SUPPLY_TEMP, ROLE_FLOW_RATE):
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

        # Stookgrens: vergelijk daggemiddeld-gebaseerde Quatt stooklijn vs huis-optimaal
        stookgrens_cur = data.stooklijn.balance_temp_api_daily
        stookgrens_opt = data.stooklijn.balance_temp_optimal
        if (
            stookgrens_cur is not None
            and stookgrens_opt is not None
            and abs(stookgrens_cur - stookgrens_opt) > ADVICE_STOOKGRENS_THRESHOLD
        ):
            changes += 1

        # Nominaal vermogen — alleen als stooklijn-regressie betrouwbaar is
        if self._stooklijn_reliable(data):
            vermogen_cur, vermogen_opt = self._calc_vermogen(data)
            if (
                vermogen_cur is not None
                and vermogen_opt is not None
                and abs(vermogen_cur - vermogen_opt) > ADVICE_VERMOGEN_THRESHOLD
            ):
                changes += 1

        # Stooklijn breakpoints zijn informatief, niet meegeteld in changes

        return changes

    def _stooklijn_reliable(self, data: QuattStooklijnData) -> bool:
        """True als de daggemiddeld-gebaseerde Quatt stooklijn betrouwbaar is.

        Twee criteria, beide moeten kloppen:
        1. balance_temp_api_daily <= ADVICE_MAX_RELIABLE_BALANCE_TEMP
        2. |slope_api_daily| >= 0.8 × |slope_optimal|

        De daily-variant middelt over volledige dagen (inclusief OFF-uren), waardoor
        modulatie-bias en over-delivery door een te agressieve stooklijn niet de
        x-intercept opblazen zoals bij de minuut-regressie het geval was.
        """
        bt = data.stooklijn.balance_temp_api_daily
        if bt is None or bt > ADVICE_MAX_RELIABLE_BALANCE_TEMP:
            return False

        slope_daily = data.stooklijn.slope_api_daily
        slope_opt = data.heat_loss_hp.slope
        if slope_daily is not None and slope_opt is not None and slope_opt != 0:
            if abs(slope_daily) < 0.8 * abs(slope_opt):
                return False

        return True

    def _calc_vermogen(
        self, data: QuattStooklijnData
    ) -> tuple[float | None, float | None]:
        """Bereken huidig en optimaal vermogen bij -10°C.

        Huidig wordt None als de daggemiddeld-gebaseerde regressie onbetrouwbaar is.
        """
        from .analysis.utils import calc_heat_demand

        # Huidig: uit de daggemiddeld-gebaseerde Quatt stooklijn
        vermogen_cur = None
        sl = data.stooklijn
        if (
            sl.slope_api_daily is not None
            and sl.intercept_api_daily is not None
            and self._stooklijn_reliable(data)
        ):
            vermogen_cur = round(sl.slope_api_daily * -10 + sl.intercept_api_daily)

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
        # "huidig" = nulpunt van de daggemiddeld-gebaseerde Quatt stooklijn
        # "optimaal" = nulpunt van de huis-optimale regressie op dagdata
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
        stooklijn_betrouwbaar = self._stooklijn_reliable(data)
        # Toon het ruwe getal altijd (ook als onbetrouwbaar), maar markeer het
        sl = data.stooklijn
        if sl.slope_api_daily is not None and sl.intercept_api_daily is not None and not stooklijn_betrouwbaar:
            attrs["nominaal_vermogen_huidig_w"] = round(sl.slope_api_daily * -10 + sl.intercept_api_daily)
        else:
            attrs["nominaal_vermogen_huidig_w"] = vermogen_cur
        attrs["nominaal_vermogen_optimaal_w"] = vermogen_opt
        attrs["nominaal_vermogen_betrouwbaar"] = stooklijn_betrouwbaar
        if not stooklijn_betrouwbaar:
            bt = round(data.stooklijn.balance_temp_api_daily, 1) if data.stooklijn.balance_temp_api_daily else "?"
            s_daily = round(data.stooklijn.slope_api_daily, 1) if data.stooklijn.slope_api_daily else None
            s_opt = round(data.heat_loss_hp.slope, 1) if data.heat_loss_hp.slope else None
            if bt is not None and float(bt) > ADVICE_MAX_RELIABLE_BALANCE_TEMP:
                reden = f"evenwichtspunt is {bt}°C (te weinig koude daggemiddelden)"
            elif s_daily is not None and s_opt is not None:
                reden = (
                    f"stooklijn-helling ({s_daily} W/°C) is te vlak "
                    f"t.o.v. warmteverlies ({s_opt} W/°C)"
                )
            else:
                reden = "onvoldoende daggemiddelden beschikbaar"
            attrs["nominaal_vermogen_advies"] = (
                f"Onbetrouwbaar: {reden}. "
                "Vergelijking pas betrouwbaar als het kouder is geweest."
            )
        elif vermogen_cur is not None and vermogen_opt is not None:
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
                "Wacht tot voldoende daggemiddelden beschikbaar zijn"
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

    Gebruikt bewust ``OPENQUATT_BREAKPOINT_TEMPS`` en niet het advies-raster:
    de punten worden op volgorde naar zes vaste number-entiteiten geschreven,
    dus de buitentemperaturen moeten één-op-één matchen met wat de firmware
    daar aanbiedt. Elk attribuut draagt zijn buitentemperatuur mee (``bp_N_buiten``),
    zodat een automation op waarde kan controleren in plaats van op positie.
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
        self._attr_name = spec.name
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
    _attr_name = "Databronnen"
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
        missing = [role for role, info in summary.items() if info["entity"] is None]
        return {
            "roles": summary,
            "missing_roles": missing,
            "roles_total": len(summary),
            "roles_resolved": len(summary) - len(missing),
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
    _attr_name = "OpenQuatt Power House Kalibratie"
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

        # T0 telt bewust niet mee: die wordt overgenomen van de regelaar, niet
        # geadviseerd. De meting kan het nulpunt niet zien — boven de stookgrens
        # wordt er niet gestookt, dus daar is geen data.
        pairs = (
            (cal.cold_temp, targets["cold_temp"], COLD_TEMP_THRESHOLD),
            (cal.rated_power, targets["rated_power"], RATED_POWER_THRESHOLD),
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
            "zero_power_temp_geadviseerd": False,
        }
        attrs["toelichting"] = (
            f"Bij {cal.cold_temp:.1f}°C buiten heeft het huis "
            f"{cal.rated_power:.0f} W nodig en draaien de warmtepompen vollast. "
            f"Tc en Pr zijn uitgerekend tegen de ingestelde stookgrens van "
            f"{cal.zero_power_temp:.1f}°C; die wordt niet geadviseerd omdat de "
            f"meting boven de stookgrens geen data heeft "
            f"(regressie zegt {cal.balance_point_measured:.1f}°C, maar dat is "
            f"extrapolatie)."
        )
        return attrs


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
    _attr_name = "Warmtevraag"
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

    async def _handle_state_change(self, event) -> None:
        self.async_write_ha_state()

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
        from .discovery import (
            ROLE_PH_ZERO_POWER_TEMP,
            async_discover_openquatt_entities,
        )
        from .power_house import T0_FROM_CONTROLLER, T0_FROM_MEASUREMENT

        if openquatt is None:
            openquatt = async_discover_openquatt_entities(self.hass)
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
                _LOGGER.warning(
                    "Warmtevraag: buitentemperatuur van '%s' is %.0f min oud "
                    "(grens %.0f min) — geen vraag gepubliceerd",
                    self._outdoor_entity,
                    age / 60,
                    OUTDOOR_MAX_AGE_SECONDS / 60,
                )
                return None

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
        return self._demand()

    @property
    def extra_state_attributes(self) -> dict | None:
        from .discovery import (
            ROLE_PH_RATED_POWER,
            async_discover_openquatt_entities,
            async_heat_demand_link,
        )

        # Eén keer detecteren voor beide vragen: die scan loopt over het hele
        # entity-register, en dit blok draait bij elke buitentemperatuur-update.
        openquatt = async_discover_openquatt_entities(self.hass)
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


class QuattChMaxWaterSensor(SensorEntity):
    """Diagnostische sensor: laatste waarde + tijdstip van chMaxWaterTemperatuur schrijfactie."""

    _attr_has_entity_name = True
    _attr_name = "Max Aanvoertemperatuur Instelling"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-high"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ch_max_water_setting"
        self._attr_device_info = get_device_info(entry.entry_id)

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
        return {
            "last_written_at": ctrl.last_written_at.isoformat() if ctrl.last_written_at else None,
            "source": ctrl._source,
            "source_entity": ctrl.source_entity,
            # Naar wélke knop geschreven is. Zonder dit is van buitenaf niet te
            # zien of de schrijfactie bij de regelaar landt die ook stuurt.
            "target_entity": ctrl.target_entity,
            "interval_minutes": int(ctrl._interval.total_seconds() // 60),
        }


_SOUND_LEVEL_SWITCH = "switch.quatt_warmteanalyse_geluidsniveau_compensatie"


class QuattSoundLevelSensor(SensorEntity):
    """Sensor met het actieve geluidsniveau — spiegelt current_level van de compensatie-switch."""

    _attr_has_entity_name = True
    _attr_name = "Geluidsniveau"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_sound_level_sensor"
        self._attr_device_info = get_device_info(entry.entry_id)
        self._level: str | None = None

    @property
    def state(self) -> str | None:
        return self._level

    async def async_added_to_hass(self) -> None:
        if (s := self.hass.states.get(_SOUND_LEVEL_SWITCH)) is not None:
            self._level = s.attributes.get("current_level")
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [_SOUND_LEVEL_SWITCH],
                self._handle_change,
            )
        )

    @callback
    def _handle_change(self, event) -> None:
        if (new := event.data.get("new_state")) is not None:
            self._level = new.attributes.get("current_level")
            self.async_write_ha_state()
