"""Cache helper for Quatt insights data."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "quatt_stooklijn_insights_cache"


class QuattInsightsCache:
    """Cache for Quatt insights data to reduce API calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the cache."""
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._cache: dict[str, dict[str, Any]] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load cache from storage."""
        if self._loaded:
            return

        data = await self._store.async_load()
        if data:
            self._cache = data.get("insights", {})
            _LOGGER.info("Loaded insights cache with %d days", len(self._cache))
        else:
            self._cache = {}
            _LOGGER.info("No existing cache found, starting fresh")

        self._loaded = True

    async def async_save(self) -> None:
        """Save cache to storage."""
        await self._store.async_save({"insights": self._cache})
        _LOGGER.debug("Saved insights cache with %d days", len(self._cache))

    def get(self, date_str: str) -> dict[str, Any] | None:
        """Get cached data for a specific date.

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            Cached insights data or None if not in cache
        """
        return self._cache.get(date_str)

    def set(self, date_str: str, data: dict[str, Any]) -> None:
        """Cache data for a specific date.

        Args:
            date_str: Date in YYYY-MM-DD format
            data: Insights data to cache
        """
        self._cache[date_str] = data
        _LOGGER.debug("Cached data for %s", date_str)

    def should_cache(self, date_str: str) -> bool:
        """Determine if a date should be cached.

        Only cache dates that are at least 1 day old (completed days).
        Today's data might still change, so don't cache it.

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            True if this date should be cached
        """
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = datetime.now().date()

            # Only cache dates before today
            return date < today

        except ValueError:
            return False

    async def async_cleanup(self, days_to_keep: int = 365) -> None:
        """Remove cache entries older than specified days.

        Args:
            days_to_keep: Number of days to keep in cache (default 1 year)
        """
        cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).date()
        cutoff_str = cutoff_date.strftime("%Y-%m-%d")

        to_remove = [date_str for date_str in self._cache if date_str < cutoff_str]

        for date_str in to_remove:
            del self._cache[date_str]

        if to_remove:
            _LOGGER.info("Removed %d old cache entries", len(to_remove))
            await self.async_save()

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        if not self._cache:
            return {
                "total_days": 0,
                "oldest_date": None,
                "newest_date": None,
            }

        dates = sorted(self._cache.keys())
        return {
            "total_days": len(dates),
            "oldest_date": dates[0] if dates else None,
            "newest_date": dates[-1] if dates else None,
        }
