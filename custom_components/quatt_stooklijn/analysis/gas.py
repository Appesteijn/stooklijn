"""Gas consumption data fetching and processing (from notebook Cell 3)."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant

from ..const import STATS_UNITS

_LOGGER = logging.getLogger(__name__)

# Eén meterstapje groter dan dit is een sprong (meterwissel, reset), geen verbruik.
MAX_GAS_STEP_M3 = 10.0


def stats_rows_to_series(rows, key: str) -> pd.Series | None:
    """Uurrijen uit ``statistics_during_period`` als reeks, index in UTC (naïef).

    ``start`` is een epoch in seconden (recente HA) of een datetime (oudere).
    Geeft None als er geen enkele bruikbare waarde in zit.
    """
    index, values = [], []
    for row in rows or []:
        value, start = row.get(key), row.get("start")
        if value is None or start is None:
            continue
        if isinstance(start, datetime):
            ts = pd.Timestamp(start)
            ts = ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts
        else:
            ts = pd.Timestamp(float(start), unit="s")
        index.append(ts)
        values.append(float(value))
    if not values:
        return None
    return pd.Series(values, index=pd.DatetimeIndex(index)).sort_index()


async def _async_hourly_statistic(
    hass: HomeAssistant, entity_id: str, start_dt, end_dt, stat_type: str
) -> pd.Series | None:
    """Uurstatistiek (``change`` of ``mean``) van één entiteit, of None.

    De langetermijnstatistiek blijft bewaard na de purge van de ruwe historie
    (standaard 10 dagen). De gasperiode ligt per definitie vóór de warmtepomp,
    dus bijna altijd buiten die retentie: alleen de statistiek heeft hem nog
    (GitHub #3).
    """
    def _fetch() -> dict:
        return statistics_during_period(
            hass, start_time=start_dt, end_time=end_dt,
            statistic_ids={entity_id}, period="hour", units=STATS_UNITS,
            types={stat_type},
        )

    try:
        stats = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as err:  # noqa: BLE001 - terugvallen op de ruwe historie
        _LOGGER.debug("Statistics for %s unavailable: %s", entity_id, err)
        return None
    return stats_rows_to_series((stats or {}).get(entity_id), stat_type)


def gas_frames(
    gas_m3: pd.Series, calorific_value: float, boiler_efficiency: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Uur- en dagtabel uit gasverbruik per uur (m³)."""
    gas_m3 = gas_m3[(gas_m3 >= 0) & (gas_m3 < MAX_GAS_STEP_M3)]
    df = gas_m3.to_frame("gas_m3")
    df = df.resample("h").sum()
    df["heat_kwh"] = df["gas_m3"] * calorific_value * boiler_efficiency
    # kWh in één uur = gemiddeld vermogen in kW.
    df["heat_w"] = df["heat_kwh"] * 1000
    daily = df.resample("D").agg({"gas_m3": "sum", "heat_kwh": "sum"})
    return df, daily


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

    tz = dt_util.get_time_zone(hass.config.time_zone)
    start_dt = dt_util.as_utc(
        datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
    )
    end_dt = dt_util.as_utc(
        datetime.strptime(f"{end_date} 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    )

    # Eerst de langetermijnstatistiek: die overleeft de purge van de ruwe
    # historie, en de gasperiode ligt vrijwel altijd verder terug dan dat.
    gas_m3 = await _async_hourly_statistic(hass, entity_id, start_dt, end_dt, "change")
    source = "statistics"
    if gas_m3 is None:
        gas_m3 = await _async_gas_from_history(hass, entity_id, start_dt, end_dt)
        source = "history"
    if gas_m3 is None:
        _LOGGER.warning(
            "No gas data found for entity %s between %s and %s: no long-term "
            "statistics (needs state_class total/total_increasing) and no "
            "recorder history",
            entity_id, start_date, end_date,
        )
        return pd.DataFrame(), pd.DataFrame()

    df_gas_hourly, df_gas_daily = gas_frames(gas_m3, calorific_value, boiler_efficiency)
    _LOGGER.info(
        "Gas data for %s from %s: %d days, %.0f m³",
        entity_id, source, len(df_gas_daily), df_gas_daily["gas_m3"].sum(),
    )

    # Temperature data & hot water correction
    has_temp = False

    # Buitentemperatuur voor de gasperiode: per sensor de statistiek
    # (uurgemiddelde), anders de ruwe historie. Per uur wint de eerste sensor in
    # de volgorde van de configuratie die een waarde heeft; de volgende vullen
    # de gaten. Tot v0.10.7-beta.16 telde alleen de eerste sensor met data:
    # met een warmtepompsensor die pas bij de installatie begint, overlapte die
    # alleen met de staart van de gasperiode (GitHub #3: 27 dagen, 11 W/K,
    # terwijl een tweede sensor het hele jaar had).
    temp_hourly: pd.Series | None = None
    for temp_entity in temp_entities or []:
        series = await _async_hourly_statistic(hass, temp_entity, start_dt, end_dt, "mean")
        temp_source = "statistics"
        if series is None:
            df_hist = await _async_temp_from_history(hass, temp_entity, start_dt, end_dt)
            series = (
                df_hist["temperatureOutside"]
                if df_hist is not None and not df_hist.empty else None
            )
            temp_source = "history"
        if series is None or series.empty:
            continue
        added = (
            len(series) if temp_hourly is None
            else int((~series.index.isin(temp_hourly.index)).sum())
        )
        temp_hourly = series if temp_hourly is None else temp_hourly.combine_first(series)
        _LOGGER.info(
            "Gas temperature from %s: %s (%d hours, %d new)",
            temp_source, temp_entity, len(series), added,
        )

    if temp_hourly is not None:
        df_temp = temp_hourly.sort_index().to_frame("temperatureOutside")
        df_gas_hourly = df_gas_hourly.join(df_temp, how="left")
        daily_temp = df_temp.groupby(df_temp.index.date)["temperatureOutside"].mean()
        df_gas_daily["avg_temperatureOutside"] = df_gas_daily.index.map(
            lambda d: daily_temp.get(d.date(), None)
        )
        has_temp = True
        missing = int(df_gas_daily["avg_temperatureOutside"].isna().sum())
        if missing:
            _LOGGER.info(
                "Gas period: %d of %d days without outdoor temperature",
                missing, len(df_gas_daily),
            )

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


async def _async_gas_from_history(
    hass: HomeAssistant, entity_id: str, start_dt, end_dt
) -> pd.Series | None:
    """Gasverbruik per uur uit de ruwe historie (meterstanden), of None.

    Alleen nog de terugval voor een gasmeter zonder statistiek; de ruwe
    historie reikt maar zo ver terug als ``purge_keep_days``.
    """
    def _fetch():
        return state_changes_during_period(hass, start_dt, end_dt, entity_id)

    states = await get_instance(hass).async_add_executor_job(_fetch)
    records = []
    for state in states.get(entity_id, []):
        try:
            records.append((state.last_changed.replace(tzinfo=None), float(state.state)))
        except (ValueError, TypeError):
            continue
    if len(records) < 2:
        return None
    meter = pd.Series(
        [v for _, v in records], index=pd.DatetimeIndex([t for t, _ in records])
    ).sort_index()
    steps = meter.diff().dropna()
    steps = steps[(steps >= 0) & (steps < MAX_GAS_STEP_M3)]
    return steps.resample("h").sum() if not steps.empty else None


async def _async_temp_from_history(
    hass: HomeAssistant, entity_id: str, start_dt, end_dt
) -> pd.DataFrame | None:
    """Buitentemperatuur per uur (mediaan) uit de ruwe historie, of None."""
    def _fetch():
        return state_changes_during_period(hass, start_dt, end_dt, entity_id)

    states = await get_instance(hass).async_add_executor_job(_fetch)
    records = []
    for s in states.get(entity_id, []):
        try:
            ts = s.last_changed
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            records.append({"timestamp": ts, "temperatureOutside": float(s.state)})
        except (ValueError, TypeError):
            continue
    if not records:
        return None
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.floor("h")
    return df.groupby("timestamp")["temperatureOutside"].median().to_frame()
