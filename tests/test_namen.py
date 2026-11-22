"""Eén naam per stuurbron en per knop, overal dezelfde (ontwerp 22-11-2026).

De interne waarden (``stooklijn``, ``mpc``, ``1r1c``) en de entity-ID's blijven:
daar hangen opgeslagen opties, het dashboard en automations aan. Alleen wat een
gebruiker leest heeft een nieuwe naam.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from custom_components.quatt_stooklijn.const import SOURCE_NAMES

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "quatt_stooklijn"

# De naam per taal; Nederlands is de tabel in const.py zelf.
NAMEN = {
    "nl": SOURCE_NAMES,
    "en": {"stooklijn": "Outdoor temperature", "mpc": "Outdoor temperature + room",
           "1r1c": "House model + room"},
    "de": {"stooklijn": "Außentemperatur", "mpc": "Außentemperatur + Raum",
           "1r1c": "Hausmodell + Raum"},
    "fr": {"stooklijn": "Température extérieure", "mpc": "Température extérieure + pièce",
           "1r1c": "Modèle de maison + pièce"},
}


@pytest.mark.parametrize("taal", list(NAMEN))
def test_keuzelijst_begint_met_de_naam_van_de_bron(taal):
    opties = json.loads((COMP / "translations" / f"{taal}.json").read_text())[
        "selector"]["advice_source"]["options"]
    for bron, naam in NAMEN[taal].items():
        assert opties[bron].startswith(f"{naam} — "), (taal, bron, opties[bron])


def test_strings_json_is_engels():
    opties = json.loads((COMP / "strings.json").read_text())["selector"]["advice_source"]["options"]
    for bron, naam in NAMEN["en"].items():
        assert opties[bron].startswith(f"{naam} — ")


# Entity-ID's mogen dit woord bevatten (…_geluidsniveau_compensatie); tekst niet.
_ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")
OUDE_WOORDEN = ("MPC advies", "MPC fout", "begrenzing", "waterplafond", "compensatie",
                "Compensatie", "Thermisch model", "Stooklijn advies", "huidig model")


@pytest.mark.parametrize("bestand", [
    COMP / "dashboard.yaml",
    COMP / "translations" / "nl.json",
])
def test_geen_oude_namen_in_tekst_voor_gebruikers(bestand):
    tekst = _ENTITY_ID.sub("", bestand.read_text())
    if bestand.suffix == ".json":
        # Sleutels zijn geen tekst voor gebruikers.
        tekst = re.sub(r'"[a-z0-9_]+":', "", tekst)
    for woord in OUDE_WOORDEN:
        assert woord not in tekst, f"{woord!r} staat nog in {bestand.name}"


@pytest.mark.parametrize("slug", [
    "quatt_warmteanalyse_aanbevolen_aanvoertemperatuur",
    "quatt_warmteanalyse_mpc_aanbevolen_aanvoertemperatuur",
    "quatt_warmteanalyse_1r1c_aanbevolen_aanvoertemperatuur",
    "quatt_warmteanalyse_{mode}_fout_aanvoertemperatuur",
    "quatt_warmteanalyse_max_aanvoertemperatuur_instelling",
    "quatt_warmteanalyse_geluidsniveau_compensatie",
])
def test_entity_ids_blijven_vastgepind(slug):
    """De weergavenaam komt uit de vertaling; de entity-ID moet vast blijven,
    anders krijgt een nieuwe installatie een id die het dashboard niet kent."""
    bron = (COMP / "sensor.py").read_text() + (COMP / "switch.py").read_text()
    assert f'"{slug}"' in bron or f'f"{slug}"' in bron
