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

#: A single strong night. Roughly a goal plus two shots, or two assists.
BIG_GAME_FP = 8.0
#: A huge night, the kind that tends to precede a real step up.
HUGE_GAME_FP = 12.0
#: How far back to look.
LOOKBACK_GAMES = 10

CACHE_TTL = 3 * 3600


@dataclasses.dataclass
class StreakInfo:
    nhl_id: int
    games: int = 0
    big_games: int = 0          # games >= BIG_GAME_FP in the lookback
    huge_games: int = 0         # games >= HUGE_GAME_FP
    longest_run: int = 0        # longest consecutive run of big games
    last3_fp: float = 0.0       # fantasy points per game over the last 3
    last5_fp: float = 0.0
    best_game: float = 0.0

    @property
    def is_hot(self) -> bool:
        """
        Two ways to qualify: a genuine consecutive run, or simply several big
        nights in a short span. The second catches a player who went
        10 / quiet / 11, which is the same signal with a dud game in the
        middle.
        """
        return self.longest_run >= 2 or self.big_games >= 3

    @property
    def heat(self) -> float:
        """
        A single sortable number. Weighted toward consecutive runs and huge
        nights, since the study showed the lift grows with streak intensity.
        """
        return round(
            self.last5_fp
            + 2.0 * self.longest_run
            + 1.5 * self.huge_games
            + 0.5 * self.big_games,
            2,
        )

    @property
    def label(self) -> str:
        bits = []
        if self.longest_run >= 2:
            bits.append(f"{self.longest_run} straight big games")
        if self.huge_games:
            bits.append(f"{self.huge_games} huge night{'s' if self.huge_games > 1 else ''}")
        elif self.big_games:
            bits.append(f"{self.big_games} big in last {self.games}")
        return ", ".join(bits) or "-"


def _game_fp(g: dict) -> float:
    return skater_fantasy_points(
        goals=g.get("goals", 0) or 0,
        assists=g.get("assists", 0) or 0,
        ppp=g.get("powerPlayPoints", 0) or 0,
        shp=g.get("shorthandedPoints", 0) or 0,
        sog=g.get("shots", 0) or 0,
        blocks=g.get("blockedShots", 0) or 0,
    )


def streak_for(nhl_id: int, season_id: str | None = None) -> StreakInfo:
    season_id = season_id or nhl_rest.current_season_id()
    cached = cache.get("streak", nhl_id, season_id)
    if cached is not None:
        return StreakInfo(**cached)

    info = StreakInfo(nhl_id=nhl_id)
    try:
        log = nhl_client.player_game_log(nhl_id, season_id)
    except Exception:
        return info

    log = sorted(log, key=lambda g: str(g.get("gameDate", "")))[-LOOKBACK_GAMES:]
    if not log:
        return info

    fps = [_game_fp(g) for g in log]
    info.games = len(fps)
    info.big_games = sum(1 for f in fps if f >= BIG_GAME_FP)
    info.huge_games = sum(1 for f in fps if f >= HUGE_GAME_FP)
    info.best_game = round(max(fps), 1)
    info.last3_fp = round(sum(fps[-3:]) / min(3, len(fps)), 2)
    info.last5_fp = round(sum(fps[-5:]) / min(5, len(fps)), 2)

    run = best = 0
    for f in fps:
        run = run + 1 if f >= BIG_GAME_FP else 0
        best = max(best, run)
    info.longest_run = best

    cache.set("streak", dataclasses.asdict(info), CACHE_TTL, nhl_id, season_id)
    return info


def streaks_for(
    nhl_ids: list[int], season_id: str | None = None, *, progress: bool = False
) -> dict[int, StreakInfo]:
    """
    Streak info for a set of players. One game-log request each on a cold
    cache, so this is called for the candidate pool rather than the league.
    """
    out: dict[int, StreakInfo] = {}
    for i, pid in enumerate(nhl_ids):
        if progress and i and i % 50 == 0:
            print(f"  streaks {i}/{len(nhl_ids)}...", flush=True)
        out[pid] = streak_for(pid, season_id)
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
