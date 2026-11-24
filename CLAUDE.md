# Quatt Stooklijn — vastgestelde principes

Dingen die al eens zijn uitgezocht en niet opnieuw ter discussie horen te
staan. Wie hiervan wil afwijken, meet eerst en past dan dit bestand aan.

## Warmtevermogen niet over een uur middelen als je een model leert

Een uurgemiddelde van het vermogen bewaart de energie, maar gooit weg wanneer
die energie binnenkwam:

```
30 min × 5 kW + 30 min × 0 kW  →  gemiddeld 2,5 kW
60 min × 2,5 kW                →  gemiddeld 2,5 kW
```

Twee verschillende uren, maar hetzelfde getal. Voor alles wat traag is (de
bouwmassa, τ ≈ 100 u) maakt dat niet uit. Voor alles wat in minuten tot uren
reageert wel. Dan gaat het mis:

- **Knikdetectie**: defrosts verdunnen het uurgemiddelde in de koude zone
  (45 min × 5,5 kW + 15 min defrost → 4,1 kW), waardoor de knik te warm
  uitkomt. Filteren lost het niet op. Zie `KNEE_DETECTION_IMPROVEMENTS.md`.
- **2R2C-model**: de snelle knoop (`Ci`, `Hm`, `alpha`, τ_snel van een paar
  uur) is alleen te leren als de timing binnen het uur zichtbaar is. Het
  model leert daarom op de vijfminutenmetingen uit `highres_log`
  (`FIT_DT_HOURS` in `analysis/model_benchmark.py`).

Uurwaarden zijn wél goed voor:

- **toetsen en plannen**: de MPC plant per uur, dus modellen worden per uur
  gescoord (`DEFAULT_DT_HOURS`). Dat het model fijner leert dan het getoetst
  wordt is de bedoeling, en kan omdat de discretisatie exact is.
- **het 1R1C**: dat leert per uur, één stap vooruit, met één warmtebuffer.
  De timing binnen het uur doet er voor dat model niet toe, de energie wel.
  Daarom leert het sinds v0.10.7-beta.6 van het **uurgemiddelde** uit het
  5-minutenlogboek (warmtepomp + ketel), met de momentopname op de timertik
  als terugval als het logboek te weinig monsters heeft. Op een huis met
  alleen een warmtepomp maakte dat niets uit (oktober 2026: stabiele blokken,
  ±5% binnen het uur, +27 W verschil). Een bijspringende ketel stookt in korte
  stoten, en die mist een momentopname of telt hij als een heel uur.
  Nagebootst (21-11-2026, radiatorhuis met ketel): momentopname 3 van de 20
  modellen online, uurgemiddelde 16.

## Het huismodel rekent met warmtepomp plus ketel

Net als de warmteverliesanalyse (`totalHeatPerHour` = warmtepomp + ketel).
Rekent het huismodel alleen met de warmtepomp, dan ziet het bij een hybride
de kamer opwarmen zonder warmte, en verklaart het dat met een negatieve C en
een te grote zonnewinst (Tweakers 21-11-2026: C −674 Wh/K, g 25,7).
Nagebootst met een onzichtbare ketel: C negatief bij 20 van de 20
vloerverwarmingshuizen. De ketelmeting van Quatt bevat geen tapwater (op het
gemeten huis de hele zomer van 2025 precies 0).

Wat dit níét oplost: vloerverwarming met een bijspringende ketel. De warmte
gaat eerst de dekvloer in, en het 1R1C kan die vertraging niet uitdrukken;
in de nabootsing bleef C ook met uurgemiddelde en ketel een factor 3 te hoog
of werd hij negatief. 5-minutendata helpt daar niet bij een fit van één stap
vooruit (C 70.700 tegen 69.400 uit uurdata, echt 23.000). Dat is werk voor
het 2R2C. Een model dat zo ontspoort (C ≤ 0, of NaN in θ) begint opnieuw
(`OnlineRCModel.is_broken`), bij het laden en na elke uurupdate. Tijdens bedrijf
vanaf de warmteverliesanalyse; bij het laden nog niet, want de analyse draait
pas na de platforms. Dan zijn het de standaardwaarden, en komen de ankers bij
de eerste analyse (`_handle_coordinator_update`).

