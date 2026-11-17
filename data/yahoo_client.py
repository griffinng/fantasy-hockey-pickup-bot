"""
Yahoo Fantasy Sports API client — direct HTTP calls using our OAuth token.
No YFPY dependency; uses the stored token from auth/yahoo_oauth.py.
"""

from __future__ import annotations
import requests
import data.cache as cache
import config
from auth.yahoo_oauth import get_valid_token

_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


def _session() -> requests.Session:
    token = get_valid_token()
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token['access_token']}",
        "Accept": "application/json",
    })
    return s


def _get(path: str, params: dict = None) -> dict:
    resp = _session().get(f"{_BASE}{path}", params={"format": "json", **(params or {})}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("fantasy_content", {})


# ---------------------------------------------------------------------------
# League helpers
# ---------------------------------------------------------------------------

def _get_game_key() -> str:
    """Detect the correct NHL game key from the user's own leagues."""
    cached = cache.get("nhl_game_key")
    if cached:
        return cached

    # Ask Yahoo which NHL leagues this user is in — the league key contains the game key
    data = _get("/users;use_login=1/games;game_codes=nhl/leagues")
    try:
        games = data.get("users", {}).get("0", {}).get("user", [{}, {}])[1]
        games_list = games.get("games", {})
        for i in range(games_list.get("count", 0)):
            game = games_list.get(str(i), {}).get("game", [{}, {}])
            game_key = game[0].get("game_key", "") if isinstance(game[0], dict) else ""
            leagues = game[1].get("leagues", {}) if isinstance(game[1], dict) else {}
            for j in range(leagues.get("count", 0)):
                league = leagues.get(str(j), {}).get("league", [{}])
                league_id = league[0].get("league_id", "") if isinstance(league[0], dict) else ""
                if str(league_id) == str(config.YAHOO_LEAGUE_ID) and game_key:
                    cache.set("nhl_game_key", game_key, config.CACHE_TTL_STATS)
                    return game_key
    except Exception:
        pass

    # Fallback: use env value or 453
    return config.YAHOO_GAME_KEY or "453"


def _league_key() -> str:
    return f"{_get_game_key()}.l.{config.YAHOO_LEAGUE_ID}"


def _my_team_key() -> str:
    cached = cache.get("my_team_key")
    if cached:
        return cached

    data = _get(f"/league/{_league_key()}/teams")
    teams = data.get("league", [{}, {}])[1].get("teams", {})
    # Find team owned by current user
    for i in range(teams.get("count", 0)):
        t = teams.get(str(i), {}).get("team", [{}])[0]
        for item in t:
            if isinstance(item, dict) and item.get("is_owned_by_current_login"):
                team_key = next(
                    (x["team_key"] for x in t if isinstance(x, dict) and "team_key" in x),
                    None
                )
                if team_key:
                    cache.set("my_team_key", team_key, config.CACHE_TTL_STATS)
                    return team_key

    # Fallback: use team 1
    team_key = f"{_league_key()}.t.1"
    cache.set("my_team_key", team_key, config.CACHE_TTL_STATS)
    return team_key


# ---------------------------------------------------------------------------
# Player dict normalisation
# ---------------------------------------------------------------------------

def _parse_player(player_data: list) -> dict:
    """Parse Yahoo player array format into a flat dict."""
    d: dict = {}
    flat = player_data[0] if player_data else []
    for item in flat:
        if not isinstance(item, dict):
            continue
        if "player_key" in item:
            d["player_key"] = item["player_key"]
        if "player_id" in item:
            d["player_id"] = str(item["player_id"])
        if "name" in item:
            d["name"] = item["name"].get("full", "")
        if "editorial_team_abbr" in item:
            d["team_abbrev"] = item["editorial_team_abbr"]
        if "eligible_positions" in item:
            positions = item["eligible_positions"]
            d["positions"] = [p["position"] for p in positions if isinstance(p, dict)]
        if "status" in item:
            d["status"] = item["status"]
        if "editorial_player_key" in item:
            # Extract NHL player ID from key like "nhl.p.12345"
            try:
                d["nhl_id"] = int(item["editorial_player_key"].split(".")[-1])
            except (ValueError, IndexError):
                d["nhl_id"] = 0

    # Ownership percentage
    if len(player_data) > 1 and isinstance(player_data[1], dict):
        pct = player_data[1].get("percent_owned", [{}])
        if isinstance(pct, list) and pct:
            d["ownership_pct"] = float(pct[0].get("value", 0) or 0)
        else:
            d["ownership_pct"] = 0.0
    else:
        d["ownership_pct"] = 0.0

    if "nhl_id" not in d:
        d["nhl_id"] = 0
    if "team_abbrev" not in d:
        d["team_abbrev"] = ""
    if "positions" not in d:
        d["positions"] = []
    if "status" not in d:
        d["status"] = ""

    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_my_roster() -> list[dict]:
    cached = cache.get("my_roster")
    if cached:
        return cached

    team_key = _my_team_key()
    data = _get(f"/team/{team_key}/roster")
    roster_raw = (
        data.get("team", [{}, {}])[1]
        .get("roster", {})
        .get("0", {})
        .get("players", {})
    )

    players = []
    count = roster_raw.get("count", 0) if isinstance(roster_raw, dict) else 0
    for i in range(count):
        p = roster_raw.get(str(i), {}).get("player", [])
        if p:
            players.append(_parse_player(p))

    cache.set("my_roster", players, config.CACHE_TTL_ROSTER)
    return players


def get_waiver_players(count: int = 150) -> list[dict]:
    cached = cache.get("waivers", count)
    if cached:
        return cached

    data = _get(
        f"/league/{_league_key()}/players",
        params={"status": "A", "count": count, "sort": "OR"},
    )
    players_raw = data.get("league", [{}, {}])[1].get("players", {})

    players = []
    total = players_raw.get("count", 0) if isinstance(players_raw, dict) else 0
    for i in range(total):
        p = players_raw.get(str(i), {}).get("player", [])
        if p:
            players.append(_parse_player(p))

    cache.set("waivers", players, config.CACHE_TTL_ROSTER, count)
    return players


def get_league_settings() -> dict:
    cached = cache.get("league_settings")
    if cached:
        return cached

    data = _get(f"/league/{_league_key()}/settings")
    result = {"league_key": _league_key()}
    cache.set("league_settings", result, config.CACHE_TTL_STATS)
    return result
