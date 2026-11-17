"""
Add/drop recommendations: who to pick up, and who to cut for them.

A pickup is only as good as the roster spot it costs you, so ranking free
agents in isolation answers half a question. This module does the subtraction.

**The trap this is built to avoid.** If you rank purely on projected points for
the rest of this week, the model will cheerfully tell you to drop a star who
has one game left for a replacement-level skater who has four. That is right
for seven days and badly wrong for the season: you cannot get the star back,
and next week the schedules invert. So every roster player carries two numbers:

    week_points    - what he gives you between now and Sunday
    keep_value     - what he is worth per game, schedule stripped out

Drop candidates are ranked on a blend, and any suggestion that cuts a player
with real keep_value gets flagged loudly rather than quietly recommended. The
bot proposes; you decide.

League rule respected here: a maximum of 4 acquisitions per week.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt

import config
import data.my_team as my_team
import data.names as names
import data.nhl_stats_rest as nhl_rest
import features.goalies as goalies
import features.projection as projection
from features.projection import PlayerProjection

# How much a drop decision weighs "what he does this week" against "what he is
# worth going forward". Pure weekly thinking churns away good players.
WEEK_WEIGHT = 0.45
KEEP_WEIGHT = 0.55

# A drop suggestion touching a player above this per-game keep value gets a
# warning. Roughly the level of a useful everyday fantasy contributor.
KEEP_VALUE_WARN = 5.0

# Position groups we are willing to swap within. Hockey rosters have C/LW/RW/D
# slots; swapping a defenceman for a winger usually will not fit your lineup.
POSITION_GROUP = {
    "c": "F", "lw": "F", "rw": "F", "l": "F", "r": "F", "f": "F",
    "d": "D", "ld": "D", "rd": "D",
    "g": "G",
}


def position_group(position: str) -> str:
    return POSITION_GROUP.get((position or "").lower(), "F")


def _role_change(proj) -> str:
    """Role-change flag, or empty for projections that have no role record."""
    role = getattr(proj, "role", None)
    return getattr(role, "role_change", "") if role is not None else ""


def role_label(proj) -> str:
    """
    Display role for either kind of projection. Goalies have no line or
    power-play unit, so they carry a depth-chart label instead.
    """
    if getattr(proj, "is_goalie", False):
        depth = getattr(proj, "depth", None)
        if depth == 1:
            return "G1"
        if depth:
            return f"G{depth}"
        return "G"
    role = getattr(proj, "role", None)
    return role.role_label if role is not None else "-"


@dataclasses.dataclass
class RosterEntry:
    """One of your players, with both the weekly and the keep view."""

    projection: PlayerProjection
    keep_value: float = 0.0        # schedule-independent, points per game
    drop_score: float = 0.0        # lower = more droppable
    on_ir: bool = False            # occupies an IR slot, not an active one

    @property
    def name(self) -> str:
        return self.projection.name

    @property
    def group(self) -> str:
        return position_group(self.projection.position)


@dataclasses.dataclass
class Move:
    """A proposed add/drop pair."""

    add: PlayerProjection
    drop: RosterEntry
    week_gain: float
    keep_cost: float
    warning: str = ""

    @property
    def is_risky(self) -> bool:
        return bool(self.warning)


def keep_value(proj) -> float:
    """
    Per-game value with the schedule removed -- what this player is worth on a
    neutral night. This is the number that should drive a drop, because the
    schedule resets every week but the player does not.

    Both skater and goalie projections compute this at projection time, since
    the two are built from entirely different quantities.
    """
    return float(getattr(proj, "keep_per_game", 0.0) or 0.0)


def build_roster_view(
    refs: list,
    projections: dict[int, PlayerProjection],
) -> tuple[list[RosterEntry], list[str]]:
    """
    Your roster, scored and sorted from most to least droppable.

    Returns (entries, unscored). Anything we could not project is returned
    rather than dropped -- a player who silently disappears from your roster
    view is worse than one shown with a blank score, because you would never
    know to look for him. Goalies land here today: the model covers skaters
    only.
    """
    entries: list[RosterEntry] = []
    unscored: list[str] = []
    for ref in refs:
        pid = getattr(ref, "nhl_id", None)
        proj = projections.get(pid) if pid else None
        if proj is None:
            reason = (
                "goalie -- not yet modelled"
                if (ref.position or "") == "g"
                else "no NHL data this season"
            )
            unscored.append(f"{ref.name} ({ref.team or '?'}) -- {reason}")
            continue

        entry = RosterEntry(
            projection=proj,
            keep_value=keep_value(proj),
            on_ir=bool(getattr(ref, "on_injured_reserve", False)),
        )
        # Yahoo's roster status beats the line-combination sites: it is your
        # league's own record of whether the player can be used at all.
        if getattr(ref, "is_out", False):
            proj.week_points = 0.0
            proj.per_game_points = 0.0
            for g in proj.games:
                g.points = 0.0
            status = (ref.status or "out").upper()
            proj.why = f"{status} per Yahoo -- cannot play this week"
        # Weekly contribution is already schedule-adjusted; keep_value is per
        # game, so scale it to a comparable weekly figure using a neutral
        # 3-game week before blending.
        weekly_equivalent = entry.keep_value * 3.0
        entry.drop_score = round(
            WEEK_WEIGHT * proj.week_points + KEEP_WEIGHT * weekly_equivalent, 2
        )
        entries.append(entry)

    entries.sort(key=lambda e: e.drop_score)
    return entries, unscored


def suggest_moves(
    roster: list[RosterEntry],
    candidates: list[PlayerProjection],
    *,
    max_moves: int | None = None,
    respect_position: bool = True,
) -> list[Move]:
    """
    Greedily pair the best available players with your most droppable ones.

    Greedy is the right tool here: the moves are nearly independent (one add
    does not change another's value), the search space is tiny, and a
    transparent "best available for worst roster spot" rule is something you
    can actually check by eye. An optimiser would buy almost nothing and cost
    you the ability to sanity-check it.
    """
    max_moves = max_moves if max_moves is not None else config.MAX_ACQUISITIONS_PER_WEEK

    available = sorted(candidates, key=lambda p: -p.week_points)
    droppable = sorted(roster, key=lambda e: e.drop_score)

    used_drops: set[int] = set()
    moves: list[Move] = []

    for cand in available:
        if len(moves) >= max_moves:
            break
        cand_group = position_group(cand.position)

        target: RosterEntry | None = None
        for entry in droppable:
            if entry.projection.nhl_id in used_drops:
                continue
            # A player in an IR slot is not taking up active roster space, so
            # cutting him frees nothing. Dropping an injured starter to chase a
            # four-game streamer is how you lose a season in November.
            if entry.on_ir:
                continue
            if respect_position and entry.group != cand_group:
                continue
            target = entry
            break
        if target is None:
            continue

        week_gain = round(cand.week_points - target.projection.week_points, 2)
        if week_gain <= 0:
            continue

        cand_keep = keep_value(cand)
        keep_cost = round(target.keep_value - cand_keep, 2)

        warning = ""
        if target.keep_value >= KEEP_VALUE_WARN and keep_cost > 0:
            warning = (
                f"{target.name} is worth {target.keep_value:.1f} pts/game going "
                f"forward vs {cand_keep:.1f} for {cand.name} -- you would win this "
                f"week and lose the rest of the season. Only do this if you need "
                f"the points now."
            )
        elif _role_change(target.projection) == "promoted":
            warning = (
                f"{target.name} was just promoted ({target.projection.role.role_note}) "
                f"-- his numbers may be about to rise."
            )

        used_drops.add(target.projection.nhl_id)
        moves.append(
            Move(add=cand, drop=target, week_gain=week_gain,
                 keep_cost=keep_cost, warning=warning)
        )

    return moves


def build_index() -> names.NameIndex:
    """Name index covering skaters and goalies."""
    index = names.NameIndex.from_nhl_rows(nhl_rest.skater_summary_season())
    for pid, row in nhl_rest.goalie_summary_season().items():
        index.add(pid, row.get("goalieFullName", ""), None, "G")
    return index


@dataclasses.dataclass
class Advice:
    roster: list[RosterEntry]
    candidates: list[PlayerProjection]
    moves: list[Move]
    unresolved_roster: list[str]
    free_agents_known: bool
    unscored_roster: list[str] = dataclasses.field(default_factory=list)
    moves_remaining: int = 0
    expected_goalie_appearances: float = 0.0
    goalie_minimum_at_risk: bool = False


def build_advice(
    *,
    today: _dt.date | None = None,
    progress: bool = False,
    max_candidates: int = 60,
) -> Advice:
    """Load roster + free agents, project everyone, and pair them up."""
    projections: dict = dict(projection.project_all(today=today, progress=progress))
    index = build_index()

    roster_refs = my_team.load_roster(index)
    fa_refs = my_team.load_free_agents(index)

    # Goalies are modelled separately and merged in, so that everything
    # downstream -- roster view, drop ranking, move suggestions -- sees one
    # combined pool rather than silently ignoring a third of your lineup.
    if progress:
        print("Projecting goalies...")
    goalie_ids = {
        r.nhl_id for r in (roster_refs + fa_refs)
        if r.nhl_id and (r.position or "") == "g"
    }
    goalie_projections = goalies.project_goalies(
        goalie_ids or None, today=today
    )
    projections.update(goalie_projections)

    roster_ids = [r.nhl_id for r in roster_refs if r.nhl_id]
    unresolved = [r.raw for r in roster_refs if not r.nhl_id]
    roster, unscored = build_roster_view(roster_refs, projections)

    roster_id_set = set(roster_ids)
    fa_ids = {r.nhl_id for r in fa_refs if r.nhl_id}
    free_agents_known = bool(fa_ids)

    if free_agents_known:
        pool = [p for pid, p in projections.items() if pid in fa_ids]
    else:
        # No free-agent list supplied: rank everyone not on your roster and let
        # the user check availability in Yahoo.
        pool = [p for pid, p in projections.items() if pid not in roster_id_set]

    pool.sort(key=lambda p: -p.week_points)
    candidates = pool[:max_candidates]

    remaining = max(
        0, config.MAX_ACQUISITIONS_PER_WEEK - config.ACQUISITIONS_USED_THIS_WEEK
    )
    moves = suggest_moves(roster, candidates, max_moves=remaining)

    roster_goalies = [
        e.projection for e in roster
        if getattr(e.projection, "is_goalie", False)
    ]
    expected_app, at_risk = goalies.appearance_shortfall(
        roster_goalies, config.MIN_GOALIE_STARTS_PER_WEEK
    )

    return Advice(
        roster=roster,
        candidates=candidates,
        moves=moves,
        unresolved_roster=unresolved,
        unscored_roster=unscored,
        free_agents_known=free_agents_known,
        moves_remaining=remaining,
        expected_goalie_appearances=expected_app,
        goalie_minimum_at_risk=at_risk,
    )


def format_advice(advice: Advice) -> str:
    """Plain-text report -- what gets written to disk by the daily refresh."""
    lines: list[str] = []

    if advice.unresolved_roster:
        lines.append("COULD NOT IDENTIFY THESE ROSTER ENTRIES:")
        for raw in advice.unresolved_roster:
            lines.append(f"   ? {raw}")
        lines.append("")

    if advice.goalie_minimum_at_risk:
        lines.append("!" * 96)
        lines.append(
            f"GOALIE APPEARANCE MINIMUM AT RISK: your goalies project to "
            f"{advice.expected_goalie_appearances:.1f} appearance(s) this week, "
            f"against a league minimum of {config.MIN_GOALIE_STARTS_PER_WEEK}."
        )
        lines.append(
            "Falling short voids ALL of your goaltending points for the week, "
            "not just the difference."
        )
        lines.append(
            "Start a goalie playing on a day you have none, or stream one from "
            "the waiver wire."
        )
        lines.append("!" * 96)
        lines.append("")

    if advice.unscored_roster:
        lines.append("ON YOUR ROSTER BUT NOT SCORED:")
        for item in advice.unscored_roster:
            lines.append(f"   - {item}")
        lines.append(
            "   These are excluded from the drop ranking below, so judge them "
            "yourself.\n"
        )

    if not advice.roster:
        lines.append(
            "No roster loaded. Paste your players into "
            f"{my_team.ROSTER_PATH} to get drop advice.\n"
        )
    else:
        lines.append("YOUR ROSTER -- most droppable first")
        lines.append("-" * 96)
        lines.append(
            f"{'PLAYER':<24} {'TM':<4} {'POS':<4} {'GP':>3} {'ROLE':<9} "
            f"{'WEEK':>7} {'KEEP/GM':>8}  NOTE"
        )
        for entry in advice.roster:
            p = entry.projection
            note = p.why[:46]
            lines.append(
                f"{p.name:<24} {p.team:<4} {p.position.upper():<4} {p.games_left:>3} "
                f"{role_label(p):<9} {p.week_points:>7.2f} "
                f"{entry.keep_value:>8.2f}  {note}"
            )
        lines.append("")

    if not advice.free_agents_known:
        lines.append(
            "NOTE: no free-agent list loaded, so the candidates below are the "
            "best available-or-not skaters in the league.\n"
            f"      Paste your league's available players into "
            f"{my_team.FREE_AGENT_PATH} for a true waiver list.\n"
        )

    lines.append("TOP PICKUP CANDIDATES")
    lines.append("-" * 96)
    lines.append(
        f"{'PLAYER':<24} {'TM':<4} {'POS':<4} {'GP':>3} {'ROLE':<9} "
        f"{'WEEK':>7} {'CONF':>5}  WHY"
    )
    for p in advice.candidates[:20]:
        flag = "*" if _role_change(p) == "promoted" else " "
        lines.append(
            f"{flag}{p.name:<23} {p.team:<4} {p.position.upper():<4} {p.games_left:>3} "
            f"{role_label(p):<9} {p.week_points:>7.2f} {p.confidence:>5.0%}  "
            f"{p.why[:52]}"
        )
    lines.append("")

    lines.append(
        f"SUGGESTED MOVES -- {advice.moves_remaining} add(s) left this week "
        f"({config.ACQUISITIONS_USED_THIS_WEEK} of "
        f"{config.MAX_ACQUISITIONS_PER_WEEK} already used)"
    )
    lines.append("-" * 96)
    if advice.moves_remaining <= 0:
        lines.append("   No acquisitions left this week.")
    elif not advice.moves:
        lines.append("   No move improves your week. Stand pat.")
    for i, move in enumerate(advice.moves, 1):
        lines.append(
            f"{i}. ADD  {move.add.name} ({move.add.team}, "
            f"{move.add.position.upper()}, {role_label(move.add)}, "
            f"{move.add.games_left} games) -- {move.add.week_points:.1f} pts"
        )
        lines.append(
            f"   DROP {move.drop.name} ({move.drop.projection.team}, "
            f"{move.drop.projection.position.upper()}, "
            f"{move.drop.projection.games_left} games) -- "
            f"{move.drop.projection.week_points:.1f} pts"
        )
        lines.append(
            f"   Net this week: +{move.week_gain:.1f}   "
            f"Keep-value change: {-move.keep_cost:+.1f}/game"
        )
        if move.warning:
            lines.append(f"   !! {move.warning}")
        lines.append("")

    return "\n".join(lines)


def _demo() -> None:  # pragma: no cover - manual smoke check
    advice = build_advice(progress=True)
    print()
    print(format_advice(advice))


if __name__ == "__main__":
    _demo()