Geschiedenis: dit stond al vast voor de knikdetectie, maar raakte zoek. In
v0.10.3 rekende de modelvergelijking "bewust op uurgemiddelden". Dat is op
15-10-2026 teruggedraaid.

## Het 1R1C heeft een constante warmtewinst nodig

Zonder term voor constante winst (`θ₄ = P_c/C`) klopt de warmtebalans in het
stookseizoen niet: het balanspunt ligt 3–4 K onder de kamertemperatuur, dus er
komt ~1 kW binnen die het model niet kent. Het U-anker houdt U vast, dus de
fout belandt in C. Gemeten in oktober 2026: C van 10.700 naar 7.400 Wh/K in
tien dagen. Nagespeeld met de echte RLS-toestand uit `.storage`: met P_c blijft
C staan en komt P_c zelf op ~1.050 W. Ook 's nachts, dus het is geen zon.

- P_c krijgt één startwaarde uit het balanspunt. Vanaf nul leert RLS hem te
  traag en zakt C intussen toch (`test_vastleggen_zonder_startwaarde_te_traag`).
- θ₄ niet bevriezen in uren zonder warmtevraag. Zijn regressor (dt) is nooit
  nul, en de oude blokupdate van `RLSEstimator` ontregelde P dan. Uitgeprobeerd:
  op het koude-starthuis liep C negatief. Met de huidige update (zie
  "Bevriezen in de RLS" hieronder) is dat niet opnieuw geprobeerd.
- **P_c staat vast op de warmteverliesanalyse** (sinds v0.10.7-beta.2):
  P_c = U × (mediaan kamer 14 d − balanspunt), bijgewerkt bij elke analyse,
  net als het U-anker. Vrij geleerd (θ₄ = P_c/C) groeide P_c in watt mee met
  C: 1.406 W tegen ~970 W uit het balanspunt, en het 1R1C voorspelde de kamer
  na 12 uur 0,7 °C te warm (slechter dan niets doen, 0,494 tegen 0,461 K).
  Nagespeeld met de echte RLS-toestand en het logboek (23-10 t/m 20-11-2026):
  met anker C stabiel 12.000–14.700 Wh/K, U 283–287, testfout 0,409 K;
  zonder anker zwierf C van 6.600 tot 18.300 en U van 235 tot 377, testfout
  0,662 K. Met anker telt P_c als bekende warmte in de regressor van θ₃ en
  heeft θ₄ géén regressor (x[3] = 0, waarde 0, kruiscovarianties op nul). Voor
  θ₄ is dat iets anders dan het bevriezen hierboven. **Maar de regressor van θ₃
  wordt daardoor `dt·(Q + P_c)`, en die is nooit nul**, ook niet in de uren
  waarin θ₃ bevroren staat. Tot 21-11-2026 stond hier dat er met anker
  "niets is met een regressor die nooit nul is"; voor θ₃ klopte dat niet. Zie
  de volgende sectie. `seed_constant_gain` heeft sinds `a5f558f` geen
  aanroeper meer: een installatie zonder balanspunt krijgt geen anker en leert
  P_c vanaf nul (en dus te traag, zie hierboven). Bewust zo gelaten tot er zo'n
  installatie is om op te meten.
- Met anker telt θ₄ **niet** mee voor de convergentie (`_n_updates_min`). Zijn
  teller loopt niet op, en in v0.10.7-beta.2 bleef een nieuw model daardoor
  voorgoed op `batch_fallback` (`TestConvergentieMetAnker`).

## Bevriezen in de RLS houdt P geldig (Schmidt-Kalman)

`RLSEstimator.update` met `frozen` zet de gain van de bevroren parameters op
nul, maar telt hun onzekerheid mee in de gain van de rest, en werkt P als
geheel bij in de Joseph-vorm. De vergeetfactor werkt alleen op het actieve
blok, zodat P[2,2] in een zomer niet opblaast (de reden om θ₃ te bevriezen).

