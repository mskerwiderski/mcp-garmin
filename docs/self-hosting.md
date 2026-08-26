# Self-hosting on a server

The third of the [three ways](../README.md#three-ways-to-use-it) to use this:
your own instance, on your own machine, for you and whoever you invite. Everyone
gets their own account and their own Garmin connection; nobody sees anybody
else's data.

This is what claude.ai and ChatGPT need. If you only use Claude Desktop or
Claude Code and want nothing on a server, the local route in
[install-claude.md](install-claude.md) is simpler. If you would rather not run
anything at all, ask for [guest access](guest-access.md).

Plan for about twenty minutes.

## What you need

- A machine that can run Docker, reachable from the internet.
- A domain name pointing at it, and a reverse proxy that terminates TLS. The
  examples use [Caddy](https://caddyserver.com), which obtains certificates on
  its own.
- Nothing else. No database server, no Redis, no object storage - state is one
  SQLite file and a cache directory.

Resource use is small: a few hundred megabytes of RAM, and disk that grows with
the FIT cache (at most 40 files per account).

## 1. Get the code

```bash
git clone https://github.com/mskerwiderski/mcp-garmin.git && cd mcp-garmin
```

## 2. Configuration

```bash
cp .env.example .env
```

Two variables, both required:

| Variable | Meaning |
|---|---|
| `PUBLIC_URL` | The public HTTPS address, no trailing slash. OAuth discovery documents, signup links and the connector URL are built from it, so it must match what people paste into their AI client. |
| `APP_SECRET` | Encrypts the stored Garmin tokens. The server refuses to start without it. |

Generate the secret on the machine itself and never commit it:

```bash
printf 'PUBLIC_URL=https://mcp.example.com\nAPP_SECRET=%s\n' "$(openssl rand -base64 36 | tr -d '=+/')" > .env && chmod 600 .env
```

**Put `APP_SECRET` in your password manager.** Lose it and every account has to
reconnect Garmin - the stored tokens become unreadable by design.

## 3. Start it

```bash
docker compose build && docker compose up -d
```

Check:

```bash
docker compose ps
```

The container reports `healthy` once it is serving. Everything persistent lives
in the `mcp_garmin_data` volume under `/data`: the SQLite database and the FIT
cache.

## 4. Put a proxy in front

With Caddy, two lines are enough:

```
mcp.example.com {
    reverse_proxy mcp-garmin:8000 {
        flush_interval -1
    }
    encode gzip
}
```

`flush_interval -1` disables response buffering, which streaming transports
need. If your proxy is not on the same Docker network, replace the upstream with
a published port instead.

Verify from outside:

```bash
curl https://mcp.example.com/healthz
```

```json
{"ok": true}
```

A repository-provided stack including Caddy is in
[`deploy/`](../deploy/docker-compose.oracle.yml) if the host has nothing yet.

## 5. Create your own account

The **first account** on a server administers it. Create an invitation for
yourself:

```bash
docker exec mcp-garmin garmin-mcp invite create --label "me"
```

Open the printed link, choose an e-mail and a password of at least ten
characters, then connect Garmin on your account page. From then on everything
administrative is on the web at `/admin`, linked from your account page.

## Inviting people

On `/admin`: type a note ("Anja"), click **Create invitation link**, send the
link. It works once and expires after seven days. There is no open signup page -
without a valid code, `/signup` returns 404.

The same from the command line:

```bash
docker exec mcp-garmin garmin-mcp invite create --label "Anja"
```

What the person then does is in [install-claude.md](install-claude.md) section 3
or [install-chatgpt.md](install-chatgpt.md).

## Administration

The admin page lists every account with its Garmin connection state, last login
and status, and lets you disable, enable or delete. Disabling takes effect
immediately, including for AI clients that are already connected: their access
tokens stop working on the next call. Deleting removes the account, its Garmin
tokens and its cached files.

You cannot change your own account there - that would let you lock yourself out
with one click. Use the command line for that.

```bash
docker exec mcp-garmin garmin-mcp user list
docker exec mcp-garmin garmin-mcp user disable someone@example.com
docker exec mcp-garmin garmin-mcp user enable someone@example.com
docker exec mcp-garmin garmin-mcp user promote someone@example.com
docker exec mcp-garmin garmin-mcp user delete someone@example.com --yes
docker exec mcp-garmin garmin-mcp invite list
```

The CLI is deliberately kept complete: it is the way back in if nobody can log
in any more.

## Upgrading

```bash
git pull && docker compose build && docker compose up -d
```

Database migrations run at startup and are additive. The volume is not touched.

## What the server stores

Per account: an e-mail address, an argon2 password hash, and Garmin's OAuth
tokens encrypted with `APP_SECRET`. Garmin passwords are never stored - they are
used once during the login and discarded. Cached FIT files live in a directory
per account and are deleted with the account.

Be clear-eyed about that encryption: it protects a stolen backup or a volume
snapshot. It does not protect against someone who owns the server, because the
key sits in the same `.env`.

## Backups

Consider **not** backing this up. Garmin tokens can be reissued in two minutes
by reconnecting, so the recovery value is small - while every backup is another
copy of other people's health data. What is genuinely worth preserving is
`APP_SECRET`, in your password manager.

If you do back up the volume, treat it like a credential store.

## Running it for other people

If you invite others, you are handling their health data.

- Keep it invite-only. That is the default and there is no switch to change it.
- The signup page tells people what is stored; leave that text in place.
- Honour deletions. The delete button really deletes, immediately.
- Keep the circle small. Every account on one server shares one address towards
  Garmin, and Garmin rate-limits by address.
- Depending on where you live, doing this for others may make you a data
  controller with duties attached. That is a real consideration, not a
  formality.

## Troubleshooting

**The container exits immediately**
`docker compose logs mcp-garmin`. A missing `APP_SECRET` is the usual cause, and
it says so.

**The OAuth flow redirects to the wrong host**
`PUBLIC_URL` does not match the address people actually use. It is the single
source for discovery documents and links.

**Someone cannot connect Garmin, rate limit or bot protection**
Garmin is blocking your server's address. They should use the token blob route
on the *Connect Garmin* page - see [install-claude.md](install-claude.md).

**`/signup` returns 404 with a code**
The invitation was used or expired. Create a new one.

**An AI client keeps saying "not connected yet"**
That account exists but has no Garmin tokens. `garmin-mcp user list` shows the
connection state per account.
