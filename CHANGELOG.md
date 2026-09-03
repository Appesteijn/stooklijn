# Changelog

Alle noemenswaardige wijzigingen per release, opgebouwd uit de
commitgeschiedenis. Versienummers volgen [SemVer](https://semver.org).

## v0.10.0 — 2026-09-03

Functioneel gelijk aan v0.9.15; wat hier bij komt is opruimwerk. Bewust geen
1.0: v0.9.0 legde vast dat dat nummer gereserveerd blijft voor het
daadwerkelijk aansturen van OpenQuatt met de MPC-regelaar, en daar voldoet
deze release niet aan. De integratie adviseert; beide schrijfpaden
(ch_max_water, geluidsniveau) staan nog standaard uit.

- De geluidsniveau-sensor hing aan een vaste entity-ID van de
  compensatie-switch. Hernoemde je die switch, of draaide je een tweede
  config-entry, dan bleef de sensor stilzwijgend leeg. Switch en sensor
  praten nu via een dispatcher-signaal per config-entry, met de laatste
  waarde in hass.data zodat de opzetvolgorde van de platforms niet uitmaakt.
- Het MPC-advies werd in de compensatielogica op dezelfde manier via een
  vaste entity-ID gelezen; dat loopt nu via de entity registry op unique_id.
- Deze CHANGELOG toegevoegd.

## v0.9.15 — 2026-08-31

Dashboard: twee kaarten die niets konden zeggen

- Grafiek "Rendement per stookdag" verwijderd. De reeks als plaatje voegde
  niets toe aan het getal erboven. De volledige dagreeks blijft als
  stookdagen-attribuut gepubliceerd — hij kost de recorder niets — voor wie
  er zelf een grafiek op wil zetten.
- "Aanvoertemperatuur — Advies vs Werkelijk" hing buiten het stookseizoen
  permanent in een laadspinner. mini-graph-card haalt per entity de historie
  op en blijft wachten als een reeks er geen heeft; onder 30 L/h staan het
  stooklijn- en het MPC-advies bewust op unknown, dus die data kwam nooit.
  De grafiek zit nu in een conditional en wordt vervangen door één regel die
  het actuele debiet noemt en zegt waarom er niets te vergelijken valt.
  De twee condities zijn elkaars exacte tegenhanger, unavailable inbegrepen,
  zodat er tijdens een herstart geen gat valt.

## v0.9.14 — 2026-08-31

Rendement: de maat bruikbaar maken voor waar hij voor bedoeld is

- Norm bevroren: de beoordeelde stookdagen zitten niet meer in hun eigen
  referentie. Zonder dit schoof een echte verbetering de mediaan van haar
  eigen temperatuurbin mee omhoog en las ze na een seizoen weer als 1,00.
- Bins per seizoenshelft. Op 226 echte stookdagen scheelde de norm bij
  9-15 graden 12 tot 21% tussen najaar en voorjaar; die bias zat eerder
  volledig in het getal en werd met drie alinea's disclaimer weggeschreven.
- Nieuwe optie: wijzigingsdatum. Bevriest de norm op alles ervoor en toetst
  alle dagen erna daaraan, met een tweesteekproefstoets op de gemeten
  dagspreiding en een minimum van 7 stookdagen.
- Alleen dagen uit dezelfde periode van het jaar doen mee aan dat oordeel.
  Februari tegen mei meet de seizoensstaart en niet je aanpassing.
- Losse dag en 7-daags gemiddelde uit kaart en attributen: een getal tonen
  met "dit betekent niets" eronder is netto negatief.

Herverdeling verwijderd

- De kaart kon alleen melden dat er niets werd aangestuurd, en de gamma-knop
  deed op elke stand hetzelfde. Sensor, optie en kaart weg; de rekenkern in
  analysis/demand_shift.py blijft staan voor als het ooit echt gaat sturen.
- Vervallen entity wordt bij het laden uit het register opgeruimd.

Vooruitblik van 6 naar 12 uur

- simulate_6h klemde de horizon hard op 6, ook bij een langere forecast.
  Heet nu simulate_forward en volgt de aangeleverde arrays.
- MPC_FORECAST_HOURS gelijkgetrokken met COAST_MAX_HOURS, zodat de
  vooruitblik en de uitlooptijd naast elkaar te leggen zijn.
- Attribuut forecast_6h heet nu forecast, met forecast_uren ernaast, en
  gaat niet langer bij elke state-write de recorder in.

## v0.9.13 — 2026-08-30

De gamma-scan uit v0.9.12 sloeg op hol buiten het stookseizoen. Bij een
buitentemperatuur boven het nulpunt is de warmtevraag nul, levert elke gamma
exact nul op, en wint bij gelijke opbrengst de laagste — dus verfijnde hij rond
0,5 en kwam er een tabel van veertien nullen uit. De kaart viel daarbij terug op
de verkeerde verklaring: dat geen enkele gamma iets oplevert zonder dat het
firmwareplafond of de comfortgrens ingrijpt, terwijl er niets begrensd was.

scan_gamma() doet nu eerst een nulmeting en geeft een lege scan terug als er geen
vraag in het venster zit. De kaart zegt dan wat er aan de hand is: geen
warmtevraag, dus niets te verschuiven. De andere melding blijft staan voor het
geval waar hij wél hoort.

Geen enkele test ving dit, want die draaien allemaal op een winterdag. Er staan
er nu twee bij: de zomer-uitstap zelf, en de tegenproef dat een dag met vraag nog
steeds een scan oplevert — zodat die uitstap niet stilletjes alles uitschakelt.

Gevonden op de live data direct na het installeren van v0.9.12.

## v0.9.12 — 2026-08-30

Gamma was tot nu toe een knop zonder schaalverdeling: je zette hem op een getal
en wachtte een maand om te zien of het iets deed. scan_gamma() rekent het hele
bereik door — grof aan de uiteinden, in stappen van 0,1 rond het optimum — en
zet per waarde de besparing, de kamerdrift en de begrenzingen naast elkaar.

Een punt telt alleen als kandidaat wanneer noch het firmwareplafond noch de
driftlimiter ingrijpt. Daarboven is de berekende besparing geen voorspelling
meer maar een uitkomst van de begrenzing. Het advies is vervolgens niet de
hoogste opbrengst maar de laagste gamma die daar nog bij in de buurt komt:
gamma kost comfort, en een half procentpunt is die extra drift niet waard.

De herverdelingskaart toont dat als tabel, met de grove stappen plus de
adviesregel. De laatste kolom blijft leeg zolang er niets aan de hand is en
noemt anders de reden.

Verder in deze release:

- De compressorstarts-sensor schreef zijn opslag weg bij elke state-change van
  de bron. Met een bron die de modulatie meldt zijn dat tien tot twintig
  identieke schrijfacties per beurt. Nu alleen nog als een beurt opent of sluit.
- De grafiek "Verhouding tegen buitentemperatuur" toonde epoch-milliseconden op
  de x-as en één punt. apexcharts-card houdt hoe dan ook een datetime-as aan;
  temperatuur wordt nu als pseudo-tijdstip gecodeerd, zoals de andere
  spreidingsgrafieken in dit dashboard al deden.
- De forecast wordt nog één keer per update opgebouwd in plaats van bij elke
  aanroep. Zonder dat zou de scan hem vijftien keer per state-write bouwen.
- De rendementskaart legt nu uit dat de norm je eigen verleden is en geen
  fabrieksopgave, wat je met het getal doet, en waarom voorjaarsdagen onder 1
  uitkomen zonder dat er iets mis is. De zin "de norm bindt op
  buitentemperatuur, niet op seizoen" is vervangen door een voorbeeld.

## v0.9.11 — 2026-08-29

De kaart "Verhouding tegen buitentemperatuur" viel uit met een Configuration
error. Op series-niveau stond type: scatter, en apexcharts-card 2.2.3 kent daar
alleen line, column en area. Hersteld naar het patroon van v0.2.38: type: line
met stroke_width nul, punten zonder lijn, scatter via de chart-opties.

Dit ging nu voor de derde keer mis — v0.2.37 draaide een eerdere poging terug,
v0.2.38 zette het patroon neer en 9dde328 zette er alsnog weer scatter in. De
bestaande tests konden het niet zien: een kaart die apexcharts weigert is nog
steeds geldige YAML. Er staat nu een test op die elk series-type in het
uitgeleverde dashboard toetst.

## v0.9.10 — 2026-08-29

Compressorstarts telde hp2 niet betrouwbaar mee. De rol stond niet in de
SourceRegistry, waardoor hp2 buiten de state-change listener viel en alleen op de
vijf-minutentick binnenkwam: looptijden te kort, starts te laat gestempeld en
korte beurten volledig gemist.

Beurtgrenzen komen nu van de last_changed van de bron in plaats van de eigen
klok, zodat een late melding de beurt niet meer inkort. Databronnen telt een rol
zonder kandidaten niet langer als gat.

- Compressorstarts: hp2 ook echt bewaken, en beurten op de meettijd stempelen

## v0.9.9 — 2026-08-29

Dashboard: leeg dashboard wordt gevuld in plaats van opnieuw aangemaakt, wat
anders bij een halve aanmaak voor altijd zou blijven mislukken (3ad09f2).

Rendement: het venster achter de verhouding heet nu wat het is — de laatste 30
stookdagen, met datumbereik en een melding buiten het stookseizoen. Twee
grafieken erbij die tonen wat een enkel getal niet kan: de reeks per stookdag
over de tijd en de verhouding tegen buitentemperatuur. De seizoensbeperking van
de maat staat nu expliciet op de kaart en in de README (9dde328).

Tests: 578 groen.

- Rendement: het venster eerlijk benoemen en de reeks tonen
- Leeg dashboard opnieuw vullen in plaats van opnieuw aanmaken

## v0.9.8 — 2026-08-29

Compressorstarts tellen — kortcyclen zichtbaar maken.

Op verzoek van een gebruiker: laat zien hoe vaak de Quatt (her)start. Veel starts
terwijl het buiten niet warm is, wijst op een te hoge stooklijn. Dat klopt, en
het was een gat: een warmtepomp die niet lager kan moduleren dan haar minimum
heeft maar één manier om minder warmte te leveren, namelijk uit- en weer
aanslaan. Een te hoge stooklijn is daardoor niet zichtbaar als een hoge
aanvoertemperatuur maar als veel korte draaibeurten.

Zie b9208d2 voor de opzet en b5c9660 voor de correctie op het duo-gedrag.

Tests: 568 groen.

- Compressorstarts: beide warmtepompen tellen, niet alleen hp1
- Compressorstarts tellen — kortcyclen zichtbaar maken

## v0.9.7 — 2026-08-29

De reparatiemelding gaf maar één uitweg: overschrijven.

Een melding met is_fixable opent meteen de flow, en die toonde een
bevestigingsformulier met een leeg schema. Daarmee is Submit de enige knop, en
Submit vervangt het dashboard. Wie zijn eigen versie wilde houden kon alleen het
dialoog wegklikken — en dan bleef het issue staan en kwam het bij de volgende
herstart gewoon terug. "Nee" was daarmee geen antwoord maar uitstel, precies het
tegenovergestelde van wat v0.9.4 beloofde.

Nu een menu met twee echte uitkomsten:

* het nieuwe dashboard overnemen, met de waarschuwing dat eigen aanpassingen
  verloren gaan;
* het eigen dashboard houden en er niet meer naar gevraagd worden.

Beide ronden de flow af met async_create_entry, want alleen dan ruimt HA het
issue op; async_abort laat het staan.

Afwijzen wordt onthouden op de vingerafdruk van de meegeleverde versie, niet als
losse vlag. Komt er later een nieuwere versie, dan is dat een nieuw aanbod en
wordt het opnieuw gevraagd — anders zet één klik het dashboard voorgoed stil.
Schrijven we het dashboard later alsnog, dan vervalt de afwijzing: wat er dan
staat komt weer van ons.

Tests: 526 groen (+8), waaronder de vraag die dit aan het licht bracht — komt de
melding na afwijzen terug? Beide bewakingen geverifieerd door het oude gedrag
terug te zetten en te controleren dat de juiste test omvalt.

## v0.9.6 — 2026-08-29

Manifest-sleutelvolgorde herstellen.

v0.9.5 loste de ontbrekende lovelace-afhankelijkheid op maar viel op het
volgende hassfest-punt: sleutels horen domain, name, daarna alfabetisch te
staan. De rest van het manifest stond al goed; after_dependencies erbij zetten
op de plek waar het logisch leek — naast dependencies — brak precies dat.

Nu op de alfabetische plek, meteen na name.

De test uit v0.9.5 controleerde alleen of de afhankelijkheden gedeclareerd
waren, niet hoe. Daar is de volgordecontrole aan toegevoegd, zodat de hele
manifest-validatie van hassfest lokaal gedekt is in plaats van per fout één
release. Geverifieerd door het manifest terug te zetten naar de v0.9.5-staat en
te controleren dat de juiste test omvalt.

Tests: 518 groen (+1). Geen gedragswijziging.

## v0.9.5 — 2026-08-29

Hassfest repareren: lovelace als after_dependency.

v0.9.4 viel om in CI. dashboard.py importeert ConfigNotFound uit
homeassistant.components.lovelace, en daarmee werd de afhankelijkheid op
lovelace voor het eerst zichtbaar voor de statische controle. Tot dan toe raakte
de code lovelace alleen aan via de string hass.data["lovelace"], en dat ziet
hassfest niet.

after_dependencies en niet dependencies: zonder lovelace draait de integratie
prima, er wordt dan alleen geen dashboard aangemaakt. Als harde dependency zou
de integratie helemaal niet meer laden op zo'n installatie.

Puur een manifest-kwestie; er verandert niets aan het gedrag. v0.9.4 draait, hij
faalt alleen de validatie.

Daarnaast een test die dit voortaan lokaal vangt. Hassfest draait pas in CI — na
de push, na de tag — en dat is te laat om het nog netjes op te lossen. De test
loopt alle .py-bestanden langs op imports uit homeassistant.components en eist
dat elke component in dependencies of after_dependencies staat. Platforms die we
zelf leveren tellen niet mee: daarvoor is het bestaan van sensor.py het bewijs,
niet een regel in het manifest. Geverifieerd door het manifest terug te zetten
naar de v0.9.4-staat en te controleren dat beide tests omvallen.

Tests: 517 groen (+2).

## v0.9.4 — 2026-08-29

Het meegeleverde dashboard blijft nu meegroeien.

Tot nu toe maakte de integratie het dashboard één keer aan en raakte het daarna
nooit meer aan: async_setup_dashboard begon met `if _DASHBOARD_URL in dashboards:
return`. Dat was veilig — niemands handwerk ging verloren — maar het betekende
ook dat elke dashboardverbetering alleen nieuwe gebruikers bereikte. Wie de
integratie al draaide bleef zitten met de versie van zijn eerste installatie,
zonder melding en zonder zichtbaar verschil. De v0.9.3-wijzigingen aan de
forecast-tabel en de kamerdrift kwamen bij niemand aan.

Altijd overschrijven kan niet: het is een gewoon opslag-dashboard, dus eigen
kaarten staan in hetzelfde bestand. De oplossing is weten of er iets te
overschrijven valt. Bij elke schrijfactie bewaren we nu een vingerafdruk van wat
we wegschreven; bij de volgende start vergelijken we die met wat er staat.

* gelijk  -> onaangeraakt, dus stil bijwerken;
* anders  -> aangepast, dus afblijven en het vragen via een reparatiemelding.

Bestaande installaties hebben nog geen vingerafdruk. Die vallen bewust in de
tweede categorie: onbekende herkomst telt als aangepast. Zou dat andersom staan,
dan overschrijft één update in één klap alle bestaande dashboards. Wie toevallig
al de meegeleverde versie draait, krijgt de herkomst gratis vastgelegd zonder dat
er iets geschreven wordt — dat is de route waarlangs bestaande installaties uit
het onbekend-gat klimmen.

De reparatiemelding waarschuwt expliciet dat bevestigen eigen aanpassingen kost;
negeren houdt het dashboard zoals het is. Daarnaast is er een service
quatt_stooklijn.update_dashboard voor wie er zelf om vraagt.

Verder rechtgezet: hass.data["lovelace"] was vroeger een dict en is later een
dataclass geworden. De oude code deed alleen getattr en deed op de dict-vorm dus
stilletjes niets. Beide vormen worden nu ondersteund.

De README beschreef nog de handmatige import; dat klopte al niet meer.

Tests: 515 groen (+20). De beslislogica staat los van Home Assistant en is
rechtstreeks getest; beide kritieke bewakingen zijn geverifieerd door ze
opzettelijk te breken en te controleren dat de juiste tests omvallen.

## v0.9.3 — 2026-08-28

Weersverwachting: herpogingen na het opstarten.

De eerste ophaalpoging valt in async_added_to_hass, en op dat moment is de
weather-integratie er soms nog niet. De fout werd op debug-niveau weggeslikt,
dus onzichtbaar, en de forecast bleef tot de volgende uurlijkse tik leeg. De
MPC-tabel toonde dan een uur lang voor elk uur dezelfde huidige buitentemperatuur
(condition: current), en de herverdeling kan op een vlakke reeks per definitie
niets vinden.

Nu drie herpogingen op 30 s, 2 min en 5 min, en een warning zodra het daarna nog
steeds niet lukt — met de naam van de weather-entity erbij. Herstelt hij later
alsnog, dan volgt een info-melding.

Daarnaast liet een mislukte poging self._forecast = [] achter, waardoor een
tijdelijke storing een goede verwachting weggooide. De vorige verwachting blijft
nu staan.

Geen aanvoeradvies zonder warmtevraag.

De MPC-sensor klemt zijn advies op MPC_SUPPLY_TEMP_MIN (20 graden). Bij nul
warmtevraag kwam daar de retourtemperatuur uit, die vervolgens naar 20 werd
opgetild — een advies van 20 graden terwijl er niets te adviseren viel, en
zichtbaar naast een stooklijnadvies van 17,2. Beide adviessensoren geven nu
niets terug als de warmtevraag nul is, net zoals de foutsensoren zwijgen bij
stilstand. ChMaxWater slaat een ontbrekende bronwaarde al netjes over.

Twee tests legden het oude gedrag vast en zijn omgeschreven naar het nieuwe
contract, met een extra test op de grens: netto vraag net boven nul geeft nog
wel een advies.

Dashboard: buitentemperatuur in de 6-uurs forecast op één decimaal (was
16.2700004577637), en de voorspelde kamerdrift toont een streepje in plaats van
None wanneer er niet verschoven wordt.

Tests: 495 groen.

## v0.9.2 — 2026-08-28

Fix: warmtevraag_verschoven bleef unknown.

De schaduwsensor riep self._mpc.model aan; de property heet thermal_model.
Elke state-write gaf een AttributeError, dus de sensor kwam nooit verder dan
unknown. Ook de type-annotatie wees naar een niet-bestaande klassenaam.

De tests in 0.9.1 vingen dit niet. Ze controleerden de broncode als tekst
(assert "async_generate_entity_id" in src) en zo'n check ziet een verkeerde
attribuutnaam per definitie nooit. Er staan nu drie tests bij die de property
daadwerkelijk uitvoeren op een stub-object; teruggezet naar de foute naam vallen
er twee om.

Dashboard: kaart "Rendement & herverdeling" op de MPC-tab. Toont de
weerneutrale rendementsmaat met haar 7- en 30-daags gemiddelde en het aantal
stookdagen waaruit de norm is gebouwd, plus de verwachte besparing van de
herverdeling, het venster, de voorspelde kamerdrift en het aantal uren boven het
firmwareplafond. Waarschuwt apart wanneer de driftbegrenzing ingrijpt of wanneer
de verdeling boven Rated maximum house power uitkomt.

Bij gamma=0 legt de kaart uit dat de herverdeling uit staat en dat er niets mee
wordt aangestuurd.

Tests: 494 groen.

## v0.9.1 — 2026-08-28

Twee meetsensoren richting 1.0 — beide read-only, niets wordt aangestuurd.

sensor.quatt_warmteanalyse_cop_prestatie
  Rendement genormaliseerd op buitentemperatuur. De kale dag-COP volgt vooral
  het weer (op deze installatie 1,85 bij -5 graden en 4,55 bij +13) en is
  daarmee onbruikbaar om een regelwijziging aan te toetsen. Deze sensor deelt
  de gemeten dag-COP door wat de installatie bij die temperatuur normaal
  presteerde. Referentiecurve is een gebinde mediaan, geen regressie: de curve
  knikt af boven 13 graden en een kapotte dag mag de norm niet verschuiven.
  State is het 30-daags gemiddelde; met ~12 procent dagruis zou een losse dag
  een trend suggereren die er niet is.

sensor.quatt_warmteanalyse_warmtevraag_verschoven
  Schaduwsensor: dezelfde warmtevraag, herverdeeld naar de uren met de beste
  COP. Nergens aan gekoppeld. Harde voorwaarde is energie-neutraliteit; alleen
  uren met werkelijke vraag doen mee, anders zou er gestookt worden boven de
  eigen stookgrens. De weging gebruikt uitsluitend buitentemperatuur-forecast
  en COP-curve — zon en kamerfout blijven bij de firmware, meewegen zou
  dubbeltellen.

  Comfort wordt op drie lagen bewaakt: energie-neutraliteit, een driftschatting
  uit de geleerde thermische massa die de verschuiving evenredig terugschaalt
  bij meer dan 0,3 K, en de comfortterm van de firmware zelf.

Nieuwe optie demand_shift_gamma (0-3, standaard 0). Bij 0 is de verschoven
reeks per definitie identiek aan de gewone.

DEMAND_SHIFT_HOURS = 24 staat bewust los van MPC_FORECAST_HOURS. De winst hangt
aan de dagzwaai binnen het venster — gemeten 0,09 procent bij 6 uur tegen 6,65
bij 24 — maar MPC_FORECAST_HOURS verhogen zou de displayforecast meevergroten
en het attribuut viervoudigen in de recorder.

Gemeten op 20 januari 2026: bij gamma=1 een besparing van 1,17 procent en een
drift van 0,16 K. Een eerdere schatting van 38 procent was fout; dat was het
COP-verschil tussen twee temperaturen, niet wat een energie-neutrale
herverdeling oplevert.

Tests: 491 groen (was 439). De normalisatie en het nul-uren-masker zijn
geverifieerd door ze te slopen en te zien welke tests omvallen.

## v0.9.0 — 2026-08-28

Minor bump in plaats van patch: substantiele nieuwe functionaliteit sinds
0.8.0, zonder breaking changes.

Breaking-change analyse over v0.8.0..HEAD:
- Config entry VERSION blijft 1, geen migratie nodig.
- Opslag-schema's ongewijzigd: insights_cache v1, thermal_model v2 sinds
  v0.4.1. Geen dataverlies bij upgrade.
- Geen enkele unique_id verwijderd of hernoemd; vijf toegevoegd
  (coast_time_min, heat_demand, power_house_calibration, source_overview,
  source_<rol>). De diff bevat uitsluitend toevoegingen.
- Geen CONF_*-key verwijderd, dus opgeslagen configuratie blijft geldig.
  De verwijderde DEFAULT_*_ENTITY-constanten waren interne fallbacks die
  nooit in een config entry stonden.
- Geen services verwijderd, manifest ongewijgd op requirements en
  dependencies, HACS-minimum nog HA 2024.1.0.
- Geen deprecation-waarschuwingen uit deze integratie op HA 2026.8.3.

Een gedragswijziging om te vermelden: sinds v0.8.22 worden de config-velden
indoor_temp_entity, boiler_heat_entity en power_input_entity daadwerkelijk
opgevolgd. Die konden eerder worden ingevuld terwijl de bronregistratie ze
negeerde. Een blijven staande dode instelling maakt niets stuk -
TestBestaandeConfiguraties legt vast dat niet-bestaande en unavailable
entiteiten worden overgeslagen.

1.0 blijft gereserveerd voor het daadwerkelijk aansturen van OpenQuatt met
de MPC-regelaar. Tot dan adviseert de integratie; de twee schrijfpaden
(ch_max_water, geluidsniveau) staan standaard uit.

## v0.8.24 — 2026-08-28

Documentatie: README herzien voor nieuwe gebruikers.

Feitelijke fouten gecorrigeerd:
- "Adapting the dashboard to your setup" beschreef drie tabellen met
  entity-IDs die je zou moeten nalopen en een find-and-replace. Het
  dashboard verwijst inmiddels uitsluitend naar de quatt_warmteanalyse-
  spiegels plus number.cic_max_water_temperature; er valt niets te
  vervangen. Sectie teruggebracht tot de twee werkelijke uitzonderingen.
- sensor.quatt_warmteanalyse_quatt_advies bestaat niet (_parameters).
- Twee verwijzingen naar een "Shadow Validatie"-tab die er niet is.
- OpenQuatt-links wezen naar een lege org-pagina en naar een repo die
  niet bestaat; beide nu OpenQuatt/OpenQuatt.
- "four data sources" terwijl de tabel eronder er vijf noemt.
- Knie-store en insights-cache stonden op 3 jaar retentie; beide zijn
  KNEE_YEARS_TO_KEEP = 100, effectief nooit gepurged.
- Vijfde dashboardtab heet Systeem, niet Geluid.

Structuur omgedraaid. Installatie stond op regel 231, achter tweehonderd
regels diepe referentie over MPC-interne werking en Power House-
feedforward. Nu: Terminology, Features, Requirements, Installation,
Configuration, Data sources, First run, Usage, Sensors, Services, daarna
pas de verdieping. Alle achttien bestaande secties behouden.

Twee nieuwe secties:
- Terminology — de Nederlandse vaktermen (stooklijn, stookgrens,
  knikpunt, nominaal vermogen) die de hele README gebruikt maar nergens
  uitlegde.
- First run — wat wanneer beschikbaar komt, en expliciet dat 'unknown'
  op de foutsensoren bij stilstand en een lege bronnentabel vlak na een
  herstart geen fouten zijn.

Screenshots van de Overzicht- en MPC-tab toegevoegd; het dashboard was
tot nu toe alleen in tekst beschreven.

Vertaling van quatt_cloud_enabled aangescherpt: de knop is vooral zinvol
met een tweede lokale bron, omdat je anders per rol één kandidaat
overhoudt zonder terugval.

## v0.8.23 — 2026-08-28

Quatt cloud-API optioneel maken.

- Nieuwe optie quatt_cloud_enabled (standaard aan, bestaande installaties
  merken niets). Uit betekent geen API-aanroepen, niet geen cache: het
  recente venster wordt op dezelfde manier als het historische venster
  cache-only gelezen, zodat de opgebouwde insights-cache (retentie 100
  jaar) gewoon blijft meedoen aan de analyse.
- De recorder-tak neemt nieuwe dagen over. Die leest long-term statistics
  (statistics_during_period, period="day"), niet ruwe states, en is dus
  niet onderhevig aan purge_keep_days. Hij berekent alle kolommen zelf,
  averageCOP incluis, uit de energietotalen.
- De knie-oogst raakt de cloud nergens: die loopt via de kandidatenlijsten
  en de recorder, en de backfill werkt met de cloud uit door op de
  bestaande cache.

Dashboard: de Databronnen-kaart meldt wanneer de cloud uit staat, met de
omvang en einddatum van de cache, plus een aparte regel zodra die meer dan
een dag achterloopt (een dag achterstand is normaal, vandaag is nog niet
compleet).

Tests: 439 groen. test_cloud_toggle bewaakt dat "cloud uit" wel de API
overslaat maar het cache-venster niet, want dat laatste zou de historie
alsnog weggooien. Daarnaast vier upgradetests bij test_sources voor het
gedrag van 0.8.22 op bestaande configuraties: een blijven staande, dode
instelling mag geen meting stukmaken.

## v0.8.22 — 2026-08-27

Bronkeuze per meting instelbaar.

- Drie ontbrekende config-sleutels toegevoegd: control_setpoint_entity,
  room_setpoint_entity en cop_entity. Die rollen waren principieel niet
  te kiezen, dus won de detectievolgorde (Quatt vóór OpenQuatt) altijd.
- ROLE_CONF_KEYS als enige koppeltabel rol -> config-sleutel. De kopie in
  SourceRegistry._configured_for kende maar 5 van de 11 rollen, waardoor
  het optiescherm indoor_temp, boiler_heat en power_input wel aanbood maar
  de bronregistratie ze negeerde.
- Optiescherm stelt de actieve bron voor in plaats van de Quatt-detectie.
  Staat de Quatt-sensor op 'unknown', dan is de registratie al doorgeschoven
  naar OpenQuatt; het formulier suggereerde dan een dode entity.
- power_input en boiler_heat lopen mee via de coordinator, zodat de
  historische analyse de ingestelde bron gebruikt in plaats van altijd de
  Quatt-sensor.

Dashboard: Databronnen-tabel toonde alle 11 rijen op een regel. De YAML
folded scalar (>) vouwt losse regelovergangen tot spaties, en de for-loop
miste de blanco regels die de andere tabellen wel hebben. Ook de blockquote
eronder liep mis ("leveren > niets").

Tests: 431 groen. Nieuwe volledigheidsguard faalt zodra een rol geen
config-sleutel heeft of het optiescherm hem niet aanbiedt.

## v0.8.21 — 2026-08-25

Stookgrens wordt geadviseerd zodra de meting er iets over te zeggen heeft

De kalibratiesensor liet T0 bewust met rust: boven de stookgrens wordt niet
gestookt, dus de regressie heeft daar geen data en het nulpunt is
extrapolatie. Dat bezwaar klopt nog steeds, maar het zegt alleen dat we het
nulpunt niet nauwkeurig kénnen — niet dat elke waarde even goed is.

Wat er wél uit de meting volgt is wat een verkeerd nulpunt kost. De
feedforward vraagt dan overal UA·(T_balans − T0) watt te weinig, elke
stookdag opnieuw. Bij deze woning was dat 189 W bij een stookgrens van 16,0,
en dat is niet aan de meetdagen boven de stookgrens af te lezen maar aan de
systematische afwijking van de dagen eronder. Zolang de sensor "model is
gekalibreerd" meldde terwijl die 189 W erin zat, was de melding misleidend.

De afwijking wordt nu gepubliceerd als stookgrens_afwijking_w, en zodra hij
groter is dan wat één knopstap kan corrigeren wordt het gemeten balanspunt
geadviseerd. Tc en Pr worden dan tegen dat nieuwe nulpunt gerekend, want
anders levert het advies een drietal op dat onderling niet klopt.

De drempel is wat het geheel bruikbaar maakt in plaats van drammerig: binnen
één stap zwijgt het advies, dus het duwt T0 één keer naar de meting en houdt
daar op. Wiskundig is dit hetzelfde als het balanspunt adviseren; het
verschil zit in wanneer er iets gezegd wordt, en in dat de afwijking
meegepubliceerd wordt zodat de extrapolatie zelf te wegen is.

Zes tests legden het oude gedrag vast en zijn herschreven naar de nieuwe
bedoeling. Eén ervan verdient aandacht: dat Tc en Pr de T0 van de regelaar
volgen geldt nu alleen binnen de band waarin niet geadviseerd wordt —
daarbuiten horen ze juist bij het geadviseerde nulpunt, en daar is een eigen
test voor bijgekomen.

## v0.8.20 — 2026-08-25

Testdekking voor de warmtevraag, met installaties zonder OpenQuatt of Quatt

De coverage wees uit dat de klasse zelf grotendeels ongetest was: het
testharnas bouwt de sensor met __new__ en overschrijft de bron, dus
async_added_to_hass draaide nooit. Daarmee had de hartslag uit v0.8.19 —
de fix voor de subtielste fout van deze ronde — geen enkele test. De
CoordinatorEntity-stub kreeg een lege async_added_to_hass zodat een entity
zijn eigen registratie kan draaien, en de hartslag is nu vastgelegd op
zowel zijn bestaan als zijn interval ten opzichte van de leeftijdsgrens.

Twee configuraties die makkelijk buiten beeld vallen zijn nu expliciet
gedekt in de interlock. Een kale CiC zonder OpenQuatt: daar is deze
schrijfroute de enige aansturing die de integratie heeft, dus onterecht
terugtreden legt haar stil zonder dat er iets voor in de plaats komt. En
OpenQuatt zonder de Quatt-integratie, waar geen enkele sensor.heatpump_*
bestaat — inclusief het omschakelmoment waarop de koppeling wél gaat lopen.

Verder vastgepind dat de sensor zijn buitentemperatuur op rol opvraagt in
plaats van op naam. Dat is precies wat hem voor beide installaties laat
werken, en een hardgecodeerde entity-ID zou het voor de helft van de
gebruikers stil breken.

Randgevallen erbij: een warmteverliesgetal van nul, een ontbrekend
balanspunt zonder regelaar, een niet-numerieke buitentemperatuur, en de
attributen vóór de eerste analyse — die leest het dashboard namelijk ook.

heat_demand.py staat op 100%, discovery.py op 99%, en de warmtevraag-sensor
heeft geen ongedekte regels meer.

## v0.8.19 — 2026-08-25

Vijf bevindingen uit de code review op v0.8.18

**Een vreemde externe vraag markeerde onze koppeling als actief.** `active`
volgde de firmware zodra die iets zei, ook als de keten aan onze kant niet af
was. Maar `demand source = external` zegt dát er een externe vraag stuurt,
niet van wie. Stond de bronhelper nog op een testwaarde, dan trad de
ch_max_water-route terug terwijl onze vraag helemaal niet aankwam, en meldde
het dashboard een koppeling die de statustekst er direct naast ontkende.
Beide kanten moeten nu kloppen: de keten compleet, en de firmware die niet
tegenspreekt.

**De versheidscontrole kon niet afgaan.** De sensor herrekende alleen op een
state-change van de buitentemperatuur, en de coordinator ververst enkel op
verzoek — dus juist een bevroren bron gaf geen enkele trigger. De controle
was inert in precies het scenario waarvoor ze bestond. Er is nu een hartslag
van vijf minuten, ruim onder de grens van dertig.

**De interlock keek niet of er wel een vraag was.** Zonder analysedata, of met
een te oude bronmeting, publiceert de sensor niets. Terugtreden legde dan
beide routes tegelijk stil. De waarde telt nu mee.

Verder: `native_value` liep buiten de detectiecache om en scande het hele
entity-register opnieuw, wat de optimalisatie tien regels lager ongedaan
maakte — er is nu één kortstondig geheugen dat beide uitleespaden deelt. En
de melding over een bevroren bron kwam drie keer per state-write in het log,
nu één keer per keer dat het gebeurt.

De eerste bevinding legde ook een gat in de tests bloot: er stond wel een
test voor een vreemde externe vraag, maar die toetste alles behalve `active`.

## v0.8.18 — 2026-08-25

Warmtevraag rekent vanaf de stookgrens van de regelaar

De sensor gebruikte het gemeten balanspunt (16,66) terwijl de regelaar op
16,0 staat. power_house.py betoogt al uitgebreid dat de regressie het
balanspunt niet kan zien — boven de stookgrens wordt niet gestookt, dus die
dagen vallen uit de fit, en de warmste waarneming ligt anderhalve graad
onder het nulpunt dat de fit eruit haalt. De kalibratiesensor neemt de
stookgrens daarom al over; deze sensor deed dat niet, en dan publiceert
dezelfde integratie twee huismodellen over hetzelfde huis.

Het verschil is niet alleen cosmetisch. In het bandje tussen beide nulpunten
vroeg de sensor tot 188 W terwijl het firmware-model daar 0 staat: als
externe vraag zou dat de installatie boven haar eigen stookgrens laten
stoken. Zonder regelaar blijft de meting het nulpunt, want dan is er geen
ander.

Koppelstatus is bevestigd in plaats van voorspeld

Dat de koppeling actief is werd afgeleid uit drie voorwaarden aan de
HA-kant. De firmware publiceert het antwoord zelf via Power House – demand
source, en die krijgt nu het laatste woord. Zo komt de stille faalmodus in
beeld waar alle drie de schakels goed staan terwijl de firmware de waarde
alsnog verwerpt en op haar eigen model draait. Kent de firmware die sensor
niet, dan blijft de voorspelling gelden — geen uitsluitsel is iets anders
dan een ontkenning. P_house is bewust niet gebruikt: die toont altijd de
gemodelleerde waarde, ook terwijl een externe vraag stuurt.

Bevroren buitentemperatuur wordt opgemerkt

Het bestaande vervalgedrag dekt alleen het geval dat onze sensor wegvalt.
Een bronsensor die blijft hangen levert nog steeds een geldig getal, dus de
proxy blijft valid en de firmware ziet geen reden om terug te vallen. De
grens ligt op 30 minuten: over 24 uur meet deze installatie een mediaan gat
van 60 s, p90 300 s en een grootste gat van 950 s, dus een strakke drempel
zou de vraag routinematig intrekken en de firmware telkens naar haar
terugvalmodel duwen. Te ruim kost hoogstens een paar honderd watt afwijking
die de comfortterm wegregelt.

## v0.8.17 — 2026-08-25

Koppelinstructie alleen tonen waar er iets te koppelen valt

De warmtevraag-kaart drong bij elke niet-actieve koppeling aan op het
omzetten van een OpenQuatt-keuzeknop. Voor een installatie met alleen een
CiC is dat een aansporing om iets te doen wat niet kan: de knop bestaat
daar niet, en de kaart zei er in dezelfde adem bij dat OpenQuatt niet
gevonden was.

De sensor draagt nu de gevonden keuzeknop als attribuut, en het dashboard
hangt de instructie daaraan op in plaats van aan de statustekst — een
mensleesbare string is geen conditie om logica op te bouwen. Zonder
OpenQuatt blijft de warmtevraag gewoon staan; die is ook los van een
regelaar een zinvolle meting.

Bijvangst: het warmteverliesgetal stond met veertien decimalen in de
attributen, terwijl het balanspunt ernaast al afgerond was.

## v0.8.16 — 2026-08-25

Warmtevraag als koppelvlak naar OpenQuatt's Power House

Sinds OpenQuatt#503 accepteert Power House een externe warmtevraag. Die
vervangt uitsluitend de feedforward P_house; de comfortterm, de clamp op
Rated maximum house power, de slew-limiter en de waterbegrenzer blijven
firmware. Precies de taakverdeling die deze integratie kan invullen: het
huismodel komt hier uit een jaar meetdata, de regeling blijft waar de
veiligheid zit.

sensor.quatt_warmteanalyse_warmtevraag publiceert UA × max(0, T_balans −
T_buiten) in W. De integratie schrijft niets — de gebruiker wijst de
bestaande OpenQuatt-bronhelper hiernaar, en het leegmaken van dat ene veld
is de noodrem. De sensor volgt de buitentemperatuur en niet alleen de
analysecyclus, anders zou de regelaar een uur op een verouderde vraag lopen.

Er wordt bewust niets van de waarde afgetrokken. De firmware trekt Kp·e al
af; zelf compenseren telt dubbel. Datzelfde geldt voor zonnewinst: zon warmt
de kamer, en dat ziet de firmware via diezelfde comfortterm.

async_heat_demand_link() controleert de drie schakels van de keten en meldt
per schakel wat er ontbreekt. Dat is nodig omdat een kapotte koppeling van
buiten onzichtbaar is: OpenQuatt valt stil en correct terug op zijn eigen
huismodel, dus het huis blijft gewoon warm — alleen niet op onze meting.
De keuzeknop wordt via de ESPHome-node op naam gezocht, want die entiteiten
krijgen het gebied als prefix in hun entity-ID.

De ch_max_water-route sluit dit uit en slaat zijn tick over zolang de
koppeling loopt. In Power House is het waterplafond een veiligheidsbegrenzer
(derate naar 0,25 binnen 3 K, trip bij +5 K), geen stuurknop: er alsnog een
aanvoeradvies naartoe schrijven knijpt de vraag die we net zelf stelden — met
een plafond dat, anders dan de vraag, niet vanzelf vervalt.

Dashboard: de Advies-view toont de vraag met de koppelstatus, en waarschuwt
als de vraag boven Pr uitkomt — daar kapt de firmware hem stilletjes af.

## v0.8.15 — 2026-08-22

Gasketel-sensor werd alsnog uit het register verwijderd

v0.8.14 haalde de sound_level_enabled-gate uit het binary_sensor-platform,
maar er is een tweede mechanisme: _async_cleanup_sound_level_entities draait
ná async_forward_entry_setups en verwijdert entiteiten uit het entiteit-
register op unique-ID-suffix zodra de geluidscompensatie uit staat. Het
suffix _gas_boiler_active stond in die lijst, dus de sensor werd aangemaakt
en meteen weer weggegooid — stil, want de opruiming logt op INFO.

Het suffix is uit de lijst gehaald. De regressietest leidt de unique-ID af
uit de entiteit zelf en toetst hem tegen de echte opruimlijst, zodat het
verband tussen die twee niet opnieuw kan wegvallen.

## v0.8.14 — 2026-08-22

Opt-in features verbergen zich nu op het dashboard

De geluidsniveaucompensatie en de aanvoertemperatuur-bijsturing zijn opt-in.
Stonden ze uit, dan bestonden hun entiteiten niet en toonde de Geluid-view
alleen nog foutmeldingen: de vermogensgrafiek en de history-graph braken op
sensor.quatt_warmteanalyse_geluidsniveau, de begrenzingskaart vulde alleen
n/b. De secties hangen nu aan een visibility-conditie — HA behandelt een
niet-bestaande entity als 'unavailable', dus ze verdwijnen vanzelf en komen
terug zodra de optie aan gaat. De view heet daarom Systeem in plaats van
Geluid, en de vermogensgrafiek is ontdaan van de geluidsserie zodat hij
altijd werkt.

Bijvangst: bij Databronnen en Aanvoertemperatuur begrenzing stond
grid_options op sectie- in plaats van kaartniveau, waardoor die twee nooit
hun volle breedte kregen.

Gasketel-sensor losgekoppeld van de geluidscompensatie

binary_sensor.quatt_warmteanalyse_gasketel_actief hing achter
sound_level_enabled omdat de compensatie zijn eerste afnemer was. Of de
gasketel bijspringt is een eigenschap van de installatie; wie de compensatie
uitzette raakte die meting ook kwijt. De entity-ID wordt nu vastgepind: voor
iedereen met de compensatie uit is dit een nieuwe entity, en HA zou de ID
anders uit het gebied van het device afleiden.

## v0.8.13 — 2026-08-22

Entity-ID van de kalibratiesensor vastpinnen

HA leidt de entity-ID van een nieuwe entity af uit het gebied van het device,
waardoor de sensor als sensor.bijkeuken_quatt_warmteanalyse_... werd
geregistreerd en de dashboardkaart hem niet kon vinden. De spiegelsensoren
pinnen hun ID hier al voor; die stap ontbrak bij de nieuwe sensor.

Bestaande installaties houden de oude ID — het entiteitregister hernoemt niet
vanzelf. Die moet handmatig hernoemd worden.

## v0.8.12 — 2026-08-22

Hotfix: twee AttributeErrors na de v0.8.11-reload

- OnlineRCModel.from_dict gebruikt cls.__new__ en slaat __init__ over, maar
  het nieuwe _u_anchored-veld werd daar niet gezet. Het teruglezen van het
  opgeslagen thermisch model faalde daardoor met AttributeError en begon
  opnieuw, waarmee de opgebouwde RLS-historie verloren ging.
- async_resolve_entity liep stuk op een ingestelde kandidatenlijst
  ('list' object has no attribute 'strip'). De buitentemperatuur wordt als
  lijst geconfigureerd, de rest als string. Dit pad wordt alleen als terugval
  geraakt wanneer de bronregistratie nog leeg is — dus precies tijdens een
  herstart of reload, waardoor sensoren niet konden worden toegevoegd en alle
  elf bronrollen op onopgelost bleven staan.

Beide met regressietests afgedekt.

## v0.8.11 — 2026-08-22

OpenQuatt-koppeling: kalibratie in plaats van sturen

- Power House-kalibratiesensor: leidt Tc en Pr af uit het gemeten huismodel
  en de gemeten capaciteitscurve. Tc is het snijpunt van vraag en capaciteit,
  niet de koudste dag; T0 wordt overgenomen van de regelaar omdat de regressie
  boven de stookgrens geen data heeft (warmste waarneming 15,2 °C, nulpunt
  extrapoleert naar 16,7 °C).
- Breakpoint-raster voor OpenQuatt losgetrokken van het advies-raster: de
  firmware staat op -20/-10/0/5/10/15, positioneel overzetten schoof de curve.
- Schrijfroute voor instelknoppen kiest OpenQuatt boven Quatt, omgekeerd aan
  de metingen: de CiC-compatibiliteitslaag bevestigt schrijfacties en negeert
  ze, dus naar de CiC schrijven is een stille no-op. Bestemming wordt elke
  tick opnieuw bepaald en staat in target_entity.
- RC-model: U wordt vastgehouden op de seizoensregressie zolang er te weinig
  warmte-input is om hem te identificeren. Zonder dat zakte de online U in de
  zomer naar 187 W/K terwijl de meting 285 W/K zegt, doordat U/C en g/C bij
  Q_hp ~ 0 inwisselbaar worden. C en g blijven wel leren.
- MPC toont nu de zonnefactor die het actieve model echt toepast; dat was de
  hardgecodeerde 8,0 terwijl de online berekening met de geleerde g rekende.

## v0.8.10 — 2026-08-16

De bronlaag bediende tot nu toe alleen het dashboard. De integratie zelf
resolvede nog rechtstreeks, en die weg kiest op bestaan in plaats van op
beschikbaarheid: een Quatt-sensor die er nog staat maar 'unknown'
teruggeeft won het van een OpenQuatt-sensor die de meting wel levert.
Zichtbaar geworden doordat het RC-model bleef klagen over een ontbrekende
kamertemperatuur terwijl OpenQuatt die gewoon publiceerde.

- sources: async_source_entity() als enige ingang voor de rest van de
  integratie. Valt terug op de losse resolver zolang de registry er nog
  niet is, of als geen enkele kandidaat een waarde geeft.
- sensor/switch/binary_sensor: alle veertien leesplekken omgezet. De
  entity-ID wordt nu per aanroep opgezocht in plaats van bij het
  opstarten vastgelegd.
- sensor: QuattAdviceErrorSensor kreeg zijn entity-ID's als
  constructor-argument mee, één keer bepaald bij setup. Die zoekt ze nu
  zelf op.
- sensor: candidate_entities() — state-change listeners volgen alle
  kandidaten in plaats van alleen de actieve bron. Anders komt er na een
  wegval per definitie geen state-change meer binnen van de opvolger.

Het schrijfpad (ch_max_water) blijft bewust op async_resolve_entity:
daar is één expliciete, stabiele doelentiteit juist gewenst.

## v0.8.9 — 2026-08-16

Repareert de entity-ID's van de spiegelsensoren uit v0.8.8.

Home Assistant bouwt de entity-id van een nieuwe entity op uit de area
van het device. Staat dat device in een area, dan werd het
sensor.bijkeuken_quatt_warmteanalyse_aanvoertemperatuur en wees geen
enkele dashboardverwijzing meer ergens naartoe. De codebase waarschuwde
hier al voor bij QuattCoastTimeSensor; die les was niet toegepast op de
nieuwe sensoren.

- sources: elke MirrorSpec heeft nu een vastgelegde slug; die is het
  publieke contract waar dashboards aan hangen.
- sensor: spiegels en overzichtssensor pinnen hun entity-id met
  async_generate_entity_id, net als QuattCoastTimeSensor al deed.
- __init__: eenmalige migratie die al aangemaakte entiteiten met een
  area-prefix terugzet op hun vaste id. Matcht op unique_id en draait
  vóór het laden van de platforms — daarna pakt HA de bestaande
  registry-id en negeert de vastgepinde waarde.
- sources: classify_source kijkt in het entity-register in plaats van in
  de rol-detectiekaarten. Die bevatten per rol één gekozen entity, dus
  een Quatt-sensor die nét niet won — sensor.thermostat_temperature_outside
  verliest van hp1 — werd ten onrechte als "overig" getoond.
- tests: bewaken dat de slugs uniek en stabiel zijn en dat het dashboard
  geen ruwe meetsensoren meer aanspreekt.

## v0.8.8 — 2026-08-16

Zichtbaar maken welke integratie welke meting levert, en het dashboard
daaraan hangen in plaats van aan vaste entity-ID's.

- sources: nieuwe bronlaag. Per meting wordt continu bepaald welke
  kandidaat een bruikbare waarde levert; de gekozen bron staat in de
  attributen in plaats van dat je hem moet afleiden.
- sensor: elf spiegelsensoren met een stabiel entity-ID, één per meting.
  Het dashboard hangt daaraan en merkt niets van een bronwissel.
- sensor: overzichtssensor "Databronnen" met de volledige kaart van
  meting naar integratie en entiteit, plus welke metingen geen bron
  hebben.
- discovery: rollen erbij voor thermostaat-setpoint, kamer-setpoint en
  COP — die eerste ontbrak, waardoor "Thermostaat vraagt" op het
  dashboard leeg bleef.
- dashboard: alle metingen omgehangen naar de spiegels, plus een nieuwe
  Databronnen-kaart. number.cic_max_water_temperature blijft direct
  aangesproken: dat is een instelknop, geen meting.

Bewust geen debounce op de spiegelwaarde. Bij het schrijfpad is wachten
juist goed, maar een spiegel hoort niet leeg te staan terwijl er een
werkende bron is. Elke spiegel volgt bovendien alle kandidaten in plaats
van alleen de actieve — anders komt een bron die herstelt nooit binnen.

## v0.8.7 — 2026-08-16

Historische analyse kan nu over een bronwissel heen kijken.

- discovery: OpenQuatt (ESPHome) als tweede provider naast de Quatt-
  integratie. De node wordt herkend aan "OpenQuatt Version" en rollen
  worden op ESPHome-naam gematcht, niet op entity-ID — HA nummert
  botsende namen door (curve_tsupply_10degc vs _10degc_2), waardoor
  -10 °C en +10 °C anders stilletjes verwisseld kunnen raken.
- discovery: async_resolve_candidates() levert alle bruikbare entities
  voor een rol in voorkeursvolgorde. Quatt blijft vóór OpenQuatt staan,
  zodat bestaande installaties dezelfde primaire bron houden.
- stooklijn: states_to_minute_series() en coalesce_series(), beide puur
  en los testbaar. De eerste bron blijft leidend; latere vullen alleen
  de minuten die de eerste laat vallen.
- stooklijn: async_fetch_live_history() accepteert kandidatenlijsten
  voor zowel temperatuur als vermogen; een losse string blijft werken.
- coordinator: bouwt die lijsten en zet per bron het aantal geleverde
  minuten plus het onderlinge verschil in data_stats.

Aanleiding: sensor.heatpump_flowmeter_temperature valt weg zodra de
Quatt-cloud hem niet meer voedt, terwijl OpenQuatt dezelfde meting
lokaal blijft opnemen. Eén entity leverde dan een halve reeks.

## v0.8.6 — 2026-08-04

De Quatt-integratie heeft bij haar v2->v3 migratie het generieke
'Heatpump'-device vervangen door losse devices (CIC, Flowmeter,
Heatpump 1, Boiler, Thermostat). HA hernoemt bestaande entity-IDs
niet, dus er lopen twee naamgevingen naast elkaar in het veld:

  vóór migratie : sensor.heatpump_flowmeter_temperature
  ná migratie   : sensor.flowmeter_temperature

Alle hardcoded entity-IDs werkten daardoor maar voor een deel van de
gebruikers, en vier ervan waren niet eens instelbaar.

Nieuw: discovery.py zoekt entiteiten op via de unique_id van de
Quatt-integratie (<hub>:<device>:<sensor_key>). Die sleutel is stabiel
bij hernoemen. Resolutie: ingesteld -> auto-detectie -> terugvalnaam.
Een ingestelde entity die niet bestaat valt door naar auto-detectie,
zodat verkeerd opgezette installaties zichzelf repareren.

Voorheen onbereikbaar, nu instelbaar én zelfoplossend:
- aanvoertemperatuur (sensor.py, switch.py)
- gasketel-warmtevermogen (switch.py, binary_sensor.py)
- recorder-statistieken power_input/boiler_heat (analysis/quatt.py)
- chMaxWater legacy-fallback (ch_max_water.py)

Ook omgezet: drie gedupliceerde _outdoor_entity-properties en twee
paar debiet/retour-properties in sensor.py.

Config flow: alle entity-velden zijn nu EntitySelector met
domeinfilter, voorgevuld met auto-detectie. Typen kan niet meer, dus
een niet-bestaande naam komt er niet meer in.

Bugfix: async_step_options sloeg alleen de geluidsinstellingen op.
Debiet, retourtemperatuur, zon, weer en kamertemperatuur werden
getoond en ingevuld, maar bij opslaan weggegooid.

Tests: conftest kon config_flow.py niet importeren (ConfigFlow-stub
accepteerde geen domain=), waardoor flow-tests nagebouwde logica
testten in plaats van de echte code. Stub gerepareerd plus een fake
entity-register. 214 tests groen (was 169).

const.py bevat geen DEFAULT_*_ENTITY meer; terugvalnamen staan alleen
in discovery.FALLBACK_ENTITIES. Dode RECORDER_COP_ENTITY verwijderd.

## v0.8.5 — 2026-07-19

Dashboard-setpoint-tile toont nu de echte sensor.heatpump_thermostat_control_setpoint
wanneer die bestaat, en valt via een visibility-conditie automatisch terug op
sensor.quatt_warmteanalyse_aanbevolen_aanvoertemperatuur wanneer de Quatt-integratie
die sensor niet aanmaakt (geen thermostaat op de OpenTherm-ingang). Voorkomt de
"unknown entity"-melding bij gebruikers zonder die sensor.

README: control_setpoint verplaatst van de "consistent across all systems"-tabel
naar de "may differ / may be missing"-sectie met uitleg over de fallback.

## v0.8.4 — 2026-07-19

- coordinator: nieuwe analysis_status "no_data" wanneer een run wel
  voltooit maar geen bruikbare output oplevert (geen knik/stooklijn/
  warmteverlies/COP), i.p.v. misleidend "completed"; log op info-niveau
- sensor: translation_key + neutraal timer-sand-icoon voor no_data
- translations (nl/en/de/fr/strings): state-vertalingen voor
  analysis_status; no_data toont als "Voltooid — nog geen stookdata"
- tests/conftest: translation_key toegevoegd aan SensorEntityDescription-stub

- Koel-voorbereiding stap 1: modus-labeling + heating-only filters gecentraliseerd
- Verwijder HACS validate GitHub Action

## v0.8.3 — 2026-06-15

- hard code sensor name to exclude room

## v0.8.2 — 2026-06-15

Tweerichtings-integratie met het energy-os project (prijsgestuurd thermisch
uitlopen met comfort-bewaking):

- thermal_model.py: simulate_coast_time() — vrije afkoel-simulatie (WP uit) die
  voorspelt hoe lang het huis op zijn thermische massa kan uitlopen tot de
  comfort-vloer. Voert de Open-Meteo zon-forecast mee, dus voorspelde zon
  verlengt de uitlooptijd.
- sensor.py: nieuwe QuattCoastTimeSensor (sensor.quatt_warmteanalyse_veilige_uitlooptijd);
  deelt RC-model + forecast met de MPC-sensor via build_forecast_arrays() en een
  thermal_model property. Unavailable tot het RC-model geconvergeerd is.
- const.py/config_flow.py: comfort-vloer-optie (default 19C) + optionele
  EOS throttle-entity.
- stooklijn.py: apply_throttle_mask() sluit door energy-os geknepen minuten
  (cap < 20) uit van knee/heat_loss/COP-fits; coordinator rapporteert het aantal
  uitgesloten minuten in data_stats.
- dashboard: MPC-tab toont veilige uitlooptijd (tegel + uitleg-kaart) met
  graceful fallback-tekst als de sensor nog niet beschikbaar is.
- tests: +12 (coast-time simulatie + throttle-masking).

## v0.8.1 — 2026-05-17

- Nieuwe kolom 'Aanvoer (geen zon)' in forecast-tabel: toont benodigde
  aanvoertemperatuur zonder zonnewinst, naast de kolom mét zonnewinst
- Zowel online RC-model (simulate_6h) als batch-fallback berekenen nu
  supply_temp_no_solar
- Verwijderd: '☀️ Zonnewinst verlaagt de warmtevraag.' melding (onjuist
  's avonds door residuele Open-Meteo straling)

## v0.8.0 — 2026-05-17

- Geluid-tab: dubbelgrafiek samengevoegd; MPC advies toegevoegd aan begrenzing-grafiek
- Verwijder overbodige 'Aanvoertemp. advies vs werkelijk'-grafiek (stond ook al in MPC-tab)
- chMaxWater hernoemd naar 'Aanvoertemperatuur begrenzing' / 'Ingestelde limiet'
- Dead band labels verwijderd uit afwijkingsgrafiek
- Vertaalbestanden (en/de/fr) aangevuld met ontbrekende sound_level en ch_max_water sleutels

## v0.7.9 — 2026-05-17

Twee tabellen in één > scalar vereisen een dubbele lege regel als
paragraafbreuk; opgelost door alles in één tabel te zetten.
Actieve bron gemarkeerd met ◀ in plaats van een aparte header.

## v0.7.8 — 2026-05-17

Voeg leesbare labels toe aan strings.json en nl.json voor alle vijf
ch_max_water config-velden (ontbraken volledig, HA toonde ruwe sleutels):
- ch_max_water_enabled  → Aanvoertemperatuur bijsturing inschakelen
- ch_max_water_entity   → Quatt max. aanvoertemperatuur entiteit
- ch_max_water_source   → Stuuradvies bron (stooklijn of MPC)
- ch_max_water_hysteresis → Minimale wijziging voor schrijfactie (°C)
- ch_max_water_interval → Schrijfinterval (minuten)

## v0.7.7 — 2026-05-17

- Revert: automatische MPC→stooklijn fallback verwijderd; de gebruiker
  kiest bewust een bron in de config flow en die wordt gerespecteerd
- Dashboard: toont MPC- en stooklijn-advies naast elkaar; de geconfigureerde
  bron is vetgedrukt zodat duidelijk is waarop gestuurd wordt

## v0.7.6 — 2026-05-17

- chMaxWater: automatische fallback van MPC naar stooklijn als MPC-sensor
  niet beschikbaar is (unknown/unavailable)
- active_source bijgehouden en beschikbaar als sensor-attribuut
- Dashboard toont nu beide adviezen (MPC + stooklijn), actieve bron,
  en "(fallback)" melding als stooklijn ingesprongen is voor MPC

## v0.7.5 — 2026-05-17

- sensor.quatt_warmteanalyse_max_aanvoertemperatuur_instelling krijgt
  attribuut interval_minutes (het geconfigureerde schrijfinterval)
- Dashboard toont nu het werkelijke interval i.p.v. hardcoded "30 min"

## v0.7.4 — 2026-05-17

- Verwijder stroke.dashArray en curve:stepline uit apex_config (veroorzaakten
  configuration error in apexcharts-card 2.2.3)
- Fix markdown tabel: lege regels tussen elke rij zodat > scalar newlines
  bewaart (zonder lege regels vouwt YAML alles samen naar één regel)
- Voeg uitleg toe voor nieuwe gebruikers wat de bijsturing doet

## v0.7.3 — 2026-05-17

stroke_dasharray is geen geldige series-property in apexcharts-card 2.x;
vervangen door apex_config.stroke.dashArray array op chart-niveau.

## v0.7.2 — 2026-05-16

- Nieuw grafiek in Geluid-tabblad: thermostaat setpoint vs chMaxWater
  limiet vs werkelijke aanvoer vs stooklijn advies (48u)
- Nieuwe statuskaart: huidige waarden + netto temperatuurverlaging

## v0.7.1 — 2026-05-16

- DEFAULT_CH_MAX_WATER_ENTITY bijgewerkt naar number.cic_max_water_temperature
  (Quatt 2.0 heeft de heatpump_ prefix verwijderd)
- Auto-detectie fallback: als geconfigureerde entity niet beschikbaar is,
  wordt automatisch de legacy naam (number.heatpump_cic_max_water_temperature)
  geprobeerd met een waarschuwing om de instelling aan te passen
- _clamp() en _write() ontvangen nu de resolved entity_id vanuit _async_tick

## v0.7.0b1 — 2026-05-16

- Nieuwe feature: periodieke bijsturing van chMaxWaterTemperatuur op basis van
  de huis-eigen stooklijn of het MPC-model. Instelbaar interval (default 30 min),
  hysteresis (default 1°C) en bronkeuze (stooklijn/mpc). Opt-in via opties.
- Compatibiliteit met Quatt integratie ≥2.0: get_insights → get_cic_insights
  met automatische fallback voor oudere installaties (≤1.0.2).
- Diagnostische sensor voor laatste geschreven max aanvoertemp + tijdstip.
- Test-stub uitgebreid met homeassistant.const (EntityCategory).

## v0.6.4 — 2026-05-08

De stookgrens en nominaal vermogen in de advieskaart gebruikten de minuut-regressie
(slope_api), die systematisch een te vlakke helling geeft doordat Quatt bij milde
temperaturen meer levert dan de huisvraag (door de stooklijn-instelling). Validatie
op echte data toonde aan dat geen enkele cutoff-variant dit corrigeert.

Switched naar slope_api_daily (daggemiddelde Quatt output), die over volledige dagen
middelt inclusief OFF-uren en daardoor convergeert naar de werkelijk geconfigureerde
stookgrens (~16°C i.p.v. ~44°C).

- _stooklijn_reliable: gebruikt balance_temp_api_daily + slope_api_daily
- _calc_vermogen: extrapolatie via slope_api_daily/intercept_api_daily
- _count_changes: vergelijkt balance_temp_api_daily vs optimaal
- Dashboard: label "Quatt stooklijn (gemeten)" → "Quatt stooklijn (daggemiddeld)"
- Tests bijgewerkt naar daily-velden

## v0.6.3 — 2026-04-22

Tweede criterium toegevoegd aan _stooklijn_reliable(): als |slope_api|
< 80% van |slope_optimal| (huis-warmteverlies) wordt de regressie als
onbetrouwbaar gemarkeerd, ongeacht het evenwichtspunt. Dit pakt de case
waarbij binning het evenwichtspunt net onder 20°C brengt maar de helling
nog steeds te vlak is door overheersende lente-data (~216 vs 284 W/°C).
De foutmelding beschrijft nu ook de reden (evenwichtspunt té hoog vs
helling te vlak).

## v0.6.2 — 2026-04-22

- switch.py: geluidsniveau-slider reset bij dag/nacht-overgang altijd
  beide selects (actief én inactief); periode-overgang detectie via
  _last_is_night zodat inactieve slider direct op max_dag/nacht-max
  wordt gezet zonder te wachten op MPC-actie
- stooklijn.py: warm-kant regressie binned per 1°C voor slope_api;
  voorheen overspoelden duizenden voorjaar-minuutpunten de tientallen
  KneeDataStore-winterpunten (1000:50 ratio), waardoor de helling
  instortte en balance_temp_api op >20°C uitkwam
- sensor.py: nominaal_vermogen_huidig telt niet mee als advies-afwijking
  en wordt als onbetrouwbaar gemarkeerd wanneer balance_temp_api >20°C;
  nieuw attribuut nominaal_vermogen_betrouwbaar
- dashboard: huidig vermogen doorgestreept weergegeven als onbetrouwbaar,
  duidelijke melding over ontbrekende koude meetdata

## v0.6.1 — 2026-04-18

- Knikpunt kan nu strikt alleen omlaag: freeze-clamp verplaatst naar
  calculate_stooklijn() zodat ook de warm-side regressie-split de frozen
  waarde gebruikt (was: alleen gerapporteerde waarde werd gecorrigeerd)
- KneeDataStore retentie verhoogd van 3 naar 100 jaar (data nooit weggooien)
- Nachtvenster geluidsbegrenzing nu instelbaar via config flow
  (standaard: 23:00–07:00, HA lokale tijd)

- docs: README bijgewerkt voor v0.6.0

## v0.6.0 — 2026-04-07

- Alleen een versieophoging.

## v0.5.15 — 2026-04-07

Voeg grid.padding.right toe aan grafieken zonder rechter y-as zodat de
x-assen van alle vier de 48-uurs grafieken op één lijn staan.

## v0.5.14 — 2026-04-06

- Fix: dag/nacht-select schrijft nu alleen naar de actieve periode;
  inactieve select wordt teruggezet naar geconfigureerd maximum zodat
  overgang schoon verloopt (dag-select stond 's ochtends op building87)
- Fix: geluidsniveausensor spiegelt nu current_level van de switch
  (periode-bewust) i.p.v. altijd de dag-select
- Fix: dashboardmelding toont nu het werkelijke effective_max i.p.v.
  hardcoded 'normal'
- Nieuw: reset vindt pas plaats na 10 minuten HP-inactiviteit (was
  direct bij eerste inactieve cyclus)
- Tests: 13 nieuwe tests voor switch-compensatielogica toegevoegd

## v0.5.13 — 2026-04-06

- MPC_SUPPLY_TEMP_COOL_MIN = 15.0°C toegevoegd als ondergrens voor koeling
- Early return 'geen warmtevraag → None' verwijderd uit QuattMpcSensor.native_value
- Sensor blijft nu numeriek (≥15°C) ook als er geen verwarmingsbehoefte is,
  waardoor de geluidsniveau-watchdog niet meer onterecht reset

## v0.5.12 — 2026-04-04

- Alleen een versieophoging.

## v0.5.11 — 2026-04-04

- Compensatie leest nachtvenster live uit Quatt-sensoren (sensor.cic_sound_night_time_*)
  i.p.v. hardcoded 23:00-07:00 (bij Mark: 19:00-07:00)
- Interne level geclampt bij dag/nacht-overgang — geen drift meer
- Reset naar effectief max voor huidige periode i.p.v. altijd 'normal'
- Step-up begrensd op dag/nacht-max (geen nutteloze stappen voorbij nacht-plafond)
- Nieuw dashboard-tabblad 'Geluid' met 5 grafieken (48u) voor analyse stookgedrag

- fix: verwijder dubbele geluidsniveau history-graph (24 uur)
- fix: verwijder retour uit aanvoertemp grafiek (flowmeter al aanwezig)
- fix: verwijder ongeldige span.end: now uit apexcharts-card grafieken
- feat: sound level compensatie dag/nacht-bewust + geluid-view dashboard
- feat: add sound level compensation with day/night max settings
- feat: add sound level compensation with day/night max settings

## v0.5.10 — 2026-04-01

- Aanvoertemp in forecast gebruikt nu max(t_return, t_indoor) zodat
  stilstaand koud water bij uitgeschakelde HP geen onrealistische 20°C geeft
- Forecast-temperaturen worden nu op tijdstip gematcht (hours-from-now)
  i.p.v. blind de eerste 6 entries te pakken; voorkomt dat nachttemperaturen
  overdag getoond worden wanneer de weather entity pas later begint

- Fix forecast: aanvoertemp corrigeren bij HP uit + temperaturen time-aligned ophalen

## v0.5.9 — 2026-04-01

Verwijder RestoreEntity — de opgeslagen 'off'-staat overschreef de nieuwe
standaard, waardoor de switch na elke herstart op Uit bleef staan.

## v0.5.8 — 2026-04-01

- Sensor en binary sensor stonden fout geregistreerd als switch.* entiteiten
- QuattSoundLevelSensor verplaatst naar sensor.py (sensor.quatt_warmteanalyse_geluidsniveau)
- QuattGasActiveSensor verplaatst naar nieuw binary_sensor.py (binary_sensor.quatt_warmteanalyse_gasketel_actief)
- Switch start voortaan automatisch aan wanneer sound_level_enabled is ingeschakeld

## v0.5.7 — 2026-04-01

- Nieuwe sensor: huidig geluidsniveau (building87/silent/library/normal)
- Nieuwe binary sensor: gasketel actief (>200 W)
- Beide worden door HA Recorder bijgehouden en zijn grafeerbaar
- Dashboard: history-graph kaart toegevoegd op MPC-tabblad
- Testconftest: stubs toegevoegd voor binary_sensor en restore_state

## v0.5.6 — 2026-03-31

Bij herladen van de integratie (bijv. na een update) probeerde de
cleanup code een startup-listener te annuleren die al afgevuurd was.
HA logde dan: "Unable to remove unknown job listener". De fix: de
listener registreert nu wanneer hij afgevuurd is, zodat cancel()
alleen aangeroepen wordt als het event nog niet plaatsgevonden heeft.

## v0.5.5 — 2026-03-31

De OTGW kamertemperatuur-override is volledig vervangen door directe
aansturing van select.cic_day_max_sound_level en
select.cic_night_max_sound_level. Het geluidsniveau bepaalt het maximale
compressorvermogen van de warmtepomp.

Logica (elke 5 min):
- Gas actief (boiler_heat_power > 200 W): één stap omhoog zodat de
  warmtepomp meer kan leveren en de gasketel minder hoeft bij te springen
- Aanvoertemp te hoog t.o.v. MPC-advies: één stap omlaag
- Aanvoertemp te laag t.o.v. MPC-advies: één stap omhoog
- Reset naar 'normal' bij uitschakelen of HP inactief

Niveauvolgorde: building87 → silent → library → normal

Bestaande gebruikers met OTGW geconfigureerd krijgen geen foutmeldingen:
de oude config key (otgw_enabled) wordt genegeerd, de nieuwe schakelaar
(sound_level_enabled) staat standaard uit.

## v0.5.4 — 2026-03-31

Alle sensor- en switch-properties lazen configuratiewaarden alleen uit
entry.data, waardoor wijzigingen via de opties-flow (opgeslagen in
entry.options) werden genegeerd en altijd de standaardwaarden werden
gebruikt. Fix: elke property mergt nu entry.data en entry.options,
conform het bestaande patroon in _indoor_temp_entity.

Tevens hardcoded strings "flow_entity" en "indoor_temp_entity" in
switch.py vervangen door de juiste constanten CONF_FLOW_ENTITY en
CONF_INDOOR_TEMP_ENTITY.

## v0.5.3 — 2026-03-26

Bij opstarten zijn sensorstates nog niet beschikbaar; dit is verwacht
gedrag. Warning → debug zodat het logboek schoon blijft.

## v0.5.2 — 2026-03-26

async_migrate_func is geen constructor-argument in deze HA-versie.
Gebruik _MigratingStore subclass met _async_migrate_func override.

## v0.5.1 — 2026-03-26

Voeg migratiefunctie toe aan ThermalModelStore zodat HA de storage-versie
correct kan ophogen (v1→v2). Zonder deze functie crashte de MPC-sensor bij
het opstarten met NotImplementedError.

## v0.5.0 — 2026-03-26

Het thermisch model (1R1C + RLS) gebruikte de PV-opbrengst (W) als
zonnewinst-input. Dit veroorzaakte collineariteit met buitentemperatuur
waardoor g_solar naar onzinnige waarden (>2.0) convergeerde.

Nu wordt Open-Meteo shortwave radiation (W/m²) gebruikt:
- g_solar = effectief raamoppervlak × SHGC (typisch 2-10 W/(W/m²))
- Geen afhankelijkheid meer van PV-sensor voor het RC-model
- Forecast solar code sterk vereenvoudigd (geen radiation_factor kalibratie)
- Model reset: storage version 1→2 (oude PV-schaal data onbruikbaar)

Overige wijzigingen:
- g_solar bounds: 0-1 → 0-20 W/(W/m²)
- Default g_solar: 0.30 → 5.0 W/(W/m²)
- Dashboard: bounds gecorrigeerd naar werkelijke code-waarden

## v0.4.11 — 2026-03-25

- Breakpoints tellen niet meer mee in aantal aanbevelingen — ze zijn
  informatief, niet vergelijkbaar met de huidige Quatt instelling
- "Instellingen optimaal" verschijnt nu als stookgrens en vermogen kloppen
- Verouderd foutbericht vervangen: "Vul de Quatt stooklijn in" →
  "Wacht tot de Quatt stooklijn is geschat uit recorder data"

## v0.4.10 — 2026-03-25

De vier invoervelden (stooklijn punt 1/2 temp+vermogen) zijn verwijderd.
De gemeten Quatt stooklijn (recorder-gebaseerd) vervangt de handmatige
instelling in de advies-sensor en batch forecast.

- config_flow: stooklijn-invoervelden verwijderd uit setup en opties
- coordinator: _calc_stooklijn_from_points en actual_stooklijn_* velden weg
- sensor: actual_stooklijn sensor verwijderd; vermogen-advies gebruikt
  nu slope_api/intercept_api; batch forecast fallback ook
- dashboard: "Geconfigureerde instelling" lijn verwijderd uit grafiek
- translations: velden verwijderd uit en/nl/de/fr

## v0.4.9 — 2026-03-25

Advies-sensor gebruikte balance_temp_api_daily (daggemiddelde schatting)
als referentie voor de huidige Quatt-instelling. Nu wordt balance_temp_api
gebruikt: het nulpunt van de recorder-gebaseerde stooklijn (minuutdata),
wat preciezer weergeeft wat de warmtepomp feitelijk doet.

- StooklijnResult: balance_temp_api veld toegevoegd (= -intercept/slope)
- Advies-sensor: stookgrens_huidig gebruikt nu balance_temp_api
- Dashboard: labels verhelderd naar "Quatt stooklijn (gemeten)" vs
  "Huis optimaal (lineair)"

## v0.4.8 — 2026-03-25

Het knikpunt (warmtepomp capaciteitslimiet) kan fysisch alleen naar
kouder verschuiven naarmate er meer koude-winterdata beschikbaar komt.
In het voorjaar veroorzaakte milde lente-data (3-8°C) een valse elbow
in de grid-search waardoor het knikpunt steeg van -1.8°C naar 3.5°C.

- KneeDataStore slaat nu best_knee_temp op (koudste ooit gezien)
- Coordinator bevriest het knikpunt als nieuwe detectie > best + 0.5°C
- Persistentie over HA-restarts via HA .storage JSON

## v0.4.7 — 2026-03-24

Bugfix: batch forecast toonde altijd 55°C (het maximum) als aanvoertemp
doordat warmtevraag in W direct als temperatuur werd gebruikt. Nu correct:
T_return + Q_net / (1.16 × flow), zelfde formule als de stooklijn advies sensor.

Dashboard: 6-uurs forecast tabel bouwt nu rijen op via Jinja2 namespace
string zodat er geen lege regels tussen tabelrijen staan. Headers en data
zijn nu correct uitgelijnd in de markdown tabel.

## v0.4.6 — 2026-03-24

Thermisch model card onderscheidt nu drie staten: (1) nog aan het leren
< 48 updates, (2) genoeg data maar parameters buiten fysisch bereik —
toont welke parameters out-of-range zijn en wat het verwachte bereik is,
(3) geconvergeerd — toont geleerde waarden.

## v0.4.5 — 2026-03-24

_params_sane() vergeleek numpy floats waardoor model_converged een
numpy.bool_ was in plaats van Python bool. Dit veroorzaakte 'State is
not JSON serializable' errors in de recorder en WebSocket API.

## v0.4.4 — 2026-03-24

- sensor.py + thermal_model.py: hp_needed omgezet van numpy.bool naar
  Python bool; voorkomt 'State is not JSON serializable' in recorder
- dashboard.yaml: solar_gain_w check verplaatst binnen Jinja2 namespace
  om 'UndefinedError: h is undefined' na for-loop te voorkomen

## v0.4.3 — 2026-03-24

Voegt q_hp_needed_w en hp_needed toe aan de batch forecast output,
zodat de 6-uurs forecast card ook werkt wanneer het RC model nog
niet geconvergeerd is (< 48 updates).

## v0.4.2 — 2026-03-22

- Fix RC model 0/48: prime bij startup + diagnostische logging

## v0.4.1 — 2026-03-22

MPC sensor leert nu automatisch de thermische eigenschappen van het huis:
- Warmteverliescoëfficiënt (U), thermische massa (C), zonnewinst-factor (g)
- RLS (Recursive Least Squares) update elk uur, convergeert in ~2 dagen
- Forward simulatie 6-uurs forecast met geleerde parameters
- Fallback naar batch warmteverliesmodel als model nog niet geconvergeerd
- Persistentie via HA .storage (overleeft herstarts)
- Dashboard MPC tab uitgebreid met thermisch model info en forecast tabel
- 26 nieuwe tests (RLS convergentie, RC model, simulate_6h)

- Verwijder ongebruikte CONF_QUATT_END_DATE import uit coordinator en config_flow

## v0.4.0 — 2026-03-22

- Alleen een versieophoging.

## v0.3.22 — 2026-03-22

4 tabs (Overzicht, Analyse, Advies, MPC) met gauge, tile cards,
apexcharts scatter plots en MPC fout-grafiek met nul-referentielijn.
Responsive layout: 2 kolommen desktop, 1 kolom mobiel.

- Nieuw dashboard: sections view met 4 tabs (Overzicht, Analyse, Advies, MPC)

## v0.3.21 — 2026-03-21

Options flow sloeg waarden op in entry.options maar las alleen uit
entry.data, waardoor ingevulde stooklijn-waarden (en alle andere
options) verloren gingen na opslaan. Nu worden data en options gemerged
en wordt de integratie herladen bij wijzigingen.

## v0.3.20 — 2026-03-21

- Alle config flow velden hebben nu duidelijke labels in 4 talen (nl/en/de/fr)
- Solar sensor beschrijving benadrukt dat elke sensor die Watt rapporteert
  gebruikt kan worden (SolarEdge, Enphase, Huawei, SMA, template)
- OTGW en MPC velden krijgen begrijpelijke labels in plaats van technische keys

## v0.3.19 — 2026-03-21

- MPC en stooklijn fout-sensoren geven None terug als flow < 30 L/h (HP uit),
  voorkomt misleidende oplopende foutwaarden bij stilstaand water
- Dashboard: advies samenvatting kaart toegevoegd op Quatt Advies tab die
  concreet opsomt welke aanpassingen aanbevolen worden

## v0.3.18 — 2026-03-21

- Dashboard referenties aangepast naar correcte entity_id (quatt_advies_parameters)
  voor compatibiliteit met bestaande installaties
- Stooklijn Optimaal vs Huidig chart gebruikt nu timestamp-truc zodat
  ApexCharts correct rendert (was: loading door directe temp waarden)
- Sensor naam terugzet naar "Quatt Advies Parameters" voor stabiele entity_id

## v0.3.17 — 2026-03-21

- Quatt Advies sensor hernoemd naar "Quatt Advies" (was "Quatt Advies Parameters")
  zodat entity_id overeenkomt met dashboard referenties
- Dashboard uitgebreid met Quatt Advies tab, OpenQuatt overzicht, en OTGW status

## v0.3.16 — 2026-03-21

Nieuwe features:
- Quatt advies sensor: toont welke parameters aan Quatt doorgeven (stookgrens, vermogen, stooklijn breakpoints)
- OTGW compensatie switch: actieve bijsturing via kamertemp-override (opt-in)
- OpenQuatt readiness: output sensoren met optimale stooklijn breakpoints en balanspunt
- README: voorspellingsmodellen sectie (lineair vs MPC vs XGBoost)

Code kwaliteit:
- analysis/utils.py: gedeelde robust_linear_fit(), calc_r2(), calc_heat_demand()
- helpers.py: gedeelde get_float_state(), get_device_info(), get_effective_flow()
- 30+ duplicaties verwijderd uit sensor.py, switch.py, text.py, heat_loss.py, stooklijn.py

## v0.3.15 — 2026-03-21

Nieuwe functie extract_knee_points_from_hourly() extraheert koude-uur-
punten uit df_hourly (Quatt API cache, tot 3 jaar). De coordinator vult
de KneeDataStore nu permanent bij met historische koude periodes die nooit
in het recorder-venster vielen (bijv. de vorst van dec 2025/jan 2026).
Hierdoor hoeft de knikpuntdetectie niet meer elke run opnieuw te leunen
op df_hourly als tijdelijke bron — de data zit na de eerste run permanent
in de store. De on-the-fly toevoeging van df_hourly in calculate_stooklijn
is verwijderd ten gunste van deze structurele oplossing.

## v0.3.14 — 2026-03-21

KneeDataStore bevat alleen koude dagen die binnen het 30-daagse recorder-
venster vielen tijdens vorige analyseruns. Koude periodes van voor de
installatie (bijv. dec 2025/jan 2026 tot -5.4°C) ontbraken daardoor.
Nu worden ook koude-uurpunten uit df_hourly (API-cache, 293 dagen) toegevoegd
aan de knikdetectie-dataset, zodat historische koude periodes alsnog
bijdragen aan het bepalen van het correcte knikpunt.

## v0.3.13 — 2026-03-21

Recorder-data voor knikpuntdetectie beperkt tot temp < 10°C (was: alle
temperaturen). Milde-weerpunten (>10°C) beinvloedden de grid-search zodat
het knikpunt in de lente naar +2.5°C schoof terwijl het fysiek rond -1.5°C
ligt. Beide datasets (recorder + KneeDataStore) zijn nu consistent gefilterd
op koude-weerpunten. Milde recorder-punten blijven beschikbaar voor de
helling-regressie (step 1b).

## v0.3.12 — 2026-03-21

- QuattInsightsCache retentie verhoogd van 1 naar 3 jaar (gelijkgesteld aan
  KneeDataStore) zodat uurdata van eerdere winters beschikbaar blijft voor
  de max-envelop analyse (Step 2)
- Dashboard databeschikbaarheidstabel: recorder-rij toont nu "Knikpunt &
  helling (actueel)" en knie-datastore "Knikpunt & helling (historisch)"
  zodat duidelijk is dat beide samen de knikpuntdetectie voeden en een
  dalend recorder-getal geen verslechtering betekent

## v0.3.11 — 2026-03-20

De forecast gebruikt nu de best beschikbare stooklijn als baseline:
1. Eerst de geanalyseerde optimale stooklijn (slope_optimal/intercept_optimal)
   — gecalibreerd op echte meetdata, meest nauwkeurig voor alle gebruikers
2. Fallback: de Quatt-app instelling (actual_stooklijn)
   — voor systemen waar de analyse nog niet gedraaid heeft

Dit maakt de MPC forecast betrouwbaar voor alle gebruikers, ook als de
Quatt API-config niet beschikbaar is of nog niet opgehaald is.

## v0.3.10 — 2026-03-20

Voorheen gebruikte de 6-uurs MPC forecast de huidige retourtemperatuur
(T_retour) als startpunt. Wanneer de warmtepomp uit staat is T_retour ~12°C,
waardoor alle forecast-uren op de minimumvloer (20°C) bleven hangen.

Nieuw: forecast berekent de aanvoertemperatuur direct uit de gecalibreerde
stooklijn (T_aanvoer = slope × T_buiten + intercept) en trekt de solar
correctie hiervan af. Dit geeft realistische voorspellingen ongeacht of de
warmtepomp op dit moment aan of uit staat.

## v0.3.9 — 2026-03-19

- MPC_SUPPLY_TEMP_MIN verlaagd van 25°C naar 20°C zodat advies bij mild weer
  niet onterecht wordt afgekapt (was oorzaak van constante 25°C output)
- Forecast-berekening in extra_state_attributes gebruikt nu ook nominaal debiet
  (800 L/h) als fallback wanneer HP uit staat, zodat forecast_6h waarden toont

## v0.3.8 — 2026-03-17

- Nominaal debiet (800 L/h) als fallback wanneer HP niet draait
- Dashboard layout verbeterd: entiteitgrid 5-kolomsindeling, markdown cards naast elkaar
- Shadow Validatie tab herschikt: Aanvoertemperatuur volledig breed, Fout + Buitentemp naast elkaar
- Fout-grafiek autoschaalt nu (vaste -5/+5 schaal verwijderd)

## v0.3.7 — 2026-03-16

- MPC shadow sensor volledig gedocumenteerd: werking, shadow mode, solar correctie
- Uitleg toegevoegd over vereiste weersvoorspeller (weather.home via Met.no)
- Sectie toegevoegd over aanpassen dashboard voor andere setups

## v0.3.6 — 2026-03-16

- Einddatum verwijderd: analyse loopt nu altijd tot date.today()
- Einddatum entity en config-flow veld verwijderd
- Dashboard: duplicate aanvoertemperatuur-grafieken verwijderd (staan al op Shadow Validatie tab)
- Dashboard: MPC en Aanbevolen Aanvoer entity cards verwijderd uit hoofdpagina
- README: sectie toegevoegd over aanpassen van dashboard voor andere setups

## v0.3.5 — 2026-03-16

- Twee nieuwe sensoren: stooklijn fout en MPC fout aanvoertemperatuur
  (advies − werkelijke aanvoer in °C, state_class measurement)
- Nieuw dashboard tabblad "Shadow Validatie" met fout-grafieken over 7 dagen
- DEFAULT_SUPPLY_TEMP_ENTITY toegevoegd aan const.py

## v0.3.4 — 2026-03-15

- MPC sensor haalt shortwave_radiation forecast op van Open-Meteo (gratis,
  geen API key, gebruikt lat/lon uit HA config) — geen handmatige configuratie nodig
- Dynamische kalibratie: radiation_factor berekend uit live solaredge ×
  SOLAR_TO_HEAT_FACTOR / shortwave_radiation[nu], zodat warmtewinst
  automatisch gekalibreerd is op het werkelijke huis
- Forecast uur 0: altijd live solaredge meting (meest nauwkeurig)
- Forecast uur 1–5: Open-Meteo shortwave_radiation × gecalibreerde factor;
  fallback naar condition-fractie als data ontbreekt
- Attributen per forecast-uur uitgebreid met condition, shortwave_wm2,
  radiation_factor en radiation_source voor transparantie
- CONDITION_SOLAR_FRACTION constante behouden als fallback proxy

- feat: MPC sensor toegevoegd aan dashboard

## v0.3.3 — 2026-03-15

- Nieuwe sensor: MPC Aanbevolen Aanvoertemperatuur (shadow mode)
  Berekent aanvoertemp op basis van heat loss coefficient + zonnewinst
  correction. Schrijft niks naar OTGW — puur voor validatie naast de
  bestaande stooklijn.
- Weersverwachting: hourly forecast via weather.get_forecasts, attribuut
  forecast_6h toont 6 uur vooruit met warmtevraag + aanvoertemp per uur.
- Zonnewinst: Q_zon = solaredge_ac_power × SOLAR_TO_HEAT_FACTOR (0.30),
  met fallback-commentaar naar toekomstige RC-regressie (solar_gain.py).
- Configureerbaar: solar_entity, weather_entity, indoor_temp_entity
  toegevoegd aan config flow opties-stap (met defaults en uitleg).
- 10 nieuwe tests voor _calc_mpc_supply_temp berekeningslogica.

## v0.3.2 — 2026-03-10

- Dagelijkse Quatt stooklijn regressie filtert nu ook de bovenkant af:
  dagen waarop de warmtevraag onder het minimale modulatievermogen (~2kW)
  valt worden uitgesloten. Op die dagen draait Quatt op minimumfrequentie
  en levert meer warmte dan gevraagd, waardoor de x-intercept eerder
  overschat werd.
- Regressie wordt nu berekend ná de optimale stooklijn (Step 3) zodat de
  bovengrens dynamisch bepaald kan worden op basis van het huis-warmteprofiel.
- Dashboard: stooklijn-advies werkt nu ook zonder handmatige Quatt-instelling;
  de geschatte stooklijn (slope/intercept) wordt gebruikt voor het concrete
  advies inclusief verspild vermogen in Watt.
- Dashboard: sensor-referentie gecorrigeerd (quatt_stooklijn_slope →
  quatt_warmteanalyse_slope).

## v0.3.1 — 2026-03-09

- dashboard.yaml gesynchroniseerd met dashboards/ (was achtergebleven):
  markers, stroke_width, lineaire regressie COP trendlijn, rows 5→6
- Entity grid columns: 3 (compromis desktop/mobiel)
Versienummering opgehoogd van 0.2.x naar 0.3.x.
- sensor.py: QuattSupplyTempSensor now returns None when flow_lph < MIN_FLOW_LPH (30 l/h)
  instead of <= 0, preventing extreme temperature spikes when the pump briefly
  reports near-zero flow during shutdown/startup transitions
- const.py: add MIN_FLOW_LPH = 30 constant
- dashboard.yaml: entity grid columns 4 → 2 for readable mobile layout

- fix: entity grid columns 2→3 (desktop/mobile compromis)
- fix: sync dashboards/quatt_stooklijn_dashboard.yaml — entity grid columns 4→2
- docs: voeg mini-graph-card toe aan README requirements

## v0.3.0 — 2026-03-08

- Alleen een versieophoging.

## v0.2.41 — 2026-03-09

- sensor.py: QuattSupplyTempSensor now returns None when flow_lph < MIN_FLOW_LPH (30 l/h)
  instead of <= 0, preventing extreme temperature spikes when the pump briefly
  reports near-zero flow during shutdown/startup transitions
- const.py: add MIN_FLOW_LPH = 30 constant
- dashboard.yaml: entity grid columns 4 → 2 for readable mobile layout

- docs: voeg mini-graph-card toe aan README requirements

## v0.2.40 — 2026-03-08

Vervang de "verbind alle punten" aanpak in de COP trendlijn door een
echte lineaire regressie berekend in de data_generator. De trendlijn
toont nu een rechte lijn door de scatter-wolk in plaats van een
zigzag die alle punten verbindt.

## v0.2.39 — 2026-03-08

apex_config.markers.size toegevoegd aan grafiek 1 (warmteprofiel) en grafiek 4
(COP): scatter-series krijgen size 4, lijn-series size 0. Zonder expliciete
marker-config toont ApexCharts standaard geen punten bij stroke_width: 0.

## v0.2.38 — 2026-03-08

chart_type: scatter verwijderd (ondersteunt geen type: line per series).
Scatter-series krijgen type: line + stroke_width: 0 (alleen punten),
lijn-series krijgen stroke_width: 2 (echte lijnen). Gebaseerd op v0.2.7 aanpak.

## v0.2.37 — 2026-03-08

chart_type: scatter hersteld op beide apexcharts-cards; type: scatter van
scatter-series verwijderd (niet ondersteund in versie 2.2.3).

## v0.2.36 — 2026-03-08

chart_type: scatter verwijderd van beide apexcharts-cards; scatter-series
krijgen nu expliciet type: scatter. Hierdoor renderen lijn-series (optimale
stooklijn, HP capaciteit, COP trendlijn) nu als echte lijnen i.p.v. losse punten.

## v0.2.35 — 2026-03-08

- Alleen een versieophoging.

## v0.2.34 — 2026-03-08

- Sectie 1: entity grid (12 cards, columns: full) + markdown/grafiek cards
- Sectie 2: alleen de 4 grafieken (thermisch profiel, aanvoertemp 14d, comfort, COP)
- Data Wissen knop verwijderd uit entity grid
- Databeschikbaarheid tabel: content: > met lege regels per rij (correct rendering)
- Stooklijn aanpassen / Warmtevraag / Wat betekenen in sectie 1

## v0.2.33 — 2026-03-08

- Twee losse entity grids (Grid 1: 6 kaarten, Grid 2: 7 kaarten) samengevoegd
  tot één type:grid met columns:4 en 13 kaarten in 4 rijen i.p.v. 5
- Analyseresultaten (COP, warmteverlies, knikpunt, etc.) eerst, daarna
  admin-kaarten (startdatum, einddatum, status, knoppen)

## v0.2.32 — 2026-03-08

- Fix Databeschikbaarheid markdown tabel: content: > → | zodat tabelrijen
  niet worden samengevoegd tot één lange regel
- Fix lege ruimte in sectie 1: kolommen verdubbeld (8→16, 16→32) zodat
  "Stooklijn aanpassen?" + history-graph de volledige rij vullen

## v0.2.31 — 2026-03-08

- Voeg "Databeschikbaarheid" card toe aan source dashboard (daggemiddeldes, uurwaarden, minuutwaarden, API-cache, knie-datastore)
- Fix kolombreedte "Wat betekenen" van 33→24 en "Warmtevraag" van 15→12
- Sync scatter filter grafiek 1: p.temp < Math.min(balanceTemp, 19)

## v0.2.30 — 2026-03-08

- dashboard: filter dagelijkse warmtevraag scatter op p.temp < Math.min(balanceTemp, 19) om randpunten bij ~20°C uit te sluiten

## v0.2.29 — 2026-03-07

- tooltip.x.formatter toegevoegd aan Grafiek 1 en 4: hover toont temperatuur (°C) i.p.v. datum
- Null terminator toegevoegd aan Optimale stooklijn, Quatt stooklijn (geschat),
  Geconfigureerde instelling en Gas trendlijn: voorkomt hover-marker artefacten
  buiten het bereik van de lijn-series

## v0.2.28 — 2026-03-07

Zelfde fix als v0.2.27 voor HP capaciteit: null-terminator toegevoegd
aan het einde van de COP trendlijn data_generator. Voorkomt dat
apexcharts de laatste COP-waarde (~5.4) toont als hover-marker op
t=20°C bij de rechterrand van grafiek 4.

## v0.2.27 — 2026-03-07

Grafiek 1 (Huis thermisch profiel):
- HP capaciteit (vorst): null-terminator toegevoegd aan einde data_generator
  zodat apexcharts geen hover-marker toont op t=20°C met y=6931W
- Dagelijkse warmtevraag: balanceTemp gecapt op 20 (Math.min) als veiligheid
  tegen opgeblazen balance_temp door outliers

Grafiek 4 (COP vs Buitentemperatuur):
- Dagelijkse COP + COP trendlijn: filter p.temp < 20 toegevoegd
  (was helemaal geen temperatuurfilter aanwezig)

Achtergrond: scatter_data bevat geen outliers (max 14.8°C, startdatum
2025-07-01 sluit test-runs al uit). De hover-dots bij t=20°C kwamen
van apexcharts die de laatste seriewaarde toont als marker op de
cursorpositie; de null-terminator voorkomt dit voor HP capaciteit.

## v0.2.26 — 2026-03-07

- maxT voor lijn-series verlaagd van 20 naar 19 zodat lijn-eindpunten
  niet meer als losse dot op de x-as rechterrand (20°C) verschijnen
- Geconfigureerde instelling loop ook afgekapt op 19°C
- Scatter filter gewijzigd van <= naar < balanceTemp

## v0.2.25 — 2026-03-07

- Scatter data in Grafiek 1 (warmtevraag + gas) gefilterd op balance_temp/balance_point
  zodat warme-dag uitschieters boven de balanstemperatuur niet worden getoond
- Fallback van 20°C als balance_temp nog niet beschikbaar is
- pyyaml en voluptuous toegevoegd aan requirements_test.txt (CI fix)

## v0.2.24 — 2026-03-07

In apexcharts-card v2.2.3 is entity in data_generator een array van
historische states, niet het huidige entity object. entity.attributes
is dus undefined -> TypeError -> fallback op timestamps als x-as.
Fix: gebruik hass.states['entity_id'].attributes overal.

Ook: quatt_stooklijn_slope entity gecorrigeerd naar werkelijke naam
sensor.quatt_warmteanalyse_quatt_warmteanalyse_slope.

## v0.2.23 — 2026-03-07

type: scatter is niet geldig op series-niveau in apexcharts-card v2.2.3.
Verwijderd — scatter series erven chart_type: scatter van de kaart.

## v0.2.22 — 2026-03-07

plotly-graph fn: ondersteunt geen vrije x-as (forceert tijdstempels).
Beide scatter grafieken vervangen door apexcharts-card met data_generator,
wat wel custom x-waarden (buitentemperatuur) ondersteunt.

## v0.2.21 — 2026-03-07

meta is undefined in plotly-graph fn: zonder meta: config, waardoor
meta.attributes een TypeError gooide en de grafieken timestamps toonden.
Vervang door hass.states['entity_id'].attributes. Zet hours_to_show: 0
om onnodige history fetches te vermijden.

## v0.2.20 — 2026-03-07

Use getattr() instead of .get() when accessing the LovelaceData dataclass
from hass.data["lovelace"] — modern HA uses a dataclass, not a plain dict.

## v0.2.19 — 2026-03-07

- Dashboard entity IDs gemigreerd van quatt_stooklijn_* naar quatt_warmteanalyse_*
- Automatische entity migratie bij upgrade (geen handmatige actie nodig)
- Dashboard wordt automatisch aangemaakt bij eerste installatie via lovelace API
- dashboard.yaml gebundeld in integratiepakket
- verify_ha_setup.py: HA-sensor validatiescript toegevoegd

- Dashboard: voeg xaxis.type: linear toe aan Plotly scatter grafieken
- Dashboard: grafiek 2 en 3 naar mini-graph-card (eenvoudiger en stabiel)
- Dashboard: grafiek 1 en 4 naar custom:plotly-graph (scatter via fn/meta)
- Dashboard: fix y-as decimalen via labels.formatter in alle 4 grafieken
- Dashboard: gebruik Date.now() mapping voor scatter grafieken (grafiek 1 en 4)

## v0.2.18 — 2026-03-06

- Fix: TypeError len(None) in geschatte COP sensor bij lege cop_scatter_data

## v0.2.17 — 2026-03-06

- Alleen een versieophoging.

## v0.2.16 — 2026-03-06

- Voeg geschatte actuele COP sensor toe en slope-ratio analyse

## v0.2.15 — 2026-03-06

- Fix stooklijn grafieken: gebruik dagelijkse regressie en verwijder misleidende lijn

## v0.2.14 — 2026-03-06

Voegt balance_temp_api_daily toe: regressie op dagelijkse totalHeatPerHour
voor temp >= knikpunt, vergelijkbaar met de oorspronkelijke notebook.
Geeft een realistischere balanstemperatuur (~18°C) dan de minuut-gebaseerde
slope_api (~26°C). Gebruikt als fallback in het stooklijn-advies dashboard.

- Dashboard: aanvoer/setpoint samen met metrics in 3-koloms grid
- Dashboard: verwijder onbetrouwbare slope_api fallback in stooklijn-advies
- Dashboard: zet stooklijn-advies naast aanvoertemperatuur-grafiek
- Dashboard: gebruik geschatte stooklijn als fallback voor stooklijn-advies

## v0.2.13 — 2026-03-06

- Dashboard: vervang dubbele tabel door concreet stooklijn-advies met effect-uitleg
- Dashboard: fix stooklijn-aanbeveling gebruikt nu heat loss regressie ipv slope_api
- Dashboard: voeg werkelijke aanvoertemperatuur (flowmeter) toe aan history graph

## v0.2.12 — 2026-03-06

- Dashboard: verplaats 'Stooklijn aanpassen?' card naar boven de grafieken
- Fix: stroke_dasharray verplaatst naar apex_config.stroke.dashArray

## v0.2.11 — 2026-03-06

- cache.py: QuattInsightsCache.async_cleanup() aangeroepen in async_load()
  zodat de insights-cache niet onbeperkt groeit (was nooit aangeroepen)
- sensor.py: knee_temperature toegevoegd als attribuut aan
  freezing_performance_slope sensor
- dashboard grafiek 3: blauwe capaciteitslijn stopt nu bij kniepunt i.p.v.
  +5°C (extrapolatie buiten datumbereik); rode lijn aangepast van
  instantane slope_api naar slope_optimal (regressie door dagpunten);
  gele Quatt stooklijn (geschat) toegevoegd als vierde serie
- dashboard grafiek 2: paarse stippellijn 'Aanbevolen instelling' toegevoegd
  (zelfde helling als geschatte stooklijn, balans op huisbalanstemperatuur)
- dashboard: markdown-kaart 'Stooklijn aanpassen?' met concrete config-punten
  en automatische check op basis van verschil in balanstemperatuur

- Test: vastleg dat zomerdagen niet in warmteverlies-scatter verschijnen

## v0.2.10 — 2026-03-06

Fix: warmteverlies-scatter toonde zomerdagen (0 W bij hoge temperaturen)
omdat scatter_data werd gebouwd vanuit alle geldige datapunten (plot_data).
Nu wordt heating_data gebruikt, consistent met de regressie (MIN_HEATING_WATTS).

## v0.2.9 — 2026-03-06

- Fix: stooklijn (geschat) stortte in bij mild weer doordat de 30-daagse
  recorder-window geen koude data meer bevatte. Koude punten uit de
  KneeDataStore worden nu ook gebruikt als anker voor de warm-side regressie.
- Fix: outlier-removal (2-pass z-score) toegevoegd aan stap 1b, net zoals
  stap 3; voorkomt scheve regressie door sporadische defrost-bleed-through.
- Fix: gas.py gebruikte hardcoded UTC voor datum-parsing; nu wordt de
  geconfigureerde HA-tijdzone gebruikt.

## v0.2.8 — 2026-03-02

Wijzigingen sinds v0.2.7:
- Constanten geconsolideerd: stooklijn.py en heat_loss.py importeren nu
  MIN_POWER_FILTER, BIN_SIZE, KEEP_THRESHOLD, DAYS_HISTORY, MIN_HEATING_WATTS
  en OUTLIER_STD_THRESHOLD vanuit const.py; lokale duplicaten verwijderd
- DAYS_HISTORY gecorrigeerd van 10 naar 30 in const.py
- Eerste-run bescherming: lege cache begrenst API-aanroepen tot API_FETCH_DAYS
  om honderden aanroepen bij eerste installatie te voorkomen
- Dubbele _calc_stooklijn_from_points()-aanroep in coordinator.py opgelost
- Twee-staps uitbijterverwijdering toegevoegd aan optimale stooklijn regressie
  (stap 3 in stooklijn.py, consistent met heat_loss.py)
- Merkiconen toegevoegd voor HA 2026.3 brands proxy API
- Luisteraar race-conditie bij herladen na opstart opgelost

- Refactor: consolideer constanten en verbeter robuustheid
- Add brand icons for HA 2026.3 brands proxy API
- Fix scatter_data: toon alle geldige punten ipv alleen regressie-inliers
- Fix test collection door homeassistant.helpers.event stub toe te voegen
- Fix listener race condition bij reload na startup

## v0.2.7 — 2026-02-22

- Verwijder outliers uit warmteverlies regressie
- Fix dashboard: correct entity_id voor aanvoertemperatuur sensor

## v0.2.6 — 2026-02-22

Voeg live sensor toe die continu de aanbevolen aanvoertemperatuur
berekent op basis van actuele buitentemperatuur, debiet en retourtemp.
Formule: T_aanvoer = T_retour + warmtevraag / (1.16 × debiet_lph)

- Nieuwe sensor: quatt_stooklijn_recommended_supply_temp
- Configureerbare debiet- en retourtemperatuur-entiteiten
- Dashboard: aanbevolen vs Quatt setpoint (entity cards + history graph)

## v0.2.5 — 2026-02-22

Voegt een rolling 3-jaar datastore toe (quatt_stooklijn_knee_data) die
gefilterde uurgemiddelden van recorder-data per dag opslaat. Bij elke
analyse worden historische koude-weerpunten samengevoegd met het huidige
30-dagenvenster, waardoor knikpuntdetectie ook bij milde periodes
voldoende koude-data heeft. Opslag: ~18 KB/jaar, max ~54 KB na 3 jaar.

- cache.py: nieuwe KneeDataStore klasse met async load/save/cleanup
- stooklijn.py: extract_knee_points_from_recorder() helper + df_knee_history
  parameter in calculate_stooklijn()
- coordinator.py: store laden, nieuwe dagen opslaan, history doorgeven
- README.md: hybrid data approach, knee detection en troubleshooting bijgewerkt

## v0.2.4 — 2026-02-21

scipy wordt niet meer gebruikt na vervanging van curve_fit door grid search.

- Verbeter knikpuntdetectie: grid search + recorder als primaire bron
- Add credits to Rickvdt in README
- Add GitHub Actions and fix test stubs

## v0.2.3 — 2026-02-19

- Add Dutch, German, and French translations
- Fix codeowners to @Appesteijn

## v0.2.2 — 2026-02-17

- Fix heat loss trendlines using actual regression values
- Update README with stooklijn estimation from recorder data

## v0.2.1 — 2026-02-17

- Increase recorder history for stooklijn estimation from 10 to 30 days

## v0.2.0 — 2026-02-17

- Use HA recorder minute-level data for Quatt stooklijn estimation
- Reduce chart x-axis range from -10..25°C to -10..20°C

## v0.1.0 — 2026-02-17

- Cap stooklijn lines at their zero-crossing temperature
- Limit stooklijn lines to scatter data temperature range
- Update README with hybrid data approach and auto-startup
- Filter scatter data to heating days only (>= 200W)
- Fix FutureWarning: use float for totalBoilerGas default
- Use cached hourly data beyond the 30-day API window
- Calculate COP from energy totals instead of recorder COP sensor
- Filter out non-heating days from COP and stooklijn calculations
- Use recorder statistics for historical data, API for recent 30 days
- Auto-run analysis on HA startup to populate dashboards
- Add intelligent caching and improved knee detection
- Add COP interpolation and electricity consumption to temperature table
- Format last analysis date as yyyy-mm-dd
- Add icon to README

## v0.0.6 — 2026-02-15

- Add integration icon (256px and 512px @2x)
- Fix gas heat loss by fetching temperature from recorder
- Rename to Quatt Warmteanalyse, add test suite, fix concat warning

## v0.0.5 — 2026-02-14

Eerste release als Home Assistant custom integration (HACS).
