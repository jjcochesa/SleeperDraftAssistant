"""
Squad review: reconcile our lineup lists against the CURRENT Sleeper pool.

Answers two questions before draft day:
  A. Who is in our lineups but NOT draftable in Sleeper? (can't be picked)
  B. Who is draftable and relevant but MISSING from our lineups? (invisible
     on our board no matter how good they are)

Group B is the important one, split into:
  B1 — established: real 25/26 PL minutes, so we already have a rate for them
  B2 — new/unproven: no PL minutes but in FPL's squad list, i.e. an actual
       26/27 squad member rather than academy noise

FPL membership is the noise filter: Sleeper's raw pool carries ~1700 entries
including youth and departed players, while FPL lists only current squads.

Usage:  python3 squad_review.py        (no API key needed — both APIs public)
"""

from collections import defaultdict

import requests

from draft_engine import (
    _match_name_list, _norm_name, load_bench, load_lineups, load_promoted,
)

SLEEPER = "https://api.sleeper.app/v1"
FPL     = "https://fantasy.premierleague.com/api/bootstrap-static/"
MIN_REL = 900          # minutes that make an unlisted player clearly relevant

players = requests.get(f"{SLEEPER}/players/clubsoccer:epl", timeout=30).json()
stats   = requests.get(f"{SLEEPER}/stats/clubsoccer:epl/regular/2025", timeout=30).json()

fpl_names, fpl_team = set(), {}
try:
    boot = requests.get(FPL, timeout=20).json()
    tmap = {t["id"]: t["name"] for t in boot.get("teams", [])}
    for e in boot.get("elements", []):
        k = _norm_name(f"{e['first_name']} {e['second_name']}")
        fpl_names.add(k)
        fpl_team[k] = tmap.get(e.get("team"), "")
    print(f"FPL squads loaded: {len(fpl_names)} players across {len(tmap)} clubs\n")
except Exception as exc:
    print(f"FPL unavailable ({exc}) — B2 will be noisy without it\n")

lineups  = load_lineups()
listed   = _match_name_list(players, stats, lineups)
listed  |= _match_name_list(players, stats, load_bench())
listed  |= _match_name_list(players, stats, load_promoted())

def mins(pid):
    return float((stats.get(pid) or {}).get("min") or 0)

def full(p):
    return (p.get("full_name") or
            " ".join(filter(None, [p.get("first_name"), p.get("last_name")])) or "")

# ---- A. listed but not resolvable in the pool --------------------------------
print("=" * 68)
print("A. IN OUR LINEUPS BUT NOT IN THE SLEEPER POOL (cannot be drafted)")
print("=" * 68)
missing = []
for club, names in lineups.items():
    if club.startswith("_"):
        continue
    for n in names:
        if not _match_name_list(players, stats, {club: [n]}):
            missing.append((club, n))
if missing:
    for club, n in sorted(missing):
        print(f"   {n:24s} {club}")
else:
    print("   none — every listed player resolves")

# ---- B. relevant but unlisted -------------------------------------------------
b1, b2 = defaultdict(list), defaultdict(list)
for pid, p in players.items():
    if pid in listed:
        continue
    name = full(p)
    if not name:
        continue
    k = _norm_name(name)
    club = fpl_team.get(k, "")
    in_fpl = k in fpl_names
    m = mins(pid)
    pos = (p.get("fantasy_positions") or [p.get("position") or "?"])[0]
    if m >= MIN_REL and (in_fpl or not fpl_names):
        b1[club or "?"].append((name, pos, m))
    elif in_fpl and m == 0:
        b2[club or "?"].append((name, pos, m))

print()
print("=" * 68)
print(f"B1. UNLISTED, ESTABLISHED ({MIN_REL}+ min in 25/26, in FPL 26/27 squads)")
print("=" * 68)
for club in sorted(b1):
    rows = sorted(b1[club], key=lambda x: -x[2])
    print(f"   {club}")
    for n, pos, m in rows:
        print(f"      {n[:28]:28s} {pos:3s} {m:5.0f} min")

print()
print("=" * 68)
print("B2. UNLISTED, NEW (no 25/26 PL minutes, but in an FPL 26/27 squad)")
print("=" * 68)
for club in sorted(b2):
    rows = sorted(b2[club])
    print(f"   {club}: " + ", ".join(f"{n} ({pos})" for n, pos, _ in rows))

print()
print("Reply with which of these to ADD (and to which club) or to ignore.")
