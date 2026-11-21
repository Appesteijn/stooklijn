"""Hybride en ontspoorde huismodellen (v0.10.7-beta.6).

Tweakers 21-11-2026: een hybride met een Intergas-ketel had na 132 uur een
huismodel met C = −674 Wh/K, U = 700 W/K en g = 25,7. Het model rekende
alleen met de warmtepomp; de warmteverliesanalyse telde de ketel al wel mee.
Nagebootst: met een onzichtbare ketel die bijspringt werd C bij 20 van de 20
vloerverwarmingshuizen negatief, zonder ketel bij geen enkel.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.quatt_stooklijn.analysis.thermal_model import OnlineRCModel
from custom_components.quatt_stooklijn.highres_log import HighResLog
from custom_components.quatt_stooklijn.thermal_store import ThermalModelStore
from tests.test_room_setpoint import _mpc_sensor

HEAT = "sensor.warmte"
BOILER = "sensor.ketel"


def _broken() -> OnlineRCModel:
    m = OnlineRCModel()
    m._rls.initialise_from_physics(U=700.0, C=-674.0, g=25.7)
    m._rls.n_updates = 132
    m._rls.n_updates_per_param[:] = 132
    return m


class TestWarmteNaarHetHuis:
    def _heat(self, heat, boiler, boiler_entity=BOILER):
        from unittest.mock import patch
        sensor = _mpc_sensor()
        values = {HEAT: heat, BOILER: boiler}
        with patch(
            "custom_components.quatt_stooklijn.sensor.async_source_entity",
            side_effect=lambda hass, entry_id, role, **kw: {
                "total_power": HEAT, "boiler_heat": boiler_entity,
            }.get(role),
        ), patch(
            "custom_components.quatt_stooklijn.sensor.get_float_state",
            side_effect=lambda hass, eid: values.get(eid),
        ):
            return sensor._house_heat_w()

    def test_warmtepomp_plus_ketel(self):
        assert self._heat(3000.0, 6000.0) == 9000.0

    def test_zonder_ketelwarmte_alleen_de_warmtepomp(self):
        assert self._heat(3000.0, 0.0) == 3000.0
        assert self._heat(3000.0, None) == 3000.0

    def test_zonder_ketelmeting_alleen_de_warmtepomp(self):
        assert self._heat(3000.0, 6000.0, boiler_entity=None) == 3000.0

    def test_geen_negatieve_warmte(self):
        assert self._heat(-50.0, -10.0) == 0.0


class TestOntspoord:
    def test_negatieve_c_is_ontspoord(self):
        assert _broken().is_broken
        assert not OnlineRCModel().is_broken

    def test_andere_waarden_buiten_bereik_niet(self):
        """U, g of P_c buiten bereik: daar kan RLS uit terugkomen."""
        m = OnlineRCModel()
        m._rls.initialise_from_physics(U=900.0, C=8000.0, g=-2.0)
        assert not m.is_broken
        assert not m.is_converged

    def test_laden_begint_opnieuw(self):
        store = ThermalModelStore.__new__(ThermalModelStore)
        store._store = MagicMock()
        store._store.async_load = AsyncMock(return_value=_broken().to_dict())
        store.model = None
        asyncio.run(store.async_load())
        assert store.model._rls.n_updates == 0
        assert not store.model.is_broken

    def test_gezond_model_blijft_bij_laden(self):
        m = OnlineRCModel()
        m._rls.initialise_from_physics(U=280.0, C=12000.0, g=3.0)
        m._rls.n_updates = 500
        store = ThermalModelStore.__new__(ThermalModelStore)
        store._store = MagicMock()
        store._store.async_load = AsyncMock(return_value=m.to_dict())
        asyncio.run(store.async_load())
        assert store.model._rls.n_updates == 500

    def test_tijdens_bedrijf_opnieuw_vanaf_de_warmteverliesanalyse(self):
        sensor = _mpc_sensor()
        sensor._thermal_store = MagicMock(model=_broken())
        sensor._thermal_store.async_save = AsyncMock()
        sensor._refresh_u_prior = MagicMock()
        assert asyncio.run(sensor._async_reset_broken_model()) is True
        nieuw = sensor._thermal_store.model
        assert nieuw._rls.n_updates == 0
        # U uit de warmteverliesanalyse van _mpc_sensor (250 W/K)
        assert nieuw.raw_params["U"] == pytest.approx(250.0)
        sensor._refresh_u_prior.assert_called_once()
        sensor._thermal_store.async_save.assert_awaited_once()

    def test_gezond_model_niet_aangeraakt(self):
        sensor = _mpc_sensor()
        gezond = OnlineRCModel()
        sensor._thermal_store = MagicMock(model=gezond)
        sensor._thermal_store.async_save = AsyncMock()
        assert asyncio.run(sensor._async_reset_broken_model()) is False
        assert sensor._thermal_store.model is gezond


# ---------------------------------------------------------------------------
# Uurgemiddelde in plaats van momentopname
# ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)


class TestUurgemiddeldeUitHetLogboek:
    def _log(self, heats):
        log = HighResLog()
        for i, q in enumerate(heats):
            log.record(T0 + timedelta(minutes=5 * (i + 1)), 20.0, 5.0, q, 0.0)
        return log

    def test_gemiddelde_over_het_interval(self):
        # een ketelstoot van 15 minuten midden in een uur warmtepomp
        heats = [3000.0] * 5 + [11000.0] * 3 + [3000.0] * 4
        mean, n = self._log(heats).mean_heat_between(T0, T0 + timedelta(hours=1))
        assert n == 12
        assert mean == pytest.approx(5000.0)

    def test_grenzen(self):
        """(since, until]: het monster op since hoort bij het vorige uur."""
        log = self._log([1000.0] * 24)
        mean, n = log.mean_heat_between(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
        assert n == 12

    def test_leeg_interval(self):
        assert self._log([1000.0] * 3).mean_heat_between(
            T0 + timedelta(hours=5), T0 + timedelta(hours=6)
        ) == (None, 0)


def _model_na_eerste_tik(q_snap):
    m = OnlineRCModel()
    m._rls.initialise_from_physics(U=280.0, C=12000.0, g=3.0)
    m.set_p_c_anchor(900.0)
    m.update(20.0, 5.0, q_snap, 0.0, T0)
    return m


class TestModelGebruiktHetGemiddelde:
    def test_gemiddelde_vervangt_de_momentopname(self):
        """Zelfde uur: met het gemiddelde leert het model iets anders dan met
        de momentopname van de vorige tik."""
        met_snap = _model_na_eerste_tik(0.0)
        met_gem = _model_na_eerste_tik(0.0)
        met_snap.update(20.3, 5.0, 0.0, 0.0, T0 + timedelta(hours=1))
        met_gem.update(20.3, 5.0, 0.0, 0.0, T0 + timedelta(hours=1), q_hp_interval_w=5000.0)
        assert not (met_snap._rls.theta == met_gem._rls.theta).all()

    def test_zonder_gemiddelde_hetzelfde_als_voorheen(self):
        a = _model_na_eerste_tik(3000.0)
        b = _model_na_eerste_tik(3000.0)
        a.update(20.1, 5.0, 3000.0, 0.0, T0 + timedelta(hours=1))
        b.update(20.1, 5.0, 3000.0, 0.0, T0 + timedelta(hours=1), q_hp_interval_w=None)
        assert (a._rls.theta == b._rls.theta).all()

    def test_stookuur_volgens_het_gemiddelde(self):
        """Momentopname 0 W, maar het uur stookte gemiddeld 4 kW: C mag leren.

        Met de momentopname zou C bevroren blijven (onder ANCHOR_MAX_Q_HP_W).
        """
        m = _model_na_eerste_tik(0.0)
        voor = m._rls.n_updates_per_param[2]
        m.update(20.3, 5.0, 0.0, 0.0, T0 + timedelta(hours=1), q_hp_interval_w=4000.0)
        assert m._rls.n_updates_per_param[2] == voor + 1


class TestSensorGeeftHetUurgemiddelde:
    def _sensor(self, n_monsters):
        sensor = _mpc_sensor()
        sensor._highres_loaded = True
        sensor._highres_store = MagicMock()
        log = HighResLog()
        for i in range(n_monsters):
            log.record(T0 + timedelta(minutes=5 * (i + 1)), 20.0, 5.0, 4000.0, 0.0)
        sensor._highres_store.log = log
        model = OnlineRCModel()
        model._prev_timestamp = T0
        sensor._thermal_store = MagicMock(model=model)
        return sensor

    def test_genoeg_monsters(self):
        assert self._sensor(12)._interval_heat_w(T0 + timedelta(hours=1)) == pytest.approx(4000.0)

    def test_te_weinig_monsters_momentopname(self):
        assert self._sensor(5)._interval_heat_w(T0 + timedelta(hours=1)) is None

    def test_logboek_niet_geladen(self):
        s = self._sensor(12)
        s._highres_loaded = False
        assert s._interval_heat_w(T0 + timedelta(hours=1)) is None
