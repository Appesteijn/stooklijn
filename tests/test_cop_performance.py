"""Tests voor de weerneutrale rendementsmaat.

Wat hier bewaakt moet worden: de verhouding moet *alleen* op werkelijk
veranderd rendement reageren, niet op het weer, niet op het seizoen, en niet op
zichzelf. Die laatste is de subtiele: bouw de norm uit dagen die je óók
beoordeelt, en een echte verbetering zakt weg in haar eigen referentie.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.cop_performance import (
    MIN_BINS,
    SEASON_AUTUMN,
    SEASON_SPRING,
    build_reference_curve,
    calculate_cop_performance,
    match_seasonally,
    reference_cop,
    season_of,
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


class TestSeizoenshelft:
    def test_najaar_loopt_van_augustus_tot_december(self):
        assert season_of(8) == SEASON_AUTUMN
        assert season_of(12) == SEASON_AUTUMN

    def test_voorjaar_loopt_van_januari_tot_juli(self):
        assert season_of(1) == SEASON_SPRING
        assert season_of(7) == SEASON_SPRING


class TestReferentiecurve:
    def test_bins_met_te_weinig_dagen_vallen_weg(self):
        rijen = [_dag(5.0, 3.5)] * 5 + [_dag(-20.0, 1.2)]  # -20 komt maar 1x voor
        curve = build_reference_curve(_frame(rijen), min_days_per_bin=3)
        for bins in curve.values():
            assert all(c > -10 for c in bins), "eenmalige koude bin hoort er niet in"

    def test_mediaan_negeert_een_uitschieter(self):
        """Eén kapotte dag mag de norm niet verschuiven."""
        rijen = [_dag(5.0, 3.5), _dag(5.0, 3.5), _dag(5.0, 3.5), _dag(5.0, 99.0)]
        curve = build_reference_curve(_frame(rijen), min_days_per_bin=3)
        # Eén temperatuurniveau geeft één bin, en dat is te weinig om op te varen.
        assert curve == {}

    def test_lege_invoer_geeft_lege_curve(self):
        assert build_reference_curve(pd.DataFrame()) == {}

    def test_de_twee_seizoenshelften_krijgen_een_eigen_norm(self):
        """Anders middelt november en mei door elkaar — precies de bias die
        voorjaarsdagen structureel onder 1 zette."""
        najaar = _seizoen(_echte_cop, n=60, start="2025-10-01")
        # Zelfde weer, 10% slechter rendement, maar in het voorjaar.
        voorjaar = _seizoen(lambda t: _echte_cop(t) * 0.90, n=60, start="2026-03-01")
        curve = build_reference_curve(pd.concat([najaar, voorjaar]))
        assert set(curve) == {SEASON_AUTUMN, SEASON_SPRING}
        gedeeld = set(curve[SEASON_AUTUMN]) & set(curve[SEASON_SPRING])
        assert gedeeld, "de twee helften horen dezelfde temperaturen te dekken"
        for centre in gedeeld:
            assert curve[SEASON_SPRING][centre] < curve[SEASON_AUTUMN][centre]


class TestReferenceCop:
    CURVE = {SEASON_AUTUMN: {-5.0: 1.8, 5.0: 3.4, 15.0: 4.2}}

    def test_interpoleert_tussen_bins(self):
        assert reference_cop(self.CURVE, 0.0, SEASON_AUTUMN) == pytest.approx(2.6)

    @pytest.mark.parametrize("temp", [-20.0, 30.0])
    def test_extrapoleert_niet(self, temp):
        """Buiten het gemeten bereik is er geen norm — dan liever niets."""
        assert reference_cop(self.CURVE, temp, SEASON_AUTUMN) is None

    def test_wijkt_niet_uit_naar_de_andere_seizoenshelft(self):
        """Uitwijken zou de vergelijking terugbrengen die de splitsing weghaalt."""
        assert reference_cop(self.CURVE, 5.0, SEASON_SPRING) is None

    def test_te_korte_curve_geeft_none(self):
        assert reference_cop({SEASON_AUTUMN: {5.0: 3.4}}, 5.0, SEASON_AUTUMN) is None


class TestWeerneutraliteit:
    """De kern: hetzelfde rendement moet dezelfde verhouding geven, koud of zacht."""

    def test_ongewijzigde_installatie_blijft_op_een(self):
        res = calculate_cop_performance(_seizoen(_echte_cop))
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
        res = calculate_cop_performance(
            pd.concat([basis, beter]), baseline_date="2026-03-01"
        )
        assert res.after.mean > 1.05
        assert res.delta_pct > 5


class TestBevrorenNorm:
    """De norm mag de dagen die eraan getoetst worden niet bevatten.

    Deed hij dat wel, dan schoof een structurele verbetering de mediaan van haar
    eigen bin mee omhoog en las ze na één seizoen weer als 1,00 — precies het
    gebruik waar de maat voor bedoeld is, stilzwijgend uitgehold.
    """

    def _slechtere_staart(self, dagen: int = 20):
        basis = _seizoen(_echte_cop, n=120, start="2025-11-01")
        staart = _frame(
            [_dag(9.0, _echte_cop(9.0) * 0.90)] * dagen, start="2026-04-20"
        )
        return pd.concat([basis, staart])

    def test_een_lange_eenzijdige_periode_wordt_niet_zijn_eigen_norm(self):
        res = calculate_cop_performance(self._slechtere_staart())
        assert res.norm_frozen
        staart = [d for d in res.daily if d["date"] >= "2026-04-20"]
        assert staart, "de staart hoort beoordeeld te worden"
        gemiddeld = float(np.mean([d["ratio"] for d in staart]))
        assert gemiddeld == pytest.approx(0.90, abs=0.03), (
            "10% slechter hoort als 0,90 te lezen, niet naar 1 te kruipen"
        )

    def test_de_beoordeelde_dagen_dragen_de_norm_niet(self):
        res = calculate_cop_performance(self._slechtere_staart())
        assert res.baseline_date is not None
        assert res.reference_days < len(res.daily) + 30

    def test_zonder_genoeg_historie_wordt_er_niet_bevroren(self):
        """Liever een eerlijk gemelde zelfreferentie dan de helft van de dagen
        zonder oordeel laten."""
        res = calculate_cop_performance(_seizoen(_echte_cop, n=35))
        assert res.rolling_30d is not None
        assert not res.norm_frozen
        assert res.baseline_date is None


class TestVoorEnNa:
    """Het eigenlijke antwoord: heeft die aanpassing van die datum geholpen?"""

    def _met_sprong(self, factor: float, dagen: int = 40):
        basis = _seizoen(_echte_cop, n=120, start="2025-11-01")
        na = _seizoen(
            lambda t: _echte_cop(t) * factor, n=dagen, start="2026-03-05"
        )
        return pd.concat([basis, na])

    def test_de_gemarkeerde_datum_splitst_norm_en_beoordeling(self):
        res = calculate_cop_performance(
            self._met_sprong(1.10), baseline_date="2026-03-05"
        )
        assert res.baseline_explicit
        assert res.baseline_date == "2026-03-05"
        assert res.before.date_to < "2026-03-05" <= res.after.date_from

    def test_een_echte_verbetering_komt_boven_de_ruis_uit(self):
        res = calculate_cop_performance(
            self._met_sprong(1.10), baseline_date="2026-03-05"
        )
        assert res.delta_pct == pytest.approx(10, abs=3)
        assert res.delta_significant

    def test_gelijk_gebleven_rendement_geeft_geen_conclusie(self):
        """Nul verschil mag niet als resultaat lezen."""
        res = calculate_cop_performance(
            self._met_sprong(1.00), baseline_date="2026-03-05"
        )
        assert abs(res.delta_pct) < 3
        assert not res.delta_significant

    def test_twee_dagen_zijn_geen_bewijs(self):
        """Met een handvol dagen is zelfs een grote uitslag nog ruis."""
        res = calculate_cop_performance(
            self._met_sprong(1.05, dagen=2), baseline_date="2026-03-05"
        )
        assert res.after.days == 2
        assert not res.delta_significant

    def test_de_dagspreiding_wordt_gemeten_niet_aangenomen(self):
        res = calculate_cop_performance(
            self._met_sprong(1.10), baseline_date="2026-03-05"
        )
        assert res.spread_pct is not None and 0 < res.spread_pct < 50

class TestSeizoenspositie:
    """Vóór en ná moeten uit hetzelfde deel van het jaar komen.

    De seizoenshelft haalt de grofste scheefheid eruit — gemeten op de echte
    226 stookdagen van deze installatie scheelde de norm bij 9 tot 15 °C 12 tot
    21% tussen najaar en voorjaar — maar bínnen een helft loopt hetzelfde effect
    door. Een aanpassing van 1 maart afzetten tegen de winter ervoor levert een
    getal waar seizoen in zit, en dat mag geen conclusie heten.
    """

    @staticmethod
    def _dagen(datums: list[str]) -> list[dict]:
        return [{"date": d, "ratio": 1.0} for d in datums]

    def test_dagen_uit_dezelfde_periode_matchen(self):
        voor = self._dagen([f"2026-02-{d:02d}" for d in range(1, 20)])
        na = self._dagen([f"2026-03-{d:02d}" for d in range(1, 8)])
        gematcht_voor, gematcht_na = match_seasonally(voor, na)
        assert len(gematcht_na) == 7
        assert gematcht_voor

    def test_een_ver_weggelegen_periode_valt_af(self):
        voor = self._dagen([f"2025-11-{d:02d}" for d in range(1, 20)])
        na = self._dagen([f"2026-05-{d:02d}" for d in range(1, 8)])
        assert match_seasonally(voor, na) == ([], [])

    def test_alleen_de_vergelijkbare_dagen_tellen_mee(self):
        """Niet alles-of-niets: de dagen dichtbij de grens houden hun oordeel,
        de rest valt eruit. Anders maakt een langer wordende reeks de
        vergelijking juist ongeldig — meer data die minder oplevert."""
        voor = self._dagen([f"2026-02-{d:02d}" for d in range(1, 20)])
        na = self._dagen(
            [f"2026-03-{d:02d}" for d in range(1, 8)]
            + [f"2026-06-{d:02d}" for d in range(1, 8)]
        )
        _, gematcht_na = match_seasonally(voor, na)
        assert len(gematcht_na) == 7
        assert all(d["date"] < "2026-04" for d in gematcht_na)

    def test_het_jaareinde_leest_niet_als_een_half_jaar(self):
        """Circulair meten: 20 december en 5 januari liggen dicht bij elkaar."""
        voor = self._dagen([f"2025-12-{d:02d}" for d in range(15, 32)])
        na = self._dagen([f"2026-01-{d:02d}" for d in range(1, 8)])
        _, gematcht_na = match_seasonally(voor, na)
        assert len(gematcht_na) == 7

    def test_een_onvergelijkbare_periode_krijgt_geen_oordeel(self):
        """Wel gestookt, wel beoordeeld, maar niets om het tegen af te zetten."""
        basis = _seizoen(_echte_cop, n=180, start="2025-11-01")
        laat = _frame(
            [_dag(9.0, _echte_cop(9.0) * 0.85)] * 20, start="2026-06-10"
        )
        res = calculate_cop_performance(
            pd.concat([basis, laat]), baseline_date="2026-06-10"
        )
        assert res.after_days_total == 20, "er is wel degelijk gestookt"
        assert not res.delta_comparable
        assert not res.delta_significant
        assert res.delta_pct is None, "geen getal zonder vergelijkbare dagen"


class TestVoorEnNaRandgevallen:
    def test_een_onbruikbare_datum_wordt_gemeld_niet_verzwegen(self):
        """Te weinig historie vóór de markering: dan valt de maat terug op het
        schuivende venster, en dat hoort zichtbaar te zijn."""
        res = calculate_cop_performance(
            _seizoen(_echte_cop, n=120, start="2025-11-01"),
            baseline_date="2025-11-03",
        )
        assert res.baseline_requested == "2025-11-03"
        assert not res.baseline_explicit

    def test_onleesbare_datum_valt_terug_op_het_venster(self):
        res = calculate_cop_performance(
            _seizoen(_echte_cop, n=120), baseline_date="geen datum"
        )
        assert res.rolling_30d is not None
        assert not res.baseline_explicit


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

    def test_de_losse_dag_staat_niet_meer_in_de_attributen(self):
        """Een getal tonen met "dit betekent niets" eronder is netto negatief:
        het is het eerste waar het oog op valt."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        src = inspect.getsource(
            QuattCopPerformanceSensor.extra_state_attributes.fget
        )
        assert "laatste_ratio" not in src
        assert "rolling_7d" not in src

    def test_de_sensor_meldt_of_de_norm_bevroren_is(self):
        """Zonder deze vlag leest het getal betrouwbaarder dan het is."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        src = inspect.getsource(
            QuattCopPerformanceSensor.extra_state_attributes.fget
        )
        for sleutel in (
            "norm_bevroren",
            "norm_grens",
            "verschil_pct",
            "verschil_significant",
            "dagspreiding_pct",
        ):
            assert sleutel in src, f"ontbreekt: {sleutel}"


class TestVensterIsStookdagenGeenKalenderdagen:
    """Het venster telt stookdagen, en dat moet ook zo op het scherm staan.

    ``ratios[-30:]`` pakt de laatste 30 *rijen*, en rijen zijn stookdagen. Eind
    augustus gaat dat getal dus over april en mei. De kaart noemde het "het
    30-daags gemiddelde", wat leest als de afgelopen maand — daarom staan de
    datums van het venster nu in de attributen.
    """

    def test_venster_slaat_zomerdagen_over(self):
        winter = _seizoen(_echte_cop, n=90, start="2025-11-01")
        zomer = _frame([_dag(24.0, 0.1, heat=0.0)] * 60, start="2026-06-01")
        res = calculate_cop_performance(pd.concat([winter, zomer]))
        # De beoordeelde dagen eindigen in de winter, niet in de zomer.
        assert res.daily[-1]["temp"] < 15
        assert res.rolling_30d is not None

    def test_de_sensor_publiceert_de_grenzen_van_het_venster(self):
        """Zonder deze datums is niet te zien hoe oud het getal is."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        src = inspect.getsource(
            QuattCopPerformanceSensor.extra_state_attributes.fget
        )
        for sleutel in (
            "venster_van",
            "venster_tot",
            "dagen_sinds_laatste_stookdag",
            "stookdagen",
        ):
            assert sleutel in src, f"ontbreekt: {sleutel}"

    def test_de_volledige_reeks_gaat_niet_de_recorder_in(self):
        """Honderden dagen bij elke state-write is verspilling; een grafiek
        leest het attribuut rechtstreeks."""
        from custom_components.quatt_stooklijn.sensor import (
            QuattCopPerformanceSensor,
        )

        assert "stookdagen" in QuattCopPerformanceSensor._unrecorded_attributes


