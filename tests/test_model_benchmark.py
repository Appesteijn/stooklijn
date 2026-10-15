"""Tests voor de vergelijking tussen het tweetoestandsmodel en het huidige.

De inzet: deze sensor is het enige wat straks antwoord geeft op "werkt 2R2C al
beter?". Een vergelijking die stilletjes scheef staat is erger dan geen
vergelijking, want je gelooft hem. Twee dingen moeten daarom vastliggen: dat
beide modellen op exact dezelfde weggehouden data scoren, en dat de
uitlijning tussen voorspelling en meting klopt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis import model_benchmark as mb
from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    MIN_TRAIN_HOURS,
    Validation,
)
from custom_components.quatt_stooklijn.sensor import QuattModelBenchmarkSensor

from .test_thermal_model import TestOnlineRCModel
from .test_thermal_model_2r2c import WAAR, _genereer

T0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def _rijen(n_uur: int, per_uur: int = 12, start: int = 0, **waarden):
    """Logboekrijen: ``per_uur`` monsters per uur, constant binnen het uur."""
    rijen = []
    stap = 3600 // per_uur
    for i in range(start, start + n_uur):
        for j in range(per_uur):
            rijen.append([
                T0 + i * 3600 + j * stap,
                waarden.get("t_in", 20.0 + 0.01 * i),
                waarden.get("t_out", 5.0),
                waarden.get("q_hp", 1000.0),
                waarden.get("q_solar", 100.0),
            ])
    return rijen


def _fijne_rijen(n_uur: int, seed: int = 1):
    """Logboekrijen uit een synthetisch tweetoestandshuis op vijf minuten.

    Echt vijfminutenwerk, niet een uurreeks die twaalf keer herhaald is: het
    2R2C leert op deze stap, en een trap per uur is geen huis.
    """
    t_in, t_out, solar, q = _genereer(WAAR, n=n_uur * 12, ruis=0.003, seed=seed,
                                      dt_hours=1.0 / 12.0, pendelen=True)[0]
    return [[T0 + i * 300, t_in[i], t_out[i], q[i], solar[i]]
            for i in range(len(t_in))]


class TestResample:
    """Van vijfminutenmonsters naar aaneengesloten uurreeksen."""

    def test_uurgemiddelde(self):
        rijen = []
        for j in range(12):
            rijen.append([T0 + j * 300, 20.0 + j, 5.0, 1000.0, 100.0])
        for j in range(12):
            rijen.append([T0 + 3600 + j * 300, 30.0, 5.0, 1000.0, 100.0])
        (t_in, _t_out, _sol, _q), = mb.resample(rijen)
        # 20..31 gemiddeld is 25,5; het tweede uur is overal 30.
        assert t_in == pytest.approx([25.5, 30.0])

    def test_gat_breekt_de_reeks(self):
        rijen = _rijen(4) + _rijen(4, start=10)
        segmenten = mb.resample(rijen)
        assert [len(s[0]) for s in segmenten] == [4, 4]

    def test_teruglopende_klok_wordt_gesorteerd(self):
        """Na een NTP-correctie staat het logboek niet op tijdsvolgorde."""
        rijen = _rijen(6)
        door_elkaar = rijen[36:] + rijen[:36]
        assert mb.resample(door_elkaar)[0][0] == pytest.approx(
            mb.resample(rijen)[0][0]
        )

    def test_te_dun_uur_vervalt_en_breekt(self):
        """Een uur met twee monsters is een momentopname, geen uurgemiddelde."""
        rijen = _rijen(3) + _rijen(1, per_uur=2, start=3) + _rijen(3, start=4)
        assert [len(s[0]) for s in mb.resample(rijen)] == [3, 3]

    def test_te_weinig_monsters(self):
        assert mb.resample([]) == []
        assert mb.resample([[T0, 20.0, 5.0, 0.0, 0.0]]) == []

    def test_kolomvolgorde_is_die_van_het_model(self):
        """Segment = (t_binnen, t_buiten, zon, warmtepomp) — niet die van het log."""
        rijen = _rijen(3, t_in=21.0, t_out=6.0, q_hp=1500.0, q_solar=250.0)
        t_in, t_out, solar, q = mb.resample(rijen)[0]
        assert (t_in[0], t_out[0], solar[0], q[0]) == (21.0, 6.0, 250.0, 1500.0)

    def test_kort_gat_wordt_overbrugd(self):
        """Een herstart kost een paar monsters; op vijf minuten mag dat de
        reeks niet breken, anders haalt geen enkel blok de minimumlengte."""
        rijen = [[T0 + i * 300, 20.0 + 0.01 * i, 5.0, 1000.0, 100.0]
                 for i in range(48) if i not in (20, 21, 22)]
        (t_in, *_), = mb.resample(rijen, 5.0 / 60.0, max_fill_hours=0.5)
        assert len(t_in) == 48
        # Lineair ingevuld: het gat ligt op de lijn van de omliggende metingen.
        assert t_in[21] == pytest.approx(20.21)

    def test_lang_gat_breekt_alsnog(self):
        rijen = [[T0 + i * 300, 20.0, 5.0, 1000.0, 100.0]
                 for i in range(48) if not 10 <= i < 20]
        segmenten = mb.resample(rijen, 5.0 / 60.0, max_fill_hours=0.5)
        assert [len(s[0]) for s in segmenten] == [10, 28]

    def test_zonder_overbrugging_breekt_elk_gat(self):
        """Het standaardgedrag blijft wat het was."""
        rijen = [[T0 + i * 300, 20.0, 5.0, 1000.0, 100.0]
                 for i in range(48) if i != 20]
        assert [len(s[0]) for s in mb.resample(rijen, 5.0 / 60.0)] == [20, 27]


class TestSplitsingOpWeek:
    """Train en test op de klok, zodat beide stapgroottes dezelfde weken zien."""

    def test_om_en_om(self):
        week = 7 * 24 * 3600
        begin = (T0 // week + 1) * week  # begin van een even of oneven week
        rijen = [[begin + i * 3600, 20.0, 5.0, 0.0, 0.0] for i in range(4 * 168)]
        train, test = mb.split_by_week(rijen)
        assert len(train) == len(test) == 2 * 168
        weken_train = {int(r[0] // week) % 2 for r in train}
        weken_test = {int(r[0] // week) % 2 for r in test}
        assert len(weken_train) == len(weken_test) == 1
        assert weken_train != weken_test

    def test_week_blijft_in_dezelfde_helft_als_het_logboek_groeit(self):
        """Met een teller over de reeksen schoof de hele verdeling op zodra er
        vooraan iets afviel. Op de klok kan dat niet."""
        rijen = [[T0 + i * 3600, 20.0, 5.0, 0.0, 0.0] for i in range(1000)]
        train_a, _ = mb.split_by_week(rijen)
        train_b, _ = mb.split_by_week(rijen[300:] + [[T0 + 1000 * 3600, 20.0, 5.0, 0.0, 0.0]])
        a = {r[0] for r in train_a if r[0] >= T0 + 300 * 3600}
        b = {r[0] for r in train_b if r[0] < T0 + 1000 * 3600}
        assert a == b

    def test_leeg(self):
        train, test = mb.split_by_week([])
        assert len(train) == 0 and len(test) == 0


class TestReferentie:
    """De score van het model dat nu draait."""

    def test_uitlijning(self):
        """Een wereld die het 1R1C exact gehoorzaamt moet fout nul geven.

        Dit is de test die een stap verschuiving vangt. Zou de voorspelling
        één stap scheef tegen de meting liggen, dan loopt de fout meteen op
        tot de dagelijkse temperatuurschommeling.
        """
        model = TestOnlineRCModel._make_trained_model()
        n = 400
        t_out = 5.0 + 5.0 * np.sin(2 * np.pi * np.arange(n) / 24)
        solar = np.maximum(0.0, 300 * np.sin(2 * np.pi * (np.arange(n) - 6) / 24))
        q = np.full(n, 1500.0)
        t_in = np.empty(n)
        t_in[0] = 20.0
        for k in range(n - 1):
            t_in[k + 1] = model.predict_t_indoor(
                t_in[k], t_out[k], q[k], solar[k], 1.0
            )
        fout = mb.reference_rmse(model, [(t_in, t_out, solar, q)], horizon=12)
        assert fout == pytest.approx(0.0, abs=1e-9)

    def test_geen_bruikbare_vensters(self):
        """Korter dan inlooptijd plus horizon levert geen enkel venster op."""
        model = TestOnlineRCModel._make_trained_model()
        kort = [(np.zeros(10), np.zeros(10), np.zeros(10), np.zeros(10))]
        assert mb.reference_rmse(model, kort, horizon=12) is None

    def test_raakt_het_draaiende_model_niet_aan(self):
        model = TestOnlineRCModel._make_trained_model()
        voor = model.to_dict()
        segs = _genereer(WAAR, n=300)
        mb.reference_rmse(model, segs, horizon=12)
        assert model.to_dict() == voor


class TestVergelijking:
    """De orkestratie: fit, scoor, oordeel."""

    def test_zelfde_testset_als_de_poort(self, monkeypatch):
        """De referentie moet op precies de reeksen scoren die de poort toetst.

        Niet een tweede splitsing die toevallig hetzelfde uitkomt, maar
        letterlijk dezelfde lijst. Twee keer splitsen is twee kansen om uit
        elkaar te lopen.
        """
        gezien = {}

        def _nep_referentie(model, segments, horizon, burn_in=None, dt_hours=1.0):
            gezien["referentie"] = segments
            return 0.5

        def _nep_poort(train, test, horizon, **kwargs):
            gezien["poort"] = test
            gezien["kwargs"] = kwargs
            return Validation(params=None, report={}, rmse=None,
                              reference_rmse=None, persistence_rmse=None,
                              accepted=False, reason="gestopt voor de test")

        monkeypatch.setattr(mb, "reference_rmse", _nep_referentie)
        monkeypatch.setattr(mb, "fit_and_score", _nep_poort)
        mb.compare(_fijne_rijen(900), MagicMock())

        assert gezien["poort"] is gezien["referentie"]
        assert gezien["poort"]
        # Leren op vijf minuten, toetsen per uur.
        assert gezien["kwargs"]["fit_dt_hours"] == pytest.approx(5.0 / 60.0)
        assert gezien["kwargs"]["test_dt_hours"] == pytest.approx(1.0)

    def test_te_weinig_data(self):
        """Met een paar dagen logboek hoort er geen oordeel te komen."""
        uitkomst = mb.compare(_rijen(48), MagicMock())
        assert uitkomst.accepted is False
        assert "te weinig" in uitkomst.reason
        assert uitkomst.rmse_k is None
        # Het dashboard moet kunnen zeggen hoe ver we nog af zijn.
        assert uitkomst.data_hours == pytest.approx(48.0)
        assert uitkomst.required_train_hours == MIN_TRAIN_HOURS
        assert uitkomst.train_hours < uitkomst.required_train_hours

    def test_meldt_de_echte_trainingsuren(self):
        """Het dashboard moet de trainingsuren tegen de eis kunnen zetten, niet
        het logboektotaal: gaten en te korte stukken tellen niet mee."""
        uitkomst = mb.compare(_fijne_rijen(900), MagicMock())
        d = uitkomst.to_dict()
        assert 0 < d["train_uren"] < d["data_uren"]
        assert d["test_uren"] > 0
        assert d["leerstap_minuten"] == pytest.approx(5.0)
        assert d["stap_uren"] == pytest.approx(1.0)
        assert d["benodigde_train_uren"] == MIN_TRAIN_HOURS

    def test_leeg_logboek(self):
        uitkomst = mb.compare([], MagicMock())
        assert uitkomst.reason == "nog geen bruikbare meetdata"
        assert uitkomst.data_hours == 0.0

    def test_beter_model_wordt_aangenomen(self):
        """Op data die een tweetoestandshuis maakte hoort 2R2C te winnen."""
        model = TestOnlineRCModel._make_trained_model()
        uitkomst = mb.compare(_fijne_rijen(1200), model)
        assert uitkomst.accepted is True, uitkomst.reason
        assert uitkomst.rmse_k < uitkomst.reference_rmse_k
        assert uitkomst.verbetering_pct > 0
        assert uitkomst.params["tau_fast_h"] < uitkomst.params["tau_slow_h"]

    def test_zonder_draaiend_model_alleen_de_ondergrens(self):
        """Geen referentie betekent niet: poort open. Niets doen blijft staan."""
        uitkomst = mb.compare(_fijne_rijen(1200), None)
        assert uitkomst.reference_rmse_k is None
        assert uitkomst.persistence_rmse_k is not None

    def test_verbetering_zonder_referentie(self):
        uitkomst = mb.Comparison(
            rmse_k=0.3, reference_rmse_k=None, persistence_rmse_k=0.5,
            accepted=True, reason="", horizon_hours=12.0, dt_hours=1.0,
            data_hours=1000.0, required_train_hours=672.0, params=None,
            fit_report={},
        )
        assert uitkomst.verbetering_pct is None


def _sensor(resultaat):
    sensor = QuattModelBenchmarkSensor.__new__(QuattModelBenchmarkSensor)
    sensor._result = resultaat
    sensor._running = False
    return sensor


def _uitkomst(**kw):
    basis = dict(rmse_k=0.3, reference_rmse_k=0.45, persistence_rmse_k=0.58,
                 accepted=False, reason="", horizon_hours=12.0, dt_hours=1.0,
                 data_hours=1000.0, required_train_hours=672.0, params=None,
                 fit_report={})
    basis.update(kw)
    return mb.Comparison(**basis)


class TestSensorStand:
    """De stand moet in één woord zeggen waar de poort staat."""

    def test_nog_niet_gemeten(self):
        sensor = _sensor(None)
        assert sensor.native_value == "nog niet gemeten"
        assert sensor.extra_state_attributes == {"gemeten": False}

    @pytest.mark.parametrize("reden,verwacht", [
        ("te weinig trainingsdata", "te weinig data"),
        ("te weinig testdata", "te weinig data"),
        ("nog geen bruikbare meetdata", "te weinig data"),
        ("geen bruikbare testvensters", "te weinig data"),
        ("fit convergeerde niet", "fit mislukt"),
        ("fit mislukt: singuliere matrix", "fit mislukt"),
        ("niet genoeg beter dan het huidige model", "niet beter"),
        ("niet beter dan aannemen dat er niets verandert", "niet beter"),
    ])
    def test_redenen(self, reden, verwacht):
        assert _sensor(_uitkomst(reason=reden)).native_value == verwacht

    def test_aangenomen(self):
        sensor = _sensor(_uitkomst(accepted=True, reason="beter op weggehouden data"))
        assert sensor.native_value == "beter"
        assert sensor.extra_state_attributes["gemeten"] is True
        assert sensor.extra_state_attributes["verbetering_pct"] == pytest.approx(33.3)


class TestVerbeteringRandgevallen:
    """De percentagewinst moet niet op nul struikelen."""

    def test_rmse_nul_is_een_uitkomst(self):
        uitkomst = _uitkomst(rmse_k=0.0, reference_rmse_k=0.4)
        assert uitkomst.verbetering_pct == pytest.approx(100.0)

    def test_referentie_nul_geeft_geen_deling_door_nul(self):
        assert _uitkomst(rmse_k=0.2, reference_rmse_k=0.0).verbetering_pct is None


class TestSensorBedrading:
    """Van meetlogboek naar uitkomst, langs het pad dat HA straks loopt."""

    @staticmethod
    def _hass():
        hass = MagicMock()

        async def _executor(func, *args):
            return func(*args)

        hass.async_add_executor_job = _executor
        return hass

    @pytest.mark.asyncio
    async def test_meet_en_publiceert(self):
        sensor = _sensor(None)
        sensor.hass = self._hass()
        sensor.async_write_ha_state = MagicMock()
        sensor._mpc = MagicMock()
        sensor._mpc.highres_samples = _fijne_rijen(1200)
        sensor._mpc.thermal_model = TestOnlineRCModel._make_trained_model()

        await sensor._async_run()

        assert sensor.native_value == "beter"
        assert sensor.extra_state_attributes["rmse_k"] is not None
        sensor.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_leeg_logboek_doet_niets(self):
        """Vóór de eerste monsters hoort er geen meting en geen fout te zijn."""
        sensor = _sensor(None)
        sensor.hass = self._hass()
        sensor.async_write_ha_state = MagicMock()
        sensor._mpc = MagicMock(highres_samples=[])

        await sensor._async_run()

        assert sensor.native_value == "nog niet gemeten"
        sensor.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_mislukte_meting_laat_de_vorige_staan(self):
        vorige = _uitkomst(accepted=True, reason="beter op weggehouden data")
        sensor = _sensor(vorige)
        sensor.hass = MagicMock()

        async def _boem(func, *args):
            raise ValueError("singuliere matrix")

        sensor.hass.async_add_executor_job = _boem
        sensor.async_write_ha_state = MagicMock()
        sensor._mpc = MagicMock(highres_samples=[[0, 20.0, 5.0, 0.0, 0.0]])

        await sensor._async_run()

        assert sensor.native_value == "beter"
        assert sensor._running is False

    @pytest.mark.asyncio
    async def test_geen_tweede_ronde_naast_de_eerste(self):
        """Op trage hardware mag een nieuwe tik er geen thread naast zetten."""
        sensor = _sensor(None)
        sensor.hass = MagicMock()
        sensor.hass.async_add_executor_job = MagicMock()
        sensor._mpc = MagicMock(highres_samples=[[0, 20.0, 5.0, 0.0, 0.0]])
        sensor._running = True

        await sensor._async_run()

        sensor.hass.async_add_executor_job.assert_not_called()
