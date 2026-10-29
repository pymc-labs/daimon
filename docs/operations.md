# Operations

Run a Daimon deployment day to day: upgrade it, back it up, deploy without
dropping turns, and read its logs, audit trail and operator tokens. For
installation, see [self-hosting](self-hosting.md).

## Upgrade an installation

Check out the new release, pull or build the image, apply migrations, then
restart the services. Read the notes below for the releases they apply to.

`daimon smoke` verifies an Anthropic deployment: it provisions a dedicated
smoke tenant, runs one billed turn and exits non-zero unless it was metered.

Every adapter and MCP process must run a timer-aware build before timers are
created. Apply migration `0029_feat084_timers` first, then update every
Discord, Slack and MCP process, and only then let agents create timers. An
older process fails to load a batch that contains a timer row. Downgrading
that migration deletes every timer row.

### Re-mint tokens without a registry entry

The MCP server admits only credentials with a live registry entry. Re-mint any
old CLI or MCP-client token that lacks a `jti` with `daimon mcp mint-token`,
and replace it in the clients that use it. Use the agent and operator mint
commands for those credential types. Treat command output as a credential.

Chat credentials renew on session reuse and on agent-chat or hub
continuation. A turn that started with an older credential can lose MCP access
if the verifier is upgraded before the processes that mint credentials. Use a
staged upgrade to keep such turns alive.

### Stage the MCP upgrade

