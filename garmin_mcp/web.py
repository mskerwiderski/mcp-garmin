"""The small web surface: sign up with an invitation, log in, manage the
account. Plain HTML strings, no template engine - this is five pages.
"""

from __future__ import annotations

import html as html_lib
import os
import urllib.parse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import users

COOKIE = "garmin_mcp_session"

CSS = """
:root{color-scheme:light dark}
body{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;
line-height:1.55}
h1{font-size:1.4rem;margin-bottom:.2rem}
p.sub{color:#777;margin-top:0}
button,.btn{padding:.6rem 1rem;font:inherit;cursor:pointer;border:0;border-radius:6px;
background:#2f6db5;color:#fff;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#888}
button.danger{background:#b23c3c}
input{width:100%;padding:.55rem;margin:.3rem 0 .8rem;border:1px solid #8884;
border-radius:6px;background:transparent;color:inherit;font:inherit}
label{font-weight:600;font-size:.9rem}
.card{border:1px solid #8884;border-radius:8px;padding:.9rem;margin:1rem 0}
.mono{font-family:ui-monospace,monospace;font-size:.85rem;color:#888;word-break:break-all}
.err{color:#b23c3c}.ok{color:#2e7d32}
.row{display:flex;gap:.5rem;margin-top:1.2rem;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;margin:.5rem 0}
td{border-top:1px solid #8884;padding:.5rem .3rem;vertical-align:top;font-size:.92rem}
h2{font-size:1.05rem;margin-top:1.6rem}
form{margin:0}
"""


def esc(s: object) -> str:
    return html_lib.escape(str(s), quote=True)


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>",
        status_code=status)


def base_url(request: Request) -> str:
    configured = os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "https")
    return f"{proto}://{request.headers.get('host', 'localhost')}"


# --- session cookie --------------------------------------------------------


def current_user(request: Request) -> int | None:
    return users.session_user(request.cookies.get(COOKIE))


def _set_cookie(response: Response, token: str) -> Response:
    response.set_cookie(COOKIE, token, httponly=True, secure=True,
                        samesite="lax", max_age=users.SESSION_TTL_MS // 1000,
                        path="/")
    return response


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={urllib.parse.quote(nxt, safe='')}",
                            status_code=302)


