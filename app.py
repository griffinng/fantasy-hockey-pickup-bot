"""
Fantasy hockey pickup dashboard.

    streamlit run app.py

Four tabs:
  Pickups       - every available player ranked by projected points for the
                  rest of this fantasy week
  My Team       - your roster, sorted most-droppable first
  Moves         - add/drop pairs, capped at your 4 acquisitions per week
  Player detail - the full arithmetic behind any one projection

The design principle throughout: never show a number without being able to
show where it came from. A ranking you cannot interrogate is a ranking you
cannot correct.
"""

from __future__ import annotations

import datetime as _dt

import pandas as pd
import streamlit as st

import config
import data.cache as cache
import data.http as http
import data.my_team as my_team
import data.nhl_stats_rest as nhl_rest
import features.advisor as advisor
import features.matchups as matchups
import features.projection as projection

st.set_page_config(
    page_title="Fantasy Hockey Pickups",
    page_icon="🏒",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner="Building projections...")
def load_advice(_day: str) -> advisor.Advice:
    """`_day` is in the signature purely to key the cache per calendar day."""
    return advisor.build_advice(progress=False)


def refresh() -> None:
    cache.bust()
    st.cache_data.clear()


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def role_badge(proj: projection.PlayerProjection) -> str:
    if proj.role.role_change == "promoted":
        return "PROMOTED"
    if proj.role.role_change == "demoted":
        return "slipping"
    return ""


def projections_to_frame(projs: list[projection.PlayerProjection]) -> pd.DataFrame:
    rows = []
    for p in projs:
        rows.append(
            {
                "Player": p.name,
                "Tm": p.team,
                "Pos": (p.position or "").upper(),
                "Week Pts": round(p.week_points, 1),
                "GP": p.games_left,
                "Line": (
                    ("D" if advisor.position_group(p.position) == "D" else "L")
                    + str(p.role.line_rank)
                ) if p.role.line_rank else "-",
                "PP": f"PP{p.role.pp_unit}" if p.role.pp_unit else "-",
                "PP%": round(p.role.pp_share * 100),
                "ES TOI": nhl_rest.seconds_to_mmss(p.es_toi),
                "PP TOI": nhl_rest.seconds_to_mmss(p.pp_toi),
                "Pts/Gm": round(p.per_game_points, 1),
                "Conf": round(p.confidence * 100),
                "Flag": role_badge(p),
                "Schedule": p.schedule_label,
                "Why": p.why,
            }
        )
    return pd.DataFrame(rows)


COLUMN_CONFIG = {
    "Week Pts": st.column_config.ProgressColumn(
        "Week Pts",
        help="Projected fantasy points between now and Sunday",
        format="%.1f",
        min_value=0,
        max_value=40,
    ),
    "Conf": st.column_config.NumberColumn(
        "Conf", help="How much data stands behind this projection", format="%d%%"
    ),
    "PP%": st.column_config.NumberColumn(
        "PP%", help="Share of team power-play time over the last 14 days",
        format="%d%%",
    ),
    "GP": st.column_config.NumberColumn("GP", help="Games left this week"),
}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

today = _dt.date.today()
week_start, week_end = matchups.fantasy_week_bounds(today)

