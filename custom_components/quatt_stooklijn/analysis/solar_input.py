"""Gemeten zon uit het meetlogboek, en de zon zoals de ramen hem zien.

Het meetlogboek heeft per vijf minuten de Open-Meteo-straling (horizontaal) en,
sinds v0.10.6-beta.12, het PV-vermogen van een optionele zonnesensor. Hier
wordt daaruit afgeleid:

* de **gemeten** horizontale straling (PV → helderheid → straling), per monster
  en per uur — de "waarheid" waartegen de zonverwachting gescoord wordt;
* logboekrijen waarin de zonkolom vervangen is door wat er **door de ramen**
  binnenkomt, voor het 2R2C met gevelzon in de modelvergelijking.

Zie ZONNEWARMTE_PLAN.md voor waarom: gemeten zon op de gevel gaf het 2R2C
14–20 % minder fout op 12 uur vooruit dan horizontale straling uit Open-Meteo.
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
    window_irradiance,
)

_TS, _SOL, _PV = 0, 4, 5

#: Minimaal aandeel daglichtmonsters met een PV-waarde (gemeten of aangevuld)
#: voordat de gevelzon-variant meedoet. Met minder valt het grootste deel terug
#: op Open-Meteo en zegt de vergelijking niets over de zonnesensor.
MIN_PV_COVERAGE = 0.8

#: Hoeveel monsters een uur minstens moet hebben voor een uurwaarde (van 12).
MIN_SAMPLES_PER_HOUR = 6


@dataclass(frozen=True)
class Orientation:
    """Waar de ramen en de panelen naar wijzen, en waar het huis staat."""

    lat: float
    lon: float
    window_azimuth: float = 180.0
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


def window_rows(
    rows,
    where: Orientation,
    hourly_pv: list[tuple[float, float]] | None = None,
    saved_scale: float | None = None,
) -> tuple[list[list[float]] | None, dict]:
    """Logboekrijen met de zonkolom vervangen door de zon op de ramen (W/m²).

    Bron per monster: gemeten (PV, eventueel aangevuld uit de recorder), anders
    terugval op de Open-Meteo-waarde uit het logboek. Geeft ``(None, info)`` als
    de dekking door de zonnesensor te laag is om iets over de sensor te zeggen.
    ``saved_scale``: zie ``measured_ghi``.
    """
    arr = _as_array(rows)
    info: dict = {"pv_dekking": None, "pv_schaal": None, "pv_schaal_bron": None}
    if len(arr) == 0:
        return None, info
    ts, ghi_pv, scale, source = measured_ghi(arr, where, hourly_pv, saved_scale)
    info["pv_schaal"] = round(scale, 2) if scale is not None else None
    info["pv_schaal_bron"] = source
    el, az, e0 = sun_position(ts, where.lat, where.lon)
    day = el > 1.0
    covered = np.isfinite(ghi_pv)
    info["pv_dekking"] = round(float(covered[day].mean()), 3) if day.any() else None
    if scale is None or info["pv_dekking"] is None or info["pv_dekking"] < MIN_PV_COVERAGE:
        return None, info
    ghi = np.where(covered, ghi_pv, arr[:, _SOL])
    out = arr[:, :5].copy()
    out[:, _SOL] = window_irradiance(ghi, el, az, e0, where.window_azimuth)
    return out.tolist(), info
