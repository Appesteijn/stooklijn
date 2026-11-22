"""Sound level compensation switch for Quatt Stooklijn integration.

Beïnvloedt het maximale geluidsniveau (en daarmee het vermogen) van de
warmtepomp op basis van een aanvoeradvies en gasketelactiviteit. Welk advies
(stooklijn of MPC) kiest de gebruiker; zie advice_sources.

Logica (elke 5 min):
1. HP inactief (debiet < MIN_FLOW_LPH)
   → reset naar geconfigureerd max, stop
2. Gas actief (boiler-warmtevermogen > drempel)
   → één stap omhoog (HP mag harder draaien zodat ketel minder nodig is)
3. Aanvoertemp > advies + dead band
   → één stap omlaag (te veel warmte)
4. Aanvoertemp < advies − dead band
   → één stap omhoog (te weinig warmte)
5. Binnen dead band → geen actie

Niveauvolgorde (zwak → sterk): building87 → silent → library → normal
Reset naar geconfigureerd dag/nacht-max bij: HP inactief, advies te lang weg,
een fout in de cyclus.

Terug naar de eigen stand van de gebruiker (sinds v0.10.7-beta.8) bij
uitschakelen, herladen en het stoppen van Home Assistant. Die stand wordt per
select bewaard op een schoon moment: bij inschakelen, of bij de eerste start na
een nette stop. Het maximum per periode komt nooit boven die eigen stand: een
nachtlimiet uit de Quatt-app blijft zo gelden. Ontbreekt een bewaarde stand
(bijvoorbeeld bij een upgrade terwijl de compensatie aan stond), dan gaat de
select zoals voorheen naar het ingestelde maximum.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.switch import ENTITY_ID_FORMAT, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOILER_HEAT_ENTITY,
    CONF_FLOW_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_SOUND_LEVEL_MAX_DAY,
    CONF_SOUND_LEVEL_MAX_NIGHT,
    CONF_SOUND_NIGHT_START_HOUR,
    CONF_SOUND_NIGHT_END_HOUR,
    CONF_SOUND_LEVEL_SOURCE,
    DEFAULT_SOUND_LEVEL_MAX,
    DEFAULT_SOUND_LEVEL_SOURCE,
    DEFAULT_SOUND_NIGHT_START_HOUR,
    DEFAULT_SOUND_NIGHT_END_HOUR,
    DOMAIN,
    MIN_FLOW_LPH,
    MPC_ADVICE_NO_DEMAND,
    SOUND_CYCLE_SECONDS,
    SOUND_ADVICE_TIMEOUT,
    SOUND_INPUT_MAX_AGE,
    SIGNAL_SOUND_LEVEL,
)
from .coordinator import QuattStooklijnCoordinator
from .discovery import (
    ROLE_BOILER_HEAT,
    ROLE_FLOW_RATE,
    ROLE_SOUND_DAY,
    ROLE_SOUND_NIGHT,
    ROLE_SUPPLY_TEMP,
)
from .advice_sources import advice_entity_id, read_advice, valid_source
from .helpers import get_device_info, get_float_state
from .sources import async_source_entity

_LOGGER = logging.getLogger(__name__)

# Niveauvolgorde: index 0 = zwakst, index 3 = sterkst
_SOUND_LEVELS = ["building87", "silent", "library", "normal"]
_NORMAL_IDX = len(_SOUND_LEVELS) - 1


# Welk advies de compensatie volgt, kiest de gebruiker (CONF_SOUND_LEVEL_SOURCE,
# zie advice_sources). Gelezen via de unique_id van de adviessensor, zodat
# hernoemen of een tweede config-entry de compensatie niet stilzwijgend blind
# maakt.
# De twee geluidsniveau-selects worden per aanroep uit de bronresolutie gehaald
# (zie ``_day_sound_entity`` / ``_night_sound_entity``). Ze stonden hier als
# vaste entity-ID, en dan alleen in de ná-migratie vorm — waardoor deze functie
# op elke installatie van vóór die migratie stilzwijgend niets deed.

_GAS_THRESHOLD_W = 200.0  # W: boven deze waarde is de gasketel actief
_DEAD_BAND = 2.0           # °C: geen actie binnen deze marge rond het advies
# Aantal opeenvolgende cycli dat een CiC-select moet afwijken voordat de
# compensatie hem terugzet. Eén cyclus marge, zodat een waarde die net is
# geschreven en via de Quatt-cloud nog onderweg is niet dubbel wordt gestuurd.
_MISMATCH_CYCLES = 2

_STORAGE_VERSION = 1
#: Terugzetten bij het stoppen van HA mag het afsluiten niet ophouden.
_RESTORE_TIMEOUT_S = 10.0
_PERIODS = ("day", "night")


# Het type vastgelegd bij het importeren: tests vervangen ``switch.datetime``
# door een mock om de klok te sturen, en dan breekt ``isinstance``.
_DATETIME_TYPE = datetime


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.sound_level_{entry_id}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sound level switch from config entry."""
    if not {**entry.data, **entry.options}.get(CONF_SOUND_LEVEL_ENABLED, False):
        return

    coordinator: QuattStooklijnCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([QuattSoundLevelSwitch(hass, coordinator, entry)])


