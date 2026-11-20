# Zonnewarmte voorspellen en gebruiken bij eigen aansturing

Onderzoek 20-11-2026 op één huis (n = 1), hieronder "het gemeten huis". Vraag: hoe voorspellen we de
warmte die de zon het huis binnenbrengt, en hoe houdt een eigen aansturing van
de warmtepomp rekening met de opwarming die de zon het komende uur (of de
komende uren) gaat leveren?

De analysescripts draaiden offline op een export van het meetlogboek en de
recorderstatistiek; ze staan niet in de repo.

## Data

- Uurstatistieken sep 2025 – nov 2026: warmte (WP + ketel), kamer, buiten,
  PV (`solaredge_ac_power` → `solaredge_i1_ac_power`). 17 panelen, alle 180°.
- `highres_log`: 20.576 vijfminutenmonsters, 08-09 t/m 20-11-2026, met de
  Open-Meteo-straling (`shortwave_radiation`, horizontaal) zoals de integratie
  hem gebruikte.
- Ramen: hoofdgevel ZZW (zon op de gevel bij azimut ~130–290°, zie de
  zonneschermautomatisering). Het zonnescherm gaat alleen uit bij warmte, dus in
  het stookseizoen stoort het weinig.

## Bevindingen

### 1. De zon in de integratie loopt een uur achter (fout, los te herstellen)

Open-Meteo levert `shortwave_radiation` als gemiddelde over het *voorafgaande*
uur, met het eind van dat uur als stempel. `_solar_radiation_at()` zoekt op
`%H:00` van het lopende uur en pakt dus het uur ervóór.

Getoetst tegen de PV-opbrengst: de gelogde straling van uur H correleert het
best met PV van uur H−1 (0,85, tegen 0,79 voor H); het zwaartepunt van de dag
ligt 0,8 u later dan bij PV. Alle modellen leren en voorspellen tot nu toe met
een zon die een uur te laat komt.

Herstel: voor uur H de waarde met stempel H+1 nemen, of
`shortwave_radiation_instant` opvragen en naar het midden van het uur
interpoleren. In de 2R2C-fit gaf alleen deze correctie gemengde uitkomsten
(0,266 → 0,246 K normaal, 0,270 → 0,278 omgedraaid); de correlatie met PV is
wel eenduidig.

### 2. Horizontale straling met één vaste zonfactor klopt niet over de seizoenen

Dagbalans (U vast 284,8, warmte = U·ΔT − P − k·zon + opslag), stookdagen:

| periode | kWh warmte bespaard per kWh PV |
|---|---|
| nov–jan | 3,0 (2,4–3,7) |
| okt | 3,2 (2,0–4,2) |
| feb–mrt | 1,5 (1,1–1,8) |
| apr–mei | 0,7 (0,4–1,1) |

Een lage winterzon schijnt bijna loodrecht door verticaal glas en valt schuin
op de panelen; een hoge voorjaarszon andersom. Met horizontale straling loopt de
zonfactor per seizoen uiteen van 3,7 tot 24 m² (×6,5). Omgerekend naar een
verticale gevel (azimut 200, met glas-IAM) is dat 4,2–12,6 m² (×3), en de
dagfout daalt van 564 naar 516 W. Gevelazimut 180–215° geeft hetzelfde: de
richting hoeft niet precies bekend te zijn.

Het restverschil komt van **verzadiging**: op dagen met echte zon gaat de
warmtepomp in de zonuren uit (in elk seizoen). Het overschot verwarmt niets
nuttigs, de kamer loopt op naar ~21,2 °C. Alleen op die dagen gemeten ligt de
zonfactor dicht bij elkaar (3,8 / 6,7 / 7,4 / 6,3 m²). Dagen waarop de WP
doorliep hadden te weinig zon om iets te meten. Een dagbalans kan de zonwinst
dus niet zuiver meten; dat moet met een dynamisch model.

### 3. Gemeten zon op de gevel maakt het 2R2C 14–20 % beter

2R2C met de echte fitcode (week-splitsing, leren op 5 min, toetsen per uur,
12 u vooruit):

| zoninvoer | normaal | omgedraaid | g (m²) |
|---|---|---|---|
| V0 horizontaal, 1 u te laat (nu) | 0,266 | 0,270 | 18,0 / 14,3 |
| V1 horizontaal, op tijd | 0,246 | 0,278 | 15,8 / 14,2 |
| V2 gevel uit Open-Meteo | 0,258 | 0,285 | 9,5 / 10,4 |
| **V3 gevel uit eigen PV** | **0,228** | **0,215** | **14,3 / 16,0** |
| niets doen | 0,461 | 0,320 | |

