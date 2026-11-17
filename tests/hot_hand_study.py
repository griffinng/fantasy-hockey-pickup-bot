"""
Does a genuine hot streak mark a real step change in a player's level?

This tests a sharper claim than the earlier recency backtest did. That one
bucketed every player by trailing scoring rate and asked whether the model
over-rated the hot ones. This asks something different and more specific:

    When a player ERUPTS -- three straight games well above a normal night --
    is he a different player afterwards than he was before?

The design deliberately compares the window BEFORE the streak against the
window AFTER it, excluding the streak games themselves from both. That
sidesteps the selection artifact that makes naive hot-hand studies worthless:
if you select players by a hot window and then measure that same window, it is
guaranteed to look inflated. Before-vs-after has no such problem.

A control group runs the identical measurement around random 3-game windows
that were NOT hot, which absorbs any league-wide seasonal drift.

The second question matters just as much: if the level does rise, is it because
the player's JOB changed -- more ice time, a power-play promotion? If so, the
projection model should (and does) read the role directly rather than inferring
it from points. If the level rises with no role change, that is a genuine
hot-hand effect and the model is missing something.

    python -m tests.hot_hand_study
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.nhl_client as nhl_client  # noqa: E402
import data.nhl_stats_rest as nhl_rest  # noqa: E402
from features.scoring import skater_fantasy_points  # noqa: E402

# A "hot" game: roughly a goal plus a couple of shots, or two assists.
HOT_GAME_FP = 8.0
STREAK_LEN = 3

# How many games either side of the streak to measure.
WINDOW = 10
MIN_WINDOW = 5

MIN_SEASON_GAMES = 18


def game_fp(g: dict) -> float:
    return skater_fantasy_points(
        goals=g.get("goals", 0) or 0,
        assists=g.get("assists", 0) or 0,
        ppp=g.get("powerPlayPoints", 0) or 0,
        shp=g.get("shorthandedPoints", 0) or 0,
        sog=g.get("shots", 0) or 0,
        blocks=g.get("blockedShots", 0) or 0,
    )


def toi_seconds(g: dict) -> float:
    return nhl_rest.mmss_to_seconds(g.get("toi"))


def _mean(vals: list[float]) -> float:
    return statistics.mean(vals) if vals else 0.0


def analyse_player(log: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Return (hot_events, control_events) for one player's chronological log.

    Each event records scoring and ice time either side of a 3-game window.
    """
    if len(log) < MIN_SEASON_GAMES:
        return [], []

    fps = [game_fp(g) for g in log]
    tois = [toi_seconds(g) for g in log]
    n = len(log)

    def event_at(start: int) -> dict | None:
        end = start + STREAK_LEN               # exclusive
        pre = fps[max(0, start - WINDOW):start]
        post = fps[end:end + WINDOW]
        if len(pre) < MIN_WINDOW or len(post) < MIN_WINDOW:
            return None
        pre_toi = tois[max(0, start - WINDOW):start]
        post_toi = tois[end:end + WINDOW]
        return {
            "pre_fp": _mean(pre),
            "post_fp": _mean(post),
            "streak_fp": _mean(fps[start:end]),
            "pre_toi": _mean(pre_toi),
            "post_toi": _mean(post_toi),
        }

    hot, control = [], []
    used: set[int] = set()
    for start in range(n - STREAK_LEN + 1):
        window = fps[start:start + STREAK_LEN]
        if all(f >= HOT_GAME_FP for f in window):
            if any(i in used for i in range(start, start + STREAK_LEN)):
                continue  # don't double-count overlapping streaks
            ev = event_at(start)
            if ev:
                hot.append(ev)
                used.update(range(start, start + STREAK_LEN))

    # Control: random non-hot 3-game windows from the same players.
    candidates = [
        s for s in range(n - STREAK_LEN + 1)
        if not all(f >= HOT_GAME_FP for f in fps[s:s + STREAK_LEN])
    ]
    random.shuffle(candidates)
    for start in candidates[:2]:
        ev = event_at(start)
        if ev:
            control.append(ev)

    return hot, control


