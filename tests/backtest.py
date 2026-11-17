"""
Does the model actually beat the simple approach?

The honest test of a projection is not whether it looks clever, it is whether
picking its top players would have earned you more points than picking the
obvious ones. So we replay past weeks:

  1. Rewind to a past Monday and build projections using only what was
     knowable then -- stats up to the Sunday before, and that week's schedule.
  2. Rank every skater.
  3. Look up what they actually scored that week.
  4. Compare against a baseline that mimics the old ranker:
     points-per-game so far, times games in the week.

Headline metric is **mean actual points of the top 20 picks**, because that is
the decision you actually make -- you are not forecasting the whole league,
you are choosing a handful of players. Rank correlation is reported too.

One honest limitation, stated plainly: Daily Faceoff's line combinations are
only available as they are *now*, not as they were six weeks ago. So the
backtest runs the model without the line/PP-unit overlay, on NHL data alone.
That means it measures the rate/ice-time/schedule/matchup machinery but
**not** the promotion-detection signal, which is the part most likely to add
value. Read the result as a floor on the model's edge, not a ceiling.

    python -m tests.backtest
"""

from __future__ import annotations

import datetime as _dt
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.nhl_stats_rest as nhl_rest  # noqa: E402
import features.matchups as matchups  # noqa: E402
import features.projection as projection  # noqa: E402
import features.streaks as streaks_mod  # noqa: E402
from features.roles import PlayerRole  # noqa: E402
from features.scoring import skater_fantasy_points  # noqa: E402

TOP_N = 20
MIN_GAMES_PLAYED = 5


