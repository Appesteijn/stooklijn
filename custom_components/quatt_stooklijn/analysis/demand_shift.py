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

**De vorm: buigen vanaf de vlakke reeks.** Per uur is ``rᵢ`` wat een kWh
warmte daar kost (``1 / COP``, of ``prijs / COP``), gedeeld door het gemiddelde
over de uren met vraag. De verschoven reeks is

    P'ᵢ = Pᵢ + γ · P̄ · (1 − rᵢ)

met ``P̄`` de gemiddelde vraag over die uren. Dat is de oplossing van "minimaliseer
de stroomkosten plus een kwadratische straf op afwijken van de vlakke reeks",
met γ als de verhouding tussen die twee. γ = 0 is exact de vlakke reeks en de
afwijking groeit lineair met γ: bij γ = 1 krijgt een uur met 10% goedkopere
warmte 10% van de gemiddelde vraag erbij. ``Σ (1 − rᵢ) = 0``, dus het totaal
blijft gelijk zonder bijsturen. Alleen waar een uur onder nul zou komen, wordt
op nul geklemd en het niveau zo verschoven dat de som weer klopt.

Tot en met v0.10.4-beta.4 was het ``P'ᵢ ∝ (COP/prijs)ᵢ^γ``. Die vorm keek alleen
naar het totaal, niet naar de vorm van de vlakke reeks: bij een kleine γ werd de
vraag vrijwel gelijk over het venster uitgesmeerd, en γ = 0 kon alleen als
uitzondering de vlakke reeks teruggeven. Tussen "uit" en "een beetje aan" zat zo
een sprong, en een groot deel van de getoonde winst kwam van het afvlakken.

**Optioneel weegt de prijs mee.** Met een prijsreeks is de kostprijs per uur
``prijs / COP`` in plaats van ``1 / COP``: warmte per euro in plaats van
warmte per kWh. Zonder prijzen, of met een vlakke prijs, is de uitkomst exact de
COP-weging — de prijs is een extra factor, geen ander model. Het huismodel zelf
kent geen prijs; die hoort in wat er geoptimaliseerd wordt, niet in de fysica.

**Aanroeper sinds v0.10.3: de prijsverschuivingssensor.** De eerdere
schaduwsensor en de γ-optie zijn in v0.9.14 verwijderd: een kaart die alleen kon
melden dat er niets werd aangestuurd, en een knop die op elke stand hetzelfde
deed. De prijssensor heeft wél een vraag om te beantwoorden — wat had verschuiven
naar goedkope uren een stookseizoen opgeleverd — en kiest γ per dag zelf via
``scan_gamma``, zodat er geen knop zonder schaalverdeling terugkomt.

**Een eigen basisreeks (``baseline``).** Voor de feedforward naar de firmware
is ``UA · (T0 − T_buiten)`` per uur de juiste reeks: de firmware haalt er zelf
de kamerfout en de zonnewinst vanaf. Voor een bedrag in euro's niet — dan telt
warmte mee die nooit geleverd wordt. Op 27-09-2026 rekende die formule 9 kWh
voor een etmaal waarin het RC-model 0 W gaf, en de warmtepomp had de hele maand
ook 0 W geleverd: de kamer stond 1,5–3,5 K boven het setpoint en de zon maakte
de koude nachten ruim goed. De prijssensor geeft daarom de uurvraag van het
RC-model mee. Zonder ``baseline`` blijft alles zoals het was.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Bovengrens op de agressiviteit. De afwijking is lineair in γ én in de zwaai van
# de kostprijs, dus hoe vlakker de dag, hoe hoger de γ waarbij een comfortgrens
# ingrijpt. Op 09-10-2026 (COP ±10%, prijs bijna vlak) haalde het voorverwarmen
# de 0,5 K pas rond γ 14,5; op een winterdag met −3/+4 °C ligt de driftgrens
# rond γ 1. Met 10 als rand bleef de scan op zo'n zachte dag tegen de rand
# hangen zonder knik te vinden. Daarboven bepalen de limiter en de klem op nul
# het resultaat, niet γ.
GAMMA_MAX = 20.0

