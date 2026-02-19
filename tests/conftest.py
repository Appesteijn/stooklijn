"""Shared test fixtures for Quatt Stooklijn tests."""

from __future__ import annotations

from dataclasses import field
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Stub homeassistant package so imports work without a real HA installation
# ---------------------------------------------------------------------------
import sys
from types import ModuleType


def _ensure_module(name: str) -> ModuleType:
    """Create a stub module if it doesn't exist."""
    if name not in sys.modules:
        sys.modules[name] = ModuleType(name)
    return sys.modules[name]


def _stub_ha():
    """Create minimal stubs so the component can be imported."""
    # Core HA modules
    ha = _ensure_module("homeassistant")
    _ensure_module("homeassistant.core")
    _ensure_module("homeassistant.config_entries")
    _ensure_module("homeassistant.helpers")
    _ensure_module("homeassistant.helpers.update_coordinator")
    _ensure_module("homeassistant.helpers.entity_platform")
    _ensure_module("homeassistant.helpers.storage")
    _ensure_module("homeassistant.components")
    _ensure_module("homeassistant.components.sensor")
    _ensure_module("homeassistant.components.text")
    _ensure_module("homeassistant.components.recorder")
    _ensure_module("homeassistant.components.recorder.history")
    _ensure_module("homeassistant.components.recorder.statistics")
    _ensure_module("homeassistant.util")
    _ensure_module("homeassistant.util.dt")
    _ensure_module("voluptuous")

    # Provide key classes/sentinels
    core = sys.modules["homeassistant.core"]
    core.HomeAssistant = MagicMock
    core.ServiceCall = MagicMock
    core.callback = lambda f: f

    config_entries = sys.modules["homeassistant.config_entries"]
    config_entries.ConfigEntry = MagicMock
    config_entries.ConfigFlow = type("ConfigFlow", (), {
        "async_show_form": lambda *a, **kw: None,
        "async_create_entry": lambda *a, **kw: None,
    })
    config_entries.OptionsFlow = type("OptionsFlow", (), {})

    # Sensor stubs
    sensor_mod = sys.modules["homeassistant.components.sensor"]
    sensor_mod.SensorEntity = type("SensorEntity", (), {})
    from dataclasses import dataclass as _dc

    @_dc(frozen=True)
    class _SensorEntityDescription:
        key: str = ""
        name: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: object = None
        state_class: object = None
        icon: str | None = None

    sensor_mod.SensorEntityDescription = _SensorEntityDescription
    sensor_mod.SensorDeviceClass = MagicMock()
    sensor_mod.SensorStateClass = MagicMock()

    # Text stubs
    text_mod = sys.modules["homeassistant.components.text"]
    text_mod.TextEntity = type("TextEntity", (), {})
    text_mod.TextMode = MagicMock()

    # Coordinator stubs
    coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    coord_mod.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {
            "__init__": lambda self, *a, **kw: None,
            "async_set_updated_data": lambda self, data: None,
            "async_refresh": AsyncMock(),
            "__class_getitem__": classmethod(lambda cls, item: cls),
        },
    )
    coord_mod.CoordinatorEntity = type(
        "CoordinatorEntity",
        (),
        {"__class_getitem__": classmethod(lambda cls, item: cls)},
    )

    # Entity platform
    ep = sys.modules["homeassistant.helpers.entity_platform"]
    ep.AddEntitiesCallback = MagicMock

    # Recorder stubs
    recorder = sys.modules["homeassistant.components.recorder"]
    recorder.get_instance = MagicMock()
    history = sys.modules["homeassistant.components.recorder.history"]
    history.state_changes_during_period = MagicMock()
    stats_mod = sys.modules["homeassistant.components.recorder.statistics"]
    stats_mod.statistics_during_period = MagicMock()

    # Storage stub
    storage_mod = sys.modules["homeassistant.helpers.storage"]
    storage_mod.Store = MagicMock

    # dt_util
    dt_mod = sys.modules["homeassistant.util.dt"]
    dt_mod.utcnow = lambda: datetime.now(timezone.utc)
    dt_mod.parse_datetime = lambda s: datetime.fromisoformat(s)

    # voluptuous stub
    vol = sys.modules["voluptuous"]
    vol.Schema = lambda *a, **kw: None
    vol.Required = lambda *a, **kw: a[0] if a else None
    vol.Optional = lambda *a, **kw: a[0] if a else None
    vol.Coerce = lambda t: t


# Run stubs before any component imports
_stub_ha()


