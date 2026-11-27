"""Heat loss analysis (from notebook Cell 9)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .utils import (
    calc_r2,
    cop_energy,
    robust_linear_fit,
    seasonal_cop,
    select_heating,
)


@dataclass
class HeatLossResult:
    """Results from heat loss analysis."""

    heat_loss_coefficient: float | None = None  # W/K
    balance_point: float | None = None  # °C
    slope: float | None = None
    intercept: float | None = None
    r2: float | None = None

    # Scatter data for dashboard
    scatter_data: list[dict] | None = None

    # Heat demand at specific temperatures (with COP if available)
    heat_at_temps: dict[int, dict] | None = None


# COP per buitentemperatuur in de tabel "Warmtevraag per temperatuur": de stookdagen
# binnen ±COP_BAND_K van die temperatuur samen (warmte / stroom). De tabel loopt in
# stappen van 5 K, dus de banden sluiten op elkaar aan.
COP_BAND_K = 2.5
# Minder dagen in de band: geen waarde. Eén of twee dagen is toeval.
COP_MIN_DAYS = 3
# De dagen in de band moeten gemiddeld zo dicht bij de temperatuur liggen. Anders
# is het een waarde van een warmere (of koudere) dag die onder een ander label
# staat: met de koudste stookdag op −3 °C zou "−5 °C" de COP van −3 °C tonen.
COP_MAX_MEAN_OFFSET_K = 1.5


def cop_at_temperature(energy: pd.DataFrame, temp: float) -> float | None:
    """COP bij een buitentemperatuur: warmte / stroom van de stookdagen eromheen.

    Tot v0.10.7-beta.16 was dit ``np.interp`` door de losse dag-COP's: de COP van
    de één of twee dagen die toevallig het dichtst bij lagen, ook niet-stookdagen
    met COP 0, en kouder dan de koudste dag vlak doorgetrokken. Tweakers
    25-11-2026: drie keer 2,2 bij −10, −5 en 0 °C (de koudste dag), en op het
    testhuis 0,0 bij 15 °C. Nu een waarde alleen waar gemeten is, anders None.
    """
    if energy.empty:
        return None
    band = energy[(energy["temp"] - temp).abs() <= COP_BAND_K]
    if len(band) < COP_MIN_DAYS:
        return None
    if abs(float(band["temp"].mean()) - temp) > COP_MAX_MEAN_OFFSET_K:
        return None
    return seasonal_cop(band)


#: Afstand tussen de temperaturen van de COP-tabel (``heat_at_temps``).
COP_TABLE_STEP_K = 5.0


def cop_from_table(heat_at_temps: dict | None, temp: float) -> float | None:
    """COP bij ``temp`` uit de COP-tabel van de warmteverliesanalyse.

    Lineair tussen de twee tabelpunten eromheen, en alleen als die allebei een
    waarde hebben en buren zijn. Daarbuiten None: niet doortrekken, net als de
    tabel zelf. Tot v0.10.7-beta.17 interpoleerde de sensor "geschatte actuele
    COP" nog door de losse dag-COP's, de fout die de tabel in beta.16 kwijtraakte.
    """
    if not heat_at_temps or temp is None:
        return None
    points: dict[float, float] = {}
    for key, value in heat_at_temps.items():
        try:
            t = float(key)
        except (TypeError, ValueError):
            continue
        cop = (value or {}).get("cop") if isinstance(value, dict) else None
        if isinstance(cop, (int, float)) and cop > 0:
            points[t] = float(cop)
    if temp in points:
        return points[temp]
    lower = [t for t in points if t < temp]
    upper = [t for t in points if t > temp]
    if not lower or not upper:
        return None
    t0, t1 = max(lower), min(upper)
    if t1 - t0 > COP_TABLE_STEP_K + 1e-6:
        return None
    return points[t0] + (points[t1] - points[t0]) * (temp - t0) / (t1 - t0)


def calculate_heat_loss(
    df_daily: pd.DataFrame,
    source_name: str = "heat_pump",
) -> HeatLossResult:
    """Calculate heat loss characteristics from daily data.

    Args:
        df_daily: Daily DataFrame with 'avg_temperatureOutside' and 'totalHeatPerHour'
        source_name: Label for the data source
    """
    result = HeatLossResult()

    if df_daily is None or df_daily.empty:
        return result

    cols_needed = ["avg_temperatureOutside", "totalHeatPerHour"]
    if not all(c in df_daily.columns for c in cols_needed):
        return result

    plot_data = df_daily[cols_needed].replace([np.inf, -np.inf], np.nan).dropna()

    if len(plot_data) < 5:
        return result

    # Filter: only genuine heating days (excludes cooling and idle/summer days)
    heating_data = select_heating(plot_data)

    if len(heating_data) < 5:
        return result

    # Two-pass regression with outlier removal
    x_all = heating_data["avg_temperatureOutside"].values
    y_all = heating_data["totalHeatPerHour"].values

    slope, intercept, inlier_mask = robust_linear_fit(x_all, y_all)

    x = x_all[inlier_mask]
    y = y_all[inlier_mask]

    if len(x) < 5:
        return result

    r2 = calc_r2(y, slope * x + intercept)

    result.slope = float(slope)
    result.intercept = float(intercept)
    result.r2 = float(r2)
    result.heat_loss_coefficient = float(-slope)

    if slope != 0:
        result.balance_point = float(-intercept / slope)

    # Heat demand at specific temperatures (with COP if available)
    result.heat_at_temps = {}

    energy = cop_energy(df_daily)

    for temp in [-10, -5, 0, 5, 10, 15]:
        demand = slope * temp + intercept
        heat_value = max(0.0, float(demand))
        cop_value = cop_at_temperature(energy, temp)

        result.heat_at_temps[temp] = {
            "heat": heat_value,
            "cop": cop_value
        }

    # Scatter data for dashboard (only heating days, matching the regression)
    result.scatter_data = [
        {
            "temp": round(float(row["avg_temperatureOutside"]), 1),
            "heat": round(float(row["totalHeatPerHour"]), 0),
        }
        for _, row in heating_data.iterrows()
    ]

    return result
