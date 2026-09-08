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
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.highres_log"

#: Bemonsteringsinterval. Fijn genoeg voor een tijdconstante van een uur,
#: grof genoeg om het logboek binnen een paar honderd kilobyte te houden.
SAMPLE_INTERVAL = timedelta(minutes=5)

#: 90 dagen bij vijf minuten. Ruim een stookseizoen, zodat een analyse in
#: maart de koudste weken nog terugvindt.
MAX_SAMPLES = 25_920

#: Monsters dichter op elkaar dan dit worden overgeslagen. Zonder deze rem
#: zou een herstart-storm of een handmatige aanroep het venster verkorten.
MIN_SPACING = timedelta(minutes=4)


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
    ) -> bool:
        """Leg één monster vast. Geeft False als het is overgeslagen."""
        ts = int(timestamp.timestamp())
        if self._samples and ts - self._samples[-1][0] < MIN_SPACING.total_seconds():
            return False
        self._samples.append([
            ts,
            round(float(t_indoor), 2),
            round(float(t_outdoor), 2),
            round(float(q_hp_w)),
            round(float(q_solar_wm2), 1),
        ])
        return True

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def span_hours(self) -> float | None:
        """Hoeveel uur er tussen het oudste en nieuwste monster zit."""
        if len(self._samples) < 2:
            return None
        return (self._samples[-1][0] - self._samples[0][0]) / 3600.0

    def to_dict(self) -> dict:
        return {
            "columns": ["ts", "t_indoor", "t_outdoor", "q_hp_w", "q_solar_wm2"],
            "samples": list(self._samples),
        }

    @classmethod
    def from_dict(cls, data: dict, max_samples: int = MAX_SAMPLES) -> HighResLog:
        log = cls(max_samples=max_samples)
        for row in data.get("samples", []):
            # Een half geschreven of met de hand aangepast bestand mag het
            # logboek niet slopen: sla onbruikbare rijen over in plaats van
            # de hele buffer weg te gooien.
            if isinstance(row, list) and len(row) == 5:
                try:
                    log._samples.append([
                        int(row[0]), float(row[1]), float(row[2]),
                        float(row[3]), float(row[4]),
                    ])
                except (TypeError, ValueError):
                    continue
        return log


class HighResLogStore:
    """Persist HighResLog across HA restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.log = HighResLog()
        self._dirty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            try:
                self.log = HighResLog.from_dict(data)
                _LOGGER.info(
                    "Meetlogboek geladen: %d monsters, %s uur",
                    len(self.log),
                    round(self.log.span_hours) if self.log.span_hours else "?",
                )
            except Exception:
                _LOGGER.warning(
                    "Meetlogboek onleesbaar, begin opnieuw", exc_info=True
                )
                self.log = HighResLog()

    def record(self, *args, **kwargs) -> bool:
        recorded = self.log.record(*args, **kwargs)
        self._dirty = self._dirty or recorded
        return recorded

    async def async_save(self) -> None:
        """Wegschrijven, maar alleen als er iets bij is gekomen.

        Bewust niet bij elk monster: een vol logboek is ongeveer een
        megabyte en veel installaties draaien op een SD-kaart.
        """
        if not self._dirty:
            return
        await self._store.async_save(self.log.to_dict())
        self._dirty = False
