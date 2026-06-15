"""Online 1R1C thermal model with Recursive Least Squares parameter learning.

Physics:
    C × dT_in/dt = Q_hp + g × Q_solar − U × (T_in − T_out)

Where:
    U = heat loss coefficient (W/K)
    C = thermal capacity (Wh/K)
    g = solar gain factor (dimensionless)

Discretised (timestep Δt hours):
    ΔT = Δt × [θ₁×(T_out−T_in) + θ₂×Q_solar + θ₃×Q_hp]

    θ₁ = U/C,  θ₂ = g/C,  θ₃ = 1/C
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

_LOGGER = logging.getLogger(__name__)

# Defaults for cold start (typical Dutch terraced house)
DEFAULT_U = 200.0       # W/K — heat loss coefficient
DEFAULT_C = 5000.0      # Wh/K — thermal capacity
DEFAULT_G_SOLAR = 5.0   # W/(W/m²) — effective window area × SHGC

# RLS tuning
RLS_FORGETTING = 0.998       # effective window ≈ 500 samples ≈ 3 weeks
RLS_INITIAL_COV = 1000.0     # large initial uncertainty
RLS_MIN_UPDATES = 48         # 2 days before model is considered converged

# Sanity bounds for learned parameters
U_MIN, U_MAX = 30.0, 800.0     # W/K
C_MIN, C_MAX = 500.0, 30000.0  # Wh/K
# g_solar: W heat gain per W/m² irradiance = effective_window_area_m2 × SHGC
# Typical house: 5-20 m² windows × 0.3-0.6 SHGC = 1.5-12 W/(W/m²)
G_MIN, G_MAX = 0.0, 20.0       # W/(W/m²)

# Minimum delta-T between indoor/outdoor to include a sample (avoids
# numerical issues when the temperature difference is too small)
MIN_DT_INDOOR_OUTDOOR = 2.0  # °C


@dataclass
class RLSEstimator:
    """Recursive Least Squares estimator with forgetting factor.

    Estimates θ in the linear model y = x'θ, updating one sample at a time.
    """

    n_params: int = 3
    forgetting: float = RLS_FORGETTING
    theta: np.ndarray = field(default=None)
    P: np.ndarray = field(default=None)
    n_updates: int = 0

    def __post_init__(self) -> None:
        if self.theta is None:
            self.theta = np.zeros(self.n_params)
        if self.P is None:
            self.P = np.eye(self.n_params) * RLS_INITIAL_COV

    def initialise_from_physics(self, U: float, C: float, g: float) -> None:
        """Set initial θ from physical parameters (cold start)."""
        self.theta = np.array([U / C, g / C, 1.0 / C])
        # Keep large P so new data quickly corrects defaults
        self.P = np.eye(self.n_params) * RLS_INITIAL_COV

    def update(self, x: np.ndarray, y: float) -> None:
        """One RLS step: x = feature vector (3,), y = measured ΔT."""
        lam = self.forgetting
        Px = self.P @ x
        denom = lam + float(x @ Px)
        if abs(denom) < 1e-12:
            return  # numerical guard
        K = Px / denom
        err = y - float(x @ self.theta)
        self.theta = self.theta + K * err
        self.P = (self.P - np.outer(K, Px)) / lam
        self.n_updates += 1

    @property
    def is_converged(self) -> bool:
        return self.n_updates >= RLS_MIN_UPDATES

    def to_dict(self) -> dict:
        return {
            "theta": self.theta.tolist(),
            "P": self.P.tolist(),
            "n_updates": self.n_updates,
            "forgetting": self.forgetting,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RLSEstimator:
        est = cls(
            n_params=len(data["theta"]),
            forgetting=data.get("forgetting", RLS_FORGETTING),
        )
        est.theta = np.array(data["theta"])
        est.P = np.array(data["P"])
        est.n_updates = data.get("n_updates", 0)
        return est


class OnlineRCModel:
    """1R1C thermal model with online parameter learning."""

    def __init__(self) -> None:
        self._rls = RLSEstimator()
        self._prev_t_indoor: float | None = None
        self._prev_t_outdoor: float | None = None
        self._prev_q_hp: float | None = None
        self._prev_q_solar: float | None = None
        self._prev_timestamp: datetime | None = None
        # Initialise with reasonable defaults
        self._rls.initialise_from_physics(DEFAULT_U, DEFAULT_C, DEFAULT_G_SOLAR)

    def initialise_from_batch(self, U: float) -> None:
        """Use batch-estimated heat loss as better starting point."""
        if U_MIN <= U <= U_MAX:
            self._rls.initialise_from_physics(U, DEFAULT_C, DEFAULT_G_SOLAR)
            _LOGGER.info(
                "RC model initialised from batch heat loss: U=%.1f W/K", U
            )

    def update(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_hp_w: float,
        q_solar_w: float,
        timestamp: datetime,
    ) -> bool:
        """Add a measurement and update the model.

        Call this once per hour.  Returns True if the update was used.
        The RLS update uses the PREVIOUS hour's conditions (T_out, Q_hp,
        Q_solar) to explain the observed ΔT, since those are the conditions
        that caused the temperature change.
        """
        if self._prev_t_indoor is None or self._prev_timestamp is None:
            # First call — store and wait for next
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Calculate time step in hours
        dt_seconds = (timestamp - self._prev_timestamp).total_seconds()
        dt_hours = dt_seconds / 3600.0

        # Guard: only accept roughly hourly samples (0.5–2h)
        if dt_hours < 0.5 or dt_hours > 2.0:
            _LOGGER.info(
                "RC model: skipping, dt=%.1f hours (need 0.5–2h)",
                dt_hours,
            )
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Guard: need meaningful temperature difference for identification
        dt_indoor_outdoor = abs(self._prev_t_indoor - self._prev_t_outdoor)
        if dt_indoor_outdoor < MIN_DT_INDOOR_OUTDOOR:
            _LOGGER.info(
                "RC model: skipping, |T_in-T_out|=%.1f°C < %.1f°C minimum",
                dt_indoor_outdoor, MIN_DT_INDOOR_OUTDOOR,
            )
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Build observation using PREVIOUS conditions:
        # y = ΔT_indoor (what happened), x = Δt × [T_out-T_in, Q_solar, Q_hp] (what caused it)
        delta_t = t_indoor - self._prev_t_indoor
        x = np.array([
            dt_hours * (self._prev_t_outdoor - self._prev_t_indoor),  # θ₁ term
            dt_hours * self._prev_q_solar,                            # θ₂ term
            dt_hours * self._prev_q_hp,                               # θ₃ term
        ])

        self._rls.update(x, delta_t)

        self._prev_t_indoor = t_indoor
        self._prev_t_outdoor = t_outdoor
        self._prev_q_hp = q_hp_w
        self._prev_q_solar = q_solar_w
        self._prev_timestamp = timestamp
        return True

    def predict_t_indoor(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_hp_w: float,
        q_solar_w: float,
        dt_hours: float = 1.0,
    ) -> float:
        """Predict indoor temperature after dt_hours."""
        theta = self._rls.theta
        delta_t = dt_hours * (
            theta[0] * (t_outdoor - t_indoor)
            + theta[1] * q_solar_w
            + theta[2] * q_hp_w
        )
        return t_indoor + delta_t

    def calc_required_power(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_solar_w: float,
        t_setpoint: float,
        dt_hours: float = 1.0,
    ) -> float:
        """Calculate HP power (W) needed to reach t_setpoint after dt_hours.

        Returns 0 if no heating needed (house warm enough or solar sufficient).
        """
        theta = self._rls.theta
        if abs(theta[2]) < 1e-12:
            return 0.0  # model not yet usable

        # Solve: t_setpoint = t_indoor + dt × [θ₁(T_out-T_in) + θ₂×Q_solar + θ₃×Q_hp]
        # => Q_hp = (t_setpoint - t_indoor - dt×θ₁×(T_out-T_in) - dt×θ₂×Q_solar) / (dt×θ₃)
        numerator = (
            t_setpoint
            - t_indoor
            - dt_hours * theta[0] * (t_outdoor - t_indoor)
            - dt_hours * theta[1] * q_solar_w
        )
        q_hp = numerator / (dt_hours * theta[2])
        return max(0.0, q_hp)

    @property
    def is_converged(self) -> bool:
        return self._rls.is_converged and self._params_sane()

    def _params_sane(self) -> bool:
        """Check if learned parameters are within physically plausible bounds."""
        p = self.raw_params
        if p is None:
            return False
        return bool(
            U_MIN <= p["U"] <= U_MAX
            and C_MIN <= p["C"] <= C_MAX
            and G_MIN <= p["g"] <= G_MAX
        )

    @property
    def raw_params(self) -> dict | None:
        """Extract physical parameters from θ. Returns None if θ₃ ≈ 0."""
        theta = self._rls.theta
        if abs(theta[2]) < 1e-12:
            return None
        C = 1.0 / theta[2]
        U = theta[0] * C
        g = theta[1] * C
        return {"U": U, "C": C, "g": g}

    @property
    def params(self) -> dict:
        """Return learned parameters as a user-friendly dict."""
        p = self.raw_params
        if p is None:
            return {
                "converged": False,
                "n_updates": self._rls.n_updates,
            }
        U, C, g = p["U"], p["C"], p["g"]
        tau = C / U if U > 0 else None
        return {
            "U_wk": round(U, 1),
            "C_whk": round(C, 0),
            "g_solar": round(g, 3),
            "tau_hours": round(tau, 1) if tau else None,
            "n_updates": self._rls.n_updates,
            "converged": self.is_converged,
        }

    def to_dict(self) -> dict:
        """Serialise full state for persistence."""
        return {
            "rls": self._rls.to_dict(),
            "prev_t_indoor": self._prev_t_indoor,
            "prev_t_outdoor": self._prev_t_outdoor,
            "prev_q_hp": self._prev_q_hp,
            "prev_q_solar": self._prev_q_solar,
            "prev_timestamp": (
                self._prev_timestamp.isoformat()
                if self._prev_timestamp
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> OnlineRCModel:
        """Deserialise from stored state."""
        model = cls.__new__(cls)
        model._rls = RLSEstimator.from_dict(data["rls"])
        model._prev_t_indoor = data.get("prev_t_indoor")
        model._prev_t_outdoor = data.get("prev_t_outdoor")
        model._prev_q_hp = data.get("prev_q_hp")
        model._prev_q_solar = data.get("prev_q_solar")
        ts = data.get("prev_timestamp")
        model._prev_timestamp = (
            datetime.fromisoformat(ts) if ts else None
        )
        return model


def simulate_6h(
    model: OnlineRCModel,
    t_indoor_now: float,
    t_return: float,
    flow_lph: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    t_setpoint: float = 20.0,
    supply_temp_min: float = 20.0,
    supply_temp_max: float = 55.0,
) -> list[dict]:
    """Simulate 6 hours forward, calculating required HP power per hour.

    Returns a list of dicts with keys:
        hour, q_hp_needed_w, t_indoor_predicted, supply_temp, hp_needed
    """
    SPECIFIC_HEAT = 1.16  # Wh/(L·K)
    results: list[dict] = []
    t_in = t_indoor_now

    n_hours = min(len(forecast_t_outdoor), len(forecast_q_solar), 6)
    for i in range(n_hours):
        t_out = forecast_t_outdoor[i]
        q_solar = forecast_q_solar[i]

        q_hp_needed = model.calc_required_power(
            t_in, t_out, q_solar, t_setpoint
        )
        q_hp_no_solar = model.calc_required_power(
            t_in, t_out, 0.0, t_setpoint
        )

        # Calculate supply temp from required power
        # Use max(t_return, t_in) because when HP is off, the return temp
        # sensor reads low (stagnant water); during operation it would be
        # at least close to indoor temperature.
        effective_return = max(t_return, t_in)
        supply_temp = None
        if q_hp_needed > 0 and flow_lph > 0:
            delta_t = q_hp_needed / (SPECIFIC_HEAT * flow_lph)
            raw_supply = effective_return + delta_t
            supply_temp = round(
                max(supply_temp_min, min(supply_temp_max, raw_supply)), 1
            )

        supply_temp_no_solar = None
        if q_hp_no_solar > 0 and flow_lph > 0:
            delta_t_ns = q_hp_no_solar / (SPECIFIC_HEAT * flow_lph)
            raw_supply_ns = effective_return + delta_t_ns
            supply_temp_no_solar = round(
                max(supply_temp_min, min(supply_temp_max, raw_supply_ns)), 1
            )

        # Predict indoor temp with the calculated heating
        t_in_next = model.predict_t_indoor(t_in, t_out, q_hp_needed, q_solar)

        results.append({
            "hour": i,
            "q_hp_needed_w": round(q_hp_needed),
            "t_indoor_predicted": round(t_in_next, 1),
            "supply_temp": supply_temp,
            "supply_temp_no_solar": supply_temp_no_solar,
            "hp_needed": bool(q_hp_needed > 200),  # MIN_HEATING_WATTS
        })

        t_in = t_in_next

    return results


def simulate_coast_time(
    model: OnlineRCModel,
    t_indoor_now: float,
    comfort_floor: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    step_minutes: int = 15,
    max_hours: int = 12,
) -> dict:
    """Simulate the free cool-down (heat pump OFF) and report how long the
    house can coast before the indoor temperature reaches ``comfort_floor``.

    Used by energy-os to decide whether the heat pump may be throttled during
    an expensive tariff window while the battery covers the load — only as long
    as the house stays warm enough.

    The forecast solar gain is fed in per step, so the learned ``g·Q_solar``
    term slows (or on a sunny day even reverses) the cool-down: **predicted
    sunshine extends the coast time**. This is why the calculation runs on the
    RC model rather than on crude outdoor-temperature buckets.

    Args:
        model: the learned RC model (uses ``predict_t_indoor`` with q_hp = 0).
        t_indoor_now: current indoor temperature (°C).
        comfort_floor: lowest acceptable indoor temperature (°C).
        forecast_t_outdoor: hourly outdoor temperature forecast (°C).
        forecast_q_solar: hourly solar input forecast (W/m², Open-Meteo
            shortwave radiation — same unit the model was trained on).
        step_minutes: simulation resolution.
        max_hours: cap on how far ahead to look.

    Returns dict:
        coast_minutes: int | None — minutes until the floor is reached, capped
            at ``max_hours * 60``. ``None`` if the model is not usable.
        comfort_at_risk: bool — True if the floor is reached within max_hours.
        reaches_floor: bool — False when the house never cools to the floor
            (e.g. enough solar gain), in which case coast_minutes is the cap.
        trajectory: list[dict] — {minute, t_indoor_predicted} samples.
    """
    # Model unusable (θ₃ ≈ 0 → no thermal capacity identified yet).
    if model.raw_params is None:
        return {
            "coast_minutes": None,
            "comfort_at_risk": False,
            "reaches_floor": False,
            "trajectory": [],
        }

    # Already at/below the floor → no headroom to coast.
    if t_indoor_now <= comfort_floor:
        return {
            "coast_minutes": 0,
            "comfort_at_risk": True,
            "reaches_floor": True,
            "trajectory": [{"minute": 0, "t_indoor_predicted": round(t_indoor_now, 2)}],
        }

    dt_hours = step_minutes / 60.0
    total_steps = int(round(max_hours * 60 / step_minutes))
    t_in = t_indoor_now
    elapsed_min = 0.0
    trajectory: list[dict] = [{"minute": 0, "t_indoor_predicted": round(t_in, 2)}]

    for step in range(total_steps):
        # Hold the forecast for the hour this step falls in; persist the last
        # known value when the forecast runs out (assume conditions continue).
        hour_idx = int(elapsed_min // 60)
        t_out = _forecast_at(forecast_t_outdoor, hour_idx)
        q_solar = _forecast_at(forecast_q_solar, hour_idx)
        if t_out is None:
            break  # no outdoor forecast at all → cannot simulate

        t_next = model.predict_t_indoor(t_in, t_out, 0.0, q_solar or 0.0, dt_hours)

        if t_next <= comfort_floor:
            # Linear-interpolate within this step for a smoother minute value.
            if t_in != t_next:
                frac = (t_in - comfort_floor) / (t_in - t_next)
                frac = min(max(frac, 0.0), 1.0)
            else:
                frac = 0.0
            elapsed_min += frac * step_minutes
            trajectory.append(
                {"minute": round(elapsed_min), "t_indoor_predicted": round(comfort_floor, 2)}
            )
            return {
                "coast_minutes": int(round(elapsed_min)),
                "comfort_at_risk": True,
                "reaches_floor": True,
                "trajectory": trajectory,
            }

        t_in = t_next
        elapsed_min += step_minutes
        trajectory.append(
            {"minute": round(elapsed_min), "t_indoor_predicted": round(t_in, 2)}
        )

    # Never reached the floor within max_hours (warm enough / enough sun).
    return {
        "coast_minutes": int(round(max_hours * 60)),
        "comfort_at_risk": False,
        "reaches_floor": False,
        "trajectory": trajectory,
    }


def _forecast_at(values: list[float], idx: int) -> float | None:
    """Return ``values[idx]``, falling back to the last entry (persistence)."""
    if not values:
        return None
    if idx < len(values):
        return values[idx]
    return values[-1]
