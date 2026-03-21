# Quatt Warmteanalyse

<p align="center">
  <img src="icon.png" alt="Quatt Warmteanalyse" width="128">
</p>

Home Assistant custom integration for analyzing your Quatt heat pump performance. Calculates optimal heating curves (stooklijn), COP, heat loss characteristics, and optionally compares with historical gas consumption.

## Features

- **Heating curve analysis** — Calculates the optimal stooklijn based on actual heat pump data and compares it with Quatt's estimated curve
- **Heat loss coefficient** — Determines your home's heat loss in W/K using linear regression
- **COP tracking** — Average coefficient of performance and per-temperature scatter data
- **Knee temperature** — Detects the outdoor temperature where supplemental heating (boiler) kicks in
- **Gas comparison** (optional) — Compare heat pump performance with historical gas consumption from before installation
- **MPC shadow sensor** — Calculates an optimal supply temperature advice based on a 6-hour weather forecast, without touching your system
- **Quatt advies** — Shows exactly which parameters to ask Quatt to adjust (stookgrens, nominaal vermogen, stooklijn breakpoints)
- **OTGW compensatie** (optional) — Active supply temperature correction via OpenTherm Gateway room temperature override
- **OpenQuatt ready** — Output sensors with optimal heating curve breakpoints and balance point, ready for OpenQuatt automations
- **Dashboard included** — Pre-built Lovelace dashboard with interactive charts

## MPC shadow sensor

The MPC (Model Predictive Control) sensor calculates what supply temperature your heat pump *should* be running at, given the weather forecast for the next 6 hours. It runs in **shadow mode**: it only produces advice and never writes any setpoints to your system.

### How it works

Every update cycle the sensor:

