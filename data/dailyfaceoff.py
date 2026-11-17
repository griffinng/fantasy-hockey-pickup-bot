"""
Daily Faceoff line-combination adapter.

Daily Faceoff is the best public source for *current* deployment: which line a
forward is on, which power-play unit he is on, and whether he is a game-time
decision. The NHL's own API tells us what a player's usage has *been*; DFO tells
us what it is *about to be*. The gap between those two is where waiver value
lives.

Implementation notes:

* The page is a server-rendered Next.js app, so the full line-up is already in
  the `__NEXT_DATA__` JSON blob. We parse that rather than scraping the rendered
  HTML -- it is structured, typed, and does not break every time they adjust a
  CSS class.
* `robots.txt` allows `/teams/*/line-combinations` (only `/api/` and `/cms/` are
  disallowed), so this path is fair game.
* We are deliberately polite: one request per team, a 6-hour cache, a short
  delay between teams, and a descriptive User-Agent. A full refresh is 32
  requests, which we do a few times a day at most.
* Everything degrades gracefully. If a page changes shape or fails, that team
  simply has no DFO data and `features/roles.py` falls back to inferring role
  from NHL power-play share alone.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
import time
from typing import Iterator

import requests

import data.cache as cache
import data.http as _http

BASE = "https://www.dailyfaceoff.com"
LINES_PATH = "/teams/{slug}/line-combinations"

CACHE_TTL_LINES = 6 * 3600  # lines move once or twice a day at most
_REQUEST_DELAY = 1.0        # seconds between team requests

_SESSION = _http.build_session(
    # Generic and descriptive. No personal information.
    "fantasy-hockey-dashboard/2.0 (personal fantasy tool)",
    Accept="text/html,application/xhtml+xml",
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)

# DFO slug -> standard NHL abbreviation. DFO's own `shortName` matches NHL
# abbreviations except where noted; this map is refreshed by `refresh_team_map()`.
TEAM_SLUGS: dict[str, str] = {
    "anaheim-ducks": "ANA", "boston-bruins": "BOS", "buffalo-sabres": "BUF",
    "calgary-flames": "CGY", "carolina-hurricanes": "CAR",
    "chicago-blackhawks": "CHI", "colorado-avalanche": "COL",
    "columbus-blue-jackets": "CBJ", "dallas-stars": "DAL",
    "detroit-red-wings": "DET", "edmonton-oilers": "EDM",
    "florida-panthers": "FLA", "los-angeles-kings": "LAK",
    "minnesota-wild": "MIN", "montreal-canadiens": "MTL",
    "nashville-predators": "NSH", "new-jersey-devils": "NJD",
    "new-york-islanders": "NYI", "new-york-rangers": "NYR",
    "ottawa-senators": "OTT", "philadelphia-flyers": "PHI",
    "pittsburgh-penguins": "PIT", "san-jose-sharks": "SJS",
    "seattle-kraken": "SEA", "st-louis-blues": "STL",
    "tampa-bay-lightning": "TBL", "toronto-maple-leafs": "TOR",
    "utah-mammoth": "UTA", "vancouver-canucks": "VAN",
    "vegas-golden-knights": "VGK", "washington-capitals": "WSH",
    "winnipeg-jets": "WPG",
}

ABBREV_TO_SLUG = {v: k for k, v in TEAM_SLUGS.items()}


@dataclasses.dataclass
class DfoPlayer:
    """One player's currently-listed deployment on his team."""

    dfo_id: int
    name: str
    team: str
    position: str            # 'lw' | 'c' | 'rw' | 'ld' | 'rd' | 'g'
    line_rank: int | None    # 1-4 for forwards, 1-3 for D pairs, None otherwise
    pp_unit: int | None      # 1 or 2, None if not on a PP unit
    pk_unit: int | None      # 1 or 2, None if not on a PK unit
    is_goalie: bool
    injury_status: str       # '' when healthy
    game_time_decision: bool
    as_of: str               # ISO timestamp DFO last updated the page
    source: str              # DFO's cited beat-reporter source

    @property
    def is_forward(self) -> bool:
        return self.position in ("lw", "c", "rw")

    @property
    def role_label(self) -> str:
        bits = []
        if self.line_rank and self.is_forward:
            bits.append(f"L{self.line_rank}")
        elif self.line_rank:
            bits.append(f"D{self.line_rank}")
        if self.pp_unit:
            bits.append(f"PP{self.pp_unit}")
        return " / ".join(bits) if bits else "-"


def _group_to_rank(group: str | None) -> tuple[int | None, int | None, int | None]:
    """
    Map DFO's groupIdentifier to (line_rank, pp_unit, pk_unit).

    Groups look like: f1-f4 (forward lines), d1-d4 (pairings),
    pp1/pp2, pk1/pk2, g (goalies).
    """
    if not group:
        return None, None, None
    group = group.lower()
    if group.startswith("pp"):
        return None, _trailing_int(group), None
    if group.startswith("pk"):
        return None, None, _trailing_int(group)
    if group.startswith(("f", "d")):
        return _trailing_int(group), None, None
    return None, None, None


def _trailing_int(value: str) -> int | None:
    m = re.search(r"(\d+)$", value)
    return int(m.group(1)) if m else None


# Real on-ice positions. Daily Faceoff reuses the position field for roster
# slots in its injured-reserve and scratch groups ('ir4', 'ir5', ...), which
# are slot labels, not positions -- taking them literally overwrote Cale
# Makar's position with "IR5" and dropped him out of the defence pool.
_REAL_POSITIONS = {"lw", "c", "rw", "l", "r", "ld", "rd", "d"}


def _clean_position(value: str | None) -> str:
    pos = (value or "").lower()
    return pos if pos in _REAL_POSITIONS else ""


