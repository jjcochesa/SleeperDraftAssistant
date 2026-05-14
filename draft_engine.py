"""
Draft engine: Sleeper EPL stats + Understat xG/xA for the snake draft assistant.
All points/projections use Sleeper's own scoring, not FPL.
"""

import json
import re
import time
import unicodedata
from typing import Optional

import requests

SLEEPER_API = "https://api.sleeper.app/v1"
UNDERSTAT_BASE = "https://understat.com"

# Updated once load_static() calls get_league() — overridden if league has custom positions
POSITION_ORDER = ["GK", "DEF", "MID", "FWD"]

# Normalise whatever Sleeper / Understat return to our four canonical labels
_POS_ALIASES = {
    "gk": "GK", "gkp": "GK", "goalkeeper": "GK", "k": "GK",
    "def": "DEF", "defender": "DEF", "d": "DEF",
    "cb": "DEF", "lb": "DEF", "rb": "DEF", "wb": "DEF",
    "mid": "MID", "midfielder": "MID", "m": "MID", "cm": "MID", "am": "MID",
    "fwd": "FWD", "forward": "FWD", "f": "FWD",
    "st": "FWD", "att": "FWD", "str": "FWD", "strk": "FWD",
}

_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; SleeperDraftAssistant/1.0)",
})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z\s]", "", ascii_str.lower()).strip()


def _norm_pos(raw: str) -> str:
    return _POS_ALIASES.get(raw.lower(), raw.upper())


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


def get_sleeper_players(sport: str = "epl") -> dict:
    return _get(f"{SLEEPER_API}/players/{sport}")


def get_sleeper_user(username_or_id: str) -> dict:
    return _get(f"{SLEEPER_API}/user/{username_or_id}")


def find_roster_id(league_id: str, user_id: str) -> Optional[int]:
    for r in get_league_rosters(league_id):
        if r.get("owner_id") == user_id:
            return r["roster_id"]
    return None


def get_sleeper_season_stats(sport: str = "epl", season: str = "2025") -> dict:
    """
    Fetch season-level player stats from Sleeper.
    Returns {player_id: raw_stats_dict}.
    Season "2025" = the 2025/26 EPL season.
    """
    return _get(f"{SLEEPER_API}/stats/{sport}/regular/{season}")


def get_sleeper_week_stats(sport: str, season: str, week: int) -> dict:
    """Single-GW stats fallback."""
    return _get(f"{SLEEPER_API}/stats/{sport}/regular/{season}/{week}")


# ---------------------------------------------------------------------------
# Sleeper points calculation
# ---------------------------------------------------------------------------

def _calc_pts(raw: dict, scoring: dict, position: str) -> float:
    """
    Apply the league's scoring_settings to a player's raw season stats.
    Tries the pre-computed pts_pts field first, then falls back to manual calc.
    """
    # Sleeper sometimes stores the pre-computed total directly
    pre = raw.get("pts_pts") or raw.get("fpts") or raw.get("pts")
    if pre is not None:
        return round(float(pre), 2)

    pos = position.lower()
    pts = 0.0

    # Goals (position-specific scoring key)
    goals = float(raw.get("goals_scored", raw.get("goals", 0)) or 0)
    pts += goals * float(scoring.get(f"goal_scored_{pos}", scoring.get("goal_scored", 0)) or 0)

    # Assists
    assists = float(raw.get("ast", raw.get("assists", 0)) or 0)
    pts += assists * float(scoring.get("ast", scoring.get("assist", 0)) or 0)

    # Clean sheets (position-specific)
    cs = float(raw.get("clean_sheets", raw.get("cs", 0)) or 0)
    pts += cs * float(scoring.get(f"cs_{pos}", 0) or 0)

    # Saves (GK only)
    if pos == "gk":
        saves = float(raw.get("saves", raw.get("save", 0)) or 0)
        pts += saves * float(scoring.get("save", scoring.get("save_x", 0)) or 0)

    # Conceding goals (negative, position-specific)
    goals_conceded = float(raw.get("goals_conceded", raw.get("gls_conceded", 0)) or 0)
    pts += goals_conceded * float(scoring.get(f"goals_conceded_{pos}", 0) or 0)

    # Cards
    yc = float(raw.get("yc", raw.get("yellow_cards", 0)) or 0)
    rc = float(raw.get("rc", raw.get("red_cards", 0)) or 0)
    pts += yc * float(scoring.get("yc", 0) or 0)
    pts += rc * float(scoring.get("rc", 0) or 0)

    # Bonus points
    bonus = float(raw.get("bonus", 0) or 0)
    pts += bonus * float(scoring.get("bonus", 1) or 1)

    return round(pts, 2)


