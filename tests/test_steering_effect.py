"""Sturingseffect: meten wat de knoppen opleveren (v0.10.7-beta.5).

Niet hoe dicht een advies bij de aanvoer van de CiC ligt, maar comfort,
verbruik, starts, gas en of de max. aanvoertemperatuur echt begrenst — per instelling van de
knoppen, zodat weken met verschillende instellingen naast elkaar te zetten zijn.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.quatt_stooklijn.analysis.steering_effect import (
    MAX_STEP_MINUTES,
    MIXED,
    Day,
    Sample,
    config_label,
    counts_for_comparison,
    summarize,
)
from custom_components.quatt_stooklijn.cycling import CycleTracker, Run
from custom_components.quatt_stooklijn.const import (
    CONF_CH_MAX_WATER_ENABLED,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_SOURCE,
    DOMAIN,
)
from custom_components.quatt_stooklijn.discovery import (
    ROLE_BOILER_HEAT,
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
)
from custom_components.quatt_stooklijn.steering_effect import QuattSteeringEffectSensor

BASIS = config_label("uit", "uit", False)
PLAFOND_MPC = config_label("mpc", "uit", False)


def _sample(config=BASIS, **kw):
    base = dict(t_indoor=20.0, setpoint=20.0, t_outdoor=5.0, p_el_w=1000.0,
                p_heat_w=4000.0, p_gas_w=0.0, supply=35.0, ceiling=None,
                flow_lph=800.0, base_temp=16.0)
    base.update(kw)
    return Sample(config=config, **base)


def _dag(config=BASIS, uren=24, **kw) -> Day:
    d = Day(date="2026-11-20")
    for _ in range(int(uren * 12)):
        d.add(_sample(config, **kw), 5.0, 30.0)
    return d


class TestDag:
    def test_energie_en_graaddagen(self):
        rec = _dag().record(starts=12)
        assert rec["kwh_stroom"] == pytest.approx(24.0)
        assert rec["kwh_warmte"] == pytest.approx(96.0)
        assert rec["cop"] == pytest.approx(4.0)
        # 24 u × (16 − 5) K = 264 graaduren = 11 graaddagen
        assert rec["graaddagen"] == pytest.approx(11.0)
        assert rec["stookuren"] == pytest.approx(24.0)
        assert rec["starts"] == 12

    def test_comfort(self):
        rec = _dag(t_indoor=19.6).record(None)
        assert rec["kamer_afwijking_k"] == pytest.approx(-0.4)
        assert rec["uren_te_koud"] == pytest.approx(24.0)
        assert rec["uren_te_warm"] == 0

    def test_kleine_afwijking_is_niet_te_koud(self):
        assert _dag(t_indoor=19.8).record(None)["uren_te_koud"] == 0

    def test_te_warm(self):
        assert _dag(t_indoor=20.6).record(None)["uren_te_warm"] == pytest.approx(24.0)

    def test_tegen_max_aanvoertemp(self):
        d = Day(date="2026-11-20")
        for i in range(12):
            # helft tegen de max. aanvoertemp (binnen 1 K), helft ruim eronder
            d.add(_sample(supply=34.5 if i % 2 else 30.0, ceiling=35.0), 5.0, 30.0)
        assert d.record(None)["tegen_max_aanvoer_pct"] == pytest.approx(50.0)

    def test_zonder_max_aanvoertemp_geen_cijfer(self):
        assert _dag(uren=1).record(None)["tegen_max_aanvoer_pct"] is None

    def test_niet_stoken_telt_niet_als_stookuur(self):
        d = Day(date="2026-11-20")
        d.add(_sample(p_heat_w=100.0), 5.0, 30.0)
        d.add(_sample(flow_lph=10.0), 5.0, 30.0)
        assert d.heating_min == 0

    def test_gat_telt_hooguit_tien_minuten(self):
        d = Day(date="2026-11-20")
        d.add(_sample(), 180.0, 30.0)
        assert d.minutes == MAX_STEP_MINUTES

    def test_ontbrekende_stroommeting_geeft_geen_kwh(self):
        d = Day(date="2026-11-20")
        for i in range(12):
            d.add(_sample(p_el_w=None if i < 4 else 1000.0), 5.0, 30.0)
        rec = d.record(None)
        assert rec["kwh_stroom"] is None
        assert rec["cop"] is None

    def test_gas(self):
        assert _dag(uren=1, p_gas_w=2000.0).record(None)["kwh_gas"] == pytest.approx(2.0)

    def test_heen_en_terug_bewaren(self):
        d = _dag(uren=2)
        terug = Day.from_dict(d.to_dict())
        assert terug.record(3) == d.record(3)


class TestInstelling:
    def test_dominante_instelling(self):
        d = Day(date="2026-11-20")
        for i in range(100):
            d.add(_sample(PLAFOND_MPC if i < 85 else BASIS), 5.0, 30.0)
        assert d.record(None)["instelling"] == PLAFOND_MPC

    def test_gemengde_dag(self):
        d = Day(date="2026-11-20")
        for i in range(100):
            d.add(_sample(PLAFOND_MPC if i < 60 else BASIS), 5.0, 30.0)
        assert d.record(None)["instelling"] == MIXED

    def test_label_gebruikt_de_namen_uit_de_opties(self):
        assert config_label("mpc", "stooklijn", False) == (
            "max. aanvoertemp: MPC · soundslider: Stooklijn · warmtevraag OpenQuatt: uit"
        )

    def test_label_gepauzeerd(self):
        assert config_label("gepauzeerd", "uit", True) == (
            "max. aanvoertemp: gepauzeerd (warmtevraag stuurt) · soundslider: uit"
            " · warmtevraag OpenQuatt: aan"
        )


class TestVergelijk:
    def test_alleen_hele_stookdagen_met_een_instelling(self):
        heel = _dag().record(None)
        assert counts_for_comparison(heel)
        assert not counts_for_comparison(_dag(uren=12).record(None))          # te kort
        assert not counts_for_comparison(_dag(t_outdoor=15.5).record(None))   # < 1 graaddag
        gemengd = dict(heel, instelling=MIXED)
        assert not counts_for_comparison(gemengd)

    def test_per_instelling_sommen_eerst(self):
        """kWh per graaddag over de som, niet het gemiddelde van daggetallen."""
        koud = _dag(PLAFOND_MPC, t_outdoor=0.0, p_el_w=2000.0).record(None)   # 48 kWh, 16 gd
        zacht = _dag(PLAFOND_MPC, t_outdoor=14.0, p_el_w=250.0).record(None)  # 6 kWh, 2 gd
        s = summarize([koud, zacht])[PLAFOND_MPC]
        assert s["dagen"] == 2
        assert s["kwh_stroom_per_graaddag"] == pytest.approx(54 / 18, abs=0.001)

    def test_twee_instellingen_naast_elkaar(self):
        recs = [_dag(BASIS).record(10), _dag(PLAFOND_MPC, t_indoor=19.6).record(30)]
        s = summarize(recs)
        assert set(s) == {BASIS, PLAFOND_MPC}
        assert s[BASIS]["uren_te_koud_per_dag"] == 0
        assert s[PLAFOND_MPC]["uren_te_koud_per_dag"] == pytest.approx(24.0)
        assert s[PLAFOND_MPC]["starts_per_stookuur"] == pytest.approx(30 / 24, abs=0.01)

    def test_gas_aandeel(self):
        s = summarize([_dag(p_gas_w=1000.0, p_heat_w=3000.0).record(None)])[BASIS]
        assert s["gas_aandeel_pct"] == pytest.approx(25.0)


class TestStartsPerDag:
    def test_starts_between(self):
        t0 = datetime(2026, 11, 20, tzinfo=timezone.utc)
        tr = CycleTracker([Run(start=t0 + timedelta(hours=h)) for h in (-1, 2, 23, 25)])
        assert tr.starts_between(t0, t0 + timedelta(days=1)) == 2


# ---------------------------------------------------------------------------
# De sensor: stand van de knoppen en het afsluiten van een dag
# ---------------------------------------------------------------------------

ROLES = {
    ROLE_INDOOR_TEMP: "sensor.kamer", ROLE_OUTDOOR_TEMP: "sensor.buiten",
    ROLE_POWER_INPUT: "sensor.stroom", ROLE_TOTAL_POWER: "sensor.warmte",
    ROLE_BOILER_HEAT: "sensor.gas", ROLE_SUPPLY_TEMP: "sensor.aanvoer",
    ROLE_FLOW_RATE: "sensor.debiet",
}
WAARDEN = {"sensor.kamer": 20.1, "sensor.buiten": 4.0, "sensor.stroom": 900.0,
           "sensor.warmte": 3600.0, "sensor.gas": 0.0, "sensor.aanvoer": 33.0,
           "sensor.debiet": 800.0, "number.max_aanvoer": 34.0}


def _sensor(options=None, switch_state=None, ctrl=None):
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    states = {}
    if switch_state is not None:
        states["switch.compensatie"] = SimpleNamespace(state=switch_state)
    hass.states.get = lambda eid: states.get(eid)
    entry = MagicMock(entry_id="e1", data={}, options=options or {})
    if ctrl is not None:
        hass.data[DOMAIN]["e1_ch_max_water"] = ctrl
    mpc = MagicMock()
    mpc._resolve_room_setpoint.return_value = (20.5, "thermostaat")
    coordinator = MagicMock(data=SimpleNamespace(heat_loss_hp=SimpleNamespace(balance_point=16.6)))
    starts = MagicMock()
    starts.starts_between.return_value = 7
    with patch("custom_components.quatt_stooklijn.steering_effect.Store"):
        s = QuattSteeringEffectSensor(hass, coordinator, entry, mpc, starts)
    s.hass = hass
    s.async_write_ha_state = MagicMock()
    s._store = MagicMock()
    s._store.async_save = AsyncMock()
    return s


def _patches(demand=False):
    return (
        patch("custom_components.quatt_stooklijn.steering_effect.async_source_entity",
              side_effect=lambda hass, entry_id, role, **kw: ROLES.get(role)),
        patch("custom_components.quatt_stooklijn.steering_effect.get_float_state",
              side_effect=lambda hass, eid: WAARDEN.get(eid)),
        patch("custom_components.quatt_stooklijn.steering_effect.resolve_own_entity_id",
              return_value="switch.compensatie"),
        patch.object(QuattSteeringEffectSensor, "_demand_link_active", return_value=demand),
    )


def _meet(sensor, demand=False):
    p = _patches(demand)
    with p[0], p[1], p[2], p[3]:
        return sensor._sample()


class TestStandVanDeKnoppen:
    def test_alles_uit(self):
        s = _meet(_sensor())
        assert s.config == BASIS
        assert s.setpoint == 20.5
        assert s.base_temp == 16.6
        assert s.p_el_w == 900.0

    def test_max_aanvoertemp_met_bron_en_waarde(self):
        ctrl = SimpleNamespace(_source="mpc", target_entity="number.max_aanvoer",
                               _resolve_number_entity=lambda: None)
        s = _meet(_sensor({CONF_CH_MAX_WATER_ENABLED: True}, ctrl=ctrl))
        assert s.config == config_label("mpc", "uit", False)
        assert s.ceiling == 34.0

    def test_max_aanvoertemp_gepauzeerd_bij_warmtevraag(self):
        ctrl = SimpleNamespace(_source="mpc", target_entity="number.max_aanvoer",
                               _resolve_number_entity=lambda: None)
        s = _meet(_sensor({CONF_CH_MAX_WATER_ENABLED: True}, ctrl=ctrl), demand=True)
        assert s.config == config_label("gepauzeerd", "uit", True)
        assert s.ceiling is None

    def test_geluid_alleen_als_de_switch_aan_staat(self):
        opts = {CONF_SOUND_LEVEL_ENABLED: True, CONF_SOUND_LEVEL_SOURCE: "stooklijn"}
        assert _meet(_sensor(opts, switch_state="on")).config == config_label("uit", "stooklijn", False)
        assert _meet(_sensor(opts, switch_state="off")).config == BASIS

    def test_geluid_standaardbron(self):
        opts = {CONF_SOUND_LEVEL_ENABLED: True}
        assert _meet(_sensor(opts, switch_state="on")).config == config_label("uit", "mpc", False)


class TestDagAfsluiten:
    def _tik(self, sensor, moment):
        p = _patches()
        with p[0], p[1], p[2], p[3], patch(
            "custom_components.quatt_stooklijn.steering_effect.dt_util.utcnow",
            return_value=moment,
        ):
            asyncio.run(sensor._async_sample())

    def test_middernacht_sluit_de_dag_met_starts(self):
        s = _sensor()
        s._loaded = True
        t = datetime(2026, 11, 20, 23, 50, tzinfo=timezone.utc)
        self._tik(s, t)
        self._tik(s, t + timedelta(minutes=5))
        self._tik(s, t + timedelta(minutes=15))   # 21 nov
        assert len(s._days) == 1
        assert s._days[0]["datum"] == "2026-11-20"
        assert s._days[0]["starts"] == 7
        assert s._day.date == "2026-11-21"
        s._store.async_save.assert_awaited()

    def test_niet_opslaan_na_mislukte_lezing(self):
        s = _sensor()
        s._loaded = False
        t = datetime(2026, 11, 20, 23, 50, tzinfo=timezone.utc)
        self._tik(s, t)
        self._tik(s, t + timedelta(minutes=15))
        s._store.async_save.assert_not_awaited()
        s._store.async_delay_save.assert_not_called()

    def test_uitgestelde_opslag_een_keer_ingepland(self):
        s = _sensor()
        s._loaded = True
        t = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        for i in range(4):
            self._tik(s, t + timedelta(minutes=5 * i))
        assert s._store.async_delay_save.call_count == 1

    def test_attributen(self):
        s = _sensor()
        s._loaded = True
        self._tik(s, datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))
        attrs = s.extra_state_attributes
        assert s.native_value == BASIS
        assert attrs["vandaag"]["datum"] == "2026-11-20"
        assert attrs["dagen_gemeten"] == 0
        assert attrs["drempels"]["graaddag_basis_c"] == 16.6
        assert set(QuattSteeringEffectSensor._unrecorded_attributes) == {
            "vandaag", "dagen", "per_instelling",
        }


class TestHuismodelBron:
    def _meet_1r1c(self, terugval):
        opts = {CONF_SOUND_LEVEL_ENABLED: True, CONF_SOUND_LEVEL_SOURCE: "1r1c"}
        sensor = _sensor(opts, switch_state="on")
        with patch(
            "custom_components.quatt_stooklijn.steering_effect.is_fallback",
            return_value=terugval,
        ):
            return _meet(sensor)

    def test_huismodel_online(self):
        assert self._meet_1r1c(False).config == config_label("uit", "1r1c", False)

    def test_terugval_apart_zichtbaar(self):
        s = self._meet_1r1c(True)
        assert s.config == config_label("uit", "1r1c_terugval", False)
        assert "terugval op MPC" in s.config
