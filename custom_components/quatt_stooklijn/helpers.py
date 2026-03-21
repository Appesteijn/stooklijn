"""Shared helpers for Quatt Stooklijn sensor and switch entities."""

from __future__ import annotations

from .const import DOMAIN, MIN_FLOW_LPH, NOMINAL_FLOW_LPH


def get_device_info(entry_id: str) -> dict:
    """Standard device info dict for all Quatt entities."""
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "Quatt Warmteanalyse",
        "manufacturer": "Quatt",
        "model": "Warmteanalyse",
    }


def get_float_state(hass, entity_id: str) -> float | None:
    """Read a float value from a HA entity state."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", "None", ""):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def get_effective_flow(flow_lph: float | None) -> float:
    """Return flow rate with fallback to nominal when HP is off or unavailable."""
    if flow_lph is not None and flow_lph >= MIN_FLOW_LPH:
        return flow_lph
    return NOMINAL_FLOW_LPH
