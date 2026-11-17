"""
Hot-streak detection.

Built after a study (tests/hot_hand_study.py) that contradicted my earlier
assumption. Comparing the 10 games before a streak against the 10 after it,
with streak games excluded from both and a control group of ordinary 3-game
windows absorbing seasonal drift:

    streak                 N     lift over control
    3 games at  6+ FP     119         +0.25
    3 games at  8+ FP      44         +0.38
    3 games at 10+ FP      20         +0.99
    4 games at  8+ FP      15         +1.87

The effect is small but it scales cleanly with intensity, and a dose-response
that orderly is not what noise looks like. Players who erupt really do settle
at a higher level than they were at before.

The larger finding, though, is about *identification* rather than prediction.
Players who produce a hot streak were already averaging 7.5 fantasy points a
game beforehand, against 4.2 for the control group, on 20:39 of ice time
against 17:15. A streak is mostly not a player improving -- it is a good
player becoming visible. If he is sitting unowned, that is a genuine find, and
it is why scanning for eruptions works as a scouting method even though
"he's hot, he'll stay hot" is a weaker claim than it sounds.

So this module surfaces streaks for the pickup list rather than feeding them
into the projection. Seeing who is erupting is useful on its own terms.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt

import data.cache as cache
import data.nhl_client as nhl_client
import data.nhl_stats_rest as nhl_rest
from features.scoring import skater_fantasy_points

#: Trailing windows to test, anchored at the player's MOST RECENT game.
#: Several lengths because eruptions come in different shapes: two monster
#: nights, a solid week, a hot fortnight. Checking only one length misses the
#: others entirely.
WINDOWS = (2, 3, 5, 8, 10)

#: Minimum games before we will call anything a streak.
MIN_GAMES = 2

#: Fallback positional baselines, overridden by live data at runtime.
FALLBACK_BASELINE = {"F": 4.79, "D": 4.09}

CACHE_TTL = 3 * 3600


@dataclasses.dataclass
class StreakInfo:
    nhl_id: int
    position: str = "F"
    games: int = 0
    baseline: float = 0.0        # positional par for one game
    season_rate: float = 0.0     # his own season pace
    best_window: int = 0         # which trailing window is hottest
    best_rate: float = 0.0       # fantasy points per game in that window
    best_total: float = 0.0      # total points in that window
    heat: float = 0.0            # lift over positional par, duration-weighted
    breakout: float = 0.0        # lift over his OWN season pace
    last_game: float = 0.0       # most recent game, to spot a cooling streak
    window_rates: dict = dataclasses.field(default_factory=dict)

    @property
    def is_hot(self) -> bool:
        """Meaningfully above positional par on at least one window."""
        return self.games >= MIN_GAMES and self.heat >= 2.0

    @property
    def label(self) -> str:
        if not self.best_window:
            return "-"
        return (
            f"{self.best_total:.0f} FP in last {self.best_window} "
            f"({self.best_rate:.1f}/gm vs {self.baseline:.1f} par)"
        )

    @property
    def cooling(self) -> bool:
        """Hot on the window, but the latest game was below par."""
        return self.is_hot and self.last_game < self.baseline


def _game_fp(g: dict) -> float:
    return skater_fantasy_points(
        goals=g.get("goals", 0) or 0,
        assists=g.get("assists", 0) or 0,
        ppp=g.get("powerPlayPoints", 0) or 0,
        shp=g.get("shorthandedPoints", 0) or 0,
        sog=g.get("shots", 0) or 0,
        blocks=g.get("blockedShots", 0) or 0,
    )


def position_baselines(season_id: str | None = None) -> dict[str, float]:
    """
    Fantasy points per game for an average forward and an average defenceman.

    Computed from this season rather than hard-coded, because it is the whole
    reason a defenceman needs a different bar: four points is a thoroughly
    ordinary night for a forward and an above-median one for a defenceman, so
    a single flat threshold would flag the wrong people in both directions.
    """
    season_id = season_id or nhl_rest.current_season_id()
    cached = cache.get("pos_baseline", season_id)
    if cached:
        return cached

    season = nhl_rest.skater_summary_season(season_id)
    realtime = nhl_rest.skater_realtime_season(season_id)
    totals = {"F": [0.0, 0.0], "D": [0.0, 0.0]}
    for pid, r in season.items():
        gp = float(r.get("gamesPlayed") or 0)
        if gp < 10:
            continue
        key = "D" if (r.get("positionCode") or "").upper() == "D" else "F"
        fp = skater_fantasy_points(
            goals=r.get("goals", 0) or 0, assists=r.get("assists", 0) or 0,
            ppp=r.get("ppPoints", 0) or 0, shp=r.get("shPoints", 0) or 0,
            sog=r.get("shots", 0) or 0,
            blocks=(realtime.get(pid, {}) or {}).get("blockedShots", 0) or 0,
        )
        totals[key][0] += fp
        totals[key][1] += gp

    out = {
        k: round(v[0] / v[1], 2) if v[1] else FALLBACK_BASELINE[k]
        for k, v in totals.items()
    }
    cache.set("pos_baseline", out, CACHE_TTL, season_id)
    return out


def streak_for(
    nhl_id: int,
    season_id: str | None = None,
    as_of: _dt.date | None = None,
    position: str = "F",
    baselines: dict[str, float] | None = None,
) -> StreakInfo:
    """
    Streak state for a player, optionally as it stood on a past date.

    Every window is anchored at the player's most recent game. An earlier
    version searched for runs anywhere in the last ten games, which flagged
    Shane Pinto for two big games that had happened ten days and four games
    earlier -- his latest outing was 0.9 points. A streak that has already
    ended is not a streak.
    """
    season_id = season_id or nhl_rest.current_season_id()
    baselines = baselines or position_baselines(season_id)
    # Daily Faceoff says 'ld'/'rd', the NHL says 'D'. A startswith('D') check
    # silently treated every right-side defenceman as a forward and held him
    # to the wrong bar.
    key = "D" if (position or "F").lower() in ("d", "ld", "rd") else "F"
    baseline = baselines.get(key, FALLBACK_BASELINE[key])

    info = StreakInfo(nhl_id=nhl_id, position=key, baseline=baseline)
    try:
        log = nhl_client.player_game_log(nhl_id, season_id)
    except Exception:
        return info

    log = sorted(log, key=lambda g: str(g.get("gameDate", "")))
    if as_of is not None:
        cutoff = as_of.isoformat()
        log = [g for g in log if str(g.get("gameDate", ""))[:10] < cutoff]
    if not log:
        return info

    fps = [_game_fp(g) for g in log]
    info.games = len(fps)
    info.last_game = round(fps[-1], 1)
    info.season_rate = round(sum(fps) / len(fps), 2)

    best_heat = 0.0
    for w in WINDOWS:
        if len(fps) < w:
            continue
        chunk = fps[-w:]                      # anchored at the latest game
        rate = sum(chunk) / w
        info.window_rates[w] = round(rate, 2)
        # Duration-weighted lift: sustained heat beats one lucky night, but a
        # violent two-game burst still registers.
        heat = (rate - baseline) * (w / 3.0) ** 0.5
        if heat > best_heat:
            best_heat = heat
            info.best_window = w
            info.best_rate = round(rate, 2)
            info.best_total = round(sum(chunk), 1)
            info.breakout = round(
                (rate - info.season_rate) * (w / 3.0) ** 0.5, 2
            )
    info.heat = round(best_heat, 2)
    return info


def streaks_for(
    nhl_ids: list[int],
    season_id: str | None = None,
    *,
    progress: bool = False,
    as_of: _dt.date | None = None,
    positions: dict[int, str] | None = None,
) -> dict[int, StreakInfo]:
    """
    Streak info for a set of players. One game-log request each on a cold
    cache, so this is called for the candidate pool rather than the league.
    """
    baselines = position_baselines(season_id)
    out: dict[int, StreakInfo] = {}
    for i, pid in enumerate(nhl_ids):
        if progress and i and i % 50 == 0:
            print(f"  streaks {i}/{len(nhl_ids)}...", flush=True)
        out[pid] = streak_for(
            pid, season_id, as_of,
            position=(positions or {}).get(pid, 'F'),
            baselines=baselines,
        )
    return out


def _demo() -> None:  # pragma: no cover - manual smoke check
    import features.advisor as advisor

    advice = advisor.build_advice(progress=False)
    ids = [p.nhl_id for p in advice.candidates]
    info = streaks_for(ids, progress=True)
    hot = sorted(
        (p for p in advice.candidates if info.get(p.nhl_id, StreakInfo(0)).is_hot),
        key=lambda p: -info[p.nhl_id].heat,
    )
    print(f"\n{'PLAYER':<24} {'TM':<4} {'HEAT':>6} {'L5':>6} {'BEST':>6}  STREAK")
    print("-" * 86)
    for p in hot[:20]:
        s = info[p.nhl_id]
        print(
            f"{p.name:<24} {p.team:<4} {s.heat:>6.1f} {s.last5_fp:>6.1f} "
            f"{s.best_game:>6.1f}  {s.label}"
        )


if __name__ == "__main__":
    _demo()


def streak_bonus(info: StreakInfo | None, weights: dict) -> float:
    """
    Extra expected fantasy points per game for a player on a run.

    `scale` is the single knob; at 0.0 this contributes nothing. The backtest
    sweep found only a marginal gain at 0.25 and losses above it, so the
    default stays low.
    """
    cfg = (weights or {}).get("streaks") or {}
    scale = float(cfg.get("scale", 0.0))
    if not info or scale <= 0 or not info.is_hot:
        return 0.0
    return min(info.heat, float(cfg.get("max_bonus", 2.5))) * scale
