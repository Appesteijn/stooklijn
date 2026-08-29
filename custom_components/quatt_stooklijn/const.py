"""Constants for the Quatt Stooklijn integration."""

from datetime import timedelta

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

# Default values
DEFAULT_GAS_CALORIFIC_VALUE = 9.77  # kWh/m³ (Dutch gas)
DEFAULT_BOILER_EFFICIENCY = 0.90  # 90%
DEFAULT_HOT_WATER_TEMP_THRESHOLD = 18.0  # °C

# Supply temperature sensor config
CONF_FLOW_ENTITY = "flow_entity"
CONF_RETURN_TEMP_ENTITY = "return_temp_entity"
CONF_SUPPLY_TEMP_ENTITY = "supply_temp_entity"

# NB: hier staan bewust géén standaard entity-IDs meer. De Quatt-integratie
# gebruikt per installatie een andere naamgeving (zie de v2→v3 device-migratie,
# uitgelegd in discovery.py), dus een vaste default werkt maar voor een deel van
# de gebruikers. De entity-ID wordt bepaald door discovery.async_resolve_entity();
# terugvalnamen staan in discovery.FALLBACK_ENTITIES — één bron van waarheid.
MIN_FLOW_LPH = 30   # l/h — below this the pump is not actively circulating
NOMINAL_FLOW_LPH = 800  # l/h — fallback when HP is off, for theoretical supply temp

# Recorder statistics sensors (derived from Quatt integration)
CONF_POWER_INPUT_ENTITY = "power_input_entity"
CONF_BOILER_HEAT_ENTITY = "boiler_heat_entity"

# De overige gespiegelde metingen. Elke rol uit sources.MIRROR_SPECS hoort een
# eigen sleutel te hebben: zonder sleutel kan de gebruiker de bron niet kiezen
# en wint de detectievolgorde (Quatt vóór OpenQuatt) altijd stilzwijgend.
# sources.ROLE_CONF_KEYS legt die koppeling vast en wordt in de tests bewaakt.
CONF_CONTROL_SETPOINT_ENTITY = "control_setpoint_entity"
CONF_ROOM_SETPOINT_ENTITY = "room_setpoint_entity"
CONF_COP_ENTITY = "cop_entity"
CONF_COMPRESSOR_ENTITY = "compressor_entity"

# How many days of detailed hourly data to fetch from Quatt API
API_FETCH_DAYS = 30

# Of de Quatt cloud-API überhaupt bevraagd wordt.
#
# Uit betekent níet dat de opgebouwde historie verdwijnt: de insights-cache
# blijft gelezen worden (retentie is 100 jaar, zie cache.py) en levert de oude
# dagen gewoon aan. Alleen nieuwe dagen komen dan uit de recorder in plaats van
# uit de cloud — die tak berekent dezelfde kolommen zelf, COP incluis.
# Standaard aan, zodat bestaande installaties niets merken.
CONF_QUATT_CLOUD_ENABLED = "quatt_cloud_enabled"
DEFAULT_QUATT_CLOUD_ENABLED = True

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
SERVICE_UPDATE_DASHBOARD = "update_dashboard"

# Agressiviteit van de COP-gewogen herverdeling van de warmtevraag.
#
# 0 = uit: de verschoven reeks is exact de vlakke reeks, dus de schaduwsensor
# toont hetzelfde als `warmtevraag`. Hoger verschuift meer warmte naar de uren
# met de beste COP, binnen de harde eis dat het totaal gelijk blijft.
# Standaard uit — dit is een schaduwsensor, geen regeling.
CONF_DEMAND_SHIFT_GAMMA = "demand_shift_gamma"
DEFAULT_DEMAND_SHIFT_GAMMA = 0.0

# Venster waarover de herverdeling rekent — bewust los van MPC_FORECAST_HOURS.
#
# De winst hangt volledig aan de dagzwaai binnen het venster: gemeten op een dag
# rond 0 °C met 8 K zwaai levert 6 uur 0,09%, 12 uur 3,4% en 24 uur 6,7%. Met
# zes uur valt er dus niets te verdelen.
#
# Maar MPC_FORECAST_HOURS verhogen zou de displayforecast van de MPC-sensor
# meevergroten: een tabel van 24 rijen op het dashboard en een viervoudig
# attribuut dat bij elke state-write de recorder in gaat. Die twee horen niet
# aan elkaar vast — het model heeft een lang venster nodig, de weergave niet.
#
# 24 uur en niet meer: daarboven wordt de temperatuurvoorspelling onbetrouwbaar,
# en de weer-entity levert ruim genoeg uren (~148) om dit te dekken.
DEMAND_SHIFT_HOURS = 24

# Hoeveel de kamertemperatuur maximaal mag wegzakken door de herverdeling (K).
#
# Gemeten op 20 januari 2026 met de geleerde massa van 25.583 Wh/K: gamma=1 geeft
# 0,16 K, gamma=2 geeft 0,25 K en gamma=3 geeft 0,34 K. Deze grens laat alles tot
# ongeveer gamma=2,5 ongemoeid en schaalt agressievere instellingen evenredig
# terug in plaats van ze te verwerpen.
#
# Geen configuratie-optie maar een veiligheidsgrens: gamma is de knop, dit is de
# rand waarbinnen die knop mag bewegen. De comfortterm van de firmware
# (3000 W/K) blijft daarnaast gewoon het echte vangnet.
DEMAND_SHIFT_MAX_DRIFT_K = 0.3

