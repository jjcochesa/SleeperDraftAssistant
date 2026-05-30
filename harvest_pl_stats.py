"""
One-time harvest: pull 2024/25 Premier League player stats from API-Football
and cache them to data/pl_stats_2024.json for use in Sleeper point projections.

Usage:
    export API_FOOTBALL_KEY="your_key_here"
    python3 harvest_pl_stats.py

Output:
    data/pl_stats_2024.json   — full record per player
    data/pl_stats_2024.csv    — flat CSV for inspection

API-Football docs: https://www.api-football.com/documentation-v3
Free / paid: GET /players?league=39&season=2024&page=N
Rate limit: 30 req/min on free, 300/min on paid — we sleep 0.3s between pages.
"""

import csv
import json
import os
import time
import unicodedata
from pathlib import Path

import requests

API_KEY    = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL   = "https://v3.football.api-sports.io"
LEAGUE_ID  = 39      # Premier League
SEASON     = 2025    # 2025/26 season
OUT_DIR    = Path("data")
OUT_JSON   = OUT_DIR / "pl_stats_2025.json"
OUT_CSV    = OUT_DIR / "pl_stats_2025.csv"

if not API_KEY:
    raise SystemExit("Set API_FOOTBALL_KEY env var before running.")

OUT_DIR.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update({
    "x-apisports-key": API_KEY,
    "Accept": "application/json",
})


def _norm_name(name: str) -> str:
    name = name.replace("ı", "i").replace("İ", "i")
    nfkd = unicodedata.normalize("NFKD", name.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _get(path: str, params: dict) -> dict:
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _extract(player: dict, stats: list) -> dict:
    """Flatten one API-Football player+stats record into a clean dict."""
    p  = player.get("player", {})
    s  = stats[0] if stats else {}
    gm = s.get("games", {})
    gl = s.get("goals", {})
    sh = s.get("shots", {})
    ps = s.get("passes", {})
    tk = s.get("tackles", {})
    du = s.get("duels", {})
    dr = s.get("dribbles", {})
    fo = s.get("fouls", {})
    ca = s.get("cards", {})
    pn = s.get("penalty", {})

    appearances = gm.get("appearences") or 0   # API-Football typo — two e's
    starts      = gm.get("lineups")     or 0
    minutes     = gm.get("minutes")     or 0

    return {
        # Identity
        "api_football_id": p.get("id"),
        "name":            p.get("name", ""),
        "firstname":       p.get("firstname", ""),
        "lastname":        p.get("lastname", ""),
        "norm_name":       _norm_name(p.get("name", "")),
        "nationality":     p.get("nationality", ""),
        "age":             p.get("age"),
        "club":            (s.get("team") or {}).get("name", ""),
        "position":        gm.get("position", ""),

        # Playing time — used for starter_rate
        "appearances":     appearances,
        "starts":          starts,
        "minutes":         minutes,
        "starter_rate":    round(starts / appearances, 3) if appearances > 0 else 0.0,

        # Attacking
        "goals":           gl.get("total")   or 0,
        "assists":         gl.get("assists") or 0,
        "shots_total":     sh.get("total")   or 0,
        "shots_on_target": sh.get("on")      or 0,       # SoT → FWD: every 2 = +1pt
        "key_passes":      ps.get("key")     or 0,       # MID: every 2 chances = +1pt
        "dribbles_success":dr.get("success") or 0,       # all pos: +1 each

        # Defensive / duels
        "tackles_total":   tk.get("total")        or 0,
        "tackles_blocks":  tk.get("blocks")       or 0,  # blocked shots → +1 each
        "interceptions":   tk.get("interceptions") or 0, # +1 each
        "duels_won":       du.get("won")           or 0, # proxy for aerials won

        # Discipline
        "yellow_cards":    ca.get("yellow")     or 0,
        "yellowred_cards": ca.get("yellowred")  or 0,    # 2nd yellow → -5pt
        "red_cards":       ca.get("red")        or 0,

        # GK
        "saves":           gl.get("saves")      or 0,    # +2 each; every 3 = +1
        "goals_conceded":  gl.get("conceded")   or 0,    # DEF/GK: -2 per extra

        # Penalties
        "penalties_won":   pn.get("won")        or 0,   # drawn → +2pt
        "penalties_missed":pn.get("missed")     or 0,   # -4pt
        "penalties_saved": pn.get("saved")      or 0,   # GK: +8pt

        # Fouls
        "fouls_drawn":     fo.get("drawn")      or 0,
        "fouls_committed": fo.get("committed")  or 0,

        # API-Football rating (useful signal for model)
        "rating":          float(gm.get("rating") or 0) if gm.get("rating") else None,
    }


def fetch_all_players() -> list[dict]:
    page, total_pages = 1, 1
    records = []

    while page <= total_pages:
        print(f"  Page {page}/{total_pages} — {len(records)} players so far...", end="\r")
        data = _get("/players", {"league": LEAGUE_ID, "season": SEASON, "page": page})

        paging       = data.get("paging", {})
        total_pages  = paging.get("total", 1)
        results      = data.get("response", [])

        for entry in results:
            rec = _extract(entry.get("player", {}), entry.get("statistics", []))
            if rec["minutes"] > 0:   # skip players with no minutes
                records.append(rec)

        page += 1
        time.sleep(0.35)

    print()  # newline after \r
    return records


# ---------------------------------------------------------------------------

print(f"\nFetching Premier League 2024/25 players from API-Football...")
players = fetch_all_players()
print(f"Total players with minutes: {len(players)}")

# Save JSON
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(players, f, indent=2, ensure_ascii=False)
print(f"Saved JSON → {OUT_JSON}")

# Save CSV
if players:
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=players[0].keys())
        writer.writeheader()
        writer.writerows(players)
    print(f"Saved CSV  → {OUT_CSV}")

# Quick sanity check — show top 10 by goals
top = sorted(players, key=lambda x: x["goals"], reverse=True)[:10]
print("\nTop 10 by goals:")
for p in top:
    print(f"  {p['name']:25s} {p['club']:20s} {p['goals']}g {p['assists']}a "
          f"{p['shots_on_target']}sot {p['minutes']}min")

print("\nDone. Commit data/pl_stats_2024.json and data/pl_stats_2024.csv to the repo.")
