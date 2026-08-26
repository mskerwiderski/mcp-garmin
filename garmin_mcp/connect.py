"""Connecting a Garmin account, per user.

Two ways in, because neither alone is enough:

  - The web login talks to Garmin's SSO from this server. Convenient, and it is
    the same flow MyFITContainer has been running from this host for months.
    The password is used once for the token exchange and never stored.
  - Pasting the blob from `garmin-mcp export` needs no server-side login at all.
    That is the way out when Cloudflare blocks the datacenter IP, and it is how
    an existing local installation moves its tokens over.

SSO logins are serialised process-wide: several people logging in at the same
second is exactly the pattern that gets an IP rate-limited by Garmin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

from . import users
from .client import GarminClient, GarminError, LoginState
from .session import forget_user

PENDING_TTL_S = 10 * 60

_sso_lock = asyncio.Lock()
_pending: dict[int, tuple[GarminClient, float]] = {}


class ConnectError(RuntimeError):
    """Message meant to be shown to the person in the browser."""


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "403" in text or "cloudflare" in text.lower():
        return ("Garmin refused the login from this server (rate limit or bot "
                "protection). Use the token blob instead - it is the reliable "
                "way in when this happens.")
    return f"Garmin login failed: {text[:200]}"


async def _finish(user_id: int, client: GarminClient) -> str:
    account = ""
    try:
        account = await client.fetch_display_name()
        oauth1, oauth2 = client.export_tokens()
    finally:
        await client.aclose()
    users.set_garmin_tokens(user_id, oauth1, oauth2, account)
    forget_user(user_id)          # drop any half-built session for this account
    return account


def _drop_pending(user_id: int) -> None:
    entry = _pending.pop(user_id, None)
    if entry is not None:
        asyncio.ensure_future(entry[0].aclose())


def _prune_pending() -> None:
    cutoff = time.time() - PENDING_TTL_S
    for uid, (_client, started) in list(_pending.items()):
        if started < cutoff:
            _drop_pending(uid)


async def start_login(user_id: int, email: str, password: str) -> bool:
    """True when the account is connected, False when MFA is still needed."""
    if not email.strip() or not password:
        raise ConnectError("Enter your Garmin e-mail and password.")
    _prune_pending()
    _drop_pending(user_id)
    client = GarminClient()
    async with _sso_lock:
        try:
            state = await client.login(email.strip(), password)
        except GarminError as exc:
            await client.aclose()
            raise ConnectError(_friendly(exc)) from exc
    if state == LoginState.NEEDS_MFA:
        _pending[user_id] = (client, time.time())
        return False
    await _finish(user_id, client)
    return True


async def submit_mfa(user_id: int, code: str) -> None:
    _prune_pending()
    entry = _pending.get(user_id)
    if entry is None:
        raise ConnectError("The login timed out. Please start again.")
    client = entry[0]
    async with _sso_lock:
        try:
            state = await client.submit_mfa(code.strip())
        except GarminError as exc:
            _drop_pending(user_id)
            raise ConnectError(f"{_friendly(exc)} Please start again.") from exc
    if state != LoginState.OK:
        raise ConnectError("That code was not accepted. Try again.")
    _pending.pop(user_id, None)
    await _finish(user_id, client)


def mfa_pending(user_id: int) -> bool:
    _prune_pending()
    return user_id in _pending


def import_blob(user_id: int, blob: str) -> str:
    """Store the tokens from `garmin-mcp export`. Returns the account label."""
    try:
        raw = json.loads(base64.b64decode(blob.strip(), validate=True))
        oauth1, oauth2 = raw["oauth1"], raw["oauth2"]
        if not isinstance(oauth1, dict) or "oauth_token" not in oauth1:
            raise ValueError("no oauth1 token in the blob")
        json.dumps(oauth2)
    except Exception as exc:                             # noqa: BLE001
        raise ConnectError(
            "That does not look like the output of `garmin-mcp export`. Copy the "
            "whole line, without spaces or line breaks.") from exc
    account = str(raw.get("account") or "")
    users.set_garmin_tokens(user_id, json.dumps(oauth1), json.dumps(oauth2), account)
    forget_user(user_id)
    return account


def disconnect(user_id: int) -> None:
    _drop_pending(user_id)
    users.clear_garmin_tokens(user_id)
    forget_user(user_id)
