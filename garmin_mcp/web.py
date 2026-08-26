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
    return page("Your account", f"""<h1>Your account</h1>
<p class="sub">{esc(user["email"])}</p>
<div class="card"><b>Garmin Connect</b><br>{garmin}</div>
<div class="card"><b>Use it in Claude or ChatGPT</b><br>
Add a custom connector with this URL:<br>
<span class="mono">{esc(url)}</span><br>
You will be asked to log in here once and to confirm access.</div>
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
