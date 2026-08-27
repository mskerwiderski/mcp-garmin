"""garmin-mcp: login on your own machine, serve from wherever you like.

    garmin-mcp login              interactive Garmin login (handles MFA)
    garmin-mcp status             which account, token validity
    garmin-mcp export             base64 blob for GARMIN_TOKENS
    garmin-mcp logout             forget the tokens
    garmin-mcp serve              stdio transport (Claude Desktop/Code)
    garmin-mcp serve --http       streamable HTTP on /mcp

The login is deliberately a local command: Garmin's SSO refuses fresh logins
from datacenter IPs, and this way no password ever reaches the server.

On a server, the same binary administers the accounts. There is no admin web
interface on purpose - an invitation is one command, and nothing that can be
reached from the internet can hand out accounts:

    garmin-mcp invite create --label "Anja"
    garmin-mcp invite list | delete <code>
    garmin-mcp user list | disable | enable | promote | demote | delete

The same things are on the web at /admin for accounts with the admin flag; the
first account created on a server has it. The command line stays the way back
in when nobody can log in.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from . import tokens as token_store
from .client import GarminClient, GarminError, LoginState


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
    except GarminError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        print("If this says 429/403 or mentions Cloudflare: you are probably on a "
              "VPN or datacenter IP. Retry from a normal home connection.",
              file=sys.stderr)
        return 1
    finally:
        await client.aclose()


async def _status() -> int:
    from .session import local_session, probe
    tok = token_store.load()
    if tok is None:
        print(f"not connected (no tokens at {token_store.token_path()})")
        return 1
    session = local_session()
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


# --- server side administration --------------------------------------------


def _admin_setup() -> None:
    from . import db
    db.init()


def _fmt_ms(ms) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def _invite(args: argparse.Namespace) -> int:
    from . import users
    _admin_setup()
    if args.invite_cmd == "create":
        code = users.create_invite(args.label or "")
        base = os.environ.get("PUBLIC_URL", "https://<your-server>").rstrip("/")
        print(f"{base}/signup?code={code}")
        print(f"valid for {users.INVITE_TTL_MS // (24 * 3600 * 1000)} days, single use")
        return 0
    if args.invite_cmd == "delete":
        if users.delete_invite(args.code):
            print("invitation deleted; its link no longer works")
            return 0
        print(f"no invitation with code {args.code}", file=sys.stderr)
        return 1
    rows = users.list_invites()
    if not rows:
        print("no invitations yet")
        return 0
    for r in rows:
        print(f"{r['state']:8} {(r['label'] or '-'):20} "
              f"expires {_fmt_ms(r['expires_at_ms'])}  {r['code']}")
    return 0


def _user(args: argparse.Namespace) -> int:
    from . import users
    from .session import cache_root, forget_user
    _admin_setup()
    if args.user_cmd == "list":
        rows = users.list_users()
        if not rows:
            print("no accounts yet")
            return 0
        print(f"{'id':>3}  {'e-mail':28} {'status':9} {'garmin':22} last seen")
        for r in rows:
            status = r["status"] + ("*" if r["is_admin"] else "")
            print(f"{r['id']:>3}  {r['email']:28} {status:9} "
                  f"{(r['garmin_account'] or '- not connected -'):22} "
                  f"{_fmt_ms(r['last_seen_ms'])}")
        print("* = administrator")
        return 0

    user_id = users.user_id_by_email(args.email)
    if user_id is None:
        print(f"no account for {args.email}", file=sys.stderr)
        return 1
    if args.user_cmd in ("promote", "demote"):
        try:
            users.set_admin(user_id, args.user_cmd == "promote")
        except users.UserError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{args.email} is now "
              f"{'an administrator' if args.user_cmd == 'promote' else 'a normal user'}")
        return 0
    if args.user_cmd in ("disable", "enable"):
        users.set_status(user_id, "disabled" if args.user_cmd == "disable" else "active")
        print(f"{args.email} is now {'disabled' if args.user_cmd == 'disable' else 'active'}")
        return 0
    if not args.yes:
        print(f"This deletes the account {args.email}, its Garmin tokens and its "
              f"cached files. Repeat with --yes to confirm.", file=sys.stderr)
        return 1
    forget_user(user_id)
    users.delete_user(user_id)
    print(f"deleted {args.email} (cache under {cache_root() / str(user_id)} removed)")
    return 0


def _serve(args: argparse.Namespace) -> int:
    if args.http:
        import uvicorn

        from .server import build_http_app
        uvicorn.run(build_http_app(), host=args.host, port=args.port,
                    log_level="info")
        return 0
    from .server import build_server
    build_server().run(transport="stdio")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp", description="Read-only MCP server for Garmin Connect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="log in to Garmin Connect and store the tokens")
    sub.add_parser("status", help="show the connected account")
    sub.add_parser("export", help="print the tokens as a GARMIN_TOKENS blob")
    sub.add_parser("logout", help="delete the stored tokens")
    invite = sub.add_parser("invite", help="invitations (server side)")
    invite_sub = invite.add_subparsers(dest="invite_cmd", required=True)
    invite_create = invite_sub.add_parser("create", help="print a one-time signup link")
    invite_create.add_argument("--label", default="", help="who it is for")
    invite_sub.add_parser("list", help="open, used and expired invitations")
    invite_delete = invite_sub.add_parser(
        "delete", help="revoke an invitation (or drop a used record)")
    invite_delete.add_argument("code")

    user = sub.add_parser("user", help="accounts (server side)")
    user_sub = user.add_subparsers(dest="user_cmd", required=True)
    user_sub.add_parser("list", help="all accounts")
    for name, helptext in (("disable", "block an account immediately"),
                           ("enable", "unblock an account"),
                           ("promote", "give access to the admin page"),
                           ("demote", "take away the admin page"),
                           ("delete", "remove an account and all its data")):
        sp = user_sub.add_parser(name, help=helptext)
        sp.add_argument("email")
        if name == "delete":
            sp.add_argument("--yes", action="store_true", help="skip the confirmation")

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
    if args.cmd == "invite":
        return _invite(args)
    if args.cmd == "user":
        return _user(args)
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
