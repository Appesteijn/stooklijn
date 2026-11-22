"""Afgiftemodel en COP op aanvoertemperatuur (v0.10.7-beta.9, analyse 22-11-2026)."""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.emitter import (
    EmitterModel,
    cop_supply_factor,
    fit_emitter,
)


def _uren(k=200.0, n=1.3, uren=300, seed=1, ruis=0.0):
    """Stookuren van een huis met P = k·(Tgem − Tkamer)^n en 800 l/u."""
    rng = np.random.default_rng(seed)
    q = rng.uniform(1800, 6500, uren)
    room = 20.0 + rng.normal(0, 0.2, uren)
    flow = np.full(uren, 800.0)
    tm = room + (q / k) ** (1 / n)
    dt = q / (1.16 * flow)
    sup = tm + dt / 2 + rng.normal(0, ruis, uren)
    ret = tm - dt / 2 + rng.normal(0, ruis, uren)
    t_out = 16.0 - q / 300.0
    return sup, ret, flow, q, room, t_out


class TestFit:
    def test_vindt_het_huis_terug(self):
        m, reden = fit_emitter(*_uren())
        assert reden == "geleerd"
        assert m.n == pytest.approx(1.3, abs=0.02)
        assert m.k == pytest.approx(200.0, rel=0.05)
        assert m.mae_k < 0.05

    def test_aanvoer_voor_een_vermogen(self):
        m, _ = fit_emitter(*_uren())
        tm = 20.0 + (4000 / 200.0) ** (1 / 1.3)
        assert m.supply(4000, 20.0) == pytest.approx(tm + 4000 / (1.16 * 800) / 2, abs=0.2)

    def test_meer_vermogen_hogere_aanvoer(self):
        m, _ = fit_emitter(*_uren())
        assert m.supply(5000, 20.0) > m.supply(3000, 20.0) > m.supply(1000, 20.0)

    def test_te_weinig_uren(self):
        sup, ret, flow, q, room, t_out = _uren(uren=20)
        m, reden = fit_emitter(sup, ret, flow, q, room, t_out)
        assert m is None and "te weinig stookuren" in reden

    def test_uren_waarin_de_pomp_niet_doorliep_tellen_niet(self):
        sup, ret, flow, q, room, t_out = _uren()
        q = q.copy(); q[:200] = q[:200] * 0.4   # warmte past niet bij debiet × ΔT
        m, reden = fit_emitter(sup, ret, flow, q, room, t_out)
        assert m is not None and m.hours == 100

    def test_rondreis(self):
        m, _ = fit_emitter(*_uren())
        assert EmitterModel.from_dict(m.to_dict()).supply(4000, 20.0) == pytest.approx(
            m.supply(4000, 20.0), abs=0.05)
        assert EmitterModel.from_dict({"k": 1}) is None

    def test_gebruikelijke_aanvoer_per_buitentemperatuur(self):
        m, _ = fit_emitter(*_uren())
        assert m.usual_supply(0.0) > m.usual_supply(10.0)


class TestCop:
    def test_hogere_aanvoer_lagere_cop(self):
        assert cop_supply_factor(5.0, 33.0, 30.0) < 1.0
        assert cop_supply_factor(5.0, 27.0, 30.0) > 1.0
        assert cop_supply_factor(5.0, 30.0, 30.0) == pytest.approx(1.0)

    def test_drie_graden_hoger_kost_ongeveer_tien_procent(self):
        assert cop_supply_factor(5.0, 33.0, 30.0) == pytest.approx(0.90, abs=0.02)


class TestStooklijnpunten:
    def test_afgiftemodel_in_plaats_van_vaste_retour(self):
        from custom_components.quatt_stooklijn.sensor import _calc_heating_curve_breakpoints
        m, _ = fit_emitter(*_uren())
        vast = _calc_heating_curve_breakpoints(-280.0, 280.0 * 16.5)
        met = _calc_heating_curve_breakpoints(-280.0, 280.0 * 16.5, emitter=m)
        assert [p["buiten_temp"] for p in met] == [p["buiten_temp"] for p in vast]
        assert met != vast
        assert met[0]["aanvoer_temp"] > met[-1]["aanvoer_temp"]