def summarise(events: list[dict], label: str) -> dict:
    if not events:
        return {}
    pre = _mean([e["pre_fp"] for e in events])
    post = _mean([e["post_fp"] for e in events])
    pre_toi = _mean([e["pre_toi"] for e in events])
    post_toi = _mean([e["post_toi"] for e in events])
    improved = sum(1 for e in events if e["post_fp"] > e["pre_fp"])
    return {
        "label": label,
        "n": len(events),
        "pre": pre,
        "post": post,
        "delta": post - pre,
        "streak": _mean([e["streak_fp"] for e in events]),
        "pre_toi": pre_toi,
        "post_toi": post_toi,
        "toi_delta": post_toi - pre_toi,
        "pct_improved": improved / len(events),
    }


def main() -> int:
    random.seed(17)
    season_id = nhl_rest.current_season_id()
    season = nhl_rest.skater_summary_season(season_id)

    players = [
        (pid, r) for pid, r in season.items()
        if (r.get("gamesPlayed") or 0) >= MIN_SEASON_GAMES
    ]
    print("=" * 94)
    print("HOT-STREAK STUDY -- does erupting for 3 games mark a real step change?")
    print("=" * 94)
    print(
        f"\nHot streak = {STREAK_LEN} consecutive games at {HOT_GAME_FP}+ fantasy "
        f"points.\nComparing the {WINDOW} games BEFORE against the {WINDOW} games "
        f"AFTER (streak excluded from both).\n"
    )
    print(f"Scanning {len(players)} skaters with {MIN_SEASON_GAMES}+ games...")

    hot_all, control_all = [], []
    for i, (pid, _) in enumerate(players):
        if i % 100 == 0 and i:
            print(f"  {i}/{len(players)}...", flush=True)
        try:
            log = nhl_client.player_game_log(pid, season_id)
        except Exception:
            continue
        log = sorted(log, key=lambda g: str(g.get("gameDate", "")))
        hot, control = analyse_player(log)
        hot_all.extend(hot)
        control_all.extend(control)

    hot_s = summarise(hot_all, "AFTER A HOT STREAK")
    ctl_s = summarise(control_all, "CONTROL (ordinary 3 games)")

    print()
    print(f"{'GROUP':<28} {'N':>5} {'PRE':>7} {'STREAK':>7} {'POST':>7} "
          f"{'DELTA':>7} {'%UP':>6}")
    print("-" * 94)
    for s in (hot_s, ctl_s):
        if s:
            print(
                f"{s['label']:<28} {s['n']:>5} {s['pre']:>7.2f} "
                f"{s['streak']:>7.2f} {s['post']:>7.2f} {s['delta']:>+7.2f} "
                f"{s['pct_improved']:>5.0%}"
            )

    if hot_s and ctl_s:
        lift = hot_s["delta"] - ctl_s["delta"]
        print("-" * 94)
        print(
            f"\nHot-streak lift over control: {lift:+.2f} fantasy points per game"
        )
        print(
            f"  (hot players went {hot_s['delta']:+.2f}, control went "
            f"{ctl_s['delta']:+.2f})"
        )

        print("\nDid their JOB change? Ice time either side of the window:")
        print(
            f"  hot     : {nhl_rest.seconds_to_mmss(hot_s['pre_toi'])} -> "
            f"{nhl_rest.seconds_to_mmss(hot_s['post_toi'])}  "
            f"({hot_s['toi_delta'] / 60:+.2f} min)"
        )
        print(
            f"  control : {nhl_rest.seconds_to_mmss(ctl_s['pre_toi'])} -> "
            f"{nhl_rest.seconds_to_mmss(ctl_s['post_toi'])}  "
            f"({ctl_s['toi_delta'] / 60:+.2f} min)"
        )

        print("\nREADING:")
        if lift > 0.5:
            print(
                "  Hot streaks DO mark a real step up beyond ordinary variation."
            )
            if hot_s["toi_delta"] - ctl_s["toi_delta"] > 30:
                print(
                    "  Ice time rose with it, so much of the gain is a ROLE change\n"
                    "  -- which the model already reads directly, and earlier."
                )
            else:
                print(
                    "  Ice time barely moved, so this is NOT just a role change.\n"
                    "  The model is missing something real."
                )
        elif lift > 0.0:
            print("  A small positive effect, within touching distance of noise.")
        else:
            print(
                "  No lift beyond the control group: the post-streak level is not\n"
                "  materially different from the pre-streak level."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
