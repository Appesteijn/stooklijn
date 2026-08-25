"""Tests voor de warmtevraag-koppeling naar Power House.

De fout die deze tests afdekken is onzichtbaar van buitenaf: valt één schakel
van de keten weg, dan valt OpenQuatt stil en correct terug op zijn eigen
huismodel. De installatie blijft dus gewoon verwarmen — alleen niet op onze
meting. Alleen de status verraadt het verschil.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.quatt_stooklijn.discovery import async_heat_demand_link
from custom_components.quatt_stooklijn.heat_demand import (
    FIRMWARE_SOURCE_HA_INPUT,
    OUTDOOR_MAX_AGE_SECONDS,
    PROXY_FALLBACK_ENTITY,
    PROXY_UNIQUE_ID,
    SOURCE_SELECTOR_ENTITY,
    HeatDemandLink,
    selector_entity,
)

OQ_MAC = "58:E6:C5:6E:9D:78"
DEMAND = "sensor.quatt_warmteanalyse_warmtevraag"
SELECT = "select.bijkeuken_openquatt_external_heat_demand_source"


class _State:
    def __init__(self, value, age_seconds: float = 0.0):
        self.state = value
        # De sensor beoordeelt de versheid van zijn bron; een testtoestand
        # zonder tijdstempel zou die controle stilzwijgend overslaan.
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.last_reported = stamp
        self.last_updated = stamp


def _oq(entity_id, name, domain="sensor"):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=f"{OQ_MAC}/0/{domain}/{name}",
        platform="esphome",
    )


def _proxy(entity_id=PROXY_FALLBACK_ENTITY):
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=PROXY_UNIQUE_ID,
        platform="template",
    )


FEEDFORWARD = "sensor.bijkeuken_openquatt_power_house_demand_source"


def _node_entries():
    return [
        _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
        _oq(SELECT, "External Heat Demand Source", "select"),
        _oq(FEEDFORWARD, "Power House – demand source"),
    ]


def _hass(entries, states):
    hass = MagicMock()
    hass._test_entity_registry = er.FakeRegistry(entries)
    known = {k: _State(v) for k, v in states.items()}
    hass.states.get = lambda entity_id: known.get(entity_id)
    return hass


def _link(
    *, firmware="HA input", selector=DEMAND, proxy=True, entries=None, feedforward=None
):
    all_entries = list(entries if entries is not None else _node_entries())
    if proxy:
        all_entries.append(_proxy())
    states = {SELECT: firmware} if firmware is not None else {}
    if feedforward is not None:
        states[FEEDFORWARD] = feedforward
    if selector is not None:
        states[SOURCE_SELECTOR_ENTITY] = selector
    if proxy:
        states[PROXY_FALLBACK_ENTITY] = "3200"
    return async_heat_demand_link(_hass(all_entries, states), DEMAND)


class TestSelectorEntity:
    """De bronhelper draagt ``entity|attribuut``; alleen het entity-deel telt."""

    def test_kale_entity(self):
        assert selector_entity(DEMAND) == DEMAND

    def test_met_attribuut(self):
        assert selector_entity(f"{DEMAND}|heat_demand_w") == DEMAND

    def test_spaties(self):
        assert selector_entity(f"  {DEMAND} | attr ") == DEMAND

    @pytest.mark.parametrize("raw", ["", "   ", None, "unknown", "unavailable"])
    def test_niet_ingesteld(self, raw):
        # Een lege string zou verderop als geldige entity-ID kunnen passeren.
        assert selector_entity(raw) is None


class TestLinkStatus:
    """Elke ontbrekende schakel heeft zijn eigen, oplosbare melding."""

    def test_volledig_gekoppeld(self):
        link = _link()
        assert link.active
        assert link.status == "actief"
        assert link.proxy_entity == PROXY_FALLBACK_ENTITY
        assert link.firmware_source == FIRMWARE_SOURCE_HA_INPUT

    def test_select_wordt_op_naam_gevonden_ondanks_gebiedsprefix(self):
        # De ESPHome-entiteiten van de node krijgen het gebied als prefix zodra
        # het device aan een gebied hangt. Op entity-ID zoeken zou hier falen.
        assert _link().active

    def test_firmware_uit(self):
        link = _link(firmware="Disabled")
        assert not link.active
        assert "Disabled" in link.status

    def test_firmware_op_api_input(self):
        link = _link(firmware="API input")
        assert not link.active
        assert "API input" in link.status

    def test_bronhelper_leeg(self):
        link = _link(selector=None)
        assert not link.active
        assert SOURCE_SELECTOR_ENTITY in link.status

    def test_bronhelper_wijst_ergens_anders(self):
        link = _link(selector="input_number.openquatt_test_heat_demand")
        assert not link.active
        assert "input_number.openquatt_test_heat_demand" in link.status

    def test_proxy_ontbreekt(self):
        link = _link(proxy=False)
        assert not link.active
        assert "package" in link.status

    def test_geen_openquatt(self):
        link = _link(entries=[], firmware=None)
        assert not link.active
        assert link.status == "OpenQuatt niet gevonden"

    def test_select_zonder_state(self):
        # Node gevonden, maar de knop heeft (nog) geen waarde. Dat is een
        # opstartmoment, geen ontbrekende installatie — en dat onderscheid
        # bepaalt of je gaat zoeken of gewoon even wacht.
        link = _link(firmware="unavailable")
        assert not link.active
        assert link.status == "OpenQuatt-keuzeknop nog zonder waarde"

    def test_attribuut_selector_telt_als_koppeling(self):
        link = _link(selector=f"{DEMAND}|buiten_temp")
        assert link.active


class TestHeatDemandLinkPureLogic:
    """De dataclass zelf, los van HA."""

    def test_alle_schakels_nodig(self):
        volledig = HeatDemandLink(
            demand_entity=DEMAND,
            firmware_source=FIRMWARE_SOURCE_HA_INPUT,
            proxy_entity=PROXY_FALLBACK_ENTITY,
            selector=DEMAND,
        )
        assert volledig.active

        for veld, waarde in (
            ("firmware_source", "Disabled"),
            ("proxy_entity", None),
            ("selector", "sensor.iets_anders"),
        ):
            kapot = replace(volledig, **{veld: waarde})
            assert not kapot.active, veld


HLC = 284.8      # W/K, uit de seizoensregressie over 418 dagen
BALANCE = 16.66  # °C
OUTDOOR = "sensor.buiten"


class TestWarmtevraagSensor:
    """Wat de sensor publiceert: het kale huismodel, niets afgetrokken."""

    def _sensor(self, *, t_outdoor, data=True, rated=None, t0=None, age=0.0):
        from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
        from custom_components.quatt_stooklijn.coordinator import QuattStooklijnData
        from custom_components.quatt_stooklijn.sensor import QuattHeatDemandSensor

        payload = None
        if data:
            payload = QuattStooklijnData(
                heat_loss_hp=HeatLossResult(
                    slope=-HLC,
                    intercept=HLC * BALANCE,
                    heat_loss_coefficient=HLC,
                    balance_point=BALANCE,
                ),
            )

        # Subklasse in plaats van de property op de echte klasse te overschrijven:
        # dat laatste lekt naar elke andere test die deze sensor aanraakt.
        class _Fake(QuattHeatDemandSensor):
            _outdoor_entity = OUTDOOR

        sensor = _Fake.__new__(_Fake)
        # __init__ overgeslagen: die vraagt een echte hass voor de entity-ID.
        sensor.coordinator = type("C", (), {"data": payload})()
        sensor.entity_id = DEMAND
        sensor.hass = MagicMock()
        # Velden die __init__ normaal zet; dat wordt hier overgeslagen omdat
        # het een echte hass vraagt voor de entity-ID.
        sensor._openquatt_cache = None
        sensor._stale_logged = False
        states = {}
        if t_outdoor is not None:
            states[OUTDOOR] = _State(str(t_outdoor), age_seconds=age)
        if rated is not None:
            states["number.oq_pr"] = _State(str(rated))
        if t0 is not None:
            states["number.oq_t0"] = _State(str(t0))
        sensor.hass.states.get = lambda entity_id: states.get(entity_id)
        return sensor

    def test_vraag_volgt_het_huismodel(self):
        # 0 °C buiten: 284,8 × 16,66 ≈ 4745 W
        sensor = self._sensor(t_outdoor=0.0)
        assert sensor.native_value == round(HLC * BALANCE)

    def test_kouder_is_meer_vraag(self):
        koud = self._sensor(t_outdoor=-5.0).native_value
        mild = self._sensor(t_outdoor=5.0).native_value
        assert koud > mild

    def test_boven_het_balanspunt_is_de_vraag_nul(self):
        # Niet negatief: een negatieve vraag zou de firmware als geldige waarde
        # aannemen en op 0 klemmen, maar hier al afkappen houdt het eerlijk.
        assert self._sensor(t_outdoor=25.0).native_value == 0

    def test_geen_analysedata_geeft_none(self):
        # De proxy maakt daar 0 W van maar zet zijn valid-vlag uit; de firmware
        # houdt dan 300 s vast en valt daarna terug op haar eigen model.
        assert self._sensor(t_outdoor=0.0, data=False).native_value is None

    def test_geen_buitentemperatuur_geeft_none(self):
        assert self._sensor(t_outdoor=None).native_value is None

    def test_attributen_dragen_het_model_en_de_koppelstatus(self):
        sensor = self._sensor(t_outdoor=0.0)
        attrs = sensor.extra_state_attributes
        assert attrs["warmteverliescoefficient"] == round(HLC, 1)
        assert attrs["balanspunt_gemeten"] == round(BALANCE, 2)
        assert attrs["nulpunt"] == round(BALANCE, 2)
        assert attrs["nulpunt_bron"] == "meting"
        assert attrs["koppeling_actief"] is False
        assert attrs["bronhelper"] == SOURCE_SELECTOR_ENTITY

    def test_zonder_openquatt_geen_keuzeknop(self):
        """Het dashboard hangt hieraan of het de koppelinstructie toont.

        Een installatie met alleen een CiC krijgt de warmtevraag gewoon te
        zien, maar niet de aansporing om een knop om te zetten die er niet is.
        """
        attrs = self._sensor(t_outdoor=0.0).extra_state_attributes
        assert attrs["keuzeknop_entity"] is None
        assert attrs["koppeling"] == "OpenQuatt niet gevonden"

    def test_met_openquatt_wel_een_keuzeknop(self):
        sensor = self._sensor(t_outdoor=0.0)
        with _patch_select(SELECT):
            attrs = sensor.extra_state_attributes
        assert attrs["keuzeknop_entity"] == SELECT

    def test_meldt_wanneer_de_firmware_de_vraag_afkapt(self):
        """Pr klemt de externe vraag, en de firmware zegt daar niets over."""
        sensor = self._sensor(t_outdoor=-15.0, rated=5550)
        with _patch_rated("number.oq_pr"):
            attrs = sensor.extra_state_attributes
        assert attrs["firmware_plafond_w"] == 5550
        assert attrs["boven_firmware_plafond"] is True

    def test_geen_melding_onder_het_plafond(self):
        sensor = self._sensor(t_outdoor=5.0, rated=5550)
        with _patch_rated("number.oq_pr"):
            attrs = sensor.extra_state_attributes
        assert attrs["boven_firmware_plafond"] is False

    def test_init_pint_de_entity_id(self):
        """HA leidt de ID van een nieuwe entity af uit het gebied van het device."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import QuattHeatDemandSensor

        src = inspect.getsource(QuattHeatDemandSensor.__init__)
        assert "async_generate_entity_id" in src
        assert "warmtevraag" in src

    def test_de_gepinde_id_is_de_id_waar_ch_max_water_op_let(self):
        """Eén naam op twee plekken; los uit elkaar lopen is niet te merken."""
        from custom_components.quatt_stooklijn.ch_max_water import HEAT_DEMAND_ENTITY
        from custom_components.quatt_stooklijn.sources import ENTITY_PREFIX

        assert HEAT_DEMAND_ENTITY == f"sensor.{ENTITY_PREFIX}_warmtevraag"


