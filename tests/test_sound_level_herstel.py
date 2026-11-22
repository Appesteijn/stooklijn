"""Soundslider: eigen stand terug, watchdog na herstart, geen herhaalde
schrijfacties, oude invoer en twee CiC's (analyse 21-11-2026)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.quatt_stooklijn.advice_sources import advice_entity_id
from custom_components.quatt_stooklijn.const import (
    CONF_SOUND_LEVEL_MAX_DAY,
    CONF_SOUND_LEVEL_MAX_NIGHT,
    SOUND_ADVICE_TIMEOUT,
)
from custom_components.quatt_stooklijn.discovery import (
    ROLE_FLOW_RATE,
    ROLE_SOUND_DAY,
    ROLE_SOUND_NIGHT,
    ROLE_SUPPLY_TEMP,
)
from custom_components.quatt_stooklijn.switch import QuattSoundLevelSwitch

DAY = "select.cic_day_max_sound_level"
NIGHT = "select.cic_night_max_sound_level"
FLOW = f"sensor.{ROLE_FLOW_RATE}"
SUPPLY = f"sensor.{ROLE_SUPPLY_TEMP}"


@pytest.fixture(autouse=True)
def _pin_sources():
    with patch(
        "custom_components.quatt_stooklijn.switch.async_source_entity",
        side_effect=lambda hass, entry_id, role, **kw: {
            ROLE_SOUND_DAY: DAY, ROLE_SOUND_NIGHT: NIGHT,
        }.get(role, f"sensor.{role}"),
    ):
        yield


class FakeStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        import copy
        self.data = copy.deepcopy(data)


class _Huis:
    """hass met echte states; ``select_option`` verzet de select echt."""

    def __init__(self, day="normal", night="library", **extra):
        self.states = {
            DAY: SimpleNamespace(state=day, attributes={}),
            NIGHT: SimpleNamespace(state=night, attributes={}),
            FLOW: SimpleNamespace(state="900", attributes={}),
            SUPPLY: SimpleNamespace(state="35", attributes={}),
        }
        for k, v in extra.items():
            self.states[k] = SimpleNamespace(state=v, attributes={})
        self.hass = MagicMock()
        self.hass.states.get = lambda e: self.states.get(e)
        self.hass.data = {}
        self.calls: list[tuple[str, str]] = []

        async def _call(domain, service, data, blocking=False):
            self.calls.append((data["entity_id"], data["option"]))
            self.states[data["entity_id"]] = SimpleNamespace(state=data["option"], attributes={})

        self.hass.services.async_call = AsyncMock(side_effect=_call)

    def advies(self, waarde):
        eid = advice_entity_id(self.hass, "e1", "mpc")
        if waarde is None:
            self.states.pop(eid, None)
        else:
            self.states[eid] = SimpleNamespace(state=str(waarde), attributes={})


def _switch(huis, store=None, max_day="normal", max_night="normal"):
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"power_entity": "sensor.cic_total_power"}
    entry.options = {CONF_SOUND_LEVEL_MAX_DAY: max_day, CONF_SOUND_LEVEL_MAX_NIGHT: max_night}
    sw = QuattSoundLevelSwitch(huis.hass, MagicMock(), entry)
    sw.hass = huis.hass
    sw.async_write_ha_state = MagicMock()
    sw.async_on_remove = MagicMock()
    sw._store = store if store is not None else FakeStore()
    return sw


def _start(sw, last_state=None):
    sw.async_get_last_state = AsyncMock(return_value=last_state)
    asyncio.run(sw.async_added_to_hass())


def _aan(level="silent"):
    return SimpleNamespace(state="on", attributes={"current_level": level})


def _dag(sw):
    return patch.object(sw, "_is_night", return_value=False)


class TestEigenStandTerug:
    def test_bij_inschakelen_bewaard_en_bij_uitzetten_terug(self):
        huis = _Huis(day="normal", night="library")
        huis.advies(25.0)                       # aanvoer 35 → te hoog → omlaag
        sw = _switch(huis)
        _start(sw)                              # optie net aangezet: geen vorige state
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
            assert huis.states[DAY].state == "library"
            asyncio.run(sw.async_turn_off())
        assert huis.states[DAY].state == "normal"
        assert huis.states[NIGHT].state == "library"

    def test_nachtlimiet_uit_de_app_blijft_gelden(self):
        """De inactieve select ging naar het ingestelde maximum (normal), en
        de nachtlimiet uit de Quatt-app was weg."""
        huis = _Huis(day="normal", night="library")
        huis.advies(35.0)                       # binnen de dode band
        sw = _switch(huis)
        _start(sw)
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
        assert huis.states[NIGHT].state == "library"
        assert sw._max_night_idx == 2

    def test_upgrade_terwijl_aan_zoals_voorheen(self):
        """Stond hij aan zonder nette stop, dan staan de selects op wat wíj
        schreven. Dat is niet de eigen stand: terug naar het maximum."""
        huis = _Huis(day="silent", night="silent")
        huis.advies(35.0)
        sw = _switch(huis)
        _start(sw, _aan("silent"))
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
            asyncio.run(sw.async_turn_off())
        assert huis.states[DAY].state == "normal"
        assert sw._max_day_idx == 3

    def test_na_een_nette_stop_wel_bewaard(self):
        store = FakeStore({"at_original": True})
        huis = _Huis(day="library", night="silent")
        huis.advies(35.0)
        sw = _switch(huis, store=store)
        _start(sw, _aan("library"))
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
        assert store.data["night"]["original"] == "silent"

    def test_ha_stop_zet_terug(self):
        huis = _Huis(day="normal", night="normal")
        huis.advies(25.0)
        sw = _switch(huis)
        _start(sw)
        event, handler = huis.hass.bus.async_listen_once.call_args.args
        assert event == "homeassistant_stop"
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
            assert huis.states[DAY].state == "library"
            asyncio.run(handler(None))
            huis.calls.clear()
            asyncio.run(sw._async_compensation_cycle())   # na de stop: niets
        assert huis.states[DAY].state == "normal"
        assert huis.calls == []
        assert sw._store.data["at_original"] is True


class TestHerladen:
    def test_herladen_met_switch_uit_laat_alles_staan(self):
        """Elke opgeslagen optie herlaadt de entry. Met de switch uit zette
        dat de niveaus toch terug, over wat de gebruiker zelf had ingesteld."""
        huis = _Huis(day="building87", night="silent")
        sw = _switch(huis)
        sw._is_on = False
        asyncio.run(sw.async_will_remove_from_hass())
        assert huis.calls == []

    def test_herladen_met_switch_aan_zet_terug(self):
        huis = _Huis(day="normal", night="normal")
        huis.advies(25.0)
        sw = _switch(huis)
        _start(sw)
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
            asyncio.run(sw.async_will_remove_from_hass())
        assert huis.states[DAY].state == "normal"


class TestWatchdogNaHerstart:
    def test_loopt_ook_zonder_ooit_een_advies(self):
        """``_last_mpc_available`` begon op None: na een herstart zonder
        advies liep de watchdog nooit, tot een etmaal lang."""
        huis = _Huis(day="building87", night="normal")
        huis.advies(None)
        sw = _switch(huis)
        _start(sw, _aan("building87"))
        sw._last_mpc_available -= timedelta(seconds=SOUND_ADVICE_TIMEOUT + 1)
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
        assert huis.states[DAY].state == "normal"


class TestGeenHerhaaldeSchrijfacties:
    def test_na_time_out_niet_elke_cyclus_opnieuw(self):
        """572 aanroepen per etmaal: de reset schreef ook als de selects al
        op het maximum stonden."""
        huis = _Huis(day="normal", night="normal")
        huis.advies(None)
        sw = _switch(huis)
        _start(sw, _aan("normal"))
        sw._last_mpc_available -= timedelta(seconds=SOUND_ADVICE_TIMEOUT + 1)
        with _dag(sw):
            for _ in range(5):
                asyncio.run(sw._async_compensation_cycle())
        assert huis.calls == []


class TestOudeInvoer:
    def test_bevroren_debiet_telt_als_onbekend(self):
        huis = _Huis(day="normal", night="normal")
        huis.advies(25.0)
        oud = datetime.now(timezone.utc) - timedelta(hours=2)
        huis.states[FLOW] = SimpleNamespace(state="900", attributes={}, last_reported=oud)
        sw = _switch(huis)
        assert sw._fresh_float(FLOW) is None
        huis.states[FLOW].last_reported = datetime.now(timezone.utc)
        assert sw._fresh_float(FLOW) == 900.0

    def test_bevroren_advies_telt_als_uitval(self):
        huis = _Huis()
        huis.advies(30.0)
        eid = advice_entity_id(huis.hass, "e1", "mpc")
        huis.states[eid].last_reported = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _switch(huis)._advice_reading() == (None, None)

    def test_bevroren_aanvoer_stapt_niet(self):
        huis = _Huis(day="normal", night="normal")
        huis.advies(25.0)
        huis.states[SUPPLY].last_reported = datetime.now(timezone.utc) - timedelta(hours=1)
        sw = _switch(huis)
        _start(sw, _aan("normal"))
        with _dag(sw):
            asyncio.run(sw._async_compensation_cycle())
        assert huis.calls == []


class TestTweeCics:
    def test_selects_worden_met_de_entry_config_opgezocht(self):
        """Zonder config won bij twee CiC's de eerste uit het register."""
        huis = _Huis()
        sw = _switch(huis)
        with patch("custom_components.quatt_stooklijn.switch.async_source_entity",
                   return_value=DAY) as zoek:
            sw._day_sound_entity
            sw._night_sound_entity
        for c in zoek.call_args_list:
            assert c.kwargs["config"]["power_entity"] == "sensor.cic_total_power"
