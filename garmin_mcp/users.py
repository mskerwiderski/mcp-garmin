"""Accounts, invitations, web sessions and the per-user Garmin tokens.

Registration is invite-only on purpose: this server holds other people's
health data, so there is no open signup path - not even a hidden one. Without
a valid code the signup page does not exist.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from . import crypto
from .db import conn, now_ms

INVITE_TTL_MS = 7 * 24 * 60 * 60 * 1000
SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000
MAX_FAILED_LOGINS = 5
LOCKOUT_MS = 15 * 60 * 1000

_hasher = PasswordHasher()


class UserError(ValueError):
    """Something the caller can show to a human as-is."""


# --- invites ---------------------------------------------------------------


def create_invite(label: str = "", ttl_ms: int = INVITE_TTL_MS) -> str:
    code = secrets.token_urlsafe(24)
    with conn() as c:
        c.execute("INSERT INTO invites (code, label, created_at_ms, expires_at_ms)"
                  " VALUES (?,?,?,?)",
                  (code, label.strip(), now_ms(), now_ms() + ttl_ms))
    return code


def invite_valid(code: str) -> bool:
    with conn() as c:
        row = c.execute("SELECT used_at_ms, expires_at_ms FROM invites WHERE code=?",
                        (code,)).fetchone()
    return bool(row) and row["used_at_ms"] is None and row["expires_at_ms"] > now_ms()


def list_invites() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM invites ORDER BY created_at_ms DESC").fetchall()
    out = []
    for r in rows:
        state = ("used" if r["used_at_ms"]
                 else "expired" if r["expires_at_ms"] <= now_ms() else "open")
        out.append({"code": r["code"], "label": r["label"], "state": state,
                    "expires_at_ms": r["expires_at_ms"], "used_by": r["used_by"]})
    return out


# --- users -----------------------------------------------------------------


def create_user(email: str, password: str, invite_code: str) -> int:
    email = email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise UserError("Please enter a valid e-mail address.")
    if len(password) < 10:
        raise UserError("The password needs at least 10 characters.")
    if not invite_valid(invite_code):
        raise UserError("This invitation is invalid, already used or expired.")
    with conn() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise UserError("An account with this e-mail already exists.")
        label = (c.execute("SELECT label FROM invites WHERE code=?",
                           (invite_code,)).fetchone() or {"label": ""})["label"]
        # The first account on a fresh server administers it - somebody has to,
        # and it is whoever created the invitation for themselves.
        first = c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        cur = c.execute(
            "INSERT INTO users (email, password_hash, label, created_at_ms, is_admin)"
            " VALUES (?,?,?,?,?)",
            (email, _hasher.hash(password), label, now_ms(), 1 if first else 0))
        user_id = int(cur.lastrowid)
        c.execute("UPDATE invites SET used_at_ms=?, used_by=? WHERE code=?",
                  (now_ms(), user_id, invite_code))
    return user_id


def verify_login(email: str, password: str) -> int:
    """The user id, or raise UserError with a message meant for the login page.

    Every write must be committed before the exception leaves this function -
    `conn()` skips its commit when an exception passes through, so raising
    inside the block would silently roll back the failed-attempt counter.
    """
    email = email.strip().lower()
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            _hasher.hash("dummy")            # keep the timing similar
            problem = "E-mail or password is wrong."
        elif row["status"] != "active":
            problem = "This account is disabled."
        elif row["locked_until_ms"] and row["locked_until_ms"] > now_ms():
            problem = "Too many failed attempts. Try again in a few minutes."
        else:
            problem = ""
            try:
                _hasher.verify(row["password_hash"], password)
            except (VerifyMismatchError, VerificationError):
                failed = row["failed_logins"] + 1
                lock = now_ms() + LOCKOUT_MS if failed >= MAX_FAILED_LOGINS else None
                c.execute("UPDATE users SET failed_logins=?, locked_until_ms=?"
                          " WHERE id=?", (0 if lock else failed, lock, row["id"]))
                problem = "E-mail or password is wrong."
            else:
                c.execute("UPDATE users SET failed_logins=0, locked_until_ms=NULL,"
                          " last_seen_ms=? WHERE id=?", (now_ms(), row["id"]))
                user_id = int(row["id"])
    if problem:
        raise UserError(problem)
    return user_id


def get_user(user_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT id, email, label, status, created_at_ms, last_seen_ms,"
                        " is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    user = get_user(user_id)
    return bool(user and user["is_admin"] and user["status"] == "active")


def set_admin(user_id: int, admin: bool) -> None:
    """Refuses to remove the last admin - locking yourself out of your own
    server would need SSH to undo."""
    with conn() as c:
        if not admin:
            others = c.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1"
                               " AND id<>?", (user_id,)).fetchone()["n"]
            if not others:
                raise UserError("This is the only administrator left.")
        c.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if admin else 0, user_id))


def user_id_by_email(email: str) -> int | None:
    with conn() as c:
        row = c.execute("SELECT id FROM users WHERE email=?",
                        (email.strip().lower(),)).fetchone()
    return int(row["id"]) if row else None


def list_users() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT u.id, u.email, u.label, u.status, u.created_at_ms, u.last_seen_ms,"
            " u.is_admin, g.account AS garmin_account FROM users u"
            " LEFT JOIN garmin_tokens g ON g.user_id = u.id"
            " ORDER BY u.created_at_ms").fetchall()
    return [dict(r) for r in rows]


def set_status(user_id: int, status: str) -> None:
    if status not in ("active", "disabled"):
        raise UserError("status must be active or disabled")
    with conn() as c:
        c.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
        if status == "disabled":                 # cut every live session at once
            c.execute("DELETE FROM web_sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM oauth_tokens WHERE user_id=?", (user_id,))


def delete_user(user_id: int) -> None:
    """Everything of that person: account, tokens, sessions. The FIT cache is
    removed by the caller (cli), which knows the cache directory."""
    with conn() as c:
        for table in ("garmin_tokens", "web_sessions", "oauth_tokens"):
            c.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


# --- web sessions ----------------------------------------------------------


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute("INSERT INTO web_sessions (token, user_id, created_at_ms,"
                  " expires_at_ms) VALUES (?,?,?,?)",
                  (token, user_id, now_ms(), now_ms() + SESSION_TTL_MS))
    return token


def session_user(token: str | None) -> int | None:
    if not token:
        return None
    with conn() as c:
        row = c.execute("SELECT s.user_id FROM web_sessions s JOIN users u"
                        " ON u.id = s.user_id WHERE s.token=? AND s.expires_at_ms>?"
                        " AND u.status='active'", (token, now_ms())).fetchone()
    return int(row["user_id"]) if row else None


def end_session(token: str | None) -> None:
    if token:
        with conn() as c:
            c.execute("DELETE FROM web_sessions WHERE token=?", (token,))


# --- garmin tokens ---------------------------------------------------------


def set_garmin_tokens(user_id: int, oauth1_json: str, oauth2_json: str,
                      account: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO garmin_tokens (user_id, oauth1_enc, oauth2_enc, account,"
            " updated_at_ms) VALUES (?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET oauth1_enc=excluded.oauth1_enc,"
            " oauth2_enc=excluded.oauth2_enc, account=excluded.account,"
            " updated_at_ms=excluded.updated_at_ms",
            (user_id, crypto.encrypt(oauth1_json), crypto.encrypt(oauth2_json),
             account, now_ms()))


def get_garmin_tokens(user_id: int) -> tuple[str, str, str] | None:
    """(oauth1_json, oauth2_json, account) or None when not connected or the
    ciphertext no longer matches APP_SECRET."""
    with conn() as c:
        row = c.execute("SELECT * FROM garmin_tokens WHERE user_id=?",
                        (user_id,)).fetchone()
    if row is None:
        return None
    o1, o2 = crypto.decrypt(row["oauth1_enc"]), crypto.decrypt(row["oauth2_enc"])
    if not o1 or not o2:
        return None
    return o1, o2, row["account"]


def clear_garmin_tokens(user_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM garmin_tokens WHERE user_id=?", (user_id,))
