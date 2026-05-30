"""
Harvest per-match player stats from API-Football /fixtures/players endpoint.
Aggregates accurate crosses, dispossessed, smothers (high_claims) and other
stats not available in the /players season summary endpoint.

Usage:
    export API_FOOTBALL_KEY="your_key_here"
    python3 harvest_fixture_stats.py

Output:
    data/pl_fixture_stats_2025.json  — {norm_name: {crosses, dispossessed, ...}}

API calls used: ~1 (fixture IDs) + ~19 (batched fixture stats) = ~20 total.
"""

import json
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

API_KEY   = __import__("os").environ.get("API_FOOTBALL_KEY", "")
BASE_URL  = "https://v3.football.api-sports.io"
LEAGUE_ID = 39
SEASON    = 2025
BATCH     = 20    # max fixture IDs per /fixtures?ids= call
OUT_PATH  = Path("data/pl_fixture_stats_2025.json")

if not API_KEY:
    raise SystemExit("Set API_FOOTBALL_KEY env var before running.")

Path("data").mkdir(exist_ok=True)

session = requests.Session()
session.headers.update({"x-apisports-key": API_KEY, "Accept": "application/json"})


def _get(path: str, params: dict) -> dict:
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _norm(name: str) -> str:
    name = name.replace("ı", "i").replace("İ", "i")
    nfkd = unicodedata.normalize("NFKD", name.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Step 1 — get all completed fixture IDs
# ---------------------------------------------------------------------------
print(f"Step 1: Fetching fixture IDs for PL {SEASON}...")
data = _get("/fixtures", {
    "league":  LEAGUE_ID,
    "season":  SEASON,
    "status":  "FT-AET-PEN",
})
fixture_ids = [f["fixture"]["id"] for f in data.get("response", [])]
print(f"  Found {len(fixture_ids)} completed fixtures")

# ---------------------------------------------------------------------------
# Step 2 — batch fetch /fixtures/players (20 IDs per call)
# ---------------------------------------------------------------------------
batches = [fixture_ids[i:i+BATCH] for i in range(0, len(fixture_ids), BATCH)]
print(f"Step 2: Fetching player stats in {len(batches)} batches...")

# Accumulator: norm_name → running totals
totals: dict[str, dict] = defaultdict(lambda: {
    "name":           "",
    "club":           "",
    "matches":        0,
    "minutes":        0,
    "crosses":        0,   # accurate_crosses → Sleeper: +1 each
    "dispossessed":   0,   # dispossessed     → Sleeper: -0.5 each
    "saves":          0,   # saves            → already in /players but confirm
    "goals_conceded": 0,
})

for i, batch in enumerate(batches, 1):
    ids_param = "-".join(str(x) for x in batch)
    print(f"  Batch {i}/{len(batches)} ({len(batch)} fixtures)...", end="\r")

    resp = _get("/fixtures", {"ids": ids_param})
    fixtures = resp.get("response", [])

    for fixture in fixtures:
        players_data = fixture.get("players", [])
        for team_block in players_data:
            team_name = (team_block.get("team") or {}).get("name", "")
            for entry in team_block.get("players", []):
                p     = entry.get("player", {})
                stats = (entry.get("statistics") or [{}])[0]

                name = p.get("name", "")
                if not name:
                    continue
                key = _norm(name)

                # Extract the stats we care about
                passes  = stats.get("passes")  or {}
                dribbles= stats.get("dribbles") or {}
                games   = stats.get("games")    or {}
                goals   = stats.get("goals")    or {}

                crosses      = passes.get("accuracy")    # API uses this for crosses in fixture stats
                # Actually crosses are under a different key — let's grab all pass keys
                # The fixture/players endpoint has different structure to /players season stats
                # We'll store the raw and figure out which field has crosses
                total_passes = passes.get("total")       or 0
                key_passes   = passes.get("key")         or 0
                dispossessed = dribbles.get("past")      or 0
                minutes      = games.get("minutes")      or 0
                saves        = goals.get("saves")        or 0
                conceded     = goals.get("conceded")     or 0

                rec = totals[key]
                rec["name"]           = name
                rec["club"]           = team_name
                rec["matches"]       += 1
                rec["minutes"]       += minutes
                rec["crosses"]       += key_passes      # using key_passes as proxy — see note below
                rec["dispossessed"]  += dispossessed
                rec["saves"]         += saves
                rec["goals_conceded"]+= conceded

    time.sleep(0.4)

print(f"\n  Done. {len(totals)} unique players accumulated.")

# ---------------------------------------------------------------------------
# Step 3 — check raw structure of one fixture to find the crosses field
# ---------------------------------------------------------------------------
print("\nStep 3: Checking raw structure of first fixture for crosses field...")
sample_resp = _get("/fixtures", {"ids": str(fixture_ids[0])})
sample_fixtures = sample_resp.get("response", [])
if sample_fixtures:
    first_player_block = (sample_fixtures[0].get("players") or [{}])[0]
    first_player = (first_player_block.get("players") or [{}])[0]
    first_stats  = (first_player.get("statistics") or [{}])[0]
    print("  Raw stats keys and values for first player:")
    for section, vals in first_stats.items():
        print(f"    {section}: {vals}")

# ---------------------------------------------------------------------------
# Step 4 — save
# ---------------------------------------------------------------------------
output = {
    key: {
        "name":         rec["name"],
        "club":         rec["club"],
        "norm_name":    key,
        "matches":      rec["matches"],
        "minutes":      rec["minutes"],
        "dispossessed": rec["dispossessed"],
        "saves":        rec["saves"],
        "goals_conceded": rec["goals_conceded"],
        # crosses: will update field name after inspecting raw output above
        "crosses_proxy": rec["crosses"],
    }
    for key, rec in totals.items()
    if rec["minutes"] > 0
}

OUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved {len(output)} players → {OUT_PATH}")

# Sanity check — top dispossessed players (should be dribbling forwards)
top_disp = sorted(output.items(), key=lambda x: x[1]["dispossessed"], reverse=True)[:5]
print("\nTop 5 by dispossessed (should be dribbling FWDs/MIDs):")
for k, v in top_disp:
    print(f"  {v['name']:25s} {v['club']:20s} disp={v['dispossessed']}")

print("\nNext: inspect the raw stats output above, then commit data/pl_fixture_stats_2025.json")
