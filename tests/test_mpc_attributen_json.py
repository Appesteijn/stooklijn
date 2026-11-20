"""De attributen van de MPC-sensor moeten door Home Assistants JSON-encoder.

Beta.8/9: de capaciteit kwam uit een numpy-fit, ``totaal > capaciteit`` werd
een ``numpy.bool`` in ``forecast[i].begrensd``, en HA kreeg de hele sensor niet
meer naar JSON: 500 in de API, geen sensor op het dashboard. Deze test bouwt de
volledige attributen op met numpy-getallen waar die in het echt vandaan komen,
en serialiseert ze met dezelfde encoder als HA (``json_bytes``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import datetime
from pathlib import Path

import numpy as np
import orjson
import pytest

from tests.test_room_setpoint import (
    FLOW,
    INDOOR,
    OUTDOOR,
    RETURN,
    SETPOINT,
    _analysis,
    _mpc_sensor,
    _sources,
)

def _json_encoder_default(obj):
    """Kopie van ``homeassistant.helpers.json.json_encoder_default``.

    De tests draaien met een stub van ``homeassistant`` (conftest), dus de
    echte module is hier niet te importeren. Wat ertoe doet: een float-subklasse
    zoals ``numpy.float64`` mag, een ``numpy.bool`` niet.
    """
    if hasattr(obj, "json_fragment"):
        return obj.json_fragment
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, float):
        return float(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError


def json_bytes(obj) -> bytes:
    """Zoals HA's ``json_bytes``: orjson, OPT_NON_STR_KEYS, bovenstaande fallback."""
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS, default=_json_encoder_default)


KOUD = {OUTDOOR: 2.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.5, SETPOINT: 20.5}
UREN = 12


def _numpy_analyse():
    """Analysedata zoals de coordinator hem levert: fits als numpy-getallen."""
    data = _analysis()
    data.heat_loss_hp = SimpleNamespace(
        slope=np.float64(-250.0),
        intercept=np.float64(250.0 * 16.5),
        balance_point=np.float64(16.5),
        heat_loss_coefficient=np.float64(250.0),
    )
    # Vriescurve: capaciteit stijgt met de buitentemperatuur, 4,5 kW bij 7 °C.
    data.stooklijn = SimpleNamespace(
        slope_local=np.float64(60.0), intercept_local=np.float64(4080.0)
    )
    return data


def _sensor(**states):
    sensor = _mpc_sensor(**states)
    sensor.coordinator = MagicMock(data=_numpy_analyse())
    sensor.benchmark = None
    sensor._start_2r2c = None
    sensor._highres_store = MagicMock()
    sensor._highres_loaded = False
    sensor._min_power = MagicMock(bins={}, at=lambda t: None)
    sensor._min_power_at = None
    sensor._solar_times = []
    sensor._solar_fetched_at = None
    return sensor


def _arrays(t_outdoor, n_hours=UREN):
    temps = [float(np.float64(t_outdoor - 0.5 * i)) for i in range(n_hours)]
    return temps, [0.0] * n_hours, [{"datetime": f"u{i}"} for i in range(n_hours)]


def _attributen(**states):
    sensor = _sensor(**states)
    with _sources(), patch.object(
        type(sensor), "build_forecast_arrays", lambda self, t, n_hours=UREN: _arrays(t)
    ), patch.object(
        type(sensor), "min_power_curve",
        property(lambda self: SimpleNamespace(bins={}, at=lambda t: None,
                                              for_temps=lambda t: None)),
    ):
        return sensor, sensor.extra_state_attributes


class TestJsonEncoder:
    def test_koud_huis_met_tekort(self):
        """Begrensde én onbegrensde uren, inhaalterm, numpy-capaciteit."""
        _, attrs = _attributen(**KOUD)
        assert len(attrs["forecast"]) == UREN
        assert any(u["begrensd"] for u in attrs["forecast"])
        json_bytes(attrs)

    def test_kamer_warm_genoeg(self):
        _, attrs = _attributen(**{**KOUD, OUTDOOR: 15.0, INDOOR: 21.5})
        json_bytes(attrs)

    @pytest.mark.parametrize("veld", ["begrensd", "hp_needed"])
    def test_booleans_zijn_gewone_bools(self, veld):
        _, attrs = _attributen(**KOUD)
        assert all(type(u[veld]) is bool for u in attrs["forecast"])

    def test_advies_begrensd_is_gewone_bool(self):
        _, attrs = _attributen(**{**KOUD, OUTDOOR: -8.0})
        assert type(attrs["advies_begrensd"]) is bool

    def test_numpy_bool_zou_falen(self):
        """Controle op de test zelf: HA's encoder weigert numpy.bool echt."""
        with pytest.raises(TypeError):
            json_bytes({"x": np.bool_(False)})
        json_bytes({"x": np.float64(1.5)})
