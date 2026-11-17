"""
Score and rank waiver wire pickups using actual league fantasy point values.

Skater FP/game = G×6 + A×4 + (±)×2 + PPP×2 + SOG×0.9 + BLK×1
Goalie  FP/game = W×5 + GA×(-3) + SV×0.6 + SHO×5

Final score = weighted_avg_fppg × games_multiplier × (1 + trend_bonus)
"""

from __future__ import annotations
import pandas as pd
import data.nhl_client as nhl
import config

_GOALIE_POSITIONS = {"G", "GK"}


def _is_goalie(player: dict) -> bool:
    return bool(set(player.get("positions", [])) & _GOALIE_POSITIONS)


def _fppg(stats: dict) -> float:
    g = stats["games"]
    return stats["fantasy_pts"] / g if g > 0 else 0.0


def score_player(player: dict) -> dict:
    nhl_id = player.get("nhl_id", 0)
    team = player.get("team_abbrev", "")
    goalie = _is_goalie(player)
    stat_fn = nhl.goalie_stats_recent if goalie else nhl.player_stats_recent

    s7 = stat_fn(nhl_id, 7)
    s14 = stat_fn(nhl_id, 14)
    s30 = stat_fn(nhl_id, 30)

    fppg7 = _fppg(s7)
    fppg14 = _fppg(s14)
    fppg30 = _fppg(s30)

    base = (
        config.FPPG_WEIGHT_7D * fppg7
        + config.FPPG_WEIGHT_14D * fppg14
        + config.FPPG_WEIGHT_30D * fppg30
    )

    games_wk = nhl.current_week_games(team) if team else 2
    multiplier = config.GAMES_WEEK_MULTIPLIERS.get(min(games_wk, 4), 1.0)

    trend_bonus = 0.0
    if fppg30 > 0 and fppg7 >= fppg30 * config.TREND_BONUS_THRESHOLD:
        trend_bonus = config.TREND_BONUS_VALUE

    final_score = base * multiplier * (1 + trend_bonus)

    # Human-readable reason
    reasons = []
    if games_wk >= 3:
        reasons.append(f"{games_wk} games this week")
    if trend_bonus > 0:
        reasons.append(f"hot ({fppg7:.1f} FP/gm last 7d)")
    if not reasons:
        reasons.append(f"{fppg14:.1f} FP/gm last 14d")

    row = {
        **player,
        "fppg_7d": round(fppg7, 1),
        "fppg_14d": round(fppg14, 1),
        "fppg_30d": round(fppg30, 1),
        "fp_14d": s14["fantasy_pts"],
        "games_this_week": games_wk,
        "score": round(final_score, 2),
        "reason": ", ".join(reasons),
        "is_goalie": goalie,
    }

    # Attach per-stat detail columns (useful in the table)
    if goalie:
        row.update({
            "W_14d": s14.get("wins", 0),
            "GA_14d": s14.get("goals_against", 0),
            "SV_14d": s14.get("saves", 0),
            "SHO_14d": s14.get("shutouts", 0),
        })
    else:
        row.update({
            "G_14d": s14.get("goals", 0),
            "A_14d": s14.get("assists", 0),
            "PPP_14d": s14.get("ppp", 0),
            "SOG_14d": s14.get("sog", 0),
            "BLK_14d": s14.get("blocks", 0),
            "PM_14d": s14.get("plus_minus", 0),
        })

    return row


def rank_waivers(
    players: list[dict],
    position_filter: str = "ALL",
    exclude_injured: bool = True,
) -> pd.DataFrame:
    """Score all waiver players and return a ranked DataFrame."""
    scored = []
    for p in players:
        if position_filter != "ALL":
            if position_filter not in p.get("positions", []):
                continue

        if exclude_injured and p.get("status") in {"IR", "IR-LTI", "Injured Reserve"}:
            continue

        try:
            scored.append(score_player(p))
        except Exception:
            continue

    df = pd.DataFrame(scored)
    if df.empty:
        return df

    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    df.index += 1
    return df