def _patch_rated(entity_id):
    """Doe alsof de Pr-knop van OpenQuatt op ``entity_id`` gevonden wordt."""
    from unittest.mock import patch

    from custom_components.quatt_stooklijn.discovery import ROLE_PH_RATED_POWER

    return patch(
        "custom_components.quatt_stooklijn.discovery."
        "async_discover_openquatt_entities",
        return_value={ROLE_PH_RATED_POWER: entity_id},
    )


def _patch_select(entity_id):
    """Doe alsof de keuzeknop van OpenQuatt op ``entity_id`` gevonden wordt."""
    from unittest.mock import patch

    from custom_components.quatt_stooklijn.discovery import (
        ROLE_EXT_HEAT_DEMAND_SOURCE,
    )

    return patch(
        "custom_components.quatt_stooklijn.discovery."
        "async_discover_openquatt_entities",
        return_value={ROLE_EXT_HEAT_DEMAND_SOURCE: entity_id},
    )


class TestNulpunt:
    """Waar de warmtevraag nul wordt — en waarom de regelaar daarin leidend is.

    De regressie ziet geen enkele dag boven de stookgrens, dus haar nulpunt is
    extrapolatie. ``power_house.py`` rekent Tc en Pr daarom al tegen de
    stookgrens van de regelaar uit; deze sensor hoort hetzelfde nulpunt te
    gebruiken, anders publiceert dezelfde integratie twee verschillende
    huismodellen.
    """

    def _sensor(self, **kw):
        return TestWarmtevraagSensor()._sensor(**kw)

    def test_regelaar_gaat_voor_de_meting(self):
        sensor = self._sensor(t_outdoor=0.0, t0=16.0)
        with _patch_t0("number.oq_t0"):
            attrs = sensor.extra_state_attributes
        assert attrs["nulpunt"] == 16.0
        assert attrs["nulpunt_bron"] == "regelaar"
        # 284,8 × 16,0 — niet × 16,66
        assert attrs["balanspunt_gemeten"] == round(BALANCE, 2)

    def test_vraag_volgt_het_nulpunt_van_de_regelaar(self):
        sensor = self._sensor(t_outdoor=0.0, t0=16.0)
        with _patch_t0("number.oq_t0"):
            assert sensor._demand() == round(HLC * 16.0)

    def test_geen_vraag_boven_de_stookgrens_van_de_regelaar(self):
        """Het bandje waarin de firmware zelf nul zegt.

        Tussen de stookgrens (16,0) en het gemeten balanspunt (16,66) zou de
        oude berekening tot ~188 W vragen, terwijl het firmware-model daar 0
        staat — de installatie zou boven haar eigen stookgrens gaan stoken.
        """
        for t in (16.0, 16.2, 16.5):
            sensor = self._sensor(t_outdoor=t, t0=16.0)
            with _patch_t0("number.oq_t0"):
                assert sensor._demand() == 0, t

    def test_zonder_regelaar_valt_hij_terug_op_de_meting(self):
        sensor = self._sensor(t_outdoor=0.0)
        assert sensor._demand() == round(HLC * BALANCE)


