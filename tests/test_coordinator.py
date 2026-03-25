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
