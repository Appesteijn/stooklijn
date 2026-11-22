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


class FakeStore:
    """Store die in het geheugen leeft; ``data`` is wat er op schijf zou staan."""

    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        import copy
        self.data = copy.deepcopy(data)


def _controller(hass, configured="", store=None, **kw):
    return ChMaxWaterController(
        hass=hass,
        number_entity=configured,
        source="mpc",
        hysteresis=1.0,
        interval_minutes=15,
        store=store if store is not None else FakeStore(),
        **kw,
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


class TestConfiguratiesZonderBeideIntegraties:
    """De interlock mag alleen terugtreden waar er echt een andere route is.

    Twee installaties die hier gemakkelijk buiten beeld vallen: een kale Quatt
    CiC zonder OpenQuatt, en OpenQuatt zonder de Quatt-integratie. Voor de
    eerste is deze schrijfroute de énige aansturing die de integratie heeft —
    treedt de interlock daar ten onrechte terug, dan valt die stil zonder dat
    er iets voor in de plaats komt.
    """

    def _hass(self, entries, states):
        hass = MagicMock()
        hass._test_entity_registry = er.FakeRegistry(entries)
        hass.states.get = lambda entity_id: states.get(entity_id)
        hass.services.async_call = AsyncMock()
        return hass

    @pytest.mark.asyncio
    async def test_kale_cic_zonder_openquatt_blijft_schrijven(self):
        # Geen ESPHome-node in het register: geen keuzeknop, geen proxy, en
        # dus niets wat de vraag zou kunnen ontvangen.
        entries = [
            er.RegistryEntry(
                entity_id=CIC_NUMBER,
                unique_id=f"{HUB}:cic:chMaxWaterTemperature",
                platform="quatt",
            )
        ]
        hass = self._hass(
            entries,
            {
                CIC_NUMBER: CIC_STATE,
                MPC_SOURCE: _State("32.0"),
                # De warmtevraag bestaat wél — hij is niet OpenQuatt-specifiek.
                HEAT_DEMAND_ENTITY: _State("3200"),
            },
        )
        ctrl = _controller(hass)
        assert ctrl._demand_link_active() is False
        await ctrl._async_tick(None)
        hass.services.async_call.assert_called_once()
        assert hass.services.async_call.call_args[0][2]["entity_id"] == CIC_NUMBER

    @pytest.mark.asyncio
    async def test_openquatt_zonder_quatt_integratie(self):
        """Geen CiC in het register; OpenQuatt levert de knop."""
        entries = [
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
            OQ_NUMBER: OQ_STATE,
            MPC_SOURCE: _State("32.0"),
            OQ_SELECT: _State("Disabled"),
            HEAT_DEMAND_ENTITY: _State("3200"),
            PROXY_FALLBACK_ENTITY: _State("3200"),
        }
        hass = self._hass(entries, states)
        ctrl = _controller(hass)
        # Koppeling staat uit, dus schrijven — en wel naar OpenQuatt.
        await ctrl._async_tick(None)
        hass.services.async_call.assert_called_once()
        assert hass.services.async_call.call_args[0][2]["entity_id"] == OQ_NUMBER

        # En zodra de koppeling wél loopt, treedt hij terug.
        states[OQ_SELECT] = _State("HA input")
        states[SOURCE_SELECTOR_ENTITY] = _State(HEAT_DEMAND_ENTITY)
        hass.services.async_call.reset_mock()
        await ctrl._async_tick(None)
        hass.services.async_call.assert_not_called()


class TestMarge:
    """Het plafond ligt een marge boven het advies.

    Het advies is de aanvoer die het huis nodig heeft. De regelaar moduleert
    al terug vóórdat hij het plafond haalt, dus een plafond precies op het
    advies levert te weinig en laat geen ruimte om een tekort in te halen.
    """

    def _hass(self, advies="32.0"):
        states = {OQ_NUMBER: OQ_STATE, MPC_SOURCE: _State(advies)}
        hass = MagicMock()
        hass._test_entity_registry = er.FakeRegistry(_registry_entries())
        hass.states.get = lambda entity_id: states.get(entity_id)
        hass.services.async_call = AsyncMock()
        return hass, states

    def _ctrl(self, hass, margin):
        return ChMaxWaterController(
            hass=hass,
            number_entity="",
            source="mpc",
            hysteresis=1.0,
            interval_minutes=15,
            margin=margin,
        )

    def _geschreven(self, hass):
        return hass.services.async_call.call_args.args[2]["value"]

    @pytest.mark.asyncio
    async def test_schrijft_advies_plus_marge(self):
        hass, _ = self._hass("32.0")
        await self._ctrl(hass, 2.0)._async_tick(None)
        assert self._geschreven(hass) == 34.0

    @pytest.mark.asyncio
    async def test_marge_nul_schrijft_het_kale_advies(self):
        hass, _ = self._hass("32.0")
        await self._ctrl(hass, 0.0)._async_tick(None)
        assert self._geschreven(hass) == 32.0

    @pytest.mark.asyncio
    async def test_marge_telt_mee_voor_de_afronding_op_de_stap(self):
        # 31,4 + 1,5 = 32,9, afgerond op de stap van 0,5 → 33,0. De knop
        # krijgt nooit een waarde die niet op zijn stap ligt.
        hass, _ = self._hass("31.4")
        await self._ctrl(hass, 1.5)._async_tick(None)
        assert self._geschreven(hass) == 33.0

    @pytest.mark.asyncio
    async def test_marge_wordt_begrensd_door_het_maximum_van_de_knop(self):
        hass, _ = self._hass("72.0")
        await self._ctrl(hass, 5.0)._async_tick(None)
        assert self._geschreven(hass) == 75.0

    def test_negatieve_marge_wordt_nul(self):
        hass, _ = self._hass()
        assert self._ctrl(hass, -3.0).margin == 0.0

    def test_te_grote_marge_wordt_afgekapt(self):
        hass, _ = self._hass()
        assert self._ctrl(hass, 25.0).margin == 10.0

    def test_default_marge_is_niet_nul(self):
        # Zonder expliciete marge (oude configuratie) hoort het plafond niet
        # meer op het kale advies te liggen.
        hass, _ = self._hass()
        ctrl = ChMaxWaterController(
            hass=hass, number_entity="", source="mpc",
            hysteresis=1.0, interval_minutes=15,
        )
        assert ctrl.margin > 0

    @pytest.mark.asyncio
    async def test_hysterese_vergelijkt_het_plafond_niet_het_advies(self):
        hass, states = self._hass("32.0")
        ctrl = self._ctrl(hass, 2.0)
        await ctrl._async_tick(None)
        assert ctrl.last_written == 34.0
        # Advies 0,5 hoger: plafond 34,5, binnen de hysterese van 1,0.
        states[MPC_SOURCE] = _State("32.5")
        await ctrl._async_tick(None)
        assert hass.services.async_call.call_count == 1
        # Advies 1,0 hoger: plafond 35,0, wél schrijven.
        states[MPC_SOURCE] = _State("33.0")
        await ctrl._async_tick(None)
        assert hass.services.async_call.call_count == 2
        assert self._geschreven(hass) == 35.0


class TestInstellingSensorNaHerstart:
    """Na een herstart of reload toont de sensor de vorige waarde, niet Unknown.

    De controller begint leeg en schrijft pas bij de eerste tick, tot een
    interval later. Unknown verborg in die tijd de dashboardkaart, terwijl de
    regelaar gewoon op de vorige waarde stond.
    """

    def _sensor(self, ctrl, last_state):
        from custom_components.quatt_stooklijn.const import DOMAIN
        from custom_components.quatt_stooklijn.sensor import QuattChMaxWaterSensor

        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1_ch_max_water": ctrl}}
        hass.states.get = lambda entity_id: _State("42")
        entry = MagicMock()
        entry.entry_id = "entry1"
        sensor = QuattChMaxWaterSensor(hass, entry)
        sensor.hass = hass
        sensor.async_get_last_state = AsyncMock(return_value=last_state)
        return sensor

    def _ctrl(self, last_written=None, last_written_at=None):
        ctrl = MagicMock()
        ctrl.last_written = last_written
        ctrl.last_written_at = last_written_at
        ctrl.target_entity = CIC_NUMBER
        ctrl.margin = 2.0
        ctrl._interval.total_seconds.return_value = 1800
        return ctrl

    @pytest.mark.asyncio
    async def test_toont_de_vorige_waarde_tot_de_eerste_schrijfactie(self):
        vorige = _State("34.0", last_written_at="2026-11-18T08:00:00+01:00")
        sensor = self._sensor(self._ctrl(), vorige)
        await sensor.async_added_to_hass()
        assert sensor.native_value == 34.0
        attrs = sensor.extra_state_attributes
        assert attrs["hersteld"] is True
        assert attrs["last_written_at"] == "2026-11-18T08:00:00+01:00"

    @pytest.mark.asyncio
    async def test_nieuwe_schrijfactie_gaat_voor(self):
        from datetime import datetime

        nu = datetime(2026, 11, 18, 9, 0)
        sensor = self._sensor(self._ctrl(36.0, nu), _State("34.0"))
        await sensor.async_added_to_hass()
        assert sensor.native_value == 36.0
        attrs = sensor.extra_state_attributes
        assert attrs["hersteld"] is False
        assert attrs["last_written_at"] == nu.isoformat()

    @pytest.mark.asyncio
    async def test_vorige_unknown_blijft_unknown(self):
        sensor = self._sensor(self._ctrl(), _State("unknown"))
        await sensor.async_added_to_hass()
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["hersteld"] is False

    @pytest.mark.asyncio
    async def test_zonder_vorige_state_unknown(self):
        sensor = self._sensor(self._ctrl(), None)
        await sensor.async_added_to_hass()
        assert sensor.native_value is None


# --------------------------------------------------------------------------- #
#  Herstel en watchdog (v0.10.7-beta.7)                                       #
# --------------------------------------------------------------------------- #


class _Knop:
    """Een hass waarin ``number.set_value`` de knop echt verzet."""

    def __init__(self, states, entries=None):
        self.states_dict = states
        self.hass = MagicMock()
        self.hass._test_entity_registry = er.FakeRegistry(entries or _registry_entries())
        self.hass.states.get = lambda entity_id: self.states_dict.get(entity_id)
        self.calls: list[tuple[str, float]] = []

        async def _call(domain, service, data, blocking=False):
            entity_id, value = data["entity_id"], data["value"]
            self.calls.append((entity_id, value))
            old = self.states_dict[entity_id]
            self.states_dict[entity_id] = _State(str(value), **old.attributes)

        self.hass.services.async_call = AsyncMock(side_effect=_call)

    def zet(self, entity_id, value):
        """Iemand verzet de knop zelf (Quatt-app, dashboard)."""
        old = self.states_dict[entity_id]
        self.states_dict[entity_id] = _State(str(value), **old.attributes)

    def waarde(self, entity_id):
        return float(self.states_dict[entity_id].state)


def _cic_op(waarde=55, advies="38.0"):
    states = {CIC_NUMBER: _State(str(waarde), min=40, max=70, step=1)}
    if advies is not None:
        states[MPC_SOURCE] = _State(advies)
    return _Knop(states)


def _zonder_advies_sinds(ctrl, minuten):
    from datetime import datetime, timedelta, timezone
    ctrl._advice_seen_at = datetime.now(timezone.utc) - timedelta(minutes=minuten)


class TestHerstel:
    """Het plafond gaat terug naar de eigen waarde van de knop.

    Tot v0.10.7-beta.7 bleef het laatste plafond staan bij uitschakelen,
    herladen, een HA-stop of een weggevallen advies: een plafond van 40 °C
    uit zacht weer bleef dan de vorst in staan.
    """

    @pytest.mark.asyncio
    async def test_bewaart_de_oorspronkelijke_waarde_voor_de_eerste_schrijfactie(self):
        knop, store = _cic_op(55), FakeStore()
        await _controller(knop.hass, store=store)._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 40          # 38 + 2 marge
        assert store.data == {"entities": {CIC_NUMBER: {"original": 55.0, "written": 40}}}

    @pytest.mark.asyncio
    async def test_uitschakelen_zet_terug_en_daarna_schrijft_hij_niets_meer(self):
        knop, store = _cic_op(55), FakeStore()
        ctrl = _controller(knop.hass, store=store)
        await ctrl._async_tick(None)
        await ctrl.async_shutdown("test")
        assert knop.waarde(CIC_NUMBER) == 55
        assert store.data == {"entities": {}}
        knop.calls.clear()
        await ctrl._async_tick(None)                  # timer nog niet weg
        assert knop.calls == []

    @pytest.mark.asyncio
    async def test_niet_terugzetten_als_iemand_de_knop_zelf_verzette(self):
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)
        knop.zet(CIC_NUMBER, 50)                      # in de Quatt-app
        knop.calls.clear()
        await ctrl.async_shutdown("test")
        assert knop.calls == []
        assert knop.waarde(CIC_NUMBER) == 50

    @pytest.mark.asyncio
    async def test_geen_schrijfactie_als_de_huidige_waarde_onbekend_is(self):
        knop = _cic_op(55)
        knop.states_dict[CIC_NUMBER] = _State("unknown", min=40, max=70, step=1)
        ctrl = _controller(knop.hass)
        ctrl._resolve_number_entity = lambda: CIC_NUMBER
        await ctrl._async_tick(None)
        assert knop.calls == []

    @pytest.mark.asyncio
    async def test_bestemmingswissel_zet_de_oude_knop_terug(self):
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 40
        knop.states_dict[OQ_NUMBER] = _State("60.0", min=25, max=75, step=0.5)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 55
        assert knop.waarde(OQ_NUMBER) == 40.0

    @pytest.mark.asyncio
    async def test_onbereikbare_knop_blijft_open_en_wordt_later_hersteld(self):
        """Bijsturing uit terwijl de CiC onbereikbaar was: de restore-only
        controller maakt het af zodra de knop terug is."""
        knop, store = _cic_op(55), FakeStore()
        ctrl = _controller(knop.hass, store=store)
        await ctrl._async_tick(None)
        knop.states_dict[CIC_NUMBER] = _State("unavailable", min=40, max=70, step=1)
        await ctrl.async_shutdown("test")
        assert CIC_NUMBER in store.data["entities"]

        knop.states_dict[CIC_NUMBER] = _State("40", min=40, max=70, step=1)
        herstel = _controller(knop.hass, store=store, restore_only=True)
        await herstel._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 55
        assert store.data == {"entities": {}}

    @pytest.mark.asyncio
    async def test_restore_only_schrijft_nooit_een_plafond(self):
        knop = _cic_op(55)
        await _controller(knop.hass, restore_only=True)._async_tick(None)
        assert knop.calls == []

    @pytest.mark.asyncio
    async def test_ha_stop_zet_terug(self):
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        remove = ctrl.async_setup()
        event, handler = knop.hass.bus.async_listen_once.call_args.args
        assert event == "homeassistant_stop"
        await ctrl._async_tick(None)
        await handler(None)
        assert knop.waarde(CIC_NUMBER) == 55
        # Na het afvuren de listener niet nog eens verwijderen (ValueError in HA).
        remove_stop = knop.hass.bus.async_listen_once.return_value
        remove()
        remove_stop.assert_not_called()

    def test_remove_voor_de_stop_meldt_de_listener_af(self):
        knop = _cic_op(55)
        remove = _controller(knop.hass).async_setup()
        remove()
        knop.hass.bus.async_listen_once.return_value.assert_called_once()

    @pytest.mark.asyncio
    async def test_hangende_knop_houdt_het_afsluiten_niet_op(self, monkeypatch):
        import custom_components.quatt_stooklijn.ch_max_water as mod
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)

        async def _hangt(*a, **kw):
            import asyncio
            await asyncio.sleep(10)

        knop.hass.services.async_call = AsyncMock(side_effect=_hangt)
        monkeypatch.setattr(mod, "RESTORE_TIMEOUT_S", 0.01)
        await ctrl.async_shutdown("test")             # mag niet blijven hangen
        assert CIC_NUMBER in ctrl._saved             # later opnieuw proberen


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_zet_terug_als_het_advies_een_uur_wegblijft(self):
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)
        del knop.states_dict[MPC_SOURCE]
        _zonder_advies_sinds(ctrl, 30)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 40          # nog niet
        _zonder_advies_sinds(ctrl, 61)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 55

    @pytest.mark.asyncio
    async def test_loopt_ook_na_een_herstart_zonder_advies(self):
        """Het soundslider-probleem uit de analyse: na een herstart zonder
        advies startte de watchdog nooit. Hier telt hij vanaf de start."""
        knop, store = _cic_op(40, advies=None), FakeStore(
            {"entities": {CIC_NUMBER: {"original": 55.0, "written": 40.0}}}
        )
        ctrl = _controller(knop.hass, store=store)    # na herstart
        _zonder_advies_sinds(ctrl, 61)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 55

    @pytest.mark.asyncio
    async def test_schrijft_weer_als_het_advies_terugkomt(self):
        knop = _cic_op(55)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)
        advies = knop.states_dict.pop(MPC_SOURCE)
        _zonder_advies_sinds(ctrl, 61)
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 55
        knop.states_dict[MPC_SOURCE] = advies
        await ctrl._async_tick(None)
        assert knop.waarde(CIC_NUMBER) == 40


