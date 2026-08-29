"""Tests voor de COP-gewogen herverdeling van de warmtevraag.

De harde eis is energie-neutraliteit: er mag geen warmte bij komen of verdwijnen,
alleen verschuiven. Alles daaronder is optimalisatie; dit is veiligheid.
"""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.demand_shift import (
    ADVIES_DREMPEL,
    GAMMA_MAX,
    calculate_demand_shift,
    scan_gamma,
)

# De gemeten curve van deze installatie (11 bins, oktober–april).
CURVE = {
    -5.0: 1.85, -3.0: 2.11, -1.0: 2.11, 1.0: 2.49, 3.0: 3.09, 5.0: 3.71,
    7.0: 4.11, 9.0: 4.23, 11.0: 4.33, 13.0: 4.55, 15.0: 4.15,
}
UA = 284.8
T0 = 16.5

# Een winterdag: 's nachts −2, 's middags +4.
DAG = [-2.0, -2.5, -3.0, -2.0, 0.0, 2.0, 4.0, 4.0, 3.0, 1.0, -1.0, -2.0]


def _shift(gamma, temps=DAG, curve=CURVE, **kw):
    return calculate_demand_shift(temps, curve, UA, T0, gamma, **kw)


class TestEnergieNeutraliteit:
    """De randvoorwaarde waar alles op rust."""

    @pytest.mark.parametrize("gamma", [0.0, 0.5, 1.0, 2.0, 3.0])
    def test_totaal_blijft_gelijk(self, gamma):
        r = _shift(gamma)
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)

    def test_ook_bij_een_vlakke_dag(self):
        r = _shift(2.0, temps=[0.0] * 12)
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)

    def test_niets_wordt_negatief(self):
        assert all(p >= 0 for p in _shift(3.0).shifted)


class TestUitStand:
    """gamma=0 moet exact het huidige gedrag geven — dat is de terugvalgarantie."""

    def test_gamma_nul_laat_de_reeks_ongemoeid(self):
        r = _shift(0.0)
        assert r.shifted == r.flat
        assert r.now_shifted == r.now_flat
        assert r.expected_saving == 0.0

    def test_negatieve_gamma_wordt_naar_nul_geklemd(self):
        assert _shift(-1.0).shifted == _shift(0.0).flat

    def test_gamma_wordt_begrensd(self):
        assert _shift(99.0).gamma == GAMMA_MAX


class TestVerschuiving:
    def test_warme_uren_krijgen_meer(self):
        r = _shift(1.5)
        warmste = int(np.argmax(DAG))
        koudste = int(np.argmin(DAG))
        # Vlak vraagt het koudste uur juist het meest; na weging draait dat om.
        assert r.flat[koudste] > r.flat[warmste]
        assert r.shifted[warmste] > r.shifted[koudste]

    def test_hogere_gamma_verschuift_meer(self):
        warmste = int(np.argmax(DAG))
        reeks = [_shift(g).shifted[warmste] for g in (0.0, 1.0, 2.0, 3.0)]
        assert reeks == sorted(reeks), "meer gamma hoort meer te verschuiven"

    def test_besparing_is_positief_en_groeit(self):
        s1 = _shift(1.0).expected_saving
        s2 = _shift(2.0).expected_saving
        assert 0 < s1 < s2

    def test_besparing_blijft_realistisch(self):
        """Een dagsprong van 7 K mag geen absurde winst voorspellen."""
        assert _shift(1.0).expected_saving < 0.25


class TestRandgevallen:
    def test_geen_forecast(self):
        r = calculate_demand_shift([], CURVE, UA, T0, 1.0)
        assert r.now_shifted is None and r.shifted == []

    @pytest.mark.parametrize("ua,t0", [(None, T0), (UA, None), (0.0, T0), (-5.0, T0)])
    def test_onbruikbaar_huismodel(self, ua, t0):
        assert calculate_demand_shift(DAG, CURVE, ua, t0, 1.0).now_shifted is None

    def test_zomer_geeft_nul(self):
        """Boven het nulpunt is er geen vraag om te verdelen."""
        r = _shift(2.0, temps=[20.0] * 12)
        assert sum(r.shifted) == 0
        assert r.now_shifted == 0

    def test_zonder_curve_valt_hij_terug_op_vlak(self):
        r = _shift(2.0, curve={})
        assert r.shifted == r.flat
        assert r.expected_saving == 0.0

    def test_vorst_onder_het_meetbereik_werkt_nog(self):
        """Klemmen op de rand, niet uitvallen — juist dán is de winst groot."""
        r = _shift(1.5, temps=[-15.0, -14.0, -8.0, -2.0, 0.0, 2.0])
        assert r.now_shifted is not None
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)

    def test_plafond_wordt_gesignaleerd_niet_geklemd(self):
        r = _shift(3.0, ceiling_w=1000.0)
        assert r.hours_above_ceiling > 0
        # Bewust niet klemmen: dat zou verzadiging onzichtbaar maken.
        assert max(r.shifted) > 1000.0
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)


