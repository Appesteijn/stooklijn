"""Config flow for Quatt Stooklijn integration."""

from __future__ import annotations

from datetime import date
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_BOILER_HEAT_ENTITY,
    CONF_CONTROL_SETPOINT_ENTITY,
    CONF_COMPRESSOR_2_ENTITY,
    CONF_COMPRESSOR_ENTITY,
    CONF_COP_ENTITY,
    CONF_DEMAND_SHIFT_GAMMA,
    CONF_FLOW_ENTITY,
    CONF_GAS_ENABLED,
    CONF_GAS_ENTITY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_END_DATE,
    CONF_GAS_START_DATE,
    CONF_HOT_WATER_TEMP_THRESHOLD,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_CH_MAX_WATER_ENABLED,
    CONF_CH_MAX_WATER_ENTITY,
    CONF_CH_MAX_WATER_SOURCE,
    CONF_CH_MAX_WATER_HYSTERESIS,
    CONF_CH_MAX_WATER_INTERVAL,
    CONF_COMFORT_FLOOR_TEMP,
    CONF_EOS_THROTTLE_ENTITY,
    CONF_POWER_INPUT_ENTITY,
    CONF_SOUND_LEVEL_ENABLED,
    CONF_SOUND_LEVEL_MAX_DAY,
    CONF_SOUND_LEVEL_MAX_NIGHT,
    CONF_SOUND_NIGHT_START_HOUR,
    CONF_SOUND_NIGHT_END_HOUR,
    CONF_POWER_ENTITY,
    CONF_QUATT_CLOUD_ENABLED,
    CONF_QUATT_START_DATE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_ROOM_SETPOINT_ENTITY,
    CONF_SOLAR_ENTITY,
    CONF_SUPPLY_TEMP_ENTITY,
    CONF_TEMP_ENTITIES,
    CONF_WEATHER_ENTITY,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_CH_MAX_WATER_SOURCE,
    DEFAULT_CH_MAX_WATER_HYSTERESIS,
    DEFAULT_CH_MAX_WATER_INTERVAL,
    DEFAULT_COMFORT_FLOOR_TEMP,
    DEFAULT_EOS_THROTTLE_ENTITY,
    DEFAULT_DEMAND_SHIFT_GAMMA,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_QUATT_CLOUD_ENABLED,
    DEFAULT_HOT_WATER_TEMP_THRESHOLD,
    DEFAULT_SOLAR_ENTITY,
    DEFAULT_SOUND_LEVEL_MAX,
    DEFAULT_SOUND_NIGHT_START_HOUR,
    DEFAULT_SOUND_NIGHT_END_HOUR,
    DEFAULT_WEATHER_ENTITY,
    DOMAIN,
    SOUND_LEVEL_OPTIONS,
)
from .discovery import (
    ROLE_BOILER_HEAT,
    ROLE_CH_MAX_WATER,
    ROLE_CONTROL_SETPOINT,
    ROLE_COMPRESSOR,
    ROLE_COMPRESSOR_2,
    ROLE_COP,
    ROLE_FLOW_RATE,
    ROLE_INDOOR_TEMP,
    ROLE_OUTDOOR_TEMP,
    ROLE_POWER_INPUT,
    ROLE_RETURN_TEMP,
    ROLE_ROOM_SETPOINT,
    ROLE_SUPPLY_TEMP,
    ROLE_TOTAL_POWER,
    async_discover_quatt_entities,
)
from .sources import async_source_entity


def _entity(domain: str | list[str], *, multiple: bool = False) -> EntitySelector:
    """Entity-kiezer in plaats van een vrij tekstveld.

    Entity-IDs verschillen per Quatt-installatie (zie discovery.py), dus laten
    typen leidt tot stille misconfiguratie: een niet-bestaande naam werd zonder
    foutmelding geaccepteerd.
    """
    return EntitySelector(EntitySelectorConfig(domain=domain, multiple=multiple))


