"""Quatt heat pump data fetching and processing.

Uses a hybrid approach:
- Recorder long-term statistics for historical daily data (months of history)
- Quatt get_insights API for recent hourly data (last 30 days, cached)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..cache import QuattInsightsCache
from ..const import (
    API_FETCH_DAYS,
    RECORDER_BOILER_HEAT_ENTITY,
    RECORDER_POWER_INPUT_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

# Global cache instance (will be initialized on first use)
_cache: QuattInsightsCache | None = None


async def _get_cache(hass: HomeAssistant) -> QuattInsightsCache:
    """Get or create the global cache instance."""
    global _cache
    if _cache is None:
        _cache = QuattInsightsCache(hass)
        await _cache.async_load()
    return _cache


async def _async_fetch_recorder_daily(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
    power_entity: str,
    temp_entity: str,
) -> pd.DataFrame:
    """Fetch daily mean statistics from HA recorder.

    Returns a DataFrame with the same columns as the API-based daily data,
    using recorder long-term statistics (available for months of history).
    """
    start_dt = dt_util.as_utc(datetime.strptime(start_date, "%Y-%m-%d"))
    end_dt = dt_util.as_utc(
        datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    )

    statistic_ids = {
        power_entity,
        temp_entity,
        RECORDER_POWER_INPUT_ENTITY,
        RECORDER_BOILER_HEAT_ENTITY,
    }

    def _fetch_stats() -> dict:
        return statistics_during_period(
            hass,
            start_time=start_dt,
            end_time=end_dt,
            statistic_ids=statistic_ids,
            period="day",
            units=None,
            types={"mean"},
        )

    stats = await get_instance(hass).async_add_executor_job(_fetch_stats)

    if not stats:
        _LOGGER.warning("No recorder statistics available")
        return pd.DataFrame()

    # Build a dict of {date: {column: value}}
    records: dict[str, dict] = {}

    for sensor_id, rows in stats.items():
        for row in rows:
            start_ts = row.get("start")
            if start_ts is None:
                continue
            day = dt_util.utc_from_timestamp(start_ts).strftime("%Y-%m-%d")

            if day not in records:
                records[day] = {}

            mean_val = row.get("mean")
            if mean_val is None:
                continue

            if sensor_id == power_entity:
                # Mean W * 24h = daily Wh
                records[day]["totalHpHeat"] = mean_val * 24
            elif sensor_id == RECORDER_POWER_INPUT_ENTITY:
                records[day]["totalHpElectric"] = mean_val * 24
            elif sensor_id == RECORDER_BOILER_HEAT_ENTITY:
                records[day]["totalBoilerHeat"] = mean_val * 24
            elif sensor_id == temp_entity:
                records[day]["avg_temperatureOutside"] = mean_val

    if not records:
        _LOGGER.warning("Recorder statistics returned no usable data")
        return pd.DataFrame()

    df = pd.DataFrame.from_dict(records, orient="index")
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    df = df.sort_index()

    # Fill missing columns with 0
    for col in ("totalHpHeat", "totalHpElectric", "totalBoilerHeat"):
        if col not in df.columns:
            df[col] = 0

    # Calculate derived columns
    df["totalHeatPerHour"] = (
        df["totalHpHeat"].fillna(0) + df["totalBoilerHeat"].fillna(0)
    ) / 24
    df["totalBoilerGas"] = 0  # Not available from recorder

    # Calculate COP from energy totals (not from the COP sensor, which
    # averages over 24h including off-periods and gives too-low values)
    hp_heat = df["totalHpHeat"].fillna(0)
    hp_elec = df["totalHpElectric"].fillna(0)
    df["averageCOP"] = (hp_heat / hp_elec).replace(
        [float("inf"), -float("inf")], 0
    )
    df.loc[hp_elec <= 0, "averageCOP"] = 0

    _LOGGER.info(
        "Recorder statistics: %d days (%s to %s)",
        len(df),
        df.index[0].strftime("%Y-%m-%d"),
        df.index[-1].strftime("%Y-%m-%d"),
    )

    return df


async def _async_fetch_api_days(
    hass: HomeAssistant,
    start_dt: datetime,
    end_dt: datetime,
    cache: QuattInsightsCache,
) -> tuple[list[pd.DataFrame], list[dict], int, int]:
    """Fetch daily/hourly data from Quatt API for a date range, using cache."""
    all_dates = pd.date_range(start=start_dt, end=end_dt)

    hourly_chunks = []
    daily_records = []
    api_calls_made = 0
    cache_hits = 0

    for current_date in all_dates:
        date_str = current_date.strftime("%Y-%m-%d")
        data = None

        # Check cache first
        cached_data = cache.get(date_str)
        if cached_data is not None:
            data = cached_data
            cache_hits += 1
        else:
            # Fetch from API
            try:
                response = await hass.services.async_call(
                    "quatt",
                    "get_insights",
                    {
                        "from_date": date_str,
                        "timeframe": "day",
                        "advanced_insights": True,
                    },
                    blocking=True,
                    return_response=True,
                )

                data = response.get("service_response", response)
                api_calls_made += 1

                if cache.should_cache(date_str):
                    cache.set(date_str, data)

            except Exception as e:
                _LOGGER.warning("Failed to fetch Quatt data for %s: %s", date_str, e)
                continue

        if data is None:
            continue

        try:
            daily_records.append(
                {
                    "date": current_date,
                    "totalHpHeat": data.get("totalHpHeat", 0),
                    "totalHpElectric": data.get("totalHpElectric", 0),
                    "totalBoilerHeat": data.get("totalBoilerHeat", 0),
                    "totalBoilerGas": data.get("totalBoilerGas", 0),
                    "averageCOP": data.get("averageCOP", None),
                }
            )

            # Process hourly graph data
            df_main = pd.DataFrame(data.get("graph", []))
            if not df_main.empty:
                for graph_key in (
                    "outsideTemperatureGraph",
                    "waterTemperatureGraph",
                    "roomTemperatureGraph",
                ):
                    df_graph = pd.DataFrame(data.get(graph_key, []))
                    if not df_graph.empty:
                        df_main = pd.merge(
                            df_main, df_graph, on="timestamp", how="left"
                        )
                hourly_chunks.append(df_main)

        except Exception as e:
            _LOGGER.warning("Failed to process data for %s: %s", date_str, e)
            continue

    return hourly_chunks, daily_records, api_calls_made, cache_hits


async def async_fetch_quatt_insights(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
    power_entity: str = "sensor.heatpump_total_power",
    temp_entity: str = "sensor.heatpump_hp1_temperature_outside",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch Quatt data using a hybrid approach.

    1. Recorder statistics for the full configured period (daily means)
    2. Quatt API for the last API_FETCH_DAYS days (hourly detail, cached)
    3. Merge: API daily data overwrites recorder data where available
    """
    cache = await _get_cache(hass)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # === Step 1: Recorder statistics for full history ===
    _LOGGER.info("Fetching recorder statistics for %s to %s...", start_date, end_date)
    df_daily_recorder = await _async_fetch_recorder_daily(
        hass, start_date, end_date, power_entity, temp_entity
    )

    # === Step 2: Quatt API for last N days (hourly detail) ===
    api_start = max(start_dt, end_dt - timedelta(days=API_FETCH_DAYS - 1))
    api_start_str = api_start.strftime("%Y-%m-%d")

    _LOGGER.info(
        "Fetching Quatt API data for %s to %s (%d days)...",
        api_start_str,
        end_date,
        (end_dt - api_start).days + 1,
    )

    hourly_chunks, daily_records, api_calls_made, cache_hits = (
        await _async_fetch_api_days(hass, api_start, end_dt, cache)
    )

    # Save cache if we made any API calls
    if api_calls_made > 0:
        await cache.async_save()

    _LOGGER.info(
        "API data: %d days (%d from cache, %d from API)",
        cache_hits + api_calls_made,
        cache_hits,
        api_calls_made,
    )

    # === Step 3: Build hourly DataFrame ===
    if hourly_chunks:
        hourly_chunks = [
            c.dropna(axis=1, how="all")
            for c in hourly_chunks
            if not c.empty and not c.isna().all().all()
        ]
        df_hourly = pd.concat(hourly_chunks, ignore_index=True)
        df_hourly["timestamp"] = pd.to_datetime(df_hourly["timestamp"])
        df_hourly = df_hourly.set_index("timestamp")
        df_hourly = df_hourly[~df_hourly.index.duplicated(keep="last")].sort_index()
    else:
        df_hourly = pd.DataFrame()

    # === Step 4: Build API daily DataFrame ===
    df_daily_api = pd.DataFrame()
    if daily_records:
        df_daily_api = pd.DataFrame(daily_records)
        df_daily_api["date"] = pd.to_datetime(df_daily_api["date"])
        df_daily_api = df_daily_api.set_index("date")

        # Calculate avg temperature from hourly data
        if not df_hourly.empty and "temperatureOutside" in df_hourly.columns:
            daily_avg_temp = df_hourly.groupby(df_hourly.index.date)[
                "temperatureOutside"
            ].mean()
            df_daily_api["avg_temperatureOutside"] = df_daily_api.index.map(
                lambda d: daily_avg_temp.get(d.date(), None)
            )

        df_daily_api["totalHeatPerHour"] = (
            df_daily_api.get("totalHpHeat", pd.Series(0)).fillna(0)
            + df_daily_api.get("totalBoilerHeat", pd.Series(0)).fillna(0)
        ) / 24

        # Calculate COP if missing
        if (
            "averageCOP" not in df_daily_api.columns
            or df_daily_api["averageCOP"].isna().all()
        ):
            df_daily_api["averageCOP"] = (
                df_daily_api["totalHpHeat"] / df_daily_api["totalHpElectric"]
            )
            df_daily_api["averageCOP"] = df_daily_api["averageCOP"].replace(
                [float("inf"), -float("inf")], 0
            )

    # === Step 5: Merge — recorder as base, API overwrites recent days ===
    if not df_daily_recorder.empty and not df_daily_api.empty:
        # API data is more accurate for recent days, so it takes priority
        df_daily = df_daily_recorder.copy()
        df_daily.update(df_daily_api)
        # Add any API-only rows (e.g. today)
        new_rows = df_daily_api.loc[~df_daily_api.index.isin(df_daily.index)]
        if not new_rows.empty:
            df_daily = pd.concat([df_daily, new_rows])
        df_daily = df_daily.sort_index()

        recorder_only = len(df_daily) - len(df_daily_api)
        _LOGGER.info(
            "Combined daily data: %d days total "
            "(%d from recorder, %d updated by API)",
            len(df_daily),
            recorder_only,
            len(df_daily_api),
        )
    elif not df_daily_api.empty:
        df_daily = df_daily_api
    elif not df_daily_recorder.empty:
        df_daily = df_daily_recorder
    else:
        df_daily = pd.DataFrame()

    return df_hourly, df_daily
