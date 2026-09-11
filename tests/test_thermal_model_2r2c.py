"""Tests voor het tweetoestandsmodel."""

from __future__ import annotations

import math

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import (
    BOUNDS,
    BURN_IN_HOURS,
    MIN_BLOCK_HOURS,
    MIN_TRAIN_HOURS,
    SPLIT_BLOCK_HOURS,
    INITIAL,
    ORDER,
    TwoStateParams,
    _expm_2x2,
    _system_matrix,
    _to_bounded,
    _to_free,
    discretize,
    fit,
    fit_and_validate,
    one_step_residuals,
    steps_in,
    persistence_rmse,
    rmse_at_horizon,
    split_alternating,
    simulate,
    track_hidden_state,
)

WAAR = TwoStateParams(U=260.0, Ci=1400.0, Cm=35000.0, Hm=2500.0,
                      g=3.2, P_int=450.0, alpha=0.65)


def _genereer(p: TwoStateParams, n: int = 1500, ruis: float = 0.01, seed: int = 1,
              dt_hours: float = 1.0, pendelen: bool = False):
    """Synthetisch huis dat exact dit model gehoorzaamt.

    De reeksen worden stap voor stap opgebouwd zodat ``t_in[i]`` en de invoer
    op index ``i`` bij hetzelfde tijdstip horen. Dat is niet dezelfde
    uitlijning als de uitvoer van ``simulate``, die een stap opschuift.
    """
    rng = np.random.default_rng(seed)
    k = np.arange(n)
    uur = k * dt_hours
    t_out = 4.0 + 7.0 * np.sin(2 * np.pi * (uur - 9) / 24) + rng.normal(0, 1.5, n)
    solar = np.maximum(0.0, 300 * np.sin(2 * np.pi * (uur - 8) / 24))
    t_in = np.empty(n); t_mass = np.empty(n); q = np.empty(n)
    t_in[0] = t_mass[0] = 20.0
    Ad, Bd = discretize(p, dt_hours)
    for i in range(n - 1):
        vraag = max(0.0, p.U * (20.5 - t_out[i]) - p.g * solar[i])
        # Aan/uit-pendelen met een periode van ongeveer een uur. Zonder die
        # snelle prikkel valt de snelle tijdconstante niet te leren: een
        # gladde dagcyclus wekt hem simpelweg niet op.
        q[i] = vraag * (1.0 + 0.8 * np.sign(np.sin(2 * np.pi * i * dt_hours)))\
            if pendelen else vraag
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
        assert set(rep) == {"iterations", "converged", "stop_reason",
                            "rms_k", "n_residuals"}
        assert rep["stop_reason"] in {"tolerantie bereikt", "geen verbetering meer"}

    def test_iteratielimiet_telt_niet_als_convergentie(self):
        """Anders laat de validatiepoort een half afgemaakte fit door."""
        _, rep = fit(_genereer(WAAR), max_iter=2)
        assert rep["stop_reason"] == "iteratielimiet"
        assert not rep["converged"]

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


class TestStapgrootte:
    """De discretisatie is exact, dus de parameters horen niet van de
    bemonsteringsstap af te hangen. Dat is precies wat een Euler-benadering
    wél zou doen — en de reden dat die hier niet gebruikt wordt."""

    def test_zelfde_parameters_bij_vijf_minuten_en_een_uur(self):
        p = TwoStateParams(**INITIAL)
        Ad_h, _ = discretize(p, 1.0)
        Ad_5, _ = discretize(p, 1.0 / 12.0)
        np.testing.assert_allclose(
            np.linalg.matrix_power(Ad_5, 12), Ad_h, atol=1e-12)

    def test_simulatie_komt_op_hetzelfde_uit(self):
        p = TwoStateParams(**INITIAL)
        n = 200
        to = np.full(n * 12, 3.0); sol = np.zeros(n * 12); q = np.full(n * 12, 2000.0)
        fijn = simulate(p, 20.0, 20.0, to, sol, q, 1.0 / 12.0)
        grof = simulate(p, 20.0, 20.0, to[:n], sol[:n], q[:n], 1.0)
        assert fijn[11::12][:n] == pytest.approx(grof, abs=1e-9)

    def test_fit_op_vijfminutendata_vindt_dezelfde_fysica(self):
        """De discretisatie is exact, dus dezelfde fysica moet er uit komen
        ongeacht hoe fijn je bemonstert."""
        p = TwoStateParams(**INITIAL)
        seg = _genereer(p, n=8000, ruis=0.002, seed=5,
                        dt_hours=1.0 / 12.0, pendelen=True)
        gevonden, rep = fit(seg, burn_in=288, dt_hours=1.0 / 12.0)
        assert rep["converged"]
        for got, want in zip(gevonden.time_constants, p.time_constants):
            assert got == pytest.approx(want, rel=0.15)

    def test_ware_parameters_zijn_het_optimum(self):
        """Vangnet tegen scheef gegenereerde testdata: als een gevonden set
        beter scoort dan de set die de data maakte, is de uitlijning stuk."""
        p = TwoStateParams(**INITIAL)
        seg = _genereer(p, n=4000, ruis=0.0, seed=8,
                        dt_hours=1.0 / 12.0, pendelen=True)
        gevonden, _ = fit(seg, burn_in=288, dt_hours=1.0 / 12.0)
        waar_rms = np.sqrt((one_step_residuals(p, seg, 288, 1 / 12) ** 2).mean())
        fit_rms = np.sqrt((one_step_residuals(gevonden, seg, 288, 1 / 12) ** 2).mean())
        assert waar_rms <= fit_rms * 1.05


