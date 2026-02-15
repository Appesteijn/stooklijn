"""Tests for the coordinator and data model."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.coordinator import (
    QuattStooklijnData,
    _calc_stooklijn_from_points,
)
from custom_components.quatt_stooklijn.analysis.stooklijn import StooklijnResult
from custom_components.quatt_stooklijn.analysis.heat_loss import HeatLossResult
from custom_components.quatt_stooklijn.const import (
    CONF_ACTUAL_STOOKLIJN_TEMP1,
    CONF_ACTUAL_STOOKLIJN_POWER1,
    CONF_ACTUAL_STOOKLIJN_TEMP2,
    CONF_ACTUAL_STOOKLIJN_POWER2,
)


class TestCalcStooklijnFromPoints:
    """Tests for _calc_stooklijn_from_points()."""

    def test_valid_two_points(self):
        """Two distinct points should produce correct slope and intercept."""
        config = {
            CONF_ACTUAL_STOOKLIJN_TEMP1: -5.0,
            CONF_ACTUAL_STOOKLIJN_POWER1: 8000.0,
            CONF_ACTUAL_STOOKLIJN_TEMP2: 15.0,
            CONF_ACTUAL_STOOKLIJN_POWER2: 2000.0,
        }
        slope, intercept = _calc_stooklijn_from_points(config)

        # slope = (2000 - 8000) / (15 - (-5)) = -6000 / 20 = -300
        assert slope == pytest.approx(-300.0)
        # intercept = 8000 - (-300) * (-5) = 8000 - 1500 = 6500
        assert intercept == pytest.approx(6500.0)

    def test_missing_point(self):
        """Missing any of the 4 values should return (None, None)."""
        config = {
            CONF_ACTUAL_STOOKLIJN_TEMP1: -5.0,
            CONF_ACTUAL_STOOKLIJN_POWER1: 8000.0,
            CONF_ACTUAL_STOOKLIJN_TEMP2: 15.0,
            # Missing POWER2
        }
        slope, intercept = _calc_stooklijn_from_points(config)
        assert slope is None
        assert intercept is None

    def test_same_temperature(self):
        """Same temperature for both points → division by zero → (None, None)."""
        config = {
            CONF_ACTUAL_STOOKLIJN_TEMP1: 5.0,
            CONF_ACTUAL_STOOKLIJN_POWER1: 5000.0,
            CONF_ACTUAL_STOOKLIJN_TEMP2: 5.0,
            CONF_ACTUAL_STOOKLIJN_POWER2: 3000.0,
        }
        slope, intercept = _calc_stooklijn_from_points(config)
        assert slope is None
        assert intercept is None

    def test_empty_config(self):
        """Empty config should return (None, None)."""
        slope, intercept = _calc_stooklijn_from_points({})
        assert slope is None
        assert intercept is None

    def test_zero_slope(self):
        """Same power at different temps → slope = 0."""
        config = {
            CONF_ACTUAL_STOOKLIJN_TEMP1: 0.0,
            CONF_ACTUAL_STOOKLIJN_POWER1: 5000.0,
            CONF_ACTUAL_STOOKLIJN_TEMP2: 10.0,
            CONF_ACTUAL_STOOKLIJN_POWER2: 5000.0,
        }
        slope, intercept = _calc_stooklijn_from_points(config)
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(5000.0)


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

    def test_with_stooklijn_points(self, mock_config_with_points):
        """Data initialized with config points should have slope/intercept."""
        slope, intercept = _calc_stooklijn_from_points(mock_config_with_points)
        data = QuattStooklijnData(
            actual_stooklijn_slope=slope,
            actual_stooklijn_intercept=intercept,
        )

        assert data.actual_stooklijn_slope == pytest.approx(-300.0)
        assert data.actual_stooklijn_intercept == pytest.approx(6500.0)

    def test_status_mutation(self):
        """Status should be mutable (used in service handlers)."""
        data = QuattStooklijnData()
        data.analysis_status = "running"
        assert data.analysis_status == "running"

        data.analysis_status = "completed"
        assert data.analysis_status == "completed"

        data.analysis_status = "error"
        assert data.analysis_status == "error"
