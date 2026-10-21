# Codex harness spike

PyMC Labs · lane 12 · 2026-10-21 · draft, offline + live Luna validation

**Codex maps to C3 without replacing the harness security or durability boundary.**
Keep `agents_sdk` as the default. Review the opt-in `HARNESS_LOOP=codex` spike,
then validate the actual Azure deployment and image before production enablement.
Pi is not needed: Codex supports the required streaming and dynamic-tool approval
path. Native Codex execution is intentionally refused under ratified C11.

## Scope and evidence

Only the inner loop changes. Both backends use the existing service, C3 actions,
policy lookup, MCP client/credential checks, UID executors, journal and supervisor.
The Codex backend uses `codex app-server` stdio JSON-RPC v2, experimental dynamic
tools, and a harness-owned Responses relay. The relay authenticates with the
captured Azure credential; Codex only has an ephemeral loopback capability.
All model traffic and test/example deployment names use `gpt-6-luna` (C12).
Paid OpenAI Responses probes were subsequently authorized by C12.1 and completed
as described below. No Azure resource or local full-image build was used.

Offline validation passed with both `HARNESS_LOOP` values: 177 harness tests
passed, 4 skipped (platform/data/live gates). Strict pyright, repository Ruff
and all 14 import-boundary contracts passed. Additional regression coverage keeps
original MCP server/tool policy identities and refuses ambiguous function aliases. The standalone UID probe passed
separately under real service/tool identities.

The real pinned binary completed a file-writing multi-request loop against
synthetic Responses SSE. Tests exercise the real mux HTTP driver, local HTTPS
MCP approval/denial, supplied tool results, hidden denied tools, streaming IDs,
dropped-reader recovery, reconstructed-driver resend, history and cancellation.
Provider-boundary tests cover missing/invalid usage, HTTP failures, native calls,
incomplete/failed responses and cancellation during model I/O. The separate
root/service/tool probe drops to service UID 10000 and tool UID 10001 and proves
that the tool cannot read controller memory/environment or the 0600 capability
file, or authenticate to the relay. Its receipt was
`{"service_uid":10000,"tool_uid":10001,"relay_isolated":true}`.

## Comparison

| Concern | Agents SDK wrapper today | Codex spike |
| --- | --- | --- |
| Azure model | Python Azure Responses client | Responses wire API through private relay to the same Azure resource/deployment; API key or captured MI |
| Tools | FunctionTool handlers execute through TurnContext | Same handlers exposed as app-server dynamic tools; Rust owns the model/tool loop |
| Approvals | C3 actions from TurnContext.tool | Same `auto`/`ask`/`deny` and supplied-result path; native exec/patch approvals refused |
| Streaming | Current adapter emits text after each non-streamed response | app-server deltas become C3 deltas; turn-qualified message ID also identifies final snapshot |
| Usage | MeteredResponsesModel emits before/after each SDK request | Relay durably starts before Azure I/O and records measured usage per HTTP request; Codex token notifications ignored |
| MCP | Harness streamable-HTTP client, origin bindings, scoped env references | Same client in wrapper; dynamic tools carry schemas/results, no credentials or upstream MCP config in Codex |
| Sandbox | Separate service/tool UIDs, workspace rules, resource limits, process cleanup | Same authoritative boundary; Codex read-only sandbox plus disabled environment/native tools; relay rejects native model calls |
| Cancel | Supervisor cancels and joins protected tool workers | Interrupt RPC, bounded controller reap and relay cancellation, then the same protected supervisor cleanup |
| History | Durable Responses input/output; one context-overflow recovery attempt | Durable Responses history prepended by relay to ephemeral Codex turns; no native rollout dependency; context-overflow retry missing |
| Dependencies | Python SDK pinned in uv.lock | SDK remains for fallback/shared clients; adds a pinned Rust executable and experimental protocol surface |

## Why the relay is necessary

`thread/tokenUsage/updated` arrives after work and is a cumulative/last-usage
view. It does not establish one durable request identity before each network
attempt. `codex exec --json` alone similarly lacks the bidirectional dynamic
tool/action interface needed here. The authenticated relay owns actual HTTP
attempts, awaits the wrapper emit callback before model authentication/I/O,
disables automatic request/stream retries and model-side storage, and reconciles
cancellation as unknown when no provider usage arrived. It never invents counts.
Usage journal completion is protected against cancellation before deciding
whether an unknown observation is needed.

Tool events are buffered until successful provider completion and measured
usage. Text can stream earlier. Failed or incomplete responses preserve actual
usage but never release tool calls. Native tools are omitted regardless of
Codex's bundled model catalog, and native/unknown outputs are rejected at the
provider boundary. Native approvals are a failure, never a grant to run with
the controller UID. C11 ratifies these choices.

## Binary, version and licence