# Ondergrens op de prijs in de weging (€/kWh). Het gewicht deelt door de prijs,
# en bij een negatieve of nul-prijs zou één uur alle warmte opeisen. Dat is ook
# fysiek onzin: de warmtepomp heeft een plafond, en daarboven kapt de firmware af.
# De kostenberekening zelf rekent wél met de echte, eventueel negatieve, prijs.
PRICE_FLOOR = 0.01


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
    # Of er met een minimumvermogen is gerekend, en hoeveel uren er daarna nog
    # tussen nul en dat minimum liggen. Dat gebeurt alleen als een losse reeks
    # uren samen minder vraagt dan één uur op het minimum en er geen draaiend
    # buuruur is om het bij te leggen.
    min_power_applied: bool = False
    hours_below_min: int = 0
    gamma: float = 0.0
    # Diepste voorspelde uitwijking van de kamertemperatuur (K, negatief = kouder).
    worst_drift_k: float | None = None
    # Factor waarmee de verschuiving is teruggeschaald om binnen max_drift_k te
    # blijven. 1,0 = onbeperkt, kleiner = de limiter greep in.
    drift_limit_factor: float = 1.0
    # Hoogste voorspelde uitwijking naar boven (K). Voorverwarmen op een
    # goedkoop uur maakt het huis tijdelijk warmer dan nodig.
    peak_drift_k: float | None = None
    # Alleen met prijzen: stroomkosten over het venster, vlak en verschoven (€),
    # en de besparing als fractie van de vlakke kosten.
    cost_flat_eur: float | None = None
    cost_shifted_eur: float | None = None
    expected_cost_saving: float | None = None
    # Wat het extra warmteverlies door voorverwarmen kost (€). Een warmer huis
    # verliest meer: UA · drift per uur, alleen over de uren boven de vlakke
    # baan. Al verrekend in ``cost_shifted_eur``; apart vermeld omdat het de
    # verborgen prijs van voorverwarmen is.
    drift_loss_eur: float | None = None
    # Alleen met prijzen: stroomverbruik over het venster (kWh). Naar een
    # goedkoop maar koud uur schuiven kan méér stroom kosten en toch goedkoper
    # zijn; zonder dit getal is dat niet te zien.
    elec_flat_kwh: float | None = None
    elec_shifted_kwh: float | None = None


@dataclass
class GammaPunt:
    """Wat één gamma-waarde over dit venster zou opleveren."""

    gamma: float
    besparing: float | None = None
    drift_k: float | None = None
    uren_boven_plafond: int = 0
    drift_begrenzing: float = 1.0

    @property
    def schoon(self) -> bool:
        """Rekent het model hier nog, of bepaalt een grens het resultaat?

        Boven het firmwareplafond kapt Power House af en gaat de energie-
        neutraliteit verloren; grijpt de driftlimiter in, dan is de verschuiving
        teruggeschaald en zegt de besparing niet meer wat gamma zou doen. In
        beide gevallen is het getal geen voorspelling meer maar een uitkomst van
        de begrenzing.
        """
        return self.uren_boven_plafond == 0 and self.drift_begrenzing >= 1.0

    @property
    def binnen_plafond(self) -> bool:
        """Blijft de verdeling onder het firmwareplafond?

        Zwakker dan ``schoon``: een teruggeschaalde verschuiving telt hier wél.
        Voor een meting van wat een dag had opgeleverd is dat de juiste maat —
        het begrensde plan houdt zich per constructie aan de comfortgrens en is
        dus uitvoerbaar. Boven het plafond niet: daar kapt de firmware af.
        """
        return self.uren_boven_plafond == 0


@dataclass
class GammaScan:
    """De reeks doorgerekende gamma's, plus welke eruit te kiezen."""

    punten: list[GammaPunt] = field(default_factory=list)
    advies: float | None = None
    advies_besparing: float | None = None


# Hoeveel van de best haalbare winst een lagere gamma nog moet pakken om de
# voorkeur te krijgen. Gamma kost comfort — elke stap trekt de kamer verder weg
# van het setpoint — dus een half procentpunt extra besparing is die stap niet
# waard. Bij gelijke opbrengst wint de rustigste instelling.
ADVIES_DREMPEL = 0.95


