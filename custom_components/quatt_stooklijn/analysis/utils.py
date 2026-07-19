"""Shared analysis utilities — regression, R², heat demand, mode classification."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..const import MIN_HEATING_WATTS, OUTLIER_STD_THRESHOLD

# Operating-mode labels, derived from net heat delivery (W).
MODE_HEATING = "heating"
MODE_COOLING = "cooling"
MODE_IDLE = "idle"


def robust_linear_fit(
    x: np.ndarray,
    y: np.ndarray,
    threshold: float = OUTLIER_STD_THRESHOLD,
    min_inliers: int = 5,
) -> tuple[float, float, np.ndarray]:
    """Two-pass linear regression with outlier removal.

    First pass: ordinary least-squares fit.
    Second pass: remove points with |residual| > threshold × σ, refit.

    Returns:
        (slope, intercept, inlier_mask) — mask is boolean array over original x/y.
    """
    slope_rough, intercept_rough = np.polyfit(x, y, 1)
    residuals = y - (slope_rough * x + intercept_rough)
    std = np.std(residuals)

    if std > 0:
        inlier_mask = np.abs(residuals) < threshold * std
        if inlier_mask.sum() < min_inliers:
            inlier_mask = np.ones(len(x), dtype=bool)
    else:
        inlier_mask = np.ones(len(x), dtype=bool)

    slope, intercept = np.polyfit(x[inlier_mask], y[inlier_mask], 1)
    return float(slope), float(intercept), inlier_mask


def calc_r2(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    """Calculate R² (coefficient of determination)."""
    ss_res = np.sum((y_actual - y_predicted) ** 2)
    ss_tot = np.sum((y_actual - np.mean(y_actual)) ** 2)
    return float(1 - (ss_res / ss_tot)) if ss_tot > 0 else 0.0


def calc_heat_demand(slope: float, intercept: float, t_outdoor: float) -> float:
    """Calculate heat demand (W) from heat loss model, clamped to ≥ 0."""
    return max(0.0, slope * t_outdoor + intercept)


def classify_heat_mode(heat_per_hour: pd.Series) -> pd.Series:
    """Classify the operating mode of each record from its net heat delivery (W).

    - ``heating``: net delivery ≥ ``MIN_HEATING_WATTS`` (genuine heating).
    - ``cooling``: net heat extraction (negative delivery). The heat pump is
      pulling heat out of the house — reserved for the future cooling analysis.
    - ``idle``: everything in between (summer standstill, DHW-only, near-zero).

    Returns a pandas Series of mode labels aligned to ``heat_per_hour``.
    """
    heat = pd.to_numeric(heat_per_hour, errors="coerce")
    mode = pd.Series(MODE_IDLE, index=heat_per_hour.index, dtype="object")
    mode[heat >= MIN_HEATING_WATTS] = MODE_HEATING
    mode[heat < 0] = MODE_COOLING
    return mode


def select_heating(df: pd.DataFrame, heat_col: str = "totalHeatPerHour") -> pd.DataFrame:
    """Return only genuine heating records, excluding cooling and idle days.

    Single source of truth for the heating-only filter shared by the stooklijn
    and heat-loss regressions, so cooling/summer data can never pollute a
    heating fit. When cooling analysis is added it gets its own ``select_cooling``
    counterpart rather than lowering this threshold.
    """
    if heat_col not in df.columns:
        return df.iloc[0:0]
    return df[classify_heat_mode(df[heat_col]) == MODE_HEATING]
