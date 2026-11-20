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

import numpy as np

from .solar_geometry import (
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


def measured_ghi(
    rows, where: Orientation, hourly_pv: list[tuple[float, float]] | None = None
) -> tuple[np.ndarray, np.ndarray, float | None]:
    """Gemeten horizontale straling per monster, uit de PV-kolom.

    Retourneert ``(ts, ghi, schaal)``. ``ghi`` is NaN waar de PV onbekend is en
    de zon op staat; nul als de zon onder is. Zonder bruikbare schaal (te weinig
    zon gezien) is alles NaN en de schaal None.
    """
    arr = _as_array(rows)
    if len(arr) == 0:
        return np.empty(0), np.empty(0), None
    ts = arr[:, _TS]
    pv = arr[:, _PV]
    if hourly_pv:
        pv = fill_pv_from_hourly(ts, pv, hourly_pv)
    el, az, e0 = sun_position(ts, where.lat, where.lon)
    cs = pv_clear_sky(el, az, e0, where.pv_tilt, where.pv_azimuth)
    scale = estimate_pv_scale(pv, cs, el)
    if scale is None:
        return ts, np.full(len(ts), np.nan), None
    return ts, ghi_from_pv(pv, el, az, e0, where.pv_tilt, where.pv_azimuth, scale), scale


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
    rows, where: Orientation, hourly_pv: list[tuple[float, float]] | None = None
) -> tuple[list[list[float]] | None, dict]:
    """Logboekrijen met de zonkolom vervangen door de zon op de ramen (W/m²).

    Bron per monster: gemeten (PV, eventueel aangevuld uit de recorder), anders
    terugval op de Open-Meteo-waarde uit het logboek. Geeft ``(None, info)`` als
    de dekking door de zonnesensor te laag is om iets over de sensor te zeggen.
    """
    arr = _as_array(rows)
    info: dict = {"pv_dekking": None, "pv_schaal": None}
    if len(arr) == 0:
        return None, info
    ts, ghi_pv, scale = measured_ghi(arr, where, hourly_pv)
    info["pv_schaal"] = round(scale, 2) if scale is not None else None
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