# Herpogingen voor de weersverwachting direct na het opstarten (seconden).
#
# De eerste poging valt in async_added_to_hass, en op dat moment is de
# weather-integratie er soms nog niet. Zonder herpoging blijft de forecast dan
# tot de volgende uurlijkse tik leeg, en draait de MPC-tabel een uur lang op de
# huidige buitentemperatuur voor elk uur — zichtbaar als 'condition: current'.
FORECAST_RETRY_DELAYS = (30, 120, 300)

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
MPC_SUPPLY_TEMP_MIN = 20.0       # °C — warmtepompen werken niet effectief onder 20°C aanvoer
MPC_SUPPLY_TEMP_COOL_MIN = 15.0  # °C — ondergrens voor koeling (LTV convectoren ~15°C)
MPC_SUPPLY_TEMP_MAX = 55.0       # °C

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

# ---------------------------------------------------------------------------
# Energy-OS brug: prijsgestuurd thermisch uitlopen + datahygiëne
# ---------------------------------------------------------------------------
# Comfort-vloer: laagste acceptabele binnentemperatuur. De coast-time sensor
# berekent hoe lang het huis (met WP uit) op zijn thermische massa kan uitlopen
# vóór de binnentemp deze grens raakt — gegeven de buitentemp- én zon-forecast.
CONF_COMFORT_FLOOR_TEMP = "comfort_floor_temp"
DEFAULT_COMFORT_FLOOR_TEMP = 19.0  # °C

# Hoe ver vooruit en met welke resolutie de afkoeling wordt gesimuleerd.
COAST_MAX_HOURS = 12
COAST_STEP_MINUTES = 15

# Optioneel: entity die aangeeft dat een externe regelaar (energy-os) de
# warmtepomp knijpt. Leeg = uit (geen filtering). Bij een waarde < FREE wordt
# de WP geknepen; die minuten worden uitgesloten van COP/warmteverlies-analyse,
# zodat de fits niet vervuild raken door externe ingrepen.
CONF_EOS_THROTTLE_ENTITY = "eos_throttle_entity"
DEFAULT_EOS_THROTTLE_ENTITY = ""  # leeg = geen filtering
EOS_THROTTLE_CAP_FREE = 20  # cap-waarde die "geen beperking" betekent

# Zonproductie-fractie per HA weather condition (proxy voor shortwave_radiation).
# Waarde × huidige solaredge_ac_power = geschatte zonproductie dat uur.
# Bron: HA weather condition strings (https://www.home-assistant.io/integrations/weather/)
# Geluidsniveau compensatie — actieve bijsturing via CiC sound level
CONF_SOUND_LEVEL_ENABLED = "sound_level_enabled"
CONF_SOUND_LEVEL_MAX_DAY = "sound_level_max_day"
CONF_SOUND_LEVEL_MAX_NIGHT = "sound_level_max_night"
CONF_SOUND_NIGHT_START_HOUR = "sound_night_start_hour"
CONF_SOUND_NIGHT_END_HOUR = "sound_night_end_hour"
SOUND_LEVEL_OPTIONS = ["building87", "silent", "library", "normal"]
DEFAULT_SOUND_LEVEL_MAX = "normal"  # standaard: geen beperking
DEFAULT_SOUND_NIGHT_START_HOUR = 23
DEFAULT_SOUND_NIGHT_END_HOUR = 7
OTGW_CYCLE_SECONDS = 300   # 5 minuten — interval compensatiecyclus
OTGW_UNAVAILABLE_TIMEOUT = 600  # 10 minuten — reset na MPC timeout

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

# chMaxWaterTemperatuur bijsturing
CONF_CH_MAX_WATER_ENABLED = "ch_max_water_enabled"
CONF_CH_MAX_WATER_ENTITY = "ch_max_water_entity"
CONF_CH_MAX_WATER_SOURCE = "ch_max_water_source"
CONF_CH_MAX_WATER_HYSTERESIS = "ch_max_water_hysteresis"
CONF_CH_MAX_WATER_INTERVAL = "ch_max_water_interval"

DEFAULT_CH_MAX_WATER_SOURCE = "stooklijn"   # "stooklijn" | "mpc"
DEFAULT_CH_MAX_WATER_HYSTERESIS = 1.0       # °C
DEFAULT_CH_MAX_WATER_INTERVAL = 30          # minuten


# --- Compressorstarts ------------------------------------------------------
#
# Kortcyclen is af te lezen aan het aantal starts per uur. De diagnose die
# gebruikers zoeken is "veel starts terwijl het buiten niet warm is": dan levert
# de warmtepomp meer dan het huis vraagt en zet ze zichzelf uit.

# Boven deze compressorfrequentie geldt de warmtepomp als draaiend. Niet exact
# nul: de sensor rapporteert bij stilstand af en toe een restwaarde, en zonder
# marge telt die ruis mee als start.
COMPRESSOR_ON_HZ = 1.0

# Een stop korter dan dit is geen stop maar een meethiaat — een gemiste update
# of een seconde ruis. Zonder deze drempel telt één haperende sensor als tien
# starts.
COMPRESSOR_MIN_OFF_SECONDS = 60

# Hoe lang de geschiedenis wordt bewaard. De recorder gooit ruwe states na tien
# dagen weg; deze integratie bewaart zelf, zodat een heel stookseizoen te
# vergelijken valt.
COMPRESSOR_KEEP_DAYS = 400

# Eigen store, los van de recorder — zie de toelichting in cycling.py.
COMPRESSOR_STORAGE_VERSION = 1
COMPRESSOR_STORAGE_KEY = f"{DOMAIN}.compressor_starts"

# Ook zonder toestandswisseling herrekenen: het uursvenster schuift door, dus
# zonder tik blijft de state hangen op het aantal van het moment waarop de
# compressor voor het laatst iets deed.
COMPRESSOR_REFRESH_INTERVAL = timedelta(minutes=5)
