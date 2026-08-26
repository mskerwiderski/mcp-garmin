# Guest access

The fastest way to use this: no server, no installation, no Docker. You get an
account on an instance that already runs, connect your Garmin, and add the
connector to Claude or ChatGPT.

This is also the **only** route that works with claude.ai in the browser and
with ChatGPT, because neither of them can start a program on your computer.

If you would rather keep everything on your own machine, see
[install-claude.md](install-claude.md) for the local route, or
[self-hosting.md](self-hosting.md) to run your own server.

---

## 1. Ask for an invitation

Send a short mail to **michael@skerwiderski.de**. Registration is invite-only,
so there is no signup page to find - without a valid code, `/signup` simply
returns 404.

You get a link back that looks like this:

```
https://mcp.garmin.skerwiderski.cloud/signup?code=...
```

It works **once** and expires after **seven days**.

## 2. Create your account

Open the link and choose:

- an e-mail address - this is your login for the connector, nothing is sent to it
- a password of at least ten characters

This account has nothing to do with your Garmin account. It is only how the
server recognises you.

## 3. Connect your Garmin account

On your account page, click **Connect Garmin** and enter your Garmin e-mail and
password, plus a multi-factor code if your account uses one.

Your Garmin password is used once to obtain OAuth tokens from Garmin and is
never stored. Only those tokens are kept, encrypted.

**If the server says rate limit or bot protection:** Garmin sometimes refuses
logins that arrive from data centres. Expand **"Garmin refuses the login from
here?"** on that page and use the token blob instead. On your own computer:

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
```

```bash
garmin-mcp login
```

```bash
garmin-mcp export
```

The last command prints one long line. Copy it completely and paste it into the
**Token blob** field.

Either way, the account page must end up saying *Connected as <your name>*.
Underneath it you find your connector URL - it ends in `/mcp`.

## 4. Add the connector

Pick your client:

### claude.ai

**Settings → Connectors → Add custom connector**, paste the URL, save. Claude
sends you to the server's login page; sign in with the account from step 2 and
click **Allow**.

### ChatGPT

Needs developer mode - see [install-chatgpt.md](install-chatgpt.md) from step 4.
Short version: **Settings → Connectors → Create**, paste the URL, authentication
**OAuth**.

### Claude Desktop or Claude Code

Both can talk to a remote server too:

```bash
claude mcp add --transport http garmin https://mcp.garmin.skerwiderski.cloud/mcp
```

Then run `/mcp` in a session to complete the login. In Claude Desktop, add the
connector under Settings → Connectors with the same URL.

## 5. Ask something

> What were my last three activities?

> Why is my training readiness so low today?

More ideas in [usage.md](usage.md).

---

## What the server knows about you

- Your e-mail address and a hash of your password.
- Garmin's OAuth tokens for your account, encrypted. **Not** your Garmin
  password.
- Cached copies of the FIT files of activities you asked about, at most 40.

Nothing is shared with other accounts. The tools are read-only: nothing can
change or delete anything in your Garmin account.

## Deleting everything

Your account page has a **Delete account** button. It removes the account, the
Garmin tokens and the cached files immediately. No mail, no waiting period.

You can also just disconnect Garmin and keep the account, or revoke the
connector in Claude or ChatGPT - each of those is independent of the others.

## Honest limitations

- The instance is run by a person, not a company. There is no uptime guarantee
  and no support desk.
- All guests share one address towards Garmin, which rate-limits by address.
  That is why access is invite-only and the circle stays small.
- Whoever operates the server could technically read the database. The
  encryption protects backups, not the machine itself. If that matters to you,
  run [your own server](self-hosting.md) - it is the same software.
