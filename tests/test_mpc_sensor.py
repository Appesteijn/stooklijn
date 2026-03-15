"""Tests voor MPC sensor berekeningen (_calc_mpc_supply_temp)."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.sensor import _calc_mpc_supply_temp
from custom_components.quatt_stooklijn.const import (
    MIN_FLOW_LPH,
    MPC_SUPPLY_TEMP_MIN,
    MPC_SUPPLY_TEMP_MAX,
    SOLAR_TO_HEAT_FACTOR,
)


# Voorbeeld parameterisatie op basis van typische woning:
# slope = -200 W/°C, intercept = 4000 W (balance_point = 20°C)
# retourtemp = 30°C, debiet = 500 l/h, geen zonnewinst


class TestCalcMpcSupplyTemp:
    """Tests voor de standalone berekeningsfunctie."""

    def test_basic_calculation(self):
        """Basisberekening bij 0°C, geen zon."""
        # warmtevraag = -200 × 0 + 4000 = 4000 W
        # T_aanvoer = 30 + 4000 / (1.16 × 500) = 30 + 6.9 ≈ 36.9°C
        result = _calc_mpc_supply_temp(
            heat_loss_slope=-200,
            heat_loss_intercept=4000,
            balance_point=20.0,
            t_outdoor=0.0,
            t_return=30.0,
            flow_lph=500,
            solar_gain_w=0.0,
        )
        assert result is not None
        assert abs(result - 36.9) < 0.1

    def test_solar_gain_reduces_supply_temp(self):
        """Zonnewinst verlaagt de benodigde aanvoertemperatuur."""
        result_no_sun = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, 500, 0.0)
        result_with_sun = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, 500, 1000.0)
        assert result_with_sun < result_no_sun

    def test_solar_gain_calculation_from_pv(self):
        """Zonnewinst via SOLAR_TO_HEAT_FACTOR: 2000 W PV → 600 W gain."""
        solar_pv = 2000.0
        solar_gain = solar_pv * SOLAR_TO_HEAT_FACTOR
        assert abs(solar_gain - 600.0) < 1.0

        result = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, 500, solar_gain)
        result_no_sun = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, 500, 0.0)
        assert result < result_no_sun

    def test_warm_outdoor_temp_no_heating(self):
        """Bij warme buitentemp (boven balance point) is warmtevraag 0."""
        # t_outdoor = 22°C > balance_point = 20°C → warmtevraag = 0
        # T_aanvoer = T_retour + 0 = 30°C (retourtemp is al boven MPC_SUPPLY_TEMP_MIN)
        result = _calc_mpc_supply_temp(-200, 4000, 20.0, 22.0, 30.0, 500, 0.0)
        assert result == 30.0

    def test_very_cold_temp_clamped_to_max(self):
        """Bij extreme kou wordt T_aanvoer geclamped op MPC_SUPPLY_TEMP_MAX."""
        # t_outdoor = -20°C → warmtevraag = -200 × (-20) + 4000 = 8000 W
        # T_aanvoer = 30 + 8000 / (1.16 × 500) = 30 + 13.8 = 43.8°C (binnen range)
        # Verlaag debiet zodat T hoog genoeg is voor clamp
        result = _calc_mpc_supply_temp(-200, 4000, 20.0, -20.0, 30.0, 50, 0.0)
        assert result == MPC_SUPPLY_TEMP_MAX

    def test_min_flow_returns_none(self):
        """Debiet onder MIN_FLOW_LPH → None (pomp staat stil)."""
        result = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, MIN_FLOW_LPH - 1, 0.0)
        assert result is None

    def test_exactly_min_flow(self):
        """Debiet precies op MIN_FLOW_LPH → berekening wel uitgevoerd."""
        result = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 30.0, MIN_FLOW_LPH, 0.0)
        assert result is not None

    def test_result_always_in_bounds(self):
        """Resultaat valt altijd binnen [MPC_SUPPLY_TEMP_MIN, MPC_SUPPLY_TEMP_MAX]."""
        test_cases = [
            (-20.0, 0.0, 500),   # heel koud
            (5.0, 500.0, 500),   # normaal winter
            (15.0, 1000.0, 300), # mild met zon
            (25.0, 0.0, 500),    # zomer
        ]
        for t_out, solar, flow in test_cases:
            result = _calc_mpc_supply_temp(-200, 4000, 20.0, t_out, 30.0, flow, solar * SOLAR_TO_HEAT_FACTOR)
            if result is not None:
                assert MPC_SUPPLY_TEMP_MIN <= result <= MPC_SUPPLY_TEMP_MAX, (
                    f"Buiten bereik bij t_out={t_out}, solar={solar}: {result}"
                )

    def test_solar_gain_cannot_exceed_demand(self):
        """Zonnewinst groter dan warmtevraag → netto vraag = 0, niet negatief."""
        # warmtevraag bij 15°C = -200 × 15 + 4000 = 1000 W
        # solar_gain = 2000 W (meer dan vraag) → net = 0 → T_aanvoer = T_retour = 30°C
        result = _calc_mpc_supply_temp(-200, 4000, 20.0, 15.0, 30.0, 500, 2000.0)
        assert result == 30.0

    def test_return_temp_affects_supply_temp(self):
        """Hogere retourtemp leidt tot hogere aanvoertemp."""
        result_low = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 25.0, 500, 0.0)
        result_high = _calc_mpc_supply_temp(-200, 4000, 20.0, 0.0, 35.0, 500, 0.0)
        assert result_high > result_low
