"""
Adapter for the NHL stats REST API (api.nhle.com/stats/rest/en).

This is a *different* service from api-web.nhle.com, which `nhl_client.py`
already wraps. We need it for one reason above all others: it is the only free
source of **power-play time on ice**, including each skater's share of his
team's total PP time (`ppTimeOnIcePctPerGame`). Per-game boxscores do not expose
PP TOI at all, so without this there is no way to tell PP1 from PP2 numerically.

It also accepts a date range (`isAggregate=true&isGame=true` plus a `gameDate`
filter), which means we can ask "what did this player's role look like over the
last 14 days" rather than being stuck with season totals. A player promoted to
PP1 three weeks into the season is invisible in season numbers; he is obvious in
a 14-day window. That gap is the edge this whole bot is built to find.

All responses are cached through `data/cache.py`.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any, Iterable

import config
import data.cache as cache
import data.http as http

BASE = "https://api.nhle.com/stats/rest/en"

_SESSION = http.build_session("fantasy-hockey-dashboard/2.0")

# This used to say that limit=-1 returned every skater in one request, so
# there was no reason to paginate. That stopped being true: the endpoint now
# caps a response at 100 rows whatever limit you ask for, while still
# reporting the real row count in `total`.
#
# The failure was silent and total. Nothing errored -- requests just came back
# with the first 100 skaters, which meant positional priors built from a
# tenth of the league, a name index that could not resolve most of the roster,
# and projections for whoever happened to sort first. Always page until
# `total` is reached, and never trust a single response to be complete.
_PAGE_SIZE = 100

# A season report is ~800 rows; this is a guard against an endpoint that
# reports a `total` it will not actually serve, so the loop cannot spin.
_MAX_PAGES = 100


# ---------------------------------------------------------------------------
# Season / date helpers
# ---------------------------------------------------------------------------

def current_season_id(today: _dt.date | None = None) -> str:
    """
    NHL season id, e.g. '20262027'. The season rolls over in September, so
    anything from September onward belongs to the season starting that year.
    """
    today = today or _dt.date.today()
    start_year = today.year if today.month >= 9 else today.year - 1
    return f"{start_year}{start_year + 1}"


def _fetch(report: str, cayenne: str, *, aggregate: bool, ttl: int) -> list[dict]:
    """
    GET one stats report, paging until complete, cached as a whole.

    Only a fully-assembled report is cached. A partial read caused by a
    mid-page network failure must not be stored, or every later run inherits
    a silently truncated league until the TTL expires.
    """
    cached = cache.get("nhl_rest", report, cayenne, aggregate)
    if cached is not None:
        return cached

    rows: list[dict] = []
    total: int | None = None
    for page in range(_MAX_PAGES):
        params: dict[str, str] = {
            "limit": str(_PAGE_SIZE),
            "start": str(page * _PAGE_SIZE),
            "cayenneExp": cayenne,
        }
        if aggregate:
            params["isAggregate"] = "true"
            params["isGame"] = "true"

        # Retry the page itself. The session already retries at the HTTP
        # layer, but a page that comes back empty or malformed is not an HTTP
        # error, and losing one page out of eight would otherwise fail the
        # whole report and take the daily run down with it.
        payload = None
        for attempt in range(3):
            try:
                payload = http.get_json(_SESSION, f"{BASE}/{report}", params=params)
            except http.FetchError:
                if attempt == 2:
                    raise
                payload = None
            if isinstance(payload, dict) and payload.get("data"):
                break
            _time.sleep(1.5 * (attempt + 1))

        if not isinstance(payload, dict):
            break
        batch = payload.get("data") or []
        rows.extend(batch)
        if total is None:
            total = payload.get("total")
        # Stop on a short page as well as on the count, so a `total` that
        # disagrees with what is served cannot loop to _MAX_PAGES.
        if len(batch) < _PAGE_SIZE:
            break
        if isinstance(total, int) and len(rows) >= total:
            break

    if isinstance(total, int) and len(rows) < total:
        raise http.FetchError(
            f"{report}: got {len(rows)} of {total} rows -- refusing to cache a "
            f"partial report, since a truncated league silently corrupts every "
            f"projection downstream."
        )

    cache.set("nhl_rest", rows, ttl, report, cayenne, aggregate)
    return rows


def _season_exp(season_id: str | None = None, game_type: int = 2) -> str:
    season_id = season_id or current_season_id()
    return f"seasonId={season_id} and gameTypeId={game_type}"


def _date_exp(start: _dt.date, end: _dt.date, game_type: int = 2) -> str:
    return (
        f'gameDate>="{start.isoformat()}" and '
        f'gameDate<="{end.isoformat()}" and '
        f"gameTypeId={game_type}"
    )


def _index_by_player(rows: Iterable[dict]) -> dict[int, dict]:
    return {int(r["playerId"]): r for r in rows if r.get("playerId") is not None}


# ---------------------------------------------------------------------------
# Skaters — season
# ---------------------------------------------------------------------------

def skater_summary_season(season_id: str | None = None) -> dict[int, dict]:
    """playerId -> season scoring summary (goals, assists, shots, TOI/gp, +/-)."""
    rows = _fetch(
        "skater/summary", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def skater_powerplay_season(season_id: str | None = None) -> dict[int, dict]:
    """playerId -> season PP usage, incl. ppTimeOnIcePerGame / ppTimeOnIcePctPerGame."""
    rows = _fetch(
        "skater/powerplay", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def skater_realtime_season(season_id: str | None = None) -> dict[int, dict]:
    """playerId -> season hits / blocked shots / takeaways (peripheral categories)."""
    rows = _fetch(
        "skater/realtime", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


# ---------------------------------------------------------------------------
# Skaters — trailing window (the part that actually matters)
# ---------------------------------------------------------------------------

def skater_summary_window(days: int, end: _dt.date | None = None) -> dict[int, dict]:
    end = end or _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = _fetch(
        "skater/summary", _date_exp(start, end),
        aggregate=True, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def skater_powerplay_window(days: int, end: _dt.date | None = None) -> dict[int, dict]:
    """
    Trailing-window PP usage. `ppTimeOnIcePctPerGame` here is the single most
    useful number in the whole pipeline: it is the fraction of his team's power-play
    time the player was on the ice for. Roughly:

        >= 0.60  ->  PP1 (he is out there for most of it)
        0.30-0.60 ->  PP2
        <  0.15  ->  not really a power-play player

    Comparing this against Daily Faceoff's *currently listed* unit is how we
    detect a promotion that hasn't shown up in the box score yet.
    """
    end = end or _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = _fetch(
        "skater/powerplay", _date_exp(start, end),
        aggregate=True, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def skater_penaltykill_window(days: int, end: _dt.date | None = None) -> dict[int, dict]:
    """
    Trailing-window short-handed usage (`shTimeOnIcePerGame`).

    We subtract this from total TOI so that even-strength minutes are genuinely
    even-strength. It matters: a defensive forward can post 17 minutes a night
    with three of them on the penalty kill, which produce almost no fantasy
    points. Crediting him with 17 even-strength minutes would overrate him.
    """
    end = end or _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = _fetch(
        "skater/penaltykill", _date_exp(start, end),
        aggregate=True, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def skater_realtime_window(days: int, end: _dt.date | None = None) -> dict[int, dict]:
    end = end or _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = _fetch(
        "skater/realtime", _date_exp(start, end),
        aggregate=True, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


# ---------------------------------------------------------------------------
# Goalies
# ---------------------------------------------------------------------------

def goalie_summary_season(season_id: str | None = None) -> dict[int, dict]:
    rows = _fetch(
        "goalie/summary", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


def goalie_summary_window(days: int, end: _dt.date | None = None) -> dict[int, dict]:
    end = end or _dt.date.today()
    start = end - _dt.timedelta(days=days)
    rows = _fetch(
        "goalie/summary", _date_exp(start, end),
        aggregate=True, ttl=config.CACHE_TTL_STATS,
    )
    return _index_by_player(rows)


# ---------------------------------------------------------------------------
# Teams — opponent difficulty inputs
# ---------------------------------------------------------------------------

# teamId -> standard abbreviation. The stats REST API identifies teams by id and
# full name; everything else in this project speaks abbreviations.
TEAM_ID_TO_ABBREV = {
    1: "NJD", 2: "NYI", 3: "NYR", 4: "PHI", 5: "PIT", 6: "BOS", 7: "BUF",
    8: "MTL", 9: "OTT", 10: "TOR", 12: "CAR", 13: "FLA", 14: "TBL", 15: "WSH",
    16: "CHI", 17: "DET", 18: "NSH", 19: "STL", 20: "CGY", 21: "COL",
    22: "EDM", 23: "VAN", 24: "ANA", 25: "DAL", 26: "LAK", 28: "SJS",
    29: "CBJ", 30: "MIN", 52: "WPG", 54: "VGK", 55: "SEA", 68: "UTA",
    # 53 was Arizona, relocated; 68 is the Utah Mammoth as of 2026-27.
}


def team_summary_season(season_id: str | None = None) -> dict[str, dict]:
    """
    abbrev -> team season summary. Supplies the matchup inputs:
      goalsAgainstPerGame   -> how leaky the opponent is
      shotsAgainstPerGame   -> SOG environment
      penaltyKillPct        -> how much the opponent's PK suppresses PP value
    """
    rows = _fetch(
        "team/summary", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    out: dict[str, dict] = {}
    for r in rows:
        abbrev = TEAM_ID_TO_ABBREV.get(int(r.get("teamId", 0)))
        if abbrev:
            out[abbrev] = r
    return out


def team_powerplay_season(season_id: str | None = None) -> dict[str, dict]:
    """
    abbrev -> team PP report. We need `ppTimeOnIce` per game to convert a
    player's PP *share* into actual expected PP minutes.
    """
    rows = _fetch(
        "team/powerplay", _season_exp(season_id),
        aggregate=False, ttl=config.CACHE_TTL_STATS,
    )
    out: dict[str, dict] = {}
    for r in rows:
        abbrev = TEAM_ID_TO_ABBREV.get(int(r.get("teamId", 0)))
        if abbrev:
            out[abbrev] = r
    return out


# ---------------------------------------------------------------------------
# Small conveniences
# ---------------------------------------------------------------------------

def seconds_to_mmss(seconds: float | None) -> str:
    if not seconds:
        return "0:00"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def mmss_to_seconds(value: str | None) -> float:
    """Parse '22:57' (as api-web boxscores give TOI) into seconds."""
    if not value:
        return 0.0
    parts = str(value).split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def _demo() -> None:  # pragma: no cover - manual smoke check
    season = current_season_id()
    print(f"season {season}")
    pp = skater_powerplay_window(14)
    ranked = sorted(
        pp.values(),
        key=lambda r: r.get("ppTimeOnIcePctPerGame") or 0,
        reverse=True,
    )[:10]
    print("\nTop PP-share skaters, last 14 days:")
    for r in ranked:
        print(
            f"  {r['skaterFullName']:<26} "
            f"share={r.get('ppTimeOnIcePctPerGame', 0):.3f}  "
            f"pp_toi/gp={seconds_to_mmss(r.get('ppTimeOnIcePerGame'))}  "
            f"gp={r.get('gamesPlayed')}"
        )
    teams = team_summary_season()
    print(f"\nteams loaded: {len(teams)}")
    worst = sorted(teams.items(), key=lambda kv: -(kv[1].get("goalsAgainstPerGame") or 0))[:5]
    print("Leakiest defences (GA/GP):")
    for abbrev, row in worst:
        print(f"  {abbrev}  {row['goalsAgainstPerGame']:.2f}")


if __name__ == "__main__":
    _demo()
