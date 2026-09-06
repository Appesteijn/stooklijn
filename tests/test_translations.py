"""Bewaakt dat elke vertaalsleutel in alle talen een naam heeft.

Entiteitsnamen liepen tot v0.10.1 via ``_attr_name``: 28 hardgecodeerde
strings, half Nederlands en half Engels, terwijl de config flow wél in vier
talen bestond. Een Duitse gebruiker kreeg een vertaalde installatiewizard en
daarna een apparaat vol Nederlandse sensornamen.

Nu loopt alles via ``translation_key``. Dat verplaatst het risico: een sleutel
zonder tekst geeft geen foutmelding maar een **naamloze entiteit** — HA valt
terug op de apparaatnaam. Deze test vangt dat af, ook voor de talen die je bij
een volgende sensor makkelijk vergeet.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / "quatt_stooklijn"
LANG_FILES = [COMPONENT / "strings.json"] + sorted(
    (COMPONENT / "translations").glob("*.json")
)

# Domein → platformbestand waarin de sleutels van dat domein staan.
PLATFORMS = {
    "sensor": "sensor.py",
    "binary_sensor": "binary_sensor.py",
    "switch": "switch.py",
    "text": "text.py",
}


def _keys_in_code() -> set[str]:
    """Alle vertaalsleutels die de code gebruikt.

    Via de AST en niet met een regex: de foutsensor kiest zijn sleutel met een
    voorwaardelijke expressie, en een regex pakt dan ook de string uit de
    vergelijking mee (``mode == "mpc"``) — een sleutel die niet bestaat.
    """
    found: set[str] = set()

    def _strings(node) -> set[str]:
        """Stringliteralen die als waarde dienen, niet die in een test staan."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            # De `test` bewust overslaan: daar staan vergelijkingen in.
            return _strings(node.body) | _strings(node.orelse)
        return set()

    for filename in PLATFORMS.values():
        tree = ast.parse((COMPONENT / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    naam = getattr(target, "attr", None) or getattr(target, "id", None)
                    if naam == "_attr_translation_key" and node.value is not None:
                        found |= _strings(node.value)
            elif isinstance(node, ast.keyword) and node.arg == "translation_key":
                found |= _strings(node.value)

    # De spiegelsensoren gebruiken hun slug als sleutel; die komt uit de
    # specificatie zelf, niet uit een literal bij translation_key.
    from custom_components.quatt_stooklijn.sources import MIRROR_SPECS

    found |= {spec.slug for spec in MIRROR_SPECS}
    # QuattDateText krijgt zijn sleutel als argument mee.
    text_src = (COMPONENT / "text.py").read_text()
    found |= set(re.findall(r'CONF_QUATT_START_DATE,\s*"([^"]+)"', text_src))
    return found


def _names_in(path: pathlib.Path) -> set[str]:
    doc = json.loads(path.read_text())
    names: set[str] = set()
    for domain, entries in doc.get("entity", {}).items():
        for key, block in entries.items():
            if block.get("name"):
                names.add(key)
    return names


@pytest.mark.parametrize("path", LANG_FILES, ids=lambda p: p.name)
def test_alle_sleutels_hebben_een_naam(path):
    """Een sleutel zonder tekst levert een naamloze entiteit op, geen fout."""
    ontbreekt = _keys_in_code() - _names_in(path)
    assert not ontbreekt, (
        f"{path.name} mist een naam voor: {sorted(ontbreekt)}"
    )


@pytest.mark.parametrize("path", LANG_FILES, ids=lambda p: p.name)
def test_geen_ongebruikte_namen(path):
    """Achtergebleven namen wijzen op een hernoemde of verwijderde sensor."""
    overbodig = _names_in(path) - _keys_in_code()
    assert not overbodig, (
        f"{path.name} heeft namen zonder sleutel in de code: {sorted(overbodig)}"
    )


def test_alle_talen_dekken_dezelfde_sleutels():
    """Geen taal mag achterlopen op de andere."""
    per_taal = {p.name: _names_in(p) for p in LANG_FILES}
    referentie = per_taal["strings.json"]
    for naam, keys in per_taal.items():
        assert keys == referentie, f"{naam} wijkt af: {sorted(keys ^ referentie)}"


def test_geen_hardgecodeerde_entiteitsnamen_meer():
    """``_attr_name`` omzeilt de vertaling; dat mag niet terugsluipen."""
    overtreders = []
    for filename in PLATFORMS.values():
        src = (COMPONENT / filename).read_text()
        if re.search(r'_attr_name\s*=\s*"', src):
            overtreders.append(filename)
    assert not overtreders, (
        f"hardgecodeerde entiteitsnaam gevonden in: {overtreders}"
    )


def test_elke_entiteit_pint_zijn_entity_id():
    """Een vertaalde naam mag de entity-id niet taalafhankelijk maken.

    Met ``has_entity_name`` leidt HA de entity-id af uit de *vertaalde* naam, en
    dus uit de taal van de gebruiker. Zonder pinnen zou een verse installatie op
    een Engelse HA ``sensor.quatt_warmteanalyse_heat_demand`` krijgen waar een
    Nederlandse ``..._warmtevraag`` krijgt — en het meegeleverde dashboard noemt
    die ids hard. Bestaande installaties merken er niets van (het register houdt
    de id vast), dus dit breekt stil en alleen bij nieuwe gebruikers.
    """
    ongepind = []
    for filename in PLATFORMS.values():
        src = (COMPONENT / filename).read_text()
        for blok in re.split(r"\nclass ", src)[1:]:
            naam = blok.split("(")[0].split(":")[0].strip()
            if naam.endswith("Description"):
                continue  # dataclass, geen entiteit
            benoemd = (
                "_attr_translation_key" in blok
                or "entity_description = description" in blok
            )
            if benoemd and "self.entity_id = async_generate_entity_id" not in blok:
                ongepind.append(f"{filename}:{naam}")
    assert not ongepind, f"entity-id niet gepind in: {ongepind}"
