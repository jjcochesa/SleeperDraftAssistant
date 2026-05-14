"""
Draft engine: Sleeper + FPL + Understat data layer for EPL snake draft assistant.
"""

import json
import re
import time
import unicodedata
from typing import Optional

import requests

SLEEPER_API = "https://api.sleeper.app/v1"
FPL_API = "https://fantasy.premierleague.com/api"
UNDERSTAT_BASE = "https://understat.com"

POSITION_ORDER = ["GKP", "DEF", "MID", "FWD"]

_POS_ALIASES = {
    "k": "GKP", "gk": "GKP", "gkp": "GKP", "goalkeeper": "GKP",
    "d": "DEF", "def": "DEF", "defender": "DEF",
    "cb": "DEF", "lb": "DEF", "rb": "DEF", "wb": "DEF",
    "m": "MID", "mid": "MID", "midfielder": "MID", "cm": "MID", "am": "MID",
    "f": "FWD", "fwd": "FWD", "forward": "FWD", "st": "FWD", "att": "FWD",
}

_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; SleeperDraftAssistant/1.0)",
    "Accept-Language": "en-GB,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """Strip accents and punctuation, lowercase — for cross-source name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z\s]", "", ascii_str.lower()).strip()


def _norm_pos(pos: str) -> str:
    return _POS_ALIASES.get(pos.lower(), pos.upper())


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
    """Look up a Sleeper user by username or user_id."""
    return _get(f"{SLEEPER_API}/user/{username_or_id}")


def find_roster_id(league_id: str, user_id: str) -> Optional[int]:
    """Return the roster_id that belongs to user_id in league_id, or None."""
    for r in get_league_rosters(league_id):
        if r.get("owner_id") == user_id:
            return r["roster_id"]
    return None


# ---------------------------------------------------------------------------
# FPL API + projections
# ---------------------------------------------------------------------------

def get_fpl_bootstrap() -> dict:
    return _get(f"{FPL_API}/bootstrap-static/")


def _project_score(fpl: dict, xga: Optional[dict] = None) -> float:
    """
    Blend FPL ep_next / form / historical pts with Understat xG+xA when available.
    Returns an estimated full-season points total.
    """
    ep_next = float(fpl.get("ep_next") or 0)
    form = float(fpl.get("form") or 0)
    total_pts = float(fpl.get("total_points") or 0)
    minutes = float(fpl.get("minutes") or 1)

    pts_per_min = total_pts / minutes
    historical_season = pts_per_min * 3420        # 38 GWs × ~90 min
    form_season = form * 38
    ep_season = ep_next * 38

    fpl_base = 0.40 * form_season + 0.35 * ep_season + 0.25 * historical_season

    if xga and (xga.get("xG", 0) or xga.get("xA", 0)):
        # Rough pts from goal/assist involvement: xG ~4.5 pts, xA ~3 pts on avg across positions
        xga_pts = xga.get("xG", 0) * 4.5 + xga.get("xA", 0) * 3.0
        return round(0.65 * fpl_base + 0.35 * xga_pts, 1)

    return round(fpl_base, 1)


def build_fpl_projections(understat: Optional[dict] = None) -> dict[str, dict]:
    """
    Return {norm_name: stats_dict}. Merges Understat xG/xA when provided.
    understat: output of fetch_understat_players().
    """
    bootstrap = get_fpl_bootstrap()
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    fpl_pos = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

    projections = {}
    for p in bootstrap["elements"]:
        full_name = f"{p['first_name']} {p['second_name']}"
        key = _norm_name(full_name)

        xga = None
        if understat:
            xga = understat.get(key)
            if xga is None:
                # Try last-name-only fallback
                last = _norm_name(p["second_name"])
                xga = next((v for k, v in understat.items() if last and k.endswith(last)), None)

        projections[key] = {
            "fpl_id": p["id"],
            "name": full_name,
            "web_name": p["web_name"],
            "team": teams.get(p["team"], ""),
            "position": fpl_pos.get(p["element_type"], "UNK"),
            "total_points": int(p.get("total_points") or 0),
            "minutes": int(p.get("minutes") or 0),
            "goals": int(p.get("goals_scored") or 0),
            "assists": int(p.get("assists") or 0),
            "clean_sheets": int(p.get("clean_sheets") or 0),
            "form": float(p.get("form") or 0),
            "ep_next": float(p.get("ep_next") or 0),
            "selected_pct": float(p.get("selected_by_percent") or 0),
            "now_cost": (p.get("now_cost") or 0) / 10,
            # Understat fields (None if not matched)
            "xG": xga.get("xG") if xga else None,
            "xA": xga.get("xA") if xga else None,
            "xG90": xga.get("xG90") if xga else None,
            "xA90": xga.get("xA90") if xga else None,
            "npxG": xga.get("npxG") if xga else None,
            "shots": xga.get("shots") if xga else None,
            "key_passes": xga.get("key_passes") if xga else None,
            "projected_pts": _project_score(p, xga),
        }
    return projections