def build_player_stats(
    players: dict,
    season_stats: dict,
    scoring: dict,
    understat: Optional[dict] = None,
) -> dict[str, dict]:
    """
    Merge Sleeper player info, season stats, and optional Understat xG/xA.
    Returns {norm_name: enriched_dict} keyed by normalised player name.
    """
    result: dict[str, dict] = {}

    for pid, sp in players.items():
        raw = season_stats.get(pid, {})

        full_name = sp.get("full_name") or sp.get("name") or pid
        key = _norm_name(full_name)

        raw_pos = (
            sp.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
            or ""
        )
        pos = _norm_pos(raw_pos) if raw_pos else "UNK"

        # Sleeper points for 25/26
        total_pts = _calc_pts(raw, scoring, pos)
        games = int(raw.get("gms_active", raw.get("games_played", raw.get("gp", 0))) or 0)
        ppg = round(total_pts / games, 2) if games > 0 else 0.0
        mins = int(raw.get("mins_played", raw.get("minutes", 0)) or 0)

        # Raw stats (for display)
        goals   = int(raw.get("goals_scored", raw.get("goals", 0)) or 0)
        assists = int(raw.get("ast", raw.get("assists", 0)) or 0)
        cs      = int(raw.get("clean_sheets", raw.get("cs", 0)) or 0)
        saves   = int(raw.get("saves", raw.get("save", 0)) or 0)
        yc      = int(raw.get("yc", raw.get("yellow_cards", 0)) or 0)
        rc      = int(raw.get("rc", raw.get("red_cards", 0)) or 0)

        # Understat xG/xA match by name
        xga: dict = {}
        if understat:
            xga = understat.get(key) or {}
            if not xga:
                last = _norm_name(sp.get("last_name") or "")
                xga = next((v for k, v in understat.items() if last and k.endswith(last)), {})

        result[key] = {
            "sleeper_id":  pid,
            "name":        full_name,
            "web_name":    sp.get("last_name") or full_name,
            "team":        sp.get("team", ""),
            "position":    pos,
            # Sleeper season stats (25/26)
            "total_pts":   total_pts,
            "ppg":         ppg,
            "games":       games,
            "minutes":     mins,
            "goals":       goals,
            "assists":     assists,
            "clean_sheets":cs,
            "saves":       saves,
            "yellow_cards":yc,
            "red_cards":   rc,
            # Understat
            "xG":          xga.get("xG"),
            "xA":          xga.get("xA"),
            "xG90":        xga.get("xG90"),
            "xA90":        xga.get("xA90"),
            "npxG":        xga.get("npxG"),
            "has_stats":   bool(raw),
        }

    return result


# ---------------------------------------------------------------------------
# Understat
# ---------------------------------------------------------------------------

def fetch_understat_players(year: int = 2025) -> dict[str, dict]:
    """
    Scrape Understat EPL xG/xA for the season starting in `year`.
    Returns {norm_name: {xG, xA, xG90, xA90, npxG, shots, key_passes, minutes}}.
    """
    html = _get_html(f"{UNDERSTAT_BASE}/league/EPL/{year}")
    match = re.search(r"var\s+playersData\s*=\s*JSON\.parse\('(.+?)'\)", html)
    if not match:
        raise ValueError("playersData not found in Understat page — layout may have changed.")

    raw = match.group(1)
    decoded = raw.encode("raw_unicode_escape").decode("unicode_escape")
    players_raw: list = json.loads(decoded)

    result: dict[str, dict] = {}
    for p in players_raw:
        name = p.get("player_name", "")
        if not name:
            continue
        key = _norm_name(name)
        time_min = float(p.get("time") or 0)
        per90 = time_min / 90.0 if time_min >= 45 else 1.0
        result[key] = {
            "understat_name": name,
            "club":      p.get("team_title", ""),
            "xG":        round(float(p.get("xG") or 0), 3),
            "xA":        round(float(p.get("xA") or 0), 3),
            "xG90":      round(float(p.get("xG") or 0) / per90, 3),
            "xA90":      round(float(p.get("xA") or 0) / per90, 3),
            "npxG":      round(float(p.get("npxG") or 0), 3),
            "shots":     int(p.get("shots") or 0),
            "key_passes":int(p.get("key_passes") or 0),
            "minutes":   int(time_min),
        }
    return result


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------

