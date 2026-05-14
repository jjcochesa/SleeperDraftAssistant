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

    # ── Roster identity ───────────────────────────────────────────────
    st.markdown("**Your team**")

    username_input = st.text_input(
        "Sleeper username",
        placeholder="enter username to auto-detect",
        key="username_field",
    )

    detected_roster_id: int | None = None
    detected_display_name: str | None = None

    if username_input:
        cache_key = f"user_lookup_{username_input}"
        if cache_key not in st.session_state:
            with st.spinner("Looking up user…"):
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

        result = st.session_state[cache_key]
        if result["roster_id"]:
            detected_roster_id = result["roster_id"]
            detected_display_name = result["display_name"]
            st.success(f"✓ **{detected_display_name}** → Roster #{detected_roster_id}")
        else:
            st.warning("Username not found in this league.")

    roster_label = "Roster ID" + (" (override)" if detected_roster_id else " (1–10)")
    my_roster_id: int = st.number_input(
        roster_label,
        min_value=1, max_value=10,
        value=detected_roster_id or 1,
        step=1,
    )

    st.divider()

    # ── Refresh controls ──────────────────────────────────────────────
    auto_refresh = st.toggle("Auto-refresh (10 s)", value=True)
    if st.button("🔄 Refresh now", use_container_width=True):
        st.session_state.pop(f"picks_ts_{selected_draft_id}", None)

    st.divider()

    # ── Data source status (populated after DraftState loads) ─────────
    ds_status_placeholder = st.empty()


# ---------------------------------------------------------------------------
# Session-cached DraftState
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Loading players & projections…")
def _load_draft_state(draft_id: str, league_id: str) -> DraftState:
    """Heavy one-time load cached globally per draft_id."""
    ds = DraftState(league_id, draft_id)
    ds.load_static(understat_year=2025)
    return ds


ds: DraftState = _load_draft_state(selected_draft_id, LEAGUE_ID)
ds.my_roster_id = my_roster_id

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

# Fill sidebar data-source status now that ds is loaded
with ds_status_placeholder.container():
    fpl_icon = "✅" if ds.fpl_loaded else "❌"
    us_icon = "✅" if ds.understat_loaded else ("⚠️" if ds.understat_error else "⏳")
    adp_icon = "✅" if ds.adp_data else "—"
    st.caption(
        f"FPL {fpl_icon}  ·  Understat {us_icon}  ·  ADP {adp_icon}"
    )
    if ds.understat_error:
        with st.expander("Understat error"):
            st.code(ds.understat_error, language=None)


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
    nxt_str = ", ".join(str(p) for p in my_next[:6])
    suffix = "…" if len(my_next) > 6 else ""
    gap = my_next[0] - ds.current_pick
    timing = "**now**" if gap == 0 else f"in {gap} pick{'s' if gap != 1 else ''}"
    st.info(f"Your next picks: **{nxt_str}{suffix}** — pick #{my_next[0]} is up {timing}")

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
                row[header] = ("★ " if is_mine else "") + f"{pick['web_name']} ({pick['position']})"
            else:
                row[header] = ""
        rows.append(row)

    df_board = pd.DataFrame(rows).set_index("Rd")

    def _style_cell(val: str) -> str:
        if val.startswith("★"):
            return "background-color:#1a472a;color:#a8e6a3;font-weight:bold"
        if val:
            return "color:#e0e0e0"
        return "color:#444"

    st.dataframe(
        df_board.style.applymap(_style_cell),
        use_container_width=True,
        height=min(38 * r + 42, 680),
    )
    st.caption("★ = your picks · each column = consistent draft slot")


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
        df_avail = pd.DataFrame(available)

        show_xga = ds.understat_loaded and df_avail["xG90"].notna().any()

        base_cols = ["name", "position", "team", "projected_pts", "total_points",
                     "minutes", "goals", "assists", "form", "ep_next", "selected_pct", "cost"]
        base_labels = ["Name", "Pos", "Club", "Proj Pts", "Season Pts",
                       "Mins", "Goals", "Assists", "Form", "EP Next", "Sel %", "Cost £m"]

        if show_xga:
            base_cols += ["xG", "xA", "xG90", "xA90", "npxG"]
            base_labels += ["xG", "xA", "xG90", "xA90", "npxG"]

        df_show = df_avail[base_cols].copy()
        df_show.columns = base_labels
        df_show.index = range(1, len(df_show) + 1)

        fmt = {"Proj Pts": "{:.1f}", "Form": "{:.1f}", "EP Next": "{:.2f}",
               "Sel %": "{:.1f}", "Cost £m": "{:.1f}"}
        if show_xga:
            fmt |= {"xG": "{:.2f}", "xA": "{:.2f}", "xG90": "{:.3f}",
                    "xA90": "{:.3f}", "npxG": "{:.2f}"}

        st.dataframe(
            df_show.style.background_gradient(subset=["Proj Pts"], cmap="YlGn").format(fmt),
            use_container_width=True,
            height=min(36 * top_n + 42, 700),
        )


