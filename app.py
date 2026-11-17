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
import data.names as names
import data.nhl_stats_rest as nhl_rest
import features.advisor as advisor
import features.goalies as goalies
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

class _NullRole:
    """Stand-in so goalie projections, which carry no role record, render."""
    line_rank = None
    pp_unit = None
    pp_share = 0.0
    role_change = ""
    role_note = ""
    injury_status = ""
    game_time_decision = False
    is_playable = True


def _role(proj):
    return getattr(proj, "role", None) or _NullRole()


def role_badge(proj) -> str:
    change = _role(proj).role_change
    if change == "promoted":
        return "PROMOTED"
    if change == "demoted":
        return "slipping"
    return ""


def projections_to_frame(projs: list[projection.PlayerProjection]) -> pd.DataFrame:
    rows = []
    for p in projs:
        rows.append(
            {
                "Player": p.name,
                "Tm": p.team,
                "Pos": names.display_position(p.position),
                "Week Pts": round(p.week_points, 1),
                "GP": p.games_left,
                "Line": advisor.role_label(p),
                "PP": f"PP{_role(p).pp_unit}" if getattr(_role(p), "pp_unit", None) else "-",
                "PP%": round(getattr(_role(p), "pp_share", 0.0) * 100),
                "ES TOI": nhl_rest.seconds_to_mmss(p.es_toi),
                "PP TOI": nhl_rest.seconds_to_mmss(p.pp_toi),
                "Pts/Gm": round(p.per_game_points, 1),
                "L14 FP/Gm": getattr(p, "recent_fp_per_game", 0.0),
                "Form": getattr(p, "form_delta", 0.0),
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
    "GP": st.column_config.NumberColumn(
        "GP", help="Games his team has left in this fantasy week", width="small"
    ),
    "L14 FP/Gm": st.column_config.NumberColumn(
        "L14 FP/Gm",
        help="Fantasy points per game actually scored over the last 14 days. "
             "Shown for your judgement — the model weights it only ~26%, "
             "because backtesting says chasing recent points makes "
             "projections worse. Cold players beat their projections more "
             "often than hot ones do.",
        format="%.1f",
    ),
    "Form": st.column_config.NumberColumn(
        "Form",
        help="Last 14 days minus season pace. Negative = currently cold. "
             "A cold player whose ice time and PP role are unchanged is "
             "usually unlucky rather than broken.",
        format="%+.1f",
    ),
    "Why": st.column_config.TextColumn(
        "Why",
        help="Only what the other columns do not already say: a role that "
             "just changed, an unusually soft or hard schedule, or an "
             "availability risk. Full breakdown is in the Player detail tab.",
        width="medium",
    ),
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
        out = [p for p in out if _role(p).role_change == "promoted"]
    if hide_injured:
        out = [p for p in out if _role(p).is_playable]
    return out


tab_pickups, tab_team, tab_moves, tab_detail = st.tabs(
    ["Pickups", "My Team", "Moves", "Player detail"]
)


# ---------------------------------------------------------------------------
# Pickups
# ---------------------------------------------------------------------------

with tab_pickups:
    candidates = apply_filters(advice.candidates)

    promoted = [p for p in candidates if _role(p).role_change == "promoted"]
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
                    f"{advisor.role_label(p)}, {p.games_left} games",
                )
                st.caption(_role(p).role_note)
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
    if advice.goalie_minimum_at_risk:
        st.error(
            f"**Goalie appearance minimum at risk.** Your goalies project to "
            f"**{advice.expected_goalie_appearances:.1f}** appearances this "
            f"week against a minimum of "
            f"**{config.MIN_GOALIE_STARTS_PER_WEEK}**. Falling short voids "
            f"*all* your goaltending points for the week, not just the "
            f"difference.",
            icon="🥅",
        )
    elif any(getattr(e.projection, "is_goalie", False) for e in advice.roster):
        st.caption(
            f"Goalie appearances projected: "
            f"**{advice.expected_goalie_appearances:.1f}** "
            f"(minimum {config.MIN_GOALIE_STARTS_PER_WEEK}) ✓"
        )

    if advice.unscored_roster:
        st.warning(
            "**On your roster but not scored** — excluded from the drop "
            "ranking below, so judge these yourself:\n\n"
            + "\n".join(f"- {item}" for item in advice.unscored_roster),
            icon="⚠️",
        )

    if not advice.roster:
        st.warning(
            "No roster loaded. Paste your players into the sidebar to get drop "
            "advice."
        )
    else:
        st.subheader("Your roster, most droppable first")
        st.caption(
            "**Scored** is banked and unchangeable. **Proj +** is what is still "
            "to come — that is the only part an add or drop can affect, so it is "
            "the number to compare against a free agent. **Keep/Gm** strips the "
            "schedule out entirely: it is what he is worth on a neutral night, "
            "and it is what should stop you cutting a good player who happens to "
            "have a thin week."
        )
        rows = []
        for entry in advice.roster:
            p = entry.projection
            rows.append(
                {
                    "Player": p.name,
                    "Tm": p.team,
                    "Pos": names.display_position(p.position),
                    "GP played": p.games_played_this_week,
                    "GP left": p.games_left,
                    "Scored": round(p.points_so_far, 1),
                    "Proj +": round(p.week_points, 1),
                    "Week total": round(p.week_total, 1),
                    "Keep/Gm": round(entry.keep_value, 1),
                    "Drop score": round(entry.drop_score, 1),
                    "Role": advisor.role_label(p),
                    "Status": getattr(_role(p), "injury_status", "") or (
                        "GTD" if getattr(_role(p), "game_time_decision", False) else ""
                    ),
                    "Why": p.why,
                }
            )
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            height=520,
            column_config={
                "GP played": st.column_config.NumberColumn(
                    "GP played",
                    help="Games his team has already played in this fantasy week",
                    width="small",
                ),
                "GP left": st.column_config.NumberColumn(
                    "GP left",
                    help="Games still to come before Sunday. Finished games are "
                         "excluded even if they were earlier today.",
                    width="small",
                ),
                "Scored": st.column_config.NumberColumn(
                    "Scored",
                    help="Fantasy points already banked this week. Done and "
                         "dusted — nothing you do now changes it.",
                    format="%.1f",
                ),
                "Proj +": st.column_config.NumberColumn(
                    "Proj +",
                    help="Projected ADDITIONAL points from here to Sunday. This "
                         "is the number that should drive an add or a drop, "
                         "because it is the only part still in play.",
                    format="%.1f",
                ),
                "Week total": st.column_config.NumberColumn(
                    "Week total",
                    help="Scored plus projected — where the week should finish.",
                    format="%.1f",
                ),
            },
        )

        banked = sum(e.projection.points_so_far for e in advice.roster)
        ahead = sum(e.projection.week_points for e in advice.roster)
        c1, c2, c3 = st.columns(3)
        c1.metric("Scored so far this week", f"{banked:.1f}")
        c2.metric("Projected rest of week", f"{ahead:.1f}")
        c3.metric("Projected week total", f"{banked + ahead:.1f}")

    if advice.ir_stash:
        st.subheader("Stashed on IR")
        st.caption(
            "These take up no active roster spot, so they are not drop "
            "candidates — leave them there until they are healthy. Keep value "
            "is what they are worth per game once they return."
        )
        st.dataframe(
            pd.DataFrame([
                {
                    "Player": e.projection.name,
                    "Tm": e.projection.team,
                    "Pos": names.display_position(e.projection.position),
                    "Keep/Gm": round(e.keep_value, 1),
                    "Status": getattr(_role(e.projection), "injury_status", "") or "IR",
                }
                for e in advice.ir_stash
            ]),
            use_container_width=True,
            hide_index=True,
        )


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------

with tab_moves:
    st.subheader("Suggested add / drop pairs")
    st.caption(
        f"**{advice.moves_remaining} add(s) left this week** "
        f"({config.ACQUISITIONS_USED_THIS_WEEK} of "
        f"{config.MAX_ACQUISITIONS_PER_WEEK} used). Keep "
        f"`ACQUISITIONS_USED_THIS_WEEK` in `config.py` current, or this will "
        f"suggest more moves than your league allows."
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
                        f"{move.add.team} · {names.display_position(move.add.position)} · "
                        f"{advisor.role_label(move.add)} · {move.add.games_left} games"
                    )
                    st.caption(move.add.why)
                with mid:
                    st.markdown(f"**DROP** {move.drop.name}")
                    st.caption(
                        f"{move.drop.projection.team} · "
                        f"{names.display_position(move.drop.projection.position)} · "
                        f"{advisor.role_label(move.drop.projection)} · "
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
            if getattr(proj, "is_goalie", False):
                st.code(goalies.explain_goalie(proj), language="text")
            else:
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