# ---------------------------------------------------------------------------
# Import component modules (after stubs are in place)
# ---------------------------------------------------------------------------
from custom_components.quatt_stooklijn.analysis.heat_loss import (
    HeatLossResult,
    calculate_heat_loss,
)
from custom_components.quatt_stooklijn.analysis.stooklijn import (
    StooklijnResult,
    calculate_stooklijn,
    _piecewise_linear,
)
from custom_components.quatt_stooklijn.coordinator import (
    QuattStooklijnData,
    _calc_stooklijn_from_points,
)
from custom_components.quatt_stooklijn.const import (
    CONF_ACTUAL_STOOKLIJN_TEMP1,
    CONF_ACTUAL_STOOKLIJN_POWER1,
    CONF_ACTUAL_STOOKLIJN_TEMP2,
    CONF_ACTUAL_STOOKLIJN_POWER2,
    CONF_QUATT_START_DATE,
    CONF_QUATT_END_DATE,
    CONF_TEMP_ENTITIES,
    CONF_POWER_ENTITY,
    CONF_GAS_ENABLED,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic data generators
# ---------------------------------------------------------------------------


@pytest.fixture
def daily_heating_df():
    """Create a daily DataFrame with a known linear relationship.

    heat = -200 * temp + 4000  (slope=-200, intercept=4000)
    So heat_loss_coefficient = 200 W/K, balance_point = 20°C
    """
    temps = np.linspace(-5, 15, 30)
    heat = -200 * temps + 4000
    df = pd.DataFrame(
        {"avg_temperatureOutside": temps, "totalHeatPerHour": heat}
    )
    df.index = pd.date_range("2024-01-01", periods=len(df), freq="D")
    df.index.name = "date"
    return df


@pytest.fixture
def daily_heating_df_noisy():
    """Daily DataFrame with noise added to linear relationship."""
    rng = np.random.default_rng(42)
    temps = np.linspace(-5, 15, 50)
    heat = -200 * temps + 4000 + rng.normal(0, 100, len(temps))
    heat = np.maximum(heat, 0)
    df = pd.DataFrame(
        {"avg_temperatureOutside": temps, "totalHeatPerHour": heat}
    )
    df.index = pd.date_range("2024-01-01", periods=len(df), freq="D")
    df.index.name = "date"
    return df


@pytest.fixture
def live_history_df():
    """Create a merged temp+power DataFrame simulating live history.

    Generates data with a clear knee at 2°C:
    - Below 2°C: power ≈ -100*(temp-2) + 6000 (flatter, reduced COP)
    - Above 2°C: power ≈ -400*(temp-2) + 6000 (steeper, normal operation)
    """
    rng = np.random.default_rng(42)
    temps = np.linspace(-3, 12, 200)
    power = np.where(
        temps < 2,
        -100 * (temps - 2) + 6000 + rng.normal(0, 100, len(temps)),
        -400 * (temps - 2) + 6000 + rng.normal(0, 100, len(temps)),
    )
    power = np.maximum(power, 2600)  # above MIN_POWER_FILTER
    df = pd.DataFrame({"temp": temps, "power": power})
    df.index = pd.date_range("2024-01-01", periods=len(df), freq="h")
    return df


@pytest.fixture
def hourly_quatt_df():
    """Hourly Quatt insights DataFrame."""
    rng = np.random.default_rng(42)
    n = 200
    temps = np.linspace(-5, 5, n)
    hp_heat = -300 * temps + 4000 + rng.normal(0, 200, n)
    hp_heat = np.maximum(hp_heat, 100)

    df = pd.DataFrame({
        "temperatureOutside": temps,
        "hpHeat": hp_heat,
        "hpElectric": hp_heat / 3.5,
        "boilerHeat": np.zeros(n),
        "boilerGas": np.zeros(n),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="h")
    df.index.name = "timestamp"
    return df


@pytest.fixture
def daily_quatt_df():
    """Daily Quatt insights DataFrame with COP data."""
    rng = np.random.default_rng(42)
    n = 30
    temps = np.linspace(-2, 12, n)
    total_heat = -200 * temps + 4000 + rng.normal(0, 50, n)
    total_heat = np.maximum(total_heat, 300)

    df = pd.DataFrame({
        "avg_temperatureOutside": temps,
        "totalHpHeat": total_heat * 24,
        "totalHpElectric": total_heat * 24 / 3.5,
        "totalBoilerHeat": np.zeros(n),
        "totalBoilerGas": np.zeros(n),
        "totalHeatPerHour": total_heat,
        "averageCOP": 3.5 + rng.normal(0, 0.3, n),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="D")
    df.index.name = "date"
    return df


@pytest.fixture
def mock_config():
    """Return a typical config dict."""
    return {
        CONF_QUATT_START_DATE: "2024-01-01",
        CONF_QUATT_END_DATE: "2024-06-30",
        CONF_TEMP_ENTITIES: ["sensor.temp_outside"],
        CONF_POWER_ENTITY: "sensor.hp_power",
        CONF_GAS_ENABLED: False,
    }


@pytest.fixture
def mock_config_with_points():
    """Config dict with stooklijn comparison points."""
    return {
        CONF_QUATT_START_DATE: "2024-01-01",
        CONF_QUATT_END_DATE: "2024-06-30",
        CONF_TEMP_ENTITIES: ["sensor.temp_outside"],
        CONF_POWER_ENTITY: "sensor.hp_power",
        CONF_GAS_ENABLED: False,
        CONF_ACTUAL_STOOKLIJN_TEMP1: -5.0,
        CONF_ACTUAL_STOOKLIJN_POWER1: 8000.0,
        CONF_ACTUAL_STOOKLIJN_TEMP2: 15.0,
        CONF_ACTUAL_STOOKLIJN_POWER2: 2000.0,
    }
