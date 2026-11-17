"""
Schedule and opponent quality for the rest of the fantasy week.

Two separate things live here, and they multiply together:

**How many games are left.** This is the single biggest lever in weekly fantasy
and the one most people underrate. A good player with one game left is worth
less this week than an average player with four. Nothing about talent changes
that arithmetic.

**Who those games are against.** Not all games are equal: a night against the
league's leakiest defence is worth meaningfully more than one against its best.
We scale each category separately, because opponents are not uniformly good --
a team can suppress goals while allowing plenty of shots, which matters when
shots pay 0.9 apiece.

The multipliers are deliberately gentle. Opponent quality is real but it is a
small effect next to games-remaining and ice time, and an aggressive matchup
adjustment mostly adds noise. Exponents below 1 compress the range: a team
allowing 20% more goals than average yields roughly a 12% bump, not 20%.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import functools as _functools

import data.nhl_client as nhl_client
import data.nhl_stats_rest as nhl_rest

# How hard opponent quality pushes each category. All < 1 to compress.
GOAL_EXPONENT = 0.60
SHOT_EXPONENT = 0.50
PP_EXPONENT = 0.50
BLOCK_EXPONENT = 0.40

# Playing at home is worth a little. This is well-established and small.
HOME_BONUS = 1.02
AWAY_PENALTY = 0.99

# Second half of a back-to-back: tired legs, and for goalies a real chance the
# backup gets the start.
BACK_TO_BACK_PENALTY = 0.96

# Clamp so one weird team cannot produce an absurd multiplier.
MULT_FLOOR, MULT_CEILING = 0.80, 1.25


@dataclasses.dataclass
class Game:
    """One scheduled game from a single team's point of view."""

    date: _dt.date
    opponent: str
    is_home: bool
    is_back_to_back: bool = False
    # NHL game state: FUT/PRE (not started), LIVE/CRIT (in progress),
    # OFF/FINAL (finished). Needed because a game that ended earlier today is
    # still "today", and counting it as remaining would double-count it
    # against points the player has already banked this week.
    state: str = "FUT"

    @property
    def label(self) -> str:
        return f"{'vs' if self.is_home else '@'} {self.opponent}"

    @property
    def is_finished(self) -> bool:
        return self.state.upper() in ("OFF", "FINAL")


@dataclasses.dataclass
class OpponentProfile:
    """Per-category difficulty of facing one team. 1.0 == league average."""

    team: str
    goals_against_per_game: float
    shots_against_per_game: float
    penalty_kill_pct: float
    goal_mult: float = 1.0
    shot_mult: float = 1.0
    pp_mult: float = 1.0
    block_mult: float = 1.0

    @property
    def grade(self) -> str:
        """A letter for the UI. Higher goal_mult == easier opponent."""
        m = self.goal_mult
        if m >= 1.10:
            return "A"
        if m >= 1.04:
            return "B"
        if m >= 0.97:
            return "C"
        if m >= 0.91:
            return "D"
        return "F"


def _clamp(value: float) -> float:
    return max(MULT_FLOOR, min(MULT_CEILING, value))


def fantasy_week_bounds(today: _dt.date | None = None) -> tuple[_dt.date, _dt.date]:
    """
    Yahoo's NHL scoring weeks run Monday through Sunday. Returns the bounds of
    the week `today` falls in.
    """
    today = today or _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    return monday, monday + _dt.timedelta(days=6)


def build_opponent_profiles(season_id: str | None = None) -> dict[str, OpponentProfile]:
    """One profile per team, normalised against the league average."""
    teams = nhl_rest.team_summary_season(season_id)
    if not teams:
        return {}

    n = len(teams)
    avg_ga = sum(t.get("goalsAgainstPerGame") or 0 for t in teams.values()) / n
    avg_sa = sum(t.get("shotsAgainstPerGame") or 0 for t in teams.values()) / n
    avg_sf = sum(t.get("shotsForPerGame") or 0 for t in teams.values()) / n
    avg_pk = sum(t.get("penaltyKillPct") or 0 for t in teams.values()) / n

    profiles: dict[str, OpponentProfile] = {}
    for abbrev, row in teams.items():
        ga = row.get("goalsAgainstPerGame") or avg_ga
        sa = row.get("shotsAgainstPerGame") or avg_sa
        sf = row.get("shotsForPerGame") or avg_sf
        pk = row.get("penaltyKillPct") or avg_pk

        profile = OpponentProfile(
            team=abbrev,
            goals_against_per_game=ga,
            shots_against_per_game=sa,
            penalty_kill_pct=pk,
        )
        # Facing a team that concedes a lot is good for scoring.
        profile.goal_mult = _clamp((ga / avg_ga) ** GOAL_EXPONENT) if avg_ga else 1.0
        # Facing a team that allows a lot of shots is good for SOG.
        profile.shot_mult = _clamp((sa / avg_sa) ** SHOT_EXPONENT) if avg_sa else 1.0
        # A weak penalty kill (low PK%) means more power-play points allowed.
        # We compare the *failure* rate, which is the quantity that matters.
        if avg_pk and avg_pk < 1:   # expressed as a fraction
            fail, avg_fail = 1 - pk, 1 - avg_pk
        else:                        # expressed as a percentage
            fail, avg_fail = 100 - pk, 100 - avg_pk
        profile.pp_mult = _clamp((fail / avg_fail) ** PP_EXPONENT) if avg_fail else 1.0
        # Facing a team that shoots a lot gives your skaters more to block.
        profile.block_mult = _clamp((sf / avg_sf) ** BLOCK_EXPONENT) if avg_sf else 1.0
        profiles[abbrev] = profile

    return profiles


