"""
Sleeper Draft Assistant — Streamlit UI.
Snake draft helper for a 10-team, 17-round EPL Sleeper league.
All stats and points use Sleeper's own scoring system.
"""

import time

import pandas as pd
import streamlit as st

from draft_engine import (
    DraftState,
    _norm_name,
    get_league_drafts,
    get_sleeper_user,
    find_roster_id,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_ID = "1115505765961293824"
POLL_INTERVAL_S = 10
SLEEPER_SEASON = "2025"        # 2025/26 EPL season
UNDERSTAT_YEAR = 2025

st.set_page_config(
    page_title="Sleeper Draft Assistant",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚽ Draft Assistant")
    st.caption("EPL · Sleeper · Snake · 10 teams · 17 rounds")
    st.divider()

    # ── Draft ──────────────────────────────────────────────────────────
    if "drafts" not in st.session_state:
        with st.spinner("Loading drafts…"):
            try:
                st.session_state.drafts = get_league_drafts(LEAGUE_ID)
            except Exception as exc:
                st.error(f"Could not fetch drafts: {exc}")
                st.session_state.drafts = []

    drafts: list = st.session_state.drafts
    if not drafts:
        st.warning("No drafts found for this league.")
        st.stop()

    draft_labels: dict[str, str] = {
        d["draft_id"]: f"{d.get('season', 'Draft')} — {d.get('status', '?').replace('_', ' ').title()}"
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
                        "roster_id": rid,
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
        placeholder="Haaland\nSalah\nTrent Alexander-Arnold\n…",
        height=220,
        label_visibility="collapsed",
    )

    st.divider()

    # ── Refresh ────────────────────────────────────────────────────────
    auto_refresh = st.toggle("Auto-refresh (10 s)", value=True)
    if st.button("🔄 Refresh now", use_container_width=True):
        st.session_state.pop(f"picks_ts_{selected_draft_id}", None)

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
# DraftState
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Loading Sleeper data…")
def _load_draft_state(draft_id: str, league_id: str) -> DraftState:
    ds = DraftState(league_id, draft_id)
    ds.load_static(season=SLEEPER_SEASON, understat_year=UNDERSTAT_YEAR)
    return ds


ds: DraftState = _load_draft_state(selected_draft_id, LEAGUE_ID)
ds.my_roster_id = my_roster_id

# Convenience alias so tabs can reference ds.position_order
POS_ORDER = ds.position_order

# Polling
now = time.time()
last_ts = st.session_state.get(f"picks_ts_{selected_draft_id}", 0)
if now - last_ts >= POLL_INTERVAL_S:
    ds.refresh()
    st.session_state[f"picks_ts_{selected_draft_id}"] = now

if auto_refresh:
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        st_autorefresh(interval=POLL_INTERVAL_S * 1000, key="auto_refresh")
    except ImportError:
        st.markdown(
            f'<meta http-equiv="refresh" content="{POLL_INTERVAL_S}">',
            unsafe_allow_html=True,
        )

# Sidebar status
with ds_status_slot.container():
    stats_icon = "✅" if ds.stats_loaded else ("⚠️" if ds.stats_error else "—")
    us_icon    = "✅" if ds.understat_loaded else ("⚠️" if ds.understat_error else "—")
    dp_icon    = f"✅ {len(dp_lookup)}" if dp_lookup else "—"
    st.caption(f"Stats {stats_icon}  ·  xG/xA {us_icon}  ·  DP {dp_icon}")
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

my_next = ds.get_my_next_picks()
if my_next:
    nxt_str = ", ".join(str(p) for p in my_next[:6])
    suffix  = "…" if len(my_next) > 6 else ""
    gap     = my_next[0] - ds.current_pick
    timing  = "**now**" if gap == 0 else f"in {gap} pick{'s' if gap != 1 else ''}"
    st.info(f"Your next picks: **{nxt_str}{suffix}** — pick #{my_next[0]} is up {timing}")

st.divider()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_board_df(players: list[dict], include_dp: bool = True) -> pd.DataFrame:
    """
    Core display frame: Total Pts, PPG, DP Rec.
    Sorted by DP Rec (when loaded) then PPG.
    """
    rows = []
    for p in players:
        norm = _norm_name(p["name"])
        dp_rec = dp_lookup.get(norm)
        rows.append({
            "Name":      p["name"],
            "Pos":       p["position"],
            "Club":      p["team"],
            "Total Pts": p["total_pts"],
            "PPG":       p["ppg"],
            "GW":        p["games"],
            "DP Rec":    dp_rec,
            # extras for detail toggle
            "_goals":    p["goals"],
            "_assists":  p["assists"],
            "_sot":      p["shots_on_target"],
            "_kp":       p["key_passes"],
            "_cs":       p["clean_sheets"],
            "_saves":    p["saves"],
            "_tkl":      p["tackles_won"],
            "_int":      p["interceptions"],
            "_yc":       p["yellow_cards"],
            "_rc":       p["red_cards"],
            "_xG90":     p.get("xG90"),
            "_xA90":     p.get("xA90"),
            "_xG":       p.get("xG"),
            "_xA":       p.get("xA"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if dp_lookup and include_dp:
        ranked   = df[df["DP Rec"].notna()].sort_values("DP Rec")
        unranked = df[df["DP Rec"].isna()].sort_values("PPG", ascending=False)
        df = pd.concat([ranked, unranked], ignore_index=True)
    else:
        df = df.sort_values("PPG", ascending=False)

    df.index = range(1, len(df) + 1)
    return df


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_board, tab_avail, tab_mine, tab_adp = st.tabs(
    ["📋 Draft Board", "⭐ Best Available", "👤 My Team", "📊 ADP / Value"]
)


# ── Draft Board ────────────────────────────────────────────────────────────
with tab_board:
    st.subheader("Live Draft Board")

    grid = ds.get_pick_grid()
    n, r = ds.num_teams, ds.num_rounds

    rows = []
    for rnd_i, round_cols in enumerate(grid):
        row: dict = {"Rd": rnd_i + 1}
        for slot_i, pick in enumerate(round_cols):
            header = f"S{slot_i + 1}"
            if pick:
                is_mine = pick.get("roster_id") == my_roster_id
                row[header] = ("★ " if is_mine else "") + f"{pick['web_name']} ({pick['position']})"
            else:
                row[header] = ""
        rows.append(row)

    df_board = pd.DataFrame(rows).set_index("Rd")

    def _style_cell(val: str) -> str:
        if val.startswith("★"):
            return "background-color:#1a472a;color:#a8e6a3;font-weight:bold"
        return "color:#e0e0e0" if val else "color:#444"

    st.dataframe(
        df_board.style.applymap(_style_cell),
        use_container_width=True,
        height=min(38 * r + 42, 680),
    )
    st.caption("★ = your picks · each column = consistent draft slot")


# ── Best Available ─────────────────────────────────────────────────────────
with tab_avail:
    st.subheader("Best Available")

    col_pos, col_n, col_detail = st.columns([3, 1, 1])
    with col_pos:
        pos_filter = st.radio("Position", ["All"] + POS_ORDER, horizontal=True)
    with col_n:
        top_n = st.selectbox("Show", [25, 50, 100], index=0)
    with col_detail:
        show_detail = st.toggle("Detail cols", value=False)

    pos_arg = None if pos_filter == "All" else pos_filter
    available = ds.get_available(pos_arg)[:top_n]

    if not available:
        st.info("No players available for this filter.")
    else:
        df = _build_board_df(available)

        show_cols = ["Name", "Pos", "Club", "Total Pts", "PPG", "GW", "DP Rec"]

        if show_detail:
            detail_map = {
                "Goals": "_goals", "Assists": "_assists",
                "SoT": "_sot", "KP": "_kp",
                "CS": "_cs", "Saves": "_saves",
                "Tkl": "_tkl", "Int": "_int",
                "YC": "_yc", "RC": "_rc",
            }
            if ds.understat_loaded:
                detail_map |= {"xG": "_xG", "xA": "_xA", "xG90": "_xG90", "xA90": "_xA90"}
            for label, col in detail_map.items():
                df[label] = df[col]
                show_cols.append(label)

        df_show = df[show_cols].copy()

        fmt = {"Total Pts": "{:.1f}", "PPG": "{:.2f}"}
        if show_detail and ds.understat_loaded:
            fmt |= {"xG": "{:.2f}", "xA": "{:.2f}", "xG90": "{:.3f}", "xA90": "{:.3f}"}

        style = df_show.style.format(fmt, na_rep="—").background_gradient(
            subset=["PPG"], cmap="YlGn"
        )
        if dp_lookup and df_show["DP Rec"].notna().any():
            style = style.background_gradient(subset=["DP Rec"], cmap="YlOrRd_r")

        st.dataframe(style, use_container_width=True, height=min(36 * top_n + 42, 700))

        if not dp_lookup:
            st.caption("Paste your DP rankings in the sidebar to sort by recommendation.")
        if not ds.stats_loaded:
            st.warning(
                "Sleeper season stats did not load — Total Pts and PPG will show 0. "
                "Check the Stats error in the sidebar for details."
            )


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
        df_mine = _build_board_df(my_picks, include_dp=False)
        show_mine = ["Name", "Pos", "Club", "Total Pts", "PPG", "GW"]
        df_show_mine = df_mine[show_mine].sort_values(["Pos", "PPG"], ascending=[True, False])
        df_show_mine.index = range(1, len(df_show_mine) + 1)
        st.dataframe(
            df_show_mine.style.format({"Total Pts": "{:.1f}", "PPG": "{:.2f}"}, na_rep="—"),
            use_container_width=True,
        )

    if remaining > 0:
        st.divider()
        st.subheader(f"Top 3 per position  ({remaining} picks left)")
        exp_cols = st.columns(len(POS_ORDER))
        for col, pos in zip(exp_cols, POS_ORDER):
            top3 = ds.get_available(pos)[:3]
            col.markdown(f"**{pos}**")
            for p in top3:
                norm    = _norm_name(p["name"])
                dp_tag  = f" · DP#{dp_lookup[norm]}" if norm in dp_lookup else ""
                xg_tag  = f" · xG90={p['xG90']:.2f}" if p.get("xG90") else ""
                col.markdown(f"- {p['web_name']} *({p['ppg']:.1f} ppg{dp_tag}{xg_tag})*")
    elif my_picks:
        st.success("Squad complete — draft finished!")


# ── ADP / Value ────────────────────────────────────────────────────────────
with tab_adp:
    st.subheader("ADP / Value")

    # Manual ADP paste
    with st.expander("Paste ADP data", expanded=not bool(dp_lookup)):
        st.markdown("One per line: `Player Name, ADP pick number`")
        adp_text = st.text_area(
            "ADP", height=140,
            placeholder="Haaland, 1\nSalah, 2\n…",
            label_visibility="collapsed",
        )

    manual_adp: dict[str, int] = {}
    if adp_text.strip():
        for line in adp_text.strip().splitlines():
            parts = line.rsplit(",", 1)
            if len(parts) == 2:
                try:
                    manual_adp[_norm_name(parts[0].strip())] = int(parts[1].strip())
                except ValueError:
                    pass

    st.caption(
        "**PPG Rank** = order by 25/26 Sleeper PPG. "
        "**ADP** = pasted pick number. "
        "**ADP−PPG** positive = community sleeping on this player."
    )
    st.divider()

    all_avail = ds.get_available()
    rows_adp = []
    for i, p in enumerate(all_avail, 1):
        norm    = _norm_name(p["name"])
        dp_rec  = dp_lookup.get(norm)
        adp     = manual_adp.get(norm)
        diff    = (adp - i) if adp is not None else None
        rows_adp.append({
            "Name":      p["name"],
            "Pos":       p["position"],
            "Club":      p["team"],
            "Total Pts": p["total_pts"],
            "PPG":       p["ppg"],
            "PPG Rank":  i,
            "ADP":       adp,
            "ADP−PPG":   diff,
            "DP Rec":    dp_rec,
        })

    if ds.understat_loaded:
        for row, p in zip(rows_adp, all_avail):
            row["xG90"] = p.get("xG90")
            row["xA90"] = p.get("xA90")

    df_adp = pd.DataFrame(rows_adp)
    df_adp.index = range(1, len(df_adp) + 1)

    fmt_adp = {"PPG": "{:.2f}", "Total Pts": "{:.1f}"}
    if ds.understat_loaded:
        fmt_adp |= {"xG90": "{:.3f}", "xA90": "{:.3f}"}

    style_adp = df_adp.style.format(fmt_adp, na_rep="—").background_gradient(
        subset=["PPG"], cmap="YlOrRd"
    )
    if df_adp["ADP−PPG"].notna().any():
        style_adp = style_adp.background_gradient(
            subset=["ADP−PPG"], cmap="RdYlGn", vmin=-20, vmax=20
        )

    st.dataframe(style_adp, use_container_width=True, height=650)

    if not manual_adp:
        st.info("Paste ADP data above to populate the ADP and ADP−PPG columns.")
