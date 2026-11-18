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


# ---------------------------------------------------------------------------
# Daily lineup check
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LineupMove:
    """One change worth making to today's lineup."""

    slot: str
    start: str | None          # player to move in
    sit: str | None            # player to move out
    gain: float                # projected points gained
    free: bool                 # the player coming out is not playing at all
    note: str = ""

    @property
    def kind(self) -> str:
        if self.free:
            return "free"
        return "judgement"


@dataclasses.dataclass
class DailyCheck:
    date: _dt.date
    current_points: float
    optimal_points: float
    moves: list[LineupMove]
    idle_starters: list[str]       # started, but their team is not playing
    unused_available: list[str]    # benched, and their team IS playing
    no_games: bool = False

    @property
    def gain(self) -> float:
        return round(self.optimal_points - self.current_points, 2)

    @property
    def free_moves(self) -> list[LineupMove]:
        return [m for m in self.moves if m.free]

    @property
    def judgement_moves(self) -> list[LineupMove]:
        return [m for m in self.moves if not m.free]


def daily_check(
    roster: list[Candidate],
    current_slots: dict[int, str],
    day: _dt.date | None = None,
    slots: dict[str, int] | None = None,
) -> DailyCheck:
    """
    Compare the lineup as it stands against the best one available today.

    Moves are split into two kinds, because they do not deserve equal
    confidence:

    **Free.** A player is in an active slot while his team is idle, and someone
    on your bench is playing. That is not a close call between two
    projections, it is a guaranteed zero against a real number. Always take it.

    **Judgement.** Both players are playing and the solver prefers one of them.
    That is a projection difference, and projections are wrong all the time, so
    it is offered rather than urged.

    A day with no games at all produces nothing to do, which matters: an empty
    lineup looks alarming and is often simply the league not playing.
    """
    day = day or _dt.date.today()
    slots = slots or DEFAULT_SLOTS

    by_id = {c.nhl_id: c for c in roster}
    playing_today = [c for c in roster if c.points_on(day) is not None]
    if not playing_today:
        return DailyCheck(
            date=day, current_points=0.0, optimal_points=0.0, moves=[],
            idle_starters=[], unused_available=[], no_games=True,
        )

    started_ids = {
        pid for pid, slot in current_slots.items()
        if slot and slot.upper() not in ("BN", "IR", "IR+", "IR-", "NA")
    }

    current_points = round(sum(
        (by_id[pid].points_on(day) or 0.0)
        for pid in started_ids if pid in by_id
    ), 2)

    best = solve_day(roster, day, slots)
    optimal_names = {
        name for picks in best.assigned.values() for name, _ in picks
    }

    idle_starters = [
        by_id[pid].name for pid in started_ids
        if pid in by_id and by_id[pid].points_on(day) is None
    ]
    unused_available = [
        c.name for c in playing_today
        if c.nhl_id not in started_ids and c.name in optimal_names
    ]

    # Pair players the optimum wants in against players it wants out, WITHIN
    # the same slot type. Pairing across types produces nonsense like "start
    # this winger, sit that goalie": the arithmetic still works because the
    # totals are set-level, but the instruction is unfollowable.
    def norm(slot: str) -> str:
        return (slot or "").strip().lower()

    current_by_slot: dict[str, list[str]] = {}
    for pid, slot in current_slots.items():
        key = norm(slot)
        if key in slots and pid in by_id:
            current_by_slot.setdefault(key, []).append(by_id[pid].name)

    moves: list[LineupMove] = []
    unpaired_in: list[tuple[str, str, float]] = []
    unpaired_out: list[tuple[str, str, float | None]] = []

    for slot_type in slots:
        opt_here = best.assigned.get(slot_type, [])
        cur_here = current_by_slot.get(slot_type, [])
        # A player who is starting both before and after has not moved, even
        # if he shifted between slot types (a LW/RW filling the other wing).
        ins = [
            (n, pts) for n, pts in opt_here
            if _id_for(roster, n) not in started_ids
        ]
        outs = [
            (n, by_id[_id_for(roster, n)].points_on(day) if _id_for(roster, n) in by_id else None)
            for n in cur_here
            if n not in optimal_names
        ]
        # Displace idle players first: that is where the certain gain is.
        outs.sort(key=lambda o: (o[1] is not None, o[1] or 0.0))
        ins.sort(key=lambda i: -i[1])

        for (in_name, in_pts), (out_name, out_pts) in zip(ins, outs):
            is_free = out_pts is None
            gain = round(in_pts - (out_pts or 0.0), 2)
            if gain <= 0 and not is_free:
                continue        # not an improvement; do not suggest it
            moves.append(LineupMove(
                slot=slot_type, start=in_name, sit=out_name,
                gain=gain, free=is_free,
                note=(
                    f"{out_name}'s team is idle today"
                    if is_free else
                    f"{in_name} projects {gain:+.1f} over {out_name}"
                ),
            ))
        if len(ins) > len(outs):
            unpaired_in.extend((slot_type, n, p) for n, p in ins[len(outs):])
        elif len(outs) > len(ins):
            unpaired_out.extend((slot_type, n, p) for n, p in outs[len(ins):])

    # Anything the optimum wants started with no one to displace in that slot
    # is reported on its own rather than forced into a false pairing.
    for slot_type, name, pts in unpaired_in:
        moves.append(LineupMove(
            slot=slot_type, start=name, sit=None, gain=round(pts, 2),
            free=True, note=f"{slot_type.upper()} slot is empty today",
        ))
    # Deliberately NOT emitted as moves: a starter whose team is idle and who
    # has no eligible replacement on the bench. There is nothing to do about
    # him, and listing nine such players on a quiet night buries the two real
    # actions. They are already reported in `idle_starters` as context.

    moves.sort(key=lambda m: (not m.free, -m.gain))

    return DailyCheck(
        date=day,
        current_points=current_points,
        optimal_points=best.total,
        moves=moves,
        idle_starters=idle_starters,
        unused_available=unused_available,
    )


