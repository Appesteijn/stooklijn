"""Tests voor de twee poorten rond het aanvoeradvies.

Beide dekken een fout af die je pas maanden later ziet, en dan in de
langetermijnstatistiek waar hij niet meer uit te halen is:

- De foutsensor poortte alleen op debiet. Buiten het stookseizoen circuleert de
  pomp met 0 W productie, terwijl de adviessensor op een nacht onder het
  balanspunt wél een getal geeft. Het verschil daartussen werd als voorspelfout
  weggeschreven en trok de maandgemiddelden van juni t/m september richting
  −14 °C.
- De adviessensor had geen boven- en ondergrens, anders dan de MPC-tak en
  ``_calc_heating_curve_breakpoints``. Eén onzinnige retourtemperatuur — zoals
  tijdens het bronwissel-venster bij een herstart — schreef daardoor een advies
  van tientallen graden onder nul weg.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
from custom_components.quatt_stooklijn.const import (
    MIN_HEAT_OUTPUT_W,
    MPC_SUPPLY_TEMP_MAX,
    MPC_SUPPLY_TEMP_MIN,
)
from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData
from custom_components.quatt_stooklijn.discovery import (
    ROLE_FLOW_RATE,
    ROLE_OUTDOOR_TEMP,
    ROLE_RETURN_TEMP,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from custom_components.quatt_stooklijn.sensor import (
    QuattAdviceErrorSensor,
    QuattSupplyTempSensor,
)

ADVISED = "sensor.advies"
FLOW = "sensor.debiet"
HEAT = "sensor.thermisch_vermogen"
OUTDOOR = "sensor.buiten"
RETURN = "sensor.retour"
SUPPLY = "sensor.aanvoer"

ROLE_ENTITIES = {
    ROLE_FLOW_RATE: FLOW,
    ROLE_OUTDOOR_TEMP: OUTDOOR,
    ROLE_RETURN_TEMP: RETURN,
    ROLE_SUPPLY_TEMP: SUPPLY,
    ROLE_TOTAL_POWER: HEAT,
}


class _State:
    def __init__(self, value):
        self.state = str(value)


def _hass(**states):
    """Mock-hass waarin alleen de meegegeven entiteiten een waarde hebben."""
    hass = MagicMock()
    known = {k: _State(v) for k, v in states.items()}
    hass.states.get = lambda entity_id: known.get(entity_id)
    return hass


def _sources():
    """Bronresolutie vastzetten: elke rol wijst naar zijn testentiteit."""
    return patch(
        "custom_components.quatt_stooklijn.sensor.async_source_entity",
        side_effect=lambda hass, entry_id, role, **kw: ROLE_ENTITIES[role],
    )


def _error_sensor(**states):
    sensor = QuattAdviceErrorSensor.__new__(QuattAdviceErrorSensor)
    sensor.hass = _hass(**states)
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    sensor._advised_entity = ADVISED
    return sensor


def _supply_sensor(slope=-200.0, intercept=4000.0, **states):
    sensor = QuattSupplyTempSensor.__new__(QuattSupplyTempSensor)
    sensor.hass = _hass(**states)
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    coordinator = MagicMock()
    coordinator.data = QuattStooklijnData(
        heat_loss_hp=HeatLossResult(slope=slope, intercept=intercept)
    )
    # CoordinatorEntity.coordinator is in de echte HA een gewoon attribuut;
    # de stub in conftest zet hem niet, dus hier expliciet.
    sensor.coordinator = coordinator
    return sensor


class TestProductieGate:
    """De foutsensor zwijgt zolang er geen warmte het huis in gaat."""

    def test_zwijgt_bij_circulatie_zonder_productie(self):
        """Augustus: pomp draait, 0 W productie, advies staat er wel."""
        sensor = _error_sensor(
            **{FLOW: 800, HEAT: 0, ADVISED: 18.4, SUPPLY: 25.0}
        )
        with _sources():
            assert sensor.native_value is None

    def test_rekent_bij_echte_productie(self):
        sensor = _error_sensor(
            **{FLOW: 800, HEAT: 3000, ADVISED: 33.0, SUPPLY: 35.0}
        )
        with _sources():
            assert sensor.native_value == -2.0

    def test_zwijgt_zonder_productiebron(self):
        """Valt de bron weg, dan is zwijgen beter dan een fout die nergens
        op slaat — net als bij een ontbrekend debiet."""
        sensor = _error_sensor(
            **{FLOW: 800, HEAT: "unavailable", ADVISED: 33.0, SUPPLY: 35.0}
        )
        with _sources():
            assert sensor.native_value is None

    def test_debiet_gate_blijft_gelden(self):
        """Productie zonder circulatie zegt evenmin iets over de aanvoer."""
        sensor = _error_sensor(
            **{FLOW: 5, HEAT: 3000, ADVISED: 33.0, SUPPLY: 35.0}
        )
        with _sources():
            assert sensor.native_value is None

    def test_drempel_telt_zelf_mee(self):
        """Precies op de drempel is productie — de grens ligt eronder."""
        sensor = _error_sensor(
            **{
                FLOW: 800,
                HEAT: MIN_HEAT_OUTPUT_W,
                ADVISED: 33.0,
                SUPPLY: 35.0,
            }
        )
        with _sources():
            assert sensor.native_value == -2.0

    def test_net_onder_de_drempel_zwijgt(self):
        sensor = _error_sensor(
            **{
                FLOW: 800,
                HEAT: MIN_HEAT_OUTPUT_W - 1,
                ADVISED: 33.0,
                SUPPLY: 35.0,
            }
        )
        with _sources():
            assert sensor.native_value is None


class TestAdviesClamp:
    """Het advies blijft binnen dezelfde grenzen als de MPC-tak."""

    def test_onzinnige_retourtemperatuur_clamped_op_minimum(self):
        """Het bronwissel-venster bij een herstart leverde −29,9 °C."""
        sensor = _supply_sensor(
            **{OUTDOOR: 0.0, RETURN: -40.0, FLOW: 800}
        )
        with _sources():
            assert sensor.native_value == MPC_SUPPLY_TEMP_MIN

    def test_extreme_vraag_clamped_op_maximum(self):
        sensor = _supply_sensor(
            slope=-1000.0,
            intercept=40000.0,
            **{OUTDOOR: -10.0, RETURN: 30.0, FLOW: 100},
        )
        with _sources():
            assert sensor.native_value == MPC_SUPPLY_TEMP_MAX

    def test_normale_waarde_blijft_ongemoeid(self):
        """0 °C buiten: vraag 4000 W, retour 30 °C, 500 l/h → 36,9 °C."""
        sensor = _supply_sensor(**{OUTDOOR: 0.0, RETURN: 30.0, FLOW: 500})
        with _sources():
            value = sensor.native_value
        assert value == pytest.approx(36.9, abs=0.1)
        assert MPC_SUPPLY_TEMP_MIN < value < MPC_SUPPLY_TEMP_MAX

    def test_boven_het_balanspunt_geen_advies(self):
        """De bestaande afslag blijft staan: geen vraag, geen advies."""
        sensor = _supply_sensor(**{OUTDOOR: 25.0, RETURN: 22.0, FLOW: 800})
        with _sources():
            assert sensor.native_value is None
