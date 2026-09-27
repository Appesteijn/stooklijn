"""Tests voor de schaduwsensor van de prijsverschuiving.

De rekenkern heeft zijn eigen tests; hier gaat het om wat de sensor eromheen
doet: de juiste ingangen verzamelen, een dag vastleggen, dagen zonder
warmtevraag overslaan, en een reden geven als er geen uitkomst is.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.quatt_stooklijn.analysis.cop_performance import (
    CopPerformanceResult,
)
from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
from custom_components.quatt_stooklijn.const import (
    CONF_PRICE_DAL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MODE,
    CONF_PRICE_NORMAL,
    PRICE_MODE_DYNAMIC,
    PRICE_MODE_NORMAL_DAL,
)
from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData

AMS = ZoneInfo("Europe/Amsterdam")
OUTDOOR = "sensor.buiten"
PRICE = "sensor.stroomprijs"
HLC = 284.8
BALANCE = 16.5
CURVE = {
    -5.0: 1.85, -3.0: 2.11, -1.0: 2.11, 1.0: 2.49, 3.0: 3.09, 5.0: 3.71,
    7.0: 4.11, 9.0: 4.23, 11.0: 4.33, 13.0: 4.55, 15.0: 4.15,
}
# Woensdag 7 oktober 2026, net na middernacht.
NU = datetime(2026, 10, 7, 0, 5, tzinfo=AMS)


class _State:
    def __init__(self, value, attributes=None):
        self.state = value
        self.attributes = attributes or {}
        stamp = datetime.now(timezone.utc)
        self.last_reported = stamp
        self.last_updated = stamp


def _sensor(*, options, temps, states=None, data=True):
    from custom_components.quatt_stooklijn.sensor import QuattPriceShiftSensor

    class _Fake(QuattPriceShiftSensor):
        _outdoor_entity = OUTDOOR

        def _openquatt(self):
            return {}

    sensor = _Fake.__new__(_Fake)
    payload = None
    if data:
        payload = QuattStooklijnData(
            heat_loss_hp=HeatLossResult(
                slope=-HLC,
                intercept=HLC * BALANCE,
                heat_loss_coefficient=HLC,
                balance_point=BALANCE,
            ),
            cop_performance=CopPerformanceResult(
                reference={"najaar": CURVE, "voorjaar": CURVE}
            ),
        )
    sensor.coordinator = type("C", (), {"data": payload})()
    sensor._entry = type("E", (), {"data": {}, "options": options})()
    sensor._mpc = MagicMock()
    sensor._mpc.build_forecast_arrays = lambda t, n_hours: (
        list(temps[:n_hours]), [0.0] * min(n_hours, len(temps)), []
    )
    sensor._mpc.thermal_params = {"converged": True, "C_whk": 25583.0}
    sensor._openquatt_cache = None
    sensor._stale_logged = False
    sensor._store = MagicMock()
    sensor._store.async_save = AsyncMock()
    sensor._days = []
    sensor._loaded = True
    sensor._preview = None
    sensor._reason = None
    sensor.async_write_ha_state = MagicMock()
    alle = {OUTDOOR: _State(str(temps[0]))}
    alle.update(states or {})
    sensor.hass = MagicMock()
    sensor.hass.states.get = lambda entity_id: alle.get(entity_id)
    return sensor


NORMAAL_DAL = {
    CONF_PRICE_MODE: PRICE_MODE_NORMAL_DAL,
    CONF_PRICE_NORMAL: 0.25,
    CONF_PRICE_DAL: 0.20,
}
# Nacht koud, middag zacht — een gewone stookdag.
STOOKDAG = [-2.0] * 7 + [4.0] * 10 + [0.0] * 7


def _run_daily(sensor, nu=NU):
    with patch(
        "custom_components.quatt_stooklijn.sensor.dt_util.now", return_value=nu
    ):
        asyncio.run(sensor._handle_daily())


class TestDagmeting:
    def test_een_stookdag_wordt_vastgelegd(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        _run_daily(sensor)
        assert len(sensor._days) == 1
        dag = sensor._days[0]
        assert dag["datum"] == "2026-10-07"
        assert dag["venster_uren"] == 24
        assert dag["kosten_vlak_eur"] > 0
        assert dag["besparing_eur"] >= 0
        # De uurtabel is voor de voorvertoning, niet voor de store.
        assert "_uren" not in dag
        sensor._store.async_save.assert_awaited_once()

    def test_state_is_het_totaal(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        _run_daily(sensor)
        _run_daily(sensor, NU + timedelta(days=1))
        totaal = sum(d["besparing_eur"] for d in sensor._days)
        assert sensor.native_value == round(totaal, 2)
        assert sensor.extra_state_attributes["gemeten_dagen"] == 2

    def test_dezelfde_dag_twee_keer_telt_een_keer(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        _run_daily(sensor)
        _run_daily(sensor)
        assert len(sensor._days) == 1

    def test_zomerdag_telt_niet_mee(self):
        """Geen warmtevraag: niets te verschuiven, en geen meetdag."""
        sensor = _sensor(options=NORMAAL_DAL, temps=[20.0] * 24)
        _run_daily(sensor)
        assert sensor._days == []
        sensor._store.async_save.assert_not_awaited()

    def test_oude_dagen_worden_opgeruimd(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        sensor._days = [{"datum": "2024-01-01", "besparing_eur": 1.0}]
        _run_daily(sensor)
        assert [d["datum"] for d in sensor._days] == ["2026-10-07"]


class TestVergelijking:
    def test_alleen_cop_loopt_mee(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        _run_daily(sensor)
        dag = sensor._days[0]
        assert "besparing_alleen_cop_eur" in dag
        attrs = sensor.extra_state_attributes
        assert attrs["besparing_alleen_cop_eur"] == round(
            dag["besparing_alleen_cop_eur"], 2
        )

    def test_groot_prijsverschil_levert_meer_op_dan_klein(self):
        klein = _sensor(
            options={**NORMAAL_DAL, CONF_PRICE_DAL: 0.245}, temps=STOOKDAG
        )
        groot = _sensor(
            options={**NORMAAL_DAL, CONF_PRICE_DAL: 0.10}, temps=STOOKDAG
        )
        _run_daily(klein)
        _run_daily(groot)
        assert groot._days[0]["besparing_eur"] > klein._days[0]["besparing_eur"]


class TestDynamisch:
    def _prijssensor(self, uren=48):
        start = NU.replace(minute=0)
        raw = [
            {
                "start": start + timedelta(hours=i),
                "end": start + timedelta(hours=i + 1),
                # Goedkoop 's nachts, duur in de avondpiek.
                "value": 0.15 if (start + timedelta(hours=i)).hour < 6 else 0.30,
            }
            for i in range(uren)
        ]
        return _State("0.2", {"raw_today": raw, "unit_of_measurement": "EUR/kWh"})

    def test_dynamische_prijzen(self):
        sensor = _sensor(
            options={CONF_PRICE_MODE: PRICE_MODE_DYNAMIC, CONF_PRICE_ENTITY: PRICE},
            temps=STOOKDAG,
            states={PRICE: self._prijssensor()},
        )
        _run_daily(sensor)
        assert sensor._days[0]["tariefbron"] == PRICE_MODE_DYNAMIC
        assert sensor._days[0]["besparing_eur"] > 0

    def test_korte_reeks_kort_het_venster_in(self):
        sensor = _sensor(
            options={CONF_PRICE_MODE: PRICE_MODE_DYNAMIC, CONF_PRICE_ENTITY: PRICE},
            temps=STOOKDAG,
            states={PRICE: self._prijssensor(uren=12)},
        )
        _run_daily(sensor)
        assert sensor._days[0]["venster_uren"] == 12

    def test_zonder_prijssensor_een_reden(self):
        sensor = _sensor(
            options={CONF_PRICE_MODE: PRICE_MODE_DYNAMIC, CONF_PRICE_ENTITY: PRICE},
            temps=STOOKDAG,
        )
        _run_daily(sensor)
        assert sensor._days == []
        assert sensor.extra_state_attributes["reden_geen_uitkomst"] == (
            "geen prijzen voor het venster"
        )


class TestZonderIngangen:
    def test_zonder_analysedata(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG, data=False)
        _run_daily(sensor)
        assert sensor._days == []
        assert sensor._reason == "geen analysedata"

    def test_voorvertoning_toont_het_komend_etmaal(self):
        sensor = _sensor(options=NORMAAL_DAL, temps=STOOKDAG)
        with patch(
            "custom_components.quatt_stooklijn.sensor.dt_util.now", return_value=NU
        ):
            asyncio.run(sensor._handle_preview())
        etmaal = sensor.extra_state_attributes["komend_etmaal"]
        assert len(etmaal["uren"]) == 24
        assert {"prijs", "vlak_w", "verschoven_w"} <= set(etmaal["uren"][0])
        # Een voorvertoning is geen meetdag.
        assert sensor._days == []
