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
merges buffered updates. A disconnect or pagination failure publishes nothing.
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
from mux.conformance import Registry, run
from mux.drivers.openai.conformance import register

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
| C04 | PENDING | Host lease/fencing and transactional journal crash recovery |
| C05 | PASS | Child-first completion, preview authority, EOF occupancy, saved-page overlap, explicit gap and one root outcome |
| C06 | PASS | Cancel acceptance holds occupancy until an observed interrupted root |
| C07 | PENDING | Shared usage revision/outbox StateStore scenario adapter |
| C08 | PENDING | Conditional required-mount replacement refused; host preparation adapter absent |
| C09 | PASS | All artifact pages, exact binary bytes, typed truncation and actual shared-vault retention |
| C10 | PASS | Required unavailable capabilities, extension versions and foreign scope refused |
| C11 | PENDING | Deployed repository/MCP bindings and separate display-title mapping |
| C12 | PENDING | Host billing identity and overlapping-grain pricing hook |
| C13 | PENDING | Host new-slot adoption and restart persistence adapter |
| C14 | PENDING | Existing/new-thread provider selection adapter |
| C15 | PASS | Typed migration refusal preserves binding without provider writes |
| C16 | PASS | Raw handles and undeclared extension/native-input bypasses refused |
| C17 | PENDING | Host wake-generation adapter |
| C18 | PENDING | Host termination/outcome-row mapping |

Every pending entry has a typed reason and prevents certification. Supplying a
standalone memory StateStore would not prove that host send/recovery ports use
it. C07 also expects a full correction sequence in one reconcile call; OpenAI
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

`tests/conformance/recordings/openai/F1-scripted.json` is **scripted offline
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
