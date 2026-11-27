"""Quatt heat pump data fetching and processing.

Uses a hybrid approach:
- Recorder long-term statistics for historical daily data (months of history)
- Cached hourly data for any previously fetched days (beyond the API window)
- Quatt get_cic_insights API for recent hourly data (last 30 days, cached)
  (falls back to get_insights for Quatt integration ≤1.0.2)
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
    CONF_BOILER_HEAT_ENTITY,
    CONF_POWER_ENTITY,
    CONF_POWER_INPUT_ENTITY,
    CONF_TEMP_ENTITIES,
    STATS_UNITS,
)
from ..discovery import (
    ROLE_BOILER_HEAT,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_TOTAL_POWER,
    _async_openquatt_node,
    async_discover_quatt_entities,
    async_resolve_entity,
)
from .utils import MODE_HEATING, classify_heat_mode

_LOGGER = logging.getLogger(__name__)

#: OpenQuatt-entiteiten (firmwarenaam) voor de dagtotalen. Op naam, niet op
#: entity-ID: zie ``OPENQUATT_NAMES`` in discovery.py.
OPENQUATT_HEAT_NAME = "Total Heat Power"
OPENQUATT_ELECTRIC_NAME = "Total Power Input"
#: Zoveel uren moet een dag minstens hebben voordat OpenQuatt hem overneemt.
#: Een dag met gaten (installatie, storing) houdt de Quatt-waarden; 22 laat de
#: zomertijdwisseling (23 uur) door.
OPENQUATT_MIN_HOURS_PER_DAY = 22

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
    power_input_entity: str | None = None,
    boiler_heat_entity: str | None = None,
) -> pd.DataFrame:
    """Fetch daily mean statistics from HA recorder.

    Returns a DataFrame with the same columns as the API-based daily data,
    using recorder long-term statistics (available for months of history).
    """
    # HA telt een dag van lokale middernacht tot lokale middernacht. Een
    # venster op UTC-middernacht miste het eerste uur van de eerste dag.
    tz = dt_util.get_default_time_zone()
    start_dt = dt_util.as_utc(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz))
    end_dt = dt_util.as_utc(
        (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)
    )

    # Entity-IDs verschillen per installatie (zie discovery.py); niet hardcoden.
    # De al bekende entiteiten verankeren de detectie aan hun eigen CiC.
    anchor = {CONF_POWER_ENTITY: power_entity, CONF_TEMP_ENTITIES: [temp_entity]}
    if power_input_entity is None:
        power_input_entity = async_resolve_entity(hass, anchor, None, ROLE_POWER_INPUT)
    if boiler_heat_entity is None:
        boiler_heat_entity = async_resolve_entity(hass, anchor, None, ROLE_BOILER_HEAT)

    statistic_ids = {
        sid
        for sid in (power_entity, temp_entity, power_input_entity, boiler_heat_entity)
        if sid
    }

    def _fetch_stats() -> dict:
        return statistics_during_period(
            hass,
            start_time=start_dt,
            end_time=end_dt,
            statistic_ids=statistic_ids,
            period="day",
            units=STATS_UNITS,
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
            # De lokale datum: een dag die om 00:00 lokaal begint, begint in
            # UTC de avond ervoor (23:00 in de winter). Met de UTC-datum kwam
            # elke dag één dag te vroeg binnen, en "vandaag" als gisteren.
            day = dt_util.as_local(dt_util.utc_from_timestamp(start_ts)).strftime("%Y-%m-%d")

            if day not in records:
                records[day] = {}

            mean_val = row.get("mean")
            if mean_val is None:
                continue

            if sensor_id == power_entity:
                # Mean W * 24h = daily Wh
                records[day]["totalHpHeat"] = mean_val * 24
            elif sensor_id == power_input_entity:
                records[day]["totalHpElectric"] = mean_val * 24
            elif sensor_id == boiler_heat_entity:
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
    df["totalBoilerGas"] = 0.0  # Not available from recorder

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


def openquatt_daily_from_hourly(
    heat_rows: list[dict], electric_rows: list[dict]
) -> pd.DataFrame:
    """Dagtotalen (Wh) uit OpenQuatt-uurgemiddelden, per lokale dag.

    Alleen uren waarin beide een gemiddelde hebben, en alleen dagen met
    minstens ``OPENQUATT_MIN_HOURS_PER_DAY`` zulke uren. Totaal = gemiddeld
    vermogen over die uren × 24, zoals de recorder-tak ook rekent.
    """
    def _by_hour(rows):
        out = {}
        for row in rows or []:
            start, mean = row.get("start"), row.get("mean")
            if start is None or mean is None:
                continue
            if not isinstance(start, (int, float)):
                start = start.timestamp()
            out[float(start)] = float(mean)
        return out

    heat, elec = _by_hour(heat_rows), _by_hour(electric_rows)
    days: dict[str, list[tuple[float, float]]] = {}
    for start in heat.keys() & elec.keys():
        day = dt_util.as_local(dt_util.utc_from_timestamp(start)).strftime("%Y-%m-%d")
        days.setdefault(day, []).append((heat[start], elec[start]))
    records = {}
    for day, pairs in days.items():
        if len(pairs) < OPENQUATT_MIN_HOURS_PER_DAY:
            continue
        records[day] = {
            "totalHpHeat": sum(h for h, _ in pairs) / len(pairs) * 24,
            "totalHpElectric": sum(e for _, e in pairs) / len(pairs) * 24,
        }
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(records, orient="index")
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df.sort_index()


def apply_openquatt_days(df_daily: pd.DataFrame, df_oq: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Warmte en stroom van OpenQuatt over de stookdagen leggen; COP opnieuw.

    Wat eronder ligt is Quatt: de cloud-cache, of de recorder. De recorder-tak
    kiest zijn sensoren op bestaan (``async_resolve_entity``) en neemt dus de
    CiC-sensoren van de Quatt-integratie, ook als het bronnenregister live
    OpenQuatt gebruikt. Per dag overleggen werkt voor beide, en laat dagen van
    vóór OpenQuatt gewoon staan.

    Waarom: de stroom van Quatt (cloud én CiC) is een schatting die op het
    testhuis 6–16 % te hoog zat. Tegen de slimme meter, 135 nachturen in
    november 2026: OpenQuatt helling 0,997 met een basislast die klopt met de
    nachten waarin de pompen uit stonden (357 tegen 354–365 W), Quatt 0,837.
    De warmte was gelijk. Een COP uit de cloud viel daardoor structureel te
    laag uit naast die van OpenQuatt.

    Alleen stookdagen: een koeldag heeft bij OpenQuatt warmte 0 (koelen telt
    apart) en zou anders stilstand worden. Geeft ``(df, aantal dagen)``.
    """
    if df_daily.empty or df_oq.empty or "totalHeatPerHour" not in df_daily.columns:
        return df_daily, 0
    heating = classify_heat_mode(df_daily["totalHeatPerHour"]) == MODE_HEATING
    days = df_daily.index[heating].intersection(df_oq.index)
    days = days[(df_oq.loc[days, "totalHpHeat"] > 0).to_numpy()
                & (df_oq.loc[days, "totalHpElectric"] > 0).to_numpy()]
    if len(days) == 0:
        return df_daily, 0
    df = df_daily.copy()
    for col in ("totalHpHeat", "totalHpElectric"):
        if col not in df.columns:
            df[col] = float("nan")
        df.loc[days, col] = df_oq.loc[days, col]
    boiler = df.loc[days, "totalBoilerHeat"].fillna(0) if "totalBoilerHeat" in df.columns else 0
    df.loc[days, "totalHeatPerHour"] = (df.loc[days, "totalHpHeat"] + boiler) / 24
    df.loc[days, "averageCOP"] = df.loc[days, "totalHpHeat"] / df.loc[days, "totalHpElectric"]
    return df, len(days)


