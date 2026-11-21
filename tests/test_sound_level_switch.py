"""Tests for QuattSoundLevelSwitch compensation logic."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from custom_components.quatt_stooklijn.switch import (
    QuattSoundLevelSwitch,
    _SOUND_LEVELS,
)
from custom_components.quatt_stooklijn.const import MIN_FLOW_LPH
from custom_components.quatt_stooklijn.discovery import (
    ROLE_SOUND_DAY,
    ROLE_SOUND_NIGHT,
)

# De selects worden niet meer als constante geïmporteerd maar via de
# bronresolutie opgezocht, zodat ook installaties van vóór de v2→v3 migratie
# (``select.heatpump_cic_...``) bediend worden. De tests pinnen die resolutie
# hieronder vast op de ná-migratie namen.
_DAY_SOUND_ENTITY = "select.cic_day_max_sound_level"
_NIGHT_SOUND_ENTITY = "select.cic_night_max_sound_level"


@pytest.fixture(autouse=True)
def _pin_sound_selects():
    """Bronresolutie vastzetten voor elke test in dit bestand."""
    with patch(
        "custom_components.quatt_stooklijn.switch.async_source_entity",
        side_effect=lambda hass, entry_id, role, **kw: {
            ROLE_SOUND_DAY: _DAY_SOUND_ENTITY,
            ROLE_SOUND_NIGHT: _NIGHT_SOUND_ENTITY,
        }.get(role, f"sensor.{role}"),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_switch(max_day="normal", max_night="normal") -> QuattSoundLevelSwitch:
    """Create a minimal switch instance without a real HA."""
    from custom_components.quatt_stooklijn.const import (
        CONF_SOUND_LEVEL_MAX_DAY,
        CONF_SOUND_LEVEL_MAX_NIGHT,
    )
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = {
        CONF_SOUND_LEVEL_MAX_DAY: max_day,
        CONF_SOUND_LEVEL_MAX_NIGHT: max_night,
    }
    hass = MagicMock()
    sw = QuattSoundLevelSwitch(hass, coordinator, entry)
    sw.hass = hass
    sw.hass.services.async_call = AsyncMock()
    sw.async_write_ha_state = MagicMock()
    return sw


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# HP inactiviteit — 10-minuten drempel
# ---------------------------------------------------------------------------

class TestHpInactivityReset:

    def test_no_reset_before_10_minutes(self):
        """Reset mag NIET plaatsvinden bij minder dan 10 minuten inactiviteit."""
        sw = _make_switch()
        sw._current_level_idx = 0  # building87, afwijkend van max (normal=3)

        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=9, seconds=59)

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", return_value=0.0), \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt, \
             patch.object(sw, "_is_night", return_value=False):
            mock_dt.now.side_effect = [t0, t1]
            sw._hp_inactive_since = None

            _run(sw._async_do_compensation())
            assert sw._hp_inactive_since == t0
            sw.hass.services.async_call.assert_not_called()

            _run(sw._async_do_compensation())
            sw.hass.services.async_call.assert_not_called()

    def test_reset_after_10_minutes(self):
        """Reset moet plaatsvinden na exact 10 minuten inactiviteit."""
        sw = _make_switch()
        sw._current_level_idx = 0  # building87

        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_10min = t0 + timedelta(minutes=10)

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", return_value=0.0), \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt, \
             patch.object(sw, "_is_night", return_value=False):
            mock_dt.now.return_value = t0
            sw._hp_inactive_since = None
            _run(sw._async_do_compensation())

            mock_dt.now.return_value = t_10min
            _run(sw._async_do_compensation())

        sw.hass.services.async_call.assert_called()

    def test_no_reset_when_level_already_at_max(self):
        """Geen reset nodig als het niveau al op max staat, ook na 10+ minuten."""
        sw = _make_switch()
        sw._current_level_idx = 3  # normal = max

        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_15min = t0 + timedelta(minutes=15)

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", return_value=0.0), \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt, \
             patch.object(sw, "_is_night", return_value=False):
            mock_dt.now.return_value = t0
            sw._hp_inactive_since = None
            _run(sw._async_do_compensation())

            mock_dt.now.return_value = t_15min
            _run(sw._async_do_compensation())

        sw.hass.services.async_call.assert_not_called()

    def test_inactive_since_cleared_when_hp_active(self):
        """_hp_inactive_since wordt gewist zodra HP weer actief is."""
        sw = _make_switch()
        sw._hp_inactive_since = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        sw._current_level_idx = 3

        def mock_get_float(hass, entity):
            if entity == sw._flow_entity:
                return MIN_FLOW_LPH + 10.0
            return None

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=mock_get_float), \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt, \
             patch.object(sw, "_is_night", return_value=False):
            mock_dt.now.return_value = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
            _run(sw._async_do_compensation())

        assert sw._hp_inactive_since is None

    def test_inactive_timer_resets_after_hp_returns(self):
        """Na HP-stop → herstart → nieuwe stop: timer begint opnieuw."""
        sw = _make_switch()
        sw._current_level_idx = 0  # building87

        t_stop1 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        t_restart = datetime(2024, 1, 1, 10, 15, tzinfo=timezone.utc)
        t_stop2 = datetime(2024, 1, 1, 10, 20, tzinfo=timezone.utc)
        t_5min_after_stop2 = datetime(2024, 1, 1, 10, 25, tzinfo=timezone.utc)

        def flow_inactive(hass, entity):
            return 0.0

        def flow_active(hass, entity):
            if entity == sw._flow_entity:
                return MIN_FLOW_LPH + 10.0
            return None

        with patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt, \
             patch.object(sw, "_is_night", return_value=False):

            mock_dt.now.return_value = t_stop1
            with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_inactive):
                _run(sw._async_do_compensation())
            assert sw._hp_inactive_since == t_stop1

            mock_dt.now.return_value = t_restart
            with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_active):
                _run(sw._async_do_compensation())
            assert sw._hp_inactive_since is None

            mock_dt.now.return_value = t_stop2
            with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_inactive):
                _run(sw._async_do_compensation())
            assert sw._hp_inactive_since == t_stop2

            mock_dt.now.return_value = t_5min_after_stop2
            with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_inactive):
                _run(sw._async_do_compensation())
            sw.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# _async_reset_level — beide selects worden gereset
# ---------------------------------------------------------------------------

class TestResetLevel:

    def test_reset_sets_both_selects_to_max(self):
        """Reset schrijft max_day naar dag-select en max_night naar nacht-select."""
        sw = _make_switch(max_day="building87", max_night="silent")
        sw._current_level_idx = 0

        with patch.object(sw, "_is_night", return_value=False):
            _run(sw._async_reset_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_DAY_SOUND_ENTITY] == "building87"
        assert entity_to_option[_NIGHT_SOUND_ENTITY] == "silent"

    def test_reset_updates_current_level_idx(self):
        """Na reset staat _current_level_idx op effective_max."""
        sw = _make_switch(max_day="library", max_night="normal")
        sw._current_level_idx = 0

        with patch.object(sw, "_is_night", return_value=False):
            _run(sw._async_reset_level())
            assert sw._current_level_idx == _SOUND_LEVELS.index("library")

    def test_reset_uses_night_max_during_night(self):
        sw = _make_switch(max_day="building87", max_night="silent")
        sw._current_level_idx = 0

        with patch.object(sw, "_is_night", return_value=True):
            _run(sw._async_reset_level())
            assert sw._current_level_idx == _SOUND_LEVELS.index("silent")


# ---------------------------------------------------------------------------
# _async_apply_level — dag/nacht scheiding
# ---------------------------------------------------------------------------

class TestApplyLevel:

    def test_dag_stuurt_alleen_dag_select_actief(self):
        """Overdag: dag-select krijgt actueel niveau, nacht-select krijgt max_night."""
        sw = _make_switch(max_day="normal", max_night="silent")
        sw._current_level_idx = 1  # silent

        with patch.object(sw, "_is_night", return_value=False):
            _run(sw._async_apply_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_DAY_SOUND_ENTITY] == "silent"    # actief niveau
        assert entity_to_option[_NIGHT_SOUND_ENTITY] == "silent"  # max_night reset

    def test_nacht_stuurt_alleen_nacht_select_actief(self):
        """'s Nachts: nacht-select krijgt actueel niveau, dag-select krijgt max_day."""
        sw = _make_switch(max_day="library", max_night="normal")
        sw._current_level_idx = 0  # building87

        with patch.object(sw, "_is_night", return_value=True):
            _run(sw._async_apply_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_NIGHT_SOUND_ENTITY] == "building87"  # actief niveau
        assert entity_to_option[_DAY_SOUND_ENTITY] == "library"       # max_day reset

    def test_dag_clamp_op_max_day(self):
        """Niveau wordt geclampt op max_day als current_level_idx hoger is."""
        sw = _make_switch(max_day="silent", max_night="normal")
        sw._current_level_idx = 3  # normal, maar max_day = silent = 1

        with patch.object(sw, "_is_night", return_value=False):
            _run(sw._async_apply_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_DAY_SOUND_ENTITY] == "silent"  # geclampt op max_day

    def test_nacht_clamp_op_max_night(self):
        """Niveau wordt geclampt op max_night als current_level_idx hoger is."""
        sw = _make_switch(max_day="normal", max_night="building87")
        sw._current_level_idx = 3  # normal, maar max_night = building87 = 0

        with patch.object(sw, "_is_night", return_value=True):
            _run(sw._async_apply_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_NIGHT_SOUND_ENTITY] == "building87"  # geclampt op max_night

    def test_dag_select_schoon_bij_ochtend(self):
        """'s Nachts staat dag-select altijd op max_day, zodat ochtend schoon start."""
        sw = _make_switch(max_day="normal", max_night="normal")
        sw._current_level_idx = 0  # building87 's nachts

        with patch.object(sw, "_is_night", return_value=True):
            _run(sw._async_apply_level())

        calls = sw.hass.services.async_call.call_args_list
        entity_to_option = {c.args[2]["entity_id"]: c.args[2]["option"] for c in calls}
        assert entity_to_option[_DAY_SOUND_ENTITY] == "normal"  # schoon voor de ochtend


