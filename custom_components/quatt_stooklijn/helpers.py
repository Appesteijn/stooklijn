"""Shared helpers for Quatt Stooklijn sensor and switch entities."""

from __future__ import annotations

import math
from datetime import datetime

from .const import DOMAIN, FLOW_SMOOTHING_TAU_MIN, MIN_FLOW_LPH, NOMINAL_FLOW_LPH


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


class FlowSmoother:
    """Gladgestreken debiet voor de aanvoeradviezen, één per installatie.

    De adviezen rekenen aanvoer = retour + vraag / (1,16 × debiet). Na elke
    pompstart loopt het debiet een paar minuten op vanaf ~200 l/h, en met dat
    momentane getal werd het temperatuurverschil vier keer te groot. Wat telt is
    het debiet waarmee de pomp normaal draait.

    Daarom een tijdgebaseerd gemiddelde (``FLOW_SMOOTHING_TAU_MIN``) dat alleen
    meetelt zolang de pomp draait, en als effectief debiet het hoogste van het
    momentane en dat gemiddelde. Een vaste vervangwaarde (bijvoorbeeld 800 l/h
    onder een grens) zou een installatie die normaal op 500 l/h draait
    structureel verkeerd doorrekenen; het gemiddelde past zich aan elk systeem
    aan. Het hoogste van de twee, zodat een debiet dat stijgt meteen meetelt.
    """

    def __init__(self, tau_minutes: float = FLOW_SMOOTHING_TAU_MIN) -> None:
        self._tau_s = tau_minutes * 60.0
        self._average: float | None = None
        # Laatste draaiende meting: die geldt tot de volgende (nulde-orde-houd).
        self._prev_flow: float | None = None
        self._prev_at: datetime | None = None

    @property
    def average(self) -> float | None:
        """Het gemiddelde debiet tijdens draaien, of None als er nog niets is."""
        return self._average

    def update(self, flow_lph: float | None, now: datetime) -> None:
        """Verwerk een meting. Vaker aanroepen met dezelfde waarde is onschadelijk."""
        if self._prev_flow is not None and self._prev_at is not None:
            dt = (now - self._prev_at).total_seconds()
            if dt > 0 and self._average is not None:
                alpha = 1.0 - math.exp(-dt / self._tau_s)
                self._average += alpha * (self._prev_flow - self._average)
        if flow_lph is not None and flow_lph >= MIN_FLOW_LPH:
            if self._average is None:
                self._average = float(flow_lph)
            self._prev_flow, self._prev_at = float(flow_lph), now
        else:
            # Stilstand telt niet mee: na de herstart begint het tellen opnieuw
            # vanaf de eerste draaiende meting, niet vanaf de laatste van vóór.
            self._prev_flow = self._prev_at = None

    def effective(self, flow_lph: float | None) -> float:
        """Het debiet waarmee een advies hoort te rekenen."""
        if flow_lph is not None and flow_lph >= MIN_FLOW_LPH:
            if self._average is None:
                return float(flow_lph)
            return max(float(flow_lph), self._average)
        # Pomp staat stil: het normale debiet van déze installatie, en pas als
        # dat nog onbekend is het nominale.
        return self._average if self._average is not None else NOMINAL_FLOW_LPH


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
