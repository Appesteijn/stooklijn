"""Tests voor build_forecast_arrays: index i is één uur, voor elke reeks.

Gerekend na :30 rondde ``round(fc_dt - nu)`` het volgende uur af op index 0,
terwijl de zon (en bij de prijsverschuiving: prijs en tijdlabel) op het lopende
uur bleven. De voorvertoning van de prijsverschuiving stond daardoor een uur te
vroeg, en de simulatie koppelde de temperatuur van 18:00 aan de zon en prijs
van 17:00.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

TZ = timezone(timedelta(hours=2))


def _sensor(now_local: datetime) -> QuattMpcSensor:
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.hass = MagicMock()
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    # Weerverwachting op hele uren vanaf 08:00 lokaal; temperatuur = uur.
    base = datetime(2026, 10, 10, 8, tzinfo=TZ)
    sensor._forecast = [
        {
            "datetime": (base + timedelta(hours=i)).astimezone(timezone.utc).isoformat(),
            "temperature": float((8 + i) % 24),
        }
        for i in range(30)
    ]
    # Zon op hele uren vanaf middernacht; waarde = uur.
    sensor._solar_times = [
        (datetime(2026, 10, 10, tzinfo=TZ) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00")
        for i in range(48)
    ]
    sensor._solar_radiation = [float(i % 24) for i in range(48)]
    sensor._solar_fetched_at = now_local.astimezone(timezone.utc)
    return sensor


def _patched_clock(monkeypatch, now_local: datetime) -> None:
    import custom_components.quatt_stooklijn.sensor as sensor_mod

    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: now_local)
    monkeypatch.setattr(
        sensor_mod.dt_util, "utcnow", lambda: now_local.astimezone(timezone.utc)
    )


@pytest.mark.parametrize("minute", [0, 15, 29, 31, 46, 59])
def test_temperatuur_en_zon_horen_bij_hetzelfde_uur(monkeypatch, minute):
    now_local = datetime(2026, 10, 10, 10, minute, tzinfo=TZ)
    _patched_clock(monkeypatch, now_local)
    temps, solar, meta = _sensor(now_local).build_forecast_arrays(None, n_hours=6)

    assert temps == [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    assert solar == temps
    assert datetime.fromisoformat(meta[0]["datetime"]) == datetime(
        2026, 10, 10, 10, tzinfo=TZ
    )
