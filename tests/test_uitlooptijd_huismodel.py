"""De uitlooptijd rekent met het goedgekeurde 2R2C, anders met het 1R1C
(v0.10.7-beta.9)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.quatt_stooklijn.analysis.shift_room_temp import StartState
from custom_components.quatt_stooklijn.sensor import QuattCoastTimeSensor

from . import test_thermal_model as _ttm
from .test_thermal_model_2r2c import WAAR


def _sensor(two):
    s = QuattCoastTimeSensor.__new__(QuattCoastTimeSensor)
    s.hass = MagicMock()
    s._entry = SimpleNamespace(data={}, options={})
    mpc = MagicMock()
    mpc.thermal_model = _ttm.TestOnlineRCModel._make_trained_model()
    mpc.house_model_2r2c = lambda: two
    mpc.build_forecast_arrays = lambda t, n_hours: ([2.0] * 12, [0.0] * 12, [])
    mpc.min_power_curve = SimpleNamespace(bins=[])
    s._mpc = mpc
    return s


def _waarden(eid):
    return 20.5 if "indoor" in str(eid) else 2.0


def test_met_goedgekeurd_2r2c():
    s = _sensor((WAAR, StartState(t_indoor=20.5, t_mass=20.0)))
    s._mpc._indoor_temp_entity = "indoor"
    s._mpc._outdoor_entity = "outdoor"
    with patch("custom_components.quatt_stooklijn.sensor.get_float_state",
               side_effect=lambda h, e: _waarden(e)):
        attrs = s.extra_state_attributes
    assert attrs["model_source"] == "2r2c"


def test_zonder_2r2c_het_1r1c():
    s = _sensor(None)
    s._mpc._indoor_temp_entity = "indoor"
    s._mpc._outdoor_entity = "outdoor"
    with patch("custom_components.quatt_stooklijn.sensor.get_float_state",
               side_effect=lambda h, e: _waarden(e)):
        attrs = s.extra_state_attributes
    assert attrs["model_source"] == "online"