with st.sidebar:
    st.header("Setup")

    if st.button("Refresh all data", use_container_width=True):
        refresh()
        st.rerun()

    st.caption(
        f"Fantasy week **{week_start:%a %d %b}** to **{week_end:%a %d %b}**  \n"
        f"{(week_end - today).days + 1} day(s) left"
    )

    yahoo_live = my_team.yahoo_api_available()
    if yahoo_live:
        st.success("Yahoo API connected")
    else:
        st.warning(
            "Yahoo API unavailable (403). Roster and free agents are read from "
            "text files until your API access application is approved."
        )

    st.divider()
    st.subheader("Your roster")
    roster_text = st.text_area(
        "One player per line",
        value=my_team.ROSTER_PATH.read_text() if my_team.ROSTER_PATH.exists() else "",
        height=220,
        label_visibility="collapsed",
    )
    if st.button("Save roster", use_container_width=True):
        my_team.ROSTER_PATH.write_text(roster_text)
        st.cache_data.clear()
        st.rerun()

    st.subheader("Available players")
    st.caption("Paste your league's free agents. Leave blank to rank everyone.")
    fa_text = st.text_area(
        "Free agents",
        value=(
            my_team.FREE_AGENT_PATH.read_text()
            if my_team.FREE_AGENT_PATH.exists() else ""
        ),
        height=180,
        label_visibility="collapsed",
    )
    if st.button("Save free agents", use_container_width=True):
        my_team.FREE_AGENT_PATH.write_text(fa_text)
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Filters")
    pos_filter = st.multiselect(
        "Position", ["C", "LW", "RW", "D"], default=[]
    )
    min_games = st.slider("Minimum games left", 0, 4, 0)
    only_promoted = st.checkbox("Only role changes", value=False)
    hide_injured = st.checkbox("Hide injured / out", value=True)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

try:
    advice = load_advice(str(today))
except http.FetchError as exc:
    # A slow upstream API should not greet you with a stack trace.
    st.error(f"Could not load data: {exc}", icon="🔌")
    st.button("Try again", on_click=refresh)
    st.stop()
except Exception as exc:  # noqa: BLE001 - last resort, keep the page usable
    st.error(f"Unexpected error building projections: {exc}", icon="💥")
    with st.expander("Details"):
        st.exception(exc)
    st.button("Clear cache and retry", on_click=refresh)
    st.stop()

st.title("Fantasy Hockey Pickups")
st.caption(
    f"Projected points for the rest of the week, built from ice time, "
    f"power-play role, line assignment and opponent strength."
)

if advice.unresolved_roster:
    st.error(
        "Could not identify these roster entries: "
        + ", ".join(advice.unresolved_roster)
        + ". Add a team abbreviation, e.g. `Elias Pettersson, VAN, c`."
    )

if not advice.free_agents_known:
    st.info(
        "No free-agent list saved, so the Pickups tab ranks every skater not on "
        "your roster. Paste your league's available players in the sidebar to "
        "narrow it to genuine waiver options.",
        icon="ℹ️",
    )


def apply_filters(
    projs: list[projection.PlayerProjection],
) -> list[projection.PlayerProjection]:
    out = projs
    if pos_filter:
        wanted = {p.lower() for p in pos_filter}
        out = [p for p in out if (p.position or "").lower() in wanted]
    if min_games:
        out = [p for p in out if p.games_left >= min_games]
    if only_promoted:
        out = [p for p in out if p.role.role_change == "promoted"]
    if hide_injured:
        out = [p for p in out if p.role.is_playable]
    return out


tab_pickups, tab_team, tab_moves, tab_detail = st.tabs(
    ["Pickups", "My Team", "Moves", "Player detail"]
)


# ---------------------------------------------------------------------------
# Pickups
# ---------------------------------------------------------------------------

with tab_pickups:
    candidates = apply_filters(advice.candidates)

    promoted = [p for p in candidates if p.role.role_change == "promoted"]
    if promoted:
        st.subheader("Role changes worth acting on")
        st.caption(
            "These players were just given a bigger job. Their ice time has "
            "changed but their stat line has not caught up, which is exactly "
            "when they are cheapest to acquire."
        )
        cols = st.columns(min(4, len(promoted)))
        for col, p in zip(cols, promoted[:4]):
            with col:
                st.metric(
                    p.name,
                    f"{p.week_points:.1f} pts",
                    f"{p.role.role_label}, {p.games_left} games",
                )
                st.caption(p.role.role_note)
        st.divider()

    st.subheader(f"Ranked candidates ({len(candidates)})")
    if candidates:
        frame = projections_to_frame(candidates)
        st.dataframe(
            frame,
            use_container_width=True,
            hide_index=True,
            column_config=COLUMN_CONFIG,
            height=620,
        )
    else:
        st.write("No players match the current filters.")


