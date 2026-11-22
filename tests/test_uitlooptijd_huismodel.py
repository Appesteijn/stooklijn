"""De uitlooptijd rekent met het goedgekeurde 2R2C, anders met het 1R1C
(v0.10.7-beta.9). Met beide: de kortste (beta.10, "veilig" is voorzichtig)."""

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
    assert attrs["model_source"] in ("2r2c", "online")
    assert set(attrs["per_model"]) == {"2r2c", "online"}


def _met_massa(t_mass):
    s = _sensor((WAAR, StartState(t_indoor=20.5, t_mass=t_mass)))
    s._mpc._indoor_temp_entity = "indoor"
    s._mpc._outdoor_entity = "outdoor"
    with patch("custom_components.quatt_stooklijn.sensor.get_float_state",
               side_effect=lambda h, e: _waarden(e)):
        return s.native_value, s.extra_state_attributes


def test_de_kortste_van_beide_wint():
    """Live 22-11: alleen het 2R2C gaf 237 minuten, het 1R1C 145; het 2R2C
    koelde in de afkoeltoets ruim een kwart te traag af."""
    for t_mass in (17.0, 23.0):
        waarde, attrs = _met_massa(t_mass)
        assert waarde == min(attrs["per_model"].values())
        assert attrs["per_model"][attrs["model_source"]] == waarde
    # Een warme massa houdt de kamer bij het 2R2C langer op peil: dan wint het 1R1C.
    _, warm = _met_massa(23.0)
    assert warm["per_model"]["2r2c"] >= warm["per_model"]["online"]


def test_zonder_2r2c_het_1r1c():
    s = _sensor(None)
    s._mpc._indoor_temp_entity = "indoor"
    s._mpc._outdoor_entity = "outdoor"
    with patch("custom_components.quatt_stooklijn.sensor.get_float_state",
               side_effect=lambda h, e: _waarden(e)):
        attrs = s.extra_state_attributes
    assert attrs["model_source"] == "online"