De oude blokupdate werkte alleen `P[actief, actief]` bij en liet de
kruistermen staan. Dan is P geen covariantie meer, kan `λ + x'Px` negatief
worden, en krijgt de gain het verkeerde teken zodra de parameter weer
meedoet. Gevonden in een externe analyse (21-11-2026) en nagespeeld op het
echte 5-minutenlogboek (08-09 t/m 21-11-2026), jong model met beide ankers:

| start | oud: uren P indefiniet / online / 12u-fout | nieuw |
|---|---|---|
| 08-09 | 5 / 54% / 0,83 K | 0 / 54% / 0,56 K |
| 15-10 | 373 / 1% / 6,4 K (eind-g 58,7) | 0 / 89% / 0,40 K |
| 01-11 | 131 / 89% / 0,65 K | 0 / 90% / 0,51 K |

Het draaiende model (toestand 26-10) merkte er in vier weken niets van; de
P van 21-11 was nog positief-definiet, met een kleinste eigenwaarde van
3,9e-11. Op een synthetisch herfsthuis werd een rijp model met de oude update
na ~1.850 uur indefiniet, vooral in weken met veel uren onder de ankerdrempel
(het voorjaar). Een P die al indefiniet is opgeslagen, wordt bij het laden
hersteld (`repair_covariance`).

Wat níét werkt:

- **θ₃ met anker gewoon niet bevriezen.** Op echte data vanaf 08-09 liep C
  naar nul; vanaf 15-10 ging het wel goed.
- **Alleen de kruistermen consistent bijwerken**, met θ zoals in de oude
  blokupdate (de onzekerheid van θ₃ telt dan niet mee in de gain). P bleef
  geldig, maar op het herfsthuis met beide ankers eindigden 3 van de 20
  modellen goed, tegen 20 van de 20 met Schmidt-Kalman.

Tests: `tests/test_rls_covariantie.py`.

## Op vijfminutendata niet één stap vooruit fitten

Het gevolg van de vorige regel. De kamertemperatuur komt in 0,01 K binnen
maar flakkert ±0,015 K, ongeveer evenveel als de kamer per vijf minuten
opwarmt. Een eenstapsfit leest die ruis als snelle dynamiek. Op een
synthetisch huis met blokverwarming kwam τ_snel daardoor op 1,1 u uit in
plaats van 3,6 u. Fit daarom over `FIT_HORIZON_HOURS` (6 u) vooruit. De tabel
staat in `analysis/thermal_model_2r2c.py`, de test in `TestFithorizon`. Dat de
poort (`fit_and_score`) de horizon ook echt doorgeeft, bewaakt
`TestPoortBedrading`; de standaardwaarde van `fit()` is 1, dus een vergeten
argument valt stil terug op de eenstapsfit.

## De vooruitblik rekent elk uur zoals het advies

Uur +0 is het advies van nu, met en zonder huismodel. Beide paden gebruiken
`advice_demand`: `simulate_advice_forward` met de kamer uit het 1R1C,
`advice_forward_without_model` zonder (v0.10.7-beta.4). Geef de terugval geen
eigen berekening: tot beta.4 rekende hij met de stooklijn, zon eraf, zonder
inhaalterm of capaciteitsgrens, en een gebruiker las de lege "T binnen"
naast een afwijkend +0 als "de aansturing negeert de kamer". Zonder huismodel
telt de inhaalterm alleen in uur 0: hoe snel een tekort verdwijnt hangt van C
af, en een aangenomen C zit bij een zwaar huis al gauw een factor 2 mis.

## Een negatieve zonnewinst is een waarschuwing, geen ruis om weg te poetsen

Een negatieve g betekent vrijwel altijd dat de hele 1R1C-fit niet klopt, niet
alleen de zonnewinst. Laat `G_MIN = 0` in `_params_sane` het model dus naar de
batch-terugval sturen, ook als U en C binnen hun grenzen liggen. Bewaakt door
`TestNegatieveZonnewinst` (tot 22-11-2026 slaagden alle tests met `G_MIN = -5`).

