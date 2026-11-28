"""Schaduwvoorspelling (MPC_ZON_PLAN.md, stap 1): vastleggen en achteraf scoren."""

from __future__ import annotations

from datetime import datetime, timezone
import math

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis import shadow_forecast as sf
from custom_components.quatt_stooklijn.analysis.shadow_forecast import Moment, ShadowLog, score
from custom_components.quatt_stooklijn.analysis.solar_forecast_log import SolarForecastLog
from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    TwoStateParams, discretize)

P = TwoStateParams(U=280.0, Ci=9000.0, Cm=40000.0, Hm=3300.0, g=9.0, P_int=500.0, alpha=0.7)
T0 = 1_795_000_000 // 86400 * 86400          # middernacht UTC
HOURS = 48


def _huis(sun=None):
    """Uurlijks gesimuleerd huis; ``sun`` per uur (W/m²), anders een dag met zon 10–15 u."""
    if sun is None:
        sun = [400.0 if 10 <= (k % 24) < 15 else 0.0 for k in range(HOURS)]
    t_out = [5.0] * HOURS
    q = [3000.0] * HOURS
    Ad, Bd = discretize(P, 1.0)
    x = np.array([20.0, 19.5])
    states = [x.copy()]
    for k in range(HOURS):
        x = Ad @ x + Bd @ np.array([t_out[k], sun[k], q[k], 1.0])
        states.append(x.copy())
    # Vijfminutenrijen: invoer constant binnen het uur, de kamer op het hele uur exact.
    rows = []
    for k in range(HOURS):
        for i in range(12):
            ti = states[k][0] + (states[k + 1][0] - states[k][0]) * i / 12
            rows.append([T0 + k * 3600 + i * 300, ti, t_out[k], q[k], sun[k], math.nan])
    rows.append([T0 + HOURS * 3600, states[HOURS][0], 5.0, 3000.0, 0.0, math.nan])
    return rows, states, sun


def _verwachting(sun, factor=1.0, fout_op_lead_k=False):
    """Verwachting per uur: vooruitkijktijd k+1 is de echte zon / factor."""
    log = SolarForecastLog()
    for k in range(HOURS):
        h = T0 + k * 3600
        for lead in range(0, 13):
            log._f.setdefault(h, {})[lead] = sun[k] / factor
        if fout_op_lead_k:
            log._f[h][0] = 999.0      # mag niet gebruikt worden
    return log


def _moment(states, k, factor=None):
    return Moment(T0 + k * 3600, float(states[k][0]), float(states[k][1]), P.to_dict(), None, factor)


class TestLog:
    def test_een_per_uur_en_bewaard(self):
        log = ShadowLog()
        m = Moment(T0, 20.0, 19.5, P.to_dict(), [0.01, 0.001, 0.0001, 0.05], 0.96)
        assert log.record(m) and not log.record(m)
        terug = ShadowLog.from_dict(log.to_dict())
        assert terug.moments[T0].params == pytest.approx(P.to_dict())
        assert terug.moments[T0].factor == 0.96 and terug.moments[T0].r1c[3] == 0.05

    def test_opruimen(self):
        log = ShadowLog()
        log.record(Moment(T0, 20.0, 19.5, {}, None, None))
        log.record(Moment(T0 + 40 * 86400, 20.0, 19.5, {}, None, None))
        log.prune(T0 + 40 * 86400)
        assert list(log.moments) == [T0 + 40 * 86400]

    def test_rommel_wordt_overgeslagen(self):
        assert len(ShadowLog.from_dict({"moments": [[1, 2], None, ["x"] * 6]})) == 0