class TestVersheidVanDeBuitentemperatuur:
    """Een bevroren bronsensor levert nog steeds een geldig getal.

    Niets verderop in de keten merkt dat op: de proxy blijft ``valid`` en de
    firmware ziet geen reden om terug te vallen op haar eigen model. Deze
    controle is de enige plek waar dat gat gedicht wordt.
    """

    def _sensor(self, **kw):
        return TestWarmtevraagSensor()._sensor(**kw)

    def test_verse_meting_wordt_gebruikt(self):
        assert self._sensor(t_outdoor=0.0, age=60).native_value is not None

    def test_gat_van_950_s_is_nog_normaal(self):
        # Het grootste gat dat over 24 uur op deze installatie gemeten is.
        # Een strakkere drempel zou de vraag routinematig intrekken.
        assert self._sensor(t_outdoor=0.0, age=950).native_value is not None

    def test_net_onder_de_grens(self):
        assert (
            self._sensor(t_outdoor=0.0, age=OUTDOOR_MAX_AGE_SECONDS - 1).native_value
            is not None
        )

    def test_bevroren_meting_publiceert_niets(self):
        assert (
            self._sensor(t_outdoor=0.0, age=OUTDOOR_MAX_AGE_SECONDS + 1).native_value
            is None
        )


def _patch_t0(entity_id):
    """Doe alsof de stookgrens-knop van OpenQuatt op ``entity_id`` staat."""
    from unittest.mock import patch

    from custom_components.quatt_stooklijn.discovery import ROLE_PH_ZERO_POWER_TEMP

    return patch(
        "custom_components.quatt_stooklijn.discovery."
        "async_discover_openquatt_entities",
        return_value={ROLE_PH_ZERO_POWER_TEMP: entity_id},
    )


