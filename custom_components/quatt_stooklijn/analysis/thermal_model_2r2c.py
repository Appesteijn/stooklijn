"""Tweetoestandsmodel (2R2C) van het huis.

Waarom een tweede toestand
--------------------------
Het 1R1C-model heeft één warmtecapaciteit en dus één tijdconstante. Een huis
heeft er minstens twee die orden uit elkaar liggen: de binnenlucht met de
inboedel reageert in uren, de bouwmassa in dagen. RLS moet daar één getal van
maken en kiest de traagste, want die domineert de residuen. Het gevolg is een
model dat een luchttemperatuur boven het setpunt aanziet voor opgeslagen
energie in de hele bouwmassa — en dus veel te lang durft te teren.

Let op (22-11-2026): op het gemeten huis gold dat níét voor de afkoeling bij
een stilstaande pomp. Het 1R1C met P_c-anker zat daar vier uur vooruit 0,04 K
naast; een vrij gefit 2R2C voorspelde nauwelijks afkoeling (+0,25 K). Pas met
ankers en extra gewicht op stookloze vensters (zie ``Anchors``) won het 2R2C
ook daar (rmse 0,13 tegen 0,20 K). Daarom toetst de modelvergelijking de
afkoeling apart voordat een afnemer het 2R2C gebruikt.

Gemeten op uurdata van één seizoen (november t/m mei), getraind op oneven
weken en geevalueerd op even weken, RMSE van de voorspelde binnentemperatuur
in K. "niets doen" is aannemen dat de binnentemperatuur niet verandert — een
referentie die je op korte horizon verrassend moeilijk verslaat:

    horizon  niets doen   1R1C   2R2C
      1 u      0,237     0,204  0,190
      3 u      0,363     0,296  0,252
      6 u      0,479     0,371  0,290
     12 u      0,581     0,452  0,321
     24 u      0,583     0,566  0,341

Het verschil groeit met de horizon. Dat is precies de handtekening van een
ontbrekende toestand en niet van overfitten: overfitten slaat juist op korte
horizon toe, en dit is bovendien gemeten op weken die niet in de fit zaten.

Waar de warmte binnenkomt
-------------------------
``alpha`` is het deel van de warmte dat rechtstreeks in de lucht landt; de
rest gaat eerst de bouwmassa in. Dat is bewust géén configuratievraag over
het afgiftesysteem. De verdeling loopt namelijk niet gelijk op met radiator
versus vloerverwarming: een paneelradiator geeft grofweg een derde van zijn
vermogen als straling rechtstreeks op muren en vloer af, en veel installaties
zijn gemengd. De bewoner kent zijn stralingsfractie niet, en een fout antwoord
is slechter dan geen antwoord — dus wordt alpha geleerd.

    alpha -> 1   vrijwel alles komt binnen op de snelle knoop
    alpha -> 0   vrijwel alles gaat eerst de trage massa in

Op het gemeten huis (radiatoren, één kleine vloerverwarmingslus) komt alpha
op 1,0 uit: alle warmte landt op de snelle knoop. Alpha op 0 forceren — de
topologie van een vloerverwarmingshuis — maakt de twaalfuursfout meetbaar
slechter, 0,343 tegen 0,321. Het model onderscheidt de twee dus echt.

Let op wat alpha níet is: een detector voor het afgiftesysteem. Alpha en Ci
ruilen tegen elkaar uit — met een te krappe bovengrens op Ci koopt de fit
dezelfde traagheid door alpha te verlagen. Lees alpha alleen samen met Ci,
en trek er geen conclusies uit over iemands installatie.

Wat dit model niet doet
-----------------------
Bij echte vloerverwarming is de dekvloer een derde lichaam, met een eigen
tijdconstante van grofweg 2 tot 6 uur, tussen het water en de lucht in. Dit
model propt dekvloer en gebouwschil in één knoop en kiest daar een compromis
voor. Beter dan één toestand, maar niet genoeg — een derde toestand vraagt
metingen op vijf minuten, en die verzamelt ``highres_log`` sinds kort.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

import numpy as np

_LOGGER = logging.getLogger(__name__)

#: Fysieke grenzen. Ze doen echt werk: zonder deze grenzen loopt de fit weg
#: naar capaciteiten van honderdduizenden Wh/K met een negatieve interne
#: warmtelast, die numeriek beter passen maar fysiek onzin zijn.
BOUNDS: dict[str, tuple[float, float]] = {
    "U": (80.0, 600.0),           # W/K   warmteverlies schil
    "Ci": (200.0, 30000.0),       # Wh/K  snelle knoop
    "Cm": (3000.0, 150000.0),     # Wh/K  trage knoop
    "Hm": (100.0, 30000.0),       # W/K   koppeling massa <-> lucht
    "g": (0.0, 20.0),             # W per W/m2 zonnewinst
    "P_int": (0.0, 2000.0),       # W     interne warmtelast
    "alpha": (0.0, 1.0),          # -     deel dat direct in de lucht landt
}
ORDER = ("U", "Ci", "Cm", "Hm", "g", "P_int", "alpha")

#: Startpunt voor de fit: een gemiddelde Nederlandse tussenwoning met
#: radiatoren. Alleen een vertrekpunt — de fit mag hier ver vandaan.
INITIAL = {"U": 250.0, "Ci": 1500.0, "Cm": 30000.0, "Hm": 2000.0,
           "g": 3.0, "P_int": 400.0, "alpha": 0.7}


@dataclass(frozen=True)
class TwoStateParams:
    """Fysische parameters van het tweetoestandsmodel."""

    #: Warmteverlies van de schil naar buiten, W/K.
    U: float
    #: Capaciteit van de snelle knoop, Wh/K. Dit is nadrukkelijk niet alleen
    #: de lucht — die is in de orde van 130 Wh/K. Op een gemeten huis komt er
    #: 15.000 uit, met een tijdconstante van vier uur: lucht plus alles wat
    #: snel meewarmt, zoals pleisterwerk, inboedel en de bovenste centimeters
    #: van vloer en wanden.
    Ci: float
    #: Capaciteit van de trage knoop, Wh/K: de bouwmassa en de schil.
    Cm: float
    Hm: float
    g: float
    P_int: float
    alpha: float

    def as_array(self) -> np.ndarray:
        return np.array([getattr(self, k) for k in ORDER], dtype=float)

    @classmethod
    def from_array(cls, v: np.ndarray) -> TwoStateParams:
        return cls(**{k: float(v[i]) for i, k in enumerate(ORDER)})

    @property
    def time_constants(self) -> tuple[float, float]:
        """De twee tijdconstantes in uren, snelste eerst."""
        eig = np.linalg.eigvals(_system_matrix(self))
        taus = sorted(-1.0 / np.real(eig))
        return float(taus[0]), float(taus[1])

    def to_dict(self) -> dict:
        d = {k: round(getattr(self, k), 4) for k in ORDER}
        fast, slow = self.time_constants
        d["tau_fast_h"] = round(fast, 2)
        d["tau_slow_h"] = round(slow, 1)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TwoStateParams:
        return cls(**{k: float(data[k]) for k in ORDER})


def _system_matrix(p: TwoStateParams) -> np.ndarray:
    """A uit dx/dt = A x + B u, met x = [T_binnen, T_massa]."""
    return np.array([
        [-(p.U + p.Hm) / p.Ci, p.Hm / p.Ci],
        [p.Hm / p.Cm, -p.Hm / p.Cm],
    ])


def _input_matrix(p: TwoStateParams) -> np.ndarray:
    """B, met u = [T_buiten, straling, warmtevermogen, 1]."""
    return np.array([
        [p.U / p.Ci, p.g / p.Ci, p.alpha / p.Ci, p.P_int / p.Ci],
        [0.0, 0.0, (1.0 - p.alpha) / p.Cm, 0.0],
    ])


def _expm_2x2(A: np.ndarray) -> np.ndarray:
    """Exacte matrix-exponent van een 2x2 via Sylvester.

    Niet via een reeksbenadering en niet via Euler. Euler is hier de kern van
    het probleem: bij uurstappen kan een expliciete Euler een tijdconstante
    korter dan de stap niet weergeven, en dan duwt de fit de capaciteiten
    omhoog tot alles weer traag genoeg is. Precies de fout die dit model moet
    oplossen.

    De eigenwaarden zijn hier altijd reëel: de discriminant is
    (a-d)^2 + 4bc met b en c positief. Het ontaarde geval kan dus niet
    optreden bij geldige parameters, maar wordt voor de zekerheid afgevangen.
    """
    a, b = A[0, 0], A[0, 1]
    c, d = A[1, 0], A[1, 1]
    disc = (a - d) ** 2 + 4.0 * b * c
    if disc <= 1e-12:
        # Ontaard: expm(A) = e^lam (I + (A - lam I))
        lam = 0.5 * (a + d)
        return float(np.exp(lam)) * (np.eye(2) + (A - lam * np.eye(2)))
    root = np.sqrt(disc)
    l1 = 0.5 * (a + d + root)
    l2 = 0.5 * (a + d - root)
    e1, e2 = np.exp(l1), np.exp(l2)
    return ((e1 - e2) * A + (l1 * e2 - l2 * e1) * np.eye(2)) / (l1 - l2)


def discretize(
    p: TwoStateParams, dt_hours: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Discretiseer naar stappen van ``dt_hours`` met zero-order hold.

    Ad = e^{A dt}, Bd = A^-1 (Ad - I) B. A is hier altijd inverteerbaar: beide
    eigenwaarden zijn strikt negatief zolang U, Hm, Ci en Cm positief zijn.

    De stapgrootte is vrij omdat het meetlogboek op vijf minuten bemonstert.
    Op uurdata is een tijdconstante van een paar uur nauwelijks te scheiden
    van de bemonsteringsstap; op vijfminutendata wel. Omdat dit een exacte
    discretisatie is en geen Euler-benadering, verandert de betekenis van de
    parameters niet als de stapgrootte verandert.
    """
    A = _system_matrix(p)
    Ad = _expm_2x2(A * dt_hours)
    Bd = np.linalg.solve(A, (Ad - np.eye(2)) @ _input_matrix(p))
    return Ad, Bd


