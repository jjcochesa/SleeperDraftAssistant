"""
Harvest per-match player stats from API-Football /fixtures/players endpoint.
Aggregates accurate crosses, dispossessed, smothers (high_claims) and other
stats not available in the /players season summary endpoint.

Usage:
    export API_FOOTBALL_KEY="your_key_here"
    python3 harvest_fixture_stats.py

Output:
    data/pl_fixture_stats_2025.json  — {norm_name: {crosses, dispossessed, ...}}

API calls used: 1 (fixture IDs) + ~380 (one per fixture) = ~381 total.
"""

import json
import os
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

API_KEY   = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL  = "https://v3.football.api-sports.io"
LEAGUE_ID = 39
SEASON    = 2025
OUT_PATH  = Path("data/pl_fixture_stats_2025.json")
SLEEP_SEC = 0.21   # ~286 req/min, safely under Pro 300/min limit

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
print(f"Step 1: Fetching completed fixture IDs for PL {SEASON}...")
data = _get("/fixtures", {
    "league": LEAGUE_ID,
    "season": SEASON,
    "status": "FT-AET-PEN",
})
fixture_ids = [f["fixture"]["id"] for f in data.get("response", [])]
print(f"  Found {len(fixture_ids)} completed fixtures")

# ---------------------------------------------------------------------------
# Step 2 — fetch /fixtures/players for each fixture (one call per fixture)
# ---------------------------------------------------------------------------
print(f"Step 2: Fetching player stats — {len(fixture_ids)} calls (~{len(fixture_ids)*SLEEP_SEC/60:.0f} min)...")

# Accumulator: norm_name → running totals
totals: dict[str, dict] = defaultdict(lambda: {
    "name":           "",
    "club":           "",
    "matches":        0,
    "minutes":        0,
    "crosses":        0,   # passes.crosses (accurate crosses) → Sleeper: +1 each
    "dispossessed":   0,   # dribbles.past                    → Sleeper: -0.5 each
    "high_claims":    0,   # goals.saves with GK context (smothers field TBD)
    "saves":          0,
    "goals_conceded": 0,
})

raw_dumped = False

for i, fid in enumerate(fixture_ids, 1):
    print(f"  Fixture {i}/{len(fixture_ids)} (id={fid})...", end="\r")

    resp = _get("/fixtures/players", {"fixture": fid})
    teams = resp.get("response", [])

    # On first fixture, dump raw stats structure so we can verify field names
    if not raw_dumped and teams:
        first_player = (teams[0].get("players") or [{}])[0]
        first_stats  = (first_player.get("statistics") or [{}])[0]
        print(f"\n--- RAW /fixtures/players stats for first player of fixture {fid} ---")
        print(json.dumps(first_stats, indent=2))
        print("--- END RAW ---\n")
        raw_dumped = True

    for team_block in teams:
        team_name = (team_block.get("team") or {}).get("name", "")
        for entry in team_block.get("players", []):
            p     = entry.get("player", {})
            stats = (entry.get("statistics") or [{}])[0]

            name = p.get("name", "")
            if not name:
                continue
            key = _norm(name)

            passes   = stats.get("passes")   or {}
            dribbles = stats.get("dribbles") or {}
            games    = stats.get("games")    or {}
            goals    = stats.get("goals")    or {}

            minutes      = games.get("minutes")    or 0
            crosses      = passes.get("crosses")   or 0   # accurate crosses
            dispossessed = dribbles.get("past")    or 0
            saves        = goals.get("saves")      or 0
            conceded     = goals.get("conceded")   or 0

            rec = totals[key]
            rec["name"]            = name
            rec["club"]            = team_name
            rec["matches"]        += 1
            rec["minutes"]        += minutes
            rec["crosses"]        += crosses
            rec["dispossessed"]   += dispossessed
            rec["saves"]          += saves
            rec["goals_conceded"] += conceded

    time.sleep(SLEEP_SEC)

print(f"\n  Done. {len(totals)} unique players accumulated.")

# ---------------------------------------------------------------------------
# Step 3 — save
# ---------------------------------------------------------------------------
output = {
    key: {
        "name":           rec["name"],
        "club":           rec["club"],
        "norm_name":      key,
        "matches":        rec["matches"],
        "minutes":        rec["minutes"],
        "crosses":        rec["crosses"],
        "dispossessed":   rec["dispossessed"],
        "saves":          rec["saves"],
        "goals_conceded": rec["goals_conceded"],
    }
    for key, rec in totals.items()
    if rec["minutes"] > 0
}

OUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved {len(output)} players → {OUT_PATH}")

# Sanity checks
top_cross = sorted(output.items(), key=lambda x: x[1]["crosses"], reverse=True)[:5]
print("\nTop 5 by accurate crosses (should be wide MIDs/fullbacks):")
for k, v in top_cross:
    print(f"  {v['name']:25s} {v['club']:20s} crosses={v['crosses']}")

top_disp = sorted(output.items(), key=lambda x: x[1]["dispossessed"], reverse=True)[:5]
print("\nTop 5 by dispossessed (should be dribbling FWDs/MIDs):")
for k, v in top_disp:
    print(f"  {v['name']:25s} {v['club']:20s} disp={v['dispossessed']}")

print("\nDone. Commit data/pl_fixture_stats_2025.json and wire into draft_engine.py.")
