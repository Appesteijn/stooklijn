"""Config flow for Quatt Stooklijn integration."""

from __future__ import annotations

from datetime import date
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ACTUAL_STOOKLIJN_POWER1,
    CONF_ACTUAL_STOOKLIJN_POWER2,
    CONF_ACTUAL_STOOKLIJN_TEMP1,
    CONF_ACTUAL_STOOKLIJN_TEMP2,
    CONF_BOILER_EFFICIENCY,
    CONF_FLOW_ENTITY,
    CONF_GAS_ENABLED,
    CONF_GAS_ENTITY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_END_DATE,
    CONF_GAS_START_DATE,
    CONF_HOT_WATER_TEMP_THRESHOLD,
    CONF_POWER_ENTITY,
    CONF_QUATT_END_DATE,
    CONF_QUATT_START_DATE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HOT_WATER_TEMP_THRESHOLD,
    DEFAULT_POWER_ENTITY,
    DEFAULT_RETURN_TEMP_ENTITY,
    DEFAULT_TEMP_ENTITIES,
    DOMAIN,
)


class QuattStooklijnConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Quatt Stooklijn."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict = {}

    async def async_step_user(self, user_input=None):
        """Step 1: Quatt heat pump configuration."""
        errors = {}

        if user_input is not None:
            # Validate dates
            try:
                start = date.fromisoformat(user_input[CONF_QUATT_START_DATE])
                end = date.fromisoformat(user_input[CONF_QUATT_END_DATE])
                if start >= end:
                    errors["base"] = "invalid_date_range"
            except ValueError:
                errors["base"] = "invalid_date_format"

            if not errors:
                # Parse comma-separated temp entities
                temp_str = user_input.get(CONF_TEMP_ENTITIES, "")
                temp_entities = [
                    e.strip() for e in temp_str.split(",") if e.strip()
                ]
                self._data = {
                    CONF_QUATT_START_DATE: user_input[CONF_QUATT_START_DATE],
                    CONF_QUATT_END_DATE: user_input[CONF_QUATT_END_DATE],
                    CONF_TEMP_ENTITIES: temp_entities,
                    CONF_POWER_ENTITY: user_input[CONF_POWER_ENTITY],
                }
                return await self.async_step_gas()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_QUATT_START_DATE): str,
                    vol.Required(CONF_QUATT_END_DATE): str,
                    vol.Required(
                        CONF_TEMP_ENTITIES,
                        default=", ".join(DEFAULT_TEMP_ENTITIES),
                    ): str,
                    vol.Required(
                        CONF_POWER_ENTITY,
                        default=DEFAULT_POWER_ENTITY,
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_gas(self, user_input=None):
        """Step 2: Gas analysis configuration (optional)."""
        errors = {}

        if user_input is not None:
            gas_enabled = user_input.get(CONF_GAS_ENABLED, False)
            self._data[CONF_GAS_ENABLED] = gas_enabled

            if gas_enabled:
                # Validate gas fields
                gas_entity = user_input.get(CONF_GAS_ENTITY, "")
                gas_start = user_input.get(CONF_GAS_START_DATE, "")
                gas_end = user_input.get(CONF_GAS_END_DATE, "")

                if not gas_entity:
                    errors["base"] = "gas_entity_required"
                else:
                    try:
                        s = date.fromisoformat(gas_start)
                        e = date.fromisoformat(gas_end)
                        if s >= e:
                            errors["base"] = "invalid_date_range"
                    except ValueError:
                        errors["base"] = "invalid_date_format"

                if not errors:
                    self._data[CONF_GAS_ENTITY] = gas_entity
                    self._data[CONF_GAS_START_DATE] = gas_start
                    self._data[CONF_GAS_END_DATE] = gas_end
                    self._data[CONF_GAS_CALORIFIC_VALUE] = user_input.get(
                        CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE
                    )
                    self._data[CONF_BOILER_EFFICIENCY] = user_input.get(
                        CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY
                    )
                    self._data[CONF_HOT_WATER_TEMP_THRESHOLD] = user_input.get(
                        CONF_HOT_WATER_TEMP_THRESHOLD,
                        DEFAULT_HOT_WATER_TEMP_THRESHOLD,
                    )
                    return await self.async_step_options()
            else:
                return await self.async_step_options()

        return self.async_show_form(
            step_id="gas",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GAS_ENABLED, default=False): bool,
                    vol.Optional(CONF_GAS_ENTITY): str,
                    vol.Optional(CONF_GAS_START_DATE): str,
                    vol.Optional(CONF_GAS_END_DATE): str,
                    vol.Optional(
                        CONF_GAS_CALORIFIC_VALUE,
                        default=DEFAULT_GAS_CALORIFIC_VALUE,
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_BOILER_EFFICIENCY,
                        default=DEFAULT_BOILER_EFFICIENCY,
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_HOT_WATER_TEMP_THRESHOLD,
                        default=DEFAULT_HOT_WATER_TEMP_THRESHOLD,
                    ): vol.Coerce(float),
                }
            ),
            errors=errors,
        )

    async def async_step_options(self, user_input=None):
        """Step 3: Optional settings for stooklijn comparison."""
        if user_input is not None:
            # Store actual stooklijn points (optional, all 4 must be filled)
            stooklijn_fields = [
                (CONF_ACTUAL_STOOKLIJN_TEMP1, user_input.get(CONF_ACTUAL_STOOKLIJN_TEMP1, "")),
                (CONF_ACTUAL_STOOKLIJN_POWER1, user_input.get(CONF_ACTUAL_STOOKLIJN_POWER1, "")),
                (CONF_ACTUAL_STOOKLIJN_TEMP2, user_input.get(CONF_ACTUAL_STOOKLIJN_TEMP2, "")),
                (CONF_ACTUAL_STOOKLIJN_POWER2, user_input.get(CONF_ACTUAL_STOOKLIJN_POWER2, "")),
            ]
            filled = [(k, v) for k, v in stooklijn_fields if str(v).strip()]
            if len(filled) == 4:
                try:
                    for key, val in filled:
                        self._data[key] = float(val)
                except ValueError:
                    return self.async_show_form(
                        step_id="options",
                        data_schema=self._options_schema(),
                        errors={"base": "invalid_stooklijn_value"},
                    )

            return self.async_create_entry(
                title="Quatt Warmteanalyse",
                data=self._data,
            )

        return self.async_show_form(
            step_id="options",
            data_schema=self._options_schema(),
        )

    @staticmethod
    def _options_schema():
        """Return schema for options step."""
        return vol.Schema(
            {
                vol.Optional(CONF_ACTUAL_STOOKLIJN_TEMP1, default=""): str,
                vol.Optional(CONF_ACTUAL_STOOKLIJN_POWER1, default=""): str,
                vol.Optional(CONF_ACTUAL_STOOKLIJN_TEMP2, default=""): str,
                vol.Optional(CONF_ACTUAL_STOOKLIJN_POWER2, default=""): str,
                vol.Optional(
                    CONF_FLOW_ENTITY,
                    default=DEFAULT_FLOW_ENTITY,
                ): str,
                vol.Optional(
                    CONF_RETURN_TEMP_ENTITY,
                    default=DEFAULT_RETURN_TEMP_ENTITY,
                ): str,
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow."""
        return QuattStooklijnOptionsFlow(config_entry)


class QuattStooklijnOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for reconfiguration."""

    def __init__(self, config_entry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            # Parse optional stooklijn point strings to float
            result = dict(user_input)
            for key in (
                CONF_ACTUAL_STOOKLIJN_TEMP1,
                CONF_ACTUAL_STOOKLIJN_POWER1,
                CONF_ACTUAL_STOOKLIJN_TEMP2,
                CONF_ACTUAL_STOOKLIJN_POWER2,
            ):
                val = str(result.get(key, "")).strip()
                if val:
                    result[key] = float(val)
                else:
                    result.pop(key, None)
            return self.async_create_entry(title="", data=result)

        data = self._config_entry.data

        def _float_default(key):
            val = data.get(key)
            return str(val) if val is not None else ""

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_QUATT_START_DATE,
                        default=data.get(CONF_QUATT_START_DATE, ""),
                    ): str,
                    vol.Required(
                        CONF_QUATT_END_DATE,
                        default=data.get(CONF_QUATT_END_DATE, ""),
                    ): str,
                    vol.Optional(
                        CONF_ACTUAL_STOOKLIJN_TEMP1,
                        default=_float_default(CONF_ACTUAL_STOOKLIJN_TEMP1),
                    ): str,
                    vol.Optional(
                        CONF_ACTUAL_STOOKLIJN_POWER1,
                        default=_float_default(CONF_ACTUAL_STOOKLIJN_POWER1),
                    ): str,
                    vol.Optional(
                        CONF_ACTUAL_STOOKLIJN_TEMP2,
                        default=_float_default(CONF_ACTUAL_STOOKLIJN_TEMP2),
                    ): str,
                    vol.Optional(
                        CONF_ACTUAL_STOOKLIJN_POWER2,
                        default=_float_default(CONF_ACTUAL_STOOKLIJN_POWER2),
                    ): str,
                    vol.Optional(
                        CONF_FLOW_ENTITY,
                        default=data.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_RETURN_TEMP_ENTITY,
                        default=data.get(CONF_RETURN_TEMP_ENTITY, DEFAULT_RETURN_TEMP_ENTITY),
                    ): str,
                }
            ),
        )
