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
- **Dashboard included** — Pre-built Lovelace dashboard with interactive charts

## Requirements

- Home Assistant 2024.1.0 or newer
- [Quatt integration](https://github.com/marcoboers/home-assistant-quatt) configured and running
- [apexcharts-card](https://github.com/RomRider/apexcharts-card) (HACS frontend) for the dashboard charts

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

After configuration, trigger an analysis:

1. Call the `quatt_stooklijn.run_analysis` service, or
2. Press the **Analyse Starten** button on the dashboard

The analysis fetches data from Home Assistant's recorder, runs calculations, and populates the sensors.

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

## Services

| Service | Description |
|---------|-------------|
| `quatt_stooklijn.run_analysis` | Run the full analysis pipeline |
| `quatt_stooklijn.clear_data` | Clear all analysis results and reset sensors |

## How it works

The integration ports the analysis from a Jupyter notebook into a Home Assistant integration:

1. **Data collection** — Fetches heat pump data via the Quatt `get_insights` service and gas history from HA's recorder
2. **Stooklijn calculation** — Fits piecewise linear models to hourly power vs temperature data, using envelope filtering to find the maximum capacity curve
3. **Heat loss regression** — Linear regression on daily heat energy vs outdoor temperature to determine your home's thermal characteristics
4. **COP calculation** — Computes daily COP from heat output and electrical input

## Performance & Caching

### Intelligent Data Caching

The integration uses smart caching to minimize API calls to the Quatt service:

- **First run** — Fetches the last 30 days of data (not the full configured period)
- **Subsequent runs** — Only fetches new days, all historical data is cached
- **Organic growth** — Cache automatically expands by 1 day per analysis run
- **Persistent storage** — Cache survives Home Assistant restarts

### Benefits

**For new users:**
```
Day 1:  Fetches 30 days  → 30 API calls (safe, quick setup)
Day 2:  Fetches 1 day    → 1 API call (only today)
Day 30: Fetches 1 day    → Cache now contains 60 days
Day 90: Fetches 1 day    → Cache now contains 120 days
```

**Result:** Full analysis history builds up automatically over time, without overwhelming the Quatt API.

**For existing users:**
- Subsequent analyses are near-instant (1-2 seconds)
- Only 1 API call per run (today's data)
- **99.6% reduction** in API calls compared to fetching everything each time

### Knee Detection Improvements

The integration uses advanced knee detection to find the temperature where your heat pump reaches maximum capacity:

- **Primary method** — Uses all available cached Quatt hourly data (grows from 30 to 250+ days)
- **Fallback method** — Uses last 10 days of Home Assistant recorder data if Quatt data unavailable
- **Smart filtering** — Automatically removes defrost cycles and partial operation hours for cleaner data
- **Progressive accuracy** — Analysis becomes more accurate as cache grows over time

**Accuracy improvement:**
```
Traditional method: 10 days of recorder data
This integration:
  - Day 1:  30 days (3x better)
  - Day 30: 60 days (6x better)
  - Day 90: 120 days (12x better)
  - Day 250+: Full season coverage (25x better)
```

### Cache Management

The cache is stored in `.storage/quatt_stooklijn_insights_cache` and:
- Automatically cleans up data older than 1 year
- Can be manually cleared by deleting the cache file and restarting HA
- Is completely transparent (no configuration needed)

### Monitoring

Check your Home Assistant logs to see cache performance:
```
INFO: First run detected: limiting initial fetch to last 30 days
      (configuration requested 251 days). Full history will build up
      organically as you run analyses over time.
INFO: Insights data: 30 days total, 0 from cache, 30 from API
INFO: Cache now contains 30 days (2026-01-18 to 2026-02-16)
INFO: Cache will reach full year of history in ~335 days
```

After cache is established:
```
INFO: Insights data: 252 days total, 251 from cache, 1 from API
INFO: Cache now contains 252 days (2025-06-01 to 2026-02-16)
```

## Troubleshooting

### Cache Issues

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
4. Next analysis will fetch last 30 days and rebuild cache

### Analysis Issues

**Problem:** Knee detection fails or gives unexpected results

**Possible causes:**
- Not enough cold weather data in cache yet (wait for cache to grow)
- Heat pump hasn't operated at maximum capacity during cached period
- Check logs for specific error messages

**Solutions:**
- Wait for cache to accumulate more days (especially winter months)
- Ensure heat pump has run during cold periods
- Check that Quatt integration is working correctly

## License

MIT
