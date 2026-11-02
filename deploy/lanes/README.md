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

## Optional profiles

Three more profiles are off by default:

| Profile | Services | Use |
| --- | --- | --- |
| `preview-sidecar` | `sidecar`, `sidecar-proxy`, `watch-executor` | Signed page-event and polled watches |
| `preview-parity` | `preview-parity-mcp` | A synthetic MCP server for approval tests |
| `preview-notebook` | `preview-notebook` | A standalone notebook host |

The sidecar needs its own database login granted only `daimon_sidecar`, set in `DAIMON_SIDECAR__DATABASE_URL`, and signing keys in `DAIMON_SIDECAR__SIGNING_KEYS`. Set `DAIMON_SIDECAR_PUBLIC_HOST` to a separate hostname; only the proxy publishes TLS. Watches stay off until both `DAIMON_WATCHES__ENABLED` and the database `global` gate are on. See [watches](../../docs/routines.md) for owner tools and limits.

The notebook host needs `DAIMON_NOTEBOOK_IMAGE`, an admin secret of at least 32 characters and a non-empty allowed-origins list.

## Migrations

Run one migrator at a time per database. Alembic takes no advisory lock, so a second concurrent `alembic upgrade head` can fail with a DDL conflict. Let the first finish, then retry. The `init` service runs one upgrade before the other services start.