class TestGeenMinMax:
    def test_zonder_min_geen_schrijfactie(self):
        """Een terugval op 0 °C schreef 20 °C naar een knop die dat niet kan."""
        hass = _hass({CIC_NUMBER: _State("55", max=70, step=1)})
        assert _controller(hass)._clamp(18.0, CIC_NUMBER) is None


class TestKoppelingZetTerug:
    @pytest.mark.asyncio
    async def test_plafond_gaat_terug_als_de_warmtevraag_gaat_sturen(self):
        """Een plafond van eerder zou de vraag knijpen die we net zelf stellen."""
        entries = [
            *_registry_entries(),
            er.RegistryEntry(entity_id=OQ_SELECT,
                             unique_id=f"{OQ_MAC}/0/select/External Heat Demand Source",
                             platform="esphome"),
            er.RegistryEntry(entity_id=PROXY_FALLBACK_ENTITY, unique_id=PROXY_UNIQUE_ID,
                             platform="template"),
        ]
        knop = _Knop({
            OQ_NUMBER: _State("60.0", min=25, max=75, step=0.5),
            MPC_SOURCE: _State("32.0"),
            OQ_SELECT: _State("Disabled"),
            SOURCE_SELECTOR_ENTITY: _State(HEAT_DEMAND_ENTITY),
            PROXY_FALLBACK_ENTITY: _State("3200"),
            HEAT_DEMAND_ENTITY: _State("3200"),
        }, entries)
        ctrl = _controller(knop.hass)
        await ctrl._async_tick(None)
        assert knop.waarde(OQ_NUMBER) == 34.0
        knop.states_dict[OQ_SELECT] = _State("HA input")
        await ctrl._async_tick(None)
        assert knop.waarde(OQ_NUMBER) == 60.0


