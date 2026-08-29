"""Tests voor het tellen van compressorstarts.

Het getal moet één ding betrouwbaar doen: onderscheid maken tussen een
warmtepomp die kortcyclet en een sensor die hapert. Die twee zien er in de data
bijna hetzelfde uit — een reeks korte aan/uit-overgangen — en als ze niet uit
elkaar gehouden worden is het hele getal waardeloos: dan lijkt elke installatie
met een onbetrouwbare verbinding te pendelen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.quatt_stooklijn.const import (
    COMPRESSOR_MIN_OFF_SECONDS,
    COMPRESSOR_ON_HZ,
)
from custom_components.quatt_stooklijn.cycling import (
    CycleTracker,
    Run,
    is_running,
)

T0 = datetime(2026, 1, 20, 6, 0, tzinfo=timezone.utc)


def _op(minuten: float) -> datetime:
    return T0 + timedelta(minutes=minuten)


def _speel(reeks: list[tuple[float, float | None]]) -> CycleTracker:
    """Speel (minuut, frequentie) af en geef de tracker terug."""
    t = CycleTracker()
    for minuut, hz in reeks:
        t.update(hz, _op(minuut))
    return t


class TestDraaidrempel:
    def test_nul_is_stil(self):
        assert not is_running(0.0)

    def test_ontbrekende_meting_is_stil(self):
        assert not is_running(None)

    def test_restwaarde_telt_niet_als_draaien(self):
        """Bij stilstand rapporteert de sensor af en toe een restwaarde."""
        assert not is_running(COMPRESSOR_ON_HZ - 0.1)

    def test_echte_frequentie_telt_wel(self):
        assert is_running(30.0)


class TestStartsTellen:
    def test_eerste_aanslag_is_een_start(self):
        t = _speel([(0, 0.0), (1, 30.0)])
        assert t.starts_in_last(1, _op(2)) == 1

    def test_blijven_draaien_telt_niet_opnieuw(self):
        t = _speel([(0, 30.0), (1, 45.0), (2, 30.0), (3, 60.0)])
        assert t.starts_in_last(1, _op(4)) == 1

    def test_stoppen_en_opnieuw_starten_telt_twee(self):
        t = _speel([(0, 30.0), (5, 0.0), (10, 30.0)])
        assert t.starts_in_last(1, _op(11)) == 2

    def test_stilstand_alleen_telt_niets(self):
        assert _speel([(0, 0.0), (5, 0.0), (10, 0.0)]).starts_in_last(1, _op(11)) == 0

    def test_ontbrekende_metingen_tellen_niet_als_start(self):
        t = _speel([(0, 30.0), (1, None), (2, None), (3, 30.0)])
        # Het gat is korter dan de stopdrempel, dus dit is één doorlopende beurt.
        assert t.starts_in_last(1, _op(4)) == 1


class TestRuisfilter:
    """De kern: één haperende sensor mag geen tien starts worden."""

    def test_korte_onderbreking_is_geen_nieuwe_start(self):
        kort = COMPRESSOR_MIN_OFF_SECONDS / 60.0 / 2
        t = _speel([(0, 30.0), (5, 0.0), (5 + kort, 30.0)])
        assert t.starts_in_last(1, _op(10)) == 1

    def test_echte_stop_is_wel_een_nieuwe_start(self):
        lang = COMPRESSOR_MIN_OFF_SECONDS / 60.0 * 2
        t = _speel([(0, 30.0), (5, 0.0), (5 + lang, 30.0)])
        assert t.starts_in_last(1, _op(10)) == 2

    def test_hikkende_sensor_blijft_een_beurt(self):
        """Tien korte hikken tijdens één doorlopende draaibeurt."""
        reeks: list[tuple[float, float | None]] = [(0, 30.0)]
        for i in range(1, 11):
            reeks += [(i * 2, 0.0), (i * 2 + 0.2, 30.0)]
        t = _speel(reeks)
        assert t.starts_in_last(1, _op(30)) == 1

    def test_hervatten_maakt_de_beurt_weer_open(self):
        kort = COMPRESSOR_MIN_OFF_SECONDS / 60.0 / 2
        t = _speel([(0, 30.0), (5, 0.0), (5 + kort, 30.0)])
        assert t.running, "na hervatten hoort de beurt weer te lopen"


class TestVenster:
    def test_alleen_starts_binnen_het_venster_tellen(self):
        t = _speel([(0, 30.0), (10, 0.0), (200, 30.0)])
        # 200 minuten later: de eerste start valt buiten het uur.
        assert t.starts_in_last(1, _op(201)) == 1
        assert t.starts_in_last(24, _op(201)) == 2

    def test_venster_schuift_mee(self):
        """Zonder meebewegend venster blijft de state hangen op het oude getal."""
        t = _speel([(0, 30.0), (5, 0.0)])
        assert t.starts_in_last(1, _op(30)) == 1
        assert t.starts_in_last(1, _op(120)) == 0


class TestLooptijd:
    def test_gemiddelde_looptijd(self):
        t = _speel([(0, 30.0), (10, 0.0), (60, 30.0), (80, 0.0)])
        assert t.average_runtime_minutes(24, _op(90)) == 15.0

    def test_lopende_beurt_telt_niet_mee(self):
        """Anders zakt het gemiddelde juist wanneer de pomp netjes lang draait."""
        t = _speel([(0, 30.0), (30, 0.0), (60, 30.0)])
        assert t.average_runtime_minutes(24, _op(61)) == 30.0

    def test_zonder_afgeronde_beurt_geen_getal(self):
        t = _speel([(0, 30.0)])
        assert t.average_runtime_minutes(24, _op(10)) is None


class TestStartsPerDag:
    def test_te_weinig_historie_geeft_geen_getal(self):
        """Een halve dag zou anders lezen als een halvering van het aantal."""
        t = _speel([(0, 30.0), (10, 0.0)])
        assert t.starts_per_day(7, _op(60)) is None

    def test_met_genoeg_historie_wel(self):
        t = CycleTracker()
        for dag in range(8):
            for n in range(3):
                basis = dag * 1440 + n * 120
                t.update(30.0, _op(basis))
                t.update(0.0, _op(basis + 20))
        nu = _op(8 * 1440)
        assert t.starts_per_day(7, nu) == 3.0


class TestBewaren:
    def test_heen_en_terug_blijft_gelijk(self):
        t = _speel([(0, 30.0), (10, 0.0), (60, 30.0)])
        terug = CycleTracker.from_list(t.to_list())
        assert len(terug.runs) == len(t.runs)
        assert terug.runs[0].start == t.runs[0].start
        assert terug.runs[0].stop == t.runs[0].stop
        assert terug.running, "de lopende beurt hoort te overleven"

    def test_onleesbare_regel_wist_de_rest_niet(self):
        """Eén corrupte regel mag geen seizoen aan geschiedenis kosten."""
        goed = Run(start=T0, stop=_op(20)).as_dict()
        terug = CycleTracker.from_list([goed, {"start": "geen datum"}, {}])
        assert len(terug.runs) == 1

    def test_lege_opslag(self):
        assert CycleTracker.from_list(None).runs == []

    def test_oude_beurten_worden_opgeruimd(self):
        from custom_components.quatt_stooklijn.const import COMPRESSOR_KEEP_DAYS

        t = CycleTracker()
        t.update(30.0, T0)
        t.update(0.0, _op(10))
        t.prune(T0 + timedelta(days=COMPRESSOR_KEEP_DAYS + 1))
        assert t.runs == []

    def test_verse_beurten_blijven_staan(self):
        t = _speel([(0, 30.0), (10, 0.0)])
        t.prune(_op(60))
        assert len(t.runs) == 1


class TestSensorBedrading:
    def test_entity_id_wordt_vastgepind(self):
        """Zonder pinning wordt het sensor.<gebied>_quatt_warmteanalyse_… en
        breekt elke dashboardverwijzing — v0.8.8 en v0.8.11."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCompressorStartsSensor,
        )

        src = inspect.getsource(QuattCompressorStartsSensor.__init__)
        assert "async_generate_entity_id" in src
        assert "compressorstarts" in src

    def test_de_rol_is_overal_bekend(self):
        """Rol, spiegel en config-sleutel horen bij elkaar; ontbreekt er één,
        dan is de bron niet te kiezen of niet te zien."""
        from custom_components.quatt_stooklijn.discovery import (
            OPENQUATT_NAMES,
            QUATT_KEYS,
            ROLE_COMPRESSOR,
        )
        from custom_components.quatt_stooklijn.sources import (
            MIRROR_SPECS,
            ROLE_CONF_KEYS,
        )

        assert ROLE_COMPRESSOR in QUATT_KEYS
        assert ROLE_COMPRESSOR in OPENQUATT_NAMES
        assert ROLE_COMPRESSOR in ROLE_CONF_KEYS
        assert any(m.role == ROLE_COMPRESSOR for m in MIRROR_SPECS)

    def test_hp2_wordt_bewaakt_en_niet_alleen_uitgelezen(self):
        """De listener moet hp2 dekken, niet alleen hp1.

        Dit is de fout van 2026-08-29. De registry kende alleen de rollen met een
        spiegel, en compressor_2 heeft er bewust geen. candidate_entities() haalt
        de te volgen entiteiten uit diezelfde registry, dus hp2 belandde nooit in
        de state-change listener. Uitlezen ging nog wel — async_source_entity()
        valt terug op de losse resolver — waardoor de sensor een bron toonde en
        er niets kapot leek. Alleen kwam elke hp2-overgang pas binnen op de tick
        van COMPRESSOR_REFRESH_INTERVAL: looptijden tot vijf minuten te kort,
        starts te laat gestempeld, en korte beurten volledig gemist.

        Gemeten geval: hp2 sloeg aan om 14:25:54, de sensor merkte het om
        14:29:47. hp1 werd in dezelfde installatie binnen 9 ms opgepikt.
        """
        from unittest.mock import MagicMock

        from homeassistant.helpers import entity_registry as er

        from custom_components.quatt_stooklijn.const import DOMAIN
        from custom_components.quatt_stooklijn.discovery import (
            ROLE_COMPRESSOR,
            ROLE_COMPRESSOR_2,
        )
        from custom_components.quatt_stooklijn.sensor import candidate_entities
        from custom_components.quatt_stooklijn.sources import SourceRegistry

        hp1 = "sensor.heatpump_1_compressor_frequency"
        hp2 = "sensor.heatpump_2_compressor_frequency"
        entries = [
            er.RegistryEntry(
                entity_id=hp1,
                unique_id="CIC-abc123:heatpump_1:heatPumps.0.compressorFrequency",
                platform="quatt",
            ),
            er.RegistryEntry(
                entity_id=hp2,
                unique_id="CIC-abc123:heatpump_2:heatPumps.1.compressorFrequency",
                platform="quatt",
            ),
        ]

        class _State:
            def __init__(self, state):
                self.state = state

        standen = {hp1: "0", hp2: "30"}
        hass = MagicMock()
        hass._test_entity_registry = er.FakeRegistry(entries)
        hass.states.get = lambda eid: (
            _State(standen[eid]) if eid in standen else None
        )

        entry_id = "entry-1"
        registry = SourceRegistry(hass, {})
        registry.async_evaluate()
        hass.data = {DOMAIN: {f"{entry_id}_sources": registry}}

        gevolgd = candidate_entities(
            hass, entry_id, (ROLE_COMPRESSOR, ROLE_COMPRESSOR_2)
        )

        assert hp1 in gevolgd
        assert hp2 in gevolgd, "hp2 wordt niet bewaakt; alleen de tick ziet hem"