class TestVooruitblik:
    def _args(self, **kw):
        base = dict(
            t_return=27.0, flow_lph=800.0, forecast_t_outdoor=[2.0] * 4,
            u_wk=280.0, balance_point=16.5, t_setpoint=20.0, catchup_factor=10.0,
            capacity_w=lambda t: None,
        )
        base.update(kw)
        return base

    def test_uur_nul_retour_daarna_afgifte(self):
        from custom_components.quatt_stooklijn.analysis.thermal_model import (
            advice_forward_without_model)
        m, _ = fit_emitter(*_uren())
        zonder = advice_forward_without_model(20.0, **self._args())
        met = advice_forward_without_model(20.0, **self._args(emitter=m))
        assert met[0]["supply_temp"] == zonder[0]["supply_temp"]
        q = met[1]["q_hp_needed_w"]
        assert met[1]["supply_temp"] == pytest.approx(round(m.supply(q, 20.0), 1))

    def test_stilstaande_pomp_ook_uur_nul_uit_het_afgiftemodel(self):
        from custom_components.quatt_stooklijn.analysis.thermal_model import (
            advice_forward_without_model)
        m, _ = fit_emitter(*_uren())
        met = advice_forward_without_model(
            20.0, **self._args(emitter=m, pump_running=False, t_return=15.0))
        q = met[0]["q_hp_needed_w"]
        assert met[0]["supply_temp"] == pytest.approx(round(m.supply(q, 20.0), 1))


class TestPrijsverschuiving:
    def test_aanvoerstraf_verkleint_de_besparing(self):
        from custom_components.quatt_stooklijn.analysis.demand_shift import (
            calculate_demand_shift)
        m, _ = fit_emitter(*_uren())
        temps = [-2.0, -1.0, 2.0, 6.0, 8.0, 6.0, 2.0, 0.0] * 3
        curve = {t: 2.0 + 0.1 * (t + 10) for t in range(-15, 20)}
        zonder = calculate_demand_shift(temps, curve, 250.0, 17.0, 3.0)
        met = calculate_demand_shift(temps, curve, 250.0, 17.0, 3.0,
                                     supply_of=lambda p: m.supply(p, 20.0))
        assert met.supply_cop_applied and not zonder.supply_cop_applied
        assert met.shifted == zonder.shifted
        assert met.expected_saving < zonder.expected_saving


class TestAdviesBijStilstaandePomp:
    def _mpc(self, emitter, indoor=20.5):
        from unittest.mock import MagicMock, patch
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

        class _Mpc(QuattMpcSensor):
            _indoor_temp_entity = "sensor.kamer"

        s = _Mpc.__new__(_Mpc)
        s.hass = MagicMock()
        s.emitter = emitter
        return s, patch("custom_components.quatt_stooklijn.sensor.get_float_state",
                        return_value=indoor)

    def test_draaiende_pomp_rekent_met_de_retour(self):
        m, _ = fit_emitter(*_uren())
        s, p = self._mpc(m)
        with p:
            assert s._emitter_args(700.0) == {}

    def test_stilstaande_pomp_rekent_met_het_afgiftemodel(self):
        m, _ = fit_emitter(*_uren())
        s, p = self._mpc(m)
        with p:
            assert s._emitter_args(0.0) == {"emitter": m, "t_room": 20.5}
            assert s._emitter_args(None) == {"emitter": m, "t_room": 20.5}

    def test_zonder_afgiftemodel_zoals_voorheen(self):
        s, p = self._mpc(None)
        with p:
            assert s._emitter_args(0.0) == {}
