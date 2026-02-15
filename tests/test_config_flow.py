"""Tests for config flow validation logic.

Since the actual HA config flow requires the full HA test harness,
we test the validation logic extracted from the flow steps.
"""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.quatt_stooklijn.const import (
    CONF_ACTUAL_STOOKLIJN_TEMP1,
    CONF_ACTUAL_STOOKLIJN_POWER1,
    CONF_ACTUAL_STOOKLIJN_TEMP2,
    CONF_ACTUAL_STOOKLIJN_POWER2,
    CONF_QUATT_START_DATE,
    CONF_QUATT_END_DATE,
    CONF_TEMP_ENTITIES,
    CONF_POWER_ENTITY,
    CONF_GAS_ENABLED,
    CONF_GAS_ENTITY,
    CONF_GAS_START_DATE,
    CONF_GAS_END_DATE,
)


class TestDateValidation:
    """Test date validation logic used in config flow steps."""

    @staticmethod
    def validate_dates(start_str: str, end_str: str) -> str | None:
        """Reproduce date validation from async_step_user / async_step_gas."""
        try:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            if start >= end:
                return "invalid_date_range"
        except ValueError:
            return "invalid_date_format"
        return None

    def test_valid_dates(self):
        assert self.validate_dates("2024-01-01", "2024-06-30") is None

    def test_end_before_start(self):
        assert self.validate_dates("2024-06-30", "2024-01-01") == "invalid_date_range"

    def test_same_dates(self):
        assert self.validate_dates("2024-01-01", "2024-01-01") == "invalid_date_range"

    def test_invalid_format(self):
        assert self.validate_dates("not-a-date", "2024-01-01") == "invalid_date_format"

    def test_invalid_end_format(self):
        assert self.validate_dates("2024-01-01", "01/01/2024") == "invalid_date_format"

    def test_empty_strings(self):
        assert self.validate_dates("", "") == "invalid_date_format"


class TestTempEntityParsing:
    """Test comma-separated entity parsing."""

    @staticmethod
    def parse_temp_entities(raw: str) -> list[str]:
        """Reproduce parsing from async_step_user."""
        return [e.strip() for e in raw.split(",") if e.strip()]

    def test_single_entity(self):
        result = self.parse_temp_entities("sensor.temp_outside")
        assert result == ["sensor.temp_outside"]

    def test_multiple_entities(self):
        result = self.parse_temp_entities(
            "sensor.hp1_temp, sensor.hp2_temp, sensor.thermostat_temp"
        )
        assert result == ["sensor.hp1_temp", "sensor.hp2_temp", "sensor.thermostat_temp"]

    def test_extra_whitespace(self):
        result = self.parse_temp_entities("  sensor.a ,  sensor.b  ")
        assert result == ["sensor.a", "sensor.b"]

    def test_empty_string(self):
        result = self.parse_temp_entities("")
        assert result == []

    def test_trailing_comma(self):
        result = self.parse_temp_entities("sensor.a, sensor.b,")
        assert result == ["sensor.a", "sensor.b"]


class TestStooklijnPointsValidation:
    """Test stooklijn points validation (all 4 or none)."""

    @staticmethod
    def validate_stooklijn_points(user_input: dict) -> tuple[dict | None, str | None]:
        """Reproduce validation from async_step_options.

        Returns (parsed_points, error) where parsed_points is a dict of
        float values if all 4 are filled, None if all empty, and error
        is set if values are invalid.
        """
        fields = [
            (CONF_ACTUAL_STOOKLIJN_TEMP1, user_input.get(CONF_ACTUAL_STOOKLIJN_TEMP1, "")),
            (CONF_ACTUAL_STOOKLIJN_POWER1, user_input.get(CONF_ACTUAL_STOOKLIJN_POWER1, "")),
            (CONF_ACTUAL_STOOKLIJN_TEMP2, user_input.get(CONF_ACTUAL_STOOKLIJN_TEMP2, "")),
            (CONF_ACTUAL_STOOKLIJN_POWER2, user_input.get(CONF_ACTUAL_STOOKLIJN_POWER2, "")),
        ]
        filled = [(k, v) for k, v in fields if str(v).strip()]

        if len(filled) == 0:
            return None, None

        if len(filled) == 4:
            try:
                return {k: float(v) for k, v in filled}, None
            except ValueError:
                return None, "invalid_stooklijn_value"

        # Partial fill — the actual flow doesn't error here, it just ignores
        return None, None

    def test_all_four_filled(self):
        points, error = self.validate_stooklijn_points({
            CONF_ACTUAL_STOOKLIJN_TEMP1: "-5",
            CONF_ACTUAL_STOOKLIJN_POWER1: "8000",
            CONF_ACTUAL_STOOKLIJN_TEMP2: "15",
            CONF_ACTUAL_STOOKLIJN_POWER2: "2000",
        })
        assert error is None
        assert points[CONF_ACTUAL_STOOKLIJN_TEMP1] == -5.0
        assert points[CONF_ACTUAL_STOOKLIJN_POWER2] == 2000.0

    def test_all_empty(self):
        points, error = self.validate_stooklijn_points({})
        assert error is None
        assert points is None

    def test_invalid_float(self):
        points, error = self.validate_stooklijn_points({
            CONF_ACTUAL_STOOKLIJN_TEMP1: "abc",
            CONF_ACTUAL_STOOKLIJN_POWER1: "8000",
            CONF_ACTUAL_STOOKLIJN_TEMP2: "15",
            CONF_ACTUAL_STOOKLIJN_POWER2: "2000",
        })
        assert error == "invalid_stooklijn_value"

    def test_partial_fill(self):
        """Partial fill (< 4) should be silently ignored."""
        points, error = self.validate_stooklijn_points({
            CONF_ACTUAL_STOOKLIJN_TEMP1: "-5",
            CONF_ACTUAL_STOOKLIJN_POWER1: "8000",
        })
        assert error is None
        assert points is None


class TestGasStepValidation:
    """Test gas step validation."""

    @staticmethod
    def validate_gas_input(user_input: dict) -> str | None:
        """Reproduce gas validation from async_step_gas."""
        gas_enabled = user_input.get(CONF_GAS_ENABLED, False)
        if not gas_enabled:
            return None

        gas_entity = user_input.get(CONF_GAS_ENTITY, "")
        if not gas_entity:
            return "gas_entity_required"

        gas_start = user_input.get(CONF_GAS_START_DATE, "")
        gas_end = user_input.get(CONF_GAS_END_DATE, "")

        try:
            s = date.fromisoformat(gas_start)
            e = date.fromisoformat(gas_end)
            if s >= e:
                return "invalid_date_range"
        except ValueError:
            return "invalid_date_format"

        return None

    def test_gas_disabled(self):
        assert self.validate_gas_input({CONF_GAS_ENABLED: False}) is None

    def test_gas_enabled_valid(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "sensor.gas_meter",
            CONF_GAS_START_DATE: "2023-01-01",
            CONF_GAS_END_DATE: "2023-12-31",
        })
        assert result is None

    def test_gas_enabled_missing_entity(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "",
            CONF_GAS_START_DATE: "2023-01-01",
            CONF_GAS_END_DATE: "2023-12-31",
        })
        assert result == "gas_entity_required"

    def test_gas_enabled_bad_dates(self):
        result = self.validate_gas_input({
            CONF_GAS_ENABLED: True,
            CONF_GAS_ENTITY: "sensor.gas_meter",
            CONF_GAS_START_DATE: "2023-12-31",
            CONF_GAS_END_DATE: "2023-01-01",
        })
        assert result == "invalid_date_range"