def _id_for(roster: list[Candidate], name: str) -> int | None:
    for c in roster:
        if c.name == name:
            return c.nhl_id
    return None


# ---------------------------------------------------------------------------
# Add / drop pairing
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SwapResult:
    """What swapping one player for another does to the week you can field."""

    add_id: int
    drop_id: int
    add_name: str
    drop_name: str
    gain: float                      # points added to the best week
    add_starts: list[_dt.date]
    drop_was_starting: int           # days the dropped player was in the lineup


def removal_cost(
    roster: list[Candidate],
    player_id: int,
    days: list[_dt.date],
    slots: dict[str, int] | None = None,
    base_plan: WeekPlan | None = None,
) -> float:
    """
    How much the best week falls if this player disappears.

    This is the right way to rank a drop, and it is not the same as his
    projection. A player can project respectably and cost nothing to lose,
    because on every day he plays your slots are already filled by someone
    better. The number that matters is what you would actually forfeit.
    """
    base = base_plan or solve_week(roster, days, slots)
    without = solve_week(
        [c for c in roster if c.nhl_id != player_id], days, slots
    )
    return round(base.total - without.total, 2)


def evaluate_swap(
    roster: list[Candidate],
    addition: Candidate,
    drop_id: int,
    days: list[_dt.date],
    slots: dict[str, int] | None = None,
    base_total: float | None = None,
    base_plan: WeekPlan | None = None,
) -> SwapResult:
    """
    Exact value of one add/drop pair: solve the week with the swap applied.

    Done directly rather than as (value of adding) minus (cost of dropping),
    because those two interact. Dropping a defenceman can change which slot
    the new player fills, and the approximation quietly misses that.
    """
    base_plan = base_plan or solve_week(roster, days, slots)
    base = base_total if base_total is not None else base_plan.total

    swapped = [c for c in roster if c.nhl_id != drop_id] + [addition]
    after = solve_week(swapped, days, slots)

    add_starts = []
    for day_plan in after.days:
        if any(
            name == addition.name
            for picks in day_plan.assigned.values()
            for name, _ in picks
        ):
            add_starts.append(day_plan.date)

    # Reuse the caller's baseline rather than re-solving it per pair. With
    # eighteen candidates against six drop targets this was a hundred-odd
    # redundant week solves, and it dominated the runtime.
    before = base_plan
    drop_days = 0
    drop_name = next(
        (c.name for c in roster if c.nhl_id == drop_id), str(drop_id)
    )
    for day_plan in before.days:
        if any(
            name == drop_name
            for picks in day_plan.assigned.values()
            for name, _ in picks
        ):
            drop_days += 1

    return SwapResult(
        add_id=addition.nhl_id, drop_id=drop_id,
        add_name=addition.name, drop_name=drop_name,
        gain=round(after.total - base, 2),
        add_starts=add_starts, drop_was_starting=drop_days,
    )
