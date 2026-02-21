# Knikpuntdetectie — Implementatie en bevindingen

Dit document beschrijft hoe knikpuntdetectie werkt, waarom bepaalde keuzes zijn gemaakt,
en wat experimenten met echte data hebben geleerd.

---

## Wat is het knikpunt?

Het knikpunt is de buitentemperatuur waarbij de warmtepomp overgaat van **maximaal vermogen**
(koud genoeg dat de WP alles moet geven wat erin zit) naar **moduleren** (milder weer, WP draait
op een lager toerental omdat het huis minder warmte nodig heeft).

```
Vermogen (W)
│
│  ████████████████  ← WP op max (koude kant)
│  ████████████████\
│                   \  ← WP moduleert (warme kant)
│                    \
└────────────────────────── Buitentemperatuur (°C)
                    ↑
              knikpunt
```

---

## Algoritme: exhaustieve grid search

De kniktemperatuur wordt gevonden door voor **elke kandidaat-kniktemperatuur** (stappen van
0,25 °C van −4 °C tot +4 °C) de totale regressiefout te berekenen van twee afzonderlijke
lineaire fits (links en rechts van het knikpunt). Het knikpunt met de laagste totale MSE wint.

```python
def _find_knee_by_grid_search(x_data, y_data, ...):
    for knee_t in candidates:          # elke 0,25°C van -4 tot +4
        split left / right             # splits op knee_t
        fit two linear regressions
        # Fysieke beperkingen:
        #  1. warme kant moet negatieve helling hebben
        #  2. koude kant moet vlakker zijn dan warme kant (factor 0,75)
        mse = total_residuals / n_points
        if mse < best_mse: update best
```

**Voordelen ten opzichte van `scipy.curve_fit`** (de vroegere methode):

| Aspect | Vroeger (`curve_fit`) | Nu (grid search) |
|--------|----------------------|-------------------|
| Lokale minima | Gevoelig (startpunt-afhankelijk) | Evalueert alle opties |
| Determinisme | Data-distributie-afhankelijk | Altijd zelfde resultaat |
| Debugbaarheid | Black-box optimizer | Elke kandidaat inspecteerbaar |
| Scipy dependency | Vereist | Niet meer nodig |

---

## Databronnen en hun beperkingen

### Quatt uurdata (`df_hourly`)
- Afkomstig van `quatt.get_insights` API
- Uurgemiddelden van `hpHeat` (warmteproductie WP) en `temperatureOutside`
- Beschikbaar voor de volledige geconfigureerde periode (maanden)

**Fundamenteel probleem voor knikdetectie:**
Defrosts verlagen het uurgemiddelde in de koude zone. Voorbeeld:

```
Uur met defrost bij -2°C:
  45 min × 5.500 W (actief) + 15 min × 0 W (defrost)
  → uurgemiddelde = 4.125 W

Uur zonder defrost bij +5°C:
  60 min × 5.500 W (modulerend)
  → uurgemiddelde = 5.500 W
```

Dit maakt de koude kant systematisch zwakker dan de warme kant,
waardoor de grid search het knikpunt te warm inschat (~3 °C).

Poging om dit te verhelpen met een max-envelopfilter (houd ≥ 90 % van
het binmaximum) mislukte: slechts 160 van de 4.687 uurpunten bleven over,
waardoor de stabiliteit juist verslechterde (Δ 1,25 °C vs. Δ 0,75 °C).

### HA recorder minuutdata (`df_ha_merged`)
- Afkomstig van `state_changes_during_period` (ingebouwde HA recorder)
- Alle statuswijzigingen van het vermogensensor, opgeresampeld naar 1-minuut raster
- Periode: laatste `DAYS_HISTORY` (standaard 30) dagen

**Voordeel voor knikdetectie:**
Elke defrost-minuut (vermogen ≈ 0 W) valt individueel onder de `MIN_POWER_FILTER`
en wordt uitgefilterd, **zonder de omliggende minuten te beïnvloeden**.
Hierdoor is de koude kant volledig onbeïnvloed door defrosts.

**Nadeel:**
Beperkt tot de laatste 30 dagen. Bij een lange warme periode zijn er
mogelijk weinig of geen datapunten onder het knikpunt — in dat geval
wordt geen geldig knikpunt gevonden en wordt teruggevallen op Quatt-data.

---

## Prioriteitsvolgorde knikdetectie

```
1. HA recorder minuutdata (primair)
   ↓ nauwkeuriger (geen defrost-verdunning)
   ↓ geeft ~1,75 °C op echte data (stabiel)
   Als geen geldig knikpunt gevonden ↓

2. Quatt uurdata (fallback)
   ↓ langere geschiedenis (maanden)
   ↓ geeft ~3 °C op echte data (opwaartse bias door defrost-verdunning)
   Als geen geldig knikpunt gevonden ↓

3. Fallbacktemperatuur: −0,5 °C
```

---

## Validatieresultaten (februrari 2026, echte HA-data)

Tests uitgevoerd met `test_knee_detection.py` op echte HA-server data.

