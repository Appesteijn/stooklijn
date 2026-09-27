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
  lijst met tijdstempels en waarden is; zie ``hourly_prices_from_attributes``.

Bewust zonder Home Assistant, zodat de tijdrekening — DST, weekend, kwartier-
prijzen — testbaar is zonder draaiende HA.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo

# Tijd- en waardesleutels zoals de gangbare prijsintegraties ze gebruiken:
# Nord Pool (HACS) ``raw_today``/``raw_tomorrow`` met start/value, ENTSO-e
# ``prices`` met time/price, EnergyZero en afgeleiden met from/price, Tibber-
# achtigen met startsAt/total. De eerste sleutel die in een punt voorkomt wint.
_TIME_KEYS = ("start", "from", "time", "datetime", "startsAt", "start_time", "date")
_VALUE_KEYS = ("value", "price", "total", "electricity_price", "price_eur_kwh")

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


def _points(attributes: Mapping) -> list[tuple[datetime | str, float]]:
    """Alle (tijd, waarde)-paren die in de attributen te vinden zijn."""
    found: list[tuple[datetime | str, float]] = []
    for value in attributes.values():
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                t = next((item[k] for k in _TIME_KEYS if k in item), None)
                v = next((item[k] for k in _VALUE_KEYS if k in item), None)
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


def hourly_prices_from_attributes(
    attributes: Mapping,
    start: datetime,
    n_hours: int,
    *,
    unit: str | None = None,
    tz: tzinfo | None = None,
) -> list[float]:
    """Uurprijzen vanaf ``start`` uit de attributen van een prijssensor.

    Kwartierprijzen (sinds oktober 2025 op de day-ahead-markt) worden per uur
    gemiddeld. De reeks stopt bij het eerste uur zonder prijs: een gat
    opvullen met een gemiddelde zou een verschuiving naar een verzonnen goedkoop
    uur kunnen opleveren. Wie een kortere reeks terugkrijgt dan gevraagd, kort
    het venster in; bij minder dan ``MIN_PRICED_HOURS`` uur is er niets zinvols
    te verschuiven en is de uitkomst leeg.
    """
    factor = _unit_factor(unit)
    per_hour: dict[datetime, list[float]] = {}
    for t, v in _points(attributes):
        moment = _parse_time(t, tz)
        if moment is None:
            continue
        hour = moment.replace(minute=0, second=0, microsecond=0)
        per_hour.setdefault(hour, []).append(v * factor)

    if start.tzinfo is None:
        start = start.replace(tzinfo=tz or timezone.utc)
    first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

    prices: list[float] = []
    for i in range(n_hours):
        values = per_hour.get(first + timedelta(hours=i))
        if not values:
            break
        prices.append(sum(values) / len(values))
    return prices if len(prices) >= MIN_PRICED_HOURS else []
