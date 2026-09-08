"""Tests voor het tweetoestandsmodel."""

from __future__ import annotations

import math

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    BOUNDS,
    INITIAL,
    ORDER,
    TwoStateParams,
    _expm_2x2,
    _system_matrix,
    _to_bounded,
    _to_free,
    discretize,
    fit,
    one_step_residuals,
    rmse_at_horizon,
    simulate,
    track_hidden_state,
)

WAAR = TwoStateParams(U=260.0, Ci=1400.0, Cm=35000.0, Hm=2500.0,
                      g=3.2, P_int=450.0, alpha=0.65)


def _genereer(p: TwoStateParams, n: int = 1500, ruis: float = 0.01, seed: int = 1):
    """Synthetisch huis dat exact dit model gehoorzaamt."""
    rng = np.random.default_rng(seed)
    k = np.arange(n)
    t_out = 4.0 + 7.0 * np.sin(2 * np.pi * (k - 9) / 24) + rng.normal(0, 1.5, n)
    solar = np.maximum(0.0, 300 * np.sin(2 * np.pi * (k - 8) / 24))
    t_in = np.empty(n); t_mass = np.empty(n); q = np.empty(n)
    t_in[0] = t_mass[0] = 20.0
    Ad, Bd = discretize(p)
    for i in range(n - 1):
        q[i] = max(0.0, p.U * (20.5 - t_out[i]) - p.g * solar[i])
        x = Ad @ np.array([t_in[i], t_mass[i]]) + Bd @ np.array(
            [t_out[i], solar[i], q[i], 1.0])
        t_in[i + 1] = x[0] + rng.normal(0, ruis)
        t_mass[i + 1] = x[1]
    q[-1] = q[-2]
    return [(t_in, t_out, solar, q)]


class TestMatrixExponent:
    """Zonder scipy zelf geschreven, dus toetsen tegen onafhankelijke wiskunde."""

    def test_gelijk_aan_de_reeks(self):
        """Voor bescheiden eigenwaarden is de Taylorreeks een geldige referentie."""
        A = _system_matrix(TwoStateParams(**INITIAL))
        ref = sum(np.linalg.matrix_power(A, k) / math.factorial(k) for k in range(60))
        np.testing.assert_allclose(_expm_2x2(A), ref, atol=1e-12)

    def test_nulmatrix_geeft_identiteit(self):
        np.testing.assert_allclose(_expm_2x2(np.zeros((2, 2))), np.eye(2), atol=1e-15)

    def test_diagonaal(self):
        A = np.diag([-0.5, -2.0])
        np.testing.assert_allclose(
            _expm_2x2(A), np.diag([math.exp(-0.5), math.exp(-2.0)]), atol=1e-14)

    def test_ontaarde_eigenwaarden(self):
        """Kan bij geldige parameters niet optreden, maar mag niet klappen."""
        A = np.array([[-1.0, 0.0], [0.0, -1.0]])
        np.testing.assert_allclose(_expm_2x2(A), np.diag([math.exp(-1)] * 2), atol=1e-14)

    def test_eigenwaarden_zijn_altijd_reeel(self):
        """De discriminant is (a-d)^2 + 4bc met b, c > 0."""
        rng = np.random.default_rng(0)
        for _ in range(300):
            p = TwoStateParams(**{k: float(rng.uniform(*BOUNDS[k])) for k in ORDER})
            eig = np.linalg.eigvals(_system_matrix(p))
            assert np.all(np.abs(np.imag(eig)) < 1e-12)
            assert np.all(np.real(eig) < 0)


class TestDiscretisatie:
    def test_stationaire_toestand_klopt_analytisch(self):
        """Bij constante invoer moet het model op de energiebalans uitkomen:
        U * (T_binnen - T_buiten) = Q + P_int."""
        p = TwoStateParams(**INITIAL)
        n = 6000
        traj = simulate(p, 20.0, 20.0, np.zeros(n), np.zeros(n), np.full(n, 2500.0))
        assert traj[-1] == pytest.approx((2500.0 + p.P_int) / p.U, abs=1e-3)

    def test_zonder_warmtepomp_zakt_het_naar_de_interne_warmtelast(self):
        """Niet naar de buitentemperatuur: de interne warmtelast blijft staan
        en houdt het huis P_int / U boven buiten."""
        p = TwoStateParams(**INITIAL)
        n = 8000
        traj = simulate(p, 20.0, 20.0, np.zeros(n), np.zeros(n), np.zeros(n))
        assert traj[-1] == pytest.approx(p.P_int / p.U, abs=1e-2)
        assert np.all(np.diff(traj) < 1e-9)  # monotoon dalend

    def test_zonder_enige_warmtebron_zakt_het_naar_buiten(self):
        p = TwoStateParams(**{**INITIAL, "P_int": 0.0})
        n = 8000
        traj = simulate(p, 20.0, 20.0, np.zeros(n), np.zeros(n), np.zeros(n))
        assert traj[-1] == pytest.approx(0.0, abs=1e-2)

    def test_tijdconstantes_zijn_geordend(self):
        fast, slow = TwoStateParams(**INITIAL).time_constants
        assert 0 < fast < slow