Uitgezocht op 21-11-2026 (Tweakers: g = −1,96 na 73 uur, U 257 en C 13.280,
model op de terugval). Op een nagebootst novemberhuis (80 jonge modellen, 14
dagen, kamerruis ±0,015 K) hadden de uren met g ≥ 0 een mediaanfout van 2% op
U en 7% op C. In elk uur met −2 ≤ g < 0 zat U er 119% en C 59% naast, binnen de
grenzen. Een plausibele U zegt weinig: in uren zonder warmtevraag staat U
vast op de warmteverliesanalyse.

Wat níét werkt, alle drie geprobeerd:

- **g bij gebruik als 0 tellen** (`max(0, θ₂)`). Dat haalt de waarschuwing
  weg en zet precies de slechte modellen online: vooruitblik,
  prijsverschuiving en de veilige uitlooptijd, die als comfortgrens voor
  sturing van buitenaf bedoeld is.
- **θ₂ in de RLS op nul zetten** zonder P mee te nemen, samen met g 's nachts
  bevriezen: het koude-starthuis van `TestOnlineRCModel` ontspoorde (U
  −477 W/K, C negatief).
- **Alleen 's nachts bevriezen of alleen de ondergrens**: geen verbetering op
  het novemberhuis.

Het echte probleem is dat een jong model in november onstabiel leert. Een
belangrijke oorzaak bleek de covariantie-update (zie "Bevriezen in de RLS"),
opgelost in v0.10.7-beta.7; of het Tweakers-geval daarmee verklaard is, is
niet bewezen (de nabootsing gaf één keer g −2,37 na 73 uur). Wie
daaraan werkt, speelt eerst na op echte data (RLS-toestand en logboek uit
`.storage`), niet alleen synthetisch.

## Namen voor gebruikers staan op één plek

De stuurbronnen heten *Buitentemperatuur*, *Buitentemperatuur + kamer* en
*Huismodel + kamer* (`SOURCE_NAMES` in `const.py`, ontwerp 22-11-2026). De
interne waarden `stooklijn`, `mpc` en `1r1c` blijven, net als de entity-ID's:
daar hangen opgeslagen opties, het dashboard en automations aan. Kaarten lezen
de naam uit een attribuut (`bron_naam`, `advies_bron_naam`), niet uit een eigen
tabel. `tests/test_namen.py` houdt de keuzelijst in vier talen gelijk aan de
tabel en weert de oude woorden uit dashboard en `nl.json`.

Het sturingseffect bewaart per dag de sleutel (`max_water=mpc;sound=uit;demand=0`),
niet de naam. Tot beta.8 werd de tekst bewaard, en dan splitst een hernoeming
elke instelling in twee groepen. Wie een naam verandert, hoeft daar dus niets
voor te migreren; wie de sleutel verandert wel (`migrate_config`).

## Het 2R2C mag pas rekenen als het ook de afkoeling kent

Vrij gefit voorspelt het 2R2C de kamer twaalf uur vooruit goed, maar zijn
opslag (Ci, Cm) is niet te bepalen en hij kent de afkoeling bij een
stilstaande pomp niet: in november staat de pomp geen uur uit. Nagespeeld op
het logboek van 08-09 t/m 22-11-2026 (32 stookloze vensters van vier uur,
allemaal september en oktober): gemeten −0,25 K; 1R1C fout −0,04, vrij 2R2C
+0,25 (nauwelijks afkoeling), 2R2C met ankers en afkoelgewicht +0,06
(spreiding 0,13 tegen 0,20 bij het 1R1C).

- **U vast op de warmteverliesanalyse** kost niets (12 u: 0,262 tegen 0,266 K).
- **De totale gratis warmte vast**, niet P_int alleen: P_int = U·(kamer −
  balanspunt) − g·(gemiddelde zon). Het balanspunt bevat de gemiddelde zon al;
  P_int als constante vastzetten telt die dubbel en kostte 7–16 %.