### Dataset
- Quatt uurdata: 2025-08-01 → 2026-02-20 (204 dagen, 4.687 uurpunten)
- Recorder minuutdata: laatste 30 dagen (~15.000 minuten, 12.259 actief ≥ 2.500 W)

### Effect van toevoegen van 7 extra warme dagen (2026-02-14 t/m 2026-02-20)

| Methode | Vóór (t/m 13-feb) | Ná (t/m 20-feb) | Δ |
|---------|-------------------|-----------------|---|
| `curve_fit` op recorder (oud) | 0,03 °C | — | instabiel |
| Grid search op Quatt (rolling-std filter) | +3,75 °C | +3,00 °C | 0,75 °C |
| Grid search op recorder | **+1,75 °C** | **+1,75 °C** | **0 °C ✓** |

De recorder-gebaseerde knikdetectie is stabiel: het knikpunt verandert niet
bij het toevoegen van extra warme data.

### Waarom sprong het knikpunt eerder van 0,2 → 3,0 °C?

Twee factoren tegelijk:
1. **Methode-switch**: bij geen of weinig Quatt-data werd `curve_fit` op recorder
   gebruikt (onstabiel, gaf 0,03 °C). Zodra voldoende Quatt-data beschikbaar was,
   schakelde het systeem over op Quatt uurdata met `curve_fit` (~3 °C).
2. **Optimalisator-gevoeligheid**: `curve_fit` op een stuksgewijs lineair model
   heeft meerdere lokale minima. Het startpunt (`p0`) bepaalt welk minimum gevonden
   wordt. Beide zijn nu vervangen door grid search.

---

## Implementatiegeschiedenis

### v0.x — `curve_fit` op Quatt uurdata
- Gebruikt `scipy.optimize.curve_fit` met piecewise lineair model
- Gevoelig voor startpunt → lokale minima → grote sprongen bij data-wijzigingen
- Verwijderd: onstabiel

### v0.x+1 — Grid search (eerste versie)
- Vervangt `curve_fit` door exhaustieve grid search (−4 t/m +4 °C, stap 0,25 °C)
- Fysieke beperking: warme kant moet negatieve helling hebben
- Quatt uurdata primair, recorder als fallback
- Resultaat: stabiel maar te hoge waarde (~3 °C) door defrost-dilutie

### Huidig — Grid search met verbeterde beperkingen + recorder primair

**Wijzigingen in `_find_knee_by_grid_search()`:**
1. Extra fysieke beperking: koude-kant helling mag niet meer dan 75 % van de
   warme-kant helling zijn (verwerpt quasi-rechte lijnen als knik).
2. MSE genormaliseerd over totaalpunten (niet per segment), zodat een
   klein koud-segment niet kunstmatig bevoordeeld wordt.

**Wijziging in `calculate_stooklijn()`:**
- HA recorder minuutdata is nu **primair** voor knikdetectie.
- Quatt uurdata is **fallback** (wanneer recorder onvoldoende koud-weer-data heeft).

---

## Geteste maar teruggedraaide aanpak: max-envelopfilter

**Idee:** vervang de rolling-std filter (spillover bij defrosts) door een filter
die per 0,5 °C-bin alleen uren ≥ 90 % van het binmaximum behoudt.

**Probleem:** slechts 160 van 4.687 uurpunten (3,4 %) bleven over. Gevolgen:
- Te weinig punten voor stabiele regressie.
- Non-monotoon gedrag: toevoegen van data kan punten uit andere bins verwijderen
  (omdat het binmaximum omhoog gaat → 90 %-drempel stijgt → eerder geaccepteerde
  punten vallen er nu uit). Dit maakt de methode juist onstabiel.
- Stabiliteit verslechterde: Δ 1,25 °C vs. Δ 0,75 °C met de rolling-std filter.

**Conclusie:** niet productie-ready. Het fundamentele probleem (defrost-dilutie van
uurgemiddelden) is niet oplosbaar door filtering — de recorder minuutdata is de
juiste databron voor knikdetectie.

---

## Temperatuurbereik grid search: −4 t/m +4 °C

Gebaseerd op Quatt Hybrid specificaties:
- Bij −7 °C: nominaal 5,6 kW, max 6,7 kW, COP 3,2
- Bij +7 °C: ~5,1 kW (W45), COP 3,8

Het knikpunt hangt af van de **warmtevraag van het huis** bij de betreffende
temperatuur. Voor een Nederlands huis is 0–3 °C een realistisch bereik.
Uitbreiden naar [−7, +7] is mogelijk maar zelden nodig.

---

## Codeverwijzingen

| Functie | Bestand | Doel |
|---------|---------|------|
| `_find_knee_by_grid_search()` | `analysis/stooklijn.py` | Grid search algoritme |
| `_filter_stable_hours()` | `analysis/stooklijn.py` | Quatt-data filter (fallback-pad) |
| `_perform_knee_detection_quatt()` | `analysis/stooklijn.py` | Knikdetectie op Quatt uurdata |
| `calculate_stooklijn()` STEP 1 | `analysis/stooklijn.py` | Prioriteit recorder → Quatt |
| `test_knee_detection.py` | projectroot | Validatiescript met echte HA-data |
