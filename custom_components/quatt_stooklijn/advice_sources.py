"""Welk model een knop aanstuurt: de gedeelde keuze voor alles wat de Quatt beïnvloedt.

Elke knop die iets naar de warmtepomp schrijft (het max-water-plafond, de
geluidsniveaucompensatie) volgt een aanvoeradvies. Welk advies, kiest de
gebruiker per knop uit deze lijst. Eén lijst, zodat een knop er niet stilletjes
een eigen bron bij verzint en de opties overal dezelfde woorden gebruiken.

De bronnen:

- ``stooklijn``: warmteverlieslijn uit de stookdagen, alleen de
  buitentemperatuur. Weet niet of de kamer op temperatuur is.
- ``mpc``: dezelfde lijn plus een kamercorrectie (10·U·(setpoint − kamer)),
  begrensd op wat de warmtepomp kan. De naam is historisch: tot v0.10.6-beta.6
  rekende dit advies met het lerende huismodel (1R1C), sindsdien niet meer.
  De waarde blijft ``mpc`` zodat bestaande instellingen blijven werken.
- ``1r1c``: de basis uit het lerende huismodel (geleerde U, zon van nu,
  constante warmtewinst, setpoint) met dezelfde kamercorrectie en
  capaciteitsgrens (``rc_advice_demand``). Zolang het model niet online is,
  geeft de sensor het MPC-advies door, met ``terugval: true``. Toegevoegd in
  v0.10.7-beta.5 om op meer huizen te meten of het 1R1C beter stuurt; op één
  huis (november 2026) deed de rechte lijn het beter, met een 1R1C dat toen
  nog een fout in P_c had.

Het 2R2C als bron bestaat (nog) niet: daar hoort eerst een eigen aanvoeradvies
bij. Een nieuwe bron toevoegen is één regel in ``ADVICE_SOURCES`` plus de
sensor die het advies publiceert, met ``advies_status`` als attribuut.

Gelezen wordt via de unique_id van de adviessensor, niet via een vaste
entity-ID: die breekt bij een hernoemde entiteit of een tweede config-entry.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import MPC_ADVICE_STATUS_ATTR
from .helpers import resolve_own_entity_id

# bron → unique_id-achtervoegsel van de sensor die het advies publiceert.
ADVICE_SOURCES: dict[str, str] = {
    "stooklijn": "recommended_supply_temp",
    "mpc": "mpc_recommended_supply_temp",
    "1r1c": "rc_recommended_supply_temp",
}
ADVICE_SOURCE_OPTIONS: list[str] = list(ADVICE_SOURCES)

# Terugval als de entity registry de sensor (nog) niet kent, bijvoorbeeld in
# de eerste seconden na het opstarten. Dit zijn de vaste slugs waaronder de
# sensoren worden aangemaakt.
_FALLBACK_ENTITY: dict[str, str] = {
    "stooklijn": "sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
    "mpc": "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
    "1r1c": "sensor.quatt_warmteanalyse_1r1c_aanbevolen_aanvoertemperatuur",
}


def valid_source(source: str | None, default: str) -> str:
    """De bron als die bestaat, anders ``default``."""
    return source if source in ADVICE_SOURCES else default


def advice_entity_id(hass: HomeAssistant | None, entry_id: str | None, source: str) -> str:
    """Entity-ID van de adviessensor voor ``source``."""
    if hass is not None and entry_id:
        entity_id = resolve_own_entity_id(hass, "sensor", entry_id, ADVICE_SOURCES[source])
        if isinstance(entity_id, str):
            return entity_id
    return _FALLBACK_ENTITY[source]


def read_advice(
    hass: HomeAssistant | None, entry_id: str | None, source: str
) -> tuple[float | None, str | None]:
    """Het advies van ``source`` plus de reden als het er niet is.

    De reden (``advies_status``) maakt "huis warm genoeg" (``geen_vraag``)
    onderscheidbaar van "er ontbreekt een meting" (``geen_invoer``). Geeft
    (None, None) zolang de sensor er niet is.
    """
    if hass is None:
        return None, None
    state = hass.states.get(advice_entity_id(hass, entry_id, source))
    if state is None or state.state in ("unavailable", ""):
        return None, None
    attrs = getattr(state, "attributes", None)
    status = attrs.get(MPC_ADVICE_STATUS_ATTR) if isinstance(attrs, dict) else None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        value = None
    return value, status


def is_fallback(hass: HomeAssistant | None, entry_id: str | None, source: str) -> bool:
    """Geeft deze bron nu het advies van een andere bron door?

    Alleen de 1R1C-bron kan dat: zolang het huismodel niet online is, staat
    ``terugval: true`` op zijn sensor.
    """
    if hass is None:
        return False
    state = hass.states.get(advice_entity_id(hass, entry_id, source))
    attrs = getattr(state, "attributes", None)
    return bool(isinstance(attrs, dict) and attrs.get("terugval"))
