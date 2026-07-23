"""
Harvest prior-league stats for 26/27 new signings who have no Premier League
history, so their nailed-starter projections aren't stuck at 0.

For each player: find them in API-Football, pull their most recent completed
season (2025 = 2025/26) in whatever league they played, convert to per-90
rates, estimate a Sleeper points-per-90 using our scoring rules, then apply a
league-strength coefficient (a 20-goal season in Portugal != one in the PL).

Usage:
    export API_FOOTBALL_KEY="your_key_here"
    python3 harvest_new_signings.py

Output:
    data/new_signings_2026.json  — {norm_name: {est_pp90_coeff, league, coeff, ...}}

Calls: ~2 per player (profile search + season stats) x 14 ~= 30 total.
"""

import json
import os
import time
import unicodedata
from pathlib import Path

import requests

from draft_engine import SLEEPER_SCORING, _norm_name, _norm_pos

API_KEY  = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"
SEASON   = 2025          # 2025/26 — each player's last season before the PL move
OUT_PATH = Path("data/new_signings_2026.json")

if not API_KEY:
    raise SystemExit("Set API_FOOTBALL_KEY env var before running.")

session = requests.Session()
session.headers.update({"x-apisports-key": API_KEY, "Accept": "application/json"})

# (display name, lastname to search, firstname hint for disambiguation)
TARGETS = [
    ("Bazoumana Toure",  "Toure",       "Bazoumana"),
    ("Hayden Hackney",   "Hackney",     "Hayden"),
    ("Jeremy Jacquet",   "Jacquet",     ""),
    ("Giovanni Leoni",   "Leoni",       "Giovanni"),
    ("Johan Manzambi",   "Manzambi",    ""),
    ("Oscar Mingueza",   "Mingueza",    ""),
    ("Tarik Muharemovic","Muharemovic", ""),
    ("Marco Palestra",   "Palestra",    ""),
    ("Geovany Quenda",   "Quenda",      ""),
    ("Jannik Schuster",  "Schuster",    "Jannik"),
    ("Luka Vuskovic",    "Vuskovic",    "Luka"),
    ("Antonio Silva",    "Silva",       "Antonio"),   # Benfica CB
    ("Ousmane Diomande", "Diomande",    "Ousmane"),
    ("Christos Tzolis",  "Tzolis",      ""),
]


def _coeff(league_name: str) -> tuple[float, str]:
    """League-strength multiplier vs the Premier League (tunable defaults)."""
    n = (league_name or "").lower()
    table = [
        ("2. bundesliga", 0.50), ("bundesliga 2", 0.50),
        ("championship",  0.55),
        ("jupiler",       0.55), ("pro league", 0.55),   # Belgium
        ("eredivisie",    0.65),
        ("primeira",      0.60), ("liga portugal", 0.60),
        ("la liga",       0.90), ("primera",     0.90),
        ("serie a",       0.85),
        ("ligue 1",       0.80),
        ("bundesliga",    0.85),   # (after the "2. bundesliga" checks above)
        ("super lig",     0.55),   # Turkey
        ("super league",  0.50),   # Greece
        ("bundesliga - austria", 0.50), ("austria", 0.50),
        ("premier league", 1.00),
    ]
    for key, val in table:
        if key in n:
            return val, key
    return 0.55, "default"


def _get(path: str, params: dict) -> dict:
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def find_player(search: str, hint: str) -> tuple:
    data = _get("/players/profiles", {"search": search})
    resp = data.get("response", [])
    if not resp:
        return None, None
    cands = resp
    if hint:
        f = [r for r in resp if hint.lower() in (r["player"].get("firstname") or "").lower()]
        if f:
            cands = f
    p = cands[0]["player"]
    return p.get("id"), p.get("name")


def primary_stats(pid: int) -> dict | None:
    data = _get("/players", {"id": pid, "season": SEASON})
    resp = data.get("response", [])
    if not resp:
        return None
    stats_list = resp[0].get("statistics", []) or []
    stats_list = [s for s in stats_list if ((s.get("games") or {}).get("minutes") or 0) > 0]
    if not stats_list:
        return None
    return max(stats_list, key=lambda s: (s.get("games") or {}).get("minutes") or 0)


def est_sleeper_pts(s: dict, pos: str) -> float:
    """Estimate Sleeper points from a season stat line (undercounts: no clean
    sheets / accurate crosses / aerials / defcon extras available here)."""
    g  = s.get("goals")    or {}
    sh = s.get("shots")    or {}
    ps = s.get("passes")   or {}
    tk = s.get("tackles")  or {}
    dr = s.get("dribbles") or {}
    ca = s.get("cards")    or {}
    pn = s.get("penalty")  or {}
    counts = {
        "goals":              g.get("total")         or 0,
        "assists":            g.get("assists")       or 0,
        "shots_on_target":    sh.get("on")           or 0,
        "key_passes":         ps.get("key")          or 0,
        "successful_dribbles":dr.get("success")      or 0,
        "tackles_won":        (tk.get("total") or 0) * 0.6,   # total->won proxy
        "interceptions":      tk.get("interceptions") or 0,
        "blocked_shots":      tk.get("blocks")        or 0,
        "yellow_card":        ca.get("yellow")        or 0,
        "red_card":           ca.get("red")           or 0,
        "penalties_missed":   pn.get("missed")        or 0,
        "saves":              g.get("saves")          or 0,
        "goals_against":      g.get("conceded")       or 0,
    }
    pts = 0.0
    for stat, val in counts.items():
        if not val:
            continue
        rule = SLEEPER_SCORING.get(stat)
        if rule is None:
            continue
        mult = rule.get(pos, 0) if isinstance(rule, dict) else float(rule)
        pts += val * mult
    return pts


out: dict = {}
print(f"Harvesting {len(TARGETS)} new signings (prior season {SEASON}/{SEASON+1})...\n")
for display, search, hint in TARGETS:
    try:
        pid, api_name = find_player(search, hint)
        if not pid:
            print(f"  {display:22s} — NOT FOUND in API-Football")
            continue
        time.sleep(0.2)
        s = primary_stats(pid)
        if not s:
            print(f"  {display:22s} — no {SEASON} minutes found")
            continue
        gm      = s.get("games")  or {}
        league  = (s.get("league") or {}).get("name", "")
        team    = (s.get("team")   or {}).get("name", "")
        minutes = gm.get("minutes") or 0
        pos     = _norm_pos((gm.get("position") or "")[:3]) if gm.get("position") else "MID"
        n90     = minutes / 90.0
        coeff, matched = _coeff(league)
        est_pts  = est_sleeper_pts(s, pos)
        est_pp90 = round(est_pts / n90, 2) if n90 > 0 else 0.0
        out[_norm_name(display)] = {
            "display":         display,
            "api_name":        api_name,
            "team":            team,
            "league":          league,
            "coeff":           coeff,
            "coeff_matched":   matched,
            "position":        pos,
            "minutes":         int(minutes),
            "appearances":     gm.get("appearences") or 0,
            "est_pp90_raw":    est_pp90,
            "est_pp90_coeff":  round(est_pp90 * coeff, 2),
        }
        print(f"  {display:22s} {team:18s} {league:22s} "
              f"min={int(minutes):4d} pp90={est_pp90:5.2f} x{coeff} -> {round(est_pp90*coeff,2)}")
        time.sleep(0.2)
    except Exception as exc:
        print(f"  {display:22s} — ERROR {exc}")

OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved {len(out)} players -> {OUT_PATH}")
print("Review the leagues/coefficients above; tell me any you want re-tiered.")