class TestVensterlengte:
    """Het venster is de bepalende factor, en staat los van de displayforecast.

    Gemeten op een dag rond 0 °C met 8 K zwaai: 6 uur levert 0,09%, 24 uur 6,7%.
    Met het venster van de MPC-displaysensor valt er dus niets te verdelen —
    vandaar een eigen constante.
    """

    @staticmethod
    def _dag(n):
        return [4.0 * np.sin((i - 8) / 24 * 2 * np.pi) for i in range(n)]

    def test_langer_venster_levert_meer_op(self):
        winst = [
            calculate_demand_shift(self._dag(n), CURVE, UA, T0, 1.0).expected_saving
            for n in (6, 12, 24)
        ]
        assert winst == sorted(winst)
        assert winst[0] < 0.005, "zes uur hoort verwaarloosbaar te zijn"
        assert winst[-1] > 0.03, "vierentwintig uur hoort wel wat op te leveren"

    def test_schaduwsensor_gebruikt_het_lange_venster(self):
        """Niet dat van de displayforecast — dat is het hele punt van stap 3."""
        import inspect

        from custom_components.quatt_stooklijn.const import (
            DEMAND_SHIFT_HOURS,
            MPC_FORECAST_HOURS,
        )
        from custom_components.quatt_stooklijn.sensor import (
            QuattShiftedHeatDemandSensor,
        )

        assert DEMAND_SHIFT_HOURS > MPC_FORECAST_HOURS
        # De vensterkeuze zit in _ingangen: die bouwt de forecast één keer op
        # voor zowel _shift als de gamma-scan.
        src = inspect.getsource(QuattShiftedHeatDemandSensor._ingangen)
        assert "DEMAND_SHIFT_HOURS" in src

    def test_uren_zonder_vraag_blijven_nul(self):
        """Nooit warmte schuiven naar uren boven het nulpunt.

        Anders laat je de firmware stoken boven haar eigen stookgrens. Dit kwam
        aan het licht op een zomerforecast: alle COP's klemmen dan op de
        curverand, de gewichten worden gelijk, en zonder masker smeert de vraag
        uit over het hele venster.
        """
        temps = [10.0] * 6 + [22.0] * 6
        r = calculate_demand_shift(temps, CURVE, UA, T0, 1.0)
        assert all(p == 0 for p in r.flat[6:]), "testopzet: tweede helft vraagt niets"
        assert all(p == 0 for p in r.shifted[6:]), "daar mag niets heen geschoven worden"
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)

    def test_volledig_boven_het_nulpunt_blijft_vlak(self):
        temps = [20.0] * 12
        r = calculate_demand_shift(temps, CURVE, UA, T0, 2.0)
        assert r.shifted == r.flat == [0.0] * 12


C_WH_K = 25583.0  # thermische massa uit het geleerde RC-model


class TestComfortbewaking:
    """De kamer mag niet wegzakken, en de limiter mag de balans niet breken."""

    def test_drift_wordt_geschat(self):
        r = _shift(1.0, thermal_mass_wh_k=C_WH_K)
        assert r.worst_drift_k is not None
        assert r.worst_drift_k < 0, "verschuiven kost tijdelijk warmte"

    def test_hogere_gamma_geeft_meer_drift(self):
        drifts = [
            abs(_shift(g, thermal_mass_wh_k=C_WH_K).worst_drift_k)
            for g in (0.5, 1.0, 2.0, 3.0)
        ]
        assert drifts == sorted(drifts)

    def test_limiter_houdt_zich_aan_de_grens(self):
        r = _shift(3.0, thermal_mass_wh_k=C_WH_K, max_drift_k=0.1)
        assert abs(r.worst_drift_k) <= 0.1 + 1e-6
        assert r.drift_limit_factor < 1.0

    def test_limiter_breekt_de_energiebalans_niet(self):
        """Terugschalen mag geen warmte laten verdwijnen — dat is de hele eis."""
        r = _shift(3.0, thermal_mass_wh_k=C_WH_K, max_drift_k=0.05)
        assert sum(r.shifted) == pytest.approx(sum(r.flat), rel=1e-6)

    def test_ruime_grens_grijpt_niet_in(self):
        r = _shift(1.0, thermal_mass_wh_k=C_WH_K, max_drift_k=5.0)
        assert r.drift_limit_factor == 1.0

    def test_zonder_massa_geen_schatting(self):
        """Liever niets dan een drift die op een verzonnen C gebaseerd is."""
        assert _shift(1.0).worst_drift_k is None

    def test_gamma_nul_drijft_niet(self):
        r = _shift(0.0, thermal_mass_wh_k=C_WH_K)
        assert r.worst_drift_k in (None, 0.0) or abs(r.worst_drift_k) < 1e-9


