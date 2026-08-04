"""Tests for config flow validation logic.

De klassen onderaan draaien de echte flow-stappen; de klassen hierboven
reproduceren losse validatielogica en dateren van vóór dat dat kon.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.config_flow import (
    QuattStooklijnConfigFlow,
    QuattStooklijnOptionsFlow,
)
from custom_components.quatt_stooklijn.const import (
    CONF_BOILER_HEAT_ENTITY,
    CONF_CH_MAX_WATER_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_POWER_INPUT_ENTITY,
    CONF_QUATT_START_DATE,
    CONF_QUATT_END_DATE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_POWER_ENTITY,
    CONF_GAS_ENABLED,
    CONF_GAS_ENTITY,
    CONF_GAS_START_DATE,
    CONF_GAS_END_DATE,
    CONF_WEATHER_ENTITY,
)


class TestDateValidation:
    """Test date validation logic used in config flow steps."""

    @staticmethod
    def validate_dates(start_str: str, end_str: str) -> str | None:
        """Reproduce date validation from async_step_user / async_step_gas."""
        try:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            if start >= end:
                return "invalid_date_range"
        except ValueError:
            return "invalid_date_format"
        return None

    def test_valid_dates(self):
        assert self.validate_dates("2024-01-01", "2024-06-30") is None

    def test_end_before_start(self):
        assert self.validate_dates("2024-06-30", "2024-01-01") == "invalid_date_range"

    def test_same_dates(self):
        assert self.validate_dates("2024-01-01", "2024-01-01") == "invalid_date_range"

    def test_invalid_format(self):
        assert self.validate_dates("not-a-date", "2024-01-01") == "invalid_date_format"

    def test_invalid_end_format(self):
        assert self.validate_dates("2024-01-01", "01/01/2024") == "invalid_date_format"

    def test_empty_strings(self):
        assert self.validate_dates("", "") == "invalid_date_format"


class TestTempEntityParsing:
    """Test comma-separated entity parsing."""

    @staticmethod
    def parse_temp_entities(raw: str) -> list[str]:
        """Reproduce parsing from async_step_user."""
        return [e.strip() for e in raw.split(",") if e.strip()]

    def test_single_entity(self):
        result = self.parse_temp_entities("sensor.temp_outside")
        assert result == ["sensor.temp_outside"]

    def test_multiple_entities(self):
        result = self.parse_temp_entities(
            "sensor.hp1_temp, sensor.hp2_temp, sensor.thermostat_temp"
        )
        assert result == ["sensor.hp1_temp", "sensor.hp2_temp", "sensor.thermostat_temp"]

    def test_extra_whitespace(self):
        result = self.parse_temp_entities("  sensor.a ,  sensor.b  ")
        assert result == ["sensor.a", "sensor.b"]

    def test_empty_string(self):
        result = self.parse_temp_entities("")
        assert result == []

    def test_trailing_comma(self):
        result = self.parse_temp_entities("sensor.a, sensor.b,")
        assert result == ["sensor.a", "sensor.b"]


class TestGasStepValidation:
    """Test gas step validation."""

    @staticmethod
    def validate_gas_input(user_input: dict) -> str | None:
        """Reproduce gas validation from async_step_gas."""
        gas_enabled = user_input.get(CONF_GAS_ENABLED, False)
        if not gas_enabled:
            return None

        gas_entity = user_input.get(CONF_GAS_ENTITY, "")
        if not gas_entity:
            return "gas_entity_required"

        gas_start = user_input.get(CONF_GAS_START_DATE, "")
        gas_end = user_input.get(CONF_GAS_END_DATE, "")

        try:
            s = date.fromisoformat(gas_start)
            e = date.fromisoformat(gas_end)
            if s >= e:
                return "invalid_date_range"
        except ValueError:
            return "invalid_date_format"

        return None

    def test_gas_disabled(self):
        assert self.validate_gas_input({CONF_GAS_ENABLED: False}) is None

    def test_gas_enabled_valid(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "sensor.gas_meter",
            CONF_GAS_START_DATE: "2023-01-01",
            CONF_GAS_END_DATE: "2023-12-31",
        })
        assert result is None

    def test_gas_enabled_missing_entity(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "",
            CONF_GAS_START_DATE: "2023-01-01",
            CONF_GAS_END_DATE: "2023-12-31",
        })
        assert result == "gas_entity_required"

    def test_gas_enabled_bad_dates(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "sensor.gas_meter",
            CONF_GAS_START_DATE: "2023-12-31",
            CONF_GAS_END_DATE: "2023-01-01",
        })
        assert result == "invalid_date_range"


# ---------------------------------------------------------------------------
# Echte flow-stappen
#
# Twee dingen die eerder misgingen bij nieuwe gebruikers:
# 1. Entity-velden waren vrije tekstvelden met defaults uit één specifieke
#    Quatt-naamgeving — een niet-bestaande naam werd zonder foutmelding bewaard.
# 2. Stap 3 toonde die velden wél, maar sloeg alleen de geluidsinstellingen op,
#    dus wie ze correct invulde raakte ze alsnog kwijt.
# ---------------------------------------------------------------------------

_HUB = "CIC-abc123"


def _reg_entry(entity_id, device_id, sensor_key):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{_HUB}:{device_id}:{sensor_key}",
        platform="quatt",
    )


# Installatie ná de device-migratie van de Quatt-integratie.
_MODERN = [
    _reg_entry("sensor.flowmeter_temperature", "flowmeter", "flowMeter.waterSupplyTemperature"),
    _reg_entry("sensor.flowmeter_flowrate", "flowmeter", "qc.flowRateFiltered"),
    _reg_entry("sensor.heatpump_1_temperature_water_in", "heatpump_1", "hp1.temperatureWaterIn"),
    _reg_entry("sensor.heatpump_1_temperature_outside", "heatpump_1", "hp1.temperatureOutside"),
    _reg_entry("sensor.cic_total_power", "cic", "computedPower"),
    _reg_entry("sensor.cic_total_power_input", "cic", "computedPowerInput"),
    _reg_entry("sensor.boiler_heat_power", "boiler", "boiler.computedBoilerHeatPower"),
    _reg_entry("sensor.thermostat_room_temperature", "thermostat", "thermostat.otFtRoomTemperature"),
]


def _hass(entries=_MODERN):
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(entries)
    hass.states.get = lambda entity_id: None
    return hass


def _run(coro):
    return asyncio.run(coro)


def _flow(entries=_MODERN):
    flow = QuattStooklijnConfigFlow()
    flow.hass = _hass(entries)
    return flow


class TestSetupStapEen:
    def test_formulier_biedt_entity_kiezers(self):
        """Geen vrij tekstveld meer, maar een kiezer uit bestaande entities."""
        schema = _run(_flow().async_step_user())["data_schema"]
        assert schema[CONF_POWER_ENTITY]["selector"] == "entity"
        assert schema[CONF_TEMP_ENTITIES]["config"]["multiple"] is True

    def test_lijst_wordt_als_lijst_bewaard(self):
        flow = _flow()
        _run(flow.async_step_user({
            CONF_QUATT_START_DATE: "2024-01-01",
            CONF_TEMP_ENTITIES: ["sensor.a", "sensor.b"],
            CONF_POWER_ENTITY: "sensor.cic_total_power",
        }))
        assert flow._data[CONF_TEMP_ENTITIES] == ["sensor.a", "sensor.b"]

    def test_komma_string_blijft_werken(self):
        """Terugvalpad voor bestaande of geïmporteerde configuratie."""
        flow = _flow()
        _run(flow.async_step_user({
            CONF_QUATT_START_DATE: "2024-01-01",
            CONF_TEMP_ENTITIES: "sensor.a, sensor.b",
            CONF_POWER_ENTITY: "sensor.cic_total_power",
        }))
        assert flow._data[CONF_TEMP_ENTITIES] == ["sensor.a", "sensor.b"]

    def test_ongeldige_datum_geeft_fout(self):
        result = _run(_flow().async_step_user({
            CONF_QUATT_START_DATE: "geen-datum",
            CONF_TEMP_ENTITIES: [],
            CONF_POWER_ENTITY: "sensor.cic_total_power",
        }))
        assert result["errors"]["base"] == "invalid_date_format"


class TestSetupStapDrie:
    def test_entity_velden_worden_bewaard(self):
        """Regressietest: deze velden verdwenen eerder stilzwijgend."""
        flow = _flow()
        flow._data = {CONF_QUATT_START_DATE: "2024-01-01"}
        data = _run(flow.async_step_options({
            CONF_FLOW_ENTITY: "sensor.flowmeter_flowrate",
            CONF_RETURN_TEMP_ENTITY: "sensor.heatpump_1_temperature_water_in",
            CONF_SUPPLY_TEMP_ENTITY: "sensor.flowmeter_temperature",
            CONF_INDOOR_TEMP_ENTITY: "sensor.thermostat_room_temperature",
        }))["data"]
        assert data[CONF_FLOW_ENTITY] == "sensor.flowmeter_flowrate"
        assert data[CONF_RETURN_TEMP_ENTITY] == "sensor.heatpump_1_temperature_water_in"
        assert data[CONF_SUPPLY_TEMP_ENTITY] == "sensor.flowmeter_temperature"
        assert data[CONF_INDOOR_TEMP_ENTITY] == "sensor.thermostat_room_temperature"

    def test_geluidsinstellingen_houden_hun_default(self):
        flow = _flow()
        flow._data = {CONF_QUATT_START_DATE: "2024-01-01"}
        result = _run(flow.async_step_options({}))
        assert result["data"][CONF_SOUND_LEVEL_ENABLED] is False

    def test_aanvoertemperatuur_is_instelbaar(self):
        """Stond hardcoded en was daardoor onbereikbaar bij andere naamgeving."""
        assert CONF_SUPPLY_TEMP_ENTITY in _run(_flow().async_step_options())["data_schema"]


class TestOptiescherm:
    def _options(self, data=None, entries=_MODERN):
        entry = MagicMock()
        entry.data = data or {}
        entry.options = {}
        flow = QuattStooklijnOptionsFlow(entry)
        flow.hass = _hass(entries)
        return flow

    @pytest.mark.parametrize("key", [
        CONF_SUPPLY_TEMP_ENTITY,
        CONF_POWER_INPUT_ENTITY,
        CONF_BOILER_HEAT_ENTITY,
        CONF_POWER_ENTITY,
        CONF_TEMP_ENTITIES,
    ])
    def test_voorheen_onbereikbare_velden_zijn_instelbaar(self, key):
        assert key in _run(self._options().async_step_init())["data_schema"]

    def test_ingevulde_waarde_wordt_bewaard(self):
        result = _run(self._options().async_step_init(
            {CONF_SUPPLY_TEMP_ENTITY: "sensor.flowmeter_temperature"}
        ))
        assert result["data"][CONF_SUPPLY_TEMP_ENTITY] == "sensor.flowmeter_temperature"

    def test_weather_veld_filtert_op_weather_domein(self):
        schema = _run(self._options().async_step_init())["data_schema"]
        assert schema[CONF_WEATHER_ENTITY]["config"]["domain"] == "weather"

    def test_ch_max_water_veld_filtert_op_number_domein(self):
        schema = _run(self._options().async_step_init())["data_schema"]
        assert schema[CONF_CH_MAX_WATER_ENTITY]["config"]["domain"] == "number"