def track_hidden_state(
    p: TwoStateParams,
    t_indoor: np.ndarray,
    t_outdoor: np.ndarray,
    q_solar: np.ndarray,
    q_hp: np.ndarray,
    dt_hours: float = 1.0,
) -> np.ndarray:
    """Reconstrueer de massatemperatuur uit gemeten binnentemperatuur.

    T_massa is niet gemeten. Het wordt vooruit gerekend met de gemeten
    binnentemperatuur als invoer, wat het equivalent is van een Kalman-filter
    waarin de binnentemperatuur als exact wordt aangenomen. De beginwaarde
    is de eerste binnentemperatuur; die keuze sterft uit met de trage
    tijdconstante, vandaar de inlooptijd bij fitten en evalueren.
    """
    Ad, Bd = discretize(p, dt_hours)
    # De invoer die niet van de toestand afhangt in één keer vectorieel; alleen
    # de terugkoppeling op tm zelf moet stap voor stap. Op vijfminutendata
    # loopt deze lus duizenden keren per kostenevaluatie, en Python-floats zijn
    # daar een factor tien sneller dan numpy-scalars.
    drive = (Ad[1, 0] * np.asarray(t_indoor, dtype=float)
             + Bd[1, 0] * np.asarray(t_outdoor, dtype=float)
             + Bd[1, 1] * np.asarray(q_solar, dtype=float)
             + Bd[1, 2] * np.asarray(q_hp, dtype=float)
             + Bd[1, 3]).tolist()
    a = float(Ad[1, 1])
    n = len(t_indoor)
    tm = [0.0] * n
    tm[0] = float(t_indoor[0])
    for k in range(n - 1):
        tm[k + 1] = drive[k] + a * tm[k]
    return np.array(tm)


