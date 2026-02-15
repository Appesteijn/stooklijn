"""Tests for sensor value_fn and attr_fn logic."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData
from custom_components.quatt_stooklijn.analysis.stooklijn import StooklijnResult
from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
from custom_components.quatt_stooklijn.sensor import SENSOR_DESCRIPTIONS


def _make_data(**overrides) -> QuattStooklijnData:
    """Create a QuattStooklijnData with optional overrides."""
    return QuattStooklijnData(**overrides)


def _find_desc(key: str):
    """Find a sensor description by key."""
    for d in SENSOR_DESCRIPTIONS:
        if d.key == key:
            return d
    raise KeyError(f"No sensor description with key={key}")


class TestSensorValueFunctions:
    """Test each sensor's value_fn with populated and empty data."""

    def test_heat_loss_coefficient_populated(self):
        """heat_loss_coefficient should round to 1 decimal."""
        data = _make_data(
            heat_loss_hp=HeatLossResult(heat_loss_coefficient=198.456, slope=-198.456)
        )
        desc = _find_desc("heat_loss_coefficient")
        assert desc.value_fn(data) == 198.5

    def test_heat_loss_coefficient_none(self):
        """heat_loss_coefficient should be None when not computed."""
        data = _make_data()
        desc = _find_desc("heat_loss_coefficient")
        assert desc.value_fn(data) is None

    def test_balance_point_populated(self):
        data = _make_data(
            heat_loss_hp=HeatLossResult(balance_point=19.7)
        )
        desc = _find_desc("balance_point")
        assert desc.value_fn(data) == 19.7

    def test_balance_point_none(self):
        data = _make_data()
        desc = _find_desc("balance_point")
        assert desc.value_fn(data) is None

    def test_optimal_stooklijn_slope(self):
        data = _make_data(
            stooklijn=StooklijnResult(slope_optimal=-287.3)
        )
        desc = _find_desc("optimal_stooklijn_slope")
        assert desc.value_fn(data) == -287.3

    def test_quatt_stooklijn_slope(self):
        data = _make_data(
            stooklijn=StooklijnResult(slope_api=-350.1)
        )
        desc = _find_desc("quatt_stooklijn_slope")
        assert desc.value_fn(data) == -350.1

    def test_knee_temperature(self):
        data = _make_data(
            stooklijn=StooklijnResult(knee_temperature=2.45)
        )
        desc = _find_desc("knee_temperature")
        assert desc.value_fn(data) == 2.45

    def test_average_cop(self):
        data = _make_data(average_cop=3.456)
        desc = _find_desc("average_cop")
        assert desc.value_fn(data) == 3.46

    def test_average_cop_none(self):
        data = _make_data()
        desc = _find_desc("average_cop")
        assert desc.value_fn(data) is None

    def test_freezing_performance_slope(self):
        data = _make_data(
            stooklijn=StooklijnResult(slope_local=-150.7)
        )
        desc = _find_desc("freezing_performance_slope")
        assert desc.value_fn(data) == -150.7

    def test_gas_heat_loss_coefficient(self):
        data = _make_data(
            heat_loss_gas=HeatLossResult(heat_loss_coefficient=250.3, slope=-250.3)
        )
        desc = _find_desc("gas_heat_loss_coefficient")
        assert desc.value_fn(data) == 250.3

    def test_last_analysis(self):
        ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        data = _make_data(last_analysis=ts)
        desc = _find_desc("last_analysis")
        assert desc.value_fn(data) == "2024-06-15"

    def test_analysis_status(self):
        data = _make_data(analysis_status="completed")
        desc = _find_desc("analysis_status")
        assert desc.value_fn(data) == "completed"

    def test_actual_stooklijn(self):
        data = _make_data(actual_stooklijn_slope=-300.0)
        desc = _find_desc("actual_stooklijn")
        assert desc.value_fn(data) == -300.0

    def test_actual_stooklijn_none(self):
        data = _make_data()
        desc = _find_desc("actual_stooklijn")
        assert desc.value_fn(data) is None


