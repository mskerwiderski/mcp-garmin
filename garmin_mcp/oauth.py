"""Minimal OAuth 2.1 authorization server for the remote MCP endpoint.

Ported from MyFITContainer app/oauth.py (e9931f0) with two changes: the store is
a JSON file instead of SQLModel, and the resource owner is whoever knows
MCP_PASSPHRASE instead of an SSO admin session. This is a single-user server -
there is nothing to log into but the server itself.

Covers exactly what claude.ai and ChatGPT ask for:
  - Dynamic Client Registration (RFC 7591)
  - Authorization Code + PKCE S256 (RFC 6749/7636), refresh grant
  - Authorization Server Metadata (RFC 8414), Protected Resource (RFC 9728)

Neither claude.ai nor ChatGPT lets you type a static bearer header into a custom
connector, so for those clients this is not optional.
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import os
import secrets
import time
import urllib.parse
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                                 Response)

ACCESS_TTL_MS = 60 * 60 * 1000              # 1 h
REFRESH_TTL_MS = 30 * 24 * 60 * 60 * 1000   # 30 d
CODE_TTL_MS = 60 * 1000                     # 60 s
SCOPE = "mcp"
SERVER_NAME = "Garmin Connect"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _new_token() -> str:
    return _b64url(secrets.token_bytes(32))


def _verify_pkce_s256(verifier: str, challenge: str) -> bool:
    computed = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(computed, challenge)


def _base_url(request: Request) -> str:
    configured = os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    host = request.headers.get("host", "localhost")
    proto = request.headers.get("x-forwarded-proto", "https")
    return f"{proto}://{host}"


def passphrase() -> str:
    return os.environ.get("MCP_PASSPHRASE", "")


# --- Storage ---------------------------------------------------------------
#
# Clients and tokens are persisted so a restart does not force every connector
# through the consent screen again. Auth codes live 60 s and stay in memory.

class _Store:
    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}      # access_token -> record
        self.codes: dict[str, dict] = {}
        self._path: Path | None = None

    def bind(self, path: Path | None) -> None:
        self._path = path
        if path and path.exists():
            try:
                raw = json.loads(path.read_text("utf-8"))
                self.clients = raw.get("clients") or {}
                self.tokens = raw.get("tokens") or {}
            except (OSError, ValueError):
                pass

    def flush(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"clients": self.clients,
                                       "tokens": self.tokens}), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self._path)
        except OSError:            # read-only filesystem: keep serving from memory
            pass


STORE = _Store()


def state_path() -> Path:
    env = os.environ.get("MCP_STATE_FILE")
    return Path(env).expanduser() if env else Path.home() / ".garmin-mcp" / "oauth.json"


def prune_expired() -> None:
    now = _now_ms()
    for code, rec in list(STORE.codes.items()):
        if rec["expires_at_ms"] < now:
            del STORE.codes[code]
    dropped = [t for t, rec in STORE.tokens.items() if rec["refresh_expires_at_ms"] < now]
    for t in dropped:
        del STORE.tokens[t]
    if dropped:
        STORE.flush()


def validate_access_token(token: str) -> bool:
    rec = STORE.tokens.get(token)
    return rec is not None and rec["access_expires_at_ms"] >= _now_ms()


# --- Discovery -------------------------------------------------------------


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


# --- Dynamic Client Registration -------------------------------------------


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
    STORE.clients[client_id] = {"client_name": name, "redirect_uris": redirect_uris,
                                "created_at_ms": _now_ms()}
    STORE.flush()
    return JSONResponse({
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_id_issued_at": _now_ms() // 1000,
        "scope": SCOPE,
    }, status_code=201)


# --- Authorize -------------------------------------------------------------


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


def _parse_authorize(params: dict[str, str]) -> tuple[_AuthorizeRequest | None, Response | None]:
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    if not client_id:
        return None, _error_html("Missing client_id", 400)
    client = STORE.clients.get(client_id)
    if not client:
        return None, _error_html("Unknown client_id", 400)
    if not redirect_uri or redirect_uri not in client["redirect_uris"]:
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
    if not passphrase():
        return _error_html("This server has no MCP_PASSPHRASE set, so it cannot "
                           "authorize connectors. Set it and restart.", 503)
    req, err = _parse_authorize(dict(request.query_params))
    if err is not None:
        return err
    assert req is not None
    return HTMLResponse(_consent_html(req))


async def post_authorize(request: Request) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items() if isinstance(v, str)}
    req, err = _parse_authorize(params)
    if err is not None:
        return err
    assert req is not None
    if str(form.get("decision") or "") != "allow":
        return _redirect_error(req.redirect_uri, req.state, "access_denied", "denied")
    given = str(form.get("passphrase") or "")
    if not passphrase() or not hmac.compare_digest(given, passphrase()):
        return HTMLResponse(_consent_html(req, error="Wrong passphrase."), status_code=401)

    code = _new_token()
    STORE.codes[code] = {"client_id": req.client_id, "redirect_uri": req.redirect_uri,
                         "code_challenge": req.code_challenge, "scope": req.scope,
                         "expires_at_ms": _now_ms() + CODE_TTL_MS}
    url = urllib.parse.urlparse(req.redirect_uri)
    q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    q["code"] = code
    if req.state:
        q["state"] = req.state
    return RedirectResponse(url._replace(query=urllib.parse.urlencode(q)).geturl(),
                            status_code=302)


# --- Token -----------------------------------------------------------------


def _mint_pair(client_id: str, scope: str | None) -> dict:
    access, refresh = _new_token(), _new_token()
    now = _now_ms()
    STORE.tokens[access] = {"refresh_token": refresh, "client_id": client_id,
                            "scope": scope, "access_expires_at_ms": now + ACCESS_TTL_MS,
                            "refresh_expires_at_ms": now + REFRESH_TTL_MS}
    STORE.flush()
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MS // 1000, "refresh_token": refresh,
            "scope": scope or SCOPE}


def _find_refresh(refresh_token: str) -> tuple[str, dict] | None:
    for access, rec in STORE.tokens.items():
        if hmac.compare_digest(rec["refresh_token"], refresh_token):
            return access, rec
    return None


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
        rec = STORE.codes.pop(code, None)
        if (not rec or rec["expires_at_ms"] < _now_ms()
                or rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri
                or not _verify_pkce_s256(verifier, rec["code_challenge"])):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_mint_pair(rec["client_id"], rec["scope"]))

    if grant == "refresh_token":
        refresh_token, client_id = g("refresh_token"), g("client_id")
        if not refresh_token or not client_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        found = _find_refresh(refresh_token)
        if not found:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access, old = found
        del STORE.tokens[access]
        if old["client_id"] != client_id or old["refresh_expires_at_ms"] < _now_ms():
            STORE.flush()
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return JSONResponse(_mint_pair(old["client_id"], old["scope"]))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --- HTML ------------------------------------------------------------------


def _esc(s: object) -> str:
    return html_lib.escape(str(s), quote=True)


_CSS = ("body{font-family:system-ui,sans-serif;max-width:480px;margin:3rem auto;"
        "padding:0 1rem;line-height:1.5}button{padding:.6rem 1rem;font:inherit;cursor:pointer;"
        "border:0;border-radius:6px;background:#2f6db5;color:#fff}button.secondary{background:#888}"
        "input{width:100%;padding:.55rem;margin:.3rem 0 .8rem;border:1px solid #ccc;border-radius:6px}"
        ".client{background:#f4f4f4;border:1px solid #ddd;border-radius:6px;padding:.75rem;margin:1rem 0}"
        ".mono{font-family:ui-monospace,monospace;font-size:.85rem;color:#666;word-break:break-all}"
        ".err{color:#b23c3c}.row{display:flex;gap:.5rem;margin-top:1.2rem}")


def _consent_html(req: _AuthorizeRequest, error: str | None = None) -> str:
    def hidden(k: str, v: str | None) -> str:
        return "" if v is None else f'<input type="hidden" name="{k}" value="{_esc(v)}">'
    err = f'<p class="err">{_esc(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize access - {_esc(SERVER_NAME)} MCP</title><style>{_CSS}</style></head><body>
<h1>Authorize access</h1>
<div class="client"><b>{_esc(req.client_name)}</b><br>
<span class="mono">Redirect: {_esc(req.redirect_uri)}</span></div>
<p>This app is requesting <b>read-only</b> access to your Garmin Connect data.</p>
{err}
<form method="post" action="/oauth/authorize">
{hidden("response_type","code")}{hidden("client_id",req.client_id)}
{hidden("redirect_uri",req.redirect_uri)}{hidden("code_challenge",req.code_challenge)}
{hidden("code_challenge_method","S256")}{hidden("state",req.state)}{hidden("scope",req.scope)}
<label for="pp">Server passphrase</label>
<input id="pp" name="passphrase" type="password" autocomplete="current-password" autofocus>
<div class="row"><button name="decision" value="allow">Allow</button>
<button name="decision" value="deny" class="secondary">Deny</button></div></form>
</body></html>"""


def _error_html(msg: str, status: int = 400) -> Response:
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><style>{_CSS}</style>'
                        f"<h1>Error</h1><p>{_esc(msg)}</p>", status_code=status)
