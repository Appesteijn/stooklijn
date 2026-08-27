"""Tests voor de bronlaag: wie levert welke meting.

De kern die hier bewaakt moet worden: een dashboard hangt aan een vast
entity-ID, dus de spiegel moet blijven kloppen terwijl de onderliggende bron
wisselt. En het moet aflèèsbaar zijn welke integratie levert, niet af te leiden.
"""

import pathlib
import re
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.const import CONF_TEMP_ENTITIES, DOMAIN
from custom_components.quatt_stooklijn.discovery import (
    ROLE_OUTDOOR_TEMP,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from custom_components.quatt_stooklijn.sources import (
    MIRROR_ROLES,
    MIRROR_SPECS,
    OVERVIEW_SLUG,
    ROLE_CONF_KEYS,
    SOURCE_OPENQUATT,
    SOURCE_OTHER,
    SOURCE_QUATT,
    SourceRegistry,
    async_source_entity,
    classify_source,
    is_usable,
)

HUB = "CIC-abc123"
OQ_MAC = "58:E6:C5:6E:9D:78"


def _quatt(entity_id, device_id, sensor_key):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{HUB}:{device_id}:{sensor_key}",
        platform="quatt",
    )


def _oq(entity_id, name, domain="sensor"):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{OQ_MAC}/0/{domain}/{name}",
        platform="esphome",
    )


class _FakeState:
    def __init__(self, state):
        self.state = state


def _hass(entries=(), states=None):
    """Mock-hass met een werkend register en state machine.

    De meegegeven ``states``-dict wordt bewust niet gekopieerd: tests die een
    bron zien wegvallen of terugkomen muteren hem tussen twee evaluaties door.
    """
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(entries)
    known = states if states is not None else {}
    hass.states.get = lambda entity_id: (
        _FakeState(known[entity_id]) if entity_id in known else None
    )
    return hass


QUATT_ENTRIES = [
    _quatt("sensor.heatpump_flowmeter_temperature", "flowmeter",
           "flowMeter.waterSupplyTemperature"),
    _quatt("sensor.heatpump_total_power", "cic", "computedPower"),
    _quatt("sensor.heatpump_hp1_temperature_outside", "heatpump_1",
           "hp1.temperatureOutside"),
]

OQ_ENTRIES = [
    _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
    _oq("sensor.openquatt_water_supply_temp_selected", "Water Supply Temp (Selected)"),
    _oq("sensor.openquatt_total_heat_power", "Total Heat Power"),
    _oq("sensor.openquatt_outside_temperature_selected", "Outside Temperature (Selected)"),
]


class TestIsUsable:
    @pytest.mark.parametrize("value", ["unknown", "unavailable", "None", "", "kapot"])
    def test_onbruikbare_states(self, value):
        hass = _hass(states={"sensor.x": value})
        assert is_usable(hass, "sensor.x") is False

    def test_onbekende_entity(self):
        assert is_usable(_hass(), "sensor.bestaat_niet") is False

    @pytest.mark.parametrize("value", ["0", "0.0", "-3.5", "21.75"])
    def test_getallen_zijn_bruikbaar(self, value):
        """Nul is een geldige meting, geen 'geen data'."""
        hass = _hass(states={"sensor.x": value})
        assert is_usable(hass, "sensor.x") is True


class TestClassifySource:
    def test_herkent_beide_integraties(self):
        hass = _hass([*QUATT_ENTRIES, *OQ_ENTRIES])
        oq = {"sensor.openquatt_water_supply_temp_selected"}

        assert classify_source(
            hass, "sensor.heatpump_flowmeter_temperature", oq
        ) == SOURCE_QUATT
        assert classify_source(
            hass, "sensor.openquatt_water_supply_temp_selected", oq
        ) == SOURCE_OPENQUATT

    def test_onbekende_bron_is_overig(self):
        """Een eigen template-sensor is een geldige bron, maar niet van beide."""
        assert classify_source(_hass([]), "sensor.mijn_eigen", set()) == SOURCE_OTHER

    def test_naam_is_geen_bewijs(self):
        """Entiteiten kunnen hernoemd zijn; het register is leidend."""
        entries = [_quatt("sensor.openquatt_lijkt_erop", "cic", "computedPower")]
        assert classify_source(
            _hass(entries), "sensor.openquatt_lijkt_erop", set()
        ) == SOURCE_QUATT

    def test_quatt_sensor_die_geen_rol_won_is_geen_overig(self):
        """De reden dat dit niet meer op de rol-detectiekaart mag draaien.

        Discovery kiest per rol één entity. sensor.thermostat_temperature_outside
        verliest het van hp1, maar komt onmiskenbaar uit de Quatt-integratie —
        dat als "overig" tonen is misleidend.
        """
        entries = [
            *QUATT_ENTRIES,
            _quatt("sensor.thermostat_temperature_outside", "thermostat",
                   "temperatureOutside"),
        ]
        assert classify_source(
            _hass(entries), "sensor.thermostat_temperature_outside", set()
        ) == SOURCE_QUATT


