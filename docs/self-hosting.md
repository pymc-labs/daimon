# Self-hosting Daimon

This guide expands the README quickstart. It covers the full Docker Compose
setup, running the processes by hand, Slack, Microsoft Teams, the Claude Code
login mounts, chart storage and connecting MCP servers.

## Azure infrastructure

The repository includes Bicep templates in `deploy/azure/` and a root `azure.yaml`
for Azure Developer CLI. They provision a Container Apps environment, Discord,
Teams, MCP and scheduler apps, a manual migration/seeding job, Burstable
PostgreSQL, RBAC Key Vault, a private Blob container, ACR, Log Analytics and an
optional Azure OpenAI model deployment with a Foundry project. Known model names
used as deployment aliases must match their underlying model regardless of
letter case. The services array selects which adapters run; MCP receives the
selected platform settings.
Service secrets use Key Vault references through managed identity.

Deployment is staged: foundation, image publication and successful init, then
serving apps. The baseline uses public endpoints and does not implement private
networking. By default (`chartStorage=azure_blob`), MCP uses private Blob chart
storage with the runtime managed identity: delegation-key access is scoped to
the storage account and blob data access to the charts container. Existing S3
installations must set `chartStorage=external` to omit Blob defaults and roles;
Blob mode rejects shared or MCP S3 secret mappings before app startup. These templates have been validated
offline only; subscription deployment and runtime behavior have not been verified. See `deploy/azure/README.md` in the
repository for parameters, deployment commands, required roles and limitations.

## Self-hosted resource records

`daimon selfhosted init` stores an agent and an environment in Postgres for an
existing tenant. Apply migrations first. It needs the database connection and
no Anthropic or model credential. It prints the agent ID and environment ID,
one per line; repeating it with matching names and configuration reuses them.
A changed configuration is refused. Archived names can be initialized again
with a new active record.

```bash
# DAIMON_DATABASE__URL contains the Postgres connection URL.
daimon selfhosted init \
  --tenant-id <tenant-uuid> \
  --harness-url https://harness.example \
  --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY
```

The default names are `daimon` and `default`; override them with `--name` and
`--environment-name`. `--system-file` reads the agent's system prompt from a
UTF-8 file. `--model` is the Azure deployment name. The command records
`DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY` as a variable name; it does not read or store its value.
`--packages-note` records an operator note without installing packages.

The record schema also accepts Foundry routing with
`--transport foundry_invocations --agent-name <hosted-agent>` and
`--project-endpoint <project-endpoint>`, with `--auth-mode entra` and
`--auth-ref <audience>`. Both routing fields are required for that transport.
The harness image runs tini as PID 1 to reap orphaned tool processes. Custom
entrypoints must retain that reaper. Multi-session service additionally requires
all shared scratch directories, including `/var/tmp` and `/dev/shm`, to deny tool
UID writes; startup scans writable storage mounts and refuses unsafe directories.
Tools use private per-session temporary directories.

Current deployments use direct transport; the harness refuses unsigned Foundry
gateway authentication. Direct `/v0/*` requests are the default transport. HTTPS is required outside
loopback. `--allow-insecure-http` explicitly permits HTTP on internal service
names/private addresses for an in-cluster deployment.

Self-hosted resources belong to one tenant. Their full mux specs, revisions,
archive state and mutation receipts live in Postgres. MCP credentials are
referenced by environment variable name, and harness URLs cannot embed
credentials. These records do not provision an Azure resource or start a
harness.

Selfhosted admission is disabled by default. Enable
`DAIMON_SELFHOSTED__ADMISSION_ENABLED` only with a configured self-hosted runtime
and a profile that supplies usage observations. Select selfhosted on the channel;
agent backend preferences cannot choose it. Existing setup/handoff threads that
reference Managed Agents responders cannot continue on a selfhosted channel.

For self-hosted tool preparation, wildcard MCP credential bindings require
`DAIMON_SELFHOSTED__SINGLE_TENANT=true`; shared installations must list tenant
UUIDs. Credential-free HTTP MCP destinations in private or metadata ranges are
refused, except loopback. Operators can allow exact hosts through the JSON list
`DAIMON_SELFHOSTED__MCP_ALLOWED_HTTP_HOSTS`. This host check classifies literals
and internal names; configure harness egress to prevent DNS rebinding and
redirects into private networks too.

## Upgrading an existing installation

The new MCP server is strict immediately by default: credentials need a live
registry entry. Re-mint old CLI or MCP-client tokens that do not contain a `jti`
with the updated `daimon mcp mint-token` command, then replace them in the
clients that use them. Use the updated agent/operator mint commands for those
credential types. Treat command output as a credential and store it privately.

Chat credentials renew automatically on session reuse and agent-chat or hub
continuation. Existing turns using older vault credentials can stop reaching
MCP if the verifier is upgraded before their minters. Use a staged upgrade when
preserving those turns matters.

### Staged MCP upgrade

1. Apply migrations with the existing migration procedure. Deploy the updated
   MCP service first: it also creates credentials for agent-chat, hub and OAuth
   sessions. For the initial rollout only, explicitly set
   `DAIMON_MCP__ALLOW_LEGACY_TOKENS=true` and
   `DAIMON_MCP__LEGACY_TOKEN_CUTOFF` to a timezone-aware UTC deadline five hours
   ahead. Only non-internal legacy credentials qualify. Unregistered internal
   credentials are refused immediately, including during this window; replace
   any external callers with registered credentials before updating MCP.
2. Update every credential minter, including platform workers, the scheduler
   and any separately installed CLI. Refresh notebook services too. Record when
   worker refresh completes and when the new MCP revision finishes deploying.
   Old MCP requests can still create credentials after traffic switches, so
   add the old revision's request timeout to its deployment completion time.
3. From the later of worker completion and that last possible old MCP mint,
   allow the existing 45-minute turn ceiling plus one minute. Complete this
   drain before the original deadline.
4. Set `DAIMON_MCP__ALLOW_LEGACY_TOKENS=false` on every MCP replica. After this
   flip, or when the deadline passes, every unregistered credential is refused
   regardless of issuance time. Keep clients on the updated minters.

The deployment workflow automates this order for an existing MCP service. A
retry preserves the original deadline; it never extends or reopens the window.
An expired deadline allows boot but remains strict. Configuration refuses dates
before 2026-10-16 UTC, naive/invalid dates and deadlines over six hours ahead.
Enabling compatibility without an explicit deadline is also refused.

A missing MCP service needs deliberate provisioning before this workflow. A
stopped worker cannot provide a refresh receipt; resume and update it before
attempting the first cutover. Do not fabricate a receipt. Rolling back to old
minters after strict admission is unsupported: their output will be refused.

### Interrupted rollout and recovery

If rollout stops after the MCP update but before worker refresh, the deadline
still closes admission automatically. Old workers can then lose MCP access
until they are updated. Alert on the failed rollout and authentication failures;
finish worker refresh promptly, using the original deadline. If it has already
expired, repair the minters while leaving admission strict. Do not reset the
window to hide an interrupted upgrade.

A provider or database error during credential renewal prevents a continued
turn from sending. Check those dependencies and retry; renewal keeps the
existing session mutation fence. Renewal never creates a missing credential
slot or replaces a person's OAuth credential.

Billing checkout revokes its temporary credential after the HTTP hop. Cleanup
failure is logged without credential values and preserves the checkout URL or
original HTTP error; the original ten-minute expiry remains its upper bound.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 23 or later (the image build
  needs BuildKit) with Compose.
