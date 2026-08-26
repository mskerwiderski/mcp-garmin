"""OAuth 2.1 authorization server for the remote MCP endpoint, multi-tenant.

Ported from MyFITContainer app/oauth.py and kept structurally identical - DCR,
PKCE S256, refresh grant and the two discovery documents are unchanged. What
changed for multiple users:

  - clients and tokens live in SQLite instead of a JSON file,
  - every auth code and token pair carries the user_id it was issued for,
  - the consent screen identifies the user by their session cookie instead of
    asking for one shared passphrase.

Neither claude.ai nor ChatGPT offers a field for a static bearer header, so for
those clients this authorization server is not optional.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import urllib.parse
from base64 import urlsafe_b64encode
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import users, web
from .db import conn, now_ms
from .web import esc, page

ACCESS_TTL_MS = 60 * 60 * 1000              # 1 h
REFRESH_TTL_MS = 30 * 24 * 60 * 60 * 1000   # 30 d
CODE_TTL_MS = 60 * 1000                     # 60 s
SCOPE = "mcp"

# Auth codes live 60 seconds and are consumed once - memory is the right place.
_CODES: dict[str, dict] = {}


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _new_token() -> str:
    return _b64url(secrets.token_bytes(32))


def _verify_pkce_s256(verifier: str, challenge: str) -> bool:
    computed = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(computed, challenge)


def _base_url(request: Request) -> str:
    return web.base_url(request)


# --- storage ---------------------------------------------------------------


def prune_expired() -> None:
    now = now_ms()
    for code, rec in list(_CODES.items()):
        if rec["expires_at_ms"] < now:
            del _CODES[code]
    with conn() as c:
        c.execute("DELETE FROM oauth_tokens WHERE refresh_expires_at_ms < ?", (now,))
        c.execute("DELETE FROM web_sessions WHERE expires_at_ms < ?", (now,))


def access_token_user(token: str) -> int | None:
    """The user this bearer belongs to, or None. Also the point where a
    disabled account loses access: the join drops inactive users."""
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT t.user_id FROM oauth_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.access_token=? AND t.access_expires_at_ms >= ?"
            " AND u.status='active'", (token, now_ms())).fetchone()
    return int(row["user_id"]) if row else None


# --- discovery -------------------------------------------------------------


def get_protected_resource_metadata(request: Request) -> Response:
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    })


def get_authorization_server_metadata(request: Request) -> Response:
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    })


# --- dynamic client registration -------------------------------------------


async def post_register(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    uris_in = body.get("redirect_uris") if isinstance(body, dict) else None
    redirect_uris = [u for u in uris_in if isinstance(u, str)] if isinstance(uris_in, list) else []
    if not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "redirect_uris required"}, status_code=400)
    for u in redirect_uris:
        parsed = urllib.parse.urlparse(u)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            return JSONResponse({"error": "invalid_redirect_uri",
                                 "error_description": f"must be https: {u}"}, status_code=400)

    name_in = body.get("client_name") if isinstance(body, dict) else None
    name = (name_in.strip()[:200] if isinstance(name_in, str) and name_in.strip()
            else "Unnamed MCP client")
    client_id = _new_token()
    with conn() as c:
        c.execute("INSERT INTO oauth_clients (client_id, client_name, redirect_uris,"
                  " created_at_ms) VALUES (?,?,?,?)",
                  (client_id, name, json.dumps(redirect_uris), now_ms()))
    return JSONResponse({
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": now_ms() // 1000,
        "scope": SCOPE,
    }, status_code=201)


# --- authorize -------------------------------------------------------------


@dataclass(frozen=True)
class _AuthorizeRequest:
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    scope: str | None
    client_name: str


def _redirect_error(redirect_uri: str, state: str | None, error: str, desc: str) -> Response:
    url = urllib.parse.urlparse(redirect_uri)
    q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    q.update({"error": error, "error_description": desc})
    if state:
        q["state"] = state
    return RedirectResponse(url._replace(query=urllib.parse.urlencode(q)).geturl(),
                            status_code=302)


def _get_client(client_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM oauth_clients WHERE client_id=?",
                        (client_id,)).fetchone()
    return dict(row) if row else None


def _parse_authorize(params: dict[str, str]) -> tuple[_AuthorizeRequest | None, Response | None]:
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    if not client_id:
        return None, _error_html("Missing client_id", 400)
    client = _get_client(client_id)
    if not client:
        return None, _error_html("Unknown client_id", 400)
    if not redirect_uri or redirect_uri not in json.loads(client["redirect_uris"]):
        return None, _error_html("redirect_uri not registered for this client", 400)
    if params.get("response_type") != "code":
        return None, _redirect_error(redirect_uri, params.get("state"),
                                     "unsupported_response_type", "only code supported")
    code_challenge = params.get("code_challenge")
    if not code_challenge or (params.get("code_challenge_method") or "S256") != "S256":
        return None, _redirect_error(redirect_uri, params.get("state"),
                                     "invalid_request", "PKCE S256 required")
    return _AuthorizeRequest(client_id, redirect_uri, code_challenge,
                             params.get("state"), params.get("scope"),
                             client["client_name"]), None


async def get_authorize(request: Request) -> Response:
    user_id = web.current_user(request)
    if user_id is None:
        return web.login_redirect(request)
    req, err = _parse_authorize(dict(request.query_params))
    if err is not None:
        return err
    assert req is not None
    return page("Authorize access", _consent_html(req, user_id))


async def post_authorize(request: Request) -> Response:
    user_id = web.current_user(request)
    if user_id is None:
        return _error_html("Not logged in", 401)
    form = await request.form()
    params = {k: str(v) for k, v in form.items() if isinstance(v, str)}
    req, err = _parse_authorize(params)
    if err is not None:
        return err
    assert req is not None
    if str(form.get("decision") or "") != "allow":
        return _redirect_error(req.redirect_uri, req.state, "access_denied", "denied")

    code = _new_token()
    _CODES[code] = {"client_id": req.client_id, "redirect_uri": req.redirect_uri,
                    "code_challenge": req.code_challenge, "scope": req.scope,
                    "user_id": user_id, "expires_at_ms": now_ms() + CODE_TTL_MS}
    url = urllib.parse.urlparse(req.redirect_uri)
    q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    q["code"] = code
    if req.state:
        q["state"] = req.state
    return RedirectResponse(url._replace(query=urllib.parse.urlencode(q)).geturl(),
                            status_code=302)


# --- token -----------------------------------------------------------------


def _mint_pair(client_id: str, user_id: int, scope: str | None) -> dict:
    access, refresh = _new_token(), _new_token()
    now = now_ms()
    with conn() as c:
        c.execute("INSERT INTO oauth_tokens (access_token, refresh_token, client_id,"
                  " user_id, scope, access_expires_at_ms, refresh_expires_at_ms)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (access, refresh, client_id, user_id, scope,
                   now + ACCESS_TTL_MS, now + REFRESH_TTL_MS))
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MS // 1000, "refresh_token": refresh,
            "scope": scope or SCOPE}


async def post_token(request: Request) -> Response:
    prune_expired()
    try:
        form = await request.form()
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    def g(k: str) -> str | None:
        v = form.get(k)
        return v if isinstance(v, str) else None

    grant = g("grant_type")
    if grant == "authorization_code":
        code, redirect_uri = g("code"), g("redirect_uri")
        client_id, verifier = g("client_id"), g("code_verifier")
        if not all([code, redirect_uri, client_id, verifier]):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        rec = _CODES.pop(code, None)
        if (not rec or rec["expires_at_ms"] < now_ms()
                or rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri
                or not _verify_pkce_s256(verifier, rec["code_challenge"])):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_mint_pair(rec["client_id"], rec["user_id"], rec["scope"]))

    if grant == "refresh_token":
        refresh_token, client_id = g("refresh_token"), g("client_id")
        if not refresh_token or not client_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        with conn() as c:
            row = c.execute("SELECT * FROM oauth_tokens WHERE refresh_token=?",
                            (refresh_token,)).fetchone()
            if row is not None:
                c.execute("DELETE FROM oauth_tokens WHERE refresh_token=?",
                          (refresh_token,))
        if (row is None or row["client_id"] != client_id
                or row["refresh_expires_at_ms"] < now_ms()
                or users.get_user(row["user_id"]) is None):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_mint_pair(row["client_id"], row["user_id"], row["scope"]))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --- html ------------------------------------------------------------------


def _consent_html(req: _AuthorizeRequest, user_id: int) -> str:
    user = users.get_user(user_id) or {"email": ""}
    tokens = users.get_garmin_tokens(user_id)
    warn = ("" if tokens else
            '<p class="err">Your Garmin account is not connected yet, so the tools '
            'will have nothing to answer with. You can connect it on your '
            '<a href="/account">account page</a> - this authorization stays valid.</p>')

    def hidden(k: str, v: str | None) -> str:
        return "" if v is None else f'<input type="hidden" name="{k}" value="{esc(v)}">'

    return f"""<h1>Authorize access</h1>
<p class="sub">Signed in as {esc(user["email"])}</p>
<div class="card"><b>{esc(req.client_name)}</b><br>
<span class="mono">Redirect: {esc(req.redirect_uri)}</span></div>
<p>This app is requesting <b>read-only</b> access to <b>your</b> Garmin Connect
data. It cannot see any other account on this server.</p>
{warn}
<form method="post" action="/oauth/authorize">
{hidden("response_type", "code")}{hidden("client_id", req.client_id)}
{hidden("redirect_uri", req.redirect_uri)}{hidden("code_challenge", req.code_challenge)}
{hidden("code_challenge_method", "S256")}{hidden("state", req.state)}
{hidden("scope", req.scope)}
<div class="row"><button name="decision" value="allow">Allow</button>
<button name="decision" value="deny" class="secondary">Deny</button></div></form>"""


def _error_html(msg: str, status: int = 400) -> Response:
    return page("Error", f"<h1>Error</h1><p>{esc(msg)}</p>", status=status)