class TestFirmwareBevestiging:
    """De firmware heeft het laatste woord over wat ze gebruikt.

    ``Power House – demand source`` is de enige betrouwbare indicator:
    ``P_house`` toont bewust altijd de gemodelleerde waarde, ook terwijl een
    externe vraag stuurt.
    """

    def test_bevestigd_is_actief(self):
        link = _link(feedforward="external")
        assert link.confirmed is True
        assert link.active
        assert "bevestigd" in link.status

    def test_keten_compleet_maar_firmware_op_eigen_model(self):
        """De stille faalmodus: aan de HA-kant lijkt alles goed."""
        link = _link(feedforward="model")
        assert link.wired
        assert link.confirmed is False
        assert link.mismatch
        assert not link.active
        assert "eigen huismodel" in link.status

    def test_zonder_de_diagnostische_sensor_geen_uitsluitsel(self):
        """Ouder OpenQuatt kent hem niet — dat is geen ontkenning."""
        link = _link(entries=[
            _oq("sensor.openquatt_openquatt_version", "OpenQuatt Version", "text_sensor"),
            _oq(SELECT, "External Heat Demand Source", "select"),
        ])
        assert link.confirmed is None
        assert not link.mismatch
        # De voorspelling is dan het beste dat er is.
        assert link.active
        assert link.status == "actief"

    def test_firmware_die_extern_meldt_terwijl_de_keten_niet_af_is(self):
        """Een andere bron voedt de firmware; dat is niet ónze koppeling.

        ``demand source = external`` zegt dát er een externe vraag stuurt, niet
        van wie. Hierop afgaan zou de ch_max_water-route stilzetten terwijl
        onze vraag helemaal niet aankomt — precies de situatie waarin de
        bronhelper nog op een testwaarde staat.
        """
        link = _link(selector="input_number.iets_anders", feedforward="external")
        assert not link.wired
        assert not link.active
        assert not link.mismatch
        assert "input_number.iets_anders" in link.status

    def test_externe_vraag_van_een_andere_bron_bij_elke_losse_schakel(self):
        for kw in (
            {"selector": "input_number.iets_anders"},
            {"firmware": "API input"},
            {"proxy": False},
        ):
            link = _link(feedforward="external", **kw)
            assert not link.active, kw

    def test_geen_mismatch_als_de_keten_niet_af_is(self):
        link = _link(firmware="Disabled", feedforward="model")
        assert not link.mismatch
