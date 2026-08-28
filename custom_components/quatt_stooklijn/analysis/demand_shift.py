"""Dezelfde warmte, verschoven naar de uren met de beste COP.

De gepubliceerde warmtevraag is nu ``UA · max(0, T0 − T_buiten)`` van dít
moment. Deze module herverdeelt diezelfde vraag over het forecast-venster naar
de uren waar de warmtepomp hem het goedkoopst levert.

De randvoorwaarde is hard: ``Σ P' = Σ P``. Er gaat evenveel warmte in, alleen op
andere momenten. Het huis kan dus niet uitgehongerd worden door een rekenfout in
de weging — hooguit ongelukkig verdeeld, en daar vangt de comfortterm van de
firmware het op.

**Wat hier bewust níet in zit: zon en kamertemperatuur.** De firmware trekt
``Kp · e`` al af en ziet zonnewinst via diezelfde comfortterm. Meewegen zou
dubbeltellen — zie de toelichting in ``heat_demand.py``. De weging gebruikt
uitsluitend de buitentemperatuur-forecast en de gemeten COP-curve. Die curve is
een eigenschap van de wárm­tepomp, niet van het huis, en de firmware modelleert
hem nergens. Daarom is dit additief in plaats van overlappend.

De opbrengstschatting is het punt van deze module in de schaduwfase: zonder een
voorspeld getal valt γ niet te kiezen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Bovengrens op de agressiviteit. Boven ~3 wordt de verdeling zo scheef dat
# vrijwel alles in één uur belandt, en dan bepaalt de verzadigingsclamp van de
# firmware het resultaat in plaats van dit model.
GAMMA_MAX = 3.0


@dataclass
class DemandShiftResult:
    """Vlakke en verschoven vraag over het venster, plus de verwachte winst."""

    # Per uur: de vraag zoals hij nu gepubliceerd zou worden. Bewust
    # onafgerond — afronden hoort bij de weergave, en per uur afronden breekt
    # de energie-neutraliteit met een paar tienden watt.
    flat: list[float] = field(default_factory=list)
    # Per uur: dezelfde totale vraag, herverdeeld naar COP.
    shifted: list[float] = field(default_factory=list)
    # Wat er op dit moment gepubliceerd zou worden — uur 0 van beide reeksen.
    now_flat: float | None = None
    now_shifted: float | None = None
    # Geschatte elektriciteitsbesparing over het venster, als fractie.
    # 0,08 betekent 8% minder stroom voor dezelfde warmte.
    expected_saving: float | None = None
    # Uren waarin de verschoven vraag boven het firmwareplafond zou uitkomen.
    # Daar kapt de firmware af en gaat de energie-neutraliteit alsnog verloren.
    hours_above_ceiling: int = 0
    gamma: float = 0.0
    # Diepste voorspelde uitwijking van de kamertemperatuur (K, negatief = kouder).
    worst_drift_k: float | None = None
    # Factor waarmee de verschuiving is teruggeschaald om binnen max_drift_k te
    # blijven. 1,0 = onbeperkt, kleiner = de limiter greep in.
    drift_limit_factor: float = 1.0


def _cop_for_weighting(curve: dict[float, float], temp: float) -> float | None:
    """COP bij deze temperatuur, geklemd op de randen van de curve.

    Anders dan ``cop_performance.reference_cop`` wordt hier wél geklemd in
    plaats van ``None`` teruggegeven. Voor een *weging* is de randwaarde een
    verdedigbare benadering, en het alternatief is slechter: bij vorst onder het
    gemeten bereik zou de herverdeling uitvallen op precies de dagen waarop ze
    het meeste oplevert.

    De klemming werkt bovendien de goede kant op. Onder het bereik is de
    werkelijke COP nóg lager dan de randwaarde, dus het voordeel van wegschuiven
    wordt onderschat, niet overschat.
    """
    if len(curve) < 2:
        return None
    xs = sorted(curve)
    ys = [curve[x] for x in xs]
    return float(np.interp(temp, xs, ys))  # np.interp klemt op de randen


def calculate_demand_shift(
    forecast_temps: list[float],
    reference_curve: dict[float, float],
    ua: float | None,
    t_zero: float | None,
    gamma: float = 0.0,
    ceiling_w: float | None = None,
    thermal_mass_wh_k: float | None = None,
    max_drift_k: float | None = None,
) -> DemandShiftResult:
    """Herverdeel de warmtevraag over het venster naar COP.

    Args:
        forecast_temps: buitentemperatuur per uur, index 0 = nu.
        reference_curve: gemeten COP per temperatuurbin.
        ua: warmteverliescoëfficiënt (W/K).
        t_zero: nulpunt — de buitentemperatuur waarboven niet gestookt wordt.
        gamma: agressiviteit. 0 laat de reeks ongemoeid.
        ceiling_w: firmwareplafond, alleen om te signaleren — er wordt hier
            bewust niet op geklemd, want dat maakt verzadiging onzichtbaar.
        thermal_mass_wh_k: C uit het RC-model, om de kamerdrift te schatten.
        max_drift_k: hoeveel de kamer maximaal mag wegzakken (K, positief).
            Wordt die overschreden, dan wordt de héle verschuiving evenredig
            teruggeschaald in plaats van verworpen — dat houdt het gedrag
            voorspelbaar en de energie-neutraliteit intact.
    """
    result = DemandShiftResult(gamma=gamma)
    if not forecast_temps or ua is None or t_zero is None or ua <= 0:
        return result

    flat = [max(0.0, ua * (t_zero - t)) for t in forecast_temps]
    result.flat = flat
    result.now_flat = round(flat[0], 1)

    total = float(sum(flat))
    gamma = max(0.0, min(GAMMA_MAX, float(gamma)))
    result.gamma = gamma

    # Geen vraag, of uitgeschakeld: de verschoven reeks is de vlakke reeks.
    # Dit is niet alleen een optimalisatie maar de gedefinieerde uit-stand —
    # gamma=0 hoort exact het huidige gedrag te geven.
    if total <= 0 or gamma == 0.0:
        result.shifted = list(flat)
        result.now_shifted = result.now_flat
        result.expected_saving = 0.0
        return result

    cops = [_cop_for_weighting(reference_curve, t) for t in forecast_temps]
    if any(c is None or c <= 0 for c in cops):
        # Zonder bruikbare COP-curve valt er niets te wegen. Terugvallen op de
        # vlakke reeks is dan de juiste uitkomst, niet een foutmelding.
        result.shifted = list(flat)
        result.now_shifted = result.now_flat
        result.expected_saving = 0.0
        return result

    # Alleen uren met een werkelijke vraag doen mee aan de verdeling.
    #
    # Zonder dit masker schuift er warmte naar uren waarin het huis niets nodig
    # heeft — boven het nulpunt is de vlakke vraag nul. Dan zou de firmware
    # gaan stoken boven haar eigen stookgrens, precies de fout waar
    # ``heat_demand.py`` voor waarschuwt. Het valt op zodra alle COP's op de
    # curverand klemmen: de gewichten worden gelijk en de vraag smeert uit over
    # het hele venster.
    active = np.array(flat, dtype=float) > 0
    weights = np.where(active, np.array([c ** gamma for c in cops]), 0.0)
    if weights.sum() <= 0:
        result.shifted = list(flat)
        result.now_shifted = result.now_flat
        result.expected_saving = 0.0
        return result
    shifted = total * weights / weights.sum()

    flat_arr = np.array(flat, dtype=float)

    # Kamerdrift: het cumulatieve warmtetekort gedeeld door de thermische massa.
    #
    # Eerste orde en bewust conservatief: een iets koeler huis verliest ook iets
    # minder warmte, dus de werkelijke uitwijking is kleiner dan deze schatting.
    # De comfortterm van de firmware (Kp · e) corrigeert daar bovenop — die
    # blijft draaien en is de eigenlijke vangnet. Wat hier gebeurt is begrenzen
    # vóórdat dat vangnet nodig is, want elke correctie die de firmware moet
    # maken landt juist op het koude uur met de slechte COP.
    if thermal_mass_wh_k and thermal_mass_wh_k > 0:
        drift = np.cumsum(shifted - flat_arr) / thermal_mass_wh_k
        worst = float(drift.min())
        if max_drift_k and max_drift_k > 0 and worst < -abs(max_drift_k):
            # Evenredig terugschalen naar precies de limiet. Σ(shifted−flat) = 0
            # blijft gelden voor elke factor, dus de energie-neutraliteit
            # overleeft dit ongeschonden.
            factor = abs(max_drift_k) / abs(worst)
            shifted = flat_arr + factor * (shifted - flat_arr)
            result.drift_limit_factor = round(factor, 3)
            drift = np.cumsum(shifted - flat_arr) / thermal_mass_wh_k
            worst = float(drift.min())
        result.worst_drift_k = round(worst, 3)

    result.shifted = [float(p) for p in shifted]
    result.now_shifted = round(float(shifted[0]), 1)

    # Verwachte besparing: stroom is warmte gedeeld door COP, dus de winst zit
    # in het verschuiven naar uren met een hogere noemer.
    cop_arr = np.array(cops, dtype=float)
    elec_flat = float(np.sum(flat_arr / cop_arr))
    elec_shift = float(np.sum(shifted / cop_arr))
    if elec_flat > 0:
        result.expected_saving = round((elec_flat - elec_shift) / elec_flat, 4)

    if ceiling_w is not None and ceiling_w > 0:
        result.hours_above_ceiling = int(np.sum(shifted > ceiling_w))

    return result
