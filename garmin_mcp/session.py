"""One Garmin client per identity, plus the registry that hands them out.

Two identities exist. Over stdio the identity is "the person who started the
process" and the tokens come from the local file. Over HTTP the identity is the
user behind the bearer token and the tokens come from the database. Everything
else - refresh, persistence of the refreshed token, the FIT cache - is the same
code, which is why the token source is injected rather than branched on.

The FIT cache is namespaced per identity: those files are somebody's training
data, and two accounts must never read each other's.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import shutil
from pathlib import Path

from . import tokens as token_file
from . import users
from .client import GarminClient

CACHE_MAX_FILES = 40

# Set by the HTTP endpoint before it hands the request to the MCP session
# manager; unset over stdio. Verified to survive into the tool coroutine.
CURRENT_USER: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "garmin_mcp_user", default=None)


class NotConnected(RuntimeError):
    """No Garmin tokens for this identity yet."""


def cache_root() -> Path:
    env = os.environ.get("GARMIN_MCP_CACHE")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "cache"


class _FileTokens:
    """stdio: ~/.garmin-mcp/tokens.json or the GARMIN_TOKENS blob."""

    scope = "local"
    hint = ("no Garmin tokens found - run `garmin-mcp login` on your own machine")

    def load(self):
        tok = token_file.load()
        if tok is None:
            return None
        o1, o2 = tok.as_json_pair()
        return o1, o2, tok.account

    def save(self, oauth1: str, oauth2: str, account: str) -> None:
        if os.environ.get(token_file.ENV_BLOB):      # read-only source, nothing to write
            return
        token_file.save(token_file.from_json_pair(oauth1, oauth2, account))


class _UserTokens:
    """HTTP: the encrypted tokens of one account."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.scope = str(user_id)
        self.hint = ("your Garmin account is not connected yet - open the account "
                     "page of this server and connect it")

    def load(self):
        return users.get_garmin_tokens(self.user_id)

    def save(self, oauth1: str, oauth2: str, account: str) -> None:
        users.set_garmin_tokens(self.user_id, oauth1, oauth2, account)


class GarminSession:
    def __init__(self, store) -> None:
        self._store = store
        self._client: GarminClient | None = None
        self._lock = asyncio.Lock()
        self.account = ""

    async def client(self) -> GarminClient:
        async with self._lock:
            if self._client is None:
                loaded = self._store.load()
                if loaded is None:
                    raise NotConnected(self._store.hint)
                oauth1, oauth2, self.account = loaded
                c = GarminClient()
                c.restore(oauth1, oauth2)
                self._client = c
            before = self._client.oauth2_token.to_json() if self._client.oauth2_token else ""
            await self._client._refresh_if_needed()
            after = self._client.oauth2_token.to_json() if self._client.oauth2_token else ""
            if after != before:
                o1, o2 = self._client.export_tokens()
                self._store.save(o1, o2, self.account)
            return self._client

    async def display_id(self) -> str:
        c = await self.client()
        return await c.profile_display_id()

    @property
    def cache_dir(self) -> Path:
        return cache_root() / self._store.scope

    async def fit_bytes(self, activity_id: int) -> bytes | None:
        path = self.cache_dir / f"{activity_id}.fit"
        if path.exists():
            os.utime(path, None)
            return path.read_bytes()
        c = await self.client()
        data = await c.download_original_fit(activity_id)
        if data:
            self._store_cached(path, data)
        return data

    def _store_cached(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            files = sorted(path.parent.glob("*.fit"), key=lambda p: p.stat().st_mtime)
            for old in files[:-CACHE_MAX_FILES]:
                old.unlink(missing_ok=True)
        except OSError:                  # read-only filesystem - the cache is optional
            pass

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- registry --------------------------------------------------------------

_SESSIONS: dict[str, GarminSession] = {}


def local_session() -> GarminSession:
    return _SESSIONS.setdefault("local", GarminSession(_FileTokens()))


def session_for_user(user_id: int) -> GarminSession:
    key = f"u{user_id}"
    if key not in _SESSIONS:
        _SESSIONS[key] = GarminSession(_UserTokens(user_id))
    return _SESSIONS[key]


def current_session() -> GarminSession:
    """Whose data this request is about. Over stdio there is only one answer."""
    user_id = CURRENT_USER.get()
    return local_session() if user_id is None else session_for_user(user_id)


def forget_user(user_id: int) -> None:
    """Drop the cached client and every cached FIT of that account. Called when
    the account is deleted or the user disconnects Garmin."""
    session = _SESSIONS.pop(f"u{user_id}", None)
    if session is not None and session._client is not None:
        client = session._client
        session._client = None
        try:
            asyncio.get_running_loop().create_task(client.aclose())
        except RuntimeError:                       # no loop (CLI) - let GC handle it
            pass
    shutil.rmtree(cache_root() / str(user_id), ignore_errors=True)


async def close_all() -> None:
    for session in list(_SESSIONS.values()):
        await session.aclose()
    _SESSIONS.clear()


async def probe(session: GarminSession) -> dict:
    """Who this identity is connected as - the `whoami` tool and `garmin-mcp
    status` both use it."""
    c = await session.client()
    name = await c.fetch_display_name()
    exp = c.oauth2_token.expires_at if c.oauth2_token else None
    return {"account": name or session.account, "oauth2_expires_at": exp}
