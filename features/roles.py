"""
Who is actually playing where, and how much.

This module answers the question the old ranker never asked: *what is this
player's job right now?* It fuses two views that disagree in a useful way:

  - **Daily Faceoff** says what the coach is lining up tonight (L1, PP1, ...).
    Current, but it is one reporter's listing and can be a day stale.
  - **The NHL API** says what the player's ice time has actually been over the
    last N days. Factual, but lagging -- a player promoted four games ago still
    looks like a third-liner in a 14-day average.

When those two disagree, that is not noise, that is the signal. A player Daily
Faceoff lists on PP1 whose trailing power-play share is still low was *recently
promoted*, and his box-score numbers have not caught up yet. The rest of the
league is looking at his points; we are looking at his minutes. That is the
`role_change` flag, and it is the single most valuable output here.

The reverse case matters too: still listed on PP1 but trailing share has
collapsed means he is on the way out, and we should not pay for the label.
"""

from __future__ import annotations

import dataclasses

import data.dailyfaceoff as dfo
import data.names as names
import data.nhl_stats_rest as nhl_rest

# A skater is treated as a genuine first-unit power-play player when he is on
# the ice for this much of his team's power-play time. Units are five skaters
# and the first unit usually eats 60-75% of the minutes.
PP1_SHARE_FLOOR = 0.55
PP2_SHARE_FLOOR = 0.25

# Minimum games in the window before trailing numbers mean anything.
MIN_GAMES_FOR_TREND = 3

# How far apart the listed unit and the measured share must be to call it a
# role change rather than ordinary wobble.
ROLE_CHANGE_SHARE_GAP = 0.20


@dataclasses.dataclass
class PlayerRole:
    """Everything we know about one player's deployment."""

    nhl_id: int
    name: str
    team: str
    position: str
    is_goalie: bool = False

    # --- what Daily Faceoff lists right now ---
    line_rank: int | None = None
    pp_unit_listed: int | None = None
    pk_unit: int | None = None
    injury_status: str = ""
    game_time_decision: bool = False
    listing_age_hours: float = float("inf")

    # --- what the NHL data measures over the trailing window ---
    games_window: int = 0
    toi_per_game: float = 0.0        # seconds, all situations
    pp_toi_per_game: float = 0.0     # seconds
    sh_toi_per_game: float = 0.0     # seconds
    pp_share: float = 0.0            # fraction of team PP time
    pp_unit_implied: int | None = None

    # --- the interesting bit ---
    role_change: str = ""            # 'promoted' | 'demoted' | ''
    role_note: str = ""

    @property
    def es_toi_per_game(self) -> float:
        """Even-strength seconds per game: total minus PP minus SH."""
        return max(0.0, self.toi_per_game - self.pp_toi_per_game - self.sh_toi_per_game)

    @property
    def pp_unit(self) -> int | None:
        """
        Best single answer for which unit he is on.

        Daily Faceoff wins when the two disagree, because it is current and the
        trailing average is, by construction, backward-looking. The whole point
        is to act on the promotion before the average catches up.
        """
        if self.pp_unit_listed is not None:
            return self.pp_unit_listed
        return self.pp_unit_implied

    @property
    def role_label(self) -> str:
        bits = []
        if self.line_rank:
            bits.append(("L" if self.position in ("lw", "c", "rw") else "D")
                        + str(self.line_rank))
        if self.pp_unit:
            bits.append(f"PP{self.pp_unit}")
        return " ".join(bits) if bits else "-"

    @property
    def is_playable(self) -> bool:
        """In the line-up, not injured out of it."""
        status = (self.injury_status or "").lower()
        return status not in ("out", "ir", "ltir", "injured reserve")


def _implied_unit(share: float) -> int | None:
    if share >= PP1_SHARE_FLOOR:
        return 1
    if share >= PP2_SHARE_FLOOR:
        return 2
    return None


def _classify_role_change(role: PlayerRole) -> tuple[str, str]:
    """
    Compare the listed unit against the measured share and decide whether the
    player's job just changed.
    """
    listed = role.pp_unit_listed
    share = role.pp_share

    # Not enough recent evidence to claim anything. A player with two games in
    # the window is usually just back from injury, not newly promoted, and
    # presenting that as a role change sends you chasing the wrong players.
    # We mark it separately so the UI can show it without ranking it as a
    # buy signal.
    if role.games_window < MIN_GAMES_FOR_TREND:
        if listed == 1:
            return "unproven", (
                f"listed PP1 but only {role.games_window} game(s) of recent data "
                f"-- likely returning from absence rather than newly promoted"
            )
        return "", ""

    if listed == 1 and share < PP1_SHARE_FLOOR - ROLE_CHANGE_SHARE_GAP:
        return "promoted", (
            f"listed PP1 but has only taken {share:.0%} of team PP time over the "
            f"window -- the promotion is newer than the stat line"
        )
    if listed == 2 and share < PP2_SHARE_FLOOR / 2:
        return "promoted", (
            f"listed PP2 with just {share:.0%} recent PP share -- new to the unit"
        )
    if listed is None and share >= PP1_SHARE_FLOOR:
        return "demoted", (
            f"ran {share:.0%} of PP time recently but is not on a listed unit"
        )
    if listed == 2 and share >= PP1_SHARE_FLOOR:
        return "demoted", (
            f"dropped from first-unit usage ({share:.0%} share) to a listed PP2"
        )
    return "", ""


