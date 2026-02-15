"""Text entities for Quatt Stooklijn integration (editable dates)."""

from __future__ import annotations

import logging
from datetime import date

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_QUATT_END_DATE,
    CONF_QUATT_START_DATE,
    DOMAIN,
)
from .coordinator import QuattStooklijnCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Quatt Stooklijn text entities from config entry."""
    coordinator: QuattStooklijnCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        QuattDateText(coordinator, entry, CONF_QUATT_START_DATE, "Analyse Startdatum"),
        QuattDateText(coordinator, entry, CONF_QUATT_END_DATE, "Analyse Einddatum"),
    ])


class QuattDateText(TextEntity):
    """Editable date text entity."""

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    _attr_pattern = r"\d{4}-\d{2}-\d{2}"

    def __init__(
        self,
        coordinator: QuattStooklijnCoordinator,
        entry: ConfigEntry,
        config_key: str,
        name: str,
    ) -> None:
        """Initialize the text entity."""
        self._coordinator = coordinator
        self._entry = entry
        self._config_key = config_key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{config_key}"
        self._attr_icon = "mdi:calendar"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Quatt Warmteanalyse",
            "manufacturer": "Quatt",
            "model": "Warmteanalyse",
        }

    @property
    def native_value(self) -> str | None:
        """Return the current date value."""
        return self._coordinator.config.get(self._config_key)

    async def async_set_value(self, value: str) -> None:
        """Set a new date value."""
        value = value.strip()
        # Validate date format
        try:
            date.fromisoformat(value)
        except ValueError:
            _LOGGER.error("Invalid date format '%s', expected YYYY-MM-DD", value)
            return

        # Update coordinator config
        self._coordinator.config[self._config_key] = value

        # Persist to config entry
        new_data = dict(self._entry.data)
        new_data[self._config_key] = value
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)

        self.async_write_ha_state()
