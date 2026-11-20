"""Gemeten zon uit het meetlogboek en de zon op de ramen (ZONNEWARMTE_PLAN.md)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.solar_geometry import (
    clearsky_ghi,
    pv_clear_sky,
    sun_position,
)
from custom_components.quatt_stooklijn.analysis.solar_input import (
    MIN_PV_COVERAGE,
    Orientation,
    fill_pv_from_hourly,
    hourly_measured,
    measured_ghi,
    window_rows,
)

WAAR = Orientation(lat=52.37, lon=4.90, window_azimuth=200, pv_tilt=35, pv_azimuth=180)
START = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()


def _logboek(dagen=3, kc=0.8, schaal=4.0, met_pv=True, start=START):
    """Vijfminutenrijen met een PV-kolom die echt bij de zonnestand past."""
    ts = np.arange(start, start + dagen * 86400, 300.0)
    el, az, e0 = sun_position(ts, WAAR.lat, WAAR.lon)
    pv = schaal * kc * pv_clear_sky(el, az, e0, WAAR.pv_tilt, WAAR.pv_azimuth)
    if not met_pv:
        pv = np.full(len(ts), np.nan)
    sol = clearsky_ghi(el) * kc
    return [[t, 20.0, 5.0, 2000.0, s, p] for t, s, p in zip(ts, sol, pv)]


class TestAanvullen:
    def test_alleen_gaten(self):
        ts = np.array([0.0, 900.0, 1800.0, 2700.0])
        pv = np.array([np.nan, 5.0, np.nan, np.nan])
        uit = fill_pv_from_hourly(ts, pv, [(-3600.0, 100.0), (0.0, 300.0)])
        # Middens op −1800 en 1800: lineair daartussen, eigen meting blijft.
        assert uit[0] == pytest.approx(200.0)
        assert uit[1] == 5.0
        assert uit[2] == pytest.approx(300.0)
        assert np.isnan(uit[3])      # buiten het bereik: niets verzinnen


class TestGemetenStraling:
    def test_helderheid_terug(self):
        ts, ghi, schaal = measured_ghi(_logboek(kc=0.8), WAAR)
        assert schaal == pytest.approx(4.0 * 0.8, rel=0.02)
        # De schaal neemt de helderheid van een altijd-even-heldere dag op;
        # met een gemengde dag komt kc wel terug, zie de volgende test.

    def test_gemengde_dagen(self):
        helder = _logboek(dagen=2, kc=1.0)
        grijs = _logboek(dagen=1, kc=0.3, start=START + 2 * 86400)
        ts, ghi, schaal = measured_ghi(helder + grijs, WAAR)
        dag3 = (ts >= START + 2 * 86400) & (sun_position(ts, WAAR.lat, WAAR.lon)[0] > 15)
        verwacht = 0.3 * clearsky_ghi(sun_position(ts[dag3], WAAR.lat, WAAR.lon)[0])
        assert ghi[dag3] == pytest.approx(verwacht, rel=0.03)

    def test_zonder_pv_geen_schaal(self):
        _ts, ghi, schaal = measured_ghi(_logboek(met_pv=False), WAAR)
        assert schaal is None and np.isnan(ghi).all()

    def test_per_uur(self):
        ts, ghi, _ = measured_ghi(_logboek(kc=1.0), WAAR)
        per_uur = hourly_measured(ts, ghi, WAAR)
        middag = int(START // 3600 * 3600 + 11 * 3600)
        mean, helderheid = per_uur[middag]
        assert mean > 300
        assert helderheid == pytest.approx(1.0, abs=0.05)


class TestRamen:
    def test_zonkolom_vervangen(self):
        rijen, info = window_rows(_logboek(), WAAR)
        assert info["pv_dekking"] == 1.0
        assert len(rijen) == len(_logboek())
        assert len(rijen[0]) == 5
        middag = [r for r in rijen if (r[0] - START) % 86400 == 11 * 3600]
        assert middag[0][4] > 0

    def test_te_weinig_pv_geen_variant(self):
        rijen, info = window_rows(_logboek(met_pv=False), WAAR)
        assert rijen is None
        assert info["pv_dekking"] < MIN_PV_COVERAGE

    def test_aangevuld_uit_de_recorder(self):
        """Een logboek van vóór de zonnesensor krijgt PV uit de uurstatistiek."""
        log = _logboek(met_pv=False)
        met = _logboek()
        ts = np.array([r[0] for r in met])
        pv = np.array([r[5] for r in met])
        uur = (ts // 3600) * 3600
        hourly = [(float(h), float(pv[uur == h].mean())) for h in np.unique(uur)]
        rijen, info = window_rows(log, WAAR, hourly)
        assert rijen is not None
        assert info["pv_dekking"] > 0.95

    def test_oud_logboek_met_vijf_kolommen(self):
        oud = [r[:5] for r in _logboek()]
        rijen, info = window_rows(oud, WAAR)
        assert rijen is None and info["pv_dekking"] == 0.0