class TestMeettijd:
    """Beurtgrenzen horen op het moment van de meting, niet van het verwerken.

    Een melding komt niet altijd meteen binnen: na een herstart, of wanneer een
    bron alleen op de periodieke tick wordt gelezen. Wie dan op de klok van het
    verwerken stempelt, kort de beurt in met precies die vertraging — en dat is
    de fout die op 2026-08-29 een looptijd van 16,0 minuten als 12,1 boekte.
    """

    def test_late_stop_telt_vanaf_de_meting(self):
        t = CycleTracker()
        t.update(30.0, _op(0))
        # De stop gold op minuut 16, maar wij zien hem pas op minuut 20.
        t.update(0.0, _op(16))
        assert t.runs[-1].minutes == pytest.approx(16.0)

    def test_late_start_telt_vanaf_de_meting(self):
        t = CycleTracker()
        t.update(30.0, _op(4))
        t.update(0.0, _op(20))
        assert t.runs[-1].start == _op(4)
        assert t.runs[-1].minutes == pytest.approx(16.0)

    def test_meting_uit_het_verleden_keert_de_volgorde_niet_om(self):
        """Een last_changed ouder dan wat al vastligt mag niets breken.

        Na een herstart of een bronwissel kan de meettijd achterlopen op de
        laatst geboekte grens. Een beurt die eindigt vóór ze begon is geen
        meting maar een boekhoudfout, dus de grens wordt meegeschoven.
        """
        t = CycleTracker()
        t.update(30.0, _op(10))
        t.update(0.0, _op(5))
        assert t.runs[-1].minutes is not None
        assert t.runs[-1].minutes >= 0

    def test_herhaalde_meting_met_dezelfde_tijd_verandert_niets(self):
        """Blijft de bron op 30 Hz staan, dan blijft last_changed stilstaan.

        Elke tick levert dan dezelfde tijd aan; dat mag geen tweede start geven
        en de lopende beurt niet afsluiten.
        """
        t = CycleTracker()
        t.update(30.0, _op(0))
        for _ in range(5):
            t.update(30.0, _op(0))
        assert t.starts_in_last(24, _op(30)) == 1
        assert t.running


