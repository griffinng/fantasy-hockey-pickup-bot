"""
Fantasy Hockey Dashboard — Streamlit app entry point.
Run with: streamlit run app.py
"""

import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Fantasy Hockey Dashboard",
    page_icon="🏒",
    layout="wide",
)

import data.cache as cache
import data.yahoo_client as yahoo
import data.nhl_client as nhl
import features.waiver_analyzer as waiver_mod
import features.drop_analyzer as drop_mod
import features.schedule_optimizer as sched_mod
import config

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🏒 FH Dashboard")
    st.caption(f"League: {config.YAHOO_LEAGUE_ID}")

    if st.button("Refresh all data", use_container_width=True):
        cache.bust()
        st.cache_data.clear()
        st.success("Cache cleared — reload the page.")

    st.divider()
    st.subheader("Scoring weights")
    w7 = st.slider("Last 7 days", 0.0, 1.0, config.FPPG_WEIGHT_7D, 0.05)
    w14 = st.slider("Last 14 days", 0.0, 1.0, config.FPPG_WEIGHT_14D, 0.05)
    w30 = st.slider("Last 30 days", 0.0, 1.0, config.FPPG_WEIGHT_30D, 0.05)
    total = w7 + w14 + w30
    if abs(total - 1.0) > 0.01:
        st.warning(f"Weights sum to {total:.2f} — adjust to 1.0")
    else:
        config.FPPG_WEIGHT_7D = w7
        config.FPPG_WEIGHT_14D = w14
        config.FPPG_WEIGHT_30D = w30

    st.divider()
    st.subheader("Filters")
    pos_filter = st.selectbox("Position", ["ALL", "C", "LW", "RW", "D", "G"])
    min_games_wk = st.slider("Min games this week (waivers)", 1, 4, 2)

    st.divider()
    with st.expander("League scoring values"):
        st.markdown(
            "**Skaters**\n"
            f"- G: {config.SKATER_POINTS['goals']} pts\n"
            f"- A: {config.SKATER_POINTS['assists']} pts\n"
            f"- +/-: {config.SKATER_POINTS['plus_minus']} pts\n"
            f"- PPP: {config.SKATER_POINTS['ppp']} pts\n"
            f"- SOG: {config.SKATER_POINTS['sog']} pts\n"
            f"- BLK: {config.SKATER_POINTS['blocks']} pts\n\n"
            "**Goalies**\n"
            f"- W: {config.GOALIE_POINTS['wins']} pts\n"
            f"- GA: {config.GOALIE_POINTS['goals_against']} pts\n"
            f"- SV: {config.GOALIE_POINTS['saves']} pts\n"
            f"- SHO: {config.GOALIE_POINTS['shutouts']} pts"
        )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=config.CACHE_TTL_ROSTER, show_spinner="Loading your roster...")
def load_roster():
    return yahoo.get_my_roster()


@st.cache_data(ttl=config.CACHE_TTL_ROSTER, show_spinner="Loading waiver wire...")
def load_waivers():
    return yahoo.get_waiver_players(150)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _colour_recommendation(val: str) -> str:
    if val == "Drop":
        return "background-color: #ffcccc"
    if val == "Monitor":
        return "background-color: #fff3cc"
    return "background-color: #ccffcc"


def _colour_games(val):
    if not isinstance(val, (int, float)):
        return ""
    if val >= 4:
        return "background-color: #1a7a1a; color: white"
    if val >= 3:
        return "background-color: #5cb85c; color: white"
    if val == 2:
        return "background-color: #f0ad4e"
    return "background-color: #d9534f; color: white"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_overview, tab_waivers, tab_drops, tab_schedule = st.tabs([
    "Weekly Overview",
    "Waiver Wire",
    "Drop Candidates",
    "Points Max",
])


# ── Tab 1: Weekly Overview ──────────────────────────────────────────────────

