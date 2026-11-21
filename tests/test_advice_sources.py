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
    OUTDOOR,
    RETURN,
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

    def test_standaarden_veranderen_niets(self):
        """Plafond bleef 'stooklijn', geluid volgde altijd MPC."""
        assert DEFAULT_CH_MAX_WATER_SOURCE == "stooklijn"
        assert DEFAULT_SOUND_LEVEL_SOURCE == "mpc"


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
            assert c._read_recommended() == 31.0


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
        schema = _run(self._options().async_step_init())["data_schema"]
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

    def _attrs(self, analyse=True, **states):
        sensor = QuattSupplyTempSensor.__new__(QuattSupplyTempSensor)
        sensor.hass = _hass_states(**states)
        sensor._entry = MagicMock(entry_id="e1", data={}, options={})
        sensor.coordinator = MagicMock(data=_analysis() if analyse else None)
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

    def test_zonder_analyse_geen_invoer(self):
        attrs, _ = self._attrs(analyse=False, **{OUTDOOR: 5.0, RETURN: 28.0, FLOW: 800})
        assert attrs[MPC_ADVICE_STATUS_ATTR] == MPC_ADVICE_NO_INPUT
