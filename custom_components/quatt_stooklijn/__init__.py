"""Quatt Stooklijn integration for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, SERVICE_CLEAR_DATA, SERVICE_RUN_ANALYSIS
from .coordinator import (
    QuattStooklijnCoordinator,
    QuattStooklijnData,
    _calc_stooklijn_from_points,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "text"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Quatt Stooklijn from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = QuattStooklijnCoordinator(hass, dict(entry.data))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register service (once for the domain)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_ANALYSIS):

        async def handle_run_analysis(call: ServiceCall) -> None:
            """Handle the run_analysis service call."""
            # Run analysis for all configured entries
            for coord in hass.data[DOMAIN].values():
                if isinstance(coord, QuattStooklijnCoordinator):
                    _LOGGER.info("Triggering Quatt Stooklijn analysis")
                    # Set status to "running" and notify sensors immediately
                    coord.data.analysis_status = "running"
                    coord.async_set_updated_data(coord.data)
                    try:
                        await coord.async_refresh()
                    except Exception:
                        coord.data.analysis_status = "error"
                        coord.async_set_updated_data(coord.data)
                        raise

        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_ANALYSIS,
            handle_run_analysis,
            schema=vol.Schema({}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_DATA):

        async def handle_clear_data(call: ServiceCall) -> None:
            """Handle the clear_data service call."""
            for coord in hass.data[DOMAIN].values():
                if isinstance(coord, QuattStooklijnCoordinator):
                    _LOGGER.info("Clearing Quatt Stooklijn analysis data")
                    slope, intercept = _calc_stooklijn_from_points(coord.config)
                    coord.data = QuattStooklijnData(
                        actual_stooklijn_slope=slope,
                        actual_stooklijn_intercept=intercept,
                    )
                    coord.async_set_updated_data(coord.data)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_DATA,
            handle_clear_data,
            schema=vol.Schema({}),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove service if no entries left
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_RUN_ANALYSIS)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_DATA)

    return unload_ok
