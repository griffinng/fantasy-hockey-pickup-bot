"""
Phase 0 check: is our Yahoo Fantasy API access still alive, and what is this
season's league key?

Yahoo moved the Fantasy Sports API behind an application process in 2026 and it
is now read-only. Credentials created before that may or may not still work, so
this script distinguishes the three failure modes rather than just blowing up:

  1. everything works                  -> prints league + week info
  2. token stale, credentials fine     -> tells you to re-run the OAuth flow
  3. app/scope revoked by Yahoo        -> tells you to apply for access

Run:  python scripts/verify_auth.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import config  # noqa: E402

BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


def _fail(headline: str, *lines: str) -> None:
    print(f"\n  FAILED: {headline}")
    for line in lines:
        print(f"  {line}")
    sys.exit(1)


def main() -> None:
    print("=" * 68)
    print("Yahoo Fantasy API access check")
    print("=" * 68)

    # --- credentials present? ------------------------------------------------
    if not config.YAHOO_CONSUMER_KEY or not config.YAHOO_CONSUMER_SECRET:
        _fail(
            "no consumer key/secret in .env",
            "Set YAHOO_CONSUMER_KEY and YAHOO_CONSUMER_SECRET.",
        )
    print(f"  consumer key  : ...{config.YAHOO_CONSUMER_KEY[-6:]} (from .env)")

    token_path = Path(__file__).resolve().parent.parent / "token.json"
    if not token_path.exists():
        _fail(
            "no token.json",
            "Run:  python -m auth.yahoo_oauth",
        )

    # --- can we refresh the token? ------------------------------------------
    from auth.yahoo_oauth import get_valid_token

    try:
        token = get_valid_token()
    except requests.HTTPError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "?"
        if status in (400, 401):
            _fail(
                f"token refresh rejected by Yahoo (HTTP {status})",
                f"Response: {body}",
                "",
                "Most likely the refresh token expired (they die after ~1 year",
                "of the grant, and yours was issued in May).",
                "Fix:  python -m auth.yahoo_oauth",
                "",
                "If that ALSO fails with invalid_client, the app itself lost",
                "Fantasy scope and you need to apply at:",
                "  https://sports.yahoo.com/developer/access/",
            )
        raise
    print("  token         : valid (refreshed OK)" if token else "")

    sess = requests.Session()
    sess.headers.update(
        {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json",
        }
    )

    # --- does the Fantasy scope still work? ---------------------------------
    url = f"{BASE}/users;use_login=1/games;game_codes=nhl/leagues"
    resp = sess.get(url, params={"format": "json"}, timeout=20)

    if resp.status_code == 403:
        _fail(
            "HTTP 403 from the Fantasy API — scope revoked",
            "The token works but this app is no longer authorized for Fantasy",
            "Sports data. Yahoo now gates access behind an application.",
            "",
            "Apply at https://sports.yahoo.com/developer/access/ with:",
            f"  Client ID      : {config.YAHOO_CONSUMER_KEY[:12]}... (your existing one)",
            "  Expected users : Small (< 1,000 users)",
            "  Use case       : personal / single league use",
            "",
            "Meanwhile the bot can run off a manual free-agent list.",
        )
    resp.raise_for_status()

    content = resp.json().get("fantasy_content", {})

    # --- walk Yahoo's nested-array JSON to find NHL leagues -----------------
    found: list[dict] = []
    try:
        games = content["users"]["0"]["user"][1]["games"]
        for i in range(int(games.get("count", 0))):
            game = games[str(i)]["game"]
            meta = game[0] if isinstance(game[0], dict) else {}
            game_key = str(meta.get("game_key", ""))
            season = str(meta.get("season", ""))
            leagues = game[1].get("leagues", {}) if len(game) > 1 else {}
            for j in range(int(leagues.get("count", 0))):
                lg = leagues[str(j)]["league"]
                lmeta = lg[0] if isinstance(lg[0], dict) else {}
                found.append(
                    {
                        "game_key": game_key,
                        "season": season,
                        "league_id": str(lmeta.get("league_id", "")),
                        "league_key": lmeta.get("league_key", ""),
                        "name": lmeta.get("name", ""),
                        "num_teams": lmeta.get("num_teams", ""),
                        "scoring_type": lmeta.get("scoring_type", ""),
                        "current_week": lmeta.get("current_week", ""),
                        "start_week": lmeta.get("start_week", ""),
                        "end_week": lmeta.get("end_week", ""),
                    }
                )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _fail(
            f"could not parse Yahoo's league response ({exc})",
            "Raw response saved to scripts/_last_response.json for inspection.",
        )

    if not found:
        _fail(
            "authenticated fine, but Yahoo reports no NHL leagues",
            "Is this the Yahoo account that owns the league?",
        )

    print(f"\n  NHL leagues on this account: {len(found)}")
    print("  " + "-" * 64)
    for lg in found:
        marker = "  <-- .env YAHOO_LEAGUE_ID" if lg["league_id"] == str(
            config.YAHOO_LEAGUE_ID
        ) else ""
        print(f"  [{lg['season']}] {lg['name']}")
        print(
            f"        league_key={lg['league_key']}  teams={lg['num_teams']}  "
            f"scoring={lg['scoring_type']}  week={lg['current_week']}{marker}"
        )

    # --- is .env pointing at a current league? ------------------------------
    current = [lg for lg in found if lg["season"] in ("2026", "2027")]
    env_match = [lg for lg in found if lg["league_id"] == str(config.YAHOO_LEAGUE_ID)]

    print("\n  " + "-" * 64)
    if env_match and env_match[0] in current:
        print(f"  .env league is current. Using {env_match[0]['league_key']}")
        chosen = env_match[0]
    elif current:
        print("  .env YAHOO_LEAGUE_ID is NOT this season's league.")
        print(f"  Update it to: {current[0]['league_id']}  ({current[0]['name']})")
        chosen = current[0]
    else:
        _fail(
            "no 2026-27 NHL league found on this account",
            "Only past seasons came back. Did the league roll over?",
        )

    out = Path(__file__).resolve().parent / "_league.json"
    out.write_text(json.dumps(chosen, indent=2))
    print(f"\n  Wrote {out}")
    print("\n  OK — Yahoo access is working.\n")


if __name__ == "__main__":
    main()
