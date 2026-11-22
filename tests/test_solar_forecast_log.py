"""Zonverwachtingen per vooruitkijktijd bewaren en scoren (ZONNEWARMTE_PLAN.md)."""

from __future__ import annotations

import json

import pytest

from custom_components.quatt_stooklijn.analysis.solar_forecast_log import (
    KEEP_HOURS,
    SolarForecastLog,
)

H = 3600
T0 = 1_795_000_000 // H * H     # een heel uur


def _ophaling(log, fetched, waarden):
    """Verwachting vanaf het uur van ophalen: waarden[i] voor uur base + i."""
    base = fetched // H * H
    return log.record(fetched, [(base + i * H, v) for i, v in enumerate(waarden)])


class TestVastleggen:
    def test_vooruitkijktijd_telt_vanaf_het_uur_van_ophalen(self):
        log = SolarForecastLog()
        _ophaling(log, T0 + 20 * 60, [100, 200, 300])
        assert log._f[T0] == {0: 100}
        assert log._f[T0 + H] == {1: 200}
        assert log._f[T0 + 2 * H] == {2: 300}

    def test_alleen_tot_twaalf_uur_vooruit(self):
        log = SolarForecastLog()
        _ophaling(log, T0, list(range(48)))
        assert max(log._f) == T0 + 12 * H

    def test_verleden_telt_niet(self):
        log = SolarForecastLog()
        log.record(T0, [(T0 - H, 100.0)])
        assert len(log) == 0

    def test_eerste_ophaling_binnen_het_uur_wint(self):
        """Een herstart haalt opnieuw op; dat is geen nieuwe verwachting."""
        log = SolarForecastLog()
        _ophaling(log, T0 + 60, [0, 100])
        _ophaling(log, T0 + 1800, [0, 999])
        assert log._f[T0 + H][1] == 100

    def test_onbekende_waarde_overgeslagen(self):
        log = SolarForecastLog()
        assert log.record(T0, [(T0 + H, None), (T0 + 2 * H, float("nan"))]) == 0

    def test_opruimen(self):
        log = SolarForecastLog()
        _ophaling(log, T0, [1, 2, 3])
        log.prune(T0 + KEEP_HOURS * H + 1.5 * H)
        assert T0 not in log._f and T0 + 2 * H in log._f

    def test_rondreis_json(self):
        log = SolarForecastLog()
        _ophaling(log, T0, [10.04, 20.0])
        terug = SolarForecastLog.from_dict(json.loads(json.dumps(log.to_dict())))
        assert terug._f == {T0: {0: 10.0}, T0 + H: {1: 20.0}}

    def test_kapot_bestand_kost_alleen_de_kapotte_regel(self):
        log = SolarForecastLog.from_dict(
            {"forecasts": {str(T0): {"1": 5}, "x": {"1": 5}, str(T0 + H): "kapot"}}
        )
        assert log._f == {T0: {1: 5.0}}


def _log_met_verwachtingen(uren=24, verwacht=300.0):
    """Elk uur een ophaling met een vlakke verwachting voor 12 uur vooruit."""
    log = SolarForecastLog()
    for i in range(uren):
        _ophaling(log, T0 + i * H + 60, [verwacht] * 13)
    return log