class QuattSoundLevelSwitch(SwitchEntity, RestoreEntity):
    """Switch om geluidsniveau-compensatie aan/uit te zetten."""

    _attr_has_entity_name = True
    _attr_translation_key = "sound_level_compensation"
    _attr_icon = "mdi:volume-high"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sound_level_compensation"
        self._attr_device_info = get_device_info(entry.entry_id)
        # Vaste entity-id: de naam komt uit de vertaling en zou de id anders
        # taalafhankelijk maken, terwijl het dashboard hem hard noemt.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT,
            "quatt_warmteanalyse_geluidsniveau_compensatie",
            hass=hass,
        )

        self._is_on = True  # standaard aan: config-optie = feature actief
        self._current_level_idx: int = _NORMAL_IDX
        # Vanaf de start, niet pas na het eerste advies: begon dit op None,
        # dan liep de watchdog na een herstart zonder advies nooit, en bleef
        # een hersteld laag niveau tot een etmaal staan.
        self._last_mpc_available: datetime | None = datetime.now(timezone.utc)
        self._hp_inactive_since: datetime | None = None
        self._last_is_night: bool | None = None
        # Terugkoppeling: per select hoeveel cycli hij al afwijkt van wat de
        # compensatie wil. Na een herstart is het interne niveau hersteld uit
        # de vorige state, maar is niet bekend wat de CiC nu heeft; daarom
        # direct gelijktrekken bij de eerste cyclus.
        self._mismatch_cycles: dict[str, int] = {}
        self._synced_since_start = False
        self._corrections = 0
        self._last_correction: str | None = None

        merged = {**entry.data, **entry.options}

        # Maximaal geluidsniveau (= max vermogen) dat compensatie mag instellen
        max_day = merged.get(CONF_SOUND_LEVEL_MAX_DAY, DEFAULT_SOUND_LEVEL_MAX)
        max_night = merged.get(CONF_SOUND_LEVEL_MAX_NIGHT, DEFAULT_SOUND_LEVEL_MAX)
        self._cfg_max_day_idx = (
            _SOUND_LEVELS.index(max_day) if max_day in _SOUND_LEVELS else _NORMAL_IDX
        )
        self._cfg_max_night_idx = (
            _SOUND_LEVELS.index(max_night) if max_night in _SOUND_LEVELS else _NORMAL_IDX
        )

        # De eigen stand van de gebruiker per periode, en of de selects daar nu
        # (voor zover bekend) op staan. Zie de moduledocstring.
        self._store: Store | None = None
        self._saved: dict | None = None
        self._may_capture = False
        self._stopped = False

        # Nachtvenster in HA-lokale tijd — geconfigureerd in HA, onafhankelijk van
        # Quatt-sensoren (die na zomertijd een verkeerde UTC-offset kunnen meegeven).
        self._night_start_hour: int = merged.get(CONF_SOUND_NIGHT_START_HOUR, DEFAULT_SOUND_NIGHT_START_HOUR)
        self._night_end_hour: int = merged.get(CONF_SOUND_NIGHT_END_HOUR, DEFAULT_SOUND_NIGHT_END_HOUR)

        # Welk model de compensatie aanstuurt (advice_sources).
        self._advice_source: str = valid_source(
            merged.get(CONF_SOUND_LEVEL_SOURCE), DEFAULT_SOUND_LEVEL_SOURCE
        )

    def _is_night(self) -> bool:
        """Is het nu nacht?

        Vergelijkt de HA-lokale tijd met het geconfigureerde nachtvenster.
        Gebruikt dt_util.now() (Europe/Amsterdam, DST-correct) en de uren
        die de gebruiker zelf in HA heeft ingesteld — onafhankelijk van
        Quatt-sensoren die na een zomertijdwissel een verkeerde UTC-offset
        kunnen rapporteren.
        """
        now = dt_util.now()
        current_minutes = now.hour * 60 + now.minute
        night_start = self._night_start_hour * 60
        night_end = self._night_end_hour * 60

        # Nachtvenster overspant middernacht (bijv. 23:00–07:00)
        if night_start > night_end:
            return current_minutes >= night_start or current_minutes < night_end
        else:
            return night_start <= current_minutes < night_end

    @property
    def _max_day_idx(self) -> int:
        return self._capped(self._cfg_max_day_idx, "day")

    @property
    def _max_night_idx(self) -> int:
        return self._capped(self._cfg_max_night_idx, "night")

    def _capped(self, cfg_idx: int, period: str) -> int:
        """Het ingestelde maximum, maar nooit boven de eigen stand van de gebruiker.

        Een nachtlimiet uit de Quatt-app verdween anders bij het inschakelen:
        de inactieve select ging naar het ingestelde maximum (standaard
        ``normal``).
        """
        record = (self._saved or {}).get(period)
        original = record.get("original") if isinstance(record, dict) else None
        if original in _SOUND_LEVELS:
            return min(cfg_idx, _SOUND_LEVELS.index(original))
        return cfg_idx

    def _effective_max_idx(self) -> int:
        """Max niveau-index voor de huidige dagperiode."""
        return self._max_night_idx if self._is_night() else self._max_day_idx

    # Entity-ID's worden per aanroep opgezocht in plaats van bij het opstarten
    # vastgelegd: welke integratie een meting levert kan tijdens bedrijf wisselen
    # (zie sources.py).
    @property
    def _supply_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_SUPPLY_TEMP,
            config=cfg, conf_key=CONF_SUPPLY_TEMP_ENTITY,
        )

    @property
    def _flow_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_FLOW_RATE,
            config=cfg, conf_key=CONF_FLOW_ENTITY,
        )

    @property
    def _boiler_heat_entity(self) -> str:
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_BOILER_HEAT,
            config=cfg, conf_key=CONF_BOILER_HEAT_ENTITY,
        )

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        if not self._is_on:
            # Uit → aan: de selects staan op wat de gebruiker had.
            self._may_capture = True
        self._is_on = True
        self._stopped = False
        self._last_mpc_available = datetime.now(timezone.utc)
        self.async_write_ha_state()
        _LOGGER.info("Geluidsniveau compensatie ingeschakeld")

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        await self._async_restore_originals("uitgeschakeld")
        self.async_write_ha_state()
        _LOGGER.info("Geluidsniveau compensatie uitgeschakeld")

    async def async_added_to_hass(self) -> None:
        """Herstel vorige state en registreer periodieke cyclus."""
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == "on"
            # Herstel geluidsniveau-index uit opgeslagen attributen
            attrs = last_state.attributes or {}
            saved_level = attrs.get("current_level")
            if saved_level in _SOUND_LEVELS:
                self._current_level_idx = _SOUND_LEVELS.index(saved_level)

        saved = await self._async_saved()
        # Alleen een schoon moment levert de eigen stand op: de optie net
        # aangezet (geen vorige state), de switch stond uit, of de vorige
        # stop heeft alles netjes teruggezet. Stond de compensatie aan zonder
        # nette stop, dan staan de selects op wat wíj schreven.
        self._may_capture = (
            last_state is None
            or last_state.state != "on"
            or bool(saved.get("at_original"))
        )

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_compensation_cycle,
                timedelta(seconds=SOUND_CYCLE_SECONDS),
            )
        )

        fired = False

        async def _on_stop(_event) -> None:
            nonlocal fired
            fired = True
            if self._is_on:
                await self._async_restore_originals("Home Assistant stopt")
            self._stopped = True

        remove_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

        def _remove_stop() -> None:
            if not fired:
                remove_stop()

        self.async_on_remove(_remove_stop)

    async def async_will_remove_from_hass(self) -> None:
        # Alleen als de compensatie aan stond: tot v0.10.7-beta.8 zette elke
        # herlaad (elke opgeslagen optie) de niveaus terug, ook met de switch
        # uit, en overschreef zo wat de gebruiker zelf had ingesteld.
        if self._is_on and not self._stopped:
            await self._async_restore_originals("herladen")
        self._stopped = True

    async def _async_compensation_cycle(self, _now=None) -> None:
        """Voer één compensatiecyclus uit."""
        if not self._is_on or self._stopped:
            return
        try:
            await self._async_capture_originals()
            await self._async_do_compensation()
        except Exception:
            _LOGGER.exception("Geluidsniveau compensatie cyclus gefaald, reset")
            await self._async_reset_level()
        self.async_write_ha_state()

    async def _async_do_compensation(self) -> None:
        """Kernlogica: bepaal of het geluidsniveau omhoog of omlaag moet."""

        effective_max = self._effective_max_idx()
        is_night = self._is_night()
        period = "nacht" if is_night else "dag"

        # Dag/nacht-overgang: clamp indien nodig én reset de inactieve slider altijd
        period_changed = self._last_is_night is not None and is_night != self._last_is_night
        self._last_is_night = is_night
        if self._current_level_idx > effective_max:
            _LOGGER.info(
                "Dag/nacht-overgang (%s): geluidsniveau %s → %s",
                period,
                _SOUND_LEVELS[self._current_level_idx],
                _SOUND_LEVELS[effective_max],
            )
            self._current_level_idx = effective_max
            await self._async_apply_level()
        elif period_changed:
            # Level hoeft niet geclampt, maar inactieve slider moet alsnog op max
            _LOGGER.info(
                "Dag/nacht-overgang (%s): inactieve slider gereset naar max", period
            )
            await self._async_apply_level()

        # 0. Klopt wat de CiC heeft nog met wat wij denken?
        await self._async_verify_selects()

        # 1. HP actief?
        flow = self._fresh_float(self._flow_entity)
        if flow is None or flow < MIN_FLOW_LPH:
            now = datetime.now(timezone.utc)
            if self._hp_inactive_since is None:
                self._hp_inactive_since = now
            inactive_minutes = (now - self._hp_inactive_since).total_seconds() / 60
            if inactive_minutes >= 10 and self._current_level_idx != effective_max:
                _LOGGER.debug(
                    "HP >10 min inactief, reset geluidsniveau naar %s-max (%s)",
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_reset_level()
            return

        self._hp_inactive_since = None

        # 2. Gas actief? → stap omhoog zodat HP meer kan leveren
        boiler_heat = self._fresh_float(self._boiler_heat_entity)
        if boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W:
            if self._current_level_idx < effective_max:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Gas actief (%.0f W) → geluidsniveau omhoog naar '%s' (%s-max: %s)",
                    boiler_heat,
                    _SOUND_LEVELS[self._current_level_idx],
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_apply_level()
            else:
                _LOGGER.debug(
                    "Gas actief maar geluidsniveau al op %s-max (%s)",
                    period,
                    _SOUND_LEVELS[effective_max],
                )
            return

        # 3. Terugkoppeling op het gekozen advies
        advised, status = self._advice_reading()
        actual_supply = self._fresh_float(self._supply_entity)
        # Geen advies omdat het huis warm genoeg is, is geen storing maar een
        # antwoord: de aanvoer is dan hoger dan nodig. Dat telde t/m v0.10.5 als
        # uitval, en na tien minuten zette de watchdog het niveau dan op het
        # maximum — vrij spel precies wanneer er de minste warmte nodig is.
        no_demand = advised is None and status == MPC_ADVICE_NO_DEMAND

        # Watchdog: reset als het advies te lang ontbreekt
        if advised is not None or no_demand:
            self._last_mpc_available = datetime.now(timezone.utc)
        elif self._last_mpc_available is not None:
            elapsed = (
                datetime.now(timezone.utc) - self._last_mpc_available
            ).total_seconds()
            if elapsed > SOUND_ADVICE_TIMEOUT:
                _LOGGER.warning(
                    "Advies (%s) >%d sec niet beschikbaar, reset geluidsniveau",
                    self._advice_source,
                    SOUND_ADVICE_TIMEOUT,
                )
                await self._async_reset_level()
                return

        if no_demand:
            # Zelfde richting als "aanvoer te hoog". Bij een kleine positieve
            # vraag geeft het advies de ondergrens van 20 °C en stapt de compensatie
            # al omlaag; nul vraag hoort daar niet het omgekeerde van te doen.
            if self._current_level_idx > 0:
                self._current_level_idx -= 1
                _LOGGER.info(
                    "Geen warmtevraag (%s) → geluidsniveau omlaag naar '%s'",
                    self._advice_source,
                    _SOUND_LEVELS[self._current_level_idx],
                )
                await self._async_apply_level()
            return

        if advised is None or actual_supply is None:
            return

        error = advised - actual_supply  # negatief = te heet

        if error < -_DEAD_BAND:
            # Aanvoer te hoog → stap omlaag
            if self._current_level_idx > 0:
                self._current_level_idx -= 1
                _LOGGER.info(
                    "Aanvoer te hoog (fout=%.1f°C) → geluidsniveau omlaag naar '%s'",
                    error,
                    _SOUND_LEVELS[self._current_level_idx],
                )
                await self._async_apply_level()

        elif error > _DEAD_BAND:
            # Aanvoer te laag → stap omhoog
            if self._current_level_idx < effective_max:
                self._current_level_idx += 1
                _LOGGER.info(
                    "Aanvoer te laag (fout=%.1f°C) → geluidsniveau omhoog naar '%s' (%s-max: %s)",
                    error,
                    _SOUND_LEVELS[self._current_level_idx],
                    period,
                    _SOUND_LEVELS[effective_max],
                )
                await self._async_apply_level()

    @property
    def _day_sound_entity(self) -> str | None:
        """De dag-select, elke keer opnieuw opgezocht.

        Niet één keer bij het opbouwen vastleggen: de Quatt-integratie kan er bij
        een herstart later zijn dan wij, en een naam die dan leeg was zou blijven
        hangen tot de volgende herstart.
        """
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_SOUND_DAY, config=self._config
        )

    @property
    def _night_sound_entity(self) -> str | None:
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_SOUND_NIGHT, config=self._config
        )

    @property
    def _config(self) -> dict:
        """De entry-config: de metingen erin verankeren de selects aan de eigen
        CiC. Zonder config won bij twee CiC's in één HA de eerste uit het
        register (``async_config_quatt_hubs``)."""
        return {**self._entry.data, **self._entry.options}

    def _desired_selects(self) -> list[tuple[str | None, str]]:
        """Wat elke CiC-select volgens de compensatie nu hoort te zijn.

        De actieve periode-select krijgt het huidige niveau (begrensd op het
        maximum van die periode), de inactieve zijn geconfigureerde maximum,
        zodat bij een periode-overgang de CiC een schone waarde aantreft.
        """
        day_entity = self._day_sound_entity
        night_entity = self._night_sound_entity
        if self._is_night():
            active_entity, active_idx = night_entity, min(self._current_level_idx, self._max_night_idx)
            idle_entity, idle_idx = day_entity, self._max_day_idx
        else:
            active_entity, active_idx = day_entity, min(self._current_level_idx, self._max_day_idx)
            idle_entity, idle_idx = night_entity, self._max_night_idx
        return [
            (active_entity, _SOUND_LEVELS[active_idx]),
            (idle_entity, _SOUND_LEVELS[idle_idx]),
        ]

    def _select_state(self, entity_id: str | None) -> str | None:
        """Huidige stand van een CiC-select, of None als die niet te lezen is."""
        if not entity_id or self.hass is None:
            return None
        state = self.hass.states.get(entity_id)
        value = getattr(state, "state", None)
        return value if isinstance(value, str) and value in _SOUND_LEVELS else None

    async def _async_verify_selects(self) -> None:
        """Zet een CiC-select terug als hij afwijkt van wat de compensatie wil.

        De compensatie schrijft alleen bij een stap. Binnen de dode band kan dat
        uren niet gebeuren, en zonder terugkoppeling bleef een afwijking zo
        lang staan: een schrijfactie die niet aankwam, een herstart (het niveau
        komt uit de vorige state, de CiC kan intussen iets anders hebben), of
        een wijziging in de Quatt-app. Gemeld op Tweakers (21-11-2026): de
        integratie stond op ``library``, de CiC een hele nacht op
        ``building87``, en de kamer zakte van 20,5 naar 20,0 °C.

        Een onleesbare select (unavailable, onbekende optie) telt niet als
        afwijking: dan is er niets om op te vertrouwen.
        """
        first = not self._synced_since_start
        self._synced_since_start = True
        for entity_id, wanted in self._desired_selects():
            actual = self._select_state(entity_id)
            if actual is None or actual == wanted:
                self._mismatch_cycles.pop(entity_id, None)
                continue
            count = self._mismatch_cycles.get(entity_id, 0) + 1
            self._mismatch_cycles[entity_id] = count
            if not first and count < _MISMATCH_CYCLES:
                continue
            _LOGGER.warning(
                "Geluidsniveau: %s staat op '%s', compensatie wil '%s'%s; teruggezet",
                entity_id,
                actual,
                wanted,
                " (eerste cyclus na start)" if first else f" ({count} cycli)",
            )
            await self._async_set_select(entity_id, wanted)
            self._corrections += 1
            self._last_correction = (
                f"{dt_util.now().strftime('%Y-%m-%d %H:%M')} {entity_id}: "
                f"{actual} → {wanted}"
            )

    async def _async_apply_level(self) -> None:
        """Schrijf het huidige geluidsniveau naar beide selects (zie _desired_selects)."""
        for entity_id, option in self._desired_selects():
            if not entity_id:
                # Geen select gevonden: overslaan in plaats van de service met
                # een lege entity-ID aanroepen.
                _LOGGER.debug("Geluidsniveau: select niet gevonden, overgeslagen")
                continue
            await self._async_set_select(entity_id, option)
        _LOGGER.debug(
            "Geluidsniveau ingesteld: dag=%s, nacht=%s",
            _SOUND_LEVELS[min(self._current_level_idx, self._max_day_idx)],
            _SOUND_LEVELS[min(self._current_level_idx, self._max_night_idx)],
        )

    async def _async_reset_level(self) -> None:
        """Reset dag- en nacht-geluidsniveau naar het geconfigureerde maximum."""
        self._current_level_idx = self._effective_max_idx()
        try:
            for entity_id, max_idx in (
                (self._day_sound_entity, self._max_day_idx),
                (self._night_sound_entity, self._max_night_idx),
            ):
                if not entity_id:
                    continue
                await self._async_set_select(entity_id, _SOUND_LEVELS[max_idx])
            _LOGGER.debug(
                "Geluidsniveau gereset: dag=%s, nacht=%s",
                _SOUND_LEVELS[self._max_day_idx],
                _SOUND_LEVELS[self._max_night_idx],
            )
        except Exception:
            _LOGGER.warning("Kon geluidsniveau niet resetten")

    def _advice_reading(self) -> tuple[float | None, str | None]:
        """Het gekozen advies plus de reden als het er niet is (``advies_status``).

        Zie advice_sources.read_advice: per aanroep opgezocht op unique_id,
        zodat een hernoemde sensor of een tweede config-entry blijft werken.
        Een advies dat te lang niets gerapporteerd heeft, telt als uitval.
        """
        entity_id = advice_entity_id(self.hass, self._entry.entry_id, self._advice_source)
        if self._stale(entity_id):
            return None, None
        return read_advice(self.hass, self._entry.entry_id, self._advice_source)

    # ------------------------------------------------------------------
    # Invoer, schrijven en de eigen stand van de gebruiker

    def _stale(self, entity_id: str | None) -> bool:
        """Heeft deze entity langer dan ``SOUND_INPUT_MAX_AGE`` niets gemeld?

        ``last_reported`` loopt ook door als de waarde gelijk blijft (een
        integratie die blijft melden), ``last_updated`` alleen bij een
        wijziging; de eerste die er is telt. Zonder tijdstempel: niet oud.
        """
        if not entity_id or self.hass is None:
            return False
        state = self.hass.states.get(entity_id)
        stamp = getattr(state, "last_reported", None)
        if not isinstance(stamp, _DATETIME_TYPE):
            stamp = getattr(state, "last_updated", None)
        if not isinstance(stamp, _DATETIME_TYPE):
            return False
        age = (dt_util.utcnow() - stamp).total_seconds()
        return age > SOUND_INPUT_MAX_AGE

    def _fresh_float(self, entity_id: str | None) -> float | None:
        """``get_float_state``, maar None voor een bevroren sensor."""
        if not entity_id or self._stale(entity_id):
            return None
        return get_float_state(self.hass, entity_id)

    async def _async_set_select(self, entity_id: str, option: str) -> None:
        """Zet een select, maar alleen als hij er nog niet op staat.

        Na een time-out of een terugkerende fout zette ``_async_reset_level``
        elke cyclus beide selects opnieuw, ook als ze al op het maximum
        stonden: 572 aanroepen per etmaal.
        """
        self._mismatch_cycles.pop(entity_id, None)
        if self._select_state(entity_id) == option:
            return
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
        )
        saved = await self._async_saved()
        if saved.get("at_original"):
            saved["at_original"] = False
            await self._async_save()

    def _get_store(self) -> Store:
        if self._store is None:
            self._store = Store(self.hass, _STORAGE_VERSION, storage_key(self._entry.entry_id))
        return self._store

    async def _async_saved(self) -> dict:
        if self._saved is None:
            data = None
            try:
                data = await self._get_store().async_load()
            except Exception:
                _LOGGER.warning("Geluidsniveau: opgeslagen eigen stand onleesbaar", exc_info=True)
            self._saved = {}
            if isinstance(data, dict):
                for period in _PERIODS:
                    rec = data.get(period)
                    if (isinstance(rec, dict) and rec.get("original") in _SOUND_LEVELS
                            and isinstance(rec.get("entity"), str)):
                        self._saved[period] = {"entity": rec["entity"], "original": rec["original"]}
                self._saved["at_original"] = bool(data.get("at_original"))
        return self._saved

    async def _async_save(self) -> None:
        try:
            await self._get_store().async_save(dict(self._saved or {}))
        except Exception:
            _LOGGER.warning("Geluidsniveau: eigen stand opslaan mislukt", exc_info=True)

    async def _async_capture_originals(self) -> None:
        """Bewaar de eigen stand van beide selects, alleen op een schoon moment."""
        if not self._may_capture:
            return
        saved = await self._async_saved()
        changed = False
        for period, entity_id in (("day", self._day_sound_entity),
                                  ("night", self._night_sound_entity)):
            current = self._select_state(entity_id)
            if current is None:
                continue
            saved[period] = {"entity": entity_id, "original": current}
            changed = True
        self._may_capture = False
        if changed:
            _LOGGER.info(
                "Geluidsniveau: eigen stand bewaard (dag=%s, nacht=%s)",
                (saved.get("day") or {}).get("original"),
                (saved.get("night") or {}).get("original"),
            )
            await self._async_save()

    async def _async_restore_originals(self, reason: str) -> None:
        """Zet beide selects terug op de eigen stand van de gebruiker.

        Zonder bewaarde stand: naar het ingestelde maximum, zoals voorheen.
        Een select die niet te lezen is, blijft openstaan voor een volgende keer.
        """
        saved = await self._async_saved()
        complete = True
        for period, entity_id, cfg_idx in (
            ("day", self._day_sound_entity, self._cfg_max_day_idx),
            ("night", self._night_sound_entity, self._cfg_max_night_idx),
        ):
            record = saved.get(period)
            target_entity = record["entity"] if record else entity_id
            if not target_entity:
                continue
            option = record["original"] if record else _SOUND_LEVELS[cfg_idx]
            if self._select_state(target_entity) is None:
                complete = False
                continue
            try:
                async with asyncio.timeout(_RESTORE_TIMEOUT_S):
                    await self._async_set_select(target_entity, option)
            except Exception as exc:  # incl. TimeoutError
                _LOGGER.warning(
                    "Geluidsniveau: terugzetten van %s mislukt (%s)",
                    target_entity, exc or type(exc).__name__,
                )
                complete = False
                continue
            saved.pop(period, None)
        self._current_level_idx = self._effective_max_idx()
        saved["at_original"] = complete
        await self._async_save()
        _LOGGER.info("Geluidsniveau: eigen stand teruggezet (%s)", reason)

    @callback
    def async_write_ha_state(self) -> None:
        """Schrijf de state en publiceer het actieve niveau.

        De spiegelsensor leest niet mee op een hardcoded entity-ID maar krijgt
        het niveau hier aangereikt; hass.data houdt de laatste waarde vast voor
        een sensor die later wordt toegevoegd dan deze switch.
        """
        super().async_write_ha_state()
        level = _SOUND_LEVELS[self._current_level_idx]
        self.hass.data.setdefault(DOMAIN, {})[
            f"{self._entry.entry_id}_sound_level"
        ] = level
        async_dispatcher_send(
            self.hass, SIGNAL_SOUND_LEVEL.format(self._entry.entry_id), level
        )

    @property
    def extra_state_attributes(self) -> dict:
        advised, status = self._advice_reading() if self.hass else (None, None)
        actual_supply = get_float_state(self.hass, self._supply_entity) if self.hass else None
        flow = get_float_state(self.hass, self._flow_entity) if self.hass else None
        boiler_heat = get_float_state(self.hass, self._boiler_heat_entity) if self.hass else None
        is_night = self._is_night()
        effective_max = self._effective_max_idx()

        night_window = f"{self._night_start_hour:02d}:00–{self._night_end_hour:02d}:00"

        return {
            "current_level": _SOUND_LEVELS[self._current_level_idx],
            "period": "nacht" if is_night else "dag",
            "night_window": night_window,
            "effective_max": _SOUND_LEVELS[effective_max],
            "max_day": _SOUND_LEVELS[self._max_day_idx],
            "max_night": _SOUND_LEVELS[self._max_night_idx],
            "effective_day": _SOUND_LEVELS[min(self._current_level_idx, self._max_day_idx)],
            "effective_night": _SOUND_LEVELS[min(self._current_level_idx, self._max_night_idx)],
            # Welk model de compensatie aanstuurt, en de entiteit waar dat
            # advies staat (advice_sources). Tot v0.10.7-beta.4 altijd MPC; de
            # attributen heetten toen mpc_advised/mpc_advies_status/mpc_error.
            "advies_bron": self._advice_source,
            "advies_entiteit": advice_entity_id(
                self.hass, self._entry.entry_id, self._advice_source
            ),
            "advies": advised,
            # Waarom het advies leeg is: "geen_vraag" stuurt omlaag, de rest
            # telt voor de watchdog als uitval.
            "advies_status": status,
            "actual_supply": actual_supply,
            # Advies − werkelijke aanvoer: negatief = aanvoer te warm.
            "advies_fout": (
                round(advised - actual_supply, 1)
                if advised is not None and actual_supply is not None
                else None
            ),
            "hp_active": flow is not None and flow >= MIN_FLOW_LPH,
            "gas_active": boiler_heat is not None and boiler_heat > _GAS_THRESHOLD_W,
            "boiler_heat_w": round(boiler_heat) if boiler_heat is not None else None,
            # Wat de CiC werkelijk heeft, naast wat de compensatie wil
            # (effective_day/effective_night), plus hoe vaak er is bijgestuurd.
            "cic_day": self._select_state(self._day_sound_entity) if self.hass else None,
            "cic_night": self._select_state(self._night_sound_entity) if self.hass else None,
            "corrections": self._corrections,
            "last_correction": self._last_correction,
        }
