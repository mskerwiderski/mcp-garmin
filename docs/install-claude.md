# Installing for Claude

Three ways to use this with Claude. Pick one - they do not conflict, but you
only need one.

| | Claude Desktop / Claude Code | claude.ai in the browser |
|---|---|---|
| Runs | on your machine | on a server you host |
| Setup | install, log in, register | [self-hosting guide](self-hosting.md) first |
| Section | [1](#1-claude-code) and [2](#2-claude-desktop) | [3](#3-claudeai) |

---

## 1. Claude Code

### Install

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
```

No `uv`? `pipx install git+https://github.com/mskerwiderski/mcp-garmin` works
the same way, and so does a plain `pip install` inside a virtual environment.

Check that the command exists:

```bash
garmin-mcp --help
```

### Connect your Garmin account

```bash
garmin-mcp login
```

You are asked for your Garmin e-mail and password, and for a multi-factor code
if your account uses one. What gets stored is only Garmin's OAuth tokens, in
`~/.garmin-mcp/tokens.json` with file mode 0600 - never your password.

Verify:

```bash
garmin-mcp status
```

```
connected as Your Name
access token valid until 2026-08-27T16:54:55
```

The access token is short-lived and refreshes itself. The long-lived token
behind it is good for about a year.

### Register the server

```bash
claude mcp add garmin -- garmin-mcp serve
```

Check it:

```bash
claude mcp list
```

```
garmin: /Users/you/.local/bin/garmin-mcp serve - ✓ Connected
```

**Start a new session** - Claude Code loads MCP servers at startup, so a session
that was already running will not see it. Then ask:

> What were my last three activities?

There is no long-running process to look for. The client starts the server when
it needs it and stops it afterwards; `ps` showing nothing in between is correct.

---

## 2. Claude Desktop

Install and log in exactly as in section 1, then find the config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the server:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "garmin-mcp",
      "args": ["serve"]
    }
  }
}
```

If Claude Desktop reports that the command was not found, it does not share your
shell's `PATH`. Use the absolute path instead - `which garmin-mcp` prints it:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/Users/you/.local/bin/garmin-mcp",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Desktop completely (quit, not just close the window). The tools
appear under the connectors icon in the message box.

---

## 3. claude.ai

The browser cannot start a local process, so this needs a hosted instance. Three
ways to get one: be [invited as a guest](guest-access.md) to an existing
instance, [run your own](self-hosting.md), or use one a friend runs.

### Sign up on the instance

1. Open the invitation link you were given. It looks like
   `https://mcp.example.com/signup?code=...`, works once and expires after seven
   days. No link yet? See [guest access](guest-access.md).
2. Create your account with an e-mail address and a password of at least ten
   characters.
3. On your account page, click **Connect Garmin** and enter your Garmin
   credentials. Your Garmin password is used once to obtain tokens and is not
   stored. If Garmin refuses the login from that server, expand **"Garmin
   refuses the login from here?"** and use the token blob instead - see
   [below](#the-token-blob-fallback).
4. The account page now shows *Connected as <your name>* and, right underneath,
   the connector URL. Copy it - it ends in `/mcp`.

### Add the connector

Claude Code can also use a remote instance instead of a local process:

```bash
claude mcp add --transport http garmin https://mcp.example.com/mcp
```

Run `/mcp` in a session afterwards to complete the OAuth login. For claude.ai in
the browser:

1. **Settings → Connectors → Add custom connector**.
2. Paste the URL, for example `https://mcp.example.com/mcp`.
3. Claude registers itself and sends you to the server's login page. Log in with
   the account from step 2 and click **Allow**.
4. The connector appears in your list. Open a new chat and ask something.

### The token blob fallback

Garmin sometimes refuses logins that come from data centres. In that case, log
in from your own machine and move the tokens over:

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
```

```bash
garmin-mcp login
```

```bash
garmin-mcp export
```

The last command prints one long line. Copy it completely, without line breaks,
and paste it into the **Token blob** field on the server's *Connect Garmin*
page. This is also the quickest way to move an existing local installation to a
server.

---

## Troubleshooting

**"your Garmin account is not connected yet"**
The server is reachable but has no tokens for your account. Locally: run
`garmin-mcp login`. On a hosted instance: open `/account` and connect Garmin.

**`claude mcp list` says "Failed to connect"**
Run `garmin-mcp serve` by hand. If it exits immediately, the error is printed.
A missing `garmin-mcp` on `PATH` is the usual cause.

**The tools do not show up in a running session**
MCP servers are loaded when a session starts. Start a new one.

**Login fails with 429, 403, or a mention of Cloudflare**
Garmin is blocking the address you are logging in from. From home this usually
means a VPN is on - turn it off and retry. From a server, use the token blob
fallback above.

**"MFA code rejected"**
Codes expire quickly. Start the login again to get a fresh one. On a hosted
server the pending login is discarded after ten minutes.

**Everything works but answers are empty**
Ask Claude to call `whoami`. It reports which Garmin account is connected. An
empty result with a working connection usually means the data really is not
there - Garmin files sleep under the morning you woke up, and training readiness
only exists once the watch has synced after waking.
