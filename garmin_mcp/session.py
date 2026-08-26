"""One process-wide Garmin client, built from the stored tokens.

Every tool goes through `GarminSession.client()`, which refreshes the OAuth2
access token when needed and writes it back to disk so a restart does not have
to refresh again. Refresh uses the OAuth1 token against connectapi.garmin.com
and never touches Garmin's SSO.

The session also owns the FIT cache: the download-service hands out a ZIP per
call, so an activity that gets analysed and then plotted would be fetched
twice. Cached files live under GARMIN_MCP_CACHE (default ~/.garmin-mcp/cache).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import tokens as token_store
from .client import GarminClient

CACHE_MAX_FILES = 40


class NotConnected(RuntimeError):
    """No Garmin tokens available - `garmin-mcp login` has not run."""


def cache_dir() -> Path:
    env = os.environ.get("GARMIN_MCP_CACHE")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "cache"


class GarminSession:
    def __init__(self) -> None:
        self._client: GarminClient | None = None
        self._lock = asyncio.Lock()
        self._account = ""
        self._persist = os.environ.get(token_store.ENV_BLOB) is None

    async def client(self) -> GarminClient:
        async with self._lock:
            if self._client is None:
                tok = token_store.load()
                if tok is None:
                    raise NotConnected(
                        "no Garmin tokens found. Run `garmin-mcp login` on your own "
                        f"machine (looked at {token_store.token_path()} and "
                        f"${token_store.ENV_BLOB})")
                c = GarminClient()
                o1, o2 = tok.as_json_pair()
                c.restore(o1, o2)
                self._client, self._account = c, tok.account
            before = self._client.oauth2_token.to_json() if self._client.oauth2_token else ""
            await self._client._refresh_if_needed()
            after = self._client.oauth2_token.to_json() if self._client.oauth2_token else ""
            if after != before and self._persist:
                o1, o2 = self._client.export_tokens()
                token_store.save(token_store.from_json_pair(o1, o2, self._account))
            return self._client

    async def display_id(self) -> str:
        """The GUID-ish socialProfile.displayName that the wellness and
        usersummary endpoints want in their path."""
        c = await self.client()
        return await c.profile_display_id()

    @property
    def account(self) -> str:
        return self._account

    async def fit_bytes(self, activity_id: int) -> bytes | None:
        """Original FIT of an activity, cached on disk. None when the original
        is not a FIT (GPX/TCX import)."""
        path = cache_dir() / f"{activity_id}.fit"
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
        except OSError:      # read-only filesystem - the cache is optional
            pass

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def probe(session: GarminSession) -> dict:
    """Who are we connected as - used by the `whoami` tool and `garmin-mcp status`."""
    c = await session.client()
    name = await c.fetch_display_name()
    exp = c.oauth2_token.expires_at if c.oauth2_token else None
    return {"account": name or session.account, "oauth2_expires_at": exp}
