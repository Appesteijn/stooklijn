"""Shared analysis utilities — regression, R², heat demand."""

from __future__ import annotations

import numpy as np

from ..const import OUTLIER_STD_THRESHOLD


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
