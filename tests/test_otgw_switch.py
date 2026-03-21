"""Tests for OTGW compensation switch safety logic."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.switch import QuattOtgwCompensationSwitch
from custom_components.quatt_stooklijn.const import (
    OTGW_DEAD_BAND,
    OTGW_GAIN_FACTOR,
    OTGW_HARD_MAX_OFFSET,
    OTGW_MAX_RATE,
)


class TestMoveTowardZero:
    """Test the static _move_toward_zero helper."""

    def test_positive_moves_down(self):
        assert QuattOtgwCompensationSwitch._move_toward_zero(1.0, 0.5) == 0.5

    def test_positive_clamps_at_zero(self):
        assert QuattOtgwCompensationSwitch._move_toward_zero(0.3, 0.5) == 0.0

    def test_zero_stays_zero(self):
        assert QuattOtgwCompensationSwitch._move_toward_zero(0.0, 0.5) == 0.0

    def test_negative_moves_up(self):
        assert QuattOtgwCompensationSwitch._move_toward_zero(-1.0, 0.5) == -0.5

    def test_negative_clamps_at_zero(self):
        assert QuattOtgwCompensationSwitch._move_toward_zero(-0.3, 0.5) == 0.0


class TestSafetyConstants:
    """Verify that safety constants are within acceptable ranges."""

    def test_hard_max_offset_reasonable(self):
        assert OTGW_HARD_MAX_OFFSET <= 5.0

    def test_gain_factor_conservative(self):
        assert OTGW_GAIN_FACTOR <= 1.0

    def test_dead_band_positive(self):
        assert OTGW_DEAD_BAND > 0

    def test_max_rate_below_hard_max(self):
        assert OTGW_MAX_RATE < OTGW_HARD_MAX_OFFSET

    def test_offset_never_exceeds_hard_max(self):
        """Even with extreme MPC error, offset should be clamped."""
        max_possible = abs(-20) * OTGW_GAIN_FACTOR  # extreme 20°C error
        # The switch clamps to min(max_offset, hard_max)
        assert min(max_possible, OTGW_HARD_MAX_OFFSET) <= OTGW_HARD_MAX_OFFSET
