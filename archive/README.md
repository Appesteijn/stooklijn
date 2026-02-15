# Quatt Warmteanalyse

Analyse-tool voor het berekenen van de stooklijn en het warmteverliescoëfficiënt van je woning op basis van data uit Home Assistant. Ontworpen voor gebruikers van een Quatt warmtepomp, maar ook bruikbaar voor gasverwarmingsdata.

## Wat doet het?

Dit Jupyter notebook haalt verwarmingsdata op uit Home Assistant en analyseert de relatie tussen buitentemperatuur en warmtevraag. Het berekent:

- **Stooklijn** — de optimale verwarmingscurve voor je woning
- **Warmteverliescoëfficiënt** — hoeveel warmte je woning verliest per °C temperatuurverschil
- **COP-analyse** — hoe efficiënt je warmtepomp presteert bij verschillende buitentemperaturen
- **Capaciteitsanalyse** — of je warmtepomp voldoende capaciteit heeft bij vriestemperaturen
- **Warm water correctie** — schat automatisch het gasverbruik voor warm water en trekt dit af van de verwarmingsvraag (gas-gebruikers)

## Ondersteunde configuraties

| Type | Cell 2 (warmtepomp) | Cell 2B (gas) | Omschrijving |
|---|---|---|---|
| 🔵 Warmtepomp | ✅ | ⏭️ Skip | Quatt warmtepomp gebruikers |
| 🟠 Gas | ⏭️ Skip | ✅ | Alleen gasverwarming |
| 🟣 Overstap | ✅ | ✅ | Gas → warmtepomp (vergelijking) |

> ⚠️ **Overstappers:** Zorg dat de datumperiodes van Cell 2 en Cell 2B **niet overlappen**. Gebruik Cell 2B voor de periode *vóór* de warmtepomp en Cell 2 voor de periode *erna*.

## Vereisten

- Python 3.x
- Home Assistant met [Quatt integratie](https://github.com/marcoboers/home-assistant-quatt)
- Een long-lived access token van Home Assistant

### Installatie

```bash
pip install pandas numpy scipy matplotlib seaborn requests openpyxl python-dotenv
```

## Configuratie

Kopieer het voorbeeldbestand en vul je eigen gegevens in:

```bash
cp .env.example .env
```

Bewerk `.env` met je Home Assistant URL en token:

```
HA_URL=https://jouw-homeassistant-url.com
TOKEN=jouw-long-lived-access-token
```

Je kunt een token aanmaken via **Home Assistant > Profiel > Beveiliging > Langlevende toegangstokens** of ga direct naar [http://homeassistant.local:8123/profile/security](http://homeassistant.local:8123/profile/security).

## Gebruik

### Optie 1: JupyterLab add-on in Home Assistant

Je kunt dit notebook direct in Home Assistant draaien via de [JupyterLab add-on](https://github.com/hassio-addons/addon-jupyterlab). Upload het notebook en het `.env` bestand naar JupyterLab en voer de cellen uit. Het voordeel is dat je geen aparte Python-installatie nodig hebt en dat de API-verbinding met Home Assistant lokaal blijft.

### Optie 2: Lokaal draaien

```bash
jupyter notebook "Quatt stooklijn v4.ipynb"
```

Het notebook werkt incrementeel — eerder opgehaalde data wordt opgeslagen in CSV-bestanden en bij een volgende run alleen aangevuld met ontbrekende dagen.

### Configureerbare parameters in het notebook

| Parameter | Standaard | Beschrijving |
|---|---|---|
| `START_DATE` | `2025-08-01` | Startdatum voor warmtepomp data |
| `END_DATE` | `2026-02-13` | Einddatum voor warmtepomp data |
| `GAS_START_DATE` | `2025-08-01` | Startdatum voor gasdata |
| `GAS_END_DATE` | `2026-02-13` | Einddatum voor gasdata |
| `MIN_POWER_FILTER` | `2500` W | Minimaal vermogen om meting mee te nemen |
| `BIN_SIZE` | `0.5` °C | Breedte van temperatuurbins |
| `GAS_CALORIFIC_VALUE` | `9.77` kWh/m³ | Calorische waarde van gas |
| `BOILER_EFFICIENCY` | `0.90` | Rendement van de ketel |
| `HOT_WATER_OUTSIDE_TEMP_THRESHOLD` | `18.0` °C | Buitentemperatuur waarboven alleen warm water wordt verbruikt |

## Uitvoer

Het notebook genereert:

- **Grafieken** — stooklijn, COP-curves, capaciteitsanalyse, warmteverlies
- **quatt_hourly.csv** — uurdata van de warmtepomp
- **quatt_daily.csv** — dagelijkse samenvatting
- **quatt_insights_data.xlsx** — gecombineerd Excel-bestand
- **gas_hourly.csv** — uurdata gasverbruik (alleen bij gas-gebruikers)
- **gas_daily.csv** — dagelijkse gasverbruik samenvatting (alleen bij gas-gebruikers)
