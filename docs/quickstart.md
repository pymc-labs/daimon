# Quickstart

Run Daimon on your own machine with Docker Compose and talk to it in Discord.
This takes about 15 minutes. For production, continue with
[self-hosting](self-hosting.md).

## What you need

- [Docker](https://docs.docker.com/get-docker/) 23 or later, with Compose
  (the image build needs BuildKit).
- An Anthropic API key from a workspace used only by this deployment. Daimon
  manages that workspace's Managed Agents resources as its own, so sharing it
  causes collisions.
- A Discord server you control.

No Anthropic account? See [Run on OpenAI instead](#run-on-openai-instead).

## 1. Configure the environment

```bash
git clone https://github.com/pymc-labs/daimon.git && cd daimon
cp .env.example .env
```

Open `.env`, then uncomment and fill in:

| Variable | Value |
| --- | --- |
| [`DAIMON_ANTHROPIC__API_KEY`](configuration.md#daimon_anthropic__api_key) | Your Anthropic API key. |
| [`DAIMON_MCP__JWT_SECRET`](configuration.md#daimon_mcp__jwt_secret) | Any random string, for example `openssl rand -hex 32`. |
| [`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url) | `http://localhost:8765/mcp` for local use. |
| `POSTGRES_PASSWORD` | A strong, URL-safe value. Avoid `@ : / % #`. |
| [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys) | A Fernet key. Without it, agent keys cannot be saved. |

Generate the Fernet key with OpenSSL:

```bash
openssl rand -base64 32 | tr '+/' '-_'
```

Back the key up separately from the database. Set `DAIMON_MCP__JWT_SECRET`,
`DAIMON_MCP__PUBLIC_URL` and `POSTGRES_PASSWORD` before the first
`docker compose` command: Compose refuses to start without them. `.env` is
gitignored. `.env.example` documents every other setting.

## 2. Create the Discord bot

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, create a bot user and copy its token into `.env` as
   `DAIMON_DISCORD__BOT_TOKEN`.
3. Still under **Bot**, enable the **Message Content Intent** and the
   **Server Members Intent**. Both are privileged: the first lets the bot read
   mentions, the second keeps web access in step with who is in the server.
   The bot cannot connect without the first. Without the second it connects,
   and web access goes by what each sign-in proves.
4. Under **OAuth2 > URL Generator**, select the `bot` and
   `applications.commands` scopes.
5. Under **Bot Permissions**, select `View Channels`, `Send Messages`,
   `Send Messages in Threads`, `Create Public Threads`, `Manage Threads`,
   `Read Message History`, `Embed Links` and `Manage Webhooks`. These eight
   give the permissions integer `326954470400`.
6. Open the generated URL and invite the bot to your server.

`Manage Webhooks` lets agents post under their own names. A channel override
can deny it, so also allow it under **Edit Channel > Permissions** for each
channel where agents answer. Without it, replies still work and the agent name
appears in bold on the first chunk.

## 3. Start the stack

```bash
docker compose up --build -d
```

This starts Postgres, runs migrations, seeds the default agents, environments
and skills, then starts the `mcp`, `discord` and `scheduler` services. The
seeding runs in the `init` service, which `up -d` does not show.

Check `init` once:

```bash
docker compose logs init
```

It says whether it seeded the Anthropic defaults or skipped them. Skipping
means `DAIMON_ANTHROPIC__API_KEY` is unset.

## 4. Say hello

`@mention` the bot in a channel. It replies in a new thread, runs the analysis
and posts the result there. Anyone in the server can follow up in the thread.

## If the bot stays silent

- Run `docker compose logs discord`. An unset `DAIMON_DISCORD__BOT_TOKEN` is
  the usual cause.
- Confirm the Message Content and Server Members intents are on and the bot
  can see the channel.
- Run `docker compose ps` and check that `mcp`, `discord` and `scheduler` are
  up. A service that exits at start usually names the missing variable in its
  log.
- Check that `.env` holds the Fernet key:
  `docker compose run --rm init "daimon crypto verify"`.

## Run on OpenAI instead

Leave `DAIMON_ANTHROPIC__API_KEY` unset. After step 3, provision an OpenAI
agent and route your channels to it:

```bash
docker compose run --rm init "daimon openai init --tenant-id <tenant-uuid> --project <openai-project-id> --model <openai-model-id>"
docker compose run --rm init "daimon agents backend set <agent-name-or-id> openai"
```

Your tenant UUID is the `id` field in the output of
`docker compose run --rm init "daimon tenants list --json"`. Before the first command,
add `DAIMON_CHANNEL_OPENAI_KEY=<key>` and
`DAIMON_TURN__PROVIDER_RUNTIME_FILE=/app/runtime/openai.json` to `.env`. After
both, run `docker compose up -d` so the services read the new values.
[Model providers](providers.md) covers routing, verification and what OpenAI
does not support.

## Next steps

- [Self-hosting](self-hosting.md): published images, several MCP instances and
  production settings.
- [Slack](slack.md), [Microsoft Teams](teams.md) and [GitHub](github.md):
  connect more platforms.
- [Operations](operations.md): upgrades and backups.
