"""Tests voor het kamerdoel waar het MPC-advies op rekent.

Tot v0.10.1 stond hier een kale ``20.0`` op vier plekken — drie berekeningen en
de default van ``simulate_forward``. Dat is de waarde van precies één huis. Het
advies is lineair in ``t_setpoint − t_binnen``, dus voor iedereen die anders
stookt zat het er elk uur van de dag even ver naast: op 21,5 °C vroeg het advies
structureel 1,5 K te weinig, zonder dat er ook maar iets aan de sensor te zien
was dat daarop wees.

De thermostaat levert die waarde zelf. ``ROLE_ROOM_SETPOINT`` werd al
gedetecteerd en gespiegeld; hij werd alleen nooit gebruikt.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.quatt_stooklijn.analysis.thermal_model import OnlineRCModel
from custom_components.quatt_stooklijn.const import (
    CONF_ROOM_SETPOINT_FALLBACK,
    DEFAULT_ROOM_SETPOINT,
    ROOM_SETPOINT_MAX,
    ROOM_SETPOINT_MIN,
)
from custom_components.quatt_stooklijn.discovery import (
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_RETURN_TEMP,
    ROLE_ROOM_SETPOINT,
    ROLE_TOTAL_POWER,
)
from custom_components.quatt_stooklijn.sensor import QuattMpcSensor

FLOW = "sensor.debiet"
HEAT = "sensor.thermisch_vermogen"
INDOOR = "sensor.kamertemperatuur"
OUTDOOR = "sensor.buiten"
RETURN = "sensor.retour"
SETPOINT = "sensor.kamer_setpoint"

ROLE_ENTITIES = {
    ROLE_FLOW_RATE: FLOW,
    ROLE_INDOOR_TEMP: INDOOR,
    ROLE_OUTDOOR_TEMP: OUTDOOR,
    ROLE_RETURN_TEMP: RETURN,
    ROLE_ROOM_SETPOINT: SETPOINT,
    ROLE_TOTAL_POWER: HEAT,
}


class _State:
    def __init__(self, value):
        self.state = str(value)


def _hass(**states):
    """Mock-hass waarin alleen de meegegeven entiteiten een waarde hebben."""
    hass = MagicMock()
    known = {k: _State(v) for k, v in states.items()}
    hass.states.get = lambda entity_id: known.get(entity_id)
    return hass


def _sources():
    return patch(
        "custom_components.quatt_stooklijn.sensor.async_source_entity",
        side_effect=lambda hass, entry_id, role, **kw: ROLE_ENTITIES[role],
    )


def _converged_model() -> OnlineRCModel:
    """Een model dat door ``is_converged`` komt, met plausibele huiswaarden."""
    model = OnlineRCModel()
    model._rls.initialise_from_physics(U=250.0, C=6000.0, g=5.0)
    model._rls.n_updates = 100  # ruim boven RLS_MIN_UPDATES
    return model


def _mpc_sensor(options: dict | None = None, **states) -> QuattMpcSensor:
    sensor = QuattMpcSensor.__new__(QuattMpcSensor)
    sensor.hass = _hass(**states)
    sensor._entry = MagicMock(entry_id="e1", data={}, options=options or {})
    sensor._thermal_store = MagicMock(model=_converged_model())
    sensor._thermal_loaded = True
    sensor._solar_radiation = []
    sensor._forecast = []
    sensor.coordinator = MagicMock(data=None)
    return sensor


class TestResolveRoomSetpoint:
    """Welke waarde het advies aanhoudt, en waar die vandaan komt."""

    def test_volgt_de_thermostaat(self):
        sensor = _mpc_sensor(**{SETPOINT: 21.5})
        with _sources():
            assert sensor._resolve_room_setpoint() == (21.5, "thermostaat")

    def test_terugval_zonder_bron(self):
        """Een thermostaat die niet via OpenTherm aan de CiC hangt levert hem niet."""
        sensor = _mpc_sensor()
        with _sources():
            assert sensor._resolve_room_setpoint() == (
                DEFAULT_ROOM_SETPOINT,
                "terugval",
            )

    def test_terugval_bij_unavailable(self):
        """Bij een herstart van de bron staat de sensor tijdelijk op unavailable.

        Doorrekenen op 'unavailable' kan niet, en het advies helemaal intrekken
        voor de duur van een herstart is erger dan even op de terugval draaien.
        """
        sensor = _mpc_sensor(**{SETPOINT: "unavailable"})
        with _sources():
            waarde, bron = sensor._resolve_room_setpoint()
        assert waarde == DEFAULT_ROOM_SETPOINT
        assert bron == "terugval"

    def test_zomerstand_wordt_gevolgd(self):
        """15 °C is een echte keuze, geen meetfout — dus die telt gewoon mee."""
        sensor = _mpc_sensor(**{SETPOINT: 15.0})
        with _sources():
            assert sensor._resolve_room_setpoint() == (15.0, "thermostaat")

    def test_handmatige_boost_valt_binnen_de_band(self):
        """Een gebruiker die de knop even op 25 zet moet gevolgd worden."""
        sensor = _mpc_sensor(**{SETPOINT: 25.0})
        with _sources():
            assert sensor._resolve_room_setpoint() == (25.0, "thermostaat")

    def test_terugval_buiten_de_band(self):
        """Buiten het OT-bereik is het geen kamerdoel maar een meetfout."""
        for onzin in (ROOM_SETPOINT_MIN - 0.1, ROOM_SETPOINT_MAX + 0.1, 0.0, 99.0):
            sensor = _mpc_sensor(**{SETPOINT: onzin})
            with _sources():
                waarde, bron = sensor._resolve_room_setpoint()
            assert (waarde, bron) == (DEFAULT_ROOM_SETPOINT, "terugval"), onzin


class TestInstelbareTerugval:
    """De terugval is een instelling, geen tweede huisaanname.

    Juist de installatie zonder OpenTherm-thermostaat krijgt de sensor nooit, en
    zou dus permanent aan de ingebouwde waarde vastzitten.
    """

    def test_ingestelde_waarde_wint_van_de_default(self):
        sensor = _mpc_sensor(options={CONF_ROOM_SETPOINT_FALLBACK: 21.5})
        with _sources():
            assert sensor._resolve_room_setpoint() == (21.5, "terugval")

    def test_thermostaat_wint_van_de_instelling(self):
        """De instelling is een terugval, geen override."""
        sensor = _mpc_sensor(
            options={CONF_ROOM_SETPOINT_FALLBACK: 21.5}, **{SETPOINT: 19.0}
        )
        with _sources():
            assert sensor._resolve_room_setpoint() == (19.0, "thermostaat")

    def test_zonder_instelling_de_oude_waarde(self):
        """Bestaande installaties updaten zonder gedragsverandering."""
        sensor = _mpc_sensor()
        with _sources():
            assert sensor._resolve_room_setpoint() == (
                DEFAULT_ROOM_SETPOINT,
                "terugval",
            )

    def test_instelling_werkt_door_in_het_advies(self):
        states = {OUTDOOR: 2.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.5}
        with _sources():
            standaard = _mpc_sensor(**states).native_value
            hoger = _mpc_sensor(
                options={CONF_ROOM_SETPOINT_FALLBACK: 21.5}, **states
            ).native_value
        assert hoger > standaard


class TestAdviesVolgtHetSetpoint:
    """Het geadviseerde aanvoerniveau moet meebewegen met het kamerdoel."""

    def _advies(self, setpoint):
        sensor = _mpc_sensor(**{
            OUTDOOR: 2.0,
            RETURN: 28.0,
            FLOW: 800,
            INDOOR: 19.5,
            SETPOINT: setpoint,
        })
        with _sources():
            return sensor.native_value

    def test_hoger_doel_vraagt_hogere_aanvoer(self):
        assert self._advies(21.5) > self._advies(20.0)

    def test_verschil_is_niet_verwaarloosbaar(self):
        """1,5 K kamerdoel is bij dit huis meer dan een tiende aanvoertemperatuur.

        Dit is de kern van de bug: het ging niet om afrondingsruis maar om een
        advies dat merkbaar te laag lag voor iedereen die warmer stookt dan
        ``DEFAULT_ROOM_SETPOINT``.
        """
        assert self._advies(21.5) - self._advies(20.0) > 1.0

    def test_zonder_bron_gedraagt_het_zich_als_voorheen(self):
        """Geen setpoint-sensor: exact het oude gedrag, dus geen verrassing."""
        zonder = _mpc_sensor(**{
            OUTDOOR: 2.0, RETURN: 28.0, FLOW: 800, INDOOR: 19.5,
        })
        with _sources():
            assert zonder.native_value == self._advies(DEFAULT_ROOM_SETPOINT)