class TestUnloadRuimtOp:
    """Na unload stond de controller nog in hass.data: de adviessensor meldde
    "wordt automatisch bijgestuurd" terwijl er niets meer stuurde, en
    hass.data[DOMAIN] werd nooit leeg, dus de services bleven aangemeld."""

    def _hass(self, data):
        from custom_components.quatt_stooklijn.const import DOMAIN
        hass = MagicMock()
        hass.data = {DOMAIN: data}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        return hass

    @pytest.mark.asyncio
    async def test_unload_zet_terug_en_ruimt_alles_op(self):
        from custom_components.quatt_stooklijn import async_unload_entry
        from custom_components.quatt_stooklijn.const import DOMAIN
        ctrl = MagicMock()
        ctrl.async_shutdown = AsyncMock()
        hass = self._hass({
            "e1": MagicMock(), "e1_sources": 1, "e1_supply_check": 2,
            "e1_ch_max_water": ctrl, "e1_sound_level": "building87",
        })
        entry = MagicMock(entry_id="e1")
        assert await async_unload_entry(hass, entry) is True
        ctrl.async_shutdown.assert_awaited_once()
        assert hass.data[DOMAIN] == {}
        assert hass.services.async_remove.call_count == 3

    @pytest.mark.asyncio
    async def test_andere_entry_blijft_staan(self):
        from custom_components.quatt_stooklijn import async_unload_entry
        from custom_components.quatt_stooklijn.const import DOMAIN
        hass = self._hass({"e1": MagicMock(), "e1_sound_level": "x", "e2": MagicMock()})
        await async_unload_entry(hass, MagicMock(entry_id="e1"))
        assert list(hass.data[DOMAIN]) == ["e2"]
        hass.services.async_remove.assert_not_called()


