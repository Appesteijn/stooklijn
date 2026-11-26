"""Tests for the coordinator and data model."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.coordinator import (
    QuattStooklijnData,
)
from custom_components.quatt_stooklijn.analysis.stooklijn import StooklijnResult
from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult


class TestQuattStooklijnData:
    """Tests for the QuattStooklijnData dataclass."""

    def test_default_values(self):
        """Default data object should have expected defaults."""
        data = QuattStooklijnData()

        assert data.analysis_status == "idle"
        assert data.average_cop is None
        assert data.last_analysis is None
        assert isinstance(data.stooklijn, StooklijnResult)
        assert isinstance(data.heat_loss_hp, HeatLossResult)
        assert isinstance(data.heat_loss_gas, HeatLossResult)

    def test_status_mutation(self):
        """Status should be mutable (used in service handlers)."""
        data = QuattStooklijnData()
        data.analysis_status = "running"
        assert data.analysis_status == "running"

        data.analysis_status = "completed"
        assert data.analysis_status == "completed"

        data.analysis_status = "error"
        assert data.analysis_status == "error"


class TestDagelijkseAnalyse:
    """``async_scheduled_refresh``: een mislukte of lege dagrun wist geen
    werkende analyse, en zet de sensoren niet op onbeschikbaar."""

    @staticmethod
    def _coordinator(previous_status: str = "completed"):
        from datetime import datetime, timezone

        from custom_components.quatt_stooklijn.coordinator import (
            QuattStooklijnCoordinator,
        )

        # Zonder __init__: die maakt stores aan, en die zijn hier niet nodig.
        coord = QuattStooklijnCoordinator.__new__(QuattStooklijnCoordinator)
        coord.data = QuattStooklijnData(
            analysis_status=previous_status,
            last_analysis=datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc),
            average_cop=3.1,
        )
        coord.published = []
        coord.async_set_updated_data = lambda d: coord.published.append(
            (d, d.analysis_status)
        )
        return coord

    async def test_geslaagd_publiceert_nieuwe_analyse(self):
        coord = self._coordinator()
        new = QuattStooklijnData(analysis_status="completed", average_cop=3.4)

        async def run():
            coord.data = new
            return new

        coord._async_update_data = run
        await coord.async_scheduled_refresh()
        assert coord.published[0][1] == "running"
        assert coord.published[-1] == (new, "completed")
        assert coord.data is new

    async def test_fout_laat_vorige_analyse_staan(self):
        coord = self._coordinator()
        previous = coord.data

        async def run():
            raise RuntimeError("recorder weg")

        coord._async_update_data = run
        await coord.async_scheduled_refresh()  # gooit niet
        assert coord.data is previous
        assert coord.data.average_cop == 3.1
        assert coord.published[-1] == (previous, "error")

    async def test_geen_data_wist_geslaagde_analyse_niet(self):
        coord = self._coordinator()
        previous = coord.data
        empty = QuattStooklijnData(analysis_status="no_data")

        async def run():
            coord.data = empty
            return empty

        coord._async_update_data = run
        await coord.async_scheduled_refresh()
        assert coord.data is previous
        assert coord.published[-1] == (previous, "completed")

    async def test_geen_data_zonder_eerdere_analyse_wel_tonen(self):
        coord = self._coordinator(previous_status="idle")
        empty = QuattStooklijnData(analysis_status="no_data")

        async def run():
            coord.data = empty
            return empty

        coord._async_update_data = run
        await coord.async_scheduled_refresh()
        assert coord.published[-1] == (empty, "no_data")

    async def test_overgeslagen_als_er_al_een_draait(self):
        coord = self._coordinator(previous_status="running")
        called = []

        async def run():
            called.append(True)

        coord._async_update_data = run
        await coord.async_scheduled_refresh()
        assert not called
        assert not coord.published


class TestDagelijkseAnalyseIngepland:
    """De setup plant de dagrun op het tijdstip uit ``const``."""

    def test_tijdstip_na_de_nacht_niet_rond_middernacht(self):
        from custom_components.quatt_stooklijn.const import (
            ANALYSIS_DAILY_HOUR,
            ANALYSIS_DAILY_MINUTE,
            PRICE_SHIFT_RUN_HOUR,
        )

        assert 4 <= ANALYSIS_DAILY_HOUR <= 9
        assert ANALYSIS_DAILY_HOUR != PRICE_SHIFT_RUN_HOUR
        assert 0 <= ANALYSIS_DAILY_MINUTE < 60