def scan_gamma(
    forecast_temps: list[float],
    reference_curve: dict[float, float],
    ua: float | None,
    t_zero: float | None,
    *,
    ceiling_w: float | None = None,
    thermal_mass_wh_k: float | None = None,
    max_drift_k: float | None = None,
    prices: list[float] | None = None,
    max_overshoot_k: float | None = None,
    grove_stap: float = 0.5,
    fijne_stap: float = 0.1,
    begrenzing_toegestaan: bool = False,
    baseline: list[float] | None = None,
    min_power_w: list[float] | None = None,
) -> GammaScan:
    """Reken het hele bereik van gamma door en wijs de bruikbaarste aan.

    Gamma is niet uit te rekenen maar wel af te lezen: de opbrengst loopt niet
    netjes door. Ergens houdt extra agressiviteit op winst op te leveren, en
    even daarboven neemt een begrenzing het over — de driftlimiter schaalt de
    verschuiving terug, of uren komen boven het firmwareplafond. Rond díe knik
    wordt fijner gerekend dan aan de uiteinden, want daar valt de keuze.

    Het advies is de láágste gamma die nog vrijwel de volle winst pakt, niet de
    hoogste opbrengst: zie ADVIES_DREMPEL.

    ``begrenzing_toegestaan`` laat punten meetellen waarop de driftlimiter de
    verschuiving terugschaalde. Voor het kiezen van een vaste knop is dat fout —
    het getal zegt dan niets over wat die gamma doet. Voor een dagmeting is het
    juist goed: op een dag met een scherpe temperatuursprong grijpt de limiter
    al bij de kleinste gamma in, en zonder deze optie noteert de meting nul
    terwijl een begrensde verschuiving wel degelijk iets had opgeleverd.

    Met ``prices`` is de opbrengst de kostenbesparing, zonder de stroom-
    besparing. Dat zijn verschillende dingen: naar een goedkoop maar koud uur
    schuiven kost méér kWh en toch minder euro's.

    Bewust zonder Home Assistant: het gedrag dat ertoe doet — waar de knik ligt
    en welke gamma eruit komt — hoort testbaar te zijn zonder draaiende HA.
    """

    def meet(gamma: float) -> GammaPunt:
        r = calculate_demand_shift(
            forecast_temps,
            reference_curve,
            ua,
            t_zero,
            gamma,
            ceiling_w=ceiling_w,
            thermal_mass_wh_k=thermal_mass_wh_k,
            max_drift_k=max_drift_k,
            prices=prices,
            max_overshoot_k=max_overshoot_k,
            baseline=baseline,
            min_power_w=min_power_w,
        )
        return GammaPunt(
            gamma=round(gamma, 2),
            besparing=(
                r.expected_cost_saving if prices is not None else r.expected_saving
            ),
            drift_k=r.worst_drift_k,
            uren_boven_plafond=r.hours_above_ceiling,
            drift_begrenzing=r.drift_limit_factor,
        )

    if grove_stap <= 0 or fijne_stap <= 0:
        return GammaScan()

    # Geen vraag in het venster: buiten het stookseizoen valt er niets te
    # verschuiven en levert elke gamma exact nul op. Zonder deze uitstap rekent
    # hij het hele bereik door, verfijnt hij rond een willekeurig punt — bij
    # gelijke opbrengst wint immers de laagste gamma — en levert hij een tabel
    # met veertien nullen op die suggereert dat er iets te kiezen valt.
    nulmeting = calculate_demand_shift(
        forecast_temps, reference_curve, ua, t_zero, 0.0, baseline=baseline
    )
    if not nulmeting.flat or sum(nulmeting.flat) <= 0:
        return GammaScan()

    grof = [
        meet(round(grove_stap * i, 2))
        for i in range(1, int(GAMMA_MAX / grove_stap) + 1)
    ]
    def bruikbaar_punt(p: GammaPunt) -> bool:
        ok = p.binnen_plafond if begrenzing_toegestaan else p.schoon
        return ok and p.besparing is not None

    bruikbaar = [p for p in grof if bruikbaar_punt(p)]
    if not bruikbaar:
        return GammaScan(punten=grof)

    # De knik ligt rond de gamma met de hoogste schone opbrengst: net eronder
    # loopt de winst nog op, net erboven vlakt hij af of grijpt een grens in.
    top = max(bruikbaar, key=lambda p: (p.besparing or 0.0, -p.gamma))
    onder = max(fijne_stap, top.gamma - grove_stap)
    boven = min(GAMMA_MAX, top.gamma + grove_stap)

    punten = {p.gamma: p for p in grof}
    stappen = int(round((boven - onder) / fijne_stap))
    for i in range(stappen + 1):
        g = round(onder + i * fijne_stap, 2)
        if g not in punten and 0 < g <= GAMMA_MAX:
            punten[g] = meet(g)

    reeks = [punten[g] for g in sorted(punten)]
    schoon = [p for p in reeks if bruikbaar_punt(p)]
    if not schoon:
        return GammaScan(punten=reeks)

    beste = max(p.besparing or 0.0 for p in schoon)
    if beste <= 0:
        return GammaScan(punten=reeks)

    # Laagste gamma die de drempel haalt — rustiger bij vrijwel dezelfde winst.
    keuze = next(
        p for p in schoon if (p.besparing or 0.0) >= beste * ADVIES_DREMPEL
    )
    return GammaScan(
        punten=reeks, advies=keuze.gamma, advies_besparing=keuze.besparing
    )


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
    prices: list[float] | None = None,
    max_overshoot_k: float | None = None,
    weigh_prices: bool = True,
    baseline: list[float] | None = None,
    min_power_w: list[float] | None = None,
) -> DemandShiftResult:
    """Herverdeel de warmtevraag over het venster naar COP, en optioneel prijs.

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
        prices: stroomprijs per uur (€/kWh), even lang als ``forecast_temps``.
            Weegt mee als noemer en levert de kosten in euro's op. ``None`` =
            alleen COP, precies het gedrag van vóór de prijsweging.
        max_overshoot_k: hoeveel de kamer maximaal boven de vlakke baan mag
            uitkomen (K, positief). Zelfde terugschaling als ``max_drift_k``.
            Ook zonder prijzen nodig: ligt het uur met de beste COP vóór de
            koude piek, dan verwarmt ook de COP-weging voor. Op 09-10-2026 trok
            hij de ochtendpiek naar de avond ervoor, omdat de gemeten curve bij
            13 °C piekt en bij 15 °C weer zakt.
        weigh_prices: ``False`` = de prijzen alleen gebruiken om de kosten uit
            te rekenen, niet in de weging. Zo is een pure COP-verschuiving in
            euro's naast de prijsverschuiving te leggen.
        baseline: vraag per uur (W) die herverdeeld wordt, in plaats van
            ``UA · (t_zero − T)``. Even lang als ``forecast_temps``; ``t_zero``
            is dan niet nodig. ``ua`` wel: die prijst het extra warmteverlies
            van voorverwarmen.
        min_power_w: laagste continue warmte-afgifte per uur (W), zie
            ``min_power``. Dan is elk uur van de verschoven reeks nul of
            minstens dit minimum: binnen een reeks uren eronder draait pas het
            uur waarin de opgespaarde vraag het minimum haalt. ``None`` = geen
            minimum. γ = 0 blijft de
            vlakke reeks: dat is wat de firmware nu doet, pendelen binnen het
            uur inbegrepen.
    """
    result = DemandShiftResult(gamma=gamma)
    if not forecast_temps or ua is None or ua <= 0:
        return result
    if baseline is None and t_zero is None:
        return result
    if baseline is not None and len(baseline) != len(forecast_temps):
        # Zelfde reden als bij de prijzen: een verschoven reeks rekent elk uur
        # met de verkeerde temperatuur en COP.
        return result
    if prices is not None and len(prices) != len(forecast_temps):
        # Een verschoven prijsreeks zou elk uur de verkeerde prijs geven —
        # zonder foutmelding. Dan liever geen uitkomst.
        return result

    if baseline is not None:
        flat = [max(0.0, float(p)) for p in baseline]
    else:
        flat = [max(0.0, ua * (t_zero - t)) for t in forecast_temps]
    result.flat = flat
    result.now_flat = round(flat[0], 1)

    total = float(sum(flat))
    gamma = max(0.0, min(GAMMA_MAX, float(gamma)))
    result.gamma = gamma

    cops = [_cop_for_weighting(reference_curve, t) for t in forecast_temps]
    cops_ok = not any(c is None or c <= 0 for c in cops)

    # Geen vraag, of uitgeschakeld: de verschoven reeks is de vlakke reeks.
    # Dit is niet alleen een optimalisatie maar de gedefinieerde uit-stand —
    # gamma=0 hoort exact het huidige gedrag te geven. Met prijzen komen de
    # kosten er wel bij, zodat de nulmeting een bedrag heeft om tegen af te zetten.
    if total <= 0 or gamma == 0.0:
        result.shifted = list(flat)
        result.now_shifted = result.now_flat
        result.expected_saving = 0.0
        if prices is not None and cops_ok:
            _fill_costs(result, flat, flat, cops, prices, None, ua)
        return result

    if not cops_ok:
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
    # ``heat_demand.py`` voor waarschuwt. Het viel op bij een zomerforecast,
    # toen de oude gewichtsvorm de vraag over het hele venster uitsmeerde.
    flat_arr = np.array(flat, dtype=float)
    active = flat_arr > 0
    # Kostprijs van een kWh warmte per uur: 1/COP, of prijs/COP.
    cost = 1.0 / np.array(cops, dtype=float)
    if prices is not None and weigh_prices:
        cost = cost * np.maximum(np.array(prices, dtype=float), PRICE_FLOOR)
    shifted = _bend(flat_arr, active, cost, gamma)

    # Kamerdrift: het cumulatieve warmtetekort gedeeld door de thermische massa.
    #
    # Eerste orde en bewust conservatief: een iets koeler huis verliest ook iets
    # minder warmte, dus de werkelijke uitwijking is kleiner dan deze schatting.
    # De comfortterm van de firmware (Kp · e) corrigeert daar bovenop — die
    # blijft draaien en is de eigenlijke vangnet. Wat hier gebeurt is begrenzen
    # vóórdat dat vangnet nodig is, want elke correctie die de firmware moet
    # maken landt juist op het koude uur met de slechte COP.
    drift = None
    if thermal_mass_wh_k and thermal_mass_wh_k > 0:
        drift = np.cumsum(shifted - flat_arr) / thermal_mass_wh_k
        worst = float(drift.min())
        peak = float(drift.max())
        # Evenredig terugschalen naar precies de limiet. Σ(shifted−flat) = 0
        # blijft gelden voor elke factor, dus de energie-neutraliteit overleeft
        # dit ongeschonden. Omlaag en omhoog elk hun eigen grens; de strengste
        # bepaalt de factor.
        factor = 1.0
        if max_drift_k and max_drift_k > 0 and worst < -abs(max_drift_k):
            factor = min(factor, abs(max_drift_k) / abs(worst))
        if max_overshoot_k and max_overshoot_k > 0 and peak > abs(max_overshoot_k):
            factor = min(factor, abs(max_overshoot_k) / peak)
        if factor < 1.0:
            shifted = flat_arr + factor * (shifted - flat_arr)
            result.drift_limit_factor = round(factor, 3)
            drift = np.cumsum(shifted - flat_arr) / thermal_mass_wh_k
            worst = float(drift.min())
            peak = float(drift.max())
        result.worst_drift_k = round(worst, 3)
        result.peak_drift_k = round(peak, 3)

    # Aan/uit per uur: na de limiter, zodat die de blokken niet weer uitsmeert.
    # De drift wordt daarna opnieuw berekend; het verplaatsen blijft binnen een
    # reeks uren onder het minimum en houdt die drift dus klein.
    if min_power_w is not None and len(min_power_w) == len(flat):
        shifted, result.hours_below_min = _blocks(
            shifted, np.array(min_power_w, dtype=float), cost
        )
        result.min_power_applied = True
        if drift is not None:
            drift = np.cumsum(shifted - flat_arr) / thermal_mass_wh_k
            result.worst_drift_k = round(float(drift.min()), 3)
            result.peak_drift_k = round(float(drift.max()), 3)

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

    if prices is not None:
        _fill_costs(result, flat, list(shifted), cops, prices, drift, ua)

    return result


