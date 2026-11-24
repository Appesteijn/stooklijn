"""Shared helpers for Quatt Stooklijn sensor and switch entities."""

from __future__ import annotations

from collections import deque
import math
from datetime import datetime

from .const import (
    DEFROST_HOLD_AFTER_MIN,
    DEFROST_HOLD_MAX_MIN,
    DEFROST_LOOKBACK_MIN,
    DEFROST_RECOVERED_K,
    DEFROST_TRANSIT_MIN,
    DOMAIN,
    FLOW_SMOOTHING_TAU_MIN,
    MIN_FLOW_LPH,
    NOMINAL_FLOW_LPH,
)


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
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    # float("nan") en float("inf") slagen gewoon. Een sensor die "nan"
    # doorgeeft, zette het stooklijnadvies op 55,0 °C met status "advies"
    # (min(55, nan) is 55) en het 1R1C blijvend op NaN.
    return value if math.isfinite(value) else None


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


class ReturnHold:
    """De retour waar een aanvoeradvies mee rekent, met ontdooiingen eruit.

    Een advies is ``retour + vraag / (1,16 · debiet)``. Tijdens en na een
    ontdooiing is de retour afgekoeld water dat de warmtepomp er zelf uit heeft
    gehaald, en dan zakte het advies mee terwijl het huis niets anders nodig
    had. Wie het advies volgt (de max-water-bijsturing leest het eens per half
    uur) kreeg dan een plafond dat net na het ontdooien knijpt.

    Daarom: bij een ontdooiing de hoogste retour van de minuten ervoor vasthouden
    (``DEFROST_LOOKBACK_MIN``), en rekenen met de hoogste van die waarde en de
    gemeten retour. Losgelaten als de retour hersteld is, als de laatste
    ontdooiing ``DEFROST_HOLD_AFTER_MIN`` voorbij is, of na
    ``DEFROST_HOLD_MAX_MIN``. Nooit lager dan gemeten: een retour die echt
    stijgt telt meteen mee.

    Eén per installatie, gedeeld door alle adviezen, zoals ``FlowSmoother``.
    Vaker aanroepen op hetzelfde moment verandert niets.
    """

    def __init__(
        self,
        lookback_min: float = DEFROST_LOOKBACK_MIN,
        after_min: float = DEFROST_HOLD_AFTER_MIN,
        max_min: float = DEFROST_HOLD_MAX_MIN,
        recovered_k: float = DEFROST_RECOVERED_K,
        transit_min: float = DEFROST_TRANSIT_MIN,
    ) -> None:
        self._lookback_s = lookback_min * 60.0
        self._transit_s = transit_min * 60.0
        self._after_s = after_min * 60.0
        self._max_s = max_min * 60.0
        self._recovered_k = recovered_k
        # Retourmetingen buiten een ontdooiing, nieuwste achteraan.
        self._recent: deque[tuple[datetime, float]] = deque()
        self._held: float | None = None
        self._since: datetime | None = None
        self._last_defrost: datetime | None = None

    @property
    def held(self) -> float | None:
        """De vastgehouden retour, of None als er niets wordt vastgehouden."""
        return self._held

    def update(self, t_return: float | None, defrosting: bool, now: datetime) -> None:
        """Verwerk de toestand van nu."""
        if defrosting:
            self._last_defrost = now
            if self._held is None:
                values = [v for _, v in self._recent]
                if t_return is not None:
                    values.append(t_return)
                if values:
                    self._held = max(values)
                    self._since = now
            return

        if self._held is not None:
            since_defrost = (now - self._last_defrost).total_seconds()
            # Het koude water moet nog rond: vlak na het einde staat de retour
            # nog op de oude waarde, en dat is geen herstel.
            recovered = (
                since_defrost >= self._transit_s
                and t_return is not None
                and t_return >= self._held - self._recovered_k
            )
            quiet = since_defrost >= self._after_s
            too_old = (now - self._since).total_seconds() >= self._max_s
            if not (recovered or quiet or too_old):
                return
            self._held = self._since = None
            # Wat er tijdens het vasthouden binnenkwam was afgekoeld water.
            self._recent.clear()

        if t_return is not None:
            self._recent.append((now, t_return))
        while self._recent and (now - self._recent[0][0]).total_seconds() > self._lookback_s:
            self._recent.popleft()

    def effective(self, t_return: float | None) -> float | None:
        """De retour voor het advies. None blijft None: geen meting is geen meting."""
        if t_return is None or self._held is None:
            return t_return
        return max(t_return, self._held)


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
