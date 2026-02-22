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
- Automatically cleans up data older than 1 year
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
