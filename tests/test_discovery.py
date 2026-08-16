"""Tests voor de auto-detectie van Quatt-entiteiten.

Achtergrond: de Quatt-integratie heeft het generieke ``Heatpump``-device
vervangen door losse devices. Installaties van vóór die migratie houden hun
oude entity-IDs, nieuwe installaties krijgen andere. Beide moeten werken.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.discovery import (
    ROLE_BOILER_HEAT,
    ROLE_CH_MAX_WATER,
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_RETURN_TEMP,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    async_discover_openquatt_entities,
    async_discover_quatt_entities,
    async_entity_exists,
    async_resolve_candidates,
    async_resolve_entity,
    async_resolve_from_list,
)

HUB = "CIC-abc123"
OQ_MAC = "58:E6:C5:6E:9D:78"


def _entry(entity_id, device_id, sensor_key, platform="quatt", disabled=False):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{HUB}:{device_id}:{sensor_key}",
        platform=platform,
        disabled=disabled,
    )


def _oq(entity_id, name, domain="sensor", mac=OQ_MAC, disabled=False):
    """ESPHome-registerregel zoals OpenQuatt die aanmaakt."""
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{mac}/0/{domain}/{name}",
        platform="esphome",
        disabled=disabled,
    )


def _hass(entries=(), states=None):
    """MagicMock-hass met een werkend entity-register en state machine."""
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(entries)
    known = dict(states or {})
    hass.states.get = lambda entity_id: known.get(entity_id)
    return hass


# Installatie ná de device-migratie: geen 'heatpump_'-prefix meer.
MODERN = [
    _entry("sensor.flowmeter_temperature", "flowmeter", "flowMeter.waterSupplyTemperature"),
    _entry("sensor.flowmeter_flowrate", "flowmeter", "qc.flowRateFiltered"),
    _entry("sensor.heatpump_1_temperature_water_in", "heatpump_1", "hp1.temperatureWaterIn"),
    _entry("sensor.heatpump_1_temperature_outside", "heatpump_1", "hp1.temperatureOutside"),
    _entry("sensor.cic_total_power", "cic", "computedPower"),
    _entry("sensor.cic_total_power_input", "cic", "computedPowerInput"),
    _entry("sensor.boiler_heat_power", "boiler", "boiler.computedBoilerHeatPower"),
    _entry("sensor.thermostat_room_temperature", "thermostat", "thermostat.otFtRoomTemperature"),
    _entry("number.cic_max_water_temperature", "cic", "chMaxWaterTemperature"),
]

# Installatie van vóór de migratie: alles onder één 'Heatpump'-device.
LEGACY = [
    _entry("sensor.heatpump_flowmeter_temperature", "flowmeter", "flowMeter.waterSupplyTemperature"),
    _entry("sensor.heatpump_flowmeter_flowrate", "flowmeter", "qc.flowRateFiltered"),
    _entry("sensor.heatpump_hp1_temperature_water_in", "heatpump_1", "hp1.temperatureWaterIn"),
    _entry("sensor.heatpump_hp1_temperature_outside", "heatpump_1", "hp1.temperatureOutside"),
    _entry("sensor.heatpump_total_power", "cic", "computedPower"),
    _entry("sensor.heatpump_total_power_input", "cic", "computedPowerInput"),
    _entry("sensor.heatpump_boiler_heat_power", "boiler", "boiler.computedBoilerHeatPower"),
    _entry("sensor.heatpump_thermostat_room_temperature", "thermostat", "thermostat.otFtRoomTemperature"),
    _entry("number.heatpump_cic_max_water_temperature", "cic", "chMaxWaterTemperature"),
]


class TestDiscovery:
    def test_moderne_naamgeving_volledig_gevonden(self):
        found = async_discover_quatt_entities(_hass(MODERN))
        assert found[ROLE_SUPPLY_TEMP] == "sensor.flowmeter_temperature"
        assert found[ROLE_FLOW_RATE] == "sensor.flowmeter_flowrate"
        assert found[ROLE_RETURN_TEMP] == "sensor.heatpump_1_temperature_water_in"
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.heatpump_1_temperature_outside"
        assert found[ROLE_TOTAL_POWER] == "sensor.cic_total_power"
        assert found[ROLE_POWER_INPUT] == "sensor.cic_total_power_input"
        assert found[ROLE_BOILER_HEAT] == "sensor.boiler_heat_power"
        assert found[ROLE_INDOOR_TEMP] == "sensor.thermostat_room_temperature"
        assert found[ROLE_CH_MAX_WATER] == "number.cic_max_water_temperature"

    def test_legacy_naamgeving_volledig_gevonden(self):
        found = async_discover_quatt_entities(_hass(LEGACY))
        assert found[ROLE_SUPPLY_TEMP] == "sensor.heatpump_flowmeter_temperature"
        assert found[ROLE_TOTAL_POWER] == "sensor.heatpump_total_power"
        assert found[ROLE_CH_MAX_WATER] == "number.heatpump_cic_max_water_temperature"
        assert len(found) == 9

    def test_zonder_quatt_integratie_leeg(self):
        assert async_discover_quatt_entities(_hass([])) == {}

    def test_andere_integratie_wordt_genegeerd(self):
        other = [_entry("sensor.iets", "flowmeter",
                        "flowMeter.waterSupplyTemperature", platform="daikin")]
        assert async_discover_quatt_entities(_hass(other)) == {}

    def test_uitgeschakelde_entity_telt_niet_mee(self):
        disabled = [_entry("sensor.flowmeter_temperature", "flowmeter",
                           "flowMeter.waterSupplyTemperature", disabled=True)]
        assert ROLE_SUPPLY_TEMP not in async_discover_quatt_entities(_hass(disabled))

    def test_hp1_gaat_voor_op_thermostaat_buitentemp(self):
        """Warmtepompsensor is nauwkeuriger dan de thermostaatwaarde."""
        entries = [
            _entry("sensor.thermostat_temperature_outside", "thermostat", "temperatureOutside"),
            _entry("sensor.heatpump_1_temperature_outside", "heatpump_1", "hp1.temperatureOutside"),
        ]
        found = async_discover_quatt_entities(_hass(entries))
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.heatpump_1_temperature_outside"

    def test_hp2_als_hp1_ontbreekt(self):
        entries = [_entry("sensor.heatpump_2_temperature_outside", "heatpump_2",
                          "hp2.temperatureOutside")]
        found = async_discover_quatt_entities(_hass(entries))
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.heatpump_2_temperature_outside"


# Zoals de HCQ Q-edition ze in het register zet.
OPENQUATT = [
    _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
    _oq("sensor.openquatt_water_supply_temp_selected", "Water Supply Temp (Selected)"),
    _oq("sensor.openquatt_flow_average_selected", "Flow average (Selected)"),
    _oq("sensor.openquatt_hp1_water_in_temperature", "HP1 - Water in temperature"),
    _oq("sensor.openquatt_outside_temperature_selected", "Outside Temperature (Selected)"),
    _oq("sensor.openquatt_total_heat_power", "Total Heat Power"),
    _oq("sensor.openquatt_total_power_input", "Total Power Input"),
    _oq("sensor.openquatt_boiler_heat_power", "Boiler Heat Power"),
    _oq("sensor.openquatt_room_temperature_selected", "Room Temperature (Selected)"),
    _oq("number.openquatt_maximum_water_temperature", "Maximum water temperature", "number"),
]


class TestOpenQuattDiscovery:
    def test_alle_rollen_gevonden(self):
        found = async_discover_openquatt_entities(_hass(OPENQUATT))
        assert found[ROLE_SUPPLY_TEMP] == "sensor.openquatt_water_supply_temp_selected"
        assert found[ROLE_FLOW_RATE] == "sensor.openquatt_flow_average_selected"
        assert found[ROLE_RETURN_TEMP] == "sensor.openquatt_hp1_water_in_temperature"
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.openquatt_outside_temperature_selected"
        assert found[ROLE_TOTAL_POWER] == "sensor.openquatt_total_heat_power"
        assert found[ROLE_POWER_INPUT] == "sensor.openquatt_total_power_input"
        assert found[ROLE_BOILER_HEAT] == "sensor.openquatt_boiler_heat_power"
        assert found[ROLE_INDOOR_TEMP] == "sensor.openquatt_room_temperature_selected"
        assert found[ROLE_CH_MAX_WATER] == "number.openquatt_maximum_water_temperature"

    def test_zonder_openquatt_leeg(self):
        assert async_discover_openquatt_entities(_hass(MODERN)) == {}

    def test_esphome_node_zonder_signature_telt_niet_mee(self):
        """Een andere ESPHome-node mag geen rollen leveren.

        ESPHome-entiteiten van álle nodes delen hetzelfde platform, dus zonder
        de signature-check zou een willekeurige node met een sensor die toevallig
        'Total Heat Power' heet als OpenQuatt worden aangezien.
        """
        vreemde_node = [
            _oq("sensor.andere_total_heat_power", "Total Heat Power", mac="AA:BB:CC:DD:EE:FF"),
        ]
        assert async_discover_openquatt_entities(_hass(vreemde_node)) == {}

    def test_alleen_de_openquatt_node_wint_bij_meerdere_nodes(self):
        entries = [
            *OPENQUATT,
            _oq("sensor.andere_total_heat_power", "Total Heat Power", mac="AA:BB:CC:DD:EE:FF"),
        ]
        found = async_discover_openquatt_entities(_hass(entries))
        assert found[ROLE_TOTAL_POWER] == "sensor.openquatt_total_heat_power"

    def test_uitgeschakelde_entity_telt_niet_mee(self):
        entries = [
            _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
            _oq("sensor.openquatt_total_heat_power", "Total Heat Power", disabled=True),
        ]
        assert ROLE_TOTAL_POWER not in async_discover_openquatt_entities(_hass(entries))

    def test_matcht_op_naam_niet_op_entity_id(self):
        """HA's entity-ID's zijn hier onbetrouwbaar, de ESPHome-naam niet.

        OpenQuatt heeft zowel 'Curve Tsupply @ -10°C' als '@ 10°C'; HA maakt daar
        `..._10degc` en `..._10degc_2` van. Zo'n botsing mag de rol niet kunnen
        laten omslaan — daarom wordt op naam gematcht.
        """
        entries = [
            _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
            # Verwarrend genummerde entity-ID's, in omgekeerde volgorde.
            _oq("sensor.openquatt_temp_selected_2", "Outside Temperature (Selected)"),
            _oq("sensor.openquatt_temp_selected", "HP1 - Outside temperature"),
        ]
        found = async_discover_openquatt_entities(_hass(entries))
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.openquatt_temp_selected_2"

    def test_valt_terug_op_hp1_als_selected_ontbreekt(self):
        entries = [
            _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
            _oq("sensor.openquatt_hp1_outside_temperature", "HP1 - Outside temperature"),
        ]
        found = async_discover_openquatt_entities(_hass(entries))
        assert found[ROLE_OUTDOOR_TEMP] == "sensor.openquatt_hp1_outside_temperature"


class TestResolveCandidates:
    def test_quatt_voor_openquatt(self):
        """Bestaande installaties houden dezelfde primaire bron."""
        got = async_resolve_candidates(_hass([*LEGACY, *OPENQUATT]), None, ROLE_TOTAL_POWER)
        assert got[0] == "sensor.heatpump_total_power"
        assert "sensor.openquatt_total_heat_power" in got

    def test_ingestelde_entity_staat_vooraan(self):
        hass = _hass([*MODERN, *OPENQUATT], states={"sensor.mijn_eigen": "1200"})
        got = async_resolve_candidates(hass, "sensor.mijn_eigen", ROLE_TOTAL_POWER)
        assert got[0] == "sensor.mijn_eigen"
        assert got[1] == "sensor.cic_total_power"

    def test_lijst_als_configuratie(self):
        """De buitentemperatuur wordt als voorkeurslijst geconfigureerd."""
        hass = _hass([*MODERN, *OPENQUATT])
        got = async_resolve_candidates(
            hass,
            ["sensor.bestaat_niet", "sensor.heatpump_1_temperature_outside"],
            ROLE_OUTDOOR_TEMP,
        )
        assert got[0] == "sensor.heatpump_1_temperature_outside"
        assert "sensor.bestaat_niet" not in got

    def test_geen_dubbelen(self):
        hass = _hass([*MODERN, *OPENQUATT])
        got = async_resolve_candidates(hass, "sensor.cic_total_power", ROLE_TOTAL_POWER)
        assert got.count("sensor.cic_total_power") == 1

    def test_alleen_openquatt_aanwezig(self):
        got = async_resolve_candidates(_hass(OPENQUATT), None, ROLE_SUPPLY_TEMP)
        assert got == ["sensor.openquatt_water_supply_temp_selected"]

    def test_niets_gevonden_geeft_lege_lijst(self):
        """Anders dan async_resolve_entity verzint dit geen naam die niet bestaat."""
        assert async_resolve_candidates(_hass([]), None, ROLE_SUPPLY_TEMP) == []

    def test_lege_string_telt_niet_als_configuratie(self):
        got = async_resolve_candidates(_hass(MODERN), "  ", ROLE_SUPPLY_TEMP)
        assert got == ["sensor.flowmeter_temperature"]


class TestResolve:
    def test_ingestelde_entity_wint_als_die_bestaat(self):
        hass = _hass(MODERN, states={"sensor.mijn_eigen": "21.0"})
        got = async_resolve_entity(
            hass, {"supply_temp_entity": "sensor.mijn_eigen"}, "supply_temp_entity",
            ROLE_SUPPLY_TEMP,
        )
        assert got == "sensor.mijn_eigen"

    def test_kapotte_instelling_valt_terug_op_detectie(self):
        """Het forumscenario: default wijst naar een entity die hier niet bestaat."""
        hass = _hass(MODERN)
        got = async_resolve_entity(
            hass,
            {"supply_temp_entity": "sensor.heatpump_flowmeter_temperature"},
            "supply_temp_entity",
            ROLE_SUPPLY_TEMP,
        )
        assert got == "sensor.flowmeter_temperature"

    def test_zonder_instelling_gebruikt_detectie(self):
        got = async_resolve_entity(_hass(MODERN), {}, "supply_temp_entity", ROLE_SUPPLY_TEMP)
        assert got == "sensor.flowmeter_temperature"

    def test_lege_string_telt_als_niet_ingesteld(self):
        got = async_resolve_entity(
            _hass(MODERN), {"supply_temp_entity": "  "}, "supply_temp_entity", ROLE_SUPPLY_TEMP
        )
        assert got == "sensor.flowmeter_temperature"

    def test_zonder_detectie_valt_terug_op_bestaande_naam(self):
        """Geen Quatt-integratie, maar de sensor bestaat wel in de state machine."""
        hass = _hass([], states={"sensor.flowmeter_temperature": "35.0"})
        got = async_resolve_entity(hass, {}, "supply_temp_entity", ROLE_SUPPLY_TEMP)
        assert got == "sensor.flowmeter_temperature"

    def test_niets_gevonden_geeft_eerste_terugvalnaam(self):
        """Levert altijd iets bruikbaars op, zodat de UI een waarde kan tonen."""
        got = async_resolve_entity(_hass([]), {}, "supply_temp_entity", ROLE_SUPPLY_TEMP)
        assert got == "sensor.heatpump_flowmeter_temperature"

    def test_entity_alleen_in_register_telt_als_bestaand(self):
        """Bij het opstarten is het register gevuld voordat de states dat zijn."""
        hass = _hass(MODERN)
        assert async_entity_exists(hass, "sensor.flowmeter_temperature") is True

    def test_entity_alleen_in_states_telt_als_bestaand(self):
        """YAML-template-sensoren zonder unique_id staan niet in het register."""
        hass = _hass([], states={"sensor.mijn_template": "3"})
        assert async_entity_exists(hass, "sensor.mijn_template") is True

    def test_onbekende_entity_bestaat_niet(self):
        assert async_entity_exists(_hass([]), "sensor.bestaat_niet") is False

    def test_lege_entity_id_bestaat_niet(self):
        assert async_entity_exists(_hass([]), "") is False


class TestResolveFromList:
    def test_eerste_bestaande_uit_de_lijst_wint(self):
        hass = _hass(MODERN)
        got = async_resolve_from_list(
            hass,
            ["sensor.bestaat_niet", "sensor.heatpump_1_temperature_outside"],
            ROLE_OUTDOOR_TEMP,
        )
        assert got == "sensor.heatpump_1_temperature_outside"

    def test_lege_lijst_valt_terug_op_detectie(self):
        got = async_resolve_from_list(_hass(MODERN), [], ROLE_OUTDOOR_TEMP)
        assert got == "sensor.heatpump_1_temperature_outside"

    def test_niets_bestaat_valt_terug_op_detectie(self):
        """Precies de forumsituatie: ingestelde lijst met legacy-namen."""
        got = async_resolve_from_list(
            _hass(MODERN),
            ["sensor.heatpump_hp1_temperature_outside", "sensor.heatpump_hp2_temperature_outside"],
            ROLE_OUTDOOR_TEMP,
        )
        assert got == "sensor.heatpump_1_temperature_outside"

    def test_none_lijst_is_toegestaan(self):
        got = async_resolve_from_list(_hass(MODERN), None, ROLE_OUTDOOR_TEMP)
        assert got == "sensor.heatpump_1_temperature_outside"


class TestGeenRegressieVoorBestaandeInstallaties:
    """Mark's eigen installatie moet exact dezelfde entities blijven gebruiken."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (ROLE_SUPPLY_TEMP, "sensor.heatpump_flowmeter_temperature"),
            (ROLE_FLOW_RATE, "sensor.heatpump_flowmeter_flowrate"),
            (ROLE_RETURN_TEMP, "sensor.heatpump_hp1_temperature_water_in"),
            (ROLE_OUTDOOR_TEMP, "sensor.heatpump_hp1_temperature_outside"),
            (ROLE_TOTAL_POWER, "sensor.heatpump_total_power"),
            (ROLE_POWER_INPUT, "sensor.heatpump_total_power_input"),
            (ROLE_BOILER_HEAT, "sensor.heatpump_boiler_heat_power"),
            (ROLE_INDOOR_TEMP, "sensor.heatpump_thermostat_room_temperature"),
            (ROLE_CH_MAX_WATER, "number.heatpump_cic_max_water_temperature"),
        ],
    )
    def test_legacy_installatie_ongewijzigd(self, role, expected):
        assert async_resolve_entity(_hass(LEGACY), {}, None, role) == expected
