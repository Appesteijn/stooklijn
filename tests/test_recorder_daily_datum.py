"""Dagstatistiek uit de recorder krijgt de lokale datum.

HA telt een dag van lokale middernacht tot lokale middernacht; in de winter
begint de dag van 20-11 dus om 19-11 23:00 UTC (testhuis, 24-11-2026, via de
websocket nagekeken). ``_async_fetch_recorder_daily`` zette die op de UTC-datum:
elke dag kwam één dag te vroeg binnen, en het lopende "vandaag" als gisteren.
Het venster begon op UTC-middernacht en miste het eerste uur van de eerste dag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis import quatt as quatt_mod

AMS = ZoneInfo("Europe/Amsterdam")
POWER, TEMP = "sensor.warmte", "sensor.buiten"


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class _Recorder:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


@pytest.fixture
def amsterdam(monkeypatch):
    monkeypatch.setattr(quatt_mod.dt_util, "get_default_time_zone", lambda: AMS)


async def _daily(rows_per_entity, start="2026-11-20", end="2026-11-21"):
    calls = {}

    def _stats(hass, start_time, end_time, statistic_ids, period, units, types):
        calls.update(start=start_time, end=end_time, period=period)
        return {e: r for e, r in rows_per_entity.items() if e in statistic_ids}

    with patch.object(quatt_mod, "get_instance", return_value=_Recorder()), \
         patch.object(quatt_mod, "statistics_during_period", side_effect=_stats):
        df = await quatt_mod._async_fetch_recorder_daily(
            MagicMock(), start, end, POWER, TEMP, "sensor.elek", "sensor.ketel"
        )
    return df, calls


async def test_dag_krijgt_de_lokale_datum(amsterdam):
    rows = {
        POWER: [{"start": _ts("2026-11-19T23:00:00+00:00"), "mean": 3000.0},
                {"start": _ts("2026-11-20T23:00:00+00:00"), "mean": 4000.0}],
        TEMP: [{"start": _ts("2026-11-19T23:00:00+00:00"), "mean": 4.0},
               {"start": _ts("2026-11-20T23:00:00+00:00"), "mean": 1.0}],
    }
    df, _ = await _daily(rows)
    assert list(df.index) == [pd.Timestamp("2026-11-20"), pd.Timestamp("2026-11-21")]
    assert df.loc["2026-11-20", "totalHpHeat"] == 3000.0 * 24
    assert df.loc["2026-11-21", "avg_temperatureOutside"] == 1.0


async def test_venster_van_lokale_middernacht_tot_lokale_middernacht(amsterdam):
    _, calls = await _daily({}, start="2026-11-20", end="2026-11-21")
    assert calls["period"] == "day"
    assert calls["start"] == datetime(2026, 11, 19, 23, tzinfo=timezone.utc)
    assert calls["end"] == datetime(2026, 11, 21, 23, tzinfo=timezone.utc)


async def test_zomertijd(amsterdam):
    """In de zomer begint de lokale dag om 22:00 UTC."""
    rows = {POWER: [{"start": _ts("2026-07-14T22:00:00+00:00"), "mean": 500.0}]}
    df, calls = await _daily(rows, start="2026-07-15", end="2026-07-15")
    assert list(df.index) == [pd.Timestamp("2026-07-15")]
    assert calls["start"] == datetime(2026, 7, 14, 22, tzinfo=timezone.utc)
