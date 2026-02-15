# Quatt Warmteanalyse

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

## License

MIT
