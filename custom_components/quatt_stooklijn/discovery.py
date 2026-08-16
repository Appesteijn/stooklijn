"""Auto-detectie van Quatt-entiteiten via het entity-register.

De Quatt-integratie zet ``_attr_has_entity_name = True``, waardoor Home Assistant
entity-IDs opbouwt als ``sensor.<device>_<entity>``. Sinds de v2→v3 migratie van
die integratie is het ene generieke ``Heatpump``-device vervangen door losse
devices (``CIC``, ``Flowmeter``, ``Heatpump 1``, ``Boiler``, ``Thermostat``, …).

HA hernoemt bestaande entity-IDs nooit bij zo'n device-wijziging. Daardoor lopen
er twee naamgevingen naast elkaar in het veld:

    installatie vóór de migratie : sensor.heatpump_flowmeter_temperature
    installatie ná de migratie   : sensor.flowmeter_temperature

Hardcoded entity-IDs werken dus per definitie maar voor één van beide groepen.
De ``unique_id`` van de Quatt-integratie is wél stabiel:

    <hub_id>:<device_id>:<sensor_key>

bijvoorbeeld ``CIC-abc123:flowmeter:flowMeter.waterSupplyTemperature``. Die
sleutel verandert niet als het device hernoemd wordt, dus we zoeken daarop.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Platform (integratie-domein) van de officiële Quatt-integratie.
QUATT_PLATFORM = "quatt"

# OpenQuatt draait op ESPHome; er is geen eigen platform-naam om op te filteren.
OPENQUATT_PLATFORM = "esphome"

# Entity-naam die alleen op een OpenQuatt-node voorkomt. Hiermee wordt bepaald
# wélke ESPHome-node OpenQuatt is — zonder deze check zou een willekeurige
# andere node met een sensor "Total Heat Power" ook meegenomen worden.
OPENQUATT_SIGNATURE_NAME = "OpenQuatt Version"

# Logische rollen die deze integratie nodig heeft.
ROLE_SUPPLY_TEMP = "supply_temp"
ROLE_FLOW_RATE = "flow_rate"
ROLE_RETURN_TEMP = "return_temp"
ROLE_OUTDOOR_TEMP = "outdoor_temp"
ROLE_TOTAL_POWER = "total_power"
ROLE_POWER_INPUT = "power_input"
ROLE_BOILER_HEAT = "boiler_heat"
ROLE_INDOOR_TEMP = "indoor_temp"
ROLE_CH_MAX_WATER = "ch_max_water"
ROLE_CONTROL_SETPOINT = "control_setpoint"
ROLE_ROOM_SETPOINT = "room_setpoint"
ROLE_COP = "cop"

# Rol → Quatt sensor-keys, in volgorde van voorkeur. Deze keys komen uit
# sensor_descriptions_cic.py / sensor_descriptions_heat.py / number.py van de
# Quatt-integratie en zijn onafhankelijk van de device-naamgeving.
QUATT_KEYS: dict[str, tuple[str, ...]] = {
    ROLE_SUPPLY_TEMP: ("flowMeter.waterSupplyTemperature",),
    ROLE_FLOW_RATE: ("qc.flowRateFiltered",),
    ROLE_RETURN_TEMP: ("hp1.temperatureWaterIn", "hp2.temperatureWaterIn"),
    # Buitentemperatuur: de warmtepomp-sensor is nauwkeuriger dan de
    # thermostaat-waarde, dus hp1/hp2 gaan voor.
    ROLE_OUTDOOR_TEMP: (
        "hp1.temperatureOutside",
        "hp2.temperatureOutside",
        "temperatureOutside",
    ),
    ROLE_TOTAL_POWER: ("computedPower",),
    ROLE_POWER_INPUT: ("computedPowerInput",),
    ROLE_BOILER_HEAT: ("boiler.computedBoilerHeatPower",),
    ROLE_INDOOR_TEMP: ("thermostat.otFtRoomTemperature",),
    ROLE_CH_MAX_WATER: ("chMaxWaterTemperature",),
    ROLE_CONTROL_SETPOINT: ("thermostat.otFtControlSetpoint",),
    ROLE_ROOM_SETPOINT: ("thermostat.otFtRoomSetpoint",),
    ROLE_COP: ("computedQuattCop",),
}

# Terugvalnamen als auto-detectie niets vindt (Quatt-integratie afwezig, of een
# andere warmtepomp). Legacy-naam eerst — die hoort bij de oudste installaties,
# waar deze integratie vandaan komt. Alleen gebruikt als de entity bestaat.
FALLBACK_ENTITIES: dict[str, tuple[str, ...]] = {
    ROLE_SUPPLY_TEMP: (
        "sensor.heatpump_flowmeter_temperature",
        "sensor.flowmeter_temperature",
    ),
    ROLE_FLOW_RATE: (
        "sensor.heatpump_flowmeter_flowrate",
        "sensor.flowmeter_flowrate",
    ),
    ROLE_RETURN_TEMP: (
        "sensor.heatpump_hp1_temperature_water_in",
        "sensor.heatpump_1_temperature_water_in",
    ),
    ROLE_OUTDOOR_TEMP: (
        "sensor.heatpump_hp1_temperature_outside",
        "sensor.heatpump_1_temperature_outside",
        "sensor.thermostat_temperature_outside",
    ),
    ROLE_TOTAL_POWER: (
        "sensor.heatpump_total_power",
        "sensor.cic_total_power",
    ),
    ROLE_POWER_INPUT: (
        "sensor.heatpump_total_power_input",
        "sensor.cic_total_power_input",
    ),
    ROLE_BOILER_HEAT: (
        "sensor.heatpump_boiler_heat_power",
        "sensor.boiler_heat_power",
    ),
    ROLE_INDOOR_TEMP: (
        "sensor.heatpump_thermostat_room_temperature",
        "sensor.thermostat_room_temperature",
    ),
    ROLE_CH_MAX_WATER: (
        "number.heatpump_cic_max_water_temperature",
        "number.cic_max_water_temperature",
    ),
    ROLE_CONTROL_SETPOINT: (
        "sensor.heatpump_thermostat_control_setpoint",
        "sensor.thermostat_control_setpoint",
    ),
    ROLE_ROOM_SETPOINT: (
        "sensor.heatpump_thermostat_room_setpoint",
        "sensor.thermostat_room_setpoint",
    ),
    ROLE_COP: (
        "sensor.heatpump_total_quatt_cop",
        "sensor.cic_total_quatt_cop",
    ),
}

# Rol → OpenQuatt entity-namen, in volgorde van voorkeur.
#
# ESPHome bouwt zijn unique_id op als "<mac>/<n>/<domain>/<Naam>", waarbij de
# naam letterlijk uit de firmware-YAML komt. Die naam is dus de stabiele sleutel
# — de entity-ID juist niet. Dat is hier geen theoretisch punt: OpenQuatt heeft
# zowel een "Curve Tsupply @ -10°C" als een "Curve Tsupply @ 10°C", en HA heeft
# daar `number.openquatt_curve_tsupply_10degc` en `..._10degc_2` van gemaakt.
# Op entity-ID matchen verwisselt die twee stilletjes; op naam matchen niet.
OPENQUATT_NAMES: dict[str, tuple[str, ...]] = {
    ROLE_SUPPLY_TEMP: ("Water Supply Temp (Selected)",),
    ROLE_FLOW_RATE: ("Flow average (Selected)", "HP1 - Flow"),
    ROLE_RETURN_TEMP: ("HP1 - Water in temperature", "HP2 - Water in temperature"),
    ROLE_OUTDOOR_TEMP: (
        "Outside Temperature (Selected)",
        "HP1 - Outside temperature",
        "HP2 - Outside temperature",
    ),
    ROLE_TOTAL_POWER: ("Total Heat Power",),
    ROLE_POWER_INPUT: ("Total Power Input",),
    ROLE_BOILER_HEAT: ("Boiler Heat Power",),
    ROLE_INDOOR_TEMP: ("Room Temperature (Selected)",),
    ROLE_CH_MAX_WATER: ("Maximum water temperature",),
    ROLE_CONTROL_SETPOINT: ("OT - Control Setpoint",),
    ROLE_ROOM_SETPOINT: ("Room Setpoint (Selected)", "OT - Room Setpoint"),
    ROLE_COP: ("Total COP",),
}


@callback
def async_discover_quatt_entities(hass: HomeAssistant) -> dict[str, str]:
    """Zoek per rol de bijbehorende Quatt-entity op via het entity-register.

    Geeft een ``{rol: entity_id}``-mapping terug met alleen de rollen die
    daadwerkelijk gevonden zijn. Ontbreekt de Quatt-integratie, dan is het
    resultaat leeg — dat is geen fout.
    """
    registry = er.async_get(hass)

    # sensor_key → entity_id voor alles wat de Quatt-integratie heeft aangemaakt.
    by_key: dict[str, str] = {}
    for reg_entry in registry.entities.values():
        if reg_entry.platform != QUATT_PLATFORM or reg_entry.disabled:
            continue
        # unique_id = "<hub_id>:<device_id>:<sensor_key>"; de sensor-key bevat
        # zelf geen dubbele punt, dus de laatste ':' is de scheiding.
        _, sep, sensor_key = reg_entry.unique_id.rpartition(":")
        if sep and sensor_key:
            by_key.setdefault(sensor_key, reg_entry.entity_id)

    resolved: dict[str, str] = {}
    for role, keys in QUATT_KEYS.items():
        for key in keys:
            if key in by_key:
                resolved[role] = by_key[key]
                break

    if resolved:
        _LOGGER.debug("Quatt auto-detectie: %d van %d rollen gevonden — %s",
                      len(resolved), len(QUATT_KEYS), resolved)
    else:
        _LOGGER.debug(
            "Quatt auto-detectie: geen entiteiten van platform '%s' gevonden",
            QUATT_PLATFORM,
        )
    return resolved


@callback
def async_discover_openquatt_entities(hass: HomeAssistant) -> dict[str, str]:
    """Zoek per rol de bijbehorende OpenQuatt-entity op via het entity-register.

    Werkt als ``async_discover_quatt_entities``, maar tegen de ESPHome-node die
    OpenQuatt draait. Omdat ESPHome-entiteiten van álle nodes hetzelfde platform
    delen, worden ze eerst op MAC-adres gegroepeerd; alleen de groep die
    ``OPENQUATT_SIGNATURE_NAME`` bevat telt mee.

    Geen OpenQuatt in huis, dan is het resultaat leeg — dat is geen fout.
    """
    registry = er.async_get(hass)

    # mac → {entity-naam: entity_id}, voor alle ESPHome-nodes.
    by_node: dict[str, dict[str, str]] = {}
    for reg_entry in registry.entities.values():
        if reg_entry.platform != OPENQUATT_PLATFORM or reg_entry.disabled:
            continue
        # unique_id = "<mac>/<index>/<domain>/<naam>". De naam mag zelf een '/'
        # bevatten, dus splitsen met maxsplit=3.
        parts = reg_entry.unique_id.split("/", 3)
        if len(parts) != 4:
            continue
        mac, _index, _domain, name = parts
        by_node.setdefault(mac, {}).setdefault(name, reg_entry.entity_id)

    node = next(
        (names for names in by_node.values() if OPENQUATT_SIGNATURE_NAME in names),
        None,
    )
    if node is None:
        _LOGGER.debug(
            "OpenQuatt auto-detectie: geen ESPHome-node met '%s' gevonden",
            OPENQUATT_SIGNATURE_NAME,
        )
        return {}

    resolved: dict[str, str] = {}
    for role, names in OPENQUATT_NAMES.items():
        for name in names:
            if name in node:
                resolved[role] = node[name]
                break

    _LOGGER.debug(
        "OpenQuatt auto-detectie: %d van %d rollen gevonden — %s",
        len(resolved), len(OPENQUATT_NAMES), resolved,
    )
    return resolved


@callback
def async_entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    """Bestaat deze entity? Register én state machine, want beide kunnen leiden.

    Het register is bij het opstarten al gevuld terwijl de state machine dat nog
    niet hoeft te zijn (laadvolgorde van integraties). Andersom staan
    YAML-template-sensoren zonder ``unique_id`` juist niet in het register.
    """
    if not entity_id:
        return False
    if hass.states.get(entity_id) is not None:
        return True
    return er.async_get(hass).async_get(entity_id) is not None


@callback
def async_resolve_entity(
    hass: HomeAssistant,
    config: dict,
    conf_key: str | None,
    role: str,
    *,
    discovered: dict[str, str] | None = None,
) -> str | None:
    """Bepaal welke entity-ID voor een rol gebruikt moet worden.

    Volgorde:
      1. Wat de gebruiker heeft ingesteld, mits die entity bestaat.
      2. Auto-detectie via het Quatt entity-register.
      3. Een bekende terugvalnaam die bestaat.
      4. De eerste terugvalnaam (zodat er iets te tonen valt in de UI).

    Stap 1 valt bewust door naar 2 als de ingestelde entity niet bestaat: dat
    repareert installaties die met verkeerde defaults zijn opgezet, zonder dat
    de gebruiker iets hoeft aan te passen.
    """
    if discovered is None:
        discovered = async_discover_quatt_entities(hass)

    configured = (config.get(conf_key) or "").strip() if conf_key else ""
    if configured:
        if async_entity_exists(hass, configured):
            return configured
        detected = discovered.get(role)
        if detected:
            _LOGGER.warning(
                "Ingestelde entity '%s' (%s) bestaat niet; automatisch "
                "teruggevallen op '%s'. Pas de instelling aan om deze melding "
                "te laten verdwijnen.",
                configured,
                role,
                detected,
            )
            return detected

    if role in discovered:
        return discovered[role]

    fallbacks = FALLBACK_ENTITIES.get(role, ())
    for candidate in fallbacks:
        if async_entity_exists(hass, candidate):
            return candidate

    return configured or (fallbacks[0] if fallbacks else None)


@callback
def async_resolve_from_list(
    hass: HomeAssistant,
    candidates: list[str] | None,
    role: str,
    *,
    discovered: dict[str, str] | None = None,
) -> str | None:
    """Als ``async_resolve_entity``, maar voor een ingestelde lijst kandidaten.

    Gebruikt voor de buitentemperatuur, waar de gebruiker meerdere sensoren in
    voorkeursvolgorde opgeeft. De eerste die bestaat wint; bestaat er geen, dan
    valt het terug op auto-detectie.
    """
    for candidate in candidates or []:
        if candidate and async_entity_exists(hass, candidate):
            return candidate
    return async_resolve_entity(hass, {}, None, role, discovered=discovered)


@callback
def async_resolve_candidates(
    hass: HomeAssistant,
    configured: str | list[str] | None,
    role: str,
    *,
    discovered: dict[str, str] | None = None,
    openquatt: dict[str, str] | None = None,
) -> list[str]:
    """Geef álle bruikbare entity-IDs voor een rol, in volgorde van voorkeur.

    Waar ``async_resolve_entity`` één winnaar kiest, levert dit de hele reeks op:
    eerst wat de gebruiker heeft ingesteld, dan de Quatt-detectie, dan de
    OpenQuatt-detectie, dan bekende terugvalnamen. Alleen entiteiten die
    daadwerkelijk bestaan komen erin, zonder dubbelen.

    Bedoeld voor de historische analyse, die een bronwissel midden in het
    venster moet kunnen overbruggen: de eerste bron blijft leidend, de volgende
    vullen alleen de gaten die de eerste laat vallen. Voor live sensoren blijft
    ``async_resolve_entity`` de juiste keuze — die hoort één bron te volgen.

    De Quatt-detectie staat bewust vóór OpenQuatt: bestaande installaties
    houden zo exact dezelfde primaire bron als voorheen.
    """
    if discovered is None:
        discovered = async_discover_quatt_entities(hass)
    if openquatt is None:
        openquatt = async_discover_openquatt_entities(hass)

    if configured is None:
        configured_list: list[str] = []
    elif isinstance(configured, str):
        configured_list = [configured]
    else:
        configured_list = list(configured)

    ordered = [
        *configured_list,
        discovered.get(role),
        openquatt.get(role),
        *FALLBACK_ENTITIES.get(role, ()),
    ]

    candidates: list[str] = []
    for entity_id in ordered:
        entity_id = (entity_id or "").strip()
        if not entity_id or entity_id in candidates:
            continue
        if async_entity_exists(hass, entity_id):
            candidates.append(entity_id)
    return candidates
