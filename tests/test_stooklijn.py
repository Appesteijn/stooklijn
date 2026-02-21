"""Unit tests for stooklijn (heating curve) analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.stooklijn import (
    StooklijnResult,
    _find_knee_by_grid_search,
    calculate_stooklijn,
)


class TestFindKneeByGridSearch:
    """Tests for the grid-search knee detection helper."""

    def _make_knee_data(self, knee_temp: float, n: int = 30):
        """Synthetic data with a clear knee at knee_temp.

        Left of knee: flat (slope ~0), right of knee: decreasing (slope -400 W/°C).
        """
        temps = np.linspace(-4, 8, n)
        power = np.where(
            temps < knee_temp,
            6000.0,  # flat on the cold side (at max capacity)
            6000.0 - 400.0 * (temps - knee_temp),  # decreasing on the warm side
        )
        return temps, power

    def test_finds_correct_knee(self):
        """Grid search should find the knee at the correct temperature."""
        x, y = self._make_knee_data(knee_temp=1.0)
        knee_t, knee_p = _find_knee_by_grid_search(x, y)

        assert knee_t is not None
        assert knee_t == pytest.approx(1.0, abs=0.5)  # within one step
        assert knee_p is not None
        assert knee_p == pytest.approx(6000.0, rel=0.05)

    def test_returns_none_when_right_slope_positive(self):
        """If the warm side has a positive slope, no valid knee exists."""
        # Power increases with temperature everywhere — no physical knee
        x = np.linspace(-4, 8, 30)
        y = 3000.0 + 200.0 * x  # always increasing
        knee_t, knee_p = _find_knee_by_grid_search(x, y)
        assert knee_t is None
        assert knee_p is None

    def test_returns_none_with_too_few_points(self):
        """Fewer points than min_points_per_segment on one side → None."""
        x = np.array([-1.0, 0.0, 1.0])
        y = np.array([6000.0, 5800.0, 5200.0])
        knee_t, _ = _find_knee_by_grid_search(x, y, min_points_per_segment=5)
        assert knee_t is None

    def test_knee_at_negative_temp(self):
        """Should correctly detect a knee below 0°C."""
        x, y = self._make_knee_data(knee_temp=-2.0)
        knee_t, _ = _find_knee_by_grid_search(x, y)
        assert knee_t is not None
        assert knee_t == pytest.approx(-2.0, abs=0.5)


class TestCalculateStooklijn:
    """Tests for calculate_stooklijn()."""

    def test_all_none_inputs(self):
        """All None inputs should return empty result."""
        result = calculate_stooklijn(None, None, None)

        assert result.knee_temperature is None
        assert result.slope_api is None
        assert result.slope_local is None
        assert result.slope_optimal is None

    def test_empty_dataframes(self):
        """Empty DataFrames should return empty result."""
        result = calculate_stooklijn(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )

        assert result.knee_temperature is None
        assert result.slope_local is None
        assert result.slope_optimal is None

    def test_knee_detection(self, live_history_df):
        """Knee should be detected near 2°C with synthetic data."""
        result = calculate_stooklijn(live_history_df, None, None)

        assert result.knee_temperature is not None
        # Knee should be roughly around 2°C (within bounds [-3, 4])
        assert -3 <= result.knee_temperature <= 4
        assert result.knee_power is not None
        assert result.knee_power > 0

    def test_api_stooklijn_slope(self, live_history_df):
        """API stooklijn (right of knee) should have negative slope."""
        result = calculate_stooklijn(live_history_df, None, None)

        if result.slope_api is not None:
            # Slope should be negative (power decreases as temp increases)
            assert result.slope_api < 0
            assert result.intercept_api is not None

    def test_envelope_analysis(self, live_history_df, hourly_quatt_df):
        """Max-envelope analysis should produce local slope for freezing temps."""
        result = calculate_stooklijn(live_history_df, hourly_quatt_df, None)

        # Local slope (freezing performance) depends on data below knee temp
        if result.slope_local is not None:
            assert result.intercept_local is not None
            assert result.r2_local is not None
            assert 0 <= result.r2_local <= 1

    def test_optimal_stooklijn(self, daily_quatt_df):
        """Optimal stooklijn from daily data should have negative slope."""
        result = calculate_stooklijn(None, None, daily_quatt_df)

        assert result.slope_optimal is not None
        assert result.slope_optimal < 0  # power decreases as temp increases
        assert result.intercept_optimal is not None
        assert result.r2_optimal is not None
        assert result.r2_optimal > 0.8  # should be a good fit with our synthetic data
        assert result.balance_temp_optimal is not None

    def test_optimal_scatter_data(self, daily_quatt_df):
        """Scatter data should be populated from daily data."""
        result = calculate_stooklijn(None, None, daily_quatt_df)

        assert result.scatter_data is not None
        assert len(result.scatter_data) > 0
        first = result.scatter_data[0]
        assert "temp" in first
        assert "heat" in first
        assert "cop" in first

    def test_cop_scatter_data(self, daily_quatt_df):
        """COP scatter data should be populated when averageCOP column exists."""
        result = calculate_stooklijn(None, None, daily_quatt_df)

        assert result.cop_scatter_data is not None
        assert len(result.cop_scatter_data) > 0
        first = result.cop_scatter_data[0]
        assert "temp" in first
        assert "cop" in first

    def test_full_pipeline(self, live_history_df, hourly_quatt_df, daily_quatt_df):
        """Full pipeline with all three data sources."""
        result = calculate_stooklijn(live_history_df, hourly_quatt_df, daily_quatt_df)

        # Knee detection should work
        assert result.knee_temperature is not None
        # Optimal stooklijn should work
        assert result.slope_optimal is not None

    def test_insufficient_live_data(self):
        """Fewer than 10 points above MIN_POWER_FILTER → no knee detection."""
        df = pd.DataFrame({
            "temp": [5, 6, 7],
            "power": [3000, 2800, 2600],
        })
        df.index = pd.date_range("2024-01-01", periods=3, freq="h")

        result = calculate_stooklijn(df, None, None)
        assert result.knee_temperature is None

    def test_hourly_missing_columns(self):
        """Hourly data without required columns → no envelope analysis."""
        df = pd.DataFrame({"some_col": [1, 2, 3, 4, 5, 6]})
        df.index = pd.date_range("2024-01-01", periods=6, freq="h")

        result = calculate_stooklijn(None, df, None)
        assert result.slope_local is None

    def test_daily_missing_columns(self):
        """Daily data without required columns → no optimal stooklijn."""
        df = pd.DataFrame({"some_col": [1, 2, 3, 4, 5, 6, 7]})
        df.index = pd.date_range("2024-01-01", periods=7, freq="D")

        result = calculate_stooklijn(None, None, df)
        assert result.slope_optimal is None

    def test_too_few_daily_points(self):
        """Fewer than 5 valid daily points → no optimal stooklijn."""
        df = pd.DataFrame({
            "avg_temperatureOutside": [5, 10, 15],
            "totalHeatPerHour": [3000, 2000, 1000],
        })
        df.index = pd.date_range("2024-01-01", periods=3, freq="D")

        result = calculate_stooklijn(None, None, df)
        assert result.slope_optimal is None
