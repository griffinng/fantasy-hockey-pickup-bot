import os
from dotenv import load_dotenv

load_dotenv()

YAHOO_CONSUMER_KEY = os.getenv("YAHOO_CONSUMER_KEY", "")
YAHOO_CONSUMER_SECRET = os.getenv("YAHOO_CONSUMER_SECRET", "")
YAHOO_LEAGUE_ID = os.getenv("YAHOO_LEAGUE_ID", "")
_YAHOO_GAME_KEY_ENV = os.getenv("YAHOO_GAME_KEY", "")


def _detect_nhl_game_key() -> str:
    """Auto-detect the current NHL game key from the Yahoo Fantasy API."""
    if _YAHOO_GAME_KEY_ENV:
        return _YAHOO_GAME_KEY_ENV
    try:
        import requests
        resp = requests.get(
            "https://fantasysports.yahooapis.com/fantasy/v2/game/nhl",
            params={"format": "json"},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            key = (
                data.get("fantasy_content", {})
                .get("game", [{}])[0]
                .get("game_key", "")
            )
            if key:
                return str(key)
    except Exception:
        pass
    return "453"  # fallback to 2024-25


YAHOO_GAME_KEY = _detect_nhl_game_key()

# Cache TTL in seconds
CACHE_TTL_STATS = 3600       # 1 hour for player stats
CACHE_TTL_ROSTER = 900       # 15 min for roster/waiver data
CACHE_TTL_SCHEDULE = 3600    # 1 hour for NHL schedule

# ---------------------------------------------------------------------------
# League scoring point values (H2H Points)
# ---------------------------------------------------------------------------

# Skater fantasy points per stat unit.
# Confirmed against the league settings screenshot, 2026-11-17.
# NOTE: this league has NO plus/minus category. An earlier version of this file
# scored +/- at 2.0, which badly distorted every projection -- plus/minus is
# high-variance and mostly noise, so crediting it rewarded luck.
SKATER_POINTS = {
    "goals":    6.0,
    "assists":  4.0,
    "ppp":      1.25,   # powerplay points
    "shp":      1.5,    # shorthanded points
    "sog":      0.9,    # shots on goal
    "blocks":   1.0,
}

# Goalie fantasy points per stat unit
GOALIE_POINTS = {
    "wins":     5.0,
    "goals_against": -3.0,
    "saves":    0.6,
    "shutouts": 5.0,
}

# Minimum goalie starts per week required by the league
MIN_GOALIE_STARTS_PER_WEEK = 3

# Max waiver acquisitions per week
MAX_ACQUISITIONS_PER_WEEK = 4

# How many you have already used this week. Yahoo shows this on your team page
# ("You have used N of your 4 player adds for this week"). Keep it current, or
# the bot will cheerfully recommend more moves than the league will let you make.
ACQUISITIONS_USED_THIS_WEEK = 1

# ---------------------------------------------------------------------------
# Scoring algorithm weights
# ---------------------------------------------------------------------------

# Fantasy FPPG recency weights — must sum to 1.0
FPPG_WEIGHT_7D = 0.40
FPPG_WEIGHT_14D = 0.30
FPPG_WEIGHT_30D = 0.30

# Games-this-week multiplier applied on top of FPPG base score
# 4 games → +30%, 3 games → +15%, 2 games → 0%, 1 game → -20%
GAMES_WEEK_MULTIPLIERS = {4: 1.30, 3: 1.15, 2: 1.00, 1: 0.80, 0: 0.00}

# Trend bonus: if 7-day FPPG > 30-day FPPG by this ratio, add bonus
TREND_BONUS_THRESHOLD = 1.20   # 20% above 30-day average
TREND_BONUS_VALUE = 0.10       # +10% to final score

# How many weeks ahead to build schedule matrix
SCHEDULE_WEEKS_AHEAD = 8

NHL_API_BASE = "https://api-web.nhle.com/v1"