with tab_overview:
    st.header("Weekly Overview")

    try:
        roster = load_roster()
        waivers = load_waivers()
    except Exception as e:
        st.error(f"Could not load Yahoo data: {e}")
        st.info(
            "Set `YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`, and `YAHOO_LEAGUE_ID` "
            "in your `.env` file, then run:\n\n```\npython -m auth.yahoo_oauth\n```"
        )
        st.stop()

    my_games = sum(
        nhl.current_week_games(p["team_abbrev"]) for p in roster if p.get("team_abbrev")
    )
    col1, col2, col3 = st.columns(3)
    col1.metric("Roster games this week", my_games)
    col2.metric("Rostered players", len(roster))

    injured = [p for p in roster if p.get("status") in {"IR", "IR-LTI", "DTD", "Out"}]
    if injured:
        col3.metric("Injury flags", len(injured), delta=f"{len(injured)} flagged", delta_color="inverse")
        st.warning("Injured / questionable: " + ", ".join(p["name"] for p in injured))

    st.divider()
    st.subheader("Quick Recommendations")

    try:
        waiver_df = waiver_mod.rank_waivers(waivers, "ALL")
        if not waiver_df.empty:
            top = waiver_df.iloc[0]
            st.success(
                f"**Pick up:** {top['name']} ({top.get('team_abbrev', '')} · "
                f"{top['games_this_week']} games this week) — {top['reason']}"
            )

        drop_df = drop_mod.build_drop_df(roster)
        if not drop_df.empty and "recommendation" in drop_df.columns:
            top_drop = drop_df[drop_df["recommendation"] == "Drop"]
            if not top_drop.empty:
                d = top_drop.iloc[0]
                st.error(
                    f"**Drop:** {d['name']} ({d.get('team_abbrev', '')} · "
                    f"{d['games_this_week']} games this week, "
                    f"{d['fppg_7d']} FP/gm last 7d)"
                )

        stream_df = sched_mod.streaming_windows(waivers, min_games=3)
        if not stream_df.empty:
            s = stream_df.iloc[0]
            st.info(
                f"**Stream add:** {s['Player']} ({s['Team']}) — "
                f"{s['Peak Games/Wk']} games/week in: {s['Best Weeks']}"
            )
    except Exception as e:
        st.warning(f"Could not generate recommendations: {e}")

    st.divider()
    st.subheader("Your Roster — This Week")
    roster_rows = []
    for p in roster:
        games = nhl.current_week_games(p.get("team_abbrev", "")) if p.get("team_abbrev") else "?"
        roster_rows.append({
            "Player": p["name"],
            "Pos": "/".join(p.get("positions", [])),
            "Team": p.get("team_abbrev", ""),
            "Games This Week": games,
            "Status": p.get("status", "") or "OK",
        })
    st.dataframe(pd.DataFrame(roster_rows), use_container_width=True, hide_index=True)


# ── Tab 2: Waiver Wire ──────────────────────────────────────────────────────

with tab_waivers:
    st.header("Waiver Wire Analyzer")
    st.caption(
        "Ranked by weighted FP/game (last 7/14/30 days) × games-this-week multiplier × trend bonus. "
        "Scoring: G=6, A=4, ±=2, PPP=2, SOG=0.9, BLK=1 | W=5, GA=-3, SV=0.6, SHO=5"
    )

    try:
        waivers = load_waivers()
        with st.spinner("Scoring waiver players..."):
            waiver_df = waiver_mod.rank_waivers(waivers, pos_filter)

        if waiver_df.empty:
            st.info("No available players found for the selected position filter.")
        else:
            filtered = waiver_df[waiver_df["games_this_week"] >= min_games_wk].copy()

            # Skaters vs goalies have different stat columns — build display df
            skater_cols = ["name", "positions", "team_abbrev", "games_this_week",
                           "fppg_7d", "fppg_14d", "fppg_30d", "score",
                           "G_14d", "A_14d", "PPP_14d", "SOG_14d", "BLK_14d", "PM_14d", "reason"]
            goalie_cols = ["name", "positions", "team_abbrev", "games_this_week",
                           "fppg_7d", "fppg_14d", "fppg_30d", "score",
                           "W_14d", "GA_14d", "SV_14d", "SHO_14d", "reason"]

            if pos_filter == "G":
                show_cols = [c for c in goalie_cols if c in filtered.columns]
            else:
                show_cols = [c for c in skater_cols if c in filtered.columns]

            display = filtered[show_cols].rename(columns={
                "name": "Player", "positions": "Pos", "team_abbrev": "Team",
                "games_this_week": "Games/Wk",
                "fppg_7d": "FP/gm 7d", "fppg_14d": "FP/gm 14d", "fppg_30d": "FP/gm 30d",
                "score": "Score", "reason": "Why",
                "G_14d": "G", "A_14d": "A", "PPP_14d": "PPP",
                "SOG_14d": "SOG", "BLK_14d": "BLK", "PM_14d": "+/-",
                "W_14d": "W", "GA_14d": "GA", "SV_14d": "SV", "SHO_14d": "SHO",
            })

            st.dataframe(
                display,
                use_container_width=True,
                column_config={
                    "Score": st.column_config.ProgressColumn(
                        "Score", min_value=0, max_value=float(display["Score"].max() or 1)
                    ),
                    "Games/Wk": st.column_config.NumberColumn("Games/Wk", format="%d"),
                },
            )
            st.caption(f"Showing {len(filtered)} of {len(waiver_df)} available players")

            if st.button("Export top 10 as text"):
                lines = [
                    f"{i+1}. {r['name']} ({r.get('team_abbrev','')}) — {r['reason']}"
                    for i, (_, r) in enumerate(filtered.head(10).iterrows())
                ]
                st.code("\n".join(lines))

    except Exception as e:
        st.error(f"Error loading waiver data: {e}")


# ── Tab 3: Drop Candidates ──────────────────────────────────────────────────

