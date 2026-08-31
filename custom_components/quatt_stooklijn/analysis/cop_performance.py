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

Twee dingen maakten de maat eerder onbruikbaar voor het enige doel dat ze heeft
— "heeft mijn aanpassing geholpen?" — en die zijn hier opgelost:

**De norm at zijn eigen verbetering op.** De curve werd gebouwd uit álle
stookdagen, dus ook uit de dagen ná de aanpassing. Die schuiven de mediaan in hun
bin mee omhoog, en na één seizoen leest een echte verbetering weer als 1,00. De
norm wordt nu *bevroren*: de beoordeelde dagen zitten er niet in. Zie
``_split_norm``.

**De seizoensrand zat in de norm.** 12 °C in november is iets anders dan 12 °C in
mei: in mei is het huis al warm, helpt de zon mee en draait de pomp in korte runs.
Dat kost rendement. Eén bin die beide door elkaar middelt, zet voorjaarsdagen
structureel onder 1 zonder dat er iets mis is — een bias die alleen met een
alinea disclaimer te repareren viel. De bins zijn nu per seizoenshelft, zodat
najaar tegen najaar en voorjaar tegen voorjaar wordt afgezet en het getal zegt
wat het belooft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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

# De twee seizoenshelften. De grens ligt op 1 augustus: daarvoor loopt het
# stookseizoen af (huis warm, zon hoog, korte runs), daarna bouwt het weer op.
# Deep-winterdagen vallen aan weerszijden van 1 januari in verschillende helften,
# maar die hebben allebei ruim genoeg dagen voor een eigen bin, dus dat kost
# alleen wat bins en introduceert geen scheefheid.
SEASON_AUTUMN = "najaar"
SEASON_SPRING = "voorjaar"
AUTUMN_START_MONTH = 8

# Het venster waarover beoordeeld wordt als er geen wijzigingsdatum is gezet,
# geteld in stookdagen. Deze dagen zitten niet in de norm, zodat een verandering
# er niet in wegzakt terwijl je ernaar kijkt.
DEFAULT_EVAL_WINDOW_DAYS = 30

# Hoeveel beoordeelde dagen de bevroren norm minimaal moet kunnen scoren.
#
# Bewust een aantal en geen percentage. Aan de staart van het stookseizoen liggen
# de zachtste dagen per definitie ín het beoordelingsvenster en dus niet in de
# norm; die dagen krijgen geen oordeel, en dat hoort ook. Op een percentage zou de
# bevriezing daar precies uitvallen wanneer ze het meest nodig is — gemeten op
# 226 stookdagen viel ze in mei terug op de zelfrefererende norm, wat als een
# defect leest. Waar het om gaat is of er genoeg dagen overblijven om iets te
# zeggen, niet welk deel van het venster gedekt is.

# Harde ondergrens voor een uitspraak over vóór/ná, in stookdagen.
#
# De tweesteekproefstoets hieronder rekent met de spreiding van de
# vóór-periode, en op een rustige reeks komt die zo laag uit dat twee dagen al
# "significant" heten. Dat is een eigenschap van de toets, niet van de
# werkelijkheid: twee opeenvolgende stookdagen delen hun weer, hun zonuren en
# hun bewonersgedrag, dus ze zijn geen twee onafhankelijke metingen. Onder een
# week hoort er geen conclusie te staan, hoe groot de uitslag ook is.
MIN_EVAL_DAYS_FOR_VERDICT = 7

# Hoe ver twee periodes in de kalender uit elkaar mogen liggen voor een uitspraak
# over vóór/ná, in dagen.
#
# De seizoenshelft haalt de grofste scheefheid eruit — een oktoberdag van 12 °C
# tegen een aprildag van 12 °C scheelde op deze installatie 12 tot 21% aan norm —
# maar bínnen een helft loopt hetzelfde effect door: bij 10 °C in februari vraagt
# het huis nog warmte en draait de pomp lang, bij 10 °C in mei niet. Een aanpassing
# van 1 maart afzetten tegen de winter ervoor levert daarom een verschil dat voor
# een deel het seizoen is. Zonder overlap in seizoenspositie hoort er dus wel een
# getal te staan, maar geen conclusie.
SEASON_MATCH_DAYS = 30


@dataclass
class PeriodStats:
    """Gemiddelde verhouding over een aaneengesloten reeks stookdagen."""

    days: int = 0
    mean: float | None = None
    date_from: str | None = None
    date_to: str | None = None


