"""
Draft engine: Sleeper + FPL data layer for the EPL snake draft assistant.
"""

import re
import time
import unicodedata
from typing import Optional

import requests

SLEEPER_API = "https://api.sleeper.app/v1"
FPL_API = "https://fantasy.premierleague.com/api"

POSITION_ORDER = ["GKP", "DEF", "MID", "FWD"]

_POS_ALIASES = {
    "k": "GKP", "gk": "GKP", "gkp": "GKP", "goalkeeper": "GKP",
    "d": "DEF", "def": "DEF", "defender": "DEF",
    "cb": "DEF", "lb": "DEF", "rb": "DEF", "wb": "DEF",
    "m": "MID", "mid": "MID", "midfielder": "MID", "cm": "MID", "am": "MID",
    "f": "FWD", "fwd": "FWD", "forward": "FWD", "st": "FWD", "att": "FWD",
}

_http = requests.Session()
_http.headers.update({"User-Agent": "SleeperDraftAssistant/1.0"})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """Strip accents and lowercase for cross-source name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z\s]", "", ascii_str.lower()).strip()


def _norm_pos(pos: str) -> str:
    return _POS_ALIASES.get(pos.lower(), pos.upper())


def _get(url: str, retries: int = 3) -> dict | list:
    for attempt in range(retries):
        try:
            r = _http.get(url, timeout=10)
            r.raise_for_status()
            return r.json()
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
    """Return {player_id: player_info} for all players in the sport."""
    return _get(f"{SLEEPER_API}/players/{sport}")


# ---------------------------------------------------------------------------
# FPL API + projections
# ---------------------------------------------------------------------------

def get_fpl_bootstrap() -> dict:
    return _get(f"{FPL_API}/bootstrap-static/")


def _fpl_projected_pts(p: dict) -> float:
    """
    Blend FPL's own ep_next, recent form, and season history into a
    single projected-points-per-GW figure scaled to a 38-GW season.
    """
    ep_next = float(p.get("ep_next") or 0)
    form = float(p.get("form") or 0)
    total_pts = float(p.get("total_points") or 0)
    minutes = float(p.get("minutes") or 1)

    # Points per 90 → annualised over 38 GWs (≈3420 mins)
    pts_per_min = total_pts / minutes
    historical_season = pts_per_min * 3420

    # form * 38 gives a form-based full-season extrapolation
    form_season = form * 38

    # ep_next is one GW → scale to season
    ep_season = ep_next * 38

    return round(0.4 * form_season + 0.35 * ep_season + 0.25 * historical_season, 1)


def build_fpl_projections() -> dict[str, dict]:
    """Return {norm_name: stats_dict} keyed by normalised player name."""
    bootstrap = get_fpl_bootstrap()
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    fpl_pos = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

    projections = {}
    for p in bootstrap["elements"]:
        full_name = f"{p['first_name']} {p['second_name']}"
        key = _norm_name(full_name)
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
            "projected_pts": _fpl_projected_pts(p),
        }
    return projections


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
        self.projections: dict[str, dict] = {}   # norm_name  → fpl data
        self.users: dict[int, str] = {}          # roster_id  → display_name
        self._last_pick_count = -1

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    def load_static(self) -> None:
        """Fetch once-per-session data: draft info, players, projections, users."""
        self.draft_info = get_draft(self.draft_id)
        self.players = get_sleeper_players("epl")
        self.projections = build_fpl_projections()

        users_raw = get_league_users(self.league_id)
        rosters_raw = get_league_rosters(self.league_id)
        uid_to_name = {u["user_id"]: u.get("display_name", u["user_id"]) for u in users_raw}
        for r in rosters_raw:
            self.users[r["roster_id"]] = uid_to_name.get(r["owner_id"], f"Team {r['roster_id']}")

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
        """Merge Sleeper player record with FPL projection data."""
        sp = self.players.get(sleeper_id, {})
        full_name = sp.get("full_name") or sp.get("name") or sleeper_id
        key = _norm_name(full_name)

        # Try exact key, then first-word fallback for name mismatches
        proj = self.projections.get(key) or {}
        if not proj:
            last = _norm_name(sp.get("last_name") or "")
            proj = next(
                (v for k, v in self.projections.items() if last and last in k),
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
            "matched_fpl": bool(proj),
        }

    # ------------------------------------------------------------------
    # Board data
    # ------------------------------------------------------------------

    def get_available(self, position: Optional[str] = None) -> list[dict]:
        """Undrafted players ranked by projected_pts, optionally filtered by position."""
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
        """Return my 1-indexed draft slot, or None if not found."""
        if self.my_roster_id is None:
            return None
        for slot_str, rid in self.draft_info.get("slot_to_roster_id", {}).items():
            if rid == self.my_roster_id:
                return int(slot_str)
        return None

    def get_my_next_picks(self) -> list[int]:
        """Return upcoming overall pick numbers that belong to my roster."""
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
        """
        2D grid [round_idx][slot_idx] where each column = consistent draft slot.
        Respects snake ordering — Sleeper's draft_slot field stays fixed across rounds.
        """
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
        Compare value rank vs ADP rank for undrafted players.
        adp_lookup: {norm_name: adp_rank}. If None, returns available list by value.
        Positive value_diff = undervalued (ADP rank > value rank).
        """
        available = self.get_available()
        if adp_lookup is None:
            return [{**p, "value_rank": i + 1, "adp": None, "value_diff": None}
                    for i, p in enumerate(available)]

        result = []
        for i, player in enumerate(available):
            value_rank = i + 1
            adp = adp_lookup.get(_norm_name(player["name"]), value_rank)
            result.append({**player, "value_rank": value_rank, "adp": adp, "value_diff": adp - value_rank})
        return sorted(result, key=lambda x: x["value_diff"], reverse=True)
