"""Tests voor de weerneutrale rendementsmaat.

Wat hier bewaakt moet worden: de verhouding moet *alleen* op werkelijk
veranderd rendement reageren, niet op het weer. Een koude maand mag hem niet
omlaag duwen, want dan meet je het seizoen in plaats van de installatie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.cop_performance import (
    MIN_BINS,
    build_reference_curve,
    calculate_cop_performance,
    reference_cop,
)


def _dag(temp: float, cop: float, heat: float = 3000.0) -> dict:
    return {
        "avg_temperatureOutside": temp,
        "averageCOP": cop,
        "totalHeatPerHour": heat,
    }


def _frame(rijen: list[dict], start: str = "2026-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(rijen), freq="D")
    return pd.DataFrame(rijen, index=idx)


def _seizoen(cop_fn, n: int = 120, start: str = "2025-11-01") -> pd.DataFrame:
    """Een winter waarin de buitentemperatuur golft tussen -6 en +12 °C."""
    rijen = []
    for i in range(n):
        temp = 3.0 + 9.0 * np.sin(i / 9.0)
        rijen.append(_dag(temp, cop_fn(temp)))
    return _frame(rijen, start)


# De werkelijke curve van deze installatie, lineair benaderd tussen de
# gemeten punten (-10 -> 1,70 en +10 -> 4,90).
def _echte_cop(temp: float) -> float:
    return 2.20 + 0.16 * temp


class TestReferentiecurve:
    def test_bins_met_te_weinig_dagen_vallen_weg(self):
        rijen = [_dag(5.0, 3.5)] * 5 + [_dag(-20.0, 1.2)]  # -20 komt maar 1x voor
        curve = build_reference_curve(_frame(rijen), min_days_per_bin=3)
        assert all(c > -10 for c in curve), "eenmalige koude bin hoort er niet in"

    def test_mediaan_negeert_een_uitschieter(self):
        """Eén kapotte dag mag de norm niet verschuiven."""
        rijen = [_dag(5.0, 3.5), _dag(5.0, 3.5), _dag(5.0, 3.5), _dag(5.0, 99.0)]
        curve = build_reference_curve(_frame(rijen), min_days_per_bin=3)
        assert list(curve.values()) == [3.5]

    def test_lege_invoer_geeft_lege_curve(self):
        assert build_reference_curve(pd.DataFrame()) == {}


class TestReferenceCop:
    CURVE = {-5.0: 1.8, 5.0: 3.4, 15.0: 4.2}

    def test_interpoleert_tussen_bins(self):
        assert reference_cop(self.CURVE, 0.0) == pytest.approx(2.6)

    @pytest.mark.parametrize("temp", [-20.0, 30.0])
    def test_extrapoleert_niet(self, temp):
        """Buiten het gemeten bereik is er geen norm — dan liever niets."""
        assert reference_cop(self.CURVE, temp) is None

    def test_te_korte_curve_geeft_none(self):
        assert reference_cop({5.0: 3.4}, 5.0) is None


class TestWeerneutraliteit:
    """De kern: hetzelfde rendement moet dezelfde verhouding geven, koud of zacht."""

    def test_ongewijzigde_installatie_blijft_op_een(self):
        res = calculate_cop_performance(_seizoen(_echte_cop))
        assert res.latest_ratio == pytest.approx(1.0, abs=0.05)
        assert res.rolling_30d == pytest.approx(1.0, abs=0.05)

    def test_koude_periode_drukt_de_verhouding_niet(self):
        """Een koude staart heeft een lagere COP maar niet een lagere ratio."""
        res = calculate_cop_performance(_seizoen(_echte_cop))
        koud = [d for d in res.daily if d["temp"] < 0]
        zacht = [d for d in res.daily if d["temp"] > 8]
        assert koud and zacht
        gem_koud = float(np.mean([d["ratio"] for d in koud]))
        gem_zacht = float(np.mean([d["ratio"] for d in zacht]))
        # De COP verschilt fors, de verhouding hoort dat niet te doen.
        assert float(np.mean([d["cop"] for d in zacht])) > float(
            np.mean([d["cop"] for d in koud])
        ) + 1.0
        assert gem_koud == pytest.approx(gem_zacht, abs=0.08)

    def test_echte_verbetering_wordt_wel_gezien(self):
        """10% beter rendement moet als ~1,10 terugkomen."""
        basis = _seizoen(_echte_cop, n=120, start="2025-11-01")
        beter = _seizoen(lambda t: _echte_cop(t) * 1.10, n=20, start="2026-03-01")
        res = calculate_cop_performance(pd.concat([basis, beter]))
        assert res.rolling_7d > 1.05
        assert res.latest_ratio > 1.05


class TestRandgevallen:
    def test_geen_data(self):
        assert calculate_cop_performance(None).latest_ratio is None
        assert calculate_cop_performance(pd.DataFrame()).latest_ratio is None

    def test_ontbrekende_kolom(self):
        df = _frame([{"avg_temperatureOutside": 5.0, "averageCOP": 3.0}])
        assert calculate_cop_performance(df).latest_ratio is None

    def test_te_weinig_bins_geeft_niets(self):
        """Met één temperatuurniveau valt er niet te interpoleren."""
        res = calculate_cop_performance(_frame([_dag(5.0, 3.5)] * 10))
        assert res.latest_ratio is None
        assert len(res.reference) < MIN_BINS

    def test_zomerdagen_tellen_niet_mee(self):
        """Idle dagen mogen de norm niet verdunnen."""
        winter = _seizoen(_echte_cop, n=60, start="2025-12-01")
        zomer = _frame([_dag(20.0, 0.1, heat=0.0)] * 60, start="2026-06-01")
        res = calculate_cop_performance(pd.concat([winter, zomer]))
        assert all(d["temp"] < 15 for d in res.daily)

    def test_cop_nul_wordt_genegeerd(self):
        winter = _seizoen(_echte_cop, n=60, start="2025-12-01")
        kapot = _frame([_dag(5.0, 0.0)] * 5, start="2026-03-01")
        res = calculate_cop_performance(pd.concat([winter, kapot]))
        assert all(d["cop"] > 0 for d in res.daily)

    def test_inf_wordt_genegeerd(self):
        winter = _seizoen(_echte_cop, n=60, start="2025-12-01")
        kapot = _frame([_dag(5.0, float("inf"))] * 5, start="2026-03-01")
        res = calculate_cop_performance(pd.concat([winter, kapot]))
        assert all(np.isfinite(d["cop"]) for d in res.daily)


class TestSensorBedrading:
    """Het entity-ID moet vastgepind zijn.

    Home Assistant leidt de ID van een nieuwe entity af uit het *gebied* van het
    device. Zonder expliciete pinning wordt het
    ``sensor.bijkeuken_quatt_warmteanalyse_cop_prestatie`` en breekt elke
    dashboardverwijzing. Dat ging in v0.8.8 mis bij de spiegelsensoren en in
    v0.8.11 nog een keer bij de kalibratiesensor — vandaar deze test.
    """

    def test_entity_id_wordt_vastgepind(self):
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        src = inspect.getsource(QuattCopPerformanceSensor.__init__)
        assert "async_generate_entity_id" in src, "entity-ID niet vastgepind"
        assert "cop_prestatie" in src

    def test_state_is_het_maandgemiddelde_niet_de_losse_dag(self):
        """Eén dag heeft ~12% spreiding; als state zou dat ruis suggereren."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        src = inspect.getsource(QuattCopPerformanceSensor.native_value.fget)
        assert "rolling_30d" in src
        assert "latest_ratio" not in src
