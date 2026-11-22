"""Controle op de aanvoertemperatuursensor.

Het aanvoeradvies rekent vermogen om naar temperatuur met de retour van de
warmtepomp: ``retour + P / (1,16 · debiet)``. Die retour en het vermogen komen
van de sensoren in de units zelf; de aanvoer waarmee het advies vergeleken wordt
(en waarop een regelaar zijn max-aanvoerplafond toetst) komt van een losse
sensor. Wijkt die af, dan lijkt elk advies even ver verschoven, en de
foutsensoren laten dat als modelfout zien.

Gevonden op 18-11-2026: de PT1000 van een niet-gekalibreerde OpenQuatt las 0,65
K boven de uitgang van HP2 als één unit draaide, 0,81 K als beide draaiden,
dag na dag binnen 0,07 K. De CiC-flowmeter van dezelfde installatie lag een
heel seizoen binnen 0,1 K. Wie OpenQuatt gebruikt kan de sensor kalibreren, maar
moet dan wel weten dat het nodig is — vandaar deze melding.

**De vergelijking.** Bij een duo staan de units in serie (de Quatt-integratie
rekent het totale ΔT zelf als hp2-uit − hp1-in). Tussen de uitgang van HP2 en
de aanvoersensor kan het water niet warmer worden, alleen iets afkoelen. Een
aanvoer die structureel boven hp2-uit ligt is dus een sensorafwijking. Op een
solo is hp1-uit de referentie.

**Alleen stabiele momenten.** Bij een start, een stop of een ontdooicyclus lopen
de twee sensoren tijdelijk uiteen omdat het water er niet tegelijk langskomt.
Een monster telt daarom pas als de compressortoestand en het stoken minstens
``STABLE_S`` ongewijzigd zijn, het debiet er is, en de gasketel niet meestookt
(die zit tussen de units en de aanvoersensor). Het oordeel valt op de mediaan
van minstens ``MIN_SAMPLES`` monsters: één verdwaalde meting verandert niets.

**Bewust geen correctie in wat stuurt.** De integratie past de aanvoer niet
aan waar ze rekent of schrijft. Een regelaar toetst het plafond dat we
schrijven aan zijn eigen aanvoersensor; als wij met een gecorrigeerde waarde
rekenen en hij niet, lopen de twee uiteen. Om dezelfde reden leert het
afgiftemodel op de ongecorrigeerde aanvoer: de stooklijnpunten gaan naar een
regelaar die met diezelfde sensor meet. Alleen de afwijkingssensoren (advies −
gemeten aanvoer) trekken de afwijking eraf zolang de melding openstaat
(``correction_for``): die tonen hoe ver het advies ernaast zit, en lieten
anders de meetfout zien als modelfout (live 22-11-2026: −1,4 °C, waarvan
0,88 K de sensor).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta
import logging
from statistics import median
from typing import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .discovery import (
    ROLE_BOILER_HEAT,
    ROLE_COMPRESSOR,
    ROLE_COMPRESSOR_2,
    ROLE_FLOW_RATE,
    ROLE_HP1_WATER_OUT,
    ROLE_HP2_WATER_OUT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    async_resolve_candidates,
)
from .helpers import get_float_state
from .sources import ROLE_CONF_KEYS, async_source_entity, is_usable

_LOGGER = logging.getLogger(__name__)

ISSUE_TRANSLATION_KEY = "supply_sensor_offset"
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.supply_check"

#: Hoe vaak de toestand gelezen wordt. Fijn genoeg om een ontdooicyclus van een
#: paar minuten te zien; die moet de stabiliteitsklok resetten.
TICK = timedelta(seconds=60)
#: Hoe lang compressortoestand en stoken ongewijzigd moeten zijn. In de meting
#: van 18-11 was het verschil na een kwartier stabiel (dag-tot-dag 0,07 K).
STABLE_S = 15 * 60
#: Minimale afstand tussen twee opgeslagen monsters.
SAMPLE_SPACING_S = 5 * 60
MIN_FLOW_LPH = 300.0
MIN_HEAT_W = 500.0
#: De ketel telt als uit tot dit vermogen; meetruis rond nul mag geen monster kosten.
MAX_BOILER_W = 50.0
#: 12 uur stabiel stoken voordat er een oordeel valt, 24 uur in het venster.
MIN_SAMPLES = 144
WINDOW_SAMPLES = 288
#: Monsters ouder dan dit tellen niet meer: in het voorjaar kan een venster van
#: 24 stookuren anders weken teruggaan.
MAX_AGE_S = 14 * 24 * 3600
#: Melden vanaf deze afwijking, intrekken pas onder de tweede. Bij 800 l/h is
#: 0,3 K ~280 W. De CiC-flowmeter die als goed geldt bleef een seizoen binnen 0,1 K.
RAISE_K = 0.3
CLEAR_K = 0.2
#: Een kalibratie verschuift de sensor in één keer. Als de laatste
#: ``STEP_RECENT`` monsters zoveel van het hele venster verschillen, tellen
#: alleen die nog, anders duurt het een dag voordat de melding verdwijnt.
STEP_RECENT = 24
STEP_K = 0.3
#: Een gat tussen twee tikken groter dan dit zet de stabiliteitsklok terug.
MAX_GAP_S = 3 * 60


@dataclass(frozen=True)
class Reading:
    """Eén momentopname van alles wat de controle nodig heeft."""

    supply: float | None
    reference: float | None
    flow: float | None
    heat_w: float | None
    boiler_w: float | None
    #: Per unit of de compressor draait; None als geen enkele frequentie bekend is.
    running: tuple[bool, ...] | None


class OffsetTracker:
    """Verzamelt aanvoer − uitgang op stabiele stookmomenten. Kent geen HA."""

    def __init__(self, samples: list[list[float]] | None = None) -> None:
        self.samples: deque[tuple[float, float]] = deque(
            ((float(t), float(v)) for t, v in samples or []),
            maxlen=WINDOW_SAMPLES,
        )
        self._signature: tuple[bool, ...] | None = None
        self._signature_since: float | None = None
        self._heating_since: float | None = None
        self._last_sample: float | None = self.samples[-1][0] if self.samples else None
        self._last_observe: float | None = None

    def observe(self, ts: float, r: Reading) -> bool:
        """Verwerk een momentopname. True als er een monster bij kwam."""
        if self._last_observe is not None and ts - self._last_observe > MAX_GAP_S:
            # Wat er in het gat gebeurde is onbekend; een ontdooicyclus kan erin
            # gevallen zijn. Opnieuw een kwartier afwachten.
            self._heating_since = None
            self._signature_since = ts
        self._last_observe = ts
        heating = (
            r.heat_w is not None
            and r.heat_w > MIN_HEAT_W
            and (r.boiler_w or 0.0) <= MAX_BOILER_W
            and r.running is not None
            and any(r.running)
        )
        if not heating:
            self._heating_since = None
        elif self._heating_since is None:
            self._heating_since = ts

        if r.running != self._signature:
            self._signature = r.running
            self._signature_since = ts

        if (
            not heating
            or r.supply is None
            or r.reference is None
            or r.flow is None
            or r.flow < MIN_FLOW_LPH
            or ts - self._heating_since < STABLE_S
            or ts - self._signature_since < STABLE_S
        ):
            return False
        if self._last_sample is not None and ts - self._last_sample < SAMPLE_SPACING_S:
            return False

        while self.samples and ts - self.samples[0][0] > MAX_AGE_S:
            self.samples.popleft()
        self.samples.append((ts, r.supply - r.reference))
        self._last_sample = ts
        self._drop_before_step()
        return True

    def _drop_before_step(self) -> None:
        # Alle recente monsters aan dezelfde kant, niet hun mediaan: anders gaat
        # dit al af zodra de helft nieuw is, en blijven er oude achter.
        if len(self.samples) <= STEP_RECENT:
            return
        recent = [v for _, v in list(self.samples)[-STEP_RECENT:]]
        ref = self.median_all()
        if max(recent) < ref - STEP_K or min(recent) > ref + STEP_K:
            kept = list(self.samples)[-STEP_RECENT:]
            self.samples.clear()
            self.samples.extend(kept)

    def median_all(self) -> float:
        return median(v for _, v in self.samples)

    def offset(self) -> float | None:
        """De afwijking, of None zolang er te weinig stabiele monsters zijn."""
        if len(self.samples) < MIN_SAMPLES:
            return None
        return self.median_all()

    def as_list(self) -> list[list[float]]:
        return [[round(t), round(v, 3)] for t, v in self.samples]


class SupplySensorCheck:
    """Leest elke minuut de toestand en meldt een afwijkende aanvoersensor."""

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._config = config
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}")
        self.tracker = OffsetTracker()
        self._pair: tuple[str | None, str | None] | None = None
        #: De afwijking waarmee de melding het laatst is aangemaakt; None = geen melding.
        self._reported: float | None = None

    def correction_for(self, entity_id: str | None) -> float | None:
        """De afwijking van ``entity_id``, zolang die gemeld wordt; anders None.

        Alleen voor weergave (de afwijkingssensoren), niet voor wat stuurt.
        """
        if not entity_id or self._pair is None or entity_id != self._pair[0]:
            return None
        if self._reported is None:
            return None
        return self.tracker.offset()

    @property
    def issue_id(self) -> str:
        return f"{ISSUE_TRANSLATION_KEY}_{self._entry_id}"

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._pair = (data.get("supply_entity"), data.get("reference_entity"))
        self.tracker = OffsetTracker(data.get("samples"))

    @callback
    def async_setup(self) -> Callable[[], None]:
        cancel = async_track_time_interval(self._hass, self._async_tick, TICK)

        @callback
        def _teardown() -> None:
            # Ook de melding weg: na een herlaad komt hij met de bewaarde
            # monsters bij de eerste tik terug, na verwijderen niet.
            from homeassistant.helpers import issue_registry as ir

            cancel()
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)

        return _teardown

    # -- lezen -------------------------------------------------------------

    def _role(self, role: str) -> str | None:
        return async_source_entity(
            self._hass, self._entry_id, role,
            config=self._config, conf_key=ROLE_CONF_KEYS.get(role),
        )

    def _reference_entity(self) -> str | None:
        """Uitgang van de laatste unit in de serie: hp2 als die meet, anders hp1."""
        for role in (ROLE_HP2_WATER_OUT, ROLE_HP1_WATER_OUT):
            for entity_id in async_resolve_candidates(self._hass, None, role):
                if is_usable(self._hass, entity_id):
                    return entity_id
        return None

    def _float(self, entity_id: str | None) -> float | None:
        return get_float_state(self._hass, entity_id) if entity_id else None

    def read(self) -> tuple[Reading, str | None, str | None]:
        supply_entity = self._role(ROLE_SUPPLY_TEMP)
        reference_entity = self._reference_entity()
        freqs = [
            self._float(self._role(role))
            for role in (ROLE_COMPRESSOR, ROLE_COMPRESSOR_2)
        ]
        known = [f for f in freqs if f is not None]
        reading = Reading(
            supply=self._float(supply_entity),
            reference=self._float(reference_entity),
            flow=self._float(self._role(ROLE_FLOW_RATE)),
            heat_w=self._float(self._role(ROLE_TOTAL_POWER)),
            boiler_w=self._float(self._role(ROLE_BOILER_HEAT)),
            running=tuple(f > 0 for f in known) if known else None,
        )
        return reading, supply_entity, reference_entity

    # -- de tik ------------------------------------------------------------

    @callback
    def _async_tick(self, _now=None) -> None:
        reading, supply_entity, reference_entity = self.read()
        pair = (supply_entity, reference_entity)
        if reading.supply is not None and reading.reference is not None and pair != self._pair:
            # Andere sensoren, ander verschil: de oude monsters zeggen niets meer.
            if self._pair is not None and any(self._pair):
                _LOGGER.info(
                    "Aanvoersensorcontrole: bron gewisseld van %s naar %s, "
                    "opnieuw begonnen", self._pair, pair,
                )
            self._pair = pair
            self.tracker = OffsetTracker()
        if self.tracker.observe(dt_util.utcnow().timestamp(), reading):
            self._store.async_delay_save(self._data_to_save, 1800)
        self._async_evaluate()

    def _data_to_save(self) -> dict:
        return {
            "supply_entity": self._pair[0] if self._pair else None,
            "reference_entity": self._pair[1] if self._pair else None,
            "samples": self.tracker.as_list(),
        }

    @callback
    def _async_evaluate(self) -> None:
        from homeassistant.helpers import issue_registry as ir

        offset = self.tracker.offset()
        if offset is None:
            return
        if abs(offset) >= RAISE_K:
            # Alleen bij openen of een merkbaar ander getal: de issue-registry
            # schrijft elke wijziging naar schijf, en dit loopt elke minuut.
            if self._reported is not None and abs(offset - self._reported) < 0.05:
                return
            hours = len(self.tracker.samples) * SAMPLE_SPACING_S / 3600
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TRANSLATION_KEY,
                translation_placeholders={
                    "supply_entity": self._pair[0] or "?",
                    "reference_entity": self._pair[1] or "?",
                    "offset": f"{offset:+.2f}",
                    "hours": f"{hours:.0f}",
                    "clear": f"{CLEAR_K:.1f}",
                },
            )
            if self._reported is None:
                _LOGGER.warning(
                    "Aanvoersensor %s wijkt %+.2f K af van %s",
                    self._pair[0], offset, self._pair[1],
                )
            self._reported = offset
        elif abs(offset) < CLEAR_K:
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
            self._reported = None
