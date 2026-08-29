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

    def test_apexcharts_series_hebben_een_geldig_type(self):
        """apexcharts-card 2.2.3 kent per series alleen line, column en area.

        Dit is nu drie keer misgegaan. v0.2.37 draaide een eerdere poging terug
        ("type: scatter niet geldig in apexcharts-card 2.2.3"), v0.2.38 zette het
        vaste patroon neer — type: line met stroke_width: 0, punten zonder lijn —
        en v0.9.9 zette er alsnog weer een type: scatter in. De kaart faalt dan
        met een rode Configuration error en verdwijnt uit beeld, maar niets in de
        tests merkte het: YAML-geldigheid en het aantal kaarten kloppen gewoon.

        Een scatter maak je met chart-opties, niet met een series-type.
        """
        from custom_components.quatt_stooklijn.dashboard import _load_shipped

        toegestaan = {"line", "column", "area"}
        fout = []
        for view in _load_shipped()["views"]:
            for sectie in view.get("sections", []):
                for kaart in sectie.get("cards", []):
                    for genest in [kaart, *kaart.get("cards", [])]:
                        if genest.get("type") != "custom:apexcharts-card":
                            continue
                        titel = genest.get("header", {}).get("title", "(zonder titel)")
                        for serie in genest.get("series", []):
                            soort = serie.get("type")
                            if soort is not None and soort not in toegestaan:
                                fout.append(f"{titel}: type: {soort}")

        assert not fout, "ongeldig series-type: " + "; ".join(fout)

    def test_afdruk_van_het_meegeleverde_bestand_is_stabiel(self):
        """Twee keer inlezen hoort dezelfde afdruk te geven.

        Zou dat niet zo zijn, dan zou elke herstart het dashboard als 'gewijzigd'
        zien en ongevraagd overschrijven.
        """
        from custom_components.quatt_stooklijn.dashboard import _load_shipped

        assert fingerprint(_load_shipped()) == fingerprint(_load_shipped())


class TestManifestAfhankelijkheden:
    """Elke component die we importeren hoort in het manifest te staan.

    Dit is precies wat hassfest controleert, en dat draait pas in CI — na de
    push, na de tag. v0.9.4 liep daarop stuk: ``dashboard.py`` importeert
    ``ConfigNotFound`` uit ``homeassistant.components.lovelace``, en tot dan toe
    raakte de code lovelace alleen aan via de string ``hass.data["lovelace"]``,
    wat een statische controle nooit ziet.

    Platforms die we zélf leveren tellen niet mee: daarvoor is ``sensor.py``
    bestaan het bewijs, niet een regel in het manifest.
    """

    @staticmethod
    def _manifest():
        import json
        from pathlib import Path

        pad = (
            Path(__file__).parent.parent
            / "custom_components"
            / "quatt_stooklijn"
            / "manifest.json"
        )
        return json.loads(pad.read_text(encoding="utf-8")), pad.parent

    def test_geimporteerde_componenten_zijn_gedeclareerd(self):
        import re

        manifest, map_ = self._manifest()
        gedeclareerd = set(manifest.get("dependencies", [])) | set(
            manifest.get("after_dependencies", [])
        )

        patroon = re.compile(r"homeassistant\.components\.([a-z_]+)")
        ontbreekt: dict[str, set[str]] = {}
        for bestand in map_.rglob("*.py"):
            for component in patroon.findall(bestand.read_text(encoding="utf-8")):
                if component in gedeclareerd:
                    continue
                # Eigen platform? Dan hoort het er niet in.
                if (map_ / f"{component}.py").exists():
                    continue
                ontbreekt.setdefault(component, set()).add(bestand.name)

        assert not ontbreekt, (
            "niet gedeclareerd in manifest.json (dependencies of "
            f"after_dependencies): { {k: sorted(v) for k, v in ontbreekt.items()} }"
        )

    def test_lovelace_is_optioneel(self):
        """after_dependencies, niet dependencies.

        Zonder lovelace draait de integratie prima — er wordt dan alleen geen
        dashboard aangemaakt. Als harde dependency zou de integratie niet meer
        laden op een installatie zonder lovelace.
        """
        manifest, _ = self._manifest()
        assert "lovelace" in manifest.get("after_dependencies", [])
        assert "lovelace" not in manifest.get("dependencies", [])

    def test_sleutels_staan_in_de_volgorde_die_hassfest_eist(self):
        """domain, name, daarna alfabetisch.

        Ook dit kostte een release: het manifest stond toevallig al goed, en
        ``after_dependencies`` erbij zetten op de plek waar het logisch leek
        (naast ``dependencies``) brak de volgorde.
        """
        manifest, _ = self._manifest()
        sleutels = list(manifest)
        assert sleutels[:2] == ["domain", "name"], f"begint met {sleutels[:2]}"
        rest = sleutels[2:]
        assert rest == sorted(rest), f"niet alfabetisch vanaf sleutel 3: {rest}"