def simulate(
    p: TwoStateParams,
    t_indoor0: float,
    t_mass0: float,
    t_outdoor: np.ndarray,
    q_solar: np.ndarray,
    q_hp: np.ndarray,
    dt_hours: float = 1.0,
) -> np.ndarray:
    """Vrijloop-simulatie: geen enkele gemeten binnentemperatuur onderweg.

    Let op de uitlijning: ``out[k]`` is de binnentemperatuur op tijdstip
    ``k+1``, want het is het resultaat van invoer ``u[k]``. De uitvoer is dus
    één stap opgeschoven ten opzichte van de invoerreeksen.
    """
    Ad, Bd = discretize(p, dt_hours)
    x = np.array([t_indoor0, t_mass0])
    out = np.empty(len(t_outdoor))
    for k in range(len(t_outdoor)):
        u = np.array([t_outdoor[k], q_solar[k], q_hp[k], 1.0])
        x = Ad @ x + Bd @ u
        out[k] = x[0]
    return out


def room_response(
    p: TwoStateParams, delta_w, dt_hours: float = 1.0
) -> np.ndarray:
    """Hoeveel de kamer afwijkt door een verandering in het vermogen.

    Het model is lineair, dus het verschil tussen twee plannen hangt alleen af
    van het verschil in vermogen; weer, zon en begintoestand vallen weg. Uit
    rust, ``out[k]`` na stap ``k``. Voor de driftgrens van de prijsverschuiving.
    """
    Ad, Bd = discretize(p, dt_hours)
    b = Bd[:, 2]
    x = np.zeros(2)
    out = np.empty(len(delta_w))
    for k, dq in enumerate(delta_w):
        x = Ad @ x + b * float(dq)
        out[k] = x[0]
    return out


class TwoStateStepper:
    """Het 2R2C achter de interface van het 1R1C (``predict_t_indoor``).

    Voor de uitlooptijd, die stap voor stap doorrekent vanaf zijn eigen
    vorige uitkomst. De massatemperatuur loopt hier mee; begin met de
    gereconstrueerde waarde uit het logboek (``shift_room_temp.start_state``).
    """

    def __init__(self, p: TwoStateParams, t_mass: float) -> None:
        self.p = p
        self.t_mass = float(t_mass)
        self._cache: dict[float, tuple[np.ndarray, np.ndarray]] = {}
        self.raw_params = p.to_dict()

    def predict_t_indoor(self, t_in, t_out, q_hp, q_solar, dt_hours) -> float:
        mats = self._cache.get(dt_hours)
        if mats is None:
            mats = self._cache[dt_hours] = discretize(self.p, dt_hours)
        Ad, Bd = mats
        x = Ad @ np.array([float(t_in), self.t_mass]) + Bd @ np.array(
            [float(t_out), float(q_solar), float(q_hp), 1.0])
        self.t_mass = float(x[1])
        return float(x[0])


# --------------------------------------------------------------------------- #
#  Fitten                                                                      #
# --------------------------------------------------------------------------- #
#
# Geen scipy in deze integratie, dus Levenberg-Marquardt met een numerieke
# Jacobiaan. Zeven parameters en een paar duizend residuen: dat is klein
# genoeg om in een executor-thread binnen een seconde of wat te draaien.
#
# De grenzen uit BOUNDS worden afgedwongen door een variabelentransformatie in
# plaats van door clippen. Clippen laat de optimalisator tegen een muur duwen
# en levert een schijnbaar convergerende fit op met parameters die op hun grens
# vastzitten; de transformatie maakt de grens onbereikbaar en het probleem
# onbegrensd, wat LM veel beter ligt.

_LM_MAX_ITER = 60
_LM_TOL = 1e-10

#: Uren voordat de aanname T_massa(0) = T_binnen(0) is uitgestorven.
BURN_IN_HOURS = 24.0

