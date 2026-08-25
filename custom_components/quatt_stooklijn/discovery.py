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

from .heat_demand import (
    PROXY_FALLBACK_ENTITY,
    PROXY_UNIQUE_ID,
    SOURCE_SELECTOR_ENTITY,
    HeatDemandLink,
    selector_entity,
)

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
# Power House-modelparameters. Alleen OpenQuatt kent deze; de Quatt-integratie
# heeft geen equivalent, dus ze staan bewust niet in QUATT_KEYS of
# FALLBACK_ENTITIES. Ze zijn ook geen meting maar een instelknop — de spiegels
# in sources.py laten ze daarom links liggen.
ROLE_PH_ZERO_POWER_TEMP = "ph_zero_power_temp"
ROLE_PH_COLD_TEMP = "ph_cold_temp"
ROLE_PH_RATED_POWER = "ph_rated_power"
# De keuzeknop die bepaalt of Power House naar een externe warmtevraag luistert.
# Uitsluitend om te *lezen*: hij zegt of de gepubliceerde warmtevraag ergens
# aankomt. Bewust géén schrijfbestemming, en dus ook niet bekend bij
# ``async_resolve_setting_entity`` — deze integratie zet de regelaar niet zelf
# in een andere modus.
ROLE_EXT_HEAT_DEMAND_SOURCE = "ext_heat_demand_source"

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
    # "Maximum heating outdoor temperature" heet in de firmware nog
    # house_zero_power_temp_c; ESPHome bouwt de entity-ID uit de *naam*, dus dit
    # is de stabiele sleutel — niet de id en niet number.openquatt_house_zero_*.
    ROLE_PH_ZERO_POWER_TEMP: ("Maximum heating outdoor temperature",),
    ROLE_PH_COLD_TEMP: ("House cold temp",),
    ROLE_PH_RATED_POWER: ("Rated maximum house power",),
    ROLE_EXT_HEAT_DEMAND_SOURCE: ("External Heat Demand Source",),
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
def _async_openquatt_node(hass: HomeAssistant) -> dict[str, str]:
    """Geef ``{entity-naam: entity_id}`` van de ESPHome-node die OpenQuatt draait.

    ESPHome-entiteiten van álle nodes delen hetzelfde platform, dus ze worden
    eerst op MAC-adres gegroepeerd; alleen de groep die
    ``OPENQUATT_SIGNATURE_NAME`` bevat telt mee. Leeg = geen OpenQuatt.
    """
    registry = er.async_get(hass)

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

    return next(
        (names for names in by_node.values() if OPENQUATT_SIGNATURE_NAME in names),
        {},
    )


@callback
def async_openquatt_node_entities(hass: HomeAssistant) -> set[str]:
    """Álle entity-ID's die op de OpenQuatt-node zitten.

    Nodig om te bepalen of een willekeurige entity van OpenQuatt komt, ook als
    het niet degene is die voor een rol is gekozen.
    """
    return set(_async_openquatt_node(hass).values())


@callback
def async_discover_openquatt_entities(hass: HomeAssistant) -> dict[str, str]:
    """Zoek per rol de bijbehorende OpenQuatt-entity op via het entity-register.

    Werkt als ``async_discover_quatt_entities``, maar tegen de ESPHome-node die
    OpenQuatt draait. Geen OpenQuatt in huis, dan is het resultaat leeg — dat is
    geen fout.
    """
    node = _async_openquatt_node(hass)
    if not node:
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
def _first_configured(hass: HomeAssistant, configured) -> str:
    """Normaliseer een ingestelde waarde naar één entity-ID.

    De buitentemperatuur wordt als *lijst* van kandidaten ingesteld, de rest als
    losse string. ``async_resolve_entity`` kreeg daardoor soms een lijst binnen
    en liep stuk op ``.strip()``. Dat bleef lang onzichtbaar omdat de
    bronregistratie normaal al een waarde heeft en dit pad alleen wordt geraakt
    als terugval — precies tijdens een herstart of reload, wanneer de registratie
    nog leeg is en er dus het minst aan de hand mag zijn.

    Uit een lijst wint de eerste die bestaat; bestaat er geen, dan de eerste
    niet-lege, zodat de UI nog iets kan tonen.
    """
    if configured is None:
        return ""
    if isinstance(configured, str):
        return configured.strip()
    candidates = [str(c).strip() for c in configured if c and str(c).strip()]
    for candidate in candidates:
        if async_entity_exists(hass, candidate):
            return candidate
    return candidates[0] if candidates else ""


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

    configured = _first_configured(hass, config.get(conf_key) if conf_key else None)
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
def async_entity_has_value(hass: HomeAssistant, entity_id: str) -> bool:
    """Geeft deze entity op dit moment een bruikbare waarde?

    Strenger dan ``async_entity_exists``: een entity die in het register staat
    maar ``unknown``/``unavailable`` teruggeeft telt hier niet mee. Nodig voor
    het schrijfpad, dat moet weten of de knop er ook echt is.
    """
    state = hass.states.get(entity_id)
    return state is not None and state.state not in ("unknown", "unavailable", "")


