"""De kamertemperatuur, op één plek: de thermostaat, begrensd op extra sensoren.

Waarom (KAMERSENSOREN_PLAN.md): een thermostaat waar de zon op valt, meet
zichzelf. Extra temperatuursensoren in dezelfde kamer begrenzen hem naar
beneden; de rekenregels staan in ``analysis/room_reference.py``.

Alles in de integratie dat de kamertemperatuur leest, leest hem hier
(``room_value``), niet meer zelf van de entity. Zonder extra sensoren is de
uitkomst precies de thermostaat, bit voor bit: de correctie bestaat alleen
voor wie hem instelt.

Het vaste verschil per referentie en de zonuren per sensor worden elke zes uur
uit de uurstatistiek van de recorder bepaald en bewaard, zodat de correctie na
een herstart meteen werkt. Zonuren vragen een helderheid per uur; die levert
de MPC-sensor (``clearness_provider``): uit de zonnesensor als die er is,
anders uit de Open-Meteo-verwachting van dat uur. Zonder helderheid worden
geen zonuren bepaald en telt elke referentie altijd mee.

Ook zonder extra sensoren kijkt hij of de thermostaat op zonnige middagen
pieken maakt die een kamer niet maakt (``sun_peaks``, op de vijfminutenwaarden
van het meetlogboek, ``rows_provider``). Dat is niet te onderscheiden van een
kamer die echt snel opwarmt, dus die melding is neutraal; met een extra sensor
beslist het teken (KAMERSENSOREN_PLAN.md, stap 2). Drie meldingen onder
Reparaties, zie ``_async_update_issues``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
import logging
import math
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .analysis.room_reference import (
    PEAK_WINDOW_DAYS,
    SUN_DAYS,
    compose,
    day_is_sunny,
    night_offsets,
    peak_notice_step,
    sun_effects,
    sun_peaks,
)
from .const import (
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INDOOR_TEMP_REFERENCES,
    DOMAIN,
    ROOM_REFERENCE_STORAGE_KEY,
)
from .discovery import ROLE_INDOOR_TEMP
from .helpers import get_float_state
from .sources import async_source_entity

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
REFRESH_INTERVAL = timedelta(hours=6)
#: Eerste bepaling na het opstarten: de recorder en de MPC-sensor moeten er zijn.
STARTUP_DELAY_S = 120.0

#: (begin, eind) → ({uurbegin: helderheid}, bron), bron "zonnesensor" of "open-meteo".
ClearnessProvider = Callable[[float, float], Awaitable[tuple[dict[float, float], str | None]]]
#: since → rijen van het meetlogboek in de volgorde van ``highres_log.COLUMNS``.
RowsProvider = Callable[[float], list[list[float | None]]]

#: Meldingen onder Reparaties (vertaalsleutels); de issue-id krijgt de entry erbij.
ISSUE_SUN_PEAKS = "room_sun_peaks"
ISSUE_SENSOR_SUN = "room_sensor_sun"
ISSUE_REFERENCE_SUN = "room_reference_sun"
ISSUES = (ISSUE_SUN_PEAKS, ISSUE_SENSOR_SUN, ISSUE_REFERENCE_SUN)
#: Wat telt als "afgekapt" in de telling over 14 dagen.
CLIPPED_MIN_K = 0.05


def room_key(entry_id: str) -> str:
    return f"{entry_id}_room"


def async_room_temperature(hass: HomeAssistant, entry_id: str) -> RoomTemperature | None:
    """De ``RoomTemperature`` van deze installatie, of None.

    Strikt op type: alles wat geen ``RoomTemperature`` is (nog niet opgezet,
    een nagebootste ``hass`` in tests) valt terug op de thermostaat zelf.
    """
    data = getattr(hass, "data", None)
    domain = data.get(DOMAIN) if isinstance(data, dict) else None
    room = domain.get(room_key(entry_id)) if isinstance(domain, dict) else None
    return room if isinstance(room, RoomTemperature) else None


def room_value(hass: HomeAssistant, entry_id: str, fallback_entity: str | None) -> float | None:
    """De kamertemperatuur voor deze installatie.

    Zonder ``RoomTemperature`` (tijdens het opstarten, of in tests) de entity
    zelf: dan gedraagt alles zich zoals vóór de extra sensoren.
    """
    room = async_room_temperature(hass, entry_id)
    if room is not None:
        return room.value()
    return get_float_state(hass, fallback_entity) if fallback_entity else None


def _clock(hours: float, up: bool) -> str:
    """Uur van de dag (decimaal) als hh:mm, afgerond op een kwartier."""
    q = (math.ceil if up else math.floor)(hours * 4) / 4
    q = min(max(q, 0.0), 23.75)
    return f"{int(q):02d}:{int(round(q % 1 * 60)):02d}"


def _kelvin(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def reference_entities(config: dict, primary: str | None = None) -> list[str]:
    """De ingestelde extra kamersensoren, zonder lege velden en zonder de thermostaat."""
    refs = config.get(CONF_INDOOR_TEMP_REFERENCES) or []
    if isinstance(refs, str):
        refs = [refs]
    out: list[str] = []
    for e in refs:
        if e and e != primary and e not in out:
            out.append(e)
    return out


class RoomTemperature:
    """De kamertemperatuur van één installatie; zie de moduletekst."""

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._config = config
        self._store: Store = Store(hass, STORAGE_VERSION, f"{ROOM_REFERENCE_STORAGE_KEY}_{entry_id}")
        #: Thermostaat waarvoor de verschillen gelden. Wisselt de bron, dan
        #: gelden ze niet meer tot de volgende bepaling.
        self._learned_for: str | None = None
        self.offsets: dict[str, float] = {}
        self.sun: dict[str, set[int]] = {}
        #: Per sensor per uur van de dag hoeveel zon (K), voor de meldingen.
        self.sun_k: dict[str, dict[int, float]] = {}
        self.updated_at: str | None = None
        self.clearness_source: str | None = None
        self.clearness_provider: ClearnessProvider | None = None
        self.rows_provider: RowsProvider | None = None
        #: Melding zonder referenties (``peak_notice_step``) en de laatst verwerkte dag.
        self.peak_state: dict | None = None
        self.peak_day: str | None = None
        #: Hoeveel er de laatste 14 dagen is afgekapt: {"uren", "gem_k"}.
        self.clipped: dict | None = None
        self._refreshing = False

    # -- lezen ---------------------------------------------------------------

    @property
    def primary_entity(self) -> str | None:
        return async_source_entity(
            self._hass, self._entry_id, ROLE_INDOOR_TEMP,
            config=self._config, conf_key=CONF_INDOOR_TEMP_ENTITY,
        )

    @property
    def references(self) -> list[str]:
        return reference_entities(self._config, self.primary_entity)

    def raw(self) -> float | None:
        """De thermostaat zelf."""
        primary = self.primary_entity
        return get_float_state(self._hass, primary) if primary else None

    def value(self) -> float | None:
        return self._compose()[0]

    def _compose(self) -> tuple[float | None, dict]:
        primary = self.primary_entity
        raw = get_float_state(self._hass, primary) if primary else None
        refs = reference_entities(self._config, primary)
        if not refs or primary != self._learned_for:
            return raw, {"gebruikt": [], "referentie": None, "afgekapt_k": 0.0}
        hour = dt_util.now().hour
        excluded = {e for e in refs if hour in self.sun.get(e, set())}
        values = {e: get_float_state(self._hass, e) for e in refs}
        return compose(raw, values, self.offsets, excluded)

    def attributes(self) -> dict:
        """Voor de spiegelsensor kamertemperatuur."""
        primary = self.primary_entity
        out: dict = {}
        st = self.peak_state or {}
        if st.get("aan") and st.get("cluster"):
            c = st["cluster"]
            out["zonpieken"] = {
                "van": _clock(c["van"], False), "tot": _clock(c["tot"], True),
                "kelvin": round(c["kelvin"], 1), "dagen": c["dagen"],
            }
        refs = self.references
        if not refs:
            return out
        value, details = self._compose()
        out.update({
            "thermostaat": self.raw(),
            "begrensd": bool(details["afgekapt_k"] > 0),
            "afgekapt_k": round(details["afgekapt_k"], 2),
            "referentie_k": (
                round(details["referentie"], 2) if details["referentie"] is not None else None
            ),
            "referenties": {
                e: {
                    "naam": self._name(e),
                    "verschil_k": round(self.offsets[e], 2) if e in self.offsets else None,
                    "gebruikt": e in details["gebruikt"],
                    "zon_uren": sorted(self.sun.get(e, set())),
                }
                for e in refs
            },
            "thermostaat_zon_uren": sorted(self.sun.get(primary, set())) if primary else [],
            "afgekapt_14d": self.clipped,
            "zon_bron": self.clearness_source,
            "bepaald_op": self.updated_at,
        })
        return out

    def _name(self, entity_id: str) -> str:
        state = self._hass.states.get(entity_id)
        name = state.attributes.get("friendly_name") if state is not None else None
        return str(name) if isinstance(name, str) and name else entity_id

    # -- bepalen -------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._learned_for = data.get("primary")
        self.offsets = {k: float(v) for k, v in (data.get("offsets") or {}).items()}
        self.sun = {k: set(v) for k, v in (data.get("sun") or {}).items()}
        self.sun_k = {k: {int(h): float(x) for h, x in v.items()}
                      for k, v in (data.get("sun_k") or {}).items()}
        self.updated_at = data.get("updated_at")
        self.clearness_source = data.get("clearness_source")
        self.peak_state = data.get("peak_state")
        self.peak_day = data.get("peak_day")
        self.clipped = data.get("clipped")

    def _snapshot(self) -> dict:
        return {
            "primary": self._learned_for,
            "offsets": self.offsets,
            "sun": {k: sorted(v) for k, v in self.sun.items()},
            "sun_k": {k: {str(h): x for h, x in v.items()} for k, v in self.sun_k.items()},
            "updated_at": self.updated_at,
            "clearness_source": self.clearness_source,
            "peak_state": self.peak_state,
            "peak_day": self.peak_day,
            "clipped": self.clipped,
        }

    @callback
    def async_setup(self) -> Callable[[], None]:
        cancel_interval = async_track_time_interval(
            self._hass, self._async_refresh_cb, REFRESH_INTERVAL
        )
        cancel_first = async_call_later(self._hass, STARTUP_DELAY_S, self._async_refresh_cb)

        @callback
        def _teardown() -> None:
            # Meldingen blijven staan: bij een herlaad komen ze bij de eerste
            # bepaling terug, en weghalen en terugzetten flikkert alleen.
            cancel_interval()
            cancel_first()

        return _teardown

    async def _async_refresh_cb(self, _now=None) -> None:
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001
            # Een mislukte bepaling laat de vorige staan.
            _LOGGER.warning("Extra kamersensoren bepalen mislukt", exc_info=True)

    async def async_refresh(self) -> None:
        """Verschillen, zonuren, pieken en meldingen opnieuw bepalen."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            primary = self.primary_entity
            refs = reference_entities(self._config, primary)
            now = time.time()
            tz = dt_util.get_default_time_zone()
            clear: dict[float, float] = {}
            source = None
            if self.clearness_provider is not None and primary:
                window = max(SUN_DAYS, PEAK_WINDOW_DAYS + 1)
                clear, source = await self.clearness_provider(now - window * 86400, now)
                clear = clear or {}
            self.clearness_source = source if clear else None

            if primary and refs:
                # Laat import: sensor.py importeert deze module.
                from .sensor import async_hourly_means

                start = now - SUN_DAYS * 86400
                means = await async_hourly_means(self._hass, [primary, *refs], start, now)
                prim = means.get(primary, {})
                ref_series = {e: means.get(e, {}) for e in refs}
                offsets = night_offsets(prim, ref_series, now, tz)
                effects: dict[str, dict[int, float]] = {}
                if clear:
                    effects = sun_effects(
                        {primary: prim, **ref_series}, {primary: 0.0, **offsets}, clear, now, tz,
                    )
                self.offsets, self._learned_for = offsets, primary
                self.sun_k = effects
                self.sun = {k: set(v) for k, v in effects.items()}
            else:
                self.offsets, self.sun, self.sun_k, self._learned_for = {}, {}, {}, None

            self._update_peaks(clear, now, tz)
            self._update_clipped(now)
            self.updated_at = dt_util.utcnow().isoformat()
            await self._store.async_save(self._snapshot())
            self._async_update_issues()
        finally:
            self._refreshing = False

    def _thermostat_samples(self, since: float):
        """Vijfminutenmonsters van de thermostaat zelf: ``t_indoor_raw`` waar
        die er is (extra sensoren actief), anders ``t_indoor``."""
        import numpy as np

        from .highres_log import COLUMNS

        if self.rows_provider is None:
            return np.empty(0), np.empty(0), np.empty(0)
        i_in, i_raw = COLUMNS.index("t_indoor"), COLUMNS.index("t_indoor_raw")
        ts, raw, used = [], [], []
        for row in self.rows_provider(since) or []:
            t_in = row[i_in] if len(row) > i_in else None
            t_raw = row[i_raw] if len(row) > i_raw else None
            value = t_raw if isinstance(t_raw, (int, float)) and math.isfinite(t_raw) else t_in
            if isinstance(value, (int, float)) and math.isfinite(value):
                ts.append(float(row[0]))
                raw.append(float(value))
                used.append(float(t_in) if isinstance(t_in, (int, float)) else float("nan"))
        order = np.argsort(ts, kind="stable")
        return np.asarray(ts)[order], np.asarray(raw)[order], np.asarray(used)[order]

    def _update_peaks(self, clear: dict[float, float], now: float, tz) -> None:
        """De melding zonder referenties: alle afgeronde dagen sinds de vorige keer."""
        if not clear or self.rows_provider is None:
            return
        lat, lon = float(self._hass.config.latitude), float(self._hass.config.longitude)
        today = dt_util.now().date()
        first = today - timedelta(days=PEAK_WINDOW_DAYS)
        start_day = first
        if self.peak_day:
            try:
                start_day = max(first, date.fromisoformat(self.peak_day) + timedelta(days=1))
            except ValueError:
                start_day = first
        if start_day >= today:
            return
        ts, raw, _used = self._thermostat_samples(
            dt_util.as_utc(dt_util.start_of_local_day(first - timedelta(days=PEAK_WINDOW_DAYS))).timestamp()
        )
        peaks = sun_peaks(ts, raw, clear, lat, lon, tz)
        state = self.peak_state
        day = start_day
        while day < today:
            state = peak_notice_step(state, peaks, day, day_is_sunny(clear, day, tz))
            day += timedelta(days=1)
        self.peak_state = state
        self.peak_day = (today - timedelta(days=1)).isoformat()

    def _update_clipped(self, now: float) -> None:
        """Hoeveel er de laatste 14 dagen is afgekapt (uren, gemiddeld)."""
        if not self.references or self.rows_provider is None:
            self.clipped = None
            return
        ts, raw, used = self._thermostat_samples(now - 14 * 86400)
        if len(ts) == 0:
            self.clipped = None
            return
        import numpy as np

        diff = raw - used
        hit = diff[np.isfinite(diff) & (diff > CLIPPED_MIN_K)]
        # Vijf minuten per monster.
        self.clipped = {
            "uren": round(len(hit) * 5 / 60, 1),
            "gem_k": round(float(hit.mean()), 2) if len(hit) else 0.0,
        }

    # -- meldingen -----------------------------------------------------------

    def _issue_id(self, key: str) -> str:
        return f"{key}_{self._entry_id}"

    @callback
    def _async_update_issues(self) -> None:
        """Zet of verwijder de drie meldingen; zie KAMERSENSOREN_PLAN.md, ontwerp 6.

        - Met referenties beslist het teken: krijgt de thermostaat zelf zon,
          dan ``room_sensor_sun``; zo niet, dan geen melding over de thermostaat
          (de kamer warmt echt op, of er is niets).
        - Zonder referenties de neutrale ``room_sun_peaks``.
        - Een referentie die zelf zon krijgt: ``room_reference_sun``.
        """
        from homeassistant.helpers import issue_registry as ir

        primary = self.primary_entity
        refs = self.references
        want: dict[str, dict] = {}
        if refs and primary and self.sun_k.get(primary):
            hours = self.sun_k[primary]
            want[ISSUE_SENSOR_SUN] = {
                "sensor": self._name(primary),
                "van": f"{min(hours):02d}:00", "tot": f"{max(hours) + 1:02d}:00",
                "kelvin": _kelvin(max(hours.values())),
                "uren": _kelvin((self.clipped or {}).get("uren", 0.0)),
            }
        elif not refs and primary and (self.peak_state or {}).get("aan"):
            c = self.peak_state.get("cluster") or {}
            if c:
                want[ISSUE_SUN_PEAKS] = {
                    "sensor": self._name(primary),
                    "dagen": str(c["dagen"]),
                    "van": _clock(c["van"], False), "tot": _clock(c["tot"], True),
                    "kelvin": _kelvin(c["kelvin"]),
                }
        sunny_refs = [e for e in refs if self.sun_k.get(e)]
        if sunny_refs:
            used = [self._name(e) for e in refs if e not in sunny_refs]
            want[ISSUE_REFERENCE_SUN] = {
                "sensoren": "; ".join(
                    f"{self._name(e)} ({min(self.sun_k[e]):02d}:00–{max(self.sun_k[e]) + 1:02d}:00, "
                    f"+{_kelvin(max(self.sun_k[e].values()))} °C)"
                    for e in sunny_refs
                ),
                "gebruikt": ", ".join(used) if used else "—",
            }
        for key in ISSUES:
            if key in want:
                ir.async_create_issue(
                    self._hass, DOMAIN, self._issue_id(key),
                    is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key=key, translation_placeholders=want[key],
                )
            else:
                ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(key))
