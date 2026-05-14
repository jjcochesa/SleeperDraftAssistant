"""
Sleeper Draft Assistant — Streamlit UI.
Snake draft helper for a 10-team, 17-round EPL Sleeper league.
"""

import time

import pandas as pd
import streamlit as st

from draft_engine import (
    DraftState,
    POSITION_ORDER,
    get_league_drafts,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_ID = "1115505765961293824"
POLL_INTERVAL_S = 10

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
    st.caption("EPL · Snake · 10 teams · 17 rounds")
    st.divider()

    # Load draft list once
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

    draft_labels = {
        d["draft_id"]: f"{d.get('season', 'Draft')} — {d.get('status', '?').replace('_', ' ').title()}"
        for d in drafts
    }
    selected_draft_id: str = st.selectbox(
        "Draft",
        options=list(draft_labels),
        format_func=lambda x: draft_labels[x],
    )

    st.divider()
    my_roster_id: int = st.number_input(
        "My Roster ID (1–10)", min_value=1, max_value=10, value=1, step=1
    )

    st.divider()
    auto_refresh = st.toggle("Auto-refresh (10 s)", value=True)
    if st.button("🔄 Refresh now", use_container_width=True):
        st.session_state.pop(f"ds_{selected_draft_id}_picks_ts", None)

    st.divider()
    st.caption(f"League `{LEAGUE_ID}`")


# ---------------------------------------------------------------------------
# Session-cached DraftState
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Loading players & projections…")
def _load_draft_state(draft_id: str, league_id: str) -> DraftState:
    """Heavy one-time load: Sleeper players + FPL bootstrap. Cached globally."""
    ds = DraftState(league_id, draft_id)
    ds.load_static()
    return ds


ds: DraftState = _load_draft_state(selected_draft_id, LEAGUE_ID)
ds.my_roster_id = my_roster_id  # update without invalidating cache

# Polling: refresh picks on every rerun if interval has passed
now = time.time()
last_refresh = st.session_state.get(f"ds_{selected_draft_id}_picks_ts", 0)
if now - last_refresh >= POLL_INTERVAL_S:
    ds.refresh()
    st.session_state[f"ds_{selected_draft_id}_picks_ts"] = now

# Auto-refresh: schedule the next rerun after remaining wait
if auto_refresh:
    elapsed = time.time() - last_refresh
    wait_ms = max(0, int((POLL_INTERVAL_S - elapsed) * 1000))
    # streamlit-autorefresh if installed, else JavaScript meta-refresh fallback
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        st_autorefresh(interval=POLL_INTERVAL_S * 1000, key="auto_refresh")
    except ImportError:
        st.markdown(
            f'<meta http-equiv="refresh" content="{POLL_INTERVAL_S}">',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

status_icon = {"pre_draft": "🟡", "drafting": "🟢", "complete": "🔵"}.get(ds.status, "⚪")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Status", f"{status_icon} {ds.status.replace('_', ' ').title()}")
c2.metric("Current Pick", f"{ds.current_pick} / {ds.total_picks}")
c3.metric("Drafted", len(ds.drafted_ids))
c4.metric("Available", len(ds.players) - len(ds.drafted_ids))
my_slot = ds.get_my_draft_slot()
c5.metric("My Slot", my_slot if my_slot else "—")

my_next = ds.get_my_next_picks()
if my_next:
    next_str = ", ".join(str(p) for p in my_next[:6])
    suffix = "…" if len(my_next) > 6 else ""
    st.info(f"Your next picks: **{next_str}{suffix}**   (pick #{my_next[0]} is up {'now' if my_next[0] == ds.current_pick else f'in {my_next[0] - ds.current_pick} picks'})")

st.divider()


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
                marker = "★ " if is_mine else ""
                row[header] = f"{marker}{pick['web_name']} ({pick['position']})"
            else:
                row[header] = ""
        rows.append(row)

    df_board = pd.DataFrame(rows).set_index("Rd")

    def _board_style(val: str) -> str:
        if val.startswith("★"):
            return "background-color: #1a472a; color: #a8e6a3; font-weight: bold"
        if val:
            return "color: #e0e0e0"
        return "color: #444"

    st.dataframe(
        df_board.style.applymap(_board_style),
        use_container_width=True,
        height=min(38 * r + 42, 680),
    )
    st.caption("★ = your picks. Each column = consistent draft slot.")


# ── Best Available ─────────────────────────────────────────────────────────
with tab_avail:
    st.subheader("Best Available Players")

    col_pos, col_n = st.columns([3, 1])
    with col_pos:
        pos_filter = st.radio("Position", ["All"] + POSITION_ORDER, horizontal=True)
    with col_n:
        top_n = st.selectbox("Show", [25, 50, 100], index=0)

    pos_arg = None if pos_filter == "All" else pos_filter
    available = ds.get_available(pos_arg)[:top_n]

    if not available:
        st.info("No players available for this filter.")
    else:
        df_avail = pd.DataFrame(available)[[
            "name", "position", "team", "projected_pts", "total_points",
            "minutes", "goals", "assists", "form", "ep_next", "selected_pct", "cost",
        ]].copy()
        df_avail.columns = [
            "Name", "Pos", "Club", "Proj Pts", "Season Pts",
            "Mins", "Goals", "Assists", "Form", "EP Next", "Sel %", "Cost £m",
        ]
        df_avail.index = range(1, len(df_avail) + 1)

        st.dataframe(
            df_avail.style
                .background_gradient(subset=["Proj Pts"], cmap="YlGn")
                .format({"Proj Pts": "{:.1f}", "Form": "{:.1f}", "EP Next": "{:.2f}",
                         "Sel %": "{:.1f}", "Cost £m": "{:.1f}"}),
            use_container_width=True,
            height=min(36 * top_n + 42, 700),
        )


# ── My Team ────────────────────────────────────────────────────────────────
with tab_mine:
    st.subheader("My Drafted Squad")

    my_picks = ds.get_my_picks()
    needs = ds.get_positional_needs()
    remaining = ds.num_rounds - len(my_picks)

    # Positional summary chips
    pos_cols = st.columns(len(POSITION_ORDER))
    for col, pos in zip(pos_cols, POSITION_ORDER):
        col.metric(pos, needs[pos])

    st.divider()

    if not my_picks:
        st.info("No picks recorded yet for your roster.")
    else:
        df_mine = pd.DataFrame(my_picks)[[
            "name", "position", "team", "projected_pts",
            "goals", "assists", "clean_sheets", "form",
        ]].copy()
        df_mine.columns = ["Name", "Pos", "Club", "Proj Pts", "Goals", "Assists", "CS", "Form"]
        df_mine = df_mine.sort_values(["Pos", "Proj Pts"], ascending=[True, False])
        df_mine.index = range(1, len(df_mine) + 1)
        st.dataframe(
            df_mine.style.format({"Proj Pts": "{:.1f}", "Form": "{:.1f}"}),
            use_container_width=True,
        )

    if remaining > 0:
        st.divider()
        st.subheader(f"Top 3 available per position  ({remaining} picks left)")
        exp_cols = st.columns(len(POSITION_ORDER))
        for col, pos in zip(exp_cols, POSITION_ORDER):
            top3 = ds.get_available(pos)[:3]
            col.markdown(f"**{pos}**")
            for p in top3:
                col.markdown(
                    f"- {p['web_name']} *(proj {p['projected_pts']:.0f})*"
                )
    elif my_picks:
        st.success("Squad complete — draft finished!")


# ── ADP / Value ────────────────────────────────────────────────────────────
with tab_adp:
    st.subheader("ADP vs Projected Value")
    st.caption(
        "Rankings are by projected season points. "
        "ADP data can be pasted below to surface under/overdrafted players."
    )

    with st.expander("Paste ADP data (optional)", expanded=False):
        st.markdown(
            "Format: one player per line — `Player Name, ADP rank`  \n"
            "e.g. `Erling Haaland, 1`"
        )
        adp_text = st.text_area("ADP data", height=150, placeholder="Haaland, 1\nSalah, 2\n…")

    adp_lookup: dict | None = None
    if adp_text.strip():
        adp_lookup = {}
        from draft_engine import _norm_name
        for line in adp_text.strip().splitlines():
            parts = line.rsplit(",", 1)
            if len(parts) == 2:
                try:
                    adp_lookup[_norm_name(parts[0].strip())] = int(parts[1].strip())
                except ValueError:
                    pass

    analysis = ds.get_adp_analysis(adp_lookup)[:60]
    if analysis:
        cols_base = ["name", "position", "team", "projected_pts", "selected_pct", "value_rank"]
        col_labels = ["Name", "Pos", "Club", "Proj Pts", "Sel %", "Value Rank"]

        if adp_lookup:
            cols_base += ["adp", "value_diff"]
            col_labels += ["ADP", "Value Diff"]

        df_adp = pd.DataFrame(analysis)[cols_base].copy()
        df_adp.columns = col_labels
        df_adp.index = range(1, len(df_adp) + 1)

        fmt = {"Proj Pts": "{:.1f}", "Sel %": "{:.1f}"}
        style = df_adp.style.format(fmt).background_gradient(subset=["Proj Pts"], cmap="YlOrRd")
        if adp_lookup and "Value Diff" in df_adp.columns:
            style = style.background_gradient(subset=["Value Diff"], cmap="RdYlGn")

        st.dataframe(style, use_container_width=True, height=600)

        if not adp_lookup:
            st.info("Paste ADP data above to highlight under/overdrafted players.")