class TestGrenzen:
    def test_transformatie_is_omkeerbaar(self):
        v = np.array([INITIAL[k] for k in ORDER])
        np.testing.assert_allclose(_to_bounded(_to_free(v)), v, rtol=1e-6)

    def test_blijft_altijd_binnen_de_grenzen(self):
        """De hele reden voor de transformatie: geen enkele stap van de
        optimalisator mag buiten het fysisch plausibele terechtkomen."""
        rng = np.random.default_rng(3)
        for _ in range(500):
            v = _to_bounded(rng.normal(0, 50, len(ORDER)))
            for i, k in enumerate(ORDER):
                lo, hi = BOUNDS[k]
                assert lo <= v[i] <= hi

    def test_extreme_vrije_waarden_lopen_niet_over(self):
        v = _to_bounded(np.full(len(ORDER), -1e6))
        assert np.all(np.isfinite(v))


class TestFit:
    def test_vindt_bekende_parameters_terug(self):
        p, rep = fit(_genereer(WAAR))
        assert rep["converged"]
        for k in ORDER:
            assert getattr(p, k) == pytest.approx(getattr(WAAR, k), rel=0.15)

    def test_vindt_de_tijdconstantes_terug(self):
        p, _ = fit(_genereer(WAAR))
        for got, want in zip(p.time_constants, WAAR.time_constants):
            assert got == pytest.approx(want, rel=0.15)

    def test_verslag_is_bruikbaar(self):
        """Een niet-geconvergeerde fit hoort herkenbaar te zijn."""
        p, rep = fit(_genereer(WAAR))
        assert rep["n_residuals"] > 0
        assert rep["rms_k"] < 0.05
        assert set(rep) == {"iterations", "converged", "rms_k", "n_residuals"}

    def test_te_korte_segmenten_geven_een_nette_fout(self):
        with pytest.raises(ValueError):
            fit([(np.zeros(5), np.zeros(5), np.zeros(5), np.zeros(5))])


class TestVerborgenToestand:
    def test_reconstructie_volgt_de_echte_massatemperatuur(self):
        """T_massa wordt niet gemeten; het wordt afgeleid uit de gemeten
        binnentemperatuur. Na de inlooptijd hoort dat te kloppen."""
        seg = _genereer(WAAR, n=600, ruis=0.0)
        t_in, t_out, solar, q = seg[0]
        tm = track_hidden_state(WAAR, t_in, t_out, solar, q)
        # opnieuw opbouwen wat de generator als massa had
        Ad, Bd = discretize(WAAR)
        echt = np.empty(len(t_in)); echt[0] = 20.0
        ti = np.empty(len(t_in)); ti[0] = 20.0
        for i in range(len(t_in) - 1):
            x = Ad @ np.array([ti[i], echt[i]]) + Bd @ np.array(
                [t_out[i], solar[i], q[i], 1.0])
            ti[i + 1], echt[i + 1] = x[0], x[1]
        assert np.abs(tm[100:] - echt[100:]).max() < 0.05


class TestHorizonfout:
    def test_perfecte_parameters_geven_bijna_nul(self):
        seg = _genereer(WAAR, n=500, ruis=0.0)
        assert rmse_at_horizon(WAAR, seg, 12) < 0.02

    def test_te_kort_segment_geeft_none(self):
        seg = [(np.zeros(10), np.zeros(10), np.zeros(10), np.zeros(10))]
        assert rmse_at_horizon(WAAR, seg, 12) is None

    def test_slechte_parameters_zijn_meetbaar_slechter(self):
        seg = _genereer(WAAR, n=800, ruis=0.0)
        slecht = TwoStateParams(U=500.0, Ci=400.0, Cm=100000.0, Hm=300.0,
                                g=0.0, P_int=0.0, alpha=0.0)
        assert rmse_at_horizon(slecht, seg, 12) > 10 * rmse_at_horizon(WAAR, seg, 12)


class TestSerialisatie:
    def test_roundtrip(self):
        terug = TwoStateParams.from_dict(WAAR.to_dict())
        assert terug == WAAR

    def test_dict_bevat_de_tijdconstantes(self):
        d = WAAR.to_dict()
        assert "tau_fast_h" in d and "tau_slow_h" in d
        assert d["tau_fast_h"] < d["tau_slow_h"]

    def test_array_roundtrip(self):
        assert TwoStateParams.from_array(WAAR.as_array()) == WAAR
