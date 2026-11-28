"""Schaduwvoorspelling: de kamer vooruit met de zon die toen verwacht werd.

Stap 1 van MPC_ZON_PLAN.md. Een planner rekent met de zonverwachting, maar
het 2R2C is tot nu toe alleen getoetst met de zon die achteraf scheen. Hier
wordt elk uur vastgelegd waarmee een planner op dat moment zou rekenen (de
parameters van het 2R2C en het 1R1C, de begintoestand, de correctiefactor
voor de zon), en achteraf doorgerekend:

* met het vermogen en de buitentemperatuur die er echt waren, zodat alleen de
  zon verschilt;
* met de zonverwachting van vóór het vastleggen (vooruitkijktijd k+1 voor stap
  k), met en zonder correctiefactor, met de zon uit het logboek (de waarde van
  het lopende uur, waarop het model geleerd heeft) en zonder zon;
* tegen de gemeten kamer op het eind van elke stap.

Puur numpy; de runtime staat in ``sensor.py`` (modelvergelijking).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
import math

import numpy as np

from .solar_forecast_log import SolarForecastLog
from .thermal_model_2r2c import TwoStateParams, simulate

#: Zoveel uur vooruit.
HORIZON_HOURS = 12
#: Hierop wordt gescoord.
SCORE_LEADS = (1, 3, 6, 12)
#: Zo lang blijven vastgelegde momenten bewaard. Het doel vraagt vier weken.
KEEP_DAYS = 35
#: Een startuur is zonnig als de zon in de eerste zes uur hier boven komt (W/m²,
#: de zonkolom van het logboek).
SUNNY_WM2 = 100.0
SUNNY_HOURS = 6
#: Minimaal aantal vijfminutenmonsters per uur voor de invoer (van 12).
MIN_SAMPLES = 6
#: De gemeten kamer op het eind van een stap: het monster dat het dichtst bij
#: ligt, mits binnen zoveel seconden.
TRUTH_TOLERANCE_S = 10 * 60
#: Doel uit MPC_ZON_PLAN.md stap 1, op de zonnige starturen, met de
#: gecorrigeerde verwachting: RMSE op 3 en 6 uur, en de gemiddelde afwijking op 6 uur.
GOAL_RMSE_K = {3: 0.25, 6: 0.30}
GOAL_BIAS_K = 0.10
GOAL_DAYS = 28
GOAL_SUNNY_DAYS = 5

VARIANTS = ("2r2c_verwacht", "2r2c_verwacht_ruw", "2r2c_achteraf", "2r2c_geen_zon",
            "1r1c_verwacht", "niets_doen")

# Kolommen van het meetlogboek (highres_log.COLUMNS).
_TS, _T_IN, _T_OUT, _Q_HP, _SOL = 0, 1, 2, 3, 4


@dataclass
class Moment:
    """Wat er op het hele uur ``ts`` bekend was."""

    ts: int
    t_indoor: float
    t_mass: float
    params: dict
    #: 1R1C: θ₁ (U/C), θ₂ (g/C), θ₃ (1/C) en de constante winst (K/u), of None.
    r1c: list[float] | None
    #: ``SolarForecastLog.lead_factor`` op dat moment, of None.
    factor: float | None

    def to_list(self) -> list:
        return [
            self.ts, round(self.t_indoor, 3), round(self.t_mass, 3),
            {k: round(float(v), 6) for k, v in self.params.items()},
            [float(x) for x in self.r1c] if self.r1c else None,
            round(self.factor, 4) if self.factor is not None else None,
        ]

    @classmethod
    def from_list(cls, row) -> Moment | None:
        try:
            ts, ti, tm, params, r1c, factor = row
            return cls(int(ts), float(ti), float(tm), dict(params),
                       [float(x) for x in r1c] if r1c else None,
                       float(factor) if factor is not None else None)
        except (TypeError, ValueError):
            return None


class ShadowLog:
    """De vastgelegde momenten, één per uur."""

    def __init__(self) -> None:
        self.moments: dict[int, Moment] = {}

    def __len__(self) -> int:
        return len(self.moments)

    def record(self, moment: Moment) -> bool:
        """Eén per uur; een tweede (herstart binnen het uur) telt niet."""
        if moment.ts in self.moments:
            return False
        self.moments[moment.ts] = moment
        return True

    def prune(self, now_ts: float, keep_days: int = KEEP_DAYS) -> None:
        cutoff = now_ts - keep_days * 86400
        for ts in [t for t in self.moments if t < cutoff]:
            del self.moments[ts]

    def to_dict(self) -> dict:
        return {"moments": [m.to_list() for _, m in sorted(self.moments.items())]}

    @classmethod
    def from_dict(cls, data: Mapping | None) -> ShadowLog:
        log = cls()
        for row in (data or {}).get("moments") or []:
            m = Moment.from_list(row)
            if m is not None:
                log.moments[m.ts] = m
        return log


def _hourly(rows) -> dict[int, tuple[float, float, float]]:
    """Per uur (begin): gemiddelde buitentemperatuur, warmte en zon."""
    by: dict[int, list] = {}
    for r in rows:
        vals = (r[_T_OUT], r[_Q_HP], r[_SOL])
        if any(v is None or not math.isfinite(v) for v in vals):
            continue
        by.setdefault(int(r[_TS] // 3600 * 3600), []).append(vals)
    return {h: tuple(float(x) for x in np.mean(v, axis=0))
            for h, v in by.items() if len(v) >= MIN_SAMPLES}


def _truth(ts: np.ndarray, t_in: np.ndarray, at: float) -> float | None:
    if len(ts) == 0:
        return None
    i = int(np.searchsorted(ts, at))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - at) <= TRUTH_TOLERANCE_S:
            if best is None or abs(ts[j] - at) < abs(ts[best] - at):
                best = j
    return float(t_in[best]) if best is not None else None


def _r1c(theta: list[float], t0: float, t_out, sol, q) -> np.ndarray:
    u, g, h, c = theta
    t = t0
    out = np.empty(len(t_out))
    for k in range(len(t_out)):
        t = t + u * (t_out[k] - t) + g * sol[k] + h * q[k] + c
        out[k] = t
    return out


def errors(moment: Moment, hourly, ts, t_in, forecast: SolarForecastLog) -> dict | None:
    """De fout per variant per stap (voorspeld − gemeten), of None als er iets ontbreekt."""
    h0 = moment.ts
    steps = [h0 + k * 3600 for k in range(HORIZON_HOURS)]
    if any(h not in hourly for h in steps):
        return None
    truth = [_truth(ts, t_in, h + 3600) for h in steps]
    if any(v is None for v in truth):
        return None
    t_out = np.array([hourly[h][0] for h in steps])
    q = np.array([hourly[h][1] for h in steps])
    sol_after = np.array([hourly[h][2] for h in steps])
    # Stap k is het uur h0+k; de verwachting van vóór het vastleggen is die van
    # het uur ervoor, vooruitkijktijd k+1.
    fc = [forecast.at(h, k + 1) for k, h in enumerate(steps)]
    truth = np.array(truth)
    out: dict[str, np.ndarray] = {"niets_doen": np.full(HORIZON_HOURS, moment.t_indoor) - truth}
    p = TwoStateParams.from_dict(moment.params)
    out["2r2c_achteraf"] = simulate(p, moment.t_indoor, moment.t_mass, t_out, sol_after, q) - truth
    out["2r2c_geen_zon"] = simulate(
        p, moment.t_indoor, moment.t_mass, t_out, np.zeros(HORIZON_HOURS), q) - truth
    if all(v is not None for v in fc):
        raw = np.array(fc, dtype=float)
        cor = raw * (moment.factor if moment.factor is not None else 1.0)
        out["2r2c_verwacht"] = simulate(p, moment.t_indoor, moment.t_mass, t_out, cor, q) - truth
        out["2r2c_verwacht_ruw"] = simulate(p, moment.t_indoor, moment.t_mass, t_out, raw, q) - truth
        if moment.r1c:
            out["1r1c_verwacht"] = _r1c(moment.r1c, moment.t_indoor, t_out, cor, q) - truth
    out["_zonnig"] = bool(sol_after[:SUNNY_HOURS].max() >= SUNNY_WM2)
    return out


def _stats(errs: list[np.ndarray]) -> dict:
    if not errs:
        return {}
    a = np.array(errs)
    return {
        str(lead): {
            "rmse_k": round(float(np.sqrt(np.mean(a[:, lead - 1] ** 2))), 3),
            "gem_k": round(float(np.mean(a[:, lead - 1])), 3),
        }
        for lead in SCORE_LEADS
    }


def score(log: ShadowLog, rows, forecast: SolarForecastLog, now_ts: float, tz: tzinfo) -> dict:
    """Alle afgeronde momenten doorrekenen; zie de moduletekst."""
    data = sorted((r for r in rows if r[_T_IN] is not None and math.isfinite(r[_T_IN])),
                  key=lambda r: r[_TS])
    ts = np.array([float(r[_TS]) for r in data])
    t_in = np.array([float(r[_T_IN]) for r in data])
    hourly = _hourly(data)
    per: dict[str, list] = {v: [] for v in VARIANTS}
    sunny: dict[str, list] = {v: [] for v in VARIANTS}
    days, sunny_days = set(), set()
    done = 0
    for m in sorted(log.moments.values(), key=lambda m: m.ts):
        if m.ts + (HORIZON_HOURS + 1) * 3600 > now_ts:
            continue
        e = errors(m, hourly, ts, t_in, forecast)
        if e is None:
            continue
        done += 1
        day = datetime.fromtimestamp(m.ts, tz).date()
        days.add(day)
        if e["_zonnig"]:
            sunny_days.add(day)
        for v in VARIANTS:
            if v in e:
                per[v].append(e[v])
                if e["_zonnig"]:
                    sunny[v].append(e[v])
    result = {
        "momenten": len(log),
        "gescoord": done,
        "zonnig": len(sunny["niets_doen"]),
        "dagen": len(days),
        "zonnige_dagen": len(sunny_days),
        "alle": {v: _stats(per[v]) for v in VARIANTS if per[v]},
        "zonnige_uren": {v: _stats(sunny[v]) for v in VARIANTS if sunny[v]},
    }
    result["doel"] = goal(result)
    return result


def goal(result: dict) -> dict:
    """Het doel van stap 1 (MPC_ZON_PLAN.md), op de zonnige starturen."""
    s = (result.get("zonnige_uren") or {}).get("2r2c_verwacht") or {}
    enough = result["dagen"] >= GOAL_DAYS and result["zonnige_dagen"] >= GOAL_SUNNY_DAYS
    if not s:
        return {"gehaald": None, "genoeg_data": False}
    ok = (all(s[str(k)]["rmse_k"] <= v for k, v in GOAL_RMSE_K.items())
          and abs(s["6"]["gem_k"]) <= GOAL_BIAS_K)
    return {"gehaald": ok if enough else None, "op_koers": ok, "genoeg_data": enough}