with tab_drops:
    st.header("Drop Candidates")
    st.caption(
        "Drop score 0–1: higher = stronger case to drop. "
        "Red = drop now, yellow = monitor, green = keep."
    )

    try:
        roster = load_roster()
        waivers = load_waivers()

        with st.spinner("Analyzing your roster..."):
            drop_df = drop_mod.build_drop_df(roster)
            waiver_ranked = waiver_mod.rank_waivers(waivers, "ALL")

        if drop_df.empty:
            st.info("Could not load roster data.")
        else:
            display_cols = [
                "name", "positions", "team_abbrev", "status",
                "fppg_7d", "fppg_30d", "games_this_week",
                "drop_score", "recommendation",
            ]
            display_cols = [c for c in display_cols if c in drop_df.columns]
            drop_display = drop_df[display_cols].rename(columns={
                "name": "Player", "positions": "Pos", "team_abbrev": "Team",
                "fppg_7d": "FP/gm 7d", "fppg_30d": "FP/gm 30d",
                "games_this_week": "Games/Wk", "drop_score": "Drop Score",
                "recommendation": "Action",
            })

            styled_drop = drop_display.style.applymap(
                _colour_recommendation, subset=["Action"]
            )
            st.dataframe(styled_drop, use_container_width=True, hide_index=True)

            # Side-by-side comparison
            candidates = drop_df[drop_df["recommendation"] == "Drop"]
            if not candidates.empty:
                st.divider()
                st.subheader("Drop vs Best Available Replacement")
                for _, row in candidates.iterrows():
                    positions = row.get("positions", [])
                    pos = positions[0] if isinstance(positions, list) and positions else "C"
                    repl = drop_mod.best_replacement(pos, waiver_ranked)

                    col_a, col_sep, col_b = st.columns([5, 1, 5])
                    with col_a:
                        st.markdown(f"**DROP: {row['name']}**")
                        st.markdown(
                            f"Team: {row.get('team_abbrev', '—')}  \n"
                            f"FP/gm 7d: {row['fppg_7d']}  \n"
                            f"FP/gm 30d: {row['fppg_30d']}  \n"
                            f"Games this week: {row['games_this_week']}  \n"
                            f"Status: {row.get('status', '') or 'OK'}"
                        )
                    with col_sep:
                        st.markdown(
                            "<div style='text-align:center;font-size:2rem;margin-top:2rem'>→</div>",
                            unsafe_allow_html=True,
                        )
                    with col_b:
                        if repl:
                            st.markdown(f"**ADD: {repl.get('name', '?')}**")
                            st.markdown(
                                f"Team: {repl.get('team_abbrev', '—')}  \n"
                                f"FP/gm 7d: {repl.get('fppg_7d', '—')}  \n"
                                f"FP/gm 30d: {repl.get('fppg_30d', '—')}  \n"
                                f"Games this week: {repl.get('games_this_week', '—')}  \n"
                                f"Why: {repl.get('reason', '—')}"
                            )
                        else:
                            st.markdown("_No suitable replacement found on waivers_")
                    st.divider()

    except Exception as e:
        st.error(f"Error analyzing roster: {e}")


# ── Tab 4: Points Max / Schedule ────────────────────────────────────────────

with tab_schedule:
    st.header("Points Max — Schedule Optimizer")
    st.caption(
        "Game counts per week. "
        "Dark green = 4 games, light green = 3, orange = 2, red = 1. "
        "Pick up players before their high-game week; drop after."
    )

    view = st.radio("View", ["My Roster", "Streaming Windows (Waivers)"], horizontal=True)

    try:
        roster = load_roster()
        waivers = load_waivers()

        if view == "My Roster":
            with st.spinner("Building schedule matrix..."):
                matrix_df = sched_mod.roster_schedule_matrix(roster)
            week_cols = [c for c in matrix_df.columns if c not in ("Player", "Pos", "Team")]
            styled = matrix_df.style.applymap(_colour_games, subset=week_cols)
            st.dataframe(styled, use_container_width=True, hide_index=True)

        else:
            min_g = st.slider("Min games in peak week", 2, 4, 3)
            with st.spinner("Finding streaming windows..."):
                stream_df = sched_mod.streaming_windows(waivers, min_games=min_g)

            if stream_df.empty:
                st.info("No streaming opportunities found with the current threshold.")
            else:
                week_cols = [
                    c for c in stream_df.columns
                    if c not in ("Player", "Pos", "Team", "Peak Games/Wk", "Best Weeks", "Ownership %")
                ]
                styled = stream_df.style.applymap(_colour_games, subset=week_cols)
                st.dataframe(styled, use_container_width=True, hide_index=True)
                st.caption(
                    f"{len(stream_df)} streaming candidates. "
                    f"Max 4 adds/week — prioritize highest FP/gm players."
                )

    except Exception as e:
        st.error(f"Error building schedule matrix: {e}")
