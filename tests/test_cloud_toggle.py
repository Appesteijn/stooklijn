"""De Quatt cloud-API moet uitgezet kunnen worden zonder historie te verliezen.

Wat hier bewaakt wordt is één ding: 'cloud uit' betekent *geen API-aanroepen*,
niet 'geen cache'. De opgebouwde insights-cache (retentie 100 jaar) moet gewoon
gelezen blijven worden, anders zou uitzetten de historie alsnog weggooien.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from custom_components.quatt_stooklijn.analysis import quatt as quatt_mod
from custom_components.quatt_stooklijn.const import (
    CONF_QUATT_CLOUD_ENABLED,
    DEFAULT_QUATT_CLOUD_ENABLED,
)


def _run(coro):
    return asyncio.run(coro)


def _fetch(use_cloud: bool):
    """Draai async_fetch_quatt_insights en geef de cache_only-vlaggen terug.

    Stap 2 (historisch venster) is altijd cache-only; stap 3 (recent venster) is
    de tak die van de schakelaar afhangt. Beide aanroepen worden vastgelegd.
    """
    cache = MagicMock()
    cache.get_stats.return_value = {"total_days": 100}
    cache.async_save = AsyncMock()

    calls: list[bool] = []

    async def _fake_api_days(hass, start, end, cache_, *, cache_only=False):
        calls.append(cache_only)
        return ([], [], 0, 0)

    with (
        patch.object(quatt_mod, "_get_cache", AsyncMock(return_value=cache)),
        patch.object(
            quatt_mod, "_async_fetch_recorder_daily",
            AsyncMock(return_value=pd.DataFrame()),
        ),
        patch.object(quatt_mod, "async_discover_quatt_entities", MagicMock(return_value={})),
        patch.object(quatt_mod, "async_resolve_entity", MagicMock(return_value="sensor.x")),
        patch.object(quatt_mod, "_async_fetch_api_days", _fake_api_days),
    ):
        _run(
            quatt_mod.async_fetch_quatt_insights(
                MagicMock(), "2025-07-01", "2026-08-27", use_cloud=use_cloud
            )
        )
    return calls


class TestCloudSchakelaar:
    def test_cloud_aan_bevraagt_de_api(self):
        calls = _fetch(use_cloud=True)
        # Laatste aanroep is stap 3: die mag de API raadplegen.
        assert calls[-1] is False

    def test_cloud_uit_doet_geen_enkele_api_aanroep(self):
        calls = _fetch(use_cloud=False)
        assert calls, "stap 3 moet nog steeds draaien, alleen zonder API"
        assert all(c is True for c in calls)

    def test_cloud_uit_leest_de_cache_nog_steeds(self):
        """Het venster wordt niet overgeslagen — anders verdween de historie."""
        assert len(_fetch(use_cloud=False)) == len(_fetch(use_cloud=True))

    def test_standaard_is_aan(self):
        """Bestaande installaties mogen niets merken van de upgrade."""
        assert DEFAULT_QUATT_CLOUD_ENABLED is True
        assert CONF_QUATT_CLOUD_ENABLED == "quatt_cloud_enabled"
