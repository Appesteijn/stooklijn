"""Data coordinator for Quatt Stooklijn integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .analysis.gas import async_fetch_gas_data
from .analysis.heat_loss import HeatLossResult, calculate_heat_loss
from .analysis.quatt import async_fetch_quatt_insights
from .analysis.stooklijn import (
    StooklijnResult,
    async_fetch_live_history,
    calculate_stooklijn,
)
from .const import (
    CONF_ACTUAL_STOOKLIJN_POWER1,
    CONF_ACTUAL_STOOKLIJN_POWER2,
    CONF_ACTUAL_STOOKLIJN_TEMP1,
    CONF_ACTUAL_STOOKLIJN_TEMP2,
    CONF_BOILER_EFFICIENCY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_ENABLED,
    CONF_GAS_END_DATE,
    CONF_GAS_ENTITY,
    CONF_GAS_START_DATE,
    CONF_HOT_WATER_TEMP_THRESHOLD,
    CONF_POWER_ENTITY,
    CONF_QUATT_END_DATE,
    CONF_QUATT_START_DATE,
    CONF_TEMP_ENTITIES,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HOT_WATER_TEMP_THRESHOLD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _calc_stooklijn_from_points(config: dict) -> tuple[float | None, float | None]:
    """Calculate slope and intercept from two config points, if all are set."""
    t1 = config.get(CONF_ACTUAL_STOOKLIJN_TEMP1)
    p1 = config.get(CONF_ACTUAL_STOOKLIJN_POWER1)
    t2 = config.get(CONF_ACTUAL_STOOKLIJN_TEMP2)
    p2 = config.get(CONF_ACTUAL_STOOKLIJN_POWER2)
    if any(v is None for v in (t1, p1, t2, p2)):
        return None, None
    if t1 == t2:
        return None, None
    slope = (p2 - p1) / (t2 - t1)
    intercept = p1 - slope * t1
    return slope, intercept


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

    # Actual Quatt stooklijn settings (calculated from config points)
    actual_stooklijn_slope: float | None = None
    actual_stooklijn_intercept: float | None = None


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
        slope, intercept = _calc_stooklijn_from_points(config)
        self.data = QuattStooklijnData(
            actual_stooklijn_slope=slope,
            actual_stooklijn_intercept=intercept,
        )

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
            config[CONF_QUATT_END_DATE],
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

        # Step 4: Run stooklijn analysis (CPU-heavy, run in executor)
        _LOGGER.info("Running stooklijn calculations...")
        stooklijn_result = await self.hass.async_add_executor_job(
            calculate_stooklijn,
            df_ha_merged,
            df_hourly if not df_hourly.empty else None,
            df_daily if not df_daily.empty else None,
        )

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

        # Assemble results
        self.data = QuattStooklijnData(
            stooklijn=stooklijn_result,
            heat_loss_hp=heat_loss_hp,
            heat_loss_gas=heat_loss_gas,
            average_cop=average_cop,
            last_analysis=datetime.now(timezone.utc),
            analysis_status="completed",
            actual_stooklijn_slope=_calc_stooklijn_from_points(config)[0],
            actual_stooklijn_intercept=_calc_stooklijn_from_points(config)[1],
        )

        _LOGGER.info("Quatt Stooklijn analysis completed")
        return self.data