def _spearman(pairs: list[tuple[float, float]]) -> float:
    """Rank correlation without pulling in scipy."""
    if len(pairs) < 3:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    def rank(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (
        sum((a - mx) ** 2 for a in rx) ** 0.5
        * sum((b - my) ** 2 for b in ry) ** 0.5
    )
    return num / den if den else 0.0


def actual_week_points(week_start: _dt.date, week_end: _dt.date) -> dict[int, float]:
    """What every skater really scored in that week."""
    summary = nhl_rest._fetch(
        "skater/summary", nhl_rest._date_exp(week_start, week_end),
        aggregate=True, ttl=86400,
    )
    realtime = nhl_rest._fetch(
        "skater/realtime", nhl_rest._date_exp(week_start, week_end),
        aggregate=True, ttl=86400,
    )
    rt_index = {int(r["playerId"]): r for r in realtime if r.get("playerId")}

    out: dict[int, float] = {}
    for row in summary:
        pid = int(row.get("playerId", 0))
        if not pid:
            continue
        rt = rt_index.get(pid, {})
        out[pid] = skater_fantasy_points(
            goals=row.get("goals", 0) or 0,
            assists=row.get("assists", 0) or 0,
            ppp=row.get("ppPoints", 0) or 0,
            shp=row.get("shPoints", 0) or 0,
            sog=row.get("shots", 0) or 0,
            blocks=rt.get("blockedShots", 0) or 0,
        )
    return out


def build_historical_roles(
    as_of: _dt.date, window_days: int, teams_by_player: dict[int, str]
) -> dict[int, PlayerRole]:
    """
    Roles as they could be measured on `as_of`, using NHL data only.
    No Daily Faceoff overlay -- see the module docstring.
    """
    cutoff = as_of - _dt.timedelta(days=1)
    start = cutoff - _dt.timedelta(days=window_days)

    summary = {
        int(r["playerId"]): r
        for r in nhl_rest._fetch(
            "skater/summary", nhl_rest._date_exp(start, cutoff),
            aggregate=True, ttl=86400,
        )
        if r.get("playerId")
    }
    pp = {
        int(r["playerId"]): r
        for r in nhl_rest._fetch(
            "skater/powerplay", nhl_rest._date_exp(start, cutoff),
            aggregate=True, ttl=86400,
        )
        if r.get("playerId")
    }
    pk = {
        int(r["playerId"]): r
        for r in nhl_rest._fetch(
            "skater/penaltykill", nhl_rest._date_exp(start, cutoff),
            aggregate=True, ttl=86400,
        )
        if r.get("playerId")
    }

    roles: dict[int, PlayerRole] = {}
    for pid, row in summary.items():
        share = float(pp.get(pid, {}).get("ppTimeOnIcePctPerGame") or 0.0)
        roles[pid] = PlayerRole(
            nhl_id=pid,
            name=row.get("skaterFullName", ""),
            team=teams_by_player.get(pid, ""),
            position=(row.get("positionCode") or "").lower(),
            games_window=int(row.get("gamesPlayed") or 0),
            toi_per_game=float(row.get("timeOnIcePerGame") or 0.0),
            pp_toi_per_game=float(pp.get(pid, {}).get("ppTimeOnIcePerGame") or 0.0),
            sh_toi_per_game=float(pk.get(pid, {}).get("shTimeOnIcePerGame") or 0.0),
            pp_share=share,
            # Listed unit is unknown historically, so the implied unit is all
            # we have. This is precisely the information the live model adds.
            pp_unit_implied=(1 if share >= 0.55 else 2 if share >= 0.25 else None),
            listing_age_hours=0.0,
        )
    return roles


def baseline_scores(
    as_of: _dt.date,
    week_end: _dt.date,
    teams_by_player: dict[int, str],
) -> dict[int, float]:
    """
    The old ranker: season points-per-game to date, times games this week.
    No ice time, no role, no opponent.
    """
    season_start = _dt.date(as_of.year if as_of.month >= 9 else as_of.year - 1, 9, 1)
    cutoff = as_of - _dt.timedelta(days=1)

    summary = nhl_rest._fetch(
        "skater/summary", nhl_rest._date_exp(season_start, cutoff),
        aggregate=True, ttl=86400,
    )
    realtime = {
        int(r["playerId"]): r
        for r in nhl_rest._fetch(
            "skater/realtime", nhl_rest._date_exp(season_start, cutoff),
            aggregate=True, ttl=86400,
        )
        if r.get("playerId")
    }

    games_cache: dict[str, int] = {}
    out: dict[int, float] = {}
    for row in summary:
        pid = int(row.get("playerId", 0))
        gp = float(row.get("gamesPlayed") or 0)
        if not pid or gp < MIN_GAMES_PLAYED:
            continue
        total = skater_fantasy_points(
            goals=row.get("goals", 0) or 0,
            assists=row.get("assists", 0) or 0,
            ppp=row.get("ppPoints", 0) or 0,
            shp=row.get("shPoints", 0) or 0,
            sog=row.get("shots", 0) or 0,
            blocks=realtime.get(pid, {}).get("blockedShots", 0) or 0,
        )
        fppg = total / gp
        team = teams_by_player.get(pid, "")
        if team not in games_cache:
            games_cache[team] = len(matchups.team_games(team, as_of, week_end)) if team else 0
        out[pid] = fppg * games_cache[team]
    return out


def model_scores(
    as_of: _dt.date,
    teams_by_player: dict[int, str],
    weights: dict,
) -> dict[int, float]:
    """Our model, restricted to what was knowable on `as_of`."""
    window_days = int(weights.get("window_days", 14))
    cutoff = as_of - _dt.timedelta(days=1)
    season_start = _dt.date(as_of.year if as_of.month >= 9 else as_of.year - 1, 9, 1)

    def fetch(report: str, start: _dt.date, end: _dt.date) -> dict[int, dict]:
        return {
            int(r["playerId"]): r
            for r in nhl_rest._fetch(
                report, nhl_rest._date_exp(start, end), aggregate=True, ttl=86400
            )
            if r.get("playerId")
        }

    window = (
        fetch("skater/summary", cutoff - _dt.timedelta(days=window_days), cutoff),
        fetch("skater/powerplay", cutoff - _dt.timedelta(days=window_days), cutoff),
        fetch("skater/realtime", cutoff - _dt.timedelta(days=window_days), cutoff),
    )
    season = (
        fetch("skater/summary", season_start, cutoff),
        fetch("skater/powerplay", season_start, cutoff),
        fetch("skater/realtime", season_start, cutoff),
    )

    roles = build_historical_roles(as_of, window_days, teams_by_player)
    priors = projection.build_positional_priors(season[0], season[1], season[2], roles)
    profiles = matchups.build_opponent_profiles()
    team_pp = nhl_rest.team_powerplay_season()

    # Streak state as it stood on `as_of`. Only computed when the bonus is
    # switched on, since it costs a game-log fetch per player on a cold cache.
    streak_info: dict = {}
    if float((weights.get("streaks") or {}).get("scale", 0.0)) > 0:
        streak_info = streaks_mod.streaks_for(
            list(roles), as_of=as_of,
            positions={pid: r.position for pid, r in roles.items()},
        )

    out: dict[int, float] = {}
    for pid, role in roles.items():
        if role.games_window < 2:
            continue
        team_pp_seconds = float(
            (team_pp.get(role.team, {}) or {}).get("ppTimeOnIcePerGame") or 250.0
        )
        rates = projection.estimate_rates(
            pid, role, window=window, season=season, priors=priors, weights=weights
        )
        proj = projection.project_player(
            pid, role, rates=rates, profiles=profiles,
            team_pp_seconds=team_pp_seconds, weights=weights, today=as_of,
            streak=streak_info.get(pid), historical=True,
        )
        out[pid] = proj.week_points
    return out


def evaluate_week(
    week_start: _dt.date, teams_by_player: dict[int, str], weights: dict
) -> dict | None:
    week_end = week_start + _dt.timedelta(days=6)
    actual = actual_week_points(week_start, week_end)
    if not actual:
        return None

    model = model_scores(week_start, teams_by_player, weights)
    base = baseline_scores(week_start, week_end, teams_by_player)

    common = set(model) & set(base) & set(actual)
    if len(common) < 50:
        return None

    def top_mean(scores: dict[int, float]) -> float:
        top = sorted(common, key=lambda p: -scores.get(p, 0))[:TOP_N]
        return statistics.mean(actual.get(p, 0.0) for p in top)

    model_pairs = [(model[p], actual[p]) for p in common]
    base_pairs = [(base[p], actual[p]) for p in common]

    return {
        "week": week_start,
        "n": len(common),
        "model_top": top_mean(model),
        "base_top": top_mean(base),
        "league_mean": statistics.mean(actual[p] for p in common),
        "model_rho": _spearman(model_pairs),
        "base_rho": _spearman(base_pairs),
        "model_mae": statistics.mean(abs(m - a) for m, a in model_pairs),
        "base_mae": statistics.mean(abs(b - a) for b, a in base_pairs),
    }


def main(weeks_back: int = 5) -> int:
    weights = projection.load_weights()
    today = _dt.date.today()
    this_monday = today - _dt.timedelta(days=today.weekday())

    # Team lookup from season data (players rarely change team; good enough).
    season_rows = nhl_rest.skater_summary_season()
    teams_by_player = {
        pid: (r.get("teamAbbrevs") or "").split(",")[-1].strip()
        for pid, r in season_rows.items()
    }

    print("=" * 92)
    print("BACKTEST -- model vs. the naive 'points per game x games this week' ranker")
    print("=" * 92)
    print(
        "\nNOTE: historical line/PP-unit listings are not available, so the model "
        "runs here\nwithout its promotion-detection signal. This is a FLOOR on its "
        "edge, not a ceiling.\n"
    )

    results = []
    for i in range(weeks_back, 0, -1):
        week_start = this_monday - _dt.timedelta(weeks=i)
        print(f"  evaluating week of {week_start}...", flush=True)
        res = evaluate_week(week_start, teams_by_player, weights)
        if res:
            results.append(res)

    if not results:
        print("\nNo weeks had enough data to evaluate.")
        return 1

    print()
    print(f"{'WEEK':<12} {'N':>5} {'MODEL top20':>12} {'BASE top20':>11} "
          f"{'LEAGUE avg':>11} {'MODEL rho':>10} {'BASE rho':>9}")
    print("-" * 92)
    for r in results:
        print(
            f"{str(r['week']):<12} {r['n']:>5} {r['model_top']:>12.1f} "
            f"{r['base_top']:>11.1f} {r['league_mean']:>11.1f} "
            f"{r['model_rho']:>10.3f} {r['base_rho']:>9.3f}"
        )

    m_top = statistics.mean(r["model_top"] for r in results)
    b_top = statistics.mean(r["base_top"] for r in results)
    m_rho = statistics.mean(r["model_rho"] for r in results)
    b_rho = statistics.mean(r["base_rho"] for r in results)
    m_mae = statistics.mean(r["model_mae"] for r in results)
    b_mae = statistics.mean(r["base_mae"] for r in results)

    print("-" * 92)
    print(f"{'AVERAGE':<12} {'':>5} {m_top:>12.1f} {b_top:>11.1f} "
          f"{statistics.mean(r['league_mean'] for r in results):>11.1f} "
          f"{m_rho:>10.3f} {b_rho:>9.3f}")

    print("\nSUMMARY")
    print("-" * 92)
    diff = m_top - b_top
    pct = (diff / b_top * 100) if b_top else 0
    print(
        f"  Top-{TOP_N} actual points : model {m_top:.1f} vs baseline {b_top:.1f}  "
        f"({diff:+.1f}, {pct:+.1f}%)"
    )
    print(f"  Rank correlation     : model {m_rho:.3f} vs baseline {b_rho:.3f}")
    print(f"  Mean absolute error  : model {m_mae:.1f} vs baseline {b_mae:.1f}")

    if diff > 0:
        print(
            f"\n  The model's picks beat the naive ranker by {diff:.1f} points per "
            f"top-{TOP_N} player per week."
        )
    else:
        print(
            "\n  The model did NOT beat the baseline on this sample. Tune "
            "models/weights.yaml\n  (start with rates.prior_hours and "
            "ice_time.line_baseline_weight) and re-run."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