class TestSourceRegistry:
    def test_kiest_quatt_als_beide_leveren(self):
        """Bestaande installaties houden hun primaire bron."""
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                "sensor.heatpump_flowmeter_temperature": "35.0",
                "sensor.openquatt_water_supply_temp_selected": "35.2",
            },
        )
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()

        source = registry.get(ROLE_SUPPLY_TEMP)
        assert source.active == "sensor.heatpump_flowmeter_temperature"
        assert source.integration == SOURCE_QUATT

    def test_valt_terug_op_openquatt_als_quatt_niets_levert(self):
        """Het scenario waar dit voor gebouwd is."""
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                "sensor.heatpump_flowmeter_temperature": "unknown",
                "sensor.openquatt_water_supply_temp_selected": "35.2",
            },
        )
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()

        source = registry.get(ROLE_SUPPLY_TEMP)
        assert source.active == "sensor.openquatt_water_supply_temp_selected"
        assert source.integration == SOURCE_OPENQUATT

    def test_schakelt_terug_zodra_de_voorkeursbron_herstelt(self):
        states = {
            "sensor.heatpump_flowmeter_temperature": "unavailable",
            "sensor.openquatt_water_supply_temp_selected": "35.2",
        }
        hass = _hass([*QUATT_ENTRIES, *OQ_ENTRIES], states=states)
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()
        assert registry.active_entity(ROLE_SUPPLY_TEMP) == (
            "sensor.openquatt_water_supply_temp_selected"
        )

        states["sensor.heatpump_flowmeter_temperature"] = "34.9"
        changed = registry.async_evaluate()

        assert ROLE_SUPPLY_TEMP in changed
        assert registry.active_entity(ROLE_SUPPLY_TEMP) == (
            "sensor.heatpump_flowmeter_temperature"
        )

    def test_geen_enkele_bron_geeft_none(self):
        hass = _hass(
            QUATT_ENTRIES,
            states={"sensor.heatpump_flowmeter_temperature": "unavailable"},
        )
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()

        source = registry.get(ROLE_SUPPLY_TEMP)
        assert source.active is None
        assert source.integration is None
        assert source.available is False

    def test_ongewijzigde_rol_staat_niet_in_changed(self):
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={"sensor.heatpump_total_power": "1200"},
        )
        registry = SourceRegistry(hass, {})
        first = registry.async_evaluate()
        second = registry.async_evaluate()

        assert ROLE_TOTAL_POWER in first
        assert second == []

    def test_ingestelde_entity_gaat_voor(self):
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                "sensor.mijn_eigen": "33.0",
                "sensor.heatpump_flowmeter_temperature": "35.0",
            },
        )
        registry = SourceRegistry(hass, {"supply_temp_entity": "sensor.mijn_eigen"})
        registry.async_evaluate()

        source = registry.get(ROLE_SUPPLY_TEMP)
        assert source.active == "sensor.mijn_eigen"
        assert source.integration == SOURCE_OTHER

    def test_buitentemp_gebruikt_de_ingestelde_lijst(self):
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                "sensor.mijn_weerstation": "12.0",
                "sensor.heatpump_hp1_temperature_outside": "11.5",
            },
        )
        registry = SourceRegistry(
            hass, {"temp_entities": ["sensor.mijn_weerstation"]}
        )
        registry.async_evaluate()
        assert registry.active_entity(ROLE_OUTDOOR_TEMP) == "sensor.mijn_weerstation"

    def test_summary_bevat_alle_rollen(self):
        hass = _hass([*QUATT_ENTRIES, *OQ_ENTRIES])
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()

        summary = registry.summary()
        assert set(summary) == set(MIRROR_ROLES)
        for info in summary.values():
            assert set(info) == {"entity", "integration", "candidates", "switched_at"}

    def test_integrations_in_use_heeft_vaste_volgorde(self):
        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                # Quatt levert het vermogen, OpenQuatt de aanvoertemperatuur.
                "sensor.heatpump_total_power": "1200",
                "sensor.heatpump_flowmeter_temperature": "unknown",
                "sensor.openquatt_water_supply_temp_selected": "35.2",
            },
        )
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()
        assert registry.integrations_in_use() == [SOURCE_QUATT, SOURCE_OPENQUATT]

    def test_zonder_bronnen_is_de_lijst_leeg(self):
        registry = SourceRegistry(_hass([]), {})
        registry.async_evaluate()
        assert registry.integrations_in_use() == []


