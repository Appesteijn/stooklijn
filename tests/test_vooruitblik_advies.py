"""De vooruitblik rekent elk uur zoals het advies (v0.10.6-beta.8).

Aanleiding, Tweakers 20-11-2026: MPC-advies 36,8 °C, maar de vooruitblik toonde
bij +0 21.023 W en 52,9 °C. De tabel rekende nog met ``calc_required_power``
(het hele tekort binnen één uur, onbegrensd), het advies sinds beta.6 met
warmteverlies + 10·U·tekort, begrensd op de capaciteit. Huis van die
gebruiker, afgeleid uit zijn schermafbeeldingen: C ≈ 21 kWh/K, U ≈ 280 W/K
(7 kW bij −10 °C, stookgrens 15 °C), Quatt van 4,5 kW.
"""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    advice_demand,
    simulate_advice_forward,
    supply_temp_for,
)

U = 280.0
T_BAL = 15.0
C = 21_000.0
SETPOINT = 20.5
FACTOR = 10.0
T_RETURN = 30.0
FLOW = 800.0


class _Model:
    """1R1C met bekende parameters:
    C·dT/dt = U·(T_buiten − T_binnen) + g·Q_zon + Q_wp + P_c.

    P_c is de constante winst die het balanspunt onder de kamertemperatuur
    legt (zie CLAUDE.md): U·(setpoint − T_balans). Zonder die term koelt het
    huis af bij precies het vermogen dat het advies vraagt.
    """

    def __init__(self, u=U, c=C, g=5.0, p_c=U * (SETPOINT - T_BAL)):
        self.u, self.c, self.g, self.p_c = u, c, g, p_c

    @property
    def raw_params(self):
        return {"U": self.u, "C": self.c, "g": self.g, "P_c": self.p_c}

    def predict_t_indoor(self, t_indoor, t_outdoor, q_hp_w, q_solar_w, dt_hours=1.0):
        return t_indoor + dt_hours * (
            self.u * (t_outdoor - t_indoor) + self.g * q_solar_w + q_hp_w + self.p_c
        ) / self.c


def _sim(t_in=19.7, temps=None, solar=None, capacity=lambda t: 4500.0, **kw):
    temps = temps or [7.0, 3.3, 3.1, 3.4, 3.5, 3.5]
    solar = solar or [0.0] * len(temps)
    return simulate_advice_forward(
        kw.pop("model", _Model()),
        t_indoor_now=t_in,
        t_return=T_RETURN,
        flow_lph=FLOW,
        forecast_t_outdoor=temps,
        forecast_q_solar=solar,
        u_wk=U,
        balance_point=T_BAL,
        t_setpoint=SETPOINT,
        catchup_factor=FACTOR,
        capacity_w=capacity,
        comfort_floor=20.05,
        **kw,
    )


class TestPlusNulIsHetAdvies:
    def test_zelfde_aanvoer_als_het_advies_van_nu(self):
        d = advice_demand(U, T_BAL, 7.0, 19.7, SETPOINT, FACTOR, 4500.0)
        advies = supply_temp_for(d.capped_w, T_RETURN, FLOW, 20.0, 55.0)
        assert _sim()[0]["supply_temp"] == advies

    def test_geen_21_kw_meer_bij_een_zwaar_huis(self):
        """De oude tabel vroeg C·0,8 K + verlies ≈ 21 kW; nu de capaciteit."""
        uur = _sim()[0]
        assert uur["q_hp_needed_w"] <= 4500
        assert uur["supply_temp"] < 40.0

    def test_vermogen_is_warmteverlies_plus_inhaal(self):
        # 280·(15−7) + 10·280·0,8 = 2.240 + 2.240 = 4.480 W, onder 4.500.
        uur = _sim()[0]
        assert uur["q_hp_needed_w"] == pytest.approx(4480, abs=1)
        assert uur["begrensd"] is False


