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
from scipy.optimize import curve_fit

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Analysis constants
MIN_POWER_FILTER = 2500  # W
DEFROST_THRESHOLD = 0  # W
BIN_SIZE = 0.5  # °C
KEEP_THRESHOLD = 0.90  # 90% of max
DAYS_HISTORY = 10


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


def _piecewise_linear(x, x0, y0, k1, k2):
    """Piecewise linear function for knee detection."""
    return np.where(x < x0, k1 * (x - x0) + y0, k2 * (x - x0) + y0)


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
    # STEP 1: Knee detection (piecewise linear fit on live data)
    # =========================================================
    if df_ha_merged is not None and not df_ha_merged.empty:
        # Categorize data
        valid_mask = df_ha_merged["power"] >= MIN_POWER_FILTER
        df_fit = df_ha_merged[valid_mask].copy()

        if not df_fit.empty and len(df_fit) > 10:
            x_data = df_fit["temp"].values
            y_data = df_fit["power"].values

            p0 = [1.0, y_data.max(), 0, -400]
            lower_b = [-3, 3000, -500, -2000]
            upper_b = [4, 9000, 500, -100]

            try:
                popt, _ = curve_fit(
                    _piecewise_linear, x_data, y_data, p0=p0, bounds=(lower_b, upper_b)
                )
                dynamic_min_temp = popt[0]
                result.knee_temperature = float(popt[0])
                result.knee_power = float(popt[1])

                # Fit line to data right of knee (API stooklijn)
                df_right = df_fit[df_fit["temp"] >= dynamic_min_temp]
                if len(df_right) > 1:
                    slope, intercept = np.polyfit(
                        df_right["temp"].values, df_right["power"].values, 1
                    )
                    result.slope_api = float(slope)
                    result.intercept_api = float(intercept)

                _LOGGER.info(
                    "Knee detected at %.2f°C, %d W",
                    result.knee_temperature,
                    result.knee_power,
                )
            except Exception:
                _LOGGER.warning("Piecewise fit failed for knee detection")

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
    if df_daily is not None and not df_daily.empty:
        cols_needed = ["avg_temperatureOutside", "totalHeatPerHour"]
        if all(c in df_daily.columns for c in cols_needed):
            plot_data = df_daily[cols_needed].replace(
                [np.inf, -np.inf], np.nan
            ).dropna()

            if len(plot_data) > 5:
                x = plot_data["avg_temperatureOutside"].values
                y = plot_data["totalHeatPerHour"].values
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

                # Build scatter data for dashboard (daily points)
                scatter = []
                for _, row in plot_data.iterrows():
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

        # Build COP scatter data
        if "averageCOP" in df_daily.columns and "avg_temperatureOutside" in df_daily.columns:
            cop_data = df_daily[["avg_temperatureOutside", "averageCOP"]].replace(
                [np.inf, -np.inf], np.nan
            ).dropna()
            result.cop_scatter_data = [
                {
                    "temp": round(float(row["avg_temperatureOutside"]), 1),
                    "cop": round(float(row["averageCOP"]), 2),
                }
                for _, row in cop_data.iterrows()
            ]

    return result
