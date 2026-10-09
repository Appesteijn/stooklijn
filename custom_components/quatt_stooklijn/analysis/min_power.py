"""Het laagste warmtevermogen dat de warmtepomp continu kan leveren.

Een warmtepomp moduleert niet tot nul. Onder zijn laagste compressorstand
levert hij een vraag van 1,1 kW niet als 1,1 kW, maar als een blok op ~2,2 kW
met daarna een pauze. Op 09-10-2026 deed HP2 precies dat: 35–43 minuten aan,
~30 minuten uit. Per uur komt de warmte er wel, maar hóé hij komt — in blokken
— bepaalt starts, en dus wat een plan in werkelijkheid kan.

Dat minimum staat nergens als instelling. Het hangt van de firmware af (de
laagste compressorfrequentie) en van de buitentemperatuur: op dezelfde laagste
stand levert de unit bij zacht weer méér warmte dan bij vorst. Gemeten op
09-10-2026: 's ochtends ~1,94 kW, 's middags bij warmer weer 2,23–2,56 kW.
Daarom wordt het geleerd uit het meetlogboek in plaats van ingesteld.

**Wat telt als meting.** Alleen monsters midden in een run: de twee monsters
ervoor en het monster erna draaien ook. Zo vallen het opstarten (de eerste
minuten loopt de afgifte op) en het afbouwen af. Per bin buitentemperatuur
telt een laag percentiel, geen minimum: één rare meting mag het niet bepalen.

**Monotoon naar de kou.** Een bin waarin de warmtepomp alleen op vollast heeft
gedraaid — een koude week waarin hij nooit hoefde te moduleren — levert een
veel te hoog "minimum". Fysisch is het minimum bij kou niet hoger dan bij zacht
weer, dus van warm naar koud wordt het lopend minimum genomen. Dat kapt zo'n
vollastbin af op wat een warmere bin al liet zien.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Boven dit vermogen draait de warmtepomp. Ruim onder elk compressorminimum en
# ruim boven de ruis van een stilstaande unit (sluimerverbruik, flowmeter).
RUNNING_W = 500.0

# Breedte van een temperatuurbin (K). Smal genoeg om de helling te zien
# (~0,6 kW over 5 K op 09-10-2026), breed genoeg voor genoeg monsters per bin.
BIN_K = 2.0

# Minimaal aantal monsters in een bin: een half uur stabiel draaien.
MIN_SAMPLES_PER_BIN = 6

# Welk percentiel van de afgifte als minimum telt.
QUANTILE = 0.10

# Boven deze buitentemperatuur wordt niet gestookt. Een monster daarboven dat
# toch "draait" is koelen: OpenQuatt meldt dan 0 W warmte, maar niet elke bron
# hoeft dat te doen, en door het lopende minimum zou één koelbin elke stookbin
# omlaag trekken.
MAX_HEATING_T_OUT = 20.0

# Twee monsters liggen normaal vijf minuten uit elkaar. Bij een groter gat is
# "het monster ervoor" geen buurman meer en zegt het niets over de run.
MAX_GAP_S = 6 * 60


@dataclass
class MinPowerCurve:
    """Minimumvermogen per buitentemperatuur, met klemming op de randen."""

    # Bincentrum (°C) → minimum (W), oplopend in temperatuur.
    bins: dict[float, float] = field(default_factory=dict)
    samples: int = 0

    def at(self, t_outdoor: float) -> float | None:
        """Het minimum bij deze buitentemperatuur, of ``None`` zonder curve."""
        if not self.bins:
            return None
        xs = sorted(self.bins)
        return float(np.interp(t_outdoor, xs, [self.bins[x] for x in xs]))

    def for_temps(self, temps: list[float]) -> list[float] | None:
        """Het minimum per uur van een forecast, of ``None`` zonder curve."""
        if not self.bins:
            return None
        return [self.at(t) for t in temps]  # type: ignore[misc]

    def as_attribute(self) -> dict:
        return {
            "monsters": self.samples,
            "per_buitentemperatuur": {
                f"{t:g}": round(p) for t, p in sorted(self.bins.items())
            },
        }


def learn_min_power(samples: list[list[float]]) -> MinPowerCurve:
    """Leer het minimum uit rijen ``[ts, t_in, t_out, q_hp_w, q_solar]``.

    Geeft een lege curve als er te weinig stabiele runs in zitten; elke
    gebruiker valt dan terug op het gedrag zonder minimum.
    """
    if len(samples) < 4:
        return MinPowerCurve()
    arr = np.array([row[:4] for row in samples], dtype=float)
    # Het logboek kan na een teruglopende klok uit volgorde staan.
    arr = arr[np.argsort(arr[:, 0], kind="stable")]
    ts, t_out, q = arr[:, 0], arr[:, 2], arr[:, 3]

    running = (q > RUNNING_W) & (t_out <= MAX_HEATING_T_OUT)
    gap = np.diff(ts)
    close = np.concatenate([[False], gap <= MAX_GAP_S])  # i en i−1 buren
    # Midden in een run: i−2, i−1, i en i+1 draaien, zonder gaten ertussen.
    mid = np.zeros(len(q), dtype=bool)
    mid[2:-1] = (
        running[2:-1]
        & running[1:-2] & close[2:-1]
        & running[:-3] & close[1:-2]
        & running[3:] & close[3:]
    )
    if not mid.any():
        return MinPowerCurve()

    centers = np.round(t_out[mid] / BIN_K) * BIN_K
    values = q[mid]
    bins: dict[float, float] = {}
    for c in np.unique(centers):
        v = values[centers == c]
        if len(v) >= MIN_SAMPLES_PER_BIN:
            bins[float(c)] = float(np.quantile(v, QUANTILE))
    if not bins:
        return MinPowerCurve()

    # Lopend minimum van warm naar koud; zie de moduledocstring.
    lopend = float("inf")
    for c in sorted(bins, reverse=True):
        lopend = min(lopend, bins[c])
        bins[c] = lopend
    return MinPowerCurve(bins=bins, samples=int(mid.sum()))
