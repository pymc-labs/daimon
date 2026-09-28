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

### Running a published image instead of building

Every tagged release is published to the GitHub Container Registry as
`ghcr.io/pymc-labs/daimon`, so a host that should not compile anything can run
a release straight from the registry. All five application services run the
same image and differ only in the command Compose gives them, so one `image:`
line per service is the whole change. Put it in `docker-compose.override.yml`,
which Compose reads on top of `docker-compose.yml` automatically:

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
  scheduler:
    image: ghcr.io/pymc-labs/daimon:0.2.0
```

Then `docker compose pull && docker compose up -d`. Drop the `--build` flag
from step 3 and from the Slack profile command: it rebuilds from the working
tree under the same tag and throws the pulled image away. The image ships
only code; `init` still seeds `defaults/` from your checkout, so check out the
matching tag first (`git checkout v0.2.0`). Upgrading is checking out the
next tag, editing the override and running those two commands again.

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

## Backup and disaster recovery

Use managed Postgres with point-in-time recovery for production. Set a recovery
point objective (maximum acceptable data loss) and a recovery time objective
(maximum outage), then schedule and rehearse backups to meet them. A dump on the
same disk as the database does not protect against disk loss. Keep encrypted,
off-host copies with retention and access controls, and retain the application
release/commit and migration revision alongside each recovery set.

### Backup contract

| State class | What to preserve | Recovery contract |
| --- | --- | --- |
| Postgres | Whole database: tenants, encrypted credentials, mappings, routines, usage, balances, and Alembic revision | Consistent logical snapshot via the optional service below, or provider PITR. Restore into a fresh DB with the same major Postgres version and matching application release. Roles/ownership/grants are intentionally excluded; the restore user owns the objects. |
| Encryption and configuration | All active **and historical** `DAIMON_CRYPTO__KEYS`, signing secrets, OAuth/API credentials, `.env`, Compose overrides, defaults and pinned code | Keep in a separate encrypted secret-manager backup. Losing a decryption key makes its stored ciphertext unrecoverable. Restore keys before starting services; do not generate replacement keys for existing ciphertext. |
| Report and notebook hosts | Their configured persistent data directories/volumes, including published files, source bundles and metadata | Stop writers, snapshot/copy the entire directories with permissions, then restore to the same mount paths. The main Compose file does not mount these optional hosts: inventory their actual deployments. |
| Media and artifact stores | Media file-store data and any configured S3-compatible artifact bucket | Snapshot local media directories; enable bucket versioning/backup independently of URL expiry and lifecycle deletion. Restore keys/paths unchanged; expired presigned links must be reissued. |
| Managed Agents objects | Current agents/environments, every downloadable custom skill version, current memory-store contents | `daimon backup platform-export` creates a workspace-wide operator archive. Existing DB mappings remain valid only in the original intact MA workspace. Loss of that workspace requires manual recreation and ID remapping. |
| MA transcripts and session disks | Platform-retained session events, files and ephemeral sandboxes | **Not covered** by the object export or database dump. Do not promise recovery of ongoing sessions. Retain important deliverables in backed-up stores; start fresh sessions after platform loss. |

Quiesce adapters, scheduler, webhook ingestion and optional file hosts before
capturing a coordinated recovery set. A Postgres dump is internally consistent,
but it cannot make file stores and MA atomic with the database. Stop active MA
turns and edits too. Record capture times and any changes allowed during capture.
The service below only backs up Postgres, not the other rows of this table.

### Create backups

With the normal Compose environment configured:

```bash
mkdir -p backups
chmod 700 backups
docker compose --profile backup run --rm backup backup /backups/2026-09-28T220000Z
uv run daimon backup platform-export backups/platform-2026-09-28T220000Z.zip
```

Use a unique timestamp each time; existing destinations are never overwritten.
`DAIMON_BACKUP_DIR` can select a different host directory. Schedule the one-shot
Compose command with your host scheduler; no backup daemon runs by default.
A completed database directory contains `database.dump` and `SHA256SUMS`.
A failed backup may leave an incomplete directory: never treat it as usable
without its checksum file and a successful restore drill. Encrypt and copy the
recovery set off-host; the checksum detects corruption, not malicious tampering.

The platform command only reads the dedicated MA workspace configured by
`DAIMON_ANTHROPIC__API_KEY`. It does not run model turns. Its private (0600) ZIP
contains JSON definitions with original IDs and metadata, nested skill ZIPs,
full memory contents, and a versioned manifest with per-entry SHA-256 hashes.
It includes all tenants in that workspace and can contain private data and
secret-bearing definitions: restrict access like a database dump. Upstream
failures or a full, potentially truncated skills page abort the export without
publishing a partial ZIP. Remote names and
memory paths remain data, never filesystem paths.

This first version is **not replayable by `defaults apply`**. That importer owns
deployment defaults, not arbitrary tenant objects or memory contents. Retain
original `defaults/` and skill sources for `daimon defaults apply`; use exported
JSON/skill ZIPs for manual tenant-object reconstruction, restore memory paths
and contents through MA, then reconcile database IDs before enabling traffic.
Vault secrets, historic agent/environment/memory versions, and transcripts are
excluded. This archive is evidence and recovery material, not a full MA clone.

### Restore a database

Keep adapters and scheduler stopped. Provision an **empty** database first and
restore with the Postgres 18 client image (or matching local client tools).
Set `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD` and `PGDATABASE` through your secret
manager/environment, never put passwords in command arguments. With Compose,
for a new database named `daimon_recovery` on its Postgres service:

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" daimon_recovery'
docker compose --profile backup run --rm \
  -e PGDATABASE=daimon_recovery -e DAIMON_RESTORE_DATABASE=daimon_recovery \
  backup restore /backups/2026-09-28T220000Z
```

For local client tools the equivalent command is:

```bash
DAIMON_RESTORE_DATABASE="$PGDATABASE" sh scripts/backup/postgres.sh restore backups/2026-09-28T220000Z
```

The command validates the checksum, requires explicit target-name confirmation,
refuses a database containing relations, and runs `pg_restore` in one transaction
with errors fatal. It never drops existing data. Restore only trusted archives:
Postgres dumps contain executable SQL. Use a dedicated empty target with no
concurrent writers. On failure, investigate before retrying; do not bypass the
empty-database guard to overwrite a running deployment.

Restore file stores and the original keys, point application configuration at
the recovered database, and start the matching application release first.
Inspect `alembic_version`, row counts and representative tenant/credential,
routine, usage and published-file records. Verify decryption and platform IDs
before enabling traffic or scheduling. Apply upgrades only after this check.
If the original MA workspace survives, retain its mappings; if it was lost, do
not resume routines until manual object reconstruction and ID remapping finish.

### Local restore drill

```bash
sh scripts/backup/drill.sh
```

This runs the actual backup/restore commands against a disposable,
network-isolated Postgres container and removes only that container on exit.
It verifies rows, a migration marker and an identity sequence, plus rejection of
nonempty targets, missing confirmation and corrupt dumps. It uses no deployment
credentials. Also rehearse your real recovery set in an isolated environment:
this small drill does not establish your production recovery time or prove that
external stores, keys and MA mappings are complete.
