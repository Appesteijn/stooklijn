"""Data coordinator for Quatt Stooklijn integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .analysis.gas import async_fetch_gas_data
from .analysis.heat_loss import HeatLossResult, calculate_heat_loss
from .analysis.quatt import async_fetch_quatt_insights, async_get_cache_stats
from .analysis.stooklijn import (
    StooklijnResult,
    async_fetch_live_history,
    calculate_stooklijn,
    extract_knee_points_from_hourly,
    extract_knee_points_from_recorder,
)
from .cache import KneeDataStore
from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_ENABLED,
    CONF_GAS_END_DATE,
    CONF_GAS_ENTITY,
    CONF_GAS_START_DATE,
    CONF_HOT_WATER_TEMP_THRESHOLD,
    CONF_POWER_ENTITY,
    CONF_QUATT_START_DATE,
    CONF_TEMP_ENTITIES,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HOT_WATER_TEMP_THRESHOLD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class QuattStooklijnData:
    """Container for all analysis results."""

    stooklijn: StooklijnResult = field(default_factory=StooklijnResult)
    heat_loss_hp: HeatLossResult = field(default_factory=HeatLossResult)
    heat_loss_gas: HeatLossResult = field(default_factory=HeatLossResult)
    average_cop: float | None = None
    last_analysis: datetime | None = None
    analysis_status: str = "idle"

    # Period scatter data for time-split comparison

    # Data availability stats (populated after each analysis run)
    data_stats: dict = field(default_factory=dict)


class QuattStooklijnCoordinator(DataUpdateCoordinator[QuattStooklijnData]):
    """Coordinator that runs the analysis on demand."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # On-demand only
        )
        self.config = config
        self._knee_store = KneeDataStore(hass)
        self.data = QuattStooklijnData()

    async def _async_update_data(self) -> QuattStooklijnData:
        """Run the full analysis pipeline."""
        _LOGGER.info("Starting Quatt Stooklijn analysis")

        try:
            return await self._run_analysis()
        except Exception:
            self.data.analysis_status = "error"
            _LOGGER.exception("Quatt Stooklijn analysis failed")
            raise

    async def _run_analysis(self) -> QuattStooklijnData:
        """Execute the analysis steps."""
        config = self.config

        # Step 1: Fetch Quatt insights data (hybrid: recorder + API)
        _LOGGER.info("Fetching Quatt insights data...")
        temp_entities = config.get(CONF_TEMP_ENTITIES, [])
        df_hourly, df_daily = await async_fetch_quatt_insights(
            self.hass,
            config[CONF_QUATT_START_DATE],
            date.today().isoformat(),
            power_entity=config.get(CONF_POWER_ENTITY, "sensor.heatpump_total_power"),
            temp_entity=temp_entities[0] if temp_entities else "sensor.heatpump_hp1_temperature_outside",
        )

        # Step 2: Fetch gas data (if enabled)
        df_gas_daily = None
        if config.get(CONF_GAS_ENABLED):
            _LOGGER.info("Fetching gas consumption data...")
            _, df_gas_daily = await async_fetch_gas_data(
                self.hass,
                config[CONF_GAS_ENTITY],
                config[CONF_GAS_START_DATE],
                config[CONF_GAS_END_DATE],
                config.get(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE),
                config.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY),
                config.get(
                    CONF_HOT_WATER_TEMP_THRESHOLD, DEFAULT_HOT_WATER_TEMP_THRESHOLD
                ),
                df_hourly if not df_hourly.empty else None,
                temp_entities=config.get(CONF_TEMP_ENTITIES),
            )

        # Step 3: Fetch live history for knee detection
        _LOGGER.info("Fetching live history for stooklijn analysis...")
        df_ha_merged = await async_fetch_live_history(
            self.hass,
            config[CONF_TEMP_ENTITIES],
            config[CONF_POWER_ENTITY],
        )

        # Step 3b: Update persistent knee data store
        # Primary: extract recorder minute-data (defrost-filtered, accurate).
        # Backfill: also add cold-weather hourly points from the API cache for
        # historical dates not yet in the store (e.g. cold spells from before
        # the integration was installed that were never in the recorder window).
        await self._knee_store.async_load()
        saved = False
        if df_ha_merged is not None and not df_ha_merged.empty:
            new_days = extract_knee_points_from_recorder(df_ha_merged)
            added = self._knee_store.merge_days(new_days)
            if added > 0:
                _LOGGER.info("Knee data store: added %d new days from recorder", added)
                saved = True

        if not df_hourly.empty:
            hourly_days = await self.hass.async_add_executor_job(
                extract_knee_points_from_hourly, df_hourly
            )
            added_hourly = self._knee_store.merge_days(hourly_days)
            if added_hourly > 0:
                _LOGGER.info(
                    "Knee data store: backfilled %d days from API cache", added_hourly
                )
                saved = True

        if saved:
            await self._knee_store.async_save()
        else:
            _LOGGER.debug("Knee data store: no new days to add")

        stats = self._knee_store.get_stats()
        _LOGGER.info(
            "Knee data store: %d days, %d hourly points (oldest: %s)",
            stats["total_days"],
            stats["total_points"],
            stats["oldest_date"],
        )
        df_knee_history = self._knee_store.get_as_dataframe()

        # Step 4: Run stooklijn analysis (CPU-heavy, run in executor)
        _LOGGER.info("Running stooklijn calculations...")
        stooklijn_result = await self.hass.async_add_executor_job(
            calculate_stooklijn,
            df_ha_merged,
            df_hourly if not df_hourly.empty else None,
            df_daily if not df_daily.empty else None,
            df_knee_history,
        )

        # Knee freeze: the knee temperature is a physical property of the heat
        # pump (max capacity limit) and can only move to colder values as more
        # cold-weather data is collected.  In spring/summer the mild-weather data
        # causes the grid-search to find a spurious "elbow" at warm temperatures
        # (e.g. 3.5°C instead of -1.8°C).  We persist the coldest reliable knee
        # ever seen and reject any warmer detection beyond a 0.5°C noise margin.
        if stooklijn_result.knee_temperature is not None:
            updated = self._knee_store.update_best_knee(stooklijn_result.knee_temperature)
            if updated:
                await self._knee_store.async_save()
                _LOGGER.info(
                    "Knee store: new best knee %.2f°C saved",
                    stooklijn_result.knee_temperature,
                )
            else:
                best = self._knee_store.best_knee_temp
                if best is not None and stooklijn_result.knee_temperature > best + 0.5:
                    _LOGGER.info(
                        "Knee freeze: detected %.2f°C > best %.2f°C + 0.5 margin — "
                        "keeping %.2f°C",
                        stooklijn_result.knee_temperature,
                        best,
                        best,
                    )
                    stooklijn_result.knee_temperature = best

        # Step 5: Heat loss analysis
        _LOGGER.info("Running heat loss analysis...")
        heat_loss_hp = await self.hass.async_add_executor_job(
            calculate_heat_loss,
            df_daily if not df_daily.empty else None,
            "heat_pump",
        )

        heat_loss_gas = HeatLossResult()
        if df_gas_daily is not None and not df_gas_daily.empty:
            heat_loss_gas = await self.hass.async_add_executor_job(
                calculate_heat_loss,
                df_gas_daily,
                "gas",
            )

        # Step 6: Calculate average COP (only heating days)
        average_cop = None
        if not df_daily.empty and "averageCOP" in df_daily.columns:
            # Filter: only days with meaningful heating and valid COP
            cop_mask = (
                df_daily["averageCOP"].replace([float("inf"), -float("inf")], None).notna()
                & (df_daily.get("totalHeatPerHour", pd.Series(0)) >= 200)
                & (df_daily["averageCOP"] > 0)
            )
            cop_valid = df_daily.loc[cop_mask, "averageCOP"]
            if len(cop_valid) > 0:
                average_cop = float(cop_valid.mean())

        # Collect data availability stats
        cache_stats = await async_get_cache_stats(self.hass)
        knee_stats = self._knee_store.get_stats()
        computed_data_stats = {
            "daily_days": len(df_daily) if not df_daily.empty else 0,
            "hourly_hours": len(df_hourly) if not df_hourly.empty else 0,
            "minute_minutes": len(df_ha_merged) if df_ha_merged is not None and not df_ha_merged.empty else 0,
            "cache_days": cache_stats["total_days"],
            "cache_oldest": cache_stats["oldest_date"],
            "cache_newest": cache_stats["newest_date"],
            "knee_store_days": knee_stats["total_days"],
            "knee_store_points": knee_stats["total_points"],
            "knee_store_oldest": knee_stats["oldest_date"],
            "knee_store_newest": knee_stats["newest_date"],
        }

        # Assemble results
        self.data = QuattStooklijnData(
            stooklijn=stooklijn_result,
            heat_loss_hp=heat_loss_hp,
            heat_loss_gas=heat_loss_gas,
            average_cop=average_cop,
            last_analysis=datetime.now(timezone.utc),
            analysis_status="completed",
            data_stats=computed_data_stats,
        )

        _LOGGER.info("Quatt Stooklijn analysis completed")
        return self.data
