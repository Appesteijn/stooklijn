"""Welk model een knop aanstuurt (v0.10.7-beta.5).

Elke knop die de Quatt beïnvloedt (max-water-plafond, geluidsniveau-
compensatie) kiest zijn advies uit dezelfde lijst. Tot beta.4 had alleen het
plafond die keuze; de geluidscompensatie volgde altijd MPC.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from custom_components.quatt_stooklijn import advice_sources as bron
from custom_components.quatt_stooklijn.ch_max_water import ChMaxWaterController
from custom_components.quatt_stooklijn.config_flow import (
    QuattStooklijnConfigFlow,
    QuattStooklijnOptionsFlow,
)
from custom_components.quatt_stooklijn.const import (
    CONF_CH_MAX_WATER_SOURCE,
    CONF_QUATT_START_DATE,
    CONF_SOUND_LEVEL_SOURCE,
    DEFAULT_CH_MAX_WATER_SOURCE,
    DEFAULT_SOUND_LEVEL_SOURCE,
    MPC_ADVICE_NO_DEMAND,
    MPC_ADVICE_NO_INPUT,
    MPC_ADVICE_OK,
    MPC_ADVICE_STATUS_ATTR,
)
from custom_components.quatt_stooklijn.sensor import QuattSupplyTempSensor
from tests.test_room_setpoint import (
    FLOW,
    INDOOR,
    OUTDOOR,
    RETURN,
    SETPOINT,
    _analysis,
    _hass as _hass_states,
    _sources,
)

ENT = {
    "mpc_recommended_supply_temp": "sensor.mijn_mpc",
    "recommended_supply_temp": "sensor.mijn_stooklijn",
    "rc_recommended_supply_temp": "sensor.mijn_1r1c",
}


def _resolver():
    return patch(
        "custom_components.quatt_stooklijn.advice_sources.resolve_own_entity_id",
        side_effect=lambda hass, platform, entry_id, suffix: ENT[suffix],
    )


class _State:
    def __init__(self, value, **attrs):
        self.state = value
        self.attributes = attrs


def _hass(states):
    hass = MagicMock()
    hass.states.get = lambda entity_id: states.get(entity_id)
    return hass


class TestBronnenlijst:
    def test_drie_bronnen_met_bestaande_waarden(self):
        """'stooklijn' en 'mpc' blijven: bestaande instellingen werken door."""
        assert bron.ADVICE_SOURCE_OPTIONS == ["stooklijn", "mpc", "1r1c"]

    @pytest.mark.parametrize("waarde,verwacht", [
        ("stooklijn", "stooklijn"), ("mpc", "mpc"), ("2r2c", "mpc"), (None, "mpc"),
    ])
    def test_onbekende_bron_valt_terug(self, waarde, verwacht):
        assert bron.valid_source(waarde, "mpc") == verwacht

    def test_standaarden(self):
        """Sinds v0.10.7-beta.9 beginnen beide knoppen op "Buitentemperatuur +
        kamer" (ontwerp 22-11-2026). Een installatie die de max.
        aanvoertemperatuur al aan had zonder opgeslagen bron, houdt
        "Buitentemperatuur" (zie TestBronVastzetten)."""
        from custom_components.quatt_stooklijn.const import LEGACY_CH_MAX_WATER_SOURCE
        assert DEFAULT_CH_MAX_WATER_SOURCE == "mpc"
        assert DEFAULT_SOUND_LEVEL_SOURCE == "mpc"
        assert LEGACY_CH_MAX_WATER_SOURCE == "stooklijn"


class TestLezen:
    def test_via_unique_id(self):
        hass = _hass({"sensor.mijn_stooklijn": _State("31.5", advies_status="advies")})
        with _resolver():
            assert bron.advice_entity_id(hass, "e1", "stooklijn") == "sensor.mijn_stooklijn"
            assert bron.read_advice(hass, "e1", "stooklijn") == (31.5, "advies")

    def test_terugval_op_vaste_slug_zonder_registry(self):
        with patch(
            "custom_components.quatt_stooklijn.advice_sources.resolve_own_entity_id",
            return_value=None,
        ):
            assert bron.advice_entity_id(MagicMock(), "e1", "mpc") == (
                "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur"
            )
        assert bron.advice_entity_id(None, None, "stooklijn") == (
            "sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur"
        )

    def test_geen_vraag_zonder_waarde(self):
        hass = _hass({"sensor.mijn_mpc": _State("unknown", advies_status="geen_vraag")})
        with _resolver():
            assert bron.read_advice(hass, "e1", "mpc") == (None, "geen_vraag")

    def test_unavailable_zegt_niets(self):
        hass = _hass({"sensor.mijn_mpc": _State("unavailable", advies_status="geen_vraag")})
        with _resolver():
            assert bron.read_advice(hass, "e1", "mpc") == (None, None)

    def test_ontbrekende_sensor(self):
        with _resolver():
            assert bron.read_advice(_hass({}), "e1", "mpc") == (None, None)


class TestPlafondKiestBron:
    def _controller(self, source, entry_id="e1"):
        hass = _hass({
            "sensor.mijn_mpc": _State("34.0", advies_status="advies"),
            "sensor.mijn_stooklijn": _State("31.0", advies_status="advies"),
        })
        return ChMaxWaterController(
            hass=hass, number_entity="", source=source,
            hysteresis=1.0, interval_minutes=15, entry_id=entry_id,
        )

    @pytest.mark.parametrize("source,waarde", [("stooklijn", 31.0), ("mpc", 34.0)])
    def test_leest_de_gekozen_bron(self, source, waarde):
        c = self._controller(source)
        with _resolver():
            assert c._read_recommended() == waarde
            assert c.source_entity == ENT[
                "mpc_recommended_supply_temp" if source == "mpc" else "recommended_supply_temp"
            ]

    def test_onbekende_bron_wordt_de_standaard(self):
        c = self._controller("2r2c")
        with _resolver():
            assert c._read_recommended() == 34.0


def _run(coro):
    return asyncio.run(coro)


class TestOpties:
    def _options(self, data=None):
        entry = MagicMock()
        entry.data = data or {}
        entry.options = {}
        flow = QuattStooklijnOptionsFlow(entry)
        flow.hass = MagicMock()
        return flow

    @pytest.mark.parametrize("key", [CONF_CH_MAX_WATER_SOURCE, CONF_SOUND_LEVEL_SOURCE])
    def test_beide_knoppen_dezelfde_keuzelijst(self, key):
        from tests.test_config_flow import _velden
        schema = _velden(_run(self._options().async_step_init())["data_schema"])
        assert schema[key]["selector"] == "select"
        assert schema[key]["config"]["translation_key"] == "advice_source"
        assert schema[key]["config"]["options"] == ["stooklijn", "mpc", "1r1c"]

    def test_eerste_configuratie_vraagt_de_geluidsbron(self):
        flow = QuattStooklijnConfigFlow()
        flow.hass = MagicMock()
        flow._data = {CONF_QUATT_START_DATE: "2024-01-01"}
        schema = _run(flow.async_step_options())["data_schema"]
        assert schema[CONF_SOUND_LEVEL_SOURCE]["selector"] == "select"

    def test_eerste_configuratie_bewaart_de_standaard(self):
        flow = QuattStooklijnConfigFlow()
        flow.hass = MagicMock()
        flow._data = {CONF_QUATT_START_DATE: "2024-01-01"}
        result = _run(flow.async_step_options({}))
        assert result["data"][CONF_SOUND_LEVEL_SOURCE] == "mpc"


class TestStooklijnStatus:
    """Het stooklijnadvies zegt nu ook waarom er geen advies is, zoals MPC."""

    def _attrs(self, analyse=True, emitter=None, **states):
        sensor = QuattSupplyTempSensor.__new__(QuattSupplyTempSensor)
        sensor.hass = _hass_states(**states)
        sensor._entry = MagicMock(entry_id="e1", data={}, options={})
        sensor.coordinator = MagicMock(data=_analysis() if analyse else None,
                                       emitter=emitter)
        with _sources():
            return sensor.extra_state_attributes, sensor.native_value

    def test_advies(self):
        attrs, waarde = self._attrs(**{OUTDOOR: 5.0, RETURN: 28.0, FLOW: 800})
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_OK
        assert waarde is not None

    def test_boven_het_balanspunt_geen_vraag(self):
        attrs, waarde = self._attrs(**{OUTDOOR: 18.0, RETURN: 28.0, FLOW: 800})
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_NO_DEMAND
        assert waarde is None

    def test_ontbrekende_retour_is_geen_invoer(self):
        attrs, waarde = self._attrs(**{OUTDOOR: 5.0, FLOW: 800})
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_NO_INPUT
        assert waarde is None

    def test_stilstaande_pomp_rekent_met_het_afgiftemodel(self):
        """Review 22-11: deze bron rekende met afgekoeld retourwater."""
        from custom_components.quatt_stooklijn.analysis.emitter import EmitterModel
        m = EmitterModel(k=200.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        attrs, waarde = self._attrs(emitter=m, **{OUTDOOR: 5.0, RETURN: 15.0,
                                                  FLOW: 0, INDOOR: 20.5})
        assert attrs["aanvoer_uit"] == "afgiftemodel"
        _, zonder = self._attrs(**{OUTDOOR: 5.0, RETURN: 15.0, FLOW: 0})
        assert waarde > zonder

    def test_stilstaande_pomp_heeft_de_retour_niet_nodig(self):
        from custom_components.quatt_stooklijn.analysis.emitter import EmitterModel
        m = EmitterModel(k=200.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        attrs, waarde = self._attrs(emitter=m, **{OUTDOOR: 5.0, FLOW: 0, INDOOR: 20.5})
        assert waarde is not None
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_OK

    def test_zonder_kamersensor_het_setpoint(self):
        """Tweede review 22-11: zonder kamersensor rekende deze bron met een
        vaste 20 °C, de andere bronnen met het kamerdoel."""
        from custom_components.quatt_stooklijn.analysis.emitter import EmitterModel
        m = EmitterModel(k=200.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        _, warm = self._attrs(emitter=m, **{OUTDOOR: 5.0, RETURN: 15.0, FLOW: 0,
                                            SETPOINT: 21.5})
        _, gewoon = self._attrs(emitter=m, **{OUTDOOR: 5.0, RETURN: 15.0, FLOW: 0,
                                              SETPOINT: 20.0})
        assert warm == pytest.approx(gewoon + 1.5, abs=0.11)

    def test_draaiende_pomp_houdt_de_retour(self):
        from custom_components.quatt_stooklijn.analysis.emitter import EmitterModel
        m = EmitterModel(k=200.0, n=1.3, flow_lph=800.0, hours=100, mae_k=0.3)
        met = self._attrs(emitter=m, **{OUTDOOR: 5.0, RETURN: 28.0, FLOW: 800})
        zonder = self._attrs(**{OUTDOOR: 5.0, RETURN: 28.0, FLOW: 800})
        assert met[0]["aanvoer_uit"] == "retour"
        assert met[1] == zonder[1]

    def test_zonder_analyse_geen_invoer(self):
        attrs, _ = self._attrs(analyse=False, **{OUTDOOR: 5.0, RETURN: 28.0, FLOW: 800})
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_NO_INPUT



class TestNaNAdvies:
    """``read_advice`` liet "nan" door. In het waterplafond werd dat
    min(max, nan) = het maximum van de knop: 70 °C (analyse 22-11-2026)."""

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_niet_eindig_advies_is_geen_advies(self, raw):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from custom_components.quatt_stooklijn.advice_sources import read_advice
        hass = MagicMock()
        hass.states.get = lambda e: SimpleNamespace(state=raw, attributes={})
        assert read_advice(hass, None, "mpc")[0] is None


class TestBronVastzetten:
    """De standaardbron van de max. aanvoertemperatuur werd "Buitentemperatuur +
    kamer". Een installatie die de knop al aan had zonder opgeslagen bron, mag
    na de update niet stil op een andere bron gaan sturen."""

    def _entry(self, data=None, options=None):
        from unittest.mock import MagicMock
        entry = MagicMock()
        entry.data = data or {}
        entry.options = options or {}
        return entry

    def _pin(self, entry):
        from unittest.mock import MagicMock
        from custom_components.quatt_stooklijn import _async_pin_ch_max_water_source
        hass = MagicMock()
        _async_pin_ch_max_water_source(hass, entry)
        return hass.config_entries.async_update_entry

    def test_aan_zonder_bron_houdt_buitentemperatuur(self):
        update = self._pin(self._entry(options={"ch_max_water_enabled": True, "x": 1}))
        update.assert_called_once()
        assert update.call_args.kwargs["options"] == {
            "ch_max_water_enabled": True, "x": 1, "ch_max_water_source": "stooklijn"}

    @pytest.mark.parametrize("data,options", [
        ({}, {"ch_max_water_enabled": True, "ch_max_water_source": "mpc"}),
        ({"ch_max_water_source": "1r1c"}, {"ch_max_water_enabled": True}),
        ({}, {"ch_max_water_enabled": False}),
        ({}, {}),
    ])
    def test_verder_niets(self, data, options):
        self._pin(self._entry(data, options)).assert_not_called()