def _bend(
    flat: np.ndarray, active: np.ndarray, cost: np.ndarray, gamma: float
) -> np.ndarray:
    """Buig de vlakke reeks naar de goedkope uren: ``P + γ · P̄ · (1 − r)``.

    Zie de moduledocstring voor de afleiding. Uren zonder vraag doen niet mee en
    blijven nul. Komt een uur onder nul, dan wordt het daar geklemd en schuift
    het niveau ``λ`` omlaag tot de som weer klopt (waterfilling). De som is
    monotoon in ``λ``, dus bisectie vindt hem altijd.
    """
    shifted = np.zeros_like(flat)
    if not active.any():
        return shifted
    f = flat[active]
    r = cost[active] / cost[active].mean()
    step = gamma * f.mean()
    total = float(f.sum())

    def verdeel(lam: float) -> np.ndarray:
        return np.maximum(0.0, f + step * (lam - r))

    # Zonder klem is λ = 1 exact, want Σ (1 − r) = 0.
    p = verdeel(1.0)
    if (f + step * (1.0 - r)).min() < 0:
        # Klemmen verhoogt de som, dus λ ligt onder 1. Bij de ondergrens is
        # elk uur nul.
        lo = float(np.min(r - f / step))
        hi = 1.0
        for _ in range(100):
            mid = 0.5 * (lo + hi)
            if verdeel(mid).sum() < total:
                lo = mid
            else:
                hi = mid
        p = verdeel(hi)
        # Het restje van de bisectie over de niet-geklemde uren, zodat de
        # energie-neutraliteit tot op afronding blijft gelden.
        vrij = p > 0
        p[vrij] += (total - p.sum()) / vrij.sum()
    shifted[active] = p
    return shifted


