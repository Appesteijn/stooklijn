"""Afgiftemodel: hoeveel water­temperatuur boven de kamer er nodig is voor een vermogen.

Radiatoren en vloerverwarming geven warmte af volgens

    P = K · (T_water,gem − T_kamer)^n

met n ≈ 1,3 voor radiatoren en ≈ 1,1 voor vloerverwarming. Met het debiet
volgt daaruit de aanvoer: T_aanvoer = T_gem + P / (1,16 · debiet) / 2.

Waarom (analyse 22-11-2026): zonder dit model rekende de integratie de aanvoer
uit de retour. De stooklijnpunten met een vaste retour van 28 °C en 800 l/u,
de vooruitblik met de retour van nu voor alle twaalf uur, en de max.
aanvoertemperatuur ook met de retour van nu als de pomp stilstaat (dan is
dat afgekoeld water). Getoetst op het gemeten huis, november 2026, 708
stookuren, fit op de ene helft van de weken en getoetst op de andere:

    benodigde aanvoer                    gem. fout   gem. |fout|
    afgiftemodel                          +0,04 K      0,40 K
    vaste retour 28 °C, 800 l/u           +1,79 K      1,83 K
    retour van vier uur eerder            −0,11 K      0,61 K
    retour van dit uur                    +0,01 K      0,09 K

De retour van dit uur blijft dus het beste voor *nu*, als de pomp draait. Het
afgiftemodel is voor alles daarbuiten.

Geleerd uit uurgemiddelden van de recorder, alleen uren waarin de pomp het
hele uur gedraaid lijkt te hebben: genoeg warmte en debiet, en de warmte uit
debiet × ΔT klopt met de gemeten warmte.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: Uren waarin dit niet geldt tellen niet mee.
MIN_HEAT_W = 1500.0
MIN_FLOW_LPH = 300.0
#: Warmte uit debiet × ΔT mag zoveel afwijken van de gemeten warmte.
MAX_HEAT_MISMATCH = 0.25
#: Zo veel bruikbare uren voordat het model iets zegt.
MIN_HOURS = 48
#: Kleinste spreiding in (T_gem − T_kamer) om de exponent te kunnen bepalen.
MIN_SPREAD_K = 3.0
#: Grenzen aan de exponent. Daarbuiten is het geen afgifte maar ruis.
EXPONENT_BOUNDS = (1.0, 1.6)
#: Venster van de recorderdata.
WINDOW_DAYS = 60

_CP = 1.16  # Wh/(l·K)


@dataclass(frozen=True)
class EmitterModel:
    """``P = k · (T_gem − T_kamer)^n``, plus het gebruikelijke debiet."""

    k: float
    n: float
    flow_lph: float
    hours: int
    #: Gemiddelde |fout| in de aanvoer op de eigen uren (K).
    mae_k: float
    #: De aanvoer die bij een buitentemperatuur hoort, zoals de installatie
    #: stookte: (buitentemperatuur, aanvoer) per graad. Ter informatie op de
    #: sensor; de COP-correctie rekent ten opzichte van de vlakke reeks (op
    #: zijn minst het minimumvermogen), want deze tabel kent alleen de uren
    #: waarin de pomp het hele uur draaide en mist dus de zachte dagen.
    supply_by_outdoor: list[tuple[float, float]] = field(default_factory=list)
    #: De kamertemperatuur waarbij het geleerd is (mediaan van de stookuren):
    #: wat het huis werkelijk aanhoudt. Voor de stooklijnpunten, die anders
    #: een kamer moeten aannemen; het setpoint zou ze 's nachts laten zakken.
    room_c: float = 20.0

    def mean_water(self, power_w: float, t_room: float) -> float:
        if power_w <= 0:
            return t_room
        return t_room + (power_w / self.k) ** (1.0 / self.n)

    def supply(self, power_w: float, t_room: float, flow_lph: float | None = None) -> float:
        """Benodigde aanvoer voor ``power_w`` bij deze kamertemperatuur."""
        flow = flow_lph if flow_lph and flow_lph >= MIN_FLOW_LPH else self.flow_lph
        tm = self.mean_water(power_w, t_room)
        return tm + max(0.0, power_w) / (_CP * flow) / 2.0

    def usual_supply(self, t_outdoor: float) -> float | None:
        """De aanvoer die normaal bij deze buitentemperatuur hoort."""
        if not self.supply_by_outdoor:
            return None
        xs = [p[0] for p in self.supply_by_outdoor]
        ys = [p[1] for p in self.supply_by_outdoor]
        return float(np.interp(t_outdoor, xs, ys))

    def to_dict(self) -> dict:
        return {
            "k": round(self.k, 2), "n": round(self.n, 3),
            "debiet_lph": round(self.flow_lph), "uren": self.hours,
            "fout_aanvoer_k": round(self.mae_k, 2),
            "kamer_c": round(self.room_c, 2),
            "aanvoer_per_buitentemp": [[round(a, 1), round(b, 2)]
                                       for a, b in self.supply_by_outdoor],
        }

    @classmethod
    def from_dict(cls, d: dict) -> EmitterModel | None:
        try:
            return cls(
                k=float(d["k"]), n=float(d["n"]), flow_lph=float(d["debiet_lph"]),
                hours=int(d["uren"]), mae_k=float(d["fout_aanvoer_k"]),
                supply_by_outdoor=[(float(a), float(b))
                                   for a, b in d.get("aanvoer_per_buitentemp", [])],
                room_c=float(d.get("kamer_c", 20.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def fit_emitter(
    supply, ret, flow, heat_w, t_room, t_outdoor,
) -> tuple[EmitterModel | None, str]:
    """Fit op uurgemiddelden. Geeft ``(model, reden)``; model None als het niet kan."""
    a = [np.asarray(v, dtype=float) for v in (supply, ret, flow, heat_w, t_room, t_outdoor)]
    sup, rt, fl, q, tr, to = a
    with np.errstate(divide="ignore", invalid="ignore"):
        calc = _CP * fl * (sup - rt)
        ok = (
            np.isfinite(sup) & np.isfinite(rt) & np.isfinite(fl) & np.isfinite(q)
            & np.isfinite(tr) & np.isfinite(to)
            & (q > MIN_HEAT_W) & (fl > MIN_FLOW_LPH)
            & (np.abs(calc - q) / q < MAX_HEAT_MISMATCH)
        )
    dT = (sup + rt) / 2.0 - tr
    ok &= dT > 1.0
    n_ok = int(ok.sum())
    if n_ok < MIN_HOURS:
        return None, f"te weinig stookuren ({n_ok} van {MIN_HOURS})"
    x, y = dT[ok], q[ok]
    if float(np.percentile(x, 90) - np.percentile(x, 10)) < MIN_SPREAD_K:
        return None, "te weinig spreiding in watertemperatuur"
    # De exponent gekozen op de fout in de aanvoer, waar het model voor is,
    # niet op de fout in log-vermogen: die weegt de uren met weinig vermogen
    # te zwaar. Per exponent volgt K in gesloten vorm.
    lo, hi = EXPONENT_BOUNDS
    flows, rooms, sups = fl[ok], tr[ok], sup[ok]
    best = None
    for n in np.arange(lo, hi + 1e-9, 0.01):
        logk = float(np.mean(np.log(y) - n * np.log(x)))
        cand = EmitterModel(k=float(np.exp(logk)), n=float(n),
                            flow_lph=float(np.median(flows)), hours=n_ok, mae_k=0.0)
        tm = rooms + (y / cand.k) ** (1.0 / n)
        pred = tm + y / (_CP * flows) / 2.0
        mae = float(np.mean(np.abs(pred - sups)))
        if best is None or mae < best[1]:
            best = (cand, mae)
    model, mae = best
    # Gebruikelijke aanvoer per hele graad buiten (mediaan), voor de COP.
    bins = np.round(to[ok])
    table = [(float(b), float(np.median(sup[ok][bins == b])))
             for b in np.unique(bins) if (bins == b).sum() >= 3]
    return EmitterModel(k=model.k, n=model.n, flow_lph=model.flow_lph, hours=n_ok,
                        mae_k=mae, supply_by_outdoor=table,
                        room_c=float(np.median(rooms))), "geleerd"


def _carnot(t_supply: float, t_outdoor: float) -> float:
    return (t_supply + 273.15) / max(t_supply - t_outdoor, 5.0)


def cop_supply_factor(t_outdoor: float, t_supply: float, t_supply_usual: float) -> float:
    """Hoeveel de COP verandert als de aanvoer afwijkt van wat er normaal bij
    deze buitentemperatuur hoort, volgens Carnot.

    Uit de meting zelf niet te leren: de aanvoer volgt de stooklijn en beweegt
    dus mee met buiten (november 2026: een fit op buiten én aanvoer gaf een
    aanvoercoëfficiënt met het verkeerde teken). De verhouding van twee
    Carnot-waarden laat de onbekende efficiëntie wegvallen.
    """
    return _carnot(t_supply, t_outdoor) / _carnot(t_supply_usual, t_outdoor)