@dataclass
class CopPerformanceResult:
    """Referentiecurve plus de afwijking per dag."""

    # Seizoenshelft → {bin_middelpunt (°C): mediane COP van de stookdagen daarin}.
    reference: dict[str, dict[float, float]] = field(default_factory=dict)
    # Per stookdag: datum, seizoenshelft, buitentemp, gemeten COP, verwachte
    # COP, verhouding.
    daily: list[dict] = field(default_factory=list)
    # De laatst beoordeelde stookdag. Alleen de datum: de verhouding van één dag
    # heeft ~12% spreiding en suggereert als los getal een trend die er niet is.
    # De datum is er wél, want zonder die is niet te zien hoe oud de reeks is —
    # buiten het stookseizoen staat alles hier maandenlang stil.
    latest_date: str | None = None
    # Waar een wijziging op beoordeeld wordt: één dag is te ruis-gevoelig.
    rolling_30d: float | None = None
    # Hoeveel stookdagen de referentiecurve dragen.
    reference_days: int = 0

    # ── De scheiding tussen norm en beoordeling ──
    # False betekent dat de beoordeelde dagen zelf in de norm zitten en een
    # verbetering dus deels tegen zichzelf wordt afgezet. Alleen zo bij te weinig
    # historie; het dashboard hoort dat te melden.
    norm_frozen: bool = False
    # De grens: dagen ervóór dragen de norm, dagen erna worden beoordeeld.
    baseline_date: str | None = None
    # De grens is door de gebruiker gezet (een gemarkeerde aanpassing) in plaats
    # van afgeleid uit het venster.
    baseline_explicit: bool = False
    # Wat de gebruiker vroeg, ook als het niet gehonoreerd kon worden. Zonder dit
    # verdwijnt een genegeerde wijzigingsdatum stilzwijgend.
    baseline_requested: str | None = None

    # ── Vóór en ná ──
    # Alleen de dagen die op seizoenspositie te vergelijken zijn; zie
    # ``match_seasonally``. Wat er verder nog aan stookdagen ná de grens ligt
    # staat in ``after_days_total``, zodat het verschil tussen "nog niets
    # gestookt" en "wel gestookt maar niet vergelijkbaar" zichtbaar blijft.
    before: PeriodStats = field(default_factory=PeriodStats)
    after: PeriodStats = field(default_factory=PeriodStats)
    after_days_total: int = 0
    # Verschil van ná ten opzichte van vóór, in procenten. Positief = beter.
    delta_pct: float | None = None
    # Dag-tot-dag spreiding van de verhouding (%), gemeten in plaats van
    # aangenomen. Dit is de ruis waar het verschil bovenuit moet komen.
    spread_pct: float | None = None
    # Ligt het verschil buiten twee standaardfouten? Zo niet, dan is het verschil
    # niet van toeval te onderscheiden en zijn er meer stookdagen nodig.
    delta_significant: bool = False
    # Liggen de twee periodes in hetzelfde deel van het stookseizoen? Zo niet,
    # dan zit er seizoen in het verschil en is het geen uitspraak over de
    # aanpassing. Zie SEASON_MATCH_DAYS.
    delta_comparable: bool = False


def season_of(month: int) -> str:
    """Seizoenshelft van een maand — zie ``AUTUMN_START_MONTH``."""
    return SEASON_AUTUMN if month >= AUTUMN_START_MONTH else SEASON_SPRING


def build_reference_curve(
    df_heating: pd.DataFrame,
    bin_width: float = DEFAULT_BIN_WIDTH,
    min_days_per_bin: int = DEFAULT_MIN_DAYS_PER_BIN,
) -> dict[str, dict[float, float]]:
    """Mediane COP per temperatuurbin per seizoenshelft.

    Geeft ``{seizoen: {bin_middelpunt: cop}}``. Bins met te weinig dagen vallen
    weg in plaats van mee te doen met een toevallige waarde; de interpolatie
    overbrugt het gat later vanzelf. Een seizoenshelft met minder dan
    ``MIN_BINS`` bins valt in zijn geheel weg — daar valt niet zinvol tussen te
    interpoleren, en pooling met de andere helft zou juist de bias terugbrengen
    die de splitsing weghaalt.
    """
    if df_heating is None or df_heating.empty:
        return {}

    index = pd.to_datetime(df_heating.index, errors="coerce")
    temps = df_heating["avg_temperatureOutside"].to_numpy(dtype=float)
    cops = df_heating["averageCOP"].to_numpy(dtype=float)
    seasons = np.array(
        [season_of(m) if not pd.isna(m) else "" for m in index.month],
        dtype=object,
    )

    # Bin-index via floor-deling, zodat het middelpunt reproduceerbaar is.
    idx = np.floor(temps / bin_width).astype(int)

    curve: dict[str, dict[float, float]] = {}
    for season in (SEASON_AUTUMN, SEASON_SPRING):
        in_season = seasons == season
        if not in_season.any():
            continue
        bins: dict[float, float] = {}
        for bin_idx in np.unique(idx[in_season]):
            mask = in_season & (idx == bin_idx)
            if int(mask.sum()) < min_days_per_bin:
                continue
            centre = round((bin_idx + 0.5) * bin_width, 2)
            bins[centre] = float(np.median(cops[mask]))
        if len(bins) >= MIN_BINS:
            curve[season] = bins
    return curve


