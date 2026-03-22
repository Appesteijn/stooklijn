"""Persistence for the online thermal model state."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .analysis.thermal_model import OnlineRCModel
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.thermal_model"


class ThermalModelStore:
    """Persist OnlineRCModel state across HA restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.model = OnlineRCModel()

    async def async_load(self) -> None:
        """Load saved model state."""
        data = await self._store.async_load()
        if data:
            try:
                self.model = OnlineRCModel.from_dict(data)
                params = self.model.params
                _LOGGER.info(
                    "Loaded thermal model: %d updates, converged=%s, U=%.1f W/K",
                    params.get("n_updates", 0),
                    params.get("converged", False),
                    params.get("U_wk", 0),
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to load thermal model, starting fresh",
                    exc_info=True,
                )
                self.model = OnlineRCModel()
        else:
            _LOGGER.info("No saved thermal model found, starting fresh")

    async def async_save(self) -> None:
        """Save current model state."""
        await self._store.async_save(self.model.to_dict())