# ---------------------------------------------------------------------------
# Periode-overgang detectie — inactieve slider reset bij overgang
# ---------------------------------------------------------------------------

class TestPeriodTransition:

    def test_nacht_slider_reset_bij_ochtend_zonder_clamp(self):
        """Als niveau <= max_day (geen clamp), nacht-slider toch op max_night zetten bij 07:00."""
        sw = _make_switch(max_day="normal", max_night="normal")
        sw._current_level_idx = 0  # building87, was nacht

        def flow_active(hass, entity):
            if entity == sw._flow_entity:
                return MIN_FLOW_LPH + 10.0
            return None

        # Simuleer nacht → dag overgang
        sw._last_is_night = True  # was nacht

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_active), \
             patch.object(sw, "_is_night", return_value=False), \
             patch.object(sw, "_async_apply_level", new_callable=AsyncMock) as mock_apply, \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 7, 0, tzinfo=timezone.utc)
            _run(sw._async_do_compensation())

        mock_apply.assert_called_once()

    def test_geen_apply_bij_zelfde_periode(self):
        """Geen extra apply_level als periode niet veranderd is."""
        sw = _make_switch(max_day="normal", max_night="normal")
        sw._current_level_idx = 3  # normal, al op max

        def flow_active(hass, entity):
            if entity == sw._flow_entity:
                return MIN_FLOW_LPH + 10.0
            return None

        sw._last_is_night = False  # was al dag

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=flow_active), \
             patch.object(sw, "_is_night", return_value=False), \
             patch.object(sw, "_async_apply_level", new_callable=AsyncMock) as mock_apply, \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
            _run(sw._async_do_compensation())

        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# Geen warmtevraag ≠ uitval van de MPC-sensor