# --------------------------------------------------------------------------- #
#  Restpunten uit de analyse van 21-11-2026                                   #
# --------------------------------------------------------------------------- #


class TestHystereseOpHetDoel:
    """Met stap 1 en hysterese 1,0 gaf 0,2 K ruis rond een afrondingsgrens een
    schrijfactie per sprong: 48 per etmaal."""

    EIGEN = "number.eigen_knop"

    def _knop(self):
        return _Knop({
            self.EIGEN: _State("50", min=20, max=60, step=1),
            MPC_SOURCE: _State("36.4"),
        })

    @pytest.mark.asyncio
    async def test_ruis_rond_een_afrondingsgrens_schrijft_niet(self):
        knop = self._knop()
        ctrl = _controller(knop.hass, configured=self.EIGEN)
        await ctrl._async_tick(None)                  # 38,4 → 38
        for advies in ("36.6", "36.4", "36.7", "36.3", "36.6"):   # 38,3 … 38,7
            knop.states_dict[MPC_SOURCE] = _State(advies)
            await ctrl._async_tick(None)
        assert [v for _, v in knop.calls] == [38]

    @pytest.mark.asyncio
    async def test_echte_beweging_schrijft_wel(self):
        knop = self._knop()
        ctrl = _controller(knop.hass, configured=self.EIGEN)
        await ctrl._async_tick(None)                  # doel 38,4
        knop.states_dict[MPC_SOURCE] = _State("37.5")  # doel 39,5: +1,1
        await ctrl._async_tick(None)
        assert [v for _, v in knop.calls] == [38, 40]

    @pytest.mark.asyncio
    async def test_zelfde_afgeronde_waarde_nooit_opnieuw(self):
        knop = self._knop()
        ctrl = _controller(knop.hass, configured=self.EIGEN)
        await ctrl._async_tick(None)                  # doel 38,4 → 38
        knop.states_dict[MPC_SOURCE] = _State("35.1")  # doel 37,1: −1,3 maar → 37
        await ctrl._async_tick(None)
        knop.states_dict[MPC_SOURCE] = _State("36.0")  # doel 38,0 → 38
        await ctrl._async_tick(None)
        assert [v for _, v in knop.calls] == [38, 37]


