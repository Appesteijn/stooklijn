"""Tests voor het online 1R1C thermisch model en RLS parameter learning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    DEFAULT_C,
    DEFAULT_G_SOLAR,
    DEFAULT_U,
    RLS_MIN_UPDATES,
    OnlineRCModel,
    RLSEstimator,
    simulate_6h,
)


# --------------------------------------------------------------------------- #
#  RLSEstimator tests                                                         #
# --------------------------------------------------------------------------- #


class TestRLSEstimator:
    """Tests voor de Recursive Least Squares estimator."""

    def test_converges_to_known_params(self):
        """RLS convergeert naar bekende θ met synthetische data."""
        rng = np.random.default_rng(42)
        theta_true = np.array([0.04, 0.00006, 0.0002])  # U/C, g/C, 1/C

        rls = RLSEstimator()
        for _ in range(200):
            x = rng.standard_normal(3) * np.array([10.0, 500.0, 3000.0])
            y = float(x @ theta_true) + rng.normal(0, 0.01)
            rls.update(x, y)

        np.testing.assert_allclose(rls.theta, theta_true, rtol=0.1)

    def test_convergence_flag(self):
        """is_converged wordt True na voldoende updates."""
        rls = RLSEstimator()
        assert not rls.is_converged

        for i in range(RLS_MIN_UPDATES):
            rls.update(np.array([1.0, 0.0, 0.0]), 0.5)

        assert rls.is_converged

    def test_serialisation_roundtrip(self):
        """to_dict/from_dict behoudt alle state."""
        rls = RLSEstimator()
        rls.update(np.array([1.0, 2.0, 3.0]), 0.5)
        rls.update(np.array([0.5, 1.0, 1.5]), 0.3)

        data = rls.to_dict()
        restored = RLSEstimator.from_dict(data)

        np.testing.assert_array_almost_equal(restored.theta, rls.theta)
        np.testing.assert_array_almost_equal(restored.P, rls.P)
        assert restored.n_updates == rls.n_updates

    def test_initialise_from_physics(self):
        """Initial θ is correctly set from U, C, g."""
        rls = RLSEstimator()
        rls.initialise_from_physics(U=200.0, C=5000.0, g=0.30)

        assert abs(rls.theta[0] - 200.0 / 5000.0) < 1e-10
        assert abs(rls.theta[1] - 0.30 / 5000.0) < 1e-10
        assert abs(rls.theta[2] - 1.0 / 5000.0) < 1e-10


# --------------------------------------------------------------------------- #
#  OnlineRCModel tests                                                        #
# --------------------------------------------------------------------------- #


class TestOnlineRCModel:
    """Tests voor het 1R1C thermisch model."""

    @staticmethod
    def _make_trained_model(
        U: float = 200.0,
        C: float = 5000.0,
        g: float = 0.30,
        n_hours: int = 300,
    ) -> OnlineRCModel:
        """Genereer synthetische data met thermostat-gestuurd HP cycling."""
        rng = np.random.default_rng(123)
        model = OnlineRCModel()
        t_indoor = 20.0
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        hp_on = True
        setpoint = 20.0

        for h in range(n_hours):
            t_outdoor = 5.0 + 5.0 * np.sin(2 * np.pi * h / 24)
            q_solar = max(0, 1200 * np.sin(2 * np.pi * (h - 6) / 24))

            # Thermostat cycling: on below setpoint-0.3, off above setpoint+0.3
            if t_indoor < setpoint - 0.3:
                hp_on = True
            elif t_indoor > setpoint + 0.3:
                hp_on = False

            # HP produces ~3500W when on (with variation), 0 when off
            q_hp = (3500 + rng.normal(0, 200)) if hp_on else 0.0
            q_hp = max(0.0, q_hp)

            # True physics
            dt_true = (q_hp + g * q_solar - U * (t_indoor - t_outdoor)) / C
            t_indoor_next = t_indoor + dt_true + rng.normal(0, 0.005)

            model.update(t_indoor, t_outdoor, q_hp, q_solar, t0 + timedelta(hours=h))
            t_indoor = t_indoor_next

        return model

    def test_learns_correct_U(self):
        """Model leert warmteverliescoëfficiënt U binnen 15%."""
        model = self._make_trained_model(U=200.0, C=5000.0)
        params = model.raw_params
        assert params is not None
        assert abs(params["U"] - 200.0) / 200.0 < 0.15

    def test_learns_correct_C(self):
        """Model leert thermische massa C binnen 20%."""
        model = self._make_trained_model(U=200.0, C=5000.0)
        params = model.raw_params
        assert params is not None
        assert abs(params["C"] - 5000.0) / 5000.0 < 0.20

    def test_learns_correct_g(self):
        """Model leert zonnewinst-factor g binnen 25%."""
        model = self._make_trained_model(U=200.0, C=5000.0, g=0.30)
        params = model.raw_params
        assert params is not None
        assert abs(params["g"] - 0.30) / 0.30 < 0.25

    def test_is_converged_after_training(self):
        """Model is geconvergeerd na voldoende samples."""
        model = self._make_trained_model(n_hours=100)
        assert model.is_converged

    def test_not_converged_initially(self):
        """Nieuw model is niet geconvergeerd."""
        model = OnlineRCModel()
        assert not model.is_converged

    def test_predict_t_indoor(self):
        """Voorspelling is consistent met geleerde parameters."""
        model = self._make_trained_model()
        t_pred = model.predict_t_indoor(
            t_indoor=20.0, t_outdoor=5.0, q_hp_w=3000.0, q_solar_w=0.0,
        )
        # Met HP aan (3000W), huis moet (iets) warmer worden
        assert t_pred >= 20.0

    def test_predict_cooling_without_hp(self):
        """Zonder HP en zon koelt het huis af."""
        model = self._make_trained_model()
        t_pred = model.predict_t_indoor(
            t_indoor=20.0, t_outdoor=0.0, q_hp_w=0.0, q_solar_w=0.0,
        )
        assert t_pred < 20.0

    def test_calc_required_power(self):
        """calc_required_power geeft redelijke waarde."""
        model = self._make_trained_model(U=200.0)
        q = model.calc_required_power(
            t_indoor=20.0, t_outdoor=0.0, q_solar_w=0.0, t_setpoint=20.0,
        )
        # Bij 20°C verschil en U≈200: ≈ 4000W nodig om temp te houden
        assert 2000 < q < 6000

    def test_required_power_zero_above_setpoint(self):
        """Geen verwarming nodig als T_indoor al boven setpoint."""
        model = self._make_trained_model()
        q = model.calc_required_power(
            t_indoor=21.0, t_outdoor=20.0, q_solar_w=500.0, t_setpoint=20.0,
        )
        assert q == 0.0

    def test_solar_reduces_required_power(self):
        """Zonnewinst verlaagt de benodigde HP power."""
        model = self._make_trained_model()
        q_no_sun = model.calc_required_power(
            t_indoor=20.0, t_outdoor=5.0, q_solar_w=0.0, t_setpoint=20.0,
        )
        q_with_sun = model.calc_required_power(
            t_indoor=20.0, t_outdoor=5.0, q_solar_w=2000.0, t_setpoint=20.0,
        )
        assert q_with_sun < q_no_sun

    def test_initialise_from_batch(self):
        """initialise_from_batch zet U als startwaarde."""
        model = OnlineRCModel()
        model.initialise_from_batch(U=180.0)
        params = model.raw_params
        assert params is not None
        assert abs(params["U"] - 180.0) < 0.1

    def test_skips_non_hourly_samples(self):
        """Samples met dt < 0.5h of > 2h worden overgeslagen."""
        model = OnlineRCModel()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # First sample
        model.update(20.0, 5.0, 3000.0, 0.0, t0)
        # Second sample 10 minutes later — should be skipped
        assert not model.update(20.1, 5.0, 3000.0, 0.0, t0 + timedelta(minutes=10))
        assert model._rls.n_updates == 0

    def test_skips_small_temperature_difference(self):
        """Samples met T_in ≈ T_out worden overgeslagen (niet identificeerbaar)."""
        model = OnlineRCModel()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

        model.update(20.0, 19.5, 0.0, 0.0, t0)
        # T_indoor ≈ T_outdoor → skip
        assert not model.update(20.0, 19.5, 0.0, 0.0, t0 + timedelta(hours=1))
        assert model._rls.n_updates == 0

    def test_serialisation_roundtrip(self):
        """to_dict/from_dict behoudt alle state."""
        model = self._make_trained_model(n_hours=60)
        data = model.to_dict()
        restored = OnlineRCModel.from_dict(data)

        np.testing.assert_array_almost_equal(
            restored._rls.theta, model._rls.theta
        )
        assert restored._rls.n_updates == model._rls.n_updates
        assert restored._prev_t_indoor == model._prev_t_indoor

    def test_params_dict_format(self):
        """params dict bevat verwachte sleutels."""
        model = self._make_trained_model()
        p = model.params
        assert "U_wk" in p
        assert "C_whk" in p
        assert "g_solar" in p
        assert "tau_hours" in p
        assert "n_updates" in p
        assert "converged" in p
        assert p["converged"]


# --------------------------------------------------------------------------- #
#  simulate_6h tests                                                          #
# --------------------------------------------------------------------------- #


class TestSimulate6h:
    """Tests voor de 6-uurs forward simulatie."""

    @staticmethod
    def _trained_model() -> OnlineRCModel:
        return TestOnlineRCModel._make_trained_model(U=200.0, C=5000.0, g=0.30)

    def test_returns_6_hours(self):
        """Simulatie geeft 6 resultaten voor 6 forecast uren."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * 6,
            forecast_q_solar=[0.0] * 6,
        )
        assert len(results) == 6

    def test_cold_weather_needs_heating(self):
        """Bij kou is HP nodig (hp_needed=True)."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[0.0] * 6,
            forecast_q_solar=[0.0] * 6,
        )
        assert results[0]["hp_needed"]
        assert results[0]["supply_temp"] is not None
        assert results[0]["q_hp_needed_w"] > 0

    def test_warm_weather_no_heating(self):
        """Bij warm weer is geen verwarming nodig."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=22.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[22.0] * 6,
            forecast_q_solar=[1000.0] * 6,
            t_setpoint=20.0,
        )
        assert not results[0]["hp_needed"]

    def test_supply_temp_within_bounds(self):
        """Supply temp blijft binnen MPC grenzen."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[-10.0] * 6,
            forecast_q_solar=[0.0] * 6,
        )
        for r in results:
            if r["supply_temp"] is not None:
                assert 20.0 <= r["supply_temp"] <= 55.0

    def test_solar_reduces_heating_demand(self):
        """Zon verlaagt de warmtevraag in de simulatie."""
        model = self._trained_model()
        results_no_sun = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * 6,
            forecast_q_solar=[0.0] * 6,
        )
        results_sun = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * 6,
            forecast_q_solar=[2000.0] * 6,
        )
        assert results_sun[0]["q_hp_needed_w"] < results_no_sun[0]["q_hp_needed_w"]

    def test_indoor_temp_predicted(self):
        """t_indoor_predicted is ingevuld voor elk uur."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * 6,
            forecast_q_solar=[0.0] * 6,
        )
        for r in results:
            assert r["t_indoor_predicted"] is not None

    def test_handles_short_forecast(self):
        """Werkt ook met minder dan 6 forecast uren."""
        model = self._trained_model()
        results = simulate_6h(
            model,
            t_indoor_now=20.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0, 4.0],
            forecast_q_solar=[0.0, 0.0],
        )
        assert len(results) == 2
