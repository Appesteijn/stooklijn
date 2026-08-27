"""Welke integratie levert welke meting — één plek die dat bepaalt én toont.

Deze integratie kan zijn metingen uit twee bronnen halen: de officiële
Quatt-integratie (cloud, via de CiC) en OpenQuatt (lokaal, via ESPHome). In een
Q-edition-opstelling draaien die naast elkaar, en de cloudkant kan wegvallen
terwijl OpenQuatt dezelfde meting gewoon blijft leveren.

Tot nu toe loste elke sensor dat zelf op, één keer bij het opstarten, met een
vast entity-ID. Viel die bron weg, dan bleef de sensor leeg zonder dat ergens
zichtbaar was waaróm. En een dashboard kan helemaal niet resolven — dat
hardcodeert een entity-ID en breekt mee.

Daarom deze laag:

- per rol wordt continu bepaald welke kandidaat bruikbaar is;
- de gekozen waarde wordt gepubliceerd als een eigen sensor met een stabiel
  entity-ID, zodat het dashboard daaraan kan hangen en niets meer weet van
  Quatt versus OpenQuatt;
- welke bron actief is staat in de attributen, dus het is af te lezen in plaats
  van af te leiden.

Bewust géén debounce op de waarde. Bij het schrijfpad is wachten juist goed —
niet omschakelen op een reconnect van twintig seconden — maar een spiegel hoort
nooit leeg te staan terwijl er een werkende bron is. De hoogst geprioriteerde
bruikbare kandidaat wint, elke evaluatie opnieuw. Wisselingen worden gelogd, dus
geflipper blijft zichtbaar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util

# const.py importeert zelf niets, dus dit levert geen kringverwijzing op.
from .const import (
    CONF_BOILER_HEAT_ENTITY,
    CONF_CONTROL_SETPOINT_ENTITY,
    CONF_COP_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_POWER_ENTITY,
    CONF_POWER_INPUT_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_ROOM_SETPOINT_ENTITY,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
)
from .discovery import (
    QUATT_PLATFORM,
    ROLE_BOILER_HEAT,
    ROLE_CONTROL_SETPOINT,
    ROLE_COP,
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_RETURN_TEMP,
    ROLE_ROOM_SETPOINT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    async_discover_openquatt_entities,
    async_discover_quatt_entities,
    async_openquatt_node_entities,
    async_resolve_candidates,
    async_resolve_entity,
)

_LOGGER = logging.getLogger(__name__)

# Hoe vaak opnieuw wordt gekeken welke bron bruikbaar is. Eén minuut is ruim
# genoeg: de onderliggende sensoren updaten om de tien seconden, en de spiegel
# leest zijn waarde live uit de state machine — dit interval bepaalt alleen hoe
# snel een bronwissel wordt opgemerkt en gelogd.
EVALUATE_INTERVAL = timedelta(minutes=1)

# States die betekenen "deze bron levert nu niets".
UNUSABLE_STATES = ("unknown", "unavailable", "none", "")

# Herkenbare namen voor in de attributen.
SOURCE_QUATT = "quatt"
SOURCE_OPENQUATT = "openquatt"
SOURCE_OTHER = "overig"

# De rollen die als spiegel worden gepubliceerd, met hun presentatie. De sleutel
# is de rol; ``name`` bepaalt het entity-ID (sensor.quatt_warmteanalyse_<slug>).
#
# ch_max_water staat er bewust niet bij: dat is een instelknop waar naartoe
# geschreven wordt, geen meting die gespiegeld hoort te worden.
@dataclass(frozen=True)
class MirrorSpec:
    role: str
    name: str
    unit: str | None
    device_class: str | None
    icon: str
    # Vastgelegde object-id, niet afgeleid van de naam. HA bouwt de entity-id
    # voor een nieuwe entity op uit de área van het device, dus zonder dit
    # wordt het sensor.bijkeuken_quatt_warmteanalyse_… — en dan klopt geen
    # enkele dashboardverwijzing meer. Wijzig deze slugs nooit: er hangen
    # dashboards en automatiseringen aan.
    slug: str


MIRROR_SPECS: tuple[MirrorSpec, ...] = (
    MirrorSpec(ROLE_SUPPLY_TEMP, "Aanvoertemperatuur", "°C", "temperature",
               "mdi:thermometer-water", "aanvoertemperatuur"),
    MirrorSpec(ROLE_RETURN_TEMP, "Retourtemperatuur", "°C", "temperature",
               "mdi:thermometer-chevron-down", "retourtemperatuur"),
    MirrorSpec(ROLE_OUTDOOR_TEMP, "Buitentemperatuur", "°C", "temperature",
               "mdi:home-thermometer-outline", "buitentemperatuur"),
    MirrorSpec(ROLE_INDOOR_TEMP, "Kamertemperatuur", "°C", "temperature",
               "mdi:home-thermometer", "kamertemperatuur"),
    MirrorSpec(ROLE_CONTROL_SETPOINT, "Thermostaat Setpoint", "°C", "temperature",
               "mdi:thermostat", "thermostaat_setpoint"),
    MirrorSpec(ROLE_ROOM_SETPOINT, "Kamer Setpoint", "°C", "temperature",
               "mdi:home-thermometer-outline", "kamer_setpoint"),
    MirrorSpec(ROLE_FLOW_RATE, "Debiet", "L/h", None, "mdi:pump", "debiet"),
    MirrorSpec(ROLE_TOTAL_POWER, "Thermisch Vermogen", "W", "power",
               "mdi:heat-wave", "thermisch_vermogen"),
    MirrorSpec(ROLE_POWER_INPUT, "Opgenomen Vermogen", "W", "power",
               "mdi:flash", "opgenomen_vermogen"),
    MirrorSpec(ROLE_BOILER_HEAT, "Ketelvermogen", "W", "power", "mdi:fire",
               "ketelvermogen"),
    MirrorSpec(ROLE_COP, "COP", None, None, "mdi:chart-line", "cop"),
)

# Object-id van de overzichtssensor, om dezelfde reden vastgelegd.
OVERVIEW_SLUG = "databronnen"

# Prefix waaronder alle entiteiten van deze integratie leven.
ENTITY_PREFIX = "quatt_warmteanalyse"

MIRROR_ROLES: tuple[str, ...] = tuple(spec.role for spec in MIRROR_SPECS)

# Rol → config-sleutel waarmee de gebruiker de bron zelf kiest.
#
# Een ingestelde entity gaat vóór alle detectie (zie async_resolve_candidates),
# dus dit is de enige manier om de vaste voorkeur "Quatt vóór OpenQuatt" te
# overrulen. Elke rol uit MIRROR_SPECS hoort hier te staan: ontbreekt er een,
# dan biedt het optiescherm wél een keuze aan die de bronregistratie vervolgens
# negeert — een stille misconfiguratie. test_sources bewaakt de volledigheid.
ROLE_CONF_KEYS: dict[str, str] = {
    ROLE_SUPPLY_TEMP: CONF_SUPPLY_TEMP_ENTITY,
    ROLE_RETURN_TEMP: CONF_RETURN_TEMP_ENTITY,
    ROLE_OUTDOOR_TEMP: CONF_TEMP_ENTITIES,
    ROLE_INDOOR_TEMP: CONF_INDOOR_TEMP_ENTITY,
    ROLE_CONTROL_SETPOINT: CONF_CONTROL_SETPOINT_ENTITY,
    ROLE_ROOM_SETPOINT: CONF_ROOM_SETPOINT_ENTITY,
    ROLE_FLOW_RATE: CONF_FLOW_ENTITY,
    ROLE_TOTAL_POWER: CONF_POWER_ENTITY,
    ROLE_POWER_INPUT: CONF_POWER_INPUT_ENTITY,
    ROLE_BOILER_HEAT: CONF_BOILER_HEAT_ENTITY,
    ROLE_COP: CONF_COP_ENTITY,
}


@callback
def classify_source(
    hass: HomeAssistant,
    entity_id: str,
    openquatt_entities: set[str],
) -> str:
    """Bepaal van welke integratie een entity-ID afkomstig is.

    Op het entity-register, niet op de naam: ``sensor.openquatt_...`` is geen
    bewijs, want entiteiten kunnen hernoemd zijn.

    Ook niet op de rol-detectiekaarten. Die bevatten per rol één gekozen entity,
    dus een Quatt-sensor die nét niet de voorkeur kreeg — bijvoorbeeld
    ``sensor.thermostat_temperature_outside`` waar hp1 voorging — zou dan ten
    onrechte als "overig" worden aangemerkt.
    """
    if entity_id in openquatt_entities:
        return SOURCE_OPENQUATT

    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None:
        return SOURCE_OTHER
    if reg_entry.platform == QUATT_PLATFORM:
        return SOURCE_QUATT
    return SOURCE_OTHER


@callback
def async_source_entity(
    hass: HomeAssistant,
    entry_id: str,
    role: str,
    *,
    config: dict | None = None,
    conf_key: str | None = None,
) -> str | None:
    """Geef de entity-ID die deze rol op dit moment levert.

    Dit is wat de rest van de integratie hoort te gebruiken in plaats van
    ``async_resolve_entity``. Die kiest op *bestaan*: een Quatt-sensor die er
    nog staat maar ``unknown`` teruggeeft wint het van een OpenQuatt-sensor die
    de meting wél levert. De registry kiest op *beschikbaarheid* en lost dat op.

    Valt terug op de losse resolver als de registry er nog niet is — tijdens het
    opstarten kan een entiteit al gebouwd worden voordat de eerste evaluatie is
    gedraaid — of als geen enkele kandidaat een waarde geeft. Dan is er niets te
    kiezen en is een naam tonen beter dan niets.
    """
    from .const import DOMAIN

    registry: SourceRegistry | None = hass.data.get(DOMAIN, {}).get(
        f"{entry_id}_sources"
    )
    if registry is not None:
        active = registry.active_entity(role)
        if active:
            return active

    return async_resolve_entity(hass, config or {}, conf_key, role)


@callback
def is_usable(hass: HomeAssistant, entity_id: str) -> bool:
    """Levert deze entity op dit moment een bruikbaar getal?"""
    state = hass.states.get(entity_id)
    if state is None or str(state.state).lower() in UNUSABLE_STATES:
        return False
    try:
        float(state.state)
    except (ValueError, TypeError):
        return False
    return True


@dataclass
class RoleSource:
    """Stand van zaken voor één meting."""

    role: str
    candidates: list[str] = field(default_factory=list)
    active: str | None = None
    integration: str | None = None
    switched_at: datetime | None = None

    @property
    def available(self) -> bool:
        return self.active is not None


class SourceRegistry:
    """Houdt per rol bij welke bron levert, en herevalueert dat periodiek."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        self._hass = hass
        self._config = config
        self._roles: dict[str, RoleSource] = {
            role: RoleSource(role=role) for role in MIRROR_ROLES
        }
        self._listeners: list = []

    # ------------------------------------------------------------------

    @property
    def roles(self) -> dict[str, RoleSource]:
        return self._roles

    def get(self, role: str) -> RoleSource | None:
        return self._roles.get(role)

    def active_entity(self, role: str) -> str | None:
        source = self._roles.get(role)
        return source.active if source else None

    def summary(self) -> dict[str, dict]:
        """Rol → {entity, integratie, kandidaten} voor de overzichtssensor."""
        return {
            role: {
                "entity": source.active,
                "integration": source.integration,
                "candidates": list(source.candidates),
                "switched_at": (
                    source.switched_at.isoformat() if source.switched_at else None
                ),
            }
            for role, source in self._roles.items()
        }

    def integrations_in_use(self) -> list[str]:
        used = {s.integration for s in self._roles.values() if s.integration}
        # Vaste volgorde, zodat de sensorstate niet wisselt op set-volgorde.
        return [
            name
            for name in (SOURCE_QUATT, SOURCE_OPENQUATT, SOURCE_OTHER)
            if name in used
        ]

    # ------------------------------------------------------------------

    @callback
    def async_setup(self):
        """Eerste evaluatie + periodieke herevaluatie. Geeft remove-callback."""
        self.async_evaluate()
        remove = async_track_time_interval(
            self._hass, self._async_tick, EVALUATE_INTERVAL
        )
        self._listeners.append(remove)

        def _remove_all() -> None:
            for unsub in self._listeners:
                unsub()
            self._listeners.clear()

        return _remove_all

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_evaluate()

    @callback
    def async_evaluate(self) -> list[str]:
        """Bepaal per rol de actieve bron. Geeft de gewijzigde rollen terug.

        Kandidaten worden elke keer opnieuw opgehaald: integraties kunnen ná het
        opstarten van deze integratie geladen worden, en dan moet een nieuw
        beschikbare bron alsnog meedoen.
        """
        discovered = async_discover_quatt_entities(self._hass)
        openquatt = async_discover_openquatt_entities(self._hass)
        openquatt_entities = async_openquatt_node_entities(self._hass)
        changed: list[str] = []

        for role, source in self._roles.items():
            source.candidates = async_resolve_candidates(
                self._hass,
                self._configured_for(role),
                role,
                discovered=discovered,
                openquatt=openquatt,
            )

            best = next(
                (c for c in source.candidates if is_usable(self._hass, c)), None
            )
            if best == source.active:
                continue

            previous = source.active
            source.active = best
            source.integration = (
                classify_source(self._hass, best, openquatt_entities)
                if best
                else None
            )
            source.switched_at = dt_util.now()
            changed.append(role)

            if best is None:
                _LOGGER.warning(
                    "Bron voor '%s' weggevallen: geen van %s levert een waarde",
                    role,
                    source.candidates or "(geen kandidaten)",
                )
            elif previous is None:
                _LOGGER.info(
                    "Bron voor '%s': %s (%s)", role, best, source.integration
                )
            else:
                _LOGGER.warning(
                    "Bron voor '%s' gewisseld: %s → %s (%s)",
                    role, previous, best, source.integration,
                )

        return changed

    def _configured_for(self, role: str) -> str | list[str] | None:
        """Wat de gebruiker voor deze rol heeft ingesteld, indien iets."""
        conf_key = ROLE_CONF_KEYS.get(role)
        return self._config.get(conf_key) if conf_key else None
