"""Tests voor ``advies_status`` op de MPC-sensor.

Een leeg advies betekende twee dingen: het huis heeft geen warmte nodig, of er
ontbreekt een meting. De geluidsniveau-compensatie behandelde beide als uitval
en gaf na tien minuten vrij spel — juist als de kamer al boven setpoint stond.
De sensor is de enige die weet welke van de twee het is, dus hij zegt het erbij.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.quatt_stooklijn.const import (
    MPC_ADVICE_NO_DEMAND,
    MPC_ADVICE_NO_INPUT,
    MPC_ADVICE_OK,
)
from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

from tests.test_room_setpoint import (
    FLOW,
    INDOOR,
    OUTDOOR,
    RETURN,
    SETPOINT,
    _converged_model,
    _hass,
    _mpc_sensor,
    _sources,
)

KOUD = {OUTDOOR: 2.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.5, SETPOINT: 20.0}


class TestOnlineModel:

    def test_advies(self):
        sensor = _mpc_sensor(**KOUD)
        with _sources():
            waarde, status = sensor._current_advice()
        assert status == MPC_ADVICE_OK
        assert waarde is not None

    def test_kamer_boven_setpoint_is_geen_vraag(self):
        sensor = _mpc_sensor(**{**KOUD, OUTDOOR: 15.0, INDOOR: 23.0})
        with _sources():
            assert sensor._current_advice() == (None, MPC_ADVICE_NO_DEMAND)

    def test_buitentemperatuur_ontbreekt(self):
        states = {k: v for k, v in KOUD.items() if k != OUTDOOR}
        sensor = _mpc_sensor(**states)
        with _sources():
            assert sensor._current_advice() == (None, MPC_ADVICE_NO_INPUT)

    def test_state_en_status_uit_dezelfde_berekening(self):
        """native_value en het attribuut mogen elkaar niet tegenspreken."""
        for states in (KOUD, {**KOUD, OUTDOOR: 15.0, INDOOR: 23.0}):
            sensor = _mpc_sensor(**states)
            with _sources():
                assert sensor.native_value == sensor._current_advice()[0]


def _batch_sensor(balance_point=16.5, slope=-250.0, intercept=4125.0, **states):
    """Sensor op de terugval: model niet geconvergeerd, batchregressie wel."""
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.hass = _hass(**states)
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    sensor._thermal_store = MagicMock(model=_converged_model())
    sensor._thermal_loaded = False  # forceert de batch-tak
    sensor._solar_radiation = []
    sensor._forecast = []
    heat_loss = MagicMock(slope=slope, intercept=intercept, balance_point=balance_point)
    sensor.coordinator = MagicMock()
    sensor.coordinator.data.heat_loss_hp = heat_loss
    return sensor


class TestBatchTerugval:

    def test_advies(self):
        sensor = _batch_sensor(**KOUD)
        with _sources():
            waarde, status = sensor._current_advice()
        assert status == MPC_ADVICE_OK
        assert waarde is not None

    def test_warm_buiten_is_geen_vraag(self):
        sensor = _batch_sensor(**{**KOUD, OUTDOOR: 20.0})
        with _sources():
            assert sensor._current_advice() == (None, MPC_ADVICE_NO_DEMAND)

    def test_geen_analysedata(self):
        sensor = _batch_sensor(**KOUD)
        sensor.coordinator.data = None
        with _sources():
            assert sensor._current_advice() == (None, MPC_ADVICE_NO_INPUT)

    def test_onvolledige_regressie(self):
        sensor = _batch_sensor(slope=None, **KOUD)
        with _sources():
            assert sensor._current_advice() == (None, MPC_ADVICE_NO_INPUT)