- **Stookloze vensters wegen extra** (`COOLING_WEIGHT` 0,5).
- **Goedkeuring voor gebruik** (`Comparison.approved`) vraagt de gewone toets,
  de toets met de weken omgedraaid, een fit op het hele logboek, en een
  afkoeltoets: minstens 10 vensters, gemiddelde fout ≤ 0,10 K én ≤ 15 % van
  de gemeten daling (ondergrens 0,03 K), en niet slechter dan het 1R1C. De
  relatieve grens kwam erbij na beta.9: live haalde het 2R2C de vaste 0,10 K
  met +0,068 K op −0,248 K (27 %), en gaf een uitlooptijd van 237 minuten
  tegen 145 bij het 1R1C. Pas dan rekent de driftgrens met het 2R2C; de
  veilige uitlooptijd rekent met beide en neemt de kortste. De kamerlijnen (vooruitblik en prijsverschuiving) tonen al eerder een
  2R2C, de fit die de poort haalde, ter informatie; na goedkeuring rekenen ze
  met dezelfde parameters als de driftgrens (`params_use`). Een goedkeuring
  vervalt na 48 uur zonder nieuwe meting (`_approved_for_use`). Het advies
  rekent met de warmteverliesanalyse, niet met het 2R2C.
- Een installatie die in de winter begint, heeft geen stookloze vensters; dan
  blijft het 1R1C staan tot het tussenseizoen. Dat is de bedoeling.

## Aanvoer uit vermogen: het afgiftemodel, behalve nu met draaiende pomp

`analysis/emitter.py`: `P = k·(T_gem − T_kamer)^n`, eens per etmaal uit de
uurgemiddelden van de recorder. Gemeten op het testhuis (november 2026, getoetst
op andere weken dan geleerd), benodigde aanvoer:

| methode | gem. fout | gem. afwijking |
|---|---|---|
| retour van dit uur | +0,01 K | 0,09 K |
| afgiftemodel | ±0,1 K | 0,42–0,45 K |
| retour van vier uur eerder | −0,11 K | 0,61 K |
| vaste retour 28 °C / 800 l/u | +1,79 K | 1,83 K |

Dus: het advies van nu met draaiende pomp blijft op de gemeten retour; al het
andere (uren vooruit, stilstaande pomp, stooklijnpunten) rekent met het
afgiftemodel. De exponent wordt gekozen op de fout in de aanvoer, niet op
log-vermogen.

De COP op aanvoertemperatuur is uit de meting niet te leren: de aanvoer volgt
de stooklijn (een fit op buiten én aanvoer gaf een verkeerd teken). De
prijsverschuiving gebruikt daarom de verhouding van twee Carnot-waarden ten
opzichte van de vlakke reeks (`cop_supply_factor`). Beide vermogens worden
eerst geklemd op het minimumvermogen: daaronder pendelt de installatie op dat
minimum, met de aanvoer die daarbij hoort. Alleen de referentie klemmen gaf een
restje onder het minimum een COP-bonus (review 22-11: 30 % besparing op een
zachte dag, 0 % zonder aanvoer-COP); geen van beide klemmen strafte een blok op
het minimum af tegen een vermogen dat de installatie niet kan leveren.

## Ontdooiingen horen niet in het advies (ReturnHold)

Het advies van nu rekent op de gemeten retour (zie hierboven, 0,09 K). Rond een
ontdooiing klopt die aanname niet: de warmtepomp koelt zelf het water, dat komt
~3 minuten na het einde als retour terug (testhuis: dieptepunt 2,4 K onder de
waarde ervoor, mediaan; bij twee units tegelijk 5 K) en herstelt in 20–40
minuten. `ReturnHold` (`helpers.py`) houdt dan de hoogste retour van de tien
minuten ervoor vast, en rekent nooit lager dan gemeten.

- **Herkennen op vermogen én vlag.** Thermisch vermogen < −1000 W ving op het
  testhuis 128 van 128 ontdooiingen zonder valse melding (−500 W: 2 vals,
  −100 W: 30, stilstand en opstarten). De Quatt-vlag (`hp1.computedDefrost`)
  stond op hetzelfde huis een week lang uit, OpenQuatt meldde ze wel. Dus
  alle vlaggen tellen, niet één winnaar, en het vermogen erbij. De vlaggen
  zitten niet in de bronregistry: die rekent met getallen.
- **Herstel pas na de transittijd** (`DEFROST_TRANSIT_MIN`): vlak na het einde
  staat de retour nog op de oude waarde. Zonder die wachttijd zou het
  vasthouden daar meteen loslaten, vóór het koude water aankomt. Op het
  testhuis scheelde het in de nagespeelde dips weinig (p90 1,20 → 1,08 K met
  vlaggen); het is een vangnet voor een vlag die uitgaat voor de retour zakt.
