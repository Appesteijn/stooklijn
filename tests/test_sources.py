"""Tests voor de bronlaag: wie levert welke meting.

De kern die hier bewaakt moet worden: een dashboard hangt aan een vast
entity-ID, dus de spiegel moet blijven kloppen terwijl de onderliggende bron
wisselt. En het moet aflèèsbaar zijn welke integratie levert, niet af te leiden.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.discovery import (
    ROLE_OUTDOOR_TEMP,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from custom_components.quatt_stooklijn.sources import (
    MIRROR_ROLES,
    SOURCE_OPENQUATT,
    SOURCE_OTHER,
    SOURCE_QUATT,
    SourceRegistry,
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
        discovered = {ROLE_SUPPLY_TEMP: "sensor.heatpump_flowmeter_temperature"}
        openquatt = {ROLE_SUPPLY_TEMP: "sensor.openquatt_water_supply_temp_selected"}

        assert classify_source(
            "sensor.heatpump_flowmeter_temperature", discovered, openquatt
        ) == SOURCE_QUATT
        assert classify_source(
            "sensor.openquatt_water_supply_temp_selected", discovered, openquatt
        ) == SOURCE_OPENQUATT

    def test_onbekende_bron_is_overig(self):
        """Een eigen template-sensor is een geldige bron, maar niet van beide."""
        assert classify_source("sensor.mijn_eigen", {}, {}) == SOURCE_OTHER

    def test_naam_is_geen_bewijs(self):
        """Entiteiten kunnen hernoemd zijn; de detectiekaart is leidend."""
        openquatt = {ROLE_SUPPLY_TEMP: "sensor.iets_heel_anders"}
        assert classify_source("sensor.openquatt_lijkt_erop", {}, openquatt) == SOURCE_OTHER
        assert classify_source("sensor.iets_heel_anders", {}, openquatt) == SOURCE_OPENQUATT


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
