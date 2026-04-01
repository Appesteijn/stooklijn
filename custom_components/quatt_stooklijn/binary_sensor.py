"""Binary sensor entities for Quatt Stooklijn integration."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_SOUND_LEVEL_ENABLED, DOMAIN
from .helpers import get_device_info, get_float_state

_LOGGER = logging.getLogger(__name__)

_BOILER_HEAT_ENTITY = "sensor.heatpump_boiler_heat_power"
_GAS_THRESHOLD_W = 200.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities from config entry."""
    if not {**entry.data, **entry.options}.get(CONF_SOUND_LEVEL_ENABLED, False):
        return

    async_add_entities([QuattGasActiveSensor(entry)])


class QuattGasActiveSensor(BinarySensorEntity):
    """Binary sensor: gasketel actief als aanvulling op de warmtepomp."""

    _attr_has_entity_name = True
    _attr_name = "Gasketel Actief"
    _attr_icon = "mdi:fire"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_gas_boiler_active"
        self._attr_device_info = get_device_info(entry.entry_id)
        self._boiler_heat: float | None = None

    @property
    def is_on(self) -> bool:
        return self._boiler_heat is not None and self._boiler_heat > _GAS_THRESHOLD_W

    @property
    def extra_state_attributes(self) -> dict:
        return {"boiler_heat_w": round(self._boiler_heat) if self._boiler_heat is not None else None}

    async def async_added_to_hass(self) -> None:
        self._boiler_heat = get_float_state(self.hass, _BOILER_HEAT_ENTITY)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [_BOILER_HEAT_ENTITY],
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
