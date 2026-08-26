# mcp-garmin

Read-only, multi-tenant MCP server for Garmin Connect.

**Read [docs/architecture.md](docs/architecture.md) before changing anything.**
It carries the design decisions and the Garmin quirks behind them; this file
only adds what is specific to working in this repository.

Documentation and code comments are English here, deviating from the portfolio
convention, because this repository is written to be handed to other people.

## Rules that are easy to break

- **`client.py` and `fit/*` are vendored from MyFITContainer.** Fixes have to be
  carried to the other repository by hand. Additions here carry a
  `mcp-garmin addition` marker.
- **Every tool needs a projection.** Raw Garmin JSON in a response is a bug -
  see the projection section in the architecture document.
- **Every tool needs `_guard`.** Without it the SDK swallows the cause and the
  model sees `Error executing tool <name>`.
- **Never bind a session at registration time.** `tools.register` takes a
  resolver function; an instance would make the server single-tenant.
- **`db.conn()` skips its commit when an exception passes through.** Commit
  before raising.
- **Docstrings are user interface.** They are what the model reads when deciding
  which tool to call.

## Working on it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check --select E9,F garmin_mcp tests
```

Tests never touch the network or the real `~/.garmin-mcp`; `conftest.py` points
every path at a temporary directory.

## Deployment

Runs on the Strato VPS as `/root/mcp-garmin` behind
`mcp.garmin.skerwiderski.cloud`, following the house container pattern: no host
port binding, joins the shared `root_default` network, Caddy proxies by
container name with `flush_interval -1`.

Deploy is rsync plus `docker compose build && up -d` - `/root/mcp-garmin` is not
a git clone. The `.env` on the server is never touched from here.

Everything persistent is in the volume at `/data`. The volume is deliberately
not backed up (it holds other people's Garmin tokens, reissued in minutes);
`APP_SECRET` belongs in a password manager instead.

Administration runs through `docker exec mcp-garmin garmin-mcp invite|user` or
the `/admin` page. See [docs/self-hosting.md](docs/self-hosting.md).
