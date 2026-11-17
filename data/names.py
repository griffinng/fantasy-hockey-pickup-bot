"""
Player-name matching across sources.

Three systems disagree about how to spell a hockey player: the NHL API
("Matvei Michkov"), Daily Faceoff (sometimes "Matvey"), and Yahoo (which likes
periods: "T.J. Oshie"). Accents come and go, juniors keep their suffix in one
place and lose it in another, and a handful of players simply share a name.

This module resolves a (name, team) pair to an NHL player id, in four escalating
steps, stopping at the first that is unambiguous:

  1. a manual override file, for the handful nothing else gets right
  2. exact normalised match within the same team
  3. exact normalised match league-wide (covers a player traded mid-week)
  4. fuzzy match within the same team, above a similarity floor

Anything still unresolved is reported rather than silently dropped -- a player
who fails to match is a player missing from your rankings, and a silent hole is
worse than a loud one.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path

OVERRIDES_PATH = Path(__file__).parent / "name_overrides.json"

# Below this ratio we refuse to guess.
FUZZY_FLOOR = 0.88

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize(name: str) -> str:
    """
    'T.J. Oshie' -> 'tj oshie'; 'Tim Stützle' -> 'tim stutzle';
    'Zach Werenski Jr.' -> 'zach werenski'.
    """
    if not name:
        return ""
    # Strip accents: Stützle -> Stutzle
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_name.lower()
    # Keep letters and spaces only; "T.J." collapses to "tj"
    cleaned = re.sub(r"[^a-z\s]", "", lowered)
    parts = [p for p in cleaned.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


#: How positions are shown to a human. Daily Faceoff distinguishes left from
#: right defence, which is useful internally -- it is one of the things that
#: tells the two Elias Petterssons apart -- but it is noise on screen, where a
#: defenceman is simply a defenceman.
_DISPLAY_POSITIONS = {
    "ld": "D", "rd": "D", "d": "D",
    "lw": "LW", "rw": "RW", "l": "LW", "r": "RW",
    "c": "C", "g": "G",
}


def display_position(position: str | None) -> str:
    """'rd' -> 'D', 'lw' -> 'LW'. Unknown values pass through upper-cased."""
    if not position:
        return ""
    return _DISPLAY_POSITIONS.get(position.lower(), position.upper())


def _load_overrides() -> dict[str, int]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    return {normalize(k): int(v) for k, v in raw.items()}


class NameIndex:
    """Resolves names to NHL player ids. Build once per run, query many times."""

    # Daily Faceoff position codes -> NHL positionCode.
    _POSITION_MAP = {
        "c": "C", "lw": "L", "rw": "R", "ld": "D", "rd": "D",
        "l": "L", "r": "R", "d": "D",
    }

    def __init__(self) -> None:
        # name -> *list* of ids, because a team really can carry two players
        # with the same name. Vancouver currently rosters two Elias Petterssons,
        # a first-line centre and a second-pair defenceman. Storing a single id
        # here silently overwrote one with the other.
        self._by_team: dict[str, dict[str, list[int]]] = {}
        self._global: dict[str, list[int]] = {}
        self._names: dict[int, str] = {}
        # (team, last name, first initial) -> ids. Catches formal-vs-familiar
        # first names, which is by far the most common mismatch:
        # "Zack Bolduc" (Daily Faceoff) vs "Zachary Bolduc" (NHL).
        self._by_team_surname: dict[tuple[str, str, str], list[int]] = {}
        self._positions: dict[int, str] = {}
        self._overrides = _load_overrides()
        self.unmatched: list[tuple[str, str]] = []
        self.ambiguous: list[tuple[str, str]] = []

    def add(
        self, nhl_id: int, name: str, team: str | None, position: str | None = None
    ) -> None:
        key = normalize(name)
        if not key:
            return
        self._names[nhl_id] = name
        if position:
            self._positions[nhl_id] = position.upper()
        self._global.setdefault(key, [])
        if nhl_id not in self._global[key]:
            self._global[key].append(nhl_id)
        if team:
            team = team.upper()
            bucket = self._by_team.setdefault(team, {}).setdefault(key, [])
            if nhl_id not in bucket:
                bucket.append(nhl_id)
            parts = key.split()
            if len(parts) >= 2:
                surname_key = (team, parts[-1], parts[0][0])
                self._by_team_surname.setdefault(surname_key, [])
                if nhl_id not in self._by_team_surname[surname_key]:
                    self._by_team_surname[surname_key].append(nhl_id)

    def _narrow_by_position(self, ids: list[int], position: str | None) -> list[int]:
        """Use position to break a same-name tie, e.g. the two Elias Petterssons."""
        if len(ids) <= 1 or not position:
            return ids
        want = self._POSITION_MAP.get(position.lower(), position.upper())
        narrowed = [i for i in ids if self._positions.get(i) == want]
        return narrowed or ids

    @classmethod
    def from_nhl_rows(cls, rows: dict[int, dict]) -> "NameIndex":
        """Build from a `nhl_stats_rest` season summary (playerId -> row)."""
        index = cls()
        for pid, row in rows.items():
            name = row.get("skaterFullName") or row.get("goalieFullName") or ""
            team = (row.get("teamAbbrevs") or "").split(",")[-1].strip() or None
            position = row.get("positionCode") or ("G" if row.get("goalieFullName") else None)
            index.add(int(pid), name, team, position)
        return index

    def resolve(
        self, name: str, team: str | None = None, position: str | None = None
    ) -> int | None:
        key = normalize(name)
        if not key:
            return None

        team_key = (team or "").upper()

        # 1. manual override -- most specific key first
        for override_key in (
            f"{key}|{team_key}|{(position or '').lower()}",
            f"{key}|{team_key}",
            key,
        ):
            if override_key in self._overrides:
                return self._overrides[override_key]

        team_map = self._by_team.get(team_key, {})

        # 2. exact, same team (position breaks same-name ties)
        if key in team_map:
            ids = self._narrow_by_position(team_map[key], position)
            if len(ids) == 1:
                return ids[0]
            if len(ids) > 1:
                self.ambiguous.append((name, team_key))
                return None

        # 3. exact, league-wide -- only if unambiguous
        candidates = self._narrow_by_position(self._global.get(key, []), position)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            self.ambiguous.append((name, team_key))
            return None

        # 4. same team, same surname, same first initial.
        #    "Zack Bolduc" -> "Zachary Bolduc". Safe because it is scoped to one
        #    roster, where two players sharing a surname AND an initial is rare;
        #    if it does happen we decline rather than coin-flip.
        parts = key.split()
        if team_key and len(parts) >= 2:
            by_surname = self._narrow_by_position(
                self._by_team_surname.get((team_key, parts[-1], parts[0][0]), []),
                position,
            )
            if len(by_surname) == 1:
                return by_surname[0]

        # 5. fuzzy, same team only
        if team_map:
            close = difflib.get_close_matches(key, team_map.keys(), n=1, cutoff=FUZZY_FLOOR)
            if close:
                ids = self._narrow_by_position(team_map[close[0]], position)
                if len(ids) == 1:
                    return ids[0]

        self.unmatched.append((name, team_key))
        return None

    def display_name(self, nhl_id: int) -> str:
        return self._names.get(nhl_id, str(nhl_id))

    def report_unmatched(self) -> str:
        if not self.unmatched and not self.ambiguous:
            return "All names resolved."
        lines: list[str] = []
        if self.ambiguous:
            lines.append(
                f"{len(set(self.ambiguous))} name(s) were AMBIGUOUS (two players "
                "share the name and position did not separate them):"
            )
            for name, team in sorted(set(self.ambiguous)):
                lines.append(f"  ! {name} ({team or 'no team'})")
        if self.unmatched:
            lines.append(f"{len(set(self.unmatched))} name(s) did not resolve to an NHL id:")
            for name, team in sorted(set(self.unmatched)):
                lines.append(f"  - {name} ({team or 'no team'})")
        lines.append(
            f"\nFix permanently in {OVERRIDES_PATH.name}, keyed by "
            '"name", "name|TEAM", or "name|TEAM|pos" -> nhl_player_id.'
        )
        return "\n".join(lines)
