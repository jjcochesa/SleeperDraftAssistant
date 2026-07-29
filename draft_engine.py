"""
Draft engine: Sleeper clubsoccer:epl stats + FPL cost/ownership + Understat xG/xA.
Points use Sleeper scoring rules only — FPL data is for cost and ADP proxy ONLY.
"""

import json
import re
import time
import unicodedata
from datetime import datetime
from typing import Optional

import requests

SLEEPER_API    = "https://api.sleeper.app/v1"
FPL_API        = "https://fantasy.premierleague.com/api"
UNDERSTAT_BASE = "https://understat.com"
SLEEPER_SPORT  = "clubsoccer:epl"

POSITION_ORDER = ["GK", "DEF", "MID", "FWD"]

# ---------------------------------------------------------------------------
# Sleeper scoring rules
# Dict value = {position: pts_per_stat}; float = flat across all positions.
# ---------------------------------------------------------------------------
SLEEPER_SCORING: dict[str, dict | float] = {
    "goals":                {"FWD": 9,    "MID": 9,    "DEF": 10,   "GK": 10},
    "assists":              {"FWD": 6,    "MID": 6,    "DEF": 7,    "GK": 7},
    "shots_on_target":      2.0,
    "key_passes":           {"FWD": 2,    "MID": 2,    "DEF": 2,    "GK": 0},
    "successful_dribbles":  1.0,
    "accurate_crosses":     1.0,
    "yellow_card":         -2.0,
    "red_card":            -7.0,
    "second_yellow":       -5.0,
    "aerials_won":          {"FWD": 0.5,  "MID": 0.5,  "DEF": 1.0,  "GK": 1.0},
    "effective_clearances": {"FWD": 0,    "MID": 0,    "DEF": 0.25, "GK": 0.25},
    "saves":                2.0,
    # Sleeper's cos stat already gates on 60 min
    "clean_sheets":         {"FWD": 0,    "MID": 1,    "DEF": 6,    "GK": 8},
    "tackles_won":          1.0,
    "interceptions":        1.0,
    "blocked_shots":        1.0,
    "goals_against":        {"FWD": 0,    "MID": 0,    "DEF": -2,   "GK": -2},
    "own_goals":           -5.0,
    "penalties_missed":    -4.0,
    "penalties_saved":      8.0,
    "smothers":             1.0,
    "high_claims":          1.0,
    "dispossessed":        -0.5,
    "penalty_kicks_drawn":  2.0,
}

# Sleeper API short codes → canonical stat name (confirmed from official Sleeper stat table)
_SLEEPER_FIELD: dict[str, list[str]] = {
    "goals":                ["g"],
    "assists":              ["at"],
    "shots_on_target":      ["sot"],
    "key_passes":           ["kp"],
    "successful_dribbles":  ["cos"],   # "contests succeeded" — drb is always empty
    "accurate_crosses":     ["acnc"],  # accurate crosses no corners — ac is always empty
    "aerials_won":          ["aer"],
    "effective_clearances": ["clr"],
    "saves":                ["sv"],
    "clean_sheets":         ["cs"],
    "high_claims":          ["hcs"],
    "smothers":             ["sm"],
    "tackles_won":          ["tkw"],
    "interceptions":        ["int"],
    "blocked_shots":        ["bs"],
    "goals_against":        ["ga"],
    "own_goals":            ["og"],
    "penalties_missed":     ["pkm"],
    "penalties_saved":      ["pks"],
    "penalty_kicks_drawn":  ["pkd"],
    "yellow_card":          ["yc"],
    "second_yellow":        ["yc2"],
    "red_card":             ["rc"],
    "minutes":              ["min"],
    "games_played":         ["gp"],
    "dispossessed":         ["dis"],
}

_POS_ALIASES: dict[str, str] = {
    "gk": "GK",  "gkp": "GK",  "goalkeeper": "GK", "k": "GK",
    "def": "DEF", "defender": "DEF", "d": "DEF",
    "cb": "DEF",  "lb": "DEF",  "rb": "DEF",  "wb": "DEF",
    "mid": "MID", "midfielder": "MID", "m": "MID", "cm": "MID", "am": "MID",
    "fwd": "FWD", "forward": "FWD", "f": "FWD",
    "st": "FWD",  "att": "FWD", "str": "FWD", "strk": "FWD",
}

_http = requests.Session()
_http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SleeperDraftAssistant/1.0)"})

# Static EPL team abbreviation → display name (covers Sleeper 3-letter codes)
_EPL_ABBREV: dict[str, str] = {
    "ARS": "Arsenal",       "AVL": "Aston Villa",    "BOU": "Bournemouth",
    "BRE": "Brentford",     "BHA": "Brighton",       "CHE": "Chelsea",
    "CRY": "Crystal Palace","EVE": "Everton",         "FUL": "Fulham",
    "IPS": "Ipswich",       "LEI": "Leicester",      "LEE": "Leeds",
    "LIV": "Liverpool",     "MCI": "Man City",       "MUN": "Man Utd",
    "NEW": "Newcastle",     "NFO": "Nott'm Forest",  "SOU": "Southampton",
    "TOT": "Spurs",         "WHU": "West Ham",       "WOL": "Wolves",
}

