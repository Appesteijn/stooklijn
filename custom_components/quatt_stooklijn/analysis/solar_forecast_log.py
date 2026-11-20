"""Zonverwachtingen bewaren per vooruitkijktijd, en achteraf scoren.

Waarom (ZONNEWARMTE_PLAN.md): een aansturing die rekening houdt met de zon van
straks heeft een verwachting voor 1–6 uur vooruit nodig. Hoe goed Open-Meteo
dat bij dit huis doet, was niet te meten: er werd alleen de waarde voor het
lopende uur bewaard. Dit logboek legt bij elke ophaling vast wat er voor de
komende twaalf uur verwacht werd, en zet dat later naast wat er kwam.

"Wat er kwam" is bij voorkeur gemeten (zonnesensor, zie ``solar_input``).
Zonder zonnesensor is de enige referentie de verwachting van vlak ervóór
(vooruitkijktijd 0): dan meet je hoeveel de verwachting nog verschuift, niet
hoe goed hij is.

Alle tijden zijn het **begin** van het uur in UTC-seconden; waarden in W/m²
horizontaal. Vooruitkijktijd L: het uur begint L hele uren na het uur van
ophalen (0 = het lopende uur).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

MAX_LEAD_HOURS = 12
KEEP_HOURS = 30 * 24
SCORE_LEADS = (1, 2, 3, 6, 12)
#: Uren waarin verwachting en meting allebei onder dit blijven tellen niet mee:
#: een nacht die als nacht verwacht werd zegt niets over de zonverwachting.
MIN_DAYLIGHT_WM2 = 10.0
#: Venster voor de correctiefactor (gemeten / verwacht).
BIAS_WINDOW_HOURS = 14 * 24
#: Vooruitkijktijden waarover de correctiefactor gaat: de horizon van een
#: aansturing, niet het lopende uur.
BIAS_LEADS = (1, 2, 3, 4, 5, 6)


class SolarForecastLog:
    """Per uur de verwachte straling, per vooruitkijktijd."""

    def __init__(self) -> None:
        self._f: dict[int, dict[int, float]] = {}

    def __len__(self) -> int:
        return sum(len(v) for v in self._f.values())

    def record(self, fetched_ts: float, hours: list[tuple[float, float | None]]) -> int:
        """Leg een ophaling vast. ``hours``: (begin van het uur, W/m²).

        Per uur en vooruitkijktijd telt de eerste ophaling. Een herstart binnen
        het uur haalt opnieuw op; die tweede waarde is geen andere verwachting.
        """
        base = int(fetched_ts // 3600) * 3600
        added = 0
        for start, value in hours:
            if value is None or not np.isfinite(value):
                continue
            start = int(start)
            lead = (start - base) // 3600
            if 0 <= lead <= MAX_LEAD_HOURS:
                slot = self._f.setdefault(start, {})
                if lead not in slot:
                    slot[lead] = float(value)
                    added += 1
        return added

    def prune(self, now_ts: float) -> None:
        cutoff = now_ts - KEEP_HOURS * 3600
        for start in [s for s in self._f if s < cutoff]:
            del self._f[start]

    def hours(self) -> list[int]:
        """Alle uren (begin, UTC-seconden) met minstens één verwachting."""
        return sorted(self._f)

    def at(self, start: int, lead: int) -> float | None:
        """De verwachting voor dit uur op deze vooruitkijktijd, of None."""
        return self._f.get(int(start), {}).get(lead)

    def latest(self, start: int) -> float | None:
        """De verwachting met de kortste vooruitkijktijd voor dit uur."""
        slot = self._f.get(int(start))
        if not slot:
            return None
        return slot[min(slot)]

    def to_dict(self) -> dict:
        return {
            "forecasts": {
                str(s): {str(lead): round(v, 1) for lead, v in leads.items()}
                for s, leads in self._f.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> SolarForecastLog:
        log = cls()
        for s, leads in ((data or {}).get("forecasts") or {}).items():
            try:
                log._f[int(s)] = {int(lead): float(v) for lead, v in leads.items()}
            except (TypeError, ValueError, AttributeError):
                continue
        return log

    def score(
        self,
        now_ts: float,
        measured: Mapping[int, tuple[float, float | None]] | None = None,
        clear_sky: Mapping[int, float] | None = None,
        leads: tuple[int, ...] = SCORE_LEADS,
    ) -> dict:
        """Fout per vooruitkijktijd, over afgeronde uren.

        ``measured``: per uur (gemeten W/m², helderheid). Zonder meting wordt
        de kortste verwachting de referentie. ``clear_sky``: straling bij heldere
        hemel per uur, nodig voor de persistentie ("helderheid van L uur geleden
        aanhouden"); alleen met meting.
        """
        reference_kind = "zonnesensor" if measured else "latere_verwachting"
        done = [s for s in self._f if s + 3600 <= now_ts]
        per_lead: dict[str, dict] = {}
        for lead in leads:
            f, r, p_f, p_r, p_p = [], [], [], [], []
            for s in done:
                fc = self._f[s].get(lead)
                if fc is None:
                    continue
                if measured is not None:
                    m = measured.get(s)
                    if m is None:
                        continue
                    ref = m[0]
                else:
                    if lead == min(self._f[s]):
                        continue      # zichzelf als referentie
                    ref = self.latest(s)
                if max(fc, ref) < MIN_DAYLIGHT_WM2:
                    continue
                f.append(fc)
                r.append(ref)
                if measured is not None and clear_sky is not None:
                    past = measured.get(s - lead * 3600)
                    cs = clear_sky.get(s)
                    if past is not None and past[1] is not None and cs is not None:
                        p_f.append(fc)
                        p_r.append(ref)
                        p_p.append(min(past[1], 1.15) * cs)
            entry: dict = {"n": len(f)}
            if f:
                e = np.array(f) - np.array(r)
                entry["bias"] = round(float(e.mean()), 1)
                entry["mae"] = round(float(np.abs(e).mean()), 1)
            if p_p:
                entry["n_persistentie"] = len(p_p)
                entry["persistentie_mae"] = round(
                    float(np.abs(np.array(p_p) - np.array(p_r)).mean()), 1)
                entry["verwachting_mae_zelfde_uren"] = round(
                    float(np.abs(np.array(p_f) - np.array(p_r)).mean()), 1)
            per_lead[str(lead)] = entry

        factor = None
        if measured is not None:
            recent = [s for s in done if s >= now_ts - BIAS_WINDOW_HOURS * 3600]
            num = den = 0.0
            for s in recent:
                m = measured.get(s)
                if m is None:
                    continue
                for lead in BIAS_LEADS:
                    fc = self._f[s].get(lead)
                    if fc is not None and max(fc, m[0]) >= MIN_DAYLIGHT_WM2:
                        num += m[0]
                        den += fc
            if den > 0:
                factor = round(num / den, 3)
        return {
            "referentie": reference_kind,
            "per_vooruitkijktijd": per_lead,
            "correctiefactor": factor,
            "uren_in_logboek": len(self._f),
        }