async def _async_fetch_openquatt_daily(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
    configured_heat: str | None,
    configured_electric: str | None,
) -> pd.DataFrame:
    """Dagtotalen van OpenQuatt uit de uurstatistiek, of leeg.

    Leeg zonder OpenQuatt, en als de gebruiker warmte of stroom op een andere
    sensor heeft gezet: wat ingesteld is gaat voor, ook voor OpenQuatt.
    """
    node = _async_openquatt_node(hass)
    heat, elec = node.get(OPENQUATT_HEAT_NAME), node.get(OPENQUATT_ELECTRIC_NAME)
    if not heat or not elec:
        return pd.DataFrame()
    if (configured_heat and configured_heat != heat) or (
        configured_electric and configured_electric != elec
    ):
        return pd.DataFrame()

    tz = dt_util.get_default_time_zone()
    start_dt = dt_util.as_utc(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz))
    end_dt = dt_util.as_utc(
        (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)
    )

    def _fetch() -> dict:
        return statistics_during_period(
            hass, start_time=start_dt, end_time=end_dt,
            statistic_ids={heat, elec}, period="hour", units=None, types={"mean"},
        )

    stats = await get_instance(hass).async_add_executor_job(_fetch)
    return openquatt_daily_from_hourly((stats or {}).get(heat, []), (stats or {}).get(elec, []))