def _blocks(
    shifted: np.ndarray, min_w: np.ndarray, cost: np.ndarray
) -> tuple[np.ndarray, int]:
    """Maak van elke reeks uren onder het minimum aan/uit-blokken.

    Per aaneengesloten reeks uren met ``0 < P < minimum`` wordt in de tijd
    doorgelopen en de gevraagde warmte opgespaard. Zodra het opgespaarde het
    minimum van dat uur haalt, draait dat uur op precies het minimum en schuift
    de rest door naar het volgende uur. Wat aan het eind van de reeks overblijft, gaat naar het
    laatste draaiende uur van de reeks, of anders naar het goedkoopste
    aangrenzende uur dat al draait. Lukt ook dat niet — een losse reeks die
    samen minder vraagt dan één uur op het minimum — dan blijft het in het
    laatste uur staan en telt dat uur als "onder het minimum".

    Bewust geen blok op de goedkoopste plek van de reeks. Na het buigen kan een
    reeks een half etmaal lang zijn, en één blok aan het eind liet het huis op
    09-10-2026 van 03:00 tot 10:00 zonder warmte: dat is een verschuiving buiten
    de driftlimiter om, geen aan/uit-blok. Zo schuift warmte hooguit ongeveer
    één blok op (op het minimum ~0,1 K bij deze thermische massa) en blijft het
    echte verschuiven bij het buigen en de limiter. De som blijft per reeks
    gelijk, dus ook in totaal.

    Op het minimum en niet alles wat opgespaard is: tot en met v0.10.4-beta.5
    leverde een draaiend uur alles, en bij een vraag van ~2 kW tegen een
    minimum van 2,3 kW werd dat elk tweede uur ~4,5 kW met een uur uit ertussen.
    Zo draait de warmtepomp niet; hij loopt op zijn laagste stand en gaat af en
    toe uit.

    Geeft de nieuwe reeks en het aantal uren dat onder het minimum blijft.
    """
    p = shifted.astype(float).copy()
    n = len(p)
    onder = (p > 0) & (p < min_w)
    blijft_onder = 0
    i = 0
    while i < n:
        if not onder[i]:
            i += 1
            continue
        j = i
        while j < n and onder[j]:
            j += 1
        # Reeks [i, j).
        gespaard = 0.0
        laatst_aan = None
        for h in range(i, j):
            gespaard += p[h]
            p[h] = 0.0
            if gespaard >= min_w[h]:
                p[h] = min_w[h]
                gespaard -= min_w[h]
                laatst_aan = h
        if gespaard > 0:
            # De buren liggen per constructie buiten de reeks, dus draaien ze
            # óf op minstens het minimum óf helemaal niet.
            buren = [b for b in (i - 1, j) if 0 <= b < n and p[b] > 0]
            if laatst_aan is not None:
                p[laatst_aan] += gespaard
            elif buren:
                p[min(buren, key=lambda b: cost[b])] += gespaard
            else:
                p[j - 1] = gespaard
                blijft_onder += 1
        i = j
    return p, blijft_onder

