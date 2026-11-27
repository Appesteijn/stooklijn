"""Extra kamersensoren: vast verschil, zonuren per sensor, samenstellen.

Zie KAMERSENSOREN_PLAN.md en ``analysis.room_reference``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.quatt_stooklijn.analysis.room_reference import (
    CLIP_K,
    compose,
    night_offsets,
    sun_hours,
)

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 11, 27, 22, 0, tzinfo=AMS).timestamp()


def _hours(days: int):
    start = datetime(2026, 11, 27, 0, 0, tzinfo=AMS) - timedelta(days=days)
    return [(start + timedelta(hours=k)).timestamp() for k in range(days * 24)]


def _hod(t):
    return datetime.fromtimestamp(t, AMS).hour


class TestVastVerschil:
    def test_mediaan_van_de_nachturen(self):
        hrs = _hours(7)
        prim = {t: 20.0 for t in hrs}
        # Overdag wijkt de referentie af (bijvoorbeeld zon); dat mag niet meetellen.
        ref = {t: 21.45 if _hod(t) < 6 else 22.5 for t in hrs}
        assert night_offsets(prim, {"midden": ref}, NOW, AMS) == {"midden": pytest.approx(-1.45)}

    def test_te_weinig_nachturen(self):
        hrs = _hours(1)[:5]
        assert night_offsets({t: 20.0 for t in hrs}, {"midden": {t: 21.0 for t in hrs}}, NOW, AMS) == {}

    def test_alleen_het_venster(self):
        oud = _hours(20)[:24 * 10]
        nieuw = _hours(7)
        prim = {t: 20.0 for t in oud + nieuw}
        ref = {**{t: 25.0 for t in oud}, **{t: 21.0 for t in nieuw}}
        assert night_offsets(prim, {"r": ref}, NOW, AMS)["r"] == pytest.approx(-1.0)


def _zonreeks(zon_op: dict[str, set[int]], amp=1.0, days=14):
    """Thermostaat en twee referenties; zon op de genoemde sensor op die uren,
    alleen op zonnige dagen (elke tweede dag)."""
    hrs = _hours(days)
    series = {n: {} for n in ("thermostaat", "midden", "bench")}
    clear = {}
    for t in hrs:
        dag = int((t - hrs[0]) // 86400)
        zonnig = dag % 2 == 0
        clear[t] = 0.9 if zonnig else 0.1
        for n in series:
            base = {"thermostaat": 20.0, "midden": 21.45, "bench": 20.6}[n]
            extra = amp if (zonnig and _hod(t) in zon_op.get(n, set())) else 0.0
            series[n][t] = base + extra
    offsets = {"thermostaat": 0.0, "midden": -1.45, "bench": -0.6}
    return series, offsets, clear


class TestZonuren:
    def test_testhuis(self):
        # Zoals november 2026: de iSense rond 14 u, de bench 11–12 u, midden nooit.
        series, offsets, clear = _zonreeks({"thermostaat": {14}, "bench": {11, 12}})
        uit = sun_hours(series, offsets, clear, NOW, AMS)
        assert uit == {"thermostaat": {14}, "midden": set(), "bench": {11, 12}}

    def test_twee_sensoren_teken_beslist(self):
        series, offsets, clear = _zonreeks({"thermostaat": {14}})
        del series["bench"]
        uit = sun_hours(series, offsets, clear, NOW, AMS)
        assert uit == {"thermostaat": {14}, "midden": set()}

    def test_echte_zonnewarmte_is_geen_sensorzon(self):
        # De hele kamer warmt op: alle sensoren evenveel. Geen enkele sensor krijgt zon.
        series, offsets, clear = _zonreeks({n: {13, 14, 15} for n in ("thermostaat", "midden", "bench")})
        assert all(not v for v in sun_hours(series, offsets, clear, NOW, AMS).values())

    def test_onder_de_drempel(self):
        series, offsets, clear = _zonreeks({"thermostaat": {14}}, amp=0.3)
        assert sun_hours(series, offsets, clear, NOW, AMS)["thermostaat"] == set()

    def test_zonder_bewolkte_uren_geen_uitspraak(self):
        series, offsets, clear = _zonreeks({"thermostaat": {14}})
        clear = {t: 0.9 for t in clear}
        assert sun_hours(series, offsets, clear, NOW, AMS)["thermostaat"] == set()

    def test_een_sensor_geen_uitspraak(self):
        series, offsets, clear = _zonreeks({"thermostaat": {14}})
        assert sun_hours({"thermostaat": series["thermostaat"]}, offsets, clear, NOW, AMS) == {"thermostaat": set()}


class TestSamenstellen:
    def test_zonder_referenties_bitgelijk(self):
        v = 19.617187500000004
        value, details = compose(v, {}, {})
        assert value is v and details["gebruikt"] == []

    def test_begrensd_op_de_mediaan(self):
        # Thermostaat in de zon: 21,7; midden 21,0 en beweging 19,9 met hun verschil.
        value, d = compose(21.7, {"midden": 21.0, "beweging": 19.9},
                           {"midden": -1.45, "beweging": -0.1})
        ref = (21.0 - 1.45 + 19.9 - 0.1) / 2
        assert value == pytest.approx(ref + CLIP_K)
        assert d["afgekapt_k"] == pytest.approx(21.7 - ref - CLIP_K)
        assert d["gebruikt"] == ["beweging", "midden"]

    def test_nooit_omhoog(self):
        value, d = compose(19.5, {"midden": 22.0}, {"midden": -1.45})
        assert value == 19.5 and d["afgekapt_k"] == 0.0

    def test_referentie_in_de_zon_telt_niet(self):
        value, _ = compose(21.7, {"bench": 22.5, "midden": 21.0}, {"bench": -0.6, "midden": -1.45},
                           excluded={"bench"})
        assert value == pytest.approx(21.0 - 1.45 + CLIP_K)

    @pytest.mark.parametrize("refs,offsets", [
        ({"midden": None}, {"midden": -1.45}),      # levert niets
        ({"midden": 21.0}, {}),                      # nog geen verschil geleerd
    ])
    def test_onbruikbaar_dan_thermostaat(self, refs, offsets):
        assert compose(21.7, refs, offsets)[0] == 21.7

    def test_zonder_thermostaat_niets(self):
        assert compose(None, {"midden": 21.0}, {"midden": -1.45})[0] is None


class TestAchteraf:
    """``history_corrections``: wat compose live had afgekapt, per uur."""

    def _reeks(self, days=21):
        from custom_components.quatt_stooklijn.analysis.room_reference import history_corrections
        hrs = _hours(days)
        th, mid, clear = {}, {}, {}
        for t in hrs:
            dag = int((t - hrs[0]) // 86400)
            zonnig = dag % 2 == 0
            clear[t] = 0.9 if zonnig else 0.1
            th[t] = 20.0 + (1.5 if zonnig and _hod(t) == 14 else 0.0)
            mid[t] = 21.45
        return history_corrections, hrs, th, {"midden": mid}, clear

    def test_zonpiek_afgekapt_tot_de_clipgrens(self):
        hc, hrs, th, refs, clear = self._reeks()
        out = hc(th, refs, clear, AMS, hrs[0], hrs[-1] + 1)
        assert out and all(_hod(t) == 14 for t in out)
        assert all(v == pytest.approx(-(1.5 - CLIP_K)) for v in out.values())

    def test_niet_voordat_het_verschil_geleerd_is(self):
        # De eerste dagen zijn er te weinig nachturen (12): geen correctie.
        hc, hrs, th, refs, clear = self._reeks()
        out = hc(th, refs, clear, AMS, hrs[0], hrs[-1] + 1)
        assert min(out) >= hrs[0] + 2 * 86400

    def test_weet_niets_van_later(self):
        # Een verschil dat pas later verandert, mag eerdere uren niet raken.
        hc, hrs, th, refs, clear = self._reeks()
        half = hrs[len(hrs) // 2]
        voor = hc(th, refs, clear, AMS, hrs[0], half)
        later = {t: (v - 5.0 if t >= half else v) for t, v in refs["midden"].items()}
        assert hc(th, {"midden": later}, clear, AMS, hrs[0], half) == voor

    def test_referentie_in_de_zon_telt_niet(self):
        # Drie referenties; de bench krijgt om 11 u zon. Telt hij mee, dan is de
        # mediaan de bewegingsmelder (20,4 → afkappen tot 20,7); telt hij niet,
        # dan het midden van midden en beweging (20,2 → 20,5).
        from custom_components.quatt_stooklijn.analysis.room_reference import history_corrections
        hrs = _hours(21)
        th, mid, bew, bench, clear = {}, {}, {}, {}, {}
        for t in hrs:
            zonnig = int((t - hrs[0]) // 86400) % 2 == 0
            elf = _hod(t) == 11
            clear[t] = 0.9 if zonnig else 0.1
            th[t] = 20.0 + (1.0 if zonnig and elf else 0.0)
            mid[t] = 21.45
            bew[t] = 20.1 + (0.4 if elf else 0.0)
            bench[t] = 20.6 + (2.0 if zonnig and elf else 0.0)
        out = history_corrections(th, {"midden": mid, "beweging": bew, "bench": bench},
                                  clear, AMS, hrs[0], hrs[-1] + 1)
        laatste_week = [v for t, v in out.items() if t >= hrs[-1] - 7 * 86400 and _hod(t) == 11]
        assert laatste_week and all(v == pytest.approx(-0.5) for v in laatste_week)

    def test_zonder_referenties_niets(self):
        hc, hrs, th, _refs, clear = self._reeks()
        assert hc(th, {}, clear, AMS, hrs[0], hrs[-1] + 1) == {}