class TestScoren:
    def test_tegen_meting(self):
        log = _log_met_verwachtingen(verwacht=300.0)
        gemeten = {T0 + i * H: (200.0, 0.4) for i in range(40)}
        s = log.score(T0 + 30 * H, gemeten)
        e = s["per_vooruitkijktijd"]["3"]
        assert s["referentie"] == "zonnesensor"
        assert e["bias"] == pytest.approx(100.0)
        assert e["mae"] == pytest.approx(100.0)
        assert e["n"] > 0

    def test_alleen_afgeronde_uren(self):
        log = _log_met_verwachtingen(uren=4)
        gemeten = {T0 + i * H: (200.0, 0.4) for i in range(40)}
        nu = T0 + 3 * H + 1800
        s = log.score(nu, gemeten)
        # Uur T0+3H loopt nog en telt niet.
        assert s["per_vooruitkijktijd"]["1"]["n"] == 2

    def test_nacht_telt_niet(self):
        log = _log_met_verwachtingen(verwacht=0.0)
        gemeten = {T0 + i * H: (0.0, None) for i in range(40)}
        assert log.score(T0 + 30 * H, gemeten)["per_vooruitkijktijd"]["1"]["n"] == 0

    def test_zonder_meting_tegen_de_laatste_verwachting(self):
        log = SolarForecastLog()
        _ophaling(log, T0, [0, 0, 0, 400])          # 3 uur vooruit: 400
        _ophaling(log, T0 + 3 * H, [250])            # vlak ervóór: 250
        s = log.score(T0 + 5 * H)
        assert s["referentie"] == "latere_verwachting"
        assert s["per_vooruitkijktijd"]["3"]["bias"] == pytest.approx(150.0)

    def test_persistentie_naast_de_verwachting(self):
        """Helderheid van L uur geleden × heldere hemel van nu."""
        log = _log_met_verwachtingen(verwacht=300.0)
        gemeten = {T0 + i * H: (200.0, 0.5) for i in range(40)}
        helder = {T0 + i * H: 500.0 for i in range(40)}
        e = log.score(T0 + 30 * H, gemeten, helder)["per_vooruitkijktijd"]["2"]
        assert e["persistentie_mae"] == pytest.approx(50.0)        # 0,5×500 − 200
        assert e["verwachting_mae_zelfde_uren"] == pytest.approx(100.0)

    def test_correctiefactor(self):
        log = _log_met_verwachtingen(verwacht=400.0)
        gemeten = {T0 + i * H: (300.0, 0.6) for i in range(40)}
        assert log.score(T0 + 30 * H, gemeten)["correctiefactor"] == pytest.approx(0.75)

    def test_leeg(self):
        s = SolarForecastLog().score(T0)
        assert s["per_vooruitkijktijd"]["3"] == {"n": 0}
        assert s["correctiefactor"] is None


class TestVooruitblikFactor:
    """De factor die de vooruitblik gebruikt: lopend uur / verwachting vooruit.

    Niet gemeten / verwacht: de huismodellen leren hun zonfactor op de
    Open-Meteo-waarde van het lopende uur. Live 22-11-2026 gaf gemeten /
    verwacht 0,77; die toepassen zou de zon dubbel omlaag halen.
    """

    @staticmethod
    def _dagen(log, n_days, nu_waarde, vooruit_waarde):
        """Per dag 8 zonuren; elk uur een ophaling met lead 0 en leads 1–6."""
        for d in range(n_days):
            for h in range(8, 16):
                start = T0 + d * 24 * H + h * H
                for lead in range(0, 7):
                    slot = log._f.setdefault(start, {})
                    slot[lead] = nu_waarde if lead == 0 else vooruit_waarde

    def test_verwachting_vooruit_te_hoog(self):
        log = SolarForecastLog()
        self._dagen(log, 10, 160.0, 200.0)
        assert log.lead_factor(T0 + 11 * 24 * H) == pytest.approx(0.8)

    def test_te_weinig_zonuren_geen_factor(self):
        log = SolarForecastLog()
        self._dagen(log, 2, 160.0, 200.0)       # 16 zonuren
        assert log.lead_factor(T0 + 3 * 24 * H) is None

    def test_begrensd(self):
        log = SolarForecastLog()
        self._dagen(log, 10, 20.0, 200.0)
        assert log.lead_factor(T0 + 11 * 24 * H) == 0.7

    def test_nachten_tellen_niet(self):
        log = SolarForecastLog()
        self._dagen(log, 10, 160.0, 200.0)
        for d in range(10):
            for h in range(0, 6):
                log._f[T0 + d * 24 * H + h * H] = {0: 0.0, 1: 0.0, 2: 0.0}
        assert log.lead_factor(T0 + 11 * 24 * H) == pytest.approx(0.8)

    def test_zonder_lopend_uur_telt_het_uur_niet(self):
        log = SolarForecastLog()
        self._dagen(log, 10, 160.0, 200.0)
        for s in log._f:
            log._f[s].pop(0)
        assert log.lead_factor(T0 + 11 * 24 * H) is None
