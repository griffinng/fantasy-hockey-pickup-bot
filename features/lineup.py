"""
Daily lineup solver, and what a pickup is actually worth to *your* roster.

The point this exists to fix: a weekly projection is an upper bound. It assumes
a player appears in every game his team plays and that you start him every
time. It knows nothing about the shape of your roster. If you already field six
forwards on Thursday into six forward slots, a seventh forward who plays
Thursday is worth exactly nothing to you that day, however good he is.

So value is measured as a difference between lineups:

    marginal value = (best week I can field WITH him)
                   - (best week I can field WITHOUT him)

Each day is an assignment problem -- players to slots, maximising projected
points, subject to Yahoo's position eligibility -- which `scipy` solves exactly
via `linear_sum_assignment`. The sizes here are tiny (about twenty players
across ten slots) so exactness is free, and an exact answer is worth having
because the whole number is a difference between two solutions: approximation
error in either one shows up directly in the result.

One trap worth naming. Scarcity has to be measured against the **schedule**,
not just the roster. A day where you can field nobody looks like a gaping hole
and may be worth nothing at all, because the league is not playing that day.
The solver gets this right by construction -- it can only assign players whose
teams actually play -- but a heuristic along the lines of "fill your empty
days" would walk straight into it.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt

from scipy.optimize import linear_sum_assignment

import config
import features.matchups as matchups

#: Slots that must be filled each day, from the Yahoo roster page.
DEFAULT_SLOTS: dict[str, int] = {"c": 2, "lw": 2, "rw": 2, "d": 4, "g": 2}

#: Fallback eligibility when a player's Yahoo positions are unknown.
_FALLBACK_ELIGIBLE = {
    "c": ["c"], "lw": ["lw"], "rw": ["rw"], "l": ["lw"], "r": ["rw"],
    "d": ["d"], "ld": ["d"], "rd": ["d"], "g": ["g"], "f": ["c", "lw", "rw"],
}


@dataclasses.dataclass
class Candidate:
    """One player available to be slotted on a given day."""

    nhl_id: int
    name: str
    eligible: tuple[str, ...]
    points_by_date: dict[_dt.date, float]

    def points_on(self, day: _dt.date) -> float | None:
        """Projected points, or None if his team is not playing."""
        return self.points_by_date.get(day)


@dataclasses.dataclass
class DayLineup:
    date: _dt.date
    assigned: dict[str, list[tuple[str, float]]]   # slot -> [(name, pts)]
    total: float
    empty_slots: list[str]
    benched: list[str]


@dataclasses.dataclass
class WeekPlan:
    days: list[DayLineup]

    @property
    def total(self) -> float:
        return round(sum(d.total for d in self.days), 2)


def eligibility_of(player, fallback_position: str = "") -> tuple[str, ...]:
    """
    Yahoo slot eligibility for a player, falling back to his NHL position.

    Yahoo grants multi-position eligibility and the NHL data carries only one
    position, so the Yahoo value is used wherever we have it.
    """
    elig = getattr(player, "eligible", None)
    if elig:
        return tuple(e for e in elig if e in DEFAULT_SLOTS)
    pos = (fallback_position or getattr(player, "position", "") or "").lower()
    return tuple(_FALLBACK_ELIGIBLE.get(pos, ["c", "lw", "rw"]))


def solve_day(
    candidates: list[Candidate],
    day: _dt.date,
    slots: dict[str, int] | None = None,
) -> DayLineup:
    """
    Best legal lineup for one day.

    Built as a rectangular assignment problem and solved exactly. Players whose
    teams are idle are simply absent from the pool, which is what makes the
    "empty slots on a day with no games" case resolve correctly without a
    special rule.
    """
    slots = slots or DEFAULT_SLOTS

    playing = [
        c for c in candidates
        if c.points_on(day) is not None and c.eligible
    ]
    slot_list: list[str] = []
    for slot, count in slots.items():
        slot_list.extend([slot] * count)

    if not playing or not slot_list:
        return DayLineup(
            date=day, assigned={}, total=0.0,
            empty_slots=list(slot_list), benched=[c.name for c in playing],
        )

    # Cost matrix: rows players, columns slots. Ineligible pairings get a large
    # penalty rather than infinity so the solver always finds some assignment.
    NEG = -1e6
    matrix = []
    for c in playing:
        pts = c.points_on(day) or 0.0
        matrix.append([
            -pts if slot in c.eligible else -NEG
            for slot in slot_list
        ])

    rows, cols = linear_sum_assignment(matrix)

    assigned: dict[str, list[tuple[str, float]]] = {}
    used_rows: set[int] = set()
    filled_cols: set[int] = set()
    total = 0.0
    for r, col in zip(rows, cols):
        player = playing[r]
        slot = slot_list[col]
        if slot not in player.eligible:
            continue                      # solver had to park him somewhere
        pts = player.points_on(day) or 0.0
        assigned.setdefault(slot, []).append((player.name, round(pts, 2)))
        total += pts
        used_rows.add(r)
        filled_cols.add(col)

    return DayLineup(
        date=day,
        assigned=assigned,
        total=round(total, 2),
        empty_slots=[slot_list[i] for i in range(len(slot_list)) if i not in filled_cols],
        benched=[c.name for i, c in enumerate(playing) if i not in used_rows],
    )


def solve_week(
    candidates: list[Candidate],
    days: list[_dt.date],
    slots: dict[str, int] | None = None,
) -> WeekPlan:
    return WeekPlan(days=[solve_day(candidates, d, slots) for d in days])


def candidate_from_projection(proj, eligible: tuple[str, ...]) -> Candidate:
    """Turn a PlayerProjection or GoalieProjection into a solver candidate."""
    return Candidate(
        nhl_id=proj.nhl_id,
        name=proj.name,
        eligible=eligible,
        points_by_date={g.game.date: g.points for g in proj.games},
    )


def remaining_days(today: _dt.date | None = None) -> list[_dt.date]:
    today = today or _dt.date.today()
    _, week_end = matchups.fantasy_week_bounds(today)
    return [
        today + _dt.timedelta(days=i)
        for i in range((week_end - today).days + 1)
    ]


@dataclasses.dataclass
class FitResult:
    nhl_id: int
    marginal: float                     # points added to the best week
    days_used: list[_dt.date]           # days he would actually start
    note: str = ""

    @property
    def starts(self) -> int:
        return len(self.days_used)


def marginal_value(
    roster: list[Candidate],
    addition: Candidate,
    days: list[_dt.date],
    slots: dict[str, int] | None = None,
    base_plan: WeekPlan | None = None,
) -> FitResult:
    """
    How much better your week gets if this player were available to start.

    Deliberately does not model dropping anyone. The player you would cut is
    almost always one who was not starting, so removing him changes the best
    lineup by nothing; folding a drop into this number would mostly add noise
    and make the result harder to read. What is measured is the thing you
    actually care about: does he improve the lineups you can field?
    """
    base = base_plan or solve_week(roster, days, slots)
    with_him = solve_week(roster + [addition], days, slots)

    days_used = []
    for day_before, day_after in zip(base.days, with_him.days):
        in_after = any(
            name == addition.name
            for picks in day_after.assigned.values()
            for name, _ in picks
        )
        if in_after and day_after.total > day_before.total + 1e-9:
            days_used.append(day_after.date)

    marginal = round(with_him.total - base.total, 2)

    if marginal <= 0.05:
        note = "no lineup gain — your slots are already full on his game days"
    elif len(days_used) == 1:
        note = f"starts {days_used[0]:%a}, a day you are short"
    else:
        note = f"starts {len(days_used)} day(s) you are short"

    return FitResult(
        nhl_id=addition.nhl_id, marginal=marginal,
        days_used=days_used, note=note,
    )


def coverage_report(
    roster: list[Candidate],
    days: list[_dt.date],
    slots: dict[str, int] | None = None,
) -> list[tuple[_dt.date, int, int, list[str]]]:
    """(day, slots filled, slots total, empty slot names) for each day."""
    slots = slots or DEFAULT_SLOTS
    total_slots = sum(slots.values())
    out = []
    for day in days:
        lineup = solve_day(roster, day, slots)
        out.append((day, total_slots - len(lineup.empty_slots), total_slots,
                    lineup.empty_slots))
    return out


def league_slots() -> dict[str, int]:
    """Slot structure, overridable in config.py."""
    return dict(getattr(config, "ROSTER_SLOTS", DEFAULT_SLOTS))
