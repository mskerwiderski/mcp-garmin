# garmin-mcp

Read-only MCP server for **Garmin Connect**. Ask Claude or ChatGPT about your
own training: activities, heart rate, sleep, HRV, Body Battery, training status
— and, unlike other Garmin MCP servers, the **original FIT file** the watch
wrote, including Connect-IQ sensor channels (Stryd, SmO2, CORE).

Single user by design: you run your own instance with your own Garmin account.
Nothing here writes to Garmin Connect.

## Why the login is a local command

Garmin's SSO sits behind Cloudflare, which since March 2026 answers fresh logins
from datacenter IPs with 429/403. So the login runs on **your machine**:

```bash
garmin-mcp login
```

It handles MFA and stores the resulting OAuth1/OAuth2 tokens in
`~/.garmin-mcp/tokens.json` (mode 0600). The OAuth1 token is valid for about a
year and mints OAuth2 access tokens against `connectapi.garmin.com`, so the
server never touches `sso.garmin.com` — and your Garmin password never leaves
your machine.

## Install

### 1. Local, for Claude Desktop / Claude Code / ChatGPT desktop (stdio)

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

That is the whole setup. No server, no OAuth, no HTTPS.

### 2. Remote, for claude.ai and ChatGPT (streamable HTTP)

The web clients cannot run a local process and have no field for a static
bearer header, so the server needs a public HTTPS URL and OAuth. Both are
built in — you only supply a passphrase.

```bash
garmin-mcp login          # on your machine
garmin-mcp export         # prints the GARMIN_TOKENS blob for the server
```

On the host:

```bash
cp .env.example .env      # set PUBLIC_URL, MCP_PASSPHRASE, GARMIN_TOKENS
docker compose build && docker compose up -d
```

Put a reverse proxy with TLS in front of it (Caddy is two lines):

```
mcp-garmin.example.com {
    reverse_proxy mcp-garmin:8000
    encode gzip
}
```

Then in **claude.ai**: Settings → Connectors → Add custom connector →
`https://mcp-garmin.example.com/mcp`. Claude registers itself (RFC 7591), you
get the consent screen, you type the passphrase, done.

In **ChatGPT**: Settings → Connectors → Create (developer mode must be enabled
for your workspace; Plus/Pro/Business), same URL, authentication OAuth.

### 3. Free hosting in Germany

The container is small and needs no database. What actually works:

| Host | Verdict |
|---|---|
| **Oracle Cloud Free Tier, `eu-frankfurt-1`** | Genuinely free, always on, own IP, persistent disk. Best free option. See below. |
| Own VPS | If you already have one with a reverse proxy, this is 10 minutes of work. |
| Render Free (Frankfurt) | Works, but spins down after 15 min idle and has no persistent disk. Set `GARMIN_TOKENS` and expect a cold start on the first tool call. |
| Fly.io / Koyeb | No dependable free tier in 2026. |
| Hugging Face Spaces | Free, but US region and it sleeps. |

**Oracle Cloud Free Tier, step by step**

1. Create an Always Free compute instance in `eu-frankfurt-1` (Ampere A1, or
   two E2.1.Micro if ARM capacity is unavailable), Ubuntu 24.04.
2. Open ports 80 and 443 **twice**: in the VCN security list *and* in the
   instance's own firewall. The Oracle Ubuntu image ships with iptables rules
   that drop them, and without port 80 the TLS certificate challenge fails.
   ```bash
   sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. Point an A record at the instance IP. A wildcard record pointing elsewhere
   does not help — you need an explicit one that overrides it.
4. Install Docker, copy this repo, fill `.env`, then
   `docker compose up -d` plus a Caddy container for TLS.

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
| `list_gear` | Shoes and bikes with accumulated distance |
| `get_profile` | Thresholds, zones, VO2max, FTP, critical swim speed |
| `whoami` | Which account is connected, token validity |

Every result is projected down to the fields that matter — Garmin's raw JSON
would eat the context window for breakfast. Streams are resampled to 120 points
by default with min/max/avg per channel.

## Commands

```
garmin-mcp login        log in to Garmin Connect and store the tokens
garmin-mcp status       which account, how long the access token is valid
garmin-mcp export       tokens as a base64 blob for GARMIN_TOKENS
garmin-mcp logout       delete the stored tokens
garmin-mcp serve        stdio transport
garmin-mcp serve --http streamable HTTP on /mcp
```

## Configuration

| Variable | Meaning |
|---|---|
| `PUBLIC_URL` | Public HTTPS URL; the OAuth discovery documents are built from it |
| `MCP_PASSPHRASE` | Consent screen passphrase. Without it the server refuses to authorize connectors |
| `MCP_TOKEN` | Optional static bearer for header-capable clients |
| `GARMIN_TOKENS` | Optional base64 token blob for hosts without a disk |
| `GARMIN_TOKENS_FILE` | Token file path (default `~/.garmin-mcp/tokens.json`) |
| `MCP_STATE_FILE` | Registered OAuth clients (default `~/.garmin-mcp/oauth.json`) |
| `GARMIN_MCP_CACHE` | FIT cache directory (default `~/.garmin-mcp/cache`) |

## Notes

- This uses Garmin's **internal** Connect API, the same one every community
  tool uses. There is no official API for individuals; Garmin's Health API is a
  partner programme. Use this for your own account.
- Anyone who knows `MCP_PASSPHRASE` can connect a client to your Garmin data.
- The Garmin client and the FIT parsing are vendored from
  [MyFITContainer](https://github.com/mskerwiderski/MyFITContainer); the file
  headers name the source commit.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
