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
`hidepid=invisible`, no exempt proc group, UID switching, and shared scratch directories that
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
`localhost`. Daimon MCP URLs are compared after normalizing host/default port
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
The relay removes controller-authored skill/environment messages and private
controller paths before provider I/O and durable history. Harness-provided
skills remain available through C3. Denied calls count toward `max_tool_calls`;
the relay also caps model requests per turn at `min(max_tool_calls + 2, 128)`.
Only the harness-owned Responses request fields and reasoning effort/summary
cross the provider boundary. Shutdown joins usage commits before turn end,
including when cancellation and the deadline overlap.

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
