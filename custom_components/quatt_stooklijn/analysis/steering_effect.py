"""Sturingseffect: wat een knop oplevert, niet hoe goed hij de CiC naspeelt.

De foutsensoren (advies − werkelijke aanvoer) meten hoe dicht een advies bij
de aanvoer ligt die de installatie zelf koos. Een max. aanvoertemperatuur die
precies doet wat de CiC al deed, voegt niets toe; een advies dat eronder ligt kan juist goed
zijn. Voor de vraag "stuurt dit beter" tellen andere dingen:

* **comfort** — hoe ver en hoe lang de kamer onder (of boven) het setpoint zat;
* **verbruik** — kWh stroom per graaddag, en de COP;
* **slijtage** — compressorstarts per stookuur;
* **gas** — het aandeel ketelwarmte bij een hybride;
* **tegen de max. aanvoertemperatuur** — hoeveel stooktijd de aanvoer tegen
  de ingestelde maximale aanvoertemperatuur aanliep. Gebeurt dat niet, dan
  begrenst die knop niets en is een verschil ook niet zijn effect.

Per dag wordt bijgehouden welke instelling van de knoppen actief was
(``config``), zodat dagen met verschillende instellingen naast elkaar te zetten
zijn. Een harde A/B-test is het niet: weer en gedrag verschillen per week. De
graaddagen (op het balanspunt van het huis) en de comfortmaat houden het
vergelijk zo eerlijk als het met gewone dagen kan.

Dit bestand bevat alleen rekenwerk, zonder Home Assistant, zodat het gedrag
zonder draaiende HA te toetsen is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Meer dan zoveel onder het setpoint telt als te koud. Kleiner dan dit is de
#: normale schommeling van een thermostaat.
COLD_K = 0.3
#: Meer dan zoveel boven het setpoint telt als te warm (zon, koken, overshoot).
WARM_K = 0.5
#: Aanvoer binnen zoveel van de max. aanvoertemperatuur telt als "ertegen". Ruim,
#: omdat aanvoersensoren onderling tot ~0,8 K verschillen (zie supply_check).
CEILING_TOLERANCE_K = 1.0
#: Onder dit warmtevermogen stookt de warmtepomp niet (standby, pomp).
MIN_HEATING_W = 200.0
#: Een gat tussen twee metingen telt hooguit zo lang mee. Na een herstart
#: anders één meting voor een heel uur laten tellen vertekent de dag.
MAX_STEP_MINUTES = 10.0
#: Een dag hoort bij één instelling als die minstens dit deel van de tijd
#: actief was. Anders heet hij "gemengd" en telt hij niet mee in het vergelijk.
DOMINANT_SHARE = 0.8
#: Een dag telt pas mee in het vergelijk met minstens zoveel graaddagen. Op
#: een dag zonder warmtevraag zegt kWh per graaddag niets.
MIN_DEGREE_DAYS = 1.0
#: Minimaal zoveel meetminuten voor een volledige dag (20 uur).
MIN_DAY_MINUTES = 20 * 60
#: Zoveel dagen worden bewaard.
KEEP_DAYS = 120
#: Graaddagbasis als het balanspunt van het huis (nog) niet bekend is.
DEFAULT_BASE_TEMP = 18.0

MIXED = "gemengd"


@dataclass
class Sample:
    """Eén meting. Ontbrekende waarden zijn None."""

    config: str
    t_indoor: float | None = None
    setpoint: float | None = None
    t_outdoor: float | None = None
    p_el_w: float | None = None
    p_heat_w: float | None = None
    p_gas_w: float | None = None
    supply: float | None = None
    ceiling: float | None = None
    flow_lph: float | None = None
    base_temp: float = DEFAULT_BASE_TEMP


@dataclass
class Day:
    """Optelling van één kalenderdag (lokale tijd)."""

    date: str
    minutes: float = 0.0
    config_minutes: dict[str, float] = field(default_factory=dict)
    dev_k_min: float = 0.0
    dev_min: float = 0.0
    cold_min: float = 0.0
    warm_min: float = 0.0
    el_wh: float = 0.0
    el_min: float = 0.0
    heat_wh: float = 0.0
    gas_wh: float = 0.0
    degree_hours: float = 0.0
    heating_min: float = 0.0
    ceiling_known_min: float = 0.0
    ceiling_min: float = 0.0

    def add(self, s: Sample, minutes: float, min_flow_lph: float) -> None:
        """Tel één meting mee voor ``minutes`` minuten."""
        if minutes <= 0:
            return
        minutes = min(minutes, MAX_STEP_MINUTES)
        h = minutes / 60.0
        self.minutes += minutes
        self.config_minutes[s.config] = self.config_minutes.get(s.config, 0.0) + minutes

        if s.t_indoor is not None and s.setpoint is not None:
            dev = s.t_indoor - s.setpoint
            self.dev_k_min += dev * minutes
            self.dev_min += minutes
            if dev < -COLD_K:
                self.cold_min += minutes
            elif dev > WARM_K:
                self.warm_min += minutes

        if s.p_el_w is not None:
            self.el_wh += max(0.0, s.p_el_w) * h
            self.el_min += minutes
        if s.p_heat_w is not None:
            self.heat_wh += max(0.0, s.p_heat_w) * h
        if s.p_gas_w is not None:
            self.gas_wh += max(0.0, s.p_gas_w) * h
        if s.t_outdoor is not None:
            self.degree_hours += max(0.0, s.base_temp - s.t_outdoor) * h

        heating = (
            s.p_heat_w is not None
            and s.p_heat_w > MIN_HEATING_W
            and (s.flow_lph is None or s.flow_lph >= min_flow_lph)
        )
        if heating:
            self.heating_min += minutes
            if s.ceiling is not None and s.supply is not None:
                self.ceiling_known_min += minutes
                if s.supply >= s.ceiling - CEILING_TOLERANCE_K:
                    self.ceiling_min += minutes

    @property
    def config(self) -> tuple[str, float]:
        """De instelling die deze dag bepaalde, en haar aandeel in de tijd."""
        if not self.config_minutes or self.minutes <= 0:
            return MIXED, 0.0
        key, mins = max(self.config_minutes.items(), key=lambda kv: kv[1])
        share = mins / self.minutes
        return (key if share >= DOMINANT_SHARE else MIXED), share

    def record(self, starts: int | None) -> dict:
        """De dag als vast verslag, zoals hij bewaard en getoond wordt."""
        config, share = self.config
        graaddagen = self.degree_hours / 24.0
        stookuren = self.heating_min / 60.0
        el_compleet = self.minutes > 0 and self.el_min >= 0.9 * self.minutes
        return {
            "datum": self.date,
            "instelling": config,
            "aandeel_instelling": round(share, 2),
            "meeturen": round(self.minutes / 60.0, 1),
            "graaddagen": round(graaddagen, 2),
            "kamer_afwijking_k": (
                round(self.dev_k_min / self.dev_min, 2) if self.dev_min else None
            ),
            "uren_te_koud": round(self.cold_min / 60.0, 1),
            "uren_te_warm": round(self.warm_min / 60.0, 1),
            "kwh_stroom": round(self.el_wh / 1000.0, 2) if el_compleet else None,
            "kwh_warmte": round(self.heat_wh / 1000.0, 2),
            "kwh_gas": round(self.gas_wh / 1000.0, 2),
            "cop": (
                round(self.heat_wh / self.el_wh, 2)
                if el_compleet and self.el_wh > 0 else None
            ),
            "stookuren": round(stookuren, 1),
            "starts": starts,
            "tegen_max_aanvoer_pct": (
                round(100.0 * self.ceiling_min / self.ceiling_known_min, 1)
                if self.ceiling_known_min else None
            ),
        }

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> Day:
        day = cls(date=data["date"])
        for key, value in data.items():
            if hasattr(day, key):
                setattr(day, key, value)
        day.config_minutes = dict(data.get("config_minutes") or {})
        return day


def counts_for_comparison(rec: dict) -> bool:
    """Telt deze dag mee in het vergelijk tussen instellingen?

    Alleen hele dagen met één duidelijke instelling en echte warmtevraag.
    """
    return (
        rec.get("instelling") not in (None, MIXED)
        and (rec.get("meeturen") or 0) * 60 >= MIN_DAY_MINUTES
        and (rec.get("graaddagen") or 0) >= MIN_DEGREE_DAYS
    )


def summarize(records: list[dict]) -> dict[str, dict]:
    """Per instelling: gemiddelden over de dagen die meetellen.

    Sommen eerst, delen daarna: een gemiddelde van daggemiddelden zou een
    zachte dag met weinig stookuren even zwaar laten wegen als een koude.
    """
    groups: dict[str, list[dict]] = {}
    for rec in records:
        if counts_for_comparison(rec):
            groups.setdefault(rec["instelling"], []).append(rec)

    out: dict[str, dict] = {}
    for config, recs in groups.items():
        n = len(recs)
        dd = sum(r["graaddagen"] for r in recs)
        el_recs = [r for r in recs if r.get("kwh_stroom") is not None]
        el = sum(r["kwh_stroom"] for r in el_recs)
        el_dd = sum(r["graaddagen"] for r in el_recs)
        heat_el = sum(r["kwh_warmte"] for r in el_recs)
        heat = sum(r["kwh_warmte"] for r in recs)
        gas = sum(r["kwh_gas"] for r in recs)
        start_recs = [r for r in recs if r.get("starts") is not None]
        starts = sum(r["starts"] for r in start_recs)
        start_uren = sum(r["stookuren"] for r in start_recs)
        dev_recs = [r for r in recs if r.get("kamer_afwijking_k") is not None]
        tegen_max = [r for r in recs if r.get("tegen_max_aanvoer_pct") is not None]
        max_uren = sum(r["stookuren"] for r in tegen_max)
        out[config] = {
            "dagen": n,
            "graaddagen": round(dd, 1),
            "kwh_stroom_per_graaddag": round(el / el_dd, 3) if el_dd else None,
            "cop": round(heat_el / el, 2) if el else None,
            "kamer_afwijking_k": (
                round(
                    sum(r["kamer_afwijking_k"] * r["meeturen"] for r in dev_recs)
                    / sum(r["meeturen"] for r in dev_recs),
                    2,
                )
                if dev_recs else None
            ),
            "uren_te_koud_per_dag": round(sum(r["uren_te_koud"] for r in recs) / n, 1),
            "uren_te_warm_per_dag": round(sum(r["uren_te_warm"] for r in recs) / n, 1),
            "starts_per_stookuur": round(starts / start_uren, 2) if start_uren else None,
            "gas_aandeel_pct": (
                round(100.0 * gas / (gas + heat), 1) if (gas + heat) > 0 else None
            ),
            "tegen_max_aanvoer_pct": (
                round(
                    sum(r["tegen_max_aanvoer_pct"] * r["stookuren"] for r in tegen_max) / max_uren, 1
                )
                if max_uren else None
            ),
        }
    return out


#: Stand van de max. aanvoertemperatuur als de warmtevraag OpenQuatt Power
#: House rechtstreeks aanstuurt: de knop schrijft dan niets (zie
#: ChMaxWaterController._async_tick).
PAUSED = "gepauzeerd"
#: Op de 1R1C-bron gezet terwijl het huismodel nog niet online is.
FALLBACK_1R1C = "1r1c_terugval"

#: Namen zoals in de opties ("Stooklijn", "MPC"), niet de interne waarden.
_NAMES = {
    "uit": "uit",
    "stooklijn": "Stooklijn",
    "mpc": "MPC",
    "1r1c": "Huismodel (1R1C)",
    # Op 1R1C gezet, maar het huismodel is nog niet online: dan stuurt het
    # MPC-advies. Apart, zodat zulke dagen het 1R1C niet vertekenen.
    FALLBACK_1R1C: "Huismodel (1R1C), terugval op MPC",
    PAUSED: "gepauzeerd (warmtevraag stuurt)",
}


def config_label(max_water: str, sound: str, demand: bool) -> str:
    """Leesbare, vaste sleutel voor de stand van de knoppen.

    Dezelfde woorden als in de opties en op de kaarten, zodat de stand zonder
    uitleg te lezen is. Voorbeeld:
    ``max. aanvoertemp: MPC · soundslider: uit · warmtevraag OpenQuatt: uit``.
    """
    return (
        f"max. aanvoertemp: {_NAMES.get(max_water, max_water)}"
        f" · soundslider: {_NAMES.get(sound, sound)}"
        f" · warmtevraag OpenQuatt: {'aan' if demand else 'uit'}"
    )
