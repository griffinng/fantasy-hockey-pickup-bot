"""
Identify drop candidates on your roster using actual fantasy point values.

Drop score (0–1, higher = stronger drop candidate) is based on:
- Declining FPPG trend (7d vs 30d)
- Low game count this week
- Injury status

Goalies are scored separately since their contribution is fundamentally
different (saves/wins/GA matter, not goals/assists).
"""

from __future__ import annotations
from typing import Optional
import pandas as pd
import data.nhl_client as nhl
import config

_INJURY_STATUSES = {"IR", "IR-LTI", "Injured Reserve", "DTD", "Out"}
_GOALIE_POSITIONS = {"G", "GK"}


def _is_goalie(player: dict) -> bool:
    return bool(set(player.get("positions", [])) & _GOALIE_POSITIONS)


def _drop_score(fppg7: float, fppg30: float, games_wk: int, status: str) -> float:
    score = 0.0

    if fppg30 > 0:
        ratio = fppg7 / fppg30
        if ratio < 0.50:
            score += 0.35
        elif ratio < 0.75:
            score += 0.20
    elif fppg7 == 0 and fppg30 == 0:
        score += 0.25

    if games_wk <= 1:
        score += 0.30
    elif games_wk == 2:
        score += 0.10

    if status in _INJURY_STATUSES:
        score += 0.35

    return min(score, 1.0)


def _goalie_drop_score(stats7: dict, stats30: dict, games_wk: int, status: str) -> float:
    """Goalies are valued on FP/game and whether they're a starter (games played)."""
    score = 0.0

    fppg7 = stats7["fantasy_pts"] / stats7["games"] if stats7["games"] > 0 else 0.0
    fppg30 = stats30["fantasy_pts"] / stats30["games"] if stats30["games"] > 0 else 0.0

    # Not a starter (< 2 games in last 14 days is a backup red flag)
    games14 = nhl.goalie_stats_recent  # used inline below
    if stats7["games"] == 0 and stats30["games"] <= 2:
        score += 0.30

    if fppg30 > 0 and fppg7 < fppg30 * 0.60:
        score += 0.25

    if games_wk <= 1:
        score += 0.25

    if status in _INJURY_STATUSES:
        score += 0.35

    return min(score, 1.0)


def score_roster(roster: list[dict]) -> list[dict]:
    scored = []
    for p in roster:
        nhl_id = p.get("nhl_id", 0)
        team = p.get("team_abbrev", "")
        status = p.get("status", "")
        goalie = _is_goalie(p)
        games_wk = nhl.current_week_games(team) if team else 2

        if goalie:
            s7 = nhl.goalie_stats_recent(nhl_id, 7)
            s30 = nhl.goalie_stats_recent(nhl_id, 30)
            fppg7 = s7["fantasy_pts"] / s7["games"] if s7["games"] > 0 else 0.0
            fppg30 = s30["fantasy_pts"] / s30["games"] if s30["games"] > 0 else 0.0
            drop_score = _goalie_drop_score(s7, s30, games_wk, status)
            extra = {
                "W_7d": s7.get("wins", 0),
                "GA_7d": s7.get("goals_against", 0),
                "SV_7d": s7.get("saves", 0),
            }
        else:
            s7 = nhl.player_stats_recent(nhl_id, 7)
            s30 = nhl.player_stats_recent(nhl_id, 30)
            fppg7 = s7["fantasy_pts"] / s7["games"] if s7["games"] > 0 else 0.0
            fppg30 = s30["fantasy_pts"] / s30["games"] if s30["games"] > 0 else 0.0
            drop_score = _drop_score(fppg7, fppg30, games_wk, status)
            extra = {
                "G_7d": s7.get("goals", 0),
                "A_7d": s7.get("assists", 0),
                "PPP_7d": s7.get("ppp", 0),
                "SOG_7d": s7.get("sog", 0),
            }

        if drop_score >= 0.60:
            flag = "Drop"
        elif drop_score >= 0.30:
            flag = "Monitor"
        else:
            flag = "Keep"

        scored.append({
            **p,
            **extra,
            "fppg_7d": round(fppg7, 1),
            "fppg_30d": round(fppg30, 1),
            "games_this_week": games_wk,
            "drop_score": round(drop_score, 2),
            "recommendation": flag,
        })

    return sorted(scored, key=lambda x: x["drop_score"], reverse=True)


def build_drop_df(roster: list[dict]) -> pd.DataFrame:
    scored = score_roster(roster)
    return pd.DataFrame(scored).reset_index(drop=True)


def best_replacement(position: str, waiver_df: pd.DataFrame) -> dict | None:
    if waiver_df.empty:
        return None
    mask = waiver_df["positions"].apply(
        lambda pos: position in pos if isinstance(pos, list) else False
    )
    filtered = waiver_df[mask]
    if filtered.empty:
        return None
    return filtered.iloc[0].to_dict()
