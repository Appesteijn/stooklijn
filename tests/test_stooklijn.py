"""Unit tests for stooklijn (heating curve) analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis.stooklijn import (
    StooklijnResult,
    _piecewise_linear,
    calculate_stooklijn,
)


class TestPiecewiseLinear:
    """Tests for the piecewise linear helper."""

    def test_below_knee(self):
        """Points below x0 should follow k1 slope."""
        # x0=2, y0=6000, k1=-100, k2=-400
        result = _piecewise_linear(np.array([0.0]), 2.0, 6000.0, -100.0, -400.0)
        # y = -100*(0-2) + 6000 = 200 + 6000 = 6200
        assert result[0] == pytest.approx(6200)

    def test_above_knee(self):
        """Points above x0 should follow k2 slope."""
        result = _piecewise_linear(np.array([5.0]), 2.0, 6000.0, -100.0, -400.0)
        # y = -400*(5-2) + 6000 = -1200 + 6000 = 4800
        assert result[0] == pytest.approx(4800)

    def test_at_knee(self):
        """At the knee point, both slopes give y0."""
        result = _piecewise_linear(np.array([2.0]), 2.0, 6000.0, -100.0, -400.0)
        assert result[0] == pytest.approx(6000)

    def test_vectorized(self):
        """Should work on arrays."""
        x = np.array([-1, 0, 2, 5, 10])
        result = _piecewise_linear(x, 2.0, 6000.0, -100.0, -400.0)
        assert len(result) == 5


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