class TestSensorAttrFunctions:
    """Test each sensor's attr_fn."""

    def test_heat_loss_attrs_populated(self):
        data = _make_data(
            heat_loss_hp=HeatLossResult(
                slope=-200,
                r2=0.95,
                scatter_data=[{"temp": 5, "heat": 3000}],
                heat_at_temps={0: 4000},
            )
        )
        desc = _find_desc("heat_loss_coefficient")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["r2"] == 0.95
        assert attrs["scatter_data"] == [{"temp": 5, "heat": 3000}]
        assert attrs["heat_at_temps"] == {0: 4000}

    def test_heat_loss_attrs_none(self):
        data = _make_data()
        desc = _find_desc("heat_loss_coefficient")
        attrs = desc.attr_fn(data)
        assert attrs is None

    def test_optimal_stooklijn_attrs(self):
        data = _make_data(
            stooklijn=StooklijnResult(
                slope_optimal=-300,
                intercept_optimal=6000,
                r2_optimal=0.92,
                balance_temp_optimal=20.0,
                scatter_data=[{"temp": 5, "heat": 3000, "cop": 3.5}],
            )
        )
        desc = _find_desc("optimal_stooklijn_slope")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["intercept"] == 6000
        assert attrs["r2"] == 0.92
        assert attrs["balance_temp"] == 20.0

    def test_quatt_stooklijn_attrs(self):
        data = _make_data(
            stooklijn=StooklijnResult(slope_api=-400, intercept_api=7000)
        )
        desc = _find_desc("quatt_stooklijn_slope")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["intercept"] == 7000

    def test_knee_temperature_attrs(self):
        data = _make_data(
            stooklijn=StooklijnResult(knee_temperature=2.0, knee_power=6000)
        )
        desc = _find_desc("knee_temperature")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["knee_power"] == 6000

    def test_cop_attrs(self):
        data = _make_data(
            stooklijn=StooklijnResult(
                cop_scatter_data=[{"temp": 5, "cop": 3.5}]
            )
        )
        desc = _find_desc("average_cop")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["cop_scatter_data"] == [{"temp": 5, "cop": 3.5}]

    def test_freezing_attrs(self):
        data = _make_data(
            stooklijn=StooklijnResult(
                slope_local=-150, intercept_local=5000, r2_local=0.88
            )
        )
        desc = _find_desc("freezing_performance_slope")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["r2"] == 0.88

    def test_gas_heat_loss_attrs(self):
        data = _make_data(
            heat_loss_gas=HeatLossResult(
                slope=-250,
                balance_point=18.0,
                r2=0.91,
                scatter_data=[{"temp": 5, "heat": 3500}],
            )
        )
        desc = _find_desc("gas_heat_loss_coefficient")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["r2"] == 0.91
        assert attrs["balance_point"] == 18.0

    def test_actual_stooklijn_attrs(self):
        data = _make_data(
            actual_stooklijn_slope=-300.0,
            actual_stooklijn_intercept=6500.0,
        )
        desc = _find_desc("actual_stooklijn")
        attrs = desc.attr_fn(data)
        assert attrs is not None
        assert attrs["intercept"] == 6500.0

    def test_actual_stooklijn_attrs_none(self):
        data = _make_data()
        desc = _find_desc("actual_stooklijn")
        attrs = desc.attr_fn(data)
        assert attrs is None


class TestAllSensorsWithEmptyData:
    """Verify no sensor crashes with default empty data."""

    def test_all_value_fns_with_default_data(self):
        """Every value_fn should handle empty data gracefully."""
        data = QuattStooklijnData()
        for desc in SENSOR_DESCRIPTIONS:
            result = desc.value_fn(data)
            # Should not raise; most return None except analysis_status="idle"
            if desc.key == "analysis_status":
                assert result == "idle"

    def test_all_attr_fns_with_default_data(self):
        """Every attr_fn should handle empty data gracefully."""
        data = QuattStooklijnData()
        for desc in SENSOR_DESCRIPTIONS:
            if desc.attr_fn is not None:
                result = desc.attr_fn(data)
                # Should not raise
