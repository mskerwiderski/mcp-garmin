# Installing for ChatGPT

ChatGPT can only talk to a server it can reach over HTTPS - it cannot start a
program on your computer. So this route always needs a hosted instance: be
[invited as a guest](guest-access.md) to one that already runs, or
[host your own](self-hosting.md).

If you want the local, no-server route, use Claude Desktop or Claude Code
instead: [install-claude.md](install-claude.md).

## Before you start

- A ChatGPT plan that offers developer mode: Plus, Pro, Business, Enterprise or
  Edu. On Business and above, a workspace admin has to allow it first.
- The connector URL of your instance. It is shown on your account page and ends
  in `/mcp`, for example `https://mcp.example.com/mcp`.
- An account on that instance with Garmin connected - steps 1 to 3 below.

## 1. Create your account

Open the invitation link you were given
(`https://mcp.example.com/signup?code=...`). It works once and expires after
seven days. Choose an e-mail address and a password of at least ten characters.

No link yet? See [guest access](guest-access.md) - open access is planned, and
until then [hosting your own instance](self-hosting.md) is the way that needs
nobody else.

## 2. Connect Garmin

On your account page, click **Connect Garmin** and enter your Garmin e-mail and
password, plus a multi-factor code if your account uses one. The password is
used once to obtain OAuth tokens and is never stored.

If the server reports a rate limit or bot protection, Garmin is refusing logins
from that machine. Expand **"Garmin refuses the login from here?"**, then on your
own computer:

```bash
uv tool install git+https://github.com/mskerwiderski/mcp-garmin
```

```bash
garmin-mcp login
```

```bash
garmin-mcp export
```

Copy the single long line that `export` prints into the **Token blob** field and
submit. The account page must end up saying *Connected as <your name>*.

## 3. Copy the connector URL

It is on the account page, in the box titled *Use it in Claude or ChatGPT*.

## 4. Turn on developer mode

In ChatGPT: **Settings → Connectors → Advanced → Developer mode**.

On Business, Enterprise and Edu workspaces this switch only appears once an
admin has enabled it in **Workspace Settings → Permissions & Roles → Connected
data / custom MCP connectors**. If you cannot find the toggle, that is the
reason - ask your admin, or use a personal Plus or Pro account.

## 5. Add the connector

**Settings → Connectors → Create**, then fill in:

| Field | Value |
|---|---|
| Name | `Garmin` |
| Description | `My own Garmin Connect data: activities, sleep, HRV, training status` |
| MCP server URL | `https://mcp.example.com/mcp` |
| Authentication | OAuth |

The description matters more than it looks: the model reads it when deciding
whether a question is one this connector should answer.

Save. ChatGPT registers itself with the server and opens its login page. Sign in
with the account from step 1 and click **Allow**.

## 6. Use it

Start a new chat, open the connector menu in the composer and make sure `Garmin`
is enabled for the conversation. Then ask away:

> Give me my last five runs with distance, pace and average heart rate.

> What did my sleep and HRV look like last week?

More ideas in [usage.md](usage.md).

Every tool in this connector is read-only, so you can safely let ChatGPT call
them without confirming each one. If you prefer to approve each call, that
setting sits next to the connector.

## Troubleshooting

**Developer mode is not in Settings**
Your plan does not include it, or a workspace admin has not enabled it. See
step 4.

**"Could not connect" when saving the connector**
Open `https://your-server/healthz` in a browser - it must answer `{"ok": true}`.
If it does not, the server or its TLS certificate is the problem, not ChatGPT.
Make sure you entered the URL **including** `/mcp`.

**The OAuth window opens but the login fails**
Use the account you created in step 1, not your Garmin credentials. Those two
are unrelated: the first is your login to the connector, the second is what the
connector uses to read Garmin.

**Tools appear but every answer says "your Garmin account is not connected yet"**
Step 2 was not completed for this account. Open `/account` on your instance -
it must say *Connected as ...*.

**Answers stop working after a while**
Access tokens expire after an hour and ChatGPT refreshes them automatically. If
that fails, remove the connector and add it again. If the server administrator
disabled your account, every token stops working immediately and by design.

**The model does not use the connector**
Enable it explicitly for the conversation in the composer, or name it: "Using
the Garmin connector, show me ...". A vague description makes this worse, which
is why step 5 suggests a specific one.