class TestRandgevallen:
    def test_geen_data(self):
        assert calculate_cop_performance(None).rolling_30d is None
        assert calculate_cop_performance(pd.DataFrame()).rolling_30d is None

    def test_ontbrekende_kolom(self):
        df = _frame([{"avg_temperatureOutside": 5.0, "averageCOP": 3.0}])
        assert calculate_cop_performance(df).rolling_30d is None

    def test_te_weinig_bins_geeft_niets(self):
        """Met één temperatuurniveau valt er niet te interpoleren."""
        res = calculate_cop_performance(_frame([_dag(5.0, 3.5)] * 10))
        assert res.rolling_30d is None
        assert len(res.reference) < MIN_BINS

    def test_zomerdagen_tellen_niet_mee(self):
        """Idle dagen mogen de norm niet verdunnen."""
        winter = _seizoen(_echte_cop, n=90, start="2025-11-01")
        zomer = _frame([_dag(20.0, 0.1, heat=0.0)] * 60, start="2026-06-01")
        res = calculate_cop_performance(pd.concat([winter, zomer]))
        assert all(d["temp"] < 15 for d in res.daily)

    def test_cop_nul_wordt_genegeerd(self):
        winter = _seizoen(_echte_cop, n=90, start="2025-11-01")
        kapot = _frame([_dag(5.0, 0.0)] * 5, start="2026-03-01")
        res = calculate_cop_performance(pd.concat([winter, kapot]))
        assert all(d["cop"] > 0 for d in res.daily)

    def test_inf_wordt_genegeerd(self):
        winter = _seizoen(_echte_cop, n=90, start="2025-11-01")
        kapot = _frame([_dag(5.0, float("inf"))] * 5, start="2026-03-01")
        res = calculate_cop_performance(pd.concat([winter, kapot]))
        assert all(np.isfinite(d["cop"]) for d in res.daily)


class TestAardVanDeMaat:
    def test_het_gemiddelde_over_alles_ligt_rond_een(self):
        """Sluitcontrole: de norm is een mediaan over dezelfde soort dagen, dus
        bij ongewijzigd gedrag ligt alles eromheen."""
        res = calculate_cop_performance(_seizoen(_echte_cop, n=150))
        alle = float(np.mean([d["ratio"] for d in res.daily]))
        assert alle == pytest.approx(1.0, abs=0.03)
