"""
Pre-draft data check. Run this in the days before the draft to confirm the
external data has rolled over to 26/27 and that every lineup name resolves.

Checks, in order of how badly each one would hurt:
  1. FPL season rollover — the draftable-pool filter trusts FPL to contain
     ONLY the current 20 clubs. If FPL is still serving 25/26, relegated
     clubs are still in the pool and promoted clubs are missing entirely.
  2. Sleeper pool — has it picked up the late signings it was missing?
  3. Lineup resolution — every nailed/bench/promoted name maps to a player.
  4. Spot-checks on players we know are load-bearing.

Usage:  python3 preflight.py          (no API key needed — both APIs are public)
"""

import json
from pathlib import Path

import requests

from draft_engine import (
    _match_name_list, _norm_name, load_bench, load_lineups, load_promoted,
    build_fpl_lookup,
)

SLEEPER = "https://api.sleeper.app/v1"
FPL     = "https://fantasy.premierleague.com/api/bootstrap-static/"

PROMOTED_CLUBS = {"hull", "coventry", "ipswich"}
RELEGATED_HINT = {"west ham", "wolves", "wolverhampton", "burnley"}

# Players whose ranking is load-bearing enough to check by name every time.
SPOT_CHECK = ["Saka", "Isak", "Osula", "Maddison", "Kerkez", "Haaland",
              "Bruno Fernandes", "Gabriel Magalhaes"]

print("=" * 68)
print("1. FPL SEASON ROLLOVER  (drives the draftable-pool filter)")
print("=" * 68)
fpl_ok = False
try:
    boot = requests.get(FPL, timeout=20).json()
    teams = sorted(t["name"] for t in boot.get("teams", []))
    print(f"   FPL lists {len(teams)} clubs: {', '.join(teams)}")
    low = " | ".join(t.lower() for t in teams)
    have_promoted = {c for c in PROMOTED_CLUBS if c in low}
    have_relegated = {c for c in RELEGATED_HINT if c in low}
    if len(have_promoted) >= 2 and not have_relegated:
        print("   -> looks like 26/27. Pool filter is CORRECT.")
        fpl_ok = True
    else:
        print(f"   -> STILL 25/26 (found promoted: {have_promoted or 'none'}, "
              f"relegated still present: {have_relegated or 'none'})")
        print("      The in_pl filter will keep relegated clubs and DROP the")
        print("      promoted ones until FPL updates. Re-run closer to kickoff.")
    # set-piece data availability
    sp = sum(1 for e in boot.get("elements", []) if e.get("penalties_order"))
    print(f"   set-piece data: {sp} players have a penalties_order")
except Exception as exc:
    print(f"   FPL FETCH FAILED: {exc}")

print()
print("=" * 68)
print("2. SLEEPER POOL")
print("=" * 68)
players = requests.get(f"{SLEEPER}/players/clubsoccer:epl", timeout=30).json()
stats   = requests.get(f"{SLEEPER}/stats/clubsoccer:epl/regular/2025", timeout=30).json()
with_min = sum(1 for s in stats.values() if s.get("min"))
print(f"   {len(players)} players in pool; {with_min} with 25/26 minutes")

# were previously-missing signings added?
WATCH = ["Tzolis", "Lindelof", "Berge", "Diomande", "Antonio Silva"]
print("   previously-missing signings:")
for w in WATCH:
    t = set(_norm_name(w).split())
    hit = [p.get("full_name") for p in players.values()
           if t <= set(_norm_name(p.get("full_name") or "").split())]
    print(f"     {w:16s} {'IN POOL: ' + hit[0] if hit else 'still absent'}")

print()
print("=" * 68)
print("3. LINEUP RESOLUTION")
print("=" * 68)
for label, loader in [("nailed", load_lineups), ("bench", load_bench),
                      ("promoted", load_promoted)]:
    lists = loader()
    total = sum(len(v) for k, v in lists.items() if not k.startswith("_"))
    matched = _match_name_list(players, stats, lists)
    print(f"   {label:9s} {len(matched):3d} / {total:3d} names resolved", end="")
    if len(matched) < total:
        # find which ones failed
        bad = []
        for club, names in lists.items():
            if club.startswith("_"):
                continue
            for n in names:
                if not _match_name_list(players, stats, {club: [n]}):
                    bad.append(f"{n} ({club})")
        print(f"   UNRESOLVED: {', '.join(bad)}")
    else:
        print("   all good")

print()
print("=" * 68)
print("4. SPOT CHECKS")
print("=" * 68)
nailed = _match_name_list(players, stats, load_lineups())
prom   = _match_name_list(players, stats, load_promoted())
bench  = _match_name_list(players, stats, load_bench())
for name in SPOT_CHECK:
    t = set(_norm_name(name).split())
    hits = [(pid, p) for pid, p in players.items()
            if t <= set(_norm_name(p.get("full_name") or "").split())]
    if not hits:
        print(f"   {name:20s} NOT IN POOL")
        continue
    pid, p = max(hits, key=lambda x: float((stats.get(x[0]) or {}).get("min") or 0))
    tags = []
    if pid in nailed: tags.append("NAILED*")
    if pid in prom:   tags.append("promoted")
    if pid in bench:  tags.append("BENCH")
    mins = float((stats.get(pid) or {}).get("min") or 0)
    print(f"   {name:20s} {p.get('full_name','')[:22]:22s} min={mins:6.0f} "
          f"{'  '.join(tags) or '(not flagged)'}"
          + ("   <-- EXPECTED NAILED" if not tags and name != "Maddison" else ""))
print()
print("Done. Biggest thing to act on is anything flagged in section 1 or 3.")
