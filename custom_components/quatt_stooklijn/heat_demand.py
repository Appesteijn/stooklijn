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

# Wat ``Power House – demand source`` meldt zodra de externe vraag daadwerkelijk
# de feedforward voedt. Alles anders betekent dat de firmware op haar eigen
# huismodel draait.
FEEDFORWARD_EXTERNAL = "external"

# Hoe oud de gebruikte buitentemperatuur mag zijn voordat de vraag niet meer
# gepubliceerd wordt. Gemeten over 24 uur op deze installatie: mediaan 60 s,
# p90 300 s, grootste gat 950 s. Een strakke drempel zou de vraag daarom
# regelmatig ten onrechte intrekken, en elke intrekking duwt de firmware naar
# haar terugvalmodel. Ruim kiezen kost hoogstens dat er even met een verouderde
# temperatuur gerekend wordt — bij ~1 K/uur is dat een paar honderd watt, en de
# comfortterm regelt dat alsnog weg. Vandaar bijna twee keer het grootste
# waargenomen gat.
#
# Dit gat wordt nergens anders opgemerkt: een bevroren bronsensor levert nog
# steeds een geldige waarde, dus de proxy blijft ``valid`` en de firmware ziet
# geen reden om terug te vallen.
OUTDOOR_MAX_AGE_SECONDS = 1800


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
    firmware_feedforward: str | None = None
    proxy_entity: str | None = None
    selector: str | None = None

    @property
    def selector_points_here(self) -> bool:
        return self.selector == self.demand_entity

    @property
    def wired(self) -> bool:
        """Is de keten aan de HA-kant compleet?

        Dit is een *voorspelling*: alle drie de schakels staan goed, dus de
        vraag hoort aan te komen. Of dat ook zo is zegt alleen de firmware —
        zie ``confirmed``.
        """
        return (
            self.firmware_source == FIRMWARE_SOURCE_HA_INPUT
            and self.proxy_entity is not None
            and self.selector_points_here
        )

    @property
    def confirmed(self) -> bool | None:
        """Wat de firmware zelf zegt te gebruiken. ``None`` = geen uitsluitsel.

        Ouder OpenQuatt kent deze diagnostische sensor niet, en dan valt er
        niets te bevestigen. Dat is iets anders dan een ontkenning, dus geen
        ``False``.
        """
        if self.firmware_feedforward is None:
            return None
        return self.firmware_feedforward == FEEDFORWARD_EXTERNAL

    @property
    def active(self) -> bool:
        """Voedt deze sensor nu de feedforward van Power House?

        De firmware heeft het laatste woord. Zegt die niets, dan is de
        voorspelling het beste dat er is.
        """
        if self.confirmed is not None:
            return self.confirmed
        return self.wired

    @property
    def mismatch(self) -> bool:
        """Keten compleet, maar de firmware draait alsnog op haar eigen model.

        Dit is de stille faalmodus waar de hele statuslogica om draait: aan de
        HA-kant lijkt alles goed, en toch komt de vraag niet aan.
        """
        return self.wired and self.confirmed is False

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
        if self.mismatch:
            return (
                "ingesteld, maar de firmware draait op haar eigen huismodel — "
                "ze verwerpt de gepubliceerde waarde"
            )
        if self.confirmed:
            return "actief (bevestigd door de firmware)"
        return "actief"
