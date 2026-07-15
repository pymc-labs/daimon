# daimon

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Self-hostable Discord bot built on [Anthropic Managed Agents](https://docs.anthropic.com/en/api/managed-agents/).

**One-click install, operator-run.** You deploy daimon once, on your own
Anthropic API key, and it serves any number of Discord servers from that
single deployment — each server is an isolated tenant, so guilds never see
each other's data even though they share your key. You install it, you own
the key, you're responsible for it; the people in your servers just
`@mention` the bot and get a working agent.

## Features

- **Mention-triggered threaded conversations** — `@daimon` in a channel
  starts (or continues) a threaded turn with session continuity
- **Slash-command admin surface** — `/config`, `/agents`, `/environments`,
  `/skills`, `/help` mirror the CLI, gated by Discord's own
  `Manage Server` permission
- **CLI + MCP adapters** alongside Discord, all sharing the same core turn
  pipeline and tenant-scoped stores
- **Slack adapter** (optional) — full parity with Discord, per-workspace
  OAuth install, plus opt-in per-user Slack access; see
  [`docs/slack.md`](docs/slack.md) for how per-user access is scoped
- **Per-tenant isolation** enforced at the database `tenant_id` layer, not
  the API-key boundary — one shared Anthropic key powers every guild

## Requirements

- An Anthropic API key **with Managed Agents beta access** — Managed Agents
  is a closed beta; request access before you start, or the quickstart below
  will fail at session-create time
- [Docker](https://docs.docker.com/get-docker/) (for Postgres, and for
  `docker compose up` deploys)
- [`uv`](https://docs.astral.sh/uv/) (Python package/dependency manager)
- Python 3.12+ (managed automatically by `uv` if not already installed)

## Quickstart

Every command below runs in order from a clean checkout and ends with a bot
that responds to an `@mention` in Discord.

### 1. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in:

- `DAIMON_ANTHROPIC__API_KEY` — your beta-enabled Anthropic key
- `DAIMON_MCP__JWT_SECRET` — any random string (e.g. `openssl rand -hex 32`)
- `DAIMON_MCP__PUBLIC_URL` — `http://localhost:8765/mcp` is fine for local use

`docker compose` interpolates every service in `docker-compose.yml` — including
`mcp` and `scheduler` — for **any** command against the file, even
`docker compose up -d postgres`. Those three vars are guarded with
`${VAR:?...}` (fail-fast on missing value), so all three must be set in `.env`
before the first `docker compose` command below, not just the Anthropic key.
You'll add `DAIMON_DISCORD__BOT_TOKEN` in step 4. `.env` is gitignored —
secrets never get committed here.

### 2. Install dependencies

```bash
uv sync
```

### 3. Start Postgres and run migrations

```bash
docker compose up -d postgres
export DAIMON_DATABASE_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon
uv run alembic upgrade head
```

The `alembic` CLI reads the **flat** `DAIMON_DATABASE_URL` env var (not the
nested `.env` value) — this `export` is required every time you run alembic
directly. The running app reads the nested `DAIMON_DATABASE__URL` from `.env`
instead; you don't need to export that one.

Seed the default agents/environments/skills:

```bash
uv run daimon defaults apply
```

### 4. Create and configure the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and create a new application.
2. Under **Bot**, create a bot user and copy its token into `.env` as
   `DAIMON_DISCORD__BOT_TOKEN`.
3. Still under **Bot**, enable the **Message Content Intent** — this is a
   privileged intent and the bot will not receive message text without it
   (the adapter requests `intents.message_content = True` at startup and
   will fail to read mentions correctly if the portal toggle is off).
4. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, then under **Bot Permissions** select at
   least: `Send Messages`, `Send Messages in Threads`,
   `Create Public Threads`, `Manage Threads`, `Read Message History`.
5. Copy the generated URL, open it in a browser, and invite the bot to a
   test server you control.

### 5. Run the bot

```bash
uv run python -m daimon.adapters.discord
```

In the Discord server you invited the bot to, send a message that
`@mention`s the bot. It replies in a new thread — that's a working
deployment.

## Layout

- `packages/core/` — `daimon-core` library (MA client, stores, turn pipeline)
- `packages/adapters/cli/` — the `daimon` admin CLI
- `packages/adapters/discord/` — the Discord bot adapter
- `packages/adapters/mcp/` — the MCP server adapter
- `packages/adapters/slack/` — the Slack adapter (optional)
- `defaults/` — YAML defaults seeded into Managed Agents + local DB
- `docs/slack.md` — Slack adapter per-user access model
- `scripts/probes/` — Managed Agents API probes

## Architecture

- **Core / adapters split**: `daimon.core` owns schema, stores, and the turn
  pipeline; it has zero adapter imports. Each `daimon.adapters.*` package
  (CLI, Discord, MCP, Slack) owns one platform's I/O, rendering, and auth,
  and adapters never import each other. Enforced by `import-linter` in CI.
- **Managed Agents is the source of truth** for agent/environment/session/
  skill/vault content. Our own database holds identity, namespacing, and
  provenance only — no local mirror of MA state.
- **Tenancy**: one Discord server (guild) that has installed the bot is one
  daimon tenant. Many tenants share a single Anthropic API key / MA
  workspace — the operator's. Isolation is enforced at the database
  `tenant_id` layer (and matching MA resource metadata), not at the
  API-key boundary.

## Deploy

- **Docker Compose** (this repo's `docker-compose.yml`) runs Postgres plus
  the `mcp`, `discord`, and `scheduler` process groups from one image —
  see the Quickstart above for the `.env` prerequisite.
- A Terraform/GCP deployment guide is planned.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev environment setup and the
quality gates every PR must keep green.

## Security

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability.

## License

[MIT](LICENSE)
