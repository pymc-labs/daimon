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
as described below. No Azure resource was used. The checksum-verified Codex
image stage was built locally; full-image validation runs in CI.

Offline validation passed with both `HARNESS_LOOP` values: 229 harness tests
passed, 4 skipped (platform/data/live gates). Strict pyright, repository Ruff
and all 14 import-boundary contracts passed. Additional regression coverage keeps
original MCP server/tool policy identities and refuses ambiguous function aliases. The standalone UID probe passed
separately under real service/tool identities with a running pinned controller.

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
| History | Durable Responses input/output; one context-overflow recovery attempt | Durable Responses history prepended by relay to ephemeral Codex turns; native in-turn checkpoint compaction retained; context-overflow retry missing |
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
  thread resume and Agents SDK/Codex history migration are not promised. Codex's
  in-turn checkpoint compaction works, but older harness history is still prepended
  on each request. Add harness history compaction and bounded context-overflow
  handling before long production conversations (F55).
- **Durability:** this lane delegates every event to the existing wrapper. Its
  base checkout does not contain lane 4's pending remote WAL/checkpoint/sequence
  reservation implementation (C4.5–C4.12). Merge/rebase and test those paths
  before production; the relay's awaited callback is the integration point.
- **CI/image:** the harness test job installs checksum-verified Codex 0.162.1.
  The image job builds the release stage, checks `codex --version`, and runs the
  tool-UID relay probe with a running controller. Local tests may still skip
  when the binary is absent. Dependency attribution/SBOM remains a review gate.
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

Codex refreshed after round-3 fixes on 2026-10-21, 09:29–09:30 UTC.
Agents SDK comparison retained from 08:07–08:09 UTC on the same date.

Both loops passed seven scenarios through the real authenticated harness HTTP
API against the paid OpenAI Responses endpoint, with real service/tool UID
separation. The override requires `HARNESS_LIVE_PROBE=openai` and
`HARNESS_RUNTIME_MODE=probe`, and refuses any Azure/MI configuration; a key alone never
selects it, and every non-Luna request is refused before I/O. Azure remains the
production default provider. The key was sourced only in probe shells, captured
in the service, scrubbed from the environment, and absent from tool execution.

| Final passing run | Codex | Agents SDK |
| --- | --- | --- |
| Scenarios passing | 7/7 | 7/7 |
| Requests | 13 | 13 |
| Measured / unknown | 12 / 1 cancelled | 12 / 1 cancelled |
| Measured input tokens | 6,221 | 6,247 |
| Cached input tokens (subset) | 0 | 0 |
| Measured output tokens | 237 | 269 |
| Reasoning tokens (subset) | 0 | 28 |
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
and code mode, and kills residual controller process-group members. The original
round-1 claim did not cover bundled system skills: those remained advertised.
Round-1 fixes explicitly disable `[skills.bundled]` and skill instructions,
remove controller skill/environment injections and private paths at the relay,
and test `$imagegen`/`$skill-installer` against the real binary. Round-2 fixes
reconstruct the user message directly from C3 input, preserve durable history
verbatim, and discard the controller's injected user/developer preamble by provenance.
Real-binary tests preserve all three marker prefixes across prior history and
a tool continuation, while controller injections remain absent. Private path
scrubbing applies only to controller continuations. Harness-supplied C3 skills
remain supported. Cancel/deadline during shutdown forces controller reap and
propagates interruption; regression tests verify the cancel HTTP receipt and
terminal outcome. Both loop suites also pass with inherited probe variables
and a synthetic API key; individual probe tests opt in explicitly. Denied calls now count against `max_tool_calls`,
and ordinary model requests are capped at `min(max_tool_calls + 2, 128)`.
Compaction requests have a separate counter with the same cap and do not consume
ordinary continuation slots; both kinds remain fully metered. Relay request
fields are allowlisted and usage outcomes are idempotent by request id; shielded
shutdown finishes all handlers before the terminal event. Cancellation
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


## Round-3 compaction repair

Codex 0.162.1 local checkpoint compaction stays enabled. The first controller
request appends its copy of C3 input after the injected preamble. The relay
records that user item's ID, reconstructs authoritative C3 input verbatim, and
excludes preamble user/system/developer items. An initial request may already carry the ID-less compaction prompt; anchor
selection uses the last ID-bearing user item in that case. The stable ID remains in Codex's
replacement history. User items after it are controller continuations: the
ID-less trailing compaction prompt and the ID-bearing replacement summary.
They are forwarded with private-path scrubbing, without classifying their text
by prefix. A missing original user ID fails closed rather than silently losing
progress. Provider output is scrubbed before forwarding or retaining it, so a
summary cannot carry private controller paths into later requests or history.

A real pinned-binary regression reports 116,000 input tokens after each of two
write calls. It triggers two compactions, carries both summaries into subsequent
requests, executes each write exactly once, and completes in five measured
requests. Only three requests consume the ordinary continuation budget; two
use the separately bounded compaction budget. Injected state/scratch paths are
absent from provider requests and durable history after both compactions.

Live Luna validation on 2026-10-21, 09:27–09:28 UTC reran the review's 14-command
workload: `seq N N+19999`, for N = 100000, 200000, …, 1400000, sequentially.
All 14 commands ran exactly once, all 14 first numbers were reported correctly,
and the turn completed in 17 requests including two compactions. Every request
has one measured usage outcome, matching all four independent provider counts;
none is unknown. Tokens: 1,069,091 input, 797,087 cached input (subset), 847
output, and 91 reasoning (subset). This is a separate long-turn experiment,
not part of the seven-scenario comparison totals above. It replaces the failed
round-3 behavior (21 executions of seven unique commands, no remaining commands,
and a request-limit error). Sanitized inventory, usage and output digests are
in `tests/live/codex-compaction.json`; large tool outputs are represented by
excerpts, lengths and SHA-256 rather than repeated numeric text. The reusable
probe is `live_luna_probe.py --backend codex --scenario compaction`.
