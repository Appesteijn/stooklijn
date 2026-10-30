"""Meet of het tweetoestandsmodel beter voorspelt dan het model dat draait.

De vraag "werkt 2R2C al beter?" is niet met een blik op de parameters te
beantwoorden. Een model met mooiere getallen kan slechter voorspellen, en een
fit op te weinig of te eenzijdige data ziet er vrijwel altijd goed uit op de
data waarop hij gefit is. Daarom meet dit bestand drie dingen op dezelfde
weggehouden data, dezelfde horizon en dezelfde stapgrootte:

    2R2C            het kandidaat-model, vers gefit op de andere helft
    huidig model    het 1R1C dat nu in productie draait, ongewijzigd
    niets doen      aannemen dat de binnentemperatuur niet verandert

Die derde hoort erbij. Op korte horizon is hij verrassend sterk, en een model
dat er niet overheen komt voegt niets toe hoe fraai de andere twee getallen
ook staan.

Leren op vijf minuten, toetsen per uur
--------------------------------------
Het 2R2C leert op de vijfminutenmonsters, niet op uurgemiddelden. Een
uurgemiddelde van het vermogen gooit precies weg wat de snelle knoop moet
leren: een uur met dertig minuten op 5 kW en een uur met zestig minuten op
2,5 kW hebben hetzelfde gemiddelde, maar niet dezelfde luchttemperatuur aan
het eind van dat uur. Voor de trage bouwmassa telt alleen de energie; voor
Ci, Hm en alpha telt wanneer die energie binnenkwam. Dit was eerder al
vastgesteld voor de knikdetectie (defrost-verdunning, zie
KNEE_DETECTION_IMPROVEMENTS.md) en staat als regel in CLAUDE.md.

Getoetst wordt wél per uur, op dezelfde uren voor alle drie de modellen. Het
1R1C rekent in uurstappen en de MPC plant per uur; het 2R2C fijner toetsen
zou het belonen voor informatie die het in gebruik niet krijgt. Dat kan
zonder kunstgrepen omdat de discretisatie exact is: parameters die op vijf
minuten geleerd zijn, betekenen op een uurstap hetzelfde.

Train- en testweken worden op kalendertijd gekozen, niet per reeks. Dan
krijgen beide stapgroottes gegarandeerd dezelfde weken, en blijft een week
in dezelfde helft als het logboek groeit.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

import numpy as np

from .thermal_model import OnlineRCModel
from .thermal_model_2r2c import (
    BURN_IN_HOURS,
    MIN_BLOCK_HOURS,
    MIN_TRAIN_HOURS,
    SPLIT_BLOCK_HOURS,
    Segment,
    Validation,
    fit_and_score,
    steps_in,
)

_LOGGER = logging.getLogger(__name__)

#: Horizon waarop vergeleken wordt. Twaalf uur, omdat dat de vooruitblik is
#: waar de MPC zijn advies op baseert. Op één uur winnen alle modellen het
#: nauwelijks van niets doen; pas over een halve dag scheiden ze.
DEFAULT_HORIZON_HOURS = 12.0

#: Stapgrootte waarop getoetst wordt. Zie de kop van dit bestand.
DEFAULT_DT_HOURS = 1.0

#: Stapgrootte waarop het 2R2C leert: die van het meetlogboek.
FIT_DT_HOURS = 5.0 / 60.0

#: Langste gat dat bij het leren overbrugd wordt, door lineair te
#: interpoleren. Op vijf minuten breekt anders elk ontbrekend monster de
#: reeks, en een herstart van HA kost er al een of twee. Stukken korter dan
#: MIN_BLOCK_HOURS tellen niet mee, dus zonder overbrugging zou een logboek
#: met een dagelijkse herstart nooit één bruikbaar trainingsblok opleveren.
#: Een half uur is ruim onder de snelle tijdconstante van een huis; wat er in
#: zo'n gat gebeurde valt uit de omliggende metingen goed genoeg te raden.
MAX_GAP_FILL_HOURS = 0.5

#: Hoeveel van een uur gemeten moet zijn voordat het als uur meetelt. Een uur
#: met twee monsters is geen uurgemiddelde maar een momentopname met een
#: uurstempel, en die schuift de vrijloop onvoorspelbaar scheef.
MIN_BUCKET_COVERAGE = 0.5

#: Iteratieplafond voor de fit. De standaard van ``fit()`` (60) is te krap
#: voor echte data: een proeffit op het logboek van oktober 2026 stopte op 60
#: en convergeerde pas bij 133. Een fit die tegen het plafond loopt keurt de
#: poort af als "fit convergeerde niet", en dan meldt de vergelijking dat elke
#: nacht opnieuw zonder ooit een uitkomst te geven. Een iteratie kost op een
#: vol logboek een tiende seconde op een pc; deze draait één keer per nacht in
#: een executor.
FIT_MAX_ITER = 200

#: Kolomvolgorde in het meetlogboek.
_TS, _T_IN, _T_OUT, _Q_HP, _Q_SOLAR = range(5)


def _median_step_seconds(ts: np.ndarray) -> float | None:
    """Bemonsteringsinterval van het logboek, uit de data zelf.

    Niet uit ``highres_log.SAMPLE_INTERVAL`` overgenomen: een logboek dat over
    een wijziging van dat interval heen loopt draagt beide cadansen, en dan is
    wat er werkelijk in het bestand staat de enige betrouwbare bron.
    """
    gaps = np.diff(ts)
    gaps = gaps[gaps > 0]
    if len(gaps) == 0:
        return None
    return float(np.median(gaps))


def resample(
    rows: list[list[float]],
    dt_hours: float = DEFAULT_DT_HOURS,
    min_coverage: float = MIN_BUCKET_COVERAGE,
    max_fill_hours: float = 0.0,
) -> list[Segment]:
    """Vat de logboekmonsters samen tot aaneengesloten reeksen.

    Elke bak van ``dt_hours`` wordt het gemiddelde van de monsters erin. Bakken
    met te weinig monsters vervallen, en op een gat breekt de reeks: een
    segment is per definitie aaneengesloten, want de vrijloop rekent stap voor
    stap door en kan niet over een ontbrekend uur heen.

    Gaten tot en met ``max_fill_hours`` worden lineair overbrugd in plaats van
    de reeks te breken. Standaard gebeurt dat niet; zie ``MAX_GAP_FILL_HOURS``
    voor waarom het bij het leren op vijf minuten wel moet.

    De monsters worden eerst op tijd gesorteerd. Het logboek staat daar niet
    altijd op: na een teruglopende klok — een NTP-correctie bij een herstart
    zonder RTC — staan er oudere stempels achter nieuwere.
    """
    if len(rows) < 2:
        return []
    data = np.asarray(rows, dtype=float)
    data = data[np.argsort(data[:, _TS], kind="stable")]

    sample_s = _median_step_seconds(data[:, _TS])
    if sample_s is None:
        return []
    bucket_s = dt_hours * 3600.0
    # Eén monster per bak is geen dekking maar de ondergrens van tellen.
    expected = max(1.0, bucket_s / sample_s)
    min_samples = max(1, int(np.ceil(min_coverage * expected)))

    # Gesorteerd, dus elke bak is één aaneengesloten blok rijen en kan in één
    # keer opgeteld worden. Per bak filteren kost op een vol logboek met
    # vijfminutenbakken honderden miljoenen vergelijkingen.
    index = np.floor(data[:, _TS] / bucket_s).astype(np.int64)
    buckets, first, counts = np.unique(index, return_index=True, return_counts=True)
    cols = data[:, [_T_IN, _T_OUT, _Q_SOLAR, _Q_HP]]
    means = np.add.reduceat(cols, first, axis=0) / counts[:, None]

    # Te dunne bakken vervallen; daarmee ontstaat op die plek een gat.
    keep = counts >= min_samples
    buckets, means = buckets[keep], means[keep]
    if len(buckets) < 2:
        return []

    max_missing = int(round(max_fill_hours / dt_hours))
    breaks = np.flatnonzero(np.diff(buckets) - 1 > max_missing) + 1
    segments: list[Segment] = []
    for idx, vals in zip(np.split(buckets, breaks), np.split(means, breaks)):
        if len(idx) < 2:
            continue
        full = np.arange(idx[0], idx[-1] + 1)
        if len(full) != len(idx):
            vals = np.column_stack(
                [np.interp(full, idx, vals[:, i]) for i in range(vals.shape[1])]
            )
        segments.append(tuple(vals[:, i].copy() for i in range(4)))  # type: ignore[arg-type]
    return segments


def split_by_week(rows: list[list[float]]) -> tuple[np.ndarray, np.ndarray]:
    """Verdeel logboekrijen om-en-om per kalenderweek over train en test.

    Op tijd en niet per reeks, om twee redenen. Het leren en het toetsen
    gebeuren op een andere stapgrootte, en alleen een splitsing op de klok
    geeft beide gegarandeerd dezelfde weken. En een week blijft zo in
    dezelfde helft als het logboek groeit of vooraan afvalt; met een teller
    over de reeksen schuift bij elk nieuw gat de hele verdeling op.

    De weken tellen vanaf de Unix-epoch, dus van donderdag tot donderdag UTC.
    Welke dag maakt niet uit, zolang het elke keer dezelfde is.
    """
    if len(rows) == 0:
        return np.zeros((0, 5)), np.zeros((0, 5))
    data = np.asarray(rows, dtype=float)
    week = np.floor(data[:, _TS] / (SPLIT_BLOCK_HOURS * 3600.0)).astype(np.int64)
    even = week % 2 == 0
    return data[even], data[~even]


def _long_enough(segments: list[Segment], dt_hours: float) -> list[Segment]:
    """Alleen stukken waar een inlooptijd plus een horizon in past."""
    min_len = steps_in(MIN_BLOCK_HOURS, dt_hours)
    return [s for s in segments if len(s[0]) >= min_len]


def _hours(segments: list[Segment], dt_hours: float) -> float:
    return sum(len(s[0]) for s in segments) * dt_hours


def reference_rmse(
    model: OnlineRCModel,
    segments: list[Segment],
    horizon: int,
    burn_in: int | None = None,
    dt_hours: float = DEFAULT_DT_HOURS,
) -> float | None:
    """Vrijloopfout van het huidige 1R1C-model, op dezelfde vensters.

    Bewust dezelfde vensters, dezelfde inlooptijd en dezelfde uitlijning als
    ``thermal_model_2r2c.rmse_at_horizon``. Zou hier ook maar één van die drie
    afwijken, dan meet het verschil tussen de twee modellen deels het verschil
    tussen twee meetmethodes, en dat is precies de fout die een vergelijking
    waardeloos maakt.

    Het model wordt alleen gelezen. ``predict_t_indoor`` raakt de RLS-toestand
    niet aan, dus de leerhistorie van het draaiende model blijft intact.

    De inlooptijd is hier strikt genomen overbodig — 1R1C heeft geen verborgen
    toestand die moet uitsterven — maar hij hoort er wel te staan: anders
    krijgt de referentie vensters die de kandidaat niet heeft.
    """
    if burn_in is None:
        burn_in = steps_in(BURN_IN_HOURS, dt_hours)
    errs: list[np.ndarray] = []
    for t_in, t_out, q_solar, q_hp in segments:
        if len(t_in) <= burn_in + horizon + 1:
            continue
        for k in range(burn_in, len(t_in) - horizon):
            t = float(t_in[k])
            pred = np.empty(horizon)
            for j in range(horizon):
                # Invoer op k+j verklaart de temperatuur op k+j+1: dezelfde
                # afspraak als simulate() in het 2R2C-model.
                t = model.predict_t_indoor(
                    t,
                    float(t_out[k + j]),
                    float(q_hp[k + j]),
                    float(q_solar[k + j]),
                    dt_hours,
                )
                pred[j] = t
            errs.append(pred - t_in[k + 1:k + 1 + horizon])
    if not errs:
        return None
    return float(np.sqrt((np.concatenate(errs) ** 2).mean()))


@dataclass(frozen=True)
class Comparison:
    """Wat de vergelijking heeft opgeleverd."""

    #: None zolang er geen fit was; de RMSE van het kandidaat-model in K.
    rmse_k: float | None
    #: RMSE van het model dat nu draait, op dezelfde testdata.
    reference_rmse_k: float | None
    #: RMSE van aannemen dat er niets verandert.
    persistence_rmse_k: float | None
    accepted: bool
    reason: str
    horizon_hours: float
    dt_hours: float
    #: Hoeveel uur bruikbare meetdata er in de vergelijking zat.
    data_hours: float
    #: Hoeveel uur trainingsdata er minimaal nodig is voordat er gefit wordt.
    required_train_hours: float
    params: dict | None
    fit_report: dict
    #: Stapgrootte waarop het 2R2C geleerd heeft.
    fit_dt_hours: float = FIT_DT_HOURS
    #: Bruikbare trainingsuren na het splitsen en weggooien van te korte
    #: stukken. Dít getal moet ``required_train_hours`` halen; het totaal aan
    #: logboekuren zegt daar weinig over, want gaten en korte stukken tellen
    #: niet mee.
    train_hours: float = 0.0
    test_hours: float = 0.0

    @property
    def verbetering_pct(self) -> float | None:
        """Hoeveel procent scherper dan het huidige model, positief is beter."""
        # Expliciet op None toetsen: een RMSE van precies nul is onwaarschijnlijk
        # maar niet onmogelijk, en die hoort een uitkomst te geven in plaats van
        # als "ontbreekt" weggefilterd te worden. Op de referentie wordt wel op
        # nul getoetst, want daar zou het een deling door nul zijn.
        if self.rmse_k is None or not self.reference_rmse_k:
            return None
        return round(
            (self.reference_rmse_k - self.rmse_k) / self.reference_rmse_k * 100.0, 1
        )

    def to_dict(self) -> dict:
        return {
            "rmse_k": self.rmse_k,
            "reference_rmse_k": self.reference_rmse_k,
            "persistence_rmse_k": self.persistence_rmse_k,
            "verbetering_pct": self.verbetering_pct,
            "accepted": self.accepted,
            "reason": self.reason,
            "horizon_uren": self.horizon_hours,
            "stap_uren": self.dt_hours,
            "leerstap_minuten": round(self.fit_dt_hours * 60.0, 1),
            "data_uren": round(self.data_hours, 1),
            "train_uren": round(self.train_hours, 1),
            "test_uren": round(self.test_hours, 1),
            "benodigde_train_uren": self.required_train_hours,
            "params": self.params,
            "fit": self.fit_report,
        }


def compare(
    rows: list[list[float]],
    model: OnlineRCModel | None,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    dt_hours: float = DEFAULT_DT_HOURS,
    min_coverage: float = MIN_BUCKET_COVERAGE,
    max_iter: int = FIT_MAX_ITER,
    fit_dt_hours: float = FIT_DT_HOURS,
) -> Comparison:
    """Fit 2R2C op de ene helft van het logboek en scoor drie modellen op de
    andere.

    Het 2R2C leert op ``fit_dt_hours``, alle drie worden getoetst op
    ``dt_hours``. Zie de kop van dit bestand voor waarom die twee verschillen.

    Rekent zwaar genoeg om niet in de event loop thuis te horen: draai hem in
    een executor.
    """
    horizon = steps_in(horizon_hours, dt_hours)
    data_hours = _hours(resample(rows, dt_hours, min_coverage), dt_hours)

    train_rows, test_rows = split_by_week(rows)
    train = _long_enough(
        resample(train_rows, fit_dt_hours, min_coverage, MAX_GAP_FILL_HOURS),
        fit_dt_hours,
    )
    test = _long_enough(resample(test_rows, dt_hours, min_coverage), dt_hours)
    train_hours = _hours(train, fit_dt_hours)
    test_hours = _hours(test, dt_hours)

    def _uitkomst(validation: Validation | None, ref: float | None,
                  reason: str | None = None) -> Comparison:
        def _r(v: float | None) -> float | None:
            return round(v, 3) if v is not None else None
        return Comparison(
            rmse_k=_r(validation.rmse) if validation else None,
            reference_rmse_k=_r(ref),
            persistence_rmse_k=_r(validation.persistence_rmse) if validation else None,
            accepted=validation.accepted if validation else False,
            reason=reason or validation.reason,
            horizon_hours=horizon_hours,
            dt_hours=dt_hours,
            data_hours=data_hours,
            required_train_hours=MIN_TRAIN_HOURS,
            params=validation.params.to_dict() if validation and validation.params else None,
            fit_report=validation.report if validation else {},
            fit_dt_hours=fit_dt_hours,
            train_hours=train_hours,
            test_hours=test_hours,
        )

    if data_hours == 0.0:
        return _uitkomst(None, None, "nog geen bruikbare meetdata")

    # De referentie moet vóór de fit bekend zijn: de poort toetst het
    # kandidaat-model eraan af. Zonder referentie zou de poort alleen tegen
    # "niets doen" toetsen en dus veel te makkelijk open gaan. Hij scoort op
    # precies de testreeksen die ook de poort krijgt — hetzelfde object, niet
    # een tweede splitsing die uit de pas kan lopen.
    ref = (
        reference_rmse(model, test, horizon, dt_hours=dt_hours)
        if model is not None and test
        else None
    )
    validation = fit_and_score(
        train, test, horizon, reference_rmse=ref,
        fit_dt_hours=fit_dt_hours, test_dt_hours=dt_hours, max_iter=max_iter,
    )
    return _uitkomst(validation, ref)
