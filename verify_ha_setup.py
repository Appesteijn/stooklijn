"""
Verify Home Assistant setup for Quatt Stooklijn integration.

Reads credentials from archive/.env and checks:
  1. HA connectivity
  2. All entities referenced in the dashboard YAML
  3. All required input sensors (temp, power, flow, recorder)
  4. Discovery of all quatt-related entities on the server
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

ENV_FILE = Path(__file__).parent / "archive" / ".env"
DASHBOARD_FILE = Path(__file__).parent / "dashboards" / "quatt_stooklijn_dashboard.yaml"


def load_env(path: Path) -> dict[str, str]:
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


try:
    env = load_env(ENV_FILE)
except FileNotFoundError:
    print(f"[ERROR] .env niet gevonden op: {ENV_FILE}")
    sys.exit(1)

HA_URL = env.get("HA_URL", "").rstrip("/")
TOKEN = env.get("TOKEN", "")

if not HA_URL or not TOKEN:
    print("[ERROR] HA_URL of TOKEN ontbreekt in .env")
    sys.exit(1)

print(f"HA_URL : {HA_URL}")
print(f"TOKEN  : {TOKEN[:20]}...{TOKEN[-6:]}")
print()

# ---------------------------------------------------------------------------
# HA REST helper
# ---------------------------------------------------------------------------

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}


def ha_get(path: str) -> dict | list | None:
    url = f"{HA_URL}/api{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [HTTP {e.code}] {url} — {body[:120]}")
        return None
    except urllib.error.URLError as e:
        print(f"  [URLError] {url} — {e.reason}")
        return None


# ---------------------------------------------------------------------------
# 1. Connectivity check
# ---------------------------------------------------------------------------

print("=" * 60)
print("1. Verbinding met Home Assistant")
print("=" * 60)

api_info = ha_get("/")
if api_info is None:
    print("[FOUT] Kan geen verbinding maken. Controleer HA_URL en TOKEN.")
    sys.exit(1)

print(f"  OK — HA versie: {api_info.get('version', 'onbekend')}")
print()

# ---------------------------------------------------------------------------
# 2. Fetch all states once
# ---------------------------------------------------------------------------

print("Alle states ophalen...", end=" ", flush=True)
all_states_list = ha_get("/states") or []
all_states: dict[str, dict] = {s["entity_id"]: s for s in all_states_list}
print(f"{len(all_states)} entiteiten geladen.")
print()


def check_entity(entity_id: str) -> tuple[str, str]:
    """Return (mark, status_string) for an entity_id."""
    if entity_id not in all_states:
        return "?", "NIET GEVONDEN"
    state = all_states[entity_id].get("state", "?")
    unit = all_states[entity_id].get("attributes", {}).get("unit_of_measurement", "")
    if state in ("unavailable", "unknown", "None"):
        return "!", f"NIET BESCHIKBAAR  (state={state})"
    return "v", f"{state} {unit}".strip()


# ---------------------------------------------------------------------------
# 3. Dashboard entities check
# ---------------------------------------------------------------------------

print("=" * 60)
print("2. Entiteiten gebruikt in het dashboard")
print("=" * 60)

dashboard_entities: list[str] = []
if DASHBOARD_FILE.exists():
    content = DASHBOARD_FILE.read_text()
    # Extract all entity: ... lines and sensor/text refs inside templates
    dashboard_entities = sorted(set(re.findall(r"\b((?:sensor|text|input_\w+)\.\w+)", content)))
    print(f"  {len(dashboard_entities)} unieke entiteiten gevonden in {DASHBOARD_FILE.name}")
    print()

    dash_ok = dash_warn = dash_miss = 0
    for eid in dashboard_entities:
        mark, status = check_entity(eid)
        if mark == "v":
            dash_ok += 1
        elif mark == "!":
            dash_warn += 1
        else:
            dash_miss += 1
        print(f"  [{mark}] {eid}")
        print(f"       -> {status}")

    print()
    print(f"  Dashboard samenvatting: {dash_ok} ok, {dash_warn} niet beschikbaar, {dash_miss} ontbreekt")
else:
    print(f"  Dashboard YAML niet gevonden: {DASHBOARD_FILE}")
    print("  (sla dit over)")

print()

# ---------------------------------------------------------------------------
# 4. Required input sensors
# ---------------------------------------------------------------------------

INPUT_SENSORS = {
    "sensor.heatpump_hp1_temperature_outside": ("Buitentemp HP1 (primair)", False),
    "sensor.heatpump_hp2_temperature_outside": ("Buitentemp HP2", True),
    "sensor.thermostat_temperature_outside":   ("Buitentemp thermostaat", True),
    "sensor.heatpump_total_power":             ("Totaalvermogen", False),
    "sensor.heatpump_total_power_input":       ("Vermogen input (recorder)", False),
    "sensor.heatpump_total_quatt_cop":         ("Quatt COP (recorder)", False),
    "sensor.heatpump_boiler_heat_power":       ("Boiler warmtevermogen (recorder)", False),
    "sensor.heatpump_flowmeter_flowrate":      ("Debiet (l/h)", False),
    "sensor.heatpump_hp1_temperature_water_in": ("Retourtemperatuur", False),
}

print("=" * 60)
print("3. Vereiste input-sensoren")
print("=" * 60)

inp_ok = inp_warn = inp_fail = 0
for eid, (label, optional) in INPUT_SENSORS.items():
    mark, status = check_entity(eid)
    tag = "(optioneel)" if optional else "(vereist) "
    if mark == "v":
        inp_ok += 1
    elif mark == "!" or (mark == "?" and optional):
        inp_warn += 1
    else:
        inp_fail += 1
    print(f"  [{mark}] {tag} {eid}")
    print(f"       {label} -> {status}")

print()
print(f"  Input samenvatting: {inp_ok} ok, {inp_warn} waarschuwing, {inp_fail} ontbreekt")
print()

# ---------------------------------------------------------------------------
# 5. Discovery — all quatt entities on the server
# ---------------------------------------------------------------------------

print("=" * 60)
print("4. Alle quatt-entiteiten op de server (discovery)")
print("=" * 60)

quatt_entities = {
    eid: s for eid, s in all_states.items()
    if "quatt" in eid.lower()
}

if quatt_entities:
    for eid in sorted(quatt_entities):
        s = quatt_entities[eid]
        state = s.get("state", "?")
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        in_dashboard = "  [dashboard]" if eid in dashboard_entities else ""
        print(f"  {eid}: {state} {unit}{in_dashboard}")
else:
    print("  Geen quatt-entiteiten gevonden op de server.")
    print("  Is de quatt_stooklijn integratie geinstalleerd en geconfigureerd?")

print()

# ---------------------------------------------------------------------------
# Totaaloverzicht
# ---------------------------------------------------------------------------

print("=" * 60)
print("Totaaloverzicht")
print("=" * 60)

if DASHBOARD_FILE.exists():
    total_issues = dash_miss + inp_fail
    total_warn = dash_warn + inp_warn
    if total_issues == 0 and total_warn == 0:
        print("  Alles in orde! Alle sensoren gevonden en beschikbaar.")
    elif total_issues == 0:
        print(f"  Grotendeels in orde. {total_warn} waarschuwing(en) — zie details hierboven.")
    else:
        print(f"  {total_issues} sensor(en) ontbreekt volledig, {total_warn} niet beschikbaar.")
        print("  Zie details hierboven.")
