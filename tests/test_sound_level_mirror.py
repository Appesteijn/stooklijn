"""Tests voor de koppeling tussen de geluidsniveau-switch en de spiegelsensor.

De sensor hing vroeger aan een hardcoded entity-ID van de switch. Die brak
zodra de gebruiker de switch hernoemde of een tweede config-entry aanmaakte.
Nu publiceert de switch zijn niveau via een dispatcher-signaal plus hass.data,
en leest de sensor daaruit.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from custom_components.quatt_stooklijn.const import DOMAIN, SIGNAL_SOUND_LEVEL
from custom_components.quatt_stooklijn.sensor import QuattSoundLevelSensor
from custom_components.quatt_stooklijn.switch import (
    QuattSoundLevelSwitch,
    _SOUND_LEVELS,
)


def _run(coro):
    return asyncio.run(coro)


def _entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {}
    entry.options = {}
    return entry


def _make_switch(entry_id="test_entry") -> QuattSoundLevelSwitch:
    hass = MagicMock()
    hass.data = {}
    sw = QuattSoundLevelSwitch(hass, MagicMock(), _entry(entry_id))
    sw.hass = hass
    return sw


class TestSwitchPublishesLevel:

    def test_level_in_hass_data(self):
        """Het actieve niveau belandt in hass.data, per entry gescheiden."""
        sw = _make_switch()
        sw._current_level_idx = _SOUND_LEVELS.index("silent")

        with patch("custom_components.quatt_stooklijn.switch.async_dispatcher_send"):
            sw.async_write_ha_state()

        assert sw.hass.data[DOMAIN]["test_entry_sound_level"] == "silent"

    def test_signal_carries_level(self):
        """Het dispatcher-signaal is per entry en draagt het niveau mee."""
        sw = _make_switch(entry_id="andere_entry")
        sw._current_level_idx = _SOUND_LEVELS.index("library")

        with patch(
            "custom_components.quatt_stooklijn.switch.async_dispatcher_send"
        ) as send:
            sw.async_write_ha_state()

        send.assert_called_once_with(
            sw.hass, SIGNAL_SOUND_LEVEL.format("andere_entry"), "library"
        )


class TestSensorReadsLevel:

    def _make_sensor(self, hass_data: dict) -> QuattSoundLevelSensor:
        sensor = QuattSoundLevelSensor(_entry())
        sensor.hass = MagicMock()
        sensor.hass.data = hass_data
        sensor.async_on_remove = MagicMock()
        sensor.async_write_ha_state = MagicMock()
        return sensor

    def test_picks_up_level_set_before_sensor_existed(self):
        """Een switch die eerder werd opgezet dan de sensor gaat niet verloren."""
        sensor = self._make_sensor({DOMAIN: {"test_entry_sound_level": "normal"}})

        _run(sensor.async_added_to_hass())

        assert sensor.state == "normal"

    def test_no_level_yet_is_none(self):
        """Zonder gepubliceerd niveau blijft de sensor leeg in plaats van te falen."""
        sensor = self._make_sensor({})

        _run(sensor.async_added_to_hass())

        assert sensor.state is None

    def test_signal_updates_state(self):
        """Een binnenkomend signaal werkt de state bij."""
        sensor = self._make_sensor({})
        _run(sensor.async_added_to_hass())

        sensor._handle_level("building87")

        assert sensor.state == "building87"
        sensor.async_write_ha_state.assert_called_once()

    def test_listens_on_own_entry_signal(self):
        """De sensor abonneert op het signaal van zijn eigen entry."""
        sensor = self._make_sensor({})

        with patch(
            "custom_components.quatt_stooklijn.sensor.async_dispatcher_connect"
        ) as connect:
            _run(sensor.async_added_to_hass())

        assert connect.call_args[0][1] == SIGNAL_SOUND_LEVEL.format("test_entry")
