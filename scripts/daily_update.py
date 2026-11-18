"""
Daily refresh: pull everything fresh, rebuild the model, write a report.

Run it by hand:

    python scripts/daily_update.py

or let it run every morning (see scripts/install_schedule.sh). It is safe to
run repeatedly -- it just re-fetches and overwrites the day's report.

Timing matters more than it looks. Daily Faceoff posts confirmed lines once
the morning skate is done, so a run around 11:00-12:00 local gets you the real
lines for tonight rather than yesterday's guess. A run at 6am will mostly give
you last night's listing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.cache as cache  # noqa: E402
import features.advisor as advisor  # noqa: E402
import features.matchups as matchups  # noqa: E402

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
SNAPSHOT_DIR = REPORTS_DIR / "predictions"


def write_snapshot(advice, today: _dt.date) -> Path:
    """
    Freeze today's per-game projections so they can be scored later.

    Without this, checking yesterday's accuracy means rebuilding the
    projections from scratch -- and that rebuild cannot see the Daily Faceoff
    line and power-play listings, which are only ever published for *today*.
    The reconstruction therefore grades a weaker model than the one that
    actually gave the advice, and systematically understates it.

    A few kilobytes of JSON per day removes the problem entirely:
    `python -m tests.score_predictions` reads this file and compares like
    with like.
    """
    import json

    per_game: dict[str, dict[str, float]] = {}
    week: dict[str, float] = {}
    meta: dict[str, dict] = {}

    # Every projection the run produced, not just the shortlist -- a snapshot
    # of only the players the model already liked cannot tell you later
    # whether it was calibrated.
    sources = list((advice.all_projections or {}).values())
    sources += list(advice.candidates)
    sources += [e.projection for e in advice.roster if getattr(e, "projection", None)]
    sources += [e.projection for e in advice.ir_stash if getattr(e, "projection", None)]

    for proj in sources:
        pid = str(getattr(proj, "nhl_id", "") or "")
        if not pid or pid in per_game:
            continue
        per_game[pid] = {
            f"{g.game.date:%Y-%m-%d}": round(float(g.points), 3)
            for g in getattr(proj, "games", [])
        }
        week[pid] = round(float(getattr(proj, "week_points", 0.0)), 3)
        role = getattr(proj, "role", None)
        games = getattr(proj, "games", [])
        meta[pid] = {
            "name": getattr(proj, "name", ""),
            "team": getattr(proj, "team", ""),
            "position": getattr(proj, "position", ""),
            # A projection is E[points if he plays] x P(he plays). Scoring it
            # against players who are known to have played needs the second
            # factor divided back out, or the model looks pessimistic purely
            # because it hedged on availability.
            "play_prob": round(
                float(
                    getattr(games[0], "play_prob", None)
                    or getattr(games[0], "start_prob", 1.0)
                ), 3
            ) if games else 1.0,
            # Stored because this is precisely what a later rebuild cannot
            # recover: which line and PP unit he was listed on that morning.
            "line_rank": getattr(role, "line_rank", None) if role else None,
            "pp_unit": getattr(role, "pp_unit", None) if role else None,
            "role_change": getattr(role, "role_change", "") if role else "",
        }

    blob = {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": f"{today:%Y-%m-%d}",
        "per_game": per_game,
        "week_points": week,
        "meta": meta,
    }
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{today:%Y-%m-%d}.json"
    path.write_text(json.dumps(blob, indent=1))
    return path


def run(*, fresh: bool = True, quiet: bool = False) -> Path:
    started = _dt.datetime.now()
    today = started.date()

    if fresh:
        # Drop the whole cache so nothing stale survives into today's numbers.
        cache.bust()

    advice = advisor.build_advice(today=today, progress=not quiet)

    week_start, week_end = matchups.fantasy_week_bounds(today)
    header = [
        "=" * 96,
        f"FANTASY HOCKEY PICKUP REPORT -- {today:%A %d %B %Y}",
        f"Fantasy week {week_start} to {week_end}  "
        f"({(week_end - today).days + 1} day(s) remaining)",
        f"Generated {started:%H:%M:%S}",
        "=" * 96,
        "",
    ]

    # Goalies have no PlayerRole at all, so this has to go through the
    # advisor's null-safe helpers rather than reaching into `.role`.
    promoted = [
        p for p in advice.candidates if advisor.role_change(p) == "promoted"
    ]
    if promoted:
        header.append("ROLE CHANGES WORTH ACTING ON")
        header.append("-" * 96)
        for p in promoted[:10]:
            header.append(f"  * {p.name} ({p.team}) -- {advisor.role_note(p)}")
        header.append("")

    body = advisor.format_advice(advice)
    footer = [
        "",
        "-" * 96,
        "Stats: NHL (api-web.nhle.com, api.nhle.com).  "
        "Lines and units: Daily Faceoff.",
        "Fantasy data provided by Yahoo Fantasy.",
    ]

    text = "\n".join(header) + body + "\n".join(footer)

    REPORTS_DIR.mkdir(exist_ok=True)
    dated = REPORTS_DIR / f"{today:%Y-%m-%d}.txt"
    dated.write_text(text)
    (REPORTS_DIR / "latest.txt").write_text(text)
    snapshot = write_snapshot(advice, today)
    if not quiet:
        print(f"\nPrediction snapshot written to {snapshot}")

    if not quiet:
        print()
        print(text)
    return dated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-fresh", action="store_true",
        help="use cached data instead of refetching (fast, for testing)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="write the report without printing it",
    )
    args = parser.parse_args()

    try:
        path = run(fresh=not args.no_fresh, quiet=args.quiet)
    except Exception:
        # A scheduled run must fail loudly in the log rather than silently.
        print("DAILY UPDATE FAILED", file=sys.stderr)
        traceback.print_exc()
        return 1

    if args.quiet:
        print(path)
    else:
        print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