def _fill_costs(
    result: DemandShiftResult,
    flat: list[float],
    shifted: list[float],
    cops: list[float],
    prices: list[float],
    drift: np.ndarray | None,
    ua: float,
) -> None:
    """Zet de stroomkosten van beide reeksen in euro's op het resultaat.

    Elke stap is een uur, dus W is hier Wh; stroom is warmte gedeeld door COP.

    De kamerdrift wordt als warmteverlies meegeteld: een huis dat door
    voorverwarmen ``drift`` K warmer staat dan in de vlakke baan verliest per uur
    ``UA · drift`` Wh extra, en die warmte moet ergens vandaan komen. Hij wordt
    geprijsd tegen de gemiddelde euro per kWh warmte van de verschoven reeks.
    Dat is een benadering, maar zonder deze term is voorverwarmen gratis, en dat
    overschat de winst precies in het geval waar de prijsweging om draait.

    Alleen de uren waarin het huis wármer staat tellen. Een kouder huis
    verliest weliswaar minder, maar dat is comfort inleveren en geen besparing:
    zou het meetellen, dan leverde elke verschuiving die de kamer tot de
    driftgrens laat wegzakken er een bonus bij op — op een gewone winterdag een
    vijfde van het bedrag.

    De drift hangt aan de geleerde thermische massa. Is die te hoog geschat, dan
    is de drift te laag en valt ook deze correctie te laag uit.
    """
    flat_arr = np.array(flat, dtype=float)
    shift_arr = np.array(shifted, dtype=float)
    cop_arr = np.array(cops, dtype=float)
    price_arr = np.array(prices, dtype=float)

    cost_flat = float(np.sum(flat_arr / cop_arr / 1000.0 * price_arr))
    cost_shift = float(np.sum(shift_arr / cop_arr / 1000.0 * price_arr))
    result.elec_flat_kwh = round(float(np.sum(flat_arr / cop_arr)) / 1000.0, 3)
    result.elec_shifted_kwh = round(float(np.sum(shift_arr / cop_arr)) / 1000.0, 3)

    loss_eur = 0.0
    heat_kwh = float(shift_arr.sum()) / 1000.0
    if drift is not None and heat_kwh > 0:
        eur_per_kwh_heat = cost_shift / heat_kwh
        extra_heat_kwh = float(np.sum(ua * np.maximum(drift, 0.0))) / 1000.0
        loss_eur = extra_heat_kwh * eur_per_kwh_heat
    result.drift_loss_eur = round(loss_eur, 4)

    result.cost_flat_eur = round(cost_flat, 4)
    result.cost_shifted_eur = round(cost_shift + loss_eur, 4)
    if cost_flat > 0:
        result.expected_cost_saving = round(
            (cost_flat - result.cost_shifted_eur) / cost_flat, 4
        )
    else:
        result.expected_cost_saving = 0.0
