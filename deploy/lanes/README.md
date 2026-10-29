# Self-host with Compose

`compose.lane.yml` runs Postgres, migrations and defaults, the MCP server, the Discord bot, and the scheduler. The Slack adapter is optional. The file publishes no ports; connect the MCP service to your own reverse proxy or add a port mapping in a local Compose override.

Build an image from the repository root:

```sh
docker build -t daimon:local .
```

Create `deploy/lanes/.env` with values for your deployment. The Compose file requires these settings:

```dotenv
DAIMON_LANE_IMAGE=daimon:local
DAIMON_LANE_SHA=local
POSTGRES_PASSWORD=replace-with-a-database-password
DAIMON_ANTHROPIC__API_KEY=replace-with-your-api-key
DAIMON_MCP__JWT_SECRET=replace-with-a-random-secret
DAIMON_MCP__PUBLIC_URL=https://your-domain.example/mcp
DAIMON_DISCORD__BOT_TOKEN=replace-with-your-bot-token
```

Keep `.env` private. See [the environment reference](../../.env.example) for other settings. Then start the services:

```sh
cd deploy/lanes
docker compose --env-file .env -f compose.lane.yml up -d
```

To run Slack, add its credentials from `.env.example` and include `--profile slack` before `up`. For a fuller installation guide, see [self-hosting](../../docs/self-hosting.md).
