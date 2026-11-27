"""Extra kamersensoren tegen een thermostaat in de zon.

Een kamertemperatuursensor waar de zon op schijnt meet zichzelf. Op het
testhuis (november 2026) liep de thermostaat op zonnige middagen tot 2,3 K op
terwijl drie andere sensoren in dezelfde kamer vlak bleven; OpenQuatt knepen
de pomp daardoor af en het huismodel leerde een te grote zonnewinst. Zie
KAMERSENSOREN_PLAN.md.

De regels, alle drie gemeten (plan, "Wat werkt en wat niet"):

1. De thermostaat blijft de bron. Het setpoint hoort bij de thermostaat, en zon
   maakt een sensor alleen warmer: de referenties mogen hem alleen naar
   beneden begrenzen (``compose``), nooit vervangen of middelen. Een mediaan
   inclusief de thermostaat maakte het huismodel slechter.
2. Per referentie een vast verschil met de thermostaat, geleerd op de
   nachturen (``night_offsets``): 's nachts schijnt er geen zon, dus daar meten
   beide de kamer.
3. Een referentie die zelf zon krijgt, telt in die uren niet mee
   (``sun_hours``). Begrensd op zo'n sensor werd het slechter dan zonder.

Puur numpy, geen Home Assistant: de runtime staat in ``room_temperature.py``.
Tijden zijn het begin van het uur in UTC-seconden; uren van de dag zijn lokaal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, tzinfo
import math

import numpy as np

#: Naam van de thermostaat in ``sun_effects``; kan geen entity-ID zijn.
_PRIMARY = "\0thermostaat"

#: Nachturen (lokaal, begin van het uur) waarop het vaste verschil geleerd wordt.
NIGHT_HOURS = range(0, 6)
#: Over hoeveel dagen het verschil geleerd wordt.
OFFSET_DAYS = 7
#: Zoveel nachturen zijn minstens nodig voor een verschil.
OFFSET_MIN_HOURS = 12

#: Venster voor de zonherkenning per sensor.
SUN_DAYS = 14
#: Een uur van de dag "krijgt zon" bij een afwijking van minstens zoveel,
#: zonnige uren min bewolkte uren. Testhuis: sensoren met zon 0,45–1,1 K,
#: sensoren zonder hoogstens +0,35 K (heatbooster bij de radiator).
SUN_THRESHOLD_K = 0.4
#: Helderheid (gemeten / heldere hemel) vanaf waar een uur zonnig is,
#: en tot waar het bewolkt is.
SUNNY_CLEARNESS = 0.5
CLOUDY_CLEARNESS = 0.2
#: Minimaal aantal zonnige en bewolkte uren per uur van de dag.
SUN_MIN_SUNNY = 3
SUN_MIN_CLOUDY = 2

#: Zoveel mag de thermostaat boven de referentie uitkomen. Gelijk aan de
#: bovenkant van de comfortband van OpenQuatt; kleiner kapt ook gewone
#: verschillen tussen plekken in de kamer af.
CLIP_K = 0.3


def _local_hour(ts: float, tz: tzinfo) -> int:
    return datetime.fromtimestamp(ts, tz).hour


def night_offsets(
    primary: Mapping[float, float],
    references: Mapping[str, Mapping[float, float]],
    now_ts: float,
    tz: tzinfo,
    days: int = OFFSET_DAYS,
    min_hours: int = OFFSET_MIN_HOURS,
) -> dict[str, float]:
    """Vast verschil per referentie: mediaan van (thermostaat − referentie).

    Over de nachturen van de laatste ``days`` dagen. Een referentie met te
    weinig gedeelde nachturen krijgt geen verschil en doet niet mee.
    """
    out: dict[str, float] = {}
    lo = now_ts - days * 86400
    for name, series in references.items():
        diffs = [
            primary[t] - series[t]
            for t in series
            if t in primary and lo <= t < now_ts and _local_hour(t, tz) in NIGHT_HOURS
        ]
        if len(diffs) >= min_hours:
            out[name] = float(np.median(diffs))
    return out


def sun_effects(
    series: Mapping[str, Mapping[float, float]],
    offsets: Mapping[str, float],
    clearness: Mapping[float, float],
    now_ts: float,
    tz: tzinfo,
    days: int = SUN_DAYS,
    threshold: float = SUN_THRESHOLD_K,
) -> dict[str, dict[int, float]]:
    """Per sensor de uren van de dag waarin hij zon krijgt, met hoeveel (K).

    ``series`` bevat de thermostaat en de referenties; ``offsets`` het vaste
    verschil per naam (de thermostaat 0). Per uur wordt elke sensor, na zijn
    verschil, vergeleken met de mediaan van de andere. Krijgt een sensor zon,
    dan ligt die afwijking in zonnige uren hoger dan in bewolkte. Alleen
    positieve afwijkingen tellen: zon maakt warmer.

    Met twee sensoren is de mediaan van "de andere" gewoon de andere, en
    beslist het teken: zit de zon op de een, dan loopt alleen diens afwijking
    op. Krijgen beide tegelijk zon, dan is dat niet te zien.
    """
    names = [n for n in series if n in offsets]
    out: dict[str, dict[int, float]] = {n: {} for n in series}
    if len(names) < 2:
        return out
    lo = now_ts - days * 86400
    hours = sorted(
        t for t in set.intersection(*(set(series[n]) for n in names))
        if lo <= t < now_ts and t in clearness
    )
    by_hod: dict[str, dict[int, dict[str, list[float]]]] = {n: {} for n in names}
    for t in hours:
        corrected = {n: series[n][t] + offsets[n] for n in names}
        c = clearness[t]
        if c >= SUNNY_CLEARNESS:
            kind = "zon"
        elif c <= CLOUDY_CLEARNESS:
            kind = "bewolkt"
        else:
            continue
        hod = _local_hour(t, tz)
        for n in names:
            ref = float(np.median([corrected[m] for m in names if m != n]))
            slot = by_hod[n].setdefault(hod, {"zon": [], "bewolkt": []})
            slot[kind].append(corrected[n] - ref)
    for n in names:
        for hod, slot in by_hod[n].items():
            if len(slot["zon"]) < SUN_MIN_SUNNY or len(slot["bewolkt"]) < SUN_MIN_CLOUDY:
                continue
            effect = float(np.median(slot["zon"]) - np.median(slot["bewolkt"]))
            if effect >= threshold:
                out[n][hod] = effect
    return out


def sun_hours(*args, **kwargs) -> dict[str, set[int]]:
    """Als ``sun_effects``, alleen de uren."""
    return {n: set(v) for n, v in sun_effects(*args, **kwargs).items()}


def compose(
    primary: float | None,
    references: Mapping[str, float | None],
    offsets: Mapping[str, float],
    excluded: set[str] | frozenset[str] = frozenset(),
    clip_k: float = CLIP_K,
) -> tuple[float | None, dict]:
    """De kamertemperatuur: de thermostaat, begrensd op de referenties.

    ``min(thermostaat, mediaan(referentie + verschil) + clip_k)`` over de
    referenties die een verschil hebben, een waarde leveren en niet in
    ``excluded`` staan (zelf zon in dit uur). Zonder bruikbare referentie de
    thermostaat zelf, onveranderd. Zonder thermostaat None: een referentie is
    een bovengrens, geen vervanging.

    Geeft ``(waarde, details)``; details: ``gebruikt`` (namen),
    ``referentie`` (de mediaan, of None) en ``afgekapt_k``.
    """
    details = {"gebruikt": [], "referentie": None, "afgekapt_k": 0.0}
    if primary is None:
        return None, details
    usable = {
        n: v + offsets[n]
        for n, v in references.items()
        if v is not None and n in offsets and n not in excluded
    }
    if not usable:
        return primary, details
    ref = float(np.median(list(usable.values())))
    value = min(primary, ref + clip_k)
    details["gebruikt"] = sorted(usable)
    details["referentie"] = ref
    details["afgekapt_k"] = primary - value
    return value, details


#: Zo vaak bepaalt de integratie live het verschil en de zonuren opnieuw.
REFRESH_S = 6 * 3600


def history_corrections(
    primary: Mapping[float, float],
    references: Mapping[str, Mapping[float, float]],
    clearness: Mapping[float, float],
    tz: tzinfo,
    start: float,
    end: float,
    refresh_s: float = REFRESH_S,
) -> dict[float, float]:
    """Wat ``compose`` in het verleden had afgekapt, per uur (K, ≤ 0).

    Uit uurgemiddelden: ``primary`` de thermostaat, ``references`` per naam.
    Het verschil en de zonuren worden net als live om de ``refresh_s``
    opnieuw bepaald, steeds alleen uit de uren daarvóór; een uur weet dus
    niets van later. Uren zonder correctie (geen verschil geleerd, niets
    afgekapt) ontbreken.

    Voor het meetlogboek (``RoomTemperature.async_backfill``): de referenties
    zijn traag genoeg om de correctie van een uur over zijn vijfminuten-
    monsters te leggen. Zo nagespeeld op het testhuis (KAMERSENSOREN_PLAN.md).
    """
    out: dict[float, float] = {}
    offsets: dict[str, float] | None = None
    sun: dict[str, set[int]] = {}
    next_refresh = -math.inf
    for t in sorted(h for h in primary if start <= h < end):
        if t >= next_refresh:
            offsets = night_offsets(primary, references, t, tz)
            sun = sun_hours({_PRIMARY: primary, **references},
                            {_PRIMARY: 0.0, **offsets}, clearness, t, tz)
            next_refresh = t + refresh_s
        if not offsets:
            continue
        hod = _local_hour(t, tz)
        excluded = {n for n in references if hod in sun.get(n, set())}
        value, _ = compose(primary[t], {n: s.get(t) for n, s in references.items()},
                           offsets, excluded)
        if value is not None and value < primary[t]:
            out[t] = value - primary[t]
    return out


# --------------------------------------------------------------------------- #
#  Zonder referenties: pieken die een kamer niet maakt                         #
# --------------------------------------------------------------------------- #
#
# KAMERSENSOREN_PLAN.md, stap 1 en 2. Op het testhuis: melding aan op 18-09,
# geen valse melding; avondpieken (koken, mensen) vallen af op "donker",
# ochtendpieken met de pomp aan op "bewolkt uur". Wat dit níét kan: zon op de
# sensor onderscheiden van een kamer die echt snel opwarmt (nagebootst licht
# huis: 5 van 5 dezelfde pieken). De melding is daarom neutraal.

#: Een piek: zoveel stijging in het uur ervoor ...
PEAK_RISE_K = 0.4
#: ... en zoveel daling binnen twee uur erna.
PEAK_DROP_K = 0.3
#: Zon minstens zo hoog bij het begin van de stijging, anders "donker".
PEAK_MIN_ELEVATION = 3.0
#: Zonpieken rond hetzelfde uur (± 1 u) op minstens zoveel dagen in 14 ...
PEAK_MIN_DAYS = 3
#: ... en minstens zoveel keer zo vaak als bewolkte daglichtpieken op dat uur.
PEAK_SUN_RATIO = 2.0
#: Uit na zoveel zonnige dagen zonder zonpiek.
PEAK_QUIET_DAYS = 14
PEAK_WINDOW_DAYS = 14


def sun_peaks(
    ts: np.ndarray,
    temp: np.ndarray,
    clearness: Mapping[float, float],
    lat: float,
    lon: float,
    tz: tzinfo,
) -> list[dict]:
    """Pieken in de thermostaat: ≥ ``PEAK_RISE_K`` op in een uur, ≥ ``PEAK_DROP_K`` af binnen twee.

    ``ts``/``temp``: vijfminutenmonsters (gesorteerd), de thermostaat zelf.
    Per piek de soort: ``zon`` (zonnig uur), ``bewolkt``, ``half``, ``donker``
    (zon onder ``PEAK_MIN_ELEVATION``) of ``?`` (geen helderheid).
    """
    from .solar_geometry import sun_position

    ts = np.asarray(ts, dtype=float)
    temp = np.asarray(temp, dtype=float)
    if len(ts) < 30:
        return []
    sm = np.convolve(temp, np.ones(3) / 3, mode="same")
    out: list[dict] = []
    i, n = 0, len(ts)
    while i < n:
        t0 = ts[i]
        lo = int(np.searchsorted(ts, t0 - 3600))
        hi = int(np.searchsorted(ts, t0 + 7200, side="right"))
        if i + 1 - lo >= 8 and hi - i >= 16:
            pre, post = sm[lo:i + 1], sm[i:hi]
            rise, drop = sm[i] - pre.min(), sm[i] - post.min()
            if (rise >= PEAK_RISE_K and drop >= PEAK_DROP_K
                    and sm[i] >= pre.max() - 1e-9 and sm[i] >= post.max() - 1e-9):
                t_rise = float(ts[lo + int(np.argmin(pre))])
                el = float(sun_position(np.array([t_rise]), lat, lon)[0][0])
                c = clearness.get(float(int(t_rise // 3600) * 3600))
                if el < PEAK_MIN_ELEVATION:
                    kind = "donker"
                elif c is None:
                    kind = "?"
                elif c >= SUNNY_CLEARNESS:
                    kind = "zon"
                elif c <= CLOUDY_CLEARNESS:
                    kind = "bewolkt"
                else:
                    kind = "half"
                local = datetime.fromtimestamp(t0, tz)
                out.append({"ts": float(t0), "dag": local.date().isoformat(),
                            "uur": local.hour + local.minute / 60.0,
                            "stijging_k": float(rise), "kind": kind})
                i = int(np.searchsorted(ts, t0 + 7200))
                continue
        i += 1
    return out


def day_is_sunny(clearness: Mapping[float, float], day, tz: tzinfo) -> bool | None:
    """Zonnig: gemiddelde helderheid 10–15 u ≥ ``SUNNY_CLEARNESS``. None zonder gegevens."""
    vals = [c for t, c in clearness.items()
            if datetime.fromtimestamp(t, tz).date() == day and 10 <= datetime.fromtimestamp(t, tz).hour < 15]
    if len(vals) < 3:
        return None
    return float(np.mean(vals)) >= SUNNY_CLEARNESS


def _cluster(peaks: list[dict], day, window: int = PEAK_WINDOW_DAYS) -> dict | None:
    """Het beste cluster zonpieken rond hetzelfde uur in de ``window`` dagen tot en met ``day``."""
    from datetime import date, timedelta

    d = date.fromisoformat(day) if isinstance(day, str) else day
    lo = (d - timedelta(days=window - 1)).isoformat()
    hi = d.isoformat()
    sun = [p for p in peaks if p["kind"] == "zon" and lo <= p["dag"] <= hi]
    cloud = [p for p in peaks if p["kind"] == "bewolkt" and lo <= p["dag"] <= hi]
    best = None
    for p0 in sun:
        near = [p for p in sun if abs(p["uur"] - p0["uur"]) <= 1.0]
        days = {p["dag"] for p in near}
        n_cloud = len({p["dag"] for p in cloud if abs(p["uur"] - p0["uur"]) <= 1.0})
        if len(days) >= PEAK_MIN_DAYS and len(days) >= PEAK_SUN_RATIO * n_cloud:
            if best is None or len(days) > best["dagen"]:
                best = {"dagen": len(days), "bewolkt": n_cloud,
                        "van": min(p["uur"] for p in near) - 1.0,
                        "tot": max(p["uur"] for p in near) + 0.5,
                        "kelvin": float(np.median([p["stijging_k"] for p in near]))}
    return best


def peak_notice_step(state: Mapping | None, peaks: list[dict], day, sunny: bool | None) -> dict:
    """De melding zonder referenties, één afgeronde dag verder.

    Aan zodra er een cluster is (``_cluster``); uit pas na
    ``PEAK_QUIET_DAYS`` zonnige dagen zonder zonpiek. ``state``:
    ``{"aan", "stil", "cluster"}``, bewaarbaar als JSON.
    """
    st = {"aan": False, "stil": 0, "cluster": None, **(state or {})}
    d = day.isoformat() if hasattr(day, "isoformat") else str(day)
    cluster = _cluster(peaks, d)
    had_peak = any(p["kind"] == "zon" and p["dag"] == d for p in peaks)
    if cluster is not None:
        st["cluster"] = cluster
    if not st["aan"]:
        if cluster is not None:
            st["aan"], st["stil"] = True, 0
    else:
        if had_peak:
            st["stil"] = 0
        elif sunny:
            st["stil"] += 1
        if st["stil"] >= PEAK_QUIET_DAYS:
            st["aan"], st["stil"] = False, 0
    return st