1. Apply migrations. Deploy the updated MCP service first. For this rollout
   only, set
   [`DAIMON_MCP__ALLOW_LEGACY_TOKENS=true`](configuration.md#daimon_mcp__allow_legacy_tokens)
   and
   [`DAIMON_MCP__LEGACY_TOKEN_CUTOFF`](configuration.md#daimon_mcp__legacy_token_cutoff)
   to a timezone-aware UTC deadline about five hours ahead. Only external,
   non-internal legacy credentials qualify. Replace any external callers with
   registered credentials before this step.
2. Update every process that mints credentials: platform workers, the
   scheduler, any separately installed CLI and notebook services. Note when
   worker refresh finishes and when the new MCP revision finishes deploying.
3. From the later of those two times, wait the 45-minute turn ceiling plus
   one minute, plus the old MCP revision's request timeout. Finish the wait
   before the deadline.
4. Set `DAIMON_MCP__ALLOW_LEGACY_TOKENS=false` on every MCP replica. After
   this, or once the deadline passes, every unregistered credential is refused.

Configuration refuses naive or invalid dates, deadlines more than six hours
ahead, and enabling compatibility without a deadline. A retry keeps the
original deadline. An expired deadline lets the service boot but stays strict.
Rolling back to old minters after strict admission is unsupported.

### Recover an interrupted rollout

If the rollout stops after the MCP update but before worker refresh, the
deadline still closes admission and old workers lose MCP access. Alert on the
failed rollout and on authentication failures, then finish the worker refresh.
If the deadline has expired, repair the minters and leave admission strict.
Never reset the window to hide an interrupted upgrade.

A provider or database error during credential renewal stops a continued turn
from sending. Check those dependencies and retry.

### Remove Fleet storage

Migration `0141_remove_fleet` drops the Fleet worker and run tables and the
`fleet_grant` column on agent backend revisions. It destroys Fleet run history
and grants and cannot be reversed. Back up that history first, stop the old
application and its background processes, then apply it. Old builds still need
those tables.

## Deploy without dropping turns

When a worker stops, it refuses new mentions and waits up to 60 seconds for
turns in progress. After that it cancels what is left and allows five seconds
for cleanup. Cancelling ends only this process's delivery. The provider
session stays recoverable, not abandoned: the next process delivers the answer
if the session finished, or marks the card interrupted and stops the session.

In your Compose files, set `stop_grace_period: 90s` on Discord, Slack, Teams
and the scheduler. Recreate with a CLI timeout above that:

```bash
docker compose up -d --remove-orphans --timeout 95
```

### Overlap Discord during a deploy

Stopping the Discord container first leaves the gateway closed for about 14
seconds, and a button or slash command used in that gap gets no response. To
avoid the gap, start the new container beside the old one:

```bash
docker compose up -d --no-deps --no-recreate --scale discord=2 discord
```

Wait until the new container answers its liveness port, then stop and remove
the old one with a 95-second timeout, and recreate the other services. Compose
numbers the new container one higher than the highest existing one, so do not
assume the name `daimon-discord-1`.

Only one process holds the Discord gateway. The new process starts fully, then
waits on a database lease. The old process keeps handling buttons for its
in-flight turns while it drains, then releases the lease, and the new process
connects at once. If the old process dies, the new one waits until its
heartbeat expires. Both processes must share the same database.

## Back up and recover

Use managed Postgres with point-in-time recovery in production. Set a recovery
point objective (acceptable data loss) and recovery time objective (acceptable
outage), then schedule and rehearse backups to meet them. Keep encrypted,
off-host copies, and record the application release and migration revision
with each recovery set. A dump on the database's own disk does not survive
disk loss.

### What to back up

| State | Preserve | Recovery |
| --- | --- | --- |
| Postgres | The whole database, including the Alembic revision. | Logical snapshot from the backup service below, or provider point-in-time recovery. Restore into a fresh database on the same major Postgres version and application release. Roles and grants are excluded. |
| Keys and configuration | Every active and historical `DAIMON_CRYPTO__KEYS` entry, signing secrets, OAuth and API credentials, `.env`, Compose overrides. | Keep in a separate encrypted secret store. A lost key makes its ciphertext unrecoverable. Restore keys before starting services. |
| Report and notebook hosts | Their data directories. | Stop writers, copy the directories with permissions, restore to the same paths. The main Compose file does not mount these hosts. |
| Media and artifact stores | Media directories and any artifact bucket. | Snapshot media. Enable bucket versioning independent of link expiry. Reissue expired links. |
| Managed Agents objects | Agents, environments, custom skill versions, memory contents. | `daimon backup platform-export`. Database IDs stay valid only in the original, intact workspace. |
| Session transcripts and disks | Held by the provider. | Not covered. Keep important deliverables in backed-up stores and start fresh sessions after a platform loss. |

Quiesce the adapters, scheduler, webhook ingestion and file hosts before you
capture a coordinated set, and stop active turns. The backup service saves
only Postgres.

### Create a backup

```bash
mkdir -p backups
chmod 700 backups
docker compose --profile backup run --rm backup backup /backups/<timestamp>
uv run daimon backup platform-export backups/platform-<timestamp>.zip
```

Use a new timestamp each time; existing destinations are never overwritten.
Set `DAIMON_BACKUP_DIR` for another host directory. No backup daemon runs, so
schedule the one-shot command yourself. A complete backup directory holds
`database.dump` and `SHA256SUMS`. A failed run can leave an incomplete
directory, so never use one without its checksum file and a restore test. The
checksum detects corruption, not tampering.

The platform export reads the workspace behind `DAIMON_ANTHROPIC__API_KEY` and
runs no model turns. Its private ZIP holds definitions with original IDs,
skill ZIPs and memory contents for every tenant in the workspace. It can
contain secret-bearing definitions, so restrict it like a database dump. It is
not replayable by `daimon defaults apply`. Use it to rebuild tenant objects by
hand and remap IDs. It excludes vault secrets, historic versions and
transcripts.

### Restore a database

Keep adapters and the scheduler stopped. Create an empty database, then restore
with the Postgres 18 client image or matching local tools. Pass `PGHOST`,
`PGPORT`, `PGUSER`, `PGPASSWORD` and `PGDATABASE` through your secret store,
not as command arguments.

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" daimon_recovery'
docker compose --profile backup run --rm \
  -e PGDATABASE=daimon_recovery -e DAIMON_RESTORE_DATABASE=daimon_recovery \
  backup restore /backups/<timestamp>
```

With local client tools instead:

```bash
DAIMON_RESTORE_DATABASE="$PGDATABASE" sh scripts/backup/postgres.sh restore backups/<timestamp>
```

The command checks the checksum, requires the target name, refuses any database
that already has tables, and restores in one transaction. It never drops data.
Restore only trusted archives, because dumps contain executable SQL. On
failure, find the cause before retrying.

Then restore file stores and the original keys, point the application at the
recovered database and start the matching release. Check `alembic_version`,
row counts, and a sample of tenant, credential, routine and usage records.
Confirm decryption and platform IDs before enabling traffic. If the original
Managed Agents workspace survives, keep its mappings. If it was lost, do not
resume routines until you have rebuilt the objects and remapped IDs.

Run `sh scripts/backup/drill.sh` to rehearse the commands against a disposable
Postgres container. It does not prove your own recovery time or that your
external stores and keys are complete, so also rehearse a real recovery set.

### Encrypt agent environment values

Agent environment values are encrypted with `DAIMON_CRYPTO__KEYS`. Without
keys the stack still starts and serves existing values, logs
`agent_env.encryption_keys_missing`, and refuses every agent key write.

```bash
docker compose run --rm init "daimon crypto verify"
docker compose run --rm init "daimon crypto encrypt-plaintext"
```

`verify` exits non-zero if keys are missing, any key is stored in plaintext,
or a stored key cannot be decrypted. It lists counts per tenant. After you add
keys to a deployment that stored values without them, restart every process
(MCP, Discord, Slack, Teams, scheduler), run `encrypt-plaintext`, then run
`verify` again. Setting keys alone does not rewrite existing values.

To rotate keys, put the new key first and keep the old ones. Retire an old key
only after `verify` shows nothing depends on it. Old backups can still hold
plaintext, so apply secret-retention controls to them.

Stop old application readers and writers before you upgrade through
`0028_agent_env_encryption`, because old readers cannot decode encrypted
values. The migration waits five seconds for a lock, so retry in a maintenance
window if a busy database fails it. To roll back, stop all processes and run
`alembic downgrade -1` with the original keys available.

## Read the logs

Discord, Slack, Teams, the scheduler and MCP write one JSON line per record,
including records from libraries. `event` is the message. Each line has
`logger`, `level`, `severity`, `timestamp` and, for errors, a structured
`exception` list. After SIGTERM or SIGINT, lines also carry
`shutting_down: true`. Redaction applies to every line.

These events carry identifiers, counts and durations, never message text:

| `event` | Level | Fields |
| --- | --- | --- |
| `runtime.started` | info | `process`, `release`; once per process start |
| `runtime.health` | info | `process` and the window's measurements, every [`DAIMON_OBSERVABILITY__HEALTH_INTERVAL_S`](configuration.md#daimon_observability__health_interval_s) seconds |
| `scheduler.tick_completed` | info | `duration_ms`; at most once a minute |
| `turn.user_failure` | warning | `platform`, `reason`, `error_class`, `origin`, `backend`, `release`, `turn_id`, `tenant_id`; once for a turn that ended without an answer |
| `turn.would_stall` | warning | `step_s`, `silent_s`, `frame_silent_s`, `tool_open`, `last_status`, `session_id`; once per step (5, 10, 15, 30 minutes) a turn's provider sends nothing. Measurement only: it never ends a turn. `tool_open` false does not rule out an OpenAI provider-side tool, whose previews Daimon never sees; `last_status` is the last status read, not the current one |

`turn.user_failure` covers upstream errors, lost connections, the turn
ceiling, failed recovery and queue-full shedding. Its `backend` names the
backend and profile, such as `gemini/gemini.inline_reuse`, or `unknown`
before one is chosen, so alerts can split by backend. Lines logged while a
chat or routine turn's reply is driven and finalized also carry `backend`,
`profile` and `model`. It is not logged for
completed turns, stops the user asked for, model refusals, admission refusals
such as an empty balance, or missing setup. An uncaught exception is logged
as `process.uncaught_exception` at critical.

`release` is the commit the image was built from, set by the `DAIMON_RELEASE`
build argument. An image built without it reports the package version.

## Audit security events

Each authenticated MCP `tools/call` and `tools/list` request writes one
tenant-scoped audit row: tenant, account, platform user and agent identifiers,
tool name, time, outcome and a reason code. A registered token adds its kind
and `jti`. Arguments, messages, credentials, response bodies and error text
are never stored. Refusals from access rules record `authz:<reason>`.

```bash
daimon audit list <tenant-uuid> --since 2026-09-28T00:00:00Z --json
daimon audit prune <tenant-uuid>
```

`list` accepts `--limit` (1 to 1000), `--offset` and
`--account <account-uuid>`, which includes one person's rows in a privacy
export. Repeat per tenant for a person in several workspaces. It uses local
database access and is not an MCP tool.

Retention is 90 days
([`DAIMON_SECURITY_AUDIT_RETENTION_DAYS`](configuration.md#daimon_security_audit_retention_days);
`0` keeps rows indefinitely). Schedule `daimon audit prune` for each tenant, for example
daily, and include it and your backup expiry in your retention policy. Privacy
erasure clears a person's identifiers from audit rows, and deleting a tenant
removes its rows. Database triggers block ordinary updates and deletes. They
guard against application mistakes, not against a database administrator.

## Manage operator tokens

An operator token lets an external integration call a few MCP tools over
`/mcp` for one server admin. Mint one with one or more `--scope` flags:

```bash
daimon mcp mint-operator-token --tenant <tenant-uuid> --account <admin-account-or-user-id> --scope tenant:read [--ttl-days 30] [--label <note>]
daimon mcp list-tokens [--tenant <tenant-uuid>] [--kind operator] [--all]
daimon mcp revoke-token <jti>
daimon mcp set-token-scopes --jti <jti> --scope <scope-to-keep>
```

The default lifetime is 30 days and the maximum 90. With `promo:create`, an
optional `--max-issued-usd` sets a ceiling in whole cents. Server admins can
also mint, list and revoke tenant-scope tokens from **Who answers where** in
the setup panels. Those tokens last 30 days, are shown once and never carry
`promo:create`. `set-token-scopes` only removes scopes. To add one, mint a new
token.

| Scope | Tools |
| --- | --- |
| `tenant:read` | `get_tenant_summary`, `list_channel_budgets`, `get_channel_budget`, `list_channel_skills`, `list_agent_tool_rules`, `list_environments` |
| `channels:write` | `set_channel_budget`, `clear_channel_budget`, `set_agent_default` and `clear_agent_default` (channel defaults only), `set_channel_rule`, `set_agent_rule`, `set_channel_environment` and `clear_channel_environment` (channels only), `add_channel_skill`, `remove_channel_skill`, `set_agent_tool_rule`, `clear_agent_tool_rule` |
| `agents:archive` | `archive_channel_copy` |
| `promo:redeem` | `redeem_promo_code` |
| `promo:create` | `create_promo_code`, `list_promo_codes`, `revoke_promo_code` (deployment-wide) |

On each request the token must be unrevoked and unexpired, and its account must
still be a server admin. The stored admin role updates on that account's next
platform turn, so a demoted admin's token keeps working until then or until it
expires. `revoke-token` stops it on its next request. An operator token cannot
open a billing checkout. It is rate limited by
[`DAIMON_MCP__OPERATOR_CALLS_PER_MINUTE`](configuration.md#daimon_mcp__operator_calls_per_minute).
Tool details are in [mcp-tools](mcp-tools.md).

`daimon channels list <platform> <workspace-id> [--json]` prints the same
channel summary as `get_tenant_summary`, plus each channel's readers and
writers.

## Meter self-hosted harness usage

Self-hosted Azure metering needs deployment-to-model mappings and verified
rates in `DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS` and
`DAIMON_BILLING__SELFHOSTED__MODELS`. See [billing](billing.md).
