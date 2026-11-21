"""De vooruitblik zonder geconvergeerd huismodel (v0.10.7-beta.4).

Aanleiding, Tweakers 21-11-2026: bij ``batch_fallback`` was "T binnen" leeg en
leek de aansturing de kamer te negeren. Het advies rekende wél met de kamer
(warmteverlies + 10·U·tekort), maar de terugval-vooruitblik rekende met de
stooklijn, trok de zon af en kende geen inhaalterm of capaciteitsgrens: +0
lag naast het advies. Nu dezelfde ``advice_demand`` als het advies; de
inhaalterm alleen in uur 0, omdat zonder C niet te zeggen is hoe snel de
kamer bijtrekt.
"""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    advice_demand,
    advice_forward_without_model,
    supply_temp_for,
)

U = 285.0
T_BAL = 16.6
SETPOINT = 20.5
FACTOR = 10.0
T_RETURN = 28.0
FLOW = 800.0


def _vooruit(t_in=20.2, temps=None, capacity=lambda t: 6000.0, **kw):
    return advice_forward_without_model(
        t_in,
        T_RETURN,
        FLOW,
        temps or [6.5, 7.8, 8.5, 8.5, 7.9, 6.8],
        u_wk=U,
        balance_point=T_BAL,
        t_setpoint=SETPOINT,
        catchup_factor=FACTOR,
        capacity_w=capacity,
        supply_temp_min=20.0,
        supply_temp_max=55.0,
        **kw,
    )


def _advies(t_out, t_in, capacity=6000.0):
    d = advice_demand(U, T_BAL, t_out, t_in, SETPOINT, FACTOR, capacity)
    return supply_temp_for(d.capped_w, T_RETURN, FLOW, 20.0, 55.0)


class TestUurNul:
    def test_is_het_advies_met_inhaalterm(self):
        uren = _vooruit(t_in=20.2)
        assert uren[0]["supply_temp"] == _advies(6.5, 20.2)
        # 285·(16,6−6,5) + 10·285·0,3 = 2.878,5 + 855
        assert uren[0]["q_hp_needed_w"] == pytest.approx(3733.5, abs=1)

    def test_begrensd_op_de_capaciteit(self):
        uren = _vooruit(t_in=19.0, capacity=lambda t: 3000.0)
        assert uren[0]["begrensd"] is True
        assert uren[0]["q_hp_needed_w"] == 3000

    def test_zonder_kamertemperatuur_geen_inhaalterm(self):
        uren = _vooruit(t_in=None)
        assert uren[0]["q_hp_needed_w"] == pytest.approx(U * (T_BAL - 6.5), abs=1)


class TestLatereUren:
    def test_geen_inhaalterm_na_uur_nul(self):
        """Bewuste onderschatting: zonder C geen kamer om het tekort te volgen."""
        uren = _vooruit(t_in=20.2)
        for u, t_out in zip(uren[1:], [7.8, 8.5, 8.5, 7.9, 6.8]):
            assert u["q_hp_needed_w"] == pytest.approx(U * (T_BAL - t_out), abs=1)

    def test_capaciteit_per_uur(self):
        uren = _vooruit(temps=[6.5, -5.0], capacity=lambda t: 3000.0 if t < 0 else 6000.0)
        assert uren[0]["begrensd"] is False
        assert uren[1]["begrensd"] is True

    def test_boven_het_balanspunt_geen_vraag(self):
        uren = _vooruit(t_in=19.0, temps=[6.5, 18.0])
        assert uren[1]["q_hp_needed_w"] == 0
        assert uren[1]["hp_needed"] is False
        assert uren[1]["supply_temp"] is None


class TestVelden:
    def test_zelfde_sleutels_als_met_huismodel(self):
        assert set(_vooruit()[0]) == {
            "hour", "q_hp_needed_w", "t_indoor_predicted", "supply_temp",
            "supply_temp_no_solar", "hp_needed", "begrensd", "stored_heat_kwh",
        }

    def test_geen_kamer_geen_buffer(self):
        assert all(u["t_indoor_predicted"] is None for u in _vooruit())
        assert all(u["stored_heat_kwh"] is None for u in _vooruit())

    def test_zon_verandert_niets(self):
        """Zon zit niet in de vraag, en zonder kamer ook niet in de inhaalterm."""
        assert all(u["supply_temp_no_solar"] == u["supply_temp"] for u in _vooruit())

    def test_max_hours(self):
        assert len(_vooruit(max_hours=3)) == 3

    def test_booleans_zijn_gewone_bools(self):
        for u in _vooruit(t_in=19.0, capacity=lambda t: 3000.0):
            assert type(u["begrensd"]) is bool and type(u["hp_needed"]) is bool
