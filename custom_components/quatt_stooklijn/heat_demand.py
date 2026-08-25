"""De warmtevraag van het huis als koppelvlak naar een externe regelaar.

Sinds `OpenQuatt#503 <https://github.com/OpenQuatt/OpenQuatt/pull/503>`_ kent de
Power House-strategie een externe warmtevraag-ingang. Die vervangt uitsluitend
de **feedforward** — het gemodelleerde ``P_house`` — terwijl de comfortterm, de
verzadigingsclamp, de slew-limiter en de waterbegrenzer in de firmware blijven.
Dat is precies de taakverdeling die deze integratie kan invullen: het huismodel
komt hier uit een jaar meetdata, de regeling blijft waar de veiligheid zit.

**Deze integratie schrijft niets.** Ze publiceert één getal — het benodigde
huisvermogen in W — en de gebruiker wijst de bestaande OpenQuatt-bronhelper
daarnaar. Zo blijft de keten zichtbaar en omkeerbaar: één ``input_text`` leeg
maken zet de installatie terug op haar eigen model. De API-ingang van OpenQuatt
is bewust géén optie; die koppelt aan IP en auth en vervalt na 15 minuten.

Twee dingen worden bewust *niet* van de gepubliceerde waarde afgetrokken:

* **De kamerfout.** De firmware trekt zelf ``Kp · e`` af (bij deze installatie
  3000 W/K). Wie hier al compenseert telt dubbel.
* **De zonnewinst.** Zon warmt de kamer op, en dat ziet de firmware al via
  dezelfde comfortterm. Ook hier zou zelf aftrekken dubbeltellen.

Wat overblijft is het kale huismodel: ``P = UA · (T_balans − T_buiten)``,
begrensd op nul. De firmware kapt zelf af op ``Rated maximum house power``, dus
hier wordt niet op Pr geklemd — dat zou de verzadiging juist onzichtbaar maken.
"""

from __future__ import annotations

from dataclasses import dataclass

# De HA-kant van #503 komt uit het upstream-package ``dynamic-sources.yaml``.
# De proxy is een template-sensor met een vaste unique-ID; de bronhelper is een
# ``input_text`` waarvan de entity-ID uit de YAML-sleutel volgt. Beide staan los
# van een device en krijgen dus géén gebiedsprefix — anders dan de
# ESPHome-entiteiten van de node zelf, waarvoor de naamgebaseerde detectie in
# discovery.py bestaat.
PROXY_UNIQUE_ID = "openquatt_ext_heat_demand"
PROXY_FALLBACK_ENTITY = "sensor.openquatt_ext_heat_demand"
SOURCE_SELECTOR_ENTITY = "input_text.openquatt_source_heat_demand"

# De optie van ``select.… External Heat Demand Source`` waarbij de firmware naar
# de HA-proxy luistert. De andere opties zijn "Disabled" en "API input".
FIRMWARE_SOURCE_HA_INPUT = "HA input"

# De bronhelper accepteert ``entity|attribuut``; alleen het entity-deel telt
# voor de vraag of hij naar ons wijst.
SELECTOR_SEPARATOR = "|"


def selector_entity(raw: str | None) -> str | None:
    """Haal de entity-ID uit de waarde van de bronhelper.

    Leeg, ``unknown`` of ``unavailable`` betekent "niet ingesteld" en geeft
    ``None`` — een lege string zou verderop als een geldige entity-ID kunnen
    passeren.
    """
    if not raw:
        return None
    entity = raw.split(SELECTOR_SEPARATOR, 1)[0].strip()
    if not entity or entity in ("unknown", "unavailable"):
        return None
    return entity


@dataclass(frozen=True)
class HeatDemandLink:
    """Of de gepubliceerde warmtevraag daadwerkelijk de feedforward voedt.

    Alle drie de schakels moeten kloppen: de firmware moet op ``HA input``
    staan, het HA-package moet de proxy leveren, en de bronhelper moet naar
    *deze* sensor wijzen. Eén ontbrekende schakel maakt het verschil tussen
    "de regelaar volgt onze meting" en "de regelaar doet zijn eigen ding" —
    en dat is van buiten niet te zien, want de firmware valt stilletjes en
    correct terug op haar eigen huismodel.
    """

    demand_entity: str
    select_entity: str | None = None
    firmware_source: str | None = None
    proxy_entity: str | None = None
    selector: str | None = None

    @property
    def selector_points_here(self) -> bool:
        return self.selector == self.demand_entity

    @property
    def active(self) -> bool:
        """Voedt deze sensor nu de feedforward van Power House?"""
        return (
            self.firmware_source == FIRMWARE_SOURCE_HA_INPUT
            and self.proxy_entity is not None
            and self.selector_points_here
        )

    @property
    def status(self) -> str:
        """Waar de keten stukloopt, in de volgorde waarin je het oplost."""
        if self.select_entity is None:
            return "OpenQuatt niet gevonden"
        if self.firmware_source is None:
            # Node gevonden, knop nog zonder waarde: dat is een opstartmoment,
            # geen ontbrekende installatie, en het onderscheid bepaalt of je
            # gaat zoeken of gewoon even wacht.
            return "OpenQuatt-keuzeknop nog zonder waarde"
        if self.proxy_entity is None:
            return "HA-package van OpenQuatt niet geïnstalleerd"
        if self.selector is None:
            return f"bronhelper leeg — zet {SOURCE_SELECTOR_ENTITY} op {self.demand_entity}"
        if not self.selector_points_here:
            return f"bronhelper wijst naar {self.selector}"
        if self.firmware_source != FIRMWARE_SOURCE_HA_INPUT:
            return (
                f"firmware staat op '{self.firmware_source}' — "
                f"zet hem op '{FIRMWARE_SOURCE_HA_INPUT}'"
            )
        return "actief"