def _safe_next(raw: str | None) -> str:
    """Only same-site paths - an open redirect here would hand an attacker the
    OAuth authorize flow."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/account"


# --- pages -----------------------------------------------------------------


async def get_signup(request: Request) -> Response:
    code = request.query_params.get("code", "")
    if not users.invite_valid(code):
        return page("Not found",
                    "<h1>Not found</h1><p>This server has no open sign-up. "
                    "Accounts are created from an invitation link.</p>", status=404)
    return page("Create your account", _signup_html(code))


def _signup_html(code: str, error: str = "", email: str = "") -> str:
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    return f"""<h1>Create your account</h1>
<p class="sub">Invited access to a private Garmin Connect MCP server.</p>
{err}
<form method="post" action="/signup">
<input type="hidden" name="code" value="{esc(code)}">
<label for="email">E-mail</label>
<input id="email" name="email" type="email" value="{esc(email)}" required autofocus>
<label for="pw">Password (at least 10 characters)</label>
<input id="pw" name="password" type="password" minlength="10" required>
<div class="card"><b>What this server stores</b><br>
Your e-mail, a password hash, and - once you connect Garmin - the OAuth tokens
for your Garmin account, encrypted. It reads your Garmin data on request and
never writes to Garmin. Your Garmin password is never stored. You can delete
your account and everything in it at any time from your account page.</div>
<div class="row"><button>Create account</button></div></form>"""


async def post_signup(request: Request) -> Response:
    form = await request.form()
    code = str(form.get("code") or "")
    email = str(form.get("email") or "")
    try:
        user_id = users.create_user(email, str(form.get("password") or ""), code)
    except users.UserError as exc:
        return page("Create your account", _signup_html(code, str(exc), email),
                    status=400)
    return _set_cookie(RedirectResponse("/account", status_code=303),
                       users.start_session(user_id))


async def get_login(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse(_safe_next(request.query_params.get("next")),
                                status_code=302)
    return page("Log in", _login_html(request.query_params.get("next", "")))


def _login_html(nxt: str, error: str = "", email: str = "") -> str:
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    return f"""<h1>Log in</h1>
{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{esc(nxt)}">
<label for="email">E-mail</label>
<input id="email" name="email" type="email" value="{esc(email)}" required autofocus>
<label for="pw">Password</label>
<input id="pw" name="password" type="password" required>
<div class="row"><button>Log in</button></div></form>"""


async def post_login(request: Request) -> Response:
    form = await request.form()
    nxt = _safe_next(str(form.get("next") or ""))
    email = str(form.get("email") or "")
    try:
        user_id = users.verify_login(email, str(form.get("password") or ""))
    except users.UserError as exc:
        return page("Log in", _login_html(nxt, str(exc), email), status=401)
    return _set_cookie(RedirectResponse(nxt, status_code=303),
                       users.start_session(user_id))


async def logout(request: Request) -> Response:
    users.end_session(request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


async def get_account(request: Request) -> Response:
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    user = users.get_user(user_id)
    tokens = users.get_garmin_tokens(user_id)
    url = f"{base_url(request)}/mcp"
    if tokens:
        garmin = (f'<p class="ok">Connected as <b>{esc(tokens[2] or "Garmin")}</b></p>'
                  '<form method="post" action="/account/garmin/disconnect">'
                  '<button class="secondary">Disconnect Garmin</button></form>')
    else:
        garmin = ('<p>Not connected yet - the tools cannot answer anything until '
                  'you do.</p><a class="btn" href="/account/garmin">Connect Garmin</a>')
    admin_card = ('<div class="card"><b>Administration</b><br>'
                  '<a class="btn" href="/admin">Invitations and accounts</a></div>'
                  if users.is_admin(user_id) else "")
    return page("Your account", f"""<h1>Your account</h1>
<p class="sub">{esc(user["email"])}</p>
<div class="card"><b>Garmin Connect</b><br>{garmin}</div>
<div class="card"><b>Use it in Claude or ChatGPT</b><br>
Add a custom connector with this URL:<br>
<span class="mono">{esc(url)}</span><br>
You will be asked to log in here once and to confirm access.</div>
{admin_card}
<div class="row">
<a class="btn secondary" href="/logout">Log out</a>
<form method="post" action="/account/delete"
onsubmit="return confirm('Delete your account, its Garmin tokens and all cached files?')">
<button class="danger">Delete account</button></form></div>""")


async def post_delete_account(request: Request) -> Response:
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    from .session import forget_user
    forget_user(user_id)
    users.delete_user(user_id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


# --- connecting Garmin -----------------------------------------------------


def _garmin_html(error: str = "", mfa: bool = False, email: str = "") -> str:
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    if mfa:
        return f"""<h1>Enter your Garmin code</h1>
<p class="sub">Garmin sent a multi-factor code to your e-mail or app.</p>
{err}
<form method="post" action="/account/garmin/mfa">
<label for="code">Code</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
 required autofocus>
<div class="row"><button>Confirm</button>
<a class="btn secondary" href="/account/garmin">Start over</a></div></form>"""
    return f"""<h1>Connect Garmin Connect</h1>
{err}
<form method="post" action="/account/garmin/login">
<label for="gmail">Garmin e-mail</label>
<input id="gmail" name="email" type="email" value="{esc(email)}" required autofocus>
<label for="gpw">Garmin password</label>
<input id="gpw" name="password" type="password" required>
<div class="row"><button>Connect</button>
<a class="btn secondary" href="/account">Cancel</a></div></form>
<div class="card"><b>Your password is not stored.</b> It is used once to obtain
Garmin's OAuth tokens; only those are kept, encrypted. This server never writes
to your Garmin account.</div>
<details><summary>Garmin refuses the login from here?</summary>
<p>Garmin sometimes blocks logins coming from servers. Install the command line
tool on your own machine, run <span class="mono">garmin-mcp login</span> and then
<span class="mono">garmin-mcp export</span>, and paste the single line it prints
below.</p>
<form method="post" action="/account/garmin/blob">
<label for="blob">Token blob</label>
<input id="blob" name="blob" required>
<div class="row"><button class="secondary">Import tokens</button></div></form>
</details>"""


async def get_garmin(request: Request) -> Response:
    from . import connect
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    return page("Connect Garmin", _garmin_html(mfa=connect.mfa_pending(user_id)))


async def post_garmin_login(request: Request) -> Response:
    from . import connect
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    form = await request.form()
    email = str(form.get("email") or "")
    try:
        done = await connect.start_login(user_id, email,
                                         str(form.get("password") or ""))
    except connect.ConnectError as exc:
        return page("Connect Garmin", _garmin_html(str(exc), email=email), status=400)
    if done:
        return RedirectResponse("/account", status_code=303)
    return page("Enter your Garmin code", _garmin_html(mfa=True))


async def post_garmin_mfa(request: Request) -> Response:
    from . import connect
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    form = await request.form()
    try:
        await connect.submit_mfa(user_id, str(form.get("code") or ""))
    except connect.ConnectError as exc:
        return page("Enter your Garmin code",
                    _garmin_html(str(exc), mfa=connect.mfa_pending(user_id)),
                    status=400)
    return RedirectResponse("/account", status_code=303)


async def post_garmin_blob(request: Request) -> Response:
    from . import connect
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    form = await request.form()
    try:
        connect.import_blob(user_id, str(form.get("blob") or ""))
    except connect.ConnectError as exc:
        return page("Connect Garmin", _garmin_html(str(exc)), status=400)
    return RedirectResponse("/account", status_code=303)


async def post_garmin_disconnect(request: Request) -> Response:
    from . import connect
    user_id = current_user(request)
    if user_id is None:
        return login_redirect(request)
    connect.disconnect(user_id)
    return RedirectResponse("/account", status_code=303)


# --- administration --------------------------------------------------------
#
# Reachable only for accounts with the admin flag, and a non-admin gets a 404
# rather than a 403: there is no reason to confirm that this page exists.


def _require_admin(request: Request):
    user_id = current_user(request)
    if user_id is None:
        return None, login_redirect(request)
    if not users.is_admin(user_id):
        return None, page("Not found", "<h1>Not found</h1>", status=404)
    return user_id, None


def _ms(value) -> str:
    import datetime as dt
    return (dt.datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
            if value else "-")


def _admin_html(admin_id: int, new_link: str = "", error: str = "") -> str:
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    link = ""
    if new_link:
        link = (f'<div class="card"><b class="ok">Invitation created</b><br>'
                f'<span class="mono">{esc(new_link)}</span><br>'
                f'Valid for 7 days, single use. Send it to the person directly.</div>')

    rows = []
    for u in users.list_users():
        badge = " (admin)" if u["is_admin"] else ""
        state = ("active" if u["status"] == "active" else
                 '<span class="err">disabled</span>')
        garmin = esc(u["garmin_account"]) if u["garmin_account"] else "- not connected -"
        if u["id"] == admin_id:
            actions = '<span class="mono">that is you</span>'
        else:
            toggle = "enable" if u["status"] != "active" else "disable"
            actions = (
                f'<form method="post" action="/admin/user/{u["id"]}/{toggle}"'
                f' style="display:inline"><button class="secondary">{toggle}</button></form> '
                f'<form method="post" action="/admin/user/{u["id"]}/delete"'
                f' style="display:inline" onsubmit="return confirm('
                f"'Delete {esc(u['email'])}, their Garmin tokens and cached files?'"
                f')"><button class="danger">delete</button></form>')
        rows.append(
            f'<tr><td>{esc(u["email"])}{badge}<br><span class="mono">{garmin}</span></td>'
            f'<td>{state}<br><span class="mono">seen {_ms(u["last_seen_ms"])}</span></td>'
            f'<td>{actions}</td></tr>')
    users_table = ("<p>No accounts yet.</p>" if not rows else
                   '<table><tbody>' + "".join(rows) + "</tbody></table>")

    inv_rows = []
    for i in users.list_invites():
        cls = {"open": "ok", "used": "", "expired": "err"}[i["state"]]
        inv_rows.append(
            f'<tr><td>{esc(i["label"] or "-")}</td>'
            f'<td class="{cls}">{i["state"]}</td>'
            f'<td class="mono">expires {_ms(i["expires_at_ms"])}</td></tr>')
    invites_table = ("<p>No invitations yet.</p>" if not inv_rows else
                     '<table><tbody>' + "".join(inv_rows) + "</tbody></table>")

    return f"""<h1>Administration</h1>
<p class="sub">Accounts and invitations for this server.</p>
{err}{link}
<div class="card"><b>Invite someone</b>
<form method="post" action="/admin/invite">
<label for="label">Who is it for? (a note for you, shown nowhere else)</label>
<input id="label" name="label" placeholder="Anja">
<div class="row"><button>Create invitation link</button></div></form></div>
<h2>Accounts</h2>
{users_table}
<h2>Invitations</h2>
{invites_table}
<div class="row"><a class="btn secondary" href="/account">Back to your account</a></div>"""


async def get_admin(request: Request) -> Response:
    admin_id, err = _require_admin(request)
    if err is not None:
        return err
    return page("Administration", _admin_html(admin_id))


async def post_admin_invite(request: Request) -> Response:
    admin_id, err = _require_admin(request)
    if err is not None:
        return err
    form = await request.form()
    code = users.create_invite(str(form.get("label") or "")[:80])
    return page("Administration",
                _admin_html(admin_id, f"{base_url(request)}/signup?code={code}"))


async def post_admin_user(request: Request) -> Response:
    admin_id, err = _require_admin(request)
    if err is not None:
        return err
    target = int(request.path_params["user_id"])
    action = request.path_params["action"]
    if target == admin_id:
        return page("Administration",
                    _admin_html(admin_id, error="You cannot change your own account "
                                                "here - use the account page."),
                    status=400)
    if users.get_user(target) is None:
        return page("Administration", _admin_html(admin_id, error="No such account."),
                    status=404)
    if action == "delete":
        from .session import forget_user
        forget_user(target)
        users.delete_user(target)
    else:
        users.set_status(target, "active" if action == "enable" else "disabled")
    return RedirectResponse("/admin", status_code=303)
