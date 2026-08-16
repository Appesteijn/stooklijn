"""Quatt Stooklijn integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .ch_max_water import ChMaxWaterController
from .sources import ENTITY_PREFIX, MIRROR_SPECS, OVERVIEW_SLUG, SourceRegistry
from .discovery import ROLE_CH_MAX_WATER, async_resolve_entity
from .const import (
    CONF_CH_MAX_WATER_ENABLED,
    CONF_CH_MAX_WATER_ENTITY,
    CONF_CH_MAX_WATER_SOURCE,
    CONF_CH_MAX_WATER_HYSTERESIS,
    CONF_CH_MAX_WATER_INTERVAL,
    CONF_SOUND_LEVEL_ENABLED,
    DEFAULT_CH_MAX_WATER_SOURCE,
    DEFAULT_CH_MAX_WATER_HYSTERESIS,
    DEFAULT_CH_MAX_WATER_INTERVAL,
    DOMAIN,
    SERVICE_CLEAR_DATA,
    SERVICE_RUN_ANALYSIS,
)
from .coordinator import (
    QuattStooklijnCoordinator,
    QuattStooklijnData,
)

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_URL = "quatt-warmteanalyse"
_DASHBOARD_YAML = Path(__file__).parent / "dashboard.yaml"


async def _async_migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Hernoem quatt_stooklijn_* entiteiten naar quatt_warmteanalyse_* (eenmalige migratie)."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)

    migrated = 0
    for entity_entry in entities:
        old_id = entity_entry.entity_id
        if "quatt_stooklijn_" not in old_id:
            continue
        new_id = old_id.replace("quatt_stooklijn_", "quatt_warmteanalyse_")
        if registry.async_get(new_id) is not None:
            continue  # Nieuwe naam al in gebruik, sla over
        registry.async_update_entity(old_id, new_entity_id=new_id)
        migrated += 1
        _LOGGER.info("Entiteit gemigreerd: %s → %s", old_id, new_id)

    if migrated:
        _LOGGER.info("%d entiteit(en) hernoemd naar quatt_warmteanalyse_*", migrated)


async def _async_migrate_source_entity_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Herstel spiegel-entity-ID's die met een area-prefix zijn aangemaakt.

    In v0.8.8 kregen de spiegelsensoren hun entity-id van Home Assistant, en die
    bouwt hem op uit de area van het device. Bij een device in een area levert
    dat ``sensor.bijkeuken_quatt_warmteanalyse_aanvoertemperatuur`` op, terwijl
    het dashboard de schone id verwacht. Vanaf v0.8.9 wordt de id vastgepind;
    entiteiten die al met de prefix bestaan worden hier eenmalig hernoemd.

    Matcht op unique_id, niet op naam — dat is de enige stabiele sleutel.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)

    wanted = {
        f"{entry.entry_id}_source_{spec.role}": f"sensor.{ENTITY_PREFIX}_{spec.slug}"
        for spec in MIRROR_SPECS
    }
    wanted[f"{entry.entry_id}_source_overview"] = (
        f"sensor.{ENTITY_PREFIX}_{OVERVIEW_SLUG}"
    )

    migrated = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        target = wanted.get(reg_entry.unique_id)
        if target is None or reg_entry.entity_id == target:
            continue
        if registry.async_get(target) is not None:
            _LOGGER.warning(
                "Kan %s niet hernoemen naar %s: die id is al in gebruik",
                reg_entry.entity_id, target,
            )
            continue
        registry.async_update_entity(reg_entry.entity_id, new_entity_id=target)
        migrated += 1
        _LOGGER.info("Bronsensor hernoemd: %s → %s", reg_entry.entity_id, target)

    if migrated:
        _LOGGER.info("%d bronsensor(en) teruggezet op hun vaste entity-id", migrated)


async def _async_setup_dashboard(hass: HomeAssistant) -> None:
    """Create the Quatt Warmteanalyse dashboard if it doesn't exist yet."""
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            return

        dashboards = getattr(lovelace, "dashboards", None) or {}
        if _DASHBOARD_URL in dashboards:
            return  # Already exists

        dashboards_collection = getattr(lovelace, "dashboards_collection", None)
        if dashboards_collection is None:
            return

        await dashboards_collection.async_create_item({
            "url_path": _DASHBOARD_URL,
            "require_admin": False,
            "icon": "mdi:chart-line",
            "title": "Quatt Warmteanalyse",
            "show_in_sidebar": True,
            "mode": "storage",
        })

        dashboard_obj = dashboards.get(_DASHBOARD_URL)
        if dashboard_obj is not None and _DASHBOARD_YAML.exists():
            config = yaml.safe_load(_DASHBOARD_YAML.read_text())
            await dashboard_obj.async_save(config)
            _LOGGER.info("Quatt Warmteanalyse dashboard aangemaakt")

    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Kon dashboard niet automatisch aanmaken: %s", err)


# Unique-ID suffixen van entiteiten die alleen bestaan als sound_level_enabled=True.
_SOUND_LEVEL_ENTITY_SUFFIXES = (
    "_sound_level_compensation",  # switch
    "_gas_boiler_active",         # binary_sensor
    "_sound_level_sensor",        # sensor
)


