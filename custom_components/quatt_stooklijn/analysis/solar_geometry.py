"""Zonnestand en straling op een vlak — de zon zoals de ramen hem zien.

Waarom dit nodig is (onderzoek 20-11-2026, zie ZONNEWARMTE_PLAN.md): de
integratie rekende met straling op een horizontaal vlak en één vaste
zonfactor. Een lage winterzon schijnt bijna loodrecht door verticaal glas, een
hoge voorjaarszon nauwelijks. Hoeveel stookwarmte een kWh PV-opbrengst bespaart
liep daardoor uiteen van 3,0 (nov–jan) tot 0,7 (apr–mei). Omgerekend naar het
vlak van de ramen halveert die spreiding ruim.

Twee bronnen voor "hoe zonnig is het":

* **PV-opbrengst** (gemeten, lokaal). Per moment de helderheid = PV / PV bij
  heldere hemel, daarna terug naar horizontale straling. In de 2R2C-fit was dit
  de beste zoninvoer: 14–20 % minder fout op 12 uur vooruit.
* **Open-Meteo** (verwacht). Nodig om vooruit te kijken, maar per uur flink
  onnauwkeurig.

Alleen numpy, geen pvlib: de integratie mag alleen numpy en pandas. De
nauwkeurigheid (± een halve graad zonnestand, Haurwitz voor heldere hemel,
Erbs voor direct/diffuus) is ruim genoeg naast de onzekerheid in wolken.

Hoeken in graden. Azimut 0 = noord, 90 = oost, 180 = zuid, 270 = west.
"""

from __future__ import annotations

import numpy as np

#: Gemiddelde transmissie van diffuus licht door dubbel glas, relatief aan
#: loodrechte inval. Diffuus licht komt van alle kanten, ook schuin.
DIFFUSE_GLASS_TRANSMISSION = 0.85

#: ASHRAE-hoekafhankelijkheid van glas: transmissie = 1 − b0·(1/cos θ − 1).
GLASS_IAM_B0 = 0.1

#: Reflectie van de grond (gras, bestrating).
ALBEDO = 0.2

#: Elevatie waaronder de zon niet meetelt (refractie, horizon, rekenruis).
MIN_ELEVATION_DEG = 1.0