- **Tijdens de ontdooiing mag de waarde nog omhoog.** De OpenQuatt-vlag gaat
  ~6 minuten vóór de omkering aan; live 24-11 steeg de retour in die minuten
  van 28,28 naar 28,40 en rekende het dal met 28,28. Alleen omhoog en alleen
  zolang er ontdooid wordt: koud water is altijd lager, en na het einde is een
  stijgende retour herstel. Naspeling: dip mediaan 0,30 → 0,23 K (met vlaggen).
- Losgelaten na 30 minuten zonder ontdooiing: 10 min liet een dip van 0,8 K
  staan, 30 min 0,2 K, 45 min 0,18 K bij meer vastgehouden tijd. Een duo dat om
  beurten ontdooit herstelt soms een uur niet; daarom hoogstens 90 minuten.
- Het afgiftemodel is hier geen alternatief: met draaiende pomp is het 0,4 K
  minder nauwkeurig, en een ontdooiing duurt maar 8 minuten.

## De capaciteitsgrens kapt alleen de inhaalterm (`cap_demand`)

De vriescurve (`slope_local`/`intercept_local`) is de bovenrand van de
uurgemiddelden onder de knik: wat de pomp ooit leverde, niet wat hij kan.
Ontdooiverdunning, stille modus en ons eigen waterplafond drukken hem, en een
plafond dat de pomp afknijpt leert zo zijn eigen te lage capaciteit. Onder de
koudste meting wordt hij lineair doorgetrokken. Tweakers 24-11-2026: helling
≥ 640 W/K (testhuis 374), bij −2,8 °C ≤ 2,5 kW tegen 4,2 kW verlies; tien uur
`advies_begrensd`, advies 31 → 26 °C, kamer 19,6 → 18,6 °C.

Daarom `min(totaal, max(capaciteit, stationair))`: nooit onder het
warmteverlies. Kan de pomp het verlies echt niet halen, dan levert hij wat hij
kan en kost een hoger advies niets.

De grens zelf blijft nodig: hij is de vangrail tegen adviespieken bij een groot
tekort (nachtverlaging inhalen). Wat de lijn als capaciteit mag zeggen staat in
`freezing_capacity` (analysis/stooklijn.py), het enige pad via `_capacity_w`:

- **Niet doortrekken.** Kouder dan het koudste gemeten uur onder de knik
  (`capacity_min_temp`, na de uitbijterfilter) geen grens. Tweakers 24-11,
  tweede ronde: 832 W/K, knik 0,75 °C, nul bij −4,8 °C.
- **Gemeten ondergrens.** Een uur met P bij T' bewijst minstens P voor elke
  T ≥ T' (`measured_capacity_steps`, trap op `capacity_measured`). Slechte uren
  trekken niets omlaag, dus ook de terugkoppeling via ons eigen plafond niet.
  Testhuis, alleen november 2025: bij −3/−5 °C 4,1/2,9 → 4,3/4,3 kW (hele
  winter 5,6/4,9). Hele cache: −4/−6 °C 5,2/4,5 → 5,7/5,3 kW.
- **Geen stabiliteitsfilter.** `_filter_stable_hours` op de vriescurve getest
  op maandvensters: helling 577 → 518–536 W/K, nauwelijks verschil. Het
  probleem is het aantal uren per vak, niet halve uren.
- De Power House-kalibratie gebruikt de lijn nog rechtstreeks (snijpunt met het
  verlies); daar verandert niets.

## Een blijvend tekort werkt het I-deel weg (`BalanceBias`)

De inhaalterm is een P-regelaar (10·U per K). Zit het warmteverlies te laag
geschat, dan blijft er een vaste afwijking van fout / (11·U) staan; Tweakers
24-11-2026: 0,38 K, vijf uur stabiel. `BalanceBias` telt het tekort op
(`bias += 10·tekort·dt/Ti`) en schuift het balanspunt in `_heat_loss_line`, dus
ook de vooruitblik en het voorspellende advies rekenen ermee.

