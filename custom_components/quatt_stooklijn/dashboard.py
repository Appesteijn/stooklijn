"""Het meegeleverde dashboard aanmaken en bijhouden.

Tot en met v0.9.3 werd het dashboard één keer aangemaakt en daarna nooit meer
aangeraakt. Dat was veilig — niemands handwerk ging verloren — maar het betekende
ook dat elke dashboardverbetering alleen nieuwe gebruikers bereikte. Wie de
integratie al draaide bleef zitten met de versie van zijn eerste installatie,
zonder melding en zonder zichtbaar verschil.

De uitweg is niet "voortaan altijd overschrijven", want het dashboard is een
gewoon opslag-dashboard: zet de gebruiker er een kaart bij, dan staat die in
hetzelfde bestand. De uitweg is wéten of er iets te overschrijven valt. Daarom
onthouden we bij elke schrijfactie een vingerafdruk van wat we wegschreven, en
vergelijken we die bij de volgende start met wat er nú staat:

* gelijk  → niemand heeft eraan gezeten, dus veilig bijwerken;
* anders  → de gebruiker heeft het aangepast, dus afblijven en het vragen.

Bestaande installaties hebben nog geen vingerafdruk. Daar geldt "niet zeker", en
dat behandelen we als aangepast: vragen, niet doen. Vanaf de eerstvolgende
schrijfactie is de herkomst wél bekend.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DASHBOARD_URL = "quatt-warmteanalyse"
DASHBOARD_TITLE = "Quatt Warmteanalyse"
DASHBOARD_ICON = "mdi:chart-line"
DASHBOARD_YAML = Path(__file__).parent / "dashboard.yaml"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.dashboard"

ISSUE_ID = "dashboard_update_available"

# Uitkomsten van decide().
CREATE = "create"          # dashboard bestaat nog niet
UP_TO_DATE = "up_to_date"  # staat er al precies zo op
UPDATE = "update"          # onaangeraakt sinds wij het schreven → bijwerken
ASK = "ask"                # aangepast of herkomst onbekend → niet aanraken


def fingerprint(config: Any) -> str:
    """Stabiele vingerafdruk van een dashboardconfig.

    ``sort_keys`` maakt hem ongevoelig voor sleutelvolgorde: HA schrijft de
    config door JSON heen, en de volgorde die terugkomt hoeft niet die van
    dashboard.yaml te zijn. Wat we willen weten is of de *inhoud* nog dezelfde
    is, niet hoe hij toevallig geserialiseerd werd.
    """
    blob = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def decide(
    live: Any | None, shipped: Any, last_written: str | None
) -> str:
    """Bepaal wat er met het dashboard moet gebeuren.

    Bewust een losse functie zonder HA eromheen: dit is de hele afweging, en die
    hoort testbaar te zijn zonder draaiende Home Assistant.

    Args:
        live: de config zoals hij nu op het dashboard staat, of None als het
            dashboard nog niet bestaat (of leeg is).
        shipped: de config uit dashboard.yaml van deze versie.
        last_written: de vingerafdruk die wij bij onze laatste schrijfactie
            opsloegen, of None als we die niet hebben.
    """
    if live is None:
        return CREATE
    live_fp = fingerprint(live)
    if live_fp == fingerprint(shipped):
        return UP_TO_DATE
    if last_written is not None and live_fp == last_written:
        return UPDATE
    return ASK


def _load_shipped() -> Any:
    """Lees dashboard.yaml van schijf (blocking — hoort in de executor)."""
    return yaml.safe_load(DASHBOARD_YAML.read_text(encoding="utf-8"))


def _lovelace_part(lovelace: Any, name: str) -> Any:
    """Haal een onderdeel uit hass.data['lovelace'].

    Dat was vroeger een dict en is later een dataclass geworden. Beide vormen
    komen in het wild voor, dus we proberen ze allebei in plaats van stilletjes
    niets te doen op de ene helft van de installaties.
    """
    if lovelace is None:
        return None
    part = getattr(lovelace, name, None)
    if part is None and isinstance(lovelace, dict):
        part = lovelace.get(name)
    return part


class DashboardManager:
    """Beheert het meegeleverde dashboard voor één Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    # -- opslag van de vingerafdruk ----------------------------------------

    async def async_last_written(self) -> str | None:
        """De vingerafdruk van wat wij het laatst wegschreven, indien bekend."""
        data = await self._store.async_load()
        if not data:
            return None
        return data.get("fingerprint")

    async def _async_remember(self, config: Any) -> None:
        from homeassistant.util import dt as dt_util

        await self._store.async_save(
            {
                "fingerprint": fingerprint(config),
                "written_at": dt_util.utcnow().isoformat(),
            }
        )

    # -- toegang tot het dashboard zelf ------------------------------------

    def _dashboard_obj(self) -> Any:
        lovelace = self._hass.data.get("lovelace")
        dashboards = _lovelace_part(lovelace, "dashboards") or {}
        return dashboards.get(DASHBOARD_URL)

    async def _async_live_config(self) -> Any | None:
        """De config zoals hij nu op het dashboard staat, of None."""
        from homeassistant.components.lovelace.const import ConfigNotFound

        dashboard_obj = self._dashboard_obj()
        if dashboard_obj is None:
            return None
        try:
            return await dashboard_obj.async_load(False)
        except ConfigNotFound:
            # Dashboard bestaat wel, maar heeft nog geen config. Voor ons
            # hetzelfde als "bestaat niet": er valt niets te overschrijven.
            return None

    async def _async_write(self, config: Any) -> bool:
        """Schrijf de config weg en onthoud de vingerafdruk."""
        dashboard_obj = self._dashboard_obj()
        if dashboard_obj is None:
            return False
        await dashboard_obj.async_save(config)
        await self._async_remember(config)
        return True

    async def _async_create(self, config: Any) -> bool:
        """Maak het dashboard aan en vul het."""
        lovelace = self._hass.data.get("lovelace")
        collection = _lovelace_part(lovelace, "dashboards_collection")
        if collection is None:
            return False
        await collection.async_create_item(
            {
                "url_path": DASHBOARD_URL,
                "require_admin": False,
                "icon": DASHBOARD_ICON,
                "title": DASHBOARD_TITLE,
                "show_in_sidebar": True,
                "mode": "storage",
            }
        )
        return await self._async_write(config)

    # -- de melding --------------------------------------------------------

    def _async_raise_issue(self) -> None:
        from homeassistant.helpers import issue_registry as ir

        ir.async_create_issue(
            self._hass,
            DOMAIN,
            ISSUE_ID,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ID,
        )

    def _async_clear_issue(self) -> None:
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(self._hass, DOMAIN, ISSUE_ID)

    # -- het eigenlijke werk -----------------------------------------------

    async def async_setup(self) -> str:
        """Maak het dashboard aan of werk het bij; geeft het genomen besluit terug."""
        try:
            shipped = await self._hass.async_add_executor_job(_load_shipped)
            live = await self._async_live_config()
            last_written = await self.async_last_written()
            decision = decide(live, shipped, last_written)

            if decision == CREATE:
                if await self._async_create(shipped):
                    _LOGGER.info("Quatt Warmteanalyse dashboard aangemaakt")
                self._async_clear_issue()

            elif decision == UPDATE:
                if await self._async_write(shipped):
                    _LOGGER.info(
                        "Quatt Warmteanalyse dashboard bijgewerkt naar de "
                        "meegeleverde versie (er waren geen eigen aanpassingen)"
                    )
                self._async_clear_issue()

            elif decision == UP_TO_DATE:
                # Staat al goed. Is de herkomst nog onbekend, leg hem dan nu
                # vast — dan kan de volgende update wél automatisch.
                if last_written != fingerprint(shipped):
                    await self._async_remember(shipped)
                self._async_clear_issue()

            else:  # ASK
                _LOGGER.info(
                    "Dashboard wijkt af van de meegeleverde versie en is niet "
                    "door ons geschreven; niet aangeraakt"
                )
                self._async_raise_issue()

            return decision
        except Exception:  # noqa: BLE001
            # Een dashboard is comfort, geen kernfunctie: het mag de setup van
            # de integratie nooit tegenhouden.
            _LOGGER.warning("Kon het dashboard niet bijwerken", exc_info=True)
            return ASK

    async def async_force_update(self) -> bool:
        """Schrijf de meegeleverde versie hoe dan ook weg.

        Dit is het pad achter de service en de reparatiemelding: de gebruiker
        heeft er dan expliciet om gevraagd, dus eigen aanpassingen sneuvelen
        met medeweten.
        """
        shipped = await self._hass.async_add_executor_job(_load_shipped)
        if self._dashboard_obj() is None:
            written = await self._async_create(shipped)
        else:
            written = await self._async_write(shipped)
        if written:
            self._async_clear_issue()
            _LOGGER.info("Quatt Warmteanalyse dashboard overschreven op verzoek")
        return written
