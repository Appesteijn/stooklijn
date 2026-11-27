"""OpenQuatt gaat voor op Quatt (cloud-cache en CiC-sensoren) voor warmte en stroom per dag.

Testhuis 27-11-2026: tegen de slimme meter had de stroom van OpenQuatt een
helling van 0,997 (Quatt 0,837); de warmte was gelijk. Zie
``analysis.quatt.apply_openquatt_days``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis import quatt as quatt_mod
from custom_components.quatt_stooklijn.analysis.quatt import (
    OPENQUATT_MIN_HOURS_PER_DAY,
    apply_openquatt_days,
    openquatt_daily_from_hourly,
)

AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(autouse=True)
def amsterdam(monkeypatch):
    monkeypatch.setattr(quatt_mod.dt_util, "get_default_time_zone", lambda: AMS)
    monkeypatch.setattr(quatt_mod.dt_util, "DEFAULT_TIME_ZONE", AMS, raising=False)


def _uren(dag: str, n: int, w: float) -> list[dict]:
    """``n`` uurgemiddelden van ``w`` W vanaf lokale middernacht van ``dag``."""
    begin = datetime.fromisoformat(dag).replace(tzinfo=AMS).astimezone(timezone.utc)
    return [{"start": (begin + timedelta(hours=i)).timestamp(), "mean": w} for i in range(n)]


class TestDagtotalen:
    def test_lokale_dag_en_vierentwintig_uur(self):
        df = openquatt_daily_from_hourly(_uren("2026-11-26", 24, 4000.0), _uren("2026-11-26", 24, 1500.0))
        assert list(df.index) == [pd.Timestamp("2026-11-26")]
        assert df.loc["2026-11-26", "totalHpHeat"] == pytest.approx(96_000)
        assert df.loc["2026-11-26", "totalHpElectric"] == pytest.approx(36_000)

    def test_dag_met_gaten_houdt_quatt(self):
        n = OPENQUATT_MIN_HOURS_PER_DAY - 1
        assert openquatt_daily_from_hourly(_uren("2026-11-26", n, 4000.0), _uren("2026-11-26", n, 1500.0)).empty

    def test_alleen_uren_met_beide(self):
        warmte = _uren("2026-11-26", 24, 4000.0)
        stroom = _uren("2026-11-26", 24, 1500.0)[:20]
        assert openquatt_daily_from_hourly(warmte, stroom).empty

    def test_zomertijdwissel_telt_mee(self):
        # 29-03-2026 heeft 23 uur (zomertijd); dat haalt de drempel nog.
        df = openquatt_daily_from_hourly(_uren("2026-03-29", 23, 3000.0), _uren("2026-03-29", 23, 1000.0))
        assert df.loc["2026-03-29", "totalHpHeat"] == pytest.approx(72_000)


def _dagen(**kolommen) -> pd.DataFrame:
    return pd.DataFrame(kolommen, index=pd.to_datetime(["2026-07-01", "2026-11-25", "2026-11-26"]))


class TestOverleggen:
    def _cloud(self):
        # Zomerdag (koelen), twee stookdagen; cloud-COP te laag.
        return _dagen(
            totalHpHeat=[-20_000.0, 96_000.0, 100_000.0],
            totalHpElectric=[6_000.0, 42_000.0, 45_000.0],
            totalBoilerHeat=[0.0, 2_400.0, 0.0],
            totalHeatPerHour=[-20_000 / 24, 98_400 / 24, 100_000 / 24],
            averageCOP=[0.0, 96 / 42, 100 / 45],
        )

    def _oq(self):
        return _dagen(totalHpHeat=[0.0, 96_500.0, 100_200.0], totalHpElectric=[5_000.0, 36_000.0, 37_500.0])

    def test_stookdagen_van_openquatt(self):
        df, n = apply_openquatt_days(self._cloud(), self._oq())
        assert n == 2
        assert df.loc["2026-11-26", "totalHpElectric"] == 37_500
        assert df.loc["2026-11-26", "averageCOP"] == pytest.approx(100_200 / 37_500)

    def test_ketel_blijft_in_de_warmtevraag(self):
        df, _ = apply_openquatt_days(self._cloud(), self._oq())
        assert df.loc["2026-11-25", "totalHeatPerHour"] == pytest.approx((96_500 + 2_400) / 24)

    def test_koeldag_blijft_staan(self):
        df, _ = apply_openquatt_days(self._cloud(), self._oq())
        assert df.loc["2026-07-01", "totalHpHeat"] == -20_000
        assert df.loc["2026-07-01", "averageCOP"] == 0

    def test_dagen_zonder_openquatt_blijven(self):
        oq = self._oq().loc[["2026-11-26"]]
        df, n = apply_openquatt_days(self._cloud(), oq)
        assert n == 1
        assert df.loc["2026-11-25", "totalHpElectric"] == 42_000

    @pytest.mark.parametrize("leeg", ["cloud", "oq"])
    def test_leeg_verandert_niets(self, leeg):
        cloud = pd.DataFrame() if leeg == "cloud" else self._cloud()
        oq = pd.DataFrame() if leeg == "oq" else self._oq()
        df, n = apply_openquatt_days(cloud, oq)
        assert n == 0 and df is cloud


class _Recorder:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


NODE = {"Total Heat Power": "sensor.openquatt_total_heat_power",
        "Total Power Input": "sensor.openquatt_total_power_input"}


class TestOphalen:
    async def _haal(self, node, heat=None, elec=None):
        rows = {NODE["Total Heat Power"]: _uren("2026-11-26", 24, 4000.0),
                NODE["Total Power Input"]: _uren("2026-11-26", 24, 1500.0)}
        with patch.object(quatt_mod, "_async_openquatt_node", return_value=node), \
             patch.object(quatt_mod, "get_instance", return_value=_Recorder()), \
             patch.object(quatt_mod, "statistics_during_period",
                          side_effect=lambda hass, **kw: {e: rows[e] for e in kw["statistic_ids"]}) as stats:
            df = await quatt_mod._async_fetch_openquatt_daily(MagicMock(), "2026-11-26", "2026-11-26", heat, elec)
        return df, stats

    async def test_zonder_openquatt_niets(self):
        df, stats = await self._haal({})
        assert df.empty and not stats.called

    async def test_met_openquatt(self):
        df, stats = await self._haal(NODE)
        assert df.loc["2026-11-26", "totalHpElectric"] == pytest.approx(36_000)
        assert stats.call_args.kwargs["period"] == "hour"

    async def test_ingestelde_andere_sensor_gaat_voor(self):
        df, stats = await self._haal(NODE, elec="sensor.eigen_kwh_meter_vermogen")
        assert df.empty and not stats.called

    async def test_ingesteld_op_openquatt_zelf_mag(self):
        df, _ = await self._haal(NODE, heat=NODE["Total Heat Power"], elec=NODE["Total Power Input"])
        assert not df.empty