class TestSensorMeetTijd:
    def test_meting_van_geeft_de_last_changed_mee(self):
        """De sensor moet de tijd van de bron doorgeven, niet de eigen klok."""
        from unittest.mock import MagicMock

        from custom_components.quatt_stooklijn.sensor import (
            QuattCompressorStartsSensor,
        )

        class _State:
            state = "30.0"
            last_changed = T0

        stub = MagicMock()
        stub._source_for.return_value = "sensor.hp"
        stub.hass.states.get.return_value = _State()

        waarde, gemeten_op = QuattCompressorStartsSensor._meting_van(stub, "compressor")
        assert waarde == 30.0
        assert gemeten_op == T0

    @pytest.mark.parametrize("stand", ["unavailable", "unknown", ""])
    def test_onbruikbare_stand_geeft_geen_waarde_en_geen_tijd(self, stand):
        from unittest.mock import MagicMock

        from custom_components.quatt_stooklijn.sensor import (
            QuattCompressorStartsSensor,
        )

        class _State:
            last_changed = T0

            def __init__(self, s):
                self.state = s

        stub = MagicMock()
        stub._source_for.return_value = "sensor.hp"
        stub.hass.states.get.return_value = _State(stand)

        assert QuattCompressorStartsSensor._meting_van(stub, "compressor") == (
            None,
            None,
        )


