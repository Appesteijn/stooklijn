# Caching Implementation - Demo

## Wat is geïmplementeerd

### 1. Cache Helper ([cache.py](custom_components/quatt_stooklijn/cache.py))

Een nieuwe `QuattInsightsCache` class die:
- Data opslaat in Home Assistant's storage (`.storage/quatt_stooklijn_insights_cache`)
- Per dag data cached (key = "YYYY-MM-DD")
- Alleen **voltooide dagen** cached (vandaag wordt niet gecached omdat die data kan veranderen)
- Automatische cleanup van oude cache entries (standaard 1 jaar retention)

### 2. Modified quatt.py

De `async_fetch_quatt_insights` functie is aangepast om:
1. **Cache te checken** voor elke dag
2. **Alleen ontbrekende dagen** op te halen via API
3. **Nieuwe data te cachen** (alleen dagen < vandaag)
4. **Logging** van cache statistieken

## Hoe het werkt

### Eerste run (zonder cache)
```
Periode: 2025-06-01 tot 2026-02-15 (260 dagen)

API calls: 260 ❌ (elk dag wordt opgehaald)
Cache hits: 0
Tijd: ~2-5 minuten (afhankelijk van API snelheid)
```

### Tweede run (met cache)
```
Periode: 2025-06-01 tot 2026-02-15 (260 dagen)

API calls: 1 ✅ (alleen vandaag/nieuwe dag)
Cache hits: 259
Tijd: ~1-2 seconden
```

### Derde run (dag later)
```
Periode: 2025-06-01 tot 2026-02-16 (261 dagen)

API calls: 1 ✅ (alleen de nieuwe dag)
Cache hits: 260
Tijd: ~1-2 seconden
```

## Log Output Voorbeeld

### Eerste run:
```
INFO: Loaded insights cache with 0 days
INFO: Insights data: 260 days total, 0 from cache, 260 from API
INFO: Cache contains 259 days (2025-06-01 to 2026-02-14)
```

### Tweede run:
```
INFO: Loaded insights cache with 259 days
DEBUG: Using cached data for 2025-06-01
DEBUG: Using cached data for 2025-06-02
... (257 more)
DEBUG: Using cached data for 2026-02-14
INFO: Insights data: 260 days total, 259 from cache, 1 from API
INFO: Cache contains 260 days (2025-06-01 to 2026-02-15)
```

## Cache Storage Locatie

De cache wordt opgeslagen in:
```
<config_dir>/.storage/quatt_stooklijn_insights_cache
```

Bijvoorbeeld:
```
/config/.storage/quatt_stooklijn_insights_cache
```

## Cache Structuur

```json
{
  "version": 1,
  "key": "quatt_stooklijn_insights_cache",
  "data": {
    "insights": {
      "2025-06-01": {
        "from": "2025-05-31T22:00:00.000Z",
        "to": "2025-06-01T21:59:59.999Z",
        "totalHpHeat": 125000,
        "totalHpElectric": 45000,
        "averageCOP": 2.8,
        "graph": [...],
        "outsideTemperatureGraph": [...],
        ...
      },
      "2025-06-02": { ... },
      ...
    }
  }
}
```

## Performance Impact

### Zonder caching:
- **90 dagen analyse**: 90 API calls (~2-3 minuten)
- **260 dagen analyse**: 260 API calls (~5-10 minuten)
- Elke analyse duurt lang
- Risk van rate limiting

### Met caching:
- **Eerste run**: Nog steeds langzaam (moet alles ophalen)
- **Alle volgende runs**: ~1-2 seconden ⚡
- **Nieuwe dag toevoegen**: +1 API call
- **API calls per maand**: ~30 (alleen nieuwe dagen)

## Reduction in API Calls

### Scenario: Gebruiker draait analyse 1x per week

**Zonder caching** (over 1 jaar):
```
52 weeks × 260 days = 13,520 API calls 😱
```

**Met caching** (over 1 jaar):
```
First run: 260 calls
Next 51 runs: 51 × 7 new days = 357 calls
Total: 617 API calls ✅

Reduction: 95.4% minder API calls!
```

### Scenario: Gebruiker draait analyse 1x per dag

**Zonder caching** (over 1 jaar):
```
365 × 260 days = 94,900 API calls 😱😱😱
```

**Met caching** (over 1 jaar):
```
First run: 260 calls
Next 364 runs: 364 × 1 new day = 364 calls
Total: 624 API calls ✅

Reduction: 99.3% minder API calls!
```

## Cache Maintenance

De cache wordt automatisch:
- **Geladen** bij eerste gebruik
- **Opgeslagen** na nieuwe API calls
- **Cleanup** kan handmatig worden aangeroepen (standaard: keep 365 days)

Om de cache te clearen (als developer):
```bash
# Verwijder het cache bestand
rm <config_dir>/.storage/quatt_stooklijn_insights_cache
```

## Features

✅ **Smart caching**: Alleen voltooide dagen worden gecached
✅ **Persistent**: Overleeft Home Assistant restarts
✅ **Automatic**: Geen configuratie nodig
✅ **Safe**: Gebruikt HA's officiele storage API
✅ **Efficient**: Alleen nieuwe data wordt opgehaald
✅ **Logging**: Duidelijke logs over cache hits/misses

## Backward Compatible

De caching laag is **volledig transparant**:
- Bestaande code blijft werken
- Geen wijzigingen in coordinator nodig
- Geen wijzigingen in configuratie nodig
- Eerste run werkt exact hetzelfde als voorheen

## Testing

Om te testen of het werkt:
1. Start Home Assistant met de nieuwe code
2. Draai een analyse (eerste keer = langzaam)
3. Check de logs: "Insights data: X days total, 0 from cache, X from API"
4. Draai dezelfde analyse opnieuw (moet snel zijn)
5. Check de logs: "Insights data: X days total, X-1 from cache, 1 from API"

## Toekomstige Verbeteringen

Mogelijke uitbreidingen:
- Cache preloading tijdens HA startup
- Cache expiry voor vandaag (refresh elke X uren)
- Cache statistics sensor (laten zien hoeveel API calls bespaard)
- Manual cache clear service