#: Hoe ver vooruit de fit zijn voorspelfout meet, ongeacht de stapgrootte.
#:
#: Een eenstapsfit werkt niet op vijfminutendata. De kamertemperatuur komt in
#: 0,01 K binnen maar flakkert binnen minuten ±0,015 K, terwijl hij tijdens
#: stoken zo'n 0,02 K per vijf minuten stijgt (gemeten oktober 2026): ruis en
#: signaal zijn per stap even groot. De eenstapsfit neemt die ruisende meting
#: bovendien als begintoestand van elke voorspelling, en het resultaat is een
#: snelle knoop die veel te snel lijkt.
#:
#: Gemeten op een synthetisch huis met τ_snel = 3,6 u, verwarmd in blokken
#: zoals de echte warmtepomp (een nachtblok, af en toe een korte run), met
#: 0,015 K witte meetruis:
#:
#:     fithorizon   τ_snel   Ci (echt 8000)   alpha (echt 0,90)
#:       5 min      1,1 u        3664              0,53
#:       1 u        3,0 u        7003              0,82
#:       6 u        3,5 u        7784              0,89
#:
#: Over een langere horizon middelt de ruis uit, terwijl de vijfminuteninvoer
#: erin blijft: wanneer binnen het uur de pomp liep, telt nog steeds mee. Zes
#: uur ligt boven de snelle tijdconstante van een gewoon huis en onder de
#: twaalf uur waarop getoetst wordt.
FIT_HORIZON_HOURS = 6.0

#: Om de hoeveel tijd een voorspelling in de fit begint. Startpunten vijf
#: minuten uit elkaar delen over een horizon van zes uur vrijwel alles; om het
#: kwartier geeft dezelfde fit met een derde van het geheugen. Bij een vol
#: logboek scheelt dat zo'n 30 MB aan Jacobiaan op een Raspberry Pi.
FIT_START_EVERY_HOURS = 0.25

Segment = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]


def steps_in(hours: float, dt_hours: float) -> int:
    """Reken een venster in uren om naar een aantal stappen.

    Elke drempel in dit bestand is natuurkunde, geen array-boekhouding: een
    inlooptijd van een dag blijft een dag, of je nu op uren of op vijf minuten
    bemonstert. Ze staan daarom in uren en worden hier omgerekend.

    Dit stond er niet altijd zo. De drempels waren rechtstreeks in stappen
    genoteerd met uren in hun naam, wat klopte zolang alles op uurdata draaide.
    Sinds ``dt_hours`` bestaat is dat een val: op vijfminutendata betekende een
    inlooptijd van 24 twee uur, werd de week-om-week-splitsing een splitsing per
    veertien uur, en zakte de eis van twee weken trainingsdata naar 28 uur. De
    poort die een slecht model moet tegenhouden werd zo twaalf keer zwakker,
    zonder dat er iets aan te zien was.
    """
    return max(1, int(round(hours / dt_hours)))


def _to_free(values: np.ndarray) -> np.ndarray:
    """Fysieke parameters -> onbegrensde optimalisatieruimte."""
    out = np.empty(len(ORDER))
    for i, key in enumerate(ORDER):
        lo, hi = BOUNDS[key]
        frac = np.clip((values[i] - lo) / (hi - lo), 1e-6, 1 - 1e-6)
        out[i] = np.log(frac / (1.0 - frac))
    return out


def _to_bounded(free: np.ndarray) -> np.ndarray:
    """Onbegrensde optimalisatieruimte -> fysieke parameters."""
    out = np.empty(len(ORDER))
    for i, key in enumerate(ORDER):
        lo, hi = BOUNDS[key]
        out[i] = lo + (hi - lo) / (1.0 + np.exp(-np.clip(free[i], -40, 40)))
    return out


def one_step_residuals(
    p: TwoStateParams,
    segments: list[Segment],
    burn_in: int | None = None,
    dt_hours: float = 1.0,
) -> np.ndarray:
    """Fout in de voorspelling van de binnentemperatuur, één stap vooruit.

    ``burn_in`` is een aantal stappen; laat hem weg voor de inlooptijd van
    ``BURN_IN_HOURS``, omgerekend naar de gebruikte stapgrootte.
    """
    return multi_step_residuals(p, segments, 1, burn_in, dt_hours)


def multi_step_residuals(
    p: TwoStateParams,
    segments: list[Segment],
    steps: int,
    burn_in: int | None = None,
    dt_hours: float = 1.0,
    start_every: int = 1,
) -> np.ndarray:
    """Fout in de voorspelling van de binnentemperatuur, 1 t/m ``steps`` stappen
    vooruit, vanaf elk ``start_every``-de tijdstip na de inlooptijd.

    Elke voorspelling begint bij de gemeten binnentemperatuur en de
    gereconstrueerde massatemperatuur, en rekent daarna vrij door op de echte
    invoer. Alle startpunten lopen tegelijk, als één matrix: de lus gaat over
    de horizon, niet over de tijdreeks.
    """
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    Ad, Bd = discretize(p, dt_hours)
    parts = []
    for ti, to, sol, q in segments:
        n = len(ti)
        if n <= burn_in + steps:
            continue
        tm = track_hidden_state(p, ti, to, sol, q, dt_hours)
        u = np.column_stack([to, sol, q, np.ones(n)])
        starts = np.arange(burn_in, n - steps, start_every)
        x = np.column_stack([ti[starts], tm[starts]])
        for j in range(steps):
            x = x @ Ad.T + u[starts + j] @ Bd.T
            parts.append(x[:, 0] - ti[starts + j + 1])
    if not parts:
        return np.zeros(0)
    return np.concatenate(parts)


