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
    ADVICE_SOURCE_OPENQUATT,
    ADVICE_SOURCE_UNKNOWN,
    OPENQUATT_BREAKPOINT_TEMPS,
    QuattAdviceSensor,
    _calc_quatt_advice,
)
from custom_components.quatt_stooklijn.power_house import calc_power_house_calibration
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


class TestQuattAdviceLogic:
    """Advies zonder HA-runtime: "van X naar Y" alleen als X is uitgelezen."""

    def _make_data(
        self,
        balance_opt=16.1,
        balance_api_daily=13.0,
        slope=-209.2,
        intercept=3368.0,
    ) -> QuattStooklijnData:
        # Standaard de getallen van de melding op Tweakers (27-09-2026): Quatt
        # bevestigt stookgrens 16, de daggemiddelde fit kwam op 13,0 uit.
        return QuattStooklijnData(
            stooklijn=StooklijnResult(
                balance_temp_optimal=balance_opt,
                balance_temp_api_daily=balance_api_daily,
                slope_api_daily=-310.0,
                intercept_api_daily=4030.0,
            ),
            heat_loss_hp=HeatLossResult(
                slope=slope, intercept=intercept,
                heat_loss_coefficient=abs(slope),
                balance_point=balance_opt,
            ),
        )

    def test_quatt_cic_geen_van_naar_advies(self):
        """De geëxtrapoleerde 13,0 mag nergens als huidige stookgrens opduiken."""
        attrs = _calc_quatt_advice(self._make_data(), None, openquatt=False)
        assert attrs["stookgrens_huidig"] is None
        assert attrs["stookgrens_bron"] == ADVICE_SOURCE_UNKNOWN
        assert attrs["stookgrens_optimaal"] == 16.1
        assert "Verhoog" not in attrs["stookgrens_advies"]
        assert "13" not in attrs["stookgrens_advies"]
        assert "16.1" in attrs["stookgrens_advies"]
        assert "Quatt-app" in attrs["stookgrens_advies"]
        assert attrs["aantal_aanpassingen"] == 0

    def test_quatt_cic_vermogen_alleen_aanbevolen(self):
        attrs = _calc_quatt_advice(self._make_data(), None, openquatt=False)
        assert attrs["nominaal_vermogen_huidig_w"] is None
        # -209,2 * -10 + 3368 = 5460
        assert attrs["nominaal_vermogen_optimaal_w"] == 5460
        assert attrs["nominaal_vermogen_bron"] == ADVICE_SOURCE_UNKNOWN
        assert "5460 W" in attrs["nominaal_vermogen_advies"]
        assert "Verlaag" not in attrs["nominaal_vermogen_advies"]

    def test_openquatt_stookgrens_goed(self):
        attrs = _calc_quatt_advice(self._make_data(), 16.0, openquatt=True)
        assert attrs["stookgrens_huidig"] == 16.0
        assert attrs["stookgrens_bron"] == ADVICE_SOURCE_OPENQUATT
        assert attrs["stookgrens_advies"] == "Stookgrens is goed ingesteld"
        assert attrs["aantal_aanpassingen"] == 0

    def test_openquatt_stookgrens_wijkt_af(self):
        attrs = _calc_quatt_advice(
            self._make_data(balance_opt=16.66), 13.0, openquatt=True
        )
        # Afgerond op de knopstap van 0,5, net als de Power House-kalibratie.
        assert attrs["stookgrens_advies"] == "Verhoog stookgrens van 13.0 naar 16.5°C"
        assert attrs["aantal_aanpassingen"] == 1

    def test_openquatt_zelfde_regel_als_kalibratie(self):
        """Onder één knopstap zwijgen beide, erboven adviseren beide."""
        data = self._make_data(balance_opt=16.66)
        for setting in (16.5, 16.0, 17.5):
            attrs = _calc_quatt_advice(data, setting, openquatt=True)
            cal = calc_power_house_calibration(
                209.2, 16.66, knee_power=5000, controller_zero_power_temp=setting
            )
            assert (attrs["aantal_aanpassingen"] == 1) == cal.zero_power_temp_advised

    def test_openquatt_vermogen_verwijst_naar_power_house(self):
        attrs = _calc_quatt_advice(self._make_data(), 16.0, openquatt=True)
        assert attrs["nominaal_vermogen_bron"] == ADVICE_SOURCE_OPENQUATT
        assert "Power House" in attrs["nominaal_vermogen_advies"]
        assert attrs["nominaal_vermogen_huidig_w"] is None

    def test_zonder_balanspunt_geen_stookgrensadvies(self):
        attrs = _calc_quatt_advice(
            self._make_data(balance_opt=None), None, openquatt=False
        )
        assert attrs["stookgrens_advies"] is None
        assert attrs["stookgrens_optimaal"] is None

    def test_stooklijnpunten_blijven(self):
        attrs = _calc_quatt_advice(self._make_data(), None, openquatt=False)
        assert len(attrs["stooklijn_punten"]) == len(ADVICE_BREAKPOINT_TEMPS)
        assert attrs["stooklijn_advies"].startswith("Stel stooklijn in op:")


class TestQuattAdviceSensorState:
    """De state moet zeggen of er iets geverifieerd is."""

    def _sensor(self, data, openquatt, setting):
        sensor = QuattAdviceSensor.__new__(QuattAdviceSensor)
        sensor.coordinator = type("C", (), {"data": data})()
        sensor._openquatt_stookgrens = lambda: (openquatt, setting)
        return sensor

    def _data(self, balance_opt=16.1):
        return QuattStooklijnData(
            stooklijn=StooklijnResult(balance_temp_optimal=balance_opt),
            heat_loss_hp=HeatLossResult(slope=-209.2, intercept=3368.0),
        )

    def test_quatt_cic(self):
        sensor = self._sensor(self._data(), False, None)
        assert sensor.native_value == "Aanbevolen instellingen beschikbaar"

    def test_openquatt_goed(self):
        sensor = self._sensor(self._data(), True, 16.0)
        assert sensor.native_value == "Geen afwijking gevonden"

    def test_openquatt_afwijking(self):
        sensor = self._sensor(self._data(balance_opt=16.66), True, 13.0)
        assert sensor.native_value == "1 aanpassing aanbevolen"

    def test_zonder_data(self):
        sensor = self._sensor(None, False, None)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None


class TestOpenQuattBreakpointGrid:
    """Het OpenQuatt-raster moet matchen met de firmware, niet met het advies.

    De zes punten worden positioneel naar zes vaste number-entiteiten
    geschreven. Staan ze op het advies-raster (-10/-5/…), dan landt de waarde
    voor -10 op de knop voor -20 en schuift de hele koude kant een punt op.
    """

    def test_raster_matcht_de_firmware(self):
        assert OPENQUATT_BREAKPOINT_TEMPS == (-20, -10, 0, 5, 10, 15)

    def test_wijkt_af_van_het_advies_raster(self):
        """Vangt het per ongeluk weer gelijktrekken van de twee rasters."""
        assert OPENQUATT_BREAKPOINT_TEMPS != ADVICE_BREAKPOINT_TEMPS

    def test_zes_punten_net_als_de_firmware(self):
        assert len(OPENQUATT_BREAKPOINT_TEMPS) == 6

    def test_breakpoints_dragen_hun_buitentemperatuur(self):
        bps = _calc_heating_curve_breakpoints(
            -200, 4000, outdoor_temps=OPENQUATT_BREAKPOINT_TEMPS
        )
        assert [bp["buiten_temp"] for bp in bps] == list(OPENQUATT_BREAKPOINT_TEMPS)
