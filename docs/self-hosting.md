# Self-hosting

Run Daimon in production with Docker Compose. Read this after the
[quickstart](quickstart.md) once the stack answers in Discord.

## Check the production settings

Set these before the first `docker compose` command: Compose refuses to start
without them.

- [`DAIMON_MCP__JWT_SECRET`](configuration.md#daimon_mcp__jwt_secret)
- [`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url), an
  address that Slack, GitHub and browsers can reach, not `localhost`
- `POSTGRES_PASSWORD`

Then check these:

- **Encryption keys.** [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys)
  encrypts agent keys, workspace tokens and MCP tokens. During rotation, list
  keys newest first, comma-separated (`NEWKEY,OLDKEY`) or as a JSON list.
  Confirm the setup with `docker compose run --rm init "daimon crypto verify"`.
- **Database connections.** Each process opens up to
  [`DAIMON_DATABASE__POOL_SIZE`](configuration.md#daimon_database__pool_size)
  plus `DAIMON_DATABASE__MAX_OVERFLOW` connections. Count every worker and MCP
  instance against the Postgres limit.
- **Operator alerts.** Set
  [`DAIMON_OPS__ALERT_WEBHOOK_URL`](configuration.md#daimon_ops__alert_webhook_url)
  to a private Discord webhook for alerts about installs, Stripe top-ups and
  Anthropic limits. Unset, no alerts are sent.
- **Agent names and avatars.** Off by default. See
  [agent identity](agent-identity.md) before enabling
  [`DAIMON_AGENT_IDENTITY__ENABLED`](configuration.md#daimon_agent_identity__enabled).

Postgres backups and upgrades are in [operations](operations.md).

## Run a published image

Every tagged release is published as `ghcr.io/pymc-labs/daimon`. All seven
application services (`init`, `mcp`, `discord`, `slack`, `teams`, `scheduler`,
`web`) run that one image. Set the `image:` line for each in
`docker-compose.override.yml`, which Compose reads automatically:

```yaml
services:
  init:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  mcp:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  discord:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  slack:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  teams:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  scheduler:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  web:
    image: ghcr.io/pymc-labs/daimon:0.2.0
```

Then pull and start:

```bash
docker compose pull && docker compose up -d
```

Drop `--build` from every later `up` command, including the Slack and Teams
profile commands. It rebuilds from your working tree under the same tag and
discards the pulled image.

The image ships code only. `init` seeds `defaults/` from your checkout, so
check out the matching tag first (`git checkout v0.2.0`). To upgrade, check out
the next tag, edit the override and run the two commands again.

## Start Slack and Teams

Slack and Teams run as Compose profiles. Setup is on their own pages:

```bash
docker compose --profile slack up -d
docker compose --profile teams up -d
```

Add `--build` to either command only if you build from source. See
[Slack](slack.md) and [Microsoft Teams](teams.md).

## Run the processes by hand

Use this to run without Docker for the application processes. It needs
[`uv`](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras --all-packages
docker compose up -d postgres
export DAIMON_DATABASE__URL=postgresql+asyncpg://daimon:<POSTGRES_PASSWORD>@localhost:5432/daimon
uv run alembic upgrade head
uv run daimon defaults apply
uv run python -m daimon.adapters.discord
uv run uvicorn daimon.adapters.mcp.server:create_mcp_app --factory --port 8765
uv run daimon-scheduler
```

Run each of the last three in its own terminal. The `export` is required: the
`alembic` CLI reads the shell environment and does not load `.env`. The MCP
server and the scheduler must run too. Start Slack and Teams the same way,
with `python -m daimon.adapters.slack` and `python -m daimon.adapters.teams`.

## Run several MCP instances

The MCP endpoint is stateless. It issues no session ID, and every request
carries its own token. Replicas need no sticky sessions behind a load
balancer, and a redeploy does not strand connected clients. The hourly limits
on notebook publishes and bundle uploads are kept in memory, so each replica
counts its own.

## Store charts and artifacts

Hosted MCP clients receive bounded chart images directly from the Anthropic
Files API. This needs no bucket and is on by default.

To add short-lived presigned chart links, set
`DAIMON_ARTIFACTS__ENDPOINT_URL`, `DAIMON_ARTIFACTS__BUCKET`,
`DAIMON_ARTIFACTS__ACCESS_KEY_ID` and `DAIMON_ARTIFACTS__SECRET_ACCESS_KEY`
(see [configuration](configuration.md#daimon_artifacts__endpoint_url)). Charts
then go to a private artifact store, and `deliver_turn_charts` returns links.
Clients that run `start_turn` themselves can call `deliver_turn_charts` after
`get_my_session` reports `idle` or `terminated`.

- The default `s3` backend needs SigV4 and virtual-hosted-style addressing.
  Path-style-only endpoints, including default MinIO setups, are not
  supported.
- Objects stay private. Daimon never sets a public-read ACL.
- Link expiry does not delete objects. Set a bucket lifecycle rule for your
  retention period.

### Use Azure Blob Storage

Set this on the MCP service:

```bash
DAIMON_ARTIFACTS__BACKEND=azure_blob
DAIMON_ARTIFACTS__ENDPOINT_URL=https://<account>.blob.core.windows.net
DAIMON_ARTIFACTS__BUCKET=charts
DAIMON_ARTIFACTS__AZURE_CLIENT_ID=<user-assigned-managed-identity-client-id>
```

1. Create the private container named by `BUCKET` and disable anonymous blob
   access on the storage account.
2. Give the managed identity **Storage Blob Delegator** at storage-account
   scope. This lets it create user-delegation keys.
3. Give it **Storage Blob Data Contributor** on the charts container, or a
   custom container-scoped role with read and write but no delete.

`ENDPOINT_URL` must be the bare account URL, with no container path, query or
credentials. No account key or S3 credential is needed, and there is no
fallback to other Azure credentials. Links are HTTPS-only, read-only and last
`DAIMON_ARTIFACTS__URL_TTL_SECONDS` (default 600, maximum 86400). Expiry does
not delete the blob, so set an Azure Storage lifecycle policy. Downloads are
forced as attachments, and SVGs are served as `application/octet-stream`.

For local development with Azurite only, set
`DAIMON_ARTIFACTS__AZURE_CONNECTION_STRING` with a loopback `BlobEndpoint`
matching `ENDPOINT_URL`, such as `http://127.0.0.1:10000/devstoreaccount1`.
Never set it in Azure.

Notebook and report hosts keep their own data directories. These settings
select only the MCP chart store, which has no listing or deletion API.
`.env.example` lists the optional region, URL lifetime and image-embedding
settings.

## Deploy on Azure

The repository holds Bicep templates in `deploy/azure/` and an `azure.yaml`
for the Azure Developer CLI. They provision Container Apps for Discord, Teams,
MCP and the scheduler, plus a migration job, PostgreSQL, Key Vault, Blob
storage, a container registry and optionally an Azure OpenAI deployment. The
templates are validated offline, and live deployments are not verified end to
end.

Deployment runs in stages: foundation, image publication and init, then the
serving apps. Endpoints are public, and private networking is not
implemented. Parameters, commands, roles and limitations are in the
[Azure deployment guide](https://github.com/pymc-labs/daimon/blob/main/deploy/azure/README.md).

## Connect more

- [Model providers](providers.md): OpenAI, Gemini and the self-hosted harness.
- [Slack](slack.md), [Microsoft Teams](teams.md), [GitHub](github.md).
- [MCP servers](mcp-servers.md) and the [Claude Code plugin](plugin.md).