class TestAsyncSourceEntity:
    """De helper waarmee de rest van de integratie zijn bron opvraagt."""

    def _hass_met_registry(self, entries, states, config=None):
        hass = _hass(entries, states=states)
        registry = SourceRegistry(hass, config or {})
        registry.async_evaluate()
        hass.data = {DOMAIN: {"entry1_sources": registry}}
        return hass

    def test_gebruikt_de_actieve_bron(self):
        """De kern: kiezen op beschikbaarheid, niet op bestaan.

        async_resolve_entity zou hier de Quatt-sensor teruggeven — die bestáát,
        hij geeft alleen 'unknown'. Dat is precies hoe het RC-model bleef hangen
        op een dode sensor terwijl OpenQuatt de waarde gewoon leverde.
        """
        hass = self._hass_met_registry(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            {
                "sensor.heatpump_flowmeter_temperature": "unknown",
                "sensor.openquatt_water_supply_temp_selected": "35.2",
            },
        )
        got = async_source_entity(hass, "entry1", ROLE_SUPPLY_TEMP)
        assert got == "sensor.openquatt_water_supply_temp_selected"

    def test_zonder_registry_valt_terug_op_de_losse_resolver(self):
        """Tijdens het opstarten kan een entiteit er eerder zijn dan de registry."""
        hass = _hass(QUATT_ENTRIES, states={})
        hass.data = {}
        got = async_source_entity(hass, "entry1", ROLE_SUPPLY_TEMP)
        assert got == "sensor.heatpump_flowmeter_temperature"

    def test_zonder_bruikbare_bron_valt_terug(self):
        """Niets levert iets: dan is een naam tonen beter dan None."""
        hass = self._hass_met_registry(
            QUATT_ENTRIES,
            {"sensor.heatpump_flowmeter_temperature": "unavailable"},
        )
        got = async_source_entity(hass, "entry1", ROLE_SUPPLY_TEMP)
        assert got == "sensor.heatpump_flowmeter_temperature"

    def test_volgt_een_bronwissel(self):
        states = {
            "sensor.heatpump_flowmeter_temperature": "35.0",
            "sensor.openquatt_water_supply_temp_selected": "35.2",
        }
        hass = self._hass_met_registry([*QUATT_ENTRIES, *OQ_ENTRIES], states)
        assert async_source_entity(hass, "entry1", ROLE_SUPPLY_TEMP) == (
            "sensor.heatpump_flowmeter_temperature"
        )

        states["sensor.heatpump_flowmeter_temperature"] = "unknown"
        hass.data[DOMAIN]["entry1_sources"].async_evaluate()

        assert async_source_entity(hass, "entry1", ROLE_SUPPLY_TEMP) == (
            "sensor.openquatt_water_supply_temp_selected"
        )


