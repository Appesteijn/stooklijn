"""Config flow for Quatt Stooklijn integration."""

from __future__ import annotations

from datetime import date
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_FLOW_ENTITY,
    CONF_GAS_ENABLED,
    CONF_GAS_ENTITY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_END_DATE,
    CONF_GAS_START_DATE,
    CONF_HOT_WATER_TEMP_THRESHOLD,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_MAX_DAY,
    CONF_SOUND_LEVEL_MAX_NIGHT,
    CONF_SOUND_NIGHT_START_HOUR,
    CONF_SOUND_NIGHT_END_HOUR,
    CONF_POWER_ENTITY,
    CONF_QUATT_START_DATE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_FLOW_ENTITY,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HOT_WATER_TEMP_THRESHOLD,
    DEFAULT_INDOOR_TEMP_ENTITY,
    DEFAULT_POWER_ENTITY,
    DEFAULT_SOUND_LEVEL_MAX,
    DEFAULT_SOUND_NIGHT_START_HOUR,
    DEFAULT_SOUND_NIGHT_END_HOUR,
    DEFAULT_RETURN_TEMP_ENTITY,
    DEFAULT_SOLAR_ENTITY,
    DEFAULT_TEMP_ENTITIES,
    DEFAULT_WEATHER_ENTITY,
    DOMAIN,
    SOUND_LEVEL_OPTIONS,
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
                date.fromisoformat(user_input[CONF_QUATT_START_DATE])
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
                    CONF_TEMP_ENTITIES: temp_entities,
                    CONF_POWER_ENTITY: user_input[CONF_POWER_ENTITY],
                }
                return await self.async_step_gas()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_QUATT_START_DATE): str,
                    vol.Required(
                        CONF_TEMP_ENTITIES,
                        default="sensor.thermostat_temperature_outside, sensor.heatpump_hp1_temperature_outside, sensor.heatpump_hp2_temperature_outside",
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
            self._data[CONF_SOUND_LEVEL_ENABLED] = user_input.get(CONF_SOUND_LEVEL_ENABLED, False)
            self._data[CONF_SOUND_LEVEL_MAX_DAY] = user_input.get(CONF_SOUND_LEVEL_MAX_DAY, DEFAULT_SOUND_LEVEL_MAX)
            self._data[CONF_SOUND_LEVEL_MAX_NIGHT] = user_input.get(CONF_SOUND_LEVEL_MAX_NIGHT, DEFAULT_SOUND_LEVEL_MAX)
            self._data[CONF_SOUND_NIGHT_START_HOUR] = user_input.get(CONF_SOUND_NIGHT_START_HOUR, DEFAULT_SOUND_NIGHT_START_HOUR)
            self._data[CONF_SOUND_NIGHT_END_HOUR] = user_input.get(CONF_SOUND_NIGHT_END_HOUR, DEFAULT_SOUND_NIGHT_END_HOUR)

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
                vol.Optional(
                    CONF_FLOW_ENTITY,
                    default=DEFAULT_FLOW_ENTITY,
                ): str,
                vol.Optional(
                    CONF_RETURN_TEMP_ENTITY,
                    default=DEFAULT_RETURN_TEMP_ENTITY,
                ): str,
                # --- MPC / zonnewinst ---
                # Zonnestroom-sensor in Watt. Gebruik bij voorkeur de output van
                # je omvormer (bijv. sensor.solaredge_ac_power). Heb je geen PV,
                # laat dan leeg of gebruik een stralingsensor (W/m² × dakoppervlak).
                vol.Optional(
                    CONF_SOLAR_ENTITY,
                    default=DEFAULT_SOLAR_ENTITY,
                ): str,
                # Weersverwachting-entiteit voor het MPC forecast-venster.
                # Standaard weather.home (Open-Meteo via HA weather integratie).
                vol.Optional(
                    CONF_WEATHER_ENTITY,
                    default=DEFAULT_WEATHER_ENTITY,
                ): str,
                # Kamertemperatuur voor RC-model kalibratie (solar gain learning).
                # Gebruik bij voorkeur een sensor dicht bij een groot zuidraam:
                # die reageert het snelst op zon en geeft het scherpste leersignaal.
                # Elke kamerthermometer werkt; hoe dichter bij de zon, hoe beter.
                vol.Optional(
                    CONF_INDOOR_TEMP_ENTITY,
                    default=DEFAULT_INDOOR_TEMP_ENTITY,
                ): str,
                # --- Geluidsniveau compensatie ---
                # Schakel in om de warmtepomp actief bij te sturen via
                # select.cic_day_max_sound_level en select.cic_night_max_sound_level.
                vol.Optional(
                    CONF_SOUND_LEVEL_ENABLED,
                    default=False,
                ): bool,
                # Maximaal geluidsniveau dat de compensatie mag instellen.
                # Voorkomt dat de HP te hard gaat draaien (bijv. 's nachts).
                vol.Optional(
                    CONF_SOUND_LEVEL_MAX_DAY,
                    default=DEFAULT_SOUND_LEVEL_MAX,
                ): vol.In(SOUND_LEVEL_OPTIONS),
                vol.Optional(
                    CONF_SOUND_LEVEL_MAX_NIGHT,
                    default=DEFAULT_SOUND_LEVEL_MAX,
                ): vol.In(SOUND_LEVEL_OPTIONS),
                # Nachtvenster — HA-lokale tijd (CET/CEST), onafhankelijk van Quatt-sensoren.
                # Vul het uur in waarop de nacht begint resp. eindigt (0–23).
                vol.Optional(
                    CONF_SOUND_NIGHT_START_HOUR,
                    default=DEFAULT_SOUND_NIGHT_START_HOUR,
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Optional(
                    CONF_SOUND_NIGHT_END_HOUR,
                    default=DEFAULT_SOUND_NIGHT_END_HOUR,
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
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
            result = dict(user_input)
            return self.async_create_entry(title="", data=result)

        data = {**self._config_entry.data, **self._config_entry.options}

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
                    vol.Optional(
                        CONF_FLOW_ENTITY,
                        default=data.get(CONF_FLOW_ENTITY, DEFAULT_FLOW_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_RETURN_TEMP_ENTITY,
                        default=data.get(CONF_RETURN_TEMP_ENTITY, DEFAULT_RETURN_TEMP_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_SOLAR_ENTITY,
                        default=data.get(CONF_SOLAR_ENTITY, DEFAULT_SOLAR_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_WEATHER_ENTITY,
                        default=data.get(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_INDOOR_TEMP_ENTITY,
                        default=data.get(CONF_INDOOR_TEMP_ENTITY, DEFAULT_INDOOR_TEMP_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_SOUND_LEVEL_ENABLED,
                        default=data.get(CONF_SOUND_LEVEL_ENABLED, False),
                    ): bool,
                    vol.Optional(
                        CONF_SOUND_LEVEL_MAX_DAY,
                        default=data.get(CONF_SOUND_LEVEL_MAX_DAY, DEFAULT_SOUND_LEVEL_MAX),
                    ): vol.In(SOUND_LEVEL_OPTIONS),
                    vol.Optional(
                        CONF_SOUND_LEVEL_MAX_NIGHT,
                        default=data.get(CONF_SOUND_LEVEL_MAX_NIGHT, DEFAULT_SOUND_LEVEL_MAX),
                    ): vol.In(SOUND_LEVEL_OPTIONS),
                    vol.Optional(
                        CONF_SOUND_NIGHT_START_HOUR,
                        default=data.get(CONF_SOUND_NIGHT_START_HOUR, DEFAULT_SOUND_NIGHT_START_HOUR),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                    vol.Optional(
                        CONF_SOUND_NIGHT_END_HOUR,
                        default=data.get(CONF_SOUND_NIGHT_END_HOUR, DEFAULT_SOUND_NIGHT_END_HOUR),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                }
            ),
        )
