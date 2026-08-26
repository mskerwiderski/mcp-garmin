"""Server entry points: stdio for local clients, streamable HTTP for the web.

stdio is single-user by definition - the process belongs to whoever started it,
and it reads the local token file. No accounts, no passwords, no OAuth.

The HTTP server is multi-tenant. It resolves the bearer token to an account,
puts that account into a ContextVar and only then hands the request to the MCP
session manager; the tools read the ContextVar. That is the whole tenancy
mechanism, and it is why no request can reach another account's data.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import crypto, db, oauth, session as sessions, tools, web

INSTRUCTIONS = """Read-only access to one Garmin Connect account.

Activities: list_activities, get_activity (Garmin's own numbers),
analyze_activity_fit / get_activity_streams / get_swim_detail /
get_activity_sensors (the original file the watch wrote).
Health: get_daily_health, get_training_status, get_body_composition,
get_blood_pressure. Challenges: list_challenges, get_challenge.
Context: get_profile, list_gear, whoami.

Nothing in this server writes to Garmin Connect."""


def build_server(get_session=None) -> MCPServer:
    server = MCPServer(name="garmin-connect", instructions=INSTRUCTIONS)
    tools.register(server, get_session or sessions.current_session)
    return server


# --- HTTP ------------------------------------------------------------------


def _scope_bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            val = value.decode("latin-1")
            if val.startswith("Bearer "):
                return val[7:].strip()
    return None


def _scope_base_url(scope: Scope) -> str:
    import os
    configured = os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    host, proto = "localhost", "https"
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
        elif name == b"x-forwarded-proto":
            proto = value.decode("latin-1")
    return f"{proto}://{host}"


async def _send_unauthorized(scope: Scope, send: Send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    meta = f"{_scope_base_url(scope)}/.well-known/oauth-protected-resource"
    www_auth = f'Bearer realm="mcp", resource_metadata="{meta}"'
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"www-authenticate", www_auth.encode("latin-1")),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


class _McpEndpoint:
    def __init__(self, server: MCPServer) -> None:
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        user_id = oauth.access_token_user(_scope_bearer(scope) or "")
        if user_id is None:
            await _send_unauthorized(scope, send)
            return
        sessions.CURRENT_USER.set(user_id)
        await self._server.session_manager.handle_request(scope, receive, send)


def build_http_app() -> Starlette:
    crypto._fernet()          # fail fast: no APP_SECRET, no server
    db.init()
    server = build_server()
    # Initialises the session manager lazily; the returned app is not mounted,
    # the endpoint below drives the same manager with our own auth in front.
    server.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def root(_request: Request) -> RedirectResponse:
        return RedirectResponse("/account", status_code=302)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        oauth.prune_expired()
        async with server.session_manager.run():
            yield
        await sessions.close_all()

    return Starlette(
        middleware=[Middleware(
            CORSMiddleware, allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
            expose_headers=["WWW-Authenticate"], allow_credentials=False)],
        routes=[
            Route("/", root),
            Route("/health", health),
            Route("/healthz", health),
            # web
            Route("/signup", web.get_signup),
            Route("/signup", web.post_signup, methods=["POST"]),
            Route("/login", web.get_login),
            Route("/login", web.post_login, methods=["POST"]),
            Route("/logout", web.logout, methods=["GET", "POST"]),
            Route("/account", web.get_account),
            Route("/account/delete", web.post_delete_account, methods=["POST"]),
            Route("/account/garmin", web.get_garmin),
            Route("/account/garmin/login", web.post_garmin_login, methods=["POST"]),
            Route("/account/garmin/mfa", web.post_garmin_mfa, methods=["POST"]),
            Route("/account/garmin/blob", web.post_garmin_blob, methods=["POST"]),
            Route("/account/garmin/disconnect", web.post_garmin_disconnect,
                  methods=["POST"]),
            Route("/admin", web.get_admin),
            Route("/admin/invite", web.post_admin_invite, methods=["POST"]),
            Route("/admin/user/{user_id:int}/{action}", web.post_admin_user,
                  methods=["POST"]),
            # OAuth discovery (RFC 8414 + RFC 9728)
            Route("/.well-known/oauth-protected-resource",
                  oauth.get_protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp",
                  oauth.get_protected_resource_metadata),
            Route("/.well-known/oauth-authorization-server",
                  oauth.get_authorization_server_metadata),
            Route("/oauth/register", oauth.post_register, methods=["POST"]),
            Route("/oauth/authorize", oauth.get_authorize),
            Route("/oauth/authorize", oauth.post_authorize, methods=["POST"]),
            Route("/oauth/token", oauth.post_token, methods=["POST"]),
            Route("/mcp", _McpEndpoint(server), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


def __getattr__(name: str):
    """PEP 562: `uvicorn garmin_mcp.server:app` builds the app on attribute
    access, so merely importing this module has no side effects."""
    if name == "app":
        return build_http_app()
    raise AttributeError(name)
