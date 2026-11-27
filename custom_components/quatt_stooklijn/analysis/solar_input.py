"""Gemeten zon uit het meetlogboek.

Het meetlogboek heeft per vijf minuten de Open-Meteo-straling (horizontaal) en,
sinds v0.10.6-beta.12, het PV-vermogen van een optionele zonnesensor. Hier
wordt daaruit de **gemeten** horizontale straling afgeleid (PV → helderheid →
straling), per monster en per uur: de "waarheid" waartegen de zonverwachting
gescoord wordt.

Tot beta.19 stond hier ook ``window_rows``: de zon op de ramen, voor een
2R2C-variant met gevelzon. Die won alleen door de zon op de thermostaat; op
een begrensde kamertemperatuur was horizontaal even goed of beter (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from .solar_geometry import (
    PV_SCALE_WINTER_ELEVATION,
    clearsky_ghi,
    estimate_pv_scale,
    ghi_from_pv,
    pv_clear_sky,
    sun_position,
)

_TS, _SOL, _PV = 0, 4, 5

#: Hoeveel monsters een uur minstens moet hebben voor een uurwaarde (van 12).
MIN_SAMPLES_PER_HOUR = 6


@dataclass(frozen=True)
class Orientation:
    """Waar de panelen naar wijzen, en waar het huis staat."""

    lat: float
    lon: float
    pv_tilt: float = 35.0
    pv_azimuth: float = 180.0


def _as_array(rows) -> np.ndarray:
    """Logboekrijen als float-array met zes kolommen (PV = NaN als hij ontbreekt)."""
    arr = np.asarray(rows, dtype=float)
    if arr.ndim != 2 or len(arr) == 0:
        return np.empty((0, 6))
    if arr.shape[1] == 5:
        arr = np.column_stack([arr, np.full(len(arr), np.nan)])
    return arr[:, :6]


def fill_pv_from_hourly(
    ts: np.ndarray, pv: np.ndarray, hourly: list[tuple[float, float]]
) -> np.ndarray:
    """Vul ontbrekende PV-waarden aan uit uurgemiddelden (recorderstatistiek).

    ``hourly`` is ``(begin van het uur, gemiddeld W)``. Het gemiddelde hoort bij
    het midden van het uur en wordt daartussen lineair geïnterpoleerd. Alleen
    monsters zonder eigen meting worden ingevuld, en alleen binnen het bereik
    van de uurreeks: buiten dat bereik verzinnen we niets.
    """
    pv = np.array(pv, dtype=float)
    if not hourly:
        return pv
    h = np.array(sorted(hourly), dtype=float)
    mids = h[:, 0] + 1800.0
    vals = np.clip(h[:, 1], 0.0, None)
    gap = ~np.isfinite(pv)
    inside = (ts >= mids[0]) & (ts <= mids[-1])
    todo = gap & inside
    if todo.any():
        pv[todo] = np.interp(ts[todo], mids, vals)
    return pv


#: Waar de schaal van de PV-installatie vandaan komt (``MeasuredSun.scale_source``).
SCALE_MEASURED = "gemeten"     # eigen vijfminutenmonsters, zon boven 15°
SCALE_SAVED = "bewaard"        # de laatste schaal die zo bepaald is
SCALE_FILLED = "aangevuld"     # met de uuraanvulling erbij, zon boven 15°
SCALE_LOW_SUN = "lage_zon"     # met de uuraanvulling erbij, zon boven 10° (winter)


class MeasuredSun(NamedTuple):
    """Gemeten horizontale straling per monster, met de gebruikte schaal."""

    ts: np.ndarray
    ghi: np.ndarray
    scale: float | None
    scale_source: str | None


def measured_ghi(
    rows,
    where: Orientation,
    hourly_pv: list[tuple[float, float]] | None = None,
    saved_scale: float | None = None,
) -> MeasuredSun:
    """Gemeten horizontale straling per monster, uit de PV-kolom.

    ``ghi`` is NaN waar de PV onbekend is en de zon op staat; nul als de zon
    onder is. De schaal komt, in deze volgorde, uit:

    1. de eigen vijfminutenmonsters, met de zon boven 15° (``SCALE_MEASURED``);
    2. ``saved_scale``, de laatste die zo bepaald is (``SCALE_SAVED``). Een
       PV-installatie verandert niet van grootte, en rond de jaarwisseling komt
       de zon in een venster van 30 dagen bijna niet boven 15°;
    3. de monsters met de uuraanvulling erbij, zon boven 15° (``SCALE_FILLED``);
    4. idem met de zon boven 10° (``SCALE_LOW_SUN``), voor wie in de winter
       begint en nog niets bewaard heeft.

    De uuraanvulling vlakt de dag af: op heldere momenten 8 % te laag, op
    donkere 36 % te hoog. Het percentiel pakt de heldere, dus een schaal uit
    aangevulde monsters viel op het testhuis 6 % te laag uit (13–23-11-2026:
    4,32 tegen 4,59 uit de vijfminutenstatistiek van dezelfde dagen). Daarom
    pas na de bewaarde schaal. Voor de straling zelf telt de aanvulling wel.

    Lukt geen van vieren, dan is alles NaN en de schaal None.
    """
    arr = _as_array(rows)
    if len(arr) == 0:
        return MeasuredSun(np.empty(0), np.empty(0), None, None)
    ts = arr[:, _TS]
    pv_own = arr[:, _PV]
    pv = fill_pv_from_hourly(ts, pv_own, hourly_pv) if hourly_pv else pv_own
    el, az, e0 = sun_position(ts, where.lat, where.lon)
    cs = pv_clear_sky(el, az, e0, where.pv_tilt, where.pv_azimuth)
    scale, source = estimate_pv_scale(pv_own, cs, el), SCALE_MEASURED
    if scale is None and saved_scale is not None and saved_scale > 0:
        scale, source = float(saved_scale), SCALE_SAVED
    if scale is None and hourly_pv:
        scale, source = estimate_pv_scale(pv, cs, el), SCALE_FILLED
    if scale is None:
        scale = estimate_pv_scale(pv, cs, el, min_elevation=PV_SCALE_WINTER_ELEVATION)
        source = SCALE_LOW_SUN
    if scale is None:
        return MeasuredSun(ts, np.full(len(ts), np.nan), None, None)
    ghi = ghi_from_pv(pv, el, az, e0, where.pv_tilt, where.pv_azimuth, scale)
    return MeasuredSun(ts, ghi, scale, source)


def hourly_measured(
    ts: np.ndarray, ghi: np.ndarray, where: Orientation
) -> dict[int, tuple[float, float | None]]:
    """Per uur (begin, UTC-seconden): gemiddelde gemeten straling en helderheid.

    De helderheid (gemeten / heldere hemel) is None als de zon te laag staat om
    er iets zinnigs over te zeggen; dan bestaat er ook geen persistentie.
    """
    out: dict[int, tuple[float, float | None]] = {}
    if len(ts) == 0:
        return out
    hour = (ts // 3600).astype(np.int64) * 3600
    ok = np.isfinite(ghi)
    for h in np.unique(hour[ok]):
        sel = ok & (hour == h)
        if sel.sum() < MIN_SAMPLES_PER_HOUR:
            continue
        mean = float(np.mean(ghi[sel]))
        el, _az, _e0 = sun_position(np.array([h + 1800.0]), where.lat, where.lon)
        cs = float(clearsky_ghi(el)[0])
        out[int(h)] = (mean, mean / cs if cs > 50.0 else None)
    return out

