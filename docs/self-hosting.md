# Self-hosting daimon

This guide expands the README quickstart. It covers the full Docker Compose
setup, running the processes by hand, Slack, the Claude Code login mounts,
chart storage and connecting MCP servers.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose.
- An Anthropic API key **in a workspace dedicated to this deployment**.
  daimon manages the workspace's Managed Agents resources as its own, so
  sharing the workspace with anything else causes collisions.

## 1. Configure the environment

```bash
cp .env.example .env
```

Open `.env`, then uncomment and fill in:

- `DAIMON_ANTHROPIC__API_KEY`: your Anthropic API key.
- `DAIMON_MCP__JWT_SECRET`: any random string, e.g. `openssl rand -hex 32`.
- `DAIMON_MCP__PUBLIC_URL`: `http://localhost:8765/mcp` is fine for local use.
- `POSTGRES_PASSWORD`: a strong, URL-safe value (avoid `@ : / % #`).

All four must be set before the first `docker compose` command:
`docker-compose.yml` interpolates them for every service with fail-fast
`${VAR:?...}` guards. `.env` is gitignored, so secrets never get committed.
`.env.example` documents every other setting.

## 2. Create the Discord application

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, create a bot user and copy its token into `.env` as
   `DAIMON_DISCORD__BOT_TOKEN`.
3. Still under **Bot**, enable the **Message Content Intent**. It is a
   privileged intent; without it the bot cannot read mentions.
4. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, then under **Bot Permissions** select at
   least `Send Messages`, `Send Messages in Threads`,
   `Create Public Threads`, `Manage Threads` and `Read Message History`.
5. Open the generated URL and invite the bot to a test server you control.

Setup and routines commands require Discord's `Manage Server` permission.

## 3. Start the stack

```bash
docker compose up --build -d
```

This brings up Postgres, runs migrations and seeds the default agents,
environments and skills (the `init` service does both), then starts the
`mcp`, `discord` and `scheduler` services.

Once it settles, send a message that `@mention`s the bot. It replies in a
new thread. If the bot stays silent, check `docker compose logs discord`; an
unset `DAIMON_DISCORD__BOT_TOKEN` is the usual cause.

### Running the processes by hand

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
environment and does not load `.env`.

## Slack (optional)

Slack needs a publicly reachable `DAIMON_MCP__PUBLIC_URL`. The bot token is
issued by an OAuth install callback served by the `mcp` process, not read
from an env var, and Slack will not redirect to `localhost`.

1. Create the Slack app from
   [`slack-app-manifest.yaml`](slack-app-manifest.yaml) and follow the steps
   in its header comment. It fills in the scopes, slash commands, events and
   Socket Mode toggles.
2. Put the resulting `DAIMON_SLACK__SIGNING_SECRET`, `DAIMON_SLACK__APP_TOKEN`,
   `DAIMON_SLACK__CLIENT_ID` and `DAIMON_SLACK__CLIENT_SECRET` in `.env`, plus
   `DAIMON_CRYPTO__KEYS` (a Fernet key; the adapter stores workspace tokens
   encrypted and refuses to start without one).
3. `docker compose --profile slack up --build -d`
4. Open `https://<your-host>/oauth/slack/install` and install to a workspace.

[`slack.md`](slack.md) covers the trust model for per-user Slack access.
Read it before enabling that feature.

## Claude Code login mounts

Coding-agent clients such as Claude Code connect through the plugin in
[`plugin/`](https://github.com/pymc-labs/daimon/blob/main/plugin/README.md) instead of a per-agent token. It logs in via
Slack or Discord OAuth and reaches every daimon install the logged-in person
belongs to.

Each platform's mount needs its own OAuth app, plus `DAIMON_HUB__*`,
`DAIMON_CRYPTO__KEYS` (login state is encrypted at rest) and
`DAIMON_MCP__PUBLIC_URL` (the mounts derive their public base URL from it).
Register these redirect URIs on the OAuth apps, where `{origin}` is
`DAIMON_MCP__PUBLIC_URL` without the trailing `/mcp`:

- Slack: `{origin}/slack/auth/callback`
- Discord: `{origin}/discord/auth/callback`

The Discord app requests the `identify` and `guilds` scopes. The Slack app
requests user scopes (`users:read`, `channels:history`, `groups:history`,
`channels:read`, `groups:read`, `im:history`, `mpim:history`, `im:read`,
`mpim:read`, `search:read`) so a daimon reads Slack as the person asking and
never sees a channel they cannot.

A login reaches only workspaces where daimon is installed and ready, checked
on every call. Membership is re-read when the login token is issued or
refreshed: a Slack token stops working the moment its user leaves the
workspace, while someone removed from a Discord server keeps that server's
daimons until their Discord token expires.
`DAIMON_HUB__ALLOWED_CLIENT_REDIRECT_URIS` limits which clients may complete
a login; the default covers coding agents on loopback and claude.ai.

## Chart delivery and artifact storage

Hosted MCP clients receive bounded chart images directly from the Anthropic
Files API. This embed-only path is enabled by default and needs no bucket.

Clients that orchestrate `start_turn`, `get_my_session` and `list_events`
themselves can call `deliver_turn_charts(handle)` after `get_my_session`
reports `idle` or `terminated` to receive the same chart payload. Calls made
while a turn is running or rescheduling are refused.

To add short-lived presigned chart links, configure
`DAIMON_ARTIFACTS__ENDPOINT_URL`, `DAIMON_ARTIFACTS__BUCKET`,
`DAIMON_ARTIFACTS__ACCESS_KEY_ID` and `DAIMON_ARTIFACTS__SECRET_ACCESS_KEY`.
When URL delivery is configured, `deliver_turn_charts` writes the chart to
the private artifact store.

- The storage boundary uses the vendor-neutral S3 API with SigV4 and
  virtual-hosted-style addressing. Confirm that contract with your provider.
  Path-style-only endpoints, including default MinIO setups, are not
  supported.
- Objects remain private. daimon never applies a public-read ACL.
- Presigned URL expiry does not delete stored objects. Configure a bucket
  lifecycle rule for the retention period your deployment requires.

See `.env.example` for the optional region, URL lifetime and image-embedding
controls.

## Connecting Notion, Linear and other MCP servers

Ask the agent to connect a server and it posts a card only you can open.

- A server that takes a bearer token (Linear, GitHub) gets a private token
  form. The token is checked against the server before it is stored and is
  shared by everyone who talks to that agent.
- A server that signs people in through a browser (Notion, Slack, Atlassian)
  gets a sign-in link. daimon registers itself as an OAuth client, you
  approve in the browser, and the grant lands in your own vault, refreshed
  by Anthropic. Each person connects their own account.

The sign-in routes live at `{origin}/oauth/mcp/start` and
`{origin}/oauth/mcp/callback`, so `DAIMON_MCP__PUBLIC_URL` must be reachable
from a browser and `DAIMON_CRYPTO__KEYS` must be set. If one connection
fails, the agent still answers and names the server it could not use under
the reply. Ask it to disconnect the server or connect it again.
