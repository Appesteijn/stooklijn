"""Periodieke bijsturing van chMaxWaterTemperature op basis van stooklijn of MPC.

Schrijft maximaal één keer per interval naar de Quatt remote API (via de
number.set_value service). Schrijft alleen als de aanbevolen waarde meer dan
`hysteresis` graden afwijkt van de laatst geschreven waarde.

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

from .const import CH_MAX_WATER_ENTITY_LEGACY, DEFAULT_CH_MAX_WATER_SOURCE

_LOGGER = logging.getLogger(__name__)

# Vaste entity-slugs waaronder de sensoren worden geregistreerd.
_SOURCE_ENTITY: dict[str, str] = {
    "stooklijn": "sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
    "mpc": "sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
}


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
        self._number_entity = number_entity
        self._source = source if source in _SOURCE_ENTITY else DEFAULT_CH_MAX_WATER_SOURCE
        self._hysteresis = hysteresis
        self._interval = timedelta(minutes=interval_minutes)

        self._last_written: float | None = None
        self._last_written_at: datetime | None = None

    # ------------------------------------------------------------------

    @property
    def source_entity(self) -> str:
        return _SOURCE_ENTITY[self._source]

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
            "ChMaxWaterController gestart: bron=%s, entity=%s, "
            "hysteresis=%.1f°C, interval=%d min",
            self._source,
            self._number_entity,
            self._hysteresis,
            self._interval.seconds // 60,
        )
        return async_track_time_interval(
            self._hass, self._async_tick, self._interval
        )

    async def _async_tick(self, _now: datetime) -> None:
        """Periodieke check: schrijf nieuwe waarde als dat nodig is."""
        recommended = self._read_recommended()
        if recommended is None:
            _LOGGER.debug("ChMaxWater: bronentiteit '%s' niet beschikbaar", self.source_entity)
            return

        entity_id = self._resolve_number_entity()
        if entity_id is None:
            _LOGGER.warning(
                "ChMaxWater: number entity '%s' niet beschikbaar, schrijfactie overgeslagen",
                self._number_entity,
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

    def _read_recommended(self) -> float | None:
        """Lees de aanbevolen aanvoertemperatuur uit de bronentiteit."""
        state = self._hass.states.get(self.source_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _resolve_number_entity(self) -> str | None:
        """Geef de entity-ID terug die beschikbaar is (geconfigureerd of legacy fallback)."""
        state = self._hass.states.get(self._number_entity)
        if state is not None and state.state not in ("unknown", "unavailable"):
            return self._number_entity
        # Probeer de legacy entity-ID (Quatt ≤1.0.2: heatpump_ prefix).
        legacy = CH_MAX_WATER_ENTITY_LEGACY
        if self._number_entity != legacy:
            state_legacy = self._hass.states.get(legacy)
            if state_legacy is not None and state_legacy.state not in ("unknown", "unavailable"):
                _LOGGER.warning(
                    "ChMaxWater: '%s' niet gevonden, gebruik legacy '%s'. "
                    "Pas de entity-instelling aan om deze melding te voorkomen.",
                    self._number_entity,
                    legacy,
                )
                return legacy
        return None

    def _clamp(self, value: float, entity_id: str) -> float | None:
        """Begrens waarde op min/max van de Quatt number entity."""
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
        """Schrijf de waarde naar de Quatt number entity via HA service."""
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
                "ChMaxWater: chMaxWaterTemperature ingesteld op %.1f°C (bron: %s)",
                value,
                self._source,
            )
        except Exception as exc:
            _LOGGER.error("ChMaxWater: schrijfactie mislukt: %s", exc)
