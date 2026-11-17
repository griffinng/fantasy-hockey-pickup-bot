"""
Build a weekly game-count matrix for roster + waiver players.
Highlights which weeks are ideal for pickups/drops ("streaming windows").
"""

from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import data.nhl_client as nhl
import config


def _week_labels(weeks_ahead: int) -> list[str]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    labels = []
    for w in range(weeks_ahead):
        wk_start = monday + timedelta(weeks=w)
        wk_end = wk_start + timedelta(days=6)
        labels.append(f"{wk_start.strftime('%b %d')}–{wk_end.strftime('%b %d')}")
    return labels


def roster_schedule_matrix(players: list[dict], weeks_ahead: int = config.SCHEDULE_WEEKS_AHEAD) -> pd.DataFrame:
    """
    Return DataFrame where rows = players, columns = week labels,
    values = number of games that week.
    """
    teams = list({p["team_abbrev"] for p in players if p.get("team_abbrev")})
    game_counts = nhl.build_weekly_game_counts(teams, weeks_ahead)
    week_labels = _week_labels(weeks_ahead)

    rows = []
    for p in players:
        abbrev = p.get("team_abbrev", "")
        counts = game_counts.get(abbrev, [0] * weeks_ahead)
        row = {
            "Player": p.get("name", ""),
            "Pos": "/".join(p.get("positions", [])),
            "Team": abbrev,
        }
        for i, label in enumerate(week_labels):
            row[label] = counts[i] if i < len(counts) else 0
        rows.append(row)

    return pd.DataFrame(rows)


def streaming_windows(
    waiver_players: list[dict],
    min_games: int = 3,
    weeks_ahead: int = config.SCHEDULE_WEEKS_AHEAD,
) -> pd.DataFrame:
    """
    Find waiver players who have `min_games` or more in at least one upcoming week.
    Good candidates to pick up before that week and drop after.
    """
    teams = list({p["team_abbrev"] for p in waiver_players if p.get("team_abbrev")})
    game_counts = nhl.build_weekly_game_counts(teams, weeks_ahead)
    week_labels = _week_labels(weeks_ahead)

    rows = []
    for p in waiver_players:
        abbrev = p.get("team_abbrev", "")
        counts = game_counts.get(abbrev, [0] * weeks_ahead)

        peak_games = max(counts) if counts else 0
        if peak_games < min_games:
            continue

        best_weeks = [
            week_labels[i] for i, c in enumerate(counts) if c >= min_games
        ]

        row = {
            "Player": p.get("name", ""),
            "Pos": "/".join(p.get("positions", [])),
            "Team": abbrev,
            "Peak Games/Wk": peak_games,
            "Best Weeks": ", ".join(best_weeks),
            "Ownership %": p.get("ownership_pct", 0),
        }
        for i, label in enumerate(week_labels):
            row[label] = counts[i] if i < len(counts) else 0

        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("Peak Games/Wk", ascending=False).reset_index(drop=True)
