"""Wat doet verschuiven met de kamertemperatuur, volgens het 2R2C?

De verschuiving zelf rekent met het 1R1C en begrenst de kamerdrift met diens
ene capaciteit. Het 2R2C voorspelt de kamertemperatuur op weggehouden data
duidelijk beter (twaalf uur vooruit 0,28 K tegen 0,46 K, november 2026), dus
dat is het model om te laten zien wat een plan met de kamer doet.

Andersom gebruiken is hier bewust niet gedaan: de warmte uitrekenen die nodig
is om op het setpoint te blijven. Die vraag leunt op wat dit model het slechtst
kent, de stationaire balans (U, P_int en g ruilen tegen elkaar uit), en op de
massatemperatuur. Gemeten op 09-11-2026: 0,1 K in de massa is 825 W, en g gaf
om 14:00 5,8 kW aan zon tegen 1,0 kW bij het 1R1C.

Het model is lineair, dus het verschil tussen de twee lijnen hangt alleen af
van het verschil in vermogen. Weersverwachting, zon en begintoestand vallen
eruit. Dat verschil is daarmee het betrouwbaarste getal van deze berekening;
de hoogte van de lijnen is onzekerder.

Dit stuurt niets aan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model_benchmark import (
    FIT_DT_HOURS,
    MAX_GAP_FILL_HOURS,
    MIN_BUCKET_COVERAGE,
    resample,
)
from .thermal_model_2r2c import (
    BURN_IN_HOURS,
    TwoStateParams,
    simulate,
    steps_in,
    track_hidden_state,
)

#: Hoeveel logboek de massatemperatuur reconstrueert. De beginfout sterft uit
#: met Cm/Hm, op het gemeten huis 16 uur; vier dagen is daar ruim boven.
TRACK_WINDOW_HOURS = 96.0

#: Hoe oud het laatste monster mag zijn. De begintoestand is een meting; is die
#: er niet, dan liever geen lijn dan een lijn die ergens anders begint.
MAX_SAMPLE_AGE_HOURS = 0.5


@dataclass(frozen=True)
class RoomForecast:
    """Kamertemperatuur aan het begin van elke stap, zonder en met verschuiven."""

    flat: list[float] = field(default_factory=list)
    shifted: list[float] = field(default_factory=list)
    t_indoor0: float | None = None
    t_mass0: float | None = None
    #: Waarom er geen lijnen zijn, als die er niet zijn.
    reason: str | None = None


def forecast_room_temperature(
    params: TwoStateParams,
    rows: list[list[float]],
    at_ts: float,
    t_outdoor: list[float],
    q_solar: list[float],
    flat_w: list[float],
    shifted_w: list[float],
    dt_hours: float,
) -> RoomForecast:
    """Reken beide vermogensreeksen vrij door vanaf de toestand op ``at_ts``.

    De begintoestand komt uit het meetlogboek: de gemeten kamertemperatuur en
    de daaruit gereconstrueerde massatemperatuur, op de vijf minuten waarop het
    model ook geleerd heeft. Monsters na ``at_ts`` tellen niet mee.
    """
    lo = at_ts - TRACK_WINDOW_HOURS * 3600.0
    window = [r for r in rows if lo < r[0] <= at_ts]
    if not window or at_ts - max(r[0] for r in window) > MAX_SAMPLE_AGE_HOURS * 3600.0:
        return RoomForecast(reason="geen recente meting in het logboek")

    segments = resample(window, FIT_DT_HOURS, MIN_BUCKET_COVERAGE, MAX_GAP_FILL_HOURS)
    if not segments or len(segments[-1][0]) < steps_in(BURN_IN_HOURS, FIT_DT_HOURS):
        return RoomForecast(reason="te weinig aaneengesloten metingen")

    t_in, t_out, sol, q_hp = segments[-1]
    t_mass = track_hidden_state(params, t_in, t_out, sol, q_hp, FIT_DT_HOURS)
    t0, tm0 = float(t_in[-1]), float(t_mass[-1])

    def lijn(power: list[float]) -> list[float]:
        out = simulate(params, t0, tm0, t_outdoor, q_solar, power, dt_hours)
        # simulate() geeft de temperatuur na elke stap; de grafiek zet hem bij
        # het begin van de stap, net als het vermogen.
        return [round(t0, 2)] + [round(float(v), 2) for v in out[:-1]]

    return RoomForecast(
        flat=lijn(flat_w),
        shifted=lijn(shifted_w),
        t_indoor0=round(t0, 2),
        t_mass0=round(tm0, 2),
    )
