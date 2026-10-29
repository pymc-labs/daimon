# Model providers

Choose which model backend answers in your channels, and run OpenAI, Gemini or
a self-hosted harness instead of, or beside, the default Anthropic backend.
Read this if you self-host and do not want every turn on Anthropic.

## Compare backends

| | Anthropic (default) | OpenAI | Gemini | Self-hosted harness |
| --- | --- | --- | --- | --- |
| Needs | Anthropic API key | OpenAI key and project | Gemini key | Your own harness service and Azure OpenAI |
| Models | The agent's own model | `gpt-6-luna`, `gpt-6-astra` | `gemini-3.8-flash`, `gemini-4-argon` (priced through 2026-11-30, then refused) | The deployment you name (default `gpt-6.1-sol`) |
| Shared threads | Yes | No | No | No |
| Multi-agent delegation | Yes | No | Not documented | Not documented |
| Memory across turns | Yes | No | Not documented | No |
| Workspace export and import | Yes | No | Not documented | No |
| Skills | Full | Default set | Not documented | Seeded defaults only |

Everything else works the same on every backend: chat platforms, routines and
the default skill set. A backend that cannot meet a channel's requirements
refuses the turn. Daimon never runs it on Anthropic in its place.

## Route channels to a backend

With nothing configured, every channel runs on Anthropic using the agent's own
model. Selecting another backend uses its default model unless you pass
`--model`. Route in one of two ways:

```bash
docker compose run --rm init "daimon agents backend set <agent-name-or-id> openai"
```

This sends every channel that uses the agent to OpenAI. To route single
channels instead, use `daimon channels rule set` and scope the runtime file row
with `--platform`, `--channel-id` and `--account-id` on `daimon openai init`.

A channel's own pin outranks the agent's provider there. An admin can clear it
from chat by asking Daimon to clear the channel's pin and confirming the card.
Existing threads keep their model.