class TestScoren:
    def _score(self, log, rows, fc, now=None):
        return score(log, rows, fc, now or T0 + HOURS * 3600 + 3600, timezone.utc)

    def test_juiste_zon_geen_fout(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        for k in (6, 8, 20):
            log.record(_moment(states, k))
        s = self._score(log, rows, _verwachting(sun))
        assert s["gescoord"] == 3
        for v in ("2r2c_verwacht", "2r2c_achteraf"):
            assert all(abs(e["rmse_k"]) < 1e-6 for e in s["alle"][v].values()), v
        assert s["zonnige_uren"]["2r2c_geen_zon"]["6"]["gem_k"] < -0.1

    def test_vooruitkijktijd_k_plus_1(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        log.record(_moment(states, 8))
        s = self._score(log, rows, _verwachting(sun, fout_op_lead_k=True))
        assert s["alle"]["2r2c_verwacht"]["1"]["rmse_k"] < 1e-6

    def test_correctiefactor(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        log.record(_moment(states, 8, factor=0.8))
        s = self._score(log, rows, _verwachting(sun, factor=0.8))
        assert s["alle"]["2r2c_verwacht"]["6"]["rmse_k"] < 1e-6
        assert s["alle"]["2r2c_verwacht_ruw"]["6"]["gem_k"] > 0.05   # te veel zon: te warm

    def test_zonnig_startuur(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        log.record(_moment(states, 6))      # zon binnen zes uur
        log.record(_moment(states, 20))     # nacht
        s = self._score(log, rows, _verwachting(sun))
        assert (s["gescoord"], s["zonnig"], s["zonnige_dagen"]) == (2, 1, 1)

    def test_nog_niet_afgerond(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        log.record(_moment(states, 30))
        assert self._score(log, rows, _verwachting(sun), now=T0 + 42 * 3600)["gescoord"] == 0

    def test_gat_in_het_logboek(self):
        rows, states, sun = _huis()
        rows = [r for r in rows if not (T0 + 12 * 3600 <= r[0] < T0 + 13 * 3600)]
        log = ShadowLog()
        log.record(_moment(states, 8))
        assert self._score(log, rows, _verwachting(sun))["gescoord"] == 0

    def test_zonder_verwachting_wel_achteraf(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        log.record(_moment(states, 8))
        s = self._score(log, rows, SolarForecastLog())
        assert "2r2c_verwacht" not in s["alle"] and "2r2c_achteraf" in s["alle"]

    def test_1r1c_rekent_mee(self):
        rows, states, sun = _huis()
        log = ShadowLog()
        m = _moment(states, 8)
        m.r1c = [280.0 / 12000, 9.0 / 12000, 1 / 12000, 500.0 / 12000]
        log.record(m)
        s = self._score(log, rows, _verwachting(sun))
        assert "1r1c_verwacht" in s["alle"]


class TestDoel:
    def _r(self, rmse3, rmse6, gem6, dagen=30, zonnig=6):
        st = {"1": {"rmse_k": 0.1, "gem_k": 0}, "3": {"rmse_k": rmse3, "gem_k": 0},
              "6": {"rmse_k": rmse6, "gem_k": gem6}, "12": {"rmse_k": 0.3, "gem_k": 0}}
        return {"dagen": dagen, "zonnige_dagen": zonnig, "zonnige_uren": {"2r2c_verwacht": st}}

    def test_gehaald(self):
        assert sf.goal(self._r(0.24, 0.29, 0.05))["gehaald"] is True

    @pytest.mark.parametrize("args", [(0.26, 0.29, 0.0), (0.24, 0.31, 0.0), (0.24, 0.29, -0.11)])
    def test_niet_gehaald(self, args):
        assert sf.goal(self._r(*args))["gehaald"] is False

    def test_te_weinig_dagen(self):
        d = sf.goal(self._r(0.24, 0.29, 0.0, dagen=10))
        assert d["gehaald"] is None and d["op_koers"] is True and d["genoeg_data"] is False


class TestVastleggenInDeSensor:
    def _sensor(self, accepted=True, approved=False, start_reason=None):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from custom_components.quatt_stooklijn.sensor import QuattModelBenchmarkSensor

        b = QuattModelBenchmarkSensor.__new__(QuattModelBenchmarkSensor)
        b._shadow = ShadowLog()
        b._shadow_store = MagicMock()
        b._result = SimpleNamespace(accepted=accepted, params={"U": 1.0}, params_use={"U": 2.0},
                                    approved=approved, measured_ts=None)
        start = SimpleNamespace(t_indoor=20.1, t_mass=19.4, reason=start_reason)
        model = MagicMock()
        model._rls.theta = np.array([0.02, 0.0008, 0.0001, 0.0])
        model._constant_gain_term = lambda th: 0.04
        b._mpc = SimpleNamespace(
            _start_state_2r2c=MagicMock(return_value=(None, start)),
            thermal_model=model,
            solar_forecast_log=SimpleNamespace(lead_factor=lambda now: 0.96),
        )
        return b

    def _run(self, b, approved_for_use=False):
        import asyncio
        from unittest.mock import patch
        import custom_components.quatt_stooklijn.sensor as sensor_mod
        from datetime import datetime
        vast = datetime(2026, 11, 28, 10, 0, 30, tzinfo=timezone.utc)
        with patch.object(sensor_mod, "_approved_for_use", return_value=approved_for_use), \
             patch.object(sensor_mod.dt_util, "utcnow", return_value=vast):
            asyncio.run(b._async_shadow_record())

    def test_legt_vast_wat_de_kamerlijn_gebruikt(self):
        b = self._sensor()
        self._run(b)
        (m,) = b._shadow.moments.values()
        assert m.params == {"U": 1.0} and (m.t_indoor, m.t_mass) == (20.1, 19.4)
        assert m.r1c == pytest.approx([0.02, 0.0008, 0.0001, 0.04]) and m.factor == 0.96
        assert m.ts == int(datetime(2026, 11, 28, 10, tzinfo=timezone.utc).timestamp())
        b._shadow_store.async_delay_save.assert_called_once()

    def test_goedgekeurd_dan_de_fit_op_het_hele_logboek(self):
        b = self._sensor()
        self._run(b, approved_for_use=True)
        assert next(iter(b._shadow.moments.values())).params == {"U": 2.0}

    @pytest.mark.parametrize("kw", [{"accepted": False}, {"start_reason": "te weinig"}])
    def test_niets_zonder_bruikbaar_2r2c(self, kw):
        b = self._sensor(**kw)
        self._run(b)
        assert len(b._shadow) == 0

    def test_tweede_keer_in_hetzelfde_uur_niet_opgeslagen(self):
        b = self._sensor()
        self._run(b)
        self._run(b)
        assert len(b._shadow) == 1 and b._shadow_store.async_delay_save.call_count == 1

    def test_attribuut(self):
        from unittest.mock import MagicMock
        from custom_components.quatt_stooklijn.sensor import QuattModelBenchmarkSensor

        b = QuattModelBenchmarkSensor.__new__(QuattModelBenchmarkSensor)
        b._result = MagicMock(params={"g": 8.0}, to_dict=lambda: {}, approved=False)
        b._shadow_score = {"gescoord": 3}
        assert b.extra_state_attributes["schaduwvoorspelling"] == {"gescoord": 3}