- **Ti = 48 u.** Regelkring 1R1C met PI: ζ = U(1+N)/(2·√(N·U·C/Ti)). Zware
  vloer (U 219, C 30 kWh/K): ζ 1,0 bij 48 u, 0,7 bij 24 u. Met de 2R2C-vloer
  is de werkelijke kring trager; daarom de ruime keuze. Nagespeeld in
  `TestRegelkring`: geen doorschieten.
- **Alleen leren als max-water op dit advies stuurt.** Stuurt het advies
  niets, dan komt een tekort niet van het advies, en loopt een integrator tot
  zijn grens. Ook niet bij begrensd advies, een tekort > 1 K, of binnen drie
  uur na een setpointwissel (nachtverlaging inhalen).
- Grens ±3 K. Op het testhuis (22–24-11, twee dagen setpointkolom) gemiddeld
  tekort 0,05 K, 's nachts 0,13–0,23 K; een opengelusde naspeling zegt niets
  over de gesloten kring, dus effect daar nog niet gemeten.

## De PV-schaal: eigen monsters, dan bewaard, dan aangevuld, dan boven 10°

Gemeten zon komt uit PV-vermogen gedeeld door een schaal (W per W/m² op het
paneel bij heldere hemel), het 97e percentiel over momenten met de zon boven
15° (`estimate_pv_scale`, minstens 100). In een venster van 30 dagen haalt dat
op 52° NB begin januari 53 momenten, in Groningen geen. Daarom
(`measured_ghi`, v0.10.7-beta.11), in deze volgorde: de eigen
vijfminutenmonsters boven 15°; de laatste schaal die zo bepaald is (per
zonnesensor, `.storage`); met de uuraanvulling erbij boven 15°; idem boven 10°.
Alleen de eerste wordt bewaard.

**Niet ijken op de uuraanvulling.** Een uurgemiddelde, lineair geïnterpoleerd,
vlakt de dag af: per monster op heldere momenten 0,92× de echte waarde, op
donkere 1,36×. Het percentiel pakt de heldere, dus de schaal komt te laag uit
en alle gemeten zon te hoog. Testhuis 13–23-11-2026, dezelfde monsters: 4,32
uit de aanvulling tegen 4,59 uit de vijfminutenstatistiek van de recorder
(−6 %). Live stond 4,18 (1.530 van 1.643 monsters aangevuld); uit de eigen
monsters 4,73. Voor de straling zelf blijft de aanvulling in gebruik (beter
dan geen meting); hoe groot de fout per uur daar is, is niet gemeten.

Gemeten op het testhuis (20–23-11-2026, panelen 195°): 4,75 uit 10–15° tegen
4,74 boven 15°. Met de richting 15° verkeerd (180°): 5,07 tegen 4,76. Lage zon
is dus gevoeliger voor de richting; daarom gaat de bewaarde schaal voor.
Paneelrichting 180 of 195° maakte boven 15° nauwelijks uit (4,76 / 4,74),
dus de bewaarde schaal is niet aan de geleerde richting gekoppeld.

## Perioden ouder dan een week: langetermijnstatistiek, niet de ruwe historie

`state_changes_during_period` leest de ruwe recorderhistorie, en die wordt na
`purge_keep_days` (standaard 10) weggegooid. De gasperiode ligt per definitie
vóór de warmtepomp; GitHub #3 (purge 7 dagen) vond daardoor nooit gasdata.
`analysis/gas.py` leest nu eerst de uurstatistiek (`change` voor de meter,
`mean` voor de buitentemperatuur) en valt alleen terug op de ruwe historie bij
een sensor zonder `state_class`. `start` is in de Python-API een epoch in
seconden (de websocket geeft milliseconden). Wie een nieuwe historische
periode ophaalt: zelfde volgorde.

Dagstatistiek (`period="day"`) loopt van lokale middernacht tot lokale
middernacht; `start` van de dag van 20-11 is 19-11 23:00 UTC. Label met
`dt_util.as_local(...)`, en begin het venster op lokale middernacht. Met de
UTC-datum schoof `_async_fetch_recorder_daily` elke dag één dag terug (en
"vandaag" werd gisteren). Uurstatistiek heeft dit probleem niet.
