"""Rendement los van het weer: presteert de installatie beter dan haar eigen norm?

De dag-COP is onbruikbaar om een wijziging aan te toetsen, want hij hangt vooral
van de buitentemperatuur af: op deze installatie 1,70 bij −10 °C en 4,90 bij
+10 °C. Twee dagen vergelijken zegt dus niets zolang het weer verschilt.

Daarom eerst een referentiecurve uit de eigen historie — wat wás de COP normaal
bij deze buitentemperatuur — en dan per dag de verhouding daarmee. Die
verhouding is dimensieloos en weerneutraal: 1,00 is precies zoals altijd, hoger
is beter.

Bewust een **gebinde mediaan** en geen regressie. De curve is niet lineair (hij
knikt af boven ~10 °C, waar de pomp vooral nog kortcyclet) en één slechte dag mag
de norm niet verschuiven. Een mediaan per bin lost beide op zonder aan te nemen
hoe de curve loopt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .utils import select_heating

# Breedte van een temperatuurbin in °C. Smaller geeft een scherpere curve maar
# minder dagen per bin; 2 °C houdt op een normaal stookseizoen genoeg over.
DEFAULT_BIN_WIDTH = 2.0

# Minimaal aantal stookdagen in een bin voordat hij meetelt als norm. Onder deze
# grens is de mediaan te toevallig om een dag tegen af te zetten.
DEFAULT_MIN_DAYS_PER_BIN = 3

# Minimaal aantal bins voor een bruikbare curve. Met minder valt er niet zinvol
# te interpoleren tussen de bins.
MIN_BINS = 3


@dataclass
class CopPerformanceResult:
    """Referentiecurve plus de afwijking per dag."""

    # Bin-middelpunt (°C) → mediane COP van de stookdagen in die bin.
    reference: dict[float, float] = field(default_factory=dict)
    # Per stookdag: datum, buitentemp, gemeten COP, verwachte COP, verhouding.
    daily: list[dict] = field(default_factory=list)
    # Verhouding van de meest recente stookdag.
    latest_ratio: float | None = None
    latest_date: str | None = None
    # Voortschrijdende gemiddelden — waar je een wijziging op beoordeelt, want
    # één dag is te ruis-gevoelig.
    rolling_7d: float | None = None
    rolling_30d: float | None = None
    # Hoeveel stookdagen de referentiecurve dragen.
    reference_days: int = 0


def build_reference_curve(
    df_heating: pd.DataFrame,
    bin_width: float = DEFAULT_BIN_WIDTH,
    min_days_per_bin: int = DEFAULT_MIN_DAYS_PER_BIN,
) -> dict[float, float]:
    """Mediane COP per temperatuurbin, als ``{bin_middelpunt: cop}``.

    Bins met te weinig dagen vallen weg in plaats van mee te doen met een
    toevallige waarde; de interpolatie overbrugt het gat later vanzelf.
    """
    if df_heating.empty:
        return {}

    temps = df_heating["avg_temperatureOutside"].to_numpy(dtype=float)
    cops = df_heating["averageCOP"].to_numpy(dtype=float)

    # Bin-index via floor-deling, zodat het middelpunt reproduceerbaar is.
    idx = np.floor(temps / bin_width).astype(int)

    curve: dict[float, float] = {}
    for bin_idx in np.unique(idx):
        mask = idx == bin_idx
        if int(mask.sum()) < min_days_per_bin:
            continue
        centre = round((bin_idx + 0.5) * bin_width, 2)
        curve[centre] = float(np.median(cops[mask]))
    return curve


def reference_cop(curve: dict[float, float], temp: float) -> float | None:
    """Verwachte COP bij deze buitentemperatuur, lineair tussen de bins.

    Buiten het bereik van de curve wordt **niet** geëxtrapoleerd: daar is geen
    norm, en een verzonnen norm zou een verhouding opleveren die nergens op
    slaat. ``np.interp`` klemt op de randwaarden, dus dat wordt hier expliciet
    afgevangen.
    """
    if len(curve) < 2:
        return None
    xs = sorted(curve)
    if temp < xs[0] or temp > xs[-1]:
        return None
    ys = [curve[x] for x in xs]
    return float(np.interp(temp, xs, ys))


def calculate_cop_performance(
    df_daily: pd.DataFrame | None,
    bin_width: float = DEFAULT_BIN_WIDTH,
    min_days_per_bin: int = DEFAULT_MIN_DAYS_PER_BIN,
) -> CopPerformanceResult:
    """Bouw de referentiecurve en zet elke stookdag ertegen af.

    Args:
        df_daily: dagframe met ``avg_temperatureOutside``, ``averageCOP`` en
            ``totalHeatPerHour``, geïndexeerd op datum.
    """
    result = CopPerformanceResult()
    if df_daily is None or df_daily.empty:
        return result

    needed = ["avg_temperatureOutside", "averageCOP", "totalHeatPerHour"]
    if not all(col in df_daily.columns for col in needed):
        return result

    df = df_daily[needed].replace([np.inf, -np.inf], np.nan).dropna()
    # Alleen echte stookdagen — dezelfde filter als de warmteverliesregressie,
    # zodat zomerdagen de norm niet verdunnen.
    df = select_heating(df)
    # Een COP van 0 of lager is geen meting maar een dag zonder bruikbare data.
    df = df[df["averageCOP"] > 0]
    if df.empty:
        return result

    curve = build_reference_curve(df, bin_width, min_days_per_bin)
    if len(curve) < MIN_BINS:
        return result

    result.reference = curve
    result.reference_days = int(len(df))

    df = df.sort_index()
    for date, row in df.iterrows():
        temp = float(row["avg_temperatureOutside"])
        cop = float(row["averageCOP"])
        ref = reference_cop(curve, temp)
        if ref is None or ref <= 0:
            continue
        result.daily.append({
            "date": str(date)[:10],
            "temp": round(temp, 1),
            "cop": round(cop, 2),
            "cop_ref": round(ref, 2),
            "ratio": round(cop / ref, 3),
        })

    if not result.daily:
        return result

    ratios = [d["ratio"] for d in result.daily]
    result.latest_ratio = ratios[-1]
    result.latest_date = result.daily[-1]["date"]
    result.rolling_7d = round(float(np.mean(ratios[-7:])), 3)
    result.rolling_30d = round(float(np.mean(ratios[-30:])), 3)
    return result