OWN_HUB, NEIGHBOR_HUB = "CIC-eigen", "CIC-buren"
NEIGHBOR_NUMBER = "number.cic_buren_max_water_temperature"


def _twee_cics():
    def _e(hub, entity_id, key):
        return er.RegistryEntry(entity_id=entity_id, unique_id=f"{hub}:cic:{key}",
                                platform="quatt")
    # De eigen CiC staat eerst in het register: die wint elke ongefilterde detectie.
    entries = [
        _e(OWN_HUB, "sensor.heatpump_total_power", "computedPower"),
        _e(OWN_HUB, CIC_NUMBER, "chMaxWaterTemperature"),
        _e(NEIGHBOR_HUB, "sensor.cic_buren_total_power", "computedPower"),
        _e(NEIGHBOR_HUB, NEIGHBOR_NUMBER, "chMaxWaterTemperature"),
    ]
    return _Knop({
        CIC_NUMBER: _State("55", min=40, max=70, step=1),
        NEIGHBOR_NUMBER: _State("60", min=40, max=70, step=1),
        "sensor.heatpump_total_power": _State("3000"),
        "sensor.cic_buren_total_power": _State("2000"),
        MPC_SOURCE: _State("38.0"),
    }, entries)


class TestTweeCics:
    """De resolver kreeg alleen de eigen sleutel mee, geen anker: met twee
    CiC's in één HA schreef de entry van de buren naar de eigen knop."""

    @pytest.mark.asyncio
    async def test_schrijft_naar_de_cic_van_de_eigen_metingen(self):
        knop = _twee_cics()
        ctrl = _controller(knop.hass, anchor_config={
            "power_entity": "sensor.cic_buren_total_power"})
        await ctrl._async_tick(None)
        assert knop.calls == [(NEIGHBOR_NUMBER, 40)]

    @pytest.mark.asyncio
    async def test_zonder_anker_zoals_voorheen(self):
        knop = _twee_cics()
        await _controller(knop.hass)._async_tick(None)
        assert knop.calls == [(CIC_NUMBER, 40)]


