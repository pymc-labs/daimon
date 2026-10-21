# Daimon harness

A standalone `daimon-harness/0` service. It runs the OpenAI Agents SDK locally
against an Azure OpenAI deployment through `ModelAdapter`, with an opt-in
Codex app-server loop; it never imports the
Daimon host. Tracing to OpenAI is disabled and API retries are disabled so one
model request has one usage observation.

## Run locally

From the repository root:

```sh
uv sync --all-packages --all-extras
# Supply HARNESS_TOKEN and Azure credentials through your environment.
uv run --package daimon-harness python -m daimon_harness
docker build --platform linux/amd64 -f apps/harness/Dockerfile -t daimon-harness .
docker run --rm -p 8088:8088 --env-file /path/to/private/harness.env \
  --mount source=daimon-home,target=/home/harness daimon-harness
```

Run one worker per persistent HOME. `HARNESS_SESSION_MODE=single` is the
production default: the first session ID seen on a turn or event poll is bound
persistently under `.daimon`; other IDs return HTTP 409 `session_bound`, including
after restart. Existing multi-session state refuses single-mode startup. Deploy
one container per session using Azure Container Apps dynamic sessions with a
custom container, and route every call for that session to its sandbox identifier.
The service takes a process lease to prevent two workers operating on one journal.

`HARNESS_SESSION_MODE=multi` requires a `/proc` mount with `hidepid=2` or
`hidepid=invisible`, with `gid=60002`, UID switching, and shared scratch directories that
are not writable by any tool UID (including ACL grants). Unsafe configurations
refuse startup. Session UIDs are allocated persistently from 10001 through 60000;
exhaustion refuses further sessions. Each session has private `work` and `tmp`
directories: tool HOME and cwd use `work`, and TMPDIR uses `tmp`. A writable global
`/tmp` and visible process arguments cannot be used as shared-session boundaries.
The selected mode is logged at startup. Offline test mode may bypass the multi
mode platform checks only with its mandatory fake adapter and no credentials.

