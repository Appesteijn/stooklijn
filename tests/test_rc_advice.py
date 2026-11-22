"""De stuurbron "1r1c": aanvoeradvies uit het lerende huismodel (v0.10.7-beta.5).

Dat het 1R1C slechter stuurt dan de rechte lijn is in november 2026 alleen op
één huis vastgesteld, met een 1R1C dat toen nog een fout in P_c had. Als
kiesbare bron kan het zich op meer huizen bewijzen, gemeten met het
sturingseffect. Zolang het model niet online is, geeft hij het MPC-advies door.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.quatt_stooklijn import advice_sources as bron
from custom_components.quatt_stooklijn.analysis.steering_effect import config_key, config_label
from custom_components.quatt_stooklijn.analysis.thermal_model import (
    OnlineRCModel,
    rc_advice_demand,
)
from custom_components.quatt_stooklijn.const import (
    MPC_ADVICE_NO_DEMAND,
    MPC_ADVICE_OK,
)
from custom_components.quatt_stooklijn.sensor import QuattRcAdviceSensor
from tests.test_mpc_attributen_json import json_bytes
from tests.test_room_setpoint import (
    FLOW,
    INDOOR,
    OUTDOOR,
    RETURN,
    SETPOINT,
    _mpc_sensor,
    _sources,
)

U, C, G, P_C = 250.0, 6000.0, 5.0, 900.0


def _model(converged=True) -> OnlineRCModel:
    m = OnlineRCModel()
    m._rls.initialise_from_physics(U=U, C=C, g=G)
    m.set_p_c_anchor(P_C)
    n = 100 if converged else 0
    m._rls.n_updates = n
    m._rls.n_updates_per_param[:] = n
    return m


class TestRekensom:
    def test_stationair_volgens_het_model(self):
        """U·(setpoint − buiten) − g·zon − P_c, zonder tekort geen inhaal."""
        d = rc_advice_demand(_model(), 5.0, 20.0, 20.0, 100.0, 10.0, None)
        assert d.stationary_w == pytest.approx(U * 15 - G * 100 - P_C)
        assert d.catchup_w == 0
        assert d.total_w == pytest.approx(d.stationary_w)

    def test_zelfde_inhaalterm_als_mpc(self):
        d = rc_advice_demand(_model(), 5.0, 19.7, 20.0, 0.0, 10.0, None)
        assert d.catchup_w == pytest.approx(10 * U * 0.3)

    def test_lager_setpoint_minder_vraag(self):
        """Nachtverlaging telt mee; de rechte lijn kent geen setpoint."""
        dag = rc_advice_demand(_model(), 5.0, None, 20.0, 0.0, 10.0, None)
        nacht = rc_advice_demand(_model(), 5.0, None, 18.0, 0.0, 10.0, None)
        assert dag.stationary_w - nacht.stationary_w == pytest.approx(2 * U)

    def test_zon_verlaagt_de_vraag(self):
        donker = rc_advice_demand(_model(), 5.0, None, 20.0, 0.0, 10.0, None)
        zon = rc_advice_demand(_model(), 5.0, None, 20.0, 300.0, 10.0, None)
        assert donker.stationary_w - zon.stationary_w == pytest.approx(G * 300)

    def test_zon_onbekend_telt_als_geen_zon(self):
        a = rc_advice_demand(_model(), 5.0, None, 20.0, None, 10.0, None)
        b = rc_advice_demand(_model(), 5.0, None, 20.0, 0.0, 10.0, None)
        assert a.stationary_w == b.stationary_w

    def test_niet_onder_nul_en_geen_inhaal_zonder_vraag(self):
        d = rc_advice_demand(_model(), 18.0, 19.0, 20.0, 500.0, 10.0, None)
        assert d.stationary_w == 0
        assert d.catchup_w == 0

    def test_begrensd_op_capaciteit(self):
        d = rc_advice_demand(_model(), -10.0, 19.0, 20.0, 0.0, 10.0, 4000.0)
        assert d.capped_w == 4000.0
        assert d.capped is True


KOUD = {OUTDOOR: 2.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.8, SETPOINT: 20.5}


def _rc_sensor(converged=True, **states):
    mpc = _mpc_sensor(**states)
    mpc._thermal_store = MagicMock(model=_model(converged))
    mpc._get_current_solar_radiation_wm2 = lambda: 0.0
    sensor = QuattRcAdviceSensor.__new__(QuattRcAdviceSensor)
    sensor.hass = mpc.hass
    sensor._mpc = mpc
    return sensor, mpc


class TestSensor:
    def test_advies_uit_het_model(self):
        sensor, mpc = _rc_sensor(**KOUD)
        with _sources():
            waarde = sensor.native_value
            attrs = sensor.extra_state_attributes
            mpc_waarde = mpc.native_value
        assert attrs["terugval"] is False
        assert attrs["advies_status"] == MPC_ADVICE_OK
        # 250·18,5 − 900 + 10·250·0,7 = 5475 W, retour 28, 800 l/h
        assert attrs["vraag_w"] == pytest.approx(5475, abs=1)
        assert waarde == pytest.approx(28 + 5475 / (1.16 * 800), abs=0.1)
        assert waarde != mpc_waarde
        json_bytes(attrs)

    def test_terugval_op_mpc_zolang_het_model_leert(self):
        sensor, mpc = _rc_sensor(converged=False, **KOUD)
        with _sources():
            waarde = sensor.native_value
            attrs = sensor.extra_state_attributes
            mpc_waarde = mpc.native_value
        assert attrs["terugval"] is True
        assert waarde == mpc_waarde
        json_bytes(attrs)

    def test_warm_genoeg_geen_vraag(self):
        sensor, _ = _rc_sensor(**{**KOUD, OUTDOOR: 17.0, INDOOR: 21.5})
        with _sources():
            assert sensor.native_value is None
            assert sensor.extra_state_attributes["advies_status"] == MPC_ADVICE_NO_DEMAND


class TestTerugvalZichtbaar:
    def test_is_fallback_leest_het_attribuut(self):
        hass = MagicMock()
        hass.states.get = lambda eid: SimpleNamespace(state="31.0", attributes={"terugval": True})
        with patch(
            "custom_components.quatt_stooklijn.advice_sources.resolve_own_entity_id",
            return_value="sensor.mijn_1r1c",
        ):
            assert bron.is_fallback(hass, "e1", "1r1c") is True
        assert bron.is_fallback(None, "e1", "1r1c") is False

    def test_label_in_het_sturingseffect(self):
        assert config_label(config_key("1r1c", "uit", False)).startswith(
            "max. aanvoertemperatuur: Huismodel + kamer ·")
        assert "Huismodel + kamer, leert nog" in config_label(
            config_key("1r1c_terugval", "uit", False))