class TestEntityIdStabiliteit:
    """De entity-ID's van de spiegels zijn een publiek contract.

    Dashboards en automatiseringen hangen eraan. In v0.8.8 liet ik HA ze zelf
    genereren, en die bouwt hem op uit de area van het device — resultaat:
    sensor.bijkeuken_quatt_warmteanalyse_… en een dashboard dat nergens meer
    naar wees. Vandaar dat de slug nu vastligt in de spec, en deze test.
    """

    def test_slugs_zijn_uniek(self):
        slugs = [spec.slug for spec in MIRROR_SPECS] + [OVERVIEW_SLUG]
        assert len(slugs) == len(set(slugs))

    def test_slugs_zijn_bruikbare_object_ids(self):
        for slug in [spec.slug for spec in MIRROR_SPECS] + [OVERVIEW_SLUG]:
            assert slug
            assert slug == slug.lower()
            assert re.fullmatch(r"[a-z0-9_]+", slug), slug

    def test_elke_rol_heeft_precies_een_spiegel(self):
        assert len(MIRROR_SPECS) == len(MIRROR_ROLES) == len(set(MIRROR_ROLES))

    def test_dashboard_verwijst_niet_meer_naar_ruwe_meetsensoren(self):
        """Het dashboard hoort aan de spiegels te hangen, niet aan de bron.

        number.cic_max_water_temperature is de uitzondering: dat is een
        instelknop waar naartoe geschreven wordt, geen meting.
        """
        dashboard = (
            pathlib.Path(__file__).parent.parent
            / "custom_components/quatt_stooklijn/dashboard.yaml"
        ).read_text()

        verboden = re.findall(r"sensor\.heatpump_[a-z0-9_]+", dashboard)
        assert verboden == [], f"nog niet omgehangen: {sorted(set(verboden))}"

    def test_dashboard_gebruikt_bestaande_spiegels(self):
        """Elke quatt_warmteanalyse-spiegel in het dashboard moet ook bestaan."""
        dashboard = (
            pathlib.Path(__file__).parent.parent
            / "custom_components/quatt_stooklijn/dashboard.yaml"
        ).read_text()

        bekend = {spec.slug for spec in MIRROR_SPECS} | {OVERVIEW_SLUG}
        gebruikt = set(
            re.findall(r"sensor\.quatt_warmteanalyse_([a-z0-9_]+)", dashboard)
        )
        # Alleen de slugs die op een spiegel lijken controleren; het dashboard
        # verwijst ook naar analyse-sensoren met heel andere namen.
        for slug in gebruikt & {
            "aanvoertemperatuur", "retourtemperatuur", "buitentemperatuur",
            "kamertemperatuur", "thermostaat_setpoint", "kamer_setpoint",
            "debiet", "thermisch_vermogen", "opgenomen_vermogen",
            "ketelvermogen", "cop", "databronnen",
        }:
            assert slug in bekend


class TestRolConfiguratie:
    """Elke gespiegelde meting moet zelf te kiezen zijn.

    De kandidatenvolgorde zet Quatt bewust vóór OpenQuatt, zodat bestaande
    installaties niet van bron wisselen. Een ingestelde entity is de enige
    manier om daar vanaf te wijken — ontbreekt de config-sleutel voor een rol,
    dan zit de gebruiker onherroepelijk aan die volgorde vast.
    """

    def test_elke_spiegelrol_heeft_een_config_sleutel(self):
        assert set(ROLE_CONF_KEYS) == set(MIRROR_ROLES)

    def test_config_sleutels_zijn_uniek(self):
        sleutels = list(ROLE_CONF_KEYS.values())
        assert len(sleutels) == len(set(sleutels))

    @pytest.mark.parametrize("role", MIRROR_ROLES)
    def test_ingestelde_bron_wint_voor_elke_rol(self, role):
        """Ook als de Quatt-sensor voor die rol gewoon een waarde levert."""
        eigen = f"sensor.eigen_{role}"
        conf_key = ROLE_CONF_KEYS[role]
        # De buitentemperatuur is historisch een lijst; de rest een enkele ID.
        waarde = [eigen] if conf_key == CONF_TEMP_ENTITIES else eigen

        hass = _hass(
            [*QUATT_ENTRIES, *OQ_ENTRIES],
            states={
                eigen: "21.0",
                "sensor.heatpump_flowmeter_temperature": "35.0",
                "sensor.heatpump_hp1_temperature_outside": "11.5",
                "sensor.heatpump_total_power": "3000",
                "sensor.openquatt_water_supply_temp_selected": "35.5",
                "sensor.openquatt_total_heat_power": "3100",
                "sensor.openquatt_outside_temperature_selected": "11.4",
            },
        )
        registry = SourceRegistry(hass, {conf_key: waarde})
        registry.async_evaluate()

        assert registry.active_entity(role) == eigen
        assert registry.get(role).integration == SOURCE_OTHER
