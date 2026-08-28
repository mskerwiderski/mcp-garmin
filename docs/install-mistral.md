# Installing for Mistral Vibe (formerly Le Chat)

Mistral renamed Le Chat to **Vibe** during 2026; older articles, and the app
listing for a while, still say Le Chat. It is the same product, and existing
accounts, conversations and plans carried over.

Vibe can talk to any remote MCP server, so this route needs a hosted
instance: be [invited as a guest](guest-access.md) to one that already runs, or
[host your own](self-hosting.md).

It cannot start a program on your computer, so the local route in
[install-claude.md](install-claude.md) does not apply here.

## Why this works without any special handling

Mistral's custom connectors speak **streamable HTTP** and support **OAuth 2.1
with dynamic client registration** - which is exactly what this server offers.
There is nothing to configure beyond the address: the app detects the
authentication method itself and walks you through the consent screen.

## Where it runs

| | Custom MCP connectors |
|---|---|
| Browser (chat.mistral.ai) | yes |
| iOS and Android app | yes, including adding them |
| macOS | no native app - use the browser |

Unlike ChatGPT, the phone app is not a second-class citizen here: connectors can
be added and used from it.

## Before you start

- An account on an instance, with Garmin connected (steps 1 to 3 below).
- Its connector URL, shown on your account page. It ends in `/mcp`.
- Permission to add connectors. Adding one is an administrator function; on
  personal plans you are the administrator of your own workspace. In a team
  workspace, an administrator has to add it.

## 1. Create your account

Open the invitation link you were given
(`https://mcp.example.com/signup?code=...`). It works once and expires after
seven days. Choose an e-mail address and a password of at least ten characters.

No link yet? See [guest access](guest-access.md).

## 2. Connect Garmin

On your account page, click **Connect Garmin** and enter your Garmin e-mail and
password, plus a multi-factor code if your account uses one. The password is
used once to obtain OAuth tokens and is never stored.

If the server reports a rate limit or bot protection, use the token blob
instead - expand **"Garmin refuses the login from here?"** and follow the steps
in [guest-access.md](guest-access.md#3-connect-your-garmin-account).

The account page must end up saying *Connected as <your name>*.

## 3. Add the connector

**Connectors → + Add Connector → Custom MCP Connector**. If the entry is not in the main navigation, open the sidebar and look under **Intelligence**.

| Field | Value |
|---|---|
| Connector name | `garmin` - no spaces, no special characters |
| Server URL | `https://mcp.example.com/mcp` |
| Description | `My Garmin Connect data: activities, sleep, HRV, training status` |

Save. Mistral detects that the server uses OAuth, sends you to its login page,
and you confirm access with the account from step 1.

## 4. Use it

Enable the connector for your conversation and ask:

> What were my last five runs, with distance, pace and average heart rate?

More ideas in [usage.md](usage.md).

## Limitations on Mistral's side

These come from the custom connector implementation, not from this server:

- **No dynamic tool discovery.** When this server gains new tools, Mistral will
  not pick them up on its own - remove the connector and add it again.
- **No resources and no prompt templates.** Only tools are used. Nothing here
  depends on either, so this costs you nothing today.

## Troubleshooting

**"Could not connect" when saving**
The most common cause is the address. It must end in `/mcp` - the base URL
alone does not answer the MCP handshake. Check in a browser that
`https://your-server/healthz` returns `{"ok": true}`.

**The connector name is rejected**
Mistral wants a single word without spaces or special characters. `garmin`
works, `Garmin Connect` does not.

**The OAuth window opens but the login fails**
Use the account you created in step 1, not your Garmin credentials. Those are
two different things: the first is your login to the connector, the second is
what the connector uses to read Garmin.

**Every answer says "your Garmin account is not connected yet"**
Step 2 was not completed for this account. Open `/account` on your instance; it
must say *Connected as ...*.
