"""De kamertemperatuur op één plek (room_temperature.py, KAMERSENSOREN_PLAN.md stap 3)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.quatt_stooklijn import room_temperature as rt
from custom_components.quatt_stooklijn.const import CONF_INDOOR_TEMP_REFERENCES, DOMAIN
from custom_components.quatt_stooklijn.analysis.room_reference import CLIP_K

AMS = ZoneInfo("Europe/Amsterdam")
TH, MID, BEW = "sensor.isense", "sensor.midden", "sensor.beweging"


class _State:
    def __init__(self, v, name=None):
        self.state = str(v)
        self.attributes = {"friendly_name": name} if name else {}


def _hass(values: dict):
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    hass.states.get = lambda e: _State(values[e]) if e in values else None
    return hass


def _room(values, refs=None, primary=TH):
    hass = _hass(values)
    cfg = {CONF_INDOOR_TEMP_REFERENCES: refs} if refs is not None else {}
    with patch.object(rt, "Store", MagicMock()):
        room = rt.RoomTemperature(hass, "e1", cfg)
    room._store = MagicMock(async_save=AsyncMock(), async_load=AsyncMock(return_value=None))
    hass.data[DOMAIN][rt.room_key("e1")] = room
    p = patch.object(rt, "async_source_entity", return_value=primary)
    p.start()
    return hass, room, p


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


@pytest.fixture(autouse=True)
def issues(monkeypatch):
    ir = ModuleType("homeassistant.helpers.issue_registry")
    ir.async_create_issue = MagicMock()
    ir.async_delete_issue = MagicMock()
    ir.IssueSeverity = MagicMock(WARNING="warning")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", ir)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "issue_registry", ir, raising=False)
    return ir


def _created(ir):
    return {c.args[2]: c.kwargs for c in ir.async_create_issue.call_args_list}


class TestZonderExtraSensoren:
    def test_bitgelijk_aan_de_thermostaat(self):
        v = 19.617187500000004
        hass, room, _ = _room({TH: v})
        assert rt.room_value(hass, "e1", TH) == v
        assert room.value() == v and room.attributes() == {}

    def test_zonder_roomtemperature_de_entity_zelf(self):
        hass = _hass({TH: 20.1})
        assert rt.room_value(hass, "e1", TH) == 20.1
        assert rt.room_value(hass, "e1", None) is None

    def test_een_mock_hass_telt_niet(self):
        assert rt.async_room_temperature(MagicMock(), "e1") is None


class TestBegrenzen:
    def _geleerd(self, room, sun=None):
        room._learned_for = TH
        room.offsets = {MID: -1.45, BEW: -0.1}
        room.sun = sun or {}

    def test_zonpiek_afgekapt(self):
        hass, room, _ = _room({TH: 21.7, MID: 21.0, BEW: 19.9}, refs=[MID, BEW])
        self._geleerd(room)
        ref = (21.0 - 1.45 + 19.9 - 0.1) / 2
        assert room.value() == pytest.approx(ref + CLIP_K)
        assert room.raw() == 21.7
        attrs = room.attributes()
        assert attrs["begrensd"] is True and attrs["thermostaat"] == 21.7
        assert attrs["referenties"][MID]["verschil_k"] == -1.45

    def test_verschil_nog_niet_geleerd(self):
        hass, room, _ = _room({TH: 21.7, MID: 21.0}, refs=[MID])
        assert room.value() == 21.7

    def test_andere_thermostaat_dan_geleerd(self):
        hass, room, _ = _room({TH: 21.7, MID: 21.0}, refs=[MID])
        self._geleerd(room)
        room._learned_for = "sensor.quatt_thermostaat"
        assert room.value() == 21.7

    def test_referentie_in_de_zon_in_dit_uur_telt_niet(self):
        hass, room, _ = _room({TH: 21.7, MID: 21.0, BEW: 23.0}, refs=[MID, BEW])
        self._geleerd(room, sun={BEW: {11}})
        with patch.object(rt.dt_util, "now", return_value=datetime(2026, 11, 27, 11, 30, tzinfo=AMS)):
            assert room.value() == pytest.approx(21.0 - 1.45 + CLIP_K)
        with patch.object(rt.dt_util, "now", return_value=datetime(2026, 11, 27, 14, 30, tzinfo=AMS)):
            # Om 14 u telt de bewegingsmelder weer mee (hier warmer, dus geen extra afkappen).
            assert room.value() == pytest.approx(min(21.7, (21.0 - 1.45 + 23.0 - 0.1) / 2 + CLIP_K))

    def test_thermostaat_niet_als_eigen_referentie(self):
        assert rt.reference_entities({CONF_INDOOR_TEMP_REFERENCES: [TH, MID, "", MID]}, TH) == [MID]


class TestBepalen:
    def test_verschil_en_zonuren_uit_de_statistiek_en_bewaard(self):
        hass, room, _ = _room({TH: 20.0, MID: 21.45}, refs=[MID])
        # Twee weken uren; nachten gelijk, zon op de thermostaat om 14 u op zonnige dagen.
        start = datetime(2026, 11, 13, 0, 0, tzinfo=AMS).timestamp()
        th, mid, clear = {}, {}, {}
        for k in range(14 * 24):
            t = start + k * 3600
            hod = datetime.fromtimestamp(t, AMS).hour
            zonnig = (k // 24) % 2 == 0
            clear[t] = 0.9 if zonnig else 0.1
            th[t] = 20.0 + (1.2 if zonnig and hod == 14 else 0.0)
            mid[t] = 21.45
        now = start + 14 * 86400
        room.clearness_provider = AsyncMock(return_value=(clear, "zonnesensor"))
        with patch("custom_components.quatt_stooklijn.sensor.async_hourly_means",
                   AsyncMock(return_value={TH: th, MID: mid})), \
             patch.object(rt.time, "time", return_value=now), \
             patch.object(rt.dt_util, "get_default_time_zone", return_value=AMS):
            asyncio.run(room.async_refresh())
        assert room.offsets == {MID: pytest.approx(-1.45)}
        assert room.sun[TH] == {14} and room.sun[MID] == set()
        assert room.clearness_source == "zonnesensor" and room._learned_for == TH
        saved = room._store.async_save.call_args.args[0]
        assert saved["primary"] == TH and saved["sun"][TH] == [14]

    def test_bewaard_en_teruggelezen(self):
        hass, room, _ = _room({TH: 20.0}, refs=[MID])
        room._store.async_load = AsyncMock(return_value={
            "primary": TH, "offsets": {MID: -1.45}, "sun": {TH: [14]}, "updated_at": "x",
            "clearness_source": "open-meteo"})
        asyncio.run(room.async_load())
        assert room.offsets == {MID: -1.45} and room.sun == {TH: {14}} and room._learned_for == TH

    def test_zonder_extra_sensoren_leeggemaakt(self):
        hass, room, _ = _room({TH: 20.0}, refs=[])
        room.offsets, room.sun, room._learned_for = {MID: -1.0}, {MID: {11}}, TH
        asyncio.run(room.async_refresh())
        assert room.offsets == {} and room.sun == {} and room._learned_for is None


class TestVolgen:
    def test_listeners_volgen_de_extra_sensoren(self):
        from custom_components.quatt_stooklijn.discovery import ROLE_INDOOR_TEMP
        from custom_components.quatt_stooklijn.sensor import candidate_entities

        hass, room, _ = _room({TH: 20.0, MID: 21.0}, refs=[MID, BEW])
        assert {MID, BEW} <= set(candidate_entities(hass, "e1", (ROLE_INDOOR_TEMP,)))
        assert not {MID, BEW} & set(candidate_entities(hass, "e1", ("outdoor_temp",)))


def test_niemand_leest_de_thermostaat_nog_zelf():
    """Alles leest de kamertemperatuur via room_value; anders ziet die plek de
    zonpieken weer (KAMERSENSOREN_PLAN.md, ontwerp 5)."""
    root = Path(__file__).parents[1] / "custom_components" / "quatt_stooklijn"
    for name in ("sensor.py", "steering_effect.py", "advice_sources.py", "ch_max_water.py",
                 "heat_demand.py", "power_house.py", "binary_sensor.py"):
        src = (root / name).read_text()
        assert not re.search(r"get_float_state\([^)]*_indoor_temp_entity", src), name


class TestMeldingen:
    def test_bevestigd_met_referentie(self, issues):
        hass, room, _ = _room({TH: 21.7, MID: 21.0}, refs=[MID])
        room.sun_k = {TH: {13: 0.5, 14: 1.12, 15: 0.55}, MID: {}}
        room.clipped = {"uren": 12.5, "gem_k": 0.5}
        room._async_update_issues()
        made = _created(issues)
        assert set(made) == {"room_sensor_sun_e1"}
        ph = made["room_sensor_sun_e1"]["translation_placeholders"]
        assert (ph["van"], ph["tot"], ph["kelvin"], ph["uren"]) == ("13:00", "16:00", "1,1", "12,5")
        deleted = {c.args[2] for c in issues.async_delete_issue.call_args_list}
        assert deleted == {"room_sun_peaks_e1", "room_reference_sun_e1"}

    def test_met_referentie_zonder_zon_op_de_thermostaat_geen_melding(self, issues):
        # De kamer warmt echt op: pieken zonder referentie, maar de referentie loopt mee.
        hass, room, _ = _room({TH: 21.7, MID: 21.0}, refs=[MID])
        room.sun_k = {TH: {}, MID: {}}
        room.peak_state = {"aan": True, "stil": 0, "cluster": {"dagen": 4, "van": 13.4, "tot": 15.2, "kelvin": 1.7}}
        room._async_update_issues()
        assert _created(issues) == {}

    def test_neutraal_zonder_referenties(self, issues):
        hass, room, _ = _room({TH: 21.7})
        room.peak_state = {"aan": True, "stil": 0, "cluster": {"dagen": 6, "van": 13.43, "tot": 15.22, "kelvin": 1.68}}
        room._async_update_issues()
        ph = _created(issues)["room_sun_peaks_e1"]["translation_placeholders"]
        assert (ph["dagen"], ph["van"], ph["tot"], ph["kelvin"]) == ("6", "13:15", "15:15", "1,7")

    def test_referentie_in_de_zon(self, issues):
        hass, room, _ = _room({TH: 20.0, MID: 21.0, "sensor.bench": 21.0}, refs=[MID, "sensor.bench"])
        hass.states.get = lambda e: _State(20.0, {"sensor.bench": "Bench", MID: "Woonkamer midden"}.get(e))
        room.sun_k = {TH: {}, MID: {}, "sensor.bench": {11: 0.81, 12: 0.88}}
        room._async_update_issues()
        ph = _created(issues)["room_reference_sun_e1"]["translation_placeholders"]
        assert ph["sensoren"] == "Bench (11:00–13:00, +0,9 °C)" and ph["gebruikt"] == "Woonkamer midden"

    def test_niets_aan_de_hand_alles_weg(self, issues):
        hass, room, _ = _room({TH: 20.0})
        room._async_update_issues()
        assert _created(issues) == {} and issues.async_delete_issue.call_count == 3


class TestPiekenInDeRuntime:
    def _rows(self, days=20, spike=1.5):
        """Vijfminutenrijen; zon op de thermostaat om 14:30 op even dagen."""
        from custom_components.quatt_stooklijn.highres_log import COLUMNS
        start = datetime(2026, 11, 7, 0, 0, tzinfo=AMS).timestamp()
        rows, clear = [], {}
        i_raw = COLUMNS.index("t_indoor_raw")
        for k in range(days * 288):
            t = start + k * 300
            local = datetime.fromtimestamp(t, AMS)
            day = (local.date() - datetime(2026, 11, 7).date()).days
            sunny = day % 2 == 0
            h = local.hour + local.minute / 60
            extra = spike * max(0.0, 1 - abs(h - 14.5) / 0.5) if sunny else 0.0
            row = [t] + [None] * (len(COLUMNS) - 1)
            row[1:5] = [20.0 + extra, 5.0, 3000.0, 0.0]
            rows.append(row)
            clear[float(int(t // 3600) * 3600)] = 0.9 if sunny else 0.1
        return rows, clear, start + days * 86400

    def test_melding_zonder_referenties(self, issues):
        hass, room, _ = _room({TH: 20.0})
        hass.config = SimpleNamespace(latitude=52.37, longitude=4.90)
        rows, clear, end = self._rows()
        room.rows_provider = lambda since: [r for r in rows if r[0] >= since]
        room.clearness_provider = AsyncMock(return_value=(clear, "zonnesensor"))
        with patch.object(rt.time, "time", return_value=end), \
             patch.object(rt.dt_util, "now", return_value=datetime.fromtimestamp(end, AMS)), \
             patch.object(rt.dt_util, "get_default_time_zone", return_value=AMS):
            asyncio.run(room.async_refresh())
        assert room.peak_state["aan"] is True
        ph = _created(issues)["room_sun_peaks_e1"]["translation_placeholders"]
        assert ph["van"] <= "13:45" and ph["tot"] >= "15:00"
        assert room.attributes()["zonpieken"]["dagen"] >= 3

    def test_bewolkt_weer_geen_melding(self, issues):
        hass, room, _ = _room({TH: 20.0})
        hass.config = SimpleNamespace(latitude=52.37, longitude=4.90)
        rows, clear, end = self._rows(spike=0.0)
        room.rows_provider = lambda since: [r for r in rows if r[0] >= since]
        room.clearness_provider = AsyncMock(return_value=(clear, "zonnesensor"))
        with patch.object(rt.time, "time", return_value=end), \
             patch.object(rt.dt_util, "now", return_value=datetime.fromtimestamp(end, AMS)), \
             patch.object(rt.dt_util, "get_default_time_zone", return_value=AMS):
            asyncio.run(room.async_refresh())
        assert not (room.peak_state or {}).get("aan")
        assert _created(issues) == {}

    def test_thermostaat_ruw_gaat_voor_en_afgekapt_geteld(self):
        from custom_components.quatt_stooklijn.highres_log import COLUMNS
        hass, room, _ = _room({TH: 20.0, MID: 21.0}, refs=[MID])
        i_raw = COLUMNS.index("t_indoor_raw")
        t0 = datetime(2026, 11, 27, 12, 0, tzinfo=AMS).timestamp()
        rows = []
        for k in range(24):                       # twee uur
            row = [t0 + k * 300] + [None] * (len(COLUMNS) - 1)
            row[1] = 20.0
            row[i_raw] = 20.6 if k < 12 else 20.0  # eerste uur 0,6 K afgekapt
            rows.append(row)
        room.rows_provider = lambda since: rows
        ts, raw, used = room._thermostat_samples(0)
        assert raw[0] == 20.6 and used[0] == 20.0
        room._update_clipped(t0 + 7200)
        assert room.clipped == {"uren": 1.0, "gem_k": 0.6}


class TestLogboekAchteraf:
    """``_async_backfill``: het oude meetlogboek één keer begrenzen per set referenties."""

    def _data(self):
        start = datetime(2026, 11, 6, 0, 0, tzinfo=AMS).timestamp()
        th, mid, clear = {}, {}, {}
        for k in range(21 * 24):
            t = start + k * 3600
            zonnig = (k // 24) % 2 == 0
            clear[t] = 0.9 if zonnig else 0.1
            th[t] = 20.0 + (1.2 if zonnig and datetime.fromtimestamp(t, AMS).hour == 14 else 0.0)
            mid[t] = 21.45
        return start, start + 21 * 86400, th, mid, clear

    def _run(self, room, th, mid, clear, now, refs=(MID,)):
        from custom_components.quatt_stooklijn.highres_log import COLUMNS
        rows = [[t] + [20.0] + [None] * (len(COLUMNS) - 2) for t in sorted(th)[7 * 24:]]
        room.rows_provider = lambda since: [r for r in rows if since is None or r[0] >= since]
        room.clearness_provider = AsyncMock(return_value=(clear, "zonnesensor"))
        room._hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
        means = {TH: th, MID: mid, BEW: mid}
        with patch("custom_components.quatt_stooklijn.sensor.async_hourly_means",
                   AsyncMock(side_effect=lambda h, ents, s, e: {x: means[x] for x in ents})), \
             patch.object(rt.time, "time", return_value=now), \
             patch.object(rt.dt_util, "get_default_time_zone", return_value=AMS):
            asyncio.run(room.async_refresh())

    def test_een_keer_en_bewaard(self):
        start, now, th, mid, clear = self._data()
        hass, room, _ = _room({TH: 20.0, MID: 21.45}, refs=[MID])
        room.log_rewriter = MagicMock(return_value=40)
        self._run(room, th, mid, clear, now)
        corr = room.log_rewriter.call_args.args[0]
        assert corr and all(datetime.fromtimestamp(t, AMS).hour == 14 for t in corr)
        assert all(v == pytest.approx(-(1.2 - CLIP_K)) for v in corr.values())
        assert room.backfill["refs"] == [MID] and room.backfill["monsters"] == 40
        assert room._store.async_save.call_args.args[0]["backfill"] == room.backfill
        assert room.attributes()["logboek_gecorrigeerd"]["monsters"] == 40
        self._run(room, th, mid, clear, now)
        assert room.log_rewriter.call_count == 1

    def test_andere_referenties_eerst_terug(self):
        start, now, th, mid, clear = self._data()
        hass, room, _ = _room({TH: 20.0, MID: 21.45, BEW: 21.45}, refs=[MID, BEW])
        room.backfill = {"refs": [MID], "uren": 3, "monsters": 36, "op": "x"}
        room.log_rewriter = MagicMock(return_value=12)
        self._run(room, th, mid, clear, now)
        calls = [c.args[0] for c in room.log_rewriter.call_args_list]
        assert calls[0] is None and isinstance(calls[1], dict)
        assert room.backfill["refs"] == sorted([MID, BEW])

    def test_referenties_weg_dan_terug(self):
        hass, room, _ = _room({TH: 20.0}, refs=[])
        room.backfill = {"refs": [MID], "uren": 3, "monsters": 36, "op": "x"}
        room.log_rewriter = MagicMock(return_value=36)
        asyncio.run(room.async_refresh())
        room.log_rewriter.assert_called_once_with(None)
        assert room.backfill is None

    def test_logboek_nog_niet_gelezen(self):
        start, now, th, mid, clear = self._data()
        hass, room, _ = _room({TH: 20.0, MID: 21.45}, refs=[MID])
        room.log_rewriter = MagicMock(return_value=None)
        self._run(room, th, mid, clear, now)
        assert room.backfill is None