# ---------------------------------------------------------------------------

class TestGeenWarmtevraag:
    """Kamer op setpoint: de MPC geeft bewust geen advies.

    Tot en met v0.10.5 telde dat als uitval, en na tien minuten zette de
    watchdog het geluidsniveau op het maximum — juist op het moment dat er de
    minste warmte nodig was. Gemeten op een echt huis: 2,6% van de draaiuren,
    ~1x per dag, bij zacht weer.
    """

    T0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _tick(self, sw, reading, minutes, supply=35.0):
        def floats(hass, entity):
            if entity == sw._flow_entity:
                return MIN_FLOW_LPH + 500.0
            if entity == sw._supply_entity:
                return supply
            return None  # geen ketel

        with patch("custom_components.quatt_stooklijn.switch.get_float_state", side_effect=floats), \
             patch.object(sw, "_mpc_reading", return_value=reading), \
             patch.object(sw, "_is_night", return_value=False), \
             patch("custom_components.quatt_stooklijn.switch.datetime") as mock_dt:
            mock_dt.now.return_value = self.T0 + timedelta(minutes=minutes)
            _run(sw._async_do_compensation())

    def _start(self, level_idx):
        sw = _make_switch()
        sw._current_level_idx = level_idx
        sw._last_is_night = False
        # Laatste bruikbare advies vlak voor T0, zoals na een normale cyclus.
        self._tick(sw, (35.0, "advies"), 0)
        sw.hass.services.async_call.reset_mock()
        return sw

    def test_stapt_omlaag(self):
        sw = self._start(level_idx=2)  # library
        self._tick(sw, (None, "geen_vraag"), 5)
        assert sw._current_level_idx == 1
        sw.hass.services.async_call.assert_called()

    def test_geen_reset_naar_max_na_tien_minuten(self):
        """De kern van de bug: een uur lang geen vraag mag niet op 'normal' eindigen."""
        sw = self._start(level_idx=2)
        for minuten in range(5, 65, 5):
            self._tick(sw, (None, "geen_vraag"), minuten)
        assert sw._current_level_idx == 0
        # De nacht-select gaat bij elke apply bewust naar zijn max (inactieve
        # periode); de actieve dag-select mag dat nooit.
        dag = [
            c.args[2]["option"]
            for c in sw.hass.services.async_call.call_args_list
            if c.args[2]["entity_id"] == _DAY_SOUND_ENTITY
        ]
        assert dag == ["silent", "building87"]

    def test_op_laagste_niveau_geen_schrijfactie(self):
        """Elke vijf minuten dezelfde waarde naar de CiC sturen is zinloos verkeer."""
        sw = self._start(level_idx=0)
        self._tick(sw, (None, "geen_vraag"), 5)
        self._tick(sw, (None, "geen_vraag"), 20)
        assert sw._current_level_idx == 0
        sw.hass.services.async_call.assert_not_called()

    def test_ontbrekende_invoer_blijft_watchdog(self):
        """De watchdog zelf blijft: zonder metingen weet de compensatie niets."""
        sw = self._start(level_idx=0)
        self._tick(sw, (None, "geen_invoer"), 5)
        assert sw._current_level_idx == 0
        self._tick(sw, (None, "geen_invoer"), 15)
        assert sw._current_level_idx == _SOUND_LEVELS.index("normal")

    def test_onbekende_status_blijft_watchdog(self):
        """Sensor weg of unavailable: geen status, dus als uitval behandelen."""
        sw = self._start(level_idx=0)
        self._tick(sw, (None, None), 15)
        assert sw._current_level_idx == _SOUND_LEVELS.index("normal")

    def test_advies_terug_na_geen_vraag_werkt_normaal(self):
        sw = self._start(level_idx=2)
        self._tick(sw, (None, "geen_vraag"), 5)
        # Aanvoer 5 K onder advies → weer omhoog.
        self._tick(sw, (40.0, "advies"), 10, supply=35.0)
        assert sw._current_level_idx == 2