# --------------------------------------------------------------------------- #
#  Ankers en afkoeling (v0.10.7-beta.9)                                        #
# --------------------------------------------------------------------------- #
#
# Vrij gefit ruilen U, P_int en g tegen elkaar uit, en de opslag (Ci, Cm) is
# niet te bepalen: op het gemeten huis gaf een andere splitsing van dezelfde
# data een factor 2–3 andere Ci en Cm. Nagespeeld op het logboek van 08-09 t/m
# 22-11-2026 (scratchpad ``mpc/anker.py``):
#
# - U vast op de warmteverliesanalyse kost niets (12 u: 0,262 tegen 0,266 K).
# - P_int als constante vast op U·(kamer − balanspunt) kost 7–16 %, want het
#   balanspunt bevat de gemiddelde zon al en het model telt de zon apart.
#   Daarom staat de *totale* gratis warmte vast: P_int + g·(gemiddelde zon).
# - In november staat de warmtepomp geen uur uit. De afkoeling bij een
#   stilstaande pomp leert het model alleen uit september en oktober, en die
#   uren vallen in de minderheid. Zonder extra gewicht voorspelde het vrij
#   gefitte model ~0,01 K/u afkoeling tegen 0,062 gemeten; het 1R1C zat er
#   0,04 K per vier uur naast. Met ``COOLING_WEIGHT`` 0,5: −0,046 K/u, bij
#   gelijke fout na twaalf uur.

#: Een venster telt als "uit" als de warmte en de zon het hele venster onder
#: deze waarden blijven.
OFF_MAX_HEAT_W = 50.0
OFF_MAX_SUN_WM2 = 5.0
#: Lengte van zo'n venster.
COOLING_HOURS = 4.0
#: Aandeel van de kostfunctie dat de stookloze vensters krijgen bij gelijke
#: fout per residu. 0 is de oude fit.
COOLING_WEIGHT = 0.5


@dataclass(frozen=True)
class Anchors:
    """Wat de warmteverliesanalyse al weet: U, en de totale gratis warmte.

    ``p_free_w`` is U × (kamer − balanspunt), net als het anker van het 1R1C;
    ``sun_mean_wm2`` de gemiddelde zon over dezelfde periode. In de fit wordt
    P_int dan ``p_free_w − g × sun_mean_wm2`` (niet negatief).
    """

    u: float
    p_free_w: float
    sun_mean_wm2: float

    def to_dict(self) -> dict:
        return {"U": round(self.u, 1), "gratis_warmte_w": round(self.p_free_w),
                "zon_gemiddeld_wm2": round(self.sun_mean_wm2, 1)}


def off_windows(
    segments: list[Segment],
    dt_hours: float,
    hours: float = COOLING_HOURS,
    burn_in: int | None = None,
) -> list[tuple[int, int]]:
    """(segment, startindex) van niet-overlappende vensters van ``hours`` lang
    zonder warmte en zonder zon, na de inlooptijd."""
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    n = steps_in(hours, dt_hours)
    out = []
    for si, (ti, _to, sol, q) in enumerate(segments):
        k = burn_in
        while k < len(ti) - n:
            if (np.max(q[k:k + n]) < OFF_MAX_HEAT_W
                    and np.max(sol[k:k + n]) < OFF_MAX_SUN_WM2):
                out.append((si, k))
                k += n
            else:
                k += 1
    return out


def cooling_errors(
    p: TwoStateParams,
    segments: list[Segment],
    windows: list[tuple[int, int]],
    dt_hours: float,
    hours: float = COOLING_HOURS,
) -> np.ndarray:
    """Voorspeld min gemeten aan het eind van elk stookloos venster."""
    if not windows:
        return np.zeros(0)
    n = steps_in(hours, dt_hours)
    Ad, Bd = discretize(p, dt_hours)
    tm_cache: dict[int, np.ndarray] = {}
    errs = []
    for si, k in windows:
        ti, to, sol, q = segments[si]
        if si not in tm_cache:
            tm_cache[si] = track_hidden_state(p, ti, to, sol, q, dt_hours)
        x = np.array([ti[k], tm_cache[si][k]])
        for j in range(n):
            x = Ad @ x + Bd @ np.array([to[k + j], sol[k + j], q[k + j], 1.0])
        errs.append(x[0] - ti[k + n])
    return np.array(errs)


def _summary(errs: np.ndarray, measured: np.ndarray) -> dict:
    return {
        "n": int(len(errs)),
        "gemeten_k": round(float(np.mean(measured)), 3),
        "fout_gem_k": round(float(np.mean(errs)), 3),
        "fout_rmse_k": round(float(np.sqrt(np.mean(errs ** 2))), 3),
    }


def cooling_check(
    p: TwoStateParams,
    segments: list[Segment],
    dt_hours: float = 1.0,
    hours: float = COOLING_HOURS,
    reference=None,
) -> dict:
    """Hoe goed het model de afkoeling bij een stilstaande pomp voorspelt.

    ``reference`` is optioneel het 1R1C (``predict_t_indoor``), op dezelfde
    vensters. Geeft ``{"n": 0}`` zonder vensters.
    """
    windows = off_windows(segments, dt_hours, hours)
    if not windows:
        return {"n": 0}
    n = steps_in(hours, dt_hours)
    measured = np.array([segments[si][0][k + n] - segments[si][0][k] for si, k in windows])
    out = {"uren": hours, "model": _summary(cooling_errors(p, segments, windows, dt_hours, hours), measured)}
    out["n"] = out["model"]["n"]
    if reference is not None:
        errs = []
        for si, k in windows:
            ti, to, sol, q = segments[si]
            t = float(ti[k])
            for j in range(n):
                t = reference.predict_t_indoor(t, float(to[k + j]), float(q[k + j]),
                                               float(sol[k + j]), dt_hours)
            errs.append(t - ti[k + n])
        out["referentie"] = _summary(np.array(errs), measured)
    return out


