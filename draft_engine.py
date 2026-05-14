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

# Sleeper API short codes → canonical stat name (try fields in order)
_SLEEPER_FIELD: dict[str, list[str]] = {
    "goals":                ["g"],
    "assists":              ["at"],
    "shots_on_target":      ["sot"],
    "key_passes":           ["kp"],
    "successful_dribbles":  ["drb"],
    "accurate_crosses":     ["acnc"],
    "aerials_won":          ["aer"],
    "effective_clearances": ["clr"],
    "saves":                ["svs"],
    "clean_sheets":         ["cos"],
    "tackles_won":          ["tkw"],
    "interceptions":        ["int"],
    "blocked_shots":        ["bs"],
    "goals_against":        ["ga"],
    "own_goals":            ["og"],
    "penalties_missed":     ["pm"],
    "penalties_saved":      ["ps"],
    "yellow_card":          ["yc"],
    "red_card":             ["rc"],
    "minutes":              ["min"],
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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """
    Accent-strip + lowercase for cross-source name matching.
    Turkish dotless-ı (U+0131) has no NFKD decomposition — replaced explicitly.
    """
    name = name.replace("ı", "i").replace("İ", "i")
    nfkd = unicodedata.normalize("NFKD", name.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _norm_pos(raw: str) -> str:
    return _POS_ALIASES.get(raw.lower(), raw.upper())


def _sleeper_season_year() -> int:
    """Return the Sleeper season year for the current EPL season (starts August)."""
    now = datetime.now()
    return now.year if now.month >= 8 else now.year - 1



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
    Return {norm_name: {"cost": float, "ownership_pct": float, "team_name": str}}
    Keyed by normalised player name for cross-source matching.
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

def build_player_stats(
    players:      dict,
    season_stats: dict,
    fpl_lookup:   Optional[dict] = None,
    understat:    Optional[dict] = None,
) -> dict[str, dict]:
    """
    Merge Sleeper player info, season stats, FPL cost/ownership, Understat xG/xA.
    Returns {norm_name: enriched_dict}.

    Scoring insight baked into defensive stat extraction:
    Sleeper rewards defensive volume (tkl=1, int=1, blk=1, sot=2).
    Box-to-box MIDs (Rice, Garner) outperform; pure-attacking MIDs without
    defensive actions underperform vs community expectations.
    """
    result: dict[str, dict] = {}

    for pid, sp in players.items():
        raw = season_stats.get(pid, {})

        full_name = sp.get("full_name") or sp.get("name") or pid
        key       = _norm_name(full_name)

        # Position: always use Sleeper's classification, never FPL's
        raw_pos = (
            sp.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
            or ""
        )
        pos = _norm_pos(raw_pos) if raw_pos else "UNK"

        # Sleeper points and games
        total_pts = _calc_pts(raw, pos)
        mins      = _raw_stat(raw, "minutes")
        # Approximate GW appearances from minutes (cap at 38)
        games     = min(38, round(mins / 90)) if mins > 0 else 0
        ppg       = round(total_pts / games, 2) if games > 0 else 0.0

        # 26/27 projection: ppg × 34 GWs; require ≥5 games to avoid small-sample noise
        projected_pts = round(ppg * 34, 1) if games >= 5 else 0.0

        # Raw stats
        goals  = _raw_stat(raw, "goals")
        assists= _raw_stat(raw, "assists")
        sot    = _raw_stat(raw, "shots_on_target")
        kp     = _raw_stat(raw, "key_passes")
        cs     = _raw_stat(raw, "clean_sheets")
        saves  = _raw_stat(raw, "saves")
        tkl    = _raw_stat(raw, "tackles_won")
        ints   = _raw_stat(raw, "interceptions")
        blk    = _raw_stat(raw, "blocked_shots")
        yc     = _raw_stat(raw, "yellow_card")
        rc     = _raw_stat(raw, "red_card")

        # FPL: cost + ownership (for ADP proxy) — no positional data used
        fpl = fpl_lookup.get(key) if fpl_lookup else None
        if fpl is None and fpl_lookup:
            last = _norm_name(sp.get("last_name") or "")
            if last:
                fpl = next(
                    (v for k, v in fpl_lookup.items() if k.endswith(last)),
                    None,
                )

        # Understat xG/xA
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
            "team":            (fpl.get("team_name") if fpl else None) or sp.get("team", ""),
            "position":        pos,
            # 25/26 Sleeper season totals
            "total_pts":       total_pts,
            "ppg":             ppg,
            "games":           games,
            "minutes":         int(mins),
            # Stat breakdown
            "goals":           int(goals),
            "assists":         int(assists),
            "shots_on_target": int(sot),
            "key_passes":      int(kp),
            "clean_sheets":    int(cs),
            "saves":           int(saves),
            "tackles_won":     int(tkl),
            "interceptions":   int(ints),
            "blocked_shots":   int(blk),
            "yellow_cards":    int(yc),
            "red_cards":       int(rc),
            # FPL-sourced fields (cost + community consensus only)
            "cost":            fpl["cost"]          if fpl else None,
            "ownership_pct":   fpl["ownership_pct"] if fpl else None,
            # Understat
            "xG":              xga.get("xG"),
            "xA":              xga.get("xA"),
            "xG90":            xga.get("xG90"),
            "xA90":            xga.get("xA90"),
            "npxG":            xga.get("npxG"),
            "projected_pts":   projected_pts,
            "has_stats":       bool(raw),
        }

    # ADP rank: community consensus via FPL ownership %.
    # Higher ownership → lower (better) ADP rank.
    # Excludes players with no FPL match.
    ranked = sorted(
        ((k, d) for k, d in result.items() if d["ownership_pct"] is not None),
        key=lambda x: x[1]["ownership_pct"],
        reverse=True,
    )
    for rank, (key, _) in enumerate(ranked, 1):
        result[key]["adp_rank"] = rank

    return result


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
            "total_pts":       data.get("total_pts", 0.0),
            "ppg":             data.get("ppg", 0.0),
            "games":           data.get("games", 0),
            "minutes":         data.get("minutes", 0),
            "goals":           data.get("goals", 0),
            "assists":         data.get("assists", 0),
            "shots_on_target": data.get("shots_on_target", 0),
            "key_passes":      data.get("key_passes", 0),
            "clean_sheets":    data.get("clean_sheets", 0),
            "saves":           data.get("saves", 0),
            "tackles_won":     data.get("tackles_won", 0),
            "interceptions":   data.get("interceptions", 0),
            "blocked_shots":   data.get("blocked_shots", 0),
            "yellow_cards":    data.get("yellow_cards", 0),
            "red_cards":       data.get("red_cards", 0),
            "cost":            data.get("cost"),
            "ownership_pct":   data.get("ownership_pct"),
            "adp_rank":        data.get("adp_rank"),
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
        out = []
        for sid in self.players:
            if sid in self.drafted_ids:
                continue
            p = self._enrich(sid)
            if position and p["position"] != position:
                continue
            out.append(p)
        key = sort_by if sort_by in ("projected_pts", "ppg", "total_pts") else "projected_pts"
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
    try:
        season_stats = get_sleeper_season_stats(season)
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

    player_data = build_player_stats(players, season_stats, fpl_lookup, understat)

    return {
        "players":          players,
        "player_data":      player_data,
        "stats_loaded":     stats_loaded,
        "stats_error":      stats_error,
        "fpl_loaded":       fpl_loaded,
        "understat_loaded": understat_loaded,
        "understat_error":  understat_error,
    }