@_functools.lru_cache(maxsize=512)
def team_games(
    team: str,
    start: _dt.date,
    end: _dt.date,
) -> list[Game]:
    """
    A team's games in [start, end], with back-to-backs flagged.

    Back-to-back detection looks at the *full* schedule, not just the window,
    so a Monday game that follows a Sunday game is correctly flagged even
    though Sunday sits in the previous week.
    """
    raw = nhl_client.team_schedule(team)
    if not raw:
        return []

    abbrev = nhl_client._nhl_abbrev(team)
    all_dates: list[_dt.date] = []
    parsed: list[tuple[_dt.date, str, bool, str]] = []
    for g in raw:
        try:
            gd = _dt.date.fromisoformat(str(g.get("gameDate", ""))[:10])
        except ValueError:
            continue
        home = (g.get("homeTeam", {}) or {}).get("abbrev", "")
        away = (g.get("awayTeam", {}) or {}).get("abbrev", "")
        if abbrev not in (home, away):
            continue
        is_home = home == abbrev
        opponent = away if is_home else home
        all_dates.append(gd)
        parsed.append((gd, opponent, is_home, str(g.get("gameState", "FUT"))))

    date_set = set(all_dates)
    games: list[Game] = []
    for gd, opponent, is_home, state in parsed:
        if not (start <= gd <= end):
            continue
        games.append(
            Game(
                date=gd,
                opponent=opponent,
                is_home=is_home,
                is_back_to_back=(gd - _dt.timedelta(days=1)) in date_set,
                state=state,
            )
        )
    games.sort(key=lambda g: g.date)
    return games


def remaining_games(
    team: str,
    today: _dt.date | None = None,
    include_today: bool = True,
    exclude_finished: bool = True,
) -> list[Game]:
    """
    Games left in the current fantasy week. This is the number that drives the
    whole weekly projection.

    Games that have already finished are excluded even when they fall on
    today's date. Without that check, an afternoon game that ended hours ago
    still counts as "remaining", and the player gets projected for points he
    has in fact already banked.
    """
    today = today or _dt.date.today()
    _, week_end = fantasy_week_bounds(today)
    start = today if include_today else today + _dt.timedelta(days=1)
    games = team_games(team, start, week_end)
    if not exclude_finished:
        # The backtest replays past weeks, where every game is finished by
        # definition. Filtering those out would leave every projection at zero.
        return games
    return [g for g in games if not g.is_finished]


def played_games(team: str, today: _dt.date | None = None) -> list[Game]:
    """Games this team has already completed in the current fantasy week."""
    today = today or _dt.date.today()
    week_start, _ = fantasy_week_bounds(today)
    return [g for g in team_games(team, week_start, today) if g.is_finished]


def situational_mult(game: Game) -> float:
    """Home/away and back-to-back, independent of who the opponent is."""
    mult = HOME_BONUS if game.is_home else AWAY_PENALTY
    if game.is_back_to_back:
        mult *= BACK_TO_BACK_PENALTY
    return mult


def _demo() -> None:  # pragma: no cover - manual smoke check
    import sys

    team = sys.argv[1].upper() if len(sys.argv) > 1 else "PHI"
    today = _dt.date.today()
    start, end = fantasy_week_bounds(today)
    profiles = build_opponent_profiles()

    print(f"Fantasy week: {start} .. {end}   (today {today})\n")
    games = remaining_games(team, today)
    print(f"{team} has {len(games)} game(s) left this week:")
    for g in games:
        p = profiles.get(g.opponent)
        extra = (
            f"  GA/gp {p.goals_against_per_game:.2f}  goal x{p.goal_mult:.3f}  "
            f"shot x{p.shot_mult:.3f}  pp x{p.pp_mult:.3f}  grade {p.grade}"
            if p else "  (no profile)"
        )
        b2b = "  [back-to-back]" if g.is_back_to_back else ""
        print(f"   {g.date} {g.label:<8}{extra}{b2b}")

    print("\nEasiest opponents to face right now (by goal multiplier):")
    for p in sorted(profiles.values(), key=lambda x: -x.goal_mult)[:6]:
        print(f"   {p.team}  x{p.goal_mult:.3f}  (GA/gp {p.goals_against_per_game:.2f})  {p.grade}")
    print("\nToughest:")
    for p in sorted(profiles.values(), key=lambda x: x.goal_mult)[:6]:
        print(f"   {p.team}  x{p.goal_mult:.3f}  (GA/gp {p.goals_against_per_game:.2f})  {p.grade}")


if __name__ == "__main__":
    _demo()
