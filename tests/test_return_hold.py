"""Tests voor het vasthouden van de retour rond een ontdooiing (ReturnHold).

Een aanvoeradvies is retour + vraag / (1,16 · debiet). Bij een ontdooiing haalt
de warmtepomp warmte uit het water, dat koude water komt een paar minuten later
als retour terug, en het advies zakte mee (Tweakers 24-11-2026: elke ontdooiing
zichtbaar in beide adviezen). Gemeten op het testhuis, 17–24 nov 2026, 109
ontdooiingen: adviesdip mediaan 2,4 K, p90 4,8 K; met het vasthouden 0,2 en 1,1 K.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest

from custom_components.quatt_stooklijn.const import DEFROST_POWER_W
from custom_components.quatt_stooklijn.helpers import ReturnHold
from custom_components.quatt_stooklijn.sensor import _advice_return, _defrosting

from tests.test_room_setpoint import (
    FLOW,
    HEAT,
    INDOOR,
    OUTDOOR,
    RETURN,
    SETPOINT,
    _State,
    _mpc_sensor,
    _sources,
)

T0 = datetime(2026, 11, 23, 6, 25, tzinfo=timezone.utc)


def _min(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def _voed(hold: ReturnHold, t_return: float, minuten, start: float = 0.0) -> float:
    """Elke minuut dezelfde retour zonder ontdooiing; geeft het eindmoment."""
    for m in range(int(minuten) + 1):
        hold.update(t_return, False, _min(start + m))
    return start + minuten


class TestReturnHold:

    def test_zonder_ontdooiing_de_gemeten_retour(self):
        h = ReturnHold()
        _voed(h, 29.0, 30)
        assert h.held is None
        assert h.effective(28.5) == 28.5

    def test_geen_meting_blijft_geen_meting(self):
        h = ReturnHold()
        _voed(h, 29.0, 5)
        h.update(None, True, _min(6))
        assert h.effective(None) is None

    def test_hoogste_retour_van_de_minuten_ervoor(self):
        """Het vermogen gaat pas onder de grens als de retour al zakt."""
        h = ReturnHold()
        _voed(h, 29.4, 10)
        h.update(28.9, True, _min(11))
        assert h.held == 29.4
        assert h.effective(26.4) == 29.4

    def test_oude_waarden_tellen_niet(self):
        h = ReturnHold(lookback_min=10)
        h.update(35.0, False, _min(0))
        _voed(h, 29.0, 15, start=5)
        h.update(29.0, True, _min(21))
        assert h.held == 29.0

    def test_nooit_lager_dan_gemeten(self):
        h = ReturnHold()
        _voed(h, 29.0, 5)
        h.update(29.0, True, _min(6))
        assert h.effective(31.0) == 31.0

    def test_herstel_telt_pas_na_de_transittijd(self):
        """Vlak na het einde staat de retour nog op de oude waarde: geen herstel."""
        h = ReturnHold(transit_min=5)
        _voed(h, 29.4, 10)
        h.update(29.4, True, _min(11))
        h.update(29.4, False, _min(12))       # einde ontdooiing, water nog onderweg
        assert h.held == 29.4
        h.update(27.0, False, _min(14))       # nu komt het koude water
        assert h.effective(27.0) == 29.4
        h.update(29.3, False, _min(25))       # hersteld binnen 0,2 K
        assert h.held is None
        assert h.effective(29.3) == 29.3

    def test_losgelaten_na_de_rustperiode(self):
        h = ReturnHold(after_min=30)
        _voed(h, 29.4, 10)
        h.update(29.0, True, _min(11))
        h.update(27.0, False, _min(40))
        assert h.held == 29.4
        h.update(27.0, False, _min(41))
        assert h.held is None

    def test_om_beurten_ontdooien_houdt_de_eerste_waarde(self):
        """Een duo: de tweede ontdooiing begint voor de retour hersteld is."""
        h = ReturnHold()
        _voed(h, 29.4, 10)
        h.update(29.0, True, _min(11))
        h.update(27.3, False, _min(18))
        h.update(28.6, True, _min(24))        # tweede unit
        assert h.held == 29.4

    def test_absolute_grens_bij_ketting_van_ontdooiingen(self):
        h = ReturnHold(max_min=90, after_min=30)
        _voed(h, 29.4, 10)
        m = 11
        while m < 11 + 100:
            h.update(27.5, True, _min(m))
            h.update(27.5, False, _min(m + 2))
            m += 13
        # Elke 13 minuten een nieuwe ontdooiing, nooit hersteld: na 90 minuten
        # laat hij los en rekent het advies weer met de gemeten retour.
        h.update(27.5, False, _min(m))
        assert h.held is None

    def test_tijdens_vasthouden_geen_nieuwe_monsters(self):
        """Na het loslaten begint de terugblik opnieuw, zonder het koude water."""
        h = ReturnHold(after_min=30)
        _voed(h, 29.4, 10)
        h.update(29.0, True, _min(11))
        for m in range(12, 43):
            h.update(26.0 + (m - 12) * 0.01, False, _min(m))
        assert h.held is None
        h.update(26.5, True, _min(44))
        assert h.held == pytest.approx(26.5)

    def test_dubbele_aanroep_is_onschadelijk(self):
        h = ReturnHold()
        _voed(h, 29.4, 10)
        h.update(29.0, True, _min(11))
        h.update(29.0, True, _min(11))
        assert h.held == 29.4

    def test_echte_dubbele_ontdooiing(self):
        """Testhuis 23-11-2026 06:28–06:58: beide units tegelijk, 5 K dip.

        Per minuut retour en thermisch vermogen, zoals gemeten. Zonder
        vasthouden zakte het MPC-advies van 35,6 naar 30,4 °C.
        """
        retour = [29.7] * 8 + [29.8, 29.7, 28.4, 26.4, 24.7, 24.8, 25.7, 25.9,
                              26.4, 27.0, 27.4, 27.7, 28.0, 28.2, 28.3, 28.4, 28.6,
                              28.7, 28.8, 28.8, 28.9, 29.0, 29.0]
        vermogen = [5800] * 8 + [-5054, -8787, -9957, 779, 6670] + [6000] * 18
        h = ReturnHold()
        eff = []
        for m, (r, p) in enumerate(zip(retour, vermogen)):
            h.update(r, p < DEFROST_POWER_W, _min(m))
            eff.append(h.effective(r))
        assert min(retour) == 24.7
        # Nooit onder de retour van vóór de ontdooiing; in het dal de hoogste
        # van de tien minuten ervoor.
        assert min(eff) == pytest.approx(29.7)
        assert eff[retour.index(24.7)] == pytest.approx(29.8)


class TestInDeAdviezen:

    def _sensor(self, **states):
        sensor = _mpc_sensor(**states)
        sensor.coordinator.return_hold = ReturnHold()
        sensor.coordinator.defrost_entities = (monotonic(), ["binary_sensor.hp1_defrost"])
        known = {k: _State(v) for k, v in states.items()}
        sensor.hass.states.get = lambda e: known.get(e)
        return sensor, known

    def _advies(self, sensor, minuut):
        with _sources(), patch(
            "custom_components.quatt_stooklijn.sensor.dt_util.utcnow",
            return_value=_min(minuut),
        ):
            return sensor.native_value

    def test_zonder_return_hold_het_oude_gedrag(self):
        coordinator = MagicMock(spec=["data"])
        assert _advice_return(MagicMock(), coordinator, MagicMock(), 27.0) == 27.0

    def test_vlag_of_vermogen(self):
        sensor, known = self._sensor(**{HEAT: 5000.0})
        with _sources():
            assert not _defrosting(sensor.hass, sensor.coordinator, sensor._entry)
            known["binary_sensor.hp1_defrost"] = _State("on")
            assert _defrosting(sensor.hass, sensor.coordinator, sensor._entry)
            known["binary_sensor.hp1_defrost"] = _State("off")
            known[HEAT] = _State(-3000.0)
            assert _defrosting(sensor.hass, sensor.coordinator, sensor._entry)
            known[HEAT] = _State(-200.0)       # stilstand of opstarten
            assert not _defrosting(sensor.hass, sensor.coordinator, sensor._entry)

    def test_mpc_advies_zakt_niet_mee(self):
        states = {OUTDOOR: 3.0, RETURN: 29.7, FLOW: 800.0, INDOOR: 19.9,
                  SETPOINT: 20.0, HEAT: 5800.0}
        sensor, known = self._sensor(**states)
        voor = None
        for m in range(10):
            voor = self._advies(sensor, m)
        known[HEAT] = _State(-8000.0)
        self._advies(sensor, 10)
        known[HEAT] = _State(6000.0)
        known[RETURN] = _State(24.7)
        na = self._advies(sensor, 13)
        assert na == pytest.approx(voor)
        assert sensor.coordinator.return_hold.held == 29.7
        # Ter vergelijking: met de gemeten retour 5 K lager.
        sensor.coordinator.return_hold = ReturnHold()
        assert self._advies(sensor, 14) == pytest.approx(voor - 5.0, abs=0.05)