1. Fetches the outdoor temperature forecast for the next 6 hours from your weather entity
2. Estimates solar heat gain for each hour based on solar radiation forecast (from [Open-Meteo](https://open-meteo.com/)) or your PV inverter output
3. Applies a simple RC thermal model of your home (heat loss coefficient + thermal mass) to predict how much heat the house will need hour by hour
4. Picks the supply temperature that keeps the house comfortable while avoiding unnecessary overheating

The result is compared to the actual supply temperature via the **error sensors** on the Shadow Validatie tab:
- A positive error means the sensor advises a higher supply temperature than what's currently running (risk of underheating)
- A negative error means the sensor advises lower (heat pump is running warmer than necessary)

### Solar gain correction

If you have a PV inverter (`sensor.solaredge_ac_power` or similar), the sensor learns how much of the solar production translates into actual heat gain inside your home. This calibration improves automatically over time: on sunny days the MPC recommendation will be lower than on overcast days with the same outdoor temperature.

Without a PV sensor the integration falls back to a fixed conversion factor based on Open-Meteo shortwave radiation estimates.

### Shadow mode — why no live control yet

Live control requires an OpenTherm Gateway (OTGW) to write setpoints to the boiler/heat pump. Shadow mode lets you collect real-world validation data: after a few weeks of data you can judge whether the MPC advice would have improved efficiency before enabling live control.

If you have an OTGW installed, you can enable **OTGW compensatie** (see below) to actively correct the Quatt supply temperature.

## Quatt advies sensor

The `sensor.quatt_warmteanalyse_quatt_advies` sensor analyzes your heat pump data and tells you exactly what parameters to ask Quatt to change in their app. This is useful because Quatt support can adjust your installation settings remotely, but you need to tell them what to change.

The sensor state shows how many adjustments are recommended (e.g. "3 aanpassingen aanbevolen" or "Instellingen optimaal"). The attributes contain the specific advice:

| Attribute | Description |
|-----------|-------------|
| `stookgrens_huidig` | Current Quatt balance temperature (°C) |
| `stookgrens_optimaal` | Recommended balance temperature based on your home's heat loss |
| `stookgrens_advies` | Human-readable advice text |
| `nominaal_vermogen_huidig_w` | Current Quatt rated power at -10°C (W) |
| `nominaal_vermogen_optimaal_w` | Recommended rated power based on actual heat demand |
| `nominaal_vermogen_advies` | Human-readable advice text |
| `stooklijn_punten` | 6 optimal heating curve breakpoints (-10°C to +15°C) |
| `stooklijn_advies` | All breakpoints as readable text |

> **Note:** The "nominaal vermogen" comparison requires that you enter your current Quatt stooklijn setting (two points) in the integration configuration (Step 3). Without this, the sensor can only show the recommended values, not the difference.

## OTGW compensatie

If you have an [OpenTherm Gateway](https://otgw.tclcode.com/) installed between your thermostat and Quatt CiC, the integration can actively correct overheating by adjusting the room temperature that the CiC "sees".

### How it works

The Quatt CiC uses room temperature and thermostat setpoint to determine heat output. It does not accept external supply temperature setpoints. The OTGW can intercept and modify the OpenTherm messages between thermostat and CiC.

When the MPC sensor detects that the CiC is overheating (supply temperature too high), the integration increases the OTGW room temperature override. The CiC thinks the room is warmer than it actually is and reduces its output.

### Safety features

| Safety measure | Details |
|---------------|---------|
| **Direction** | Only makes CiC think room is *warmer* (reduces output). Never colder (never increases output). |
| **Max offset** | Configurable, default 2.0°C, hard maximum 3.0°C |
| **Rate limit** | Max 0.5°C change per 5-minute cycle |
| **Dead band** | No action when MPC error is within ±1.0°C |
| **HP inactive** | Override resets to 0 when heat pump is off (flow < 30 l/h) |
| **MPC timeout** | Override resets if MPC sensor is unavailable for >10 minutes |
| **Switch off** | Override always resets to 0 when the switch is turned off |
| **HA shutdown** | Override resets on entity removal |

### Configuration

Enable OTGW compensation in the integration configuration (Step 3 or Options):

| Setting | Default | Description |
|---------|---------|-------------|
| `otgw_enabled` | `false` | Enable OTGW compensation |
| `otgw_room_temp_override` | `number.otgw_room_temperature_override` | OTGW override entity |
| `otgw_max_offset` | `2.0` | Maximum room temperature offset (°C) |

After enabling, a switch entity appears: `switch.quatt_warmteanalyse_otgw_compensatie`. Turn it on to start active compensation.

The switch exposes these attributes for monitoring:

| Attribute | Description |
|-----------|-------------|
| `current_offset` | Current room temperature offset being applied (°C) |
| `mpc_error` | Difference between MPC advice and actual supply temp (°C) |
| `hp_active` | Whether the heat pump is currently running |

## OpenQuatt readiness

If you plan to install an [OpenQuatt](https://github.com/openquatt) (ESPHome-based CiC replacement), the integration provides output sensors that OpenQuatt automations can consume directly.

### Sensors

**`sensor.quatt_warmteanalyse_openquatt_stooklijn`** — Optimal heating curve breakpoints

State = number of breakpoints (6). Attributes:

| Attribute | Description |
|-----------|-------------|
| `breakpoints` | Full list of `{buiten_temp, aanvoer_temp}` dicts |
| `bp_1_buiten` .. `bp_6_buiten` | Outdoor temperature per breakpoint (°C) |
| `bp_1_aanvoer` .. `bp_6_aanvoer` | Optimal supply temperature per breakpoint (°C) |

**`sensor.quatt_warmteanalyse_openquatt_balance_point`** — Optimal balance temperature (°C)

**`sensor.quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur`** — Real-time optimal supply temperature (°C), updated with weather forecast and solar gain.

### Example automation

To sync the balance point to OpenQuatt automatically:

```yaml
automation:
  - alias: "Sync balance point to OpenQuatt"
    trigger:
      - platform: state
        entity_id: sensor.quatt_warmteanalyse_openquatt_balance_point
    action:
      - service: number.set_value
        target:
          entity_id: number.openquatt_house_zero_power_temp_c
        data:
          value: "{{ states('sensor.quatt_warmteanalyse_openquatt_balance_point') }}"
```

## Requirements

- Home Assistant 2024.1.0 or newer
- [Quatt integration](https://github.com/marcoboers/home-assistant-quatt) configured and running
- [apexcharts-card](https://github.com/RomRider/apexcharts-card) (HACS frontend) for the dashboard charts
- [mini-graph-card](https://github.com/kalkih/mini-graph-card) (HACS frontend) for the historical trend charts

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > **Custom repositories**
3. Add `https://github.com/Appesteijn/stooklijn` and select **Integration** as category
4. Search for "Quatt Warmteanalyse" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/quatt_stooklijn` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "Quatt Warmteanalyse"
3. Follow the setup wizard:

### Step 1: Heat pump data
- **Start/end date** — The period to analyze (after heat pump installation)
- **Temperature sensors** — Comma-separated entity IDs for outdoor temperature (in priority order)
- **Power sensor** — Entity for total heat pump power

### Step 2: Gas analysis (optional)
- **Gas entity** — Cumulative gas meter (m³)
- **Gas period** — Date range from *before* heat pump installation
- **Calorific value** — Gas energy content (default: 9.77 kWh/m³ for Dutch gas)
- **Boiler efficiency** — Your old boiler's efficiency (default: 0.90)
- **Hot water threshold** — Temperature above which gas usage is counted as hot water only (default: 18°C)

### Step 3: Current stooklijn (optional)
- Enter your current Quatt stooklijn setting as two points (e.g. -10°C/10000W and 16°C/0W) for comparison in charts

## Usage

The analysis runs **automatically** when Home Assistant starts, so your dashboards are always populated after a restart.

You can also trigger an analysis manually:

1. Call the `quatt_stooklijn.run_analysis` service, or
2. Press the **Analyse Starten** button on the dashboard

### Dashboard

Import the dashboard from `dashboards/quatt_stooklijn_dashboard.yaml`:

1. Go to **Settings** > **Dashboards** > **Add Dashboard**
2. Choose **New dashboard from scratch**
3. Open the dashboard, switch to YAML mode (three dots > **Edit in YAML**)
4. Paste the contents of `quatt_stooklijn_dashboard.yaml`

The dashboard shows:
- Key metrics (heat loss, balance temperature, knee point, COP)
- Heat loss vs outdoor temperature (with gas comparison if configured)
- Stooklijn comparison: optimal vs Quatt estimated vs your actual setting
- Heating demand vs Quatt capacity
- COP vs outdoor temperature
- Heat demand table at various temperatures

### Adapting the dashboard to your setup

The dashboard uses a mix of entity IDs: those created by this integration (always working) and those from the Quatt hardware integration and other devices in your home (may need adjustment).

**These entity IDs are auto-generated by this integration — no changes needed:**

All `sensor.quatt_warmteanalyse_*` entities.

**These come from the [Quatt HA integration](https://github.com/marcoboers/home-assistant-quatt) — consistent across all Quatt systems:**

| Entity ID | Description |
|-----------|-------------|
| `sensor.heatpump_flowmeter_temperature` | Actual supply temperature |
| `sensor.heatpump_flowmeter_flowrate` | Flow rate (l/h) |
| `sensor.heatpump_hp1_temperature_outside` | Outdoor temperature (HP1 sensor) |
| `sensor.heatpump_hp1_temperature_water_in` | Return water temperature |
| `sensor.heatpump_thermostat_control_setpoint` | Quatt's current setpoint |
| `sensor.heatpump_thermostat_room_temperature` | Room temperature |
| `sensor.heatpump_thermostat_room_setpoint` | Room setpoint |

If you have two heat pumps, the dashboard also references `sensor.heatpump_hp2_temperature_outside`. If you only have one, this sensor is simply unavailable — that's fine, the dashboard still works.

**These may differ per household — check and replace as needed:**

| Entity ID | What to replace it with |
|-----------|------------------------|
| `sensor.thermostat_temperature_outside` | Your outdoor temperature sensor (Toon, Nest, weather station, etc.) |
| `sensor.solaredge_ac_power` | Your solar inverter power sensor — or remove the MPC solar graph if you have no PV |

**Weather forecast — required for MPC shadow sensor:**

The MPC sensor needs a weather forecast entity to predict the next 6 hours of outdoor temperature and solar radiation. During setup you are asked to provide one; the default is `weather.home`.

Almost every Home Assistant installation has this: the built-in [Met.no integration](https://www.home-assistant.io/integrations/met/) creates `weather.home` automatically. If your entity is named differently (e.g. `weather.your_city`), update it in **Settings > Devices & Services > Quatt Warmteanalyse > Configure**.

> **No weather integration?** The MPC sensor will stay `unavailable`. Install Met.no (free, no API key) or any other HA weather integration to enable it.

**Search and replace:**

Open `dashboards/quatt_stooklijn_dashboard.yaml` in a text editor and use find-and-replace:

```
sensor.thermostat_temperature_outside  →  your outdoor temp entity
sensor.solaredge_ac_power              →  your solar power entity (or remove those lines)
```

The MPC/shadow validation tab also uses `sensor.heatpump_flowmeter_temperature` for the supply temperature — this is already in the Quatt hardware list above.

## Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| `heat_loss_coefficient` | W/K | Heat loss per degree below balance point |
| `balance_point` | °C | Outdoor temp where no heating is needed |
| `optimal_stooklijn_slope` | W/°C | Slope of the optimal heating curve |
| `quatt_stooklijn_slope` | W/°C | Slope of Quatt's estimated curve |
| `knee_temperature` | °C | Temperature where boiler must assist |
| `average_cop` | — | Average coefficient of performance |
| `freezing_performance_slope` | W/°C | Heat pump performance below 0°C |
| `gas_heat_loss_coefficient` | W/K | Heat loss from gas period (if configured) |
| `actual_stooklijn` | W/°C | Your configured Quatt stooklijn |
| `last_analysis` | timestamp | When the last analysis was run |
| `analysis_status` | — | Current analysis status |
| `quatt_advies` | — | Number of recommended Quatt parameter changes |
| `openquatt_balance_point` | °C | Optimal balance point for OpenQuatt |
| `openquatt_stooklijn` | — | 6 heating curve breakpoints for OpenQuatt |

**Live sensors** (update in real-time based on current conditions):

| Sensor | Unit | Description |
|--------|------|-------------|
| `geschatte_actuele_cop` | — | Interpolated COP at current outdoor temperature |
| `aanbevolen_aanvoertemperatuur` | °C | Recommended supply temperature (stooklijn-based) |
| `mpc_aanbevolen_aanvoertemperatuur` | °C | MPC recommended supply temperature (with weather + solar forecast) |
| `stooklijn_fout_aanvoertemperatuur` | °C | Error: stooklijn advice − actual supply |
| `mpc_fout_aanvoertemperatuur` | °C | Error: MPC advice − actual supply |

**Control entities** (only when OTGW compensation is enabled):

| Entity | Type | Description |
|--------|------|-------------|
| `otgw_compensatie` | switch | Enable/disable active OTGW compensation |

## Services

| Service | Description |
|---------|-------------|
| `quatt_stooklijn.run_analysis` | Run the full analysis pipeline |
| `quatt_stooklijn.clear_data` | Clear all analysis results and reset sensors |

## How it works

The integration ports the analysis from a Jupyter notebook into a Home Assistant integration:

1. **Data collection** — Uses a hybrid approach combining four data sources (see below)
2. **Stooklijn estimation** — Estimates the current Quatt stooklijn from HA recorder minute-level power data, using the 2500W filter to capture full-capacity operation
3. **Knee detection** — Piecewise linear fit on Quatt hourly data to find the temperature where the boiler must assist
4. **Heat loss regression** — Linear regression on daily heat energy vs outdoor temperature to determine your home's thermal characteristics
5. **COP calculation** — Computes daily COP from heat output (`totalHpHeat`) and electrical input (`totalHpElectric`) for accurate values
6. **Auto-startup** — Analysis runs automatically when Home Assistant starts, so dashboards are always populated

### Hybrid data approach

The integration combines five data sources for the best balance of coverage, accuracy, and speed:

| Source | Data type | Period | Purpose |
|--------|-----------|--------|---------|
| **HA Recorder statistics** | Daily means | Full configured period (months) | Heat loss regression, COP scatter, optimal stooklijn |
| **HA Recorder state changes** | Minute-level | Last 30 days | Knee detection (primary), stooklijn estimation |
| **Knee data store** | Hourly, filtered | Rolling 3 years | Knee detection: cold-weather history across winters |
| **Quatt API** | Hourly detail | Last 30 days | Knee detection (fallback), envelope analysis |
| **Insights cache** | Hourly detail | Previously fetched days | Extends Quatt hourly data beyond 30-day API window |

**How it works per analysis run:**

1. **Recorder statistics** — Fetches daily mean values from HA's long-term statistics for the full configured period. These are derived from the Quatt integration sensors that HA already records (power, temperature, electricity input, boiler heat).
2. **Recorder state changes** — Fetches minute-level power and temperature readings from the last 30 days (limited by HA's `purge_keep_days` setting, default 10 days). Used as the primary input for knee detection and stooklijn estimation.
3. **Knee data store** — Loads previously saved cold-weather data points (see below). Combined with the current recorder window so knee detection benefits from multiple winters of data.
4. **Cached historical data** — Checks the insights cache for any Quatt hourly data from before the 30-day API window. This data was fetched in previous runs and is reused without any API calls.
5. **Quatt API** — Fetches the last 30 days of hourly data from the Quatt `get_insights` service. Already-cached days are skipped. Used as fallback for knee detection when recorder data is insufficient.
6. **Merge** — Recorder data forms the base, API data overwrites recent days (more accurate for the last 30 days).

**Result:** From the first run you get months of daily data (via recorder), plus 30 days of hourly detail. Both caches grow organically over time, and knee detection improves with each cold period.

## Voorspellingsmodellen

De integratie gebruikt drie modellen die op elkaar voortbouwen:

### Lineair model (heat loss regressie)

De basis van de hele integratie. Past een rechte lijn door je historische dagdata:

```
warmtevraag (W) = slope × T_buiten + intercept
```

- **Input:** dagelijkse Quatt API data (gemiddelde buitentemperatuur vs. totaal vermogen)
- **Output:** warmteverliescoëfficiënt (W/K), balanspunt (°C), nominaal vermogen bij elke temperatuur
- **Gebruikt door:** MPC sensor, Quatt advies sensor, stooklijn breakpoints, OpenQuatt sensoren
- **Methode:** twee-pass lineaire regressie met outlier-filtering (residuen > 2.5σ worden verwijderd)
- **Beperking:** neemt aan dat de relatie temperatuur→warmtevraag een rechte lijn is — houdt geen rekening met wind, zon of thermische massa

### MPC forecast (physics-based)

Bouwt voort op het lineaire model en voegt real-time correcties toe:

```
T_aanvoer = T_retour + max(0, warmtevraag − zonnewarmte) / (1.16 × debiet)
```

- **Input:** live sensordata (retourtemp, debiet, buitentemp) + weersvoorspelling (6 uur) + zonnestraling (Open-Meteo)
- **Output:** aanbevolen aanvoertemperatuur per uur, nu + 6 uur vooruit
- **Voordeel t.o.v. lineair:** corrigeert voor zonnewinst en gebruikt actuele retourtemperatuur en debiet
- **Solar learning:** leert dynamisch hoeveel van je PV-opwek als warmte in huis terechtkomt door SolarEdge data te vergelijken met Open-Meteo stralingsdata

| | Lineair model | MPC forecast |
|---|---|---|
| **Databron** | Historische dagdata | Live sensors + weersvoorspelling |
| **Zon/wind** | Nee | Zon ja, wind nee |
| **Updatefrequentie** | Bij analyse (handmatig/opstart) | Elke sensorupdate |
| **Doel** | Thermische karakteristiek van je woning | Real-time aanvoertemp advies |

### XGBoost (experimenteel, niet actief)

In de repository staan getrainde XGBoost modellen (`.ubj` bestanden) uit de Jupyter notebooks (`ml_train_baseline.ipynb`, `ml_multistep.ipynb`). Deze zijn **niet geïntegreerd** in de Home Assistant component.

- **Wat het doet:** voorspelt warmtevraag met meer features dan het lineaire model (wind, zon, tijd, thermische massa)
- **Potentieel voordeel:** nauwkeuriger voorspelling bij wisselend weer, kan het lineaire model in de MPC vervangen
- **Status:** research-fase — de modellen zijn getraind maar nog niet aangesloten op de integratie

### Hoe de modellen samenwerken

```
Historische data → [Lineair model] → slope, intercept, balanspunt
                                          ↓
Live sensors + weer → [MPC forecast] → optimale aanvoertemperatuur
                                          ↓
                          ┌───────────────┼───────────────┐
                          ↓               ↓               ↓
                    Quatt advies    OTGW compensatie   OpenQuatt
                   (statisch)       (actieve sturing)  (output sensoren)
```

## Performance & Caching

### API call efficiency

Thanks to the hybrid approach, the integration makes very few API calls:

```
First run:  ~30 API calls (last 30 days) + instant recorder fetch
Day 2:      ~1 API call   (only today, rest cached)
Day 30:     ~1 API call   (cache now contains 60 days of hourly data)
Day 90:     ~1 API call   (cache now contains 120 days of hourly data)
```

Subsequent analyses typically complete in 1-2 seconds with only 1 API call.

### Quatt stooklijn estimation

The integration estimates your current Quatt stooklijn (heating curve) from HA recorder data:

- Uses **minute-level state changes** from the recorder (not Quatt API hourly averages)
- Filters for continuous full-capacity operation (≥ 2500W)
- Fits a linear regression to data right of the knee point
- Minute-level data is essential: hourly averages can include partial operation hours that distort the slope

### Knee detection

The integration uses a grid-search algorithm to find the outdoor temperature where your heat pump reaches maximum capacity (the "knee"):

**Priority order:**
1. **HA Recorder (primary)** — Minute-level data with no defrost dilution bias. Each minute below the power threshold (2500W) is individually excluded, so defrost cycles don't lower the average. Combined with the knee data store for a stronger multi-year dataset.
2. **Quatt hourly (fallback)** — Used when the recorder lacks sufficient cold-weather data (e.g. after a mild 30-day period). Quatt hourly averages mix active operation with defrost cycles, which biases the detected knee ~1–2°C too warm.

**Why recorder data is more accurate:**
Quatt hourly averages at cold temperatures include defrost cycles (typically 15 min/hour), which lower the average power by ~25%. This makes the cold side of the piecewise fit look weaker than the warm side, pushing the detected knee toward warmer temperatures (~3°C instead of ~1.75°C on real data).

**Smart filtering:**
- Removes minutes where power < 2500W (defrost, standby, partial operation)
- Rolling standard deviation filter removes unstable hours in Quatt fallback path
- Physical constraints on the piecewise fit reject near-straight-line splits

### Knee data store

The knee data store (`quatt_stooklijn_knee_data`) persistently accumulates cold-weather data points across analyses:

- After each analysis, active HP minutes (power ≥ 2500W, temp < 10°C) are resampled to hourly averages and stored per day
- New analyses merge stored historical points with the current 30-day recorder window
- This means cold-weather data from previous winters is always available for knee detection, even during mild periods
- Rolling 3-year window: entries older than 3 years are purged automatically

**Storage footprint:** ~8 hourly points per heating day × ~150 heating days/year ≈ **~18 KB/year**, growing to a max of ~54 KB after 3 years.

### Insights cache management

The insights cache is stored in `.storage/quatt_stooklijn_insights_cache` and:
- Automatically cleans up data older than 3 years (matching the knee data store retention)
- Can be manually cleared by deleting the cache file and restarting HA
- Is completely transparent (no configuration needed)
- Survives Home Assistant restarts

### Monitoring

Check your Home Assistant logs to see data source performance:
```
INFO: Fetching recorder statistics for 2025-06-01 to 2026-02-17...
INFO: Recorder statistics: 261 days (2025-06-01 to 2026-02-17)
INFO: Found 28 days of cached historical hourly data
INFO: Fetching Quatt API data for 2026-01-19 to 2026-02-17 (30 days)...
INFO: API/cache data: 58 days total (57 from cache, 1 from API)
INFO: Knee data store loaded: 45 days, 312 hourly points
INFO: Knee data store: added 1 new days
INFO: Knee data store: 46 days, 319 hourly points (oldest: 2025-11-15)
INFO: Knee detection: 4821 current + 319 historical points
INFO: Knee detected (recorder+history): 1.75°C, 5870 W (5140 points total)
INFO: Quatt stooklijn estimated from recorder: slope=-353.5 W/°C, intercept=6037 W, zero at 17.1°C (1820 data points)
```

## Troubleshooting

### Cache issues

**Problem:** Every analysis makes many API calls (cache not working)

**Solutions:**
1. Check `.storage/quatt_stooklijn_insights_cache` exists
2. Check Home Assistant has write permissions to `.storage/` directory
3. Check logs for cache errors

**Problem:** Want to start fresh with empty cache

**Solution:**
1. Stop Home Assistant
2. Delete `.storage/quatt_stooklijn_insights_cache`
3. Start Home Assistant
4. Next analysis will fetch last 30 days from API and rebuild cache (recorder data is always available)

### Analysis issues

**Problem:** Knee detection fails or gives unexpected results

**Possible causes:**
- Not enough cold weather data in the knee store yet (store is empty on first install)
- The last 30 days were all mild (no data below the knee temperature)
- Heat pump hasn't operated at maximum capacity during any stored period

**Solutions:**
- After the first cold period, the knee store will populate automatically — subsequent analyses will include that data
- Check logs for `Knee data store: X days, Y hourly points` to see what is available
- If knee detection falls back to Quatt hourly data, expect a result ~1–2°C warmer than the true value; this corrects itself once cold-weather recorder data is stored

**Problem:** Want to reset the knee data store

**Solution:**
1. Stop Home Assistant
2. Delete `.storage/quatt_stooklijn_knee_data`
3. Start Home Assistant — the store rebuilds from the current 30-day recorder window on the next analysis

**Problem:** COP values seem too low

**Possible causes:**
- Summer days without heating drag down the average
- The integration filters on days with >= 200W heating demand, but check your configured date range

**Solution:**
- The integration automatically filters non-heating days from COP and stooklijn calculations

## Credits

This integration is based on the Jupyter notebooks originally created by [Rickvdt](https://github.com/Rickvdt/hello-world). The notebooks provided the foundation for the analysis methods used here.

## License

MIT