class TestKamerLooptRealistischOp:
    def test_kamer_bereikt_het_setpoint_niet_in_een_uur(self):
        uren = _sim()
        assert uren[0]["t_indoor_predicted"] < SETPOINT

    def test_kamer_stijgt_en_het_vermogen_daalt_mee(self):
        """Hetzelfde koude weer: elk uur minder tekort, dus minder inhaal."""
        uren = _sim(temps=[5.0] * 8)
        kamers = [u["t_indoor_predicted"] for u in uren]
        assert kamers == sorted(kamers)
        assert kamers[-1] <= SETPOINT + 0.05

    def test_kouder_dan_de_capaciteit_aankan_wordt_begrensd(self):
        uren = _sim(temps=[-5.0] * 3, capacity=lambda t: 4500.0)
        assert all(u["begrensd"] for u in uren)
        assert all(u["q_hp_needed_w"] == 4500 for u in uren)

    def test_capaciteit_volgt_de_buitentemperatuur_per_uur(self):
        gezien: list[float] = []

        def cap(t):
            gezien.append(t)
            return 4500.0

        _sim(temps=[7.0, 3.0, 1.0], capacity=cap)
        assert sorted(set(gezien)) == [1.0, 3.0, 7.0]

    def test_zonder_capaciteit_onbegrensd(self):
        uur = _sim(temps=[-5.0], capacity=lambda t: None)[0]
        assert uur["begrensd"] is False
        assert uur["q_hp_needed_w"] > 4500


class TestGeenVraag:
    def test_boven_het_balanspunt_en_warm_geen_vraag(self):
        uur = _sim(t_in=21.0, temps=[16.0])[0]
        assert uur["q_hp_needed_w"] == 0
        assert uur["supply_temp"] is None
        assert uur["hp_needed"] is False

    def test_kamer_ruim_boven_setpoint_drukt_de_vraag(self):
        """Inhaalterm negatief: een te warme kamer verlaagt het advies."""
        warm = _sim(t_in=21.5, temps=[10.0])[0]
        precies = _sim(t_in=SETPOINT, temps=[10.0])[0]
        assert warm["q_hp_needed_w"] < precies["q_hp_needed_w"]


class TestZon:
    def test_zon_warmt_de_kamer_en_drukt_het_volgende_uur(self):
        solar = [600.0, 600.0, 0.0, 0.0]
        uren = _sim(t_in=SETPOINT, temps=[8.0] * 4, solar=solar)
        # Kolom "geen zon" volgt een kamer zonder zon: die blijft koeler, dus
        # meer inhaal en een hogere aanvoer.
        assert uren[2]["supply_temp_no_solar"] > uren[2]["supply_temp"]

    def test_zon_zit_niet_in_de_vraag_van_het_eerste_uur(self):
        """Het balanspunt bevat de gemiddelde zon al; zie advice_demand."""
        met = _sim(temps=[7.0], solar=[600.0])[0]
        zonder = _sim(temps=[7.0], solar=[0.0])[0]
        assert met["supply_temp"] == zonder["supply_temp"]
        assert met["t_indoor_predicted"] > zonder["t_indoor_predicted"]


class TestBuffer:
    def test_buffer_hoort_bij_de_voorspelde_kamer(self):
        """Niet bij de stand waarmee het uur begon (−7,3 kWh bij 19,7 °C)."""
        uur = _sim()[0]
        # t_indoor_predicted is op 0,1 K afgerond: bij 21 kWh/K ±1,05 kWh.
        verwacht = C * (uur["t_indoor_predicted"] - 20.05) / 1000
        assert uur["stored_heat_kwh"] == pytest.approx(verwacht, abs=1.1)
        assert uur["stored_heat_kwh"] > C * (19.7 - 20.05) / 1000


class TestJson:
    """Beta.8/9: numpy-capaciteit gaf numpy.bool in ``begrensd``, en HA kreeg
    de hele MPC-sensor niet meer naar JSON (500 in de API)."""

    def test_numpy_capaciteit_geeft_gewone_bool(self):
        import json

        import numpy as np

        uren = _sim(temps=[7.0, -5.0, 3.0], capacity=lambda t: np.float64(4500.0))
        assert all(type(u["begrensd"]) is bool for u in uren)
        assert all(type(u["hp_needed"]) is bool for u in uren)
        json.dumps(uren)

    def test_advice_demand_capped_is_bool(self):
        import numpy as np

        d = advice_demand(U, T_BAL, -5.0, 19.7, SETPOINT, FACTOR, np.float64(4500.0))
        assert type(d.capped) is bool