def _is_goalie_row(position: str | None, group: str | None) -> bool:
    pos = (position or "").lower()
    return pos.startswith("g") or (group or "").lower() == "g"


def _fetch_team_payload(slug: str) -> dict | None:
    """Fetch and parse one team's `combinations` payload. None on any failure."""
    cached = cache.get("dfo_lines", slug)
    if cached is not None:
        return cached

    url = BASE + LINES_PATH.format(slug=slug)
    try:
        resp = _SESSION.get(url, timeout=25)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
        combos = payload["props"]["pageProps"]["combinations"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    cache.set("dfo_lines", combos, CACHE_TTL_LINES, slug)
    return combos


def team_lines(team: str) -> list[DfoPlayer]:
    """
    Current deployment for one team, by NHL abbreviation (e.g. 'PHI').

    A player appears once per group he belongs to -- Michkov shows up in both
    `f1` and `pp1` -- so we merge those rows into a single record per player.
    """
    slug = ABBREV_TO_SLUG.get(team.upper())
    if not slug:
        return []

    combos = _fetch_team_payload(slug)
    if not combos:
        return []

    abbrev = combos.get("teamAbbreviation", team).upper()
    as_of = combos.get("updatedAt", "")
    source = combos.get("sourceName", "")

    merged: dict[int, DfoPlayer] = {}
    for row in combos.get("players", []):
        pid = row.get("playerId")
        if pid is None:
            continue
        pid = int(pid)
        group = row.get("groupIdentifier")
        line_rank, pp_unit, pk_unit = _group_to_rank(group)
        raw_position = row.get("positionIdentifier")
        position = _clean_position(raw_position)

        existing = merged.get(pid)
        if existing is None:
            merged[pid] = DfoPlayer(
                dfo_id=pid,
                name=row.get("name", ""),
                team=abbrev,
                position=position,
                line_rank=line_rank,
                pp_unit=pp_unit,
                pk_unit=pk_unit,
                is_goalie=_is_goalie_row(raw_position, group),
                injury_status=(row.get("injuryStatus") or ""),
                game_time_decision=bool(row.get("gameTimeDecision")),
                as_of=as_of,
                source=source,
            )
        else:
            # Fill in whichever group this row represents.
            if line_rank is not None and existing.line_rank is None:
                existing.line_rank = line_rank
            # The even-strength row carries the real position; power-play and
            # IR rows do not, so only ever upgrade from blank.
            if position and not existing.position:
                existing.position = position
            if pp_unit is not None:
                existing.pp_unit = pp_unit
            if pk_unit is not None:
                existing.pk_unit = pk_unit
            if row.get("injuryStatus"):
                existing.injury_status = row["injuryStatus"]
            if row.get("gameTimeDecision"):
                existing.game_time_decision = True

    return list(merged.values())


def all_lines(teams: list[str] | None = None, *, progress: bool = False) -> list[DfoPlayer]:
    """
    Deployment for every team. ~32 requests on a cold cache, one per team,
    spaced out; near-instant once warm.
    """
    teams = teams or sorted(ABBREV_TO_SLUG)
    out: list[DfoPlayer] = []
    for i, team in enumerate(teams):
        slug = ABBREV_TO_SLUG.get(team.upper())
        was_cached = slug is not None and cache.get("dfo_lines", slug) is not None
        players = team_lines(team)
        out.extend(players)
        if progress:
            print(f"  [{i + 1:2d}/{len(teams)}] {team}: {len(players):2d} players"
                  f"{'' if not was_cached else '  (cached)'}")
        if not was_cached and i < len(teams) - 1:
            time.sleep(_REQUEST_DELAY)
    return out


def staleness_hours(player: DfoPlayer, now: _dt.datetime | None = None) -> float:
    """How old DFO's listing is. Anything over ~36h deserves a warning badge."""
    if not player.as_of:
        return float("inf")
    try:
        stamp = _dt.datetime.fromisoformat(player.as_of.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - stamp).total_seconds() / 3600


def refresh_team_map() -> dict[str, str]:
    """
    Re-derive slug -> abbreviation from DFO's own team list, so a relocation or
    rename doesn't silently drop a team. Returns the fresh mapping.
    """
    combos_page = _SESSION.get(
        BASE + LINES_PATH.format(slug="philadelphia-flyers"), timeout=25
    )
    match = _NEXT_DATA_RE.search(combos_page.text)
    if not match:
        return TEAM_SLUGS
    teams = (
        json.loads(match.group(1))["props"]["pageProps"].get("sortedTeams", [])
    )
    return {t["slug"]: t["shortName"] for t in teams if t.get("slug")}


def _demo() -> None:  # pragma: no cover - manual smoke check
    import sys

    team = sys.argv[1].upper() if len(sys.argv) > 1 else "PHI"
    players = team_lines(team)
    if not players:
        print(f"No Daily Faceoff data for {team}")
        return
    print(f"{team} -- updated {players[0].as_of} (source: {players[0].source})\n")

    for label, key in (
        ("FORWARD LINES", lambda p: p.is_forward and p.line_rank),
        ("DEFENCE PAIRS", lambda p: p.position in ("ld", "rd") and p.line_rank),
        ("POWER PLAY", lambda p: p.pp_unit),
        ("GOALIES", lambda p: p.is_goalie),
    ):
        print(label)
        subset = [p for p in players if key(p)]
        subset.sort(key=lambda p: (p.line_rank or p.pp_unit or 9, p.position))
        for p in subset:
            flags = []
            if p.injury_status:
                flags.append(p.injury_status)
            if p.game_time_decision:
                flags.append("GTD")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"   {p.role_label:<12} {p.position.upper():<3} {p.name}{suffix}")
        print()


if __name__ == "__main__":
    _demo()
