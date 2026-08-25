"""Periodieke bijsturing van de max-aanvoertemperatuur op stooklijn of MPC.

Schrijft maximaal één keer per interval naar de number-entity van de regelaar
die op dat moment daadwerkelijk stuurt — de Quatt CiC, of OpenQuatt als die de
buitenunits regelt. Welke dat is bepaalt `async_resolve_setting_entity`; zie
daar waarom een schrijfactie naar de verkeerde van de twee stil verdampt.

Schrijft alleen als de aanbevolen waarde meer dan `hysteresis` graden afwijkt
van de laatst geschreven waarde.

Bronentiteit (instelbaar via config):
- "stooklijn" → sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur
- "mpc"        → sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util

from .const import CONF_CH_MAX_WATER_ENTITY, DEFAULT_CH_MAX_WATER_SOURCE
from .discovery import (
    ROLE_CH_MAX_WATER,
    async_entity_has_value,
    async_heat_demand_link,
    async_resolve_setting_entity,
)

_LOGGER = logging.getLogger(__name__)

# Vaste entity-slugs waaronder de sensoren worden geregistreerd.
_SOURCE_ENTITY: dict[str, str] = {
    "stooklijn": "sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
    "mpc": "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
}

# De warmtevraag-sensor uit dezelfde integratie. Wijst de OpenQuatt-bronhelper
# hiernaar, dan loopt de aansturing via de vermogensvraag en heeft deze
# controller niets meer te zoeken — zie ``_demand_link_active``.
HEAT_DEMAND_ENTITY = "sensor.quatt_warmteanalyse_warmtevraag"


class ChMaxWaterController:
    """Beheert periodieke schrijfacties naar chMaxWaterTemperature.

    Gebruik:
        controller = ChMaxWaterController(hass, config)
        remove = controller.async_setup()   # registreert timer
        ...
        remove()                            # deregistreert bij unload
    """

    def __init__(
        self,
        hass: HomeAssistant,
        number_entity: str,
        source: str,
        hysteresis: float,
        interval_minutes: int,
    ) -> None:
        self._hass = hass
        # Wat de gebruiker heeft ingesteld — leeg is normaal en betekent
        # "detecteer zelf". Bewust niet één keer opgelost en vastgehouden: de
        # regelaar kan wisselen (OpenQuatt-node die na HA opkomt, of juist
        # losgekoppeld wordt), en dan hoort de schrijfactie mee te verhuizen.
        self._configured_entity = (number_entity or "").strip()
        self._source = source if source in _SOURCE_ENTITY else DEFAULT_CH_MAX_WATER_SOURCE
        self._hysteresis = hysteresis
        self._interval = timedelta(minutes=interval_minutes)

        self._last_written: float | None = None
        self._last_written_at: datetime | None = None
        self._target_entity: str | None = None
        # Eén logregel per keer dat de koppeling actief wórdt, niet elke tick.
        self._demand_link_logged = False

    # ------------------------------------------------------------------

    @property
    def source_entity(self) -> str:
        return _SOURCE_ENTITY[self._source]

    @property
    def target_entity(self) -> str | None:
        """De number-entity waar het laatst naartoe geschreven is."""
        return self._target_entity

    @property
    def last_written(self) -> float | None:
        return self._last_written

    @property
    def last_written_at(self) -> datetime | None:
        return self._last_written_at

    # ------------------------------------------------------------------

    def async_setup(self):
        """Registreer de periodieke timer. Geeft de remove-callback terug."""
        _LOGGER.info(
            "ChMaxWaterController gestart: bron=%s, bestemming=%s, "
            "hysteresis=%.1f°C, interval=%d min",
            self._source,
            self._configured_entity or "auto-detectie",
            self._hysteresis,
            self._interval.seconds // 60,
        )
        return async_track_time_interval(
            self._hass, self._async_tick, self._interval
        )

    async def _async_tick(self, _now: datetime) -> None:
        """Periodieke check: schrijf nieuwe waarde als dat nodig is."""
        if self._demand_link_active():
            # Twee routes naar dezelfde grootheid, en ze sluiten elkaar uit.
            # Loopt de warmtevraag rechtstreeks naar Power House, dan is het
            # waterplafond daar geen stuursignaal meer maar een veiligheids-
            # begrenzer (derate binnen 3 K, trip bij +5 K). Er alsnog een
            # aanvoeradvies naartoe schrijven knijpt de vraag die we net zelf
            # hebben gesteld — met een plafond dat, anders dan de vraag, niet
            # vanzelf vervalt als de koppeling wegvalt.
            if not self._demand_link_logged:
                _LOGGER.info(
                    "ChMaxWater: overgeslagen — de warmtevraag stuurt Power "
                    "House rechtstreeks aan. Het waterplafond is dan een "
                    "veiligheidsbegrenzer, geen stuurknop."
                )
                self._demand_link_logged = True
            return
        self._demand_link_logged = False

        recommended = self._read_recommended()
        if recommended is None:
            _LOGGER.debug("ChMaxWater: bronentiteit '%s' niet beschikbaar", self.source_entity)
            return

        entity_id = self._resolve_number_entity()
        if entity_id is None:
            _LOGGER.warning(
                "ChMaxWater: geen bruikbare number entity gevonden "
                "(ingesteld: %s), schrijfactie overgeslagen",
                self._configured_entity or "auto-detectie",
            )
            return

        clamped = self._clamp(recommended, entity_id)
        if clamped is None:
            return

        if not self._should_write(clamped):
            _LOGGER.debug(
                "ChMaxWater: geen schrijfactie (aanbevolen=%.1f, geschreven=%.1f, hysteresis=%.1f)",
                clamped,
                self._last_written if self._last_written is not None else float("nan"),
                self._hysteresis,
            )
            return

        await self._write(clamped, entity_id)

    # ------------------------------------------------------------------

    def _demand_link_active(self) -> bool:
        """Stuurt de gepubliceerde warmtevraag Power House nu rechtstreeks aan?

        De waarde zelf telt mee, niet alleen de koppeling. Publiceert de sensor
        niets — geen analysedata, of een bronmeting die te oud is — dan stuurt
        er via die route niets, en zou terugtreden beide wegen tegelijk
        stilleggen.
        """
        if not async_entity_has_value(self._hass, HEAT_DEMAND_ENTITY):
            return False
        return async_heat_demand_link(self._hass, HEAT_DEMAND_ENTITY).active

    def _read_recommended(self) -> float | None:
        """Lees de aanbevolen aanvoertemperatuur uit de geconfigureerde bronentiteit."""
        state = self._hass.states.get(self.source_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _resolve_number_entity(self) -> str | None:
        """Geef de number-entity van de regelaar die nu daadwerkelijk stuurt.

        Elke tick opnieuw, niet één keer bij het opstarten: welke regelaar de
        buitenunits aanstuurt kan veranderen zonder dat HA herstart. Zie
        ``async_resolve_setting_entity`` voor de voorkeursvolgorde.
        """
        resolved = async_resolve_setting_entity(
            self._hass,
            {CONF_CH_MAX_WATER_ENTITY: self._configured_entity},
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

        self._target_entity = resolved
        return resolved

    def _clamp(self, value: float, entity_id: str) -> float | None:
        """Begrens waarde op min/max/step van de gekozen number entity."""
        state = self._hass.states.get(entity_id)

        attrs = state.attributes
        min_val = attrs.get("min", 0.0)
        max_val = attrs.get("max", 80.0)
        step = attrs.get("step", 1.0)

        clamped = max(min_val, min(max_val, value))

        # Rond af op de stap van de entity (doorgaans 1°C).
        if step and step > 0:
            clamped = round(clamped / step) * step

        return clamped

    def _should_write(self, new_value: float) -> bool:
        """True als de afwijking ten opzichte van de laatste schrijfactie groot genoeg is."""
        if self._last_written is None:
            return True
        return abs(new_value - self._last_written) >= self._hysteresis

    async def _write(self, value: float, entity_id: str) -> None:
        """Schrijf de waarde naar de gekozen number entity via HA service."""
        try:
            await self._hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_written = value
            self._last_written_at = dt_util.now()
            _LOGGER.info(
                "ChMaxWater: %s ingesteld op %.1f°C (bron: %s)",
                entity_id,
                value,
                self._source,
            )
        except Exception as exc:
            _LOGGER.error("ChMaxWater: schrijfactie mislukt: %s", exc)