class TestValidatie:
    def _seg(self, n=1200, ruis=0.01):
        return _genereer(WAAR, n=n, ruis=ruis)

    def test_splitsing_verdeelt_om_en_om(self):
        seg = [(np.arange(1000.0),) * 4]
        train, test = split_alternating(seg, block=168, min_len=72)
        assert len(train) == 3 and len(test) == 3
        assert train[0][0][0] == 0.0
        assert test[0][0][0] == 168.0

    def test_te_korte_stukken_vallen_af(self):
        seg = [(np.arange(200.0),) * 4]
        train, test = split_alternating(seg, block=168, min_len=72)
        assert len(train) == 1 and len(test) == 0  # rest van 32 is te kort

    def test_telling_loopt_door_over_segmenten(self):
        """Anders belandt data met veel onderbrekingen volledig in train en
        blijft er niets over om op te toetsen."""
        segs = [(np.arange(100.0),) * 4 for _ in range(6)]
        train, test = split_alternating(segs, block=168, min_len=72)
        assert len(train) == 3 and len(test) == 3

    def test_accepteert_een_model_dat_echt_beter_is(self):
        v = fit_and_validate(self._seg(), horizon=12)
        assert v.accepted, v.reason
        assert v.rmse < v.persistence_rmse
        assert v.params is not None

    def test_weigert_bij_te_weinig_data(self):
        v = fit_and_validate(self._seg(n=200), horizon=12)
        assert not v.accepted
        assert "te weinig" in v.reason

    def test_weigert_als_het_huidige_model_al_beter_is(self):
        v = fit_and_validate(self._seg(), horizon=12, reference_rmse=1e-6)
        assert not v.accepted
        assert "huidige model" in v.reason

    def test_marge_voorkomt_wisselen_op_ruis(self):
        """Net zo goed is niet goed genoeg."""
        basis = fit_and_validate(self._seg(), horizon=12)
        gelijk = fit_and_validate(self._seg(), horizon=12,
                                  reference_rmse=basis.rmse * 1.01)
        assert not gelijk.accepted

    def test_poort_weigert_een_fit_die_de_limiet_raakte(self):
        v = fit_and_validate(self._seg(), horizon=12, max_iter=2)
        assert not v.accepted
        assert "convergeerde niet" in v.reason

    def test_uitkomst_is_serialiseerbaar(self):
        d = fit_and_validate(self._seg(), horizon=12).to_dict()
        assert d["accepted"] is True
        assert d["params"]["tau_fast_h"] > 0
        assert d["persistence_rmse_k"] > d["rmse_k"]

    def test_persistentie_op_leeg_segment(self):
        assert persistence_rmse([], horizon=12) is None


class TestDrempelsVolgenDeStapgrootte:
    """De poort moet even streng zijn op vijfminutendata als op uurdata.

    Elke drempel stond hier als een aantal stappen met uren in zijn naam. Dat
    klopte zolang alles op uurdata draaide, maar ``dt_hours`` bestaat juist
    omdat het meetlogboek op vijf minuten bemonstert — en daar betekende
    dezelfde 24 ineens twee uur inlooptijd, werd de week-om-week-splitsing een
    splitsing per veertien uur, en zakte de eis van twee weken trainingsdata
    naar 28 uur. Precies de poort die een slecht model moet tegenhouden werd zo
    twaalf keer zwakker, zonder dat er iets aan te zien was.
    """

    def test_omrekenen(self):
        assert steps_in(BURN_IN_HOURS, 1.0) == 24
        assert steps_in(BURN_IN_HOURS, 1.0 / 12.0) == 288
        assert steps_in(SPLIT_BLOCK_HOURS, 1.0 / 12.0) == 2016

    def test_nooit_nul_stappen(self):
        """Een venster korter dan één stap is nog steeds één stap."""
        assert steps_in(0.01, 1.0) == 1

    def test_blokken_blijven_een_week(self):
        """Op vijfminutendata hoort een blok 168 uur te beslaan, niet 168 stappen."""
        seg = [tuple(np.zeros(6000) for _ in range(4))]
        train, test = split_alternating(seg, dt_hours=1.0 / 12.0)
        assert len(train[0][0]) == steps_in(SPLIT_BLOCK_HOURS, 1.0 / 12.0)
        # 6000 stappen is bijna drie blokken: twee hele plus een rest die met
        # 1968 stappen (164 uur) net boven de ondergrens van 72 uur uitkomt.
        assert len(train) == 2 and len(test) == 1

    def test_korte_stukken_vallen_af(self):
        """Onder MIN_BLOCK_HOURS telt een stuk niet mee, ook op fijne data."""
        kort = steps_in(MIN_BLOCK_HOURS, 1.0 / 12.0) - 1
        seg = [tuple(np.zeros(kort) for _ in range(4))]
        train, test = split_alternating(seg, dt_hours=1.0 / 12.0)
        assert train == [] and test == []

    def test_trainingseis_schaalt_mee(self):
        """Ruim drie dagen vijfminutendata is geen twee weken trainingsdata.

        Met de oude drempel in stappen haalde dit de eis moeiteloos: 1000
        monsters is meer dan 336. In uren is het 83, en dat is het niet.
        """
        seg = _genereer(WAAR, n=1000, dt_hours=1.0 / 12.0, pendelen=True)
        v = fit_and_validate(seg, horizon=steps_in(12, 1.0 / 12.0),
                             dt_hours=1.0 / 12.0)
        assert not v.accepted
        assert v.reason == "te weinig trainingsdata"
        assert steps_in(MIN_TRAIN_HOURS, 1.0 / 12.0) == 4032
