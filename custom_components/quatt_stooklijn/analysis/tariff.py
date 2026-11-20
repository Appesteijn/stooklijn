"""Stroomprijs per uur over een venster — vast normaal/dal of dynamisch.

De prijsverschuiving in ``demand_shift`` heeft per uur een prijs nodig. Die komt
uit één van twee bronnen:

* **Normaal/dal** — een vast contract met twee tarieven. Dal geldt doordeweeks
  tussen twee instelbare uren (standaard 23:00–07:00) en, standaard, het hele
  weekend. Feestdagen zijn bij de meeste netbeheerders ook dal, maar dat hangt af
  van de regio en wordt hier niet gemodelleerd: op een feestdag rekent dit met
  normaal, wat de winst van verschuiven licht onderschat.
* **Dynamisch** — een prijssensor die de uurprijzen van vandaag en morgen als
  attribuut meelevert. Welke integratie dat is maakt niet uit zolang het een
  lijst met tijdstempels en waarden is; zie ``prices_from_attributes``.

Bewust zonder Home Assistant, zodat de tijdrekening — DST, weekend, kwartier-
prijzen — testbaar is zonder draaiende HA.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import statistics
from datetime import datetime, timedelta, timezone, tzinfo

# Tijd- en waardesleutels zoals de gangbare prijsintegraties ze gebruiken:
# Nord Pool (HACS) ``raw_today``/``raw_tomorrow`` met start/value, ENTSO-e
# ``prices`` met time/price, EnergyZero en afgeleiden met from/price, Tibber-
# achtigen met startsAt/total, Zonneplan met start_date en een geneste
# ``price_tax_included: {amount: …}``. De eerste sleutel die in een punt
# voorkomt wint, dus ``start_date`` vóór ``date`` en de prijs inclusief
# belasting vóór alles wat ook een prijs exclusief zou kunnen zijn.
_TIME_KEYS = (
    "start", "from", "time", "datetime", "startsAt", "start_time", "start_date",
    "date",
)
_VALUE_KEYS = (
    "value", "price", "total", "electricity_price", "price_eur_kwh",
    "price_tax_included",
)
# Sleutels binnen een geneste prijs ({amount: …} bij Zonneplan).
_NESTED_VALUE_KEYS = ("amount", "value", "total", "price")

# Boven deze mediaan (na de eenheidsomrekening) is een reeks geen €/kWh meer:
# zelfs het day-ahead-plafond van 4.000 €/MWh is 4 €/kWh. Alleen dan wordt de
# schaal op de sensortoestand afgeleid; zie ``_scale_to_state``.
_IMPLAUSIBLE_EUR_KWH = 10.0

# Minder uren dan dit met een bekende prijs, en het venster zegt niets meer over
# de dagzwaai. Dan liever geen uitkomst dan een verschuiving binnen twee uur.
MIN_PRICED_HOURS = 6


def is_dal(
    moment: datetime,
    dal_start_hour: int = 23,
    dal_end_hour: int = 7,
    weekend_dal: bool = True,
) -> bool:
    """Valt dit (lokale) uur in het daltarief?

    ``dal_start_hour`` > ``dal_end_hour`` is een periode over middernacht heen
    (23–7); andersom een periode binnen de dag. Gelijk betekent: geen daluren
    doordeweeks.
    """
    if weekend_dal and moment.weekday() >= 5:
        return True
    hour = moment.hour
    if dal_start_hour == dal_end_hour:
        return False
    if dal_start_hour > dal_end_hour:
        return hour >= dal_start_hour or hour < dal_end_hour
    return dal_start_hour <= hour < dal_end_hour


def normal_dal_prices(
    start: datetime,
    n_hours: int,
    normal: float,
    dal: float,
    dal_start_hour: int = 23,
    dal_end_hour: int = 7,
    weekend_dal: bool = True,
    tz: tzinfo | None = None,
) -> list[float]:
    """Prijs per uur voor een vast contract met normaal- en daltarief.

    ``start`` wordt per uur doorgeteld in UTC en pas daarna naar lokale tijd
    omgezet. Zo telt een DST-nacht precies zoveel uren als hij echt heeft,
    en valt elk uur toch in het juiste lokale tarief.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz or timezone.utc)
    start_utc = start.astimezone(timezone.utc)
    prices: list[float] = []
    for i in range(n_hours):
        moment = start_utc + timedelta(hours=i)
        local = moment.astimezone(tz) if tz is not None else moment.astimezone()
        prices.append(
            dal if is_dal(local, dal_start_hour, dal_end_hour, weekend_dal) else normal
        )
    return prices


def _parse_time(value, tz: tzinfo | None) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or timezone.utc)
    return moment.astimezone(timezone.utc)


def _unit_factor(unit: str | None) -> float:
    """Omrekenfactor naar €/kWh op basis van de eenheid van de prijssensor.

    Voor de weging maakt de eenheid niets uit — die kijkt alleen naar
    verhoudingen tussen uren — maar voor het bedrag in euro's wel.
    """
    if not unit:
        return 1.0
    u = unit.lower().replace(" ", "")
    if "mwh" in u:
        return 0.001
    if u.startswith("ct") or u.startswith("c/") or "cent" in u:
        return 0.01
    return 1.0


def _scalar(v):
    """Pak een geneste prijs uit: ``{amount: 3923884}`` → ``3923884``."""
    if isinstance(v, Mapping):
        return next((v[k] for k in _NESTED_VALUE_KEYS if k in v), None)
    return v


