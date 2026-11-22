"""Hoge-resolutie meetlogboek voor latere modelidentificatie.

Het online model leert op uurbasis. Voor wat het nu doet — de stationaire
warmtevraag en een vooruitblik van twaalf uur — is dat ruim genoeg.

Wat er op uurmonsters *niet* uit te halen is, is de snelle tijdconstante van
het huis. De uitwisseling tussen binnenlucht en bouwmassa speelt zich af op
grofweg 2 tot 6 uur, en bij vloerverwarming komt de dekvloer daar als apart
lichaam nog tussen. Een tijdconstante van twee uur schatten uit uurmonsters
zit tegen de bemonsteringsgrens aan: je ziet dát er iets snels is, maar niet
hoe snel.

HA's eigen 5-minuutstatistieken zijn geen uitweg. Die worden na ongeveer tien
dagen opgeruimd; alleen het uurgemiddelde blijft staan. Wie in het voorjaar
naar januari wil kijken, vindt daar dus niets fijners meer. Vandaar dat dit
logboek zelf een ringbuffer bijhoudt, met een venster dat een heel
stookseizoen overspant.

Het logboek voedt het model niet — het wordt alleen geschreven. Wat je ermee
doet is een aparte analyse, offline of in een latere modelversie.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta
import logging
import math

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: 2 sinds v0.10.7-beta.9: compacte opslag met extra kolommen (zie
#: ``HighResLog.to_storage``). Een oudere versie van de integratie kan versie 2
#: niet lezen; dan blijft het logboek staan en schrijft die versie niets weg.
STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.highres_log"

#: De vaste kolommen. Hun plek ligt vast: analyses lezen op index
#: (``model_benchmark._TS`` e.v., ``solar_input._PV``).
BASE_COLUMNS = ("ts", "t_indoor", "t_outdoor", "q_hp_w", "q_solar_wm2", "pv_w")

#: Sinds v0.10.7-beta.9: wat er nodig is voor een afgiftemodel (aanvoer, retour,
#: debiet), een COP op buiten- én aanvoertemperatuur (opgenomen vermogen), en
#: om terug te zien wat de knoppen deden. ``max_water`` en ``sound`` zijn de
#: stand van de knop als getal (``analysis.steering_effect.KNOB_CODES``),
#: ``ceiling`` de waarde die op de regelaar stond, ``demand`` 1 als de
#: warmtevraag naar OpenQuatt stuurde. Onbekend is NaN, op schijf leeg.
EXTRA_COLUMNS = (
    "t_supply", "t_return", "flow_lph", "p_el_w", "setpoint",
    "ceiling", "max_water", "sound", "demand",
)
COLUMNS = BASE_COLUMNS + EXTRA_COLUMNS

#: Afronding per extra kolom: genoeg voor de analyse, niet meer.
_EXTRA_DIGITS = {
    "t_supply": 2, "t_return": 2, "flow_lph": 0, "p_el_w": 0, "setpoint": 2,
    "ceiling": 1, "max_water": 0, "sound": 0, "demand": 0,
}

#: Bemonsteringsinterval. Fijn genoeg voor een tijdconstante van een uur,
#: grof genoeg om het logboek binnen een paar honderd kilobyte te houden.
SAMPLE_INTERVAL = timedelta(minutes=5)

#: 90 dagen bij vijf minuten. Ruim een stookseizoen, zodat een analyse in
#: maart de koudste weken nog terugvindt.
MAX_SAMPLES = 25_920
#: In de compacte opslag staat het tijdstip om de zoveel regels voluit (een dag).
ANCHOR_EVERY = 288
#: Een eerste veld vanaf hier is een tijdstip, geen verschil (2001-09-09).
ABSOLUTE_TS_MIN = 1_000_000_000

#: Monsters dichter op elkaar dan dit worden overgeslagen. Zonder deze rem
#: zou een herstart-storm of een handmatige aanroep het venster verkorten.
MIN_SPACING = timedelta(minutes=4)

#: Uiterlijk zo lang na het eerste nieuwe monster wordt er weggeschreven, ook
#: als de uurlijkse opslag uitblijft. Bij het afsluiten van HA meteen.
SAVE_DELAY = timedelta(hours=1)


class HighResLog:
    """Ringbuffer met meetmonsters op vaste, fijne tijdstappen."""

    def __init__(self, max_samples: int = MAX_SAMPLES) -> None:
        self._samples: deque[list[float]] = deque(maxlen=max_samples)

    def record(
        self,
        timestamp: datetime,
        t_indoor: float,
        t_outdoor: float,
        q_hp_w: float,
        q_solar_wm2: float,
        pv_w: float | None = None,
        extra: Mapping[str, float | None] | None = None,
    ) -> bool:
        """Leg één monster vast. Geeft False als het is overgeslagen.

        ``pv_w`` is de opbrengst van de zonnesensor (PV-vermogen), als die is
        ingesteld. Onbekend wordt NaN in het geheugen en ``null`` op schijf. De
        zon gemeten met PV en omgerekend naar de ramen bleek de beste zoninvoer
        voor het 2R2C; zie ZONNEWARMTE_PLAN.md.

        ``extra``: waarden voor ``EXTRA_COLUMNS``. Wat ontbreekt of geen eindig
        getal is, wordt NaN; een ontbrekende aanvoer houdt het monster niet
        tegen.
        """
        # Een NaN in de warmtekolom komt via mean_heat_between in het 1R1C.
        # Alleen pv_w mag ontbreken; de rest moet een getal zijn.
        if not all(
            math.isfinite(float(v))
            for v in (t_indoor, t_outdoor, q_hp_w, q_solar_wm2)
        ):
            return False
        ts = int(timestamp.timestamp())
        if self._samples:
            gap = ts - self._samples[-1][0]
            # Een negatieve sprong is geen te dicht monster maar een klok die
            # terugloopt (NTP-correctie na een herstart zonder RTC). Die wél
            # accepteren: anders ligt het logboek stil tot de echte tijd het
            # oude laatste tijdstip heeft ingehaald.
            if 0 <= gap < MIN_SPACING.total_seconds():
                return False
        self._samples.append([
            ts,
            round(float(t_indoor), 2),
            round(float(t_outdoor), 2),
            round(float(q_hp_w)),
            round(float(q_solar_wm2), 1),
            round(float(pv_w)) if pv_w is not None and math.isfinite(pv_w) else math.nan,
            *(_rounded((extra or {}).get(c), _EXTRA_DIGITS[c]) for c in EXTRA_COLUMNS),
        ])
        return True

    def __len__(self) -> int:
        return len(self._samples)

    def mean_heat_between(self, since: datetime, until: datetime) -> tuple[float | None, int]:
        """Gemiddelde warmte (kolom ``q_hp``) van de monsters in (since, until].

        Voor het huismodel, dat per uur leert: het uurgemiddelde vangt de
        korte stoten van een bijspringende ketel, een momentopname niet. Van
        achteren af gelezen, zodat het een vol logboek niet helemaal doorloopt.
        Geeft ``(gemiddelde, aantal)``; ``(None, 0)`` zonder monsters.
        """
        t0, t1 = since.timestamp(), until.timestamp()
        total, n = 0.0, 0
        for row in reversed(self._samples):
            ts = row[0]
            if ts > t1:
                continue
            if ts <= t0:
                break
            total += row[3]
            n += 1
        return (total / n if n else None), n

    @property
    def span_hours(self) -> float | None:
        """Hoeveel uur er tussen het oudste en nieuwste monster zit.

        Op min en max, niet op eerste en laatste: na een teruglopende klok
        staat het logboek niet meer op tijdsvolgorde.
        """
        if len(self._samples) < 2:
            return None
        ts = [row[0] for row in self._samples]
        return (max(ts) - min(ts)) / 3600.0

    def rows(self) -> list[list[float | None]]:
        """Alle monsters als rijen in de volgorde van ``COLUMNS``, onbekend als
        None. Voor analyse; ``np.asarray(rows, dtype=float)`` maakt er weer NaN
        van."""
        return [[None if _missing(v) else v for v in row] for row in self._samples]

    def to_dict(self) -> dict:
        """Kolommen en rijen, zoals een analyse ze leest. Niet wat er op schijf
        staat; zie ``to_storage``."""
        return {"columns": list(COLUMNS), "samples": self.rows()}

    def to_storage(self) -> dict:
        """Compact voor de Store: één tekstblok, een regel per monster.

        Als lijst van lijsten schreef HA elk getal op een eigen regel: 2,2 MB
        voor 74 dagen met zes kolommen (~105 tekens per monster). Zo is het
        ~70 tekens per monster met vijftien kolommen. Het tijdstip staat als
        verschil met het vorige monster (meestal 300), onbekend is een leeg veld.
        Elke ``ANCHOR_EVERY`` regels staat het tijdstip voluit: een onleesbare
        regel kost dan hooguit tot het volgende anker, in plaats van alle
        latere tijdstippen te verschuiven.
        """
        return _compact(self._samples)

    @classmethod
    def from_dict(cls, data: dict, max_samples: int = MAX_SAMPLES) -> HighResLog:
        """Leest de compacte opslag (``rows``) en de oude lijst (``samples``,
        vijf, zes of alle kolommen)."""
        log = cls(max_samples=max_samples)
        if isinstance(data.get("rows"), str):
            cols = data.get("columns") or list(COLUMNS)
            prev = None
            for line in data["rows"].split("\n"):
                parts = line.split(",")
                # Een half geschreven of met de hand aangepast bestand mag het
                # logboek niet slopen: onbruikbare regels overslaan. Is het
                # tijdstip zelf onleesbaar, dan is het volgende verschil nergens
                # meer aan vast te knopen: tot het volgende anker overslaan.
                try:
                    step = int(parts[0])
                except (TypeError, ValueError, IndexError):
                    prev = None
                    continue
                if step >= ABSOLUTE_TS_MIN:
                    ts = step
                elif prev is None:
                    continue
                else:
                    ts = prev + step
                prev = ts
                try:
                    values = [float(p) if p != "" else math.nan for p in parts[1:]]
                except (TypeError, ValueError):
                    continue
                row = _by_name(cols[1:], values)
                if row is not None:
                    log._samples.append([ts, *row])
            return log
        for row in data.get("samples", []):
            # Vijf kolommen: van vóór de zonnesensor (v0.10.6-beta.12); zes: tot
            # v0.10.7-beta.8. Die rijen krijgen onbekende waarden, ze vallen niet weg.
            if not isinstance(row, list) or len(row) < 5:
                continue
            try:
                values = [math.nan if v is None else float(v) for v in row[1:]]
                log._samples.append([int(row[0]), *_by_name(COLUMNS[1:len(row)], values)])
            except (TypeError, ValueError):
                continue
        return log


def _compact(samples) -> dict:
    """Zie ``HighResLog.to_storage``; los, zodat het in de executor kan."""
    lines = []
    prev = None
    for i, row in enumerate(samples):
        ts = int(row[0])
        head = str(ts if prev is None or i % ANCHOR_EVERY == 0 else ts - prev)
        prev = ts
        lines.append(",".join([head, *(_fmt(v) for v in row[1:])]))
    return {"format": 2, "columns": list(COLUMNS), "rows": "\n".join(lines)}


def _missing(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _rounded(v, digits: int) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    if not math.isfinite(f):
        return math.nan
    return float(round(f)) if digits == 0 else round(f, digits)


def _fmt(v) -> str:
    if _missing(v):
        return ""
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _by_name(names, values) -> list[float] | None:
    """Waarden per kolomnaam op de plek in ``COLUMNS`` zetten; onbekende
    kolommen vallen weg, ontbrekende worden NaN. None als een vaste kolom
    (behalve pv_w) ontbreekt."""
    by = dict(zip(names, values))
    row = [by.get(c, math.nan) for c in COLUMNS[1:]]
    if any(_missing(v) for v in row[:4]):
        return None
    return row


class _Store(Store):
    """Versie 1 (lijst van lijsten) leest ``from_dict`` zelf; niets om te doen."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict
    ) -> dict:
        if old_major_version > STORAGE_VERSION:
            raise NotImplementedError(
                f"meetlogboek is van een nieuwere versie ({old_major_version})"
            )
        return old_data