def reference_cop(
    curve: dict[str, dict[float, float]], temp: float, season: str
) -> float | None:
    """Verwachte COP bij deze buitentemperatuur in deze seizoenshelft.

    Lineair tussen de bins van díe helft. Buiten het bereik van de curve wordt
    **niet** geëxtrapoleerd: daar is geen norm, en een verzonnen norm zou een
    verhouding opleveren die nergens op slaat. ``np.interp`` klemt op de
    randwaarden, dus dat wordt hier expliciet afgevangen. Ook niet uitwijken naar
    de andere seizoenshelft — dat is precies de vergelijking die de splitsing
    moet voorkomen.
    """
    bins = curve.get(season)
    if not bins or len(bins) < 2:
        return None
    xs = sorted(bins)
    if temp < xs[0] or temp > xs[-1]:
        return None
    ys = [bins[x] for x in xs]
    return float(np.interp(temp, xs, ys))


def _parse_date(value: str | date | None) -> pd.Timestamp | None:
    """Een wijzigingsdatum als Timestamp, of ``None`` bij leeg of onleesbaar."""
    if value is None or value == "":
        return None
    try:
        stamp = pd.Timestamp(value).normalize()
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp


def _score(
    df: pd.DataFrame, curve: dict[str, dict[float, float]]
) -> list[dict]:
    """Zet elke stookdag af tegen de curve; dagen zonder norm vallen weg."""
    daily: list[dict] = []
    for stamp, row in df.iterrows():
        season = season_of(stamp.month)
        temp = float(row["avg_temperatureOutside"])
        cop = float(row["averageCOP"])
        ref = reference_cop(curve, temp, season)
        if ref is None or ref <= 0:
            continue
        daily.append({
            "date": str(stamp)[:10],
            "season": season,
            "temp": round(temp, 1),
            "cop": round(cop, 2),
            "cop_ref": round(ref, 2),
            "ratio": round(cop / ref, 3),
        })
    return daily


def _split_norm(
    df: pd.DataFrame,
    baseline: pd.Timestamp | None,
    eval_window_days: int,
) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """De dagen die de norm dragen, plus de grens waarop gesplitst is.

    Met een gemarkeerde wijzigingsdatum is de grens die datum. Zonder markering
    schuift de grens mee: de laatste ``eval_window_days`` *stookdagen* — niet
    kalenderdagen, want buiten het seizoen zou dat venster leeglopen — worden
    beoordeeld en dragen de norm niet.
    """
    if baseline is not None:
        return df[df.index < baseline], baseline
    if len(df) <= eval_window_days:
        return df.iloc[:0], None
    norm = df.iloc[:-eval_window_days]
    return norm, df.index[-eval_window_days]


def _doy(datum: str) -> int:
    return pd.Timestamp(datum).dayofyear


def match_seasonally(
    voor: list[dict], na: list[dict]
) -> tuple[list[dict], list[dict]]:
    """De dagen die op seizoenspositie tegen elkaar te zetten zijn.

    Per ná-dag wordt gekeken of er genoeg vóór-dagen uit dezelfde periode van
    het jaar staan; zo niet, dan valt die dag buiten het oordeel in plaats van
    dat het hele oordeel vervalt. Dat scheelt: met een vaste toets op het
    midden van de ná-periode zou een langer wordende reeks de vergelijking
    juist ongeldig maken — meer data die minder oplevert.

    Circulair gemeten, zodat een venster over de jaarwisseling heen niet als het
    halve jaar leest.
    """
    if not voor or not na:
        return [], []
    voor_doy = np.array([_doy(d["date"]) for d in voor])
    na_ok: list[dict] = []
    voor_idx: set[int] = set()
    for dag in na:
        afstand = np.abs(voor_doy - _doy(dag["date"]))
        dichtbij = np.minimum(afstand, 365 - afstand) <= SEASON_MATCH_DAYS
        if int(dichtbij.sum()) >= MIN_EVAL_DAYS_FOR_VERDICT:
            na_ok.append(dag)
            voor_idx.update(int(i) for i in np.flatnonzero(dichtbij))
    if not na_ok:
        return [], []
    return [voor[i] for i in sorted(voor_idx)], na_ok


def _period(daily: list[dict]) -> PeriodStats:
    if not daily:
        return PeriodStats()
    return PeriodStats(
        days=len(daily),
        mean=round(float(np.mean([d["ratio"] for d in daily])), 3),
        date_from=daily[0]["date"],
        date_to=daily[-1]["date"],
    )


