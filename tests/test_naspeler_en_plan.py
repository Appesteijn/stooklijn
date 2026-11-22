"""Naspeler, plan van nu en voorspellend schaduwadvies (v0.10.7-beta.9)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.replay import VARIANTS, replay
from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    TwoStateParams,
    discretize,
)

HUIS = TwoStateParams(U=280.0, Ci=8000.0, Cm=60000.0, Hm=4000.0, g=8.0,
                      P_int=500.0, alpha=0.9)
T_BAL = 16.5


def _logboek(dagen=20, seed=2):
    """Vijfminutenlogboek van HUIS onder een regelaar met inhaalfactor 10."""
    rng = np.random.default_rng(seed)
    n = dagen * 288
    dt = 5 / 60
    Ad, Bd = discretize(HUIS, dt)
    t0 = 1_795_000_000
    x = np.array([20.0, 20.0])
    rows = []
    for k in range(n):
        uur = k * dt
        t_out = 4.0 + 4.0 * np.sin(2 * np.pi * (uur - 9) / 24) + rng.normal(0, 0.3)
        sol = max(0.0, 200 * np.sin(2 * np.pi * (uur - 8) / 24))
        q = max(0.0, 280.0 * (T_BAL - t_out) + 10 * 280.0 * (20.0 - x[0]))
        rows.append([t0 + 300 * k, round(x[0] + rng.normal(0, 0.01), 2),
                     round(t_out, 2), round(q), round(sol, 1), None])
        x = Ad @ x + Bd @ np.array([t_out, sol, q, 1.0])
    return rows


class TestNaspeler:
    def test_alle_varianten_en_de_werkelijkheid(self):
        r = replay(HUIS, _logboek(), 280.0, T_BAL)
        assert set(r["varianten"]) == {v.name for v in VARIANTS}
        assert r["uren"] > 300
        assert r["beste"] in r["varianten"]

    def test_zonder_kamerterm_slechter_comfort(self):
        r = replay(HUIS, _logboek(), 280.0, T_BAL)
        v = r["varianten"]
        assert v["buitentemperatuur"]["kamer_rmse_k"] > v["inhaalfactor_10"]["kamer_rmse_k"]

    def test_zelfde_regelaar_als_het_logboek_klopt_met_de_werkelijkheid(self):
        """De naspeler met de regelaar van het logboek op het echte huis geeft
        terug wat er gebeurde. Doel = mediaan, dus niet precies 20 °C."""
        r = replay(HUIS, _logboek(), 280.0, T_BAL)
        echt, na = r["werkelijk"], r["varianten"]["inhaalfactor_10"]
        assert na["energie_kwh"] == pytest.approx(echt["energie_kwh"], rel=0.05)

    def test_te_weinig_logboek(self):
        assert "reden" in replay(HUIS, _logboek(dagen=2), 280.0, T_BAL)


class TestPlanNu:
    def _sensor(self, steps, emitter=None):
        from custom_components.quatt_stooklijn.sensor import QuattShiftPlanSensor
        s = QuattShiftPlanSensor.__new__(QuattShiftPlanSensor)
        s._price = SimpleNamespace(_preview={"_uren": steps}, _reason=None)
        mpc = MagicMock()
        mpc.emitter = emitter
        mpc._resolve_room_setpoint = lambda: (20.0, "test")
        s._mpc = mpc
        return s

    def _stappen(self, begin):
        return [{"tijd": (begin + timedelta(minutes=15 * i)).isoformat(),
                 "vlak_w": 3000, "verschoven_w": 3000 + 100 * i, "prijs": 0.2,
                 "kamer_vlak_c": 20.0, "kamer_verschoven_c": 20.0 + 0.01 * i}
                for i in range(8)]

    def test_stap_van_nu(self, monkeypatch):
        begin = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
        s = self._sensor(self._stappen(begin))
        import custom_components.quatt_stooklijn.sensor as mod
        monkeypatch.setattr(mod.dt_util, "utcnow", lambda: begin + timedelta(minutes=50))
        assert s.native_value == 3300
        a = s.extra_state_attributes
        assert a["verschil_w"] == 300
        assert a["setpoint_verschuiving_k"] == pytest.approx(0.03)
        assert a["aanvoer_plan_c"] is None

    def test_buiten_het_plan_geen_waarde(self, monkeypatch):
        begin = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
        s = self._sensor(self._stappen(begin))
        import custom_components.quatt_stooklijn.sensor as mod
        monkeypatch.setattr(mod.dt_util, "utcnow", lambda: begin + timedelta(hours=5))
        assert s.native_value is None
        assert s.extra_state_attributes["plan"] is False

    def test_zonder_voorvertoning(self):
        s = self._sensor([])
        assert s.native_value is None

    def test_aanvoer_met_afgiftemodel(self, monkeypatch):
        from custom_components.quatt_stooklijn.analysis.emitter import EmitterModel
        m = EmitterModel(k=200.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        begin = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
        s = self._sensor(self._stappen(begin), emitter=m)
        import custom_components.quatt_stooklijn.sensor as mod
        monkeypatch.setattr(mod.dt_util, "utcnow", lambda: begin + timedelta(minutes=5))
        assert s.extra_state_attributes["aanvoer_plan_c"] == pytest.approx(
            round(m.supply(3000, 20.0), 1))


class TestVoorspellendAdvies:
    def test_zonder_goedgekeurd_2r2c_niets(self):
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor
        s = QuattMpcSensor.__new__(QuattMpcSensor)
        s.house_model_2r2c = lambda: None
        s._heat_loss_line = lambda: (280.0, T_BAL)
        assert s._predictive_advice(2.0, 28.0, 800.0, 800.0, [2.0] * 4, [0.0] * 4) is None

    def test_koude_kamer_straks_meer_vraag(self):
        from custom_components.quatt_stooklijn.analysis.shift_room_temp import StartState
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor
        s = QuattMpcSensor.__new__(QuattMpcSensor)
        s.hass = MagicMock()
        s.emitter = None
        s._heat_loss_line = lambda: (280.0, T_BAL)
        s._resolve_room_setpoint = lambda: (20.0, "test")
        s._capacity_w = lambda t: None
        warm = StartState(t_indoor=20.0, t_mass=21.0)
        koud = StartState(t_indoor=20.0, t_mass=18.0)
        s.house_model_2r2c = lambda: (HUIS, warm)
        a = s._predictive_advice(2.0, 28.0, 800.0, 800.0, [2.0] * 4, [0.0] * 4)
        s.house_model_2r2c = lambda: (HUIS, koud)
        b = s._predictive_advice(2.0, 28.0, 800.0, 800.0, [2.0] * 4, [0.0] * 4)
        assert b["kamer_verwacht_c"] < a["kamer_verwacht_c"]
        assert b["vraag_w"] > a["vraag_w"]
        assert b["aanvoer_c"] > a["aanvoer_c"]