# Sleeper numeric team IDs — inferred from known players in those slots.
# Run inspect_sleeper.py (section 4) to get the full list and confirm/extend this.
_SLEEPER_NUMERIC_TEAMS: dict[str, str] = {
    "1037": "Newcastle",    # Bruno Guimarães
    "1038": "Brentford",    # Igor Thiago
    "1039": "Chelsea",      # João Pedro (transferred from Brighton)
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

# Chars with no NFKD decomposition — map explicitly before accent-stripping.
_NAME_SUB = {"ı": "i", "İ": "i", "ø": "o", "ß": "ss", "đ": "d",
             "ð": "d", "ł": "l", "æ": "ae", "œ": "oe", "þ": "th"}


def _norm_name(name: str) -> str:
    """
    Accent-strip + lowercase for cross-source name matching.
    Non-decomposing chars (Turkish ı, Nordic ø, German ß, …) are mapped first.
    """
    name = name.lower().strip()
    for a, b in _NAME_SUB.items():
        name = name.replace(a, b)
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _norm_pos(raw: str) -> str:
    return _POS_ALIASES.get(raw.lower(), raw.upper())


def _sleeper_season_year() -> int:
    """Return the Sleeper season year for the current EPL season (starts August)."""
    now = datetime.now()
    return now.year if now.month >= 8 else now.year - 1


def _resolve_team(fpl: Optional[dict], sp: dict,
                  teams_lookup: Optional[dict] = None,
                  team_map: Optional[dict] = None) -> str:
    """Return a human-readable team name.
    Priority: lineup-learned numeric-id map → Sleeper abbreviation → FPL team
              name → hardcoded ids → Sleeper teams API → '—'.
    The learned map wins over FPL because FPL is matched by NAME, which can
    attach the wrong club to a player who shares a surname with someone else.
    """
    raw = (sp.get("team") or "").strip()
    if raw:
        if team_map and raw in team_map:
            return team_map[raw]
        if raw in _EPL_ABBREV:
            return _EPL_ABBREV[raw]
    if fpl and fpl.get("team_name"):
        return fpl["team_name"]
    if not raw:
        return "—"
    if raw in _SLEEPER_NUMERIC_TEAMS:
        return _SLEEPER_NUMERIC_TEAMS[raw]
    if teams_lookup and raw in teams_lookup:
        return teams_lookup[raw]
    return "—" if raw.isdigit() else raw


def _get(url: str, retries: int = 3, **kwargs) -> dict | list:
    for attempt in range(retries):
        try:
            r = _http.get(url, timeout=12, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _get_html(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            r = _http.get(url, timeout=15)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


# ---------------------------------------------------------------------------
# Sleeper API
# ---------------------------------------------------------------------------

def get_league(league_id: str) -> dict:
    return _get(f"{SLEEPER_API}/league/{league_id}")


def get_league_drafts(league_id: str) -> list:
    return _get(f"{SLEEPER_API}/league/{league_id}/drafts")


def get_league_rosters(league_id: str) -> list:
    return _get(f"{SLEEPER_API}/league/{league_id}/rosters")


def get_league_users(league_id: str) -> list:
    return _get(f"{SLEEPER_API}/league/{league_id}/users")


def get_draft(draft_id: str) -> dict:
    return _get(f"{SLEEPER_API}/draft/{draft_id}")


def get_draft_picks(draft_id: str) -> list:
    return _get(f"{SLEEPER_API}/draft/{draft_id}/picks")


def get_sleeper_players() -> dict:
    """Return {player_id: player_info} for all clubsoccer:epl players."""
    return _get(f"{SLEEPER_API}/players/{SLEEPER_SPORT}")


def get_sleeper_teams() -> dict[str, str]:
    """
    Fetch Sleeper EPL team metadata → {team_id: name}.
    Returns empty dict if the endpoint doesn't exist (graceful fallback).
    """
    try:
        data = _get(f"{SLEEPER_API}/teams/{SLEEPER_SPORT}")
        if isinstance(data, list):
            return {str(t.get("team_id", t.get("id", ""))): t.get("name", "")
                    for t in data if t.get("name")}
        if isinstance(data, dict):
            return {str(k): v.get("name", str(v)) if isinstance(v, dict) else str(v)
                    for k, v in data.items()}
    except Exception:
        pass
    return {}


def get_sleeper_user(username_or_id: str) -> dict:
    return _get(f"{SLEEPER_API}/user/{username_or_id}")


def find_roster_id(league_id: str, user_id: str) -> Optional[int]:
    for r in get_league_rosters(league_id):
        if r.get("owner_id") == user_id:
            return r["roster_id"]
    return None


def get_sleeper_season_stats(season: Optional[str] = None) -> dict:
    """
    Season-level player stats for clubsoccer:epl.
    Defaults to the current Sleeper season year.
    Returns {player_id: stats_dict} with pts_std and raw stat codes.
    """
    yr = season or str(_sleeper_season_year())
    return _get(f"{SLEEPER_API}/stats/{SLEEPER_SPORT}/regular/{yr}")


def get_projection_base_stats(min_players: int = 50) -> tuple[dict, str]:
    """
    Return the most recent season stats that actually contain data, plus the
    season year used. Sleeper's current-season endpoint is empty until games
    are played, so from Aug (season year flips) until 26/27 has real minutes,
    this transparently falls back to the last completed season (25/26) — which
    is the correct historical base for projections. Auto-advances to the new
    season once it accumulates ≥ min_players players with minutes.
    """
    current = _sleeper_season_year()
    for yr in (current, current - 1):
        try:
            stats = get_sleeper_season_stats(str(yr))
        except Exception:
            continue
        with_mins = sum(1 for s in stats.values() if s.get("min"))
        if with_mins >= min_players:
            return stats, str(yr)
    # Nothing usable — return current-season (possibly empty) so caller degrades
    return get_sleeper_season_stats(str(current)), str(current)


# ---------------------------------------------------------------------------
# FPL API — cost and ownership ONLY (never use FPL points or position)
# ---------------------------------------------------------------------------

def get_fpl_bootstrap() -> dict:
    """
    Fetches FPL bootstrap-static. Used for:
      - now_cost  → player price in £m (÷10)
      - selected_by_percent → community ownership, used as ADP proxy
    Do NOT use FPL total_points or element_type position for Sleeper scoring.
    FPL misclassifies players like Gakpo/Sarr/Minteh as MID; Sleeper has them as FWD.
    """
    return _get(f"{FPL_API}/bootstrap-static/")


def build_fpl_lookup(bootstrap: dict) -> dict[str, dict]:
    """
    Return {norm_name: {"cost": float, "ownership_pct": float, "team_name": str, …}}
    Keyed by normalised player name for cross-source matching.
    Set-piece orders (1 = first-choice taker, None = not a taker) are FPL's
    curated data — free signal for crosses/KP volume (corners) and pen goals.
    """
    team_map = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    lookup: dict[str, dict] = {}
    for p in bootstrap.get("elements", []):
        name = f"{p['first_name']} {p['second_name']}"
        key  = _norm_name(name)
        lookup[key] = {
            "cost":          round((p.get("now_cost") or 0) / 10, 1),
            "ownership_pct": float(p.get("selected_by_percent") or 0),
            "team_name":     team_map.get(p.get("team"), ""),
            "pen_order":     p.get("penalties_order"),
            "corner_order":  p.get("corners_and_indirect_freekicks_order"),
            "fk_order":      p.get("direct_freekicks_order"),
        }
    return lookup


# ---------------------------------------------------------------------------
# Sleeper points calculation
# ---------------------------------------------------------------------------

def _raw_stat(raw: dict, stat_name: str) -> float:
    """Extract a value from a Sleeper stats dict using short-code aliases."""
    for field in _SLEEPER_FIELD.get(stat_name, []):
        v = raw.get(field)
        if v is not None:
            return float(v)
    return 0.0


def _calc_pts(raw: dict, position: str) -> float:
    """
    Calculate Sleeper fantasy points for one player-season.
    Uses pts_std when Sleeper pre-computes it; otherwise applies SLEEPER_SCORING.
    """
    pre = raw.get("pts_std")
    if pre is not None:
        return round(float(pre), 2)

    pos = position.upper()
    pts = 0.0
    for stat_name, rule in SLEEPER_SCORING.items():
        val = _raw_stat(raw, stat_name)
        if val == 0:
            continue
        multiplier = rule.get(pos, 0) if isinstance(rule, dict) else float(rule)
        pts += val * multiplier
    return round(pts, 2)


# ---------------------------------------------------------------------------
# Player data builder
# ---------------------------------------------------------------------------

# Stats estimable from API-Football (both harvested PL data and foreign new
# signings). Missing here: clean sheets, accurate crosses, aerials, defcon
# extras — so this UNDERCOUNTS. A calibration factor, fit on PL players where
# we ALSO know the true Sleeper pts_std, rescales the estimate onto the real
# points scale; a league coefficient then discounts for league strength.
def _est_flat_pts(f: dict, pos: str) -> float:
    """Estimated Sleeper points from a flat counting-stat dict at a position."""
    pts = 0.0
    for stat, val in f.items():
        if not val:
            continue
        rule = SLEEPER_SCORING.get(stat)
        if rule is None:
            continue
        mult = rule.get(pos, 0) if isinstance(rule, dict) else float(rule)
        pts += val * mult
    return pts


def _apif_flat(apif: dict) -> dict:
    """Map harvested API-Football PL fields (pl_stats) to the estimator keys."""
    return {
        "goals":               apif.get("goals")            or 0,
        "assists":             apif.get("assists")          or 0,
        "shots_on_target":     apif.get("shots_on_target")  or 0,
        "key_passes":          apif.get("key_passes")       or 0,
        "successful_dribbles": apif.get("dribbles_success") or 0,
        "tackles_won":        (apif.get("tackles_total")    or 0) * 0.6,
        "interceptions":       apif.get("interceptions")    or 0,
        "blocked_shots":       apif.get("tackles_blocks")   or 0,
        "yellow_card":         apif.get("yellow_cards")     or 0,
        "red_card":            apif.get("red_cards")        or 0,
        "penalties_missed":    apif.get("penalties_missed") or 0,
        "saves":               apif.get("saves")            or 0,
        "goals_against":       apif.get("goals_conceded")   or 0,
    }


def load_new_signings(path: str = "data/new_signings_2026.json") -> dict[str, dict]:
    """Load harvested foreign-league per-90 stats for new signings. {norm_name: {...}}."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _match_apif(sp: dict, key: str, pl_stats: Optional[dict]) -> dict:
    """Match a Sleeper player to harvested API-Football PL data by name."""
    if not pl_stats:
        return {}
    apif = pl_stats.get(key) or {}
    if not apif:
        last = _norm_name(sp.get("last_name") or "")
        if last:
            apif = pl_stats.get(f"__last__{last}") or {}
    return apif


def _median(vals: list) -> float:
    if not vals:
        return 1.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def load_lineups(path: str = "data/lineups_2026.json") -> dict[str, list]:
    """Load 26/27 nailed-starter lineups {club: [names]}. Empty if file missing."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _match_name_list(players: dict, season_stats: dict, name_lists: dict) -> set:
    """
    Resolve a {club: [names]} dict to Sleeper player_ids. A name matches a pool
    player when every token of the (normalised) name is in the pool player's
    name tokens (handles multi-word surnames + mononyms); ties broken by most
    25/26 minutes (the most likely intended player).
    """
    if not name_lists:
        return set()
    pool = []
    for pid, sp in players.items():
        fn = (sp.get("full_name") or sp.get("name")
              or " ".join(filter(None, [sp.get("first_name"), sp.get("last_name")])) or "")
        pool.append((pid, set(_norm_name(fn).split())))

    def mins(pid: str) -> float:
        return float((season_stats.get(pid) or {}).get("min") or 0)

    matched: set = set()
    for _club, names in name_lists.items():
        for name in names:
            nt = set(_norm_name(name).split())
            if not nt:
                continue
            cands = [pid for pid, pt in pool if nt <= pt]
            if cands:
                matched.add(max(cands, key=mins))
    return matched


def load_bench(path: str = "data/bench_2026.json") -> dict[str, list]:
    """Load explicit bench/faded-role overrides {club: [names]}. These players'
    expected volume is heavily discounted regardless of last season's minutes —
    for a player who has fallen out of favour, been out-signed, or is a known
    backup, so their historically-good numbers don't keep inflating them."""
    return load_lineups(path)


def load_promoted(path: str = "data/promoted_2026.json") -> dict[str, list]:
    """Load role-promotion overrides {club: [names]}: players whose 25/26
    minutes-per-appearance UNDERSTATES their 26/27 role — a striker who was a
    backup and is now first choice, or a starter whose minutes were wrecked by
    injury. Their own low mpa is ignored in favour of full starter minutes.

    This can't be inferred from data: a low-mpa player with few appearances is
    equally consistent with "injured star returning" and "fading squad player",
    and the two need opposite treatment. Hence an explicit list."""
    return load_lineups(path)


def resolve_nailed_starters(players: dict, season_stats: dict,
                            lineups: dict) -> tuple[set, dict]:
    """
    Resolve lineup names to Sleeper player_ids (see _match_name_list).

    Returns (nailed_pids, team_id_to_club). The second value is learned from the
    data: each matched player's Sleeper numeric team id is voted onto the club
    the user listed them under, giving an authoritative id->club map without
    hardcoding (Sleeper's numeric ids are otherwise unlabelled).
    """
    if not lineups:
        return set(), {}
    pool = []
    for pid, sp in players.items():
        fn = (sp.get("full_name") or sp.get("name")
              or " ".join(filter(None, [sp.get("first_name"), sp.get("last_name")])) or "")
        pool.append((pid, set(_norm_name(fn).split())))

    def mins(pid: str) -> float:
        return float((season_stats.get(pid) or {}).get("min") or 0)

    nailed: set = set()
    votes: dict[str, dict[str, int]] = {}
    for club, names in lineups.items():
        for name in names:
            nt = set(_norm_name(name).split())
            if not nt:
                continue
            cands = [pid for pid, pt in pool if nt <= pt]
            if not cands:
                continue
            pid = max(cands, key=mins)
            nailed.add(pid)
            tid = str((players.get(pid) or {}).get("team") or "").strip()
            if tid.isdigit():
                votes.setdefault(tid, {})
                votes[tid][club] = votes[tid].get(club, 0) + 1
    team_map = {tid: max(c.items(), key=lambda kv: kv[1])[0] for tid, c in votes.items()}
    return nailed, team_map


def load_fixture_stats(path: str = "data/pl_fixture_stats_2025.json") -> dict[str, dict]:
    """
    Load harvested fixture-level stats (dispossessed, crosses, saves).
    Returns {norm_name: stats_dict}. Empty dict if file missing.
    """
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data  # already keyed by norm_name


def load_pl_stats(path: str = "data/pl_stats_2025.json") -> dict[str, dict]:
    """
    Load harvested API-Football 2025/26 PL stats from disk.
    Returns {norm_name: stats_dict}. Empty dict if file missing.
    """
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}
    for rec in data:
        key = rec.get("norm_name", "")
        if key:
            result[key] = rec
        # Also index by lastname alone as fallback
        last = _norm_name(rec.get("lastname") or "")
        if last and last not in result:
            result[f"__last__{last}"] = rec
    return result


def build_player_stats(
    players:        dict,
    season_stats:   dict,
    fpl_lookup:     Optional[dict] = None,
    understat:      Optional[dict] = None,
    teams_lookup:   Optional[dict] = None,
    pl_stats:       Optional[dict] = None,
    fixture_stats:  Optional[dict] = None,
    nailed_pids:    Optional[set] = None,
    new_signings:   Optional[dict] = None,
    team_map:       Optional[dict] = None,
    bench_pids:     Optional[set] = None,
    promoted_pids:  Optional[set] = None,
) -> dict[str, dict]:
    """
    Merge Sleeper player info, season stats, FPL cost/ownership, Understat xG/xA,
    and API-Football 2025/26 PL individual stats (goals/90, SoT/90, starter_rate…).
    Joins Sleeper players ↔ stats on player_id (same key in both endpoints).
    Name normalisation is only used for FPL, Understat, and API-Football cross-source matching.
    """
    MIN_GW       = 10     # below this, projected_pts = 0 (insufficient sample)
    MIN_GW_PRIOR = 15     # only use established starters to compute position average

    # ------------------------------------------------------------------
    # Pass 1 — position-average PP90 (qualified players only, ≥ MIN_GW_PRIOR)
    # Used as the Bayesian prior so that 1-game wonders collapse toward average.
    # PP90 (points per 90 min) rather than PPG: sub appearances no longer
    # dilute a player's rate, so high-rate low-minute players surface.
    # ------------------------------------------------------------------
    pos_pp90_acc: dict[str, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for pid, sp in players.items():
        raw = season_stats.get(pid, {})
        if not raw:
            continue
        raw_pos = (sp.get("position") or (sp.get("fantasy_positions") or [""])[0] or "")
        pos     = _norm_pos(raw_pos) if raw_pos else "UNK"
        if pos not in pos_pp90_acc:
            continue
        pts  = _calc_pts(raw, pos)
        mins = _raw_stat(raw, "minutes")
        gws  = min(38, round(mins / 90)) if mins > 0 else 0
        if gws >= MIN_GW_PRIOR and pts > 0 and mins > 0:
            pos_pp90_acc[pos].append(pts / (mins / 90))

    pos_avg: dict[str, float] = {
        pos: round(sum(v) / len(v), 3) if v else 8.0
        for pos, v in pos_pp90_acc.items()
    }

    # ------------------------------------------------------------------
    # Calibration — put the API-Football estimate on the real Sleeper scale.
    # For PL players we have BOTH the API-Football stat line AND the true
    # pts_std. The estimator undercounts (no CS/crosses/aerials) yet weak-league
    # counting stats can inflate, so per position we fit calib = median(real
    # pp90 / estimated pp90). New signings' foreign estimates are then multiplied
    # by calib (scale fix) and their league coefficient (strength discount).
    # ------------------------------------------------------------------
    calib_ratios: dict[str, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    if pl_stats:
        for pid, sp in players.items():
            raw = season_stats.get(pid, {})
            if not raw:
                continue
            raw_pos = (sp.get("position") or (sp.get("fantasy_positions") or [""])[0] or "")
            pos     = _norm_pos(raw_pos) if raw_pos else "UNK"
            if pos not in calib_ratios:
                continue
            mins = _raw_stat(raw, "minutes")
            if mins < 900:                      # ≥10 full games for a stable ratio
                continue
            fn = (sp.get("full_name") or sp.get("name")
                  or " ".join(filter(None, [sp.get("first_name"), sp.get("last_name")])) or pid)
            apif = _match_apif(sp, _norm_name(fn), pl_stats)
            am = apif.get("minutes") or 0
            if not apif or am < 900:
                continue
            est_pp90 = _est_flat_pts(_apif_flat(apif), pos) / (am / 90)
            real_pp90 = _calc_pts(raw, pos) / (mins / 90)
            if est_pp90 > 0 and real_pp90 > 0:
                calib_ratios[pos].append(real_pp90 / est_pp90)
    calib: dict[str, float] = {
        pos: round(_median(v), 3) if v else 1.0 for pos, v in calib_ratios.items()
    }

    # ------------------------------------------------------------------
    # Pass 2 — build full player records
    # ------------------------------------------------------------------
    result: dict[str, dict] = {}

    for pid, sp in players.items():
        raw = season_stats.get(pid, {})   # joined directly by player_id

        # full_name: prefer explicit field; fall back to first+last concatenation
        full_name = (
            sp.get("full_name")
            or sp.get("name")
            or " ".join(filter(None, [sp.get("first_name"), sp.get("last_name")]))
            or pid
        )
        key = _norm_name(full_name)

        # Position: always use Sleeper's classification, never FPL's
        raw_pos = (
            sp.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
            or ""
        )
        pos = _norm_pos(raw_pos) if raw_pos else "UNK"

        # pts_std used verbatim when present; fallback reconstructs from stats
        total_pts = _calc_pts(raw, pos)
        mins      = _raw_stat(raw, "minutes")
        gp        = _raw_stat(raw, "games_played")
        games     = int(min(38, gp if gp > 0 else (round(mins / 90) if mins > 0 else 0)))
        # Real PPG for any player at/above the projection floor (MIN_GW=10).
        # Cameo players (<10 games) still show 0 so they don't top the PPG sort,
        # but 10-14 game players now keep their true PPG in both display and the
        # projection blend (previously zeroed at <15, which crushed that band).
        ppg       = round(total_pts / games, 2) if games >= MIN_GW else 0.0

        # API-Football individual stats lookup (PL players only)
        apif: dict = {}
        if pl_stats:
            apif = pl_stats.get(key) or {}
            if not apif:
                last = _norm_name(sp.get("last_name") or "")
                if last:
                    apif = pl_stats.get(f"__last__{last}") or {}

        # Fixture-level stats (accurate crosses unavailable in API; dispossessed is good)
        fix: dict = fixture_stats.get(key) or {} if fixture_stats else {}

        # starter_rate: fraction of appearances that were starts, from
        # API-Football. Only meaningful when we actually have that data — a
        # missing match must NOT silently default to "assumed starter", or
        # every player without a matched apif record would qualify for the
        # established-starter floor below regardless of real evidence.
        has_apif     = bool(apif)
        starter_rate = apif.get("starter_rate", 1.0) if has_apif else None

        # Nailed starter = named in the 26/27 predicted lineups (manual override).
        # Bench = explicit "this player's role has faded / he's a backup now"
        # override — takes priority over nailed (can't be both).
        nailed   = bool(nailed_pids and pid in nailed_pids)
        bench    = bool(bench_pids and pid in bench_pids)
        promoted = bool(promoted_pids and pid in promoted_pids)
        if bench:
            nailed = promoted = False
        if promoted:
            nailed = True          # a promoted player is by definition starting

        # 26/27 projection: PP90-based Bayesian shrinkage + expected minutes.
        # PP90 = pts per 90 min. Unlike PPG, a 20-min sub cameo no longer drags
        # the rate down, so high-rate rotation players are visible. Expected
        # volume (n90) is projected separately — the predicted-lineup override
        # plugs in here.
        # Players with < MIN_GW games are excluded (projected_pts = 0).
        FULL_SEASON    = 38.0   # EPL games in a season
        NAILED_APPS    = 34.0   # appearances assumed for a nailed starter
        BENCH_APPS     = 8.0    # appearances assumed for an explicit bench/faded player
        STARTER_MPA    = 80.0   # minutes/appearance for a first-choice starter
        NEWSIGNING_N90 = 28.0   # assumed 90s for a foreign new signing (adaptation)
        n90  = mins / 90.0
        # Nailed starters project even on a sub-MIN_GW sample (e.g. a squad
        # player promoted to first-choice after a departure): heavy Bayesian
        # shrinkage keeps a tiny sample from producing a fluke rate. Others
        # still need MIN_GW games to appear.
        qualifies = n90 > 0 and (games >= MIN_GW or nailed)
        pp90 = round(total_pts / n90, 2) if qualifies else 0.0
        if qualifies:
            prior_pp90 = pos_avg.get(pos, 8.0)
            # Adaptive K on 90s played: full-season veterans keep ~83% of their
            # own rate; 10-90 fringe players get ~44%. Prevents over-shrinking.
            k            = max(3.0, 40.0 / (n90 ** 0.5))
            blended_pp90 = (n90 * pp90 + k * prior_pp90) / (n90 + k)
            # Expected 90s next season = expected appearances × the player's own
            # minutes-per-appearance. Being "nailed" lifts APPEARANCES (a squad
            # player now expected to feature every week) but NOT minutes-per-game
            # — a player who averaged 55 min an outing is a sub/early-hook type,
            # and assuming he suddenly plays full 90s would double-count the
            # promotion. This keeps durable ever-presents at their real 37-38
            # while stopping cameo players exploding to a full season.
            mpa = (mins / games) if games > 0 else 0.0          # minutes/appearance
            if promoted:
                # Role promotion: last season's mpa reflects a role the player
                # no longer has (backup striker now first choice, or a starter
                # whose season was wrecked by injury). Use full starter minutes.
                mpa = max(mpa, STARTER_MPA)
            if bench:
                # Explicit override: role has faded / known backup this season.
                # Ignore last season's volume entirely so a good 25/26 (played
                # under a different manager, different pecking order, different
                # club) can't keep inflating a player who won't get minutes now.
                exp_apps = BENCH_APPS
            elif nailed:
                exp_apps = min(FULL_SEASON, max(float(games), NAILED_APPS))
            else:
                exp_apps = float(games)
                # "established starter coming off an injury-shortened season"
                # floor — only when we have REAL evidence of a high start rate,
                # not just the absence of API-Football data for this player.
                if games >= 25 and has_apif and starter_rate is not None and starter_rate >= 0.8:
                    exp_apps = max(0.75 * FULL_SEASON, exp_apps)
            exp_n90 = min(FULL_SEASON, exp_apps * (mpa / 90.0))
            projected_pts = round(blended_pp90 * exp_n90, 1)
        else:
            # Nailed new signing with no PL minutes: derive a rate from their
            # harvested foreign per-90 stats, put it on the Sleeper scale
            # (calib) and discount for league strength (coeff).
            ns = {}
            if new_signings:
                ns = new_signings.get(key) or {}
                if not ns:
                    last = _norm_name(sp.get("last_name") or "")
                    if last:
                        ns = next((v for k, v in new_signings.items() if k.endswith(last)), {})
            if nailed and ns.get("per90"):
                est_pp90 = _est_flat_pts(ns["per90"], pos)
                raw_pp90 = max(0.0, est_pp90 * calib.get(pos, 1.0) * ns.get("coeff", 0.65))
                # Shrink hard toward the position average. calib fixes the SCALE
                # and coeff discounts league STRENGTH, but neither accounts for
                # having zero Premier League evidence — a player who dominated a
                # weak league still cleared both and landed top-10. Treat a new
                # signing as a small amount of evidence against the prior, so
                # extreme foreign rates regress to something believable.
                prior_pp90   = pos_avg.get(pos, 8.0)
                NS_EVIDENCE  = 6.0                       # equivalent 90s of trust
                k_ns         = 40.0 / (NS_EVIDENCE ** 0.5)
                pp90 = round((NS_EVIDENCE * raw_pp90 + k_ns * prior_pp90)
                             / (NS_EVIDENCE + k_ns), 2)
                projected_pts = round(pp90 * NEWSIGNING_N90, 1)
            else:
                projected_pts = 0.0

        # Raw stats (field codes confirmed from official Sleeper stat table)
        goals  = _raw_stat(raw, "goals")
        assists= _raw_stat(raw, "assists")
        sot    = _raw_stat(raw, "shots_on_target")
        kp     = _raw_stat(raw, "key_passes")
        drb    = _raw_stat(raw, "successful_dribbles")
        acnc   = _raw_stat(raw, "accurate_crosses")
        aer    = _raw_stat(raw, "aerials_won")
        cs     = _raw_stat(raw, "clean_sheets")
        saves  = _raw_stat(raw, "saves")
        hcs    = _raw_stat(raw, "high_claims")
        sm     = _raw_stat(raw, "smothers")
        tkl    = _raw_stat(raw, "tackles_won")
        ints   = _raw_stat(raw, "interceptions")
        blk    = _raw_stat(raw, "blocked_shots")
        yc     = _raw_stat(raw, "yellow_card")
        rc     = _raw_stat(raw, "red_card")

        # FPL: cost + ownership + team name (name-normalised cross-source match)
        fpl = fpl_lookup.get(key) if fpl_lookup else None
        if fpl is None and fpl_lookup:
            # Surname fallback, but ONLY when it is unambiguous. The old
            # endswith() match attached the wrong club to players sharing a
            # surname (e.g. every "...silva" collapsing onto one entry), so
            # require an exact final-token match AND a unique hit.
            last = _norm_name(sp.get("last_name") or "")
            if last:
                hits = [v for k, v in fpl_lookup.items()
                        if k.split() and k.split()[-1] == last]
                if len(hits) == 1:
                    fpl = hits[0]

        team_name = _resolve_team(fpl, sp, teams_lookup, team_map)

        # Draftable-pool membership. FPL's bootstrap contains ONLY the 20
        # current PL clubs, so an FPL match is authoritative proof a player is
        # in this season's PL — it cleanly drops relegated clubs, foreign
        # clubs (departed stars like De Bruyne/TAA), Championship squads, and
        # team-placeholder rows. NOTE: FPL is used for CLUB membership only,
        # never for position — Sleeper's classification stays authoritative.
        # Team-placeholder rows ("London Chelsea") embed the club name in the
        # player name; filter them even if FPL somehow matches.
        is_placeholder = bool(team_name) and team_name != "—" and \
            team_name.lower() in full_name.lower()
        # A nailed starter (named in the lineups) is always in the pool, even if
        # FPL hasn't added them yet (new signings) — the user curated the list.
        in_pl = (nailed or fpl is not None) and not is_placeholder

        # Understat xG/xA (name-normalised cross-source match)
        xga: dict = {}
        if understat:
            xga = understat.get(key) or {}
            if not xga:
                last = _norm_name(sp.get("last_name") or "")
                if last:
                    xga = next(
                        (v for k, v in understat.items() if k.endswith(last)),
                        {},
                    )

        result[key] = {
            "sleeper_id":      pid,
            "name":            full_name,
            "web_name":        sp.get("last_name") or full_name,
            "team":            team_name,
            "position":        pos,
            "in_pl":           in_pl,
            "nailed":          nailed,
            # 25/26 Sleeper season totals (pts_std verbatim via _calc_pts)
            "total_pts":       total_pts,
            "ppg":             ppg,
            "pp90":            pp90,
            "games":           games,
            "minutes":         int(mins),
            # Stat breakdown
            "goals":              int(goals),
            "assists":            int(assists),
            "shots_on_target":    int(sot),
            "key_passes":         int(kp),
            "dribbles":           int(drb),
            "accurate_crosses":   int(acnc),
            "aerials_won":        int(aer),
            "clean_sheets":       int(cs),
            "saves":              int(saves),
            "high_claims":        int(hcs),
            "smothers":           int(sm),
            "tackles_won":        int(tkl),
            "interceptions":      int(ints),
            "blocked_shots":      int(blk),
            "yellow_cards":       int(yc),
            "red_cards":          int(rc),
            # FPL-sourced (cost + community consensus only — never FPL points/position)
            "cost":            fpl["cost"]          if fpl else None,
            "ownership_pct":   fpl["ownership_pct"] if fpl else None,
            # FPL set-piece orders (1 = first choice, None = not a taker)
            "pen_order":       fpl.get("pen_order")    if fpl else None,
            "corner_order":    fpl.get("corner_order") if fpl else None,
            "fk_order":        fpl.get("fk_order")     if fpl else None,
            # Understat
            "xG":              xga.get("xG"),
            "xA":              xga.get("xA"),
            "xG90":            xga.get("xG90"),
            "xA90":            xga.get("xA90"),
            "npxG":            xga.get("npxG"),
            # API-Football individual stats (PL players only)
            "starter_rate":    starter_rate,
            "apif_goals":      apif.get("goals"),
            "apif_assists":    apif.get("assists"),
            "apif_sot":        apif.get("shots_on_target"),
            "apif_key_passes": apif.get("key_passes"),
            "apif_tackles":    apif.get("tackles_total"),
            "apif_interceptions": apif.get("interceptions"),
            "apif_blocks":     apif.get("tackles_blocks"),
            "apif_saves":      apif.get("saves"),
            "apif_goals_conceded": apif.get("goals_conceded"),
            "apif_rating":     apif.get("rating"),
            "apif_starts":     apif.get("starts"),
            "apif_appearances":apif.get("appearances"),
            # Fixture-level stats (dispossessed from /fixtures/players; baked into pts_std)
            "dispossessed":    fix.get("dispossessed"),
            "dispossessed_pg": round(fix["dispossessed"] / fix["matches"], 2)
                               if fix.get("matches") else None,
            "projected_pts":   projected_pts,
            "has_stats":       bool(raw),
        }

    # ADP rank: community consensus via FPL ownership %.
    ranked = sorted(
        ((k, d) for k, d in result.items() if d["ownership_pct"] is not None),
        key=lambda x: x[1]["ownership_pct"],
        reverse=True,
    )
    for rank, (key, _) in enumerate(ranked, 1):
        result[key]["adp_rank"] = rank

    compute_vorp(result)
    return result


# Roster slots per position — how deep the league digs at each position, which
# is what sets replacement level. These are the league's actual roster limits
# (1 GK / 6 DEF / 6 MID / 3 FWD = 16, matching the 16-round draft), NOT a
# starting XI: the player you'd really fall back to is the next one on waivers
# after every team has filled its roster.
STARTERS_PER_TEAM = {"GK": 1, "DEF": 6, "MID": 6, "FWD": 3}


def compute_vorp(result: dict, num_teams: int = 10,
                 starters: Optional[dict] = None) -> None:
    """
    Add value-over-replacement ('vorp') to every player record, in place.

    Projected points alone is the wrong draft order: it compares a GK to a
    midfielder as if they were interchangeable. What actually matters at the
    table is how much a player beats the guy you could still get at that
    position later. Replacement level = the (num_teams x starters) ranked
    player at each position — i.e. the last starter the league will roster —
    so positions that cluster tightly (GKs) get compressed and positions with
    a steep drop-off (MIDs) get their scarcity priced in.

    Only draftable (in_pl) players count toward replacement level, otherwise
    departed stars and out-of-league players would drag the baseline down.
    """
    starters = starters or STARTERS_PER_TEAM
    by_pos: dict[str, list[float]] = {}
    for d in result.values():
        if d.get("in_pl", True) and d.get("projected_pts"):
            by_pos.setdefault(d.get("position", "UNK"), []).append(d["projected_pts"])

    baseline: dict[str, float] = {}
    for pos, vals in by_pos.items():
        vals.sort(reverse=True)
        idx = num_teams * starters.get(pos, 2)
        # If the pool is shallower than the league needs, fall back to the
        # worst rostered player rather than indexing off the end.
        baseline[pos] = vals[idx - 1] if len(vals) >= idx else (vals[-1] if vals else 0.0)

    for d in result.values():
        base = baseline.get(d.get("position", "UNK"), 0.0)
        d["vorp"] = round((d.get("projected_pts") or 0.0) - base, 1)
    return None


# ---------------------------------------------------------------------------
# Understat
# ---------------------------------------------------------------------------

def fetch_understat_players(year: int = 2025) -> dict[str, dict]:
    """
    Scrape Understat EPL xG/xA for the season starting in `year`.
    Returns {norm_name: {xG, xA, xG90, xA90, npxG, shots, key_passes, minutes}}.
    """
    html  = _get_html(f"{UNDERSTAT_BASE}/league/EPL/{year}")
    match = re.search(r"var\s+playersData\s*=\s*JSON\.parse\('(.+?)'\)", html)
    if not match:
        raise ValueError("playersData not found — Understat layout may have changed.")

    raw     = match.group(1)
    decoded = raw.encode("raw_unicode_escape").decode("unicode_escape")
    players_raw: list = json.loads(decoded)

    result: dict[str, dict] = {}
    for p in players_raw:
        name = p.get("player_name", "")
        if not name:
            continue
        key    = _norm_name(name)
        time_m = float(p.get("time") or 0)
        per90  = time_m / 90.0 if time_m >= 45 else 1.0
        result[key] = {
            "understat_name": name,
            "club":           p.get("team_title", ""),
            "xG":             round(float(p.get("xG")   or 0), 3),
            "xA":             round(float(p.get("xA")   or 0), 3),
            "xG90":           round(float(p.get("xG")   or 0) / per90, 3),
            "xA90":           round(float(p.get("xA")   or 0) / per90, 3),
            "npxG":           round(float(p.get("npxG") or 0), 3),
            "shots":          int(p.get("shots")      or 0),
            "key_passes":     int(p.get("key_passes") or 0),
            "minutes":        int(time_m),
        }
    return result


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------

class DraftState:
    """
    Manages live draft state.

    Lifecycle:
      1. load_draft_meta()    — light; call once inside @st.cache_resource
      2. inject_player_db()   — inject the heavy @st.cache_data result
      3. refresh()            — poll picks; call from @st.fragment(run_every=5)
    """

    def __init__(self, league_id: str, draft_id: str, my_roster_id: Optional[int] = None):
        self.league_id     = league_id
        self.draft_id      = draft_id
        self.my_roster_id  = my_roster_id

        self.draft_info:     dict = {}
        self.league_info:    dict = {}
        self.picks:          list[dict] = []
        self.players:        dict[str, dict] = {}   # sleeper_id → raw player
        self.player_data:    dict[str, dict] = {}   # norm_name  → enriched
        self.users:          dict[int, str]  = {}   # roster_id  → display_name
        self.position_order: list[str]       = list(POSITION_ORDER)

        # Status flags
        self.stats_loaded:     bool           = False
        self.stats_error:      Optional[str]  = None
        self.fpl_loaded:       bool           = False
        self.understat_loaded: bool           = False
        self.understat_error:  Optional[str]  = None

        self._last_pick_count = -1

    # ------------------------------------------------------------------
    # Boot — two phases so caching can be split in app.py
    # ------------------------------------------------------------------

    def load_draft_meta(self) -> None:
        """
        Light fetch: draft info, league info, user/roster mapping.
        Put this inside @st.cache_resource.
        Safe to call with a sentinel draft_id ("pre_draft") when no draft exists yet.
        """
        try:
            self.draft_info = get_draft(self.draft_id) if self.draft_id != "pre_draft" else {}
        except Exception:
            self.draft_info = {}
        try:
            self.league_info = get_league(self.league_id)
        except Exception:
            self.league_info = {}

        roster_positions = self.league_info.get("roster_positions", [])
        seen, ordered = set(), []
        for rp in roster_positions:
            rp_norm = _norm_pos(rp)
            if rp_norm not in seen and rp_norm not in ("BN", "IR", "FLEX", "SUPER_FLEX", "UNK"):
                seen.add(rp_norm)
                ordered.append(rp_norm)
        if ordered:
            self.position_order = ordered

        try:
            uid_to_name = {
                u["user_id"]: u.get("display_name", u["user_id"])
                for u in get_league_users(self.league_id)
            }
            for r in get_league_rosters(self.league_id):
                self.users[r["roster_id"]] = uid_to_name.get(r["owner_id"], f"Team {r['roster_id']}")
        except Exception:
            pass  # league not created yet; users/rosters will populate once it exists

    def inject_player_db(self, db: dict) -> None:
        """
        Inject pre-loaded player data from @st.cache_data.
        Call on every main-script rerun (the dict reference is stable when cached).
        """
        self.players          = db.get("players", {})
        self.player_data      = db.get("player_data", {})
        self.stats_loaded     = db.get("stats_loaded", False)
        self.stats_error      = db.get("stats_error")
        self.stats_season     = db.get("stats_season")
        self.fpl_loaded       = db.get("fpl_loaded", False)
        self.understat_loaded = db.get("understat_loaded", False)
        self.understat_error  = db.get("understat_error")

    def load_static(self, season: Optional[str] = None, understat_year: int = 2025) -> None:
        """
        Convenience wrapper: runs both phases in one call.
        Use when you don't need the cache_data / cache_resource split.
        """
        self.load_draft_meta()
        db = _fetch_player_db(season or str(_sleeper_season_year()), understat_year)
        self.inject_player_db(db)

    def refresh(self) -> bool:
        """Poll picks. Returns True if the pick list changed."""
        new_picks = get_draft_picks(self.draft_id)
        changed   = len(new_picks) != self._last_pick_count
        if changed:
            self.picks            = new_picks
            self._last_pick_count = len(new_picks)
        return changed

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def drafted_ids(self) -> set[str]:
        return {p["player_id"] for p in self.picks}

    @property
    def num_teams(self) -> int:
        return self.draft_info.get("settings", {}).get("teams", 10)

    @property
    def num_rounds(self) -> int:
        return self.draft_info.get("settings", {}).get("rounds", 17)

    @property
    def total_picks(self) -> int:
        return self.num_teams * self.num_rounds

    @property
    def current_pick(self) -> int:
        return len(self.picks) + 1

    @property
    def status(self) -> str:
        return self.draft_info.get("status", "unknown")

    # ------------------------------------------------------------------
    # Player enrichment
    # ------------------------------------------------------------------

    def _enrich(self, sleeper_id: str) -> dict:
        sp        = self.players.get(sleeper_id, {})
        full_name = sp.get("full_name") or sp.get("name") or sleeper_id
        key       = _norm_name(full_name)

        data = self.player_data.get(key) or {}
        if not data:
            last = _norm_name(sp.get("last_name") or "")
            if last:
                data = next(
                    (v for k, v in self.player_data.items() if k.endswith(last)),
                    {},
                )

        raw_pos = (
            data.get("position")
            or sp.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
        )
        pos = _norm_pos(raw_pos) if raw_pos else "UNK"

        return {
            "sleeper_id":      sleeper_id,
            "name":            data.get("name")     or full_name,
            "web_name":        data.get("web_name") or sp.get("last_name") or full_name,
            "team":            data.get("team")     or sp.get("team", ""),
            "position":        pos,
            "in_pl":           data.get("in_pl", True),
            "nailed":          data.get("nailed", False),
            "total_pts":       data.get("total_pts", 0.0),
            "ppg":             data.get("ppg", 0.0),
            "pp90":            data.get("pp90", 0.0),
            "games":           data.get("games", 0),
            "minutes":         data.get("minutes", 0),
            "goals":           data.get("goals", 0),
            "assists":         data.get("assists", 0),
            "shots_on_target": data.get("shots_on_target", 0),
            "key_passes":      data.get("key_passes", 0),
            "dribbles":        data.get("dribbles", 0),
            "accurate_crosses":data.get("accurate_crosses", 0),
            "aerials_won":     data.get("aerials_won", 0),
            "clean_sheets":    data.get("clean_sheets", 0),
            "saves":           data.get("saves", 0),
            "high_claims":     data.get("high_claims", 0),
            "smothers":        data.get("smothers", 0),
            "tackles_won":     data.get("tackles_won", 0),
            "interceptions":   data.get("interceptions", 0),
            "blocked_shots":   data.get("blocked_shots", 0),
            "yellow_cards":    data.get("yellow_cards", 0),
            "red_cards":       data.get("red_cards", 0),
            "cost":            data.get("cost"),
            "ownership_pct":   data.get("ownership_pct"),
            "pen_order":       data.get("pen_order"),
            "corner_order":    data.get("corner_order"),
            "fk_order":        data.get("fk_order"),
            "adp_rank":        data.get("adp_rank"),
            "vorp":            data.get("vorp", 0.0),
            "xG":              data.get("xG"),
            "xA":              data.get("xA"),
            "xG90":            data.get("xG90"),
            "xA90":            data.get("xA90"),
            "projected_pts":   data.get("projected_pts", 0.0),
            "has_stats":       data.get("has_stats", False),
        }

    # ------------------------------------------------------------------
    # Board data
    # ------------------------------------------------------------------

    def get_available(self, position: Optional[str] = None,
                      sort_by: str = "projected_pts") -> list[dict]:
        # Scope to the 20 current PL clubs via FPL membership — but only when
        # FPL actually loaded, else this would empty the whole board.
        filter_pl = getattr(self, "fpl_loaded", False)
        out = []
        for sid in self.players:
            if sid in self.drafted_ids:
                continue
            p = self._enrich(sid)
            if filter_pl and not p.get("in_pl", True):
                continue
            if position and p["position"] != position:
                continue
            out.append(p)
        key = sort_by if sort_by in ("projected_pts", "ppg", "pp90", "total_pts", "vorp") else "projected_pts"
        return sorted(out, key=lambda x: x[key], reverse=True)

    def get_my_picks(self) -> list[dict]:
        if self.my_roster_id is None:
            return []
        return [
            self._enrich(p["player_id"])
            for p in self.picks
            if p.get("roster_id") == self.my_roster_id
        ]

    def get_positional_needs(self) -> dict[str, int]:
        counts = {pos: 0 for pos in self.position_order}
        for p in self.get_my_picks():
            if p["position"] in counts:
                counts[p["position"]] += 1
        return counts

    def get_my_draft_slot(self) -> Optional[int]:
        if self.my_roster_id is None:
            return None
        for slot_str, rid in self.draft_info.get("slot_to_roster_id", {}).items():
            if rid == self.my_roster_id:
                return int(slot_str)
        return None

    def get_my_next_picks(self) -> list[int]:
        my_slot = self.get_my_draft_slot()
        if my_slot is None:
            return []
        n = self.num_teams
        upcoming = []
        for rnd in range(1, self.num_rounds + 1):
            # Snake: odd rounds left→right, even rounds right→left
            pick_in_round = my_slot if rnd % 2 == 1 else (n + 1 - my_slot)
            overall = (rnd - 1) * n + pick_in_round
            if overall >= self.current_pick:
                upcoming.append(overall)
        return upcoming

    def get_pick_grid(self) -> list[list[Optional[dict]]]:
        """2D grid [round_idx][slot_idx]. Columns are consistent draft slots."""
        n, r  = self.num_teams, self.num_rounds
        grid: list[list[Optional[dict]]] = [[None] * n for _ in range(r)]
        for pick in self.picks:
            rnd  = (pick.get("round")      or 1) - 1
            slot = (pick.get("draft_slot") or 1) - 1
            if 0 <= rnd < r and 0 <= slot < n:
                enriched = self._enrich(pick["player_id"])
                enriched["roster_id"] = pick.get("roster_id")
                enriched["picker"]    = self.users.get(pick.get("roster_id"), "")
                grid[rnd][slot] = enriched
        return grid


# ---------------------------------------------------------------------------
# Stand-alone heavy loader (used by @st.cache_data in app.py)
# ---------------------------------------------------------------------------

def _fetch_player_db(season: str, understat_year: int) -> dict:
    """
    Fetch all slow/large data and return a plain dict suitable for @st.cache_data.
    """
    players = get_sleeper_players()

    season_stats: dict = {}
    stats_loaded  = False
    stats_error:  Optional[str] = None
    stats_season  = season
    try:
        # Use the most recent season with real data as the projection base.
        # Keeps 25/26 as the base through the summer until 26/27 has minutes.
        season_stats, stats_season = get_projection_base_stats()
        stats_loaded = True
    except Exception as exc:
        stats_error = str(exc)

    fpl_lookup: Optional[dict] = None
    fpl_loaded = False
    try:
        bootstrap  = get_fpl_bootstrap()
        fpl_lookup = build_fpl_lookup(bootstrap)
        fpl_loaded = True
    except Exception:
        pass

    understat: Optional[dict] = None
    understat_loaded = False
    understat_error: Optional[str] = None
    try:
        understat        = fetch_understat_players(understat_year)
        understat_loaded = True
    except Exception as exc:
        understat_error = str(exc)

    # Sleeper teams endpoint — maps numeric team IDs to names (graceful fallback)
    teams_lookup = get_sleeper_teams()

    # API-Football harvested PL stats — individual per-player data (graceful fallback)
    pl_stats      = load_pl_stats()
    fixture_stats = load_fixture_stats()

    # 26/27 predicted lineups → nailed-starter expected-minutes override
    lineups     = load_lineups()
    nailed_pids, team_map = resolve_nailed_starters(players, season_stats, lineups)

    # Explicit bench/faded-role overrides (heavily discount regardless of 25/26)
    bench_pids = _match_name_list(players, season_stats, load_bench())

    # Role-promotion overrides (ignore last season's low minutes-per-appearance)
    promoted_pids = _match_name_list(players, season_stats, load_promoted())

    # Foreign per-90 stats for new signings (coefficient-adjusted projections)
    new_signings = load_new_signings()

    player_data = build_player_stats(
        players, season_stats, fpl_lookup, understat, teams_lookup,
        pl_stats, fixture_stats, nailed_pids, new_signings, team_map,
        bench_pids, promoted_pids
    )

    return {
        "players":          players,
        "player_data":      player_data,
        "stats_loaded":     stats_loaded,
        "stats_error":      stats_error,
        "stats_season":     stats_season,
        "fpl_loaded":       fpl_loaded,
        "understat_loaded": understat_loaded,
        "understat_error":  understat_error,
    }