def fit(
    segments: list[Segment],
    initial: dict | None = None,
    burn_in: int | None = None,
    max_iter: int = _LM_MAX_ITER,
    dt_hours: float = 1.0,
    horizon_steps: int = 1,
    start_every: int = 1,
    anchors: Anchors | None = None,
    cooling_weight: float = 0.0,
) -> tuple[TwoStateParams, dict]:
    """Fit de zeven parameters op voorspelfout over ``horizon_steps`` stappen.

    Met ``anchors`` staan U en de totale gratis warmte vast (zie ``Anchors``)
    en leert de fit er vijf. ``cooling_weight`` voegt de stookloze vensters toe
    met dat aandeel in de kostfunctie (``COOLING_WEIGHT``).

    Standaard één stap, vanaf elk tijdstip. Op vijfminutendata hoort hier
    ``steps_in(FIT_HORIZON_HOURS, dt_hours)`` te staan; zie die constante.

    Geeft de parameters plus een verslag: aantal iteraties, RMS, en of de fit
    daadwerkelijk convergeerde. Dat verslag hoort meegewogen te worden — een
    niet-convergeerde fit hoort niet in productie.
    """
    start = dict(INITIAL)
    if initial:
        start.update(initial)
    if anchors is not None:
        start["U"] = anchors.u
    full = _to_free(np.array([start[k] for k in ORDER], dtype=float))
    i_u, i_g, i_p = ORDER.index("U"), ORDER.index("g"), ORDER.index("P_int")
    free_idx = [i for i in range(len(ORDER))
                if anchors is None or i not in (i_u, i_p)]
    windows = off_windows(segments, dt_hours, burn_in=burn_in) if cooling_weight > 0 else []

    def physical(free: np.ndarray) -> np.ndarray:
        z = full.copy()
        z[free_idx] = free
        v = _to_bounded(z)
        if anchors is not None:
            v[i_u] = anchors.u
            lo, hi = BOUNDS["P_int"]
            v[i_p] = min(hi, max(lo, anchors.p_free_w - v[i_g] * anchors.sun_mean_wm2))
        return v

    def cost_at(free: np.ndarray) -> tuple[np.ndarray, float]:
        p = TwoStateParams.from_array(physical(free))
        r = multi_step_residuals(p, segments, horizon_steps, burn_in, dt_hours,
                                 start_every)
        if windows and r.size:
            # Gewogen zodat de vensters samen ``cooling_weight`` van de kosten
            # dragen als hun fout per residu even groot is als die van de rest.
            scale = np.sqrt(cooling_weight * r.size / len(windows))
            r = np.concatenate([r, scale * cooling_errors(p, segments, windows, dt_hours)])
        return r, float(r @ r)

    x = full[free_idx].copy()

    r, cost = cost_at(x)
    if r.size == 0:
        raise ValueError("geen bruikbare segmenten om op te fitten")

    lam = 1e-3
    iterations = 0
    rel = float("inf")
    # Standaard aannemen dat de lus is uitgelopen; alleen een echte
    # stopreden overschrijft dat. Andersom — "converged" op de laatste
    # gelukte stap zetten — meldt een fit die tegen de iteratielimiet aan
    # loopt als geconvergeerd, en dan laat de validatiepoort hem door.
    stop_reason = "iteratielimiet"
    for iterations in range(1, max_iter + 1):
        # Numerieke Jacobiaan met voorwaartse differenties. De stap is
        # relatief aan de schaal van de vrije variabele, niet absoluut:
        # in de getransformeerde ruimte lopen de parameters ver uiteen.
        J = np.empty((r.size, len(x)))
        for i in range(len(x)):
            step = 1e-5 * max(1.0, abs(x[i]))
            xp = x.copy()
            xp[i] += step
            J[:, i] = (cost_at(xp)[0] - r) / step

        JTJ = J.T @ J
        JTr = J.T @ r
        improved = False
        for _ in range(30):
            damped = JTJ + lam * np.diag(np.maximum(np.diag(JTJ), 1e-12))
            try:
                delta = np.linalg.solve(damped, -JTr)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            r_new, cost_new = cost_at(x + delta)
            if cost_new < cost:
                x = x + delta
                rel = (cost - cost_new) / max(cost, 1e-300)
                r, cost = r_new, cost_new
                lam = max(lam / 3.0, 1e-12)
                improved = True
                break
            lam *= 3.0
            if lam > 1e12:
                break
        if not improved:
            stop_reason = "geen verbetering meer"
            break
        if rel < _LM_TOL:
            stop_reason = "tolerantie bereikt"
            break

    params = TwoStateParams.from_array(physical(x))
    report = {
        "iterations": iterations,
        "converged": stop_reason != "iteratielimiet",
        "stop_reason": stop_reason,
        "rms_k": float(np.sqrt(cost / max(r.size, 1))),
        "n_residuals": int(r.size),
        "dt_hours": dt_hours,
        "horizon_steps": horizon_steps,
        "geankerd": anchors is not None,
        "afkoelvensters": len(windows),
    }
    return params, report


