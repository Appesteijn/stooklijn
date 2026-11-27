"""Gemeten zon uit het meetlogboek (ZONNEWARMTE_PLAN.md)."""

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
    SCALE_FILLED,
    SCALE_LOW_SUN,
    SCALE_MEASURED,
    SCALE_SAVED,
    Orientation,
    fill_pv_from_hourly,
    hourly_measured,
    measured_ghi,
)

WAAR = Orientation(lat=52.37, lon=4.90, pv_tilt=35, pv_azimuth=180)
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
        ts, ghi, schaal, bron = measured_ghi(_logboek(kc=0.8), WAAR)
        assert bron == SCALE_MEASURED
        assert schaal == pytest.approx(4.0 * 0.8, rel=0.02)
        # De schaal neemt de helderheid van een altijd-even-heldere dag op;
        # met een gemengde dag komt kc wel terug, zie de volgende test.

    def test_gemengde_dagen(self):
        helder = _logboek(dagen=2, kc=1.0)
        grijs = _logboek(dagen=1, kc=0.3, start=START + 2 * 86400)
        ts, ghi, schaal, _ = measured_ghi(helder + grijs, WAAR)
        dag3 = (ts >= START + 2 * 86400) & (sun_position(ts, WAAR.lat, WAAR.lon)[0] > 15)
        verwacht = 0.3 * clearsky_ghi(sun_position(ts[dag3], WAAR.lat, WAAR.lon)[0])
        assert ghi[dag3] == pytest.approx(verwacht, rel=0.03)

    def test_zonder_pv_geen_schaal(self):
        _ts, ghi, schaal, bron = measured_ghi(_logboek(met_pv=False), WAAR)
        assert schaal is None and bron is None and np.isnan(ghi).all()

    def test_per_uur(self):
        ts, ghi, _, _ = measured_ghi(_logboek(kc=1.0), WAAR)
        per_uur = hourly_measured(ts, ghi, WAAR)
        middag = int(START // 3600 * 3600 + 11 * 3600)
        mean, helderheid = per_uur[middag]
        assert mean > 300
        assert helderheid == pytest.approx(1.0, abs=0.05)


class TestAanvullenUitDeRecorder:
    def test_aangevuld_uit_de_recorder(self):
        """Een logboek van vóór de zonnesensor krijgt PV uit de uurstatistiek."""
        log = _logboek(met_pv=False)
        hourly = _uurgemiddelden(_logboek())
        ts, ghi, _schaal, bron = measured_ghi(log, WAAR, hourly)
        el, _az, _e0 = sun_position(ts, WAAR.lat, WAAR.lon)
        assert bron == SCALE_FILLED
        assert np.isfinite(ghi[el > 1.0]).mean() > 0.95


class TestSchaalInDeWinter:
    """Tweakers 23-11-2026: de ijking vraagt 100 momenten met de zon boven 15°.
    Eind november zijn dat er ~35 per dag; rond de jaarwisseling komt de zon in
    Groningen helemaal niet meer boven 15°."""

    NOORD = Orientation(lat=53.5, lon=6.5, pv_tilt=35, pv_azimuth=180)
    KERST = datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp()

    def _winter(self, schaal=4.0, kc=0.8, dagen=10):
        ts = np.arange(self.KERST, self.KERST + dagen * 86400, 300.0)
        w = self.NOORD
        el, az, e0 = sun_position(ts, w.lat, w.lon)
        assert el.max() < 15.0                       # het probleem zelf
        pv = schaal * kc * pv_clear_sky(el, az, e0, w.pv_tilt, w.pv_azimuth)
        return [[t, 20.0, 5.0, 2000.0, 0.0, p] for t, p in zip(ts, pv)]

    def test_bewaarde_schaal_gaat_voor(self):
        _ts, ghi, schaal, bron = measured_ghi(self._winter(), self.NOORD, saved_scale=3.3)
        assert (schaal, bron) == (3.3, SCALE_SAVED)
        assert np.isfinite(ghi).any()

    def test_zonder_bewaarde_schaal_lage_zon(self):
        _ts, _ghi, schaal, bron = measured_ghi(self._winter(), self.NOORD)
        assert bron == SCALE_LOW_SUN
        assert schaal == pytest.approx(4.0 * 0.8, rel=0.02)

    def test_verse_schaal_wint_van_bewaarde(self):
        """Wie de zon boven 15° heeft, ijkt opnieuw: panelen kunnen erbij komen."""
        _ts, _ghi, schaal, bron = measured_ghi(_logboek(kc=0.8), WAAR, saved_scale=9.9)
        assert bron == SCALE_MEASURED
        assert schaal == pytest.approx(4.0 * 0.8, rel=0.02)

    def test_nul_of_negatief_bewaard_telt_niet(self):
        _ts, _ghi, _schaal, bron = measured_ghi(self._winter(), self.NOORD, saved_scale=0.0)
        assert bron == SCALE_LOW_SUN


def _uurgemiddelden(rijen):
    ts = np.array([r[0] for r in rijen])
    pv = np.array([r[5] for r in rijen])
    uur = (ts // 3600) * 3600
    return [(float(h), float(pv[uur == h].mean())) for h in np.unique(uur)]


class TestSchaalNietUitDeAanvulling:
    """Een uurgemiddelde vlakt de dag af; op het testhuis kwam de schaal uit de
    aanvulling 6 % te laag uit (4,32 tegen 4,59). Eigen monsters gaan voor."""

    def _oud_en_nieuw(self):
        """Twee weken alleen in de uurstatistiek, daarna drie dagen eigen PV,
        met wisselende bewolking: dan vlakt het uurgemiddelde echt af."""
        rng = np.random.default_rng(7)
        dagen = []
        for d in range(17):
            dag = _logboek(dagen=1, kc=1.0, start=START + d * 86400)
            k = np.clip(rng.choice([1.0, 0.25], size=len(dag)), 0, None)
            dagen += [r[:5] + [r[5] * kk] for r, kk in zip(dag, k)]
        hourly = _uurgemiddelden(dagen)
        grens = START + 14 * 86400
        log = [r[:5] + [r[5] if r[0] >= grens else None] for r in dagen]
        return log, hourly

    def test_eigen_monsters_gaan_voor(self):
        log, hourly = self._oud_en_nieuw()
        _ts, _ghi, schaal, bron = measured_ghi(log, WAAR, hourly)
        assert bron == SCALE_MEASURED
        assert schaal == pytest.approx(4.0, rel=0.02)
        # Met de aanvulling erbij was de schaal merkbaar lager uitgekomen.
        alles = [r[:5] + [np.nan] for r in log]
        _ts, _ghi, afgevlakt, bron = measured_ghi(alles, WAAR, hourly)
        assert bron == SCALE_FILLED and afgevlakt < 0.97 * schaal

    def test_bewaarde_schaal_gaat_voor_de_aanvulling(self):
        log, hourly = self._oud_en_nieuw()
        alleen_oud = [r[:5] + [np.nan] for r in log]
        _ts, ghi, schaal, bron = measured_ghi(alleen_oud, WAAR, hourly, saved_scale=4.0)
        assert (schaal, bron) == (4.0, SCALE_SAVED)
        assert np.isfinite(ghi).any()             # de aanvulling telt wel voor de straling
