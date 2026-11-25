"""COP in de tabel "Warmtevraag per temperatuur" en de gemiddelde COP.

Tweakers 25-11-2026: gemiddelde COP 4,06 terwijl de tabel nergens boven 3,68
kwam, en drie keer 2,2 bij −10, −5 en 0 °C. De tabel interpoleerde door losse
dag-COP's (ook dagen met COP 0) en trok de koudste dag vlak door; het gemiddelde
liet elke dag even zwaar tellen.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.heat_loss import (
    calculate_heat_loss,
    cop_at_temperature,
)
from custom_components.quatt_stooklijn.analysis.utils import cop_energy, seasonal_cop


def _days(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """(buitentemp, warmte Wh/dag, stroom Wh/dag) per dag, zoals de recordertak."""
    df = pd.DataFrame(rows, columns=["avg_temperatureOutside", "totalHpHeat", "totalHpElectric"])
    df["totalBoilerHeat"] = 0.0
    df["totalHeatPerHour"] = df["totalHpHeat"] / 24
    df["averageCOP"] = (df["totalHpHeat"] / df["totalHpElectric"]).where(
        df["totalHpElectric"] > 0, 0.0
    )
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="D")
    return df


class TestGemiddeldeCop:
    def test_warmte_gedeeld_door_stroom_niet_gemiddelde_van_dagen(self):
        # Koude dag: 80 kWh warmte, COP 2,5. Zachte dag: 8 kWh, COP 5.
        df = _days([(0.0, 80_000, 32_000), (12.0, 8_000, 1_600)])
        cop = seasonal_cop(cop_energy(df))
        assert cop == pytest.approx(88_000 / 33_600)  # 2,62
        assert cop < df["averageCOP"].mean()  # het oude getal: 3,75

    def test_niet_stookdagen_en_cop_nul_tellen_niet_mee(self):
        df = _days([
            (5.0, 48_000, 16_000),   # stookdag, COP 3
            (18.0, 2_400, 400),      # 100 W gemiddeld: geen stookdag
            (6.0, 24_000, 0),        # geen stroommeting: COP 0
        ])
        energy = cop_energy(df)
        assert len(energy) == 1
        assert seasonal_cop(energy) == pytest.approx(3.0)

    def test_ketel_telt_niet_mee(self):
        df = _days([(0.0, 48_000, 16_000)])
        df["totalBoilerHeat"] = 50_000.0
        df["totalHeatPerHour"] = (df["totalHpHeat"] + df["totalBoilerHeat"]) / 24
        assert seasonal_cop(cop_energy(df)) == pytest.approx(3.0)

    def test_zonder_stroomkolom_uit_dag_cop(self):
        df = _days([(0.0, 80_000, 32_000), (12.0, 8_000, 1_600)])
        df = df.drop(columns=["totalHpElectric"])
        assert seasonal_cop(cop_energy(df)) == pytest.approx(88_000 / 33_600)

    def test_leeg(self):
        assert seasonal_cop(cop_energy(pd.DataFrame())) is None


class TestCopPerTemperatuur:
    @pytest.fixture
    def seizoen(self) -> pd.DataFrame:
        """Stookdagen van −3 tot 14 °C, COP lineair 2,0 → 4,9, met ruis."""
        rng = np.random.default_rng(1)
        temps = np.linspace(-3, 14, 120)
        cop = 2.0 + 0.17 * (temps + 3) + rng.normal(0, 0.3, len(temps))
        heat = np.maximum(300 * (17 - temps) * 24, 3_000)
        return _days(list(zip(temps, heat, heat / cop)))

    def test_band_volgt_de_curve(self, seizoen):
        energy = cop_energy(seizoen)
        for temp, verwacht in [(0, 2.51), (5, 3.36), (10, 4.21)]:
            assert cop_at_temperature(energy, temp) == pytest.approx(verwacht, abs=0.15)

    def test_geen_waarde_kouder_dan_gemeten(self, seizoen):
        energy = cop_energy(seizoen)
        # Koudste dag −3 °C: de band van −5 bevat alleen −3..−2,5 → geen waarde.
        assert cop_at_temperature(energy, -5) is None
        assert cop_at_temperature(energy, -10) is None

    def test_te_weinig_dagen(self):
        df = _days([(5.0, 48_000, 16_000), (5.5, 48_000, 16_000)])
        assert cop_at_temperature(cop_energy(df), 5) is None

    def test_dag_met_cop_nul_trekt_niets_omlaag(self):
        # Tot beta.16 gaf np.interp hier 0,0 bij 15 °C (testhuis).
        rows = [(t, 10_000, 2_200) for t in (13.0, 14.0, 14.5, 15.0)]
        rows.append((15.2, 10_000, 0))
        assert cop_at_temperature(cop_energy(_days(rows)), 15) == pytest.approx(10 / 2.2)

    def test_tabel_in_heat_loss(self, seizoen):
        result = calculate_heat_loss(seizoen)
        assert result.heat_at_temps[-10]["cop"] is None
        assert result.heat_at_temps[-5]["cop"] is None
        assert result.heat_at_temps[5]["cop"] == pytest.approx(3.36, abs=0.15)
        # Geen COP-kolom in de data (gas): geen waarde, geen fout.
        gas = seizoen.drop(columns=["averageCOP"])
        assert calculate_heat_loss(gas).heat_at_temps[5]["cop"] is None
