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
- **het 1R1C**: dat leert per uur, maar uit één momentopname op de timertik,
  niet uit een gemiddelde. In oktober 2026 gemeten: de warmtepomp draait in
  stabiele blokken (±5% binnen het uur), en de fout zit in de ~4–5
  overgangsuren per dag.

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
  nul, en de blokupdate van `RLSEstimator` ontregelt P dan. Uitgeprobeerd: op
  het koude-starthuis liep C negatief.
- **P_c staat vast op de warmteverliesanalyse** (sinds v0.10.7-beta.2):
  P_c = U × (mediaan kamer 14 d − balanspunt), bijgewerkt bij elke analyse,
  net als het U-anker. Vrij geleerd (θ₄ = P_c/C) groeide P_c in watt mee met
  C: 1.406 W tegen ~970 W uit het balanspunt, en het 1R1C voorspelde de kamer
  na 12 uur 0,7 °C te warm (slechter dan niets doen, 0,494 tegen 0,461 K).
  Nagespeeld met de echte RLS-toestand en het logboek (23-10 t/m 20-11-2026):
  met anker C stabiel 12.000–14.700 Wh/K, U 283–287, testfout 0,409 K;
  zonder anker zwierf C van 6.600 tot 18.300 en U van 235 tot 377, testfout
  0,662 K. Met anker telt P_c als bekende warmte in de regressor van θ₃ en
  heeft θ₄ géén regressor (x[3] = 0, waarde 0, kruiscovarianties op nul). Dat
  is iets anders dan het bevriezen hierboven: er is niets met een regressor
  die nooit nul is. De startwaarde (`seed_constant_gain`) blijft alleen voor
  installaties zonder balanspunt.
- Met anker telt θ₄ **niet** mee voor de convergentie (`_n_updates_min`). Zijn
  teller loopt niet op, en in v0.10.7-beta.2 bleef een nieuw model daardoor
  voorgoed op `batch_fallback` (`TestConvergentieMetAnker`).

## Op vijfminutendata niet één stap vooruit fitten

Het gevolg van de vorige regel. De kamertemperatuur komt in 0,01 K binnen
maar flakkert ±0,015 K, ongeveer evenveel als de kamer per vijf minuten
opwarmt. Een eenstapsfit leest die ruis als snelle dynamiek. Op een
synthetisch huis met blokverwarming kwam τ_snel daardoor op 1,1 u uit in
plaats van 3,6 u. Fit daarom over `FIT_HORIZON_HOURS` (6 u) vooruit. De tabel
staat in `analysis/thermal_model_2r2c.py`, de test in `TestFithorizon`.

## De vooruitblik rekent elk uur zoals het advies

Uur +0 is het advies van nu, met en zonder huismodel. Beide paden gebruiken
`advice_demand`: `simulate_advice_forward` met de kamer uit het 1R1C,
`advice_forward_without_model` zonder (v0.10.7-beta.4). Geef de terugval geen
eigen berekening: tot beta.4 rekende hij met de stooklijn, zon eraf, zonder
inhaalterm of capaciteitsgrens, en een gebruiker las de lege "T binnen"
naast een afwijkend +0 als "de aansturing negeert de kamer". Zonder huismodel
telt de inhaalterm alleen in uur 0: hoe snel een tekort verdwijnt hangt van C
af, en een aangenomen C zit bij een zwaar huis al gauw een factor 2 mis.
