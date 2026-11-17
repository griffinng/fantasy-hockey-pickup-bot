"""
Shared HTTP session with retries.

The NHL's stats API is free, undocumented and occasionally slow -- a request
that normally takes 300ms will sometimes hang until it times out. Without
retries a single unlucky call takes down a whole dashboard render or an
unattended daily run, which is a silly way to lose a report.

So: retry on timeouts, connection errors and the 5xx family, with exponential
backoff, and a generous read timeout. Retries apply to GETs only, which is all
we issue -- nothing here mutates anything, so replaying a request is always
safe.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# (connect, read). The read timeout is deliberately generous: the stats API
# answers a full-league query in a few seconds on a good day.
DEFAULT_TIMEOUT = (10, 45)

_RETRY = Retry(
    total=4,
    connect=3,
    read=3,
    status=3,
    backoff_factor=1.5,            # 0s, 1.5s, 3s, 6s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)


def build_session(user_agent: str, **headers: str) -> requests.Session:
    """A session that retries transient failures instead of giving up."""
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, **headers})
    adapter = HTTPAdapter(max_retries=_RETRY, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class FetchError(RuntimeError):
    """A request failed even after retries. Carries a readable explanation."""


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    timeout: tuple[int, int] | None = None,
) -> dict | list:
    try:
        resp = session.get(url, params=params, timeout=timeout or DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.Timeout as exc:
        raise FetchError(
            f"Timed out fetching {url.split('?')[0]} after retries. "
            f"The NHL API is probably having a slow spell -- try again shortly."
        ) from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise FetchError(f"HTTP {status} from {url.split('?')[0]}.") from exc
    except requests.RequestException as exc:
        raise FetchError(f"Could not reach {url.split('?')[0]}: {exc}") from exc
    except ValueError as exc:
        raise FetchError(f"{url.split('?')[0]} returned malformed JSON.") from exc
