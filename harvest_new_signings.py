"""
Harvest prior-league per-90 stats for 26/27 new signings with no PL history.

For each player: find them in API-Football (best fuzzy name match), pull their
most recent completed DOMESTIC-LEAGUE season (2025 = 2025/26, excluding youth /
international / cup competitions), convert to per-90 counting stats, and tag the
league + strength coefficient. The engine then rescales these onto the real
Sleeper points scale (calibration fit on PL players) before applying the coeff.

Usage:
    export API_FOOTBALL_KEY="your_key_here"
    python3 harvest_new_signings.py

Output:
    data/new_signings_2026.json  — {norm_name: {per90:{...}, league, coeff, ...}}

Calls: ~2 per player x 14 ~= 30 total.
"""

import difflib
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

from draft_engine import _est_flat_pts, _norm_name, _norm_pos

API_KEY  = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"
SEASON   = 2025          # 2025/26 — each player's last season before the PL move
OUT_PATH = Path("data/new_signings_2026.json")

# Non-domestic-league competitions to ignore (API-Football per-player stats have
# no reliable "type" field, so we filter by NAME). "Championship" stays a league.
_SKIP_WORDS = (
    "u16", "u17", "u18", "u19", "u20", "u21", "u23", "youth", "uefa",
    "champions league", "europa", "conference league", "nations", "world cup",
    "euro championship", "friendl", "qualification", "olympic", "revello",
    "cup", "coppa", "copa", "taca", "taça", "pokal", "coupe", "dfb",
    "relegation", "super cup", "supercup", "play-off", "playoff", "reserve",
)


def _is_cup(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in _SKIP_WORDS)

if not API_KEY:
    raise SystemExit("Set API_FOOTBALL_KEY env var before running.")

session = requests.Session()
session.headers.update({"x-apisports-key": API_KEY, "Accept": "application/json"})

# Well-known API-Football league IDs, for scoping stubborn name searches.
LG_LIGUE1, LG_PRIMEIRA = 61, 94

# (display, lastname search, firstname hint, nationality hint, (league_id, season)
# league_hint scopes the search to a specific league to beat name collisions.)
TARGETS = [
    ("Bazoumana Toure",  "Toure",       "Bazoumana", "",         None),
    ("Hayden Hackney",   "Hackney",     "Hayden",    "England",  None),
    ("Jeremy Jacquet",   "Jacquet",     "Jeremy",    "France",   (LG_LIGUE1, 2025)),
    ("Giovanni Leoni",   "Leoni",       "Giovanni",  "Italy",    None),
    ("Johan Manzambi",   "Manzambi",    "Johan",     "",         None),
    ("Oscar Mingueza",   "Mingueza",    "Oscar",     "Spain",    None),
    ("Tarik Muharemovic","Muharemovic", "Tarik",     "",         None),
    ("Marco Palestra",   "Palestra",    "Marco",     "Italy",    None),
    ("Geovany Quenda",   "Quenda",      "Geovany",   "Portugal", None),
    ("Jannik Schuster",  "Schuster",    "Jannik",    "",         None),
    ("Luka Vuskovic",    "Vuskovic",    "Luka",      "Croatia",  None),
    ("Antonio Silva",    "Silva",       "Antonio",   "Portugal", (LG_PRIMEIRA, 2025)),  # Benfica
    ("Ousmane Diomande", "Diomande",    "Ousmane",   "",         None),
    ("Christos Tzolis",  "Tzolis",      "Christos",  "Greece",   None),
]


def _coeff(league_name: str, country: str = "") -> tuple[float, str]:
    """League-strength multiplier vs the PL (estimates). Country disambiguates
    names shared across nations (e.g. 'Bundesliga' = Germany 0.88 vs Austria
    0.58; 'Serie A' = Italy 0.86 vs Brazil)."""
    n = (league_name or "").lower()
    c = (country or "").lower()
    if "bundesliga" in n and ("2." in n or "bundesliga 2" in n):
        return 0.65, "2.bundesliga"
    if "bundesliga" in n:
        return (0.58, "austria") if "austria" in c else (0.88, "bundesliga")
    if "serie a" in n:
        return (0.86, "serie a") if "italy" in c else (0.60, "serie a (non-IT)")
    table = [
        ("championship",  0.70), ("eredivisie", 0.70),
        ("primeira",      0.70), ("liga portugal", 0.70),
        ("jupiler",       0.63), ("pro league", 0.63),
        ("super lig",     0.62),
        ("super league",  0.55),
        ("la liga",       0.90), ("primera",     0.90),
        ("ligue 1",       0.82),
        ("premier league", 1.00),
    ]
    for key, val in table:
        if key in n:
            return val, key
    return 0.65, "default"