class TestMpcReading:
    """Waarde en reden komen uit dezelfde state."""

    def _sw_met_state(self, state):
        sw = _make_switch()
        sw.hass.states.get = lambda entity_id: state
        return sw

    def _lees(self, sw):
        with patch(
            "custom_components.quatt_stooklijn.switch.resolve_own_entity_id",
            return_value="sensor.mpc",
        ), patch(
            "custom_components.quatt_stooklijn.switch.get_float_state",
            side_effect=lambda hass, eid: (
                float(sw.hass.states.get(eid).state)
                if sw.hass.states.get(eid).state not in ("unknown", "unavailable")
                else None
            ),
        ):
            return sw._mpc_reading()

    def test_geen_vraag(self):
        state = MagicMock(state="unknown", attributes={"advies_status": "geen_vraag"})
        assert self._lees(self._sw_met_state(state)) == (None, "geen_vraag")

    def test_advies(self):
        state = MagicMock(state="34.0", attributes={"advies_status": "advies"})
        assert self._lees(self._sw_met_state(state)) == (34.0, "advies")

    def test_unavailable_negeert_oud_attribuut(self):
        """Een unavailable sensor zegt niets over de vraag, ook al hangt er nog een attribuut."""
        state = MagicMock(state="unavailable", attributes={"advies_status": "geen_vraag"})
        assert self._lees(self._sw_met_state(state)) == (None, None)

    def test_sensor_bestaat_niet(self):
        sw = _make_switch()
        with patch(
            "custom_components.quatt_stooklijn.switch.resolve_own_entity_id",
            return_value=None,
        ):
            assert sw._mpc_reading() == (None, None)


# ---------------------------------------------------------------------------
# Terugkoppeling: CiC-select wijkt af van wat de compensatie wil
# ---------------------------------------------------------------------------

