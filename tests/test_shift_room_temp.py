"""Kamertemperatuur bij verschuiven, volgens het 2R2C."""

from __future__ import annotations

import numpy as np

from custom_components.quatt_stooklijn.analysis.shift_room_temp import (
    forecast_room_temperature,
)
from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    TwoStateParams,
    discretize,
    simulate,
)

P = TwoStateParams(
    U=300.0, Ci=20000.0, Cm=120000.0, Hm=8000.0, g=4.0, P_int=500.0, alpha=0.9
)
STAP = 300
NU = 1_800_000_000.0
KWARTIER = 0.25


def _logboek(uren=72.0, q_w=3200.0, t_buiten=8.0):
    """Monsters van een echt doorgerekend huis, eindigend op ``NU``."""
    n = int(uren * 12)
    to = np.full(n, t_buiten)
    sol = np.zeros(n)
    q = np.full(n, q_w)
    ti = np.r_[20.0, simulate(P, 20.0, 20.0, to, sol, q, 1 / 12)[:-1]]
    return [[NU - STAP * (n - 1 - i), float(ti[i]), t_buiten, q_w, 0.0] for i in range(n)]


def _reken(rows, flat, shifted, at=NU):
    n = len(flat)
    return forecast_room_temperature(
        P, rows, at, [8.0] * n, [0.0] * n, flat, shifted, KWARTIER
    )


class TestBeginToestand:
    def test_eerste_punt_is_de_meting_op_het_startmoment(self):
        rows = _logboek()
        r = _reken(rows, [3000.0] * 96, [3000.0] * 96)
        assert r.reason is None
        assert r.flat[0] == round(rows[-1][1], 2)
        assert len(r.flat) == len(r.shifted) == 96

    def test_monsters_na_het_startmoment_tellen_niet_mee(self):
        rows = _logboek()
        later = [[NU + STAP * (i + 1), 25.0, 8.0, 0.0, 0.0] for i in range(6)]
        met = _reken(rows + later, [3000.0] * 96, [3000.0] * 96)
        zonder = _reken(rows, [3000.0] * 96, [3000.0] * 96)
        assert met.flat == zonder.flat

    def test_oud_logboek_geeft_geen_lijn(self):
        r = _reken(_logboek(), [3000.0] * 96, [3000.0] * 96, at=NU + 3600.0)
        assert r.reason and not r.flat

    def test_te_kort_logboek_geeft_geen_lijn(self):
        r = _reken(_logboek(uren=6.0), [3000.0] * 96, [3000.0] * 96)
        assert r.reason and not r.flat

    def test_massatemperatuur_komt_terug_uit_de_metingen(self):
        n = 72 * 12
        to, sol, q = np.full(n, 8.0), np.zeros(n), np.full(n, 3200.0)
        Ad, Bd = discretize(P, 1 / 12)
        x = np.array([20.0, 20.5])
        ti = []
        for k in range(n):
            ti.append(float(x[0]))
            tm_echt = float(x[1])
            x = Ad @ x + Bd @ np.array([to[k], sol[k], q[k], 1.0])
        rows = [[NU - STAP * (n - 1 - i), ti[i], 8.0, 3200.0, 0.0] for i in range(n)]
        r = _reken(rows, [3000.0] * 96, [3000.0] * 96)
        assert abs(r.t_mass0 - tm_echt) < 0.02


class TestDoorrekenen:
    def test_zonder_verschuiven_zijn_de_lijnen_gelijk(self):
        r = _reken(_logboek(), [3000.0] * 96, [3000.0] * 96)
        assert r.flat == r.shifted

    def test_warmte_eerder_maakt_de_kamer_eerder_warmer(self):
        vlak = [3000.0] * 96
        eerder = [5000.0] * 24 + [1000.0] * 24 + [3000.0] * 48
        r = _reken(_logboek(), vlak, eerder)
        assert r.shifted[24] > r.flat[24]

    def test_verschil_hangt_niet_af_van_de_begintoestand(self):
        vlak = [3000.0] * 96
        eerder = [5000.0] * 24 + [1000.0] * 24 + [3000.0] * 48
        koud = _reken(_logboek(q_w=2500.0), vlak, eerder)
        warm = _reken(_logboek(q_w=4000.0), vlak, eerder)
        d_koud = np.array(koud.shifted) - np.array(koud.flat)
        d_warm = np.array(warm.shifted) - np.array(warm.flat)
        assert np.allclose(d_koud, d_warm, atol=0.011)

    def test_vermogen_van_stap_k_werkt_pas_op_stap_k_plus_1(self):
        vlak = [3000.0] * 8
        anders = [3000.0, 9000.0] + [3000.0] * 6
        r = _reken(_logboek(), vlak, anders)
        assert r.shifted[1] == r.flat[1]
        assert r.shifted[2] > r.flat[2]
