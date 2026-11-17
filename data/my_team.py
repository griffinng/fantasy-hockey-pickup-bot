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


def _parse_line(line: str) -> PlayerRef | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    # Accept commas or pipes as separators
    parts = [p.strip() for p in text.replace("|", ",").split(",") if p.strip()]
    if not parts:
        return None
    name = parts[0]
    team = parts[1].upper() if len(parts) > 1 else None
    position = parts[2].lower() if len(parts) > 2 else None
    return PlayerRef(raw=text, name=name, team=team, position=position)


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