class TestTerugkoppeling:
    """De compensatie leest beide CiC-selects terug en zet een afwijking recht.

    Aanleiding (Tweakers, 21-11-2026): de integratie stond op ``library``, de
    CiC een hele nacht op ``building87``. Zonder terugkoppeling schrijft de
    compensatie alleen bij een stap, en binnen de dode band gebeurt dat niet.
    """

    def _switch(self, cic_day, cic_night, level="library"):
        sw = _make_switch()
        sw._current_level_idx = _SOUND_LEVELS.index(level)
        stand = {_DAY_SOUND_ENTITY: cic_day, _NIGHT_SOUND_ENTITY: cic_night}

        def get(entity_id):
            st = MagicMock()
            st.state = stand.get(entity_id)
            return st

        sw.hass.states.get = MagicMock(side_effect=get)
        sw._stand = stand
        return sw

    def _verify(self, sw, night=False):
        with patch.object(sw, "_is_night", return_value=night):
            _run(sw._async_verify_selects())

    def _writes(self, sw):
        return {
            c.args[2]["entity_id"]: c.args[2]["option"]
            for c in sw.hass.services.async_call.call_args_list
        }

    def test_na_start_direct_gelijkgetrokken(self):
        """Herstart: niveau hersteld als library, CiC staat op building87."""
        sw = self._switch(cic_day="building87", cic_night="normal")
        self._verify(sw)
        assert self._writes(sw) == {_DAY_SOUND_ENTITY: "library"}
        assert sw._corrections == 1
        assert "building87 → library" in sw._last_correction

    def test_klopt_dan_geen_schrijfactie(self):
        sw = self._switch(cic_day="library", cic_night="normal")
        self._verify(sw)
        self._verify(sw)
        sw.hass.services.async_call.assert_not_called()

    def test_na_start_pas_na_twee_cycli(self):
        """Eén cyclus marge: een net geschreven waarde kan nog onderweg zijn."""
        sw = self._switch(cic_day="library", cic_night="normal")
        self._verify(sw)  # eerste cyclus: klopt
        sw._stand[_DAY_SOUND_ENTITY] = "building87"
        self._verify(sw)
        sw.hass.services.async_call.assert_not_called()
        self._verify(sw)
        assert self._writes(sw) == {_DAY_SOUND_ENTITY: "library"}

    def test_afwijking_die_vanzelf_verdwijnt_telt_niet_door(self):
        sw = self._switch(cic_day="library", cic_night="normal")
        self._verify(sw)
        sw._stand[_DAY_SOUND_ENTITY] = "building87"
        self._verify(sw)
        sw._stand[_DAY_SOUND_ENTITY] = "library"
        self._verify(sw)
        sw._stand[_DAY_SOUND_ENTITY] = "building87"
        self._verify(sw)
        sw.hass.services.async_call.assert_not_called()

    def test_nacht_select_gecorrigeerd_in_de_nacht(self):
        """'s Nachts: nacht-select krijgt het niveau, dag-select zijn maximum."""
        sw = self._switch(cic_day="normal", cic_night="building87")
        self._verify(sw, night=True)
        assert self._writes(sw) == {_NIGHT_SOUND_ENTITY: "library"}

    def test_inactieve_select_terug_naar_maximum(self):
        sw = self._switch(cic_day="library", cic_night="building87")
        self._verify(sw)
        assert self._writes(sw) == {_NIGHT_SOUND_ENTITY: "normal"}

    def test_onleesbare_select_niet_aangeraakt(self):
        sw = self._switch(cic_day="unavailable", cic_night=None)
        self._verify(sw)
        self._verify(sw)
        sw.hass.services.async_call.assert_not_called()
        assert sw._corrections == 0

    def test_eigen_schrijfactie_reset_de_teller(self):
        sw = self._switch(cic_day="library", cic_night="normal")
        self._verify(sw)
        sw._stand[_DAY_SOUND_ENTITY] = "building87"
        self._verify(sw)  # teller 1
        with patch.object(sw, "_is_night", return_value=False):
            _run(sw._async_apply_level())
        sw.hass.services.async_call.reset_mock()
        self._verify(sw)  # teller opnieuw 1: nog niet corrigeren
        sw.hass.services.async_call.assert_not_called()

    def test_attributen_tonen_cic_stand(self):
        sw = self._switch(cic_day="building87", cic_night="normal")
        with patch("custom_components.quatt_stooklijn.switch.get_float_state", return_value=None), \
             patch.object(sw, "_mpc_reading", return_value=(None, None)), \
             patch.object(sw, "_is_night", return_value=False):
            attrs = sw.extra_state_attributes
        assert attrs["cic_day"] == "building87"
        assert attrs["cic_night"] == "normal"
        assert attrs["corrections"] == 0
