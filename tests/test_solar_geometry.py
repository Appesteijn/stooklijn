"""Zonnestand en straling op een vlak (ZONNEWARMTE_PLAN.md)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.solar_geometry import (
    clearsky_ghi,
    estimate_pv_scale,
    ghi_from_pv,
    plane_irradiance,
    pv_clear_sky,
    sun_position,
    window_irradiance,
)

LAT, LON = 52.37, 4.90   # Amsterdam


def _ts(*args) -> np.ndarray:
    return np.array([datetime(*args, tzinfo=timezone.utc).timestamp()])


def _zon(*args):
    el, az, e0 = sun_position(_ts(*args), LAT, LON)
    return el[0], az[0], e0[0]


class TestZonnestand:
    def test_middag_zomer(self):
        """Zonnemiddag 21 juni ≈ 11:40 UTC: elevatie 90 − 52,4 + 23,4 ≈ 61°, zuid."""
        el, az, _ = _zon(2026, 6, 21, 11, 40)
        assert el == pytest.approx(61.1, abs=0.7)
        assert az == pytest.approx(180, abs=3)

    def test_middag_winter(self):
        el, az, _ = _zon(2026, 12, 21, 11, 38)
        assert el == pytest.approx(14.2, abs=0.7)
        assert az == pytest.approx(180, abs=3)

    def test_nacht(self):
        assert _zon(2026, 12, 21, 23, 0)[0] < 0

    def test_ochtend_oost_avond_west(self):
        assert _zon(2026, 6, 21, 5, 0)[1] < 110
        assert _zon(2026, 6, 21, 18, 0)[1] > 250


class TestStraling:
    def test_heldere_hemel_nul_onder_de_horizon(self):
        assert clearsky_ghi(np.array([-5.0, 0.0]))[0] == 0.0

    def test_heldere_hemel_hoge_zon(self):
        assert 800 < clearsky_ghi(np.array([60.0]))[0] < 1000

    def test_winterzon_meer_op_de_gevel_dan_horizontaal(self):
        """De kern: een lage zon schijnt vol op verticaal glas."""
        el, az, e0 = (np.array([v]) for v in _zon(2026, 12, 21, 11, 38))
        ghi = clearsky_ghi(el)
        gevel = plane_irradiance(ghi, el, az, e0, 90, 180)
        assert gevel[0] > 2 * ghi[0]

    def test_zomerzon_minder_op_de_gevel_dan_horizontaal(self):
        el, az, e0 = (np.array([v]) for v in _zon(2026, 6, 21, 11, 40))
        ghi = clearsky_ghi(el)
        assert plane_irradiance(ghi, el, az, e0, 90, 180)[0] < ghi[0]

    def test_glas_laat_minder_door_dan_het_vlak_ontvangt(self):
        el, az, e0 = (np.array([v]) for v in _zon(2026, 3, 21, 11, 50))
        ghi = clearsky_ghi(el)
        assert window_irradiance(ghi, el, az, e0, 180)[0] < plane_irradiance(ghi, el, az, e0, 90, 180)[0]

    def test_noordgevel_krijgt_alleen_diffuus(self):
        el, az, e0 = (np.array([v]) for v in _zon(2026, 3, 21, 11, 50))
        ghi = clearsky_ghi(el)
        assert window_irradiance(ghi, el, az, e0, 0)[0] < 0.3 * window_irradiance(ghi, el, az, e0, 180)[0]


class TestPV:
    def _dag(self, kc=1.0, schaal=4.0):
        ts = np.arange(datetime(2026, 4, 10, 4, tzinfo=timezone.utc).timestamp(),
                       datetime(2026, 4, 10, 19, tzinfo=timezone.utc).timestamp(), 300.0)
        el, az, e0 = sun_position(ts, LAT, LON)
        cs = pv_clear_sky(el, az, e0, 35, 180)
        return ts, el, az, e0, cs, schaal * kc * cs

    def test_schaal_uit_een_heldere_dag(self):
        _ts_, el, _az, _e0, cs, pv = self._dag(schaal=4.0)
        assert estimate_pv_scale(pv, cs, el) == pytest.approx(4.0, rel=0.01)

    def test_geen_schaal_met_te_weinig_zon(self):
        _ts_, el, _az, _e0, cs, pv = self._dag()
        assert estimate_pv_scale(pv[:20], cs[:20], el[:20]) is None

    def test_lagere_zonhoogte_telt_meer_momenten(self):
        """De winterdrempel: met de zon alleen tussen 10° en 15° lukt het pas met 10°."""
        el = np.linspace(10.5, 14.5, 150)
        cs = np.full(150, 400.0)
        pv = 4.0 * cs
        assert estimate_pv_scale(pv, cs, el) is None
        assert estimate_pv_scale(pv, cs, el, min_elevation=10.0) == pytest.approx(4.0)

    def test_helderheid_terug_naar_straling(self):
        """Halve opbrengst = half zo helder = halve heldere-hemelstraling."""
        _ts_, el, az, e0, _cs, pv = self._dag(kc=0.5, schaal=4.0)
        ghi = ghi_from_pv(pv, el, az, e0, 35, 180, 4.0)
        dag = el > 15
        assert ghi[dag] == pytest.approx(0.5 * clearsky_ghi(el)[dag], rel=1e-6)

    def test_onbekend_overdag_is_nan_en_nacht_is_nul(self):
        el = np.array([-10.0, 30.0])
        az = np.array([0.0, 180.0])
        e0 = np.array([1361.0, 1361.0])
        ghi = ghi_from_pv(np.array([np.nan, np.nan]), el, az, e0, 35, 180, 4.0)
        assert ghi[0] == 0.0
        assert np.isnan(ghi[1])


class TestRichtingPanelen:
    """Richting uit de vorm van de PV-dag (heldere-hemel-omhullende)."""

    def _uren(self, richting, dagen=150, seed=1):
        rng = np.random.default_rng(seed)
        start = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
        ts = np.arange(start, start + dagen * 86400, 3600.0)
        el, az, e0 = sun_position(ts + 1800, LAT, LON)
        helderheid = np.repeat(rng.choice([1.0, 0.6, 0.2], size=dagen, p=[0.3, 0.3, 0.4]), 24)
        pv = 4.0 * helderheid * pv_clear_sky(el, az, e0, 25, richting)
        return ts, pv

    @pytest.mark.parametrize("richting", [135, 180, 240])
    def test_vindt_de_richting(self, richting):
        from custom_components.quatt_stooklijn.analysis.solar_geometry import estimate_pv_azimuth

        ts, pv = self._uren(richting)
        gevonden, scores = estimate_pv_azimuth(ts, pv, LAT, LON)
        assert abs(gevonden - richting) <= 15
        assert len(scores) == 13

    def test_te_weinig_data(self):
        from custom_components.quatt_stooklijn.analysis.solar_geometry import estimate_pv_azimuth

        ts, pv = self._uren(180, dagen=3)
        assert estimate_pv_azimuth(ts, pv, LAT, LON) == (None, {})
