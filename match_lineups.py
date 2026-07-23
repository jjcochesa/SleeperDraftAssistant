"""
Match the 26/27 nailed-starter lineups against the live Sleeper pool + 25/26
stats to classify each listed player — no guessing about who's "foreign".

Matching: a lineup name matches a pool player when EVERY token of the
(normalised) lineup name appears in the pool player's full-name tokens. This
handles multi-word surnames (Van Dijk, Mac Allister, Smith Rowe) and mononyms
(Alisson, Gabriel). When several pool players match, the one with the most
25/26 minutes wins (the established starter the user means).

Buckets:
  HAS 25/26 stats   -> in the PL last season; use Sleeper data directly
  NEW (no 25/26)    -> genuinely new to the PL; needs API-Football foreign stats
  UNMATCHED         -> no pool player matches; fix spelling in lineups file

Usage:  python3 match_lineups.py      (no API key needed — Sleeper is public)
"""

import difflib
import json
import unicodedata
from pathlib import Path

import requests

# Chars with no NFKD decomposition — map explicitly before stripping accents.
_SUB = {"ı": "i", "ø": "o", "ß": "ss", "đ": "d", "ð": "d",
        "ł": "l", "æ": "ae", "œ": "oe", "þ": "th"}


def norm(s: str) -> str:
    s = s.lower().strip()
    for a, b in _SUB.items():
        s = s.replace(a, b)
    nf = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nf if not unicodedata.combining(c))


def toks(s: str) -> set:
    return set(norm(s).split())


lineups = json.loads(Path("data/lineups_2026.json").read_text(encoding="utf-8"))
players = requests.get("https://api.sleeper.app/v1/players/clubsoccer:epl").json()
stats25 = requests.get("https://api.sleeper.app/v1/stats/clubsoccer:epl/regular/2025").json()


def full_name(p: dict) -> str:
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()


def minutes(pid: str) -> float:
    return float(stats25.get(pid, {}).get("min") or 0)


# Pre-tokenise the pool once.
pool = [(pid, full_name(p), toks(full_name(p))) for pid, p in players.items()]

has_stats, new_targets, unmatched = [], [], []

for team, names in lineups.items():
    if team.startswith("_"):
        continue
    for name in names:
        nt = toks(name)
        # all lineup-name tokens must be present in the pool player's tokens
        cands = [(pid, fn) for pid, fn, pt in pool if nt and nt <= pt]
        if not cands:
            unmatched.append((team, name))
            continue
        # disambiguate: most 25/26 minutes wins
        pid, fn = max(cands, key=lambda c: minutes(c[0]))
        mins = minutes(pid)
        tag = f"{name:20s} {team:20s} -> {fn}" + (f"  [{len(cands)} cands]" if len(cands) > 1 else "")
        (has_stats if mins > 0 else new_targets).append((tag, mins))

print(f"HAS 25/26 stats (use Sleeper):        {len(has_stats)}")
print(f"NEW — need API-Football foreign data: {len(new_targets)}")
print(f"UNMATCHED — fix name in lineups file: {len(unmatched)}\n")

print("=== NEW / foreign-lookup targets (matched a pool player, 0 min last season) ===")
for tag, _ in sorted(new_targets):
    print(f"  {tag}")

print("\n=== UNMATCHED — no pool player matched (spelling to fix, or not in pool) ===")
pool_norm = [(fn, norm(fn)) for _, fn, _ in pool]
for team, name in sorted(unmatched, key=lambda x: x[0]):
    q = norm(name)
    qlast = q.split()[-1]
    # substring hits (query appears inside a pool name) + fuzzy on last token
    subs = [fn for fn, nn in pool_norm if q in nn or qlast in nn.split()]
    fuzzy = difflib.get_close_matches(qlast, [nn.split()[-1] for _, nn in pool_norm],
                                      n=3, cutoff=0.7)
    fuzzy_names = [fn for fn, nn in pool_norm if nn.split()[-1] in fuzzy]
    sugg = list(dict.fromkeys(subs + fuzzy_names))[:4]
    hint = ("  ~ " + " | ".join(sugg)) if sugg else "  (no close pool name — likely not in Sleeper yet)"
    print(f"  {name:20s} {team:18s}{hint}")
