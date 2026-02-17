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

1. **Data collection** — Uses a hybrid approach combining three data sources (see below)
2. **Stooklijn calculation** — Fits piecewise linear models to hourly power vs temperature data, using envelope filtering to find the maximum capacity curve
3. **Heat loss regression** — Linear regression on daily heat energy vs outdoor temperature to determine your home's thermal characteristics
4. **COP calculation** — Computes daily COP from heat output (`totalHpHeat`) and electrical input (`totalHpElectric`) for accurate values
5. **Auto-startup** — Analysis runs automatically when Home Assistant starts, so dashboards are always populated

### Hybrid data approach

The integration combines three data sources for the best balance of coverage, accuracy, and speed:

| Source | Data type | Period | Purpose |
|--------|-----------|--------|---------|
| **HA Recorder** | Daily means | Full configured period (months) | Heat loss regression, COP scatter, stooklijn |
| **Quatt API** | Hourly detail | Last 30 days | Knee detection, envelope analysis |
| **Cache** | Hourly detail | Previously fetched days | Extends hourly data beyond 30-day API window |

**How it works per analysis run:**

1. **Recorder statistics** — Fetches daily mean values from HA's long-term statistics for the full configured period. These are derived from the Quatt integration sensors that HA already records (power, temperature, electricity input, boiler heat).
2. **Cached historical data** — Checks the cache for any hourly data from before the 30-day API window. This data was fetched in previous runs and is reused without any API calls.
3. **Quatt API** — Fetches the last 30 days of hourly data from the Quatt `get_insights` service. Already-cached days are skipped.
4. **Merge** — Recorder data forms the base, API data overwrites recent days (more accurate for the last 30 days).

**Result:** From the first run you get months of daily data (via recorder), plus 30 days of hourly detail. The hourly cache grows organically over time.

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

### Knee detection

The integration uses advanced knee detection to find the temperature where your heat pump reaches maximum capacity:

- **Primary method** — Uses all available Quatt hourly data (30 days + cached history)
- **Fallback method** — Uses last 10 days of HA recorder data if Quatt data is unavailable
- **Smart filtering** — Automatically removes defrost cycles and partial operation hours
- **Progressive accuracy** — As the cache grows, more hourly data is available for knee detection

### Cache management

The cache is stored in `.storage/quatt_stooklijn_insights_cache` and:
- Automatically cleans up data older than 1 year
- Can be manually cleared by deleting the cache file and restarting HA
- Is completely transparent (no configuration needed)
- Survives Home Assistant restarts

### Monitoring

Check your Home Assistant logs to see data source performance:
```
INFO: Fetching recorder statistics for 2025-06-01 to 2026-02-16...
INFO: Recorder statistics: 261 days (2025-06-01 to 2026-02-16)
INFO: Found 28 days of cached historical hourly data
INFO: Fetching Quatt API data for 2026-01-18 to 2026-02-16 (30 days)...
INFO: API/cache data: 58 days total (57 from cache, 1 from API)
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
- Not enough cold weather data in hourly cache yet (daily data from recorder is always available)
- Heat pump hasn't operated at maximum capacity during cached period
- Check logs for specific error messages

**Solutions:**
- Wait for cache to accumulate more winter days (knee detection uses hourly data)
- Ensure heat pump has run during cold periods
- Check that Quatt integration is working correctly

**Problem:** COP values seem too low

**Possible causes:**
- Summer days without heating drag down the average
- The integration filters on days with >= 200W heating demand, but check your configured date range

**Solution:**
- The integration automatically filters non-heating days from COP and stooklijn calculations

## License

MIT
