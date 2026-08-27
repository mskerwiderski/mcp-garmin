# How it works

For contributors, and for anyone who wants to know what the code is doing with
their tokens.

## Module map

```
garmin_mcp/
  client.py     Garmin Connect HTTP client: SSO login, OAuth1 -> OAuth2, ~60 endpoints
  fit/          FIT reading: parser.py (metrics), streams.py (time series),
                devfields.py (Connect-IQ fields)
  fitview.py    FIT results shrunk to something a model can hold
  project.py    Garmin JSON shrunk the same way
  tools.py      The 15 MCP tools; docstrings are what the model reads
  session.py    One Garmin client per identity, plus the registry
  tokens.py     Token file for the local (stdio) identity
  users.py      Accounts, invitations, web sessions, encrypted Garmin tokens
  db.py         SQLite schema and migrations
  crypto.py     Fernet encryption at rest
  oauth.py      OAuth 2.1 authorization server (DCR, PKCE, refresh)
  connect.py    Connecting a Garmin account: web login, MFA, token blob
  web.py        Signup, login, account, admin pages
  server.py     stdio and HTTP entry points
  cli.py        garmin-mcp command
```

## Two identities, one mechanism

**stdio** is single-user by definition: the process belongs to whoever started
it and reads `~/.garmin-mcp/tokens.json`. No accounts, no passwords, no OAuth.

**HTTP** is multi-tenant. The whole separation lives in one place:

```python
class _McpEndpoint:
    async def __call__(self, scope, receive, send):
        user_id = oauth.access_token_user(_scope_bearer(scope) or "")
        if user_id is None:
            await _send_unauthorized(scope, send)
            return
        sessions.CURRENT_USER.set(user_id)
        await self._server.session_manager.handle_request(scope, receive, send)
```

The bearer token is resolved to an account, the account goes into a
`ContextVar`, and only then does the request reach the MCP session manager.
Tools resolve their session per call through `current_session()`; binding a
session at registration time would quietly make the server single-tenant again,
which is why `tools.register` takes a function rather than an instance.

That the ContextVar survives into the tool coroutine was verified before the
design was built on it, and is covered by
`test_two_accounts_never_see_each_other`.

The FIT cache is namespaced per identity (`cache/local/`, `cache/<user_id>/`).
Those files are somebody's training data; a flat cache would leak between
accounts.

## Why the Garmin login is not on the server by default

Garmin's SSO sits behind Cloudflare, which since March 2026 answers fresh logins
from datacenter addresses with 429 or 403. The design consequence: the login
runs on the user's own machine, and only tokens travel. OAuth1 is valid for
about a year and mints OAuth2 access tokens against `connectapi.garmin.com`, so
nothing after the login touches `sso.garmin.com`.

A hosted server still offers a login form, because it is far friendlier and it
does work from many addresses - but the token blob from `garmin-mcp export` is
always there as the way in when it does not. SSO logins are serialised with a
process-wide lock: several people logging in at the same second is exactly the
pattern that gets an address rate-limited.

## Responses are projected, always

Garmin answers with hundreds of fields per activity, and `extract_streams`
produces up to 1500 points per channel. Everything goes through `project.py` or
`fitview.py`: a whitelist per shape, rounded numbers, empty values dropped, and
streams resampled to `max_points` with min/max/avg per channel.

This is not cosmetic. Handing raw payloads to a model burns the context window
the conversation needs, and it is the most common weakness of comparable
servers. A new tool without a projection is a bug.

## Errors have to survive the SDK

If a tool raises anything unexpected, the MCP SDK reports `Error executing tool
<name>` and swallows the cause. The most common failure here - no Garmin tokens
yet - would be unreadable. So every tool is wrapped in `_guard`, which
translates `NotConnected`, `GarminError` and `ValueError` into `ToolError`;
only those reach the model as text.

## Storage

SQLite through the standard library, one connection per operation, no ORM.
Starlette runs sync endpoints in a thread pool, so a shared connection would be
used across threads; opening per call sidesteps that and costs nothing at this
scale.

One trap, learned the hard way: `db.conn()` commits **after** the `yield`, so an
exception passing through skips the commit. The failed-login counter was
ineffective for exactly that reason - the lockout could never trigger. Write
first, leave the block, then raise.

Garmin tokens are encrypted with Fernet, key derived from `APP_SECRET`. That
protects a stolen backup, not a compromised server, and the documentation says
so rather than implying more.

## OAuth

`oauth.py` is a small, complete OAuth 2.1 authorization server: dynamic client
registration (RFC 7591), authorization code with PKCE S256, refresh grant,
authorization server metadata (RFC 8414) and protected resource metadata
(RFC 9728).

It exists because neither claude.ai nor ChatGPT offers a field for a static
bearer header. A static token was supported while the server was single-user and
was removed with multi-tenancy: it cannot be attributed to an account, so it
would be a master key.

Identity at the consent screen is the session cookie. Access tokens are checked
with a join on `users.status='active'`, which is why disabling an account cuts
live clients off immediately.

## Garmin quirks worth knowing

Undocumented API, so these were found by observation:

- **Challenges live on two endpoints.** `adHocChallenge/historical` returns only
  *finished* challenges; the one running this month is exclusively under
  `adHocChallenge/active`. Querying one of them makes the current month look
  like it does not exist.
- **A running challenge reports `playerCount: 0`** and an empty player list. The
  real table only comes from the per-uuid detail call.
- **Wellness endpoints want the profile display id**, the GUID-like
  `socialProfile.displayName`, not the human name.
- **Sleep is filed under the morning you woke up.**
- **Training readiness has an `inputContext`.** `AFTER_WAKEUP_RESET` is the
  morning value from Garmin's own Morning Report; anything else means the watch
  has not synced since waking.
- **Gear v2 uses different field names** than the older endpoint: `name`,
  `gearType`, `distanceUsedMeters`.
- **Garmin's numbers and the FIT file disagree**, especially on elevation
  (server-side correction) and pauses. Both views are exposed on purpose:
  `get_activity` versus `analyze_activity_fit`.

## Vendored code

`client.py` and `fit/*` are copies from
[MyFITContainer](https://github.com/mskerwiderski/MyFITContainer); the file
headers name the source commit. Changes made here are marked
`mcp-garmin addition`: `search_activities`, `get_activity_detail`,
`list_adhoc_challenges`, `get_adhoc_challenge`, and two renamed modules.

A fix in either project has to be carried over by hand. A shared package will be
worth it once that happens more than twice.

## Tests

103 tests, no network. The Garmin API is stubbed at the client boundary, FIT
files are synthesised with `fit-tool`, and the HTTP surface is driven through
Starlette's `TestClient`, including a full OAuth handshake the way claude.ai
walks it.

The ones that matter most:

- `test_two_accounts_never_see_each_other` - the tenancy boundary
- `test_disabling_an_account_kills_its_tokens` - revocation is immediate
- `test_both_endpoints_are_asked` - the challenge bug cannot come back
- `test_login_locks_out_after_five_attempts` - the lockout actually persists
- `test_login_next_only_accepts_local_paths` - no open redirect into OAuth

## MCP SDK

Built on `mcp` 2.x (`mcp.server.mcpserver.MCPServer`). The upper bound in
`pyproject.toml` is deliberate: 2.0 already removed `mcp.server.fastmcp` once,
and a server that rebuilds its image on every deploy would ride into the next
such change unannounced.
