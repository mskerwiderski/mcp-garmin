"""garmin-mcp: login on your own machine, serve from wherever you like.

    garmin-mcp login              interactive Garmin login (handles MFA)
    garmin-mcp status             which account, token validity
    garmin-mcp export             base64 blob for GARMIN_TOKENS
    garmin-mcp logout             forget the tokens
    garmin-mcp serve              stdio transport (Claude Desktop/Code)
    garmin-mcp serve --http       streamable HTTP on /mcp

The login is deliberately a local command: Garmin's SSO refuses fresh logins
from datacenter IPs, and this way no password ever reaches the server.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import tokens as token_store
from .client import GarminAuthError, GarminClient, LoginState


async def _login() -> int:
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    client = GarminClient()
    try:
        state = await client.login(email, password)
        if state == LoginState.NEEDS_MFA:
            code = input("MFA code from your email/app: ").strip()
            state = await client.submit_mfa(code)
        if state != LoginState.OK:
            print(f"login did not complete: {state}", file=sys.stderr)
            return 1
        account = await client.fetch_display_name()
        o1, o2 = client.export_tokens()
        path = token_store.save(token_store.from_json_pair(o1, o2, account))
        print(f"logged in as {account or '(unknown)'}")
        print(f"tokens written to {path} (mode 0600)")
        print("The OAuth1 token is valid for about a year; the server refreshes "
              "OAuth2 from it without touching Garmin's login.")
        return 0
    except GarminAuthError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        print("If this says 429/403 or mentions Cloudflare: you are probably on a "
              "VPN or datacenter IP. Retry from a normal home connection.",
              file=sys.stderr)
        return 1
    finally:
        await client.aclose()


async def _status() -> int:
    from .session import GarminSession, probe
    tok = token_store.load()
    if tok is None:
        print(f"not connected (no tokens at {token_store.token_path()})")
        return 1
    session = GarminSession()
    try:
        info = await probe(session)
    except Exception as exc:                             # noqa: BLE001
        print(f"tokens present but unusable: {exc}", file=sys.stderr)
        return 1
    finally:
        await session.aclose()
    import datetime as _dt
    exp = info.get("oauth2_expires_at")
    when = (_dt.datetime.fromtimestamp(exp).isoformat(timespec="seconds")
            if exp else "unknown")
    print(f"connected as {info.get('account') or '(unknown)'}")
    print(f"access token valid until {when}")
    return 0


def _export() -> int:
    tok = token_store.load()
    if tok is None:
        print("not connected", file=sys.stderr)
        return 1
    print(token_store.export_blob(tok))
    return 0


def _logout() -> int:
    path = token_store.token_path()
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    else:
        print("nothing to remove")
    return 0


def _serve(args: argparse.Namespace) -> int:
    if args.http:
        import uvicorn

        from .server import build_http_app
        uvicorn.run(build_http_app(), host=args.host, port=args.port,
                    log_level="info")
        return 0
    from .server import build_server
    server, _session = build_server()
    server.run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp", description="Read-only MCP server for Garmin Connect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="log in to Garmin Connect and store the tokens")
    sub.add_parser("status", help="show the connected account")
    sub.add_parser("export", help="print the tokens as a GARMIN_TOKENS blob")
    sub.add_parser("logout", help="delete the stored tokens")
    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--http", action="store_true",
                       help="streamable HTTP on /mcp instead of stdio")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.cmd == "login":
        return asyncio.run(_login())
    if args.cmd == "status":
        return asyncio.run(_status())
    if args.cmd == "export":
        return _export()
    if args.cmd == "logout":
        return _logout()
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
