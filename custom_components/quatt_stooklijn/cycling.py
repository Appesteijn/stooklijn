"""Compressorstarts tellen — de maat voor kortcyclen.

Waarom dit een eigen module is en geen teller-helper in Home Assistant: de
diagnose die gebruikers zoeken is niet "hoeveel starts vandaag" maar "veel
starts terwijl het buiten niet warm is". Dat vergt twee dingen die een gewone
teller niet levert.

Ten eerste een *voortschrijdend* venster. Een teller die om middernacht op nul
gaat zegt om half één niets, terwijl juist de nacht — lage vraag, hoge
aanvoertemperatuur — de periode is waarin het kortcyclen begint.

Ten tweede geschiedenis. De recorder gooit ruwe states na tien dagen weg, en de
vraag of een ingreep aan de stooklijn geholpen heeft, beantwoord je door twee
koudeperioden te vergelijken die maanden uit elkaar liggen. Daarom bewaart deze
integratie de starts zelf, net als de knie-datastore.

Wat er wordt geteld is de overgang van stilstand naar draaien op de
compressorfrequentie. Twee drempels houden ruis eruit:

* ``COMPRESSOR_ON_HZ`` — bij stilstand rapporteert de sensor af en toe een
  restwaarde; zonder marge telt die als start.
* ``COMPRESSOR_MIN_OFF_SECONDS`` — een stop van een paar seconden is geen stop
  maar een gemiste update. Zonder deze drempel maakt één haperende sensor van
  één start er tien.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import (
    COMPRESSOR_KEEP_DAYS,
    COMPRESSOR_MIN_OFF_SECONDS,
    COMPRESSOR_ON_HZ,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Run:
    """Eén draaibeurt: wanneer de compressor aansloeg en wanneer hij stopte."""

    start: datetime
    stop: datetime | None = None

    @property
    def minutes(self) -> float | None:
        if self.stop is None:
            return None
        return (self.stop - self.start).total_seconds() / 60.0

    def as_dict(self) -> dict[str, str]:
        d = {"start": self.start.isoformat()}
        if self.stop is not None:
            d["stop"] = self.stop.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Run:
        return cls(
            start=datetime.fromisoformat(d["start"]),
            stop=datetime.fromisoformat(d["stop"]) if d.get("stop") else None,
        )


def is_running(value: float | None) -> bool:
    """Draait de compressor bij deze frequentie?"""
    return value is not None and value >= COMPRESSOR_ON_HZ


class CycleTracker:
    """Houdt draaibeurten bij op basis van opeenvolgende frequentiemetingen.

    Bewust een gewone klasse zonder Home Assistant erin: het gedrag dat ertoe
    doet — wanneer telt iets als start — hoort testbaar te zijn zonder
    draaiende HA en zonder de klok.
    """

    def __init__(self, runs: list[Run] | None = None) -> None:
        self.runs: list[Run] = runs or []

    # -- opbouwen ----------------------------------------------------------

    @property
    def _open(self) -> Run | None:
        """De draaibeurt die nog bezig is, als die er is."""
        if self.runs and self.runs[-1].stop is None:
            return self.runs[-1]
        return None

    @property
    def _laatste_grens(self) -> datetime | None:
        """Het jongste tijdstip dat al vastligt: een stop, anders een start."""
        if not self.runs:
            return None
        laatste = self.runs[-1]
        return laatste.stop if laatste.stop is not None else laatste.start

    def update(self, value: float | None, now: datetime) -> bool:
        """Verwerk één meting op het moment waarop zij gold.

        ``now`` is het tijdstip van de *meting*, niet van het verwerken. De
        aanroeper geeft de ``last_changed`` van de bron mee, zodat een beurt
        blijft kloppen als de melding laat binnenkomt — na een herstart, of via
        de periodieke tick in plaats van een state-change. Op de waarnemingstijd
        stempelen maakte beurten korter dan ze waren.

        ``None`` betekent "we weten het niet" — de bron staat op *unavailable*
        of *unknown* — en niet "de compressor staat uit". Dat verschil is het
        halve bestaansrecht van deze sensor: wie de twee gelijkstelt, telt elke
        haperende verbinding als een reeks starts, en dan lijkt iedere
        installatie met een wankele bron te pendelen. Bij een onbekende meting
        verandert er dus niets aan de toestand.
        """
        if value is None:
            return False

        # Een meettijd uit het verleden mag de volgorde niet omkeren: bij een
        # herstart of een bronwissel kan een last_changed ouder zijn dan wat er
        # al vastligt, en een beurt die eindigt vóór ze begon is geen meting
        # maar een boekhoudfout.
        grens = self._laatste_grens
        if grens is not None and now < grens:
            now = grens

        running = is_running(value)
        open_run = self._open

        if running and open_run is None:
            # Een korte onderbreking is een meethiaat, geen echte stop: dan
            # wordt de vorige beurt hervat in plaats van een start geteld.
            if self.runs and self.runs[-1].stop is not None:
                pauze = (now - self.runs[-1].stop).total_seconds()
                if pauze < COMPRESSOR_MIN_OFF_SECONDS:
                    self.runs[-1].stop = None
                    return False
            self.runs.append(Run(start=now))
            return True

        if not running and open_run is not None:
            open_run.stop = now

        return False

    def prune(self, now: datetime) -> None:
        """Gooi beurten weg die buiten de bewaartermijn vallen."""
        grens = now - timedelta(days=COMPRESSOR_KEEP_DAYS)
        self.runs = [r for r in self.runs if r.start >= grens]

    # -- uitlezen ----------------------------------------------------------

    def starts_since(self, since: datetime) -> int:
        return sum(1 for r in self.runs if r.start >= since)

    def starts_in_last(self, hours: float, now: datetime) -> int:
        return self.starts_since(now - timedelta(hours=hours))

    def average_runtime_minutes(self, hours: float, now: datetime) -> float | None:
        """Gemiddelde looptijd van de afgeronde beurten in het venster.

        De lopende beurt telt niet mee: die is per definitie nog te kort en zou
        het gemiddelde omlaag trekken juist wanneer de pomp netjes lang draait.
        """
        grens = now - timedelta(hours=hours)
        duren = [
            r.minutes for r in self.runs if r.start >= grens and r.minutes is not None
        ]
        if not duren:
            return None
        return round(sum(duren) / len(duren), 1)

    def starts_per_day(self, days: int, now: datetime) -> float | None:
        """Gemiddeld aantal starts per etmaal over de afgelopen dagen.

        Geeft None zolang er nog geen volledige periode aan geschiedenis is —
        anders leest een halve dag als een halvering van het aantal starts.
        """
        if not self.runs:
            return None
        oudste = min(r.start for r in self.runs)
        beschikbaar = (now - oudste).total_seconds() / 86400.0
        if beschikbaar < days:
            return None
        return round(self.starts_in_last(days * 24, now) / days, 1)

    @property
    def last_start(self) -> datetime | None:
        return self.runs[-1].start if self.runs else None

    @property
    def running(self) -> bool:
        return self._open is not None

    # -- opslag ------------------------------------------------------------

    def to_list(self) -> list[dict[str, str]]:
        return [r.as_dict() for r in self.runs]

    @classmethod
    def from_list(cls, data: list | None) -> CycleTracker:
        runs: list[Run] = []
        for item in data or []:
            try:
                runs.append(Run.from_dict(item))
            except (KeyError, TypeError, ValueError):
                # Eén onleesbare regel mag de hele geschiedenis niet wissen.
                _LOGGER.debug("Onleesbare draaibeurt overgeslagen: %s", item)
        runs.sort(key=lambda r: r.start)
        return cls(runs)