| Variable | Purpose |
| --- | --- |
| `HARNESS_LOOP` | `agents_sdk` (default) or experimental `codex` |
| `HARNESS_LIVE_PROBE` | Unset by default; `openai` is an explicit Luna-only paid probe override, never a production provider default |
| `HARNESS_CODEX_BINARY` | Codex executable path; defaults to `codex` on PATH, version 0.162.1 required |
| `HARNESS_SESSION_MODE` | `single` (default, persistent session binding) or `multi` (verified proc/tmp/UID isolation required) |
| `HARNESS_AUTH_MODE` | `bearer` (default) or `entra`; `foundry_gateway` refuses startup |
| `HARNESS_MCP_CA_BUNDLE` | Optional read-only PEM CA bundle for MCP HTTPS only; chart mount `/etc/daimon/mcp-ca/ca.crt`. Adds to system roots; certificate and hostname verification remain enabled. |
| `HARNESS_TOKEN` | Local/direct HTTP bearer secret; no default |
| `HARNESS_MCP_CRED_BINDINGS` | Required for operator MCP credentials: JSON list of `{name, origin, tenants}` objects |
| `HARNESS_DAIMON_MCP_URL` | Approved URL (normalized before comparison) for the reserved `daimon-mcp` server |
| `HARNESS_ALLOWED_CALLERS` | Required in Entra mode: permitted object IDs or Entra app IDs |
| `HARNESS_ENTRA_TENANT_ID`, `HARNESS_ENTRA_AUDIENCE` | Tenant-bound validation in `HARNESS_AUTH_MODE=entra` |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_VERSION` | Defaults to `2025-04-01-preview` |
| `AZURE_OPENAI_API_KEY` | Optional; absent means `ManagedIdentityCredential` |
| `AZURE_CLIENT_ID` | Optional user-assigned managed identity selection; captured then scrubbed |
| `IDENTITY_ENDPOINT`, `IDENTITY_HEADER` | Required together for managed identity; captured by the credential then scrubbed |
| `HARNESS_REQUIRED_ROLE` | Optional Entra application role required in addition to caller identity |
| `HARNESS_VERSION` | Image git SHA for the authenticated health response |

The request's `model` is the Azure deployment name. MCP `headers_env` values
are operator credential references, captured in memory before connecting. All
tools default to automatic execution; `tool_policy: {"write": "ask"}` requests
approval for a named tool (MCP names use `server/tool`). `deny` hides and refuses
a tool. Exact keys override `server/*`, which overrides the default `auto`. Action decisions are explicit: `allow` executes the tool, `deny` refuses it,
and `result` substitutes host-executed JSON (including null). Only `result`
may carry a result field.
The reserved `daimon-mcp` server references `DAIMON_MCP_AUTHORIZATION` as a
virtual environment name. A fresh turn supplies its full bearer in
`X-Daimon-MCP-Authorization`; the service resolves it in per-turn memory, never
in `os.environ`. Other servers cannot use that reference. The credential is
excluded from request bodies, replay claims, logs and history, and known token
values are redacted if an upstream tool echoes them. A replay needs no header.
The Foundry gateway's documented allowlist does not include this header. Deployment must verify forwarding live. If stripped, use direct ACA ingress for MCP
turns until a credential-exchange route is provided.

Startup credentials are captured in memory, removed from `os.environ`, and
Linux `prctl(PR_SET_DUMPABLE, 0)` blocks same-UID reads of harness environment
and memory through `/proc`. Startup refuses if this protection cannot be set.
The image starts as root, prepares private state, then runs the service as UID
10000 with only ownership, signal and UID/GID switching capabilities. Tool
subprocesses use a separate persistent UID per session (starting at 10001). Only that
session's `work` and `tmp` directories are writable under
the harness HOME; the journal is private and skills are read-only to tools.
Children receive only PATH, HOME (workspace), TMPDIR (private session scratch),
LANG and PYTHONUSERBASE under work.
They have CPU (up to the turn deadline, capped at 300 seconds), 4 GiB address
space, 1024 processes, 256 open files and 256 MiB output-file limits; core dumps
are disabled. Each command has a wall timeout (the turn deadline, capped at 300 seconds). Output
is bounded to 256 KiB with a truncation marker. Every command exit kills its process
group, and turn cleanup kills any remaining processes of that session UID.

Production refuses startup without UID switching. There is no same-UID
production fallback. `HARNESS_TEST_MODE=1` permits unprivileged offline tests
only with an injected fake adapter and no configured model/service credentials.
The model's file tools use isolated Python helper processes under the session
UID. Descriptor-relative traversal refuses symlinks at every component; open
file descriptors are checked for regular files and used for ownership changes.
FIFOs/devices are refused without waiting. Helpers have wall/CPU/memory limits,
and the SDK awaits these subprocesses without file-operation threads. Every turn
owns its tool workers, including invocations abandoned by the SDK. Cancellation
signals and joins them, then kills remaining session-UID processes before
`turn_ended` or successful cancellation acknowledgment. Cleanup waits are bounded;
if a worker cannot be joined, the session remains active and refuses another turn.
The journal refuses tool events for an ended turn. SDK/MCP logs,
including connection failures and tracebacks, are redacted against all active
turn credentials before handlers receive them.

## Wire and persistence

All `/v0/*` calls require bearer authentication. `POST /v0/turns` normally returns
SSE, or `202 {"accepted":true,"turn_id":"..."}` when the caller requests
`Accept: application/json`. Poll `/v0/sessions/{id}/events?after_seq=N` for all
subsequent records until `turn_ended`; polling also recovers a dropped stream.
Sequence numbers are per session and strictly increase. Repeating a key with
the same request replays records; conflicting key/turn payloads return 409.
Only one turn runs per session.

`$HOME/.daimon/turns.sqlite3` owns unique turn/key claims; each session's
`$HOME/.daimon/sessions/<id>/journal.sqlite3` holds its event log and conversation
history. Writes use SQLite WAL with synchronous FULL. All runtime SQLite operations run
on a serialized worker thread; indexed terminal state makes polls independent
of prior turns. `$HOME/sessions/<sha256(session_id)[:32]>/work` holds tool files
and the sibling `skills/<name>` holds skills (`SKILL.md` plus files). URL skills
are refused in v1; provide an inline bundle. Skill instructions are included in
the model instructions. Inline skill file paths cannot escape their directory.

Disconnecting a stream does not cancel a turn. Cancellation and deadlines stop
local work, including shell process groups. On restart, a previously claimed
unfinished turn is marked interrupted and replayed; it is never re-executed,
because a tool may already have changed an external system. Pending approvals
are interrupted on restart. A client must submit a new turn to continue.

Before model I/O, `model_request_started` is committed with SQLite FULL durability.
Its generated request ID is reused by exactly one measured usage or
`usage_unavailable` error record, including cancellation and restart recovery.
The driver can hold unmatched starts as unknown usage if the process crashes.
Successful model responses must include usage; one event is recorded immediately
per response. Missing usage fails the turn with `usage_missing`. A failed or
cancelled request with no measurement emits `usage_unavailable` with its request
identifier; no zero-token usage is invented. A hard crash between receiving a
response and committing usage leaves its request start for host reconciliation. Conversation
history includes encrypted reasoning items requested for stateless Azure Responses
continuity ([Azure reference](https://learn.microsoft.com/azure/cognitive-services/openai/quickstart#encrypted-reasoning-items))
and is committed at successful turn completion. User input and redacted tool
side-effect summaries are checkpointed during the turn, so interrupted turns
remain in future context. A context-window rejection retries once after removing
older history and compacting large tool outputs, with a `context_truncated`
notice; reasoning items and their paired calls/results from the current turn
are retained together.

## Foundry hosted-agent deployment

The image is Linux amd64, serves port 8088, and exposes `/readiness` for platform
health probes. Build it, publish it to ACR, then register an agent version using
the image digest and the Invocations 2.0.0 protocol. The Foundry project identity
needs image-pull permissions; the running agent identity needs model access.
The deployment reference specifies the container contract, protocol versions,
and environment injection. This image uses a custom FastAPI server rather than
a protocol-library-owned loop. [Container deployment reference](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent).

The gateway's Responses protocol accepts OpenAI `/responses` requests and owns
conversation/stream lifecycle. C3 is a custom protocol and uses Invocations:

```text
POST {project}/agents/{name}/endpoint/protocols/invocations
     ?api-version=v1&agent_session_id=<sandbox-id>
Authorization: Bearer <Entra access token>
Accept: application/json

{"c3":{"method":"POST","path":"/v0/turns","query":{},"body":{...}}}
```

`POST /invocations` dispatches the envelope to the same C3 handlers, preserving
JSON status and SSE content. For replay, wrap a GET path and `query:{"after_seq":N}`;
for approval/cancel, wrap the corresponding POST path. Every request must route
to the same Foundry sandbox. This container's downstream Azure OpenAI Responses
calls are separate from the gateway protocol.

As documented on 2026-10-20, hosted-agent regions are Australia East, Brazil
South, Canada Central/East, Central US, East US/2, France Central, Germany West
Central, Italy North, Japan East/West, Korea Central, North Central US, Norway
East, Poland Central, South Africa North, South Central US, South India,
Southeast Asia, Spain Central, Sweden Central, Switzerland North/West, UAE
North, UK South/West, West Central US, West Europe, West US/3. Model quota and
availability must be checked separately. [Protocols and regions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents).

For Invocations the sandbox ID belongs in the query string, not the C3 body.
The driver keeps that ID separate from its mux session ID. Foundry restores
`$HOME` and uploaded files after idle eviction; the default idle window is 15
minutes, configurable from 2–60 minutes, and inactive sessions expire after
30 days. A bearer token for `https://ai.azure.com/.default` authenticates gateway
calls. [Session routing, lifetime and authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions).

Live deployment gates remain: whether a client-chosen `sh-<hash>` sandbox ID
creates a session (the docs show server-created IDs reused later); whether
SSE pass-through and buffering; invocation request timeout; HOME ownership and PyMC import at the
selected CPU/memory tier. These have not been tested on Azure. Use JSON acceptance
plus polling as the baseline; no turn depends on a long-lived gateway stream.
Do not infer an HTTP timeout from the session idle timeout.

The gateway strips `Authorization`; unsigned `x-agent-*` headers can also be
forged by tool processes over loopback. `HARNESS_AUTH_MODE=foundry_gateway`
therefore refuses startup until a verified platform proof or network boundary
is available. The initial deployment uses direct ACA transport with private
ingress and bearer or Entra authentication. Foundry hosted deployment remains
pending verification of a secure gateway proof and UID switching.
[Gateway headers and runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract).

Choose ACA dynamic sessions with Azure Files mounted at HOME if the target
region/quota, image size/Python stack, gateway isolation, custom session ID or
concurrent polling requirements cannot be met by Foundry. Preserve the direct
C3 endpoints and the same image; verify persistence and cancellation in that
environment before using it for a tenant.

## Azure Container Apps session-pool routing

For `HARNESS_SESSION_MODE=single`, configure the environment's native
`HarnessConfig` with `session_pool: true`, `transport: "direct"`,
`harness_url` set to the existing pool's `properties.poolManagementEndpoint`,
`auth_mode: "entra"` and `auth_ref: "https://dynamicsessions.io"`.
The host obtains tokens for `https://dynamicsessions.io/.default`; grant its
identity Azure ContainerApps Session Executor scoped to that pool. Bind that
audience to the exact pool endpoint origin and permitted tenants in
`DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS`.

The direct transport appends `identifier=sh-<session hash>` to every unchanged
C3 path, including submit, actions, cancel and event polling. Other query
parameters and replay bodies are preserved. Reconstructing the driver derives
the same identifier. The transport resolves the credential for each attempt;
pool tokens and per-turn MCP credentials are never persisted in native state.
Ordinary direct routing remains the default.

These paths and the fixed token audience follow the
[ACA session-pool routing documentation](https://learn.microsoft.com/en-us/azure/container-apps/sessions-usage).
The host requires `DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS` to bind audience
`https://dynamicsessions.io` to the exact pool endpoint origin and tenant. An
unbound audience or different origin is refused before acquiring a token.

Pool authorization does not replace harness caller authentication. Configure the
container with `HARNESS_AUTH_MODE=entra`,
`HARNESS_ENTRA_AUDIENCE=https://dynamicsessions.io`, the pool tenant in
`HARNESS_ENTRA_TENANT_ID`, and the host principal's object or app ID in
`HARNESS_ALLOWED_CALLERS`. The pool bearer is an Entra token for that audience;
a harness left in shared-secret bearer mode will reject it with 401. The host
refuses this audience unless `session_pool=true`, and transports refuse empty
session IDs before sending a request. Verify live
that `Authorization` and `X-Daimon-*` reach the container before enabling
admission. If forwarding fails, keep admission disabled until the verified
per-session authentication alternative is implemented. Each sandbox is ephemeral:
use a long timed cooldown, and checkpoint/restore durable state before relying
on workspace persistence after sandbox destruction.
## Durable checkpoints for ephemeral sandboxes

Configure the harness with `HARNESS_CHECKPOINT_ACCOUNT_URL` (an HTTPS Azure
Storage account origin), `HARNESS_CHECKPOINT_CONTAINER` (an existing private
container), and optionally `HARNESS_CHECKPOINT_KEEP` (1–100, default 3).
Checkpointing requires `HARNESS_SESSION_MODE=single`. Grant the harness identity
Storage Blob Data Contributor on the checkpoint container. Provision the
container separately; the harness never creates cloud resources. Azure Blob
[encrypts stored data at rest](https://learn.microsoft.com/en-us/azure/storage/common/storage-service-encryption).

The service captures `ManagedIdentityCredential` before scrubbing the identity
environment. It requires the header-protected `IDENTITY_ENDPOINT` and
`IDENTITY_HEADER`; it never falls back to IMDS, workload identity files, account
keys or connection strings. The workspace exporter uses a dedicated signal identity with
a minimal environment; only the service can access the archive, journal and
storage client. If the live pool exposes only a tool-reachable session identity,
keep admission disabled until a host-minted, per-session SAS carrier exists.
Live authentication and identity isolation still require deployment verification.

Before any model I/O, the service writes a compact turn claim to
`sessions/<sandbox_id>/wal/<turn_id>.json` and updates it with each durably
journaled `model_request_started` before that request reaches the model. The
record contains the original request digest, a credential-free recovery identity,
request inventory and event high-water; prompts and skill bundles are omitted.
Write-ahead or sequence-reservation failure returns retryable
`durability_unavailable` before crossing the failed paid-work barrier. If no
reserved sequence is available, the service returns HTTP 503 rather than
allocating an unreserved terminal event. These small writes use a separate
worker from asynchronous workspace uploads. Restore loads remote claims newer
than the checkpoint; a known idempotency key replays an interrupted outcome and
never repeats tools or model calls. Measured usage in the remote inventory is
replayed with its original request ID, model and token counts. Only started
requests with no usage record become `usage_unavailable` for reconciliation.

Each `model_request_started` commits its remote write-ahead before replay exposes
it. After tool workers stop, every terminal turn (including cancellation) checkpoints the SQLite journal, history, turn/idempotency inventory
and `work/` to `sessions/<sandbox_id>/<seq>.tar.zst`. SQLite's backup API includes
committed WAL data. Upload failures are logged by stage and exception class and
mark the session not durable until a later successful snapshot. They never seal
polls, cancellation or later turns. A `checkpoint_warning` notice is emitted once
per session as a non-terminal native notice on the next new turn. A durable
pending marker retains late upload failures after the completed turn's reader
closes, including across a local process restart. SQLite snapshot capture runs on the
journal worker; workspace export, upload and retention run separately with one
in-flight attempt and one coalesced pending snapshot. Polls and new turns do not
wait for workspace uploads; sequence and inventory commits remain correctness
barriers. The manifest caps escaped UTF-8 bytes, records a skip count
and bounded sample, and shares workspace name validation with restore. Combined
archive limits degrade to journals only. Export staging stays service-owned on
the session filesystem. Old snapshots are pruned only after a successful upload.

In production the exporter has a dedicated real/effective/saved UID (60001,
outside the session tool range). Its filesystem UID and group are the session's
tool identity, so it can read exactly the same workspace files and cannot read
service secrets, journals or Blob credentials. A small trusted bootstrap starts
with UID 0 using the service's existing CAP_SETUID, sets these identities, then
clears every capability, disables dumpability and sets no-new-privileges before
opening workspace data. Its environment contains only PATH/LANG; only pre-opened
archive/staging descriptors cross the service-owned snapshot boundary. The tool
cannot signal or ptrace the exporter, and tool-UID cleanup cannot kill it. No
filesystem bypass capability or broader file permissions are granted.

An exporter killed or timed out is retried once immediately with its partial
output truncated. No checkpoint is published until export exits successfully.
Persistent failure keeps the durable pending marker; the next authenticated
request schedules another background attempt without delaying the request.

Cleanup broadcasts SIGKILL as the session tool UID, then enumerates only numeric
entries actually present in `/proc` and probes each PID's signal permission and
state. Work is proportional to present processes, independent of `ns_last_pid`
and PID allocation wrap; survivors above a wrapped counter are still checked.
Verified zombies cannot write and are left for their parent/tini to reap. A live
or unverifiable signalable process at the deadline fails cleanup and preserves
the I5 seal. Disappearance is confirmed with ESRCH, including ENOENT stat reads.

Enumeration requires verified proc mount visibility. With hidepid enabled, the
trusted cleanup helper alone receives supplementary group **60002** and the proc
mount must have `gid=60002`; ordinary tools and exporters never receive it.
A trusted `python -I` bootstrap starts at UID 0, then drops every root UID before
signalling: real UID is the session tool UID (Linux signal permission), effective
and saved UIDs are **100000 + session tool UID** (preventing tool ptrace and
cross-session verifier signals), with no capabilities,
non-dumpable state and no-new-privileges. The bootstrap opens no workspace data.
These reserved identities are outside the session UID/group range (10001–60000)
and own no workspace or credential files. It lets trusted cleanup see even non-dumpable
tool processes without giving tools cross-session visibility. Multi mode refuses
startup without `hidepid=2,gid=60002` (or `hidepid=invisible,gid=60002`); cleanup
fails closed if it cannot verify proc visibility. Single mode normally uses
`hidepid=0`; a hidden proc mount needs the same verifier exemption.

A dedicated cgroup v2 leaf with `cgroup.kill` would avoid proc visibility
requirements, but the cached production image's cgroup mount is read-only and
has no delegated subtree. ACA delegation has not been verified live, so this
implementation uses the proc fallback and does not depend on writable cgroups.
Operators must supply the proc mount options before service startup; the service
does not acquire mount capabilities. CI configures the multi-mode mount as root,
then drops privileges before running tools. The production-image probe also
burns 256008 real PIDs (`HARNESS_PROBE_PID_BURN=1`) and verifies subsequent tools,
cleanup, checkpoint publication and restore.

An authenticated first turn, poll, action or cancel restores the latest snapshot
when local state is empty. An interrupted restore retries before serving.
Corrupt snapshots and conflicting local workspace state fail closed. Journal
sequence numbers, history and replay keys continue without re-executing a turn.
Every request includes `after_seq`, seeded from the host's persisted session
projection, including after a host process restart.
An ahead-of-local client cursor is accepted even when remote storage is entirely
empty (no checkpoint, WAL or reservation). It creates a durable restore fence
and a covering history gap above that cursor; subsequent stale-cursor submits
continue normally. Missing bytes cannot be recovered, but sequences are never reused.
Restore durably places its next event above the checkpoint, remote reserved
high-water and client cursor before publication. An abandoned reservation emits
`history_gap` with reason `sandbox_restored`, including when the checkpoint covers
the client's cursor. Sequence safety never depends on a fresh client cursor. Recovery then repairs
in-flight turns. The driver normalizes this to an authoritative mux history gap
and reconciles usage. Sequence numbers remain monotonic across lost sandboxes.
An unfinished restored turn ends as interrupted; every started request without
usage becomes `usage_unavailable`, preserving unknown usage for reconciliation.
A completed request whose usage missed both the latest checkpoint and remote
write-ahead stays unknown; no zero-token observation is fabricated.

Every transport call requires an explicit persisted `after_seq` high-water,
including cleanup cancellation; the transport never defaults it to zero.
Every C3 request carries the nonsecret `session_id` query parameter. For cold
actions/cancel it is required when checkpointing is enabled, and must match the
sandbox's binding and the turn's session. For turns and polls it must match the
body/path. The driver preserves existing query parameters and sends this context
through both direct and invocation transports. Older harnesses ignore it.

Workspace snapshots support regular files and directories, up to 512 MiB and
50,000 entries and depth 32. Unsupported entries (links, FIFOs, sockets,
devices and unreadable files) are skipped. Limits produce a degraded workspace
snapshot with journals preserved; the manifest records skipped paths/reasons
(up to 100 samples, plus the full skip count). Files keep owner execution only
(0700 for executables, 0600 otherwise); special permission bits are stripped. Extraction
rejects traversal and links, verifies file digests and SQLite integrity, and
publishes the workspace while still service-owned before handing it to the tool UID. Skills and tmp
files are regenerated and are not durable workspace state. Use a long timed pool
cooldown (for example, four hours) to reduce churn; checkpoints provide recovery
when Azure destroys a sandbox.

Blob access follows the Azure SDK's
[upload](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-upload-python)
and [download](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-download-python)
APIs. The harness also runs without checkpoint configuration for local persistent
volumes; ephemeral production pools must configure and verify recovery before
admission is enabled.

## Tests

```sh
uv run pytest apps/harness
```

The data extra pins `pymc-extras` 0.7 because 0.8 imports symbols absent from
the locked PyTensor version. A CI import test exercises the combined stack.

Production Azure templates default `AZURE_OPENAI_DEPLOYMENT` to `gpt-6.1-sol`;
the harness executes the deployment named by each turn request.

The default suite uses a fake adapter and a mocked Azure HTTP transport under
the real Agents SDK. The live Azure test is skipped unless `AZURE_OPENAI_ENDPOINT`
and either `AZURE_OPENAI_API_KEY` or `AZURE_CLIENT_ID` are set; it uses
`AZURE_OPENAI_DEPLOYMENT` (test default `gpt-6-luna`) and incurs model usage.
The live test refuses any deployment other than `gpt-6-luna` (C12). It also
checks the Responses model echo before the SDK continues, accepting Luna or
a dated Luna version and aborting on another model with the distinct,
non-retryable `c12_model_mismatch` probe error. SDK retries are disabled.
The echo check happens
after the first request: never alias another model as `gpt-6-luna`.
For cheap smoke tests, deploy `gpt-6-luna` version `2026-09-22` and set
`AZURE_OPENAI_DEPLOYMENT=gpt-6-luna`. The harness uses each request's model
as its deployment name. See [Azure model selection](../../deploy/azure/README.md#model-selection)
for SKU, region and billing configuration.


MCP environment references must start with `HARNESS_MCP_CRED_`; the sole
exception is the in-memory `DAIMON_MCP_AUTHORIZATION` reference reserved for
`daimon-mcp`. Unscoped process variables are rejected before resolution.
Credential-bearing servers require HTTPS, except literal loopback addresses or
`localhost`. No private HTTP opt-in authorizes MCP credentials. In-cluster
MCP uses the chart's HTTPS Service and CA: set
`HARNESS_MCP_CA_BUNDLE=/etc/daimon/mcp-ca/ca.crt` and mount that PEM read-only.
Missing, invalid or untrusted CA material fails closed; verification is never
disabled. This setting affects only the MCP HTTPX client, including the Codex
loop's shared MCP tools, and adds to system trust. It does not alter Azure
OpenAI transport. Redirects stay disabled.
Daimon MCP URLs are compared after normalizing host/default port
and trailing slashes; a different server name cannot alias the reserved URL.

Inline skills support UTF-8 text and base64 binary files with a verified SHA-256
digest (canonical encoding documented in the mux README). The harness rechecks
8 MiB per-skill and 24 MiB per-turn decoded limits, stages complete bundles under
the session's `skills` directory, and records digests under its private
`$HOME/.daimon/sessions/<id>/skills` directory. An unchanged
digest skips rewriting files; changed bundles replace all previous files. Seed
names containing underscores remain valid. Symlink roots and traversal paths
are refused. Message deltas and their final snapshot share a session-unique
`message_id`.


Every operator MCP credential must have an entry in `HARNESS_MCP_CRED_BINDINGS`:
`[{"name":"HARNESS_MCP_CRED_SAMPLE","origin":"https://tools.example","tenants":["11111111-1111-1111-1111-111111111111"]}]`.
The harness captures this list at startup and refuses unbound names or a server URL
whose normalized scheme, host or port differs. Paths may vary within the bound
origin. Operator bindings require an exact HTTPS origin.
Tenant UUIDs are checked by the host builder; `["*"]` is for single-tenant installs
only. Provision host and harness lists from the same operator source. The virtual
Daimon credential continues to use its separate reserved-URL binding.

Multi-session startup scans writable storage mounts for shared directories
writable by tool identities (including `/tmp`, `/var/tmp`, `/dev/shm`, and
`/run/lock`/`/var/lock`). Writable mode bits or ACLs refuse startup; provision
these locations without shared write access. Tools use their private `TMPDIR`.
At command and turn completion, cancellation, and timeout, a helper under the
session UID kills all that UID's processes, including detached descendants,
without relying on procfs visibility. It retries until `ESRCH` or a bounded
retry deadline (Linux may return success when only inaccessible UIDs remain). Pipe draining and process waits are bounded.
The container runs tini as PID 1 to reap orphaned descendants.

Log redaction runs at handlers after SDK logger filters, preserving their header
masking and interpolation arguments. It covers startup secrets (including Azure
API keys and harness bearer tokens), operator MCP credentials, per-turn MCP
credentials and acquired Azure bearer tokens. SDK debug logging uses the same
redaction path. Cancellation waits for protected journal finalization and cleanup
before acknowledgment, including when it arrives during finalization itself.

Async Azure credentials use the image's explicit `aiohttp` dependency. CI starts
the production service without an API key and obtains a token from a loopback
managed-identity endpoint stub through the real Azure async credential transport.
Command cleanup also covers cancellation while subprocess creation is pending:
creation is shielded, and cancellation waits for the child to be registered,
terminated and reaped before returning.

Model authentication accepts an API key or `ManagedIdentityCredential` only.
Managed identity requires ACA's header-protected endpoint; IMDS-only and developer
credential chains are refused. File-backed identity sources, certificate paths,
and mounted Kubernetes/workload token paths refuse startup even with an API key.
Startup parses `/proc/self/mountinfo` and checks every external mount for
tool-readable regular files, regardless of its name or read-only flag (including
traversal permissions and ambiguous ACLs). Only the image root, container network
configuration, system filesystems at their system paths, and isolated session
directories are exempt. Persistent HOME mounts are checked outside `sessions/`.
Credential caches are checked separately. Do not mount credentials
into the tool filesystem. The image probe verifies refusal with a projected 0644
token before the service drops its startup UID.

Incomplete Azure responses preserve their measured usage and partial text but end
with outcome `errored`, kind `model_incomplete`, and the provider's reason (or
`unknown` when absent). All other non-completed statuses, including failed,
cancelled, queued, in-progress and missing statuses, also end errored with kind
`model_failed` and the provider error code/message when available. These responses
never execute tool calls. CI also binds a synthetic 0644 `/mnt/model.env` to
verify that generically named credential mounts refuse startup.

The offline `tests/test_interop_driver.py` suite connects the real mux driver to
uvicorn on an ephemeral loopback port. It covers measured usage, tool writes,
approval execution, cancellation, cursor recovery after a dropped reader,
reconstructed-driver idempotent resend, a fresh driver opening a stream before
the first send, and narrowed built-in/MCP permissions.
The MCP case uses a local HTTPS server and the real Agents SDK with a synthetic
Azure response client; it checks the model's tool definitions and deliberately
requests a denied tool to prove refusal. These tests run in the harness CI job.
At each synthetic model I/O boundary it checks the durable HTTP log already
contains exactly one new request start, with one matching usage record per
completed request. Recovery gates approval emission until the reader closes and
compares the entire recovered event suffix against the durable journal.

## Codex loop spike

`HARNESS_LOOP=agents_sdk` is the default. `HARNESS_LOOP=codex` selects the
open-source Codex app-server loop; invalid values refuse startup. The image
includes the linux/amd64 Codex 0.162.1 binary from the upstream release, verified
by the SHA-256 in the Dockerfile. Local runs need the same version on PATH, or
an absolute `HARNESS_CODEX_BINARY` path. Initialization checks the version.
The upstream Apache-2.0 LICENSE and NOTICE are in `licenses/` and the image's
`/usr/local/share/licenses/codex/` directory.

The Codex provider uses the Responses wire API through an authenticated
loopback relay. The relay sends requests to
`$AZURE_OPENAI_ENDPOINT/openai/responses?api-version=$AZURE_OPENAI_API_VERSION`,
sets the request's Azure deployment, disables storage and redirects, and
acquires authentication from the harness's captured API key or managed identity.
Azure, harness and MCP credentials never enter Codex config or environment.
Only a fresh relay capability appears in a service-owned 0600 file under a
private 0700 temporary directory. The listener binds 127.0.0.1 and compares the
capability in constant time; unauthenticated requests cannot start model I/O.
The directory is removed after controller cleanup.

A private minimal model catalog selects direct function tools and full Responses,
independent of Codex's bundled deployment metadata. Plugins, marketplace/tool
suggestions, host skill discovery and code mode are disabled.

Codex receives `harness_*` dynamic tools. Their implementations are the same
harness bash/read/write/edit and streamable HTTP MCP handlers used by the
Agents SDK loop. They enforce `auto`/`ask`/`deny`, exact MCP `server/tool` keys,
`server/*`, tool limits, host-supplied results, credential origin bindings,
redaction and tool UID isolation. Denied tools are omitted from model requests;
local rejection handlers refuse hallucinated denied calls. Native Codex
shell/exec/patch/environment execution is disabled and filtered at the relay;
any native call or native approval request fails closed. Codex's read-only
sandbox is additional protection; the harness UID boundary remains authoritative.
No MCP credential or upstream MCP server is configured in Codex itself.

For each upstream HTTP request, the relay awaits the wrapper's durable
`model_request_started` callback before acquiring a token or contacting Azure.
It records measured provider usage before releasing tool calls to Codex.
Function-call stream events are held until successful completion, so incomplete
responses never execute tools. Missing usage produces `usage_unavailable` and
an errored turn; no token totals are inferred from Codex notifications. Text
deltas stream through app-server with a turn-qualified message ID shared by
the final snapshot. Cancellation sends `turn/interrupt`, bounds controller
shutdown, reaps it, cancels relay handlers, and uses the existing wrapper's
protected tool cleanup and journal finalization.

The harness journal/history remains canonical. Each turn starts an ephemeral
controller and prepends saved Responses items at the relay, preserving tool
calls/results and reasoning without trusting Codex's local rollouts. There is
no native thread resume/export or cross-backend history migration guarantee.
The Agents SDK context-overflow retry is not yet implemented for this backend.
Remote WAL/checkpoint guarantees depend on the wrapper implementation; this
adapter does not add a separate durability mechanism.

Offline validation (all model fixtures use `gpt-6-luna`):

```sh
HARNESS_LOOP=codex uv run pytest apps/harness
HARNESS_LOOP=agents_sdk uv run pytest apps/harness
# Root subprocess only; drops the service to UID 10000 and tools to UID 10001.
sudo python apps/harness/tests/codex_relay_probe.py
```

Tests that exercise codex-rs itself skip when the binary is absent locally.
CI installs the checksum-verified 0.162.1 binary in the harness test job and
runs `codex --version` plus the running-controller tool-UID relay probe in the
image job. The general harness/driver tests
continue to inject their existing fake adapter. Codex-specific tests additionally
run the real binary against synthetic Responses SSE and a local HTTPS MCP
server: tool loops, approvals, denial, supplied results, durable request starts,
streaming message identity, cancellation, recovery, idempotent resend, history,
API-key/managed-identity capture and MCP credential redaction. The standalone
UID probe verifies that a real tool process cannot read the relay capability or
controller memory/environment and gets HTTP 401 when calling the relay.
No test needs an Azure resource or real model key. The optional live SDK test
uses `AZURE_OPENAI_DEPLOYMENT=gpt-6-luna` only and refuses every other value.

Production enablement remains subject to normal review and live Azure protocol
validation; leave the default on `agents_sdk` meanwhile. The image CI job builds the checksum-verified release stage.

Codex bundled skills and skill instruction injection are disabled explicitly.
The relay reconstructs user messages from authoritative C3 input and preserves
durable history verbatim. It records the stable ID of the controller's initial
user item and discards the injected user/developer preamble before it. It retains
compaction prompts and replacement summaries after that item, with private-path
scrubbing before provider I/O and history. Native in-turn compaction stays enabled;
older harness history still needs separate compaction (F55).
Marker-prefixed user text remains intact. Harness-provided skills remain
available through C3. Denied calls count toward `max_tool_calls`;
ordinary model requests and internal compaction requests each have a separate
`min(max_tool_calls + 2, 128)` cap. Both are metered; compaction requests do not
consume ordinary continuation slots.
Only the harness-owned Responses request fields and reasoning effort/summary
cross the provider boundary. Shutdown joins usage commits before turn end,
including when cancellation and the deadline overlap. Cancellation or deadline
expiry during controller shutdown forces a prompt reap and ends interrupted.

### Paid Luna probes

`HARNESS_LIVE_PROBE=openai` with `HARNESS_RUNTIME_MODE=probe` is an explicit
test-only override for either loop.
It captures `OPENAI_API_KEY` at startup and uses only
`https://api.openai.com/v1/responses`. Every probe turn must select `gpt-6-luna`;
other models are refused before I/O. An OpenAI key alone never selects this
provider. It also requires `HARNESS_RUNTIME_MODE=probe`; startup refuses the
override in production mode (the default) or if any `AZURE_*` or `IDENTITY_*`
configuration is present. Startup logs the selected provider. Leave both probe
variables unset for Azure deployments.

`tests/live_luna_probe.py` runs the real authenticated harness HTTP service,
local MCP, service UID 10000 and tool UID 10001. It audits the actual provider
usage alongside C3 inventory and saves sanitized events without headers or
keys. Run it only in an isolated root subprocess with a Python installation
and the pinned Codex binary accessible after the UID drop:

```sh
set -a
source ~/.config/daimon-nc/openai.env
set +a
HARNESS_LIVE_PROBE=openai sudo --preserve-env=OPENAI_API_KEY,HARNESS_LIVE_PROBE \
  /opt/venv/bin/python apps/harness/tests/live_luna_probe.py \
  --binary /usr/local/bin/codex --output apps/harness/tests/live
```

The build-host probe scopes credential-file scanning to its private scratch
root to avoid traversing unrelated Docker overlays. It retains real UID
separation, environment scrubbing and non-dumpability. This does not validate
the production image's mount layout; run the existing image probes in CI.
The comparison and measured results are in `CODEX-SPIKE.md`.


The long-turn compaction probe adds `--backend codex --scenario compaction` to
the command above. It runs 14 distinct large `seq` commands sequentially, verifies
exactly-once execution and observed checkpoint compaction, and saves measured
provider usage plus output digests in `codex-compaction.json`.

Event replay may include `replay_after_seq` to read an older range while
`after_seq` still carries the highest durable client cursor for restore fencing.
This lets a resumed invocation replay its saved baseline without reusing event
sequences after sandbox loss.

Graceful shutdown allows five seconds for pending checkpoint uploads and closes
the shared model/storage identity only after storage calls finish. A stalled
upload cannot hold process exit; its cleanup retains the credential and log
redaction until the call unwinds. Restore retains acknowledged remote usage at
its original sequence rather than publishing it again above the client cursor.

The journal persists `max_client_cursor` before acting on every authenticated
session request. Polls, turns, approvals and cancels all raise the same ratchet;
a stale request never lowers it. Every new event uses the greater of the next
local sequence and `max_client_cursor + 1`. A jump first commits a `history_gap`
for the skipped range, in the same transaction as the append. Polls announce a
pending gap even when no turn is active. The ratchet survives process crashes
and checkpoints; restore carries it into the replacement journal before repair.
`replay_after_seq` selects only the read range and never changes the ratchet.
Remote WAL inventory is imported with its original event sequence and identity.

`tests/test_sequence_ratchet.py` runs 100 Hypothesis traces, each with 8–24
random operations plus first-turn and final recovery checks. Its oracle records
only HTTP events the host actually saw; request cursors remain independent.
Stale-first restore, cold cleanup cancel, first-turn loss without a checkpoint,
failed reservation writes, approvals, turns, cancels and concurrent requests
check immutable sequence/body identity, terminal-or-gap coverage and one usage
outcome per observed request. No cloud or live model is used.

### Remote sequence reservations

With Blob durability enabled, the journal reserves sequence blocks of 1024 in
`sessions/<sandbox_id>/reservations/<reserved_high_water>.json` before allocating
outside its current block. On sandbox restore, the next event is above the
checkpoint, every abandoned remote reservation, and the persisted client cursor.
A covering `history_gap` announces the abandoned range, even for a stale first
poll or cold cleanup cancel. WAL inventory retains its original event bodies and
sequences; recovery outcomes use the new block. Client cursors remain an extra
fence and `replay_after_seq` still only selects the replay range.

Model starts and usage outcomes commit to the remote WAL before replay exposes
them. A lost upload acknowledgement leaves the same event in a local pending
inventory record; replay withholds it until retry confirms the remote commit.
Measured usage is retained through that failure and never replaced by unknown
usage. Reservation/inventory failure returns retryable `durability_unavailable`
and prevents paid work from crossing the failed barrier.

The CI guard in `tests/test_sequence_ratchet.py` uses only host-visible HTTP event
responses as its oracle, independently of request cursors. Its 100 generated
traces exercise stale-first recovery, cold cancel, first-turn crashes with no
checkpoint, failed reservation writes, and turn/action/cancel interleavings.
Mutation check: copy the harness package to a private temporary directory, change
`Journal._next_seq()` from `max(local_next, floor, cursor + 1)` to
`max(local_next, cursor + 1)`, and run the random-interleavings test with that copy
first on `PYTHONPATH`. The explicit stale-first example must fail with
`host-seen sequence reused`; the production implementation must pass. Do not run
this mutation against the working source or commit it.


### Recovery order and invariants

Before paid I/O, commit the turn claim, sequence reservation, and request start
locally and remotely. Retain every measured response locally even if its next
sequence reservation fails. Usage and terminal events reach the remote WAL
before replay exposes them. Full workspace checkpoints remain best effort.

On startup, authenticate and persist the caller cursor, then load remote
reservations and WAL inventory **even when the local journal is populated**.
Restore an empty sandbox from its checkpoint; merge immutable WAL events at
their original sequences. Flush local pending inventory and measured responses
only after that merge. Then repair unfinished claims: close unmatched requests
as unknown, and append one interrupted terminal. A durable terminal skips repair.
The same ordering applies mid-process if the remote upload succeeds but the
local SQLite commit rolls back (for example, a full disk). The journal enters
**DIVERGED** state, including when committing pending inventory after a lost
acknowledgement fails. Appends, recovery and new write-ahead serialization refuse
with retryable `durability_unavailable` until the next authenticated request
re-runs the startup WAL/reservation merge. The seal clears only after the original
remote event identities and reservation fence commit locally. A failed merge
keeps the seal, including across reopening the local journal. A process restart
also always reconciles before repair, so the in-memory seal need not write to a
full disk.

WAL uploads run on the serial WAL worker and merge the stored record before
replacement. Inventory and high-water never shrink: measured usage dominates
`usage_unavailable`, original measured event bodies/sequences remain immutable,
and conflicting measured counts or claims refuse the update. This remote union
is an additional safeguard; it does not authorize exposing a divergent local
outcome before reconciliation.

Only after repair succeeds may a new turn be claimed. The claim's task owns
warning delivery and finalization; submit performs no fallible work between
committing a claim and registering its executor. Failed recovery is retryable.

- **I1:** A sequence the host has seen never receives a different event.
- **I2:** Each started request has exactly one usage outcome. Measured usage
  durable in the local journal, pending response, or remote WAL is never
  replaced with `usage_unavailable`.
- **I3:** Each claim reaches exactly one terminal; no turn event follows it.
  Session-level history gaps may follow a completed turn.
- **I4:** Every claim has an executor or is repaired on the next authenticated
  request. Failed warning delivery cannot leave an unowned claim.
- **I5:** No tool side effects after a turn's terminal; no new turn while prior
  workers live. A finished executor is not proof of cleanup. Retain its context
  and retry cleanup on authenticated requests; return retryable 503 while cleanup
  fails. Recovery never ends a turn with a retained context or a live executor.

Failed journal reopen and cursor re-recording return retryable 503. A failed
reopen is retried lazily on the next authenticated request.


The recovery property generator includes remote usage upload before local commit,
paid-response reservation failure, warning delivery at an exhausted block, and
partial recovery failure followed by retry, and a mid-process local commit
failure after remote usage upload (both normal and lost-ack commits). Its host oracle checks immutable
sequences, durable measured outcomes, one terminal with no later turn events,
and host-visible completion/admission after warning failures. A separate regression uses the real 1024
boundary for warning delivery. Remote uploads extend a measurement-only oracle;
they never advance the simulated host's event cursor.

Reproduce the nine isolated mutation checks from the repository root:

```sh
scratch=$(mktemp -d)
HARNESS_MUTATION_IMAGE=daimon-harness:l4-hardening \
  uv run python apps/harness/tests/recovery_mutations.py "$scratch"
```

The script reverts each fix separately in copied source, using the same Hypothesis
property (seed 7, including explicit regression traces). Expected pytest exit: 1
for mutations 1–6 and 8–9. Restoring startup repair before WAL reconciliation loses measured
usage; restoring submit-owned warning delivery strands a claim; removing pending
measured-response persistence loses measured usage; removing terminal WAL records
emits events after a terminal; disabling the divergence seal makes a mid-process
commit rollback replace the host's measured outcome with unknown. Mutation 2
asserts HTTP acceptance, a terminal in the host's poll, and successful admission
of the following turn, rather than inspecting executor ownership. The original
source remains untouched. Mutation 6 discards the failed executor’s context,
treating task completion as recovery permission; it fails with a clear I5
assertion on the host poll. The generator includes cleanup failure with live
workers, plus an explicit trace; a separate regression uses real cleanup timeouts.
Mutation 7 runs the production image overlap probe (Docker and a cached harness
image are required). It restores the exporter to the tool UID and must exit 1
with `exporter killed by tool cleanup (checkpoint data loss)`. The normal probe
suspends export after workspace bytes reach its pipe, overlaps a tool command and
UID cleanup, resumes export, and verifies the restored files and modes.

Cursors and generated events share the driver's `0..2**53` wire range. An
unseen cursor must leave space for a gap and a 1024-event reservation, or the
harness rejects it with 422 before persisting the ratchet. In particular,
`after_seq=2**53` and `2**53-1` cannot fence a session beyond the wire range.
Cursors from events actually emitted by this journal remain valid for replay;
reservation writes and event/gap allocation never exceed the wire maximum.

Mutation 8 restores the PID allocation-range scan; the
4-million-PID/wrap regression must reject it. Mutation 9 restores the empty-remote
cursor rejection; an explicit and randomly generated property operation must
fail with `empty remote cursor fence rejected`. The production PID-burn image
probe is enabled in CI's single-session run. Neither image probe calls a model.

The host-to-harness C3 request also carries the per-turn MCP credential. It must
use HTTPS or loopback HTTP when `X-Daimon-MCP-Authorization` is present. Generic
`allow_insecure_http` consent cannot override C3.9a; non-loopback HTTP turns with
MCP credentials are refused by the driver before any I/O. AKS uses HTTPS for
both hops with the chart CA.

## Kubernetes pod per conversation (F72)

Set `harness.mode: perConversation` in the lane-14 Helm chart. Apply
`deploy/helm/patches/per-conversation.patch` to PR #1273 at
`473a53a2ad0e315314c53d2615243ab46645a6f5`. The chart is not vendored here.
The host uses `https://<release>-daimon-harness.<namespace>.svc:8088`, direct
transport and `session_pool: false`. Host code and the MCP CA client integrate
with PR #1276. The controller preserves C3 JSON/SSE, cursors, MCP headers and
single-session routing, while replacing host authorization with a pod credential.

### Trust model (security redesign)

The host alone holds the deployment C3 bearer and MCP signing authority. It
connects to the controller on HTTPS 8088, verified against the chart CA. Only
same-release host pods may reach that port. The controller holds its HTTPS key,
a dedicated sandbox signing CA and client certificate, and a persistent random
capability derivation key on its private PVC. It never forwards the host bearer.

Sandboxes live in a separate `<release>-sandboxes` namespace. Pod Security is
`baseline`: root startup and CHOWN/DAC_OVERRIDE/FOWNER/KILL/SETUID/SETGID are
required for the existing service/tool UID separation, so `restricted` cannot
admit this image. RuntimeDefault, no SYS_ADMIN, no host mounts, no API token,
and no RoleBinding remain mandatory. Only explicitly selected AOAI/operator
MCP credentials are copied by the operator into that namespace; the host/harness
Secret, MCP signing secret and PostgreSQL credentials never enter it.

Each sandbox creates a private TLS key locally before starting tools. It sends
only a CSR and its downward-API UID to HTTPS 8089, verifying the chart CA.
The signer accepts only a UID and source PodIP already in the controller's
Kubernetes inventory cache, checks the CSR signature, and durably pins the first
public key for that UID. It issues a server certificate for that PodIP, never a
CA or client certificate. NetworkPolicy permits only the sandbox namespace on
8089. This bootstrap trusts Kubernetes PodIP assignment and the cluster network's
source-address integrity; node/CNI compromise is outside this boundary. Forwarded
headers are disabled. A later process cannot replace the pinned public key.

Controller→sandbox uses verified TLS and a dedicated controller client certificate
(mutual TLS). On claim, an internal bootstrap call delivers distinct per-pod C3
and checkpoint capabilities; the sandbox retains them in service memory only.
No capability or TLS private key appears in pod env/spec, labels, journal or logs.
The controller reconstructs capabilities from its private persistent key and pod
UID after restart. Pod-read access reveals public configuration only. MCP headers
are forwarded only after this verified channel is established. Sandbox→MCP uses
HTTPS and HARNESS_MCP_CA_BUNDLE=/etc/daimon/mcp-ca/ca.crt.

Sandbox→controller checkpoints use HTTPS 8089 and a per-pod capability scoped to
one bound session prefix. Authentication and per-capability rate limiting precede
all storage work and never call Kubernetes. Sandboxes cannot reach controller
C3 port 8088. Neither a checkpoint capability nor a sandbox C3 capability grants
host authority. Controller memory compromise exposes all active conversations;
a sandbox service compromise exposes that sandbox only. Tool UID separation
continues to protect service memory, TLS keys and credentials inside each pod.

The controller ServiceAccount has one Role, ONLY in the sandbox namespace:
`create` starts sandboxes; `delete` reaps using UID preconditions; `list` rebuilds
inventory; `get` refetches after resourceVersion conflicts; `patch` atomically
claims warm pods and records activity (and inherently permits image mutation
there). No watch, Secrets, exec, PVC, Service, node or cluster-wide verbs exist.
Helm/operator privileges create namespaces, fixed resources and credential copies.

Admission caps total sandboxes (including warm and terminating pods), default 4
on the D2s_v6 when replacing the shared harness. Warm pods count toward capacity and can be claimed at the cap.
Sandboxes have negative priority below host/Postgres/controller and never preempt.
Requests are controller 25m/128Mi and sandbox 50m/256Mi. Capacity measurements
must use these defaults and include the existing release's reserved resources.
Template hashes cover image, configuration, Secret data observed by Helm and an operator rotation revision;
stale warm pods are deleted before reuse; stale bound pods recycle once idle and checkpointed. Active turns are checked in the sandbox
before idle reaping, including after a controller restart. Maintenance failure
fails readiness. Session-specific locks serialize claims; no global lock spans
Kubernetes calls.

Checkpoint objects remain fsync + atomic rename + directory fsync before ack;
I1–I5 recovery logic above the store is unchanged. Listing visits only the requested
session directory. An operator-configurable idle retention (default 30 days)
expires unbound session directories; explicit authenticated session deletion removes
its pod and durable directory. Expiry/deletion forfeits resume and requires host
lifecycle coordination. Active/bound sessions are never retention-collected.
Readiness reports low disk and new admission returns retryable busy below
10% or 1 GiB; existing-turn events, cancellation and idempotent replay remain reachable. The single controller/PVC is an availability and backup boundary;
PVC loss remains data loss. HTTP has no production fallback on any credential hop.

### Operation and recovery

One Recreate controller holds a PVC flock. Kubernetes labels and UID/resourceVersion
preconditions retain single-flight routing across restart and warm-claim conflicts.
A 10-second maintenance loop retains a warm pool (default 1, configurable 0–2),
reaps idle sandboxes after `idleSeconds` (default 300), and expires unbound checkpoints after
`harness.perConversation.retentionSeconds` (default 30 days). `maxSandboxes`
(default 4) includes warm, active and terminating pods. Admission at capacity
returns 503 with a prompt “Busy, try again shortly” message and Retry-After.
With cap 1, a second user gets busy; at the default cap, a fifth does.
Removing the replaced shared harness and temporary proof pods from the original
D2s_v6 measurement gives 1547m baseline + 25m controller + 200m for four
sandboxes = 1772m of 1900m allocatable CPU; memory requests also fit.
This is a requests budget, not a concurrent model throughput guarantee. Increase the cap and warm count only with a measured
node budget; large PyMC workloads need larger requests and limits.

The sandbox namespace defaults to `<release>-sandboxes`; the operator supplies
`harness.perConversation.sandboxSecret` there with ONLY AOAI/operator MCP keys.
The controller Role has no Secret access. Do not copy the deployment harness
Secret. On in-place credential rotation, run Helm upgrade with a changed
`rotationRevision`; Helm also hashes the observed Secret data into the pod
template. Explicit rotation revisions cover offline renders where lookup cannot
observe the live Secret. TLS CA keys remain in the release namespace; sandboxes
mount public bundles only. Baseline admission is required for UID separation.

`DELETE /v0/sessions/{session_id}` with host authentication deletes the sandbox;
retry 503 until its deletion completes, then the durable directory is removed.
The host's conversation lifecycle must call this endpoint on deletion. Otherwise
idle retention expires it. Retention is the documented point after which an
abandoned conversation cannot resume. Back up the PVC for longer retention.

Listing visits only the requested session directory, and missing-object deletion
is idempotent. Disk admission thresholds are `minFreeBytes` (default
1 GiB) and `minFreeFraction` (default 0.1). Existing remote WAL/reservation barriers
remain fail-closed before paid work. Checkpoint writes retain temp-file fsync,
atomic rename and parent-directory fsync before acknowledgment. Each sandbox activity/bootstrap probe has a four-second bound and a separate
HTTP client. After a fresh successful durability report, unknown activity is
evicted after three failed probes unless a later probe reported a pending
checkpoint. Pending checkpoints are recorded in the pod's
`daimon-checkpoint-pending` annotation and restored before maintenance starts.
After controller restart, all discovered bound pods remain protected until a
fresh `durable=true` report, including pods whose last known checkpoint succeeded. Pods reporting `durable=false`
survive idle, template and age eviction, including the 48-hour hard age; idle
activity polls retry their checkpoint. Readiness exposes
`checkpoint_blocked_sandboxes`, and maintenance logs `sandbox_eviction_blocked`.
After a successful checkpoint, idle pods recycle after 24 hours with a 48-hour
hard limit below the 7-day server certificate validity. A stale maintenance heartbeat
fails liveness and readiness; per-pod failures are logged and isolated. Checkpoints use a separate HTTPS listener on 8089, and
initial capability checks use the in-memory inventory before any storage/API work.
Checkpoint commits and deletes additionally take the session lock and recheck
live pod binding before disk changes, so a late upload cannot recreate a deleted
session.
Per-capability 100 requests/second limits return 429; store clients retry boundedly.

Run recovery against the controller HTTP store path (ASGI transport for deterministic
fault injection); kind/AKS validate the actual verified TLS listeners separately:

```sh
scratch=$(mktemp -d)
HARNESS_RECOVERY_STORE=http uv run pytest --basetemp="$scratch/pytest" apps/harness/tests/test_sequence_ratchet.py apps/harness/tests/test_checkpoints.py
HARNESS_RECOVERY_STORE=http uv run python apps/harness/tests/recovery_mutations.py "$scratch/mutations"
```

The recovery oracle preserves I1–I5, including stale-first restore, cold cancel,
first-turn loss, WAL-only recovery, reservation failure and measured usage identity.
Mutation 10 removes the reservation floor; store mutations remove file fsync,
atomic rename and directory fsync. The commit-order test detects those missing
barriers; it does not simulate a physical power failure. Kindnet does not enforce
NetworkPolicy; AKS negative probes establish the network boundary. Measurement
receipts live with the program's lane status, not in the public documentation.

Per-conversation MCP URLs are chart-owned and namespace-qualified. Remove the
short-name overrides written by #1273 register.sh or replace both with the
chart URL; conflicting overrides fail rendering. See deploy/helm/patches/README.md
for installation ordering and certificate rotation.
