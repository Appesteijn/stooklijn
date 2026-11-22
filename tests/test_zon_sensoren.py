"""Zonnesensor, zonverwachtingslog en gevelzon op sensorniveau (v0.10.6-beta.12).

Zie ZONNEWARMTE_PLAN.md. De rekenkern staat in test_solar_*.py; hier gaat het
om de koppeling: wat de sensoren vastleggen en of het door HA's JSON-encoder
komt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import custom_components.quatt_stooklijn.sensor as sensor_mod
from custom_components.quatt_stooklijn.analysis.solar_forecast_log import SolarForecastLog
from custom_components.quatt_stooklijn.analysis.solar_geometry import pv_clear_sky, sun_position
from custom_components.quatt_stooklijn.analysis.solar_input import Orientation
from custom_components.quatt_stooklijn.const import CONF_PV_POWER_ENTITY
from custom_components.quatt_stooklijn.sensor import (
    QuattMpcSensor,
    _power_unit_factor,
    _score_solar_forecast,
)

from tests.test_mpc_attributen_json import json_bytes

AMS = ZoneInfo("Europe/Amsterdam")
PV = "sensor.pv_vermogen"
WAAR = Orientation(lat=52.37, lon=4.90, window_azimuth=200, pv_tilt=35, pv_azimuth=180)


class _State:
    def __init__(self, value, unit="W"):
        self.state = str(value)
        self.attributes = {"unit_of_measurement": unit}


def _mpc(pv=None, unit="W", options=None):
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.hass = MagicMock()
    known = {PV: _State(pv, unit)} if pv is not None else {}
    sensor.hass.states.get = lambda e: known.get(e)
    sensor.hass.config = SimpleNamespace(latitude=52.37, longitude=4.90)
    sensor._entry = MagicMock(
        entry_id="e1", data={},
        options={CONF_PV_POWER_ENTITY: PV, **(options or {})} if pv is not None else (options or {}),
    )
    sensor.solar_forecast_log = SolarForecastLog()
    sensor._solar_forecast_loaded = True
    sensor._solar_forecast_store = MagicMock()
    return sensor


class TestZonnesensor:
    def test_niet_ingesteld(self):
        assert _mpc()._pv_power_w() is None

    def test_watt(self):
        assert _mpc(pv=2500)._pv_power_w() == 2500

    def test_kilowatt(self):
        assert _mpc(pv=2.5, unit="kW")._pv_power_w() == 2500

    def test_slapende_omvormer(self):
        assert _mpc(pv="unavailable")._pv_power_w() is None

    @pytest.mark.parametrize("unit,factor", [("W", 1), ("kW", 1000), ("KW", 1000), ("MW", 1e6), ("", 1)])
    def test_eenheden(self, unit, factor):
        assert _power_unit_factor(unit) == factor

    def test_zonder_geleerde_richting_zuid(self):
        o = _mpc(pv=0).solar_orientation
        assert (o.window_azimuth, o.pv_tilt, o.pv_azimuth) == (180, 30, 180)

    def test_geleerde_richtingen_worden_gebruikt(self):
        s = _mpc(pv=0)
        s.pv_azimuth, s.window_azimuth = 195.0, 225.0
        o = s.solar_orientation
        assert (o.window_azimuth, o.pv_azimuth) == (225, 195)


class TestRichtingPanelenLeren:
    """Eens per etmaal uit een jaar PV-statistiek; mislukken laat de vorige staan."""

    def _sensor(self, monkeypatch, uren):
        s = _mpc(pv=0)
        s.pv_azimuth, s.pv_azimuth_scores, s._pv_orientation_at = None, {}, None
        calls = []

        async def fake_hourly(hass, entity, start, end):
            calls.append((entity, end - start))
            return uren

        async def executor(fn, *args):
            return fn(*args)

        monkeypatch.setattr(sensor_mod, "async_pv_hourly", fake_hourly)
        s.hass.async_add_executor_job = executor
        return s, calls

    def _jaar(self, richting=195):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        ts = np.arange(start, start + 200 * 86400, 3600.0)
        el, az, e0 = sun_position(ts + 1800, 52.37, 4.90)
        rng = np.random.default_rng(3)
        k = np.repeat(rng.choice([1.0, 0.5, 0.1], size=200), 24)
        pv = 4.0 * k * pv_clear_sky(el, az, e0, 30, richting)
        return list(zip(ts.tolist(), pv.tolist()))

    def test_leert_en_onthoudt(self, monkeypatch):
        import asyncio

        s, calls = self._sensor(monkeypatch, self._jaar(195))
        asyncio.run(s.async_learn_pv_orientation())
        assert s.pv_azimuth == pytest.approx(195, abs=15)
        assert calls[0][1] == pytest.approx(365 * 86400)
        asyncio.run(s.async_learn_pv_orientation())
        assert len(calls) == 1            # binnen een etmaal niet opnieuw

    def test_te_weinig_data_houdt_zuid(self, monkeypatch):
        import asyncio

        s, _ = self._sensor(monkeypatch, self._jaar()[:48])
        asyncio.run(s.async_learn_pv_orientation())
        assert s.pv_azimuth is None
        assert s.solar_orientation.pv_azimuth == 180

    def test_zonder_zonnesensor_niets(self, monkeypatch):
        import asyncio

        s = _mpc()
        s.pv_azimuth, s._pv_orientation_at = None, None
        asyncio.run(s.async_learn_pv_orientation())
        assert s.pv_azimuth is None


class TestRichtingRamenKiezen:
    def test_beste_op_gemiddelde_van_beide_splitsingen(self, monkeypatch):
        import custom_components.quatt_stooklijn.analysis.model_benchmark as mb
        import custom_components.quatt_stooklijn.analysis.solar_input as si
        from custom_components.quatt_stooklijn.sensor import (
            _window_variant_with_learned_direction,
        )

        # Gewone splitsing wil 135, omgedraaide 270; gemiddeld wint 180.
        rmse = {(90, False): .30, (90, True): .30, (135, False): .20, (135, True): .30,
                (180, False): .22, (180, True): .22, (225, False): .25, (225, True): .25,
                (270, False): .30, (270, True): .18}
        monkeypatch.setattr(si, "window_rows",
                            lambda rows, where, hourly: ([[where.window_azimuth]], {"pv_dekking": 1.0}))
        monkeypatch.setattr(mb, "compare_variant",
                            lambda v, reverse=False, **kw: {"rmse_k": rmse[(int(v[0][0]), reverse)],
                                                            "params": {"g": 10.0}})
        r = _window_variant_with_learned_direction([], WAAR, [])
        assert r["richting_ramen"] == 180
        assert r["rmse_k"] == .22                      # de gewone splitsing van de winnaar
        assert r["rmse_per_richting"]["135"] == .25

    def test_zelfde_ankers_als_de_hoofdvergelijking(self, monkeypatch):
        """Review 22-11: de variant werd vrij gefit, de hoofdvergelijking
        verankerd. Nu per richting ankers uit de eigen zonkolom."""
        import custom_components.quatt_stooklijn.analysis.model_benchmark as mb
        import custom_components.quatt_stooklijn.analysis.solar_input as si
        from custom_components.quatt_stooklijn.sensor import (
            _window_variant_with_learned_direction,
        )

        monkeypatch.setattr(si, "window_rows",
                            lambda rows, where, hourly: ([[where.window_azimuth]], {}))
        gezien = []
        monkeypatch.setattr(mb, "anchors_from_rows",
                            lambda v, u, bp: ("anker", v[0][0], u, bp))
        monkeypatch.setattr(mb, "compare_variant",
                            lambda v, reverse=False, anchors=None: gezien.append(anchors)
                            or {"rmse_k": 0.2, "params": {}})
        r = _window_variant_with_learned_direction([], WAAR, [], 280.0, 16.5)
        assert gezien and all(a is not None and a[2:] == (280.0, 16.5) for a in gezien)
        assert len({a[1] for a in gezien}) > 1          # eigen ankers per richting
        assert r["geankerd"] is True

    def test_te_weinig_pv(self, monkeypatch):
        import custom_components.quatt_stooklijn.analysis.solar_input as si
        from custom_components.quatt_stooklijn.sensor import (
            _window_variant_with_learned_direction,
        )

        monkeypatch.setattr(si, "window_rows", lambda rows, where, hourly: (None, {"pv_dekking": 0.2}))
        r = _window_variant_with_learned_direction([], WAAR, [])
        assert "richting_ramen" not in r and r["pv_dekking"] == 0.2


class TestVerwachtingVastleggen:
    def test_stempel_is_eind_van_het_uur(self, monkeypatch):
        """Open-Meteo-stempel 11:00 = uur 10–11. Opgehaald om 09:20: 1 uur vooruit."""
        monkeypatch.setattr(sensor_mod.dt_util, "get_default_time_zone", lambda: AMS)
        nu = datetime(2026, 11, 20, 9, 20, tzinfo=AMS)
        monkeypatch.setattr(sensor_mod.dt_util, "utcnow", lambda: nu.astimezone(timezone.utc))
        s = _mpc()
        s._record_solar_forecast(
            ["2026-11-20T10:00", "2026-11-20T11:00", "2026-11-20T12:00"], [50.0, 120.0, 200.0]
        )
        tien = int(datetime(2026, 11, 20, 10, tzinfo=AMS).timestamp())
        assert s.solar_forecast_log._f[tien] == {1: 120.0}
        s._solar_forecast_store.async_delay_save.assert_called_once()

    def test_niet_voor_het_laden(self, monkeypatch):
        """Anders overschrijft de eerste ophaling na een herstart dertig dagen."""
        s = _mpc()
        s._solar_forecast_loaded = False
        s._record_solar_forecast(["2026-11-20T10:00"], [50.0])
        s._solar_forecast_store.async_delay_save.assert_not_called()


def _rijen(dagen=3):
    start = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
    ts = np.arange(start, start + dagen * 86400, 300.0)
    el, az, e0 = sun_position(ts, WAAR.lat, WAAR.lon)
    pv = 3.5 * 0.7 * pv_clear_sky(el, az, e0, WAAR.pv_tilt, WAAR.pv_azimuth)
    return [[t, 20.0, 5.0, 2000.0, 0.0, p] for t, p in zip(ts, pv)], start


class TestZonverwachtingScoren:
    def _score(self, met_pv=True):
        rijen, start = _rijen()
        log = SolarForecastLog()
        for i in range(48):
            fetched = start + 24 * 3600 + i * 3600 + 60
            base = fetched // 3600 * 3600
            log.record(fetched, [(base + k * 3600, 150.0) for k in range(13)])
        nu = start + 3 * 86400
        return _score_solar_forecast(
            log, rijen, PV if met_pv else None, WAAR, [], nu, timezone.utc
        )

    def test_met_zonnesensor(self):
        s = self._score()
        assert s["referentie"] == "zonnesensor"
        assert s["pv_schaal"] == pytest.approx(3.5 * 0.7, rel=0.02)
        assert s["per_vooruitkijktijd"]["3"]["n"] > 0
        assert "persistentie_mae" in s["per_vooruitkijktijd"]["1"]

    def test_zonder_zonnesensor(self):
        s = self._score(met_pv=False)
        assert s["referentie"] == "latere_verwachting"
        assert s["pv_schaal"] is None

    def test_door_de_json_encoder(self):
        """Lessen van beta.8: numpy-typen in attributen breken de hele sensor."""
        json_bytes(self._score())
        json_bytes(self._score(met_pv=False))
        for e in self._score()["per_vooruitkijktijd"].values():
            assert all(type(v) in (int, float) for v in e.values())


class TestModelvergelijkingAttributen:
    def test_zonfactoren_los_en_door_de_encoder(self):
        from custom_components.quatt_stooklijn.sensor import QuattModelBenchmarkSensor

        b = QuattModelBenchmarkSensor.__new__(QuattModelBenchmarkSensor)
        b._result = MagicMock(params={"g": 18.0}, to_dict=lambda: {"rmse_k": 0.27})
        b._window = {"rmse_k": 0.228, "params": {"g": np.float64(14.3)}, "pv_dekking": 1.0}
        a = b.extra_state_attributes
        assert a["zonfactor_horizontaal"] == 18.0
        assert a["zonfactor_gevel"] == pytest.approx(14.3)
        assert a["gevelzon_rmse_k"] == 0.228
        json_bytes(a)

    def test_zonder_zonnesensor(self):
        from custom_components.quatt_stooklijn.sensor import QuattModelBenchmarkSensor

        b = QuattModelBenchmarkSensor.__new__(QuattModelBenchmarkSensor)
        b._result = MagicMock(params={"g": 18.0}, to_dict=lambda: {})
        b._window = None
        a = b.extra_state_attributes
        assert a["zonfactor_gevel"] is None and a["gevelzon"] is None
