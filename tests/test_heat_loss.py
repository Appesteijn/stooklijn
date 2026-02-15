"""Unit tests for heat loss analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.heat_loss import (
    HeatLossResult,
    calculate_heat_loss,
)


class TestCalculateHeatLoss:
    """Tests for calculate_heat_loss()."""

    def test_known_linear_data(self, daily_heating_df):
        """With perfect linear data, regression should recover exact parameters."""
        result = calculate_heat_loss(daily_heating_df)

        assert result.slope == pytest.approx(-200, abs=0.1)
        assert result.intercept == pytest.approx(4000, abs=1)
        assert result.r2 == pytest.approx(1.0, abs=1e-6)
        assert result.heat_loss_coefficient == pytest.approx(200, abs=0.1)
        assert result.balance_point == pytest.approx(20.0, abs=0.1)

    def test_noisy_data_reasonable_fit(self, daily_heating_df_noisy):
        """With noise, regression should still approximate the true relationship."""
        result = calculate_heat_loss(daily_heating_df_noisy)

        assert result.slope is not None
        assert result.slope == pytest.approx(-200, abs=30)
        assert result.intercept == pytest.approx(4000, abs=300)
        assert result.r2 > 0.9
        assert result.heat_loss_coefficient > 0

    def test_heat_at_temps(self, daily_heating_df):
        """Heat demand at specific temperatures should follow the regression line."""
        result = calculate_heat_loss(daily_heating_df)

        assert result.heat_at_temps is not None
        # At -10°C: heat = -200*(-10) + 4000 = 6000
        assert result.heat_at_temps[-10] == pytest.approx(6000, abs=10)
        # At 0°C: heat = 4000
        assert result.heat_at_temps[0] == pytest.approx(4000, abs=10)
        # At 15°C: heat = -200*15 + 4000 = 1000
        assert result.heat_at_temps[15] == pytest.approx(1000, abs=10)

    def test_heat_at_temps_clipped_to_zero(self):
        """Heat demand should never be negative."""
        # Create data where regression predicts negative at high temps
        temps = np.array([0, 2, 4, 6, 8, 10])
        heat = np.array([2000, 1600, 1200, 800, 400, 250])
        df = pd.DataFrame({"avg_temperatureOutside": temps, "totalHeatPerHour": heat})
        df.index = pd.date_range("2024-01-01", periods=len(df), freq="D")

        result = calculate_heat_loss(df)
        # At 15°C the regression would predict negative → should be clipped to 0
        assert result.heat_at_temps[15] >= 0

    def test_empty_dataframe(self):
        """Empty DataFrame should return empty result."""
        result = calculate_heat_loss(pd.DataFrame())

        assert result.slope is None
        assert result.intercept is None
        assert result.r2 is None
        assert result.heat_loss_coefficient is None

    def test_none_input(self):
        """None input should return empty result."""
        result = calculate_heat_loss(None)

        assert result.slope is None

    def test_too_few_rows(self):
        """Fewer than 5 rows should return empty result."""
        df = pd.DataFrame({
            "avg_temperatureOutside": [0, 5, 10],
            "totalHeatPerHour": [4000, 3000, 2000],
        })
        df.index = pd.date_range("2024-01-01", periods=3, freq="D")

        result = calculate_heat_loss(df)
        assert result.slope is None

    def test_missing_columns(self):
        """Missing required columns should return empty result."""
        df = pd.DataFrame({"some_column": [1, 2, 3, 4, 5, 6]})
        df.index = pd.date_range("2024-01-01", periods=6, freq="D")

        result = calculate_heat_loss(df)
        assert result.slope is None

    def test_all_below_min_heating(self):
        """All values below MIN_HEATING_WATTS (200W) → no regression."""
        df = pd.DataFrame({
            "avg_temperatureOutside": [15, 18, 20, 22, 25, 28],
            "totalHeatPerHour": [50, 30, 10, 5, 0, 0],
        })
        df.index = pd.date_range("2024-07-01", periods=6, freq="D")

        result = calculate_heat_loss(df)
        assert result.slope is None

    def test_inf_values_handled(self):
        """Inf values in data should be treated as NaN and dropped."""
        temps = np.linspace(-5, 15, 20)
        heat = -200 * temps + 4000
        heat[3] = np.inf
        heat[7] = -np.inf
        df = pd.DataFrame({"avg_temperatureOutside": temps, "totalHeatPerHour": heat})
        df.index = pd.date_range("2024-01-01", periods=len(df), freq="D")

        result = calculate_heat_loss(df)
        # Should still compute, just skipping inf rows
        assert result.slope is not None
        assert result.slope == pytest.approx(-200, abs=5)

    def test_scatter_data_format(self, daily_heating_df):
        """Scatter data should have the right structure."""
        result = calculate_heat_loss(daily_heating_df)

        assert result.scatter_data is not None
        assert len(result.scatter_data) > 0
        first = result.scatter_data[0]
        assert "temp" in first
        assert "heat" in first
        assert isinstance(first["temp"], float)
        assert isinstance(first["heat"], float)

    def test_scatter_includes_all_valid_points(self, daily_heating_df):
        """Scatter data should include all valid points (not just those >= 200W)."""
        result = calculate_heat_loss(daily_heating_df)

        # scatter_data comes from plot_data (all valid, non-inf, non-NaN rows)
        assert len(result.scatter_data) == len(daily_heating_df)

    def test_source_name_does_not_affect_result(self, daily_heating_df):
        """Source name is only a label; result should be identical."""
        r1 = calculate_heat_loss(daily_heating_df, source_name="heat_pump")
        r2 = calculate_heat_loss(daily_heating_df, source_name="gas")

        assert r1.slope == r2.slope
        assert r1.heat_loss_coefficient == r2.heat_loss_coefficient
