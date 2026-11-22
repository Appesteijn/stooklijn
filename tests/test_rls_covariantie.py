"""De covariantie van het 1R1C blijft geldig, en NaN komt het model niet in.

Analyse van 21-11-2026: de oude blokupdate van ``RLSEstimator`` werkte bij
bevriezen alleen ``P[actief, actief]`` bij en liet de kruistermen staan. Dan is
P geen geldige covariantie meer, kan de noemer ``λ + x'Px`` negatief worden en
krijgt de gain het verkeerde teken zodra de bevroren parameter weer meedoet.
Met het P_c-anker is de regressor van θ₃ ``dt·(Q + P_c)`` en dus nooit bijna
nul, terwijl het bevriezen van θ₃ daarop leunde.

Nagespeeld op het echte logboek (08-09 t/m 21-11-2026), jong model vanaf 15-10:
oude update 373 uur een indefiniete P en 1% online (eind-g 58,7); nieuwe
update 0 uur en 89% online. Op het herfsthuis hieronder: oud 1 van de 20
modellen goed, nieuw 20 van de 20.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    ANCHOR_MAX_Q_HP_W,
    OnlineRCModel,
    RLSEstimator,
)
from custom_components.quatt_stooklijn.helpers import get_float_state
from custom_components.quatt_stooklijn.highres_log import HighResLog

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _min_eig(P: np.ndarray) -> float:
    return float(np.linalg.eigvalsh((P + P.T) / 2)[0])


def _herfst(model: OnlineRCModel, seed: int, n_hours: int = 600,
            U: float = 285.0, C: float = 20000.0, G: float = 3.5,
            P_C: float = 1000.0) -> float:
    """Herfsthuis met veel uren onder de ankerdrempel. Geeft de kleinste
    eigenwaarde van P (alleen de geleerde parameters) over de hele run."""
    rng = np.random.default_rng(seed)
    t_in = 20.0
    worst = np.inf
    for h in range(n_hours):
        t_out = 9.0 + 5.0 * np.sin(2 * np.pi * (h - 9) / 24) + rng.normal(0, 1.0)
        q_sol = max(0.0, 300 * np.sin(2 * np.pi * (h - 8) / 24))
        q = U * (20.3 - t_out) - G * q_sol - P_C + 2000 * (20.0 - t_in) + rng.normal(0, 150)
        q = q if q >= ANCHOR_MAX_Q_HP_W else 0.0
        model.update(t_in, t_out, q, q_sol, T0 + timedelta(hours=h))
        t_in += (q + G * q_sol + P_C - U * (t_in - t_out)) / C + rng.normal(0, 0.01)
        worst = min(worst, _min_eig(model._rls.P[:3, :3]))
    return worst


class TestCovariantieBlijftGeldig:
    def test_bevriezen_houdt_p_positief_definiet(self):
        """Afwisselend bevroren en vrij, met een grote regressor op de bevroren
        parameter: precies het geval waarin de oude blokupdate P brak."""
        rng = np.random.default_rng(3)
        theta_true = np.array([0.03, 0.0003, 0.0001])
        rls = RLSEstimator()
        for k in range(2000):
            x = np.array([rng.normal(-12, 4), max(0.0, rng.normal(100, 150)),
                          rng.normal(1500, 300)])
            y = float(x @ theta_true) + rng.normal(0, 0.01)
            rls.update(x, y, frozen=(2,) if k % 3 else ())
            assert _min_eig(rls.P) > 0, f"P indefiniet na update {k}"
            np.testing.assert_allclose(rls.P, rls.P.T, rtol=1e-12, atol=0)

    def test_bevroren_parameter_beweegt_niet(self):
        rls = RLSEstimator()
        rls.theta = np.array([0.0, 0.0, 0.0002])
        rls.update(np.array([1.0, 2.0, 3000.0]), 0.5, frozen=(2,))
        assert rls.theta[2] == 0.0002

    def test_bevroren_blok_vergeet_niet(self):
        """Het oorspronkelijke doel van bevriezen: P[2,2] mag in een zomer
        zonder warmtevraag niet door de vergeetfactor opgeblazen worden."""
        rls = RLSEstimator()
        rls.P = np.diag([1e-6, 1e-8, 1e-9])
        for _ in range(3000):
            rls.update(np.array([-5.0, 200.0, 0.0]), 0.0, frozen=(2,))
        assert rls.P[2, 2] <= 1e-9 * (1 + 1e-9)

    @pytest.mark.parametrize("seed", range(5))
    def test_jong_model_met_beide_ankers_leert_het_herfsthuis(self, seed):
        """Zoals productie start: standaardwaarden, U-anker en P_c-anker.
        Met de oude update eindigde hier 1 van de 20 seeds goed."""
        m = OnlineRCModel()
        m.set_u_prior(285.0)
        m.set_p_c_anchor(1000.0)
        worst = _herfst(m, seed)
        assert worst > 0
        p = m.raw_params
        assert m.is_converged
        assert abs(p["C"] - 20000.0) / 20000.0 < 0.25
        assert abs(p["U"] - 285.0) / 285.0 < 0.15

    def test_rijp_model_blijft_een_seizoen_stabiel(self):
        """θ en P zoals de draaiende installatie op 26-10-2026, 180 dagen
        herfstweer. Met de oude update was P na mediaan 1.844 uur indefiniet
        en eindigde C tussen −6.769 en 76.895 (echt 7.463)."""
        th = [0.0385, 0.00042, 0.000134]
        C = 1 / th[2]
        m = OnlineRCModel.from_dict({"rls": {
            "theta": th,
            "P": [[7.3e-05, 9.6e-07, 3.2e-07], [9.6e-07, 7.9e-08, 3.0e-09],
                  [3.2e-07, 3.0e-09, 2.1e-09]],
            "n_updates": 4213, "n_updates_per_param": [4213, 4213, 3408],
            "forgetting": 0.998,
        }, "u_prior": th[0] * C})
        m.set_p_c_anchor(1000.0)
        worst = _herfst(m, seed=0, n_hours=4320, U=th[0] * C, C=C, G=th[1] * C)
        assert worst > 0
        assert m.is_converged
        assert abs(m.raw_params["C"] - C) / C < 0.1


class TestHerstelBijLaden:
    def test_indefiniete_p_wordt_hersteld_theta_blijft(self):
        theta = [0.03, 0.0003, 0.0001]
        P = [[1e-4, 0.0, 2e-6], [0.0, 1e-7, 0.0], [2e-6, 0.0, 1e-9]]  # indefiniet
        assert _min_eig(np.array(P)) < 0
        rls = RLSEstimator.from_dict({"theta": theta, "P": P, "n_updates": 100})
        assert _min_eig(rls.P) > 0
        np.testing.assert_array_equal(rls.theta, theta)

    def test_gezonde_p_blijft_staan(self):
        """De echte P van 21-11-2026: positief-definiet, maar met een kleinste
        eigenwaarde van 3,9e-11 dicht op de rand. Die mag niet aangeraakt worden."""
        P = np.array([
            [6.361101569040729e-05, 1.798719482671004e-06, 3.51357138647957e-07],
            [1.7987194827664835e-06, 1.5407361760234423e-07, 6.7469959979377655e-09],
            [3.5135828986173943e-07, 6.746995996838378e-09, 2.078168392013158e-09],
        ])
        rls = RLSEstimator.from_dict({"theta": [0.0266, 0.000287, 9.4e-05], "P": P.tolist()})
        np.testing.assert_allclose(rls.P, (P + P.T) / 2, rtol=0, atol=1e-18)

    def test_rijpe_p_met_groot_anker_blok_blijft_staan(self, caplog):
        """Vals alarm (22-11-2026): met het anker blijft P[3,3] op 1000, en een
        drempel relatief aan de grootste eigenwaarde las de rijpe 3×3 (kleinste
        eigenwaarde 6,75e-10) als kapot. Elke herstart een melding, en P[2,2]
        ~15% verschoven."""
        P = np.zeros((4, 4))
        P[:3, :3] = [[7.3e-05, 9.6e-07, 3.2e-07], [9.6e-07, 7.9e-08, 3.0e-09],
                     [3.2e-07, 3.0e-09, 2.1e-09]]
        P[3, 3] = 1000.0
        rls = RLSEstimator(n_params=4)
        rls.P = P.copy()
        with caplog.at_level("WARNING"):
            assert rls.repair_covariance() is False
        np.testing.assert_array_equal(rls.P, P)
        assert "covariantie" not in caplog.text

    def test_negatieve_variantie_leert_opnieuw(self):
        rls = RLSEstimator.from_dict({
            "theta": [0.03, 0.0003, 0.0001],
            "P": [[1e-4, 1e-7, 0.0], [1e-7, -1e-8, 0.0], [0.0, 0.0, 1e-9]],
        })
        assert _min_eig(rls.P) > 0
        assert rls.P[1, 1] == 1000.0 and rls.P[0, 1] == 0.0

    def test_p_met_nan_begint_opnieuw(self):
        rls = RLSEstimator.from_dict({
            "theta": [0.03, 0.0003, 0.0001],
            "P": [[float("nan"), 0, 0], [0, 1, 0], [0, 0, 1]],
        })
        assert np.all(np.isfinite(rls.P))
        assert _min_eig(rls.P) > 0


class TestNaNKomtErNietIn:
    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
    def test_get_float_state_weigert_niet_eindige_getallen(self, raw):
        hass = SimpleNamespace(states=SimpleNamespace(
            get=lambda _e: SimpleNamespace(state=raw)))
        assert get_float_state(hass, "sensor.x") is None

    def test_get_float_state_geeft_gewone_getallen_door(self):
        hass = SimpleNamespace(states=SimpleNamespace(
            get=lambda _e: SimpleNamespace(state="19.95")))
        assert get_float_state(hass, "sensor.x") == 19.95

    @pytest.mark.parametrize("veld", range(5))
    def test_model_slaat_nan_monster_over(self, veld):
        m = OnlineRCModel()
        m.set_u_prior(285.0)
        m.set_p_c_anchor(1000.0)
        m.update(20.0, 5.0, 3000.0, 0.0, T0)
        m.update(20.1, 5.0, 3000.0, 0.0, T0 + timedelta(hours=1))
        theta = m._rls.theta.copy()
        prev = m._prev_t_indoor
        args = [20.2, 5.0, 3000.0, 0.0, 3000.0]
        args[veld] = float("nan")
        used = m.update(*args[:4], T0 + timedelta(hours=2), q_hp_interval_w=args[4])
        assert used is False
        np.testing.assert_array_equal(m._rls.theta, theta)
        assert m._prev_t_indoor == prev
        # en daarna leert hij gewoon door
        assert m.update(20.2, 5.0, 3000.0, 0.0, T0 + timedelta(hours=3))
        assert np.all(np.isfinite(m._rls.theta))

    def test_model_met_nan_theta_telt_als_kapot(self):
        """Opgeslagen vóór de fix: zo'n model moet opnieuw beginnen
        (``_async_reset_broken_model``), niet voorgoed op de terugval staan."""
        m = OnlineRCModel()
        m._rls.theta[0] = float("nan")
        assert m.is_broken

    def test_logboek_weigert_nan_warmte(self):
        log = HighResLog()
        t = datetime(2026, 11, 21, tzinfo=timezone.utc)
        assert log.record(t, 20.0, 5.0, float("nan"), 0.0) is False
        assert log.record(t, float("inf"), 5.0, 2000.0, 0.0) is False
        assert len(log) == 0
        assert log.record(t, 20.0, 5.0, 2000.0, 0.0, pv_w=float("nan")) is True