class TestOnbekendeMeting:
    """`unavailable` is geen stilstand.

    Dit onderscheid is het halve bestaansrecht van de sensor. Zonder dit telt
    elke haperende verbinding als een reeks starts, en lijkt iedere installatie
    met een wankele bron te pendelen — precies het beeld dat de sensor zou
    moeten weerleggen.
    """

    def test_onbekend_sluit_een_lopende_beurt_niet_af(self):
        t = _speel([(0, 30.0), (5, None)])
        assert t.running

    def test_lange_uitval_telt_niet_als_nieuwe_start(self):
        t = _speel([(0, 30.0), (5, None), (120, None), (121, 30.0)])
        assert t.starts_in_last(24, _op(122)) == 1

    def test_onbekend_bij_stilstand_start_niets(self):
        t = _speel([(0, 0.0), (5, None), (10, None)])
        assert t.starts_in_last(24, _op(11)) == 0
        assert not t.running

    def test_na_uitval_telt_een_echte_stop_gewoon_weer(self):
        t = _speel([(0, 30.0), (5, None), (10, 30.0), (20, 0.0), (30, 30.0)])
        assert t.starts_in_last(24, _op(31)) == 2


class TestTweeUnits:
    """Een duo wisselt de warmtepompen slim om en om af.

    Dit is de correctie op de eerste opzet, die hp1 als leidende unit volgde met
    hp2 als terugval. Dat is verkeerd: ze starten en stoppen onafhankelijk, dus
    hp1 volgen telt ruwweg de helft — en een installatie die om beurten
    kortcyclet ziet er dan uit als een rustig draaiende. Precies andersom dus
    dan de sensor bedoeld is.
    """

    @staticmethod
    def _afwisselend(beurten: int, duur: float = 20.0):
        """hp1, hp2, hp1, hp2 … elk met een eigen aan/uit-reeks."""
        hp1, hp2 = CycleTracker(), CycleTracker()
        for i in range(beurten):
            t = hp1 if i % 2 == 0 else hp2
            t.update(30.0, _op(i * duur))
            t.update(0.0, _op(i * duur + duur * 0.8))
        return hp1, hp2

    def test_alleen_hp1_volgen_telt_ongeveer_de_helft(self):
        hp1, hp2 = self._afwisselend(8)
        nu = _op(8 * 20)
        alleen_hp1 = hp1.starts_in_last(24, nu)
        samen = alleen_hp1 + hp2.starts_in_last(24, nu)
        assert alleen_hp1 == 4
        assert samen == 8, "beide units horen mee te tellen"

    def test_gelijktijdig_draaien_telt_twee_starts(self):
        """Bij hoge vraag draaien ze samen; dat zijn twee aparte starts."""
        hp1, hp2 = CycleTracker(), CycleTracker()
        hp1.update(30.0, T0)
        hp2.update(30.0, _op(1))
        nu = _op(5)
        assert hp1.starts_in_last(1, nu) + hp2.starts_in_last(1, nu) == 2

    def test_overname_is_geen_voortzetting(self):
        """hp1 stopt, hp2 neemt over — voor het huis doorlopend, per unit twee
        beurten. Het aantal starts hoort dat te laten zien."""
        hp1, hp2 = CycleTracker(), CycleTracker()
        hp1.update(30.0, T0)
        hp1.update(0.0, _op(30))
        hp2.update(30.0, _op(30))
        nu = _op(60)
        assert hp1.starts_in_last(24, nu) + hp2.starts_in_last(24, nu) == 2

    def test_solo_installatie_telt_gewoon_door(self):
        """De tweede tracker blijft leeg en mag niets bederven."""
        hp1 = _speel([(0, 30.0), (20, 0.0), (60, 30.0)])
        hp2 = CycleTracker()
        nu = _op(70)
        assert hp1.starts_in_last(24, nu) + hp2.starts_in_last(24, nu) == 2
        assert hp2.average_runtime_minutes(24, nu) is None

    def test_de_tweede_rol_is_apart_en_geen_terugval(self):
        """Zou hp2 als terugval op hp1 staan, dan telde de bronlaag hem nooit
        apart en was de helft van de starts onzichtbaar."""
        from custom_components.quatt_stooklijn.discovery import (
            OPENQUATT_NAMES,
            QUATT_KEYS,
            ROLE_COMPRESSOR,
            ROLE_COMPRESSOR_2,
        )

        assert ROLE_COMPRESSOR != ROLE_COMPRESSOR_2
        for tabel in (QUATT_KEYS, OPENQUATT_NAMES):
            een, twee = tabel[ROLE_COMPRESSOR], tabel[ROLE_COMPRESSOR_2]
            assert not set(een) & set(twee), "hp2 mag geen kandidaat van hp1 zijn"

    def test_beide_units_zijn_instelbaar(self):
        from custom_components.quatt_stooklijn.discovery import (
            ROLE_COMPRESSOR,
            ROLE_COMPRESSOR_2,
        )
        from custom_components.quatt_stooklijn.sources import ROLE_CONF_KEYS

        assert ROLE_CONF_KEYS[ROLE_COMPRESSOR] != ROLE_CONF_KEYS[ROLE_COMPRESSOR_2]

    def test_de_sensor_luistert_naar_beide(self):
        """Alleen op hp1 abonneren betekent dat starts van hp2 pas bij de
        vijfminutentik opgemerkt worden — en korte beurten dus gemist."""
        import inspect

        from custom_components.quatt_stooklijn.sensor import (
            QuattCompressorStartsSensor,
        )

        src = inspect.getsource(QuattCompressorStartsSensor.async_added_to_hass)
        assert "ROLE_COMPRESSOR_2" in src
