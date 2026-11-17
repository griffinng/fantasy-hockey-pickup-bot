"""
NHL public API wrapper (api-web.nhle.com/v1).
No authentication required. All results are cached.
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Optional
import data.cache as cache
import data.http as http
import config

_BASE = config.NHL_API_BASE
_SESSION = http.build_session("fantasy-hockey-dashboard/2.0")

# Yahoo Fantasy abbreviations → NHL API abbreviations
_ABBREV_MAP = {
    "NJ":  "NJD",
    "TB":  "TBL",
    "LA":  "LAK",
    "SJ":  "SJS",
    "CLB": "CBJ",
    "MON": "MTL",
    "NAS": "NSH",
    "PHX": "ARI",
    "UTA": "UTA",  # Utah HC (new team)
    "WAS": "WSH",
}


def _nhl_abbrev(yahoo_abbrev: str) -> str:
    return _ABBREV_MAP.get(yahoo_abbrev.upper(), yahoo_abbrev.upper())


def _get(path: str, params: dict = None) -> dict | list:
    return http.get_json(_SESSION, f"{_BASE}{path}", params=params)


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

def player_landing(nhl_id: int) -> dict:
    cached = cache.get("player_landing", nhl_id)
    if cached:
        return cached
    data = _get(f"/player/{nhl_id}/landing")
    cache.set("player_landing", data, config.CACHE_TTL_STATS, nhl_id)
    return data


def player_game_log(nhl_id: int, season: str = "20242025") -> list[dict]:
    """Return list of game log entries for the player this season."""
    cached = cache.get("game_log", nhl_id, season)
    if cached:
        return cached
    data = _get(f"/player/{nhl_id}/game-log/{season}/2")  # 2 = regular season
    entries = data.get("gameLog", [])
    cache.set("game_log", entries, config.CACHE_TTL_STATS, nhl_id, season)
    return entries


def player_stats_recent(nhl_id: int, days: int) -> dict:
    """
    Aggregate skater fantasy-relevant stats from the last `days` days.
    Returns: {games, goals, assists, plus_minus, ppp, sog, blocks, fantasy_pts}
    """
    from features.scoring import skater_fantasy_points

    cutoff = date.today() - timedelta(days=days)
    log = player_game_log(nhl_id)
    totals = dict(games=0, goals=0, assists=0, ppp=0, shp=0,
                  sog=0, blocks=0, fantasy_pts=0.0)
    for g in log:
        game_date = date.fromisoformat(g.get("gameDate", "2000-01-01"))
        if game_date < cutoff:
            continue
        totals["games"] += 1
        goals = g.get("goals", 0)
        assists = g.get("assists", 0)
        ppp = g.get("powerPlayPoints", 0)
        shp = g.get("shorthandedPoints", 0)
        sog = g.get("shots", 0)
        blk = g.get("blockedShots", 0)

        totals["goals"] += goals
        totals["assists"] += assists
        totals["ppp"] += ppp
        totals["shp"] += shp
        totals["sog"] += sog
        totals["blocks"] += blk

        totals["fantasy_pts"] += skater_fantasy_points(
            goals=goals, assists=assists, ppp=ppp, shp=shp, sog=sog, blocks=blk
        )

    totals["fantasy_pts"] = round(totals["fantasy_pts"], 1)
    return totals


def goalie_stats_recent(nhl_id: int, days: int) -> dict:
    """
    Aggregate goalie fantasy stats from the last `days` days.
    Returns: {games, wins, goals_against, saves, shutouts, fantasy_pts}
    """
    cutoff = date.today() - timedelta(days=days)
    log = player_game_log(nhl_id)
    totals = dict(games=0, wins=0, goals_against=0, saves=0, shutouts=0, fantasy_pts=0.0)
    for g in log:
        game_date = date.fromisoformat(g.get("gameDate", "2000-01-01"))
        if game_date < cutoff:
            continue
        decision = g.get("decision", "")
        if not decision:
            # Backup who didn't play — skip
            continue
        totals["games"] += 1
        win = 1 if decision == "W" else 0
        ga = g.get("goalsAgainst", 0)
        sv = g.get("saveShotsAgainst", g.get("saves", 0))
        sho = g.get("shutouts", 0)

        totals["wins"] += win
        totals["goals_against"] += ga
        totals["saves"] += sv
        totals["shutouts"] += sho

        from features.scoring import goalie_fantasy_points
        totals["fantasy_pts"] += goalie_fantasy_points(
            wins=win, goals_against=ga, saves=sv, shutouts=sho
        )

    totals["fantasy_pts"] = round(totals["fantasy_pts"], 1)
    return totals


def player_injury_status(nhl_id: int) -> str:
    """Return injury status string, e.g. 'IR', 'DTD', or '' if healthy."""
    landing = player_landing(nhl_id)
    return landing.get("currentTeamRoster", {}).get("injuryStatus", "") or ""


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def team_schedule(team_abbrev: str) -> list[dict]:
    """Fetch remaining regular-season schedule for a team."""
    abbrev = _nhl_abbrev(team_abbrev)
    cached = cache.get("team_schedule", abbrev)
    if cached:
        return cached
    try:
        data = _get(f"/club-schedule-season/{abbrev}/now")
        games = data.get("games", [])
    except Exception:
        games = []
    cache.set("team_schedule", games, config.CACHE_TTL_SCHEDULE, abbrev)
    return games


def games_in_week(team_abbrev: str, week_start: date, week_end: date) -> int:
    """Count how many games a team plays within [week_start, week_end]."""
    games = team_schedule(team_abbrev)
    count = 0
    for g in games:
        gd = date.fromisoformat(g.get("gameDate", "2000-01-01")[:10])
        if week_start <= gd <= week_end:
            count += 1
    return count


def build_weekly_game_counts(
    team_abbrevs: list[str],
    weeks_ahead: int = config.SCHEDULE_WEEKS_AHEAD,
) -> dict[str, list[int]]:
    """
    Return {team_abbrev: [games_wk0, games_wk1, ..., games_wkN]} for the
    next `weeks_ahead` weeks starting from the current Monday.
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    result: dict[str, list[int]] = {}
    for abbrev in team_abbrevs:
        counts = []
        for w in range(weeks_ahead):
            wk_start = monday + timedelta(weeks=w)
            wk_end = wk_start + timedelta(days=6)
            counts.append(games_in_week(abbrev, wk_start, wk_end))
        result[abbrev] = counts
    return result


def current_week_games(team_abbrev: str) -> int:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return games_in_week(team_abbrev, monday, sunday)
