"""
The model: projected fantasy points for the rest of the current week.

The core idea is to separate **rate** from **opportunity**, because they move
independently and conflating them is what makes naive rankings wrong.

    points = how good he is per minute  x  how many minutes he'll get
                                        x  how many games are left
                                        x  how soft the opponents are

The old ranker used points-per-game blended over 7/14/30 days. That conflates
everything: a player whose ice time just doubled looks identical to one who got
lucky, and a player on a four-game week looks the same as one on a two-game
week until you multiply at the end. Splitting the terms lets each be estimated
from the data that actually informs it:

* **Rate** is estimated per 60 minutes, separately for power play and
  non-power-play, then shrunk toward a positional prior. Separating the two
  matters enormously: power-play minutes are worth roughly three times
  even-strength minutes in fantasy terms, so a player whose PP time doubles is
  a different player, while the same change at even strength barely registers.
* **Opportunity** comes from Daily Faceoff's current line and unit, pulled
  toward measured trailing ice time. When the two disagree we trust the listing,
  because that disagreement *is* the opportunity.
* **Schedule** is a straight count of games left in the Monday-Sunday week.
* **Matchup** is a gentle per-category multiplier.

Every projection keeps its own component breakdown, so any number in the UI can
be opened up and audited rather than taken on faith.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import math
from pathlib import Path

import yaml

import data.names as names
import data.nhl_stats_rest as nhl_rest
import features.matchups as matchups
import features.roles as roles_mod
from features.roles import PlayerRole
from features.scoring import skater_fantasy_points

WEIGHTS_PATH = Path(__file__).parent.parent / "models" / "weights.yaml"

_SECONDS_PER_HOUR = 3600.0


def load_weights(path: Path | None = None) -> dict:
    return yaml.safe_load((path or WEIGHTS_PATH).read_text())


# ---------------------------------------------------------------------------
# Rate estimation
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RateSplit:
    """Fantasy points per 60 minutes, split by situation."""

    pp_per60: float = 0.0
    nonpp_per60: float = 0.0
    # Component rates let us apply per-category matchup multipliers.
    nonpp_scoring_per60: float = 0.0
    nonpp_shots_per60: float = 0.0
    nonpp_blocks_per60: float = 0.0
    pp_scoring_per60: float = 0.0
    pp_shots_per60: float = 0.0
    sample_hours: float = 0.0


def _bucket_stats(
    summary: dict, pp: dict, realtime: dict
) -> tuple[dict, dict, float, float]:
    """
    Split a player's counting stats into power-play and everything-else, and
    return the ice time that produced each.

    The NHL gives us power-play totals directly, so the non-power-play bucket
    is simply the remainder. Blocks and hits effectively never happen on the
    power play, so they all land in the non-PP bucket.
    """
    games = float(summary.get("gamesPlayed") or 0)

    pp_goals = float(pp.get("ppGoals") or 0)
    pp_points = float(pp.get("ppPoints") or 0)
    pp_assists = max(0.0, pp_points - pp_goals)
    pp_shots = float(pp.get("ppShots") or 0)

    total_goals = float(summary.get("goals") or 0)
    total_assists = float(summary.get("assists") or 0)
    total_shots = float(summary.get("shots") or 0)
    sh_points = float(summary.get("shPoints") or 0)
    blocks = float(realtime.get("blockedShots") or 0)

    pp_bucket = {
        "goals": pp_goals,
        "assists": pp_assists,
        "ppp": pp_points,
        "shp": 0.0,
        "sog": pp_shots,
        "blocks": 0.0,
    }
    nonpp_bucket = {
        "goals": max(0.0, total_goals - pp_goals),
        "assists": max(0.0, total_assists - pp_assists),
        "ppp": 0.0,
        "shp": sh_points,
        "sog": max(0.0, total_shots - pp_shots),
        "blocks": blocks,
    }

    pp_toi = float(pp.get("ppTimeOnIcePerGame") or 0) * games
    total_toi = float(summary.get("timeOnIcePerGame") or 0) * games
    nonpp_toi = max(0.0, total_toi - pp_toi)
    return pp_bucket, nonpp_bucket, pp_toi, nonpp_toi


def _per60(bucket: dict, seconds: float) -> dict:
    """Convert counting stats over `seconds` of ice time into per-60 rates."""
    if seconds <= 0:
        return {k: 0.0 for k in bucket}
    hours = seconds / _SECONDS_PER_HOUR
    return {k: v / hours for k, v in bucket.items()}


def _blend(estimates: list[tuple[dict, float]]) -> dict:
    """Weighted blend of several per-60 rate dicts. Weights are in hours."""
    keys: set[str] = set()
    for rates, _ in estimates:
        keys |= set(rates)
    total_weight = sum(w for _, w in estimates)
    if total_weight <= 0:
        return {k: 0.0 for k in keys}
    return {
        k: sum(rates.get(k, 0.0) * w for rates, w in estimates) / total_weight
        for k in keys
    }


def build_positional_priors(
    season_summary: dict[int, dict],
    season_pp: dict[int, dict],
    season_realtime: dict[int, dict],
    roles: dict[int, PlayerRole],
) -> dict[str, dict]:
    """
    League-average per-60 rates by position group, computed from the data
    itself rather than hard-coded. These are what small samples regress toward.

    Separate priors for forwards and defencemen, and separately for the
    power-play bucket, because a defenceman's non-PP profile (fewer points,
    many more blocks) is nothing like a forward's.
    """
    groups: dict[str, dict[str, list]] = {}

    for pid, summary in season_summary.items():
        role = roles.get(pid)
        if not role:
            continue
        games = float(summary.get("gamesPlayed") or 0)
        if games < 5:
            continue  # too little to inform a prior

        is_d = (role.position or "").lower() in ("d", "ld", "rd")
        group = "D" if is_d else "F"

        pp_bucket, nonpp_bucket, pp_toi, nonpp_toi = _bucket_stats(
            summary, season_pp.get(pid, {}), season_realtime.get(pid, {})
        )
        entry = groups.setdefault(
            group, {"pp": [], "nonpp": [], "pp_toi": 0.0, "nonpp_toi": 0.0}
        )
        entry["pp"].append((pp_bucket, pp_toi))
        entry["nonpp"].append((nonpp_bucket, nonpp_toi))
        entry["pp_toi"] += pp_toi
        entry["nonpp_toi"] += nonpp_toi

    priors: dict[str, dict] = {}
    for group, entry in groups.items():
        pp_totals: dict[str, float] = {}
        nonpp_totals: dict[str, float] = {}
        for bucket, _ in entry["pp"]:
            for k, v in bucket.items():
                pp_totals[k] = pp_totals.get(k, 0.0) + v
        for bucket, _ in entry["nonpp"]:
            for k, v in bucket.items():
                nonpp_totals[k] = nonpp_totals.get(k, 0.0) + v
        priors[group] = {
            "pp": _per60(pp_totals, entry["pp_toi"]),
            "nonpp": _per60(nonpp_totals, entry["nonpp_toi"]),
        }
    return priors


def estimate_rates(
    pid: int,
    role: PlayerRole,
    *,
    window: tuple[dict, dict, dict],
    season: tuple[dict, dict, dict],
    priors: dict[str, dict],
    weights: dict,
) -> RateSplit:
    """Blend window, season and prior into one rate estimate for this player."""
    w_summary, w_pp, w_realtime = window
    s_summary, s_pp, s_realtime = season

    cfg = weights["rates"]
    is_d = (role.position or "").lower() in ("d", "ld", "rd")
    prior = priors.get("D" if is_d else "F", {"pp": {}, "nonpp": {}})

    w_pp_bucket, w_nonpp_bucket, w_pp_toi, w_nonpp_toi = _bucket_stats(
        w_summary.get(pid, {}), w_pp.get(pid, {}), w_realtime.get(pid, {})
    )
    s_pp_bucket, s_nonpp_bucket, s_pp_toi, s_nonpp_toi = _bucket_stats(
        s_summary.get(pid, {}), s_pp.get(pid, {}), s_realtime.get(pid, {})
    )

    prior_hours = float(cfg["prior_hours"])
    recent_mult = float(cfg["recent_multiplier"])
    season_mult = float(cfg["season_multiplier"])

    # Power-play bucket
    pp_rates = _blend([
        (_per60(w_pp_bucket, w_pp_toi), (w_pp_toi / _SECONDS_PER_HOUR) * recent_mult),
        (_per60(s_pp_bucket, s_pp_toi), (s_pp_toi / _SECONDS_PER_HOUR) * season_mult),
        (prior.get("pp", {}), prior_hours),
    ])
    # Non-power-play bucket
    nonpp_rates = _blend([
        (_per60(w_nonpp_bucket, w_nonpp_toi),
         (w_nonpp_toi / _SECONDS_PER_HOUR) * recent_mult),
        (_per60(s_nonpp_bucket, s_nonpp_toi),
         (s_nonpp_toi / _SECONDS_PER_HOUR) * season_mult),
        (prior.get("nonpp", {}), prior_hours),
    ])

    def fp(rates: dict) -> float:
        return skater_fantasy_points(**{
            k: rates.get(k, 0.0)
            for k in ("goals", "assists", "ppp", "shp", "sog", "blocks")
        })

    def scoring_only(rates: dict) -> float:
        return skater_fantasy_points(
            goals=rates.get("goals", 0.0),
            assists=rates.get("assists", 0.0),
            ppp=rates.get("ppp", 0.0),
            shp=rates.get("shp", 0.0),
        )

    return RateSplit(
        pp_per60=fp(pp_rates),
        nonpp_per60=fp(nonpp_rates),
        pp_scoring_per60=scoring_only(pp_rates),
        pp_shots_per60=skater_fantasy_points(sog=pp_rates.get("sog", 0.0)),
        nonpp_scoring_per60=scoring_only(nonpp_rates),
        nonpp_shots_per60=skater_fantasy_points(sog=nonpp_rates.get("sog", 0.0)),
        nonpp_blocks_per60=skater_fantasy_points(blocks=nonpp_rates.get("blocks", 0.0)),
        sample_hours=(w_pp_toi + w_nonpp_toi) / _SECONDS_PER_HOUR,
    )


# ---------------------------------------------------------------------------
# Expected ice time
# ---------------------------------------------------------------------------

def expected_ice_time(
    role: PlayerRole, weights: dict, team_pp_seconds: float
) -> tuple[float, float]:
    """
    Expected (non-PP, PP) seconds in the next game.

    This is where a promotion turns into points. If Daily Faceoff lists a player
    on PP1 but his trailing share is still 20%, the measured average is
    describing a job he no longer has, so we weight the role baseline heavily.
    """
    cfg = weights["ice_time"]
    changed = bool(role.role_change)
    blend = float(
        cfg["line_baseline_weight_on_change"] if changed
        else cfg["line_baseline_weight"]
    )

    # --- even strength ---
    measured_es = role.es_toi_per_game
    is_d = (role.position or "").lower() in ("d", "ld", "rd")
    baseline_table = (
        cfg["defence_es_baseline"] if is_d else cfg["forward_es_baseline"]
    )
    if role.line_rank and role.line_rank in baseline_table:
        baseline_es = float(baseline_table[role.line_rank])
    else:
        baseline_es = measured_es or 0.0

    if measured_es <= 0:
        es_toi = baseline_es
    else:
        es_toi = (1 - blend) * measured_es + blend * baseline_es
    es_toi = min(es_toi, float(cfg["max_es_toi"]))

    # --- power play ---
    pp_baselines = cfg["pp_share_baseline"]
    unit = role.pp_unit
    measured_share = role.pp_share or 0.0
    if unit in (1, 2):
        baseline_share = float(pp_baselines[unit])
        share = (1 - blend) * measured_share + blend * baseline_share
    else:
        # Not on a listed unit: trust what he has actually been getting, and
        # fade it, since being unlisted usually means it is going away.
        share = measured_share * 0.6

    pp_toi = min(share * team_pp_seconds, float(cfg["max_pp_toi"]))
    return max(0.0, es_toi), max(0.0, pp_toi)


def availability_prob(role: PlayerRole, weights: dict) -> float:
    """Probability he dresses for a given game."""
    cfg = weights["availability"]
    status = (role.injury_status or "").lower()

    if status in ("out", "ir", "ltir", "injured reserve", "suspended"):
        return float(cfg["out"])
    if role.game_time_decision or status in ("gtd", "game time decision"):
        prob = float(cfg["game_time_decision"])
    elif status in ("dtd", "day to day", "day-to-day", "questionable"):
        prob = float(cfg["day_to_day"])
    elif role.line_rank is None and role.pp_unit is None:
        # Not listed anywhere in the line-up.
        prob = float(cfg["unlisted"])
    else:
        prob = float(cfg["healthy"])

    if role.listing_age_hours > float(cfg["stale_listing_hours"]):
        prob *= float(cfg["stale_listing_penalty"])
    return max(0.0, min(1.0, prob))


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class GameProjection:
    game: matchups.Game
    points: float
    play_prob: float
    opponent_grade: str


@dataclasses.dataclass
class PlayerProjection:
    nhl_id: int
    name: str
    team: str
    position: str
    role: PlayerRole
    games: list[GameProjection] = dataclasses.field(default_factory=list)
    week_points: float = 0.0
    per_game_points: float = 0.0
    es_toi: float = 0.0
    pp_toi: float = 0.0
    rates: RateSplit | None = None
    confidence: float = 0.0
    why: str = ""
    # Per-game value with the schedule stripped out. Lives on the projection so
    # that skaters and goalies, which are modelled completely differently, can
    # still be compared on a single number when deciding who to drop.
    keep_per_game: float = 0.0

    @property
    def is_goalie(self) -> bool:
        return False

    @property
    def games_left(self) -> int:
        return len(self.games)

    @property
    def schedule_label(self) -> str:
        return ", ".join(g.game.label for g in self.games) or "-"


def _confidence(role: PlayerRole, rates: RateSplit, weights: dict) -> float:
    cfg = weights["confidence"]
    sample = min(1.0, role.games_window / float(cfg["full_sample_games"]))
    fresh = 1.0 if role.listing_age_hours <= float(cfg["fresh_listing_hours"]) else 0.7
    if role.line_rank is None and role.pp_unit is None:
        fresh *= 0.6
    return round(max(0.0, min(1.0, 0.35 + 0.65 * sample)) * fresh, 2)


def _explain(
    proj: PlayerProjection, role: PlayerRole, profiles: dict[str, matchups.OpponentProfile]
) -> str:
    """
    A short note saying only what the columns cannot.

    Games left, line, power-play unit, PP share and ice time all have their
    own columns, so repeating them here is noise that pushes the one genuinely
    new fact off the edge of the screen. This keeps the exceptions: a role
    that just changed, a schedule that is unusually soft or hard, and anything
    that threatens availability. Everything else stays in the Player detail
    tab, where there is room for it.
    """
    bits: list[str] = []

    # Availability first -- it overrides everything else.
    if role.injury_status:
        bits.append(role.injury_status.upper())
    elif role.game_time_decision:
        bits.append("GTD")

    if role.role_change == "promoted":
        bits.append(f"promoted, PP share {role.pp_share:.0%}")
    elif role.role_change == "demoted":
        bits.append("losing PP role")
    elif role.role_change == "unproven":
        bits.append("thin sample")

    grades = [g.opponent_grade for g in proj.games]
    easy = sum(1 for g in grades if g in ("A", "B"))
    hard = sum(1 for g in grades if g in ("D", "F"))
    if easy and easy > hard:
        bits.append(f"{easy} soft opp")
    elif hard and hard > easy:
        bits.append(f"{hard} tough opp")

    return ", ".join(bits)


def project_player(
    pid: int,
    role: PlayerRole,
    *,
    rates: RateSplit,
    profiles: dict[str, matchups.OpponentProfile],
    team_pp_seconds: float,
    weights: dict,
    today: _dt.date | None = None,
) -> PlayerProjection:
    """Project one skater for the remainder of the fantasy week."""
    es_toi, pp_toi = expected_ice_time(role, weights, team_pp_seconds)
    play_prob = availability_prob(role, weights)

    proj = PlayerProjection(
        nhl_id=pid,
        name=role.name,
        team=role.team,
        position=role.position,
        role=role,
        es_toi=es_toi,
        pp_toi=pp_toi,
        rates=rates,
    )

    games = matchups.remaining_games(role.team, today) if role.team else []
    es_hours = es_toi / _SECONDS_PER_HOUR
    pp_hours = pp_toi / _SECONDS_PER_HOUR

    total = 0.0
    for game in games:
        profile = profiles.get(game.opponent)
        if profile:
            goal_m, shot_m, pp_m, blk_m = (
                profile.goal_mult, profile.shot_mult,
                profile.pp_mult, profile.block_mult,
            )
            grade = profile.grade
        else:
            goal_m = shot_m = pp_m = blk_m = 1.0
            grade = "C"

        sit = matchups.situational_mult(game)

        nonpp_points = (
            rates.nonpp_scoring_per60 * goal_m
            + rates.nonpp_shots_per60 * shot_m
            + rates.nonpp_blocks_per60 * blk_m
        ) * es_hours
        pp_points = (
            rates.pp_scoring_per60 * pp_m
            + rates.pp_shots_per60 * shot_m
        ) * pp_hours

        expected = (nonpp_points + pp_points) * sit * play_prob
        total += expected
        proj.games.append(
            GameProjection(
                game=game, points=round(expected, 2),
                play_prob=play_prob, opponent_grade=grade,
            )
        )

    proj.week_points = round(total, 2)
    proj.per_game_points = round(total / len(games), 2) if games else 0.0
    # Neutral-night value: rate x expected minutes, with no schedule or matchup
    # in it. This is the number that should drive a drop decision.
    proj.keep_per_game = round(
        rates.nonpp_per60 * es_hours + rates.pp_per60 * pp_hours, 2
    )
    proj.confidence = _confidence(role, rates, weights)
    proj.why = _explain(proj, role, profiles)
    return proj


def project_all(
    *,
    today: _dt.date | None = None,
    weights: dict | None = None,
    progress: bool = False,
) -> dict[int, PlayerProjection]:
    """Project every skater the NHL has data on. Returns {nhl_id: projection}."""
    weights = weights or load_weights()
    window_days = int(weights.get("window_days", 14))

    roles = roles_mod.build_roles(window_days=window_days, progress=progress)

    if progress:
        print("Loading season splits and opponent profiles...")
    season = (
        nhl_rest.skater_summary_season(),
        nhl_rest.skater_powerplay_season(),
        nhl_rest.skater_realtime_season(),
    )
    window = (
        nhl_rest.skater_summary_window(window_days),
        nhl_rest.skater_powerplay_window(window_days),
        nhl_rest.skater_realtime_window(window_days),
    )
    profiles = matchups.build_opponent_profiles()
    team_pp = nhl_rest.team_powerplay_season()
    priors = build_positional_priors(season[0], season[1], season[2], roles)

    if progress:
        print(f"Projecting {len(roles)} skaters...")

    out: dict[int, PlayerProjection] = {}
    for pid, role in roles.items():
        team_pp_seconds = float(
            (team_pp.get(role.team, {}) or {}).get("ppTimeOnIcePerGame") or 250.0
        )
        rates = estimate_rates(
            pid, role, window=window, season=season, priors=priors, weights=weights
        )
        out[pid] = project_player(
            pid, role, rates=rates, profiles=profiles,
            team_pp_seconds=team_pp_seconds, weights=weights, today=today,
        )
    return out


def explain_player(proj: PlayerProjection) -> str:
    """A full audit trail for one projection -- every term, so nothing is magic."""
    role = proj.role
    r = proj.rates
    lines = [
        f"{proj.name}  ({proj.team}, {names.display_position(proj.position)})",
        "=" * 64,
        f"  Role        : {role.role_label}"
        + (f"   [{role.role_change.upper()}]" if role.role_change else ""),
        f"  Role note   : {role.role_note or '-'}",
        f"  Measured    : {role.games_window} games in window, "
        f"TOI {nhl_rest.seconds_to_mmss(role.toi_per_game)}/gp "
        f"(ES {nhl_rest.seconds_to_mmss(role.es_toi_per_game)}, "
        f"PP {nhl_rest.seconds_to_mmss(role.pp_toi_per_game)}, "
        f"share {role.pp_share:.0%})",
        f"  Expected    : ES {nhl_rest.seconds_to_mmss(proj.es_toi)}, "
        f"PP {nhl_rest.seconds_to_mmss(proj.pp_toi)} per game",
        "",
        "  Rates (fantasy points per 60 min, shrunk to positional prior):",
        f"    non-PP    : {r.nonpp_per60:6.2f}  "
        f"(scoring {r.nonpp_scoring_per60:.2f}, shots {r.nonpp_shots_per60:.2f}, "
        f"blocks {r.nonpp_blocks_per60:.2f})",
        f"    power play: {r.pp_per60:6.2f}  "
        f"(scoring {r.pp_scoring_per60:.2f}, shots {r.pp_shots_per60:.2f})",
        f"    sample    : {r.sample_hours:.1f} hours of ice time in window",
        "",
        f"  Availability: {availability_prob(role, load_weights()):.0%} per game",
        "",
        "  Games remaining this week:",
    ]
    for g in proj.games:
        lines.append(
            f"    {g.game.date}  {g.game.label:<8} "
            f"opp grade {g.opponent_grade}"
            f"{'  [b2b]' if g.game.is_back_to_back else ''}"
            f"   -> {g.points:5.2f} pts"
        )
    if not proj.games:
        lines.append("    (none -- week is over for this team)")
    lines += [
        "",
        f"  WEEK TOTAL  : {proj.week_points:.2f} fantasy points "
        f"({proj.per_game_points:.2f}/game)",
        f"  Confidence  : {proj.confidence:.0%}",
        f"  Summary     : {proj.why}",
    ]
    return "\n".join(lines)


def _demo() -> None:  # pragma: no cover - manual smoke check
    import sys

    args = [a for a in sys.argv[1:]]
    projections = project_all(progress=True)

    if args:
        query = " ".join(args).lower()
        hits = [p for p in projections.values() if query in p.name.lower()]
        if not hits:
            print(f"No player matching {query!r}")
            return
        for p in sorted(hits, key=lambda x: -x.week_points)[:3]:
            print()
            print(explain_player(p))
        return

    ranked = sorted(projections.values(), key=lambda p: -p.week_points)[:30]
    print(f"\n{'PLAYER':<24} {'TM':<4} {'POS':<4} {'GP':>3} {'ROLE':<9} "
          f"{'WEEK':>7} {'/GM':>6} {'CONF':>5}  WHY")
    print("-" * 130)
    for p in ranked:
        print(
            f"{p.name:<24} {p.team:<4} {names.display_position(p.position):<4} {p.games_left:>3} "
            f"{p.role.role_label:<9} {p.week_points:>7.2f} {p.per_game_points:>6.2f} "
            f"{p.confidence:>5.0%}  {p.why[:60]}"
        )


if __name__ == "__main__":
    _demo()
