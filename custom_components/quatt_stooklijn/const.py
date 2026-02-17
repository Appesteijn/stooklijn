"""Constants for the Quatt Stooklijn integration."""

DOMAIN = "quatt_stooklijn"

# Config keys
CONF_QUATT_START_DATE = "quatt_start_date"
CONF_QUATT_END_DATE = "quatt_end_date"
CONF_TEMP_ENTITIES = "temp_entities"
CONF_POWER_ENTITY = "power_entity"
CONF_GAS_ENABLED = "gas_enabled"
CONF_GAS_ENTITY = "gas_entity"
CONF_GAS_START_DATE = "gas_start_date"
CONF_GAS_END_DATE = "gas_end_date"
CONF_GAS_CALORIFIC_VALUE = "gas_calorific_value"
CONF_BOILER_EFFICIENCY = "boiler_efficiency"
CONF_HOT_WATER_TEMP_THRESHOLD = "hot_water_temp_threshold"
CONF_ACTUAL_STOOKLIJN_TEMP1 = "actual_stooklijn_temp1"
CONF_ACTUAL_STOOKLIJN_POWER1 = "actual_stooklijn_power1"
CONF_ACTUAL_STOOKLIJN_TEMP2 = "actual_stooklijn_temp2"
CONF_ACTUAL_STOOKLIJN_POWER2 = "actual_stooklijn_power2"

# Default values
DEFAULT_GAS_CALORIFIC_VALUE = 9.77  # kWh/m³ (Dutch gas)
DEFAULT_BOILER_EFFICIENCY = 0.90  # 90%
DEFAULT_HOT_WATER_TEMP_THRESHOLD = 18.0  # °C

# Default temperature entities (in priority order)
DEFAULT_TEMP_ENTITIES = [
    "sensor.heatpump_hp1_temperature_outside",
    "sensor.heatpump_hp2_temperature_outside",
    "sensor.thermostat_temperature_outside",
]
DEFAULT_POWER_ENTITY = "sensor.heatpump_total_power"

# Recorder statistics sensors (derived from Quatt integration)
RECORDER_POWER_INPUT_ENTITY = "sensor.heatpump_total_power_input"
RECORDER_COP_ENTITY = "sensor.heatpump_total_quatt_cop"
RECORDER_BOILER_HEAT_ENTITY = "sensor.heatpump_boiler_heat_power"

# How many days of detailed hourly data to fetch from Quatt API
API_FETCH_DAYS = 30

# Analysis parameters
MIN_POWER_FILTER = 2500  # W - minimum power to consider heat pump active
STD_DEV_THRESHOLD = 2.0  # Z-score threshold for outlier removal
DEFROST_THRESHOLD = 0  # °C - below this, defrost cycles may occur
BIN_SIZE = 0.5  # °C - temperature bin width
KEEP_THRESHOLD = 0.90  # Keep values >= 90% of max in each bin
DAYS_HISTORY = 10  # Days of live history for stooklijn analysis

# Service names
SERVICE_RUN_ANALYSIS = "run_analysis"
SERVICE_CLEAR_DATA = "clear_data"
