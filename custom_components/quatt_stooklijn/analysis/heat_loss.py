"""Heat loss analysis (from notebook Cell 9)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class HeatLossResult:
    """Results from heat loss analysis."""

    heat_loss_coefficient: float | None = None  # W/K
    balance_point: float | None = None  # °C
    slope: float | None = None
    intercept: float | None = None
    r2: float | None = None

    # Scatter data for dashboard
    scatter_data: list[dict] | None = None

    # Heat demand at specific temperatures (with COP if available)
    heat_at_temps: dict[int, dict] | None = None


_MIN_HEATING_WATTS = 200  # Minimum W to count as a heating day


def calculate_heat_loss(
    df_daily: pd.DataFrame,
    source_name: str = "heat_pump",
) -> HeatLossResult:
    """Calculate heat loss characteristics from daily data.

    Args:
        df_daily: Daily DataFrame with 'avg_temperatureOutside' and 'totalHeatPerHour'
        source_name: Label for the data source
    """
    result = HeatLossResult()

    if df_daily is None or df_daily.empty:
        return result

    cols_needed = ["avg_temperatureOutside", "totalHeatPerHour"]
    if not all(c in df_daily.columns for c in cols_needed):
        return result

    plot_data = df_daily[cols_needed].replace([np.inf, -np.inf], np.nan).dropna()

    if len(plot_data) < 5:
        return result

    # Filter: only days with meaningful heating demand for regression
    heating_data = plot_data[plot_data["totalHeatPerHour"] >= _MIN_HEATING_WATTS]

    if len(heating_data) < 5:
        return result

    # First-pass regression to identify outliers (e.g. test runs with unusually
    # high thermostat settings that create anomalous heat demand points)
    x_all = heating_data["avg_temperatureOutside"].values
    y_all = heating_data["totalHeatPerHour"].values

    slope_rough, intercept_rough = np.polyfit(x_all, y_all, 1)
    residuals = y_all - (slope_rough * x_all + intercept_rough)
    std = np.std(residuals)
    if std > 0:
        inlier_mask = np.abs(residuals) < 2.5 * std
    else:
        inlier_mask = np.ones(len(x_all), dtype=bool)

    regression_data = heating_data[inlier_mask]
    x = regression_data["avg_temperatureOutside"].values
    y = regression_data["totalHeatPerHour"].values

    if len(x) < 5:
        return result

    # Final regression on filtered data
    slope, intercept = np.polyfit(x, y, 1)

    # R-squared
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    result.slope = float(slope)
    result.intercept = float(intercept)
    result.r2 = float(r2)
    result.heat_loss_coefficient = float(-slope)

    if slope != 0:
        result.balance_point = float(-intercept / slope)

    # Heat demand at specific temperatures (with COP if available)
    result.heat_at_temps = {}

    # Check if COP data is available for interpolation
    has_cop = "averageCOP" in df_daily.columns
    cop_data = None
    if has_cop:
        cop_df = df_daily[["avg_temperatureOutside", "averageCOP"]].replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        if len(cop_df) >= 2:
            cop_data = cop_df.sort_values("avg_temperatureOutside")

    for temp in [-10, -5, 0, 5, 10, 15]:
        demand = slope * temp + intercept
        heat_value = max(0.0, float(demand))

        # Interpolate COP if data is available
        cop_value = None
        if cop_data is not None and len(cop_data) >= 2:
            cop_value = float(np.interp(
                temp,
                cop_data["avg_temperatureOutside"].values,
                cop_data["averageCOP"].values
            ))

        result.heat_at_temps[temp] = {
            "heat": heat_value,
            "cop": cop_value
        }

    # Scatter data for dashboard (outliers excluded)
    result.scatter_data = [
        {
            "temp": round(float(row["avg_temperatureOutside"]), 1),
            "heat": round(float(row["totalHeatPerHour"]), 0),
        }
        for _, row in regression_data.iterrows()
    ]

    return result