- V3 wint in beide splitsingen, en de zonfactor ligt dicht bij elkaar. U en
  P_int blijven onstabiel (P_int 0 in de omgedraaide splitsing); dat lost de zon
  niet op.
- V2 helpt niet: de fout in de wolkenverwachting is groter dan wat de geometrie
  wint.
- De absolute zonfactor is onzeker: de dagbalans (verzadigd) zegt 4–7 m², het
  2R2C 14–16. Alle kWh-getallen hieronder rekenen met 15 en schalen daarmee.

Dit weerspreekt een eerdere keuze: `CONF_SOLAR_ENTITY` (PV-sensor) is vervallen
omdat W/m² "een fysische maat" is en PV collineair met de buitentemperatuur zou
zijn. Gemeten is PV, omgerekend naar de gevel, de beste zoninvoer. Het
paneeltemperatuureffect (~0,4 %/K) valt weg tegen wolken.

### 4. Hoe goed is de zonverwachting

In watt zonnewarmte (g = 15), 667 daguren sep–nov 2026, waarheid = PV omgerekend
naar de gevel:

| bron | bias | MAE |
|---|---|---|
| Open-Meteo, lopend uur | +767 W | 1.463 W |
| helderheid van nu aanhouden, 1 u vooruit | −158 W | 960 W |
| idem, 2 u | −319 W | 1.322 W |
| idem, 3 u | −501 W | 1.476 W |
| idem, 6 u | −750 W | 1.598 W |
| vaste helderheid (klimatologie) | +791 W | 1.967 W |

Gemiddelde zonnewarmte overdag: 2,2 kW.

- Open-Meteo schat ~35 % te hoog (dagtotaal 27,3 vs 20,2 kWh; correlatie per dag
  0,82). Of dat Open-Meteo is of mijn omrekening van PV doet er voor het ontwerp
  niet toe: de correctie wordt online geleerd tegen dezelfde meting.
- Voor het eerstvolgende uur is "helderheid van nu aanhouden" beter dan
  Open-Meteo; rond 2 u gelijk, daarna wint Open-Meteo.
- **Niet gemeten:** hoe goed Open-Meteo 3–6 u vooruit is. Er is alleen de
  waarde voor het lopende uur bewaard. Waarschijnlijk iets slechter dan
  hierboven.
- Over blokken van 3 uur, na biascorrectie (×0,76): fout 41 % van het gemiddelde.
  Plannen met 100 % van de verwachting is in 33 % van de blokken >1 kWh te
  optimistisch, met 70 % in 17 %, met 50 % in 9 %.
- **Wanneer de zon van de gevel verdwijnt is geometrie**, en dus zeker. Onzeker
  is alleen of het dan nog zonnig is.

### 5. Wat het oplevert

Zonnige stookdagen (>10 kWh zonnewarmte, n = 113): kamer max 21,17 °C tegen
20,24 op grijze dagen; 3,0 graaduren boven 20,3 °C per dag → ~0,9 kWh extra
warmteverlies per dag. Bij het gemeten huis (radiatoren, C ~11 kWh/K) is de **energiewinst
van vooruitkijken klein** (orde 0,2 kWh stroom per zonnige dag). De winst zit in
comfort (geen 21+ °C midden op de dag) en in huizen met veel massa en een kleine
warmtepomp, waar achteraf reageren te laat is: Tweakers 20-11, ~21 kWh/K,
Quatt 4,5 kW, 0,8 °C inhalen duurt ~6 u. Daar is het 's avonds koud na een
zonnige dag.

## Ontwerp voor eigen aansturing

### Zoninvoer

1. **Leren** (2R2C, en het 1R1C zolang dat er is): gemeten zon.
   - Optionele rol "zonnesensor": PV-vermogen (of een stralingsmeter). Per
     monster helderheid = PV / heldere-hemel-PV (schaal geleerd als hoog
     percentiel), daarna naar het vlak van de ramen.
   - Zonder zonnesensor: Open-Meteo op het juiste uur (bevinding 1).
   - PV in het `highres_log` opnemen als zesde kolom; geen state-listener
     (dat was de renderlast die destijds meewoog).
2. **Voorspellen:** Open-Meteo `global_tilted_irradiance` met `tilt=90` en de
   gevelazimut (Open-Meteo: 0 = zuid), in plaats van zelf horizontaal omrekenen.
   - Biascorrectie online: factor = gemeten / verwacht over ~14 dagen
     (hier ~0,76), alleen met zonnesensor.
   - 0–2 u vooruit: helderheid van nu aanhouden (met zonnesensor), daarna
     overvloeien naar Open-Meteo.
   - Eén gevel met instelbare azimut volstaat voor het gemeten huis (180–215 even goed).
     Huizen met oost- én westglas: later twee vlakken met elk een eigen g.
