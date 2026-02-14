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

    # Heat demand at specific temperatures
    heat_at_temps: dict[int, float] | None = None


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

    x = heating_data["avg_temperatureOutside"].values
    y = heating_data["totalHeatPerHour"].values

    # Linear regression
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

    # Heat demand at specific temperatures
    result.heat_at_temps = {}
    for temp in [-10, -5, 0, 5, 10, 15]:
        demand = slope * temp + intercept
        result.heat_at_temps[temp] = max(0.0, float(demand))

    # Scatter data for dashboard (all points for context, regression uses filtered)
    result.scatter_data = [
        {
            "temp": round(float(row["avg_temperatureOutside"]), 1),
            "heat": round(float(row["totalHeatPerHour"]), 0),
        }
        for _, row in plot_data.iterrows()
    ]

    return result
