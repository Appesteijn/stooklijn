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

Waarom op uurbasis en niet op vijf minuten
------------------------------------------
Het meetlogboek bemonstert op vijf minuten, en dat is precies wat je nodig
hebt om ooit een derde toestand te identificeren. Voor deze vergelijking is
het de verkeerde stap. Het huidige model leert op uurbasis en is daar het
sterkst; op vijfminutenstappen zou de referentie iets meten wat ze in
productie nooit doet, en dan vergelijk je twee modellen op een baan waar er
maar één van is. Bovendien kost de vrijloop-evaluatie twaalf keer zoveel werk
voor een antwoord op dezelfde vraag.

De monsters worden dus tot uurgemiddelden samengevat. Wat er aan snelle
dynamiek in zit gaat daarmee verloren — dat is bekend en bedoeld.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

import numpy as np

from .thermal_model import OnlineRCModel
from .thermal_model_2r2c import (
    BURN_IN_HOURS,
    MIN_TRAIN_HOURS,
    Segment,
    fit_and_validate,
    split_alternating,
    steps_in,
)

_LOGGER = logging.getLogger(__name__)

#: Horizon waarop vergeleken wordt. Twaalf uur, omdat dat de vooruitblik is
#: waar de MPC zijn advies op baseert. Op één uur winnen alle modellen het
#: nauwelijks van niets doen; pas over een halve dag scheiden ze.
DEFAULT_HORIZON_HOURS = 12.0

#: Stapgrootte van de vergelijking. Zie de kop van dit bestand.
DEFAULT_DT_HOURS = 1.0

#: Hoeveel van een uur gemeten moet zijn voordat het als uur meetelt. Een uur
#: met twee monsters is geen uurgemiddelde maar een momentopname met een
#: uurstempel, en die schuift de vrijloop onvoorspelbaar scheef.
MIN_BUCKET_COVERAGE = 0.5

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
) -> list[Segment]:
    """Vat de logboekmonsters samen tot aaneengesloten reeksen.

    Elke bak van ``dt_hours`` wordt het gemiddelde van de monsters erin. Bakken
    met te weinig monsters vervallen, en op elk gat breekt de reeks: een
    segment is per definitie aaneengesloten, want de vrijloop rekent stap voor
    stap door en kan niet over een ontbrekend uur heen.

    De monsters worden eerst op tijd gesorteerd. Het logboek staat daar niet
    altijd op: na een teruglopende klok — een NTP-correctie bij een herstart
    zonder RTC — staan er oudere stempels achter nieuwere.
    """
    if len(rows) < 2:
        return []
    data = np.asarray(rows, dtype=float)
    data = data[np.argsort(data[:, _TS])]

    sample_s = _median_step_seconds(data[:, _TS])
    if sample_s is None:
        return []
    bucket_s = dt_hours * 3600.0
    # Eén monster per bak is geen dekking maar de ondergrens van tellen.
    expected = max(1.0, bucket_s / sample_s)
    min_samples = max(1, int(np.ceil(min_coverage * expected)))

    index = np.floor(data[:, _TS] / bucket_s).astype(np.int64)
    segments: list[Segment] = []
    cols: list[list[float]] = [[], [], [], []]
    prev_bucket: int | None = None

    def _flush() -> None:
        if len(cols[0]) >= 2:
            segments.append(tuple(np.array(c) for c in cols))  # type: ignore[arg-type]
        for c in cols:
            c.clear()

    for bucket in np.unique(index):
        chunk = data[index == bucket]
        if len(chunk) < min_samples:
            # Te dun: de bak vervalt, en daarmee breekt de reeks hier.
            _flush()
            prev_bucket = None
            continue
        if prev_bucket is not None and bucket != prev_bucket + 1:
            _flush()
        for i, col in enumerate((_T_IN, _T_OUT, _Q_SOLAR, _Q_HP)):
            cols[i].append(float(chunk[:, col].mean()))
        prev_bucket = int(bucket)
    _flush()
    return segments


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
    #: Hoeveel uur er minimaal nodig is voordat er überhaupt gefit wordt.
    required_train_hours: float
    params: dict | None
    fit_report: dict

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
            "data_uren": round(self.data_hours, 1),
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
    max_iter: int | None = None,
) -> Comparison:
    """Fit 2R2C op de helft van het logboek en scoor drie modellen op de rest.

    Rekent zwaar genoeg om niet in de event loop thuis te horen: draai hem in
    een executor.
    """
    horizon = steps_in(horizon_hours, dt_hours)
    segments = resample(rows, dt_hours, min_coverage)
    data_hours = sum(len(s[0]) for s in segments) * dt_hours

    def _leeg(reason: str) -> Comparison:
        return Comparison(
            rmse_k=None, reference_rmse_k=None, persistence_rmse_k=None,
            accepted=False, reason=reason, horizon_hours=horizon_hours,
            dt_hours=dt_hours, data_hours=data_hours,
            required_train_hours=MIN_TRAIN_HOURS * 2, params=None, fit_report={},
        )

    if not segments:
        return _leeg("nog geen bruikbare meetdata")

    # De referentie moet vóór de fit bekend zijn: fit_and_validate toetst het
    # kandidaat-model eraan af. Zonder referentie zou de poort alleen tegen
    # "niets doen" toetsen en dus veel te makkelijk open gaan.
    kwargs = {} if max_iter is None else {"max_iter": max_iter}
    # Deze splitsing moet dezelfde zijn als die fit_and_validate straks zelf
    # maakt, anders scoren de twee modellen op verschillende weken en meet het
    # verschil deels het weer. Ze is deterministisch en krijgt hier dezelfde
    # stapgrootte mee; test_zelfde_testset_als_de_poort pint dat vast.
    _, test = split_alternating(segments, dt_hours=dt_hours)
    ref = (
        reference_rmse(model, test, horizon, dt_hours=dt_hours)
        if model is not None and test
        else None
    )

    validation = fit_and_validate(
        segments, horizon=horizon, reference_rmse=ref, dt_hours=dt_hours, **kwargs
    )
    return Comparison(
        rmse_k=round(validation.rmse, 3) if validation.rmse is not None else None,
        reference_rmse_k=round(ref, 3) if ref is not None else None,
        persistence_rmse_k=(
            round(validation.persistence_rmse, 3)
            if validation.persistence_rmse is not None else None
        ),
        accepted=validation.accepted,
        reason=validation.reason,
        horizon_hours=horizon_hours,
        dt_hours=dt_hours,
        data_hours=data_hours,
        # Trainen gebeurt op de helft van de data, dus er moet twee keer de
        # trainingseis aan logboek liggen voordat de poort iets kan zeggen.
        required_train_hours=MIN_TRAIN_HOURS * 2,
        params=validation.params.to_dict() if validation.params else None,
        fit_report=validation.report,
    )