class DraftState:
    """
    Manages live draft state.
    Call load_static() once per session, then refresh() to poll picks.
    """

    def __init__(self, league_id: str, draft_id: str, my_roster_id: Optional[int] = None):
        self.league_id = league_id
        self.draft_id = draft_id
        self.my_roster_id = my_roster_id

        self.draft_info: dict = {}
        self.league_info: dict = {}
        self.scoring: dict = {}              # league scoring_settings
        self.picks: list[dict] = []
        self.players: dict[str, dict] = {}   # sleeper_id → raw player
        self.player_data: dict[str, dict] = {}  # norm_name → enriched
        self.users: dict[int, str] = {}      # roster_id → display_name
        self.position_order: list[str] = list(POSITION_ORDER)

        # Status flags
        self.stats_loaded = False
        self.stats_error: Optional[str] = None
        self.understat_loaded = False
        self.understat_error: Optional[str] = None

        self._last_pick_count = -1

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    def load_static(self, season: str = "2025", understat_year: int = 2025) -> None:
        self.draft_info = get_draft(self.draft_id)
        self.league_info = get_league(self.league_id)
        self.scoring = self.league_info.get("scoring_settings", {})

        # Derive position order from league roster positions
        roster_positions = self.league_info.get("roster_positions", [])
        seen, ordered = set(), []
        for rp in roster_positions:
            rp_norm = _norm_pos(rp)
            if rp_norm not in seen and rp_norm not in ("BN", "IR", "FLEX", "SUPER_FLEX", "UNK"):
                seen.add(rp_norm)
                ordered.append(rp_norm)
        if ordered:
            self.position_order = ordered

        self.players = get_sleeper_players("epl")

        # Understat (non-fatal)
        understat: Optional[dict] = None
        try:
            understat = fetch_understat_players(understat_year)
            self.understat_loaded = True
        except Exception as exc:
            self.understat_error = str(exc)

        # Sleeper season stats (non-fatal)
        season_stats: dict = {}
        try:
            season_stats = get_sleeper_season_stats("epl", season)
            self.stats_loaded = True
        except Exception as exc:
            self.stats_error = str(exc)

        self.player_data = build_player_stats(
            self.players, season_stats, self.scoring, understat
        )

        # User / roster mapping
        users_raw   = get_league_users(self.league_id)
        rosters_raw = get_league_rosters(self.league_id)
        uid_to_name = {u["user_id"]: u.get("display_name", u["user_id"]) for u in users_raw}
        for r in rosters_raw:
            self.users[r["roster_id"]] = uid_to_name.get(r["owner_id"], f"Team {r['roster_id']}")

    def refresh(self) -> bool:
        new_picks = get_draft_picks(self.draft_id)
        changed = len(new_picks) != self._last_pick_count
        if changed:
            self.picks = new_picks
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
        sp = self.players.get(sleeper_id, {})
        full_name = sp.get("full_name") or sp.get("name") or sleeper_id
        key = _norm_name(full_name)

        data = self.player_data.get(key) or {}
        if not data:
            # last-name fallback
            last = _norm_name(sp.get("last_name") or "")
            data = next(
                (v for k, v in self.player_data.items() if last and k.endswith(last)),
                {},
            )

        raw_pos = (
            data.get("position")
            or sp.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
        )
        pos = _norm_pos(raw_pos) if raw_pos else "UNK"

        return {
            "sleeper_id":   sleeper_id,
            "name":         data.get("name") or full_name,
            "web_name":     data.get("web_name") or sp.get("last_name") or full_name,
            "team":         data.get("team") or sp.get("team", ""),
            "position":     pos,
            "total_pts":    data.get("total_pts", 0.0),
            "ppg":          data.get("ppg", 0.0),
            "games":        data.get("games", 0),
            "minutes":      data.get("minutes", 0),
            "goals":        data.get("goals", 0),
            "assists":      data.get("assists", 0),
            "clean_sheets": data.get("clean_sheets", 0),
            "saves":        data.get("saves", 0),
            "yellow_cards": data.get("yellow_cards", 0),
            "red_cards":    data.get("red_cards", 0),
            "xG":           data.get("xG"),
            "xA":           data.get("xA"),
            "xG90":         data.get("xG90"),
            "xA90":         data.get("xA90"),
            "has_stats":    data.get("has_stats", False),
        }

    # ------------------------------------------------------------------
    # Board data
    # ------------------------------------------------------------------

    def get_available(self, position: Optional[str] = None) -> list[dict]:
        """Undrafted players sorted by PPG desc, optionally filtered by position."""
        out = []
        for sid in self.players:
            if sid in self.drafted_ids:
                continue
            p = self._enrich(sid)
            if position and p["position"] != position:
                continue
            out.append(p)
        return sorted(out, key=lambda x: x["ppg"], reverse=True)

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
            pos = p.get("position", "")
            if pos in counts:
                counts[pos] += 1
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
            pick_in_round = my_slot if rnd % 2 == 1 else (n + 1 - my_slot)
            overall = (rnd - 1) * n + pick_in_round
            if overall >= self.current_pick:
                upcoming.append(overall)
        return upcoming

    def get_pick_grid(self) -> list[list[Optional[dict]]]:
        n, r = self.num_teams, self.num_rounds
        grid: list[list[Optional[dict]]] = [[None] * n for _ in range(r)]
        for pick in self.picks:
            rnd  = (pick.get("round") or 1) - 1
            slot = (pick.get("draft_slot") or 1) - 1
            if 0 <= rnd < r and 0 <= slot < n:
                enriched = self._enrich(pick["player_id"])
                enriched["roster_id"] = pick.get("roster_id")
                enriched["picker"]    = self.users.get(pick.get("roster_id"), "")
                grid[rnd][slot] = enriched
        return grid
