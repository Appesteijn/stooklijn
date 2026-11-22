"""De 2R2C-kolom in de vooruitblik (v0.10.6-beta.9).

Het 2R2C rekent het vermogen uit de tabel vrij door en laat zien wat dat met
de kamer doet. Het stuurt het vermogen niet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from custom_components.quatt_stooklijn.analysis.shift_room_temp import (
    room_after_each_step,
    start_state,
)
from custom_components.quatt_stooklijn.analysis.thermal_model_2r2c import simulate
from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

from .test_shift_room_temp import NU, P, _logboek

PARAMS = {k: getattr(P, k) for k in ("U", "Ci", "Cm", "Hm", "g", "P_int", "alpha")}


def _uren(n=12, q=3500.0, t=5.0):
    return [{"temp_forecast": t, "q_hp_needed_w": q} for _ in range(n)]


def _sensor(result, rows=None):
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.benchmark = (
        None if result is False else SimpleNamespace(result=result)
    )
    sensor._start_2r2c = None
    store = MagicMock()
    store.log.rows.return_value = rows if rows is not None else _logboek()
    sensor._highres_store = store
    sensor._highres_loaded = True
    return sensor


def _result(accepted=True, params=PARAMS, rmse=0.27):
    return SimpleNamespace(accepted=accepted, params=params, rmse_k=rmse)


@pytest.fixture(autouse=True)
def _nu():
    moment = datetime.fromtimestamp(NU, tz=timezone.utc)
    with patch(
        "custom_components.quatt_stooklijn.sensor.dt_util.utcnow",
        return_value=moment,
    ):
        yield


class TestDoorrekenen:
    def test_temperatuur_aan_het_eind_van_elk_uur(self):
        """Zelfde uitlijning als t_indoor_predicted: na het uur, niet ervoor."""
        start = start_state(P, _logboek(), NU)
        to, sol, q = [5.0] * 3, [0.0] * 3, [3500.0] * 3
        lijn = room_after_each_step(P, start, to, sol, q, 1.0)
        verwacht = simulate(P, start.t_indoor, start.t_mass, np.array(to),
                            np.array(sol), np.array(q), 1.0)
        assert lijn == pytest.approx([round(float(v), 2) for v in verwacht])

    def test_meer_vermogen_warmere_kamer(self):
        laag, _ = _sensor(_result())._room_2r2c(_uren(q=2000.0), [0.0] * 12)
        hoog, _ = _sensor(_result())._room_2r2c(_uren(q=5000.0), [0.0] * 12)
        assert all(h > l for h, l in zip(hoog, laag))

    def test_zon_warmt_de_kamer(self):
        zonder, _ = _sensor(_result())._room_2r2c(_uren(), [0.0] * 12)
        met, _ = _sensor(_result())._room_2r2c(_uren(), [400.0] * 12)
        assert met[-1] > zonder[-1]

    def test_kortere_zonreeks_wordt_aangevuld(self):
        lijn, reden = _sensor(_result())._room_2r2c(_uren(n=12), [0.0] * 5)
        assert reden is None and len(lijn) == 12


class TestAlleenMetEenGoedgekeurdModel:
    def test_zonder_modelvergelijking(self):
        assert _sensor(False)._room_2r2c(_uren(), [0.0] * 12) == (
            None, "modelvergelijking nog niet gemeten",
        )

    def test_nog_niet_gemeten(self):
        _, reden = _sensor(None)._room_2r2c(_uren(), [0.0] * 12)
        assert reden == "modelvergelijking nog niet gemeten"

    def test_niet_beter_dan_het_1r1c(self):
        _, reden = _sensor(_result(accepted=False))._room_2r2c(_uren(), [0.0] * 12)
        assert reden == "2R2C voorspelt nog niet beter dan het huidige model"

    def test_leeg_logboek(self):
        _, reden = _sensor(_result(), rows=[])._room_2r2c(_uren(), [0.0] * 12)
        assert reden == "geen recente meting in het logboek"

    def test_kapotte_vooruitblik_breekt_de_attributen_niet(self):
        uren = _uren()
        del uren[3]["temp_forecast"]
        assert _sensor(_result())._room_2r2c(uren, [0.0] * 12) == (
            None, "doorrekenen mislukt",
        )


class TestBegintoestandBewaard:
    def test_tweede_keer_uit_de_cache(self):
        sensor = _sensor(_result())
        sensor._room_2r2c(_uren(), [0.0] * 12)
        with patch(
            "custom_components.quatt_stooklijn.analysis.shift_room_temp.start_state"
        ) as opnieuw:
            sensor._room_2r2c(_uren(), [0.0] * 12)
        opnieuw.assert_not_called()

    def test_nieuwe_parameters_rekenen_opnieuw(self):
        sensor = _sensor(_result())
        eerst, _ = sensor._room_2r2c(_uren(), [0.0] * 12)
        sensor.benchmark.result = _result(params={**PARAMS, "U": 600.0})
        daarna, _ = sensor._room_2r2c(_uren(), [0.0] * 12)
        assert daarna != eerst

    def test_verlopen_cache_rekent_opnieuw(self):
        sensor = _sensor(_result())
        sensor._room_2r2c(_uren(), [0.0] * 12)
        tijd, key, start = sensor._start_2r2c
        sensor._start_2r2c = (tijd - 10_000, key, start)
        sensor._room_2r2c(_uren(), [0.0] * 12)
        assert sensor._start_2r2c[0] > tijd - 10_000
