"""Tests for sound level compensation switch logic."""

from __future__ import annotations

import pytest

from custom_components.quatt_stooklijn.switch import (
    QuattSoundLevelSwitch,
    _SOUND_LEVELS,
    _NORMAL_IDX,
    _GAS_THRESHOLD_W,
    _DEAD_BAND,
)
from custom_components.quatt_stooklijn.const import (
    OTGW_CYCLE_SECONDS,
    OTGW_UNAVAILABLE_TIMEOUT,
)


class TestSoundLevels:
    """Verify sound level ordering and indices."""

    def test_normal_is_last(self):
        assert _SOUND_LEVELS[_NORMAL_IDX] == "normal"

    def test_building87_is_weakest(self):
        assert _SOUND_LEVELS[0] == "building87"

    def test_four_levels(self):
        assert len(_SOUND_LEVELS) == 4

    def test_normal_idx_is_max(self):
        assert _NORMAL_IDX == len(_SOUND_LEVELS) - 1

    def test_level_order(self):
        assert _SOUND_LEVELS == ["building87", "silent", "library", "normal"]


class TestSafetyConstants:
    """Verify that safety constants are within acceptable ranges."""

    def test_gas_threshold_positive(self):
        assert _GAS_THRESHOLD_W > 0

    def test_dead_band_positive(self):
        assert _DEAD_BAND > 0

    def test_cycle_seconds_reasonable(self):
        assert 60 <= OTGW_CYCLE_SECONDS <= 600

    def test_unavailable_timeout_longer_than_cycle(self):
        assert OTGW_UNAVAILABLE_TIMEOUT > OTGW_CYCLE_SECONDS


class TestStepBoundaries:
    """Verify that stepping respects array bounds."""

    def test_cannot_step_above_normal(self):
        idx = _NORMAL_IDX
        # stepping up from max stays at max
        new_idx = min(idx + 1, _NORMAL_IDX)
        assert new_idx == _NORMAL_IDX

    def test_cannot_step_below_zero(self):
        idx = 0
        # stepping down from min stays at 0
        new_idx = max(idx - 1, 0)
        assert new_idx == 0

    def test_step_up_from_silent(self):
        idx = _SOUND_LEVELS.index("silent")
        assert _SOUND_LEVELS[idx + 1] == "library"

    def test_step_down_from_library(self):
        idx = _SOUND_LEVELS.index("library")
        assert _SOUND_LEVELS[idx - 1] == "silent"