- An Anthropic API key **in a workspace dedicated to this deployment**.
  Daimon manages the workspace's Managed Agents resources as its own, so
  sharing the workspace with anything else causes collisions. Skip this if
  you're self-hosting on OpenAI only — see
  [Self-host on OpenAI](#self-host-on-openai).

## 1. Configure the environment

```bash
cp .env.example .env
```

Open `.env`, then uncomment and fill in:

- `DAIMON_ANTHROPIC__API_KEY`: your Anthropic API key. Leave unset if you're
  self-hosting on OpenAI only — see [Self-host on OpenAI](#self-host-on-openai).
- `DAIMON_MCP__JWT_SECRET`: any random string, e.g. `openssl rand -hex 32`.
- `DAIMON_MCP__PUBLIC_URL`: `http://localhost:8765/mcp` is fine for local use.
- `POSTGRES_PASSWORD`: a strong, URL-safe value (avoid `@ : / % #`).
- `DAIMON_CRYPTO__KEYS`: a Fernet key, from
  `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  Paste the key as is. During rotation, list several keys newest first,
  comma-separated (`NEWKEY,OLDKEY`) or as a JSON list (`["NEWKEY","OLDKEY"]`).
  Agent keys, workspace tokens and MCP tokens are encrypted with it. Without
  it every agent key write is refused (see
  [Agent environment encryption](#agent-environment-encryption)). Back it up
  separately from the database.

`DAIMON_MCP__JWT_SECRET`, `DAIMON_MCP__PUBLIC_URL` and `POSTGRES_PASSWORD`
must be set before the first `docker compose` command: `docker-compose.yml`
interpolates them for every service with fail-fast `${VAR:?...}` guards.
`DAIMON_ANTHROPIC__API_KEY` has no such guard — an OpenAI-only deployment
needs it left unset. Set `DAIMON_CRYPTO__KEYS` at the same time; after the
stack is up, `docker compose run --rm init "daimon crypto verify"` confirms it
is set and that no agent key is stored in plaintext. `.env` is gitignored, so secrets never get committed.
`.env.example` documents every other setting.
Agent names and avatars in message headers are off by default. Set
`DAIMON_AGENT_IDENTITY__ENABLED=true` in `.env` and restart the services
after configuring platform permissions; see [agent identity](agent-identity.md).
Set `DAIMON_OPS__ALERT_WEBHOOK_URL` to a private Discord channel webhook to
receive short alerts for installs, Stripe top-ups, and Anthropic limits.
Leave it unset to disable operator alerts.

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
   `Create Public Threads`, `Manage Threads`, `Read Message History` and
   `Embed Links`. Select `View Channels` and `Manage Webhooks` too. With
   exactly these eight permissions, the invite permissions integer is
   `326954470400`.
5. Open the generated URL and invite the bot to a test server you control.

For a server where the bot is already installed, open **Server Settings → Roles**,
select the bot's role, enable **Manage Webhooks**, and save. Check each channel
where agents answer: **Edit Channel → Permissions** must also allow the bot role
to manage webhooks. Discord's channel overrides can deny a permission granted
at server level. Without it, agent replies still post through the bot with a
bold agent name on the first answer chunk.

`/routines` requires Discord's `Manage Server` permission. The other commands are open to every member; what each person may change is checked inside.

Unclicked private credential forms can update their original Discord card
after expiry when `DAIMON_DISCORD__CREDENTIAL_FORM_EXPIRY=true`. The default
is `false`, which starts no expiry job or extra database/Discord calls. The
job runs only in the existing single posting-owner process, checks every
15 seconds, and retires expired unused requests before editing. Failed edits
remain pending across restarts; successful edits remove the buttons without
pinging anyone. Missing card identities are skipped. This does not save
credentials, extend the form's lifetime, or resume the requested work.

## 3. Start the stack

```bash
docker compose up --build -d
```

This brings up Postgres, runs migrations and seeds the default agents,
environments and skills (the `init` service does both), then starts the
`mcp`, `discord` and `scheduler` services. `docker compose up -d` doesn't
show `init`'s output; if you meant to run on Anthropic and forgot
`DAIMON_ANTHROPIC__API_KEY`, the stack still starts (an OpenAI-only
deployment needs it unset), so check `docker compose logs init` once —
it prints plainly whether it seeded Anthropic defaults or skipped them.

Set `DAIMON_DATABASE__POOL_SIZE`, `DAIMON_DATABASE__MAX_OVERFLOW` and
`DAIMON_DATABASE__POOL_TIMEOUT` per process when sizing Postgres for more
concurrent turns. Each process can open up to `POOL_SIZE + MAX_OVERFLOW`
connections; include every worker and MCP instance in the database limit.

Once it settles, send a message that `@mention`s the bot. It replies in a
new thread. If the bot stays silent, check `docker compose logs discord`; an
unset `DAIMON_DISCORD__BOT_TOKEN` is the usual cause.

### Running a published image instead of building

Every tagged release is published to the GitHub Container Registry as
`ghcr.io/pymc-labs/daimon`, so a host that should not compile anything can run
a release straight from the registry. All six application services run the
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
  teams:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  scheduler:
    image: ghcr.io/pymc-labs/daimon:0.2.0
```

Then `docker compose pull && docker compose up -d`. Drop the `--build` flag
from step 3 and from the Slack and Teams profile commands: it rebuilds from the working
tree under the same tag and throws the pulled image away. The image ships
only code; `init` still seeds `defaults/` from your checkout, so check out the
matching tag first (`git checkout v0.2.0`). Upgrading is checking out the
next tag, editing the override and running those two commands again.

The report-host image is built from the `report-host` dependency closure in the
repository's `uv.lock`. Its build checks every installed distribution against
that lock, including platform markers, and starts the installed application
directly so startup cannot resolve different packages. Build it from the
repository root with `docker build -f apps/report-host/Dockerfile .`.

### Self-hosted agent harness

For an Azure Container Apps dynamic-session pool, use the pool's published
management endpoint as `harness_url` and set the environment's native
configuration to `session_pool: true`, `transport: "direct"`,
`auth_mode: "entra"`, `auth_ref: "https://dynamicsessions.io"`.
The host's identity needs Azure ContainerApps Session Executor on the pool;
its operator harness-auth binding must allow that audience, the exact pool
endpoint origin and the tenant. Every C3 request carries the deterministic
session identifier, including event polling, approvals and cancellation.
Keep admission disabled until live header forwarding and durable state recovery
have been verified. See `apps/harness/README.md` for routing and authentication
details.

The standalone harness image runs the OpenAI Agents SDK against Azure OpenAI.
Build it from the repository root with
`docker build -f apps/harness/Dockerfile -t daimon-harness .`. Its dependencies
are exported from `uv.lock`; the image includes NumPy, pandas, PyMC and
PyMC-Marketing. Supply `HARNESS_TOKEN`, `AZURE_OPENAI_ENDPOINT` and either
`AZURE_OPENAI_API_KEY` or ACA managed identity at runtime. The model credential
uses only `ManagedIdentityCredential`, with `IDENTITY_ENDPOINT` and
`IDENTITY_HEADER` required together and an optional `AZURE_CLIENT_ID`; these
values are captured then scrubbed. File-backed credentials, certificate paths,
projected workload tokens and tool-readable regular files on external mounts
refuse startup. Mount checks use mountinfo rather than filenames and include
read-only mounts. Only image/runtime system paths and isolated session storage
are exempt. All non-completed Azure responses preserve usage but end as `errored`
with the provider reason or error.
Mount a
persistent home directory at `/home/harness`; run one worker per HOME. The image
starts as root, drops the service to UID 10000 and retains the capabilities needed
to assign separate tool UIDs and work/skills directories to sessions. Production
refuses a same-UID fallback. `HARNESS_SESSION_MODE=single` is the default: the
first session ID binds persistently and all other IDs receive 409 `session_bound`.
Use one custom container per session in Azure Container Apps dynamic sessions.
Every tool receives a session-local HOME, cwd and TMPDIR. Multi-session mode
requires verified `hidepid=2` process visibility, per-session UIDs and a shared
`/tmp` that tools cannot write; otherwise startup refuses it.
Use direct ACA transport with private ingress and
bearer or Entra authentication; unsigned Foundry gateway mode refuses startup.
Port 8088 serves authenticated `/v0/*` endpoints and `/invocations`. Model calls,
tool execution and session files remain in the configured runtime and Azure
model endpoint. The application README documents environment variables and
Foundry deployment constraints.

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
environment and does not load `.env`. Slack and Teams start the same way,
with `uv run python -m daimon.adapters.slack` and
`uv run python -m daimon.adapters.teams`.

### Several MCP instances

The MCP endpoint is stateless: it issues no session id and every request
carries its own token, so `mcp` replicas need no sticky sessions behind a load
balancer, and a redeploy does not strand connected clients. The hourly limits
on notebook publishes and bundle uploads are kept in memory, so each replica
counts its own.

The `selfhosted.loop` harness backend has a separate operator admission switch,
`DAIMON_SELFHOSTED__ADMISSION_ENABLED`, which defaults to `false`. Disabled
admission refuses with `selfhosted_admission_disabled`. Enabling it still requires
usage observations and a configured self-hosted core runtime; it does not select
a backend for any channel. See [configuration](configuration.md) for the setting.

## Self-hosted harness runtime

The opt-in `selfhosted.loop` backend runs chat turns through a separately
operated `daimon-harness/0` service. Set `DAIMON_TURN__PATH=mux` and enable
channel backends and `DAIMON_SELFHOSTED__ADMISSION_ENABLED=true`, then select
the selfhosted agent/environment for the channel.
The `selfhosted.loop` profile and `daimon selfhosted init` default to the Azure
deployment name `gpt-6.1-sol`; pass `--model` for another deployment. Azure
templates parameterize model name, version, deployment name, SKU and capacity;
see the [Azure deployment guide](https://github.com/pymc-labs/daimon/blob/main/deploy/azure/README.md) for Luna smoke tests.
Each caller gets a separate persistent harness session. Steer, shared sessions,
workers, session transfer, memory stores and workspace export/import are not
supported. Admission refuses policies the harness cannot enforce.

Mount a credential-free JSON manifest and point
`DAIMON_TURN__PROVIDER_RUNTIME_FILE` at it. Each entry grants exactly one
configuration to one tenant, channel and account; no wildcard grants are read:

```json
[
  {
    "profile": "selfhosted.loop",
    "tenant_id": "<tenant UUID>",
    "platform": "discord",
    "channel_id": "<channel id>",
    "account_id": "<account UUID>",
    "config_digest": "<admitted ConfigRevision.content_digest()>",
    "agent_id": "sh_agent_<id>",
    "environment_id": "sh_env_<id>",
    "infrastructure_usd": "0",
    "max_tool_calls": 100,
    "deadline_s": 300
  }
]
```

`infrastructure_usd` is required: enter an audited per-request infrastructure
cost, or explicitly choose zero when the operator funds it separately. Configure
selfhosted billing's deployment-to-model mapping and verified model rates;
an unpriced or expired deployment is refused before any harness HTTP call.
Unmeasured model attempts remain pending for reconciliation.

Production uses `HarnessConfig.transport="direct"` with a private harness
endpoint. Direct transport sends authenticated `/v0` requests to `harness_url`.
The Foundry invocation adapter is retained for future platform validation;
unsigned gateway identity headers do not authorize harness calls. `bearer_env`
only accepts operator variables named `DAIMON_SELFHOSTED__HARNESS_TOKEN_*`.
Declare each in `DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS`, a JSON list of
`{"name": "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY", "origin": "https://harness.example", "tenants": ["TENANT_UUID"]}`.
Entra mode requires a binding too, with `auth_mode: "entra"` and `name` equal
to the exact audience, for example `https://harness.example`. The exact HTTPS
origin and tenant must match before the host resolves a bearer secret or mints
an Entra token. An audience grant never authorizes another destination.
Wildcard tenants require `DAIMON_SELFHOSTED__SINGLE_TENANT=true`.
Environment revision pins are checked before replay and credential resolution.
`entra` reuses one Azure DefaultAzureCredential and token per runtime, refreshing
before expiry. Transport errors during refresh remain retryable. Configure ingress and the invoked identity's access
for the chosen transport. Both transports use JSON acknowledgements and durable
event polling, including after a dropped connection.

Daimon's MCP public URL and JWT secret must be configured. Each turn receives a
caller-bound MCP token only in `X-Daimon-MCP-Authorization`; it never enters the
manifest or turn JSON and is revoked when the turn ends. Third-party MCP
credentials are operator-provisioned environment references on the harness.
Host tool-safety denies and confirmation gates merge with the pinned agent's
tool policy; the stricter effective permission wins, including server defaults.
MCP approvals retain their server and tool names during live delivery and replay.
Replay applies the same explicit agent permissions as live delivery. Approval
cards and tool results share the tool call identity; confirmations retain the
harness action identity. Repeated call records can tighten an explicit approval
permission; they cannot relax it. Host inspection pauses still honor automatic
permissions and saved allow rules, including unattended runs. After a host
restart, historical approval records recover completed batch members so the
remaining approvals can still be submitted.

Selfhosted turns support unversioned seeded skills from the configured defaults
root. Custom skill uploads and channel-library additions have no supported
selfhosted provisioning path yet: no production CLI or MCP upload route writes
the selfhosted skill catalog. Existing Anthropic library uploads do not populate
it. Do not configure custom version pins expecting those uploads to be available.

The internal catalog reader can load already-retained bundles, including exact
channel grants, but its store API and test-seeded rows are not an upload feature.
It looks up agent pins in the agent's tenant catalog, never a deployment account's
catalog. Attached pins carry uploader account and principal provenance; authorized
shared-agent callers can read only those attached versions. A channel grant
without retained bytes is skipped, and an agent pin without retained bytes
refuses the turn. No provider upload or Anthropic fallback fills missing bytes.

Replay and Stop page through long in-turn histories by cursor under their
existing deadlines, without a fixed 5,000-record ceiling.
Explicit agent `ask` permissions always require confirmation, even for sandbox
writes or tools the host otherwise permits. Daimon proxy calls use the underlying
tool's host safety and confirmation rules; neutral calls run with default settings.
Session replay resumes from a durable accounting cursor, and each invocation
persists its own starting position for recovery.
On a deadline or abandonment the finalizer attempts bounded cancellation and
replays usage. Unmatched requests remain pending under their original admission;
subsequent session replay also records late receipts from earlier turns.
Unavailable skills are reported once in the bound thread. A changed agent or
environment revision requires a fresh session binding.

## Self-host on OpenAI

Daimon can run on just your own OpenAI key — no Anthropic account needed at
all. The OpenAI driver (`daimon-mux[openai]`) ships in the default install
already, so there is no separate install step. Leave
`DAIMON_ANTHROPIC__API_KEY` unset in `.env`: `init` detects there's no
Anthropic key and skips the Anthropic default-agent/skill reconciliation
step instead of failing, and every service starts without one. This section
covers the OpenAI-only setup: provisioning resources and routing channels to
them.

### What's different on OpenAI

- **No multi-agent delegation.** OpenAI's hosted-workspace profile doesn't
  support it at all — not a config gap, a capability the driver doesn't have.
- **No mid-session resource changes.** A session's initial file mounts (the
  main case: a GitHub-connected working copy) work; swapping them after the
  session starts doesn't.
- **No workspace export/import** — there's no way to export a live OpenAI
  session's workspace or restore one from a saved export.
- **No memory across turns.** Every OpenAI-routed turn starts fresh.
- **No Daimon model knobs.** The OpenAI driver fixes its own model and
  container defaults (see `docs/mux.md`'s "OpenAI deployment runtime" and
  "Preserving source capabilities in an OpenAI hosted workspace" sections).

Everything else — the chat platforms, the default skill set, scheduled
routines — works the same regardless of provider.

### 1. Get an OpenAI API key and project

Create (or choose) an OpenAI project and an API key scoped to it. Add it to
`.env` under its own name, never the reserved `OPENAI_*` namespace Daimon
refuses to read credentials from:

```bash
DAIMON_CHANNEL_OPENAI_KEY=sk-...
```

`DAIMON_CHANNEL_OPENAI_KEY` is the default name both `daimon openai init` and
the runtime file expect; pass `--api-key-env` to `init` if you'd rather name
it something else. The name is all the runtime file ever records — never the
key itself.

### 2. Provision the agent, environment and default skills

```bash
docker compose run --rm init "daimon openai init --tenant-id <your-tenant-uuid> --project <openai-project-id> --model <openai-model-id>"
```

This creates (or reuses, if you run it again) one OpenAI agent, one hosted
environment, and uploads every skill under `defaults/skills/` onto that
agent — the same default skill set an Anthropic deployment gets from `daimon
defaults apply` — then writes one row for them into the provider runtime
file. It's idempotent: an identical tenant/platform/channel/account selector
prints the existing IDs instead of provisioning (or re-seeding skills)
twice. Pass `--skip-default-skills` for a bare agent instead. It prints only
the created IDs — never the API key.

Find your tenant UUID with `daimon tenants list` once the stack has
auto-discovered it from a first message on another channel, or pick any
UUID you control if this is a fresh, OpenAI-only deployment from the start.

### 3. Point channels at it

The one-command way: route your existing default agent's new threads to
OpenAI entirely —

```bash
docker compose run --rm init "daimon agents backend set <agent-name-or-id> openai"
```

— after which every channel using that agent goes to OpenAI, with no
per-channel setup. This is the same command any backend switch uses; nothing
OpenAI-specific about it.

If you'd rather route only specific channels and leave the rest on
Anthropic, use `daimon channels rule set` per channel instead, scoped with
`--platform`/`--channel-id`/`--account-id` on `daimon openai init` (its
default is `*` — every platform, channel and account under your tenant, the
single-entry default most self-hosters want, so the runtime file never needs
one entry per channel).

The wildcard is scoped to your tenant and nothing wider: `tenant_id` is
never wildcardable, by construction — the loader refuses a manifest row that
tries (see `docs/mux.md`'s "OpenAI deployment runtime"). One deployment's
OpenAI resources can never serve a different tenant through this entry, even
with every other field left as `*`; an exact, narrower row always wins over
a coexisting wildcard default for the same tenant.

Either way, add to `.env` (the `init`/`mcp`/`discord`/`slack`/`teams`/
`scheduler` services already share `./runtime` as `/app/runtime`, see
`docker-compose.yml`):

```bash
DAIMON_TURN__PATH=mux
DAIMON_TURN__CHANNEL_BACKENDS=true
DAIMON_TURN__PROVIDER_RUNTIME_FILE=/app/runtime/openai.json
```

and point step 2's `init` command at the same path with
`--manifest /app/runtime/openai.json` (or rely on the env var above, which
`daimon openai init` reads by default).

### 4. Verify it

```bash
docker compose run --rm init "daimon openai smoke --project <openai-project-id> --model <openai-model-id>"
```

This provisions its own small dedicated agent, environment and skill
(separate from your real one), runs one chat turn that must make one tool
call, and exits nonzero with a clear reason unless it completed. It's
billed — a single cheap turn, same as `daimon smoke` for Anthropic.

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

## GitHub App (optional)

Register one GitHub App for this deployment. Set its user authorization callback
to `<root>/oauth/github/callback` and its setup URL to
`<root>/oauth/github/setup`, where `<root>` is the public MCP URL with the
trailing `/mcp` removed. For example, if `DAIMON_MCP__PUBLIC_URL` is
`https://example.com/mcp`, use `https://example.com/oauth/github/callback` and
`https://example.com/oauth/github/setup`. GitHub must be able to reach those
URLs, so a localhost URL only works with a suitable tunnel.

Set `DAIMON_GITHUB_APP__APP_ID`, `APP_SLUG`, `PRIVATE_KEY`, `CLIENT_ID` and
`CLIENT_SECRET` in `.env` using the values from that App. Set
`DAIMON_CRYPTO__KEYS` to encrypt the short-lived user tokens. The connect routes
are mounted only when these values and `DAIMON_MCP__PUBLIC_URL` are present.
The scheduler removes expired connection flows, including their encrypted
tokens. Run it alongside the MCP service.

Ask an agent to connect GitHub, or run `/github connect` in Discord or Slack.
A server admin gets a private link bound to that agent; the selected repos are for that agent
only and activate for it when confirmed. The migration retires any saved
GitHub key. An
operator can issue an agent-bound link with `daimon github connect-link --tenant <workspace-uuid>
--requester <platform-user-id> --agent <agent-uuid> --agent-name <agent-name>`.
The selected grants become live when the recipient confirms them on the web.
Members can request setup; their request is recorded for an admin. For an
operator fallback, print a seven-day, single-use invitation with
`daimon github connect-link --tenant <workspace-uuid> --requester <platform-user-id>`.
The invitation is minted on that workspace admin's behalf. The recipient signs in
to GitHub and picks repositories. No repository is preselected. The page lists
the repositories the GitHub App installation covers that the signed-in account
can see on GitHub, and notes how many it hides because the account can't.
Each selected repository is checked again against GitHub at confirmation.
Every connected repository is read and write for the agent, within the GitHub
App installation's permissions; the person's own GitHub role does not lower it.
A repository the App installation doesn't cover needs a GitHub org owner to add
it to the installation first.

In Discord or Slack, open `/github` to see the server connection, your personal
GitHub link, and waiting requests. An admin can also open GitHub from
`/agent-setup` to see an agent's repos. The browser page searches repos and
keeps Save changes visible until confirmation. Admins can review requests and
disconnect the server connection.

Operator-issued links without an agent target connect repos to the workspace.
An admin grants one to an agent with
`daimon github grants add --tenant <workspace-uuid> --agent <agent-uuid> --repo <repository-id> --baseline read --ceiling read`.
Use `grants list` and `remove` with the same tenant and agent options. Existing
sessions rotate tokens in place when the repository set is unchanged; a changed
repository set closes the sessions so the next turn mounts the new checkouts.

Before upgrading, run `daimon github retirement-preview`. It lists staged grants
the migration will delete, and every agent losing a saved key or binding with
its working repo and whether a live App grant covers it. An uncovered agent
loses GitHub access until an App grant is connected. The preview also reports
App token evidence for live sessions and stops if an unapproved agent lacks
working or private skill repo coverage. Set
`DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS` to a comma-separated list of agent UUIDs
whose loss is explicitly accepted by the operator. Set the same value for the
preview and the Alembic migration. With no value, the migration stops before
deleting rows if any agent would lose access. Both also stop when a retired
credential points to more than one workspace. A saved key with no workspace
at all, such as one left by a deleted agent, can't be matched to an owner
while any live session has no recorded agent, and that stops them too. Once
every live session names its agent, such a key is retired without restarting
any session, provided its exact agent UUID is in the accepted list. After
upgrading, run
`daimon github cleanup-legacy-vault` to preview retired
Managed Agents vault credentials. Run it with `--apply` to delete them. Repeated
runs skip credentials already deleted. This command leaves GitHub Copilot MCP
credentials configured for `https://api.githubcopilot.com/mcp` alone.
The migration asks live sessions of agents with retired GitHub credentials to
start fresh on their next turn; other sessions keep their workspace. Run
`cleanup-legacy-vault --apply` even after those sessions restart: the migration
does not revoke vault-side credentials.
During the rollout window, an old-image process may reuse a marked session if
it cannot read legacy session metadata, or return a marked active session for
reuse. Repeated metadata failures can cause repeated reuse until that process
refreshes. The marker persists, and new code replaces the session on its next
turn.

**GitHub retirement release gate.** After the migrate job, wait for the MCP
update and worker refresh. Set `REGION`, `ZONE`, `AR_REPO` and
`EXPECTED_DAIMON_IMAGE` to that deploy's values on the deploy runner. Check
every traffic-bearing or tagged MCP revision, then connect to the worker VM:

```bash
set -e
mcp_revisions=$(gcloud run services describe "${AR_REPO}-mcp" --region "$REGION" --format=json |
  jq -r '.status.traffic[] | select((.percent // 0) > 0 or .tag != null) | .revisionName')
test -n "$mcp_revisions"
while IFS= read -r revision; do
  image=$(gcloud run revisions describe "$revision" --region "$REGION" \
    --format='value(spec.containers[0].image)')
  printf '%s %s\n' "$revision" "$image"
  test "$image" = "$EXPECTED_DAIMON_IMAGE" || exit 1
done <<< "$mcp_revisions"
gcloud compute ssh "${AR_REPO}-workers" --zone "$ZONE" --tunnel-through-iap
```

In that SSH shell, set `EXPECTED_DAIMON_IMAGE` to the same image and check
every running worker container:

```bash
set -e
cd /etc/daimon
expected_id=$(sudo docker image inspect "$EXPECTED_DAIMON_IMAGE" --format '{{.Id}}')
containers=$(sudo docker compose -f compose.worker.yml ps -q)
test -n "$containers"
for container in $containers; do
  image_id=$(sudo docker inspect "$container" --format '{{.Image}}')
  name=$(sudo docker inspect "$container" --format '{{.Name}}')
  printf '%s %s\n' "$name" "$image_id"
  test "$image_id" = "$expected_id" || exit 1
done
```

Every printed MCP revision must show `EXPECTED_DAIMON_IMAGE`; every printed
worker must show `expected_id`. Migration 0105 records each marked session's
original `ma_session_id` and marking time in
`github_retirement_session_audit`. After in-flight turns drain, run both
read-only queries. The first finds any recorded turn outcome, regardless of
reason, that started on the original session after the mark:

```sql
SELECT a.thread_session_id, a.ma_session_id, a.marked_at,
       o.id AS turn_outcome_id, o.started_at, o.reason
FROM github_retirement_session_audit AS a
JOIN turn_outcomes AS o
  ON o.session_id = a.ma_session_id AND o.started_at > a.marked_at
ORDER BY a.thread_session_id, o.started_at;
```

The second lists marked original mappings still live on the same session:

```sql
SELECT a.thread_session_id, a.ma_session_id, a.marked_at
FROM github_retirement_session_audit AS a
JOIN thread_sessions AS s ON s.id = a.thread_session_id
WHERE s.status = 'live' AND s.ma_session_id = a.ma_session_id
ORDER BY a.thread_session_id;
```

Expected result for each query: zero rows. A row in the second query means
replacement is still pending; it does not itself show reuse. `started_at`
comes from turn creation (`packages/core/daimon/core/turn/outcomes.py:92-94`),
is copied into the outcome at finish (`:264-276`), and is stored later
(`:325-330`); it is not the database insertion time. Consider the old-process
window closed only after the image checks and both queries pass.
The outcome writer is best effort (`turn/outcomes.py:47-60,325-330`), so an
empty first query rules out recorded post-mark turns, not unrecorded ones.

## Microsoft Teams (optional)

Teams takes the most setup of the three platforms, because the pieces live in
four Microsoft admin portals:

- **Entra** (the organisation's directory) holds the app's identity.
- **Azure** hosts the bot registration that Teams talks to.
- **The Teams admin center** decides who may install the app.
- **Teams itself** is where you add it.

None of it is hard, but the order matters. Plan on an hour the first time.

Two things work differently from Discord and Slack:

- **Teams pushes messages to you.** Microsoft delivers every message to an
  HTTPS address you give it, so the Teams service needs a public hostname
  with a valid certificate. Discord and Slack dial out for messages instead.
- **One deployment serves one organisation.** The bot answers only people in
  the Microsoft 365 organisation it is registered in. People of another
  organisation (external participants and guests) in its channels and group
  chats are answered as members (below).

What the bot does once it is running (1:1 chats, channel threads, commands,
files and its limits) is in [`teams.md`](teams.md).

### Before you start

You need:

- **A Microsoft 365 organisation with Teams, and a work account inside it.**
  Do every step below signed in with that account. A personal Microsoft
  account, or an address from another provider, won't work: signing up for
  Azure with one creates a separate, empty directory, and a bot registered
  there can't talk to your Teams.
- **An Azure subscription in that same organisation.** Microsoft 365 doesn't
  include one. Pay-As-You-Go is enough, and the bot itself costs nothing:
  the Azure Bot's F0 tier and its Teams channel are free. Adding a small
  budget alert under Cost Management is cheap insurance.
- **A public HTTPS hostname** for the Teams service, such as
  `teams.example.com`, with a publicly trusted certificate.
- **The right roles**, or someone who has them:

| Step | Who can do it |
| --- | --- |
| Register the app in Entra | Anyone, if your organisation lets users register apps; otherwise the Application Developer role |
| Create the Azure Bot | Contributor (or Owner) on the subscription or a resource group |
| Upload the app to Teams | A Teams administrator, or anyone if custom app uploads are allowed |
| Turn on channel files (optional) | A global administrator (or Privileged Role Administrator) to consent once |

### 1. Register the app in Entra

This registration is the identity Daimon signs in as.

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → **App
   registrations** → **New registration**.
2. Give it a name (`daimon` is fine) and choose **Accounts in this
   organizational directory only**. Leave the redirect URI empty, then
   **Register**.
3. On the Overview page, copy two values:
    - **Application (client) ID**: this is `DAIMON_TEAMS__CLIENT_ID`.
    - **Directory (tenant) ID**: this is `DAIMON_TEAMS__TENANT_ID`.
4. **Certificates & secrets** → **New client secret**. Copy the **Value**
   (not the Secret ID) straight into your password manager or `.env`: it is
   shown only once. This is `DAIMON_TEAMS__CLIENT_SECRET`. Note its expiry
   date, because the bot stops answering the day it lapses.

You don't need to add any API permissions here. The bot reads channels
through a team-level permission that a team owner grants when they add the
app (more on that in step 7), and group chats through a chat-level one
(`ChatMessage.Read.Chat`) granted when the app is added to the chat. Only two optional features need a Graph
application permission: channel files (`Files.ReadWrite.All`) and private and
shared channel messages (`ChannelMessage.Read.All`, which is tenant-wide; see
[below](#private-and-shared-channels-optional)).
The same team-level consent covers activity feed notices
(`TeamsActivity.Send.Group`). A team that added a package from before 0.6.0 needs the
rebuilt one uploaded again (see Updating the app) and an owner to accept it;
until then its notices are refused and only logged.

### 2. Create the Azure Bot and connect it to Teams

The Azure Bot is the registration Teams uses to find your bot.

1. In [portal.azure.com](https://portal.azure.com), create a resource group
   (say, `daimon`) if you don't have one.
2. **Create a resource** → search for **Azure Bot** → **Create**:
    - **Pricing tier:** F0 (Free).
    - **Type of App:** Single Tenant.
    - **Creation type:** Use existing app registration, then paste the
      client ID and tenant ID from step 1.
3. Leave the messaging endpoint for now; you'll set it in step 4, once the
   Teams service is reachable.
4. Open the new bot → **Channels**. Under *Available channels*, click the
   name **Microsoft Teams**. It's a link, not a button. Accept the terms,
   choose **Microsoft Teams Commercial**, and click **Apply** (you may need
   to scroll down). Teams should then appear in the channel list as
   *Healthy*.

If clicking the name does nothing, your account probably has read-only
access to the bot; check **Access control (IAM)** → **View my access**. As a
fallback, open Cloud Shell (the `>_` icon in the portal's top bar) and run:

```bash
az bot msteams create -g <resource-group> -n <bot-name>
```

### 3. Run the Teams service

**Pick your admin team.** The owners of one team are Daimon's admins: they
can create routines, mint coding-tool tokens, replace shared keys, top up and
see everyone's usage. Anyone can create an agent. In Teams, open the team's
⋯ → **Get link to team** and copy the `groupId` parameter from the link. Once
the service runs, add the app to that team (step 7). To add or remove an
admin later, change the team's owners; no redeploy. A removed owner loses
admin within about a minute, everywhere, routines and tools included.

**Add the values to `.env`:**

```bash
DAIMON_TEAMS__CLIENT_ID=<application (client) ID>
DAIMON_TEAMS__TENANT_ID=<directory (tenant) ID>
DAIMON_TEAMS__CLIENT_SECRET=<client secret value>
DAIMON_TEAMS__ADMIN_TEAM_ID=<groupId of the admin team>
```

A few things to know:

- **Set the three credentials together.** Once any `DAIMON_TEAMS__` value is
  set, every Daimon process expects the full set. A half-filled block stops
  them all from starting, not just Teams.
- **Admins are the admin team's owners.** `DAIMON_TEAMS__ADMIN_USER_IDS` is
  no longer read, and a leftover value is ignored: make those people owners
  of the admin team.
- **`DAIMON_CRYPTO__KEYS` must be set** (see step 1 of this guide).
- **The `mcp` service reads these same values**, because the agent's Teams
  tools (posting, reading channels, sending files) run there. With Compose,
  sharing `.env` takes care of that.

**Start it:**

```bash
docker compose --profile teams up --build -d
```

The service listens on `127.0.0.1:3978` (`DAIMON_TEAMS__PORT`) and serves
`/api/messages`, `/healthz` and `/readyz`. `docker compose logs teams` should
show `teams.tenant_ready` once it has set up your organisation.

**Put it behind your public hostname.** Microsoft must reach `/api/messages`
over HTTPS. Any reverse proxy or tunnel works. With
[Caddy](https://caddyserver.com), which fetches the certificate for you:

```text
teams.example.com {
	@teams path /api/messages /healthz /readyz
	handle @teams {
		reverse_proxy 127.0.0.1:3978
	}
	handle {
		respond 404
	}
}
```

Only those four paths need to be public. The last one is used only by the
optional channel files; the optional private channel messages (below) add
`/api/graph/notifications` and `/api/graph/lifecycle`. Check it from outside your network:
`curl https://teams.example.com/healthz` should return `{"status":"live"}`.

### 4. Point the bot at Daimon

Back in the Azure Bot → **Settings** → **Configuration**, set the
**Messaging endpoint** to your hostname **with `/api/messages` on the end**:

```text
https://teams.example.com/api/messages
```

Forgetting that suffix is the most common reason a bot never answers. While
you're on that page, check that **Microsoft App ID** is your client ID,
**App Tenant ID** is your tenant ID, and **App type** is SingleTenant. Then
**Apply**. Changes can take a few minutes to reach Teams.

### 5. Build the Teams app package

Teams installs apps from a zip file holding three files at its top level:

- `manifest.json`;
- `color.png`, a 192×192 full-colour icon;
- `outline.png`, a 32×32 icon, white on a transparent background.

The manifest template is
[`teams-app-manifest.yaml`](teams-app-manifest.yaml), in manifest version
1.30 so the app can be added to private and shared channels and answer
privately in a thread. Fill in your client ID
and hostname and convert it to JSON. From the repository root, with any
Python 3 that has PyYAML:

```bash
mkdir -p teams-app
CLIENT_ID=<application (client) ID> TEAMS_HOST=teams.example.com python3 - <<'EOF'
import json, os, yaml
text = open("docs/teams-app-manifest.yaml").read()
text = text.replace("${DAIMON_TEAMS__CLIENT_ID}", os.environ["CLIENT_ID"])
text = text.replace("DAIMON_HOST", os.environ["TEAMS_HOST"])
with open("teams-app/manifest.json", "w") as f:
    json.dump(yaml.safe_load(text), f, indent=2)
EOF
```

Before you zip it, check these fields in `manifest.json`:

- `termsOfUseUrl` should point at your own terms page, since Daimon serves
  none.
- `websiteUrl` and `privacyUrl` should point at pages you serve. The Teams
  host itself answers 404 outside the four bot paths.
- `name` is what people see. If you run more than one deployment, say a test
  one and a production one, give each its own Entra app, Azure Bot and
  package, with a distinct name such as `daimon (test)`.

Then add the two icons. `assets/icon.png` can be resized to 192×192 for
`color.png`; the white-on-transparent `outline.png` you make yourself. Zip
the three files, not the folder that holds them:

```bash
cd teams-app && zip ../daimon-teams.zip manifest.json color.png outline.png
```

### 6. Upload and allow the app

In the [Teams admin center](https://admin.teams.microsoft.com):

1. **Teams apps** → **Manage apps** → **Upload new app**, then choose the zip.
2. Open the uploaded app → **Users and groups** → **Edit availability**, and
   make it available to everyone or to the people who'll use it. (Tenants not
   yet on app centric management: check that its status is **Allowed** and
   that **Permission policies** allow custom apps.)
3. Optional: under **Setup policies**, add the app to *Installed apps* to
   install and pin it for everyone.

A new upload can take anywhere from a few minutes to a few hours to show up
in the Teams client. If custom app uploads are allowed for users, you can
skip the admin center and sideload instead: in Teams, **Apps** → **Manage
your apps** → **Upload an app**.

### 7. Say hello

- **1:1 chat:** in Teams, **Apps** → **Built for your org** → your app →
  **Add**. That opens a chat with the bot and it greets you. Send `help` or
  `setup`.
- **A team:** on the app's page, open the menu next to **Add** → **Add to a
  team**, then pick the team and a channel. A team owner is asked to allow
  the app to read the team's channel messages. Accept, since that's how the
  bot reads the thread it is asked about. The bot then posts a welcome in
  the team. To ask it something, @mention it in a post or a reply. Pick the
  name from the autocomplete list so it turns into a highlighted mention:
  typed text alone doesn't count. Add it to the admin team too, or nobody
  is an admin.
- **A private or shared channel:** adding the app to the team doesn't add it
  there. The channel's owner adds it from the channel itself, in the
  channel's apps settings. A package built from an
  older template, before manifest 1.25, can't be added to these channels:
  rebuild it and upload it again (see Updating the app).

- **A group chat:** add the app from the chat's apps, and allow it to read
  the chat's messages (`ChatMessage.Read.Chat`) when asked. See
  [`teams.md`](teams.md#group-chats).

### People from another organisation (optional)

To work with a client's people in one channel, use a Teams shared channel
with B2B direct connect (Microsoft's guides: [Collaborate with external
participants in a
channel](https://learn.microsoft.com/en-us/microsoft-365/solutions/collaborate-teams-direct-connect)
and [Shared channels in Microsoft
Teams](https://learn.microsoft.com/en-us/microsoftteams/shared-channels)):

1. **Cross-tenant access, in both organisations** (Entra → **External
   Identities** → **Cross-tenant access settings**): add the other
   organisation. Yours allows B2B direct connect inbound, theirs outbound,
   for the users involved and the Office 365 application. Changes can take
   up to six hours.
2. **Teams policies** (Teams admin center → **Teams** → **Teams policies**):
   in your organisation, "Create shared channels" and "Invite external users
   to shared channels"; in theirs, "Join external shared channels".
3. **Guest access in Teams stays on.** Shared channels with external
   participants don't use guest accounts, but guest access must be enabled
   to invite them. The SharePoint and Microsoft 365 Groups guest settings
   must stay on too (the default).
4. Create the shared channel, optionally set a channel rule in Daimon
   (the setup panel's Channel settings, `set_channel_rule` or
   `daimon channels rule set`),
   then add the other organisation's people as channel members. Guests,
   including guests converted to members, can't be added to a shared
   channel; someone who also has a guest account in your organisation is
   added as an external participant (in the admin center, search
   `ext:user@domain.com`).

Daimon treats them, and guests anywhere, as members. See
[`teams.md`](teams.md#people-from-another-organisation).

### Channel files (optional)

Out of the box, the bot reads images in channel messages, and handles files
in 1:1 chats both ways. Files shared in a channel live in SharePoint (the
team's site for a standard channel, a site of its own for a private or shared
one), which the team-level permission doesn't reach. To let the bot open
those files and save its own outputs to the channel's Files tab:

1. Entra → **App registrations** → your app → **API permissions** → **Add a
   permission** → **Microsoft Graph** → **Application permissions** →
   `Files.ReadWrite.All` → **Grant admin consent**. No restart is needed:
   each channel's files turn on within 10 minutes.

This one permission covers every channel, private and shared ones included.
It reaches every file in your organisation, so Daimon only opens a shared
file inside the channel's own Files folder and only saves there. Open a new
channel's Files tab once first. A shared channel hosted by another
organisation keeps its files there, out of reach.

### Private and shared channels (optional)

Teams sends the bot only the private and shared channel posts that @mention
it. To let followed threads, Replies to the bot and typed `@Daimon` work there
too, grant the Graph application permission `ChannelMessage.Read.All` with
admin consent (as in step 1 above), set `DAIMON_TEAMS__PUBLIC_URL` and
`DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true`, and add
`/api/graph/notifications /api/graph/lifecycle` to the proxy's path list.
Details are in [`teams.md`](teams.md#private-and-shared-channels-optional).

### If the bot doesn't answer

- **Teams won't add the app and asks you to check that it's registered and
  the Teams channel is enabled:** the Azure Bot's Teams channel isn't on (step 2), or the
  manifest's `id` and `botId` don't match the client ID.
- **No answer anywhere, and nothing in `docker compose logs teams`:** the
  messages aren't reaching Daimon. Check the messaging endpoint (step 4),
  including the `/api/messages` suffix, and that `/healthz` answers from
  outside your network.
- **1:1 chats work but channel @mentions don't:** if the app was added to the
  team before the messaging endpoint was right, the team never linked up.
  Remove the app from the team (team → ⋯ → **Manage team** → **Apps**) and
  add it again. Check, too, that the mention was picked from autocomplete.
- **A Reply on a bot message gets no answer in a private channel:** Teams
  sends the bot only the private channel posts that @mention it. Mention it.
- **Every Daimon process fails at startup after adding Teams:** a
  `DAIMON_TEAMS__` value is missing, or `DAIMON_TEAMS__ADMIN_TEAM_ID` isn't a
  group ID.
- **Nobody is an admin:** `DAIMON_TEAMS__ADMIN_TEAM_ID` is unset, or the app
  isn't installed in that team (`teams.admin_team.lookup_failed` in the logs
  gives the reason and status). After a failed read the last owners count for
  up to an hour; once no owner list has been read within an hour, nobody is
  an admin until the next good read. Re-adding the app restores the owners.
- **`teams.tenant_reconcile_failed` in the logs:** Daimon couldn't finish
  setting up your organisation, and it refuses turns until it does. The log
  line says why.
- **It stopped answering after months of working:** the client secret has
  probably expired. Create a new one (step 1), update `.env` and run
  `docker compose --profile teams up -d`, which also recreates `mcp`.

### Updating the app

Changes to the manifest only reach Teams in a new package. Bump `version` in
`manifest.json`, zip it again and upload it over the existing app in the
Teams admin center (the app → **Upload file**). Teams that already have the
app pick up the update, and a team owner may be asked to accept new
permissions. A package from before template version 0.6.0 has no `/`
commands, prompt starters, private messages to Daimon, group chats or
activity feed notices: rebuild it.

## Claude Code login mounts

Coding-agent clients such as Claude Code connect through the plugin in
[`plugin/`](https://github.com/pymc-labs/daimon/blob/main/plugin/README.md) instead of a per-agent token. It logs in via
Slack or Discord OAuth and reaches every Daimon install the logged-in person
belongs to. There is no Teams login yet.

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
`mpim:read`, `search:read`) so a Daimon reads Slack as the person asking and
never sees a channel they cannot.

A login reaches only workspaces where Daimon is installed and ready, checked
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

- The default `s3` backend uses the S3 API with SigV4 and
  virtual-hosted-style addressing. Confirm that contract with your provider.
  Path-style-only endpoints, including default MinIO setups, are not
  supported.
- Objects remain private. Daimon never applies a public-read ACL.
- Presigned URL expiry does not delete stored objects. Configure a bucket
  lifecycle rule for the retention period your deployment requires.

For Azure Blob Storage, set the following on the MCP service:

```bash
DAIMON_ARTIFACTS__BACKEND=azure_blob
DAIMON_ARTIFACTS__ENDPOINT_URL=https://<account>.blob.core.windows.net
DAIMON_ARTIFACTS__BUCKET=charts
DAIMON_ARTIFACTS__AZURE_CLIENT_ID=<user-assigned-managed-identity-client-id>
```

`BUCKET` names the private Blob container for this backend. Provision the
container in advance and disable anonymous blob access on the storage account.
Assign **Storage Blob Delegator** to the managed identity at storage-account
scope, and **Storage Blob Data Contributor** scoped to the charts container
(or a custom container-scoped data role with read/write but no delete).
The account role permits generating a user-delegation key; the container role
limits blob data access to charts. Container-scoped data permissions alone
cannot mint the delegation key. No account key or S3 credential is needed.
`ManagedIdentityCredential` uses only `DAIMON_ARTIFACTS__AZURE_CLIENT_ID`, which
is required outside Azurite. There is no fallback to environment service
principals, CLI logins, or other developer credentials. `ENDPOINT_URL` must
be the account URL, without a container path, query, fragment or userinfo.
See Microsoft's
[user-delegation SAS guide](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-user-delegation-sas-create-python).

Blob links are HTTPS-only, read-only SAS capabilities for one blob, with the
same `DAIMON_ARTIFACTS__URL_TTL_SECONDS` lifetime (default 600 seconds, maximum
86400) as S3 links. SAS expiry does not delete the blob; configure an Azure
Storage lifecycle policy for retention. Download headers force attachments,
and SVGs are served as `application/octet-stream`.

For local Azurite only, set `DAIMON_ARTIFACTS__AZURE_CONNECTION_STRING` with an
explicit `BlobEndpoint` matching `DAIMON_ARTIFACTS__ENDPOINT_URL`, for example
`http://127.0.0.1:10000/devstoreaccount1`. Connection strings are rejected for
non-loopback endpoints, including Compose service hostnames such as `azurite`;
use a loopback endpoint on the host for local tests. Its account-name path is
the only allowed exception to the account-URL path restriction. Azurite uses
a shared-key service SAS because it does not support Entra user-delegation keys. Never set this option in Azure.
The optional Docker-backed test runs a disposable emulator:
`uv run pytest packages/adapters/mcp/tests/artifacts/test_azurite.py`.

Notebook and report hosts keep their own filesystem-backed data directories;
these settings only select the hosted MCP chart/artifact store. There is no
object-store listing or deletion API in this storage boundary.

See `.env.example` for the optional region, URL lifetime and image-embedding
controls.

## Connecting Notion, Linear and other MCP servers

Ask the agent to connect a server and it posts a card only you can open.
Adding a server to an agent other people use needs a server admin; anyone can
sign in to a server the agent already has. GitHub repos go through
[Connect GitHub](#github-app-optional), not a token.

- A server that takes a bearer token (Linear) gets a private token
  form. The token is checked against the server before it is stored and is
  shared by everyone who talks to that agent.
- A server that signs people in through a browser (Notion, Slack, Atlassian)
  gets a sign-in link. Daimon registers itself as an OAuth client (or uses
  one you registered, [below](#servers-without-dynamic-client-registration)),
  you approve in the browser, and the grant lands in your own vault, refreshed
  by Anthropic. Each person connects their own account.

The sign-in routes live at `{origin}/oauth/mcp/start` and
`{origin}/oauth/mcp/callback`, so `DAIMON_MCP__PUBLIC_URL` must be reachable
from a browser and `DAIMON_CRYPTO__KEYS` must be set. If one connection
fails, the agent still answers and names the server it could not use under
the reply. Ask it to disconnect the server or connect it again.

### Servers without dynamic client registration

Some authorization servers, Microsoft Entra ID among them, do not let Daimon
register itself, and the sign-in page says the server does not offer sign-in
Daimon can drive. Register a client by hand and list it in
`DAIMON_MCP_OAUTH__CLIENTS`, together with the MCP servers it may be used
for. A sign-in uses the client only when its MCP server URL is in that list
(compared exactly, ignoring case in the host, a default port and a trailing
slash) and the authorization server the MCP server names is on
`authorization_server_host`. Every other sign-in registers a client
dynamically, as before.

List only MCP servers you trust. A listed server decides which scopes the
sign-in asks for unless you set `scope`, and it receives the tokens, so it
can obtain anything the client's consented permissions allow. The list is
what stops any other server from borrowing the client: anyone who can start
a sign-in can point it at any URL. Without endpoint overrides, the
authorization and token endpoints the server advertises must be on
`authorization_server_host`, or the sign-in is refused.

For Entra ID:

1. In Entra, add an app registration. Under Authentication add a **Web**
   platform with the redirect URI `<root>/oauth/mcp/callback`, where `<root>`
   is `DAIMON_MCP__PUBLIC_URL` without the trailing `/mcp`.
2. Under Certificates & secrets, create a client secret and copy its value.
3. Under API permissions, add the delegated permissions the MCP server
   documents, then grant admin consent, or leave consent to each person if
   your tenant allows it.
4. Configure the client. A single-tenant app is refused at the
   `/organizations` endpoints MCP servers usually name (AADSTS50194), so
   point both endpoints at your tenant. Entra also rejects a `resource`
   parameter that does not match the resource the requested scope belongs
   to, so turn it off:

```bash
DAIMON_MCP_OAUTH__CLIENTS='[{"authorization_server_host": "login.microsoftonline.com",
  "mcp_servers": ["<mcp server url>"],
  "client_id": "<application id>", "client_secret": "<secret value>",
  "authorization_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/authorize",
  "token_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token",
  "send_resource": false}]'
```

If the MCP server publishes no scopes, set `scope` to the space-separated
scopes to request (for Entra, usually `<application id URI>/.default`).
A configured client also asks for `offline_access` when the authorization
server offers it, so the connection refreshes on its own. The client secret
is copied into each stored connection for those refreshes: after rotating
it, people connect again. Sign-ins through dynamically registered clients
ask for exactly the scopes they did before.

Left empty or unset, the setting changes nothing. A value that does not
parse stops Daimon at startup with an error naming the bad entry, like any
other setting. If the authorization server refuses a configured client (a
wrong or rotated secret, a redirect URI or scope it does not accept), the
sign-in page tells the person to ask the operator, and the reason is logged
as `mcp_oauth.callback_failed` or `mcp_oauth.authorization_declined`.

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

### Agent environment encryption

Agent environment values in `agent_files.content` are encrypted with
`DAIMON_CRYPTO__KEYS`, using the same MultiFernet key ring as other
credentials. Names and attribution remain readable. A deployment without keys
still starts and serves existing values, logs `agent_env.encryption_keys_missing`
at error level when the session factory starts, and refuses every agent key
write (the credential form and `request_agent_key` say so; nothing is saved).
For local development only, `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true` restores
plaintext storage and logs `agent_env.encryption_disabled` instead.

`daimon crypto verify` exits non-zero when keys are missing, any agent key is
stored in plaintext, or an encrypted key can't be decrypted with the current
keys (a key retired too early), listing the count per tenant (never names or
values).
`daimon crypto encrypt-plaintext` encrypts every plaintext row in place with the
first key, in one transaction, leaving timestamps and attribution unchanged.
Run it after enabling keys on a deployment that stored keys without them, and
only once **every** process (MCP, Discord, Slack, Teams, scheduler) has restarted with
the keys: a process still running without keys can't read the rows it encrypts,
and turns for those agents fail. Then run `verify` again. Add `verify` to your
onboarding checklist.

Stop old application readers and writers before upgrading through
`0028_agent_env_encryption`, since old readers cannot decode encrypted values.
With keys configured, the migration encrypts existing rows in one transaction;
without keys it adds storage metadata but leaves every value unchanged. The
migration uses a five-second lock timeout, so a
busy database fails the migration instead of waiting indefinitely. Retry during
a maintenance window. It requires an online database connection.

The separate `agent_files.encoding` column defaults to `plain` for legacy rows
and keyless writes. Encrypted rows are tagged `fernet_v1`; each write updates the
value and tag atomically. No text prefix is reserved: even literal values starting
with `enc:v1:` or resembling valid Fernet tokens round-trip unchanged. Store APIs
always accept plaintext user values. Migration retries encrypt only `plain` rows
and preserve existing `fernet_v1` ciphertext without double encryption.

A database trigger treats inserts and updates from old writers as `plain`, even
when they replace an encrypted row. New writers and the migration set the local
transaction marker `daimon.agent_env_writer = 'v1'` while writing explicit encoding
metadata, then clear it. This keeps old upsert/CAS writes readable by new code and
allows rollback during a mixed-writer window; it does not make old readers able
to decrypt new ciphertext. Stop old readers before keyed migration as described
above. Downgrade removes the trigger and its function after decrypting rows.
New code reads legacy `plain` rows, logs a warning containing only row identifiers
when encryption is enabled, and encrypts them on the next write. Wrong keys or
corrupt rows tagged `fernet_v1` fail closed. Wrong-key and corrupt-ciphertext errors
identify the tenant, agent and key name and point at `DAIMON_CRYPTO__KEYS`, never
the value.
The migration logs `agent_env.migration_keyless_noop` when keys are absent; keyed
rewrites log their action and row count. Do not remove keys while
encrypted values remain. When enabling keys after a keyless migration, rewrite
existing values through the application or re-run this migration's data upgrade
under maintenance, or run `daimon crypto encrypt-plaintext`; setting keys alone
does not rewrite existing rows.

For rollback, stop application readers and writers and run the migration's
Alembic downgrade with the original keys available before starting old code.
When this revision is the head, use `alembic downgrade -1`. The downgrade restores
plaintext in the same transaction and refuses to decrypt encrypted rows without
valid keys. It removes the encoding column only after successful decryption;
keyless plaintext-only downgrades leave the values unchanged.

To rotate keys, prepend a new key and retain older keys for reads. Rewriting a
decrypted environment value encrypts it with the first key. Do not retire old
keys until all stored credentials have been re-encrypted. Old backups, WAL, and
dead tuples can still contain plaintext. Normal VACUUM makes dead tuple space
reusable; it is not a secure erasure guarantee. Apply secret-retention controls
to backups and storage. Application encryption protects database-only access;
access to both the database and the key ring permits decryption.

## Managed staging image promotion

The managed staging deploy publishes canonical short-commit tags only after its
deployment gates pass. It tags notebook-host and report-host first, then Daimon
last; the Daimon tag marks the tested set as ready for promotion. A rerun accepts
an existing tag only when it points to the same tested digest. If the tag points
elsewhere or the registry cannot be checked, the deploy fails. New tags are
created without replacing or deleting existing tags.

## Worker shutdown during deployment

For worker compose files, set `stop_grace_period: 90s` on Discord, Slack,
Teams and scheduler. This leaves time for the adapters' 60-second in-flight
drain and cleanup before Docker sends SIGKILL. When recreating containers, use
`docker compose up -d --remove-orphans --timeout 95` so the CLI timeout exceeds
the compose grace. The managed deploy fetches its worker compose asset from
GCS; its source lives in `pymc-labs/daimon-infra` and an operator uploads changes
with OpenTofu before the next app deployment.

For staging deployments using prebuilt CI images, the managed workflow registers
its SSH key while migrations and MCP deployment run. It requires successful key
setup on the notebook VM and, when running, the workers VM before refreshing
either VM. A failed or incomplete setup stops the deployment. Manual staging
deployments and production promotes perform key setup serially.

### Discord overlap during deployment

Recreating the Discord container stop-first leaves the gateway closed until the
new process has started (about 14 seconds), and a button or slash command used in
that gap gets no response. The managed deploy therefore starts the new Discord
container beside the old one (`docker compose up -d --no-deps --no-recreate
--scale discord=N+1 discord`), waits until it answers its liveness port, then
stops and removes the old container with a 95-second timeout, and only then
recreates the other services. Compose numbers the new container one higher than
the highest existing one, so the Discord container is not always
`daimon-discord-1`.

Only one Discord process may be connected to the gateway. The new process starts
fully, then waits on a gateway lease: a PostgreSQL session advisory lock taken on
the connection it already holds for worker ownership, with the holder recorded in
`discord_gateway_lease`. The old process keeps handling everything, including
button clicks for its in-flight turns, while it drains. It releases the lease
after it has closed its gateway, and the standby connects at once. If the old
process dies without releasing, the standby waits until that process's ownership
heartbeat has expired (the same bound the worker-ownership fail-stop uses)
before connecting. A standby that receives SIGTERM exits without touching the
lease. A holder that loses its owner connection and cannot retake the lease stops.
The first process to take the lease with no earlier holder recorded also waits for any Discord process from before the lease to exit, so the first deploy of this change still replaces the container stop-first. The deploy script falls back to stop-first for an image that lacks the lease, so a rollback to an older image behaves as it did before. To use the same pattern with your own deploy scripts, start the new Discord
container before stopping the old one and give it the same database.

Selfhosted Azure metering requires explicit deployment-to-model mappings and
operator-verified model tariffs in `DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS` and
`DAIMON_BILLING__SELFHOSTED__MODELS`. See [selfhosted Azure usage](billing.md#selfhosted-azure-usage)
for tariff histories, the runtime admission gate and durable snapshot, explicit
compute-cost attribution, and paginated pending-usage reconciliation.