3. **Geometrie apart gebruiken:** het tijdstip waarop de zon de gevel verlaat
   (azimut > ~290° of elevatie < ~5°) is zeker en hoort als harde rand in de
   planning.

### Model

2R2C met de gevelzon als invoer. Zon grotendeels op de snelle knoop (α ≈ 1
gefit). De stationaire balans (U, P_int) niet uit de 2R2C-fit maar uit de
seizoensregressie, zoals het advies nu ook doet; het 2R2C alleen voor de
dynamiek. Anders ruilt de fit zon, P_int en U tegen elkaar uit.

### Beslissen (MPC, terugkijkend elk kwartier opnieuw plannen)

- Doel: kosten (prijs/COP) + comfortstraf (onder setpoint zwaar, boven
  setpoint + band licht) + schakelstraf.
- **Asymmetrisch vertrouwen in de zon:** een beslissing die minder stookt omdat
  er zon komt, rekent met ~60–70 % van de (gecorrigeerde) verwachting. Te koud
  weegt zwaarder dan iets te warm; bij 70 % is 17 % van de 3-uursblokken >1 kWh
  te optimistisch, en de kamermeting stuurt bij.
- Wat dat oplevert:
  - 's ochtends minder stoken als er een zonnige middag aankomt (kamer mag tot
    setpoint − 0,2 °C zakken, de zon haalt hem op);
  - 's middags in een zwaar huis blijven bijladen tot de zon de gevel verlaat,
    in plaats van uit te gaan zodra de lucht warm is;
  - 's avonds geen tekort.
- Vangnet: nooit onder de comfortgrens plannen op grond van zon; zonterm uit
  als de verwachting verouderd is of als verwachting en meting structureel
  uiteenlopen.

## Stand 20-11-2026 (avond): stap 1–3 gebouwd

Uurverschuiving hersteld, verwachtingen per vooruitkijktijd bewaard en gescoord
(sensor `zonverwachting`), zonnesensor als enige instelling. Richtingen worden
geleerd in plaats van gevraagd:

- **Panelen** uit een jaar PV-uurstatistiek: bij de juiste richting is de
  heldere-hemel-omhullende (95e percentiel van PV / heldere-hemel-PV per maand
  en uur) het vlakst. Het gemeten huis, een jaar: 180° (gelijk aan de omvormer); alleen de
  laatste 90 dagen: 195°. Helling vast op 30° (15–35° gaf geen verschil).
- **Ramen** grof, in de modelvergelijking: kandidaten 90/135/180/225/270, beste
  op het gemiddelde van beide week-splitsingen. Gemeten huis: 90 0,287 · 135 0,230
  · 180 0,225 · 225 0,219 · 270 0,247 K → 225° (gevel ZZW ≈ 205°). Fijner gaat
  niet: per splitsing sprong het optimum tussen 135° en 250°. Tien fits, 16 s op
  een pc, eens per etmaal.
- Valkuil bij offline analyse: pandas-tijdstempels kunnen in µs staan. Mijn
  eerste script deelde door 10⁹ en vond daardoor schijnbaar "west 270°". De
  integratie rekent met seconden en heeft dit niet.

## Volgende stappen, in volgorde

1. **Uurverschuiving herstellen** (bevinding 1). Klein, losstaand.
2. **Verwachtingen bewaren per vooruitkijktijd**: bij elke Open-Meteo-ophaling
   de waarden voor +1…+12 u vastleggen. Nu is niet te meten hoe goed de
   verwachting 3–6 u vooruit is, en dat is precies de horizon van de
   aansturing.
3. **Zonnesensor als optionele rol + PV in het `highres_log`**, en het 2R2C op
   de gevelzon laten leren (achter de bestaande modelvergelijking, zodat het
   alleen wint als het op weggehouden data beter is).
4. **Toetsen in het voorjaar** (feb–apr 2027): hoge zon, verzadiging, en de
   vijfminutendata die er nu nog niet is.
5. Pas dan de zon in de aansturing, met het asymmetrische vertrouwen hierboven.

## Onzekerheden

- Eén huis; het 2R2C-experiment beslaat sep–nov (lage zon).
- Absolute zonfactor onzeker (4–16 m²), zie bevinding 3.
- De omrekening PV → helderheid → gevel is grof (paneelhelling 30° aangenomen,
  schaal als 97e percentiel, Erbs-splitsing direct/diffuus).
- Een eventuele verschuiving van de Open-Meteo-run binnen het uur is niet
  onderzocht.