# ── My Team ────────────────────────────────────────────────────────────────
with tab_mine:
    st.subheader("My Drafted Squad")

    my_picks = ds.get_my_picks()
    needs = ds.get_positional_needs()
    remaining = ds.num_rounds - len(my_picks)

    pos_cols = st.columns(len(POSITION_ORDER))
    for col, pos in zip(pos_cols, POSITION_ORDER):
        col.metric(pos, needs[pos])

    st.divider()

    if not my_picks:
        st.info("No picks recorded yet for your roster.")
    else:
        df_mine = pd.DataFrame(my_picks)
        show_xga_mine = ds.understat_loaded and df_mine["xG90"].notna().any()

        mine_cols = ["name", "position", "team", "projected_pts",
                     "goals", "assists", "clean_sheets", "form"]
        mine_labels = ["Name", "Pos", "Club", "Proj Pts", "Goals", "Assists", "CS", "Form"]

        if show_xga_mine:
            mine_cols += ["xG", "xA", "xG90", "xA90"]
            mine_labels += ["xG", "xA", "xG90", "xA90"]

        df_show_mine = df_mine[mine_cols].copy()
        df_show_mine.columns = mine_labels
        df_show_mine = df_show_mine.sort_values(["Pos", "Proj Pts"], ascending=[True, False])
        df_show_mine.index = range(1, len(df_show_mine) + 1)

        fmt_mine = {"Proj Pts": "{:.1f}", "Form": "{:.1f}"}
        if show_xga_mine:
            fmt_mine |= {"xG": "{:.2f}", "xA": "{:.2f}", "xG90": "{:.3f}", "xA90": "{:.3f}"}

        st.dataframe(
            df_show_mine.style.format(fmt_mine),
            use_container_width=True,
        )

    if remaining > 0:
        st.divider()
        st.subheader(f"Top 3 per position  ({remaining} picks left)")
        exp_cols = st.columns(len(POSITION_ORDER))
        for col, pos in zip(exp_cols, POSITION_ORDER):
            top3 = ds.get_available(pos)[:3]
            col.markdown(f"**{pos}**")
            for p in top3:
                xga_note = (
                    f" xG90={p['xG90']:.2f}" if p.get("xG90") is not None else ""
                )
                col.markdown(f"- {p['web_name']} *(proj {p['projected_pts']:.0f}{xga_note})*")
    elif my_picks:
        st.success("Squad complete — draft finished!")


# ── ADP / Value ────────────────────────────────────────────────────────────
with tab_adp:
    st.subheader("ADP vs Projected Value")

    # ── Load ADP from a previous draft ────────────────────────────────
    other_drafts = [d for d in drafts if d["draft_id"] != selected_draft_id]
    if other_drafts:
        st.markdown("**Load ADP from a previous draft**")
        adp_draft_id = st.selectbox(
            "Historical draft",
            options=[d["draft_id"] for d in other_drafts],
            format_func=lambda x: draft_labels.get(x, x),
            key="adp_draft_select",
        )
        if st.button("Load as ADP baseline", key="load_adp_btn"):
            with st.spinner("Fetching historical picks…"):
                ds.load_adp_from_draft(adp_draft_id)
            st.success(f"Loaded {len(ds.adp_data)} players as ADP baseline.")
        if ds.adp_data:
            st.caption(f"ADP loaded: {len(ds.adp_data)} players from historical draft.")

    # ── Manual ADP paste (fallback / override) ─────────────────────────
    with st.expander("Paste ADP manually (overrides loaded data)", expanded=not bool(ds.adp_data)):
        st.markdown("One per line: `Player Name, ADP rank` — e.g. `Salah, 1`")
        adp_text = st.text_area("ADP data", height=140, placeholder="Haaland, 1\nSalah, 2\n…")

    manual_adp: dict | None = None
    if adp_text.strip():
        manual_adp = {}
        for line in adp_text.strip().splitlines():
            parts = line.rsplit(",", 1)
            if len(parts) == 2:
                try:
                    manual_adp[_norm_name(parts[0].strip())] = int(parts[1].strip())
                except ValueError:
                    pass

    st.caption(
        "**Value Diff** = ADP rank − projected rank. "
        "Positive = undervalued (falling in the draft relative to projected output)."
    )
    st.divider()

    analysis = ds.get_adp_analysis(manual_adp)[:60]

    if analysis:
        has_adp = any(row["adp"] is not None for row in analysis)

        base_cols = ["name", "position", "team", "projected_pts", "selected_pct", "value_rank"]
        base_labels = ["Name", "Pos", "Club", "Proj Pts", "Sel %", "Value Rank"]

        if has_adp:
            base_cols += ["adp", "value_diff"]
            base_labels += ["ADP", "Value Diff"]

        if ds.understat_loaded:
            base_cols += ["xG90", "xA90"]
            base_labels += ["xG90", "xA90"]

        df_adp = pd.DataFrame(analysis)[base_cols].copy()
        df_adp.columns = base_labels
        df_adp.index = range(1, len(df_adp) + 1)

        fmt_adp = {"Proj Pts": "{:.1f}", "Sel %": "{:.1f}"}
        if ds.understat_loaded:
            fmt_adp |= {"xG90": "{:.3f}", "xA90": "{:.3f}"}

        style = df_adp.style.format(fmt_adp, na_rep="—").background_gradient(
            subset=["Proj Pts"], cmap="YlOrRd"
        )
        if has_adp:
            style = style.background_gradient(
                subset=["Value Diff"], cmap="RdYlGn", vmin=-10, vmax=10
            )

        st.dataframe(style, use_container_width=True, height=600)

        if not has_adp:
            st.info(
                "No ADP loaded — showing by projected value only. "
                "Load a historical draft above or paste ADP manually."
            )
