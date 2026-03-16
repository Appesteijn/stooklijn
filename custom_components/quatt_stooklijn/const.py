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

# Supply temperature sensor config
CONF_FLOW_ENTITY = "flow_entity"
CONF_RETURN_TEMP_ENTITY = "return_temp_entity"

DEFAULT_FLOW_ENTITY = "sensor.heatpump_flowmeter_flowrate"
DEFAULT_RETURN_TEMP_ENTITY = "sensor.heatpump_hp1_temperature_water_in"
DEFAULT_SUPPLY_TEMP_ENTITY = "sensor.heatpump_flowmeter_temperature"
MIN_FLOW_LPH = 30  # l/h — below this the pump is not actively circulating

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
OUTLIER_STD_THRESHOLD = 2.5  # Z-score threshold for outlier removal
BIN_SIZE = 0.5  # °C - temperature bin width
KEEP_THRESHOLD = 0.90  # Keep values >= 90% of max in each bin
DAYS_HISTORY = 30  # Days of live history for stooklijn analysis
MIN_HEATING_WATTS = 200  # Minimum W/h to count as a heating day
MIN_MODULATION_WATTS = 2000  # W - minimum Quatt output at lowest compressor step (30Hz, v1.5)

# Service names
SERVICE_RUN_ANALYSIS = "run_analysis"
SERVICE_CLEAR_DATA = "clear_data"

# MPC / shadow-mode forecast sensor
CONF_SOLAR_ENTITY = "solar_entity"
CONF_WEATHER_ENTITY = "weather_entity"

# Kamertemperatuur voor RC-regressie (solar gain learning).
# Gebruik bij voorkeur een sensor dicht bij een groot zuidraam: die reageert
# het snelst op zoninstraling en geeft het scherpste leerssignaal.
# Elke kamerthermometer werkt, maar hoe dichter bij de zon, hoe beter.
CONF_INDOOR_TEMP_ENTITY = "indoor_temp_entity"

DEFAULT_SOLAR_ENTITY = "sensor.solaredge_ac_power"
DEFAULT_WEATHER_ENTITY = "weather.home"
# Meest representatieve kamerthermometer in Quatt-setups; pas aan naar jouw sensor.
DEFAULT_INDOOR_TEMP_ENTITY = "sensor.heatpump_thermostat_room_temperature"

# Raamfactor: verhouding PV-opbrengst (W) → zoninstraling woonkamer (W)
# Empirisch: SolarEdge 2000 W ≈ ~600 W netto zonnewinst via zuidgevel-ramen
#
# Dit is een fallback. De voorkeur is om deze factor te leren via RC-regressie
# op de recorder-data (zie analysis/solar_gain.py als dat geïmplementeerd is):
#
#   C × dT_room/dt = Q_hp + factor × solaredge − U × (T_room − T_buiten)
#
# Herschreven als 2-parameter OLS:
#   dT/dt = α × [Q_hp − U × (T_room − T_buiten)] + β × solaredge
#   → factor = β / α,  thermische massa C = 1 / α
#
# Als de regressie beschikbaar is (QuattStooklijnData.solar_gain_factor is not None)
# gebruikt QuattMpcSensor die waarde; anders valt hij terug op deze constante.
SOLAR_TO_HEAT_FACTOR = 0.30

# Veiligheidsgrenzen aanvoertemperatuur MPC-sensor
MPC_SUPPLY_TEMP_MIN = 25.0  # °C
MPC_SUPPLY_TEMP_MAX = 55.0  # °C

# Hoeveel forecast-uren meenemen in het MPC-attribuut
MPC_FORECAST_HOURS = 6

# Open-Meteo URL template — wordt ingevuld met lat/lon uit HA config
OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&hourly=shortwave_radiation,cloud_cover"
    "&forecast_days=2&timezone=Europe%2FAmsterdam"
)

# Standaard omrekeningsfactor shortwave_radiation (W/m²) → warmtewinst (W).
# Wordt dynamisch gekalibreerd als solaredge_ac_power beschikbaar is.
# Formule: Q_zon = shortwave_radiation × factor
# Typische waarde ≈ effectief raamoppervlak (m²) × transmissie × absorptie (~8–12 m² netto)
SOLAR_RADIATION_DEFAULT_FACTOR = 8.0  # W per W/m²

# Zonproductie-fractie per HA weather condition (proxy voor shortwave_radiation).
# Waarde × huidige solaredge_ac_power = geschatte zonproductie dat uur.
# Bron: HA weather condition strings (https://www.home-assistant.io/integrations/weather/)
CONDITION_SOLAR_FRACTION: dict[str, float] = {
    "clear-night":       0.0,
    "cloudy":            0.05,
    "exceptional":       0.3,
    "fog":               0.05,
    "hail":              0.0,
    "lightning":         0.0,
    "lightning-rainy":   0.0,
    "partlycloudy":      0.45,
    "pouring":           0.0,
    "rainy":             0.05,
    "snowy":             0.05,
    "snowy-rainy":       0.0,
    "sunny":             1.0,
    "windy":             0.7,
    "windy-variant":     0.5,
}
