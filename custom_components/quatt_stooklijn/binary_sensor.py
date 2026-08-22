"""Binary sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_BOILER_HEAT_ENTITY, DOMAIN
from .discovery import ROLE_BOILER_HEAT
from .helpers import get_device_info, get_float_state
from .sources import ENTITY_PREFIX, async_source_entity

_LOGGER = logging.getLogger(__name__)

_GAS_THRESHOLD_W = 200.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities from config entry.

    Bewust niet achter ``sound_level_enabled``: of de gasketel bijspringt is
    een eigenschap van de installatie, niet van de geluidscompensatie. De
    sensor hing daar historisch aan omdat de compensatie zijn eerste
    afnemer was — dashboards en automatiseringen die niets met geluid doen
    hadden hem daardoor niet.
    """
    async_add_entities([QuattGasActiveSensor(hass, entry)])


class QuattGasActiveSensor(BinarySensorEntity):
    """Binary sensor: gasketel actief als aanvulling op de warmtepomp."""

    _attr_has_entity_name = True
    _attr_name = "Gasketel Actief"
    _attr_icon = "mdi:fire"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_gas_boiler_active"
        # Entity-ID vastpinnen, net als de spiegelsensoren. HA leidt de ID van
        # een *nieuwe* entity af uit het gebied van het device, dus zonder dit
        # wordt het binary_sensor.bijkeuken_quatt_warmteanalyse_… Juist hier
        # telt dat: bij iedereen met de geluidscompensatie uit is dit een
        # nieuwe entity, en het dashboard verwijst naar de vaste ID.
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, f"{ENTITY_PREFIX}_gasketel_actief", hass=hass
        )
        self._attr_device_info = get_device_info(entry.entry_id)
        self._boiler_heat: float | None = None

    @property
    def _boiler_heat_entity(self) -> str:
        """Per aanroep opzoeken — welke integratie dit levert kan wisselen."""
        cfg = {**self._entry.data, **self._entry.options}
        return async_source_entity(
            self.hass, self._entry.entry_id, ROLE_BOILER_HEAT,
            config=cfg, conf_key=CONF_BOILER_HEAT_ENTITY,
        )

    @property
    def is_on(self) -> bool:
        return self._boiler_heat is not None and self._boiler_heat > _GAS_THRESHOLD_W

    @property
    def extra_state_attributes(self) -> dict:
        return {"boiler_heat_w": round(self._boiler_heat) if self._boiler_heat is not None else None}

    async def async_added_to_hass(self) -> None:
        self._boiler_heat = get_float_state(self.hass, self._boiler_heat_entity)
        # Alle kandidaten volgen, niet alleen de actieve: valt die weg, dan komt
        # er per definitie geen state-change meer binnen van zijn opvolger.
        from .sensor import candidate_entities

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                candidate_entities(
                    self.hass, self._entry.entry_id, (ROLE_BOILER_HEAT,)
                ),
                self._handle_change,
            )
        )

    @callback
    def _handle_change(self, event) -> None:
        new = event.data.get("new_state")
        try:
            self._boiler_heat = float(new.state) if new and new.state not in ("unknown", "unavailable") else None
        except (ValueError, TypeError):
            self._boiler_heat = None
        self.async_write_ha_state()
