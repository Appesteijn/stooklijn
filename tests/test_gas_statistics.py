"""Gasvergelijking uit de langetermijnstatistiek (GitHub #3).

De gasperiode ligt vóór de warmtepomp, en de ruwe recorderhistorie is dan al
lang gepurged (standaard 10 dagen, bij de melder 7). De gasanalyse vond daardoor
bij wie de integratie installeert nádat de warmtepomp er is nooit data. Nu eerst
de uurstatistiek (``change`` voor de meter, ``mean`` voor de buitentemperatuur),
en de ruwe historie alleen nog als terugval voor een sensor zonder statistiek.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis import gas
from custom_components.quatt_stooklijn.analysis.gas import (
    async_fetch_gas_data,
    gas_frames,
    stats_rows_to_series,
)

GAS = "sensor.gas_meter_gas"
TEMP = "sensor.buiten"
T0 = datetime(2024, 11, 8, tzinfo=timezone.utc).timestamp()


def _uren(n, start=T0):
    return [start + 3600 * k for k in range(n)]


class TestStatsRows:

    def test_epoch_en_lege_waarden(self):
        rows = [{"start": T0, "change": 0.3}, {"start": T0 + 3600, "change": None},
                {"start": T0 + 7200, "change": 0.5}]
        s = stats_rows_to_series(rows, "change")
        assert list(s) == [0.3, 0.5]
        assert s.index[0] == pd.Timestamp("2024-11-08 00:00")

    def test_datetime_wordt_utc(self):
        start = datetime(2024, 11, 8, 1, 0, tzinfo=timezone.utc)
        s = stats_rows_to_series([{"start": start, "mean": 4.2}], "mean")
        assert s.index[0] == pd.Timestamp("2024-11-08 01:00")

    def test_niets_bruikbaars(self):
        assert stats_rows_to_series([], "change") is None
        assert stats_rows_to_series(None, "change") is None
        assert stats_rows_to_series([{"start": T0, "mean": 1.0}], "change") is None


class TestGasFrames:

    def test_dagsom_en_warmte(self):
        idx = pd.date_range("2024-11-08", periods=48, freq="h")
        hourly, daily = gas_frames(pd.Series(0.25, index=idx), 9.77, 0.90)
        assert list(daily["gas_m3"]) == [6.0, 6.0]
        assert daily["heat_kwh"].iloc[0] == pytest.approx(6.0 * 9.77 * 0.90)
        assert hourly["heat_w"].iloc[0] == pytest.approx(0.25 * 9.77 * 0.90 * 1000)

    def test_sprongen_en_negatief_tellen_niet(self):
        idx = pd.date_range("2024-11-08", periods=4, freq="h")
        _, daily = gas_frames(pd.Series([0.5, 250.0, -3.0, 0.5], index=idx), 9.77, 0.9)
        assert daily["gas_m3"].iloc[0] == pytest.approx(1.0)


def _hass():
    return SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Amsterdam"))


class _Recorder:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _stats_jaar():
    """Een jaar: winter 2 m³/dag bij 2 °C, zomer 0,3 m³/dag (warm water) bij 20 °C."""
    gas_rows, temp_rows = [], []
    for k, ts in enumerate(_uren(365 * 24)):
        dag = k // 24
        zomer = 180 <= dag < 280
        gas_rows.append({"start": ts, "change": (0.3 if zomer else 2.0) / 24})
        temp_rows.append({"start": ts, "mean": 20.0 if zomer else 2.0})
    return {GAS: gas_rows, TEMP: temp_rows}


async def _fetch(stats, history, temp_entities=(TEMP,)):
    def _stats(hass, start_time, end_time, statistic_ids, period, units, types):
        (eid,) = statistic_ids
        assert period == "hour"
        return {eid: stats[eid]} if eid in stats else {}

    def _hist(hass, start, end, eid):
        return {eid: history.get(eid, [])}

    with patch.object(gas, "get_instance", return_value=_Recorder()), \
         patch.object(gas, "statistics_during_period", side_effect=_stats), \
         patch.object(gas, "state_changes_during_period", side_effect=_hist):
        return await async_fetch_gas_data(
            _hass(), GAS, "2024-11-08", "2025-11-07", temp_entities=list(temp_entities)
        )


class TestOphalen:

    async def test_alleen_statistiek_zoals_na_de_purge(self):
        """Het geval van #3: ruwe historie leeg, statistiek volledig."""
        hourly, daily = await _fetch(_stats_jaar(), history={})
        assert len(daily) == 365
        assert daily["gas_m3"].sum() == pytest.approx(265 * 2.0 + 100 * 0.3)
        assert daily["avg_temperatureOutside"].iloc[0] == pytest.approx(2.0)
        # Warm water via de zomermediaan eraf: winterdag 1,7 m³ voor verwarming.
        assert daily["gas_m3_heating"].iloc[0] == pytest.approx(1.7)
        assert daily["totalHeatPerHour"].iloc[0] == pytest.approx(1.7 * 9.77 * 0.9 * 1000 / 24)
        assert "temperatureOutside" in hourly.columns

    async def test_terugval_op_ruwe_historie(self):
        """Een gasmeter zonder state_class heeft geen statistiek."""
        def st(ts, v):
            return SimpleNamespace(state=str(v), last_changed=datetime.fromtimestamp(ts, timezone.utc))
        meter = [st(ts, 100 + 0.1 * k) for k, ts in enumerate(_uren(48))]
        temp = [st(ts, 3.0) for ts in _uren(48)]
        _, daily = await _fetch({}, history={GAS: meter, TEMP: temp})
        assert daily["gas_m3"].sum() == pytest.approx(4.7)
        assert daily["avg_temperatureOutside"].iloc[0] == pytest.approx(3.0)

    async def test_niets_gevonden(self, caplog):
        hourly, daily = await _fetch({}, history={})
        assert hourly.empty and daily.empty
        assert "no long-term statistics" in caplog.text

    async def test_temperatuur_uit_de_tweede_sensor(self):
        stats = _stats_jaar()
        stats["sensor.tweede"] = stats.pop(TEMP)
        _, daily = await _fetch(stats, history={}, temp_entities=(TEMP, "sensor.tweede"))
        assert daily["avg_temperatureOutside"].iloc[0] == pytest.approx(2.0)
