"""Tests for Quatt Advies sensor and heating curve breakpoints."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
from custom_components.quatt_stooklijn.analysis.stooklijn import StooklijnResult
from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData
from custom_components.quatt_stooklijn.sensor import (
    _calc_heating_curve_breakpoints,
    ADVICE_BREAKPOINT_TEMPS,
    ADVICE_NOMINAL_RETURN_TEMP,
    QuattAdviceSensor,
)
from custom_components.quatt_stooklijn.const import (
    MPC_SUPPLY_TEMP_MIN,
    MPC_SUPPLY_TEMP_MAX,
    NOMINAL_FLOW_LPH,
)


class TestCalcHeatingCurveBreakpoints:
    """Tests for _calc_heating_curve_breakpoints helper."""

    def test_returns_correct_number_of_breakpoints(self):
        bps = _calc_heating_curve_breakpoints(-200, 4000)
        assert len(bps) == len(ADVICE_BREAKPOINT_TEMPS)

    def test_higher_outdoor_gives_lower_supply(self):
        """Warmer outside → less heat needed → lower supply temp."""
        bps = _calc_heating_curve_breakpoints(-200, 4000)
        supply_temps = [bp["aanvoer_temp"] for bp in bps]
        # Each subsequent temp should be <= previous (or equal at min clamp)
        for i in range(1, len(supply_temps)):
            assert supply_temps[i] <= supply_temps[i - 1], (
                f"bp[{i}]={supply_temps[i]} > bp[{i-1}]={supply_temps[i-1]}"
            )

    def test_clamped_to_min(self):
        """At very high outdoor temps, supply should clamp to MPC_SUPPLY_TEMP_MIN."""
        # With small slope, warm outdoor temps give near-zero demand
        bps = _calc_heating_curve_breakpoints(-50, 500)
        # At 15°C: demand = -50*15 + 500 = -250 → 0 → supply = return temp
        # But return_nominal (28°C) > MPC_SUPPLY_TEMP_MIN (20°C), so no min clamp
        for bp in bps:
            assert bp["aanvoer_temp"] >= MPC_SUPPLY_TEMP_MIN

    def test_clamped_to_max(self):
        """At very cold temps with high demand, supply clamps to MPC_SUPPLY_TEMP_MAX."""
        bps = _calc_heating_curve_breakpoints(-1000, 10000)
        for bp in bps:
            assert bp["aanvoer_temp"] <= MPC_SUPPLY_TEMP_MAX

    def test_zero_demand_gives_return_temp(self):
        """When demand is 0, supply = return temp (if >= min)."""
        # slope=0, intercept=0 → demand always 0
        bps = _calc_heating_curve_breakpoints(0, 0)
        for bp in bps:
            assert bp["aanvoer_temp"] == ADVICE_NOMINAL_RETURN_TEMP

    def test_custom_outdoor_temps(self):
        bps = _calc_heating_curve_breakpoints(-200, 4000, outdoor_temps=(-5, 0, 5))
        assert len(bps) == 3
        assert bps[0]["buiten_temp"] == -5
        assert bps[1]["buiten_temp"] == 0
        assert bps[2]["buiten_temp"] == 5

    def test_breakpoint_keys(self):
        bps = _calc_heating_curve_breakpoints(-200, 4000)
        for bp in bps:
            assert "buiten_temp" in bp
            assert "aanvoer_temp" in bp

    def test_known_values(self):
        """Verify a specific calculation."""
        # demand at 0°C: -200*0 + 4000 = 4000 W
        # supply = 28 + 4000/(1.16*800) = 28 + 4.31 = 32.3
        bps = _calc_heating_curve_breakpoints(-200, 4000)
        bp_0 = next(bp for bp in bps if bp["buiten_temp"] == 0)
        expected = round(28.0 + 4000 / (1.16 * 800), 1)
        assert bp_0["aanvoer_temp"] == expected


class TestQuattAdviceSensorLogic:
    """Test the advice calculation logic without HA runtime."""

    def _make_data(
        self,
        slope=-200,
        intercept=4000,
        balance_opt=20.0,
        balance_api=17.0,
        actual_slope=-300,
        actual_intercept=6000,
    ) -> QuattStooklijnData:
        return QuattStooklijnData(
            stooklijn=StooklijnResult(
                balance_temp_optimal=balance_opt,
                balance_temp_api=balance_api,
                slope_api=actual_slope,
                intercept_api=actual_intercept,
            ),
            heat_loss_hp=HeatLossResult(
                slope=slope, intercept=intercept,
                heat_loss_coefficient=abs(slope),
                balance_point=balance_opt,
            ),
        )

    def test_count_changes_all_different(self):
        """When stookgrens and vermogen both differ, expect 2 changes."""
        data = self._make_data()
        sensor = QuattAdviceSensor.__new__(QuattAdviceSensor)
        sensor.coordinator = type("C", (), {"data": data})()
        assert sensor._count_changes(data) == 2

    def test_count_changes_optimal(self):
        """When stookgrens matches and vermogen matches → 0 changes."""
        data = self._make_data(
            balance_opt=17.0,  # matches api
            balance_api=17.0,
            actual_slope=-200,
            actual_intercept=4000,  # matches heat loss at -10°C
        )
        sensor = QuattAdviceSensor.__new__(QuattAdviceSensor)
        # Stookgrens diff = 0, vermogen diff = 0, breakpoints niet meegeteld
        assert sensor._count_changes(data) == 0

    def test_vermogen_calculation(self):
        """Check nominal power at -10°C."""
        data = self._make_data(actual_slope=-300, actual_intercept=6000)
        sensor = QuattAdviceSensor.__new__(QuattAdviceSensor)
        cur, opt = sensor._calc_vermogen(data)
        # Current: -300*-10 + 6000 = 9000
        assert cur == 9000
        # Optimal: max(0, -200*-10 + 4000) = 6000
        assert opt == 6000

    def test_vermogen_none_without_actual(self):
        """Without actual stooklijn config, current vermogen is None."""
        data = QuattStooklijnData(
            heat_loss_hp=HeatLossResult(slope=-200, intercept=4000),
        )
        sensor = QuattAdviceSensor.__new__(QuattAdviceSensor)
        cur, opt = sensor._calc_vermogen(data)
        assert cur is None
        assert opt == 6000
