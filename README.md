# garmin-mcp

**Talk to your Garmin data.** A read-only [MCP](https://modelcontextprotocol.io)
server that lets Claude, ChatGPT and Mistral Le Chat answer questions about
your training:
activities, sleep, HRV, Body Battery, training status, challenge leaderboards -
and, unlike other Garmin MCP servers, the **original FIT file** your watch
wrote, including Connect-IQ sensor channels (Stryd, Moxy/SmO2, CORE).

```
You:  How did my last long run compare to the same route in June?
You:  Why is my training readiness so low this morning?
You:  Where do I stand in the running challenge, and who is ahead of me?
You:  Did my Stryd actually record power on Tuesday's session?
```

It never writes to Garmin Connect. Every tool is read-only.

---

## Three ways to use it

| | **1. Guest access** | **2. Local** | **3. Your own server** |
|---|---|---|---|
| Status | invitation only, **open access planned** | available | available |
| You need | an invitation | Python 3.12 | a server, a domain, Docker |
| You run | nothing | the server on your machine | the server for you and your friends |
| Works with | claude.ai, ChatGPT, Le Chat, Claude Desktop, Claude Code | Claude Desktop, Claude Code | everything |
| Setup | 5 minutes | 2 minutes | 20 minutes |
| Your data lives | on the instance that invited you | only on your machine | on your server |
| Guide | [Guest access](docs/guest-access.md) | [below](#quickstart-local) | [Self-hosting](docs/self-hosting.md) |

**1. Guest access** - the only route that needs no infrastructure at all: open
the invitation link, create an account, connect your Garmin, done. Like route 3,
it works with **claude.ai in the browser, with ChatGPT and with Mistral Le
Chat** - route 2 cannot, because none of those can start a program on your
computer.

> **Guest access is not open yet.** It currently runs with a small group of
> testers, by personal invitation. A public way to request an invitation is
> planned - until then, routes 2 and 3 are ready to use today.

**2. Local** - nothing leaves your machine, no accounts, no server. Your AI
client starts the process when it needs it. Best if you use Claude Desktop or
Claude Code anyway and want to keep everything local.

**3. Your own server** - the same software as behind guest access. Run it for
yourself, invite whoever you like, own the data. See
[docs/self-hosting.md](docs/self-hosting.md).

## Quickstart (local)

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
```

```bash
garmin-mcp login
```

The login asks for your Garmin e-mail, password and - if you use it - your
multi-factor code. It stores only Garmin's OAuth tokens, in
`~/.garmin-mcp/tokens.json` with mode 0600. Your password is never written
anywhere.

Then register the server with your client:

```bash
claude mcp add garmin -- garmin-mcp serve
```

Start a new session and ask something. Full walkthrough, including Claude
Desktop: [docs/install-claude.md](docs/install-claude.md).

## Documentation

| Guide | What is in it |
|---|---|
| [Guest access](docs/guest-access.md) | Getting an invitation and using it, start to finish |
| [Install for Claude](docs/install-claude.md) | Claude Desktop, Claude Code, claude.ai, troubleshooting |
| [Install for ChatGPT](docs/install-chatgpt.md) | Developer mode, custom connector, troubleshooting |
| [Install for Mistral Le Chat](docs/install-mistral.md) | Custom MCP connector, OAuth, troubleshooting |
| [Usage examples](docs/usage.md) | What to ask, what comes back, what the numbers mean |
| [Tool reference](docs/tools.md) | Every tool, its parameters and an example response |
| [Self-hosting](docs/self-hosting.md) | Server setup, invitations, administration, upgrades |
| [How it works](docs/architecture.md) | Design decisions, and the Garmin quirks behind them |

## What it can answer

**Activities** - list and filter by sport and date, Garmin's own summary of one
activity, and the same activity as the device recorded it, straight from the
FIT file. Time series for any channel in the file, pool swim detail down to the
single length, and which Connect-IQ sensors actually delivered data.

**Health** - one call per day gives steps, resting heart rate, stress, Body
Battery, sleep phases with score, HRV status and training readiness. Plus
training status with load focus and VO2max, body composition and blood pressure
over a date range.

**Trends and plan** - a whole month of steps, Body Battery and VO2max in one
call instead of one per day; the scheduled side of Garmin with training-plan
workouts and races, so "what was planned" and "what happened" can be compared.

**Where you stand** - race predictions for 5k to marathon, fitness age,
endurance and hill score, lifetime totals, and your personal records with the
activity that set each one.

**Context** - your thresholds and heart rate zones, your gear with the
activities, kilometres, hours and days behind it, and the social challenges you run against friends, including the one
currently in progress.

See [docs/tools.md](docs/tools.md) for the full list with parameters.

## Why the login happens on your machine

Garmin's SSO sits behind Cloudflare, which since March 2026 answers fresh logins
from datacenter IPs with 429 or 403. So `garmin-mcp login` runs where you are.
The resulting OAuth1 token is valid for about a year and mints access tokens
against `connectapi.garmin.com`, so nothing after the login touches
`sso.garmin.com` again.

A hosted server offers both: a login form (the password is used once and never
stored) and pasting the token blob from `garmin-mcp export`, which is the way in
when Garmin blocks the server's address.

## Good to know

This uses Garmin's **internal** Connect API - the same one every community tool
uses. There is no official API for individuals; Garmin's Health API is a partner
programme. Use this for your own account, and keep a hosted instance to people
you actually know: all accounts on one server share one address towards Garmin.

Responses are deliberately small. Garmin answers with hundreds of fields per
activity and time series of thousands of points; everything is projected down to
what a model can actually use, and streams are resampled with min/max/avg per
channel. That is not cosmetic - the raw payloads would eat the context window
the conversation needs.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest
```

103 tests, no network access required - the Garmin API and the FIT files are
stubbed or synthesised. See [docs/architecture.md](docs/architecture.md) for how
the pieces fit together.

## License

MIT. See [LICENSE](LICENSE).