class TestHernoemdeWarmtevraag:
    """``HEAT_DEMAND_ENTITY`` was een vaste id. Hernoemd, of van een tweede
    entry, zag de controller de koppeling niet en schreef hij door."""

    @pytest.mark.asyncio
    async def test_koppeling_herkend_op_unique_id(self):
        from custom_components.quatt_stooklijn.const import DOMAIN
        hernoemd = "sensor.mijn_warmtevraag"
        entries = [
            *_registry_entries(),
            er.RegistryEntry(entity_id=OQ_SELECT,
                             unique_id=f"{OQ_MAC}/0/select/External Heat Demand Source",
                             platform="esphome"),
            er.RegistryEntry(entity_id=PROXY_FALLBACK_ENTITY, unique_id=PROXY_UNIQUE_ID,
                             platform="template"),
            er.RegistryEntry(entity_id=hernoemd, unique_id="e1_heat_demand", platform=DOMAIN),
        ]
        knop = _Knop({
            OQ_NUMBER: _State("60.0", min=25, max=75, step=0.5),
            MPC_SOURCE: _State("32.0"),
            OQ_SELECT: _State("HA input"),
            SOURCE_SELECTOR_ENTITY: _State(hernoemd),
            PROXY_FALLBACK_ENTITY: _State("3200"),
            hernoemd: _State("3200"),
        }, entries)
        ctrl = _controller(knop.hass, entry_id="e1")
        ctrl._read_recommended = lambda: 32.0
        await ctrl._async_tick(None)
        assert knop.calls == []
