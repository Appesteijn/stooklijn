"""Gas consumption data fetching and processing (from notebook Cell 3)."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_fetch_gas_data(
    hass: HomeAssistant,
    entity_id: str,
    start_date: str,
    end_date: str,
    calorific_value: float = 9.77,
    boiler_efficiency: float = 0.90,
    hot_water_temp_threshold: float = 18.0,
    df_hourly_hp: pd.DataFrame | None = None,
    temp_entities: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch gas consumption data and return (df_gas_hourly, df_gas_daily).

    Args:
        hass: Home Assistant instance
        entity_id: Gas meter entity ID (cumulative m³)
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        calorific_value: kWh per m³ (default 9.77 for Dutch gas)
        boiler_efficiency: Boiler efficiency ratio (default 0.90)
        hot_water_temp_threshold: Days above this temp are hot-water-only
        df_hourly_hp: Optional heat pump hourly data for temperature reuse
        temp_entities: Temperature sensor entity IDs to fetch from recorder
    """
    from homeassistant.util import dt as dt_util

    start_dt = dt_util.parse_datetime(f"{start_date}T00:00:00+00:00")
    end_dt = dt_util.parse_datetime(f"{end_date}T23:59:59+00:00")

    # Fetch history from recorder
    def _fetch():
        return state_changes_during_period(hass, start_dt, end_dt, entity_id)

    states = await get_instance(hass).async_add_executor_job(_fetch)

    entity_states = states.get(entity_id, [])
    if not entity_states:
        _LOGGER.warning("No gas data found for entity: %s", entity_id)
        return pd.DataFrame(), pd.DataFrame()

    # Build DataFrame from state history
    records = []
    for state in entity_states:
        try:
            value = float(state.state)
            records.append(
                {
                    "timestamp": state.last_changed.replace(tzinfo=None),
                    "state": value,
                }
            )
        except (ValueError, TypeError):
            continue

    if not records:
        return pd.DataFrame(), pd.DataFrame()

    df_gas = pd.DataFrame(records)
    df_gas = df_gas.set_index("timestamp").sort_index()

    # Calculate consumption differences (cumulative meter)
    df_gas["gas_m3"] = df_gas["state"].diff()
    df_gas = df_gas[df_gas["gas_m3"] >= 0]
    df_gas = df_gas[df_gas["gas_m3"] < 10]  # Remove unrealistic spikes

    # Convert to heat output
    df_gas["heat_kwh"] = df_gas["gas_m3"] * calorific_value * boiler_efficiency
    df_gas["heat_w"] = df_gas["heat_kwh"] * 1000

    # Resample to hourly
    df_gas_hourly = df_gas.resample("h").agg(
        {"gas_m3": "sum", "heat_kwh": "sum", "heat_w": "mean"}
    )

    # Create daily summary
    df_gas_daily = df_gas.resample("d").agg({"gas_m3": "sum", "heat_kwh": "sum"})

    # Temperature data & hot water correction
    has_temp = False

    # Primary: fetch temperature from HA recorder for the gas date range
    if temp_entities:
        for temp_entity in temp_entities:
            def _fetch_temp(eid=temp_entity):
                return state_changes_during_period(hass, start_dt, end_dt, eid)

            temp_states = await get_instance(hass).async_add_executor_job(_fetch_temp)
            entity_temp_states = temp_states.get(temp_entity, [])
            if entity_temp_states:
                temp_records = []
                for s in entity_temp_states:
                    try:
                        ts = s.last_changed
                        if ts.tzinfo is not None:
                            ts = ts.replace(tzinfo=None)
                        temp_records.append(
                            {"timestamp": ts, "temperatureOutside": float(s.state)}
                        )
                    except (ValueError, TypeError):
                        continue
                if temp_records:
                    df_temp = pd.DataFrame(temp_records)
                    df_temp["timestamp"] = pd.to_datetime(
                        df_temp["timestamp"]
                    ).dt.floor("h")
                    df_temp = df_temp.groupby("timestamp")[
                        "temperatureOutside"
                    ].median()
                    df_temp = df_temp.to_frame()

                    df_gas_hourly = df_gas_hourly.join(df_temp, how="left")

                    daily_temp = df_temp.groupby(df_temp.index.date)[
                        "temperatureOutside"
                    ].mean()
                    df_gas_daily["avg_temperatureOutside"] = (
                        df_gas_daily.index.map(
                            lambda d: daily_temp.get(d.date(), None)
                        )
                    )
                    has_temp = True
                    _LOGGER.info(
                        "Gas temperature from recorder: %s (%d records)",
                        temp_entity,
                        len(temp_records),
                    )
                    break

    # Fallback: use heat pump hourly data (works when date ranges overlap)
    if not has_temp and df_hourly_hp is not None and not df_hourly_hp.empty:
        if "temperatureOutside" in df_hourly_hp.columns:
            df_hp_temp = df_hourly_hp[["temperatureOutside"]].copy()
            if df_hp_temp.index.tz is not None:
                df_hp_temp.index = df_hp_temp.index.tz_localize(None)
            df_gas_hourly = df_gas_hourly.join(df_hp_temp, how="left")

            daily_temp = df_hourly_hp.groupby(df_hourly_hp.index.date)[
                "temperatureOutside"
            ].mean()
            df_gas_daily["avg_temperatureOutside"] = df_gas_daily.index.map(
                lambda d: daily_temp.get(d.date(), None)
            )
            has_temp = True

    # Hot water correction
    if has_temp and "avg_temperatureOutside" in df_gas_daily.columns:
        warm_days = df_gas_daily[
            df_gas_daily["avg_temperatureOutside"] >= hot_water_temp_threshold
        ]

        if len(warm_days) >= 3:
            hot_water_gas_m3 = warm_days["gas_m3"].median()
            hot_water_kwh = hot_water_gas_m3 * calorific_value * boiler_efficiency

            df_gas_daily["gas_m3_hot_water"] = hot_water_gas_m3
            df_gas_daily["heat_kwh_hot_water"] = hot_water_kwh
            df_gas_daily["gas_m3_heating"] = (
                df_gas_daily["gas_m3"] - hot_water_gas_m3
            ).clip(lower=0)
            df_gas_daily["heat_kwh_heating"] = (
                df_gas_daily["gas_m3_heating"] * calorific_value * boiler_efficiency
            )
            df_gas_daily["totalHeatPerHour"] = (
                df_gas_daily["heat_kwh_heating"] * 1000
            ) / 24
            _LOGGER.info(
                "Hot water correction applied: %.2f m³/day baseline", hot_water_gas_m3
            )
        else:
            df_gas_daily["totalHeatPerHour"] = (
                df_gas_daily["heat_kwh"] * 1000
            ) / 24
    else:
        df_gas_daily["totalHeatPerHour"] = (df_gas_daily["heat_kwh"] * 1000) / 24

    return df_gas_hourly, df_gas_daily
