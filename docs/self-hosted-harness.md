# Self-hosted harness

Run turns through a `daimon-harness/0` service that you operate, instead of
Anthropic, OpenAI or Gemini. See [model providers](providers.md) for how it
compares and how a channel selects it.

The `selfhosted.loop` profile gives each caller a separate persistent session.
It does not support steering, workers, session transfer,
memory stores or workspace export and import.

The `selfhosted.loop` profile runs replies through a separate `daimon-harness/0`
service that you operate. Each caller gets a separate persistent session. The
profile does not support steering, workers, session transfer,
memory stores or workspace export and import.

## Create the resource records

Apply migrations, then store an agent and an environment for an existing
tenant, and grant the channels that may use them:

```bash
daimon selfhosted init \
  --tenant-id <tenant-uuid> \
  --harness-url https://harness.example \
  --model <azure-deployment-name> \
  --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY \
  --channel discord:<channel-id> --account-id <account-id> \
  --input-usd <price> --output-usd <price> \
  --manifest /path/to/runtime.json
```

It needs only the database. It prints the agent and environment IDs, then the
settings to add to the environment: the admission flag, the harness auth
binding for the tenant and the model's rates. It writes one row per `--channel`
into the runtime file. Repeating it with the same configuration reuses the
resources and replaces the same rows, and a changed configuration is refused.
HTTPS is required outside loopback.

| Flag | Meaning |
| --- | --- |
| `--tenant-id`, `--harness-url` | Required. The tenant, and the harness origin. |
| `--database-url` | Required unless `DAIMON_DATABASE__URL` is set. |
| `--channel platform:channel-id` | Repeatable. Each channel gets a runtime file row. |
| `--account-id` | Required with `--channel`. The account the rows cover. |
| `--agent <agent-id>` | Repeatable. That agent may pick the model itself, in any channel. Pass it or `--channel`. |
| `--input-usd`, `--output-usd` | Required. Verified prices in USD per million tokens, with no defaults. |
| `--cache-read-usd` | Follows the input price when unset. |
| `--infrastructure-usd` | Your audited cost per request. Default `0`. |
| `--model` | The deployment name. Default `gpt-6.1-sol`. |
| `--name`, `--environment-name` | Default `daimon` and `default`. |
| `--system-file` | Read the system prompt from a file. |
| `--auth-mode` | `bearer_env` (default) or `entra`. |
| `--auth-ref` | A variable name, never its value. Default `DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY`. |
| `--transport` | `direct` (default) or `foundry_invocations`. The latter takes `--agent-name` (the Foundry hosted agent) and `--project-endpoint`. |
| `--allow-insecure-http` | Permit HTTP on internal names and private addresses. Those origins get no auth binding line. |
| `--packages-note` | A free-text packages note kept with the environment. |
| `--manifest` | The runtime file. Defaults to `DAIMON_TURN__PROVIDER_RUNTIME_FILE`. |

## Check admission and the runtime file

The printed lines cover these; this is what each does.

1. [`DAIMON_SELFHOSTED__ADMISSION_ENABLED=true`](configuration.md#daimon_selfhosted__admission_enabled)
   defaults to `false` and needs a configured harness runtime. Select the
   self-hosted agent and environment on each channel.
2. [`DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS`](configuration.md#daimon_selfhosted__harness_auth_bindings)
   declares each harness token as a JSON list of
   `{"name": "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY",
   "origin": "https://harness.example", "tenants": ["<tenant-uuid>"]}`.
   The exact origin and tenant must match before a token is read. Merge the
   printed entry into any list you already have. Wildcard tenants need
   [`DAIMON_SELFHOSTED__SINGLE_TENANT=true`](configuration.md#daimon_selfhosted__single_tenant).
3. The runtime file, pointed to by `DAIMON_TURN__PROVIDER_RUNTIME_FILE`, holds
   one row per tenant, channel and account:

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

   The digest is that of the default `selfhosted.loop` configuration for the
   model. A channel that changes its backend settings needs its own row.
4. `DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS` and
   `DAIMON_BILLING__SELFHOSTED__MODELS` hold the deployment-to-model mapping
   and the rates, as described in [billing](billing.md). A turn on an unpriced
   deployment is refused before any harness call.

Daimon's MCP public URL and JWT secret must be set. Each turn passes the
harness a caller-bound MCP token that is revoked when the turn ends. Provide
third-party MCP credentials to the harness as environment references. Wildcard
MCP credential bindings need `DAIMON_SELFHOSTED__SINGLE_TENANT=true`.
Credential-free HTTP MCP servers on private ranges are refused unless listed in
[`DAIMON_SELFHOSTED__MCP_ALLOWED_HTTP_HOSTS`](configuration.md#daimon_selfhosted__mcp_allowed_http_hosts).
Also restrict harness egress, since host checks cannot stop DNS rebinding.

## Run the harness container

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
  session and answers other IDs with 409. In either mode, a `/proc` mounted
  with `hidepid` must add `gid=60002`, or startup refuses it. Multi-session mode
  needs `hidepid=2,gid=60002` process visibility and shared scratch directories,
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

## Know the limits

- **Where it applies.** A channel pin, or an agent's model once `--agent`
  grants it; a channel pin outranks the agent's. Never a shared thread's agent
  choice. A shared
  thread is one session for everyone, without each person's own sign-ins. `ask` rules and
  publishing confirmations pause for Daimon's Approve card.
- **No memory, effort or spend limit.** A reply is bounded by the
  row's `max_tool_calls` and `deadline_s`. Routines do not run on it.
  Daimon's page-reading tool is available.
- **Connectors.** The agent's own connectors run through Daimon's relay, with
  the person's sign-in; the harness never sees it. A server with a
  `credential_ref` keeps the operator's own credential and is called directly.
  Outside a shared thread, the harness asks the person about `ask` rules itself;
  Daimon trusts the operator's harness to have asked and runs the call.

- **Skills.** Replies support only the seeded default skills. Custom uploads and
  channel-library additions have no provisioning path on this provider.
- **Agent edits.** Editing an agent applies to an open thread at its next
  message, on the same harness session. A reply already running finishes on its
  old configuration, and a message sent meanwhile is told to wait. A different
  environment, account scope or archived agent needs a new session.
- **Files.** Files the agent saves in its `outputs` folder are attached to
  its reply, then removed from the harness. Update the harness to 0.2.0 or
  later to get this; an older harness attaches no files. A file over 32 MB
  stays on the harness, and the reply says it was too large to attach.