# ---------------------------------------------------------------------------
# Understat
# ---------------------------------------------------------------------------

def fetch_understat_players(year: int = 2025) -> dict[str, dict]:
    """
    Scrape Understat's EPL player-level xG/xA data for the given season year.
    year=2025 → 2025/26 season.
    Returns {norm_name: {xG, xA, xG90, xA90, npxG, shots, key_passes, minutes}}.
    """
    html = _get_html(f"{UNDERSTAT_BASE}/league/EPL/{year}")

    match = re.search(r"var\s+playersData\s*=\s*JSON\.parse\('(.+?)'\)", html)
    if not match:
        raise ValueError("playersData not found in Understat page — layout may have changed.")

    raw = match.group(1)
    # Understat embeds the JSON as a JS string with unicode-escaped chars
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
            "club": p.get("team_title", ""),
            "xG": round(float(p.get("xG") or 0), 3),
            "xA": round(float(p.get("xA") or 0), 3),
            "xG90": round(float(p.get("xG") or 0) / per90, 3),
            "xA90": round(float(p.get("xA") or 0) / per90, 3),
            "npxG": round(float(p.get("npxG") or 0), 3),
            "shots": int(p.get("shots") or 0),
            "key_passes": int(p.get("key_passes") or 0),
            "minutes": int(time_min),
        }
    return result


# ---------------------------------------------------------------------------
# ADP from historical draft
# ---------------------------------------------------------------------------

def build_adp_from_draft(draft_id: str, players: dict) -> dict[str, int]:
    """
    Build an ADP lookup from a historical Sleeper draft's pick order.
    Returns {norm_name: pick_number}.
    """
    picks = get_draft_picks(draft_id)
    adp: dict[str, int] = {}
    for pick in picks:
        sp = players.get(pick.get("player_id", ""), {})
        full_name = sp.get("full_name") or sp.get("name") or pick.get("player_id", "")
        key = _norm_name(full_name)
        if key:
            adp[key] = pick.get("pick_no") or pick.get("draft_slot", 999)
    return adp


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------

