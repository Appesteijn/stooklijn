"""Kalibratie van het Power House-huismodel van OpenQuatt.

OpenQuatt's Power House-strategie draagt hetzelfde lineaire warmteverliesmodel
in zich dat deze integratie uit een jaar meetdata schat. Uit
``oq_power_house_strategy.yaml``::

    x       = clamp((T0 - Tout) / (T0 - Tc), 0, 1)
    P_house = Pr * x
    demand  = 20 * P_eff / Pr

Twee dingen vallen daaraan op.

Ten eerste **valt ``Pr`` weg uit het voorwaartse pad**: ``demand = 20 * x``,
ongeacht wat er in ``Rated maximum house power`` staat. Dat getal bepaalt dus
niet de vorm van de stooklijn — het schaalt alleen hoe zwaar de kamerfout-
terugkoppeling (``Kp * e``) meetelt en waar ``P_raw`` verzadigt. Wie het
huismodel wil kalibreren moet naar ``T0`` en ``Tc`` kijken.

Ten tweede is ``Tc`` functioneel iets anders dan zijn naam ("House cold temp")
suggereert: het is de buitentemperatuur waarbij ``x`` de 100% raakt, oftewel
**waarbij de warmtepompen vollast moeten draaien**. Staat hij te koud, dan
vraagt de feedforward structureel te weinig en moet de kamer eerst wegzakken
voordat de terugkoppeling het gat dicht.

Die temperatuur is geen instelling maar een snijpunt: hij ligt waar de
warmtevraag van het huis gelijk wordt aan wat de warmtepompen op dát moment nog
kunnen leveren. Beide lijnen meet deze integratie al::

    vraag       = HLC * (T0 - Tout)
    capaciteit  = cap_slope * Tout + cap_intercept     (de vriescurve)

Gelijkstellen geeft ``Tc``. De platte variant — capaciteit als één vast getal
uit de kniedetectie — is de terugval als er geen bruikbare vriescurve is; die is
gevoeliger, want hij negeert dat de capaciteit zelf met de buitentemperatuur
meezakt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# Grenzen van de OpenQuatt number-entiteiten. Buiten bereik schrijven wordt door
# ESPHome geweigerd, dus hier alvast begrenzen — liever een afgekapt advies dan
# een schrijfactie die stil faalt.
ZERO_POWER_TEMP_MIN = 5.0
ZERO_POWER_TEMP_MAX = 25.0
COLD_TEMP_MIN = -25.0
COLD_TEMP_MAX = 5.0
RATED_POWER_MIN = 1000.0
RATED_POWER_MAX = 15000.0

# Stapgrootte van dezelfde entiteiten.
TEMP_STEP = 0.5
POWER_STEP = 10.0

# Hoe ver buiten het bereik van de Tc-knop een snijpunt nog afgekapt mag worden.
# Een snijpunt van -26 °C is een randgeval en mag naar -25; een snijpunt van
# -290 °C betekent dat vraag en capaciteit elkaar in de praktijk nooit raken, en
# dan is afkappen geen benadering maar een verzinsel. Zo'n vollastpunt bestaat
# fysiek niet en het afgekapte model vraagt overal te weinig.
COLD_TEMP_CLAMP_TOLERANCE = 5.0

# De firmware verwerpt het model als T0 niet minstens deze marge boven Tc ligt
# (``oq_temp_guard_delta_c``); P_house wordt dan NaN en Power House vraagt niets.
TEMP_GUARD_DELTA = 0.5

# Bronnen voor de capaciteit bij Tc, in volgorde van betrouwbaarheid.
SOURCE_CAPABILITY_CURVE = "capaciteitscurve"
SOURCE_KNEE = "knie"

# Vanaf hoeveel verschil een aanpassing het melden waard is. Onder de stapgrootte
# adviseren is zinloos — dat schrijft dezelfde waarde terug.
ZERO_POWER_TEMP_THRESHOLD = 0.5   # °C
COLD_TEMP_THRESHOLD = 0.5         # °C
RATED_POWER_THRESHOLD = 250.0     # W


def _round_to_step(value: float, step: float) -> float:
    return round(value / step) * step


# Waar de gebruikte T0 vandaan komt.
T0_FROM_CONTROLLER = "regelaar"
T0_FROM_MEASUREMENT = "meting"


@dataclass(frozen=True)
class PowerHouseCalibration:
    """De drie getallen die het Power House-huismodel vastleggen."""

    zero_power_temp: float   # T0 — buitentemp waarbij de warmtevraag nul is
    cold_temp: float         # Tc — buitentemp waarbij de pompen vollast draaien
    rated_power: float       # Pr — warmtevraag bij Tc
    capacity_source: str     # waar de capaciteit bij Tc vandaan komt
    full_output_power: float # ongeafgeronde warmtevraag bij Tc, ter controle
    zero_power_temp_source: str      # T0_FROM_CONTROLLER of T0_FROM_MEASUREMENT
    balance_point_measured: float    # wat de regressie zegt, puur ter informatie


def calc_power_house_calibration(
    heat_loss_coefficient: float | None,
    balance_temp: float | None,
    *,
    capability_slope: float | None = None,
    capability_intercept: float | None = None,
    knee_power: float | None = None,
    controller_zero_power_temp: float | None = None,
) -> PowerHouseCalibration | None:
    """Leid Tc en Pr af, gegeven een T0 en het gemeten huismodel.

    **T0 is een invoer, geen advies.** Het is de buitentemperatuur waarbij de
    warmtevraag nul wordt, en juist dáár heeft de meting niets te zeggen: boven
    de stookgrens wordt er niet gestookt, dus die dagen vallen uit de regressie.
    Bij deze woning ligt de warmste waarneming op 15,2 °C terwijl de fit het
    nulpunt op 16,7 °C legt — anderhalve graad extrapolatie, in een gebied waar
    de ruis even groot is als het signaal. Een advies van 16,5 in plaats van de
    ingestelde 16,0 zou precisie suggereren die er niet is.

    Wat de data wél draagt is de helling, en daarmee Tc: het snijpunt van de
    vraaglijn met de capaciteitscurve ligt midden in het koude gebied waar de
    meeste meetdagen zitten.

    Daarom rekent deze functie Tc en Pr uit *tegen de T0 die de regelaar al
    heeft staan*. Dat houdt het drietal onderling consistent — Tc en Pr hangen
    allebei van T0 af, dus je kunt er niet één van laten staan en de andere twee
    tegen een ander nulpunt uitrekenen. Zonder opgegeven T0 valt hij terug op
    het gemeten balanspunt, voor een installatie waar nog niets is ingesteld.

    Geeft ``None`` zodra een van de ingrediënten ontbreekt of het resultaat
    fysisch nergens op slaat. Een half advies is hier erger dan geen advies: deze
    getallen gaan rechtstreeks naar de regelaar.
    """
    if not heat_loss_coefficient or heat_loss_coefficient <= 0:
        return None
    if balance_temp is None:
        return None

    hlc = float(heat_loss_coefficient)
    measured_balance = float(balance_temp)

    # T0 uit de regelaar als die er is; anders het gemeten balanspunt.
    if controller_zero_power_temp is not None:
        t0_raw = float(controller_zero_power_temp)
        t0_source = T0_FROM_CONTROLLER
    else:
        t0_raw = measured_balance
        t0_source = T0_FROM_MEASUREMENT

    # T0 eerst afronden: Tc en Pr worden hieruit afgeleid, en het heeft geen zin
    # ze te baseren op een waarde die straks toch afgerond de firmware in gaat.
    t0 = _round_to_step(
        min(ZERO_POWER_TEMP_MAX, max(ZERO_POWER_TEMP_MIN, t0_raw)),
        TEMP_STEP,
    )

    def _plausible(candidate: float) -> bool:
        """Is dit een vollastpunt dat deze installatie ooit bereikt?"""
        if candidate >= t0 - TEMP_GUARD_DELTA:
            return False
        return (
            COLD_TEMP_MIN - COLD_TEMP_CLAMP_TOLERANCE
            <= candidate
            <= COLD_TEMP_MAX + COLD_TEMP_CLAMP_TOLERANCE
        )

    tc_raw: float | None = None
    source: str | None = None

    # Voorkeur: snijpunt van vraaglijn en capaciteitslijn.
    if capability_slope is not None and capability_intercept is not None:
        denominator = hlc + float(capability_slope)
        # Een capaciteitslijn die met de buitentemperatuur meestijgt (positieve
        # helling) snijdt de dalende vraaglijn precies één keer. Is de noemer
        # nul of negatief, dan lopen ze uiteen of evenwijdig en bestaat er geen
        # zinnig snijpunt.
        if denominator > 0:
            candidate = (hlc * t0 - float(capability_intercept)) / denominator
            if _plausible(candidate):
                tc_raw = candidate
                source = SOURCE_CAPABILITY_CURVE

    # Terugval: capaciteit als één vast getal uit de kniedetectie.
    if tc_raw is None and knee_power and knee_power > 0:
        candidate = t0 - float(knee_power) / hlc
        if _plausible(candidate):
            tc_raw = candidate
            source = SOURCE_KNEE

    if tc_raw is None or source is None:
        _LOGGER.debug(
            "Power House-kalibratie: geen bruikbare capaciteitsschatting "
            "(helling=%s, intercept=%s, knie=%s)",
            capability_slope,
            capability_intercept,
            knee_power,
        )
        return None

    tc = _round_to_step(
        min(COLD_TEMP_MAX, max(COLD_TEMP_MIN, tc_raw)), TEMP_STEP
    )
    # Afronden en begrenzen kan Tc tegen T0 aan duwen; de firmware-marge blijft leidend.
    tc = min(tc, t0 - TEMP_GUARD_DELTA)

    # Pr uit de *afgeronde* T0 en Tc, zodat het drietal onderling klopt met wat
    # er daadwerkelijk in de firmware komt te staan.
    rated_raw = hlc * (t0 - tc)
    rated = _round_to_step(
        min(RATED_POWER_MAX, max(RATED_POWER_MIN, rated_raw)), POWER_STEP
    )

    return PowerHouseCalibration(
        zero_power_temp=t0,
        cold_temp=tc,
        rated_power=rated,
        capacity_source=source,
        full_output_power=round(rated_raw, 1),
        zero_power_temp_source=t0_source,
        balance_point_measured=round(measured_balance, 2),
    )