def calculate_cop_performance(
    df_daily: pd.DataFrame | None,
    bin_width: float = DEFAULT_BIN_WIDTH,
    min_days_per_bin: int = DEFAULT_MIN_DAYS_PER_BIN,
    baseline_date: str | date | None = None,
    eval_window_days: int = DEFAULT_EVAL_WINDOW_DAYS,
) -> CopPerformanceResult:
    """Bouw de referentiecurve en zet elke stookdag ertegen af.

    Args:
        df_daily: dagframe met ``avg_temperatureOutside``, ``averageCOP`` en
            ``totalHeatPerHour``, geïndexeerd op datum.
        baseline_date: gemarkeerde wijzigingsdatum. Dagen ervóór dragen de norm,
            dagen erna worden eraan getoetst. Leeg = de grens schuift mee met het
            venster.
        eval_window_days: aantal stookdagen dat zonder markering buiten de norm
            blijft.
    """
    result = CopPerformanceResult()
    result.baseline_requested = (
        str(baseline_date)[:10] if baseline_date else None
    )
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

    # De seizoensindeling hangt aan de kalender, dus de index moet er ook een
    # zijn. Rijen met een onleesbare datum kunnen niet ingedeeld worden.
    df = df.set_index(pd.to_datetime(df.index, errors="coerce"))
    df = df[df.index.notna()].sort_index()
    if df.empty:
        return result

    baseline = _parse_date(baseline_date)
    norm_df, grens = _split_norm(df, baseline, eval_window_days)
    curve = build_reference_curve(norm_df, bin_width, min_days_per_bin)
    daily = _score(df, curve) if curve else []

    # Is de bevriezing wat waard? Dat weegt anders per soort grens.
    #
    # Zonder markering schuift de grens mee en is ``rolling_30d`` precies het
    # beoordeelde venster: kan de bevroren norm die dagen niet scoren, dan valt
    # de state stilletjes terug op dagen die de norm zélf dragen en leest alles
    # als 1,00. Dan liever de zelfreferentie expliciet melden.
    #
    # Met een markering is "nog geen oordeel" een geldige toestand — wie
    # gisteren iets veranderde heeft nog geen stookdagen — en telt alleen of de
    # norm überhaupt dagen kan scoren.
    if grens is not None:
        na_grens = len([d for d in daily if d["date"] >= str(grens)[:10]])
        genoeg = (
            len(daily) >= MIN_EVAL_DAYS_FOR_VERDICT
            if baseline is not None
            else na_grens >= MIN_EVAL_DAYS_FOR_VERDICT
        )
    else:
        genoeg = False
    if not curve or not genoeg:
        curve = build_reference_curve(df, bin_width, min_days_per_bin)
        if not curve:
            return result
        norm_df, grens = df, None
        daily = _score(df, curve)
    else:
        result.norm_frozen = True
        result.baseline_date = str(grens)[:10] if grens is not None else None
        result.baseline_explicit = baseline is not None

    if not daily:
        return result

    result.reference = curve
    result.reference_days = int(len(norm_df))
    result.daily = daily

    ratios = [d["ratio"] for d in daily]
    result.latest_date = daily[-1]["date"]
    result.rolling_30d = round(float(np.mean(ratios[-30:])), 3)

    # Vóór en ná de grens. Zonder grens is er niets te vergelijken: dan zou de
    # "ná"-periode tegen een norm staan die haar zelf bevat.
    if result.baseline_date:
        knip = result.baseline_date
        alle_voor = [d for d in daily if d["date"] < knip]
        alle_na = [d for d in daily if d["date"] >= knip]
        result.after_days_total = len(alle_na)

        # Alleen dagen uit dezelfde periode van het jaar. Een maartdag tegen de
        # winter afzetten meet de seizoensstaart en niet je aanpassing; op deze
        # installatie scheelde de norm bij 9-15 °C 12 tot 21% tussen najaar en
        # voorjaar, en bínnen een seizoenshelft loopt datzelfde effect door.
        voor, na = match_seasonally(alle_voor, alle_na)
        result.delta_comparable = bool(na)
        result.before = _period(voor)
        result.after = _period(na)
        if voor:
            result.spread_pct = round(
                float(np.std([d["ratio"] for d in voor])) * 100, 1
            )
        if result.before.mean and result.after.mean:
            result.delta_pct = round(
                (result.after.mean / result.before.mean - 1) * 100, 1
            )
            # Tweesteekproefstoets op de gemeten dagspreiding. Blijft het
            # verschil eronder, dan is het ruis en hoort er geen conclusie aan
            # gehangen te worden.
            std = float(np.std([d["ratio"] for d in voor]))
            fout = std * np.sqrt(1 / len(voor) + 1 / len(na))
            result.delta_significant = bool(
                fout > 0
                and len(na) >= MIN_EVAL_DAYS_FOR_VERDICT
                and abs(result.after.mean - result.before.mean) > 2 * fout
            )

    return result
