"""
Yahoo OAuth 2.0 — manual flow using requests-oauthlib.
Run once to authenticate: python -m auth.yahoo_oauth
Token is saved to token.json and auto-refreshed on future runs.
"""

from __future__ import annotations
from pathlib import Path
import json
import time
import webbrowser
import config

TOKEN_PATH = Path(__file__).parent.parent / "token.json"

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
REDIRECT_URI = "https://localhost:8080"


def _save_token(token: dict):
    TOKEN_PATH.write_text(json.dumps(token, indent=2))


def _load_token() -> dict | None:
    if TOKEN_PATH.exists():
        return json.loads(TOKEN_PATH.read_text())
    return None


def _refresh_token(token: dict) -> dict:
    import requests
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "redirect_uri": REDIRECT_URI,
        },
        auth=(config.YAHOO_CONSUMER_KEY, config.YAHOO_CONSUMER_SECRET),
        timeout=10,
    )
    resp.raise_for_status()
    new_token = resp.json()
    new_token["obtained_at"] = time.time()
    _save_token(new_token)
    return new_token


def get_valid_token() -> dict:
    """Return a valid access token, refreshing if expired."""
    token = _load_token()
    if token:
        age = time.time() - token.get("obtained_at", 0)
        if age < token.get("expires_in", 3600) - 60:
            return token
        return _refresh_token(token)

    # No token — run full auth flow
    return _run_auth_flow()


def _run_auth_flow() -> dict:
    import requests
    from urllib.parse import urlencode, urlparse, parse_qs

    if not config.YAHOO_CONSUMER_KEY or not config.YAHOO_CONSUMER_SECRET:
        raise ValueError("Missing YAHOO_CONSUMER_KEY or YAHOO_CONSUMER_SECRET in .env")

    params = {
        "client_id": config.YAHOO_CONSUMER_KEY,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print("\n" + "="*60)
    print("Opening Yahoo login in your browser...")
    print("="*60)
    print("\n1. Log in to Yahoo if prompted")
    print("2. Click 'Agree' to grant access")
    print("3. Browser will show a 'can't connect' or 'connection refused' page")
    print("   — that is EXPECTED and fine")
    print("4. Copy the full URL from the address bar anyway")
    print("\nThe URL will look like:")
    print("  https://localhost:8080/?code=XXXXXXXX&state=...\n")

    webbrowser.open(auth_url)

    redirected_url = input("Paste the full URL from your browser here:\n> ").strip()

    # Extract code from whatever URL they pasted
    parsed = urlparse(redirected_url)
    params_parsed = parse_qs(parsed.query)

    if "code" not in params_parsed:
        # Maybe they only pasted the code itself
        if len(redirected_url) > 10 and "http" not in redirected_url:
            code = redirected_url
        else:
            raise ValueError(
                "Could not find 'code' in the URL. "
                "Make sure you copied the full URL immediately after clicking Agree."
            )
    else:
        code = params_parsed["code"][0]

    print(f"\nExchanging code for access token...")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(config.YAHOO_CONSUMER_KEY, config.YAHOO_CONSUMER_SECRET),
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()
    token["obtained_at"] = time.time()
    _save_token(token)

    print("Success! Token saved to token.json.")
    return token


def get_yahoo_query():
    """Return an authenticated YahooFantasySportsQuery object."""
    from yfpy.query import YahooFantasySportsQuery

    token = get_valid_token()

    # Write private.json so YFPY can find credentials
    private = {
        "consumer_key": config.YAHOO_CONSUMER_KEY,
        "consumer_secret": config.YAHOO_CONSUMER_SECRET,
    }
    private_path = Path(__file__).parent.parent / "private.json"
    private_path.write_text(json.dumps(private))

    # Write token in yahoo_oauth format so YFPY picks it up
    yahoo_token = {
        "access_token": token["access_token"],
        "consumer_key": config.YAHOO_CONSUMER_KEY,
        "consumer_secret": config.YAHOO_CONSUMER_SECRET,
        "guid": None,
        "refresh_token": token["refresh_token"],
        "session_handle": None,
        "token_time": token.get("obtained_at", time.time()),
        "token_type": "bearer",
    }
    token_path = Path(__file__).parent.parent / "yahoo_oauth2.json"
    token_path.write_text(json.dumps(yahoo_token))

    query = YahooFantasySportsQuery(
        auth_dir=str(Path(__file__).parent.parent),
        league_id=config.YAHOO_LEAGUE_ID,
        game_code="nhl",
        game_id=config.YAHOO_GAME_KEY,
        consumer_key=config.YAHOO_CONSUMER_KEY,
        consumer_secret=config.YAHOO_CONSUMER_SECRET,
    )
    return query


if __name__ == "__main__":
    _run_auth_flow()
    print("\nYou can now run the dashboard: streamlit run app.py")
