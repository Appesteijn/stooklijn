"""Tests voor de schrijfroute van de max-aanvoertemperatuur.

De fout die deze tests afdekken is stil: schrijven naar de CiC terwijl
OpenQuatt regelt levert geen enkele foutmelding op — de CiC-compatibiliteits-
laag bevestigt de schrijfactie en gooit hem weg. Alleen de bestemming zelf
verraadt of het goed gaat.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.ch_max_water import ChMaxWaterController

HUB = "CIC-abc123"
OQ_MAC = "58:E6:C5:6E:9D:78"

CIC_NUMBER = "number.cic_max_water_temperature"
OQ_NUMBER = "number.openquatt_maximum_water_temperature"
MPC_SOURCE = "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur"


class _State:
    def __init__(self, value, **attrs):
        self.state = value
        self.attributes = attrs


# De echte min/max verschillen, en dat is precies wat de fout zichtbaar maakt:
# de CiC kan niet onder de 40 en kapt een advies van 25°C stilletjes af.
CIC_STATE = _State("40", min=40, max=70, step=1)
OQ_STATE = _State("40.0", min=25, max=75, step=0.5)


def _registry_entries():
    return [
        er.RegistryEntry(
            entity_id=CIC_NUMBER,
            unique_id=f"{HUB}:cic:chMaxWaterTemperature",
            platform="quatt",
        ),
        er.RegistryEntry(
            entity_id="sensor.openquatt_openquatt_version",
            unique_id=f"{OQ_MAC}/0/text_sensor/OpenQuatt Version",
            platform="esphome",
        ),
        er.RegistryEntry(
            entity_id=OQ_NUMBER,
            unique_id=f"{OQ_MAC}/0/number/Maximum water temperature",
            platform="esphome",
        ),
    ]


def _hass(states):
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(_registry_entries())
    hass.states.get = lambda entity_id: states.get(entity_id)
    return hass


def _controller(hass, configured=""):
    return ChMaxWaterController(
        hass=hass,
        number_entity=configured,
        source="mpc",
        hysteresis=1.0,
        interval_minutes=15,
    )


class TestSchrijfbestemming:
    def test_schrijft_naar_openquatt_als_die_regelt(self):
        hass = _hass({CIC_NUMBER: CIC_STATE, OQ_NUMBER: OQ_STATE})
        assert _controller(hass)._resolve_number_entity() == OQ_NUMBER

    def test_schrijft_naar_de_cic_zonder_openquatt(self):
        hass = _hass({CIC_NUMBER: CIC_STATE})
        assert _controller(hass)._resolve_number_entity() == CIC_NUMBER

    def test_bestemming_verhuist_mee_als_openquatt_opkomt(self):
        """OpenQuatt kan ná HA opkomen; de bestemming mag niet vastroesten."""
        states = {CIC_NUMBER: CIC_STATE}
        ctrl = _controller(_hass(states))
        # Zolang de node offline is, hoort de CiC de bestemming te zijn.
        assert ctrl._resolve_number_entity() == CIC_NUMBER
        states[OQ_NUMBER] = OQ_STATE
        assert ctrl._resolve_number_entity() == OQ_NUMBER

    def test_bestemmingswissel_wist_de_hysteresis_geschiedenis(self):
        """Elke knop heeft zijn eigen waarde; vergelijken over de wissel heen mag niet."""
        states = {CIC_NUMBER: CIC_STATE}
        ctrl = _controller(_hass(states))
        ctrl._resolve_number_entity()
        ctrl._last_written = 40.0
        states[OQ_NUMBER] = OQ_STATE
        ctrl._resolve_number_entity()
        assert ctrl._last_written is None
        assert ctrl._should_write(40.0) is True

    def test_geen_bruikbare_knop_geeft_none(self):
        """Liever niets schrijven dan struikelen over een lege state."""
        assert _controller(_hass({}))._resolve_number_entity() is None

    def test_expliciete_instelling_blijft_leidend(self):
        eigen = "number.mijn_eigen"
        hass = _hass(
            {CIC_NUMBER: CIC_STATE, OQ_NUMBER: OQ_STATE, eigen: _State("45", min=20, max=60, step=1)}
        )
        assert _controller(hass, configured=eigen)._resolve_number_entity() == eigen


class TestClamp:
    """De ondergrens van de knop bepaalt wat er van het advies overblijft."""

    def test_cic_kapt_een_laag_advies_af_op_40(self):
        hass = _hass({CIC_NUMBER: CIC_STATE})
        assert _controller(hass)._clamp(25.0, CIC_NUMBER) == 40

    def test_openquatt_laat_hetzelfde_advies_wel_door(self):
        hass = _hass({CIC_NUMBER: CIC_STATE, OQ_NUMBER: OQ_STATE})
        assert _controller(hass)._clamp(25.0, OQ_NUMBER) == 25.0

    def test_rondt_af_op_de_stap_van_de_knop(self):
        hass = _hass({OQ_NUMBER: OQ_STATE})
        assert _controller(hass)._clamp(31.4, OQ_NUMBER) == 31.5
