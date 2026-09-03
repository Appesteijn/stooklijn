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


def resolve_own_entity_id(
    hass, platform: str, entry_id: str, unique_suffix: str
) -> str | None:
    """Zoek de entity-ID van een eigen entiteit op via de entity registry.

    Een hardcoded entity-ID breekt zodra de gebruiker de entiteit hernoemt of
    een tweede config-entry aanmaakt; de unique_id blijft wel stabiel. Geeft
    None terug als de entiteit (nog) niet bestaat — bijvoorbeeld omdat de
    bijbehorende optie uitstaat.
    """
    from homeassistant.helpers import entity_registry as er

    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry_id}_{unique_suffix}"
    )
