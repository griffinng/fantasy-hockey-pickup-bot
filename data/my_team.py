"""
Your roster and the league's available players.

Yahoo closed off programmatic Fantasy access in 2026 and our existing app key
now returns 403 on every endpoint, so we cannot read the league directly until
an access application is approved. That blocks exactly two facts:

    1. which players are on your team
    2. which players are unowned

Everything else the model needs -- stats, ice time, lines, power-play units,
schedule, opponents -- comes from the NHL and Daily Faceoff and is unaffected.

So those two facts come from two editable text files, and the moment Yahoo
access is restored `source()` starts returning the live API instead with no
other code change. Paste-and-save is a ten-second job and the roster only
changes when you make a move.

File format is deliberately forgiving, because the realistic workflow is
copy-pasting out of a Yahoo page:

    Matvei Michkov
    Cale Makar, COL
    Jake Guentzel | TBL | lw
    # lines starting with '#' are ignored

Team and position are optional; they only help disambiguate players who share
a name (Vancouver has two Elias Petterssons).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from pathlib import Path

import data.names as names

ROSTER_PATH = Path(__file__).parent / "my_team.txt"
FREE_AGENT_PATH = Path(__file__).parent / "free_agents.txt"

ROSTER_TEMPLATE = """\
# Your fantasy roster -- one player per line.
#
# Copy the names straight out of your Yahoo team page. Team and position are
# optional and only needed to tell apart two players with the same name:
#
#     Matvei Michkov
#     Cale Makar, COL
#     Elias Pettersson | VAN | c
#
# Lines starting with '#' are ignored.
"""

FREE_AGENT_TEMPLATE = """\
# Available players in your league -- one per line, same format as my_team.txt.
#
# From Yahoo: Players -> filter to "Available", sort by whatever you like, and
# paste the names in. A couple of hundred is plenty; nobody is picking up the
# 400th-ranked skater.
#
# If you leave this file empty, the dashboard ranks EVERY NHL skater and simply
# marks the ones it knows are on your roster. That is still useful -- you just
# have to sanity-check availability in Yahoo before adding.
"""


@dataclasses.dataclass
class PlayerRef:
    """One line of a roster file, resolved (or not) to an NHL id."""

    raw: str
    name: str
    team: str | None
    position: str | None
    nhl_id: int | None = None
    status: str | None = None     # Yahoo roster status: ir, ir+, o, out, dtd

    @property
    def eligible(self) -> list[str]:
        """
        Every slot this player can legally fill, from Yahoo's own eligibility.

        Written in the file as 'c/rw'. This is NOT the same as his NHL
        position: Yahoo grants multi-position eligibility, and without it a
        lineup solver cannot know that a bench winger may fill an open centre
        slot.
        """
        raw = (self.position or "").lower()
        out = [p.strip() for p in raw.replace(",", "/").split("/") if p.strip()]
        return out or ["f"]

    @property
    def primary(self) -> str:
        return self.eligible[0]

    @property
    def is_out(self) -> bool:
        return (self.status or "").strip().lower() in (
            "ir", "ir+", "ir-", "o", "out", "na"
        )

    @property
    def on_injured_reserve(self) -> bool:
        """IR/IR+ players sit in a separate slot and cost you no active space."""
        return (self.status or "").strip().lower() in ("ir", "ir+", "ir-")


def _parse_line(line: str) -> PlayerRef | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    # Trailing comments, so a line can carry a note (the Yahoo slot, say)
    # without the parser swallowing it as part of the position.
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    # Accept commas or pipes as separators
    parts = [p.strip() for p in text.replace("|", ",").split(",") if p.strip()]
    if not parts:
        return None
    name = parts[0]
    team = parts[1].upper() if len(parts) > 1 else None
    position = parts[2].lower() if len(parts) > 2 else None
    # Optional 4th field: a status straight from Yahoo (IR, IR+, O, OUT, DTD).
    # Yahoo is the authority for your league -- it knows things the public
    # line-combination sites lag on, and when they disagree Yahoo wins.
    status = parts[3].lower() if len(parts) > 3 else None
    return PlayerRef(
        raw=text, name=name, team=team, position=position, status=status
    )


def _read_file(path: Path, template: str) -> list[PlayerRef]:
    if not path.exists():
        path.write_text(template)
        return []
    refs = []
    for line in path.read_text().splitlines():
        ref = _parse_line(line)
        if ref:
            refs.append(ref)
    return refs


def _resolve_all(refs: list[PlayerRef], index: names.NameIndex) -> list[PlayerRef]:
    for ref in refs:
        ref.nhl_id = index.resolve(ref.name, ref.team, ref.position)
    return refs


def load_roster(index: names.NameIndex) -> list[PlayerRef]:
    """Your players. Creates a commented template file on first run."""
    return _resolve_all(_read_file(ROSTER_PATH, ROSTER_TEMPLATE), index)


def load_free_agents(index: names.NameIndex) -> list[PlayerRef]:
    """
    Available players. An empty file means "unknown" rather than "none" --
    callers should treat an empty list as "rank everyone".
    """
    return _resolve_all(_read_file(FREE_AGENT_PATH, FREE_AGENT_TEMPLATE), index)


@dataclasses.dataclass
class Freshness:
    """How much to trust one of the roster files."""

    path: Path
    exists: bool
    age_hours: float
    predates_week: bool
    level: str          # 'missing' | 'fresh' | 'aging' | 'stale'
    message: str

    @property
    def is_stale(self) -> bool:
        return self.level == "stale"

    @property
    def needs_attention(self) -> bool:
        return self.level in ("stale", "missing")


#: A file younger than this is assumed current.
FRESH_HOURS = 12
#: Beyond this it is treated as stale regardless of the fantasy week.
STALE_HOURS = 36


def freshness(path: Path, week_start: _dt.date | None = None) -> Freshness:
    """
    Judge how current a roster file is.

    These files are a snapshot taken from the Yahoo league pages, and they
    persist on disk. That is the dangerous part: when they go out of date
    nothing fails, the bot simply carries on giving confident advice about a
    roster that no longer exists. Age alone is a weak signal, so this also
    checks whether the file predates the current fantasy week -- adds reset
    weekly and rosters churn most at the turn of a week, so a file written
    before Monday is suspect however recently that was.
    """
    if not path.exists():
        return Freshness(
            path=path, exists=False, age_hours=float("inf"),
            predates_week=True, level="missing",
            message=f"{path.name} has not been created yet.",
        )

    modified = _dt.datetime.fromtimestamp(path.stat().st_mtime)
    age_hours = (_dt.datetime.now() - modified).total_seconds() / 3600

    if week_start is None:
        today = _dt.date.today()
        week_start = today - _dt.timedelta(days=today.weekday())
    predates_week = modified.date() < week_start

    if predates_week:
        level = "stale"
        message = (
            f"{path.name} was last updated {modified:%a %d %b %H:%M}, before "
            f"this fantasy week began. Rosters and free agents turn over at "
            f"the start of a week — re-read the league before trusting this."
        )
    elif age_hours > STALE_HOURS:
        level = "stale"
        message = (
            f"{path.name} is {age_hours / 24:.1f} days old "
            f"({modified:%a %d %b %H:%M})."
        )
    elif age_hours > FRESH_HOURS:
        level = "aging"
        message = (
            f"{path.name} was updated {age_hours:.0f} hours ago "
            f"({modified:%a %H:%M})."
        )
    else:
        level = "fresh"
        message = f"{path.name} updated {age_hours:.0f}h ago."

    return Freshness(
        path=path, exists=True, age_hours=round(age_hours, 1),
        predates_week=predates_week, level=level, message=message,
    )


def yahoo_api_available() -> bool:
    """
    True once Yahoo approves API access again, at which point the roster and
    free-agent lists should come from the league rather than these files.
    """
    try:
        import requests

        from auth.yahoo_oauth import get_valid_token

        token = get_valid_token()
        resp = requests.get(
            "https://fantasysports.yahooapis.com/fantasy/v2/game/nhl",
            params={"format": "json"},
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def describe(refs: list[PlayerRef], label: str) -> str:
    """Human-readable load report, including anything that failed to resolve."""
    resolved = [r for r in refs if r.nhl_id]
    failed = [r for r in refs if not r.nhl_id]
    lines = [f"{label}: {len(resolved)} resolved, {len(failed)} unresolved"]
    for ref in failed:
        lines.append(
            f"   ? {ref.raw}  -- no NHL match; add team/position, "
            f"e.g. '{ref.name}, TOR'"
        )
    return "\n".join(lines)


def _demo() -> None:  # pragma: no cover - manual smoke check
    import data.nhl_stats_rest as nhl_rest

    index = names.NameIndex.from_nhl_rows(nhl_rest.skater_summary_season())
    for pid, row in nhl_rest.goalie_summary_season().items():
        index.add(pid, row.get("goalieFullName", ""), None, "G")

    roster = load_roster(index)
    fas = load_free_agents(index)

    print(describe(roster, "Roster"))
    print(describe(fas, "Free agents"))
    if not roster:
        print(f"\nNo roster yet. Paste your players into:\n  {ROSTER_PATH}")
    print(f"\nYahoo API reachable: {yahoo_api_available()}")


if __name__ == "__main__":
    _demo()
