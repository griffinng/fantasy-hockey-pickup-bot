import sqlite3
import json
import time
import hashlib
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "cache.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache "
        "(key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"
    )
    conn.commit()
    return conn


def _make_key(namespace: str, *args) -> str:
    raw = namespace + ":" + ":".join(str(a) for a in args)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(namespace: str, *args):
    key = _make_key(namespace, *args)
    with _conn() as conn:
        row = conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    value, expires_at = row
    if time.time() > expires_at:
        return None
    return json.loads(value)


def set(namespace: str, data, ttl: int, *args):
    key = _make_key(namespace, *args)
    expires_at = time.time() + ttl
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), expires_at),
        )


def bust(namespace: str = None):
    with _conn() as conn:
        if namespace:
            conn.execute(
                "DELETE FROM cache WHERE key IN "
                "(SELECT key FROM cache WHERE key LIKE ?)",
                (f"%{namespace}%",),
            )
        else:
            conn.execute("DELETE FROM cache")
