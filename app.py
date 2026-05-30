"""
Sleeper Draft Assistant — Streamlit UI.
Snake draft helper for a 10-team, 17-round EPL Sleeper league.

Caching architecture:
  @st.cache_data(ttl=3600)  — heavy player DB (Sleeper stats, FPL cost, Understat)
  @st.cache_resource        — DraftState (stateful, holds live picks + draft meta)
  @st.fragment(run_every=5) — live draft board (polls picks without full-page rerun)
"""

import pandas as pd
import streamlit as st

from draft_engine import (
    DraftState,
    _fetch_player_db,
    _norm_name,
    _sleeper_season_year,
    get_league_drafts,
    get_sleeper_user,
    find_roster_id,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_ID = "1115505765961293824"

st.set_page_config(
    page_title="Sleeper Draft Assistant",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Heavy player DB — cached for 1 hour, shared across sessions
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner="Loading player database…")
def _load_player_db(season: str, understat_year: int) -> dict:
    return _fetch_player_db(season, understat_year)


# ---------------------------------------------------------------------------
# DraftState — cached per draft, holds live picks
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Loading draft…")
def _get_draft_state(draft_id: str, league_id: str) -> DraftState:
    ds = DraftState(league_id, draft_id)
    ds.load_draft_meta()
    return ds


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚽ Draft Assistant")
    st.caption("EPL · Sleeper · Snake · 10 teams · 17 rounds")
    st.divider()

    # ── Draft selection ────────────────────────────────────────────────
    if "drafts" not in st.session_state:
        with st.spinner("Loading drafts…"):
            try:
                st.session_state.drafts = get_league_drafts(LEAGUE_ID)
            except Exception as exc:
                st.error(f"Could not fetch drafts: {exc}")
                st.session_state.drafts = []

    drafts: list = st.session_state.drafts
    if not drafts:
        st.info("No drafts found yet — pre-draft research mode.")
        selected_draft_id = "pre_draft"
    else:
        draft_labels: dict[str, str] = {
            d["draft_id"]: (
                f"{d.get('season', 'Draft')} — "
                f"{d.get('status', '?').replace('_', ' ').title()}"
            )
            for d in drafts
        }
        selected_draft_id: str = st.selectbox(
            "Draft",
            options=list(draft_labels),
            format_func=lambda x: draft_labels[x],
        )

    st.divider()

    # ── Your team ──────────────────────────────────────────────────────
    st.markdown("**Your team**")
    username_input = st.text_input(
        "Sleeper username",
        placeholder="auto-detects your roster",
        key="username_field",
    )

    detected_roster_id: int | None = None
    if username_input:
        cache_key = f"user_lookup_{username_input}"
        if cache_key not in st.session_state:
            with st.spinner("Looking up…"):
                try:
                    user_data = get_sleeper_user(username_input)
                    uid = user_data.get("user_id")
                    rid = find_roster_id(LEAGUE_ID, uid) if uid else None
                    st.session_state[cache_key] = {
                        "roster_id":    rid,
                        "display_name": user_data.get("display_name", username_input),
                    }
                except Exception:
                    st.session_state[cache_key] = {"roster_id": None, "display_name": None}

        lu = st.session_state[cache_key]
        if lu["roster_id"]:
            detected_roster_id = lu["roster_id"]
            st.success(f"✓ **{lu['display_name']}** → Roster #{detected_roster_id}")
        else:
            st.warning("Username not found in this league.")

    my_roster_id: int = st.number_input(
        "Roster ID" + (" (override)" if detected_roster_id else " (1–10)"),
        min_value=1, max_value=10,
        value=detected_roster_id or 1,
        step=1,
    )

    st.divider()

    # ── DP Rankings ────────────────────────────────────────────────────
    st.markdown("**DP Recommended rankings**")
    st.caption("One player per line, in your preferred draft order.")
    dp_text: str = st.text_area(
        "DP rankings",
        key="dp_rankings_text",
        placeholder="Haaland\nSalah\nRice\n…",
        height=220,
        label_visibility="collapsed",
    )

    auto_col, clear_col = st.columns(2)
    with auto_col:
        if st.button("🤖 Auto-rank", use_container_width=True,
                     help="Generate DP rankings from the projection model"):
            st.session_state["_trigger_auto_dp"] = True
            st.rerun()
    with clear_col:
        if st.button("🗑 Clear", use_container_width=True):
            st.session_state["dp_rankings_text"] = ""
            st.rerun()

    st.divider()

    # ── Refresh control ────────────────────────────────────────────────
    if st.button("🔄 Reload player DB", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    ds_status_slot = st.empty()


# ---------------------------------------------------------------------------
# Parse DP rankings  {norm_name → rank}
# ---------------------------------------------------------------------------

dp_lookup: dict[str, int] = {}
if dp_text.strip():
    for i, line in enumerate(dp_text.strip().splitlines(), 1):
        name = line.strip()
        if name:
            dp_lookup[_norm_name(name)] = i


# ---------------------------------------------------------------------------
# Load data and inject into DraftState
# ---------------------------------------------------------------------------

season    = str(_sleeper_season_year())
player_db = _load_player_db(season, int(season))

ds: DraftState = _get_draft_state(selected_draft_id, LEAGUE_ID)
ds.my_roster_id = my_roster_id
ds.inject_player_db(player_db)   # cheap — stable dict reference from cache

POS_ORDER = ds.position_order

# ---------------------------------------------------------------------------
# Auto-DP generation (triggered by sidebar button, runs after DB is loaded)
# ---------------------------------------------------------------------------

def _auto_dp_score(p: dict) -> float:
    """
    Scoring for auto-generated DP rankings.
    projected_pts is the primary signal — it already captures Sleeper's full
    scoring model including defensive volume for MIDs.
    Small xG90/xA90 bonus for FWD/MID surfaces attackers whose raw pts may
    understate quality (e.g. a striker with high xG but low team chances).
    Players with no stats fall to bottom, sorted by FPL cost proxy.
    """
    proj = p.get("projected_pts") or 0.0
    if proj > 0:
        pos = p.get("position", "")
        xg90 = p.get("xG90") or 0.0
        xa90 = p.get("xA90") or 0.0
        if pos in ("FWD", "MID"):
            # xG90 → rough Sleeper pts equivalent: goal ≈ 9 pts so xG90 * 9 * 0.5
            proj += xg90 * 4.5 + xa90 * 2.5
        return proj
    # No stats: use FPL cost as a rough proxy (cost already in £m)
    return (p.get("cost") or 0.0) - 100   # negative so below all projected players

if st.session_state.pop("_trigger_auto_dp", False):
    all_players = list(ds.player_data.values())
    # Exclude GKs from FWD/MID xG bonus (already handled by pos check)
    ranked = sorted(all_players, key=_auto_dp_score, reverse=True)
    # Top 120 covers 17 rounds × ~7 relevant positions — enough for full draft
    names  = [p["name"] for p in ranked[:120] if p.get("name")]
    st.session_state["dp_rankings_text"] = "\n".join(names)
    st.rerun()

# Push to session_state so the live-board fragment can access without re-creation
st.session_state["ds"]           = ds
st.session_state["my_roster_id"] = my_roster_id
st.session_state["dp_lookup"]    = dp_lookup

# Sidebar status
with ds_status_slot.container():
    stats_icon = "✅" if ds.stats_loaded     else ("⚠️" if ds.stats_error     else "—")
    fpl_icon   = "✅" if ds.fpl_loaded       else "⚠️"
    us_icon    = "✅" if ds.understat_loaded else ("⚠️" if ds.understat_error else "—")
    dp_icon    = f"✅ {len(dp_lookup)}"      if dp_lookup                      else "—"
    st.caption(f"Stats {stats_icon}  ·  FPL {fpl_icon}  ·  xG/xA {us_icon}  ·  DP {dp_icon}")
    if ds.stats_error:
        with st.expander("Stats error"):
            st.code(ds.stats_error, language=None)
    if ds.understat_error:
        with st.expander("Understat error"):
            st.code(ds.understat_error, language=None)


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

status_icon = {"pre_draft": "🟡", "drafting": "🟢", "complete": "🔵"}.get(ds.status, "⚪")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Status",       f"{status_icon} {ds.status.replace('_', ' ').title()}")
c2.metric("Current Pick", f"{ds.current_pick} / {ds.total_picks}")
c3.metric("Drafted",      len(ds.drafted_ids))
c4.metric("Available",    len(ds.players) - len(ds.drafted_ids))
my_slot = ds.get_my_draft_slot()
c5.metric("My Slot",      my_slot if my_slot else "—")

st.divider()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _style_cell(val: str) -> str:
    if isinstance(val, str) and val.startswith("★"):
        return "background-color:#1a472a;color:#a8e6a3;font-weight:bold"
    return "color:#e0e0e0" if val else "color:#444"


def _build_rankings_df(
    players:   list[dict],
    sort_col:  str = "projected_pts",
    incl_dp:   bool = True,
) -> pd.DataFrame:
    """Rankings DataFrame: 25/26 totals, ppg, 26/27 projection, ADP, DP Rec."""
    rows = []
    for p in players:
        norm   = _norm_name(p["name"])
        dp_rec = dp_lookup.get(norm)
        rows.append({
            "Name":      p["name"],
            "Pos":       p["position"],
            "Club":      p["team"],
            "25/26 Pts": p["total_pts"],
            "PPG":       p["ppg"],
            "GW":        p["games"],
            "26/27 Proj":p["projected_pts"],
            "Draft Pos":  p.get("adp_rank"),
            "DP Rec":    dp_rec,
            # hidden detail cols
            "_goals":    p["goals"],
            "_assists":  p["assists"],
            "_sot":      p["shots_on_target"],
            "_kp":       p["key_passes"],
            "_drb":      p["dribbles"],
            "_acnc":     p["accurate_crosses"],
            "_aer":      p["aerials_won"],
            "_saves":    p["saves"],
            "_tkl":      p["tackles_won"],
            "_int":      p["interceptions"],
            "_blk":      p["blocked_shots"],
            "_yc":       p["yellow_cards"],
            "_rc":       p["red_cards"],
            "_own":      p.get("ownership_pct"),
            "_xG90":     p.get("xG90"),
            "_xA90":     p.get("xA90"),
            "_xG":       p.get("xG"),
            "_xA":       p.get("xA"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Sort: DP Rec first if available and requested, then by chosen column
    if dp_lookup and incl_dp and "DP Rec" in df.columns:
        ranked   = df[df["DP Rec"].notna()].sort_values("DP Rec")
        unranked = df[df["DP Rec"].isna()].sort_values(sort_col, ascending=False)
        df = pd.concat([ranked, unranked], ignore_index=True)
    else:
        df = df.sort_values(sort_col, ascending=False)

    df.index = range(1, len(df) + 1)
    return df


# ---------------------------------------------------------------------------
# Live draft board fragment — reruns every 5 s, rest of page stays static
# ---------------------------------------------------------------------------

def _build_snake_df(ds: DraftState) -> pd.DataFrame:
    """Build the 170-row linear snake pick list."""
    n = ds.num_teams
    r = ds.num_rounds
    slot_to_roster = ds.draft_info.get("slot_to_roster_id", {})

    # overall_pick → pick record
    pick_map: dict[int, dict] = {}
    for pick in ds.picks:
        rnd  = pick.get("round", 1)
        slot = pick.get("draft_slot", 1)
        if rnd % 2 == 1:
            overall = (rnd - 1) * n + slot
        else:
            overall = (rnd - 1) * n + (n + 1 - slot)
        pick_map[overall] = pick

    rows = []
    for overall in range(1, n * r + 1):
        rnd        = (overall - 1) // n + 1
        pos_in_rnd = (overall - 1) % n + 1
        slot       = pos_in_rnd if rnd % 2 == 1 else (n + 1 - pos_in_rnd)

        roster_id = slot_to_roster.get(str(slot))
        team_name = ds.users.get(roster_id, f"Slot {slot}") if roster_id else f"Slot {slot}"
        is_my_slot = (roster_id == ds.my_roster_id) if roster_id else False

        pick = pick_map.get(overall)
        if pick:
            p      = ds._enrich(pick["player_id"])
            is_mine = pick.get("roster_id") == ds.my_roster_id
            rows.append({
                "#":     overall,
                "Rd":    rnd,
                "Slot":  slot,
                "Team":  ("★ " if is_mine else "") + team_name,
                "Player": ("★ " if is_mine else "") + p["web_name"],
                "Pos":   p["position"],
                "Proj":  p["projected_pts"],
            })
        elif overall == ds.current_pick:
            rows.append({
                "#":     overall,
                "Rd":    rnd,
                "Slot":  slot,
                "Team":  ("★ " if is_my_slot else "") + team_name,
                "Player": "⏳ ON THE CLOCK",
                "Pos":   "",
                "Proj":  None,
            })
        else:
            rows.append({
                "#":     overall,
                "Rd":    rnd,
                "Slot":  slot,
                "Team":  ("★ " if is_my_slot else "") + team_name,
                "Player": "—",
                "Pos":   "",
                "Proj":  None,
            })

    return pd.DataFrame(rows)


@st.fragment(run_every=5)
def _draft_fragment() -> None:
    _ds  = st.session_state.get("ds")
    _rid = st.session_state.get("my_roster_id", 1)
    _dp  = st.session_state.get("dp_lookup", {})
    if _ds is None:
        return

    changed = _ds.refresh()

    # "Your next picks" banner
    my_next = _ds.get_my_next_picks()
    if my_next:
        nxt_str = ", ".join(str(p) for p in my_next[:6])
        suffix  = "…" if len(my_next) > 6 else ""
        gap     = my_next[0] - _ds.current_pick
        timing  = "**now**" if gap == 0 else f"in {gap} pick{'s' if gap != 1 else ''}"
        st.info(f"Your next picks: **{nxt_str}{suffix}** — pick #{my_next[0]} is up {timing}")

    col_snake, col_avail = st.columns([2, 1])

    with col_snake:
        st.markdown("**Snake order — all 170 picks**")
        df_snake = _build_snake_df(_ds)
        # Scroll to current pick area — highlight first undrafted row
        current_row = int(_ds.current_pick) - 1
        st.dataframe(
            df_snake[["#", "Rd", "Slot", "Team", "Player", "Pos", "Proj"]].style
                .apply(
                    lambda row: [
                        "background-color:#1a472a;color:#a8e6a3" if "★" in str(row.get("Player", "")) else
                        "background-color:#2a2a00;color:#ffff88" if str(row.get("Player", "")).startswith("⏳") else
                        ""
                    ] * len(row),
                    axis=1,
                )
                .format({"Proj": "{:.1f}"}, na_rep="—"),
            use_container_width=True,
            height=560,
        )
        st.caption("★ = your picks  ·  ⏳ = on the clock  ·  auto-refreshes every 5 s")

    with col_avail:
        st.markdown("**Available players**")
        pos_f = st.radio("Pos", ["All"] + list(_ds.position_order), horizontal=True,
                         key="_snake_pos_filter")
        pos_arg = None if pos_f == "All" else pos_f
        avail   = _ds.get_available(pos_arg, sort_by="projected_pts")[:40]

        if not avail:
            st.info("No players available.")
        else:
            rows_a = []
            for p in avail:
                norm   = _norm_name(p["name"])
                dp_rec = _dp.get(norm)
                rows_a.append({
                    "Player": p["web_name"],
                    "Pos":    p["position"],
                    "Proj":   p["projected_pts"],
                    "PPG":    p["ppg"],
                    "DP":     dp_rec,
                })
            df_a = pd.DataFrame(rows_a)
            df_a.index = range(1, len(df_a) + 1)
            st.dataframe(
                df_a.style.format(
                    {"Proj": "{:.1f}", "PPG": "{:.2f}"}, na_rep="—"
                ).background_gradient(subset=["Proj"], cmap="YlGn"),
                use_container_width=True,
                height=560,
            )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_ranks, tab_draft, tab_mine, tab_adp = st.tabs(
    ["📊 Rankings", "🐍 Live Draft", "👤 My Team", "📈 Draft Pos / Value"]
)


# ── Rankings ───────────────────────────────────────────────────────────────
with tab_ranks:
    st.subheader("Player Rankings")

    r_col1, r_col2, r_col3, r_col4 = st.columns([3, 2, 1, 1])
    with r_col1:
        pos_filter = st.radio("Position", ["All"] + POS_ORDER, horizontal=True,
                              key="ranks_pos")
    with r_col2:
        sort_mode = st.radio(
            "Sort by",
            ["26/27 Projected", "25/26 Total Pts", "GW Avg (PPG)"],
            horizontal=True,
            key="ranks_sort",
        )
    with r_col3:
        top_n = st.selectbox("Show", [25, 50, 100, 200], index=0, key="ranks_n")
    with r_col4:
        show_detail = st.toggle("Detail cols", value=False, key="ranks_detail")

    sort_col_map = {
        "26/27 Projected": "26/27 Proj",
        "25/26 Total Pts": "25/26 Pts",
        "GW Avg (PPG)":    "PPG",
    }
    sort_col = sort_col_map[sort_mode]

    pos_arg   = None if pos_filter == "All" else pos_filter
    available = ds.get_available(pos_arg, sort_by={
        "26/27 Projected": "projected_pts",
        "25/26 Total Pts": "total_pts",
        "GW Avg (PPG)":    "ppg",
    }[sort_mode])[:top_n]

    if not available:
        st.info("No players available for this filter.")
    else:
        df = _build_rankings_df(available, sort_col=sort_col)

        show_cols = ["Name", "Pos", "Club", "25/26 Pts", "PPG", "GW", "26/27 Proj", "Draft Pos", "DP Rec"]

        if show_detail:
            detail_map = {
                "Goals":   "_goals",   "Assists": "_assists",
                "SoT":     "_sot",     "KP":      "_kp",
                "Drb":     "_drb",     "AcX":     "_acnc",
                "Aer":     "_aer",     "Saves":   "_saves",
                "Tkl":     "_tkl",     "Int":     "_int",
                "Blk":     "_blk",
                "YC":      "_yc",      "RC":      "_rc",
                "FPL Own%":"_own",
            }
            if ds.understat_loaded:
                detail_map |= {"xG": "_xG", "xA": "_xA", "xG90": "_xG90", "xA90": "_xA90"}
            for label, col in detail_map.items():
                df[label] = df[col]
                show_cols.append(label)

        df_show = df[show_cols].copy()
        fmt = {"25/26 Pts": "{:.1f}", "PPG": "{:.2f}", "26/27 Proj": "{:.1f}"}
        if show_detail:
            fmt |= {"FPL Own%": "{:.1f}"}
            if ds.understat_loaded:
                fmt |= {"xG": "{:.2f}", "xA": "{:.2f}", "xG90": "{:.3f}", "xA90": "{:.3f}"}

        gradient_col = {
            "26/27 Projected": "26/27 Proj",
            "25/26 Total Pts": "25/26 Pts",
            "GW Avg (PPG)":    "PPG",
        }[sort_mode]

        style = df_show.style.format(fmt, na_rep="—").background_gradient(
            subset=[gradient_col], cmap="YlGn"
        )
        if dp_lookup and df_show["DP Rec"].notna().any():
            style = style.background_gradient(subset=["DP Rec"], cmap="YlOrRd_r")

        st.dataframe(
            style,
            column_config={"Name": st.column_config.TextColumn("Name", pinned=True)},
            use_container_width=True,
            height=min(36 * top_n + 42, 700),
        )

        if not dp_lookup:
            st.caption("Paste your DP rankings in the sidebar to sort by recommendation.")
        if not ds.stats_loaded:
            st.warning("Sleeper season stats not loaded — points and projections show 0.")

    st.caption(
        "**26/27 Proj** = Bayesian-blended PPG (individual + position prior) × 34 GWs  ·  "
        "min 10 GWs required  ·  **Draft Pos** = ranked by FPL 25/26 ownership % — "
        "proxy until Sleeper EPL community ADP is available in August"
    )


# ── Live Draft ─────────────────────────────────────────────────────────────
with tab_draft:
    st.subheader("Live Snake Draft")
    if selected_draft_id == "pre_draft":
        st.info(
            "No draft created yet — draft board will appear here once the league "
            "creates a draft. The available players panel below uses projected points."
        )
    _draft_fragment()


# ── My Team ────────────────────────────────────────────────────────────────
with tab_mine:
    st.subheader("My Drafted Squad")

    my_picks  = ds.get_my_picks()
    needs     = ds.get_positional_needs()
    remaining = ds.num_rounds - len(my_picks)

    pos_cols = st.columns(len(POS_ORDER))
    for col, pos in zip(pos_cols, POS_ORDER):
        col.metric(pos, needs.get(pos, 0))

    st.divider()

    if not my_picks:
        st.info("No picks recorded yet for your roster.")
    else:
        rows_m = []
        for p in my_picks:
            norm   = _norm_name(p["name"])
            dp_rec = dp_lookup.get(norm)
            rows_m.append({
                "Name":      p["name"],
                "Pos":       p["position"],
                "Club":      p["team"],
                "25/26 Pts": p["total_pts"],
                "PPG":       p["ppg"],
                "GW":        p["games"],
                "26/27 Proj":p["projected_pts"],
                "Draft Pos":  p.get("adp_rank"),
                "DP Rec":    dp_rec,
            })
        df_mine = pd.DataFrame(rows_m).sort_values(
            ["Pos", "26/27 Proj"], ascending=[True, False]
        )
        df_mine.index = range(1, len(df_mine) + 1)
        st.dataframe(
            df_mine.style.format(
                {"25/26 Pts": "{:.1f}", "PPG": "{:.2f}", "26/27 Proj": "{:.1f}"}, na_rep="—"
            ).background_gradient(subset=["26/27 Proj"], cmap="YlGn"),
            use_container_width=True,
        )

    if remaining > 0:
        st.divider()
        st.subheader(f"Top 3 per position  ({remaining} picks left)")
        exp_cols = st.columns(len(POS_ORDER))
        for col, pos in zip(exp_cols, POS_ORDER):
            top3 = ds.get_available(pos, sort_by="projected_pts")[:3]
            col.markdown(f"**{pos}**")
            for p in top3:
                norm    = _norm_name(p["name"])
                dp_tag  = f" DP#{dp_lookup[norm]}" if norm in dp_lookup else ""
                xg_tag  = f" xG90={p['xG90']:.2f}" if p.get("xG90") else ""
                col.markdown(
                    f"- {p['web_name']} *({p['projected_pts']:.0f} proj{dp_tag}{xg_tag})*"
                )
    elif my_picks:
        st.success("Squad complete — draft finished!")


# ── ADP / Value ────────────────────────────────────────────────────────────
with tab_adp:
    st.subheader("Draft Position / Value")
    st.caption(
        "**Draft Pos** = player's pick number in the 25/26 Sleeper draft (last season). "
        "Real 26/27 ADP won't exist until community drafts start in August. "
        "**ADP−Proj** positive = player drafted earlier than their 26/27 projection warrants. "
        "Defensive-volume MIDs (Rice, Garner, Stach) are under-drafted vs Sleeper value."
    )
    st.divider()

    all_avail = ds.get_available(sort_by="projected_pts")
    rows_adp  = []
    for i, p in enumerate(all_avail, 1):
        norm   = _norm_name(p["name"])
        dp_rec = dp_lookup.get(norm)
        adp    = p.get("adp_rank")
        diff   = (adp - i) if adp is not None else None
        row    = {
            "Name":       p["name"],
            "Pos":        p["position"],
            "Club":       p["team"],
            "25/26 Pts":  p["total_pts"],
            "PPG":        p["ppg"],
            "26/27 Proj": p["projected_pts"],
            "Proj Rank":  i,
            "Draft Pos":   adp,
            "ADP−Proj":    diff,
            "DP Rec":     dp_rec,
            "FPL Own%":   p.get("ownership_pct"),
        }
        if ds.understat_loaded:
            row["xG90"] = p.get("xG90")
            row["xA90"] = p.get("xA90")
        rows_adp.append(row)

    df_adp = pd.DataFrame(rows_adp)
    df_adp.index = range(1, len(df_adp) + 1)

    fmt_adp = {
        "PPG": "{:.2f}", "25/26 Pts": "{:.1f}",
        "26/27 Proj": "{:.1f}", "FPL Own%": "{:.1f}",
    }
    if ds.understat_loaded:
        fmt_adp |= {"xG90": "{:.3f}", "xA90": "{:.3f}"}

    style_adp = df_adp.style.format(fmt_adp, na_rep="—").background_gradient(
        subset=["26/27 Proj"], cmap="YlOrRd"
    )
    if df_adp["ADP−Proj"].notna().any():
        style_adp = style_adp.background_gradient(
            subset=["ADP−Proj"], cmap="RdYlGn", vmin=-30, vmax=30
        )

    st.dataframe(
        style_adp,
        column_config={"Name": st.column_config.TextColumn("Name", pinned=True)},
        use_container_width=True,
        height=650,
    )

    st.caption(
        "⚠️ Draft Pos is last season's Sleeper pick number — not forward-looking 26/27 ADP. "
        "**FPL Own%** = FPL 25/26 community ownership, used as a proxy until Sleeper EPL "
        "community drafts start in August. Use 26/27 Proj rank + DP Rec to build your actual draft order."
    )