def sun_position(
    ts_utc: np.ndarray, lat: float, lon: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Elevatie, azimut (graden) en zonneconstante (W/m²) per tijdstip.

    ``ts_utc`` in seconden sinds 1970 (UTC). Vereenvoudigde
    zonnestandsformules (Astronomical Almanac), ± 0,5° — ruim genoeg om straling
    op een vlak te schatten.
    """
    ts = np.asarray(ts_utc, dtype=float)
    n = ts / 86400.0 + 2440587.5 - 2451545.0          # dagen sinds J2000
    L = np.deg2rad((280.460 + 0.9856474 * n) % 360.0)
    g = np.deg2rad((357.528 + 0.9856003 * n) % 360.0)
    lam = L + np.deg2rad(1.915) * np.sin(g) + np.deg2rad(0.020) * np.sin(2 * g)
    eps = np.deg2rad(23.439 - 0.0000004 * n)
    dec = np.arcsin(np.sin(eps) * np.sin(lam))
    ra = np.arctan2(np.cos(eps) * np.sin(lam), np.cos(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    ha = np.deg2rad((gmst * 15.0 + lon) % 360.0) - ra
    phi = np.deg2rad(lat)
    el = np.arcsin(np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.cos(ha))
    az = np.arctan2(-np.sin(ha), np.tan(dec) * np.cos(phi) - np.sin(phi) * np.cos(ha))
    day_of_year = (n % 365.25)
    e0 = 1361.0 * (1.0 + 0.033 * np.cos(2 * np.pi * day_of_year / 365.25))
    return np.rad2deg(el), np.rad2deg(az) % 360.0, e0


def clearsky_ghi(elevation: np.ndarray) -> np.ndarray:
    """Horizontale straling bij heldere hemel (Haurwitz), W/m²."""
    el = np.asarray(elevation, dtype=float)
    cz = np.sin(np.deg2rad(np.clip(el, 0.0, 90.0)))
    return np.where(el > 0, 1098.0 * cz * np.exp(-0.057 / np.maximum(cz, 1e-3)), 0.0)


def diffuse_fraction(kt: np.ndarray) -> np.ndarray:
    """Aandeel diffuus in de horizontale straling bij helderheidsindex kt (Erbs)."""
    kt = np.clip(np.asarray(kt, dtype=float), 0.0, 1.2)
    return np.where(
        kt <= 0.22, 1.0 - 0.09 * kt,
        np.where(
            kt <= 0.80,
            0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4,
            0.165,
        ),
    )


def _cos_incidence(el, az, tilt, surface_az):
    z = np.deg2rad(90.0 - el)
    b = np.deg2rad(tilt)
    return (np.cos(z) * np.cos(b)
            + np.sin(z) * np.sin(b) * np.cos(np.deg2rad(az - surface_az)))


def plane_irradiance(
    ghi: np.ndarray,
    elevation: np.ndarray,
    azimuth: np.ndarray,
    e0: np.ndarray,
    tilt: float,
    surface_azimuth: float,
    glass: bool = False,
) -> np.ndarray:
    """Straling op een vlak met helling ``tilt`` (0 = plat, 90 = gevel), W/m².

    Isotrope hemel. Met ``glass`` wat er door het glas komt: directe zon met
    hoekafhankelijke transmissie, diffuus met een vaste factor.
    """
    ghi = np.asarray(ghi, dtype=float)
    el = np.asarray(elevation, dtype=float)
    up = el > MIN_ELEVATION_DEG
    cz = np.sin(np.deg2rad(np.clip(el, 0.5, 90.0)))
    kt = np.where(up, ghi / np.maximum(e0 * cz, 1.0), 0.0)
    dhi = ghi * diffuse_fraction(kt)
    dni = np.where(up, (ghi - dhi) / cz, 0.0)
    ci = _cos_incidence(el, azimuth, tilt, surface_azimuth)
    beam = dni * np.clip(ci, 0.0, None)
    diffuse_t = 1.0
    if glass:
        iam = np.where(ci > 0.05, 1.0 - GLASS_IAM_B0 * (1.0 / np.maximum(ci, 0.05) - 1.0), 0.0)
        beam = beam * np.clip(iam, 0.0, 1.0)
        diffuse_t = DIFFUSE_GLASS_TRANSMISSION
    b = np.deg2rad(tilt)
    sky = dhi * (1.0 + np.cos(b)) / 2.0 * diffuse_t
    ground = ghi * ALBEDO * (1.0 - np.cos(b)) / 2.0 * diffuse_t
    return np.where(up, beam + sky + ground, 0.0)


def window_irradiance(
    ghi: np.ndarray,
    elevation: np.ndarray,
    azimuth: np.ndarray,
    e0: np.ndarray,
    window_azimuth: float,
) -> np.ndarray:
    """Wat er door verticaal glas in gevel ``window_azimuth`` binnenkomt, W/m²."""
    return plane_irradiance(ghi, elevation, azimuth, e0, 90.0, window_azimuth, glass=True)


#: Hoe de schaal van de PV-installatie wordt bepaald: dit percentiel van
#: PV / PV-bij-heldere-hemel, over momenten waarop de zon hoog genoeg staat.
PV_SCALE_PERCENTILE = 97.0
#: Minimaal aantal bruikbare momenten voor een schaal. Bij vijf minuten en
#: zon boven 15° is dat in oktober ruim een dag, eind november drie dagen.
PV_SCALE_MIN_SAMPLES = 100
#: Zonhoogte waarboven de schaal bepaald wordt. Bij lage zon tellen schaduw
#: van de buren en fouten in de heldere-hemelstraling zwaarder.
PV_SCALE_MIN_ELEVATION = 15.0
#: Winterdrempel, alleen als er boven 15° te weinig momenten zijn. Op 52° NB
#: komt de zon van half december tot half januari nauwelijks boven 15°; in een
#: venster van 30 dagen bleven er begin januari 53 momenten over, in Groningen
#: geen. Boven 10° zijn het er dan nog ~500–1.600. Gemeten op het testhuis
#: (20–23 nov 2026, panelen 195°): schaal 4,75 uit 10–15° tegen 4,74 boven 15°.
#: Met de richting 15° verkeerd (180°) werd het 5,07 tegen 4,76: lage zon is
#: gevoeliger voor de richting, daarom alleen als terugval.
PV_SCALE_WINTER_ELEVATION = 10.0


def pv_clear_sky(elevation, azimuth, e0, pv_tilt: float, pv_azimuth: float) -> np.ndarray:
    """Straling op het paneelvlak bij heldere hemel, W/m²."""
    return plane_irradiance(clearsky_ghi(elevation), elevation, azimuth, e0, pv_tilt, pv_azimuth)


def estimate_pv_scale(
    pv_w: np.ndarray,
    pv_cs: np.ndarray,
    elevation: np.ndarray,
    min_elevation: float = PV_SCALE_MIN_ELEVATION,
) -> float | None:
    """W PV-vermogen per W/m² op het paneel bij heldere hemel (≈ kWp × rendement).

    Een hoog percentiel en geen maximum: één wolkenrand die het paneel
    kort extra licht geeft ("cloud enhancement") mag de schaal niet bepalen.
    None zolang er te weinig momenten zijn met de zon boven ``min_elevation``.
    """
    pv = np.asarray(pv_w, dtype=float)
    cs = np.asarray(pv_cs, dtype=float)
    ok = (np.asarray(elevation) > min_elevation) & (cs > 200.0) & np.isfinite(pv) & (pv > 0)
    if ok.sum() < PV_SCALE_MIN_SAMPLES:
        return None
    scale = float(np.percentile(pv[ok] / cs[ok], PV_SCALE_PERCENTILE))
    return scale if scale > 0 else None


def ghi_from_pv(
    pv_w: np.ndarray,
    elevation: np.ndarray,
    azimuth: np.ndarray,
    e0: np.ndarray,
    pv_tilt: float,
    pv_azimuth: float,
    scale: float,
) -> np.ndarray:
    """Horizontale straling afgeleid uit PV-opbrengst, W/m².

    Helderheid = PV / (schaal × PV bij heldere hemel), maal de horizontale
    straling bij heldere hemel. NaN waar de PV onbekend is en de zon op staat;
    nul als de zon onder is, ook zonder meting (omvormers slapen 's nachts).
    """
    pv = np.asarray(pv_w, dtype=float)
    el = np.asarray(elevation, dtype=float)
    cs = pv_clear_sky(el, azimuth, e0, pv_tilt, pv_azimuth)
    with np.errstate(invalid="ignore", divide="ignore"):
        kc = np.where(cs > 20.0, pv / (scale * cs), 0.0)
    kc = np.clip(kc, 0.0, 1.15)
    ghi = kc * clearsky_ghi(el)
    ghi = np.where(el <= MIN_ELEVATION_DEG, 0.0, ghi)
    return np.where(np.isfinite(pv) | (el <= MIN_ELEVATION_DEG), ghi, np.nan)


#: Aangenomen helling van de panelen. Uit de data nauwelijks te bepalen, en
#: ook niet nodig: op het gemeten huis gaven 15–35° dezelfde uitkomst.
PV_TILT_ASSUMED = 30.0
#: Kandidaten voor de richting van de panelen (azimut, stappen van 15°).
PV_AZIMUTH_CANDIDATES = tuple(range(90, 271, 15))
#: Minimaal aantal (maand, uur)-vakken met genoeg zonnige uren voor een uitspraak.
PV_ORIENTATION_MIN_CELLS = 20
_CELL_MIN_HOURS = 8


def estimate_pv_azimuth(
    hour_start_ts: np.ndarray, pv_w: np.ndarray, lat: float, lon: float
) -> tuple[float | None, dict[int, float]]:
    """Richting van de panelen uit uurgemiddelden van de PV-opbrengst.

    Bij de juiste richting is PV / PV-bij-heldere-hemel op heldere momenten over
    de hele dag even groot. Per (maand, uur van de dag) wordt de heldere-hemel-
    omhullende genomen (95e percentiel), en de richting gekozen waarbij die
    omhullende het vlakst is. Op het gemeten huis gaf dit 180°, gelijk aan de
    opgave van de omvormer; met alleen een herfst aan data 210°.

    Retourneert ``(azimut of None, {kandidaat: spreiding})``. None met te weinig
    data; dan rekent de rest met 180°.
    """
    ts = np.asarray(hour_start_ts, dtype=float) + 1800.0
    pv = np.asarray(pv_w, dtype=float)
    el, az, e0 = sun_position(ts, lat, lon)
    month = ((ts // 86400 + 4) % 365.25 // 30.44).astype(int)   # ruwe maand, volstaat
    hour = ((ts % 86400) // 3600).astype(int)
    scores: dict[int, float] = {}
    for cand in PV_AZIMUTH_CANDIDATES:
        cs = pv_clear_sky(el, az, e0, PV_TILT_ASSUMED, cand)
        ok = (el > 10.0) & (cs > 100.0) & np.isfinite(pv) & (pv > 0)
        if ok.sum() < PV_ORIENTATION_MIN_CELLS * _CELL_MIN_HOURS:
            return None, {}
        r = pv[ok] / cs[ok]
        cell = month[ok] * 24 + hour[ok]
        env = []
        for c in np.unique(cell):
            sel = cell == c
            if sel.sum() >= _CELL_MIN_HOURS:
                env.append(np.percentile(r[sel], 95))
        if len(env) < PV_ORIENTATION_MIN_CELLS:
            return None, {}
        env = np.array(env)
        scores[cand] = round(float(np.std(env) / np.mean(env)), 4)
    best = min(scores, key=scores.get)
    return float(best), scores