async def _async_cleanup_sound_level_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Verwijder soundslider-entities uit de registry als de feature is uitgeschakeld."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)

    for entity_entry in entities:
        uid = entity_entry.unique_id or ""
        if any(uid.endswith(suffix) for suffix in _SOUND_LEVEL_ENTITY_SUFFIXES):
            registry.async_remove(entity_entry.entity_id)
            _LOGGER.info(
                "Soundslider-entity verwijderd: %s", entity_entry.entity_id
            )

PLATFORMS = ["binary_sensor", "sensor", "switch", "text"]


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Quatt Stooklijn from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    merged_config = {**entry.data, **entry.options}
    coordinator = QuattStooklijnCoordinator(hass, merged_config)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Bronregistratie moet klaarstaan vóór de platforms laden: de
    # spiegelsensoren halen hem daar op tijdens hun setup.
    source_registry = SourceRegistry(hass, merged_config)
    hass.data[DOMAIN][f"{entry.entry_id}_sources"] = source_registry
    entry.async_on_unload(source_registry.async_setup())

    # Moet vóór het laden van de platforms: staat er nog een spiegelsensor met
    # een area-prefix in het register, dan pakt HA díé entity-id op basis van de
    # unique_id en negeert de vastgepinde id in de entiteit zelf.
    await _async_migrate_source_entity_ids(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Update coordinator config when user changes options
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    await _async_migrate_entity_ids(hass, entry)

    # Ruim soundslider-entities op als de feature is uitgeschakeld.
    if not merged_config.get(CONF_SOUND_LEVEL_ENABLED, False):
        await _async_cleanup_sound_level_entities(hass, entry)

    await _async_setup_dashboard(hass)

    # Auto-run analysis on startup so dashboards are populated immediately
    async def _async_startup_analysis(_event=None) -> None:
        """Run analysis automatically after HA startup."""
        _LOGGER.info("Running automatic startup analysis")
        coordinator.data.analysis_status = "running"
        coordinator.async_set_updated_data(coordinator.data)
        try:
            await coordinator.async_refresh()
        except Exception:
            coordinator.data.analysis_status = "error"
            coordinator.async_set_updated_data(coordinator.data)
            _LOGGER.exception("Startup analysis failed")

    # Schedule after HA is fully started to avoid blocking boot
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    if hass.is_running:
        # HA already running (e.g. integration reload), run immediately
        entry.async_create_background_task(
            hass, _async_startup_analysis(), "quatt_stooklijn_startup_analysis"
        )
    else:
        # HA still starting, wait for full startup
        _startup_fired = False

        async def _async_startup_analysis_once(_event=None) -> None:
            nonlocal _startup_fired
            _startup_fired = True
            await _async_startup_analysis(_event)

        cancel = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _async_startup_analysis_once
        )

        def _cancel_if_pending() -> None:
            # Alleen annuleren als het event nog niet afgevuurd is.
            # Als het event al afgevuurd is, heeft async_listen_once de
            # listener al verwijderd — cancel() aanroepen zou dan een
            # ValueError + log-melding in HA's core triggeren.
            if not _startup_fired:
                cancel()

        entry.async_on_unload(_cancel_if_pending)

    # chMaxWaterTemperatuur bijsturing (opt-in)
    if merged_config.get(CONF_CH_MAX_WATER_ENABLED, False):
        controller = ChMaxWaterController(
            hass=hass,
            # Leeg laten mag: ChMaxWaterController detecteert dan zelf de
            # juiste number-entity (zie discovery.py).
            number_entity=async_resolve_entity(
                hass, merged_config, CONF_CH_MAX_WATER_ENTITY, ROLE_CH_MAX_WATER
            ),
            source=merged_config.get(CONF_CH_MAX_WATER_SOURCE, DEFAULT_CH_MAX_WATER_SOURCE),
            hysteresis=merged_config.get(CONF_CH_MAX_WATER_HYSTERESIS, DEFAULT_CH_MAX_WATER_HYSTERESIS),
            interval_minutes=merged_config.get(CONF_CH_MAX_WATER_INTERVAL, DEFAULT_CH_MAX_WATER_INTERVAL),
        )
        hass.data[DOMAIN][f"{entry.entry_id}_ch_max_water"] = controller
        entry.async_on_unload(controller.async_setup())

    # Register service (once for the domain)
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_ANALYSIS):

        async def handle_run_analysis(call: ServiceCall) -> None:
            """Handle the run_analysis service call."""
            # Run analysis for all configured entries
            for coord in hass.data[DOMAIN].values():
                if isinstance(coord, QuattStooklijnCoordinator):
                    _LOGGER.info("Triggering Quatt Stooklijn analysis")
                    # Set status to "running" and notify sensors immediately
                    coord.data.analysis_status = "running"
                    coord.async_set_updated_data(coord.data)
                    try:
                        await coord.async_refresh()
                    except Exception:
                        coord.data.analysis_status = "error"
                        coord.async_set_updated_data(coord.data)
                        raise

        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_ANALYSIS,
            handle_run_analysis,
            schema=vol.Schema({}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_DATA):

        async def handle_clear_data(call: ServiceCall) -> None:
            """Handle the clear_data service call."""
            for coord in hass.data[DOMAIN].values():
                if isinstance(coord, QuattStooklijnCoordinator):
                    _LOGGER.info("Clearing Quatt Stooklijn analysis data")
                    coord.data = QuattStooklijnData()
                    coord.async_set_updated_data(coord.data)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_DATA,
            handle_clear_data,
            schema=vol.Schema({}),
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_sources", None)

    # Remove service if no entries left
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_RUN_ANALYSIS)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_DATA)

    return unload_ok
