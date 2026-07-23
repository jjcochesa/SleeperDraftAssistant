"""
Match the 26/27 nailed-starter lineups against the live Sleeper pool + 25/26
stats to classify each listed player — no guessing about who's "foreign".

Buckets:
  HAS 25/26 stats   -> already in the PL last season; use Sleeper data directly
  NEW (no 25/26)    -> genuinely new to the PL; needs API-Football foreign stats
  UNMATCHED         -> name doesn't match the Sleeper pool; fix spelling
  AMBIGUOUS         -> multiple pool players share the name; verify

Usage:  python3 match_lineups.py      (no API key needed — Sleeper is public)
"""

import json
import unicodedata
from pathlib import Path

import requests


def norm(s: str) -> str:
    s = s.replace("ı", "i").replace("İ", "i")
    nf = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in nf if not unicodedata.combining(c))


lineups = json.loads(Path("data/lineups_2026.json").read_text(encoding="utf-8"))
players = requests.get("https://api.sleeper.app/v1/players/clubsoccer:epl").json()
stats25 = requests.get("https://api.sleeper.app/v1/stats/clubsoccer:epl/regular/2025").json()

# Name indexes: full name and last name -> [player_ids]
by_full: dict[str, list] = {}
by_last: dict[str, list] = {}
for pid, p in players.items():
    fn = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
    by_full.setdefault(norm(fn), []).append(pid)
    ln = p.get("last_name") or ""
    if ln:
        by_last.setdefault(norm(ln), []).append(pid)


def has_min(pid: str) -> bool:
    return bool(stats25.get(pid, {}).get("min"))


has_stats, new_targets, unmatched, ambiguous = [], [], [], []

for team, names in lineups.items():
    if team.startswith("_"):
        continue
    for name in names:
        cands = by_full.get(norm(name)) or by_last.get(norm(name.split()[-1])) or []
        if not cands:
            unmatched.append((team, name))
        elif len(cands) > 1:
            withm = [c for c in cands if has_min(c)]
            ambiguous.append((team, name, len(cands)))
            (has_stats if withm else new_targets).append((team, name))
        else:
            (has_stats if has_min(cands[0]) else new_targets).append((team, name))

print(f"HAS 25/26 stats (use Sleeper):        {len(has_stats)}")
print(f"NEW — need API-Football foreign data: {len(new_targets)}")
print(f"UNMATCHED — fix name:                 {len(unmatched)}")
print(f"AMBIGUOUS — verify:                   {len(ambiguous)}\n")

print("=== NEW / foreign-lookup targets ===")
for t, n in sorted(new_targets, key=lambda x: x[0]):
    print(f"  {n:22s} {t}")
print("\n=== UNMATCHED — fix these names ===")
for t, n in sorted(unmatched, key=lambda x: x[0]):
    print(f"  {n:22s} {t}")
print("\n=== AMBIGUOUS — multiple pool matches ===")
for t, n, c in sorted(ambiguous, key=lambda x: x[0]):
    print(f"  {n:22s} {t}  ({c} matches)")
