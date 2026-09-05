"""Tests voor de warmtebuffer: hoeveel warmte er in de bouwmassa zit.

E = C × (T_binnen − comfortgrens). Twee dingen moeten kloppen en blijven
kloppen: het getal zelf, en dat de vooruitblik per uur de buffer toont die bij
de vóórspelde binnentemperatuur van dát uur hoort — niet bij de stand waarmee
het uur begon. Dat laatste is de fout die je op een dashboard nooit ziet, omdat
een uur verschuiving er plausibel uitziet.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    simulate_forward,
    stored_heat_kwh,
)
from custom_components.quatt_stooklijn.sensor import QuattStoredHeatSensor

from .test_thermal_model import TestOnlineRCModel

INDOOR = "sensor.kamertemperatuur"


def _trained_model(U=200.0, C=5000.0, g=0.30):
    return TestOnlineRCModel._make_trained_model(U=U, C=C, g=g)


class _State:
    def __init__(self, value):
        self.state = str(value)


def _sensor(model, t_indoor, comfort_floor=19.0):
    sensor = QuattStoredHeatSensor.__new__(QuattStoredHeatSensor)
    hass = MagicMock()
    known = {} if t_indoor is None else {INDOOR: _State(t_indoor)}
    hass.states.get = lambda entity_id: known.get(entity_id)
    sensor.hass = hass
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    sensor._mpc = MagicMock()
    sensor._mpc.thermal_model = model
    sensor._mpc._indoor_temp_entity = INDOOR
    sensor._mpc._comfort_floor = comfort_floor
    return sensor


class TestStoredHeatKwh:
    """De rekenkern."""

    def test_basisberekening(self):
        # 5000 Wh/K × 3 K = 15000 Wh = 15 kWh
        assert stored_heat_kwh(5000.0, 22.0, 19.0) == 15.0

    def test_op_de_grens_is_nul(self):
        assert stored_heat_kwh(5000.0, 19.0, 19.0) == 0.0

    def test_onder_de_grens_wordt_negatief(self):
        """Een tekort is informatie; afkappen op nul zou een te koud huis niet
        onderscheiden van een huis dat precies goed staat."""
        assert stored_heat_kwh(5000.0, 18.0, 19.0) == -5.0

    def test_zonder_capaciteit_geen_getal(self):
        assert stored_heat_kwh(None, 22.0, 19.0) is None

    def test_onzinnige_capaciteit_geen_getal(self):
        assert stored_heat_kwh(0.0, 22.0, 19.0) is None
        assert stored_heat_kwh(-100.0, 22.0, 19.0) is None

    def test_zonder_binnentemperatuur_geen_getal(self):
        assert stored_heat_kwh(5000.0, None, 19.0) is None

    def test_schaalt_met_de_capaciteit(self):
        klein = stored_heat_kwh(5000.0, 22.0, 19.0)
        groot = stored_heat_kwh(28000.0, 22.0, 19.0)
        assert groot > klein
        assert groot == pytest.approx(klein * 28000 / 5000, abs=0.1)


class TestVooruitblik:
    """De buffer hoort per uur bij de voorspelde binnentemperatuur."""

    def _uren(self, n=6, **kw):
        return simulate_forward(
            _trained_model(),
            t_indoor_now=21.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * n,
            forecast_q_solar=[0.0] * n,
            **kw,
        )

    def test_elk_uur_draagt_een_buffer(self):
        uren = self._uren()
        assert len(uren) == 6
        assert all(u["stored_heat_kwh"] is not None for u in uren)

    def test_buffer_hoort_bij_de_voorspelde_temperatuur(self):
        """Niet bij de temperatuur waarmee het uur begon — dat scheelt precies
        één stap en is op een dashboard onzichtbaar."""
        model = _trained_model()
        capaciteit = model.raw_params["C"]
        uren = simulate_forward(
            model,
            t_indoor_now=21.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0] * 6,
            forecast_q_solar=[0.0] * 6,
            comfort_floor=19.0,
        )
        for uur in uren:
            verwacht = capaciteit * (uur["t_indoor_predicted"] - 19.0) / 1000.0
            # t_indoor_predicted is afgerond op 0,1 K, dus de buffer mag daar
            # net zoveel van afwijken als die afronding toelaat.
            assert uur["stored_heat_kwh"] == pytest.approx(
                verwacht, abs=capaciteit * 0.05 / 1000.0 + 0.06
            )

    def test_hogere_comfortgrens_geeft_kleinere_buffer(self):
        laag = self._uren(comfort_floor=18.0)
        hoog = self._uren(comfort_floor=20.0)
        for a, b in zip(laag, hoog):
            assert a["stored_heat_kwh"] > b["stored_heat_kwh"]

    def test_default_comfortgrens_is_negentien(self):
        """Zelfde grens als de uitlooptijd-sensor; loopt die uiteen, dan gaan
        twee sensoren over dezelfde vraag verschillende antwoorden geven."""
        model = _trained_model()
        capaciteit = model.raw_params["C"]
        uur = simulate_forward(
            model,
            t_indoor_now=21.0,
            t_return=28.0,
            flow_lph=500.0,
            forecast_t_outdoor=[5.0],
            forecast_q_solar=[0.0],
        )[0]
        verwacht = capaciteit * (uur["t_indoor_predicted"] - 19.0) / 1000.0
        # Zelfde speling als hierboven: de buffer wordt op de onafgeronde
        # voorspelling gerekend, t_indoor_predicted wordt op 0,1 K afgerond.
        assert uur["stored_heat_kwh"] == pytest.approx(
            verwacht, abs=capaciteit * 0.05 / 1000.0 + 0.06
        )


class TestStoredHeatSensor:
    """De sensor zelf."""

    def test_rekent_met_het_geleerde_model(self):
        model = _trained_model()
        sensor = _sensor(model, t_indoor=22.0)
        verwacht = model.raw_params["C"] * 3.0 / 1000.0
        assert sensor.native_value == pytest.approx(verwacht, abs=0.1)

    def test_zonder_model_geen_getal(self):
        sensor = _sensor(None, t_indoor=22.0)
        assert sensor.native_value is None

    def test_ongeconvergeerd_model_geen_getal(self):
        """Zonder betrouwbare C is elk getal hier verzonnen."""
        model = MagicMock()
        model.is_converged = False
        sensor = _sensor(model, t_indoor=22.0)
        assert sensor.native_value is None

    def test_zonder_kamertemperatuur_geen_getal(self):
        sensor = _sensor(_trained_model(), t_indoor=None)
        assert sensor.native_value is None

    def test_attributen_dragen_de_marge(self):
        sensor = _sensor(_trained_model(), t_indoor=22.4, comfort_floor=19.0)
        attrs = sensor.extra_state_attributes
        assert attrs["marge_k"] == pytest.approx(3.4, abs=0.01)
        assert attrs["comfort_floor"] == 19.0
        assert attrs["kamertemperatuur"] == pytest.approx(22.4, abs=0.01)
        assert attrs["capaciteit_wh_k"] > 0
        assert attrs["model_source"] == "online"

    def test_attributen_melden_een_ontbrekend_model(self):
        attrs = _sensor(None, t_indoor=22.0).extra_state_attributes
        assert attrs["model_source"] == "unavailable"
        assert attrs["capaciteit_wh_k"] is None
        # De marge blijft wél staan: die hangt niet aan het model.
        assert attrs["marge_k"] == pytest.approx(3.0, abs=0.01)

    def test_marge_ontbreekt_zonder_kamertemperatuur(self):
        attrs = _sensor(_trained_model(), t_indoor=None).extra_state_attributes
        assert attrs["marge_k"] is None
        assert attrs["kamertemperatuur"] is None
