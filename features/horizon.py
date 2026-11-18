"""
Who is worth holding over the next month, as opposed to the next four days.

The weekly machinery answers "what wins me this week". It is the wrong tool
for "who should I cut", because almost everything that makes it work -- which
days have open slots, who is on a back-to-back, which opponent is soft -- is
noise once you look past Sunday. Over a month those effects average out and
three things are left:

    value per game  x  games he will play  x  will his role hold

So this module deliberately throws the schedule detail away and keeps only the
game COUNT, uses the schedule-free `keep_per_game` as the rate, and reports
how much of that rate depends on power-play time, which is the part that can
vanish without warning.

That last column is the one people get wrong. Two defencemen can be worth the
same 5.3 a night while one earns it from shot volume and blocks that nobody
can take away, and the other earns a third of it from a power-play job he is
one coach's decision away from losing. They are not the same asset.

    python -m features.horizon
    python -m features.horizon --days 35
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.names as names  # noqa: E402
import features.matchups as matchups  # noqa: E402

_SECONDS_PER_HOUR = 3600.0

# Share of team power-play time that marks a job as genuinely his. Below the
# lower bound he is a passenger on the second unit; above the upper he owns
# the first unit and it would take an injury or a trade to move him.
PP_SECURE = 0.50
PP_MARGINAL = 0.30


@dataclasses.dataclass
class Outlook:
    """One player's next-month picture."""

    nhl_id: int
    name: str
    team: str
    position: str
    keep_per_game: float          # neutral-night value, no schedule in it
    es_value: float               # of that, earned at even strength
    pp_value: float               # of that, earned on the power play
    games: int                    # games his team plays in the horizon
    pp_share: float               # fraction of team PP time he takes
    pp_unit: int | None
    line_rank: int | None
    recent_fp: float
    season_fp: float

    @property
    def total(self) -> float:
        """Expected points over the horizon."""
        return round(self.keep_per_game * self.games, 1)

    @property
    def games_per_week(self) -> float:
        return self.games

    @property
    def pp_dependency(self) -> float:
        """Fraction of his per-game value that comes from the power play."""
        if self.keep_per_game <= 0:
            return 0.0
        return round(self.pp_value / self.keep_per_game, 3)

    @property
    def security(self) -> str:
        """How safe the power-play share looks, on measured time alone."""
        if self.pp_dependency < 0.12:
            return "n/a"          # barely matters to him
        if self.pp_share >= PP_SECURE:
            return "secure"
        if self.pp_share >= PP_MARGINAL:
            return "contested"
        return "fragile"

    @property
    def floor_per_game(self) -> float:
        """What he is worth if the power-play time disappears entirely."""
        return round(self.es_value, 2)

    @property
    def floor_total(self) -> float:
        return round(self.floor_per_game * self.games, 1)


def games_in_horizon(team: str, start: _dt.date, days: int) -> int:
    """Games this team plays in the next `days` days, today included."""
    if not team:
        return 0
    end = start + _dt.timedelta(days=days)
    return len([g for g in matchups.team_games(team, start, end) if not g.is_finished])


def build_outlook(proj, *, today: _dt.date | None = None, days: int = 35) -> Outlook | None:
    """Turn a PlayerProjection into a month-horizon view. Skaters only."""
    rates = getattr(proj, "rates", None)
    role = getattr(proj, "role", None)
    if rates is None:
        # Goalies are modelled separately and have no per-60 split; a month
        # outlook for them would need start-share projection, not this.
        return None

    today = today or _dt.date.today()
    es = rates.nonpp_per60 * (getattr(proj, "es_toi", 0.0) / _SECONDS_PER_HOUR)
    pp = rates.pp_per60 * (getattr(proj, "pp_toi", 0.0) / _SECONDS_PER_HOUR)

    return Outlook(
        nhl_id=proj.nhl_id,
        name=proj.name,
        team=proj.team,
        position=proj.position,
        keep_per_game=round(es + pp, 2),
        es_value=round(es, 2),
        pp_value=round(pp, 2),
        games=games_in_horizon(proj.team, today, days),
        pp_share=float(getattr(role, "pp_share", 0.0) or 0.0),
        pp_unit=getattr(role, "pp_unit", None),
        line_rank=getattr(role, "line_rank", None),
        recent_fp=float(getattr(proj, "recent_fp_per_game", 0.0) or 0.0),
        season_fp=float(getattr(proj, "season_fp_per_game", 0.0) or 0.0),
    )


def format_table(outlooks: list[Outlook], days: int) -> str:
    """One readable block. Sorted worst-to-best, since this ranks drops."""
    lines = [
        f"{'PLAYER':<20} {'TM':<4} {'POS':<4} {'GP':>3} {'KEEP':>6} "
        f"{'TOTAL':>7} {'ES':>6} {'PP':>6} {'PP%':>6} {'DEPEND':>7} "
        f"{'SECURITY':<10} {'FLOOR':>7}",
        "-" * 108,
    ]
    for o in sorted(outlooks, key=lambda x: x.total):
        lines.append(
            f"{o.name[:19]:<20} {o.team:<4} {names.display_position(o.position):<4} "
            f"{o.games:>3} {o.keep_per_game:>6.2f} {o.total:>7.1f} "
            f"{o.es_value:>6.2f} {o.pp_value:>6.2f} {o.pp_share * 100:>5.0f}% "
            f"{o.pp_dependency * 100:>6.0f}% {o.security:<10} {o.floor_total:>7.1f}"
        )
    lines.append("")
    lines.append(
        f"GP      games in the next {days} days      "
        f"KEEP    value per game, schedule stripped out"
    )
    lines.append(
        "TOTAL   KEEP x GP                      "
        "FLOOR   TOTAL if his power-play time went to zero"
    )
    lines.append(
        "DEPEND  share of KEEP earned on the PP   "
        "PP%     his share of the team's PP minutes"
    )
    return "\n".join(lines)


def _demo() -> int:  # pragma: no cover - manual check
    import features.advisor as advisor

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=35)
    args = ap.parse_args()

    advice = advisor.build_advice()
    outs = []
    for e in advice.roster:
        o = build_outlook(e.projection, days=args.days)
        if o:
            outs.append(o)
    print(format_table(outs, args.days))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_demo())
