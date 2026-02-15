"""Unit tests for gas consumption analysis logic.

Since async_fetch_gas_data requires HA (recorder), we test the
computational steps by simulating the intermediate DataFrames.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


class TestGasMeterProcessing:
    """Test the gas meter processing logic extracted from gas.py."""

    @staticmethod
    def _process_gas_meter(
        records: list[dict],
        calorific_value: float = 9.77,
        boiler_efficiency: float = 0.90,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Reproduce the core gas processing logic without HA dependencies.

        This mirrors the logic in async_fetch_gas_data after records are fetched.
        """
        df_gas = pd.DataFrame(records)
        df_gas = df_gas.set_index("timestamp").sort_index()

        # Consumption differences (cumulative meter)
        df_gas["gas_m3"] = df_gas["state"].diff()
        df_gas = df_gas[df_gas["gas_m3"] >= 0]
        df_gas = df_gas[df_gas["gas_m3"] < 10]  # Remove spikes

        # Convert to heat
        df_gas["heat_kwh"] = df_gas["gas_m3"] * calorific_value * boiler_efficiency
        df_gas["heat_w"] = df_gas["heat_kwh"] * 1000

        # Resample
        df_hourly = df_gas.resample("h").agg(
            {"gas_m3": "sum", "heat_kwh": "sum", "heat_w": "mean"}
        )
        df_daily = df_gas.resample("d").agg({"gas_m3": "sum", "heat_kwh": "sum"})
        df_daily["totalHeatPerHour"] = (df_daily["heat_kwh"] * 1000) / 24

        return df_hourly, df_daily

    def test_cumulative_meter_diffs(self):
        """Consumption should be calculated as diff of cumulative readings."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 100.0},
            {"timestamp": pd.Timestamp("2024-01-01 01:00"), "state": 100.5},
            {"timestamp": pd.Timestamp("2024-01-01 02:00"), "state": 101.2},
            {"timestamp": pd.Timestamp("2024-01-01 03:00"), "state": 101.5},
        ]
        df_hourly, _ = self._process_gas_meter(records)

        # First row is NaN (no previous value to diff), so 3 valid readings
        total_gas = df_hourly["gas_m3"].sum()
        assert total_gas == pytest.approx(1.5, abs=0.01)  # 101.5 - 100.0

    def test_spike_filtering(self):
        """Spikes >= 10 m³ should be filtered out."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 100.0},
            {"timestamp": pd.Timestamp("2024-01-01 01:00"), "state": 100.5},
            {"timestamp": pd.Timestamp("2024-01-01 02:00"), "state": 115.0},  # +14.5 spike
            {"timestamp": pd.Timestamp("2024-01-01 03:00"), "state": 115.5},
        ]
        df_hourly, _ = self._process_gas_meter(records)

        # Only 0.5 + 0.5 should count; the 14.5 spike is filtered
        total_gas = df_hourly["gas_m3"].sum()
        assert total_gas == pytest.approx(1.0, abs=0.01)

    def test_negative_diffs_filtered(self):
        """Negative diffs (meter reset or error) should be filtered out."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 100.0},
            {"timestamp": pd.Timestamp("2024-01-01 01:00"), "state": 100.5},
            {"timestamp": pd.Timestamp("2024-01-01 02:00"), "state": 99.0},  # reset
            {"timestamp": pd.Timestamp("2024-01-01 03:00"), "state": 99.8},
        ]
        df_hourly, _ = self._process_gas_meter(records)

        total_gas = df_hourly["gas_m3"].sum()
        # Only 0.5 + 0.8 = 1.3 (the -1.5 reset is filtered)
        assert total_gas == pytest.approx(1.3, abs=0.01)

    def test_heat_conversion(self):
        """Gas m³ should be correctly converted to heat kWh and W."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 0.0},
            {"timestamp": pd.Timestamp("2024-01-01 01:00"), "state": 1.0},
        ]
        calorific = 10.0
        efficiency = 0.9

        df_hourly, _ = self._process_gas_meter(
            records, calorific_value=calorific, boiler_efficiency=efficiency
        )

        # 1 m³ * 10 kWh/m³ * 0.9 = 9 kWh
        assert df_hourly["heat_kwh"].sum() == pytest.approx(9.0, abs=0.01)

    def test_daily_aggregation(self):
        """Daily totals should aggregate readings by day."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 0.0},
            {"timestamp": pd.Timestamp("2024-01-01 06:00"), "state": 2.0},
            {"timestamp": pd.Timestamp("2024-01-01 12:00"), "state": 4.0},
            {"timestamp": pd.Timestamp("2024-01-01 18:00"), "state": 6.0},
            {"timestamp": pd.Timestamp("2024-01-02 00:00"), "state": 8.0},
            {"timestamp": pd.Timestamp("2024-01-02 12:00"), "state": 9.0},
        ]
        _, df_daily = self._process_gas_meter(records)

        # The first record (00:00) has NaN diff and is dropped.
        # Remaining diffs: 2, 2, 2, 2, 1 = 9 total across both days.
        total = df_daily["gas_m3"].sum()
        assert total == pytest.approx(9.0, abs=0.01)

    def test_total_heat_per_hour(self):
        """totalHeatPerHour should be heat_kwh * 1000 / 24."""
        records = [
            {"timestamp": pd.Timestamp("2024-01-01 00:00"), "state": 0.0},
            {"timestamp": pd.Timestamp("2024-01-01 12:00"), "state": 1.0},
        ]
        _, df_daily = self._process_gas_meter(
            records, calorific_value=10.0, boiler_efficiency=1.0
        )

        # 1 m³ * 10 kWh * 1.0 eff = 10 kWh → 10000 Wh / 24 h ≈ 416.7 W
        assert df_daily.loc["2024-01-01", "totalHeatPerHour"] == pytest.approx(
            416.67, abs=1
        )

    def test_empty_records(self):
        """Empty records should raise or produce empty result.

        The actual function returns pd.DataFrame() early for empty states.
        Our helper will raise because set_index('timestamp') fails on empty DF.
        This test verifies the edge case is handled.
        """
        with pytest.raises((KeyError, ValueError)):
            self._process_gas_meter([])


class TestHotWaterCorrection:
    """Test hot water correction logic."""

    @staticmethod
    def _apply_hot_water_correction(
        df_daily: pd.DataFrame,
        hot_water_temp_threshold: float = 18.0,
        calorific_value: float = 9.77,
        boiler_efficiency: float = 0.90,
    ) -> pd.DataFrame:
        """Reproduce the hot water correction from gas.py."""
        if "avg_temperatureOutside" not in df_daily.columns:
            df_daily["totalHeatPerHour"] = (df_daily["heat_kwh"] * 1000) / 24
            return df_daily

        warm_days = df_daily[
            df_daily["avg_temperatureOutside"] >= hot_water_temp_threshold
        ]

        if len(warm_days) >= 3:
            hot_water_gas_m3 = warm_days["gas_m3"].median()
            df_daily["gas_m3_heating"] = (
                df_daily["gas_m3"] - hot_water_gas_m3
            ).clip(lower=0)
            df_daily["heat_kwh_heating"] = (
                df_daily["gas_m3_heating"] * calorific_value * boiler_efficiency
            )
            df_daily["totalHeatPerHour"] = (
                df_daily["heat_kwh_heating"] * 1000
            ) / 24
        else:
            df_daily["totalHeatPerHour"] = (df_daily["heat_kwh"] * 1000) / 24

        return df_daily

    def test_correction_applied_with_warm_days(self):
        """When >= 3 warm days exist, hot water baseline should be subtracted."""
        df = pd.DataFrame({
            "gas_m3": [5, 4.5, 4, 3, 1.5, 1.2, 1.0, 1.3],
            "heat_kwh": [5 * 9.77 * 0.9] * 3 + [3 * 9.77 * 0.9] + [1.5 * 9.77 * 0.9] * 4,
            "avg_temperatureOutside": [0, 2, 5, 10, 20, 22, 25, 21],
        })
        df.index = pd.date_range("2024-01-01", periods=8, freq="D")

        result = self._apply_hot_water_correction(df)

        # 4 warm days (>= 18°C): gas = [1.5, 1.2, 1.0, 1.3]
        # Median = (1.2 + 1.3) / 2 = 1.25 m³/day baseline
        # Cold days should have gas_m3_heating = gas_m3 - 1.25
        assert "gas_m3_heating" in result.columns
        assert result.loc["2024-01-01", "gas_m3_heating"] == pytest.approx(
            5 - 1.25, abs=0.01
        )
        # Warm days with gas < baseline should be clipped to 0
        assert result.loc["2024-01-07", "gas_m3_heating"] >= 0

    def test_no_correction_with_few_warm_days(self):
        """When < 3 warm days, no hot water correction should be applied."""
        df = pd.DataFrame({
            "gas_m3": [5, 4, 3, 2, 1.5],
            "heat_kwh": [5 * 9.77 * 0.9] * 5,
            "avg_temperatureOutside": [0, 5, 10, 15, 19],
        })
        df.index = pd.date_range("2024-01-01", periods=5, freq="D")

        result = self._apply_hot_water_correction(df)

        # Only 1 warm day (19°C >= 18°C) — not enough for correction
        assert "gas_m3_heating" not in result.columns
        # totalHeatPerHour should be raw conversion
        assert result["totalHeatPerHour"].iloc[0] == pytest.approx(
            5 * 9.77 * 0.9 * 1000 / 24, abs=1
        )

    def test_no_temp_data(self):
        """Without temperature data, use raw gas for totalHeatPerHour."""
        df = pd.DataFrame({
            "gas_m3": [3, 4, 5],
            "heat_kwh": [3 * 9.77 * 0.9, 4 * 9.77 * 0.9, 5 * 9.77 * 0.9],
        })
        df.index = pd.date_range("2024-01-01", periods=3, freq="D")

        result = self._apply_hot_water_correction(df)

        assert "totalHeatPerHour" in result.columns
        expected = 3 * 9.77 * 0.9 * 1000 / 24
        assert result["totalHeatPerHour"].iloc[0] == pytest.approx(expected, abs=1)
