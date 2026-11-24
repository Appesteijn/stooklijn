"""Tests voor de capaciteit uit de vriescurve (freezing_capacity).

Tweakers 24-11-2026: vriescurve 832 W/K, intercept 3996 W, knik 0,75 °C. Bij
−3 °C 1,5 kW, bij −4,8 °C nul, terwijl het huis daar 4,3 kW verliest. Een lijn
door een handvol novemberuren, doorgetrokken tot ver in de vorst.

Nagerekend op de uurdata van het testhuis (winter 2025-2026, knik 0,75 °C): met
alleen november gaf de lijn bij −3/−5 °C 4,1/2,9 kW, de hele winter zegt
5,6/4,9 kW. Met de gemeten ondergrens 4,3/4,3 kW, en kouder dan −5,3 °C (het
koudste novemberuur) geen grens meer in plaats van 1,8 kW bij −7 °C.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.stooklijn import (
    calculate_stooklijn,
    freezing_capacity,
    measured_capacity_steps,
)
from custom_components.quatt_stooklijn.helpers import FlowSmoother

from tests.test_room_setpoint import _mpc_sensor

WSS = dict(slope=831.7, intercept=3996.2)


class TestGemetenOndergrens:

    def test_trap_oplopend(self):
        steps = measured_capacity_steps(
            [-5.0, -3.0, -4.0, -1.0, -2.0], [4300.0, 1500.0, 4100.0, 5000.0, 4800.0]
        )
        assert steps == [[-5.0, 4300.0], [-2.0, 4800.0], [-1.0, 5000.0]]

    def test_een_slecht_uur_trekt_niets_omlaag(self):
        """Ontdooien, stille modus, ketel of ons eigen plafond: bewijst niets."""
        goed = measured_capacity_steps([-4.0, -1.0], [4300.0, 5000.0])
        met_slecht = measured_capacity_steps([-4.0, -3.0, -1.0], [4300.0, 900.0, 5000.0])
        assert met_slecht == goed

    def test_geen_getallen_tellen_niet(self):
        assert measured_capacity_steps([math.nan, -2.0], [9000.0, 4000.0]) == [[-2.0, 4000.0]]
        assert measured_capacity_steps([], []) == []


class TestFreezingCapacity:

    def test_zonder_extra_gegevens_de_lijn(self):
        assert freezing_capacity(-3.0, **WSS) == pytest.approx(1501.1)
        assert freezing_capacity(-5.0, **WSS) is None          # lijn onder nul

    def test_niet_kouder_dan_gemeten(self):
        assert freezing_capacity(-2.9, **WSS, min_temp=-3.0) == pytest.approx(1584.3, abs=0.1)
        assert freezing_capacity(-3.1, **WSS, min_temp=-3.0) is None

    def test_nooit_onder_wat_de_pomp_leverde(self):
        """Een uur van 4,2 kW bij −3 °C geldt ook voor −2 °C, niet voor −3,5."""
        gemeten = [[-3.0, 4200.0]]
        assert freezing_capacity(-3.0, **WSS, measured=gemeten) == 4200.0
        assert freezing_capacity(-2.0, **WSS, measured=gemeten) == 4200.0
        assert freezing_capacity(-3.5, **WSS, measured=gemeten) == pytest.approx(1085.3, abs=0.1)
        # Waar de lijn hoger ligt, blijft de lijn.
        assert freezing_capacity(1.1, **WSS, measured=gemeten) == pytest.approx(4911.1, abs=0.1)

    @pytest.mark.parametrize("slope,intercept", [(None, 4000.0), (800.0, None), (-50.0, 6000.0), (0.0, 6000.0)])
    def test_geen_bruikbare_lijn_geen_grens(self, slope, intercept):
        """Ook niet met een gemeten trap: dat is een ondergrens, geen capaciteit."""
        assert freezing_capacity(-2.0, slope, intercept, -6.0, [[-5.0, 4000.0]]) is None


class TestUitDeAnalyse:

    def test_velden_gevuld(self):
        """Uurdata onder de knik: koudste uur en de trap komen in het resultaat."""
        uren = [(-4.0, 4300), (-3.5, 1400), (-3.0, 1500), (-2.5, 4500), (-2.0, 4600),
                (-1.5, 2000), (-1.0, 4800), (-0.8, 4900)]
        df = pd.DataFrame(
            {"temperatureOutside": [t for t, _ in uren], "hpHeat": [p for _, p in uren]},
            index=pd.date_range("2026-11-20", periods=len(uren), freq="h"),
        )
        result = calculate_stooklijn(None, df, None)
        assert result.capacity_min_temp == -4.0
        assert result.capacity_measured[0] == [-4.0, 4300.0]
        assert [p for _, p in result.capacity_measured] == sorted(
            p for _, p in result.capacity_measured
        )
        if result.slope_local is not None and result.slope_local > 0:
            cap = freezing_capacity(
                -3.0, result.slope_local, result.intercept_local,
                result.capacity_min_temp, result.capacity_measured,
            )
            assert cap >= 4300.0

    def test_zonder_uurdata_leeg(self):
        result = calculate_stooklijn(None, None, None)
        assert result.capacity_min_temp is None
        assert result.capacity_measured is None


class TestInHetAdvies:

    def _sensor(self, **stooklijn):
        sensor = _mpc_sensor()
        sensor.coordinator.data.stooklijn = SimpleNamespace(
            slope_local=831.7, intercept_local=3996.2, **stooklijn
        )
        sensor.coordinator.flow_smoother = FlowSmoother()
        return sensor

    def test_oude_analyse_zonder_velden(self):
        sensor = _mpc_sensor()
        sensor.coordinator.data.stooklijn = SimpleNamespace(slope_local=831.7, intercept_local=3996.2)
        assert sensor._capacity_w(-3.0) == pytest.approx(1501.1)

    def test_grens_en_ondergrens(self):
        sensor = self._sensor(capacity_min_temp=-3.0, capacity_measured=[[-3.0, 4200.0]])
        assert sensor._capacity_w(-2.0) == 4200.0
        assert sensor._capacity_w(-3.5) is None
        assert sensor._capacity_w(1.1) == pytest.approx(4911.1, abs=0.1)
