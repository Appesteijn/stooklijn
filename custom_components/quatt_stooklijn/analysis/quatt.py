"""Quatt heat pump data fetching and processing (from notebook Cell 2)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from homeassistant.core import HomeAssistant

from ..cache import QuattInsightsCache

_LOGGER = logging.getLogger(__name__)

# Cache configuration
MAX_INITIAL_FETCH_DAYS = 30  # Limit first run to prevent API abuse

# Global cache instance (will be initialized on first use)
_cache: QuattInsightsCache | None = None


async def _get_cache(hass: HomeAssistant) -> QuattInsightsCache:
    """Get or create the global cache instance."""
    global _cache
    if _cache is None:
        _cache = QuattInsightsCache(hass)
        await _cache.async_load()
    return _cache


async def async_fetch_quatt_insights(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch Quatt insights data and return (df_hourly, df_daily).

    Uses caching to minimize API calls. Only fetches missing dates.
    Dates before today are cached permanently (immutable data).

    On first run (empty cache), limits fetch to last MAX_INITIAL_FETCH_DAYS
    to prevent API abuse. Full history builds up organically over time.
    """
    cache = await _get_cache(hass)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Check if this is first run (empty cache)
    cache_stats = cache.get_stats()
    is_first_run = cache_stats["total_days"] == 0

    if is_first_run:
        # Limit initial fetch to prevent API abuse
        configured_days = (end_dt - start_dt).days + 1
        earliest_allowed = end_dt - timedelta(days=MAX_INITIAL_FETCH_DAYS - 1)

        if start_dt < earliest_allowed:
            _LOGGER.info(
                "First run detected: limiting initial fetch to last %d days "
                "(configuration requested %d days). Full history will build up "
                "organically as you run analyses over time.",
                MAX_INITIAL_FETCH_DAYS,
                configured_days,
            )
            start_dt = earliest_allowed
        else:
            _LOGGER.info(
                "First run detected: fetching %d days as requested "
                "(within %d day limit).",
                configured_days,
                MAX_INITIAL_FETCH_DAYS,
            )

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
            _LOGGER.debug("Using cached data for %s", date_str)
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

                # Cache the data if it's a completed day (before today)
                if cache.should_cache(date_str):
                    cache.set(date_str, data)

            except Exception as e:
                _LOGGER.warning("Failed to fetch Quatt data for %s: %s", date_str, e)
                continue

        # Process the data (from cache or API)
        if data is None:
            continue

        try:
            daily_record = {
                "date": current_date,
                "totalHpHeat": data.get("totalHpHeat", 0),
                "totalHpElectric": data.get("totalHpElectric", 0),
                "totalBoilerHeat": data.get("totalBoilerHeat", 0),
                "totalBoilerGas": data.get("totalBoilerGas", 0),
                "averageCOP": data.get("averageCOP", None),
            }

            # Process hourly graph data
            df_main = pd.DataFrame(data.get("graph", []))
            if not df_main.empty:
                # Merge temperature graphs
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

            daily_records.append(daily_record)

        except Exception as e:
            _LOGGER.warning("Failed to process data for %s: %s", date_str, e)
            continue

    # Save cache if we made any API calls
    if api_calls_made > 0:
        await cache.async_save()

    # Log cache statistics
    total_days = len(all_dates)
    cache_stats_after = cache.get_stats()

    _LOGGER.info(
        "Insights data: %d days total, %d from cache, %d from API",
        total_days,
        cache_hits,
        api_calls_made,
    )

    if cache_stats_after["total_days"] > 0:
        _LOGGER.info(
            "Cache now contains %d days (%s to %s)",
            cache_stats_after["total_days"],
            cache_stats_after["oldest_date"],
            cache_stats_after["newest_date"],
        )

        # Show growth if first run
        if is_first_run and cache_stats_after["total_days"] < 365:
            days_until_full = 365 - cache_stats_after["total_days"]
            _LOGGER.info(
                "Cache will reach full year of history in ~%d days "
                "(1 new day added per analysis run)",
                days_until_full,
            )

    # Build hourly DataFrame
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

    # Build daily DataFrame
    if daily_records:
        df_daily = pd.DataFrame(daily_records)
        df_daily["date"] = pd.to_datetime(df_daily["date"])
        df_daily = df_daily.set_index("date")
        df_daily = df_daily[~df_daily.index.duplicated(keep="last")].sort_index()

        # Calculate derived columns
        if not df_hourly.empty and "temperatureOutside" in df_hourly.columns:
            daily_avg_temp = df_hourly.groupby(df_hourly.index.date)[
                "temperatureOutside"
            ].mean()
            df_daily["avg_temperatureOutside"] = df_daily.index.map(
                lambda d: daily_avg_temp.get(d.date(), None)
            )

        df_daily["totalHeatPerHour"] = (
            df_daily.get("totalHpHeat", pd.Series(0)).fillna(0)
            + df_daily.get("totalBoilerHeat", pd.Series(0)).fillna(0)
        ) / 24

        # Calculate COP if missing
        if (
            "averageCOP" not in df_daily.columns
            or df_daily["averageCOP"].isna().all()
        ):
            df_daily["averageCOP"] = (
                df_daily["totalHpHeat"] / df_daily["totalHpElectric"]
            )
            df_daily["averageCOP"] = df_daily["averageCOP"].replace(
                [float("inf"), -float("inf")], 0
            )
    else:
        df_daily = pd.DataFrame()

    return df_hourly, df_daily
