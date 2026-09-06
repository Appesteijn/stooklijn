"""Tests voor de zonnestraling-keten: onbekend is geen nul.

Twee fouten die dezelfde vorm hebben — er staat een getal, het ziet er geldig
uit, en het hoort ergens anders bij:

- **Open-Meteo onbereikbaar** gaf ``0.0`` terug. Die nul ging de RC-regressie in
  alsof het donker was. Zonnewinst belandt dan in het residu, θ₁ en θ₂ verklaren
  dezelfde trage drift, en het geleerde warmteverlies zakt weg terwijl
  ``converged`` gewoon ``true`` blijft. Precies de fout die deze installatie in
  augustus 2026 al een keer betaald heeft — maar dan stil, en bij iedereen zonder
  route naar Open-Meteo.
- **De reeks werd op uur-index uitgelezen**, terwijl hij vanaf middernacht in een
  vaste zone (Europe/Amsterdam) werd opgehaald. Buiten die zone schoof alles een
  of twee uur op, en op een DST-dag klopte het uurrekenwerk sowieso niet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.quatt_stooklijn.const import SOLAR_FORECAST_MAX_AGE_SECONDS
from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

# Vaste lokale klok voor de tests: 6 september 2026, 10:00 in UTC+2.
TZ = timezone(timedelta(hours=2))
NOW_LOCAL = datetime(2026, 9, 6, 10, 30, tzinfo=TZ)
NOW_UTC = NOW_LOCAL.astimezone(timezone.utc)


def _times(start_hour: int = 0, count: int = 48) -> list[str]:
    """Open-Meteo-achtige stempels vanaf middernacht, lokale tijd."""
    base = datetime(2026, 9, 6, start_hour, tzinfo=TZ)
    return [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(count)]


def _sensor(values=None, times=None, fetched_at=NOW_UTC) -> QuattMpcSensor:
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.hass = MagicMock()
    sensor._entry = MagicMock(entry_id="e1", data={}, options={})
    sensor._solar_radiation = values if values is not None else []
    sensor._solar_times = times if times is not None else []
    sensor._solar_fetched_at = fetched_at
    return sensor


def _patched_clock(monkeypatch):
    """dt_util.now/utcnow vastzetten op NOW_LOCAL/NOW_UTC."""
    import custom_components.quatt_stooklijn.sensor as sensor_mod

    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: NOW_LOCAL)
    monkeypatch.setattr(sensor_mod.dt_util, "utcnow", lambda: NOW_UTC)


class TestOnbekendIsGeenNul:
    """``None`` betekent 'ik weet het niet', ``0.0`` betekent 'het is donker'."""

    def test_geen_reeks_geeft_none(self, monkeypatch):
        _patched_clock(monkeypatch)
        assert _sensor()._get_current_solar_radiation_wm2() is None

    def test_echte_nul_blijft_nul(self, monkeypatch):
        """Middernacht is 0 W/m² en dat is een uitspraak, geen ontbrekende data."""
        _patched_clock(monkeypatch)
        s = _sensor(values=[0.0] * 48, times=_times())
        assert s._get_current_solar_radiation_wm2() == 0.0

    def test_waarde_van_het_juiste_uur(self, monkeypatch):
        _patched_clock(monkeypatch)
        values = [float(i) for i in range(48)]
        s = _sensor(values=values, times=_times())
        # 10:00 lokaal is index 10 in een reeks die om middernacht begint.
        assert s._get_current_solar_radiation_wm2() == 10.0

    def test_verouderde_reeks_geeft_none(self, monkeypatch):
        """Een reeks van gisteren met de klok van vandaag uitlezen is de bug."""
        _patched_clock(monkeypatch)
        oud = NOW_UTC - timedelta(seconds=SOLAR_FORECAST_MAX_AGE_SECONDS + 60)
        s = _sensor(values=[float(i) for i in range(48)], times=_times(), fetched_at=oud)
        assert s._get_current_solar_radiation_wm2() is None

    def test_net_binnen_de_houdbaarheid(self, monkeypatch):
        _patched_clock(monkeypatch)
        vers = NOW_UTC - timedelta(seconds=SOLAR_FORECAST_MAX_AGE_SECONDS - 60)
        s = _sensor(values=[float(i) for i in range(48)], times=_times(), fetched_at=vers)
        assert s._get_current_solar_radiation_wm2() == 10.0

    def test_zonder_ophaalmoment_geen_waarde(self, monkeypatch):
        """Een reeks zonder stempel is niet te beoordelen, dus telt hij niet."""
        _patched_clock(monkeypatch)
        s = _sensor(values=[float(i) for i in range(48)], times=_times(), fetched_at=None)
        assert s._get_current_solar_radiation_wm2() is None


class TestOpTijdstempelNietOpIndex:
    """De reeks hoeft niet om middernacht te beginnen, en telt niet altijd 24 uur."""

    def test_reeks_die_later_begint(self, monkeypatch):
        """Begint de reeks om 06:00, dan is 10:00 index 4 en niet index 10."""
        _patched_clock(monkeypatch)
        values = [float(i) for i in range(24)]
        s = _sensor(values=values, times=_times(start_hour=6, count=24))
        assert s._get_current_solar_radiation_wm2() == 4.0

    def test_uur_dat_ontbreekt_geeft_none(self, monkeypatch):
        """Het uur van de DST-sprong staat niet in de reeks: dan is er niets te melden."""
        _patched_clock(monkeypatch)
        times = _times()
        values = [float(i) for i in range(48)]
        weg = times.index("2026-09-06T10:00")
        del times[weg]
        del values[weg]
        s = _sensor(values=values, times=times)
        assert s._get_current_solar_radiation_wm2() is None

    def test_toekomstig_uur_opzoeken(self, monkeypatch):
        _patched_clock(monkeypatch)
        s = _sensor(values=[float(i) for i in range(48)], times=_times())
        assert s._solar_radiation_at(NOW_LOCAL + timedelta(hours=3)) == 13.0

    def test_uur_voorbij_het_einde_geeft_none(self, monkeypatch):
        _patched_clock(monkeypatch)
        s = _sensor(values=[float(i) for i in range(12)], times=_times(count=12))
        assert s._solar_radiation_at(NOW_LOCAL + timedelta(hours=5)) is None


class TestVersheidsvlag:
    """Wat het attribuut over de reeks meldt."""

    def test_vers(self, monkeypatch):
        _patched_clock(monkeypatch)
        s = _sensor(values=[1.0] * 48, times=_times())
        assert s._solar_forecast_is_fresh is True

    def test_leeg(self, monkeypatch):
        _patched_clock(monkeypatch)
        assert _sensor()._solar_forecast_is_fresh is False

    def test_verouderd(self, monkeypatch):
        _patched_clock(monkeypatch)
        oud = NOW_UTC - timedelta(seconds=SOLAR_FORECAST_MAX_AGE_SECONDS + 1)
        s = _sensor(values=[1.0] * 48, times=_times(), fetched_at=oud)
        assert s._solar_forecast_is_fresh is False


# ---------------------------------------------------------------------------
# De ophaalronde zelf: tijdzone en opslag
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.url = None

    def get(self, url, timeout=None):
        self.url = url
        return _Resp(self._payload, self._status)


def _fetch(monkeypatch, payload, time_zone="Europe/Amsterdam", status=200):
    """Draai één ophaalronde met een nagebootste HTTP-sessie."""
    import asyncio

    import custom_components.quatt_stooklijn.sensor as sensor_mod

    _patched_clock(monkeypatch)
    session = _Session(payload, status)
    monkeypatch.setattr(sensor_mod, "async_get_clientsession", lambda hass: session)

    sensor = _sensor(fetched_at=None)
    sensor.hass.config.latitude = 52.1
    sensor.hass.config.longitude = 5.2
    sensor.hass.config.time_zone = time_zone
    sensor.async_write_ha_state = MagicMock()

    asyncio.run(sensor._async_refresh_solar_radiation())
    return sensor, session


def _payload(count=48):
    return {
        "hourly": {
            "time": _times(count=count),
            "shortwave_radiation": [float(i) for i in range(count)],
        }
    }


class TestOphalen:
    def test_tijdzone_uit_de_ha_config(self, monkeypatch):
        """Stond vast op Europe/Amsterdam; buiten die zone schoof alles op."""
        _, session = _fetch(monkeypatch, _payload(), time_zone="Europe/Berlin")
        assert "timezone=Europe%2FBerlin" in session.url

    def test_tijdzone_wordt_ge_url_encodeerd(self, monkeypatch):
        _, session = _fetch(monkeypatch, _payload(), time_zone="America/New_York")
        assert "timezone=America%2FNew_York" in session.url

    def test_zonder_tijdzone_valt_terug_op_utc(self, monkeypatch):
        _, session = _fetch(monkeypatch, _payload(), time_zone=None)
        assert "timezone=UTC" in session.url

    def test_lat_lon_uit_de_ha_config(self, monkeypatch):
        _, session = _fetch(monkeypatch, _payload())
        assert "latitude=52.1" in session.url and "longitude=5.2" in session.url

    def test_reeks_en_stempels_worden_bewaard(self, monkeypatch):
        sensor, _ = _fetch(monkeypatch, _payload())
        assert len(sensor._solar_radiation) == 48
        assert len(sensor._solar_times) == 48
        assert sensor._solar_fetched_at == NOW_UTC

    def test_half_antwoord_wordt_geweigerd(self, monkeypatch):
        """Waarden zonder bijpassende stempels: dan staat het uur niet vast."""
        payload = {"hourly": {"time": _times(count=10),
                              "shortwave_radiation": [1.0] * 48}}
        sensor, _ = _fetch(monkeypatch, payload)
        assert sensor._solar_radiation == []
        assert sensor._solar_fetched_at is None

    def test_foutstatus_laat_de_staat_ongemoeid(self, monkeypatch):
        sensor, _ = _fetch(monkeypatch, _payload(), status=503)
        assert sensor._solar_fetched_at is None


# ---------------------------------------------------------------------------
# Waar het om begonnen was: niet leren zonder straling
# ---------------------------------------------------------------------------

def _learning_sensor(monkeypatch, solar_known: bool):
    """MPC-sensor klaar voor één uurtick, met of zonder bekende straling."""
    import custom_components.quatt_stooklijn.sensor as sensor_mod
    from unittest.mock import AsyncMock

    _patched_clock(monkeypatch)
    monkeypatch.setattr(
        sensor_mod, "async_source_entity",
        lambda hass, entry_id, role, **kw: f"sensor.{role}",
    )
    monkeypatch.setattr(sensor_mod, "get_float_state", lambda hass, entity: 20.0)

    if solar_known:
        sensor = _sensor(values=[float(i) for i in range(48)], times=_times())
    else:
        sensor = _sensor()

    sensor._thermal_loaded = True
    sensor._thermal_store = MagicMock()
    sensor._thermal_store.model.update = MagicMock(return_value=True)
    sensor._thermal_store.async_save = AsyncMock()
    sensor.coordinator = MagicMock(data=None)
    sensor._refresh_u_prior = MagicMock()
    sensor._async_refresh_forecast = AsyncMock()
    sensor._async_refresh_solar_radiation = AsyncMock()
    return sensor


class TestLerenZonderStraling:
    """Een uur overslaan kost bijna niets; met een verzonnen nul leren wel."""

    def test_geen_straling_geen_update(self, monkeypatch):
        import asyncio

        sensor = _learning_sensor(monkeypatch, solar_known=False)
        asyncio.run(sensor._async_hourly_update())
        sensor._thermal_store.model.update.assert_not_called()
        sensor._thermal_store.async_save.assert_not_called()

    def test_met_straling_wel_een_update(self, monkeypatch):
        import asyncio

        sensor = _learning_sensor(monkeypatch, solar_known=True)
        asyncio.run(sensor._async_hourly_update())
        sensor._thermal_store.model.update.assert_called_once()
        # 10:00 lokaal → index 10 → 10.0 W/m² gaat het model in, niet nul.
        assert sensor._thermal_store.model.update.call_args[0][3] == 10.0

    def test_de_forecast_wordt_wel_ververst(self, monkeypatch):
        """Niet leren betekent niet stilvallen: de volgende ronde moet slagen."""
        import asyncio

        sensor = _learning_sensor(monkeypatch, solar_known=False)
        asyncio.run(sensor._async_hourly_update())
        sensor._async_refresh_solar_radiation.assert_awaited_once()
