"""Tests voor het aanmaken en bijwerken van het meegeleverde dashboard.

De inzet is asymmetrisch. Een gemiste update is vervelend: de gebruiker houdt
een oud dashboard. Een onterechte overschrijving is erger: dan is zijn eigen
werk weg, zonder waarschuwing en zonder weg terug. Elke twijfel hoort dus naar
"niet doen" te vallen, en daar zijn deze tests op gericht.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.quatt_stooklijn.dashboard import (
    ASK,
    CREATE,
    UPDATE,
    UP_TO_DATE,
    DashboardManager,
    decide,
    fingerprint,
)

@pytest.fixture(autouse=True)
def _geen_echte_store(monkeypatch):
    """De HA-stub maakt van Store een MagicMock-klasse, en die probeert zijn
    eerste argument als spec te gebruiken. Hier hebben we alleen een plek nodig
    om een vingerafdruk te bewaren; elke test zet er zelf een nep-store overheen.
    """
    import custom_components.quatt_stooklijn.dashboard as mod

    monkeypatch.setattr(mod, "Store", lambda *a, **kw: MagicMock())


OUD = {"views": [{"title": "Overzicht", "cards": [{"type": "tile"}]}]}
NIEUW = {"views": [{"title": "Overzicht", "cards": [{"type": "tile", "icon": "mdi:x"}]}]}
EIGEN = {"views": [{"title": "Overzicht", "cards": [{"type": "tile"}, {"type": "eigen"}]}]}


class TestVingerafdruk:
    def test_zelfde_inhoud_geeft_zelfde_afdruk(self):
        assert fingerprint(OUD) == fingerprint({"views": list(OUD["views"])})

    def test_andere_inhoud_geeft_andere_afdruk(self):
        assert fingerprint(OUD) != fingerprint(NIEUW)

    def test_sleutelvolgorde_maakt_niet_uit(self):
        """HA schrijft de config door JSON heen; de volgorde die terugkomt
        hoeft niet die van dashboard.yaml te zijn."""
        a = {"title": "x", "views": []}
        b = {"views": [], "title": "x"}
        assert fingerprint(a) == fingerprint(b)

    def test_leeg_is_geen_probleem(self):
        assert fingerprint({}) != fingerprint([])


class TestBesluit:
    """De drie gevallen uit het ontwerp, plus de randen."""

    def test_geen_dashboard_dan_aanmaken(self):
        assert decide(None, NIEUW, None) == CREATE
        assert decide(None, NIEUW, fingerprint(OUD)) == CREATE

    def test_staat_al_goed(self):
        assert decide(NIEUW, NIEUW, None) == UP_TO_DATE
        assert decide(NIEUW, NIEUW, fingerprint(OUD)) == UP_TO_DATE

    def test_onaangeraakt_wordt_bijgewerkt(self):
        """Precies wat wij er zelf op zetten → niemand heeft eraan gezeten."""
        assert decide(OUD, NIEUW, fingerprint(OUD)) == UPDATE

    def test_aangepast_wordt_met_rust_gelaten(self):
        assert decide(EIGEN, NIEUW, fingerprint(OUD)) == ASK

    def test_onbekende_herkomst_wordt_met_rust_gelaten(self):
        """Bestaande installaties hebben nog geen vingerafdruk.

        Dit is het geval dat bij de eerste uitrol iedereen raakt. Zou het naar
        UPDATE vallen, dan overschrijft één update in één klap alle bestaande
        dashboards.
        """
        assert decide(OUD, NIEUW, None) == ASK
        assert decide(EIGEN, NIEUW, None) == ASK

    def test_lege_vingerafdruk_telt_niet_als_bewijs(self):
        assert decide(OUD, NIEUW, "") == ASK


def _manager(live, last_written, *, bestaat=True):
    """Bouw een DashboardManager met een nep-dashboard eronder."""
    hass = MagicMock()

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)

    dashboard_obj = MagicMock()
    dashboard_obj.async_load = AsyncMock(return_value=live)
    dashboard_obj.async_save = AsyncMock()

    collection = MagicMock()
    collection.async_create_item = AsyncMock()

    hass.data = {
        "lovelace": {
            "dashboards": {"quatt-warmteanalyse": dashboard_obj} if bestaat else {},
            "dashboards_collection": collection,
        }
    }

    mgr = DashboardManager(hass)
    mgr._store = MagicMock()
    mgr._store.async_load = AsyncMock(
        return_value={"fingerprint": last_written} if last_written else None
    )
    mgr._store.async_save = AsyncMock()
    mgr._async_raise_issue = MagicMock()
    mgr._async_clear_issue = MagicMock()
    return mgr, dashboard_obj, collection


class TestManager:
    """Dezelfde matrix, maar nu langs de echte code — inclusief het schrijfpad."""

    @pytest.mark.asyncio
    async def test_aangepast_dashboard_wordt_niet_geschreven(self):
        mgr, dash, _ = _manager(EIGEN, fingerprint(OUD))
        assert await mgr.async_setup() == ASK
        dash.async_save.assert_not_called()
        mgr._async_raise_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_onbekende_herkomst_wordt_niet_geschreven(self):
        mgr, dash, _ = _manager(EIGEN, None)
        assert await mgr.async_setup() == ASK
        dash.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_onaangeraakt_dashboard_wordt_geschreven(self, monkeypatch):
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(OUD, fingerprint(OUD))
        assert await mgr.async_setup() == UPDATE
        dash.async_save.assert_awaited_once_with(NIEUW)
        mgr._async_clear_issue.assert_called()

    @pytest.mark.asyncio
    async def test_na_schrijven_is_de_afdruk_bijgewerkt(self, monkeypatch):
        """Anders valt de vólgende update terug op ASK en staat alles stil."""
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, _, _ = _manager(OUD, fingerprint(OUD))
        await mgr.async_setup()
        bewaard = mgr._store.async_save.await_args[0][0]
        assert bewaard["fingerprint"] == fingerprint(NIEUW)

    @pytest.mark.asyncio
    async def test_reeds_goed_legt_de_herkomst_alsnog_vast(self, monkeypatch):
        """Wie toevallig al de nieuwe versie draait, krijgt daarmee herkomst.

        Dat is de route waarlangs bestaande installaties uit het 'onbekend'-
        gat klimmen zonder dat er iets overschreven wordt.
        """
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(NIEUW, None)
        assert await mgr.async_setup() == UP_TO_DATE
        dash.async_save.assert_not_called()
        assert mgr._store.async_save.await_args[0][0]["fingerprint"] == fingerprint(NIEUW)

    @pytest.mark.asyncio
    async def test_ontbrekend_dashboard_wordt_aangemaakt(self, monkeypatch):
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, _, collection = _manager(None, None, bestaat=False)
        assert await mgr.async_setup() == CREATE
        collection.async_create_item.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_forceren_overschrijft_wel(self, monkeypatch):
        """Het pad achter de service en de reparatiemelding."""
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(EIGEN, None)
        assert await mgr.async_force_update() is True
        dash.async_save.assert_awaited_once_with(NIEUW)
        mgr._async_clear_issue.assert_called()

    @pytest.mark.asyncio
    async def test_een_kapot_dashboard_blokkeert_de_integratie_niet(self):
        """Een dashboard is comfort, geen kernfunctie."""
        mgr, dash, _ = _manager(OUD, None)
        dash.async_load = AsyncMock(side_effect=RuntimeError("lovelace stuk"))
        assert await mgr.async_setup() == ASK  # geen exception naar buiten


class TestMeegeleverdBestand:
    """Het bestand dat we uitleveren moet leesbaar en compleet zijn."""

    def test_dashboard_yaml_is_geldig(self):
        from custom_components.quatt_stooklijn.dashboard import _load_shipped

        cfg = _load_shipped()
        assert cfg["views"], "geen views in het meegeleverde dashboard"
        kaarten = sum(
            len(s.get("cards", []))
            for v in cfg["views"]
            for s in v.get("sections", [])
        )
        assert kaarten > 40, f"onverwacht weinig kaarten: {kaarten}"

    def test_afdruk_van_het_meegeleverde_bestand_is_stabiel(self):
        """Twee keer inlezen hoort dezelfde afdruk te geven.

        Zou dat niet zo zijn, dan zou elke herstart het dashboard als 'gewijzigd'
        zien en ongevraagd overschrijven.
        """
        from custom_components.quatt_stooklijn.dashboard import _load_shipped

        assert fingerprint(_load_shipped()) == fingerprint(_load_shipped())
