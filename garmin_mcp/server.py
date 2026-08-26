"""Server entry points: stdio for local clients, streamable HTTP for the web.

Local clients (Claude Desktop, Claude Code, ChatGPT desktop) speak stdio and
need no authentication - the process belongs to the user who started it.

The HTTP endpoint is /mcp and always requires a bearer token, either an OAuth
access token from oauth.py or the static MCP_TOKEN. claude.ai and ChatGPT have
no field for a static header, so they go through OAuth; Claude Code and Desktop
can send MCP_TOKEN directly.
"""

from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import oauth, tools
from .session import GarminSession

INSTRUCTIONS = """Read-only access to a single Garmin Connect account.

Activities: list_activities, get_activity (Garmin's own numbers),
analyze_activity_fit / get_activity_streams / get_swim_detail /
get_activity_sensors (the original file the watch wrote).
Health: get_daily_health, get_training_status, get_body_composition,
get_blood_pressure. Context: get_profile, list_gear, whoami.

Nothing in this server writes to Garmin Connect."""


def build_server(session: GarminSession | None = None) -> tuple[MCPServer, GarminSession]:
    session = session or GarminSession()
    server = MCPServer(name="garmin-connect", instructions=INSTRUCTIONS)
    tools.register(server, session)
    return server, session


# --- HTTP ------------------------------------------------------------------


def _scope_bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            val = value.decode("latin-1")
            if val.startswith("Bearer "):
                return val[7:].strip()
    return None


def _token_valid(token: str | None) -> bool:
    if not token:
        return False
    static = os.environ.get("MCP_TOKEN", "")
    if static and hmac.compare_digest(token, static):
        return True
    return oauth.validate_access_token(token)


def _scope_base_url(scope: Scope) -> str:
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
        if not _token_valid(_scope_bearer(scope)):
            await _send_unauthorized(scope, send)
            return
        await self._server.session_manager.handle_request(scope, receive, send)


def build_http_app() -> Starlette:
    server, session = build_server()
    # Initialises the session manager lazily; the returned app is not mounted,
    # the endpoint below drives the same manager with our own auth in front.
    server.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    oauth.STORE.bind(oauth.state_path())

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with server.session_manager.run():
            yield
        await session.aclose()

    return Starlette(
        middleware=[Middleware(
            CORSMiddleware, allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
            expose_headers=["WWW-Authenticate"], allow_credentials=False)],
        routes=[
            Route("/health", health),
            Route("/healthz", health),
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
