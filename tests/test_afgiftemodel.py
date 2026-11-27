"""Afgiftemodel en COP op aanvoertemperatuur (v0.10.7-beta.9, analyse 22-11-2026)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


    def test_kamer_waarbij_geleerd_is(self):
        """Review 22-11: de punten rekenden vast met 20 °C kamer. Nu met de
        kamer van de stookuren waarop het model geleerd is."""
        from dataclasses import replace
        from custom_components.quatt_stooklijn.sensor import _calc_heating_curve_breakpoints
        m, _ = fit_emitter(*_uren())
        assert m.room_c == pytest.approx(20.0, abs=0.1)
        warm = replace(m, room_c=21.5)
        a = _calc_heating_curve_breakpoints(-280.0, 280.0 * 16.5, emitter=m)
        b = _calc_heating_curve_breakpoints(-280.0, 280.0 * 16.5, emitter=warm)
        assert b[0]["aanvoer_temp"] == pytest.approx(a[0]["aanvoer_temp"] + 1.5, abs=0.15)
        assert EmitterModel.from_dict(warm.to_dict()).room_c == 21.5


    def test_max_water_nooit_lager_dan_met_vaste_retour(self):
        """Review 22-11: het afgiftemodel, doorgetrokken naar -10 °C, mag de
        ondergrens voor de max. watertemperatuur niet verlagen."""
        from custom_components.quatt_stooklijn.sensor import _calc_quatt_advice
        from tests.test_advice_sensor import TestQuattAdviceLogic
        m, _ = fit_emitter(*_uren())
        data = TestQuattAdviceLogic._make_data(None, slope=-280.0, intercept=280.0 * 16.5)
        zonder = _calc_quatt_advice(data, None, False, 40.0)
        met = _calc_quatt_advice(data, None, False, 40.0, emitter=m)
        assert met["max_water_nodig_bij_min10"] >= zonder["max_water_nodig_bij_min10"]


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

    def test_gamma_nul_telt_ook_als_met_aanvoer_cop(self):
        from custom_components.quatt_stooklijn.analysis.demand_shift import (
            calculate_demand_shift)
        m, _ = fit_emitter(*_uren())
        curve = {t: 2.0 + 0.1 * (t + 10) for t in range(-15, 20)}
        r = calculate_demand_shift([2.0] * 8, curve, 250.0, 17.0, 0.0,
                                   supply_of=lambda p: m.supply(p, 20.0))
        assert r.supply_cop_applied

    def test_onder_het_minimum_is_het_minimum_de_referentie(self):
        """Vlak 800 W met een minimum van 2400 W: de installatie pendelt toch al
        op 2400 W. Een blok op het minimum kost dan geen COP (review 22-11:
        met 800 W als referentie kwam de verschuiving 43 % duurder uit)."""
        from custom_components.quatt_stooklijn.analysis.demand_shift import (
            calculate_demand_shift)
        m = EmitterModel(k=150.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        curve = {t: 3.0 + 0.1 * (t + 10) for t in range(-15, 20)}
        prices = [0.30 if 7 <= i <= 20 else 0.15 for i in range(24)]
        kw = dict(prices=prices, baseline=[800.0] * 24, min_power_w=[2400.0] * 24)
        zonder = calculate_demand_shift([8.0] * 24, curve, 250.0, None, 0.5, **kw)
        met = calculate_demand_shift([8.0] * 24, curve, 250.0, None, 0.5,
                                     supply_of=lambda p: m.supply(p, 20.0), **kw)
        assert met.supply_cop_applied
        # Alleen de stappen boven het minimum (dubbele blokken) kosten iets.
        assert met.cost_shifted_eur < zonder.cost_shifted_eur * 1.10

    def test_restje_onder_het_minimum_geen_bonus(self):
        """Tweede review 22-11: alleen de referentie was geklemd, dus een
        restje onder het minimum kreeg een COP-factor boven 1 (30 % besparing
        op een zachte dag, 0 % zonder aanvoer-COP)."""
        from custom_components.quatt_stooklijn.analysis.demand_shift import (
            calculate_demand_shift)
        m = EmitterModel(k=150.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        curve = {t: 3.0 + 0.1 * (t + 10) for t in range(-15, 20)}
        prices = [0.30 if 1 <= i <= 2 else 0.15 for i in range(4)]
        kw = dict(prices=prices, baseline=[285.0] * 4, min_power_w=[2400.0] * 4)
        zonder = calculate_demand_shift([12.0] * 4, curve, 250.0, None, 0.5, **kw)
        met = calculate_demand_shift([12.0] * 4, curve, 250.0, None, 0.5,
                                     supply_of=lambda p: m.supply(p, 20.0), **kw)
        assert met.cost_shifted_eur >= zonder.cost_shifted_eur - 1e-9
        assert (met.expected_saving or 0) <= (zonder.expected_saving or 0) + 1e-9


class TestAdviesBijStilstaandePomp:
    def _mpc(self, emitter, indoor=20.5):
        from unittest.mock import MagicMock, patch
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

        class _Mpc(QuattMpcSensor):
            _indoor_temp_entity = "sensor.kamer"

        s = _Mpc.__new__(_Mpc)
        s.hass = MagicMock()
        s._entry = MagicMock(entry_id="e1")
        s.emitter = emitter
        # De kamertemperatuur komt sinds de extra kamersensoren uit
        # room_temperature.room_value; zonder RoomTemperature is dat de entity zelf.
        return s, patch("custom_components.quatt_stooklijn.room_temperature.get_float_state",
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


class TestBewaren:
    """Het afgiftemodel overleeft een herstart; een mislukte poging blokkeert
    geen etmaal (review 22-11-2026)."""

    def _mpc(self):
        from unittest.mock import MagicMock
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor
        s = QuattMpcSensor.__new__(QuattMpcSensor)
        s.hass = MagicMock()
        s.hass.states.get.return_value = SimpleNamespace(attributes={"unit_of_measurement": "W"})
        s.coordinator = SimpleNamespace()
        s._entry = SimpleNamespace(entry_id="e", data={}, options={})
        s._emitter_store = MagicMock()
        s._emitter_store.async_load = AsyncMock(return_value=None)
        s.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
        return s

    def _reeksen(self):
        sup, ret, flow, q, room, t_out = _uren()
        ents = ["s", "r", "f", "q", "k", "b"]
        return ents, {e: {i: float(v[i]) for i in range(len(v))}
                      for e, v in zip(ents, (sup, ret, flow, q, room, t_out))}

    def _leer(self, s, ents, means):
        from custom_components.quatt_stooklijn import sensor as mod
        it = iter(ents)
        with patch.object(mod, "async_source_entity", side_effect=lambda *a, **k: next(it)), \
                patch.object(mod, "async_hourly_means", means):
            asyncio.run(s.async_learn_emitter())

    def test_geleerd_wordt_bewaard_en_teruggezet(self):
        ents, series = self._reeksen()
        s = self._mpc()
        self._leer(s, ents, AsyncMock(return_value=series))
        assert s.emitter is not None and s.coordinator.emitter is s.emitter
        data = s._emitter_store.async_delay_save.call_args[0][0]()

        t = self._mpc()
        t._emitter_store.async_load = AsyncMock(return_value=data)
        asyncio.run(t._async_load_emitter())
        assert t.coordinator.emitter is t.emitter
        assert t.emitter.supply(4000, 20.0) == pytest.approx(s.emitter.supply(4000, 20.0), abs=0.05)
        assert t._emitter_at is not None

    def test_geleerd_model_werkt_de_stooklijnpunten_bij(self):
        """Live 22-11: de stooklijnpunten bleven na het leren op de vaste
        retour staan tot de volgende analyse."""
        from unittest.mock import MagicMock
        ents, series = self._reeksen()
        s = self._mpc()
        s.coordinator = MagicMock(data=object())
        self._leer(s, ents, AsyncMock(return_value=series))
        s.coordinator.async_update_listeners.assert_called_once()

    def test_eerste_keer_leren_alleen_zonder_model(self):
        s = self._mpc()
        s.async_learn_emitter = AsyncMock()
        s.emitter = object()
        asyncio.run(s._async_learn_emitter_first())
        s.async_learn_emitter.assert_not_awaited()
        s.emitter = None
        asyncio.run(s._async_learn_emitter_first())
        s.async_learn_emitter.assert_awaited_once()

    def test_recorderfout_probeert_het_volgende_uur_opnieuw(self):
        ents, series = self._reeksen()
        s = self._mpc()
        self._leer(s, ents, AsyncMock(side_effect=RuntimeError("recorder")))
        assert s._emitter_at is None and s.emitter is None
        self._leer(s, ents, AsyncMock(return_value=series))
        assert s.emitter is not None

    def test_blijvende_recorderfout_geeft_een_waarschuwing(self, caplog):
        import logging
        ents, series = self._reeksen()
        s = self._mpc()
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                self._leer(s, ents, AsyncMock(side_effect=RuntimeError("recorder")))
        waarschuwingen = [r for r in caplog.records
                          if r.levelno == logging.WARNING and "Afgiftemodel" in r.message]
        assert len(waarschuwingen) == 1
        self._leer(s, ents, AsyncMock(return_value=series))
        assert s._emitter_failures == 0

    def test_ontbrekende_rol_probeert_het_volgende_uur_opnieuw(self):
        ents, _ = self._reeksen()
        s = self._mpc()
        self._leer(s, [None] + ents[1:], AsyncMock())
        assert s._emitter_at is None

    def test_kapotte_opslag_geen_model(self):
        s = self._mpc()
        s._emitter_store.async_load = AsyncMock(return_value={"model": {"k": 1}})
        asyncio.run(s._async_load_emitter())
        assert s.emitter is None
