"""Tweetoestandsmodel (2R2C) van het huis.

Waarom een tweede toestand
--------------------------
Het 1R1C-model heeft één warmtecapaciteit en dus één tijdconstante. Een huis
heeft er minstens twee die orden uit elkaar liggen: de binnenlucht met de
inboedel reageert in uren, de bouwmassa in dagen. RLS moet daar één getal van
maken en kiest de traagste, want die domineert de residuen. Het gevolg is een
model dat een luchttemperatuur boven het setpunt aanziet voor opgeslagen
energie in de hele bouwmassa — en dus veel te lang durft te teren.

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
    n = len(t_indoor)
    tm = np.empty(n)
    tm[0] = t_indoor[0]
    for k in range(n - 1):
        u = (t_outdoor[k], q_solar[k], q_hp[k], 1.0)
        tm[k + 1] = Ad[1, 0] * t_indoor[k] + Ad[1, 1] * tm[k] + (
            Bd[1, 0] * u[0] + Bd[1, 1] * u[1] + Bd[1, 2] * u[2] + Bd[1, 3] * u[3]
        )
    return tm


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
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    Ad, Bd = discretize(p, dt_hours)
    parts = []
    for ti, to, sol, q in segments:
        if len(ti) <= burn_in + 1:
            continue
        tm = track_hidden_state(p, ti, to, sol, q, dt_hours)
        u = np.column_stack([to, sol, q, np.ones(len(ti))])
        pred = Ad[0, 0] * ti[:-1] + Ad[0, 1] * tm[:-1] + u[:-1] @ Bd[0]
        parts.append(pred[burn_in:] - ti[1:][burn_in:])
    if not parts:
        return np.zeros(0)
    return np.concatenate(parts)


def fit(
    segments: list[Segment],
    initial: dict | None = None,
    burn_in: int | None = None,
    max_iter: int = _LM_MAX_ITER,
    dt_hours: float = 1.0,
) -> tuple[TwoStateParams, dict]:
    """Fit de zeven parameters op eenstaps-voorspelfout.

    Geeft de parameters plus een verslag: aantal iteraties, RMS, en of de fit
    daadwerkelijk convergeerde. Dat verslag hoort meegewogen te worden — een
    niet-convergeerde fit hoort niet in productie.
    """
    start = dict(INITIAL)
    if initial:
        start.update(initial)
    x = _to_free(np.array([start[k] for k in ORDER], dtype=float))

    def cost_at(free: np.ndarray) -> tuple[np.ndarray, float]:
        r = one_step_residuals(TwoStateParams.from_array(_to_bounded(free)),
                               segments, burn_in, dt_hours)
        return r, float(r @ r)

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
        J = np.empty((r.size, len(ORDER)))
        for i in range(len(ORDER)):
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

    params = TwoStateParams.from_array(_to_bounded(x))
    report = {
        "iterations": iterations,
        "converged": stop_reason != "iteratielimiet",
        "stop_reason": stop_reason,
        "rms_k": float(np.sqrt(cost / max(r.size, 1))),
        "n_residuals": int(r.size),
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
    def _no(reason: str, **kw) -> Validation:
        return Validation(params=None, report=kw.pop("report", {}), rmse=None,
                          reference_rmse=reference_rmse, persistence_rmse=None,
                          accepted=False, reason=reason)

    train, test = split_alternating(segments, dt_hours=dt_hours)
    if sum(len(s[0]) for s in train) < min_train_steps:
        return _no("te weinig trainingsdata")
    if not test:
        return _no("te weinig testdata")

    try:
        params, report = fit(train, burn_in=burn_in, dt_hours=dt_hours,
                             max_iter=max_iter)
    except (ValueError, np.linalg.LinAlgError) as err:
        return _no(f"fit mislukt: {err}")
    if not report.get("converged"):
        return _no("fit convergeerde niet", report=report)

    rmse = rmse_at_horizon(params, test, horizon, burn_in, dt_hours)
    persist = persistence_rmse(test, horizon, burn_in, dt_hours)
    if rmse is None:
        return _no("geen bruikbare testvensters", report=report)

    def _verdict() -> tuple[bool, str]:
        if persist is not None and rmse >= persist * (1.0 - margin):
            return False, "niet beter dan aannemen dat er niets verandert"
        if reference_rmse is not None and rmse >= reference_rmse * (1.0 - margin):
            return False, "niet genoeg beter dan het huidige model"
        return True, "beter op weggehouden data"

    accepted, reason = _verdict()
    return Validation(params=params, report=report, rmse=rmse,
                      reference_rmse=reference_rmse, persistence_rmse=persist,
                      accepted=accepted, reason=reason)
