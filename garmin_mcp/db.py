"""SQLite storage. One connection per operation, no ORM.

Starlette runs sync endpoints in a threadpool, so a shared connection would be
used across threads. Opening per call sidesteps that entirely and costs
nothing at this scale - this database serves a handful of invited people.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until_ms INTEGER,
    is_admin      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invites (
    code          TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    used_at_ms    INTEGER,
    used_by       INTEGER
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token         TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_tokens (
    user_id       INTEGER PRIMARY KEY,
    oauth1_enc    TEXT NOT NULL,
    oauth2_enc    TEXT NOT NULL,
    account       TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token          TEXT PRIMARY KEY,
    refresh_token         TEXT NOT NULL,
    client_id             TEXT NOT NULL,
    user_id               INTEGER NOT NULL,
    scope                 TEXT,
    access_expires_at_ms  INTEGER NOT NULL,
    refresh_expires_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_oauth_tokens_refresh ON oauth_tokens(refresh_token);
CREATE INDEX IF NOT EXISTS ix_oauth_tokens_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_web_sessions_user ON web_sessions(user_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def db_path() -> Path:
    env = os.environ.get("MCP_DB")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "app.db"


@contextmanager
def conn():
    """Careful: an exception passing through the `yield` skips the commit, so
    writes that must survive a failure (a failed-login counter, for instance)
    have to be committed before the exception is raised.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c) -> None:
    """Additive migrations for databases created by an earlier version.

    Small enough to keep here; a migration framework for two ALTERs would be
    ceremony. Each step must be safe to run again.
    """
    columns = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "is_admin" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    # A database with accounts but no admin predates the admin page: the oldest
    # account is the person who set the server up.
    has_users = c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    has_admin = c.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    if has_users and not has_admin:
        c.execute("UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)")
