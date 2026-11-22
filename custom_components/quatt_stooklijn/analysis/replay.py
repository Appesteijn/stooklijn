"""Naspeler: het advies in een gesloten lus op het eigen logboek.

Het sturingseffect meet achteraf, per dag, wat een instelling deed. Dat kost
weken A/B voordat er iets te zeggen valt, en het weer verschilt per dag. De
naspeler rekent dezelfde dagen opnieuw door met een andere regelaar: het
goedgekeurde 2R2C is het huis, de gemeten buitentemperatuur en zon zijn het
weer, en de adviesformule (warmteverlies plus inhaalterm) bepaalt elk uur het
vermogen. Zo zijn inhaalfactoren en bronnen in één nacht naast elkaar te
zetten, op hetzelfde weer.

Wat het níét is: een meting. Het antwoord is zo goed als het 2R2C, en dat
mag alleen meedoen als de modelvergelijking het goedkeurde voor gebruik
(beide toetsen en de afkoeling). Het sturingseffect blijft de controle achteraf.

Varianten (``VARIANTS``): de inhaalfactor 0 is "Buitentemperatuur" (geen
kamerterm), 10 is wat "Buitentemperatuur + kamer" nu doet. "voorspellend"
rekent de inhaalterm op de kamer over ``PREDICT_HOURS`` uur in plaats van nu,
met het weer dat daar komt (in de naspeler het gemeten weer, dus een perfecte
verwachting: een bovengrens voor wat dat oplevert).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model_benchmark import MIN_BUCKET_COVERAGE, resample
from .thermal_model_2r2c import TwoStateParams, discretize, track_hidden_state

#: Hoeveel dagen logboek de naspeler doorrekent.
REPLAY_DAYS = 21
#: Inlooptijd per stuk logboek voor de massatemperatuur, in uren.
BURN_IN_HOURS = 24
#: Kortste stuk dat meedoet, in uren (na de inlooptijd).
MIN_SEGMENT_HOURS = 48
#: Hoe ver vooruit de voorspellende variant kijkt.
PREDICT_HOURS = 3
#: Te koud: zoveel onder het doel.
COLD_MARGIN_K = 0.5
#: Te warm: zoveel boven het doel.
WARM_MARGIN_K = 1.0


@dataclass(frozen=True)
class Variant:
    name: str
    catchup_factor: float
    predictive: bool = False


VARIANTS = (
    Variant("buitentemperatuur", 0.0),
    Variant("inhaalfactor_5", 5.0),
    Variant("inhaalfactor_10", 10.0),
    Variant("inhaalfactor_20", 20.0),
    Variant("voorspellend_10", 10.0, predictive=True),
)


def _segments(rows: list[list[float]], days: float):
    if not rows:
        return []
    last = max(r[0] for r in rows)
    recent = [r[:5] for r in rows if r[0] >= last - days * 86400]
    return [s for s in resample(recent, 1.0, MIN_BUCKET_COVERAGE)
            if len(s[0]) >= BURN_IN_HOURS + MIN_SEGMENT_HOURS]


def _demand(v: Variant, u: float, t_bal: float, target: float, t_room: float,
            t_out: float, capacity: float) -> float:
    stationary = u * max(0.0, t_bal - t_out)
    if stationary <= 0:
        return 0.0
    q = stationary + v.catchup_factor * u * (target - t_room)
    return float(min(capacity, max(0.0, q)))


def replay(
    params: TwoStateParams,
    rows: list[list[float]],
    u: float,
    balance_point: float,
    days: float = REPLAY_DAYS,
    variants: tuple[Variant, ...] = VARIANTS,
) -> dict:
    """Reken elke variant door op de laatste ``days`` dagen van het logboek.

    Het doel is de mediaan van de gemeten kamer over die dagen: wat het huis
    werkelijk aanhield. Het vermogen is begrensd op het hoogste gemeten
    uurvermogen. Geeft per variant energie en comfort, plus hetzelfde voor
    wat er werkelijk gebeurde.
    """
    segs = _segments(rows, days)
    if not segs:
        return {"reden": "te weinig aaneengesloten logboek"}
    all_ti = np.concatenate([s[0][BURN_IN_HOURS:] for s in segs])
    all_q = np.concatenate([s[3] for s in segs])
    target = float(np.median(all_ti))
    capacity = float(np.max(all_q)) if np.max(all_q) > 0 else 0.0
    if capacity <= 0:
        return {"reden": "geen stookuren in het venster"}
    Ad, Bd = discretize(params, 1.0)

    def metrics(ti: np.ndarray, q: np.ndarray) -> dict:
        err = ti - target
        return {
            "energie_kwh": round(float(q.sum()) / 1000.0, 1),
            "kamer_rmse_k": round(float(np.sqrt(np.mean(err ** 2))), 3),
            "te_koud_pct": round(100.0 * float(np.mean(err < -COLD_MARGIN_K)), 1),
            "te_warm_pct": round(100.0 * float(np.mean(err > WARM_MARGIN_K)), 1),
        }

    out: dict = {
        "doel_c": round(target, 2),
        "dagen": days,
        "uren": int(sum(len(s[0]) - BURN_IN_HOURS for s in segs)),
        "werkelijk": metrics(all_ti, np.concatenate([s[3][BURN_IN_HOURS:] for s in segs])),
        "varianten": {},
    }
    for v in variants:
        ti_all, q_all = [], []
        for t_in, t_out, sol, q_meas in segs:
            tm = track_hidden_state(params, t_in, t_out, sol, q_meas, 1.0)
            x = np.array([t_in[BURN_IN_HOURS], tm[BURN_IN_HOURS]])
            for k in range(BURN_IN_HOURS, len(t_in)):
                room_for_control = x[0]
                if v.predictive:
                    # Vooruitkijken met alleen het warmteverlies als vermogen:
                    # waar gaat de kamer heen als de inhaalterm niets doet?
                    y = x.copy()
                    for j in range(k, min(len(t_in), k + PREDICT_HOURS)):
                        q_st = u * max(0.0, balance_point - t_out[j])
                        y = Ad @ y + Bd @ np.array([t_out[j], sol[j], q_st, 1.0])
                    room_for_control = y[0]
                q = _demand(v, u, balance_point, target, room_for_control, t_out[k], capacity)
                ti_all.append(x[0])
                q_all.append(q)
                x = Ad @ x + Bd @ np.array([t_out[k], sol[k], q, 1.0])
        out["varianten"][v.name] = metrics(np.array(ti_all), np.array(q_all))
    # Beste op comfort, zonder meer energie dan de huidige stand (factor 10).
    ref = out["varianten"].get("inhaalfactor_10")
    kandidaten = {
        n: m for n, m in out["varianten"].items()
        if ref is None or m["energie_kwh"] <= ref["energie_kwh"] * 1.02
    }
    out["beste"] = min(kandidaten, key=lambda n: kandidaten[n]["kamer_rmse_k"]) if kandidaten else None
    return out
