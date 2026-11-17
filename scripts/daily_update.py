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

    promoted = [
        p for p in advice.candidates if p.role.role_change == "promoted"
    ]
    if promoted:
        header.append("ROLE CHANGES WORTH ACTING ON")
        header.append("-" * 96)
        for p in promoted[:10]:
            header.append(f"  * {p.name} ({p.team}) -- {p.role.role_note}")
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
