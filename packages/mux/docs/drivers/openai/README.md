# OpenAI Agents API driver

This is an unwired, offline-tested provider driver. Daimon's registry, configuration,
turn path and default remain unchanged: existing channels resolve to Anthropic.
`OpenAIDriver` selects `openai.persistent_workspace` (hosted compute) or the explicitly
named `openai.conversation_only` profile (`environment.type = none`). It never
silently provisions a workspace for the latter or starts a replacement session on
continuity loss. Native memory stores are unavailable and fail admission.

## Source and transport

The official [Agents guide](https://developers.openai.com/api/docs/guides/agents),
[managed Agents API guide and subpages](https://developers.openai.com/api/docs/guides/agents-api/overview),
and API reference were re-fetched and audited **2026-10-10**. The sprint lane archive
`api-snapshots/2026-10-10T070515Z/manifest.json` records URLs, fetch timestamps,
HTTP status and SHA-256 hashes for 56 snapshots, including the complete Agents
reference, streaming events, hosted environments, observability, skills/files,
Admin usage/costs, pricing and SDK installation guide. It supersedes the supplied
**2026-09-14** seven-page archive as the source checked for this driver.

The Python package remains pinned `openai>=2.54.0,<3`. The current official guide
uses generated `beta.agents` convenience bindings; locally verified version
2.54.0 has no such binding. The official pages do not establish a minimum Python
package version that includes it, so this audit does not justify a version bump.
The transport uses that SDK's verified public `AsyncOpenAI.get`, `post` and
`delete` primitives, with `OpenAI-Beta: agents=v1` and `max_retries=0`. The lead
approved this approach at 15:13Z on 2026-10-09. Today's reference retains the
implemented paths and SSE contract. There is no Responses API or Agents SDK
substitute.

| HTTP resource | Methods | Official evidence |
| --- | --- | --- |
| `/agents`, `/agents/{agent_id}` | POST create; GET list/retrieve; DELETE | [Agents](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents) |
| `/agents/environments/templates`, `/agents/environments/templates/{template_id}` | POST create; GET list/retrieve; DELETE | [Templates](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/environments/subresources/templates) |
| `/agents/sessions`, `/agents/sessions/{session_id}` | POST create; GET list/retrieve; DELETE | [Sessions](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions) |
| `/agents/sessions/{session_id}/events` | POST input; GET `?stream=true` (SSE, `Accept: text/event-stream`) | [Input events](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/subresources/events/methods/create), [stream recovery](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) |
| `/agents/sessions/{session_id}/items` | GET, `after`/`limit`/`order` pagination | [Saved items](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/subresources/items/methods/list) |
| `/agents/sessions/{session_id}/turns`, `/agents/sessions/{session_id}/turns/{turn_id}` | GET list/retrieve | [Turns](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/subresources/turns) |
| `/skills`, `/skills/{id}`, `/skills/{id}/versions/{version}` | Multipart POST skill/version; GET list/retrieve; DELETE skill | [Skills](https://developers.openai.com/api/reference/python/resources/skills) |
| `/files`, `/files/{id}`, `/files/{id}/content` | Multipart POST (`purpose=user_data`); GET metadata/binary; DELETE | [Files](https://developers.openai.com/api/reference/python/resources/files) |
| `/agents/sessions/{session_id}/artifacts`, `/{artifact_id}`, `/{artifact_id}/content` | GET all pages/metadata/binary; DELETE artifact | [Artifacts](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/subresources/artifacts) |
| `/vaults`, `/vaults/{id}/credentials`, `/{credential_id}` | GET metadata/pages; POST vault/credential; DELETE credential | [Vaults](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/vaults) |

Resource identifiers are escaped as path segments. A native page's `has_more` and
`last_id` determine continuation. A missing option is omitted; explicit empty tool
collections are sent empty. Send, steer and cancel preserve the logical submission
key in the documented `Idempotency-Key` HTTP header; no JSON key field is sent.
HTTP 202 means queued acceptance, with no invented input IDs, processed receipt or
root outcome. A POST connection failure returns `outcome_unknown`; the port never
retries it. SDK exceptions and malformed native records become `ProviderError`,
without carrying SDK objects or upstream exception messages. Caller page-limit and
empty-input validation errors use `invalid_request` rather than `upstream`.

## Explicit session controls

Hosts may construct `OpenAIDriver(..., session_controls=SessionControls(...))`
from `mux.drivers.openai.session_controls`. `container_size` accepts `small`,
`medium` or `large`, sent as `environment.container_size` on hosted session
creation. It can override a template's size. A conversation-only driver refuses
this hosted setting before provider I/O. Omission preserves the provider/template
selection; without a template, the documented hosted default is `medium`.

`spend_limit_usd_cents` accepts a strictly positive integer and maps to
`spend_control.limit` for either profile. For example, `220` means **$2.20 across
the session**, not 220 dollars or a per-turn token limit. This provider spending
control supplements host admission and accounting; it does not turn a harness
reservation into an exact bill. Without explicit controls, existing creation
request bodies remain unchanged. See [hosted resources](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted)
and [create-session parameters](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/methods/create).

The 2026-10-10 reference makes `spend_control` optional and gives a positive
integer-cent limit without a higher documented minimum. The G1 project probe
returned HTTP 400 `invalid_request_error`, `param=spend_control`, with a one-cent
limit. This establishes a rejected optional field in that project, not a verified
alternative minimum or shape. G1 therefore allows omission and its live tooling
omits the native field. The canonical guard still admits bounded accounting
reservations (OpenAI $50 line, $40 stop); G1's probe runs at most two host turns
with fixed whole-turn deadlines and no resend. Accounting caps and host deadlines
are not a native token ceiling. Explicit driver callers may still supply the
reference-shaped native setting where support has been verified.

## Identity, turns and recovery

The host supplies the provider account identity, authorization callback and optional
binding lookup. Tenant/account/provider/kind checks precede I/O. Native tenant
metadata is checked when the native record carries its tenant tag; untagged records
still require host authorization. The lookup retains the host's stable binding ID and hosted
environment identity; a mismatch or missing native session raises `ContinuityLost`.
Returned records do not create or replace a host binding.

A final saved item supplies authoritative content, keyed by item ID. Text deltas and
part-completion updates are previews; part completion does not manufacture a whole
completed message. Full content parts, phase, function call IDs and required-action
routing survive normalization. An environment connection request and a browser
authentication request remain distinct from generic tool confirmations.
The documented `agent.session.requires_action` event carries `event_id`, `type`
and `session`; function/approval turn IDs come from `session.required_actions`,
as shown in the [streaming event schema](https://developers.openai.com/api/reference/resources/beta/subresources/agents/streaming-events#agent.session.requires_action).
Missing or conflicting action turn identities refuse publication. Environment
reconnection has no native turn ID and retains the observed active root.

Only a native root turn with explicit `subagent_id = null` establishes a terminal
outcome. Subagent outcomes, session idle and EOF do not complete root work. Duplicate
terminal evidence is suppressed; conflicting outcomes are refused. Older buffered
running notifications cannot reopen a root that saved terminal evidence completed.
History refresh and recovery seed these outcomes from the retained host journal,
including after driver restart; conflicting evidence cannot replace the journal
or invalidate previously published cursors.
Cancel acknowledges the request separately from observed stop; `wait_stopped`
performs a single observation of the requested root turn with a bounded deadline.

Recovery opens and consumes a live stream before reading all saved turn/item pages.
It deduplicates overlapping pages, preserves final items over late previews and
merges buffered updates. A stream error or pagination failure publishes nothing.
A clean end of the stream is not an error: the whole snapshot is read again after
the close, so a change on a page read before the close is still seen. The gap and
reconciled markers are keyed on the published history, so a repeat recovery that
saw nothing new writes no new journal rows.
Buffered required actions and session/environment failure update the returned
projection along with its journal; root completion clears stale required actions.
An action for a known terminal root is suppressed, including an older action
notification buffered after that root's completion.
Successful recovery publishes `session.reconciled` and an explicit unrecoverable
native-event gap: the provider cannot replay missing deltas or request spans.
History listing refreshes saved state and uses materialized event IDs as cursors;
these are not resumable native SSE cursors.

## Host state and usage

Daimon's host adapters in `turn/openai_state.py` persist recovery history,
usage revisions and per-invocation pre-input baselines through the admitted
A4 journal/lease. Complete native history is published in one `record_many`
transaction. Revision checkpoints are host metadata, excluded from provider
history and display; no SDK meter is invented. `turn/openai_host.py` requires
an authorized provisioned native `SessionSpec`, durable adapters and scoped
transport injection. It creates once under a durable claim, publishes its actual
binding, and reuses the same native environment across host turns. It refuses
shared threads, transfer, sealed/read-only/publishing-restricted policies and
changed bound plans rather than erase continuity or bypass host policy.

An explicit `SessionControls.model="gpt-6-luna"` adds the documented
session `agent.model` override while retaining `agent_id`; the decoder checks
the returned session model on creation and subsequent reads. Omitting this
control preserves old request bytes. See the current [session creation reference](https://developers.openai.com/api/reference/resources/agents/subresources/sessions/methods/create).

`RecoveryJournal` and `UsageRevisions` are injected host ports. Journal replacement
must be atomic; revision allocation must persist and compare/update atomically.
The host must serialize refreshes per session because the provider exposes no
monotonic usage revision or snapshot read precondition. The included memory
implementations are for offline tests and must be retained across driver restarts;
they are not a production database adapter. The host still owns durable operation
intent, claiming, leases, fencing, stream journal commits and accounting outbox
writes through `mux.state.StateStore`. This driver does not substitute its snapshot
cache for those guarantees.

Usage is one cumulative observation per native turn, with separate subagent
identities. Missing usage/counts stay null, input includes cached tokens and output
includes reasoning tokens. Changing a meter increments the same observation's
revision; identical replay does not. Revisions support signed corrections such as
`null → 100 → 120 → 110`. Session totals are never added to turn totals. Native
cache-write counts, model identity and final billing amounts are not fabricated.
See [observability and usage](https://developers.openai.com/api/docs/guides/agents-api/observability).
Session resources also expose a nullable cumulative `usage` snapshot. A host
choosing this grain must not add overlapping turn totals. Neither grain is a final
bill. `spend_control.consumed`, when present, is an integer **floored to whole USD
cents**: divide by 100 for its USD floor, and preserve null as unknown. It does
not establish an exact model or container charge.

The hosted guide points to standard container rates and separate model rates.
Session/environment timestamps are not billed container duration. When actual
charges cannot be resolved from session/turn usage plus verified compute usage,
retain an `estimated_unverified` result and the held amount. The organization
[Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
(`GET /v1/organization/costs`) and
[Usage API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/completions)
(`GET /v1/organization/usage/completions`) require an
[organization Admin API key](https://developers.openai.com/api/reference/administration/overview).
Costs are bucketed organization/project/key/source amounts, not a session-scoped
bill, so even Admin access alone does not establish per-session exact spend.
The driver does not discover an Admin credential or call those endpoints.


An `in_progress` session can temporarily have no visible active root in its turn
listing. Create and retrieve preserve `state=running` and an unknown
`active_root_turn` in that case; accepted creation is never retried. New-turn
input remains refused while the native session is busy. Neither an empty turn
listing nor a terminal turn implies idle; subsequent native session and saved
turn observations establish occupancy and completion separately. Required
actions still refuse when their active root is unavailable.

## Current boundaries

Skills, artifacts and vault ports use the private SDK transport by default.
Models still require an injected implementation. Conversation-only creation
requires initial input, as the native API documents. The persistent profile is
core with evidence for all nine mandatory capabilities below. Explicitly selecting
backend `openai` chooses it when no profile is named; an explicit model is still
required. `openai.conversation_only` remains non-core and must be named. The
unconfigured backend, profile and thread mode remain Anthropic/per-caller.
Multiagent configuration is unsupported and complete workspace export/import is
unknown. Resource support does not imply a live or complete C01–C18 certificate.
Agent/environment conditional updates, archive, workspace export/restore, native
SSE replay and atomic `expected_turn` are refused. Session update plans return
`refuse`, and migration always raises `MigrationUnsupported`. Resource revision
fingerprints describe a retrieved record; they are not native conditional-write
preconditions. Input-mode/steering/cancel-target checks depend on host serialization,
not on a provider CAS. Unmatched tool confirmations and native input bypasses refuse.
The current computer-use schema keeps `browser_origin_access` decisions
(`approve`, `deny`, `cancel`) distinct from `browser_authentication` actions
(`submit`, `cancel`). Origin requests normalize to typed tool confirmations. An allow/deny answer
must name exactly one current-root origin request; its wire response is
`agent.session.input.computer_use_approval_request_result` with
`response.type=browser_origin_access`. Authentication stays native and refuses
this route. Unmatched/duplicate/foreign-root requests, native bypass input and
unsupported denial-message text refuse before a write. Turn serialization
remains host-owned; this does not claim a native CAS. G1's display codec shows
the observed origin and reason before the permission pause, keeping neutral
request provenance separate from display records. Daimon registers the codec
only for explicitly configured caller-private Luna channels with an injected
authorized native runtime. See the [computer-use guide](https://developers.openai.com/api/docs/guides/agents-api/tools/computer-use).


### Observed Agents model eligibility

Guarded probes on **2026-10-09** in the sprint's OpenAI project accepted minimal
agent creation with `gpt-6-luna` and `gpt-6-astra`; both created agents were deleted.
The same minimal body (`name`, `model` only) with `gpt-5-nano` returned
`invalid_request_error`. Read access passed independently. The retained error
identifiers did not provide a more specific rejection reason. These findings are
project-specific observations, not a complete provider model allowlist.

The cheapest accepted model in those probes, `gpt-6-luna`, then passed one hosted
session / one turn / one cancel smoke: the root interruption was observed through
both the stream and stop check, and session/agent cleanup succeeded. Astra was
tested only for creation. Usage counts were null, so billing remains unknown.
No recordings or full live conformance certificate were produced. The smoke's
model choice does not change production defaults; callers must still select a
model explicitly. Admission-time model eligibility validation remains a follow-up.
The current turn port has no `max_output_tokens` setting; the smoke used a bounded
deadline and an explicit guard reservation, which is not a native generation cap.

Run `uv run --all-packages --all-extras pytest packages/mux/tests/drivers/openai`.
Synthetic fixtures and `httpx.MockTransport` exercise the actual pinned SDK without
provider access. No live certificate is implied by these tests.

## Resource boundaries and core evidence

A `SkillUpload` is a deterministic ZIP with one root `SKILL.md` and exact inline
supporting bytes. Noncanonical, duplicate or nested-manifest paths refuse before
upload. Separate display titles and external catalogue/digest pins refuse rather
than silently changing the manifest. Versions are concrete positive integers;
mutable `latest` is refused. Agents have no native skills field: neutral skill
intent is stored in reserved `mux_skill_pins` metadata, with each value bounded at
512 characters. Larger pin lists use the count/digest and chunk scheme below.
Session creation resolves default versions once and sends documented hosted
`environment.skills` references. Retrieved installed versions must match the
stored concrete pins; missing/changed pins raise `ContinuityLost`.
The [session schema](https://developers.openai.com/api/reference/python/resources/beta/subresources/agents/subresources/sessions/methods/create)
explicitly declares hosted skill references, despite the older supplied general
skills guide describing capability-directory discovery.

Input files use distinct `file:<id>` references. Published artifact references
encode their parent session and artifact identities canonically. All native pages
are preserved; a turn filter is applied locally without replacing the provider's
cursor. Downloads stream exact binary bytes and close the response on error or
cancellation. Short/oversized downloads raise a typed network error. Uploads spool
at one MiB and refuse beyond 512 MiB. Native metadata does not expose MIME; the required neutral field therefore uses
the generic `application/octet-stream` fallback without guessing from filenames. Deletion validates native identity and the parent tenant before
removing exactly the requested input file or session artifact.

`openai.vaults@1` accepts a closed configuration containing scoped vault refs.
Ensure searches all pages and refuses duplicate owned names. Credential writes
resolve a host secret reference only after native vault ownership checks; records
and their revision hashes exclude tokens/refresh secrets/upstream extras. A private
string representation redacts credentials in the pinned SDK's DEBUG options log
while preserving the real JSON on the wire, verified with actual SDK logs.
Static bearer and MCP OAuth access-token bindings require HTTPS destinations;
environment credentials, archive and conditional credential writes refuse.
Session deletion retains attached vaults and never deletes their provider resources.
It waits for native `idle` or `failed` status before deleting. A definite HTTP 409
triggers another scoped read and a one-second wait before the next eligible
deletion attempt, using the same `Idempotency-Key`. The complete operation has a
30-second deadline, including HTTP calls. Busy sessions are left for their work
to finish; callers can explicitly cancel first when needed. Other provider
errors propagate without a retry. Expiry during a DELETE reports an unknown
outcome with automatic retry disabled; expiry while waiting reports a cleanup
deadline failure, never a deletion receipt. Native identity, host authorization
and tenant ownership are checked again on every poll.

This follows the [session deletion reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/delete)
(verified 2026-10-09): running execution must stop before deletion, and physical
cleanup may continue after the public resource is deleted. Actual pinned-SDK
regressions cover the idle/409/running/failed race, bounded busy/conflict waits,
ownership changes, transport failures and interrupted deletion. Live C10/C15/C16
cleanup recovery is recorded in the partial matrix below.

Initial file mounts use uploaded `file:` refs at canonical `/workspace/` paths.
Repositories, raw native/secret bindings and conditional resource replacement are
unavailable. C08 and deployed repository/MCP certification remain pending.

| Mandatory capability | Actual-driver offline evidence |
| --- | --- |
| Workspace persistence | `test_skill_workflow`: two completed turns reuse one environment and immutable installed pins; existing binding tests refuse changed environment identity |
| Turn lifecycle | C05: child-first completion, authoritative saved items, EOF occupancy and one root outcome |
| Cancel | C06: queued acknowledgement retains occupancy until an independently observed interrupted root |
| Tool loop | `test_tool_result_routes_call_and_turn_from_required_action`: preserves native function call/turn routing |
| Required actions | Documented nested session envelope tests cover function/browser/environment kinds and reject missing/conflicting turn identity |
| Skills bundle | Real multipart ZIP preserves inline manifest/supporting bytes; actual agent/session workflow installs fixed native versions |
| Artifacts | C09: all pages, binary checksum, typed interruption and shared-vault retention; scoped deletion tests use the actual SDK |
| Usage observations | Null counts, distinct subagents, replay/restart and signed `100 → 120 → 110` revisions in driver tests |
| Reconcile | C05 and recovery tests: consume stream before all saved pages, deduplicate overlaps, preserve terminal journal and publish an explicit unrecoverable gap |

Host durability, batching, attribution, pricing, wake generations and registry
selection still require their shared fixture adapters. The guarded Luna smoke
above verifies a narrow live lifecycle path; it does not certify these host
guarantees or resource workflows. That smoke made no recorder fixtures.
An earlier `gpt-5-nano` smoke returned `invalid_request`, with unknown billing
and a retained $0.45 reservation; that earlier attempt did not establish live
correctness.

The later [partial live matrix](../../../../../CERT-M2.md) records C10/C15/C16
passing on `gpt-6-luna` after reviewed cleanup recovery; fifteen missing live
scenarios remain typed PENDING. The idle-session tapes contain zero normalized
Events and replay verifies request metadata and absence of unexpected writes.
They do not certify native codecs, SSE, binary resources, hosted continuity or
host durability. Earlier failed attempts and unknown billing remain in the
receipt manifest. This is an incomplete certificate.

## Offline conformance

Registration is explicit and creates a fresh actual `OpenAIDriver` for each
shared probe. The pinned SDK talks only to `httpx.MockTransport` at
`openai.invalid`; construction makes no request or credential lookup. Use the
registration context to close all streams and SDK clients after the run:

```python
from mux.testing.conformance import Registry, run
from mux.testing.drivers.openai.conformance import register

registry = Registry()
async with register(registry):
    results = await run(registry, "openai.offline")
```

The adapter seeds native HTTP/SSE records and logs requests independently. It
does not supply normalized events, projections, receipts or pass verdicts.
Admission uses the fixed declared profile; the fixture's admission fault labels
do not alter it. Additional tests exercise its actual unsupported replay and
unknown memory-store entries. Broken driver variants fail on shared fixture
diagnostics, including under optimized Python.

| Fixture | Offline result | Evidence or missing dependency |
| --- | --- | --- |
| C01 | PENDING | Host attribution, batching and workspace binding adapter |
| C02 | PENDING | Verified native expiry classification and host session preparation |
| C03 | PENDING | Host intent, atomic claiming and receipt recovery |
| C04 | PASS | Actual native acceptance across fenced intent/ack crashes, restart, atomic journal and takeover; uncorrelated input remains unknown without another POST |
| C05 | PASS | Child-first completion, preview authority, EOF occupancy, saved-page overlap, explicit gap and one root outcome |
| C06 | PASS | Cancel acceptance holds occupancy until an observed interrupted root |
| C07 | PENDING | Shared usage revision/outbox StateStore scenario adapter |
| C08 | PENDING | Conditional required-mount replacement refused; host preparation adapter absent |
| C09 | PASS | All artifact pages, exact binary bytes, typed truncation and actual shared-vault retention |
| C10 | PASS | Required unavailable capabilities, extension versions and foreign scope refused |
| C11 | PENDING | Native repository source, separate skill display title and valid canonical skill bundle |
| C12 | PENDING | Host billing identity and overlapping-grain pricing hook |
| C13 | PENDING | Host new-slot adoption and restart persistence adapter |
| C14 | PENDING | Existing/new-thread provider selection adapter |
| C15 | PASS | Typed migration refusal preserves binding without provider writes |
| C16 | PASS | Raw handles and undeclared extension/native-input bypasses refused |
| C17 | PENDING | Host wake-generation adapter |
| C18 | PENDING | Host termination/outcome-row mapping |

Every pending entry has a typed reason and prevents certification. Supplying a
standalone memory StateStore would not prove that host send/recovery ports use
it. C04 explicitly injects `OpenAIConformanceEvents`: the restartable store owns
intent, immutable input digest, scoped principal, lease and claim; the inherited
real driver sends the native POST. The mock server independently retains accepted
inputs even when the response or subsequent local commit is lost. Recovery reads
the exact owned session and at most five asc item pages under an eight-second
deadline. A 202 has no input IDs, and saved messages have no documented input-key
lookup: matching text or a completed current root never promotes an uncertain
operation to processed. No recovery POST or idempotent redelivery is performed.
This conservative result satisfies the unchanged shared fixture; it does not
prove the production host persistence or live crash behavior. The native
[session guide](https://developers.openai.com/api/docs/guides/agents-api/sessions)
and 2026-10-10 hashed Agents reference are the protocol sources. C07 also expects a full correction sequence in one reconcile call; OpenAI
returns current native snapshots and its revision allocator is exercised across
separate reads in the driver tests. No live behavior, production persistence,
pricing or complete C01–C18 certificate is claimed.

Run `uv run --all-packages --all-extras pytest packages/mux/tests/drivers/openai/test_conformance.py`
and `uv run --all-packages --all-extras python -O -m pytest packages/mux/tests/drivers/openai/test_conformance.py`.

## F1 default-capability adapter

`OpenAIDefaultCapabilityFactory` is an explicit offline adapter for the shared F1
scenario. It uploads the actual eleven authored default skill bundles through
#584's multipart path, installs their immutable versions in one hosted session,
and independently decodes upstream uploads and deployed agent configuration.
The adapter declares `atomic_revision_pin=False`: its successful result reports
that the session was provisioned unpinned and makes no atomic revision/CAS claim.

The declared logical-to-native capability mapping sends all six default builtins
through the hosted **Bash** capability: bash/read/grep/glob use Bash commands;
write/edit use distinct Bash invocations of the hosted `apply_patch` command.
These are not six native `agent.tools` names. The adapter requires the observed
hosted environment and retains the native `command_execution` item identity,
command, successful exit status, output and agent executor.
[Agents architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)
documents hosted Bash/apply-patch; the
[Agents item reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/subresources/turns/subresources/items/methods/list)
defines command execution and MCP history items. Responses API patch shapes are
not used.

The reserved `daimon-mcp` connection uses the documented credential-free remote
HTTP transport in `agent.tools`. Its endpoint must be reachable from OpenAI's
service network; localhost is not a service-reachable deployment. Authentication
requires the existing scoped vault/session credential path, not a secret in a
saved agent. Credential references and additional MCP policy settings remain
explicitly refused by this agent port.
[MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp)
describes service/environment origins and session/vault authentication.

Completed `mcp_call` and `command_execution` items produce separate, paired mux
invocations/results with distinct stable item identities, native provenance and
turn identity. Running items remain previews, child items remain thread events,
and failed/incomplete calls or nonzero/unknown command exit codes produce error
results. Provider error bodies are not copied into normalized result payloads.
History refresh, pagination and SSE use the same batch codec.

Larger skill lists use reserved, integrity-checked metadata chunks, each at most
512 characters, with at most eight chunks plus one count/digest marker. Legacy
single-value pins remain readable and keep their original encoding. Missing,
extra or changed chunks refuse instead of losing skill intent; user metadata
cannot overwrite the reserved namespace.

`packages/mux/tests/fixtures/openai/recordings/F1-scripted.json` is **scripted offline
evidence**, not a live certificate. A fresh replay reprovisions all resources
through the pinned SDK fake and consumes every normalized operation batch. Tool
arguments use the recorder's fixed omission sentinel, so replay does not certify
argument contents, native HTTP bodies or model quality. The recorder/audit is
unchanged. Live execution requires a separate lead GO and `gpt-6-luna` only;
production defaults are unchanged.

The initial explicit host slice requires
`SessionControls(multi_agent_enabled=False)`. Creation sends the documented
`agent.multi_agent.enabled=false` override alongside the selected model, refuses
an enabled saved agent before the session POST, and verifies explicit false in
every returned native session. Reuse and every host input recheck that setting.
A missing or enabled snapshot refuses; root-only accounting cannot certify
delegated work. Child-usage accounting is a follow-up. Omitted controls preserve
standalone driver request bodies. See the fetched 2026-10-10
[multi-agent guide](https://developers.openai.com/api/docs/guides/agents-api/multi-agent).


Explicit `SessionControls.spend_limit_usd_cents` uses integer USD cents
(minimum 1, maximum 4,503,599,627,370,495). The selected limit must be echoed as
an exact integer on every native session read; missing, null, malformed or
changed limits refuse before host input. Omission remains supported and leaves
the request unchanged. An acknowledged create with an unverified echo retains
only scoped native session/agent references in `SessionSpendLimitUnverified`;
host preparation durably records the session reference under the original
creation claim for exact owned cleanup. It never treats this as a successful
binding or retries the creation automatically.

Small test sessions may explicitly select 20 cents; 70 cents belongs only to an
explicitly selected medium session. These are operational policy choices, not
claims about a service minimum. A prior project response rejected a 1-cent cap;
its precise reason is unknown. The session cap does not establish exact spend,
cover every infrastructure/tool charge, enforce token counters or replace the
budget guard. `spend_control.consumed` is nullable and floored to whole cents;
actual usage, dated pricing and container accounting remain separate. The
2026-10-10 13:49:48Z
[create reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/create)
snapshot SHA256 is
`7ec0fd6edb8c21cbc57163e2dfe024fe9c7a4dfff88a9f3b2973c1bdb9c06b8c`.
SDK request-shape tests load the retained compressed official snapshot, verify
its full hash, and pin the native body, headers, units and strict echo;
provider acceptance of 20/70 cents is not established by offline tests.

### C11 provider preparation evidence

`test_interaction_deployment.py` composes the actual Skills, Agents, Files,
Sessions and Events ports through the installed SDK. A valid inline `SKILL.md`
bundle is decoded independently from the transmitted multipart ZIP; its immutable
version is bound to the retrieved agent and echoed by the hosted session. The
same session binds a remote MCP connection and an exact uploaded input file
before a native turn exposes a typed pending function result. EOF leaves that
root occupied; neither unavailable execution nor a changed skill pin is success.

The file is an uploaded archive, without a native git checkout claim. Separate
skill display titles and repository workspace sources remain typed refusals
before I/O. The unchanged canonical C11 fixture still requires those unsupported
shapes, so C11 stays PENDING. This request-shape proof is offline evidence, not a
live certificate or a change to host preparation. It follows the 2026-10-10
hashed Agents hosted-skill reference, the
[Skills guide](https://developers.openai.com/api/docs/guides/tools-skills), and the
[Files guide](https://developers.openai.com/api/docs/guides/agents-api/environments/files).

Configured deployment transports refuse every ambient `OPENAI_*` setting by
name before SDK construction or copies, including empty and unknown settings.
Credentials and project remain explicit host-selected arguments; organization
remains empty and the origin remains `https://api.openai.com/v1`. SDK headers and
query defaults are explicit. Each request owns its HTTP client with verified TLS
using default certificate roots and no environment proxy or certificate lookup.
SDK timeout, connection limits and redirect defaults are retained; retries remain
zero. Request/stream completion, errors and cancellation retain owned cleanup.
TLS/proxy environment overrides also refuse by name before TLS creation,
including `SSLKEYLOGFILE`, certificate settings, proxy settings and CA-bundle
settings (the same name set as Gemini). `trust_env=False` alone does not disable
Python TLS key-log discovery. Clean contexts retain verified default roots and
have `keylog_filename=None`; operator environment settings are never cleared.
This policy applies to explicitly configured OpenAI channels only.

The separate [offline SDK boundary component](CONFORMANCE-SDK.md) executes
unchanged C06/C07/C09 oracles against native MockTransport snapshots and actual
driver state, with cancellation, usage-revision and binary/shared-vault
mutation controls. C08 changed tools/file mounts still refuse conditional
updates before writes. Synthetic usage correction/outbox proofs do not settle
live dollar spend; this component does not promote any live PENDING case or
change the standard registry.

### Explicit host conformance envelopes

`daimon.testing.openai_conformance.compose_openai_host` is an opt-in QA
composition over N4's `BackendSelectionEnvelope`/`WakeEnvelope` and N5's
`HostAccountingHook`. It returns observed facts to the shared mux oracle; the
application never discovers or installs it. Normal current-config admission
is required for C14. Its actual-host proof retains the existing thread's native
binding after a channel backend change, selects the new revision for a new
thread, and refuses an unavailable original registration before mutations.
Callbacks must not preselect the old revision. Any observed retention regression
remains visible as a typed `SelectionFinding` routed to N4; facts are never
repaired by the adapter. C17 runs
the actual OpenAI SDK/host codec inside the supplied active persistence context
and independently queries the wake, replacement binding, journal and released
lease. Its callback does not acquire a second persistence lease.

C12 retains `CAPABILITY_UNAVAILABLE`: immutable-disjoint coverage refuses the
shared overlapping turn/session aggregates. Its queried accounting/outbox
partials are supplemental. C18 remains typed PENDING until N4
publishes its complete outcome API and OpenAI supplies independently declared
reachable-reason coverage. Four root examples do not certify that coverage. Missing envelopes retain
their typed PENDING declarations. Shared fixture activation and complete C18
coverage remain separate owner dependencies; these offline proofs claim no
new live matrix PASS. SDK clients use the shared isolated testing factory and
all native HTTP is directed to MockTransport at invalid fixture domains.
### Explicit native credential offers

The host's `OpenAINativeCredentialAdapter` in
`daimon.core.turn.openai_credential_requests` offers the shared Connect GitHub
or private-key card for a verified OpenAI target. Its `operator_enabled`
capability defaults to false and requires a supplied `NativeCredentialDelivery`.
The separate `register_openai_credential_tools` MCP entry point also defaults to
false and requires an authenticated `OpenAICredentialGrant` factory. Register it
only on an explicitly selected native MCP server; collisions with existing tools
refuse. Existing servers and Anthropic credential paths retain their dispatch.

The operator derives the native agent/session references, provider binding,
retained config revision and catalog revision from verified host execution.
Model arguments contain only the origin ID and optional key name. The adapter
checks the current origin, requester, fresh role and policy, tenant, resource
account, requester session account, workspace, persisted binding and native
agent readback. A channel copy may belong to the guild resource account while
its session belongs to the requester; neither is inferred from an MA identity.
Legacy agent/external credentials are refused on the native endpoint.
Private-key offers require both CONFIGURE and POST on Surface.CONFIG for a
non-admin requester; this explicit capability is stricter than the legacy path.

The delivery edge must durably register the full `NativeCredentialRequest` and
a native interaction handler before posting a real platform message. Its control
uses `nc_openai_input:` and must never enter a legacy MA resolver. A missing
edge refuses before I/O. This slice supplies the scoped controller and shared
card model, rather than installing a production registry or callback handler.
An operator can use the existing platform renderer for the card, retaining its
labels and replacing its dispatch identity with the native request control.

Offline proofs execute the existing native channel-copy helper through the SDK,
fresh catalog/native readback, identity-injected MCP dispatch and actual Discord
serialization. The MCP proofs inject a controlled `AuthIdentity`; they do not
certify authentication middleware or JWT verification. They observe a Connect
GitHub offer by checking the rendered product card title and repository-selection
description, without starting OAuth. Transport-owned button labels supply no
offer evidence. Mutation regressions reject altered product titles and
descriptions. They also observe a private-key card routed to the native target.
Full own-fork scenario setup
and production delivery bindings remain separate prerequisites. The request
records the shared 30-minute TTL; it does not schedule expiry or certify an
expired card. Credential consumption, probing, OAuth and submission ordering
are outside this slice and receive no changes.

The offline D8 consumer in `tests/judge/openai_credential_catalog.py` accepts an
explicit `opt-in-product` offer delivery. A3's native MCP grant must actually
call the scoped adapter: the consumer does not invoke a tool on the model's
behalf. The button copy comes from the product-built `ConnectCard.title`, and
the consumer reads the accepted Discord payload, with owned request, native
session/root, delivery window and database-bind checks. Product title and
forbidden-description mutations reach the frozen catalog predicates. The
default consumer has no offer binding. Qualify observed results **under opt-in
profile**; a full PASS also requires the original own-fork/admin, native host
turn and routing envelopes. Supplemental card joins contain no completion
certificate. Missing source execution or delivery stays PENDING, and OAuth,
credential submission and expiry remain outside this observation.

### C18 host termination evidence

The opt-in `compose_openai_host(..., termination=OpenAITerminationEnvelope(...))`
exposes a zero-argument async `host_termination_evidence` hook. It delegates to
N4's public `termination_evidence` API and returns all 21 `TerminationSnapshot`
fields, including fresh outcome identity, committed row count, reason, nullable
error/native-session facts, authorized account/tenant/platform and recovery flags.
Expected values never seed actual host identity or repair missing committed rows.
The envelope refuses foreign native scope/provider and every unsafe DB bind.
Before dispatch it copies the admitted session factory and its routing map.
Queued outcome writes and evidence reads use that owned snapshot, with fresh
ownership checks when allocating evidence sessions. A callback reconfiguring
its original primary or routed bind cannot redirect the writer or readback.
No application installs it automatically; default Anthropic behavior is unchanged.

The OpenAI owner inventory is fixed independently of executed cases (21 reasons);
a future reason requires an explicit audit update. Partial case coverage keeps
C18 typed PENDING and names the missing reasons. Available snapshots remain
accessible as supplemental evidence. Certification requires executed evidence
for all 21 reasons.
The offline proof uses the isolated actual SDK, native Agents SSE/history and
`run_prepared_turn` with the owned Postgres journal/outcome writer, including
failed/cancelled history replay, HTTP 429, deadline and pre-native runtime refusal.
This is offline evidence; it makes no live provider certification claim.
OpenAI host proof fixtures admit the configured owned database before engine
setup. Registered isolated CI remains supported; only plain contributor-local
databases skip, and foreign URL refusals have connection-trap regressions.

| Owner-reviewed reachable reasons | Real source of the host outcome |
| --- | --- |
| completed, interrupted, upstream, session_terminated | OpenAI native root/session events, `openai_io.py`, normal driver finalizers and reducers |
| rate_limited, connection_lost, interrupt_timeout | SDK HTTP status translation, driver reconnect/stop finalizers |
| requires_action, ceiling | Native origin approval pause; driver/run deadline finalizers |
| admission_balance_depleted, admission_cap_exceeded, admission_channel_budget_exceeded | Common admission balance and budget gates |
| admission_channel_protected, admission_agent_pinned_elsewhere, admission_channel_isolated, admission_denied | Common admission/policy gates; typed native runtime/config refusal |
| admission_concurrency_shed | Host turn queue refusal before provider dispatch |
| missing_config, resolver_miss, session_busy | Common admission configuration/resolution and reauthorization |
| unknown | Unclassified preparation/authorization exceptions, with genuine nullable pre-native session |

Excluded reasons: `reducer_bug` is reserved without a production raiser;
`refused` needs the Anthropic refusal idle (`stop_details`), which the OpenAI
codec never emits;
`mcp_degraded_empty` and `retrying_unsettled` require legacy SDK error records
which the OpenAI codec never emits (`reducers.py`/`openai_codec.py`).
`session_preparation_failed` and `session_agent_mismatch` arise in legacy
binding/compatibility, after the native profile dispatch in `bind_session_impl`.
`recovery_cancelled` and `recovery_failed` require legacy mapping replacement: the
explicit profile guard in `run_prepared_turn_impl` excludes OpenAI. Native history
replay retains the same session and correctly records `recovered=False`; a
recovered replacement row is not claimed. Admission/remaining driver branches
without executed native cases stay PENDING until those proofs are supplied.

### NEW27 native credential prerequisite (opt-in only)

`tests/judge/openai_rejected_token_catalog.py` binds the frozen NEW27 source to
an actual A3 MCP-form admission attempt. The current native adapter offers only
GitHub and environment input, so the result is
`PENDING: OPENAI_NATIVE_MCP_FORM_UNAVAILABLE` under the opt-in profile.
No credential submit, rejecting HTTP probe or scoped `used_at` readback has
occurred. N7's Anthropic consume-before-probe FAIL is not OpenAI evidence.

Native MCP-token request persistence and interaction submission are product
prerequisites. Their eventual conformance binding must invoke the real native
submit against N7's owned loopback rejecting probe and query its genuinely
minted request using fixed parameterized scope predicates. Do not translate
OpenAI identities into `target_ma_agent_id`, execute the source SQL template,
invent a request row, or change consume/probe ordering to earn this assertion.
When native MCP offers become available, the current diagnostic refuses until
that submit/probe/readback binding exists. No default runtime installs this hook.

### C03 tool-free QA envelope

`conformance_c03.C03Policy` is an explicit runner policy, never a default driver
setting. It pins `gpt-6-luna`, `tools=[]`, `service_tier=default` and
`multi_agent.enabled=false` in agent/session create requests and refuses missing
or changed values in native create responses and subsequent readbacks. Session
creation requires `environment.type=none`; an empty agent tool list cannot
disable the implicit Bash/apply-patch tools of a hosted sandbox.

The [Agents architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)
and [session-create reference](https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/sessions/methods/create)
were fetched 2026-10-12. A no-environment session requires initial input. The C03
arrangement therefore uses `openai.conversation_only`, one initial create turn,
and the canonical fixture's two distinct event POSTs: three model requests, no
container. The runner must observe the initial root completed and idle before
entering the unchanged fixture. An ambiguous send consumes its admission and
never licenses another POST. This policy supplies no live certification or
financial authorization; those require separate source pins, an envelope and GO.

C03 preparation and execution validate the signed Option A config/envelope pair
against its three-request, no-container, $1.0755 hold terms. Its model
`actual_input_limit` remains unset: the old 100k whole-run cutoff applies only
to the other retained cases, and cannot gate this explicitly priced workload.
Historical financial regressions keep their original rate validity windows
and clocks; their stale prices are never installed or admitted for live C03.

Every C03 live entry requires its reviewed runner pin and separate pinned GO,
including direct one-case invocation. The legacy/default-pin runner refuses
C03 before file, budget, key or database access. Financial preparation also
checks the signed envelope's own aware expiry; a newer GO cannot extend it.


C03 QA admission lives in `scripts/qa_openai_c03_gate.py`, outside the provider
wheel. The attempt identity and financial envelope path/hash come from the GO.
Execution requires its canonical sorted JSON hash on a `LEAD PIN` line in the
QA lane's operator notes, plus matching envelope identity, unchanged financial
terms, source/config/recipe scope, binding and expiry. The existing exclusive
attempt marker is keyed by identity across output directories; receipts also
carry that identity. Legacy source-only markers are joined to their retained GO
before admission, so moving output or reformatting a consumed GO cannot retry it.
Read-only preparation takes `--go-draft` to validate financial terms and create
source/recipe pins; it does not grant execution authority.