def _scale_to_state(
    points: list[tuple[datetime, float]],
    current_price: float | None,
    now: datetime,
) -> float:
    """Factor (macht van tien) die de reeks gelijkzet aan de sensorwaarde.

    Zonneplan levert bedragen in 10⁻⁷ euro, terwijl de sensor zelf €/kWh
    toont. De eenheid van de sensor zegt dan niets over de attributen. Het
    punt dat nu loopt hoort bij de toestand van de sensor; ligt dat een
    macht van tien ernaast, dan is dat de schaal. Binnen een factor twee,
    zodat incl./excl. belasting of een net verstreken kwartier nog meetelt.

    Alleen als de reeks onmogelijk €/kWh kan zijn (mediaan boven
    ``_IMPLAUSIBLE_EUR_KWH``). Anders kan een gewone reeks die toevallig een
    factor 5–20 van de toestand afligt — spotprijs rond nul, of een toestand
    mét opslag naast attributen zonder — ten onrechte ×10 worden.
    """
    if current_price is None or current_price == 0 or not points:
        return 1.0
    if statistics.median(abs(v) for _, v in points) <= _IMPLAUSIBLE_EUR_KWH:
        return 1.0
    lopend = [(t, v) for t, v in points if t <= now and now - t < timedelta(hours=1)]
    if not lopend:
        return 1.0
    _, v = max(lopend, key=lambda p: p[0])
    if v == 0 or (v > 0) != (current_price > 0):
        return 1.0
    exponent = round(math.log10(current_price / v))
    if exponent == 0:
        return 1.0
    rest = abs(math.log10(current_price / v) - exponent)
    return 10.0**exponent if rest < math.log10(2) else 1.0


def _points(attributes: Mapping) -> list[tuple[datetime | str, float]]:
    """Alle (tijd, waarde)-paren die in de attributen te vinden zijn."""
    found: list[tuple[datetime | str, float]] = []
    for value in attributes.values():
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                t = next((item[k] for k in _TIME_KEYS if k in item), None)
                v = _scalar(next((item[k] for k in _VALUE_KEYS if k in item), None))
                if t is None or v is None:
                    continue
                try:
                    found.append((t, float(v)))
                except (TypeError, ValueError):
                    continue
        elif isinstance(value, Mapping):
            # Sommige integraties leveren {iso-tijd: prijs}.
            for key, v in value.items():
                if isinstance(key, str) and not isinstance(v, (Mapping, list)):
                    try:
                        found.append((key, float(v)))
                    except (TypeError, ValueError):
                        continue
    return found


def prices_from_attributes(
    attributes: Mapping,
    start: datetime,
    n_hours: int,
    *,
    step_minutes: int = 60,
    unit: str | None = None,
    tz: tzinfo | None = None,
    current_price: float | None = None,
    now: datetime | None = None,
) -> list[float]:
    """Prijzen per stap vanaf ``start`` uit de attributen van een prijssensor.

    ``start`` wordt afgerond op het hele uur en de uitkomst telt altijd hele
    uren. Punten binnen één stap worden gemiddeld, dus met ``step_minutes=60``
    worden kwartierprijzen uurprijzen. Levert de bron per uur en wordt er per
    kwartier gevraagd, dan krijgen de vier kwartieren de prijs van dat uur.

    De reeks stopt bij de eerste stap zonder prijs: een gat opvullen met een
    gemiddelde zou een verschuiving naar een verzonnen goedkoop moment kunnen
    opleveren. Wie een kortere reeks terugkrijgt dan gevraagd, kort het venster
    in; bij minder dan ``MIN_PRICED_HOURS`` uur is er niets zinvols te
    verschuiven en is de uitkomst leeg.

    ``current_price`` is de toestand van de prijssensor (al in de eenheid
    ``unit``). Staan de attributen in een andere schaal dan de toestand, zoals
    Zonneplan in 10⁻⁷ euro, dan wordt de reeks daarop gelijkgezet; zie
    ``_scale_to_state``.
    """
    parsed: list[tuple[datetime, float]] = []
    for t, v in _points(attributes):
        moment = _parse_time(t, tz)
        if moment is not None:
            parsed.append((moment, v))
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz or timezone.utc)
    unit_factor = _unit_factor(unit)
    factor = unit_factor * _scale_to_state(
        [(t, v * unit_factor) for t, v in parsed],
        current_price * unit_factor if current_price is not None else None,
        now.astimezone(timezone.utc),
    )
    per_step: dict[datetime, list[float]] = {}
    minutes_in_hour: dict[datetime, set[int]] = {}
    for moment, v in parsed:
        slot = moment.replace(
            minute=moment.minute - moment.minute % step_minutes,
            second=0,
            microsecond=0,
        )
        per_step.setdefault(slot, []).append(v * factor)
        minutes_in_hour.setdefault(slot.replace(minute=0), set()).add(slot.minute)

    if start.tzinfo is None:
        start = start.replace(tzinfo=tz or timezone.utc)
    first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

    steps_per_hour = 60 // step_minutes
    prices: list[float] = []
    for i in range(n_hours * steps_per_hour):
        slot = first + timedelta(minutes=step_minutes * i)
        values = per_step.get(slot)
        if not values and minutes_in_hour.get(slot.replace(minute=0)) == {0}:
            # Bron per uur: dit kwartier valt in dat ene uurpunt.
            values = per_step[slot.replace(minute=0)]
        if not values:
            break
        prices.append(sum(values) / len(values))
    hours = len(prices) // steps_per_hour
    return prices[: hours * steps_per_hour] if hours >= MIN_PRICED_HOURS else []