async def _async_fetch_api_days(
    hass: HomeAssistant,
    start_dt: datetime,
    end_dt: datetime,
    cache: QuattInsightsCache,
    *,
    cache_only: bool = False,
) -> tuple[list[pd.DataFrame], list[dict], int, int]:
    """Fetch daily/hourly data from Quatt API for a date range, using cache.

    Args:
        cache_only: If True, only return cached data (no API calls).
                    Used to retrieve historical hourly data beyond the API window.
    """
    all_dates = pd.date_range(start=start_dt, end=end_dt)

    hourly_chunks = []
    daily_records = []
    api_calls_made = 0
    cache_hits = 0

    # Quatt ≥2.0 renamed get_insights → get_cic_insights; fall back for older installs.
    insights_service = (
        "get_cic_insights"
        if hass.services.has_service("quatt", "get_cic_insights")
        else "get_insights"
    )

    for current_date in all_dates:
        date_str = current_date.strftime("%Y-%m-%d")
        data = None

        # Check cache first
        cached_data = cache.get(date_str)
        if cached_data is not None:
            data = cached_data
            cache_hits += 1
        elif not cache_only:
            # Fetch from API (only when not in cache-only mode)
            try:
                response = await hass.services.async_call(
                    "quatt",
                    insights_service,
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


async def async_get_cache_stats(hass: HomeAssistant) -> dict:
    """Return current QuattInsightsCache statistics without triggering a load."""
    if _cache is None:
        return {"total_days": 0, "oldest_date": None, "newest_date": None}
    return _cache.get_stats()


async def async_fetch_quatt_insights(
    hass: HomeAssistant,
    start_date: str,
    end_date: str,
    power_entity: str | None = None,
    temp_entity: str | None = None,
    power_input_entity: str | None = None,
    boiler_heat_entity: str | None = None,
    use_cloud: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch Quatt data using a hybrid approach.

    1. Recorder statistics for the full configured period (daily means)
    2. Cached hourly data for historical period (before API window, cache-only)
    3. Quatt API for the last API_FETCH_DAYS days (hourly detail, with API fallback)
    4. Merge: API daily data overwrites recorder data where available
    """
    cache = await _get_cache(hass)
    # Wat de gebruiker zelf instelde, vóór de detectie het invult (stap 7).
    configured_power, configured_power_input = power_entity, power_input_entity

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # === First-run guard: limit initial API calls ===
    # On first run (empty cache), fetching months of daily data would make one
    # API call per day (e.g. 251 calls for 251-day range).  Cap the API window
    # to the last API_FETCH_DAYS so the first run makes at most 30 calls.
    # The cache grows organically on subsequent runs; recorder statistics cover
    # the full configured period regardless.
    cache_stats = cache.get_stats()
    if cache_stats["total_days"] == 0:
        earliest_allowed = end_dt - timedelta(days=API_FETCH_DAYS - 1)
        if start_dt < earliest_allowed:
            _LOGGER.info(
                "First run (empty cache): limiting API fetch to last %d days "
                "(configured range starts %s — full history available via recorder)",
                API_FETCH_DAYS,
                start_date,
            )
            start_dt = earliest_allowed

    # === Step 1: Recorder statistics for full history ===
    _LOGGER.info("Fetching recorder statistics for %s to %s...", start_date, end_date)
    # Entity-IDs verschillen per installatie (zie discovery.py); niet hardcoden.
    # Wat de entry al heeft ingesteld verankert de detectie aan haar eigen CiC;
    # met twee Quatt-installaties in één HA kiest die anders vrij een hub.
    anchor = {
        CONF_POWER_ENTITY: power_entity,
        CONF_TEMP_ENTITIES: [temp_entity],
        CONF_POWER_INPUT_ENTITY: power_input_entity,
        CONF_BOILER_HEAT_ENTITY: boiler_heat_entity,
    }
    discovered = async_discover_quatt_entities(hass)
    power_entity = power_entity or async_resolve_entity(
        hass, anchor, None, ROLE_TOTAL_POWER, discovered=discovered
    )
    temp_entity = temp_entity or async_resolve_entity(
        hass, anchor, None, ROLE_OUTDOOR_TEMP, discovered=discovered
    )

    df_daily_recorder = await _async_fetch_recorder_daily(
        hass,
        start_date,
        end_date,
        power_entity,
        temp_entity,
        # Ingesteld gaat vóór detectie, net als bij power/temp hierboven.
        # Zonder deze voorrang leest de analyse alsnog de Quatt-sensor terwijl
        # de gebruiker de rol op een andere bron heeft gezet.
        power_input_entity
        or async_resolve_entity(hass, anchor, None, ROLE_POWER_INPUT, discovered=discovered),
        boiler_heat_entity
        or async_resolve_entity(hass, anchor, None, ROLE_BOILER_HEAT, discovered=discovered),
    )

    # === Step 2: Cached hourly data for historical period (before API window) ===
    api_start = max(start_dt, end_dt - timedelta(days=API_FETCH_DAYS - 1))
    history_end = api_start - timedelta(days=1)

    hourly_chunks = []
    daily_records = []
    total_cache_hits = 0

    if history_end >= start_dt:
        _LOGGER.info(
            "Checking cache for historical hourly data %s to %s...",
            start_date,
            history_end.strftime("%Y-%m-%d"),
        )
        hist_hourly, hist_daily, _, hist_cache_hits = await _async_fetch_api_days(
            hass, start_dt, history_end, cache, cache_only=True
        )
        hourly_chunks.extend(hist_hourly)
        daily_records.extend(hist_daily)
        total_cache_hits += hist_cache_hits

        if hist_cache_hits > 0:
            _LOGGER.info(
                "Found %d days of cached historical hourly data", hist_cache_hits
            )

    # === Step 3: Quatt API for last N days (hourly detail) ===
    #
    # Staat de cloud uit, dan wordt dit venster op dezelfde manier behandeld als
    # het historische venster hierboven: alleen uit de cache lezen. De reeds
    # opgebouwde dagen blijven dus meedoen; er komen alleen geen nieuwe bij.
    # De recorder-tak (stap 1) vult die rol en levert dezelfde kolommen.
    api_start_str = api_start.strftime("%Y-%m-%d")

    if use_cloud:
        _LOGGER.info(
            "Fetching Quatt API data for %s to %s (%d days)...",
            api_start_str,
            end_date,
            (end_dt - api_start).days + 1,
        )
    else:
        _LOGGER.info(
            "Quatt cloud disabled — reading %s to %s from cache only; "
            "new days come from the recorder",
            api_start_str,
            end_date,
        )

    api_hourly, api_daily, api_calls_made, api_cache_hits = (
        await _async_fetch_api_days(
            hass, api_start, end_dt, cache, cache_only=not use_cloud
        )
    )
    hourly_chunks.extend(api_hourly)
    daily_records.extend(api_daily)
    total_cache_hits += api_cache_hits

    # Save cache if we made any API calls
    if api_calls_made > 0:
        await cache.async_save()

    _LOGGER.info(
        "API/cache data: %d days total (%d from cache, %d from API)",
        total_cache_hits + api_calls_made,
        total_cache_hits,
        api_calls_made,
    )

    # === Step 4: Build hourly DataFrame ===
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

    # === Step 5: Build API daily DataFrame ===
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
            electric = df_daily_api["totalHpElectric"].replace(0, float("nan"))
            df_daily_api["averageCOP"] = df_daily_api["totalHpHeat"] / electric
            df_daily_api["averageCOP"] = df_daily_api["averageCOP"].replace(
                [float("inf"), -float("inf")], float("nan")
            )

    # === Step 6: Merge — recorder as base, API overwrites recent days ===
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

    # === Step 7: OpenQuatt gaat voor op Quatt (cloud-cache en CiC-sensoren) ===
    # Een fout hier mag de rest van de analyse niet meenemen.
    try:
        df_oq = await _async_fetch_openquatt_daily(
            hass, start_date, end_date, configured_power, configured_power_input
        )
        df_daily, n_oq = apply_openquatt_days(df_daily, df_oq)
        if n_oq:
            _LOGGER.info("COP: %d stookdagen met warmte en stroom van OpenQuatt", n_oq)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("OpenQuatt-dagtotalen ophalen mislukt; Quatt-waarden blijven", exc_info=True)

    # Label each day heating/cooling/idle from net heat delivery, so cooling
    # days stay identifiable downstream instead of being silently dropped by
    # the heating-only filters. Computed once on the merged frame so it always
    # matches the final totalHeatPerHour.
    if not df_daily.empty and "totalHeatPerHour" in df_daily.columns:
        df_daily["mode"] = classify_heat_mode(df_daily["totalHeatPerHour"])

    return df_hourly, df_daily
