"""Periodieke bijsturing van de max-aanvoertemperatuur op stooklijn of MPC.

Schrijft maximaal één keer per interval naar de number-entity van de regelaar
die op dat moment daadwerkelijk stuurt — de Quatt CiC, of OpenQuatt als die de
buitenunits regelt. Welke dat is bepaalt `async_resolve_setting_entity`; zie
daar waarom een schrijfactie naar de verkeerde van de twee stil verdampt.

Geschreven wordt het advies plus `margin`. Het advies is de aanvoer die het huis
nodig heeft; een plafond precies daarop laat de regelaar geen ruimte, want die
moduleert al terug vóór het plafond, en een tekort inhalen kan dan niet.

Schrijft alleen als het plafond meer dan `hysteresis` graden afwijkt van de
laatst geschreven waarde.

Welk advies (instelbaar via config): de gedeelde bronnenlijst uit
advice_sources — "stooklijn" (alleen buitentemperatuur) of "mpc" (met
kamercorrectie). Dezelfde keuze bestaat voor de geluidsniveaucompensatie.

Herstel (sinds v0.10.7-beta.7). Vóór de eerste schrijfactie naar een knop wordt
zijn waarde bewaard, per knop en over herstarts heen. Die waarde gaat terug
bij uitschakelen of herladen, bij het stoppen van Home Assistant, als het
advies langer dan ``CH_MAX_WATER_ADVICE_TIMEOUT`` wegblijft, als de warmtevraag
Power House rechtstreeks gaat aansturen, en voor de oude knop als de
bestemming wisselt. Daarvóór bleef het laatste plafond in al die gevallen
staan: een plafond van 30 °C uit zacht weer bleef de vorst in staan als de
adviessensor wegviel. Terug naar de eigen waarde, niet naar het maximum van de
knop: in de Quatt-app stelt de gebruiker zelf een maximum in, en dat moet
blijven gelden. Heeft iemand de knop intussen zelf verzet, dan blijft die
waarde staan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .advice_sources import advice_entity_id, read_advice, valid_source
from .const import (
    CH_MAX_WATER_ADVICE_TIMEOUT,
    CH_MAX_WATER_MARGIN_MAX,
    CONF_CH_MAX_WATER_ENTITY,
    DEFAULT_CH_MAX_WATER_MARGIN,
    DEFAULT_CH_MAX_WATER_SOURCE,
    DOMAIN,
)
from .discovery import (
    ROLE_CH_MAX_WATER,
    async_entity_has_value,
    async_heat_demand_link,
    async_resolve_setting_entity,
)
from .helpers import get_float_state, resolve_own_entity_id

_LOGGER = logging.getLogger(__name__)

# De warmtevraag-sensor uit dezelfde integratie. Wijst de OpenQuatt-bronhelper
# hiernaar, dan loopt de aansturing via de vermogensvraag en heeft deze
# controller niets meer te zoeken — zie ``_demand_link_active``. Dit is de
# vastgepinde id; opgezocht wordt op unique_id (``heat_demand_entity``).
HEAT_DEMAND_ENTITY = "sensor.quatt_warmteanalyse_warmtevraag"
HEAT_DEMAND_UNIQUE_SUFFIX = "heat_demand"

STORAGE_VERSION = 1

#: Een herstel bij het stoppen van HA mag het afsluiten niet ophouden als de
#: knop (CiC-cloud, ESPHome-node) zelf al niet meer reageert.
RESTORE_TIMEOUT_S = 10.0
#: Een gewone schrijfactie mag langer duren (CiC via de cloud).
WRITE_TIMEOUT_S = 30.0


def storage_key(entry_id: str | None) -> str:
    return f"{DOMAIN}.ch_max_water_{entry_id or 'default'}"


def heat_demand_entity(hass: HomeAssistant, entry_id: str | None) -> str:
    """De warmtevraag-sensor van deze entry, op unique_id opgezocht.

    Tot v0.10.7-beta.8 een vaste entity-ID. Een hernoemde sensor, of de sensor
    van een tweede entry, werd dan niet herkend: deze controller schreef door
    terwijl de warmtevraag Power House al aanstuurde.
    """
    if entry_id:
        found = resolve_own_entity_id(hass, "sensor", entry_id, HEAT_DEMAND_UNIQUE_SUFFIX)
        if isinstance(found, str):
            return found
    return HEAT_DEMAND_ENTITY


class ChMaxWaterController:
    """Beheert periodieke schrijfacties naar chMaxWaterTemperature.

    Gebruik:
        controller = ChMaxWaterController(hass, config)
        remove = controller.async_setup()   # registreert timer + stop-listener
        ...
        await controller.async_shutdown()   # zet de knop terug (unload)
        remove()

    Met ``restore_only`` schrijft hij geen plafond, maar probeert hij alleen een
    openstaand herstel af te maken: voor een installatie waar de bijsturing uit
    staat terwijl een eerder herstel mislukte (knop onbereikbaar bij het
    uitschakelen, of HA viel weg zonder netjes te stoppen).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        number_entity: str,
        source: str,
        hysteresis: float,
        interval_minutes: int,
        margin: float = DEFAULT_CH_MAX_WATER_MARGIN,
        entry_id: str | None = None,
        store: Store | None = None,
        restore_only: bool = False,
        anchor_config: dict | None = None,
    ) -> None:
        self._hass = hass
        # Om de adviessensor op unique_id te vinden (advice_sources). Zonder
        # entry_id valt de lezing terug op de vaste entity-slug.
        self._entry_id = entry_id
        # Wat de gebruiker heeft ingesteld — leeg is normaal en betekent
        # "detecteer zelf". Bewust niet één keer opgelost en vastgehouden: de
        # regelaar kan wisselen (OpenQuatt-node die na HA opkomt, of juist
        # losgekoppeld wordt), en dan hoort de schrijfactie mee te verhuizen.
        self._configured_entity = (number_entity or "").strip()
        self._source = valid_source(source, DEFAULT_CH_MAX_WATER_SOURCE)
        self._hysteresis = hysteresis
        self._interval = timedelta(minutes=interval_minutes)
        # Begrensd zoals in de opties: een negatieve marge zou het plafond
        # onder het advies leggen, precies het probleem dat ze oplost.
        self._margin = max(0.0, min(CH_MAX_WATER_MARGIN_MAX, float(margin)))
        self._restore_only = restore_only
        # De metingen van de entry (vermogen, debiet, …): die zeggen bij wélke
        # CiC deze installatie hoort. Zonder dit anker koos de detectie bij twee
        # CiC's in één HA de eerste uit het register, ook die van de buren.
        self._anchor_config = dict(anchor_config or {})

        self._last_written: float | None = None
        self._last_written_at: datetime | None = None
        # Het onafgeronde doel (advies + marge) bij de laatste schrijfactie.
        # De hysterese vergelijkt hierop: na afronden op de stap van de knop
        # sprong 0,2 K ruis rond een afrondingsgrens een hele graad, en met
        # stap 1 en hysterese 1,0 gaf dat 48 schrijfacties per etmaal.
        self._last_target: float | None = None
        self._target_entity: str | None = None
        # Eén logregel per keer dat de koppeling actief wórdt, niet elke tick.
        self._demand_link_logged = False

        # Pas bij het eerste gebruik aangemaakt (zie _get_store).
        self._store = store
        # entity_id -> {"original": waarde vóór de eerste schrijfactie,
        #               "written": laatst door ons geschreven waarde}
        self._saved: dict[str, dict[str, float | None]] | None = None
        self._lock = asyncio.Lock()
        # Vanaf de start, niet vanaf de eerste tick zonder advies: na een
        # herstart zonder advies hoort de watchdog gewoon te lopen.
        self._advice_seen_at: datetime = dt_util.utcnow()
        self._stopped = False

    # ------------------------------------------------------------------

    @property
    def source_entity(self) -> str:
        return advice_entity_id(self._hass, self._entry_id, self._source)

    @property
    def target_entity(self) -> str | None:
        """De number-entity waar het laatst naartoe geschreven is."""
        return self._target_entity

    @property
    def margin(self) -> float:
        """Graden boven het advies waarop het plafond komt te liggen."""
        return self._margin

    @property
    def last_written(self) -> float | None:
        return self._last_written

    @property
    def last_written_at(self) -> datetime | None:
        return self._last_written_at

    # ------------------------------------------------------------------

    def async_setup(self):
        """Registreer timer en stop-listener. Geeft de remove-callback terug."""
        if self._restore_only:
            _LOGGER.debug("ChMaxWater: alleen herstel van een eerder plafond")
        else:
            _LOGGER.info(
                "ChMaxWaterController gestart: bron=%s, bestemming=%s, "
                "marge=%.1f°C, hysteresis=%.1f°C, interval=%d min",
                self._source,
                self._configured_entity or "auto-detectie",
                self._margin,
                self._hysteresis,
                self._interval.seconds // 60,
            )
        remove_timer = async_track_time_interval(
            self._hass, self._async_tick, self._interval
        )
        fired = False

        async def _on_stop(_event) -> None:
            nonlocal fired
            fired = True
            await self.async_shutdown("Home Assistant stopt")

        remove_stop = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_stop
        )

        def _remove() -> None:
            remove_timer()
            # Een afgevuurde listen_once is al weg; nog eens verwijderen geeft
            # een ValueError in HA's core (zie ook de startup-listener).
            if not fired:
                remove_stop()

        return _remove

    async def async_shutdown(self, reason: str = "bijsturing gestopt") -> None:
        """Zet elke knop terug en schrijf daarna niets meer."""
        async with self._lock:
            self._stopped = True
            await self._async_restore(reason)

    async def _async_tick(self, _now: datetime | None) -> None:
        """Periodieke check: schrijf nieuwe waarde als dat nodig is."""
        async with self._lock:
            if self._stopped:
                return
            if self._restore_only:
                await self._async_restore("bijsturing staat uit")
                return
            await self._async_tick_locked()

    async def _async_tick_locked(self) -> None:
        if self._demand_link_active():
            # Twee routes naar dezelfde grootheid, en ze sluiten elkaar uit.
            # Loopt de warmtevraag rechtstreeks naar Power House, dan is het
            # waterplafond daar geen stuursignaal meer maar een veiligheids-
            # begrenzer (derate binnen 3 K, trip bij +5 K). Er alsnog een
            # aanvoeradvies naartoe schrijven knijpt de vraag die we net zelf
            # hebben gesteld. Een plafond van eerder ook: dat gaat dus terug.
            if not self._demand_link_logged:
                _LOGGER.info(
                    "ChMaxWater: overgeslagen — de warmtevraag stuurt Power "
                    "House rechtstreeks aan. Het waterplafond is dan een "
                    "veiligheidsbegrenzer, geen stuurknop."
                )
                self._demand_link_logged = True
            await self._async_restore("de warmtevraag stuurt Power House aan")
            return
        self._demand_link_logged = False

        recommended = self._read_recommended()
        if recommended is None:
            missing = (dt_util.utcnow() - self._advice_seen_at).total_seconds()
            _LOGGER.debug(
                "ChMaxWater: bronentiteit '%s' niet beschikbaar (%.0f min)",
                self.source_entity, missing / 60,
            )
            if missing >= CH_MAX_WATER_ADVICE_TIMEOUT:
                await self._async_restore(
                    f"geen advies sinds {missing / 60:.0f} minuten"
                )
            return
        self._advice_seen_at = dt_util.utcnow()

        entity_id = self._resolve_number_entity()
        if entity_id is None:
            _LOGGER.warning(
                "ChMaxWater: geen bruikbare number entity gevonden "
                "(ingesteld: %s), schrijfactie overgeslagen",
                self._configured_entity or "auto-detectie",
            )
            return

        # Een knop waar we niet meer naartoe schrijven, krijgt zijn eigen
        # waarde terug (bestemmingswissel CiC <-> OpenQuatt).
        await self._async_restore("bestemming gewisseld", exclude=entity_id)

        target = recommended + self._margin
        clamped = self._clamp(target, entity_id)
        if clamped is None:
            return

        if not self._should_write(clamped, target):
            _LOGGER.debug(
                "ChMaxWater: geen schrijfactie (plafond=%.1f, geschreven=%.1f, hysteresis=%.1f)",
                clamped,
                self._last_written if self._last_written is not None else float("nan"),
                self._hysteresis,
            )
            return

        await self._write(clamped, entity_id, target)

    # ------------------------------------------------------------------

    def _demand_link_active(self) -> bool:
        """Stuurt de gepubliceerde warmtevraag Power House nu rechtstreeks aan?

        De waarde zelf telt mee, niet alleen de koppeling. Publiceert de sensor
        niets — geen analysedata, of een bronmeting die te oud is — dan stuurt
        er via die route niets, en zou terugtreden beide wegen tegelijk
        stilleggen.
        """
        entity_id = heat_demand_entity(self._hass, self._entry_id)
        if not async_entity_has_value(self._hass, entity_id):
            return False
        return async_heat_demand_link(self._hass, entity_id).active

    def _read_recommended(self) -> float | None:
        """Lees de aanbevolen aanvoertemperatuur van de gekozen bron."""
        return read_advice(self._hass, self._entry_id, self._source)[0]

    def _resolve_number_entity(self) -> str | None:
        """Geef de number-entity van de regelaar die nu daadwerkelijk stuurt.

        Elke tick opnieuw, niet één keer bij het opstarten: welke regelaar de
        buitenunits aanstuurt kan veranderen zonder dat HA herstart. Zie
        ``async_resolve_setting_entity`` voor de voorkeursvolgorde.
        """
        resolved = async_resolve_setting_entity(
            self._hass,
            {**self._anchor_config, CONF_CH_MAX_WATER_ENTITY: self._configured_entity},
            CONF_CH_MAX_WATER_ENTITY,
            ROLE_CH_MAX_WATER,
        )
        # De laatste stap van de resolver mag een terugvalnaam teruggeven die
        # helemaal niet bestaat — prima om in de UI te tonen, niet om naar te
        # schrijven. Zonder deze check loopt _clamp stuk op een lege state.
        if resolved is None or not async_entity_has_value(self._hass, resolved):
            return None

        if (
            self._configured_entity
            and resolved != self._configured_entity
            and self._target_entity != resolved
        ):
            _LOGGER.warning(
                "ChMaxWater: ingestelde entity '%s' geeft geen waarde, "
                "geschreven naar '%s'. Pas de entity-instelling aan om deze "
                "melding te laten verdwijnen.",
                self._configured_entity,
                resolved,
            )

        if self._target_entity is not None and self._target_entity != resolved:
            # Andere bestemming, dus de hysteresis-geschiedenis slaat nergens
            # meer op: die knop staat op zijn eigen waarde en heeft bovendien
            # zijn eigen min/max. Eerstvolgende tick schrijft onvoorwaardelijk.
            _LOGGER.info(
                "ChMaxWater: bestemming gewisseld van '%s' naar '%s'",
                self._target_entity,
                resolved,
            )
            self._last_written = None
            self._last_target = None

        self._target_entity = resolved
        return resolved

    def _clamp(self, value: float, entity_id: str) -> float | None:
        """Begrens waarde op min/max/step van de gekozen number entity.

        Zonder ``min`` of ``max`` geen schrijfactie: een terugval op 0 °C als
        ondergrens schreef een plafond van 20 °C naar een knop die dat niet kan.
        """
        state = self._hass.states.get(entity_id)

        attrs = state.attributes
        min_val = attrs.get("min")
        max_val = attrs.get("max")
        if min_val is None or max_val is None:
            _LOGGER.warning(
                "ChMaxWater: %s meldt geen min/max, schrijfactie overgeslagen",
                entity_id,
            )
            return None
        step = attrs.get("step", 1.0)

        clamped = max(min_val, min(max_val, value))

        # Rond af op de stap van de entity (doorgaans 1°C).
        if step and step > 0:
            clamped = round(clamped / step) * step

        return clamped

    def _should_write(self, new_value: float, target: float | None = None) -> bool:
        """True als het plafond genoeg veranderd is om te schrijven.

        Met ``target`` (het onafgeronde doel) telt de beweging van het advies,
        niet die van de afgeronde waarde; zie ``_last_target``. Een plafond dat
        na afronden gelijk blijft, wordt nooit opnieuw geschreven.
        """
        if self._last_written is None:
            return True
        if new_value == self._last_written:
            return False
        if target is not None and self._last_target is not None:
            return abs(target - self._last_target) >= self._hysteresis
        return abs(new_value - self._last_written) >= self._hysteresis

    async def _write(self, value: float, entity_id: str, target: float | None = None) -> None:
        """Schrijf de waarde naar de gekozen number entity via HA service."""
        saved = await self._async_saved()
        record = saved.get(entity_id)
        if record is None:
            # Eerste schrijfactie naar deze knop: eerst onthouden wat er stond.
            original = get_float_state(self._hass, entity_id)
            if original is None:
                _LOGGER.warning(
                    "ChMaxWater: huidige waarde van %s onbekend, niet "
                    "geschreven (er zou niets zijn om naar terug te zetten)",
                    entity_id,
                )
                return
            record = {"original": original, "written": None}
            saved[entity_id] = record
            await self._async_save()
        try:
            # Met time-out: een hangende knop houdt anders het slot vast, en
            # daarmee het herstel bij unload of het stoppen van HA.
            async with asyncio.timeout(WRITE_TIMEOUT_S):
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity_id, "value": value},
                    blocking=True,
                )
        except Exception as exc:  # incl. TimeoutError
            _LOGGER.error(
                "ChMaxWater: schrijfactie mislukt: %s", exc or type(exc).__name__
            )
            return
        self._last_written = value
        self._last_written_at = dt_util.now()
        self._last_target = target
        record["written"] = value
        await self._async_save()
        _LOGGER.info(
            "ChMaxWater: %s ingesteld op %.1f°C (bron: %s, marge %.1f°C)",
            entity_id,
            value,
            self._source,
            self._margin,
        )

    # ------------------------------------------------------------------
    # Herstel

    def _get_store(self) -> Store:
        if self._store is None:
            self._store = Store(self._hass, STORAGE_VERSION, storage_key(self._entry_id))
        return self._store

    async def _async_saved(self) -> dict[str, dict[str, float | None]]:
        if self._saved is None:
            data = None
            try:
                data = await self._get_store().async_load()
            except Exception:
                _LOGGER.warning("ChMaxWater: opgeslagen herstelwaarden onleesbaar", exc_info=True)
            raw = data.get("entities", {}) if isinstance(data, dict) else {}
            self._saved = {}
            for entity_id, rec in raw.items():
                if isinstance(rec, dict) and isinstance(rec.get("original"), (int, float)):
                    written = rec.get("written")
                    self._saved[entity_id] = {
                        "original": float(rec["original"]),
                        "written": float(written) if isinstance(written, (int, float)) else None,
                    }
        return self._saved

    async def _async_save(self) -> None:
        try:
            await self._get_store().async_save({"entities": self._saved or {}})
        except Exception:
            _LOGGER.warning("ChMaxWater: herstelwaarden opslaan mislukt", exc_info=True)

    async def _async_restore(self, reason: str, exclude: str | None = None) -> None:
        """Zet elke knop met een bewaarde waarde terug, behalve ``exclude``.

        Lukt het niet (knop onbereikbaar), dan blijft de waarde bewaard en
        volgt een nieuwe poging bij de volgende tik of start.
        """
        saved = await self._async_saved()
        changed = False
        for entity_id in [e for e in saved if e != exclude]:
            record = saved[entity_id]
            original = record["original"]
            current = get_float_state(self._hass, entity_id)
            if current is None:
                _LOGGER.debug("ChMaxWater: %s onbereikbaar, herstel later", entity_id)
                continue
            written = record.get("written")
            step = (self._hass.states.get(entity_id).attributes or {}).get("step") or 1.0
            if written is not None and abs(current - written) > step / 2:
                # Iemand heeft de knop zelf verzet (app, dashboard). Dat is de
                # nieuwe instelling; die terugdraaien zou hem overschrijven.
                _LOGGER.info(
                    "ChMaxWater: %s staat op %.1f°C, niet op de geschreven "
                    "%.1f°C; niet teruggezet (%s)", entity_id, current, written, reason,
                )
            elif abs(current - original) > step / 2:
                try:
                    async with asyncio.timeout(RESTORE_TIMEOUT_S):
                        await self._hass.services.async_call(
                            "number",
                            "set_value",
                            {"entity_id": entity_id, "value": original},
                            blocking=True,
                        )
                except Exception as exc:  # incl. TimeoutError
                    _LOGGER.warning(
                        "ChMaxWater: terugzetten van %s mislukt (%s), later opnieuw",
                        entity_id, exc or type(exc).__name__,
                    )
                    continue
                _LOGGER.info(
                    "ChMaxWater: %s teruggezet op %.1f°C (%s)", entity_id, original, reason
                )
            del saved[entity_id]
            changed = True
            if entity_id == self._target_entity:
                self._last_written = None
                self._last_written_at = None
                self._last_target = None
        if changed:
            await self._async_save()
