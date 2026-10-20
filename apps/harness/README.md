# Daimon harness

A standalone `daimon-harness/0` service. It runs the OpenAI Agents SDK locally
against an Azure OpenAI deployment through `ModelAdapter`; it never imports the
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
  --mount source=daimon-home,target=/home/agent daimon-harness
```

Run one worker and one session sandbox per persistent HOME. The service takes a
process lease to prevent two workers from operating on the same journal. A
shared worker/HOME is not a security boundary between tenants. The deployment
must isolate sandboxes and scope access to the correct sandbox.

| Variable | Purpose |
| --- | --- |
| `HARNESS_AUTH_MODE` | `bearer` (default) or `foundry_gateway` behind private platform ingress |
| `HARNESS_TOKEN` | Local/direct HTTP bearer secret; no default |
| `HARNESS_ALLOWED_CALLERS` | Optional comma-separated platform caller IDs allowed in gateway mode |
| `HARNESS_ENTRA_TENANT_ID`, `HARNESS_ENTRA_AUDIENCE` | Alternative tenant-bound Entra token validation |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_VERSION` | Defaults to `2025-04-01-preview` |
| `AZURE_OPENAI_API_KEY` | Optional; absent means `DefaultAzureCredential` |
| `AZURE_CLIENT_ID` | Optional user-assigned managed identity selection |
| `HARNESS_CONFIRM_TOOLS` | Comma-separated builtins needing confirmation; default `bash,write,edit`; `*` means all; empty means none |
| `HARNESS_VERSION` | Image git SHA for the authenticated health response |

The request's `model` is the Azure deployment name. MCP `headers_env` values
are environment variable names, resolved only when connecting. Every MCP call
requires confirmation. An explicit action `result` substitutes a host-executed
function result; an omitted result executes the approved tool locally.
Shell subprocesses receive a small environment without harness/model/MCP secrets.
The shell has the privileges of the container user; filesystem checks on other
tools are not an OS sandbox. Use VM/session isolation and restrictive network
policy for untrusted workloads.

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
history. Writes use SQLite FULL durability. `$HOME/work` holds tool files and
`$HOME/skills/<name>` holds skills (`SKILL.md` plus files). URL skills fetch a
single SKILL.md, not an executable archive. Skill instructions are included in
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
history is committed at successful turn completion; partial turns remain in the
event journal for reconcile.

## Foundry hosted-agent deployment

The image is Linux amd64, serves port 8088, and exposes `/readiness` for platform
health probes. Build it, publish it to ACR, then register an agent version using
the image digest and the Invocations protocol. The Foundry project identity
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

The gateway strips `Authorization`. Set `HARNESS_AUTH_MODE=foundry_gateway`:
this requires the injected project endpoint, agent name and sandbox ID plus
`x-agent-user-id` and `x-agent-foundry-call-id` on each call. Configure Entra RBAC
to grant invocation only to the Daimon host identity; optionally set
`HARNESS_ALLOWED_CALLERS` to its platform user ID. The caller ID is logged with
each submitted turn. This mode trusts private Foundry ingress and must never
be exposed directly to the public network. Missing platform context is refused.
[Gateway headers and runtime contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract).

Choose ACA dynamic sessions with Azure Files mounted at HOME if the target
region/quota, image size/Python stack, gateway isolation, custom session ID or
concurrent polling requirements cannot be met by Foundry. Preserve the direct
C3 endpoints and the same image; verify persistence and cancellation in that
environment before using it for a tenant.

## Tests

```sh
uv run pytest apps/harness
```

The data extra pins `pymc-extras` 0.7 because 0.8 imports symbols absent from
the locked PyTensor version. A CI import test exercises the combined stack.

The default suite uses a fake adapter and a mocked Azure HTTP transport under
the real Agents SDK. The live Azure test is skipped unless `AZURE_OPENAI_ENDPOINT`
and either `AZURE_OPENAI_API_KEY` or `AZURE_CLIENT_ID` are set; it uses
`AZURE_OPENAI_DEPLOYMENT` (default `gpt-5`) and incurs model usage.
