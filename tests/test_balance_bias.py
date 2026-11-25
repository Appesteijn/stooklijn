"""Tests voor de balanspuntcorrectie tegen een blijvend tekort (BalanceBias).

Tweakers 24-11-2026: de kamer bleef vijf uur lang op 19,62 °C bij een setpoint
van 20,0, met een inhaalvraag van 831 W. De inhaalterm (10·U·tekort) is een
P-regelaar: zit het geschatte warmteverlies 830 W te laag, dan blijft de kamer
830 / (10·218,7) = 0,38 K onder het setpoint staan. Het I-deel werkt dat weg.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import (
    BalanceBias,
    advice_demand,
)
from custom_components.quatt_stooklijn.sensor import _new_balance_bias

from tests.test_room_setpoint import INDOOR, OUTDOOR, SETPOINT, _mpc_sensor, _sources

H = 3600.0
TIK = 300.0


def _bias(**kw) -> BalanceBias:
    args = dict(ti_hours=48.0, max_k=3.0, settle_hours=3.0, max_deficit_k=1.0,
                max_gap_s=900.0, catchup_factor=10.0)
    args.update(kw)
    return BalanceBias(**args)


def _loop(b: BalanceBias, uren: float, kamer=19.62, setpoint=20.0, start=0.0, **kw):
    args = dict(stationary_w=3000.0, capped=False, steering=True)
    args.update(kw)
    t = start
    while t <= start + uren * H:
        b.update(t, setpoint, kamer, **args)
        t += TIK
    return t


class TestLeren:

    def test_blijvend_tekort_schuift_het_balanspunt_op(self):
        b = _bias()
        _loop(b, 3 + 24)          # 3 uur wachten, dan een etmaal leren
        # 10 · 0,38 K · 24 u / 48 u = 1,9 K
        assert b.bias_k == pytest.approx(1.9, abs=0.02)

    def test_te_warm_schuift_omlaag(self):
        b = _bias()
        _loop(b, 3 + 24, kamer=20.2)
        assert b.bias_k == pytest.approx(-1.0, abs=0.02)

    def test_begrensd(self):
        b = _bias()
        _loop(b, 24 * 10)
        assert b.bias_k == 3.0

    def test_niet_zonder_sturing(self):
        """Stuurt het advies niets, dan komt het tekort niet van het advies."""
        b = _bias()
        _loop(b, 48, steering=False)
        assert b.bias_k == 0.0

    @pytest.mark.parametrize("kw", [
        {"capped": True},
        {"stationary_w": 0.0},
        {"kamer": 18.5},           # 1,5 K: een opwarmtraject, geen vaste afwijking
    ])
    def test_niet_leren(self, kw):
        b = _bias()
        _loop(b, 48, **kw)
        assert b.bias_k == 0.0

    def test_eerst_wachten_na_een_setpointwissel(self):
        b = _bias()
        t = _loop(b, 24, setpoint=17.0, kamer=17.0)
        assert b.bias_k == 0.0
        # Ochtend: setpoint naar 20, kamer nog 19,6. Drie uur lang niets.
        _loop(b, 2.9, start=t)
        assert b.bias_k == 0.0
        _loop(b, 1.0, start=t + 3.0 * H)
        assert b.bias_k > 0.0

    def test_een_gat_telt_niet_als_tijd(self):
        b = _bias()
        t = _loop(b, 4)
        na = b.bias_k
        b.update(t + 6 * H, 20.0, 19.62, 3000.0, False, True)
        assert b.bias_k == na

    def test_startwaarde_binnen_de_grens(self):
        assert _bias(bias_k=7.0).bias_k == 3.0


class TestHerstart:
    """Een herstart is geen setpointwissel.

    Tot v0.10.7-beta.14 telde de eerste tik na elke herstart als wissel, en
    leerde de correctie na elke update drie uur niets.
    """

    def test_bewaard_setpoint_leert_meteen_verder(self):
        voor = _bias()
        _loop(voor, 5)                          # setpoint al uren gelijk
        na = _bias(**voor.state())
        t = 6 * H
        na.update(t, 20.0, 19.62, 3000.0, False, True)       # eerste tik: geen dt
        na.update(t + TIK, 20.0, 19.62, 3000.0, False, True)
        assert na.bias_k > voor.bias_k

    def test_gewisseld_tijdens_de_herstart_wacht_wel(self):
        voor = _bias()
        _loop(voor, 5, setpoint=17.0, kamer=17.0)
        na = _bias(**voor.state())
        _loop(na, 2.9, start=6 * H)             # nu 20, kamer 19,62
        assert na.bias_k == 0.0
        _loop(na, 1.0, start=6 * H + 3.0 * H)
        assert na.bias_k > 0.0

    def test_wissel_vlak_voor_de_herstart_houdt_zijn_wachttijd(self):
        voor = _bias()
        _loop(voor, 5, setpoint=17.0, kamer=17.0)
        _loop(voor, 1.0, start=5.5 * H)         # wissel om 5,5 u
        na = _bias(**voor.state())
        _loop(na, 1.0, start=7 * H)             # tot 8 u: nog geen 3 uur
        assert na.bias_k == 0.0
        _loop(na, 1.0, start=8.6 * H)
        assert na.bias_k > 0.0

    def test_oude_opslag_zonder_setpoint_wacht_een_keer(self):
        b = _bias(bias_k=0.4)
        _loop(b, 2.9)
        assert b.bias_k == 0.4
        _loop(b, 1.0, start=3.0 * H)
        assert b.bias_k > 0.4

    def test_setpoint_zonder_tijdstip_telt_niet(self):
        b = _bias(setpoint=20.0)
        assert b.state()["setpoint"] is None


class TestRegelkring:
    """Een huis dat 3,8 K meer balanspunt nodig heeft dan de analyse zegt.

    U 218,7 W/K, C 30 kWh/K (zware vloer), buiten 1 °C. Alleen de inhaalterm:
    de kamer blijft 3,8/11 = 0,35 K onder het setpoint. Met de correctie
    (grens 3 K) blijft er 0,8/11 = 0,07 K over, zonder doorschieten.
    """

    U, C, T_BAL, MIS, T_OUT, SP = 218.7, 30_000.0, 16.46, 3.8, 1.0, 20.0

    def _sim(self, dagen, met_correctie):
        b = _bias()
        t_in, t, laagste_na_dag3, hoogste = 19.0, 0.0, None, 0.0
        while t < dagen * 24 * H:
            bias = b.bias_k if met_correctie else 0.0
            d = advice_demand(self.U, self.T_BAL + bias, self.T_OUT, t_in, self.SP, 10.0, None)
            q = max(0.0, d.capped_w)
            verlies = self.U * (t_in - self.T_OUT) - self.U * (self.SP - self.T_BAL - self.MIS)
            t_in += (q - verlies) * (TIK / H) / self.C
            b.update(t, self.SP, t_in, d.stationary_w, d.capped, True)
            hoogste = max(hoogste, t_in)
            t += TIK
        return t_in, b.bias_k, hoogste

    def test_alleen_inhaalterm_laat_een_afwijking_staan(self):
        t_in, _, _ = self._sim(10, met_correctie=False)
        assert self.SP - t_in == pytest.approx(self.MIS / 11, abs=0.01)

    def test_met_correctie_weg_en_zonder_doorschieten(self):
        t_in, bias, hoogste = self._sim(10, met_correctie=True)
        assert bias == 3.0
        assert self.SP - t_in == pytest.approx((self.MIS - 3.0) / 11, abs=0.01)
        assert hoogste < self.SP


class TestInHetAdvies:

    def test_balanspunt_schuift_mee(self):
        sensor = _mpc_sensor(**{OUTDOOR: 1.0, INDOOR: 19.62, SETPOINT: 20.0})
        u, t_bal = sensor._heat_loss_line()
        sensor.balance_bias = _new_balance_bias(1.5)
        assert sensor._heat_loss_line() == (u, pytest.approx(t_bal + 1.5))

    def test_zonder_bias_het_oude_gedrag(self):
        sensor = _mpc_sensor()
        assert not hasattr(sensor, "balance_bias")
        assert sensor._heat_loss_line() is not None

    def test_leert_alleen_als_max_water_op_dit_advies_staat(self):
        sensor = _mpc_sensor(**{OUTDOOR: 1.0, INDOOR: 19.62, SETPOINT: 20.0})
        sensor.balance_bias = _new_balance_bias()
        sensor._balance_bias_saved = 0.0
        sensor._balance_bias_store = MagicMock()
        sensor.steering = MagicMock(last_knob_states=("stooklijn", "uit", False))
        assert not sensor._steers_max_water()
        sensor.steering.last_knob_states = ("mpc", "uit", False)
        assert sensor._steers_max_water()
        sensor.steering = None
        assert not sensor._steers_max_water()

    def test_setpointwissel_wordt_bewaard(self, monkeypatch):
        """Ook zonder dat de bias beweegt: anders kent hij de wissel na een herstart niet."""
        from custom_components.quatt_stooklijn import sensor as mod

        sensor = _mpc_sensor(**{OUTDOOR: 1.0, INDOOR: 20.0, SETPOINT: 20.0})
        sensor.balance_bias = _new_balance_bias(0.5, 18.0, 0.0)
        sensor._balance_bias_saved = 0.5
        sensor._balance_bias_saved_setpoint = (18.0, 0.0)
        sensor._balance_bias_store = MagicMock()
        sensor.steering = MagicMock(last_knob_states=("mpc", "uit", False))
        monkeypatch.setattr(mod.dt_util, "utcnow",
                            lambda: MagicMock(timestamp=lambda: 10 * H))
        with _sources():
            sensor._update_balance_bias()
        assert sensor.balance_bias.bias_k == 0.5
        save = sensor._balance_bias_store.async_delay_save
        save.assert_called_once()
        assert save.call_args.args[0]() == {
            "bias_k": 0.5, "setpoint": 20.0, "setpoint_since": 10 * H,
        }
        save.reset_mock()
        with _sources():
            sensor._update_balance_bias()
        save.assert_not_called()

    def test_tik_werkt_bij_en_bewaart(self, monkeypatch):
        from custom_components.quatt_stooklijn import sensor as mod

        sensor = _mpc_sensor(**{OUTDOOR: 1.0, INDOOR: 19.62, SETPOINT: 20.0})
        sensor.balance_bias = _new_balance_bias(0.5)
        sensor.balance_bias._setpoint, sensor.balance_bias._setpoint_since = 20.0, 0.0
        sensor._balance_bias_saved = 0.5
        sensor._balance_bias_store = MagicMock()
        sensor.steering = MagicMock(last_knob_states=("mpc", "uit", False))
        klok = iter([10 * H, 10 * H + TIK] + [10 * H + TIK * k for k in range(2, 40)])
        monkeypatch.setattr(mod.dt_util, "utcnow",
                            lambda: MagicMock(timestamp=lambda: next(klok)))
        with _sources():
            for _ in range(30):
                sensor._update_balance_bias()
        assert sensor.balance_bias.bias_k > 0.5
        sensor._balance_bias_store.async_delay_save.assert_called()