class TestAfwijzen:
    """"Nee" moet een antwoord zijn, geen uitstel.

    De reparatiemelding opent meteen de flow. Met een bevestigingsformulier is
    Submit de enige knop, en die overschrijft het dashboard — wie zijn eigen
    versie wil houden kan het dialoog dan alleen wegklikken, waarna de melding
    bij de volgende herstart terugkomt. Vandaar een menu met twee uitkomsten en
    een onthouden afwijzing.
    """

    @pytest.mark.asyncio
    async def test_afwijzen_legt_de_meegeleverde_versie_vast(self, monkeypatch):
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(EIGEN, None)
        await mgr.async_decline_update()
        dash.async_save.assert_not_called(), "afwijzen mag niets schrijven"
        assert mgr._store.async_save.await_args[0][0]["declined"] == fingerprint(NIEUW)

    @pytest.mark.asyncio
    async def test_na_afwijzen_komt_de_melding_niet_terug(self, monkeypatch):
        """De kern: dit is het gedrag bij elke volgende herstart."""
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(EIGEN, None)
        mgr._store.async_load = AsyncMock(return_value={"declined": fingerprint(NIEUW)})
        assert await mgr.async_setup() == ASK
        mgr._async_raise_issue.assert_not_called()
        dash.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_een_nieuwere_versie_wordt_opnieuw_aangeboden(self, monkeypatch):
        """Afwijzen geldt voor één aanbod, niet voor altijd.

        Anders zet één klik het dashboard voorgoed stil.
        """
        import custom_components.quatt_stooklijn.dashboard as mod

        NOG_NIEUWER = {"views": [{"title": "Overzicht", "cards": [{"type": "gauge"}]}]}
        monkeypatch.setattr(mod, "_load_shipped", lambda: NOG_NIEUWER)
        mgr, _, _ = _manager(EIGEN, None)
        mgr._store.async_load = AsyncMock(return_value={"declined": fingerprint(NIEUW)})
        assert await mgr.async_setup() == ASK
        mgr._async_raise_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_schrijven_wist_een_eerdere_afwijzing(self, monkeypatch):
        """Wat er dan staat komt weer van ons; er valt niets meer af te wijzen."""
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, _, _ = _manager(EIGEN, None)
        mgr._store.async_load = AsyncMock(return_value={"declined": fingerprint(NIEUW)})
        await mgr.async_force_update()
        bewaard = mgr._store.async_save.await_args[0][0]
        assert "declined" not in bewaard
        assert bewaard["fingerprint"] == fingerprint(NIEUW)