def _prefill(key: str, value):
    """Optioneel veld met een voorgestelde waarde (leeg blijven mag).

    Wordt het veld leeggelaten, dan bepaalt de auto-detectie tijdens runtime de
    entity — dat is robuuster dan een default vastleggen die later kan wijzigen.
    """
    if value in (None, "", []):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


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
                temp_entities = user_input.get(CONF_TEMP_ENTITIES) or []
                if isinstance(temp_entities, str):
                    # Terugvalpad voor YAML-import: komma-gescheiden string.
                    temp_entities = [e.strip() for e in temp_entities.split(",") if e.strip()]
                self._data = {
                    CONF_QUATT_START_DATE: user_input[CONF_QUATT_START_DATE],
                    CONF_TEMP_ENTITIES: temp_entities,
                    CONF_POWER_ENTITY: user_input.get(CONF_POWER_ENTITY, ""),
                }
                return await self.async_step_gas()

        detected = async_discover_quatt_entities(self.hass)
        outdoor = detected.get(ROLE_OUTDOOR_TEMP)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_QUATT_START_DATE): str,
                    _prefill(CONF_TEMP_ENTITIES, [outdoor] if outdoor else []): _entity(
                        "sensor", multiple=True
                    ),
                    _prefill(CONF_POWER_ENTITY, detected.get(ROLE_TOTAL_POWER)): _entity(
                        "sensor"
                    ),
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
                    vol.Optional(CONF_GAS_ENTITY): _entity("sensor"),
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
            # Alles overnemen wat in deze stap is ingevuld. Eerder werden alleen
            # de geluidsinstellingen bewaard en verdwenen de entity-velden.
            self._data.update(user_input)
            self._data.setdefault(CONF_SOUND_LEVEL_ENABLED, False)
            self._data.setdefault(CONF_SOUND_LEVEL_MAX_DAY, DEFAULT_SOUND_LEVEL_MAX)
            self._data.setdefault(CONF_SOUND_LEVEL_MAX_NIGHT, DEFAULT_SOUND_LEVEL_MAX)
            self._data.setdefault(
                CONF_SOUND_NIGHT_START_HOUR, DEFAULT_SOUND_NIGHT_START_HOUR
            )
            self._data.setdefault(
                CONF_SOUND_NIGHT_END_HOUR, DEFAULT_SOUND_NIGHT_END_HOUR
            )

            return self.async_create_entry(
                title="Quatt Warmteanalyse",
                data=self._data,
            )

        return self.async_show_form(
            step_id="options",
            data_schema=self._options_schema(async_discover_quatt_entities(self.hass)),
        )

    @staticmethod
    def _options_schema(detected: dict[str, str]):
        """Return schema for options step, voorgevuld met auto-detectie."""
        return vol.Schema(
            {
                _prefill(CONF_FLOW_ENTITY, detected.get(ROLE_FLOW_RATE)): _entity("sensor"),
                _prefill(CONF_RETURN_TEMP_ENTITY, detected.get(ROLE_RETURN_TEMP)): _entity(
                    "sensor"
                ),
                # Aanvoertemperatuur: vergelijkingsbasis voor het stooklijn- en
                # MPC-advies. Stond eerder hardcoded en was dus onbereikbaar voor
                # installaties met een andere Quatt-naamgeving.
                _prefill(CONF_SUPPLY_TEMP_ENTITY, detected.get(ROLE_SUPPLY_TEMP)): _entity(
                    "sensor"
                ),
                # --- MPC / zonnewinst ---
                # Zonnestroom-sensor in Watt. Gebruik bij voorkeur de output van
                # je omvormer (bijv. sensor.solaredge_ac_power). Heb je geen PV,
                # laat dan leeg of gebruik een stralingsensor (W/m² × dakoppervlak).
                _prefill(CONF_SOLAR_ENTITY, DEFAULT_SOLAR_ENTITY): _entity("sensor"),
                # Weersverwachting-entiteit voor het MPC forecast-venster.
                # Standaard weather.home (Open-Meteo via HA weather integratie).
                _prefill(CONF_WEATHER_ENTITY, DEFAULT_WEATHER_ENTITY): _entity("weather"),
                # Kamertemperatuur voor RC-model kalibratie (solar gain learning).
                # Gebruik bij voorkeur een sensor dicht bij een groot zuidraam:
                # die reageert het snelst op zon en geeft het scherpste leersignaal.
                # Elke kamerthermometer werkt; hoe dichter bij de zon, hoe beter.
                _prefill(CONF_INDOOR_TEMP_ENTITY, detected.get(ROLE_INDOOR_TEMP)): _entity(
                    "sensor"
                ),
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
        # Auto-detectie levert de voorgestelde waarde als er nog niets is
        # ingesteld — of als de ingestelde entity niet (meer) bestaat.
        detected = async_discover_quatt_entities(self.hass)

        def _current(key: str, role: str | None = None, fallback=None):
            value = data.get(key)
            if value:
                return value
            # Wat er nú levert gaat voor op wat de Quatt-detectie zou kiezen.
            # Staat een Quatt-sensor op 'unknown', dan is de bronregistratie al
            # doorgeschoven naar OpenQuatt; dán is díe entity het eerlijke
            # voorstel. Zonder deze stap stelt het formulier een dode sensor
            # voor en lijkt het alsof die in gebruik is.
            if role:
                active = async_source_entity(
                    self.hass, self._config_entry.entry_id, role
                )
                if active:
                    return active
            if role and role in detected:
                return detected[role]
            return fallback

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_QUATT_START_DATE,
                        default=data.get(CONF_QUATT_START_DATE, ""),
                    ): str,
                    _prefill(
                        CONF_TEMP_ENTITIES,
                        data.get(CONF_TEMP_ENTITIES)
                        or ([detected[ROLE_OUTDOOR_TEMP]] if ROLE_OUTDOOR_TEMP in detected else []),
                    ): _entity("sensor", multiple=True),
                    _prefill(
                        CONF_POWER_ENTITY, _current(CONF_POWER_ENTITY, ROLE_TOTAL_POWER)
                    ): _entity("sensor"),
                    _prefill(
                        CONF_FLOW_ENTITY, _current(CONF_FLOW_ENTITY, ROLE_FLOW_RATE)
                    ): _entity("sensor"),
                    _prefill(
                        CONF_RETURN_TEMP_ENTITY,
                        _current(CONF_RETURN_TEMP_ENTITY, ROLE_RETURN_TEMP),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_SUPPLY_TEMP_ENTITY,
                        _current(CONF_SUPPLY_TEMP_ENTITY, ROLE_SUPPLY_TEMP),
                    ): _entity("sensor"),
                    # Bronnen voor de recorder-statistieken (COP, gasketel-aandeel).
                    _prefill(
                        CONF_POWER_INPUT_ENTITY,
                        _current(CONF_POWER_INPUT_ENTITY, ROLE_POWER_INPUT),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_BOILER_HEAT_ENTITY,
                        _current(CONF_BOILER_HEAT_ENTITY, ROLE_BOILER_HEAT),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_SOLAR_ENTITY,
                        _current(CONF_SOLAR_ENTITY, fallback=DEFAULT_SOLAR_ENTITY),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_WEATHER_ENTITY,
                        _current(CONF_WEATHER_ENTITY, fallback=DEFAULT_WEATHER_ENTITY),
                    ): _entity("weather"),
                    _prefill(
                        CONF_INDOOR_TEMP_ENTITY,
                        _current(CONF_INDOOR_TEMP_ENTITY, ROLE_INDOOR_TEMP),
                    ): _entity("sensor"),
                    # De resterende gespiegelde metingen. Ze zijn zelden nodig
                    # — auto-detectie vindt ze — maar zonder keuze kan de vaste
                    # volgorde (Quatt vóór OpenQuatt) niet overruled worden.
                    _prefill(
                        CONF_CONTROL_SETPOINT_ENTITY,
                        _current(CONF_CONTROL_SETPOINT_ENTITY, ROLE_CONTROL_SETPOINT),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_ROOM_SETPOINT_ENTITY,
                        _current(CONF_ROOM_SETPOINT_ENTITY, ROLE_ROOM_SETPOINT),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_COP_ENTITY, _current(CONF_COP_ENTITY, ROLE_COP)
                    ): _entity("sensor"),
                    # Compressorfrequentie: hieruit worden de starts geteld.
                    # Twee velden, want een duo wisselt de units om en om af en
                    # start dus onafhankelijk — zie ROLE_COMPRESSOR. Op een solo
                    # blijft het tweede veld leeg.
                    _prefill(
                        CONF_COMPRESSOR_ENTITY,
                        _current(CONF_COMPRESSOR_ENTITY, ROLE_COMPRESSOR),
                    ): _entity("sensor"),
                    _prefill(
                        CONF_COMPRESSOR_2_ENTITY,
                        _current(CONF_COMPRESSOR_2_ENTITY, ROLE_COMPRESSOR_2),
                    ): _entity("sensor"),
                    # Uit = draaien op recorder + eigen stores. De opgebouwde
                    # insights-cache blijft meedoen, hij groeit alleen niet meer.
                    # Schaduw-parameter: 0 laat de verschoven warmtevraag
                    # exact gelijk zijn aan de gewone. Niets is eraan gekoppeld.
                    vol.Optional(
                        CONF_DEMAND_SHIFT_GAMMA,
                        default=data.get(
                            CONF_DEMAND_SHIFT_GAMMA, DEFAULT_DEMAND_SHIFT_GAMMA
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=3.0)),
                    vol.Optional(
                        CONF_QUATT_CLOUD_ENABLED,
                        default=data.get(
                            CONF_QUATT_CLOUD_ENABLED, DEFAULT_QUATT_CLOUD_ENABLED
                        ),
                    ): bool,
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
                    # chMaxWaterTemperatuur bijsturing
                    vol.Optional(
                        CONF_CH_MAX_WATER_ENABLED,
                        default=data.get(CONF_CH_MAX_WATER_ENABLED, False),
                    ): bool,
                    _prefill(
                        CONF_CH_MAX_WATER_ENTITY,
                        _current(CONF_CH_MAX_WATER_ENTITY, ROLE_CH_MAX_WATER),
                    ): _entity("number"),
                    vol.Optional(
                        CONF_CH_MAX_WATER_SOURCE,
                        default=data.get(CONF_CH_MAX_WATER_SOURCE, DEFAULT_CH_MAX_WATER_SOURCE),
                    ): vol.In(["stooklijn", "mpc"]),
                    vol.Optional(
                        CONF_CH_MAX_WATER_HYSTERESIS,
                        default=data.get(CONF_CH_MAX_WATER_HYSTERESIS, DEFAULT_CH_MAX_WATER_HYSTERESIS),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=5.0)),
                    vol.Optional(
                        CONF_CH_MAX_WATER_INTERVAL,
                        default=data.get(CONF_CH_MAX_WATER_INTERVAL, DEFAULT_CH_MAX_WATER_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                    # --- Energy-OS brug ---
                    # Comfort-vloer: laagste acceptabele binnentemperatuur. De
                    # coast-time sensor berekent hoe lang het huis met WP uit kan
                    # uitlopen op zijn thermische massa vóór deze grens (incl. zon).
                    vol.Optional(
                        CONF_COMFORT_FLOOR_TEMP,
                        default=data.get(CONF_COMFORT_FLOOR_TEMP, DEFAULT_COMFORT_FLOOR_TEMP),
                    ): vol.All(vol.Coerce(float), vol.Range(min=10.0, max=22.0)),
                    # Optioneel: entity die aangeeft dat energy-os de WP knijpt
                    # (cap < 20). Die periodes worden uitgesloten van de
                    # COP/warmteverlies-analyse. Leeg = uit (geen filtering).
                    _prefill(
                        CONF_EOS_THROTTLE_ENTITY,
                        data.get(CONF_EOS_THROTTLE_ENTITY, DEFAULT_EOS_THROTTLE_ENTITY),
                    ): _entity(["sensor", "input_number", "number"]),
                }
            ),
        )
