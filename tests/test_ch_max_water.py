"""Tests voor de schrijfroute van de max-aanvoertemperatuur.

De fout die deze tests afdekken is stil: schrijven naar de CiC terwijl
OpenQuatt regelt levert geen enkele foutmelding op — de CiC-compatibiliteits-
laag bevestigt de schrijfactie en gooit hem weg. Alleen de bestemming zelf
verraadt of het goed gaat.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.ch_max_water import (
    HEAT_DEMAND_ENTITY,
    ChMaxWaterController,
)
from custom_components.quatt_stooklijn.heat_demand import (
    PROXY_FALLBACK_ENTITY,
    PROXY_UNIQUE_ID,
    SOURCE_SELECTOR_ENTITY,
)

HUB = "CIC-abc123"
OQ_MAC = "58:E6:C5:6E:9D:78"

CIC_NUMBER = "number.cic_max_water_temperature"
OQ_NUMBER = "number.openquatt_maximum_water_temperature"
MPC_SOURCE = "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur"
OQ_SELECT = "select.openquatt_external_heat_demand_source"


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


class TestWederzijdseUitsluitingMetDeWarmtevraag:
    """Twee routes naar dezelfde grootheid mogen niet tegelijk lopen.

    Stuurt de warmtevraag Power House rechtstreeks aan, dan is het waterplafond
    daar een veiligheidsbegrenzer en geen stuurknop. Er alsnog een aanvoeradvies
    naartoe schrijven knijpt de vraag die we net zelf hebben gesteld — en het
    plafond blijft staan als de koppeling wegvalt, waar de vraag vanzelf vervalt.
    """

    def _hass_met_koppeling(self, *, firmware, selector, vraag="3200"):
        entries = [
            *_registry_entries(),
            er.RegistryEntry(
                entity_id=OQ_SELECT,
                unique_id=f"{OQ_MAC}/0/select/External Heat Demand Source",
                platform="esphome",
            ),
            er.RegistryEntry(
                entity_id=PROXY_FALLBACK_ENTITY,
                unique_id=PROXY_UNIQUE_ID,
                platform="template",
            ),
        ]
        states = {
            CIC_NUMBER: CIC_STATE,
            OQ_NUMBER: OQ_STATE,
            MPC_SOURCE: _State("32.0"),
            OQ_SELECT: _State(firmware),
            SOURCE_SELECTOR_ENTITY: _State(selector),
            PROXY_FALLBACK_ENTITY: _State("3200"),
            HEAT_DEMAND_ENTITY: _State(vraag),
        }
        hass = MagicMock()
        hass._test_entity_registry = er.FakeRegistry(entries)
        hass.states.get = lambda entity_id: states.get(entity_id)
        hass.services.async_call = AsyncMock()
        return hass

    @pytest.mark.asyncio
    async def test_slaat_over_als_de_warmtevraag_stuurt(self):
        hass = self._hass_met_koppeling(
            firmware="HA input", selector=HEAT_DEMAND_ENTITY
        )
        await _controller(hass)._async_tick(None)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_schrijft_gewoon_als_de_koppeling_uit_staat(self):
        hass = self._hass_met_koppeling(
            firmware="Disabled", selector=HEAT_DEMAND_ENTITY
        )
        await _controller(hass)._async_tick(None)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_schrijft_gewoon_als_de_bronhelper_ergens_anders_wijst(self):
        # Firmware luistert wél naar HA, maar naar een andere bron. Deze
        # integratie stuurt dan niets aan en het plafond is weer een stuurknop.
        hass = self._hass_met_koppeling(
            firmware="HA input", selector="input_number.openquatt_test_heat_demand"
        )
        await _controller(hass)._async_tick(None)
        hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_schrijft_gewoon_als_de_warmtevraag_niets_publiceert(self):
        """Terugtreden mag alleen als er via de andere route ook echt gestuurd wordt.

        Zonder analysedata, of met een bronmeting die te oud is, levert de
        vraag niets. Zou de controller dan ook zwijgen, dan liggen beide wegen
        tegelijk stil.
        """
        hass = self._hass_met_koppeling(
            firmware="HA input", selector=HEAT_DEMAND_ENTITY, vraag="unknown"
        )
        await _controller(hass)._async_tick(None)
        hass.services.async_call.assert_called_once()
