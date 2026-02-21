"""Stooklijn / heating curve analysis (from notebook Cell 4).

Performs knee detection (piecewise linear fit) and max-envelope filtering
to determine heat pump performance at freezing temperatures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Analysis constants
MIN_POWER_FILTER = 2500  # W
DEFROST_THRESHOLD = 0  # W
BIN_SIZE = 0.5  # °C
KEEP_THRESHOLD = 0.90  # 90% of max
DAYS_HISTORY = 30


@dataclass
class StooklijnResult:
    """Results from stooklijn analysis."""

    # Knee detection
    knee_temperature: float | None = None
    knee_power: float | None = None

    # API-based stooklijn (right of knee)
    slope_api: float | None = None
    intercept_api: float | None = None

    # Local stooklijn (left of knee, freezing performance)
    slope_local: float | None = None
    intercept_local: float | None = None
    r2_local: float | None = None

    # Optimal stooklijn (from daily usage data)
    slope_optimal: float | None = None
    intercept_optimal: float | None = None
    r2_optimal: float | None = None
    balance_temp_optimal: float | None = None

    # Scatter data for dashboard (daily points)
    scatter_data: list[dict] | None = None

    # Hourly COP data for dashboard
    cop_scatter_data: list[dict] | None = None


def _filter_stable_hours(df: pd.DataFrame, power_col: str, temp_col: str) -> pd.DataFrame:
    """Filter hours where heat pump ran continuously (not partial hours).

    Removes hours with:
    - Very low power (< MIN_POWER_FILTER)
    - Large power variations (potential defrost cycles or on/off switching)

    Args:
        df: DataFrame with hourly data
        power_col: Name of power column
        temp_col: Name of temperature column

    Returns:
        Filtered DataFrame with stable operation hours
    """
    if df.empty or power_col not in df.columns or temp_col not in df.columns:
        return df

    # Filter minimum power
    df_filtered = df[df[power_col] >= MIN_POWER_FILTER].copy()

    if len(df_filtered) < 10:
        return df_filtered

    # Calculate rolling statistics to identify stable periods
    # Use 3-hour window to detect stability
    df_filtered["power_rolling_std"] = (
        df_filtered[power_col].rolling(window=3, center=True, min_periods=1).std()
    )

    # Keep hours where power is relatively stable
    # Threshold: std dev should be < 20% of mean power
    mean_power = df_filtered[power_col].mean()
    stability_threshold = mean_power * 0.20

    df_stable = df_filtered[
        df_filtered["power_rolling_std"] < stability_threshold
    ].copy()

    # Drop the helper column
    df_stable = df_stable.drop(columns=["power_rolling_std"])

    _LOGGER.debug(
        "Filtered stable hours: %d → %d (removed %d unstable)",
        len(df_filtered),
        len(df_stable),
        len(df_filtered) - len(df_stable),
    )

    return df_stable


def _find_knee_by_grid_search(
    x_data: np.ndarray,
    y_data: np.ndarray,
    temp_min: float = -4.0,
    temp_max: float = 4.0,
    step: float = 0.25,
    min_points_per_segment: int = 5,
) -> tuple[float | None, float | None]:
    """Find knee temperature using exhaustive grid search over candidate splits.

    Evaluates all candidate knee temperatures on a grid and picks the split
    with the lowest total residual. Unlike curve_fit, this avoids local minima
    because every candidate is evaluated — not just a path from one starting point.

    Physical constraints enforced:
    1. Warm-side slope must be negative (power decreases as temp rises above knee).
    2. Cold-side slope must be substantially flatter than the warm-side slope.
       A real knee has a near-flat cold side (HP at max capacity) and a steeply
       falling warm side (HP modulating).  If both sides have similar slopes the
       data is closer to a straight line than a knee.

    Returns:
        Tuple of (knee_temp, knee_power), or (None, None) if no valid knee found.
    """
    candidates = np.arange(temp_min, temp_max + step / 2, step)
    best_temp = None
    best_power = None
    best_mse = np.inf

    for knee_t in candidates:
        left_mask = x_data < knee_t
        right_mask = x_data >= knee_t

        if left_mask.sum() < min_points_per_segment or right_mask.sum() < min_points_per_segment:
            continue

        x_left, y_left = x_data[left_mask], y_data[left_mask]
        x_right, y_right = x_data[right_mask], y_data[right_mask]

        try:
            slope_l, intercept_l = np.polyfit(x_left, y_left, 1)
            slope_r, intercept_r = np.polyfit(x_right, y_right, 1)
        except (np.linalg.LinAlgError, ValueError):
            continue

        # Physical constraint 1: warm-side slope must be negative.
        if slope_r >= 0:
            continue

        # Physical constraint 2: cold-side must be substantially flatter than
        # warm-side.  Reject splits where the cold side is more than 75 % as
        # steep as the warm side — that pattern is a gradual slope, not a knee.
        # (Factor 0.75 is permissive enough for noisy minute-level data while
        # still rejecting near-straight-line splits.)
        if slope_l < 0 and abs(slope_l) > abs(slope_r) * 0.75:
            continue

        pred_left = slope_l * x_left + intercept_l
        pred_right = slope_r * x_right + intercept_r

        # MSE normalised by total points so no segment can dominate by being
        # artificially tiny (a tiny cold-side segment always fits well; using
        # per-segment mean would favour putting the knee at the extreme cold end).
        mse = (
            np.sum((y_left - pred_left) ** 2) + np.sum((y_right - pred_right) ** 2)
        ) / (left_mask.sum() + right_mask.sum())

        if mse < best_mse:
            best_mse = mse
            best_temp = float(knee_t)
            power_at_knee = (
                slope_l * knee_t + intercept_l + slope_r * knee_t + intercept_r
            ) / 2
            best_power = float(power_at_knee)

    return best_temp, best_power


def _perform_knee_detection_quatt(df_hourly: pd.DataFrame) -> tuple[float | None, float | None]:
    """Perform knee detection using Quatt hourly data.

    Uses all available Quatt hourly data (not just last 10 days) for more
    reliable knee detection. Filters out partial hours and defrosts.

    Note: Only returns knee point. The Quatt stooklijn slope is calculated
    separately from HA recorder minute-level data (more accurate than
    hourly averages from the API).

    Args:
        df_hourly: Quatt insights hourly data with hpHeat and temperatureOutside

    Returns:
        Tuple of (knee_temp, knee_power)
    """
    if df_hourly is None or df_hourly.empty:
        return None, None

    if "hpHeat" not in df_hourly.columns or "temperatureOutside" not in df_hourly.columns:
        return None, None

    # Prepare data
    df_prep = df_hourly[
        (df_hourly["hpHeat"].notna()) & (df_hourly["temperatureOutside"].notna())
    ].copy()

    if df_prep.empty:
        return None, None

    # Filter for stable operation hours (removes partial hours and defrosts).
    df_stable = _filter_stable_hours(df_prep, "hpHeat", "temperatureOutside")

    if len(df_stable) < 20:  # Need enough data points
        _LOGGER.warning(
            "Not enough envelope hours for knee detection (%d < 20)", len(df_stable)
        )
        return None, None

    x_data = df_stable["temperatureOutside"].values
    y_data = df_stable["hpHeat"].values

    knee_temp, knee_power = _find_knee_by_grid_search(x_data, y_data)

    if knee_temp is None:
        _LOGGER.warning(
            "Grid search knee detection found no valid knee in range [-4, +4]°C "
            "(%d stable hours, temp range %.1f–%.1f°C)",
            len(df_stable),
            x_data.min(),
            x_data.max(),
        )
        return None, None

    _LOGGER.info(
        "Knee detection (Quatt): %.2f°C, %d W (from %d stable hours)",
        knee_temp,
        knee_power,
        len(df_stable),
    )

    return knee_temp, knee_power


async def async_fetch_live_history(
    hass: HomeAssistant,
    temp_entities: list[str],
    power_entity: str,
    days: int = DAYS_HISTORY,
) -> pd.DataFrame | None:
    """Fetch recent temperature and power history from HA recorder."""
    from homeassistant.util import dt as dt_util

    end_dt = dt_util.utcnow()
    start_dt = end_dt - timedelta(days=days)

    def _fetch_entity_states(entity_id):
        """Fetch states for a single entity from recorder."""
        return state_changes_during_period(
            hass,
            start_dt,
            end_dt,
            entity_id,
        )

    # Find temperature data (first available entity in priority order)
    df_temp = None
    for temp_entity in temp_entities:
        states = await get_instance(hass).async_add_executor_job(
            _fetch_entity_states, temp_entity
        )
        entity_states = states.get(temp_entity, [])
        if entity_states:
            records = []
            for s in entity_states:
                try:
                    ts = s.last_changed
                    if ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                    records.append({"timestamp": ts, "temp": float(s.state)})
                except (ValueError, TypeError):
                    continue
            if records:
                df_t = pd.DataFrame(records)
                df_t["timestamp"] = pd.to_datetime(df_t["timestamp"]).dt.floor("min")
                df_temp = df_t.groupby("timestamp")["temp"].median()
                _LOGGER.info("Using temperature from: %s (%d records)", temp_entity, len(records))
                break

    # Power data
    df_power = None
    power_states_dict = await get_instance(hass).async_add_executor_job(
        _fetch_entity_states, power_entity
    )
    power_states = power_states_dict.get(power_entity, [])
    if power_states:
        records = []
        for s in power_states:
            try:
                ts = s.last_changed
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                records.append({"timestamp": ts, "power": float(s.state)})
            except (ValueError, TypeError):
                continue
        if records:
            df_p = pd.DataFrame(records)
            df_p["timestamp"] = pd.to_datetime(df_p["timestamp"]).dt.floor("min")
            df_power = df_p.groupby("timestamp")["power"].median()
            _LOGGER.info("Power data: %d records from %s", len(records), power_entity)

    if df_temp is None or df_power is None:
        _LOGGER.warning(
            "Could not find temperature or power data (temp=%s, power=%s)",
            df_temp is not None,
            df_power is not None,
        )
        return None

    # Merge on timestamp
    merged = pd.merge(df_temp, df_power, left_index=True, right_index=True, how="inner")
    _LOGGER.info("Merged live history: %d aligned data points", len(merged))
    return merged


def calculate_stooklijn(
    df_ha_merged: pd.DataFrame | None,
    df_hourly: pd.DataFrame | None,
    df_daily: pd.DataFrame | None,
) -> StooklijnResult:
    """Run the full stooklijn analysis.

    Args:
        df_ha_merged: Live history data (temp + power), for knee detection
        df_hourly: Quatt insights hourly data, for envelope analysis
        df_daily: Quatt insights daily data, for optimal stooklijn
    """
    result = StooklijnResult()
    dynamic_min_temp = -0.5  # fallback

    # =========================================================
    # STEP 1: Knee detection (piecewise linear fit)
    # =========================================================
    # Priority: HA recorder minute-level data first, Quatt hourly as fallback.
    #
    # Why recorder is preferred:
    #   Quatt hourly averages mix active HP operation with defrost cycles within
    #   the same hour.  At cold temperatures a 15-minute defrost per hour lowers
    #   the average by ~25 %, making the cold-side data look systematically
    #   weaker than the warm side.  This biases the detected knee toward warmer
    #   temperatures (~3 °C instead of ~1.75 °C on real data).
    #
    #   Recorder minute-level data has no such bias: each minute at defrost power
    #   (< MIN_POWER_FILTER) is individually excluded, leaving only true high-power
    #   minutes regardless of temperature.
    #
    # Why Quatt is the fallback:
    #   The recorder only covers the last DAYS_HISTORY days.  During a mild spell
    #   (all days > 4 °C) there may be no data below the knee and detection fails.
    #   Quatt data spans months and always covers at least one cold period.
    knee_detected = False

    # --- Primary: HA recorder minute-level data ---
    if df_ha_merged is not None and not df_ha_merged.empty:
        _LOGGER.info("Attempting knee detection with recorder minute-level data...")
        valid_mask = df_ha_merged["power"] >= MIN_POWER_FILTER
        df_fit = df_ha_merged[valid_mask].copy()

        if not df_fit.empty and len(df_fit) > 10:
            x_data = df_fit["temp"].values
            y_data = df_fit["power"].values

            knee_temp, knee_power = _find_knee_by_grid_search(x_data, y_data)

            if knee_temp is not None:
                dynamic_min_temp = knee_temp
                result.knee_temperature = knee_temp
                result.knee_power = knee_power

                _LOGGER.info(
                    "Knee detected (recorder): %.2f°C, %d W (from %d days)",
                    result.knee_temperature,
                    result.knee_power,
                    DAYS_HISTORY,
                )
                knee_detected = True
            else:
                _LOGGER.info(
                    "Recorder knee detection found no valid knee "
                    "(temp range %.1f–%.1f°C, %d active points) — "
                    "will try Quatt hourly data",
                    x_data.min(),
                    x_data.max(),
                    len(df_fit),
                )

    # --- Fallback: Quatt hourly data (longer history, but defrost-diluted) ---
    if not knee_detected and df_hourly is not None and not df_hourly.empty:
        _LOGGER.info(
            "Falling back to Quatt hourly data for knee detection "
            "(recorder data insufficient)..."
        )
        knee_temp, knee_power = _perform_knee_detection_quatt(df_hourly)

        if knee_temp is not None:
            dynamic_min_temp = knee_temp
            result.knee_temperature = knee_temp
            result.knee_power = knee_power
            knee_detected = True

    if not knee_detected:
        _LOGGER.warning(
            "Knee detection failed with both recorder and Quatt data. "
            "Using fallback temperature: %.2f°C",
            dynamic_min_temp,
        )

    # =========================================================
    # STEP 1b: Quatt stooklijn estimation from recorder data
    # =========================================================
    # Use HA recorder minute-level data (not Quatt hourly averages) to
    # estimate the current Quatt stooklijn. Minute-level data correctly
    # captures instantaneous power, avoiding the problem where hourly
    # averages of partial operation pass the power filter.
    if df_ha_merged is not None and not df_ha_merged.empty:
        valid_mask = df_ha_merged["power"] >= MIN_POWER_FILTER
        df_fit = df_ha_merged[valid_mask].copy()

        df_right = df_fit[df_fit["temp"] >= dynamic_min_temp]
        if len(df_right) > 1:
            slope, intercept = np.polyfit(
                df_right["temp"].values, df_right["power"].values, 1
            )
            result.slope_api = float(slope)
            result.intercept_api = float(intercept)
            _LOGGER.info(
                "Quatt stooklijn estimated from recorder: slope=%.1f W/°C, "
                "intercept=%.0f W, zero at %.1f°C (%d data points)",
                slope,
                intercept,
                -intercept / slope if slope != 0 else float("inf"),
                len(df_right),
            )

    # =========================================================
    # STEP 2: Max-envelope analysis (freezing performance)
    # =========================================================
    if df_hourly is not None and not df_hourly.empty:
        if "hpHeat" in df_hourly.columns and "temperatureOutside" in df_hourly.columns:
            df_filtered = df_hourly[
                (df_hourly["hpHeat"] > 100)
                & (df_hourly["temperatureOutside"] < dynamic_min_temp)
                & (df_hourly["temperatureOutside"].notna())
            ].copy()

            if len(df_filtered) > 5:
                x = df_filtered["temperatureOutside"].values
                y = df_filtered["hpHeat"].values

                # Pre-envelope outlier removal (z-score)
                m_rough, b_rough = np.polyfit(x, y, 1)
                resid = y - (m_rough * x + b_rough)
                std = np.std(resid)
                if std > 0:
                    mask = np.abs(resid) < (2.5 * std)
                    df_clean = df_filtered[mask].copy()
                else:
                    df_clean = df_filtered.copy()

                # Max-envelope filter
                df_clean["temp_bin"] = (
                    df_clean["temperatureOutside"] / BIN_SIZE
                ).round() * BIN_SIZE
                df_clean["max_in_bin"] = df_clean.groupby("temp_bin")[
                    "hpHeat"
                ].transform("max")
                mask_env = df_clean["hpHeat"] >= (
                    df_clean["max_in_bin"] * KEEP_THRESHOLD
                )
                df_envelope = df_clean[mask_env]

                if len(df_envelope) > 1:
                    x_env = df_envelope["temperatureOutside"].values
                    y_env = df_envelope["hpHeat"].values
                    slope, intercept = np.polyfit(x_env, y_env, 1)

                    y_pred = slope * x_env + intercept
                    ss_res = np.sum((y_env - y_pred) ** 2)
                    ss_tot = np.sum((y_env - np.mean(y_env)) ** 2)
                    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

                    result.slope_local = float(slope)
                    result.intercept_local = float(intercept)
                    result.r2_local = float(r2)

    # =========================================================
    # STEP 3: Optimal stooklijn from daily usage pattern
    # =========================================================
    _MIN_HEATING_W = 200  # Minimum W/h to count as a heating day

    if df_daily is not None and not df_daily.empty:
        cols_needed = ["avg_temperatureOutside", "totalHeatPerHour"]
        if all(c in df_daily.columns for c in cols_needed):
            plot_data = df_daily[cols_needed].replace(
                [np.inf, -np.inf], np.nan
            ).dropna()

            # Filter out non-heating days (summer) for regression
            heating_data = plot_data[
                plot_data["totalHeatPerHour"] >= _MIN_HEATING_W
            ]

            if len(heating_data) > 5:
                x = heating_data["avg_temperatureOutside"].values
                y = heating_data["totalHeatPerHour"].values
                slope, intercept = np.polyfit(x, y, 1)

                y_pred = slope * x + intercept
                ss_res = np.sum((y - y_pred) ** 2)
                ss_tot = np.sum((y - np.mean(y)) ** 2)
                r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

                result.slope_optimal = float(slope)
                result.intercept_optimal = float(intercept)
                result.r2_optimal = float(r2)
                if slope != 0:
                    result.balance_temp_optimal = float(-intercept / slope)

                # Build scatter data for dashboard (only heating days)
                scatter = []
                for _, row in heating_data.iterrows():
                    cop_val = None
                    if "averageCOP" in df_daily.columns:
                        idx = row.name if hasattr(row, "name") else None
                        if idx is not None and idx in df_daily.index:
                            cop_val = df_daily.loc[idx, "averageCOP"]
                            if pd.notna(cop_val):
                                cop_val = float(cop_val)
                            else:
                                cop_val = None
                    scatter.append(
                        {
                            "temp": round(float(row["avg_temperatureOutside"]), 1),
                            "heat": round(float(row["totalHeatPerHour"]), 0),
                            "cop": cop_val,
                        }
                    )
                result.scatter_data = scatter

        # Build COP scatter data (only heating days with valid COP)
        if "averageCOP" in df_daily.columns and "avg_temperatureOutside" in df_daily.columns:
            cop_data = df_daily[["avg_temperatureOutside", "averageCOP"]].replace(
                [np.inf, -np.inf], np.nan
            ).dropna()
            # Filter: only days with meaningful COP (heating days)
            cop_data = cop_data[cop_data["averageCOP"] > 0]
            if "totalHeatPerHour" in df_daily.columns:
                valid_idx = df_daily["totalHeatPerHour"] >= _MIN_HEATING_W
                cop_data = cop_data[cop_data.index.isin(df_daily[valid_idx].index)]
            result.cop_scatter_data = [
                {
                    "temp": round(float(row["avg_temperatureOutside"]), 1),
                    "cop": round(float(row["averageCOP"]), 2),
                }
                for _, row in cop_data.iterrows()
            ]

    return result