class TestReparatiestroom:
    """De flow moet de gebruiker écht een keuze geven."""

    def test_beide_uitkomsten_bestaan_als_stap(self):
        from custom_components.quatt_stooklijn.repairs import (
            DashboardUpdateRepairFlow,
        )

        for stap in ("async_step_init", "async_step_update", "async_step_keep"):
            assert hasattr(DashboardUpdateRepairFlow, stap), f"ontbreekt: {stap}"

    def test_init_toont_een_menu_en_geen_kaal_formulier(self):
        """Een leeg formulier geeft alleen Submit — precies de val die dit
        oploste."""
        import inspect

        from custom_components.quatt_stooklijn.repairs import (
            DashboardUpdateRepairFlow,
        )

        src = inspect.getsource(DashboardUpdateRepairFlow.async_step_init)
        assert "async_show_menu" in src
        assert "update" in src and "keep" in src

    def test_beide_keuzes_sluiten_de_melding_af(self):
        """Met async_abort zou de melding blijven staan; alleen een afgeronde
        flow laat HA het issue opruimen."""
        import inspect

        from custom_components.quatt_stooklijn.repairs import (
            DashboardUpdateRepairFlow,
        )

        for stap in ("async_step_update", "async_step_keep"):
            src = inspect.getsource(getattr(DashboardUpdateRepairFlow, stap))
            assert "async_create_entry" in src, f"{stap} rondt de flow niet af"

    def test_de_menukeuzes_hebben_een_vertaling(self):
        """Zonder vertaling toont HA de kale sleutelnaam als knoptekst."""
        import json
        from pathlib import Path

        basis = Path(__file__).parent.parent / "custom_components" / "quatt_stooklijn"
        bestanden = [basis / "strings.json"] + sorted(
            (basis / "translations").glob("*.json")
        )
        for bestand in bestanden:
            d = json.loads(bestand.read_text(encoding="utf-8"))
            opties = d["issues"]["dashboard_update_available"]["fix_flow"]["step"][
                "init"
            ]["menu_options"]
            assert set(opties) == {"update", "keep"}, f"{bestand.name}: {set(opties)}"
            assert all(v.strip() for v in opties.values()), bestand.name


class TestNieuweGebruiker:
    """Wat er gebeurt bij iemand die nog geen dashboard heeft.

    Dit pad is niet met de hand te testen op een installatie die het dashboard
    al heeft, dus het hoort hier vastgelegd te staan.
    """

    @pytest.mark.asyncio
    async def test_dashboard_wordt_aangemaakt_en_gevuld(self, monkeypatch):
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, collection = _manager(None, None, bestaat=False)
        # Zodra het aangemaakt is, geeft HA het dashboard-object terug — de
        # collectie-listener registreert het nog binnen async_create_item.
        hass_data = mgr._hass.data["lovelace"]

        async def _create(_payload):
            hass_data["dashboards"]["quatt-warmteanalyse"] = dash

        collection.async_create_item = AsyncMock(side_effect=_create)

        assert await mgr.async_setup() == CREATE
        collection.async_create_item.assert_awaited_once()
        dash.async_save.assert_awaited_once_with(NIEUW)
        # En de herkomst staat meteen vast, dus de volgende update gaat vanzelf.
        assert mgr._store.async_save.await_args[0][0]["fingerprint"] == fingerprint(
            NIEUW
        )

    @pytest.mark.asyncio
    async def test_leeg_dashboard_wordt_niet_opnieuw_aangemaakt(self, monkeypatch):
        """Registratie zonder inhoud: alleen vullen, niet nog eens aanmaken.

        async_create_item weigert een tweede registratie op hetzelfde url_path.
        Zonder deze afhandeling blijft zo'n installatie voor altijd op een leeg
        dashboard staan, met enkel een waarschuwing in het log.
        """
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, collection = _manager(None, None)  # bestaat, maar leeg
        from homeassistant.components.lovelace.const import ConfigNotFound

        dash.async_load = AsyncMock(side_effect=ConfigNotFound)

        assert await mgr.async_setup() == CREATE
        collection.async_create_item.assert_not_called()
        dash.async_save.assert_awaited_once_with(NIEUW)

    @pytest.mark.asyncio
    async def test_zonder_lovelace_gebeurt_er_niets_en_crasht_niets(self, monkeypatch):
        """En er wordt niets onthouden, zodat de volgende start het opnieuw
        probeert in plaats van te denken dat het gelukt is."""
        import custom_components.quatt_stooklijn.dashboard as mod

        monkeypatch.setattr(mod, "_load_shipped", lambda: NIEUW)
        mgr, dash, _ = _manager(None, None, bestaat=False)
        mgr._hass.data = {}

        assert await mgr.async_setup() == CREATE
        dash.async_save.assert_not_called()
        mgr._store.async_save.assert_not_called()

    def test_lovelace_staat_als_after_dependency_in_het_manifest(self):
        """Zonder dat kan de integratie geladen worden vóór lovelace, en dan
        valt er niets aan te maken."""
        import json
        from pathlib import Path

        pad = (
            Path(__file__).parent.parent
            / "custom_components"
            / "quatt_stooklijn"
            / "manifest.json"
        )
        manifest = json.loads(pad.read_text(encoding="utf-8"))
        assert "lovelace" in manifest.get("after_dependencies", [])
