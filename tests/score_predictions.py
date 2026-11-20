"""
How good were yesterday's numbers?

The backtest asks whether the model beats a naive ranker over whole weeks.
This asks a narrower and more immediate question: for one night's slate, what
did the model say each skater would score, and what did he actually score?

    python -m tests.score_predictions                  # last night
    python -m tests.score_predictions --date 2026-11-17

TWO HONEST CAVEATS, because they decide how much the output is worth.

1. ONE NIGHT IS A TINY SAMPLE. A single game of hockey is mostly variance: a
   deflection, a goalie's rebound, four minutes of garbage-time power play.
   Per-player, a one-night miss means nothing at all. What a single slate can
   tell you is whether the model is biased -- whether it is systematically
   high or low across hundreds of skaters -- and roughly whether its ordering
   held up. Treat anything player-specific here as an anecdote.

2. THE PROJECTIONS ARE RECONSTRUCTED, NOT REPLAYED. Daily Faceoff only
   publishes today's lines, so a projection built for a past date cannot see
   the line and PP-unit listings the live model used that morning. This script
   rebuilds roles from NHL ice-time data alone. That is the same handicap the
   backtest runs under, and it means these numbers slightly understate the
   live model, which has the promotion signal the reconstruction lacks.

   `--snapshot` scores a stored prediction file instead, which has no such
   gap. `scripts/daily_update.py` writes one every run from 2026-11-18 on, so
   from tomorrow this script can be exact rather than reconstructed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.my_team as my_team  # noqa: E402
import data.names as names_mod  # noqa: E402
import data.nhl_stats_rest as nhl_rest  # noqa: E402
import features.advisor as advisor  # noqa: E402
import features.matchups as matchups  # noqa: E402
import features.projection as projection  # noqa: E402
from features.scoring import skater_fantasy_points  # noqa: E402
from tests.backtest import _spearman, build_historical_roles  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "reports" / "predictions"

# A skater who was never going to play cannot tell us anything about the
# model's accuracy, so the population stats only count players whose team
# actually played and who dressed.
MIN_SEASON_GAMES = 5

# play_prob per player, filled in by the reconstruction. A projection is
# `expected points if he dresses` x `probability he dresses`, so comparing it
# against the actuals of players who DID dress is not a fair test -- the
# availability discount is guaranteed to look like pessimism. Keeping the
# probability lets us divide it back out and score the scoring model alone.
PLAY_PROB: dict[int, float] = {}


# ---------------------------------------------------------------------------
# Actuals
# ---------------------------------------------------------------------------

def actual_day_points(day: _dt.date) -> dict[int, dict]:
    """What every skater really scored on one date."""
    exp = nhl_rest._date_exp(day, day)
    summary = nhl_rest._fetch("skater/summary", exp, aggregate=True, ttl=86400)
    realtime = nhl_rest._fetch("skater/realtime", exp, aggregate=True, ttl=86400)
    rt = {int(r["playerId"]): r for r in realtime if r.get("playerId")}

    out: dict[int, dict] = {}
    for row in summary:
        pid = int(row.get("playerId", 0))
        if not pid:
            continue
        r = rt.get(pid, {})
        out[pid] = {
            "name": row.get("skaterFullName", ""),
            "team": (row.get("teamAbbrevs") or "").split(",")[-1].strip(),
            "gp": int(row.get("gamesPlayed") or 0),
            "goals": row.get("goals", 0) or 0,
            "assists": row.get("assists", 0) or 0,
            "sog": row.get("shots", 0) or 0,
            "blocks": r.get("blockedShots", 0) or 0,
            "fp": skater_fantasy_points(
                goals=row.get("goals", 0) or 0,
                assists=row.get("assists", 0) or 0,
                ppp=row.get("ppPoints", 0) or 0,
                shp=row.get("shPoints", 0) or 0,
                sog=row.get("shots", 0) or 0,
                blocks=r.get("blockedShots", 0) or 0,
            ),
        }
    return out


def actual_goalie_points(day: _dt.date) -> dict[int, dict]:
    exp = nhl_rest._date_exp(day, day)
    rows = nhl_rest._fetch("goalie/summary", exp, aggregate=True, ttl=86400)
    import config
    gp_cfg = config.GOALIE_POINTS
    out: dict[int, dict] = {}
    for row in rows:
        pid = int(row.get("playerId", 0))
        if not pid:
            continue
        wins = row.get("wins", 0) or 0
        ga = row.get("goalsAgainst", 0) or 0
        sv = row.get("saves", 0) or 0
        so = row.get("shutouts", 0) or 0
        out[pid] = {
            "name": row.get("goalieFullName", ""),
            "started": int(row.get("gamesStarted") or 0),
            "fp": round(
                wins * gp_cfg["wins"] + ga * gp_cfg["goals_against"]
                + sv * gp_cfg["saves"] + so * gp_cfg["shutouts"], 2
            ),
            "line": f"{int(wins)}W {int(sv)}sv {int(ga)}ga",
        }
    return out


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def reconstruct_day_projections(
    day: _dt.date, teams_by_player: dict[int, str], weights: dict
) -> dict[int, float]:
    """
    What the model would have projected for `day`'s games, using only data
    available the morning of `day`.

    Returns {nhl_id: projected fantasy points for that one game}.
    """
    window_days = int(weights.get("window_days", 14))
    cutoff = day - _dt.timedelta(days=1)
    season_start = _dt.date(day.year if day.month >= 9 else day.year - 1, 9, 1)

    def fetch(report: str, start: _dt.date, end: _dt.date) -> dict[int, dict]:
        return {
            int(r["playerId"]): r
            for r in nhl_rest._fetch(
                report, nhl_rest._date_exp(start, end), aggregate=True, ttl=86400
            )
            if r.get("playerId")
        }

    win_start = cutoff - _dt.timedelta(days=window_days)
    window = (
        fetch("skater/summary", win_start, cutoff),
        fetch("skater/powerplay", win_start, cutoff),
        fetch("skater/realtime", win_start, cutoff),
    )
    season = (
        fetch("skater/summary", season_start, cutoff),
        fetch("skater/powerplay", season_start, cutoff),
        fetch("skater/realtime", season_start, cutoff),
    )

    roles = build_historical_roles(day, window_days, teams_by_player)
    priors = projection.build_positional_priors(season[0], season[1], season[2], roles)
    profiles = matchups.build_opponent_profiles()
    team_pp = nhl_rest.team_powerplay_season()

    out: dict[int, float] = {}
    for pid, role in roles.items():
        if role.games_window < 2 or not role.team:
            continue
        team_pp_seconds = float(
            (team_pp.get(role.team, {}) or {}).get("ppTimeOnIcePerGame") or 250.0
        )
        rates = projection.estimate_rates(
            pid, role, window=window, season=season, priors=priors, weights=weights
        )
        proj = projection.project_player(
            pid, role, rates=rates, profiles=profiles,
            team_pp_seconds=team_pp_seconds, weights=weights, today=day,
            historical=True,
        )
        # Pull out the one game that falls on the target date.
        for gp in proj.games:
            if gp.game.date == day:
                out[pid] = gp.points
                PLAY_PROB[pid] = gp.play_prob
                break
    return out


def _read_snapshot(path: Path, day: _dt.date) -> tuple[dict[int, float], dict, set[str]]:
    """Projections for `day` out of one snapshot file, plus the teams covered."""
    blob = json.loads(path.read_text())
    key = f"{day:%Y-%m-%d}"
    meta = blob.get("meta") or {}
    proj = {
        int(pid): float(days[key])
        for pid, days in (blob.get("per_game") or {}).items()
        if key in days
    }
    teams = {
        (meta.get(str(pid)) or {}).get("team", "") for pid in proj
    } - {""}
    return proj, blob, teams


def load_snapshot(
    day: _dt.date, teams_that_played: set[str] | None = None
) -> tuple[dict[int, float], str] | None:
    """
    Best stored prediction for `day`, and a note on where it came from.

    Picking the file named after the day is the obvious thing and it is wrong.
    The projection model only ever looks at games that have NOT started, so a
    snapshot written at 18:32 has already dropped every team that faced off at
    16:00 -- including, on 2026-11-19, the one player the day's decision was
    actually about. Scoring against it silently grades the model on whatever
    late games happened to be left.

    So: consider every snapshot written on or before `day`, newest first, and
    take the first one that covers most of the teams that really played. A
    morning run beats the previous evening's because it has that day's
    confirmed lines; a run made mid-slate loses to both.
    """
    candidates = sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True)
    best: tuple[dict[int, float], dict, str] | None = None
    for path in candidates:
        try:
            as_of = _dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if as_of > day:
            continue
        proj, blob, teams = _read_snapshot(path, day)
        if not proj:
            continue
        if teams_that_played:
            covered = len(teams & teams_that_played) / len(teams_that_played)
            if covered < 0.80:
                # Written mid-slate: the teams already playing are missing.
                continue
            note = (
                f"{path.name} (generated {blob.get('generated', '?')}, "
                f"covers {covered:.0%} of teams that played)"
            )
        else:
            note = f"{path.name} (generated {blob.get('generated', '?')})"
        best = (proj, blob, note)
        break

    if best is None:
        return None
    proj, blob, note = best
    for pid, m in (blob.get("meta") or {}).items():
        PLAY_PROB[int(pid)] = float(m.get("play_prob", 1.0) or 1.0)
    return proj, note


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_player_table(rows: list[dict], title: str, note: str = "") -> str:
    lines = [title, "-" * 96]
    if note:
        lines.append(note)
    lines.append(
        f"{'PLAYER':<24} {'TM':<4} {'POS':<4} {'PROJ':>7} {'ACTUAL':>8} "
        f"{'DIFF':>7}  LINE"
    )
    for r in rows:
        diff = r["actual"] - r["proj"]
        lines.append(
            f"{r['name'][:23]:<24} {r['team']:<4} {r['pos']:<4} "
            f"{r['proj']:>7.1f} {r['actual']:>8.1f} {diff:>+7.1f}  {r.get('line','')}"
        )
    if rows:
        tp = sum(r["proj"] for r in rows)
        ta = sum(r["actual"] for r in rows)
        lines.append("-" * 96)
        lines.append(
            f"{'TOTAL':<24} {'':<4} {'':<4} {tp:>7.1f} {ta:>8.1f} {ta - tp:>+7.1f}"
        )
    return "\n".join(lines)


def calibration(pairs: list[tuple[float, float]], buckets: int = 5) -> list[tuple]:
    """Mean projection vs mean actual, by projection bucket. Catches bias."""
    if len(pairs) < buckets * 3:
        return []
    ordered = sorted(pairs)
    size = len(ordered) // buckets
    out = []
    for i in range(buckets):
        chunk = ordered[i * size:(i + 1) * size] if i < buckets - 1 else ordered[i * size:]
        if not chunk:
            continue
        out.append((
            i + 1, len(chunk),
            statistics.mean(p for p, _ in chunk),
            statistics.mean(a for _, a in chunk),
        ))
    return out


def report_recommendations(day: _dt.date) -> list[tuple[str, str]]:
    """
    The players the LIVE model actually put in front of you that morning,
    scraped back out of the report it wrote.

    This matters because the reconstruction above cannot see Daily Faceoff
    listings, and the listings are exactly where the live model's edge is
    supposed to come from. Scoring the names it really printed is the only
    way to test the part the reconstruction throws away.
    """
    path = ROOT / "reports" / f"{day:%Y-%m-%d}.txt"
    if not path.exists():
        return []
    picks: list[tuple[str, str]] = []
    in_block = False
    for line in path.read_text().splitlines():
        if line.startswith("OVERALL BEST FITS"):
            in_block = True
            continue
        if in_block:
            if line.startswith("TOP PICKUP CANDIDATES") or line.startswith("="):
                break
            # ' 1.Sam Rinzel               CHI  D      4    23.8 ...'
            stripped = line.strip()
            if not stripped or "." not in stripped[:4]:
                continue
            body = stripped.split(".", 1)[1]
            parts = body.split()
            if len(parts) < 3:
                continue
            # Walk back from the team code to recover a multi-word name.
            for i, tok in enumerate(parts):
                if tok.isupper() and 2 <= len(tok) <= 3 and i > 0:
                    picks.append((" ".join(parts[:i]), tok))
                    break
    return picks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="slate to score (default: yesterday)")
    ap.add_argument(
        "--snapshot", action="store_true",
        help="require a stored prediction file instead of reconstructing",
    )
    ap.add_argument(
        "--fa-file",
        help="score against a past free-agent snapshot rather than today's pool",
    )
    args = ap.parse_args()

    if args.fa_file:
        my_team.FREE_AGENT_PATH = Path(args.fa_file)

    day = (
        _dt.date.fromisoformat(args.date) if args.date
        else _dt.date.today() - _dt.timedelta(days=1)
    )

    print("=" * 96)
    print(f"PREDICTION SCORECARD -- slate of {day:%A %d %B %Y}")
    print("=" * 96)

    actual = actual_day_points(day)
    played = {pid: a for pid, a in actual.items() if a["gp"] > 0}
    if not played:
        print(f"\nNo skater played on {day}. Nothing to score.")
        return 1
    season_lookup = nhl_rest.skater_summary_season()
    for pid, a in played.items():
        if not a["team"]:
            a["team"] = (
                (season_lookup.get(pid, {}) or {}).get("teamAbbrevs") or ""
            ).split(",")[-1].strip()
    teams_played = sorted({a["team"] for a in played.values() if a["team"]})
    print(
        f"\n{len(played)} skaters dressed across {len(teams_played)} teams: "
        f"{', '.join(teams_played)}"
    )

    loaded = load_snapshot(day, set(teams_played))
    proj = None
    source = ""
    if loaded is not None:
        proj, note = loaded
        source = f"stored snapshot, exact -- {note}"
    if proj is None:
        if args.snapshot:
            print(
                f"\nNo usable snapshot for {day}. A file written mid-slate is "
                "skipped, because\nit has already dropped the teams that had "
                "started."
            )
            return 1
        print(
            "\nNo stored snapshot for that date, so projections are being "
            "RECONSTRUCTED from\nNHL data as it stood that morning. No line or "
            "PP-unit listings -- see the module\ndocstring. Reconstruction "
            "understates the live model slightly."
        )
        weights = projection.load_weights()
        season_rows = nhl_rest.skater_summary_season()
        teams_by_player = {
            pid: (r.get("teamAbbrevs") or "").split(",")[-1].strip()
            for pid, r in season_rows.items()
        }
        proj = reconstruct_day_projections(day, teams_by_player, weights)
        source = "reconstructed (no line/PP overlay)"

    print(f"Projection source: {source}\n")

    # ---- population accuracy ---------------------------------------------
    season_rows = nhl_rest.skater_summary_season()
    eligible = {
        pid for pid in proj
        if pid in played
        and int((season_rows.get(pid, {}) or {}).get("gamesPlayed") or 0) >= MIN_SEASON_GAMES
    }
    pairs = [(proj[pid], played[pid]["fp"]) for pid in eligible]

    print("=" * 96)
    print("POPULATION ACCURACY -- every skater who dressed")
    print("=" * 96)
    if len(pairs) < 20:
        print(f"\nOnly {len(pairs)} comparable skaters. Too few to say anything.")
    else:
        mean_p = statistics.mean(p for p, _ in pairs)
        mean_a = statistics.mean(a for _, a in pairs)
        mae = statistics.mean(abs(p - a) for p, a in pairs)
        rho = _spearman(pairs)
        print(f"\n  Skaters compared     : {len(pairs)}")
        print(f"  Mean projected       : {mean_p:.2f} FP")
        print(f"  Mean actual          : {mean_a:.2f} FP")
        print(
            f"  Bias                 : {mean_a - mean_p:+.2f} FP "
            f"({'model ran LOW' if mean_a > mean_p else 'model ran HIGH'})"
        )
        print(f"  Mean absolute error  : {mae:.2f} FP")
        print(f"  Rank correlation     : {rho:.3f}")
        print(
            "\n  A single night's rho sits well below the week-level number; one "
            "game is\n  mostly noise, and no projection can order noise."
        )

        cal = calibration(pairs)
        if cal:
            print("\n  CALIBRATION BY PROJECTION BUCKET")
            print(f"  {'BUCKET':<8} {'N':>5} {'MEAN PROJ':>11} {'MEAN ACTUAL':>13} {'GAP':>8}")
            for i, n, mp, ma in cal:
                lbl = "lowest" if i == 1 else "highest" if i == len(cal) else f"{i}"
                print(f"  {lbl:<8} {n:>5} {mp:>11.2f} {ma:>13.2f} {ma - mp:>+8.2f}")
            print(
                "\n  The gap column is what matters. A gap that grows as "
                "projections rise means\n  the model is over-trusting its own "
                "top end."
            )

        # --- separating a real bias from an availability artifact ----------
        # A projection is E[points | he dresses] x P(he dresses). Every player
        # in this sample dressed, so the P(dresses) factor is pure pessimism
        # here and will masquerade as the model under-projecting. Split the
        # population on it before concluding anything about bias.
        if PLAY_PROB:
            listed = [pid for pid in eligible if PLAY_PROB.get(pid, 1.0) >= 0.99]
            discounted = [pid for pid in eligible if PLAY_PROB.get(pid, 1.0) < 0.99]
            print("\n  BIAS, SPLIT BY WHETHER AVAILABILITY WAS DISCOUNTED")
            print(
                f"  {'GROUP':<28} {'N':>5} {'MEAN PROJ':>11} "
                f"{'MEAN ACTUAL':>13} {'GAP':>8}"
            )
            for label, group in (
                ("listed, no discount", listed),
                ("unlisted, discounted", discounted),
            ):
                if len(group) < 10:
                    continue
                mp = statistics.mean(proj[p] for p in group)
                ma = statistics.mean(played[p]["fp"] for p in group)
                print(f"  {label:<28} {len(group):>5} {mp:>11.2f} {ma:>13.2f} {ma - mp:>+8.2f}")
            if discounted:
                adj = [
                    (proj[p] / max(PLAY_PROB.get(p, 1.0), 0.01), played[p]["fp"])
                    for p in eligible
                ]
                amp = statistics.mean(p for p, _ in adj)
                ama = statistics.mean(a for _, a in adj)
                print(
                    f"  {'all, discount divided out':<28} {len(adj):>5} "
                    f"{amp:>11.2f} {ama:>13.2f} {ama - amp:>+8.2f}"
                )
            print(
                "\n  In a reconstruction nobody is 'listed' in a line-up, so "
                "every player without\n  measurable power-play time takes the "
                "unlisted-scratch discount whether or not\n  he was really a "
                "scratch. The listed row is the honest read on bias; the "
                "unlisted\n  row mostly measures that discount."
            )

        # Did picking the model's favourites actually pay?
        ranked = sorted(eligible, key=lambda p: -proj[p])
        for n in (10, 20, 30):
            if len(ranked) >= n:
                top = statistics.mean(played[p]["fp"] for p in ranked[:n])
                print(
                    f"\n  Model's top {n:<2} averaged {top:5.2f} FP "
                    f"vs {mean_a:.2f} for everyone who played "
                    f"({top - mean_a:+.2f})"
                )

    # ---- my roster --------------------------------------------------------
    index = advisor.build_index()
    roster = my_team.load_roster(index)
    goalie_actual = actual_goalie_points(day)

    rows = []
    for entry in roster:
        pid = entry.nhl_id
        if pid is None:
            continue
        pos = names_mod.display_position(entry.primary)
        if pid in goalie_actual:
            a = goalie_actual[pid]
            rows.append({
                "name": entry.name, "team": entry.team or "", "pos": "G",
                "proj": proj.get(pid, 0.0), "actual": a["fp"], "line": a["line"],
            })
        elif pid in played:
            a = played[pid]
            rows.append({
                "name": entry.name, "team": entry.team or "", "pos": pos,
                "proj": proj.get(pid, 0.0), "actual": a["fp"],
                "line": f"{a['goals']}G {a['assists']}A {a['sog']}sog {a['blocks']}blk",
            })

    print()
    print("=" * 96)
    print(
        _fmt_player_table(
            sorted(rows, key=lambda r: -r["actual"]),
            "YOUR ROSTER -- who actually played last night",
            note="Goalie projections are not reconstructed, so they show 0.0 "
                 "here; see the report text.\n",
        )
    )

    # ---- the free-agent pool ---------------------------------------------
    fa = my_team.load_free_agents(index)
    fa_rows = []
    for entry in fa:
        pid = entry.nhl_id
        if pid is None or pid not in played or pid not in proj:
            continue
        a = played[pid]
        fa_rows.append({
            "name": entry.name, "team": entry.team or "",
            "pos": names_mod.display_position(entry.primary),
            "proj": proj[pid], "actual": a["fp"],
            "line": f"{a['goals']}G {a['assists']}A {a['sog']}sog {a['blocks']}blk",
        })

    if fa_rows:
        print()
        print("=" * 96)
        pool_mean = statistics.mean(r["actual"] for r in fa_rows)
        top10 = sorted(fa_rows, key=lambda r: -r["proj"])[:10]
        print(
            _fmt_player_table(
                top10,
                "WAIVER WIRE -- the ten the model liked most among those who played",
                note=f"{len(fa_rows)} available players dressed; they averaged "
                     f"{pool_mean:.2f} FP.\n",
            )
        )
        got = statistics.mean(r["actual"] for r in top10)
        print(
            f"\n  Top 10 by projection averaged {got:.2f} FP vs {pool_mean:.2f} "
            f"for the whole available pool ({got - pool_mean:+.2f})."
        )
        best = sorted(fa_rows, key=lambda r: -r["actual"])[:5]
        print("\n  Biggest actual scores in the pool, and where the model had them:")
        order = {r["name"]: i + 1 for i, r in enumerate(sorted(fa_rows, key=lambda r: -r["proj"]))}
        for r in best:
            print(
                f"    {r['name']:<24} {r['actual']:>5.1f} FP   "
                f"model ranked him {order[r['name']]} of {len(fa_rows)} "
                f"(proj {r['proj']:.1f})"
            )

    # ---- what the LIVE model actually recommended -------------------------
    picks = report_recommendations(day)
    if picks:
        print()
        print("=" * 96)
        print("WHAT THE LIVE MODEL PUT IN FRONT OF YOU THAT MORNING")
        print("=" * 96)
        print(
            "\nThe names from that day's 'overall best fits' list -- the live "
            "model WITH the\nDaily Faceoff line and PP-unit overlay, which the "
            "reconstruction above cannot see.\nThis is the only section that "
            "tests the promotion signal.\n"
        )
        print(f"{'RANK':<6} {'PLAYER':<24} {'TM':<4} {'ACTUAL':>8}  LINE")
        scored, idle = [], []
        for rank, (name, team) in enumerate(picks, 1):
            pid = index.resolve(name, team)
            if pid is None or pid not in played:
                idle.append(name)
                continue
            a = played[pid]
            scored.append(a["fp"])
            print(
                f"{rank:<6} {name[:23]:<24} {team:<4} {a['fp']:>8.1f}  "
                f"{a['goals']}G {a['assists']}A {a['sog']}sog {a['blocks']}blk"
            )
        if idle:
            print(f"\n  Did not play: {', '.join(idle)}")
        if scored:
            mean_rec = statistics.mean(scored)
            print(
                f"\n  Those {len(scored)} averaged {mean_rec:.2f} FP."
            )
            if fa_rows:
                print(
                    f"  The available pool that night averaged {pool_mean:.2f} FP "
                    f"({mean_rec - pool_mean:+.2f})."
                )
            print(
                "\n  One slate. Treat a good night here as encouraging, not as "
                "evidence --\n  twelve players for one evening cannot "
                "distinguish a real edge from luck."
            )

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