# ---------------------------------------------------------------------------
# My team
# ---------------------------------------------------------------------------

with tab_team:
    if not advice.roster:
        st.warning(
            "No roster loaded. Paste your players into the sidebar to get drop "
            "advice."
        )
    else:
        st.subheader("Your roster, most droppable first")
        st.caption(
            "**Week Pts** is what he gives you before Sunday. **Keep/Gm** is "
            "what he is worth per game with the schedule stripped out -- that is "
            "the number that should drive a drop, because schedules reset every "
            "week but the player does not."
        )
        rows = []
        for entry in advice.roster:
            p = entry.projection
            rows.append(
                {
                    "Player": p.name,
                    "Tm": p.team,
                    "Pos": (p.position or "").upper(),
                    "Week Pts": round(p.week_points, 1),
                    "Keep/Gm": round(entry.keep_value, 1),
                    "Drop score": round(entry.drop_score, 1),
                    "GP": p.games_left,
                    "Role": p.role.role_label,
                    "Status": p.role.injury_status or (
                        "GTD" if p.role.game_time_decision else ""
                    ),
                    "Why": p.why,
                }
            )
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            height=520,
        )

        total = sum(e.projection.week_points for e in advice.roster)
        st.metric("Projected team points, rest of week", f"{total:.1f}")


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------

with tab_moves:
    st.subheader("Suggested add / drop pairs")
    st.caption(
        f"Your league allows {config.MAX_ACQUISITIONS_PER_WEEK} acquisitions "
        f"per week. These are ordered by how much they improve this week."
    )

    if not advice.roster:
        st.warning("Load your roster in the sidebar to get move suggestions.")
    elif not advice.moves:
        st.success("No move improves your week. Stand pat.")
    else:
        for i, move in enumerate(advice.moves, 1):
            with st.container(border=True):
                left, mid, right = st.columns([3, 3, 2])
                with left:
                    st.markdown(f"**ADD** {move.add.name}")
                    st.caption(
                        f"{move.add.team} · {(move.add.position or '').upper()} · "
                        f"{move.add.role.role_label} · {move.add.games_left} games"
                    )
                    st.caption(move.add.why)
                with mid:
                    st.markdown(f"**DROP** {move.drop.name}")
                    st.caption(
                        f"{move.drop.projection.team} · "
                        f"{(move.drop.projection.position or '').upper()} · "
                        f"{move.drop.projection.role.role_label} · "
                        f"{move.drop.projection.games_left} games"
                    )
                    st.caption(move.drop.projection.why)
                with right:
                    st.metric("This week", f"+{move.week_gain:.1f}")
                    st.caption(f"Keep value {-move.keep_cost:+.1f}/gm")
                if move.warning:
                    st.warning(move.warning, icon="⚠️")


# ---------------------------------------------------------------------------
# Player detail
# ---------------------------------------------------------------------------

with tab_detail:
    st.subheader("Audit a projection")
    st.caption(
        "Every number in this dashboard can be opened up. Pick a player to see "
        "each term that produced his score."
    )

    pool = {p.name: p for p in advice.candidates}
    pool.update({e.projection.name: e.projection for e in advice.roster})
    if pool:
        chosen = st.selectbox("Player", sorted(pool))
        if chosen:
            proj = pool[chosen]
            st.code(projection.explain_player(proj), language="text")

            if proj.games:
                st.bar_chart(
                    pd.DataFrame(
                        {
                            "Projected points": [g.points for g in proj.games],
                        },
                        index=[
                            f"{g.game.date:%a} {g.game.label}" for g in proj.games
                        ],
                    )
                )
    else:
        st.write("Nothing loaded yet.")


st.divider()
st.caption(
    "Stats from the NHL public API. Lines and power-play units from Daily "
    "Faceoff. Fantasy data provided by Yahoo Fantasy."
)