class TestMpcKoppeling:
    """De schaduwsensor haalt de thermische massa bij de MPC-sensor op.

    Deze test voert de property écht uit in plaats van de broncode als tekst te
    controleren. Dat onderscheid is niet theoretisch: een eerdere versie riep
    ``self.model`` aan in plaats van ``self.thermal_model``, en dat glipte langs
    alle bestaande tests omdat die alleen naar strings keken. In HA gaf het een
    AttributeError bij elke state-write.
    """

    def test_thermal_params_draait_op_een_echt_object(self):
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

        class _ZonderModel:
            thermal_model = None

        # Roept de property-body aan; een verkeerde attribuutnaam knalt hier.
        assert QuattMpcSensor.thermal_params.fget(_ZonderModel()) == {
            "converged": False
        }

    def test_thermal_params_geeft_de_modelparameters_door(self):
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

        class _Model:
            params = {"converged": True, "C_whk": 25583.0}

        class _MetModel:
            thermal_model = _Model()

        out = QuattMpcSensor.thermal_params.fget(_MetModel())
        assert out["C_whk"] == 25583.0

    def test_mpc_sensor_heeft_de_verwachte_koppelvlakken(self):
        """Beide namen die de schaduwsensor gebruikt moeten bestaan."""
        from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

        for naam in ("thermal_model", "thermal_params", "build_forecast_arrays"):
            assert hasattr(QuattMpcSensor, naam), f"ontbreekt: {naam}"


class TestGammaScan:
    """Gamma is niet uit te rekenen maar wel af te lezen.

    Zonder doorgerekende reeks moet je blind een getal kiezen en een maand
    wachten om te zien of het iets deed. De scan zet de opbrengst, de drift en
    de begrenzingen naast elkaar zodat de keuze zichtbaar wordt.
    """

    def _scan(self, **kw):
        return scan_gamma(DAG, CURVE, UA, T0, **kw)

    def test_loopt_het_hele_bereik_af(self):
        scan = self._scan()
        assert scan.punten
        assert min(p.gamma for p in scan.punten) > 0
        assert max(p.gamma for p in scan.punten) == pytest.approx(GAMMA_MAX)

    def test_gamma_nul_zit_er_niet_in(self):
        """Nul is de uit-stand, geen kandidaat: die levert per definitie niets."""
        assert all(p.gamma > 0 for p in self._scan().punten)

    def test_rond_de_knik_wordt_fijner_gerekend(self):
        """Aan de uiteinden de grove stap, rond het optimum de fijne."""
        scan = self._scan(grove_stap=0.5, fijne_stap=0.1)
        gammas = sorted(p.gamma for p in scan.punten)
        afstanden = [
            round(b - a, 2) for a, b in zip(gammas, gammas[1:], strict=False)
        ]
        assert 0.1 in afstanden, "nergens fijn gerekend"
        assert 0.5 in afstanden, "overal fijn gerekend, dat is geen verfijning"

    def test_opbrengst_loopt_op_met_gamma(self):
        punten = [p for p in self._scan().punten if p.schoon]
        besparingen = [p.besparing for p in punten]
        assert besparingen == sorted(besparingen)

    def test_advies_pakt_vrijwel_de_volle_winst(self):
        scan = self._scan()
        beste = max(p.besparing for p in scan.punten if p.schoon)
        assert scan.advies_besparing >= beste * ADVIES_DREMPEL

    def test_advies_is_de_rustigste_die_dat_haalt(self):
        """Gamma kost comfort, dus bij gelijke winst wint de laagste."""
        scan = self._scan()
        beste = max(p.besparing for p in scan.punten if p.schoon)
        lager = [
            p
            for p in scan.punten
            if p.schoon and p.gamma < scan.advies and p.besparing is not None
        ]
        assert all(p.besparing < beste * ADVIES_DREMPEL for p in lager)

    def test_plafond_maakt_een_punt_onbruikbaar(self):
        """Boven het firmwareplafond kapt Power House af.

        De besparing die het model daar berekent gaat over energie die de
        firmware niet levert, dus het punt telt niet mee als kandidaat.
        """
        scan = self._scan(ceiling_w=1500.0)
        vuil = [p for p in scan.punten if p.uren_boven_plafond > 0]
        assert vuil, "plafond te hoog gekozen, geen enkel punt raakt het"
        assert all(not p.schoon for p in vuil)
        if scan.advies is not None:
            assert scan.advies not in [p.gamma for p in vuil]

    def test_ingrijpende_driftlimiter_maakt_een_punt_onbruikbaar(self):
        scan = self._scan(thermal_mass_wh_k=800.0, max_drift_k=0.05)
        begrensd = [p for p in scan.punten if p.drift_begrenzing < 1.0]
        assert begrensd, "limiter greep nergens in"
        assert all(not p.schoon for p in begrensd)

    def test_zonder_bruikbaar_punt_geen_advies(self):
        scan = scan_gamma([], CURVE, UA, T0)
        assert scan.advies is None
        assert scan.advies_besparing is None

    def test_onzinnige_stap_geeft_een_lege_scan(self):
        assert self._scan(grove_stap=0).punten == []
