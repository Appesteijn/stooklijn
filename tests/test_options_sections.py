"""Het optieformulier in secties met helpteksten (v0.10.7-beta.6).

Tot beta.5 was het één platte lijst van 45 velden zonder één helptekst:
metingen, knoppen, prijzen en analyse door elkaar. Bewaard wordt nog steeds
plat, zodat bestaande instellingen gewoon doorwerken.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn.config_flow import (
    OPTION_SECTIONS,
    QuattStooklijnOptionsFlow,
    _flatten_sections,
)
from custom_components.quatt_stooklijn.const import (
    CONF_CH_MAX_WATER_ENABLED,
    CONF_PERFORMANCE_BASELINE_DATE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MODE,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_SOURCE,
    CONF_SUPPLY_TEMP_ENTITY,
    PRICE_MODE_DYNAMIC,
)

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / "quatt_stooklijn"
LANG_FILES = [COMPONENT / "strings.json", *sorted((COMPONENT / "translations").glob("*.json"))]


def _run(coro):
    return asyncio.run(coro)


def _flow(options=None):
    entry = MagicMock()
    entry.data = {}
    entry.options = options or {}
    entry.entry_id = "e1"
    flow = QuattStooklijnOptionsFlow(entry)
    flow.hass = MagicMock()
    return flow


def _schema(options=None):
    return _run(_flow(options).async_step_init())["data_schema"]


class TestIndeling:
    def test_zes_secties_in_vaste_volgorde(self):
        assert list(_schema()) == list(OPTION_SECTIONS)

    def test_elk_veld_precies_een_keer_en_niets_in_het_vangnet(self):
        schema = _schema()
        gezien: list[str] = []
        for naam, sectie in schema.items():
            velden = list(sectie["section"])
            assert velden == [k for k in OPTION_SECTIONS[naam] if k in velden], naam
            assert set(velden) == set(OPTION_SECTIONS[naam]), naam
            gezien += velden
        assert len(gezien) == len(set(gezien))

    def test_geen_veld_dubbel_ingedeeld(self):
        alle = [k for keys in OPTION_SECTIONS.values() for k in keys]
        assert len(alle) == len(set(alle))


class TestInklappen:
    def test_knoppen_uit_dan_ingeklapt(self):
        schema = _schema()
        assert schema["max_aanvoertemperatuur"]["options"]["collapsed"] is True
        assert schema["soundslider"]["options"]["collapsed"] is True
        assert schema["huis"]["options"]["collapsed"] is False
        assert schema["metingen"]["options"]["collapsed"] is True

    def test_knop_aan_dan_open(self):
        schema = _schema({CONF_CH_MAX_WATER_ENABLED: True, CONF_SOUND_LEVEL_ENABLED: True})
        assert schema["max_aanvoertemperatuur"]["options"]["collapsed"] is False
        assert schema["soundslider"]["options"]["collapsed"] is False

    def test_prijzen_open_als_er_een_tarief_is(self):
        schema = _schema({CONF_PRICE_MODE: PRICE_MODE_DYNAMIC, CONF_PRICE_ENTITY: "sensor.p"})
        assert schema["prijsverschuiving"]["options"]["collapsed"] is False


class TestOpslaan:
    def test_secties_worden_plat_opgeslagen(self):
        result = _run(_flow().async_step_init({
            "soundslider": {CONF_SOUND_LEVEL_ENABLED: True, CONF_SOUND_LEVEL_SOURCE: "1r1c"},
            "metingen": {CONF_SUPPLY_TEMP_ENTITY: "sensor.aanvoer"},
        }))
        assert result["data"][CONF_SOUND_LEVEL_ENABLED] is True
        assert result["data"][CONF_SOUND_LEVEL_SOURCE] == "1r1c"
        assert result["data"][CONF_SUPPLY_TEMP_ENTITY] == "sensor.aanvoer"
        assert "soundslider" not in result["data"]

    def test_platte_invoer_werkt_ook(self):
        assert _flatten_sections({CONF_SUPPLY_TEMP_ENTITY: "x"}) == {CONF_SUPPLY_TEMP_ENTITY: "x"}

    def test_ongeldige_datum_bovenaan(self):
        result = _run(_flow().async_step_init(
            {"analyse": {CONF_PERFORMANCE_BASELINE_DATE: "gisteren"}}
        ))
        assert result["errors"] == {"base": "invalid_date_format"}

    def test_dynamisch_zonder_prijssensor_bovenaan(self):
        result = _run(_flow().async_step_init(
            {"prijsverschuiving": {CONF_PRICE_MODE: PRICE_MODE_DYNAMIC}}
        ))
        assert result["errors"] == {"base": "price_entity_required"}

    def test_bij_een_fout_blijft_de_invoer_staan(self):
        """Het formulier komt terug met wat de gebruiker invulde, niet het opgeslagene."""
        result = _run(_flow().async_step_init({
            "analyse": {CONF_PERFORMANCE_BASELINE_DATE: "gisteren"},
            "soundslider": {CONF_SOUND_LEVEL_ENABLED: True},
        }))
        assert result["data_schema"]["soundslider"]["options"]["collapsed"] is False


class TestVertalingen:
    @pytest.mark.parametrize("path", LANG_FILES, ids=lambda p: p.name)
    def test_elke_sectie_heeft_naam_omschrijving_en_teksten_per_veld(self, path):
        init = json.loads(path.read_text())["options"]["step"]["init"]
        assert set(init["sections"]) == set(OPTION_SECTIONS)
        for naam, keys in OPTION_SECTIONS.items():
            sec = init["sections"][naam]
            assert sec["name"] and sec["description"], (path.name, naam)
            assert set(sec["data"]) == set(keys), (path.name, naam)
            assert set(sec["data_description"]) == set(keys), (path.name, naam)
            assert all(sec["data"].values()) and all(sec["data_description"].values())

    @pytest.mark.parametrize("path", LANG_FILES, ids=lambda p: p.name)
    def test_geen_resten_van_otgw(self, path):
        assert "otgw" not in path.read_text().lower()

    @pytest.mark.parametrize("path", LANG_FILES, ids=lambda p: p.name)
    def test_eerste_configuratie_heeft_helpteksten(self, path):
        opt = json.loads(path.read_text())["config"]["step"]["options"]
        assert set(opt["data_description"]) == set(opt["data"])