class HighResLogStore:
    """Persist HighResLog across HA restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = _Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.log = HighResLog()
        self._dirty = False
        #: Opslaan blijft geblokkeerd tot er één keer met succes is gelezen.
        #: Anders overschrijft de eerstvolgende opslag na een mislukte lezing
        #: een heel stookseizoen aan monsters met een leeg logboek — data die
        #: per definitie niet opnieuw te verzamelen is.
        self._loaded = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            try:
                # In de executor: een vol logboek inlezen kost ~120 ms.
                self.log = await self._hass.async_add_executor_job(
                    HighResLog.from_dict, data)
                _LOGGER.info(
                    "Meetlogboek geladen: %d monsters, %s uur",
                    len(self.log),
                    round(self.log.span_hours) if self.log.span_hours else "?",
                )
            except Exception:
                _LOGGER.warning(
                    "Meetlogboek onleesbaar; er wordt niets weggeschreven tot "
                    "het bestand hersteld of verwijderd is", exc_info=True
                )
                self.log = HighResLog()
                return
        self._loaded = True

    def record(self, *args, **kwargs) -> bool:
        recorded = self.log.record(*args, **kwargs)
        if recorded and self._loaded and not self._dirty:
            # Het eerste nieuwe monster na een opslag zet een uitgestelde
            # opslag klaar. Die doet twee dingen: hij schrijft weg als de
            # uurlijkse opslag uitblijft, en HA schrijft hem bij het afsluiten
            # alsnog weg (final write). Zonder dat laatste kostte elke herstart
            # alle monsters sinds de vorige opslag, tot een uur. Bij een
            # herstart per dag lag het logboek daardoor in stukken van een dag,
            # en geen enkel stuk haalde de drie dagen die de modelvergelijking
            # eist (gemeten 15-10-2026: 166 bruikbare trainingsuren van 858).
            #
            # Alleen bij het eerste monster: elke volgende aanroep zou de
            # timer opschuiven, en dan kwam hij nooit aan de beurt.
            self._store.async_delay_save(self._snapshot, SAVE_DELAY.total_seconds())
        self._dirty = self._dirty or recorded
        return recorded

    def _snapshot(self) -> dict:
        """Wat de uitgestelde opslag wegschrijft, bepaald op het moment zelf."""
        self._dirty = False
        return self.log.to_storage()

    async def async_save(self) -> None:
        """Wegschrijven, maar alleen als er iets bij is gekomen.

        Bewust niet bij elk monster: een vol logboek is ruim een megabyte en
        veel installaties draaien op een SD-kaart.
        """
        if not self._dirty or not self._loaded:
            return
        # Het opmaken (~150 ms bij een vol logboek) in de executor, op een
        # kopie: er kunnen intussen monsters bijkomen, en die blijven dan
        # "nog niet opgeslagen".
        rows = list(self.log._samples)
        self._dirty = False
        try:
            data = await self._hass.async_add_executor_job(_compact, rows)
            await self._store.async_save(data)
        except BaseException:
            self._dirty = True
            raise
