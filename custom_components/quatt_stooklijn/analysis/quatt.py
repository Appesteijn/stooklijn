"""Quatt heat pump data fetching and processing (from notebook Cell 2)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_fetch_quatt_insights(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch Quatt insights data and return (df_hourly, df_daily).

    Calls the quatt.get_insights service for each day in the range,
    collects hourly graph data and daily totals.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    all_dates = pd.date_range(start=start_dt, end=end_dt)

    hourly_chunks = []
    daily_records = []

    for current_date in all_dates:
        date_str = current_date.strftime("%Y-%m-%d")
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

        except Exception:
            _LOGGER.warning("Failed to fetch Quatt data for %s", date_str)

    # Build hourly DataFrame
    if hourly_chunks:
        hourly_chunks = [c for c in hourly_chunks if not c.empty and not c.isna().all().all()]
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
