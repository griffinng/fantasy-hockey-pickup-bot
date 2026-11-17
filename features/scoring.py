"""
League scoring, in one place.

Every other module converts raw stats to fantasy points through here, so that
changing a league setting is a one-line edit in `config.py` rather than a hunt
through the codebase.

This league (H2H points, confirmed 2026-11-17):

    skaters   G 6.0   A 4.0   PPP 1.25   SHP 1.5   SOG 0.9   BLK 1.0
    goalies   W 5.0   GA -3.0   SV 0.6   SHO 5.0

Two things worth noticing about this scoring, because they drive the model:

1. **There is no plus/minus.** Good -- it is mostly noise, and we do not have to
   model it.
2. **Shots and blocks are worth a lot in aggregate.** At 0.9 a shot, a
   four-shot night is 3.6 points, more than half a goal. A high-volume shooter
   on the third line can out-earn a prettier player on the second, and a
   shot-blocking defenceman has a real floor even when he is not scoring. The
   model therefore cares about *volume* categories, which are far more stable
   game to game than goals are, and that stability is exactly what you want
   when you are buying one week of production.
"""

from __future__ import annotations

import config


def skater_fantasy_points(
    *,
    goals: float = 0,
    assists: float = 0,
    ppp: float = 0,
    shp: float = 0,
    sog: float = 0,
    blocks: float = 0,
) -> float:
    """Fantasy points for a skater's stat line.

    PPP and SHP are *bonuses* layered on top of the goal/assist that earned
    them, which is how Yahoo scores them -- a power-play goal pays 6 + 1.25.
    """
    pts = config.SKATER_POINTS
    return (
        goals * pts["goals"]
        + assists * pts["assists"]
        + ppp * pts["ppp"]
        + shp * pts["shp"]
        + sog * pts["sog"]
        + blocks * pts["blocks"]
    )


def goalie_fantasy_points(
    *,
    wins: float = 0,
    goals_against: float = 0,
    saves: float = 0,
    shutouts: float = 0,
) -> float:
    pts = config.GOALIE_POINTS
    return (
        wins * pts["wins"]
        + goals_against * pts["goals_against"]
        + saves * pts["saves"]
        + shutouts * pts["shutouts"]
    )


def skater_points_from_nhl_row(row: dict, realtime_row: dict | None = None) -> float:
    """
    Fantasy points from an `api.nhle.com/stats/rest` skater summary row, with
    hits/blocks coming from the matching `skater/realtime` row.
    """
    realtime_row = realtime_row or {}
    return skater_fantasy_points(
        goals=row.get("goals", 0) or 0,
        assists=row.get("assists", 0) or 0,
        ppp=row.get("ppPoints", 0) or 0,
        shp=row.get("shPoints", 0) or 0,
        sog=row.get("shots", 0) or 0,
        blocks=realtime_row.get("blockedShots", 0) or 0,
    )


def goalie_points_from_nhl_row(row: dict) -> float:
    return goalie_fantasy_points(
        wins=row.get("wins", 0) or 0,
        goals_against=row.get("goalsAgainst", 0) or 0,
        saves=row.get("saves", 0) or 0,
        shutouts=row.get("shutouts", 0) or 0,
    )
