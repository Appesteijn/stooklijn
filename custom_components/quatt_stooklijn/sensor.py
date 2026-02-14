"""Sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import QuattStooklijnCoordinator, QuattStooklijnData


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
            "balance_point": d.heat_loss_gas.balance_point,
            "scatter_data": d.heat_loss_gas.scatter_data,
        }
        if d.heat_loss_gas.slope
        else None,
    ),
    QuattSensorDescription(
        key="last_analysis",
        name="Last Analysis",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check-outline",
        value_fn=lambda d: d.last_analysis,
    ),
    QuattSensorDescription(
        key="analysis_status",
        name="Analysis Status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.analysis_status,
        attr_fn=lambda _: None,
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
            "name": "Quatt Stooklijn",
            "manufacturer": "Quatt",
            "model": "Stooklijn Analyse",
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