def rmse_at_horizon(
    p: TwoStateParams,
    segments: list[Segment],
    horizon: int,
    burn_in: int | None = None,
    dt_hours: float = 1.0,
) -> float | None:
    """RMSE van een vrijloop-voorspelling over ``horizon`` stappen.

    ``horizon`` telt stappen, geen uren — op vijfminutendata is twaalf uur
    vooruit dus ``horizon=144``. Reken hem om met ``steps_in()``.

    Dit is de maat die telt. Eenstapsfout zegt weinig: daar wint bijna elk
    model van 'aannemen dat er niets verandert'. Pas over uren loopt het
    verschil tussen modelstructuren op.
    """
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    errs = []
    for ti, to, sol, q in segments:
        if len(ti) <= burn_in + horizon + 1:
            continue
        tm = track_hidden_state(p, ti, to, sol, q, dt_hours)
        for k in range(burn_in, len(ti) - horizon):
            pred = simulate(p, ti[k], tm[k], to[k:k + horizon],
                            sol[k:k + horizon], q[k:k + horizon], dt_hours)
            errs.append(pred - ti[k + 1:k + 1 + horizon])
    if not errs:
        return None
    return float(np.sqrt((np.concatenate(errs) ** 2).mean()))


# --------------------------------------------------------------------------- #
#  Validatie                                                                   #
# --------------------------------------------------------------------------- #
#
# Een beter model op papier is niet genoeg. Deze poort bestaat omdat een fit
# op te weinig of te eenzijdige data er prima uit kan zien en toch slechter
# voorspelt dan het model dat er al staat.

#: Hoeveel beter het nieuwe model moet zijn voordat het het overneemt. Zonder
#: marge wisselt de integratie heen en weer op ruis.
DEFAULT_MARGIN = 0.05

#: Blokgrootte voor de train/test-splitsing. Een week om en om, zodat koude en
#: zachte periodes in beide helften zitten. Een chronologische knip zou het
#: model op de winter trainen en op het voorjaar beoordelen, en dat meet iets
#: anders dan wat je wil weten.
SPLIT_BLOCK_HOURS = 168.0

#: Kortste stuk dat nog als blok meetelt. Korter dan dit past er geen
#: inlooptijd plus een horizon in.
MIN_BLOCK_HOURS = 72.0

#: Minimale hoeveelheid trainingsdata. Twee weken is niet veel voor zeven
#: parameters, maar het is de ondergrens waaronder een fit niets betekent.
MIN_TRAIN_HOURS = 336.0


@dataclass(frozen=True)
class Validation:
    """Uitkomst van fitten plus toetsen op weggehouden data."""

    params: TwoStateParams | None
    report: dict
    rmse: float | None
    reference_rmse: float | None
    persistence_rmse: float | None
    accepted: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "rmse_k": round(self.rmse, 4) if self.rmse is not None else None,
            "reference_rmse_k": (
                round(self.reference_rmse, 4)
                if self.reference_rmse is not None else None
            ),
            "persistence_rmse_k": (
                round(self.persistence_rmse, 4)
                if self.persistence_rmse is not None else None
            ),
            "fit": self.report,
            "params": self.params.to_dict() if self.params else None,
        }


def split_alternating(
    segments: list[Segment],
    block: int | None = None,
    min_len: int | None = None,
    dt_hours: float = 1.0,
) -> tuple[list[Segment], list[Segment]]:
    """Splits in om-en-om blokken: even blokken train, oneven test.

    ``block`` en ``min_len`` tellen stappen; laat ze weg voor de vensters uit
    ``SPLIT_BLOCK_HOURS`` en ``MIN_BLOCK_HOURS`` bij deze stapgrootte.

    De blokteller loopt door over segmentgrenzen heen. Zou hij per segment
    opnieuw beginnen, dan belandt een reeks segmenten die elk korter zijn dan
    één blok allemaal in train en blijft de testverzameling leeg — precies wat
    er gebeurt bij data met veel onderbrekingen.
    """
    if block is None:
        block = steps_in(SPLIT_BLOCK_HOURS, dt_hours)
    if min_len is None:
        min_len = steps_in(MIN_BLOCK_HOURS, dt_hours)
    train: list[Segment] = []
    test: list[Segment] = []
    index = 0
    for seg in segments:
        n = len(seg[0])
        for start in range(0, n, block):
            piece = tuple(arr[start:start + block] for arr in seg)
            if len(piece[0]) < min_len:
                continue
            (train if index % 2 == 0 else test).append(piece)
            index += 1
    return train, test


def persistence_rmse(
    segments: list[Segment],
    horizon: int,
    burn_in: int | None = None,
    dt_hours: float = 1.0,
) -> float | None:
    """Referentie: aannemen dat de binnentemperatuur niet verandert.

    Verrassend sterk op korte horizon, en daarom de eerlijke ondergrens. Een
    model dat hier niet overheen komt, voegt niets toe.
    """
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    errs = []
    for ti, _to, _sol, _q in segments:
        for k in range(burn_in, len(ti) - horizon):
            errs.append(np.full(horizon, ti[k]) - ti[k + 1:k + 1 + horizon])
    if not errs:
        return None
    return float(np.sqrt((np.concatenate(errs) ** 2).mean()))


