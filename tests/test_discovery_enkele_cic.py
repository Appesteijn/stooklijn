"""Eén CiC: het hub-anker voor meerdere installaties moet een no-op zijn.

Het anker (``async_config_quatt_hubs``) is er voor wie twee Quatt-installaties
in één HA heeft. Vrijwel iedereen heeft er één, eventueel met een Home Battery-
of Quatt Energy-hub ernaast. Voor hen moet elke resolver exact hetzelfde
opleveren als zonder anker, bij een verse installatie (lege config) en na een
update (bestaande config) — zie ``TestTweeInstallaties`` voor het andere geval.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn import discovery as new
from custom_components.quatt_stooklijn.const import (
    CONF_BOILER_HEAT_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_POWER_ENTITY,
    CONF_POWER_INPUT_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
)

CIC = "CIC-abc123"


def q(eid, dev, key, hub=CIC, disabled=False):
    return er.RegistryEntry(entity_id=eid, unique_id=f"{hub}:{dev}:{key}",
                            platform="quatt", disabled=disabled)


def oq(eid, name):
    return er.RegistryEntry(entity_id=eid, unique_id=f"58:E6:C5:6E:9D:78/0/sensor/{name}",
                            platform="esphome")


MODERN = [
    q("sensor.flowmeter_temperature", "flowmeter", "flowMeter.waterSupplyTemperature"),
    q("sensor.flowmeter_flowrate", "flowmeter", "qc.flowRateFiltered"),
    q("sensor.heatpump_1_temperature_water_in", "heatpump_1", "hp1.temperatureWaterIn"),
    q("sensor.heatpump_1_temperature_outside", "heatpump_1", "hp1.temperatureOutside"),
    q("sensor.thermostat_temperature_outside", "thermostat", "temperatureOutside"),
    q("sensor.cic_total_power", "cic", "computedPower"),
    q("sensor.cic_total_power_input", "cic", "computedPowerInput"),
    q("sensor.boiler_heat_power", "boiler", "boiler.computedBoilerHeatPower"),
    q("sensor.thermostat_room_temperature", "thermostat", "thermostat.otFtRoomTemperature"),
    q("number.cic_max_water_temperature", "cic", "chMaxWaterTemperature"),
    q("sensor.cic_total_quatt_cop", "cic", "computedQuattCop"),
    q("sensor.heatpump_1_compressor_frequency", "heatpump_1", "heatPumps.0.compressorFrequency"),
    q("select.cic_day_max_sound_level", "cic", "dayMaxSoundLevel"),
    q("select.cic_night_max_sound_level", "cic", "nightMaxSoundLevel"),
]
LEGACY = [q(e.entity_id.replace("sensor.", "sensor.heatpump_", 1)
            .replace("number.", "number.heatpump_", 1)
            .replace("select.", "select.heatpump_", 1),
            *e.unique_id.split(":")[1:]) for e in MODERN]
# Andere Quatt-hubs die een gebruiker met één CiC wél kan hebben.
OTHER_HUBS = [
    q("sensor.home_battery_state_of_charge", "battery", "stateOfCharge", hub="BAT-1234"),
    q("sensor.home_battery_power", "battery", "power", hub="BAT-1234"),
    q("sensor.quatt_energy_consumption", "energy", "consumption", hub="ENG-mark@x.nl"),
]
OPENQUATT = [
    oq("sensor.openquatt_total_heat_power", "Total Heat Power"),
    oq("sensor.openquatt_outside_temperature", "Outside Temperature"),
    oq("sensor.openquatt_hp1_compressor_frequency", "HP1 - Compressor frequency"),
    oq("sensor.openquatt_water_supply_temp_selected", "Water Supply Temp (Selected)"),
]

REGISTRIES = {
    "modern": MODERN,
    "legacy": LEGACY,
    "modern+bat+eng": OTHER_HUBS + MODERN,   # vreemde hubs eerst in register
    "legacy+openquatt": LEGACY + OPENQUATT,
    "modern+openquatt+bat": OTHER_HUBS + OPENQUATT + MODERN,
    "alleen_openquatt": OPENQUATT,
    "geen_quatt": [],
    "modern_deels_disabled": [q(e.entity_id, *e.unique_id.split(":")[1:], disabled=i % 3 == 0)
                              for i, e in enumerate(MODERN)],
}


def configs(reg):
    ids = [e.entity_id for e in reg]
    pick = lambda s: next((i for i in ids if s in i), None)
    return {
        "verse_install_leeg": {},
        "alleen_power": {CONF_POWER_ENTITY: pick("total_power") or ""},
        "alleen_temp": {CONF_TEMP_ENTITIES: [pick("temperature_outside")] if pick("temperature_outside") else []},
        "temp_weer_eerst": {CONF_TEMP_ENTITIES: ["sensor.knmi_temperatuur", pick("temperature_outside") or ""]},
        "power_template": {CONF_POWER_ENTITY: "sensor.mijn_template_power"},
        "power_bestaat_niet": {CONF_POWER_ENTITY: "sensor.weg_entity"},
        "power_openquatt": {CONF_POWER_ENTITY: "sensor.openquatt_total_heat_power",
                            CONF_TEMP_ENTITIES: [pick("temperature_outside") or ""]},
        "volledig": {
            CONF_POWER_ENTITY: pick("total_power") or "",
            CONF_FLOW_ENTITY: pick("flowrate") or "",
            CONF_SUPPLY_TEMP_ENTITY: pick("flowmeter_temperature") or "",
            CONF_RETURN_TEMP_ENTITY: pick("water_in") or "",
            CONF_POWER_INPUT_ENTITY: pick("power_input") or "",
            CONF_BOILER_HEAT_ENTITY: pick("boiler_heat") or "",
            CONF_TEMP_ENTITIES: [pick("temperature_outside") or ""],
        },
        "bat_entity_als_power": {CONF_POWER_ENTITY: "sensor.home_battery_power"},
        "energy_als_power_input": {CONF_POWER_INPUT_ENTITY: "sensor.quatt_energy_consumption"},
        "energy_input_plus_cic_temp": {CONF_POWER_INPUT_ENTITY: "sensor.quatt_energy_consumption",
                                       CONF_TEMP_ENTITIES: [pick("temperature_outside") or ""]},
    }


class S:
    def __init__(self, v): self.state = v


def hass_for(reg):
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(reg)
    st = {e.entity_id: S("1") for e in reg if not e.disabled}
    st["sensor.mijn_template_power"] = S("1")
    st["sensor.knmi_temperatuur"] = S("1")
    hass.states.get = lambda eid: st.get(eid)
    return hass


ROLE_CONF = {
    new.ROLE_TOTAL_POWER: CONF_POWER_ENTITY, new.ROLE_FLOW_RATE: CONF_FLOW_ENTITY,
    new.ROLE_SUPPLY_TEMP: CONF_SUPPLY_TEMP_ENTITY, new.ROLE_RETURN_TEMP: CONF_RETURN_TEMP_ENTITY,
    new.ROLE_POWER_INPUT: CONF_POWER_INPUT_ENTITY, new.ROLE_BOILER_HEAT: CONF_BOILER_HEAT_ENTITY,
    new.ROLE_OUTDOOR_TEMP: CONF_TEMP_ENTITIES,
}
ROLES = list(new.QUATT_KEYS)

CASES = [(r, c) for r in REGISTRIES for c in configs(REGISTRIES[r])]


@pytest.mark.parametrize("reg_name,cfg_name", CASES)
def test_anker_verandert_niets_bij_een_cic(reg_name, cfg_name, monkeypatch):
    reg = REGISTRIES[reg_name]
    cfg = configs(reg)[cfg_name]
    hass = hass_for(reg)

    def run():
        hubs = new.async_config_quatt_hubs(hass, cfg)
        discovered = new.async_discover_quatt_entities(hass, hubs)
        out = {"discovered": discovered}
        for role in ROLES:
            key = ROLE_CONF.get(role)
            configured = cfg.get(key) if key else None
            out[role] = (
                new.async_resolve_entity(hass, cfg, key, role),
                new.async_resolve_entity(hass, cfg, None, role),
                # De setting-resolver kent alleen enkelvoudige instellingen.
                None if key == CONF_TEMP_ENTITIES
                else new.async_resolve_setting_entity(hass, cfg, key, role),
                # Zoals coordinator en SourceRegistry hem aanroepen …
                new.async_resolve_candidates(
                    hass, configured, role, discovered=discovered, hubs=hubs
                ),
                # … en zonder hubs, waarbij hij zelf een anker afleidt.
                new.async_resolve_candidates(hass, configured, role),
            )
        return out

    met_anker = run()
    monkeypatch.setattr(new, "_async_anchor_hubs", lambda hass, ids: set())
    assert met_anker == run()


@pytest.mark.parametrize(
    "cfg",
    [
        {CONF_POWER_INPUT_ENTITY: "sensor.quatt_energy_consumption"},
        {CONF_POWER_ENTITY: "sensor.home_battery_power"},
        {
            CONF_POWER_INPUT_ENTITY: "sensor.quatt_energy_consumption",
            CONF_TEMP_ENTITIES: ["sensor.heatpump_1_temperature_outside"],
        },
    ],
)
def test_battery_of_energy_hub_is_geen_anker(cfg):
    """Een hub zonder CiC-metingen mag de detectie niet leegtrekken."""
    hass = hass_for(OTHER_HUBS + MODERN)
    hubs = new.async_config_quatt_hubs(hass, cfg)
    assert hubs <= {CIC}
    found = new.async_discover_quatt_entities(hass, hubs)
    assert found[new.ROLE_TOTAL_POWER] == "sensor.cic_total_power"
    assert new.async_resolve_entity(
        hass, cfg, None, new.ROLE_TOTAL_POWER
    ) == "sensor.cic_total_power"
