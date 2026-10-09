"""Tests voor het geleerde minimumvermogen van de warmtepomp.

Het minimum bepaalt of een plan in blokken moet stoken. Te laag geleerd en
het plan rekent met uren die de warmtepomp niet kan leveren; te hoog en hij
hakt elke rustige dag onnodig in blokken.
"""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.analysis.min_power import (
    MIN_SAMPLES_PER_BIN,
    MinPowerCurve,
    learn_min_power,
)

T0 = 1_791_500_000  # willekeurig startmoment (s)
STAP = 300  # vijf minuten, zoals het meetlogboek


def _run(start_idx, vermogens, t_out, t0=T0):
    """Monsters ``[ts, t_in, t_out, q_hp, q_solar]`` voor één run plus pauze."""
    return [
        [t0 + (start_idx + i) * STAP, 20.0, t_out, q, 0.0]
        for i, q in enumerate(vermogens)
    ]


def _cyclus(t_out, plateau, n_aan=8, opstart=(900.0, 1500.0), pauze=6):
    """Opstarten, plateau, stoppen — zoals HP2 op 09-10-2026."""
    return [0.0] + list(opstart) + [plateau] * n_aan + [600.0] + [0.0] * pauze


def _log(cycli):
    rows, idx = [], 0
    for t_out, plateau in cycli:
        vermogens = _cyclus(t_out, plateau)
        rows += _run(idx, vermogens, t_out)
        idx += len(vermogens)
    return rows


class TestLeren:
    def test_plateau_is_het_minimum_niet_het_opstarten(self):
        curve = learn_min_power(_log([(10.0, 2000.0)] * 3))
        assert curve.at(10.0) == pytest.approx(2000.0)

    def test_zacht_weer_levert_meer_op_dezelfde_stand(self):
        curve = learn_min_power(_log([(6.0, 1900.0)] * 3 + [(14.0, 2400.0)] * 3))
        assert curve.at(6.0) < curve.at(14.0)

    def test_tussen_de_bins_wordt_geinterpoleerd_en_buiten_geklemd(self):
        curve = learn_min_power(_log([(6.0, 1900.0)] * 3 + [(14.0, 2400.0)] * 3))
        assert 1900.0 < curve.at(10.0) < 2400.0
        assert curve.at(-10.0) == pytest.approx(1900.0)
        assert curve.at(30.0) == pytest.approx(2400.0)

    def test_vollast_in_de_kou_telt_niet_als_minimum(self):
        """Een koude week op vollast mag het minimum niet opblazen."""
        curve = learn_min_power(_log([(0.0, 4500.0)] * 3 + [(10.0, 2000.0)] * 3))
        assert curve.at(0.0) == pytest.approx(2000.0)

    def test_een_gat_in_het_logboek_breekt_de_run(self):
        """Na een gat is het vorige monster geen buurman: geen meting."""
        rows = [
            [T0 + i * 3600, 20.0, 10.0, 2000.0, 0.0] for i in range(20)
        ]
        assert learn_min_power(rows).bins == {}

    def test_te_weinig_data_geeft_een_lege_curve(self):
        rows = _run(0, _cyclus(10.0, 2000.0, n_aan=MIN_SAMPLES_PER_BIN - 3), 10.0)
        curve = learn_min_power(rows)
        assert curve.bins == {}
        assert curve.at(10.0) is None
        assert curve.for_temps([10.0, 11.0]) is None

    def test_leeg_logboek(self):
        assert learn_min_power([]).bins == {}

    def test_volgorde_van_het_logboek_doet_er_niet_toe(self):
        rows = _log([(10.0, 2000.0)] * 3)
        assert learn_min_power(rows[::-1]).bins == learn_min_power(rows).bins


class TestCurve:
    def test_per_uur_van_een_forecast(self):
        curve = MinPowerCurve(bins={6.0: 1900.0, 14.0: 2400.0})
        assert curve.for_temps([6.0, 14.0]) == [1900.0, 2400.0]

    def test_attribuut(self):
        curve = MinPowerCurve(bins={6.0: 1900.4, 14.0: 2400.0}, samples=42)
        assert curve.as_attribute() == {
            "monsters": 42,
            "per_buitentemperatuur": {"6": 1900, "14": 2400},
        }


class TestKoelen:
    def test_koelruns_tellen_niet_mee(self):
        """Een bron die bij koelen positief vermogen meldt, mag het minimum
        voor stoken niet omlaag trekken."""
        curve = learn_min_power(_log([(26.0, 1200.0)] * 3 + [(10.0, 2000.0)] * 3))
        assert curve.at(10.0) == pytest.approx(2000.0)
        assert 26.0 not in curve.bins
