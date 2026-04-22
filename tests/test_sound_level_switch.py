"""Tests for QuattSoundLevelSwitch compensation logic."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from custom_components.quatt_stooklijn.switch import (
    QuattSoundLevelSwitch,
    _SOUND_LEVELS,
    _DAY_SOUND_ENTITY,
    _NIGHT_SOUND_ENTITY,
)
from custom_components.quatt_stooklijn.const import MIN_FLOW_LPH


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
    sw = QuattSoundLevelSwitch(coordinator, entry)
    sw.hass = MagicMock()
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
