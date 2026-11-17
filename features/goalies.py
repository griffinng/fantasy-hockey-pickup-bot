"""
Goalie projections.

Goalies share almost nothing with skaters and deserve their own model. A
skater's value is rate x minutes; a goalie's is almost entirely **does he
start**, and then how many shots he faces and how many he stops.

Your league pays W 5, GA -3, SV 0.6, SHO 5. That scoring has a property worth
understanding before trusting any number here: **saves and goals against pull
in opposite directions on the same event.** A shot that goes in costs 3; a shot
stopped earns 0.6. So a goalie facing 35 shots at a .900 save percentage earns
31.5 x 0.6 = 18.9 from saves and loses 3.5 x 3 = 10.5 from goals, netting 8.4
before the win. A goalie facing 22 shots at the same .900 nets 5.3. Volume is
*good* for you, as long as the save percentage holds.

That is why a busy goalie on a mediocre team can out-earn a quiet one on a
strong team, and why this model projects shots faced explicitly rather than
leaning on goals-against average.

Three quantities drive everything:

1. **P(start)** -- from Daily Faceoff's goalie depth chart, blended with his
   recent share of his team's starts, and cut sharply on the second night of a
   back-to-back.
2. **Shots faced** -- the opponent's shots-for rate, adjusted for how much the
   goalie's own team suppresses shots.
3. **Save percentage** -- heavily regressed toward league average. Goalie save
   percentage is the noisiest statistic in mainstream hockey analytics; over a
   handful of starts it is close to meaningless, so the prior does most of the
   work until a real sample accumulates.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import math

import data.dailyfaceoff as dfo
import data.names as names
import data.nhl_stats_rest as nhl_rest
import features.matchups as matchups
from features.scoring import goalie_fantasy_points

# League-average shots a team puts on net per game; used only as a fallback.
FALLBACK_SHOTS = 29.0


@dataclasses.dataclass
class GoalieGameProjection:
    game: matchups.Game
    start_prob: float
    expected_shots: float
    expected_saves: float
    expected_goals_against: float
    win_prob: float
    shutout_prob: float
    points_if_starting: float
    points: float          # already weighted by start_prob


@dataclasses.dataclass
class GoalieProjection:
    """Mirrors the shape of PlayerProjection so the advisor can treat both alike."""

    nhl_id: int
    name: str
    team: str
    position: str = "g"
    games: list = dataclasses.field(default_factory=list)
    week_points: float = 0.0
    per_game_points: float = 0.0
    keep_per_game: float = 0.0
    confidence: float = 0.0
    why: str = ""
    role = None
    rates = None
    es_toi: float = 0.0
    pp_toi: float = 0.0

    points_so_far: float = 0.0
    games_played_this_week: int = 0
    recent_fp_per_game: float = 0.0
    season_fp_per_game: float = 0.0

    @property
    def form_delta(self) -> float:
        return round(self.recent_fp_per_game - self.season_fp_per_game, 2)

    # goalie-specific
    depth: int | None = None
    save_pct: float = 0.0
    expected_starts: float = 0.0

    @property
    def week_total(self) -> float:
        return round(self.points_so_far + self.week_points, 2)

    @property
    def games_left(self) -> int:
        return len(self.games)

    @property
    def schedule_label(self) -> str:
        return ", ".join(g.game.label for g in self.games) or "-"

    @property
    def is_goalie(self) -> bool:
        return True


def _shrunk_save_pct(row: dict, weights: dict) -> float:
    """
    Regress save percentage toward league average, weighted by shots faced.

    With a 400-shot prior, a goalie who has faced 200 shots gets only a third
    of the weight on his own number. That feels harsh and is roughly correct:
    400 shots is about 14 starts and still not enough to separate a good goalie
    from a lucky one.
    """
    cfg = weights["goalies"]
    prior_shots = float(cfg["save_pct_prior_shots"])
    league = float(cfg["league_save_pct"])

    shots = float(row.get("shotsAgainst") or 0)
    saves = float(row.get("saves") or 0)
    if shots <= 0:
        return league
    observed = saves / shots
    return (saves + league * prior_shots) / (shots + prior_shots) if shots else observed


def _start_probability(
    depth: int | None,
    recent_start_share: float,
    game: matchups.Game,
    weights: dict,
) -> float:
    cfg = weights["goalies"]
    if depth == 1:
        base = float(cfg["starter_start_prob"])
    elif depth is not None:
        base = float(cfg["backup_start_prob"])
    else:
        # No depth chart entry: fall back entirely on observed starts.
        base = recent_start_share

    # Blend the listing with what he has actually been getting.
    prob = 0.6 * base + 0.4 * recent_start_share if recent_start_share > 0 else base

    if game.is_back_to_back:
        # Starters usually sit the back half of a back-to-back; backups gain.
        if depth == 1:
            prob = min(prob, float(cfg["back_to_back_starter_prob"]))
        elif depth is not None:
            prob = max(prob, 1.0 - float(cfg["back_to_back_starter_prob"]) - 0.15)

    return max(0.0, min(1.0, prob))


def _win_probability(
    team: str,
    game: matchups.Game,
    profiles: dict[str, matchups.OpponentProfile],
    teams: dict[str, dict],
    weights: dict,
) -> float:
    """
    A deliberately simple strength model: expected goals for minus expected
    goals against, squashed through a logistic. Enough to separate a good team
    from a bad one without pretending to more precision than we have.
    """
    cfg = weights["goalies"]
    base = float(cfg["base_win_prob"])
    k = float(cfg["win_prob_strength_weight"])

    own = teams.get(team, {})
    opp = teams.get(game.opponent, {})
    if not own or not opp:
        return base

    xgf = float(own.get("goalsForPerGame") or 3.0)
    xga = float(opp.get("goalsForPerGame") or 3.0)
    margin = xgf - xga
    if game.is_home:
        margin += 0.20

    return max(0.05, min(0.95, 1.0 / (1.0 + math.exp(-k * margin * 3.0))))


def project_goalies(
    goalie_ids: set[int] | None = None,
    *,
    today: _dt.date | None = None,
    weights: dict | None = None,
    window_days: int = 30,
) -> dict[int, GoalieProjection]:
    """
    Project every goalie (or just `goalie_ids`) for the rest of the fantasy week.

    A 30-day window is used rather than the skaters' 14: goalies start at most
    half their team's games, so a fortnight can be three appearances, which is
    not enough to read anything from.
    """
    import features.projection as projection  # local import avoids a cycle

    weights = weights or projection.load_weights()
    cfg = weights["goalies"]

    season = nhl_rest.goalie_summary_season()
    window = nhl_rest.goalie_summary_window(window_days)
    teams = nhl_rest.team_summary_season()
    profiles = matchups.build_opponent_profiles()

    # Map goalies to teams and depth via Daily Faceoff.
    index = names.NameIndex()
    for pid, row in season.items():
        index.add(pid, row.get("goalieFullName", ""), None, "G")

    depth_by_id: dict[int, int] = {}
    team_by_id: dict[int, str] = {}
    injury_by_id: dict[int, str] = {}
    for listing in dfo.all_lines():
        if not listing.is_goalie:
            continue
        pid = index.resolve(listing.name, listing.team, "g")
        if pid is None:
            continue
        team_by_id[pid] = listing.team
        injury_by_id[pid] = listing.injury_status
        if listing.goalie_rank:
            depth_by_id[pid] = listing.goalie_rank

    league_shots = (
        sum(float(t.get("shotsForPerGame") or 0) for t in teams.values()) / len(teams)
        if teams else FALLBACK_SHOTS
    )

    # Points already banked this fantasy week, shown separately from projected.
    week_start, _ = matchups.fantasy_week_bounds(today or _dt.date.today())
    banked = {
        int(r["playerId"]): r
        for r in nhl_rest._fetch(
            "goalie/summary",
            nhl_rest._date_exp(week_start, today or _dt.date.today()),
            aggregate=True, ttl=900,
        )
        if r.get("playerId")
    }

    out: dict[int, GoalieProjection] = {}
    targets = goalie_ids if goalie_ids is not None else set(season)

    for pid in targets:
        srow = season.get(pid, {})
        wrow = window.get(pid, {})
        name = srow.get("goalieFullName") or wrow.get("goalieFullName") or str(pid)
        team = team_by_id.get(pid, "")
        if not team:
            continue

        depth = depth_by_id.get(pid)
        save_pct = _shrunk_save_pct(srow, weights)

        # Share of his team's games he has started recently.
        team_games = max(1.0, float((teams.get(team, {}) or {}).get("gamesPlayed") or 1))
        starts = float(srow.get("gamesStarted") or srow.get("gamesPlayed") or 0)
        recent_share = max(0.0, min(1.0, starts / team_games))

        proj = GoalieProjection(
            nhl_id=pid, name=name, team=team, depth=depth,
            save_pct=save_pct,
        )
        brow = banked.get(pid)
        if brow:
            proj.games_played_this_week = int(brow.get("gamesPlayed") or 0)
            proj.points_so_far = round(goalie_fantasy_points(
                wins=brow.get("wins", 0) or 0,
                goals_against=brow.get("goalsAgainst", 0) or 0,
                saves=brow.get("saves", 0) or 0,
                shutouts=brow.get("shutouts", 0) or 0,
            ), 2)

        if (injury_by_id.get(pid) or "").lower() in ("out", "ir", "ltir"):
            proj.why = f"{injury_by_id[pid].upper()} -- not available"
            out[pid] = proj
            continue

        games = matchups.remaining_games(team, today)
        total = 0.0
        expected_starts = 0.0

        for game in games:
            opp = teams.get(game.opponent, {})
            opp_shots = float(opp.get("shotsForPerGame") or league_shots)
            # Scale by how well the goalie's own team suppresses shots.
            own_sa = float((teams.get(team, {}) or {}).get("shotsAgainstPerGame") or league_shots)
            expected_shots = (opp_shots + own_sa) / 2.0

            expected_ga = expected_shots * (1.0 - save_pct)
            expected_saves = expected_shots - expected_ga

            win_p = _win_probability(team, game, profiles, teams, weights)
            # Poisson: P(zero goals allowed) given the expected number.
            shutout_p = math.exp(-expected_ga)

            points_if_starting = goalie_fantasy_points(
                wins=win_p,
                goals_against=expected_ga,
                saves=expected_saves,
                shutouts=shutout_p,
            )
            start_p = _start_probability(depth, recent_share, game, weights)
            points = points_if_starting * start_p

            total += points
            expected_starts += start_p
            proj.games.append(
                GoalieGameProjection(
                    game=game,
                    start_prob=round(start_p, 3),
                    expected_shots=round(expected_shots, 1),
                    expected_saves=round(expected_saves, 1),
                    expected_goals_against=round(expected_ga, 2),
                    win_prob=round(win_p, 3),
                    shutout_prob=round(shutout_p, 3),
                    points_if_starting=round(points_if_starting, 2),
                    points=round(points, 2),
                )
            )

        # Observed form, shown for judgement like the skaters'.
        def _rate(row: dict) -> float:
            gp = float((row or {}).get("gamesPlayed") or 0)
            if gp <= 0:
                return 0.0
            return round(goalie_fantasy_points(
                wins=row.get("wins", 0) or 0,
                goals_against=row.get("goalsAgainst", 0) or 0,
                saves=row.get("saves", 0) or 0,
                shutouts=row.get("shutouts", 0) or 0,
            ) / gp, 2)

        proj.recent_fp_per_game = _rate(wrow)
        proj.season_fp_per_game = _rate(srow)

        proj.week_points = round(total, 2)
        proj.expected_starts = round(expected_starts, 2)
        proj.per_game_points = round(total / len(games), 2) if games else 0.0
        # Keep value: what one start is worth, times how often he starts.
        if proj.games:
            per_start = sum(g.points_if_starting for g in proj.games) / len(proj.games)
            proj.keep_per_game = round(per_start * recent_share, 2)
        proj.confidence = round(
            min(1.0, 0.3 + 0.7 * min(1.0, starts / 12.0)) * (1.0 if depth else 0.7), 2
        )

        # Short, like the skater note: the columns carry games and depth, so
        # this says only what they cannot.
        depth_label = (
            "starter" if depth == 1 else "backup" if depth else "unlisted"
        )
        proj.why = (
            f"{depth_label}, {proj.expected_starts:.1f} starts, "
            f"{save_pct:.3f} sv%"
        )
        out[pid] = proj

    return out


def appearance_shortfall(
    roster_goalies: list[GoalieProjection], minimum: int
) -> tuple[float, bool]:
    """
    Your league voids ALL goaltending points if your goalies make fewer than
    `minimum` appearances in a week. That makes the appearance count a hard
    constraint rather than something to optimise, and it is worth shouting
    about: a 2-appearance week does not cost you a little, it costs you every
    goalie point you earned.

    Returns (expected_appearances, is_at_risk).
    """
    expected = sum(g.expected_starts for g in roster_goalies)
    return round(expected, 2), expected < minimum


def explain_goalie(proj: GoalieProjection) -> str:
    lines = [
        f"{proj.name}  ({proj.team}, G)",
        "=" * 64,
        f"  Depth chart : {proj.depth or 'unlisted'}",
        f"  Save pct    : {proj.save_pct:.3f} (regressed toward league average)",
        f"  Expected    : {proj.expected_starts:.2f} start(s) this week",
        "",
        "  Games remaining this week:",
    ]
    for g in proj.games:
        lines.append(
            f"    {g.game.date}  {g.game.label:<8} "
            f"start {g.start_prob:>4.0%}  "
            f"shots {g.expected_shots:>4.1f}  "
            f"sv {g.expected_saves:>4.1f}  ga {g.expected_goals_against:>4.2f}  "
            f"win {g.win_prob:>4.0%}"
            f"{'  [b2b]' if g.game.is_back_to_back else ''}"
        )
        lines.append(
            f"        {g.points_if_starting:5.2f} pts if he starts "
            f"-> {g.points:5.2f} expected"
        )
    if not proj.games:
        lines.append("    (none -- week is over for this team)")
    lines += [
        "",
        f"  WEEK TOTAL  : {proj.week_points:.2f} fantasy points",
        f"  Confidence  : {proj.confidence:.0%}",
    ]
    return "\n".join(lines)


def _demo() -> None:  # pragma: no cover - manual smoke check
    projs = project_goalies()
    ranked = sorted(projs.values(), key=lambda p: -p.week_points)[:20]
    print(f"{'GOALIE':<24} {'TM':<4} {'D':<3} {'GP':>3} {'STARTS':>7} "
          f"{'SV%':>6} {'WEEK':>7}  WHY")
    print("-" * 110)
    for p in ranked:
        print(
            f"{p.name:<24} {p.team:<4} {str(p.depth or '-'):<3} {p.games_left:>3} "
            f"{p.expected_starts:>7.2f} {p.save_pct:>6.3f} {p.week_points:>7.2f}  "
            f"{p.why[:44]}"
        )


if __name__ == "__main__":
    _demo()