def fit_and_validate(
    segments: list[Segment],
    horizon: int,
    reference_rmse: float | None = None,
    margin: float = DEFAULT_MARGIN,
    burn_in: int | None = None,
    dt_hours: float = 1.0,
    min_train_steps: int | None = None,
    max_iter: int = _LM_MAX_ITER,
) -> Validation:
    """Fit op de helft van de data en toets op de andere helft.

    ``reference_rmse`` is de fout van het model dat er nu staat, gemeten op
    dezelfde testverzameling en dezelfde horizon. Laat hem weg en alleen de
    persistentiedrempel geldt.

    ``horizon``, ``burn_in`` en ``min_train_steps`` tellen stappen. De
    weggelaten waarden volgen de vensters in uren uit dit bestand, omgerekend
    naar ``dt_hours``; ``horizon`` heeft geen standaard en moet dus zelf al
    omgerekend zijn.
    """
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    if min_train_steps is None:
        min_train_steps = steps_in(MIN_TRAIN_HOURS, dt_hours)
    train, test = split_alternating(segments, dt_hours=dt_hours)
    return fit_and_score(
        train, test, horizon, reference_rmse=reference_rmse, margin=margin,
        fit_dt_hours=dt_hours, test_dt_hours=dt_hours, max_iter=max_iter,
        min_train_hours=min_train_steps * dt_hours,
        fit_burn_in=burn_in, test_burn_in=burn_in,
    )


def fit_and_score(
    train: list[Segment],
    test: list[Segment],
    horizon: int,
    reference_rmse: float | None = None,
    margin: float = DEFAULT_MARGIN,
    fit_dt_hours: float = 1.0,
    test_dt_hours: float = 1.0,
    max_iter: int = _LM_MAX_ITER,
    min_train_hours: float = MIN_TRAIN_HOURS,
    fit_burn_in: int | None = None,
    test_burn_in: int | None = None,
    anchors: Anchors | None = None,
    cooling_weight: float = 0.0,
) -> Validation:
    """Fit op ``train`` en toets op ``test``, elk op een eigen stapgrootte.

    Het model mag fijner leren dan het getoetst wordt. Dat is de bedoeling:
    leren op vijf minuten, zodat de timing van het vermogen binnen het uur
    meetelt, en toetsen per uur, zoals de MPC het model gebruikt en zoals het
    huidige model gescoord wordt. Omdat de discretisatie exact is, betekenen de
    parameters op elke stap hetzelfde.

    ``horizon`` telt stappen van ``test_dt_hours``.
    """
    if fit_burn_in is None:
        fit_burn_in = steps_in(BURN_IN_HOURS, fit_dt_hours)
    if test_burn_in is None:
        test_burn_in = steps_in(BURN_IN_HOURS, test_dt_hours)

    # Vóór de poort, niet erna: de ondergrens hangt alleen van de testdata af,
    # en zolang er te weinig trainingsdata is, is dit het enige getal waar
    # het huidige model tegen af te zetten valt.
    persist = (persistence_rmse(test, horizon, test_burn_in, test_dt_hours)
               if test else None)

    def _no(reason: str, **kw) -> Validation:
        return Validation(params=None, report=kw.pop("report", {}), rmse=None,
                          reference_rmse=reference_rmse, persistence_rmse=persist,
                          accepted=False, reason=reason)

    # Kleine marge tegen afronding: 336 uur aan vijfminutenstappen is
    # 4032 stappen, en 4032 * (1/12) is in floats niet precies 336.
    if sum(len(s[0]) for s in train) * fit_dt_hours < min_train_hours - 1e-6:
        return _no("te weinig trainingsdata")
    if not test:
        return _no("te weinig testdata")

    try:
        params, report = fit(
            train, burn_in=fit_burn_in, dt_hours=fit_dt_hours, max_iter=max_iter,
            horizon_steps=steps_in(FIT_HORIZON_HOURS, fit_dt_hours),
            start_every=steps_in(FIT_START_EVERY_HOURS, fit_dt_hours),
            anchors=anchors, cooling_weight=cooling_weight,
        )
    except (ValueError, np.linalg.LinAlgError) as err:
        return _no(f"fit mislukt: {err}")
    if not report.get("converged"):
        return _no("fit convergeerde niet", report=report)

    rmse = rmse_at_horizon(params, test, horizon, test_burn_in, test_dt_hours)
    if rmse is None:
        return _no("geen bruikbare testvensters", report=report)

    def _verdict() -> tuple[bool, str]:
        # ``nan >= x`` is False: zonder deze regel las een NaN-fout als
        # "beter op weggehouden data" (analyse 21-11-2026).
        if not np.isfinite(rmse):
            return False, "geen bruikbare testvensters"
        if persist is not None and rmse >= persist * (1.0 - margin):
            return False, "niet beter dan aannemen dat er niets verandert"
        if reference_rmse is not None and rmse >= reference_rmse * (1.0 - margin):
            return False, "niet genoeg beter dan het huidige model"
        return True, "beter op weggehouden data"

    accepted, reason = _verdict()
    return Validation(params=params, report=report, rmse=rmse,
                      reference_rmse=reference_rmse, persistence_rmse=persist,
                      accepted=accepted, reason=reason)