def build_roles(window_days: int = 14, *, progress: bool = False) -> dict[int, PlayerRole]:
    """
    Build a role record for every skater the NHL has data on, enriched with
    Daily Faceoff deployment wherever we can match the name.

    Returns {nhl_player_id: PlayerRole}.
    """
    if progress:
        print("Loading NHL season/window stats...")

    season_rows = nhl_rest.skater_summary_season()
    window_summary = nhl_rest.skater_summary_window(window_days)
    window_pp = nhl_rest.skater_powerplay_window(window_days)
    window_pk = nhl_rest.skater_penaltykill_window(window_days)

    index = names.NameIndex.from_nhl_rows(season_rows)

    roles: dict[int, PlayerRole] = {}
    for pid, row in season_rows.items():
        team = (row.get("teamAbbrevs") or "").split(",")[-1].strip()
        summary = window_summary.get(pid, {})
        pp = window_pp.get(pid, {})
        pk = window_pk.get(pid, {})
        share = pp.get("ppTimeOnIcePctPerGame") or 0.0

        roles[pid] = PlayerRole(
            nhl_id=pid,
            name=row.get("skaterFullName", ""),
            team=team,
            position=(row.get("positionCode") or "").lower(),
            games_window=int(summary.get("gamesPlayed") or 0),
            toi_per_game=float(summary.get("timeOnIcePerGame") or 0.0),
            pp_toi_per_game=float(pp.get("ppTimeOnIcePerGame") or 0.0),
            sh_toi_per_game=float(pk.get("shTimeOnIcePerGame") or 0.0),
            pp_share=float(share),
            pp_unit_implied=_implied_unit(float(share)),
        )

    # --- overlay Daily Faceoff deployment ---------------------------------
    if progress:
        print("Loading Daily Faceoff line combinations...")
    listings = dfo.all_lines(progress=progress)

    matched = 0
    for listing in listings:
        if listing.is_goalie:
            continue
        pid = index.resolve(listing.name, listing.team, listing.position)
        if pid is None or pid not in roles:
            continue
        matched += 1
        role = roles[pid]
        role.line_rank = listing.line_rank
        role.pp_unit_listed = listing.pp_unit
        role.pk_unit = listing.pk_unit
        role.injury_status = listing.injury_status
        role.game_time_decision = listing.game_time_decision
        role.listing_age_hours = dfo.staleness_hours(listing)
        # DFO's position is more specific than the NHL's (lw/rw vs plain 'l'/'r'),
        # but it is blank for players listed only on IR or a special-teams unit.
        # Never overwrite a known NHL position with nothing.
        if listing.position:
            role.position = listing.position
        # Daily Faceoff knows the current team; the season row may be stale
        # for a recently traded player.
        if listing.team:
            role.team = listing.team

    for role in roles.values():
        role.role_change, role.role_note = _classify_role_change(role)

    if progress:
        print(f"\nMatched {matched} Daily Faceoff listings to NHL ids.")
        if index.unmatched:
            print(index.report_unmatched())

    return roles


def _demo() -> None:  # pragma: no cover - manual smoke check
    import sys

    team_filter = sys.argv[1].upper() if len(sys.argv) > 1 else None

    roles = build_roles(progress=True)
    rows = [r for r in roles.values() if r.line_rank or r.pp_unit]
    if team_filter:
        rows = [r for r in rows if r.team == team_filter]

    rows.sort(key=lambda r: (r.team, r.line_rank or 9, -(r.pp_share or 0)))

    print(f"\n{'PLAYER':<24} {'TM':<4} {'ROLE':<9} {'TOI':>6} {'ES':>6} {'PP':>6} {'SHARE':>6}  FLAG")
    print("-" * 84)
    for r in rows:
        flag = ""
        if r.role_change == "promoted":
            flag = "** PROMOTED"
        elif r.role_change == "demoted":
            flag = "   demoted"
        if not r.is_playable:
            flag += f"  [{r.injury_status}]"
        print(
            f"{r.name:<24} {r.team:<4} {r.role_label:<9} "
            f"{nhl_rest.seconds_to_mmss(r.toi_per_game):>6} "
            f"{nhl_rest.seconds_to_mmss(r.es_toi_per_game):>6} "
            f"{nhl_rest.seconds_to_mmss(r.pp_toi_per_game):>6} "
            f"{r.pp_share:>5.0%}  {flag}"
        )

    promoted = [r for r in roles.values() if r.role_change == "promoted" and r.is_playable]
    print(f"\n{len(promoted)} players flagged as recently promoted league-wide.")


if __name__ == "__main__":
    _demo()