Checked the actual upstream LICENSE and NOTICE at tag `rust-v0.162.1`, commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`: Apache License 2.0; NOTICE includes
OpenAI attribution and MIT-derived Ratatui attribution. Both are retained in
`apps/harness/licenses/` and copied into the image. This is distribution evidence,
not a review of every transitive binary dependency or trademark use.

The image downloads the official linux/amd64 musl release archive using Docker
ADD's checksum verification, extracts only the executable and carries no npm or
Rust compiler. Verified archive SHA-256:
`86f268d81b898f3e144c5ecff2ad2fda2c5802e042d6195b72538fe0fa125057`.
The downloaded archive contained `codex-x86_64-unknown-linux-musl` and reported
`codex-cli 0.162.1`. Runtime initialize also checks the app-server version.
The compressed archive is about 111 MB; account for final executable size,
cold-start cost, SBOM and vulnerability scanning. This image is amd64 only.

Sources checked:

- [Official app-server documentation](https://developers.openai.com/codex/app-server)
- [Pinned protocol implementation](https://github.com/openai/codex/tree/rust-v0.162.1/codex-rs/app-server-protocol)
- [Pinned tool registration](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/spec_plan.rs)
- [Actual LICENSE](https://github.com/openai/codex/blob/rust-v0.162.1/LICENSE)
- [Actual NOTICE](https://github.com/openai/codex/blob/rust-v0.162.1/NOTICE)
- [Release assets](https://github.com/openai/codex/releases/tag/rust-v0.162.1)

## Gaps and production effort

- **Live Azure compatibility is unproven.** The OpenAI live probes passed using a
  minimal direct-functions/full-Responses catalog. Still validate the `gpt-6-luna` deployment, API version, supported
  reasoning/tool schemas, usage details, content filters and MI after env scrub.
  C12 forbids running a test against any other deployment.
- **Experimental protocol:** dynamicTools, environment selection and message
  events can change. The pin is mandatory; upgrades need the same offline and
  image probes. Error logs are intentionally not streamed from the controller.
- **History:** the canonical journal survives controller restarts, but native
  thread resume/compaction and Agents SDK/Codex history migration are not promised.
  Add bounded context-overflow handling before long production conversations.
- **Durability:** this lane delegates every event to the existing wrapper. Its
  base checkout does not contain lane 4's pending remote WAL/checkpoint/sequence
  reservation implementation (C4.5–C4.12). Merge/rebase and test those paths
  before production; the relay's awaited callback is the integration point.
- **CI/image:** real-binary tests skip without Codex. Install the pin in the
  harness test job and run the standalone UID probe in a root subprocess/image;
  the image includes the pin, but was not built here. Full-image CI and dependency
  attribution/SBOM remain review gates.
- **Native tools remain unsupported.** Supporting native exec/patch later would
  require a verified execution-server integration or Codex changes that retain
  harness UID, resource, policy, tool-limit and cleanup guarantees.

Estimate after review: 2–4 engineer-days for Azure/MI/image and CI verification,
context recovery and failure-path hardening; another 1–2 days to exercise the
lead-owned remote durability integration. Native executor support is separate
work (roughly 1–2 weeks to investigate and harden), not required by C11.
These are estimates, not measured completion times.

**Recommendation:** keep the small opt-in adapter and the Agents SDK fallback.
Codex is viable for the inner loop and has passed live Luna probes; do not switch the production
default until the listed review, live and durability gates pass.


## Live Luna probes

Both loops passed seven scenarios through the real authenticated harness HTTP
API against the paid OpenAI Responses endpoint, with real service/tool UID
separation. The override is `HARNESS_LIVE_PROBE=openai`; a key alone never
selects it, and every non-Luna request is refused before I/O. Azure remains the
production default provider. The key was sourced only in probe shells, captured
in the service, scrubbed from the environment, and absent from tool execution.

| Final passing run | Codex | Agents SDK |
| --- | --- | --- |
| Scenarios passing | 7/7 | 7/7 |
| Requests | 13 | 13 |
| Measured / unknown | 12 / 1 cancelled | 12 / 1 cancelled |
| Measured input tokens | 23,419 | 6,284 |
| Cached input tokens (subset) | 11,941 | 0 |
| Measured output tokens | 244 | 284 |
| Reasoning tokens (subset) | 0 | 42 |
| Plain output deltas | 5 | 1 after response completion |
| Sequential MCP calls | 3 requests, 2 tool calls | 3 requests, 2 tool calls |

Every request start was observed in the durable journal before outbound network
I/O. Every completed request has exactly one usage event, after its start; all
four token counts match the independently captured provider response. Each
cancel has one unknown observation and an interrupted terminal event. These
figures describe this run only; prompt/history differences mean they are not
a controlled model-efficiency benchmark.

Plain streaming, automatic local MCP echo, ask/approve with actual execution,
ask/deny without execution, isolated bash, sequential MCP requests and cancel
all passed. Bash returned UID 10001 and `openai_key_visible=False` for both
backends. Codex cancelled after receiving text deltas, sent interrupt and reaped
the controller. The current SDK adapter does not stream model text incrementally;
its comparison cancellation interrupts model I/O after the durable request
start. This is a documented comparison gap, not claimed as mid-text cancellation.

Real calls exposed issues not visible with synthetic responses: the bundled
Luna catalog forces code mode/Responses Lite and can elicit native `exec` calls;
default plugins can spawn a clone that races temporary-directory cleanup. The
adapter now supplies a minimal per-deployment catalog with direct function tools,
full Responses and no native shell/patch, disables plugin/tool/host-skill discovery
and code mode, and kills residual controller process-group members. Cancellation
also suppresses writes to already closed relay transports. Native calls were
refused throughout the failed attempts; no native execution was approved.

Sanitized final transcripts and independent raw provider counts are in
`apps/harness/tests/live/`, with a summary README and retained diagnostic request
inventory. Early SDK observer failures preceded per-scenario export; the saved
files are not a complete account billing ledger. The final observer requests
uncompressed transport bytes so JSON/SSE usage auditing does not alter responses.
The probe scopes startup credential-file scanning to its private scratch root
because this is a build host with unrelated Docker mounts; production mount
validation is unchanged. Live Azure, container layout, remote durability and
normal review remain enablement gates.