@callback
def async_resolve_setting_entity(
    hass: HomeAssistant,
    config: dict,
    conf_key: str | None,
    role: str,
    *,
    discovered: dict[str, str] | None = None,
    openquatt: dict[str, str] | None = None,
) -> str | None:
    """Bepaal naar wélke instelknop geschreven moet worden.

    De tegenhanger van ``async_resolve_entity``, en met een bewust omgekeerde
    voorkeur. Voor een *meting* gaat Quatt voor, zodat bestaande installaties
    hun vertrouwde bron houden (zie ``sources.async_resolve_candidates``). Voor
    een *instelknop* telt iets anders: wie gehoorzaamt de schrijfactie.

    In een Q-edition-opstelling regelt OpenQuatt de buitenunits en kijkt de CiC
    alleen mee via de compatibiliteitslaag. Die laag bevestigt CiC-schrijfacties
    wel, maar mapt ze nergens naartoe — ``oq_cic_compatibility.yaml``: "Known CiC
    writes are acknowledged but ignored". Naar de CiC schrijven is dan een stille
    no-op. OpenQuatt's eigen knop is het echte aangrijpingspunt: die begrenst de
    stooklijn, de OT-slave en de ketel-dispatch (``oq_thermal_limits.yaml``).

    Volgorde:
      1. Wat de gebruiker heeft ingesteld, mits die entity een waarde geeft.
      2. OpenQuatt, mits de node bereikbaar is en een waarde geeft.
      3. Auto-detectie via het Quatt entity-register.
      4. Een bekende terugvalnaam die bestaat.

    Staat OpenQuatt offline, dan valt stap 2 weg en wordt de CiC gewoon weer de
    bestemming — precies wat je wilt als de HCQ losgekoppeld is.
    """
    configured = (config.get(conf_key) or "").strip() if conf_key else ""
    if configured and async_entity_has_value(hass, configured):
        return configured

    if openquatt is None:
        openquatt = async_discover_openquatt_entities(hass)
    oq_entity = openquatt.get(role)
    if oq_entity and async_entity_has_value(hass, oq_entity):
        return oq_entity

    return async_resolve_entity(
        hass, config, conf_key, role, discovered=discovered
    )


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


@callback
def async_heat_demand_link(
    hass: HomeAssistant,
    demand_entity: str,
    *,
    openquatt: dict[str, str] | None = None,
) -> HeatDemandLink:
    """Stel vast of ``demand_entity`` nu de feedforward van Power House voedt.

    Drie schakels, elk op hun eigen manier op te zoeken:

    * de keuzeknop in de firmware — een ESPHome-entity, dus via de node op
      *naam* en niet op entity-ID (zie ``_async_openquatt_node``);
    * de proxy uit het HA-package — een template-sensor met een vaste
      unique-ID, die geen device en dus geen gebiedsprefix heeft;
    * de bronhelper, waarvan de entity-ID uit de YAML-sleutel volgt.

    Ontbreekt er een, dan is dat geen fout: de firmware valt stil en correct
    terug op haar eigen huismodel. Precies daarom is deze status het melden
    waard — een niet-aangekomen koppeling ziet er verder uit als een werkende.
    """
    registry = er.async_get(hass)

    proxy = registry.async_get_entity_id("sensor", "template", PROXY_UNIQUE_ID)
    if proxy is None and async_entity_exists(hass, PROXY_FALLBACK_ENTITY):
        proxy = PROXY_FALLBACK_ENTITY

    # ``openquatt`` mag worden meegegeven door een aanroeper die de detectie
    # toch al deed: die scant het hele entity-register, en deze status wordt
    # bij elke buitentemperatuur-update opnieuw opgebouwd.
    if openquatt is None:
        openquatt = async_discover_openquatt_entities(hass)
    select_entity = openquatt.get(ROLE_EXT_HEAT_DEMAND_SOURCE)
    firmware_source: str | None = None
    if select_entity:
        state = hass.states.get(select_entity)
        if state is not None and state.state not in ("unknown", "unavailable", ""):
            firmware_source = state.state

    selector_state = hass.states.get(SOURCE_SELECTOR_ENTITY)

    return HeatDemandLink(
        demand_entity=demand_entity,
        select_entity=select_entity,
        firmware_source=firmware_source,
        proxy_entity=proxy,
        selector=selector_entity(selector_state.state if selector_state else None),
    )