def _get(path: str, params: dict) -> dict:
    r = session.get(f"{BASE_URL}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# Manual API-Football player-id overrides for names too common to auto-resolve
# (multiple same-name players). Fill in from the API-Football site if a stubborn
# namesake keeps winning, e.g. {"antonio silva": 283058}.
PID_OVERRIDE: dict[str, int] = {}


def find_player(display: str, search: str, hint: str, nat: str, league_hint) -> tuple:
    ov = PID_OVERRIDE.get(_norm_name(display))
    if ov:
        return ov, display, 1
    target = _norm_name(display)

    def score(r):
        p = r.get("player", {})
        name = _norm_name(p.get("name") or f"{p.get('firstname','')} {p.get('lastname','')}")
        s = difflib.SequenceMatcher(None, name, target).ratio()
        if hint and hint.lower() in (p.get("firstname") or "").lower():
            s += 0.3
        if nat and nat.lower() in (p.get("nationality") or "").lower():
            s += 0.25
        return s

    # League-scoped search first (beats same-name players in other divisions).
    if league_hint:
        lid, seas = league_hint
        data = _get("/players", {"search": search, "league": lid, "season": seas})
        resp = data.get("response", []) or []
        if resp:
            best = max(resp, key=score)
            p = best.get("player", {})
            return p.get("id"), p.get("name"), len(resp)

    data = _get("/players/profiles", {"search": search})
    resp = data.get("response", []) or []
    if not resp:
        return None, None, 0
    best = max(resp, key=score)
    p = best.get("player", {})
    return p.get("id"), p.get("name"), len(resp)


def primary_stats(pid: int) -> tuple:
    """Most recent season (try 2025/26, then fall back to 2024/25) with usable
    domestic-league minutes. Returns (stat_line, season_used, all_comps)."""
    last_all = []
    for season in (SEASON, SEASON - 1):
        data = _get("/players", {"id": pid, "season": season})
        resp = data.get("response", []) or []
        if not resp:
            continue
        stats_list = resp[0].get("statistics", []) or []
        last_all = stats_list
        # Domestic-league entries only (exclude cups/youth/intl by name) — so a
        # player with only cup minutes this season falls back to last season's
        # league (e.g. Leoni: 81' League Cup 25/26 -> Parma Serie A 24/25).
        league = [
            s for s in stats_list
            if not _is_cup((s.get("league") or {}).get("name", ""))
            and ((s.get("games") or {}).get("minutes") or 0) >= 270   # ≥3 full games
        ]
        if league:
            best = max(league, key=lambda s: (s.get("games") or {}).get("minutes") or 0)
            return best, season, stats_list
        time.sleep(0.15)
    return None, None, last_all


def per90(s: dict, n90: float) -> dict:
    g  = s.get("goals")    or {}
    sh = s.get("shots")    or {}
    ps = s.get("passes")   or {}
    tk = s.get("tackles")  or {}
    dr = s.get("dribbles") or {}
    ca = s.get("cards")    or {}
    pn = s.get("penalty")  or {}
    raw = {
        "goals":              g.get("total")          or 0,
        "assists":            g.get("assists")        or 0,
        "shots_on_target":    sh.get("on")            or 0,
        "key_passes":         ps.get("key")           or 0,
        "successful_dribbles":dr.get("success")       or 0,
        "tackles_won":       (tk.get("total") or 0) * 0.6,
        "interceptions":      tk.get("interceptions") or 0,
        "blocked_shots":      tk.get("blocks")        or 0,
        "yellow_card":        ca.get("yellow")        or 0,
        "red_card":           ca.get("red")           or 0,
        "penalties_missed":   pn.get("missed")        or 0,
        "saves":              g.get("saves")          or 0,
        "goals_against":      g.get("conceded")       or 0,
    }
    return {k: round(v / n90, 4) for k, v in raw.items()}


out: dict = {}
print(f"Harvesting {len(TARGETS)} new signings (prior season {SEASON}/{SEASON+1})...\n")
for display, search, hint, nat, league_hint in TARGETS:
    try:
        pid, api_name, ncand = find_player(display, search, hint, nat, league_hint)
        if not pid:
            print(f"  {display:22s} — NOT FOUND (0 candidates for '{search}')")
            continue
        time.sleep(0.2)
        s, season_used, all_stats = primary_stats(pid)
        if not s:
            comps = ", ".join((e.get("league") or {}).get("name", "?") for e in all_stats) or "none"
            print(f"  {display:22s} — {api_name}: no usable league season. comps=[{comps}]")
            time.sleep(0.2)
            continue
        gm      = s.get("games")  or {}
        league  = (s.get("league") or {}).get("name", "")
        team    = (s.get("team")   or {}).get("name", "")
        minutes = gm.get("minutes") or 0
        pos     = _norm_pos((gm.get("position") or "")[:3]) if gm.get("position") else "MID"
        n90     = minutes / 90.0
        country = (s.get("league") or {}).get("country", "")
        coeff, matched = _coeff(league, country)
        p90     = per90(s, n90) if n90 > 0 else {}
        est     = round(_est_flat_pts(p90, pos), 2)
        out[_norm_name(display)] = {
            "display":      display,
            "api_name":     api_name,
            "team":         team,
            "league":       league,
            "coeff":        coeff,
            "position":     pos,
            "minutes":      int(minutes),
            "source_season":season_used,
            "per90":        p90,
            "est_pp90_raw": est,   # pre-calibration; engine rescales onto pts_std
        }
        yr = f"{str(season_used)[2:]}/{str(season_used + 1)[2:]}"
        print(f"  {display:22s} {team:16s} {league:20s} {yr} min={int(minutes):4d} "
              f"est_pp90={est:5.2f} x{coeff}  ({ncand} cand)")
        time.sleep(0.2)
    except Exception as exc:
        print(f"  {display:22s} — ERROR {exc}")

OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved {len(out)} players -> {OUT_PATH}")
print("est_pp90 shown is PRE-calibration (raw). The engine rescales it onto the")
print("real Sleeper scale, then applies the league coefficient. Check leagues look right.")