class DraftState:
    """
    Manages live draft state. Call load_static() once at session start,
    then refresh() periodically to poll for new picks.
    """

    def __init__(self, league_id: str, draft_id: str, my_roster_id: Optional[int] = None):
        self.league_id = league_id
        self.draft_id = draft_id
        self.my_roster_id = my_roster_id

        self.draft_info: dict = {}
        self.picks: list[dict] = []
        self.players: dict[str, dict] = {}       # sleeper_id → player info
        self.projections: dict[str, dict] = {}   # norm_name  → fpl+xga data
        self.users: dict[int, str] = {}          # roster_id  → display_name
        self.adp_data: Optional[dict[str, int]] = None  # norm_name → pick_no

        # Status flags set by load_static()
        self.fpl_loaded = False
        self.understat_loaded = False
        self.understat_error: Optional[str] = None

        self._last_pick_count = -1

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    def load_static(self, understat_year: int = 2025) -> None:
        """Fetch once-per-session data. Understat failure is non-fatal."""
        self.draft_info = get_draft(self.draft_id)
        self.players = get_sleeper_players("epl")

        understat: Optional[dict] = None
        try:
            understat = fetch_understat_players(understat_year)
            self.understat_loaded = True
        except Exception as exc:
            self.understat_error = str(exc)

        try:
            self.projections = build_fpl_projections(understat)
            self.fpl_loaded = True
        except Exception:
            self.projections = {}

        users_raw = get_league_users(self.league_id)
        rosters_raw = get_league_rosters(self.league_id)
        uid_to_name = {u["user_id"]: u.get("display_name", u["user_id"]) for u in users_raw}
        for r in rosters_raw:
            self.users[r["roster_id"]] = uid_to_name.get(r["owner_id"], f"Team {r['roster_id']}")

    def load_adp_from_draft(self, prev_draft_id: str) -> None:
        """Populate self.adp_data from a historical draft's pick order."""
        self.adp_data = build_adp_from_draft(prev_draft_id, self.players)

    def refresh(self) -> bool:
        """Poll for new picks. Returns True if the pick list changed."""
        new_picks = get_draft_picks(self.draft_id)
        changed = len(new_picks) != self._last_pick_count
        if changed:
            self.picks = new_picks
            self._last_pick_count = len(new_picks)
        return changed

    # ------------------------------------------------------------------
    # Derived properties
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
        """Merge Sleeper player record with FPL + Understat projection data."""
        sp = self.players.get(sleeper_id, {})
        full_name = sp.get("full_name") or sp.get("name") or sleeper_id
        key = _norm_name(full_name)

        proj = self.projections.get(key) or {}
        if not proj:
            last = _norm_name(sp.get("last_name") or "")
            proj = next(
                (v for k, v in self.projections.items() if last and k.endswith(last)),
                {},
            )

        raw_pos = (
            proj.get("position")
            or (sp.get("fantasy_positions") or [""])[0]
            or sp.get("position", "")
        )

        return {
            "sleeper_id": sleeper_id,
            "name": proj.get("name") or full_name,
            "web_name": proj.get("web_name") or sp.get("last_name") or full_name,
            "team": proj.get("team") or sp.get("team", ""),
            "position": _norm_pos(raw_pos) if raw_pos else "UNK",
            "projected_pts": proj.get("projected_pts", 0.0),
            "total_points": proj.get("total_points", 0),
            "minutes": proj.get("minutes", 0),
            "goals": proj.get("goals", 0),
            "assists": proj.get("assists", 0),
            "clean_sheets": proj.get("clean_sheets", 0),
            "form": proj.get("form", 0.0),
            "ep_next": proj.get("ep_next", 0.0),
            "selected_pct": proj.get("selected_pct", 0.0),
            "cost": proj.get("now_cost", 0.0),
            "xG": proj.get("xG"),
            "xA": proj.get("xA"),
            "xG90": proj.get("xG90"),
            "xA90": proj.get("xA90"),
            "npxG": proj.get("npxG"),
            "shots": proj.get("shots"),
            "key_passes": proj.get("key_passes"),
            "matched_fpl": bool(proj),
        }

    # ------------------------------------------------------------------
    # Board data
    # ------------------------------------------------------------------

    def get_available(self, position: Optional[str] = None) -> list[dict]:
        out = []
        for sid in self.players:
            if sid in self.drafted_ids:
                continue
            p = self._enrich(sid)
            if position and p["position"] != position:
                continue
            out.append(p)
        return sorted(out, key=lambda x: x["projected_pts"], reverse=True)

    def get_my_picks(self) -> list[dict]:
        if self.my_roster_id is None:
            return []
        return [
            self._enrich(p["player_id"])
            for p in self.picks
            if p.get("roster_id") == self.my_roster_id
        ]

    def get_positional_needs(self) -> dict[str, int]:
        counts = {pos: 0 for pos in POSITION_ORDER}
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
        current = self.current_pick
        upcoming = []
        for rnd in range(1, self.num_rounds + 1):
            pick_in_round = my_slot if rnd % 2 == 1 else (n + 1 - my_slot)
            overall = (rnd - 1) * n + pick_in_round
            if overall >= current:
                upcoming.append(overall)
        return upcoming

    def get_pick_grid(self) -> list[list[Optional[dict]]]:
        """2D grid [round_idx][slot_idx]. Each column = consistent draft slot (snake-aware)."""
        n, r = self.num_teams, self.num_rounds
        grid: list[list[Optional[dict]]] = [[None] * n for _ in range(r)]

        for pick in self.picks:
            rnd = (pick.get("round") or 1) - 1
            slot = (pick.get("draft_slot") or 1) - 1
            if 0 <= rnd < r and 0 <= slot < n:
                enriched = self._enrich(pick["player_id"])
                enriched["roster_id"] = pick.get("roster_id")
                enriched["picker"] = self.users.get(pick.get("roster_id"), "")
                grid[rnd][slot] = enriched

        return grid

    def get_adp_analysis(self, adp_lookup: Optional[dict] = None) -> list[dict]:
        """
        Rank undrafted players by value; overlay ADP rank if data is available.
        adp_lookup overrides self.adp_data. Positive value_diff = undervalued.
        """
        lookup = adp_lookup if adp_lookup is not None else self.adp_data
        available = self.get_available()

        result = []
        for i, player in enumerate(available):
            value_rank = i + 1
            adp_rank = lookup.get(_norm_name(player["name"])) if lookup else None
            value_diff = (adp_rank - value_rank) if adp_rank is not None else None
            result.append({
                **player,
                "value_rank": value_rank,
                "adp": adp_rank,
                "value_diff": value_diff,
            })

        if lookup:
            result.sort(key=lambda x: x["value_diff"] if x["value_diff"] is not None else 0, reverse=True)

        return result
