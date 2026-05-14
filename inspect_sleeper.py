"""
Run this once to inspect the Sleeper clubsoccer:epl stats API.
Usage: python inspect_sleeper.py
"""

import json
import requests
from collections import defaultdict

SLEEPER_API  = "https://api.sleeper.app/v1"
SPORT        = "clubsoccer:epl"
SEASON       = "2025"
LEAGUE_ID    = "1115505765961293824"

session = requests.Session()
session.headers["User-Agent"] = "SleeperDraftAssistant/inspect"


def _get(url):
    r = session.get(url, timeout=15)
    print(f"  GET {url}  →  {r.status_code}")
    r.raise_for_status()
    return r.json()


# ── 1. Players ─────────────────────────────────────────────────────────────
print("\n=== 1. Players endpoint ===")
try:
    players = _get(f"{SLEEPER_API}/players/{SPORT}")
    print(f"  Total players: {len(players)}")
    # Show first player with a full_name
    sample_id, sample = next(
        ((k, v) for k, v in players.items() if v.get("full_name")), (None, None)
    )
    if sample:
        print(f"  Sample player_id: {sample_id}")
        print(f"  Sample player keys: {sorted(sample.keys())}")
        print(f"  Sample: {json.dumps(sample, indent=2)[:600]}")
except Exception as e:
    print(f"  ERROR: {e}")
    players = {}


# ── 2. Season stats ────────────────────────────────────────────────────────
print(f"\n=== 2. Season stats  /stats/{SPORT}/regular/{SEASON} ===")
season_stats = {}
try:
    season_stats = _get(f"{SLEEPER_API}/stats/{SPORT}/regular/{SEASON}")
    print(f"  Total entries: {len(season_stats)}")

    # Find an entry with actual stats (non-empty)
    rich = {k: v for k, v in season_stats.items() if v and len(v) > 2}
    print(f"  Entries with stats: {len(rich)}")

    if rich:
        sample_id = next(iter(rich))
        sample_stats = rich[sample_id]
        print(f"\n  Sample player_id: {sample_id}")
        name = players.get(sample_id, {}).get("full_name", "unknown")
        print(f"  Name: {name}")
        print(f"  All stat fields: {sorted(sample_stats.keys())}")
        print(f"  Values: {json.dumps(sample_stats, indent=2)}")
except Exception as e:
    print(f"  ERROR: {e}")


# ── 3. Cross-check a known star player ────────────────────────────────────
print("\n=== 3. Cross-check Haaland ===")
try:
    haaland_id = next(
        (k for k, v in players.items() if "haaland" in (v.get("full_name") or "").lower()),
        None,
    )
    if haaland_id:
        print(f"  Haaland player_id: {haaland_id}")
        h_stats = season_stats.get(haaland_id, {})
        print(f"  Haaland stats: {json.dumps(h_stats, indent=2)}")
    else:
        print("  Haaland not found in players dict")
except Exception as e:
    print(f"  ERROR: {e}")


# ── 4. Team ID diagnostic — list all unique team values + example players ──
print("\n=== 4. Team ID diagnostic ===")
try:
    team_examples: dict = defaultdict(list)
    for pid, sp in players.items():
        t = str(sp.get("team") or "").strip()
        if t:
            name = sp.get("full_name") or sp.get("last_name") or pid
            team_examples[t].append(name)

    print(f"  Unique team values: {len(team_examples)}")
    print("\n  ID → example players (first 3):")
    for tid in sorted(team_examples, key=lambda x: (not x.isdigit(), x)):
        examples = team_examples[tid][:3]
        print(f"    {tid:8s} → {', '.join(examples)}")
except Exception as e:
    print(f"  ERROR: {e}")


# ── 5. Sleeper teams endpoint ──────────────────────────────────────────────
print(f"\n=== 5. Sleeper teams endpoint /teams/{SPORT} ===")
try:
    teams = _get(f"{SLEEPER_API}/teams/{SPORT}")
    print(f"  Response type: {type(teams).__name__}  len: {len(teams)}")
    print(f"  Sample: {json.dumps(teams if isinstance(teams, dict) else teams[:3], indent=2)[:600]}")
except Exception as e:
    print(f"  ERROR (expected if endpoint doesn't exist): {e}")


# ── 6. GW-level endpoint (fallback) ────────────────────────────────────────
print(f"\n=== 6. GW-level endpoint  /stats/{SPORT}/regular/{SEASON}/1 ===")
try:
    gw1 = _get(f"{SLEEPER_API}/stats/{SPORT}/regular/{SEASON}/1")
    rich_gw = {k: v for k, v in gw1.items() if v and len(v) > 2}
    print(f"  Total entries: {len(gw1)}  |  with stats: {len(rich_gw)}")
    if rich_gw:
        sample_id = next(iter(rich_gw))
        name = players.get(sample_id, {}).get("full_name", "unknown")
        print(f"  Sample ({name}): {json.dumps(rich_gw[sample_id], indent=2)}")
except Exception as e:
    print(f"  ERROR: {e}")


# ── 7. League roster check ─────────────────────────────────────────────────
print(f"\n=== 7. League rosters for {LEAGUE_ID} ===")
try:
    rosters = _get(f"{SLEEPER_API}/league/{LEAGUE_ID}/rosters")
    for r in rosters:
        print(f"  roster_id={r['roster_id']}  owner={r.get('owner_id')}  "
              f"players={len(r.get('players') or [])}")
except Exception as e:
    print(f"  ERROR: {e}")


# ── 8. Draft check ─────────────────────────────────────────────────────────
print(f"\n=== 8. Drafts for league ===")
try:
    drafts = _get(f"{SLEEPER_API}/league/{LEAGUE_ID}/drafts")
    for d in drafts:
        print(f"  draft_id={d['draft_id']}  status={d.get('status')}  "
              f"season={d.get('season')}  type={d.get('type')}")
        print(f"    slot_to_roster_id: {d.get('slot_to_roster_id')}")
        print(f"    draft_order keys: {list((d.get('draft_order') or {}).keys())[:5]}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n=== Done ===")
