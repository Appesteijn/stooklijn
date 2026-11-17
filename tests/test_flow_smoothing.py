"""Tests voor het gladgestreken debiet en het capaciteitsplafond van de adviezen.

Beide adviezen rekenen aanvoer = retour + vraag / (1,16 × debiet). Na een
pompstart loopt het debiet een paar minuten op vanaf ~200 l/h, en met dat
momentane getal schoot het advies naar 42–46 °C. Nagespeeld op 14 dagen van een
echt huis: met een gladgestreken debiet verdwijnen die pieken, terwijl de
mediane afwijking ten opzichte van de werkelijke aanvoer gelijk blijft (0,62 K).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn.const import NOMINAL_FLOW_LPH
from custom_components.quatt_stooklijn.helpers import FlowSmoother
from custom_components.quatt_stooklijn.sensor import _effective_flow, _feed_flow

from tests.test_room_setpoint import FLOW, INDOOR, OUTDOOR, RETURN, SETPOINT, _mpc_sensor, _sources

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _voed(smoother, flow, minuten, start=T0, stap=1):
    """Een constante meting, elke ``stap`` minuten, zoals een sensor die rapporteert."""
    for m in range(0, minuten + 1, stap):
        smoother.update(flow, start + timedelta(minutes=m))
    return start + timedelta(minutes=minuten)


class TestFlowSmoother:

    def test_nieuw_en_stil_valt_terug_op_nominaal(self):
        assert FlowSmoother().effective(None) == NOMINAL_FLOW_LPH
        assert FlowSmoother().effective(0.0) == NOMINAL_FLOW_LPH

    def test_nieuw_en_draaiend_gebruikt_het_momentane_debiet(self):
        assert FlowSmoother().effective(650.0) == 650.0

    def test_stabiel_debiet_verandert_niets(self):
        s = FlowSmoother()
        _voed(s, 800.0, 120)
        assert s.effective(800.0) == pytest.approx(800.0)

    def test_opstartdip_wordt_opgevangen(self):
        """De kern: na een stop en herstart telt de oploop vanaf 200 l/h nauwelijks."""
        s = FlowSmoother()
        t = _voed(s, 800.0, 120)
        s.update(0.0, t + timedelta(minutes=1))           # pomp stopt
        herstart = t + timedelta(minutes=45)               # 44 min stil
        _voed(s, 200.0, 3, start=herstart)                 # 3 min oploop
        eff = s.effective(200.0)
        assert eff > 700.0
        # Ter vergelijking: met 200 l/h was het temperatuurverschil 4x zo groot.
        assert 800.0 / eff < 1.15

    def test_stilstand_telt_niet_mee(self):
        """Een lange stop mag het gemiddelde niet naar nul trekken."""
        s = FlowSmoother()
        t = _voed(s, 800.0, 60)
        s.update(0.0, t)
        for m in range(1, 600, 5):                         # 10 uur stil
            s.update(0.0, t + timedelta(minutes=m))
        assert s.average == pytest.approx(800.0)
        assert s.effective(0.0) == pytest.approx(800.0)

    def test_past_zich_aan_een_installatie_met_laag_debiet_aan(self):
        """Een vaste vervangwaarde van 800 zou dit huis structureel verkeerd doorrekenen."""
        s = FlowSmoother()
        _voed(s, 500.0, 240)
        assert s.effective(500.0) == pytest.approx(500.0)
        assert s.effective(0.0) == pytest.approx(500.0)

    def test_stijgend_debiet_telt_meteen(self):
        s = FlowSmoother()
        _voed(s, 600.0, 120)
        assert s.effective(800.0) == 800.0

    def test_dubbele_aanroep_op_hetzelfde_moment_is_onschadelijk(self):
        """Beide adviessensoren voeden dezelfde smoother."""
        a, b = FlowSmoother(), FlowSmoother()
        for m in range(0, 30):
            t = T0 + timedelta(minutes=m)
            flow = 800.0 if m < 20 else 300.0
            a.update(flow, t)
            b.update(flow, t)
            b.update(flow, t)
        assert a.average == pytest.approx(b.average)


class TestGebruikInDeAdviezen:

    def test_zonder_smoother_het_oude_gedrag(self):
        coordinator = MagicMock(spec=["data"])
        assert _effective_flow(coordinator, 200.0) == 200.0
        assert _effective_flow(coordinator, None) == NOMINAL_FLOW_LPH

    def test_met_smoother(self):
        coordinator = MagicMock()
        coordinator.flow_smoother = FlowSmoother()
        _voed(coordinator.flow_smoother, 800.0, 60)
        assert _effective_flow(coordinator, 200.0) == pytest.approx(800.0)

    def test_feed_flow_zonder_smoother_doet_niets(self):
        _feed_flow(MagicMock(spec=["data"]), 800.0)

    def _advies(self, flow, smoother=None):
        states = {OUTDOOR: 3.0, RETURN: 28.0, FLOW: flow, INDOOR: 19.9, SETPOINT: 20.0}
        sensor = _mpc_sensor(**states)
        if smoother is not None:
            sensor.coordinator = MagicMock(data=None)
            sensor.coordinator.flow_smoother = smoother
        with _sources():
            return sensor.native_value

    def test_mpc_advies_piekt_niet_meer_bij_opstarten(self):
        s = FlowSmoother()
        _voed(s, 800.0, 120)
        normaal = self._advies(800.0, s)
        zonder = self._advies(200.0)        # het oude gedrag
        met = self._advies(200.0, s)
        assert zonder - 28.0 > 3.5 * (normaal - 28.0)
        assert met == pytest.approx(normaal, abs=0.5)


class TestCapaciteitsplafond:

    def _sensor(self, slope=367.0, intercept=6821.3, **states):
        sensor = _mpc_sensor(**states)
        sensor.coordinator = MagicMock()
        sensor.coordinator.data.stooklijn.slope_local = slope
        sensor.coordinator.data.stooklijn.intercept_local = intercept
        sensor.coordinator.flow_smoother = FlowSmoother()
        return sensor

    def test_capaciteit_volgt_de_vriescurve(self):
        assert self._sensor()._capacity_w(0.0) == pytest.approx(6821.3)
        assert self._sensor()._capacity_w(5.0) == pytest.approx(367.0 * 5 + 6821.3)

    @pytest.mark.parametrize("slope,intercept", [(None, 6821.3), (367.0, None), (-50.0, 6000.0), (0.0, 6000.0)])
    def test_geen_bruikbare_curve_geen_plafond(self, slope, intercept):
        assert self._sensor(slope, intercept)._capacity_w(0.0) is None

    def test_groot_tekort_wordt_begrensd(self):
        """Twee graden onder setpoint: het model vraagt veel meer dan de pomp kan."""
        states = {OUTDOOR: 0.0, RETURN: 28.0, FLOW: 800, INDOOR: 18.0, SETPOINT: 20.0}
        zonder = self._sensor(slope=None, **states)
        met = self._sensor(**states)
        with _sources():
            a_zonder, a_met = zonder.native_value, met.native_value
        assert a_met < a_zonder
        assert a_met == pytest.approx(28.0 + 6821.3 / (1.16 * 800), abs=0.1)

    def test_normaal_tekort_niet_geraakt(self):
        states = {OUTDOOR: 3.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.9, SETPOINT: 20.0}
        with _sources():
            assert self._sensor(**states).native_value == self._sensor(slope=None, **states).native_value
