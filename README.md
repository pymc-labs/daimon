<div align="center">
  <img src="assets/daimon-data-scientist.jpg" alt="daimon, a small blue clay creature in a lab coat, presenting charts at a whiteboard" width="440">

# daimon

**Your team just hired a data scientist.**

daimon is a collaborative data science agent in your team's Discord or Slack.
It writes and runs code, fits Bayesian models with PyMC, and delivers charts
and runnable notebooks in the thread.

[![CI](https://github.com/pymc-labs/daimon/actions/workflows/ci.yml/badge.svg)](https://github.com/pymc-labs/daimon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)

**[Add it to your server in one click →](https://daimon.decision.ai/)**
or self-host it from this repo.

</div>

## It's yours. And it's open.

Most chat bots are one agent shared across a workspace. daimon is
many-to-many: you deploy it once, on your own Anthropic API key, and any
number of Discord servers and Slack workspaces install it from that single
deployment. Every install is isolated: one tenant, its own data, scoped to
that server or workspace. Adding one takes about two minutes: invite the bot,
`@mention` it, and it sets itself up. From there, everyone there can just ask
it for things.

Built on [Anthropic Managed Agents](https://platform.claude.com/docs/en/managed-agents/quickstart)
by [PyMC Labs](https://www.pymc-labs.com), the team behind the PyMC project.

> **Status:** early. Self-hosting works and is documented below — expect
> rough edges and breaking changes while things settle. The hosted version at
> [daimon.decision.ai](https://daimon.decision.ai/) is the zero-setup path.

## It doesn't chat. It does the work.

ChatGPT analyzes your data for you, alone, in a tab. daimon does it with your
whole team, in the thread — and hands back a notebook anyone can run.

- `@daimon` in a channel starts (or continues) a threaded conversation with
  session continuity
- Everything happens in conversation — setup, scheduling, billing: `@mention`
  the bot and ask. Slash commands (`/agent-setup`, `/routines`, `/billing`,
  `/privacy`, `/help`) still exist if you prefer them; setup and routines
  require Discord's `Manage Server` permission
- Scheduled routines: recurring agent runs dispatched headlessly
- Slack adapter (early, not yet as battle-tested as Discord) with
  per-workspace OAuth install and opt-in per-user access
  ([`docs/slack.md`](docs/slack.md))
- CLI and MCP adapters sharing the same core turn pipeline
- Tenant isolation enforced at the database `tenant_id` layer, so one shared
  Anthropic key can safely power every guild

## What you can ask it

> "Here's last quarter's sales export — we changed pricing in week 6. Did it
> actually help?"

> "Is variant B actually better than A, or is that just noise?"

> "Forecast next month's signups, with uncertainty bands."

> "Every Monday at 9am, pull the weekend's numbers and post a summary here."

Answers come back in the thread: a fitted model, a chart, a plain-English
read on the uncertainty, and a runnable [marimo](https://marimo.io) notebook
that reproduces the analysis.

## How it works

```mermaid
flowchart LR
    a["your Discord server"] --> d
    b["another Discord server"] --> d
    c["a Slack workspace"] --> d
    d["daimon<br>one deployment, your Anthropic key"] --> e["Claude<br>(Anthropic Managed Agents)"]
```

You run one copy of daimon. Every community that installs it gets its own
agent with its own memory, and none of them can see each other's data. When
someone `@mention`s the bot, daimon hands the conversation to Claude and
posts the replies back into the thread.

<details>
<summary>Technical architecture</summary>

```mermaid
flowchart LR
    subgraph adapters
        direction TB
        Discord
        Slack
        CLI
        MCP
        Scheduler
    end
    adapters --> core["daimon core<br>turn pipeline"]
    core <--> ma["Anthropic Managed Agents<br>agents &middot; sessions &middot; skills"]
    core --> pg[("Postgres<br>tenants &middot; thread&harr;session map")]
```

A turn: the adapter derives the tenant from platform identity, core opens or
resumes a Managed Agents session, streams its events, and the adapter renders
deltas into the thread until the session goes idle.

- `daimon.core` owns schema, stores, and the turn pipeline, and imports no
  adapters. Each adapter owns one platform's I/O and auth, and adapters never
  import each other. `import-linter` enforces both rules in CI.
- Managed Agents holds the agents, environments, sessions, and skills
  themselves. Postgres holds only metadata about them: tenant identity,
  thread-to-session mappings, config, credentials, and billing.
- One Discord guild (or Slack workspace) is one tenant. Isolation lives at
  the database `tenant_id` layer, not the API-key boundary.

</details>

## Run it yourself

You need an Anthropic API key **in a workspace dedicated to this deployment**
(daimon manages the workspace's Managed Agents resources as its own, so
sharing the workspace with anything else causes collisions) and
[Docker](https://docs.docker.com/get-docker/).

### 1. Configure environment

```bash
cp .env.example .env
```

Open `.env`, then uncomment and fill in:

- `DAIMON_ANTHROPIC__API_KEY`: your Anthropic API key
- `DAIMON_MCP__JWT_SECRET`: any random string (e.g. `openssl rand -hex 32`)
- `DAIMON_MCP__PUBLIC_URL`: `http://localhost:8765/mcp` is fine for local use
- `POSTGRES_PASSWORD`: a strong, URL-safe value (avoid `@ : / % #`)

All four must be set before your first `docker compose` command:
`docker-compose.yml` interpolates them for every service with fail-fast
`${VAR:?...}` guards. You'll add the Discord bot token in step 2. `.env` is
gitignored, so secrets never get committed.

### 2. Create the Discord application

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, create a bot user and copy its token into `.env` as
   `DAIMON_DISCORD__BOT_TOKEN`.
3. Still under **Bot**, enable the **Message Content Intent**. It's a
   privileged intent, and without the portal toggle the bot can't read
   mentions.
4. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, then under **Bot Permissions** select at
   least `Send Messages`, `Send Messages in Threads`,
   `Create Public Threads`, `Manage Threads`, and `Read Message History`.
5. Open the generated URL in a browser and invite the bot to a test server
   you control.

### 3. Start the stack

```bash
docker compose up --build -d
```

One command brings up Postgres, runs migrations and seeds the default
agents, environments, and skills (the `init` service does both
automatically), then starts the `mcp`, `discord`, and `scheduler` services.

Once it settles, send a message that `@mention`s the bot. It replies in a
new thread, and that's a working deployment. If the bot stays silent, check
`docker compose logs discord` — an unset `DAIMON_DISCORD__BOT_TOKEN` is the
usual cause.

<details>
<summary>Prefer to run the processes by hand?</summary>

Requires [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras --all-packages
docker compose up -d postgres
export DAIMON_DATABASE_URL=postgresql+asyncpg://daimon:<your-POSTGRES_PASSWORD>@localhost:5432/daimon
uv run alembic upgrade head
uv run daimon defaults apply
uv run python -m daimon.adapters.discord
```

The `export` is required because the `alembic` CLI reads the shell
environment and does not auto-load `.env`.

</details>

## Run it on Slack too (optional)

Slack needs a publicly reachable `DAIMON_MCP__PUBLIC_URL` — the bot token is
issued by an OAuth install callback served by the `mcp` process, not read from
an env var, and Slack won't redirect to `localhost`.

1. Create the Slack app from
   [`docs/slack-app-manifest.yaml`](docs/slack-app-manifest.yaml) and follow the
   steps in its header comment. It fills in the scopes, slash commands, events,
   and Socket Mode toggles for you.
2. Put the resulting `DAIMON_SLACK__SIGNING_SECRET`, `DAIMON_SLACK__APP_TOKEN`,
   `DAIMON_SLACK__CLIENT_ID`, and `DAIMON_SLACK__CLIENT_SECRET` in `.env`, plus
   `DAIMON_CRYPTO__KEYS` (a Fernet key — the adapter refuses to start without
   one, since it stores workspace tokens encrypted).
3. `docker compose --profile slack up --build -d`
4. Open `https://<your-host>/oauth/slack/install` and install to a workspace.

[`docs/slack.md`](docs/slack.md) covers the trust model for per-user Slack
access, which operators should read before enabling it.

## Layout

- `packages/core/` — `daimon-core` library (MA client, stores, turn pipeline)
- `packages/adapters/cli/` — the `daimon` admin CLI
- `packages/adapters/discord/` — the Discord bot adapter
- `packages/adapters/mcp/` — the MCP server adapter
- `packages/adapters/slack/` — the Slack adapter (optional)
- `packages/adapters/scheduler/` — the routines scheduler adapter
- `packages/testing/` — shared test fixtures/harness
- `apps/notebook-host/` — standalone marimo notebook host service
- `defaults/` — YAML defaults seeded into Managed Agents + local DB
- `tests/` — cross-package integration tests

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev environment setup and the
quality gates every PR must keep green.

## Security

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability.

## License

[MIT](LICENSE)
