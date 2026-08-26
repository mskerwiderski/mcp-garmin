# garmin-mcp

Read-only MCP server for **Garmin Connect**. Ask Claude or ChatGPT about your
training: activities, heart rate, sleep, HRV, Body Battery, training status,
challenge leaderboards - and, unlike other Garmin MCP servers, the **original
FIT file** the watch wrote, including Connect-IQ sensor channels (Stryd, SmO2,
CORE).

Nothing here writes to Garmin Connect.

## Two ways to run it

**Local (stdio).** One person, no accounts, no server. The tokens live in a file
on your machine and the MCP client starts the process on demand. This is the
right choice for Claude Desktop and Claude Code.

**Hosted (HTTP).** Several people, each with their own Garmin account, on one
server. Needed for claude.ai and ChatGPT, which cannot start a local process.
Registration is **invite-only**: you hand out signup links from the command
line, and there is no open sign-up page.

## Why the Garmin login is not a server thing by default

Garmin's SSO sits behind Cloudflare, which since March 2026 answers fresh logins
from datacenter IPs with 429/403. Locally that never applies:

```bash
garmin-mcp login
```

It handles MFA and stores the OAuth1/OAuth2 tokens in `~/.garmin-mcp/tokens.json`
(mode 0600). OAuth1 is valid for about a year and mints OAuth2 access tokens
against `connectapi.garmin.com`, so nothing after the login touches
`sso.garmin.com`.

On a hosted server both ways exist: a login form (the password is used once and
never stored) and, when Garmin blocks the server's IP, pasting the blob from
`garmin-mcp export`.

## Install: local

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
# or, from a clone:  pipx install .
garmin-mcp login
```

(Not on PyPI yet. Once it is, `uv tool install garmin-mcp` will do.)

Claude Code:

```bash
claude mcp add garmin -- garmin-mcp serve
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "garmin": { "command": "garmin-mcp", "args": ["serve"] }
  }
}
```

## Install: hosted

```bash
cp .env.example .env      # PUBLIC_URL and APP_SECRET, see below
docker compose build && docker compose up -d
```

Put a reverse proxy with TLS in front of it. With Caddy:

```
mcp.garmin.example.com {
    reverse_proxy mcp-garmin:8000 {
        flush_interval -1
    }
    encode gzip
}
```

Then invite people:

```bash
docker exec mcp-garmin garmin-mcp invite create --label "Anja"
# -> https://mcp.garmin.example.com/signup?code=...   (7 days, single use)
```

Each person opens the link, creates an account, connects their Garmin, and adds
the connector in their AI client:

- **claude.ai**: Settings -> Connectors -> Add custom connector ->
  `https://mcp.garmin.example.com/mcp`. Claude registers itself (RFC 7591), the
  person logs in on your server and confirms access.
- **ChatGPT**: Settings -> Connectors -> Create (developer mode must be enabled
  for the workspace; Plus/Pro/Business), same URL, authentication OAuth.

Accounts see only their own Garmin data. There is no shared bearer token and no
admin web interface: everything administrative happens on the command line.

## Tools

| Tool | What it answers |
|---|---|
| `list_activities` | Activities by date range and sport |
| `get_activity` | Garmin's own record of one activity |
| `analyze_activity_fit` | The same activity as the **device** recorded it |
| `get_activity_streams` | Downsampled time series, any channel in the file |
| `get_swim_detail` | Pool swim: every length, pace per 100 m, stroke count |
| `get_activity_sensors` | Which Connect-IQ sensors actually delivered data |
| `get_daily_health` | Steps, RHR, stress, Body Battery, sleep, HRV, readiness |
| `get_training_status` | Status phrase, load focus, VO2max |
| `get_body_composition` | Weight and body composition over a range |
| `get_blood_pressure` | Blood pressure measurements over a range |
| `list_challenges` | Social challenges against friends, newest first |
| `get_challenge` | The full leaderboard of one challenge |
| `list_gear` | Shoes and bikes with accumulated distance |
| `get_profile` | Thresholds, zones, VO2max, FTP, critical swim speed |
| `whoami` | Which account is connected, token validity |

Every result is projected down to the fields that matter - Garmin's raw JSON
would eat the context window for breakfast. Streams are resampled to 120 points
by default with min/max/avg per channel.

## Commands

```
garmin-mcp login              log in to Garmin Connect and store the tokens
garmin-mcp status             which account, how long the access token is valid
garmin-mcp export             tokens as a base64 blob (for GARMIN_TOKENS or import)
garmin-mcp logout             delete the stored tokens
garmin-mcp serve              stdio transport
garmin-mcp serve --http       streamable HTTP on /mcp

garmin-mcp invite create --label "Anja"    one-time signup link
garmin-mcp invite list                     open, used, expired
garmin-mcp user list                       accounts and their Garmin connection
garmin-mcp user disable <email>            blocks immediately, tokens kept
garmin-mcp user enable <email>
garmin-mcp user delete <email> --yes       account, tokens and cache
```

## Configuration

| Variable | Meaning |
|---|---|
| `PUBLIC_URL` | Public HTTPS URL; discovery documents and signup links are built from it |
| `APP_SECRET` | Encrypts the stored Garmin tokens. The server refuses to start without it, and changing it forces everyone to reconnect |
| `MCP_DB` | SQLite file (default `~/.garmin-mcp/app.db`, `/data/app.db` in the container) |
| `GARMIN_MCP_CACHE` | FIT cache directory, one subdirectory per account |
| `GARMIN_TOKENS_FILE` | stdio only: token file (default `~/.garmin-mcp/tokens.json`) |
| `GARMIN_TOKENS` | stdio only: base64 token blob instead of a file |

## What a hosted server stores, and what that means

Per account: an e-mail address, an argon2 password hash, and the Garmin OAuth
tokens, encrypted with `APP_SECRET`. Garmin passwords are never stored. Cached
FIT files live in a directory per account. Deleting an account removes all of it.

Be clear-eyed about the encryption: it protects a stolen backup or volume
snapshot. It does not protect against someone who owns the server, because the
key sits in the same `.env`. For the same reason, **not** backing up this data is
a defensible choice: tokens can be reissued in two minutes, and every backup is
another copy of somebody else's health data.

If you invite other people, you are handling their health data. Keep it
invite-only, tell them what is stored (the signup page does), and honour
deletion requests - the delete button does the real thing.

This uses Garmin's **internal** Connect API, the same one every community tool
uses; there is no official API for individuals. All accounts on one server share
one IP towards Garmin, so keep the circle small.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