A refused turn names the cause in chat: the channel's pin, a tenant allowlist
that does not enable the provider ([enable it](#enable-providers-for-a-tenant)), or an agent whose provider cannot serve the
channel yet. Backend selection is on by default
([`DAIMON_TURN__CHANNEL_BACKENDS`](configuration.md#daimon_turn__channel_backends)).
Setting it to `false` sends every turn back to Anthropic, as an emergency
stop.

## Run on OpenAI

OpenAI needs no Anthropic account. Leave
[`DAIMON_ANTHROPIC__API_KEY`](configuration.md#daimon_anthropic__api_key)
unset and `init` skips the Anthropic defaults. Two limits apply beyond the
table: no mid-session resource changes (initial file mounts work), and fixed model and
container defaults with no Daimon model settings.

1. Create an OpenAI project and an API key scoped to it. Add the key to
   `.env` under a name outside the reserved `OPENAI_*`, `ANTHROPIC_*`,
   `GOOGLE_*` and `GEMINI_*` prefixes:

   ```bash
   DAIMON_CHANNEL_OPENAI_KEY=sk-...
   ```

   The runtime file records only this name, never the key.

2. Point the runtime at the file in `.env`:

   ```bash
   DAIMON_TURN__PROVIDER_RUNTIME_FILE=/app/runtime/openai.json
   ```

   The services already share `./runtime` as `/app/runtime`. `daimon openai
   init` reads the same variable, or takes `--manifest`.

3. Invite the Discord bot first: Daimon creates your server's tenant when
   the bot joins. `daimon tenants list` then shows its UUID. Provision the
   agent, environment and default skills:

   ```bash
   docker compose run --rm init "daimon openai init --tenant-id <tenant-uuid> --project <openai-project-id> --model <openai-model-id>"
   ```

   It creates one OpenAI agent and one hosted environment, uploads every skill
   under `defaults/skills/` and writes a runtime file row. Repeating it prints
   the existing IDs. Pass `--skip-default-skills` for a bare agent and
   `--api-key-env` for a different variable name. Then run
   `docker compose up -d` so the services read the new values.

4. Route channels as shown above, then verify:

   ```bash
   docker compose run --rm init "daimon openai smoke --project <openai-project-id> --model <openai-model-id>"
   ```

   The smoke test provisions its own agent, runs one billed turn that must call
   one tool, and exits non-zero unless it completes.

## Enable providers for a tenant

A tenant may use Anthropic plus every provider whose deployment key is set,
such as `DAIMON_CHANNEL_OPENAI_KEY` or `DAIMON_CHANNEL_GEMINI_KEY`. A key
under another name through `--api-key-env` does not count. Self-hosted
backends are never on by default. An admin's own choice always wins.

An admin changes the list in chat by asking Daimon and confirming the card.
On Discord, an operator can do the same from the CLI. Each call checks that
the named user is a current server admin, and other platforms refuse it.

```bash
daimon tenants backend-policy snapshot discord <guild-id> --actor-user <admin-user-id> --output before.json
daimon tenants backend-policy apply discord <guild-id> --actor-user <admin-user-id> --snapshot before.json --backends anthropic,openai,gemini --yes
```

`--dry-run` previews a change, and `--backends default` returns to following
the keyed providers. To undo, run `revert` with `--snapshot` and
`--expected-revision <applied-revision>`.

## Edit the runtime file

The runtime file is a JSON list that you own. Each row authorizes provisioned
resources for one tenant, and holds variable names, never credentials. A row
for OpenAI:

```json
[
  {
    "profile": "openai.persistent_workspace",
    "tenant_id": "<tenant-uuid>",
    "platform": "*",
    "channel_id": "*",
    "account_id": "*",
    "project": "<openai-project-id>",
    "agent_id": "<openai-agent-id>",
    "environment_id": "<openai-environment-id>",
    "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
    "skill_ids": []
  }
]
```

`platform`, `channel_id` and `account_id` accept `"*"`. A row with `"*"` on all
three covers every channel and account in its tenant, which is what
`daimon openai init` writes. `tenant_id` and `profile` never accept `"*"`, so a
row can never serve another tenant. A narrower row wins over a wildcard row.
Two equally specific rows, or rows naming different fields, are refused as
ambiguous. Unknown fields, a stale digest or a missing resource are refused too.

An optional `spend_limit_usd_cents` passes a spend cap to a verified OpenAI
project. The host refuses `OPENAI_CUSTOM_HEADERS` for configured OpenAI
runtimes so ambient headers cannot replace the channel's key or project.

## Run on Gemini

Gemini has no provisioning command. It needs the `daimon-mux[gemini]` extra,
which the published image does not include, so build an image that adds it.
With `DAIMON_CHANNEL_GEMINI_KEY` set, Gemini runs with no runtime file row.
Add a row to the runtime file, which can hold both providers, only to override
project, spend holds or storage:

```json
{
  "profile": "gemini.inline_reuse",
  "tenant_id": "<tenant-uuid>",
  "platform": "slack",
  "channel_id": "<channel-id>",
  "account_id": "<account-uuid>",
  "config_digest": "<admitted-config-digest>",
  "project": "<gemini-project>",
  "api_key_env": "DAIMON_CHANNEL_GEMINI_KEY",
  "agent_name": "<agent-name>",
  "system": "<system instruction>",
  "tools": [],
  "environment": {"name": "inline-workspace"},
  "records_path": "/var/lib/daimon/gemini-records.sqlite",
  "holds_path": "/var/lib/daimon/gemini-holds.sqlite",
  "hold_usd": "0.05",
  "cap_usd": "0.25"
}
```

- **Tools.** `tools` is required. `[]` sends an explicitly empty list, and a
  missing value is refused. Search, URL context, code execution and custom
  tools run only when listed.
- **Storage.** The two paths must be absolute, distinct and on persistent
  volumes, shared by every worker. For several hosts, set
  `"storage_backend": "postgres"` and omit both paths. This uses the
  application database and migration `0106_gemini_runtime_state`. Cloud Run
  refuses SQLite.
- **Spend.** Each attempt places a hold, an estimate against `cap_usd`.
  Holds release on a refusal, settle when usage is final, and otherwise expire
  after 24 hours.
- **Growth.** Records and holds have no size bound or compaction, so monitor
  database storage before raising load.

In a row, name the profile `gemini.inline_reuse` and the model
`gemini-3.8-flash`.

## Run a self-hosted harness

The `selfhosted.loop` backend runs turns through a separate `daimon-harness/0`
service that you operate. Each caller gets a separate persistent session. The
backend does not support steering, shared sessions, workers, session transfer,
memory stores or workspace export and import.

### Create the resource records

Apply migrations, then store an agent and an environment for an existing tenant:

```bash
daimon selfhosted init \
  --tenant-id <tenant-uuid> \
  --harness-url https://harness.example \
  --model <azure-deployment-name> \
  --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY
```

It needs only the database and prints the agent and environment IDs. Repeating
it with the same configuration reuses them, and a changed configuration is
refused. Override the names `daimon` and `default` with `--name` and
`--environment-name`, and read the system prompt from a file with
`--system-file`. `--auth-ref` records a variable name, never its value. HTTPS is
required outside loopback. `--allow-insecure-http` permits HTTP on internal
names and private addresses.

### Enable admission and the runtime file

1. Set
   [`DAIMON_SELFHOSTED__ADMISSION_ENABLED=true`](configuration.md#daimon_selfhosted__admission_enabled).
   It defaults to `false` and needs a configured harness runtime. Select the
   self-hosted agent and environment on each channel.
2. Declare each harness token in
   [`DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS`](configuration.md#daimon_selfhosted__harness_auth_bindings),
   a JSON list of `{"name": "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY",
   "origin": "https://harness.example", "tenants": ["<tenant-uuid>"]}`.
   The exact origin and tenant must match before a token is read. Wildcard
   tenants need
   [`DAIMON_SELFHOSTED__SINGLE_TENANT=true`](configuration.md#daimon_selfhosted__single_tenant).
3. Mount a runtime file and point
   `DAIMON_TURN__PROVIDER_RUNTIME_FILE`
   at it. Each row grants one configuration to one tenant, channel and
   account:

   ```json
   [
     {
       "profile": "selfhosted.loop",
       "tenant_id": "<tenant-uuid>",
       "platform": "discord",
       "channel_id": "<channel-id>",
       "account_id": "<account-uuid>",
       "config_digest": "<admitted-config-digest>",
       "agent_id": "sh_agent_<id>",
       "environment_id": "sh_env_<id>",
       "infrastructure_usd": "0",
       "max_tool_calls": 100,
       "deadline_s": 300
     }
   ]
   ```

   `infrastructure_usd` is required: enter your audited per-request cost, or
   `"0"` if you fund infrastructure separately.
4. Set deployment-to-model mappings and verified rates, as described in
   [billing](billing.md). A turn on an unpriced deployment is refused before
   any harness call.

Daimon's MCP public URL and JWT secret must be set. Each turn passes the
harness a caller-bound MCP token that is revoked when the turn ends. Provide
third-party MCP credentials to the harness as environment references. Wildcard
MCP credential bindings need `DAIMON_SELFHOSTED__SINGLE_TENANT=true`.
Credential-free HTTP MCP servers on private ranges are refused unless listed in
[`DAIMON_SELFHOSTED__MCP_ALLOWED_HTTP_HOSTS`](configuration.md#daimon_selfhosted__mcp_allowed_http_hosts).
Also restrict harness egress, since host checks cannot stop DNS rebinding.

### Run the harness container

Build it from the repository root:

```bash
docker build -f apps/harness/Dockerfile -t daimon-harness .
```

The image holds NumPy, pandas, PyMC and PyMC-Marketing. Port 8088 serves
authenticated `/v0/*` requests. Use private ingress with bearer or Entra
authentication.

- **Credentials.** Supply `HARNESS_TOKEN`, `AZURE_OPENAI_ENDPOINT` and either
  `AZURE_OPENAI_API_KEY` or a managed identity (`IDENTITY_ENDPOINT` and
  `IDENTITY_HEADER`, optionally `AZURE_CLIENT_ID`). Startup refuses
  file-backed credentials, certificate paths and tool-readable files on
  external mounts.
- **Storage.** Mount a persistent home at `/home/harness` and run one worker
  per home. Keep the image's tini process as PID 1 in custom entrypoints.
- **Sessions.** The default `HARNESS_SESSION_MODE=single` binds the first
  session and answers other IDs with 409. Multi-session mode needs
  `hidepid=2,gid=60002` process visibility and shared scratch directories,
  including `/tmp`, `/var/tmp` and `/dev/shm`, that tools cannot write. Startup
  refuses it otherwise.
- **Azure Container Apps pools.** Use the pool's management endpoint as
  `harness_url`, one container per session, and set the environment's
  configuration to `session_pool: true`, `transport: "direct"`,
  `auth_mode: "entra"`, `auth_ref: "https://dynamicsessions.io"`. The host
  identity needs the Session Executor role on the pool.
- **Checkpoints.** Ephemeral sandboxes need Blob checkpoints before you enable
  admission. Set `HARNESS_CHECKPOINT_ACCOUNT_URL` and
  `HARNESS_CHECKPOINT_CONTAINER` to an existing private container, grant the
  service identity Storage Blob Data Contributor, and use single-session mode.
  `HARNESS_CHECKPOINT_KEEP` defaults to three snapshots. Without a checkpoint
  a destroyed sandbox loses its files and history.

If a checkpoint fails, the next turn posts a one-time notice that messages or
files may be lost. Paid work is refused with a retryable error when the
write-ahead record cannot be saved. `HARNESS_LOOP=codex` selects an
experimental loop and is not for production. Variables and recovery
guarantees are in the
[harness README](https://github.com/pymc-labs/daimon/blob/main/apps/harness/README.md).

### Know the limits

- **Skills.** Turns support only the seeded default skills. Custom uploads and
  channel-library additions have no provisioning path on this backend.
- **Agent edits.** Editing an agent applies to an open thread at its next
  message, on the same harness session. A turn already running finishes on its
  old configuration, and a message sent meanwhile is told to wait. A different
  environment, account scope or archived agent needs a new session.
- **Files.** The automatic output sweep does not run. Agents deliver files
  through the MCP message tools.
