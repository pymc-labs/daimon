# OpenAI Agents API driver

This is an unwired, offline-tested provider driver. Daimon's registry, configuration,
turn path and default remain unchanged: existing channels resolve to Anthropic.
`OpenAIDriver` selects `openai.persistent_workspace` (hosted compute) or the explicitly
named `openai.conversation_only` profile (`environment.type = none`). It never
silently provisions a workspace for the latter or starts a replacement session on
continuity loss. Native memory stores are unavailable and fail admission.

## Source and transport

The supplied official Agents API snapshots were fetched **2026-09-14** and checked
**2026-10-09** (`source-O-{overview,architecture,configuration,sessions,events,lifecycle,skills}.txt`),
alongside the official reference pages linked below. The Python package remains pinned
`openai>=2.54.0,<3`; version 2.54 has no generated `beta.agents` binding. The transport
uses that SDK's public `AsyncOpenAI.get`, `post` and `delete` primitives, with
`OpenAI-Beta: agents=v1` and `max_retries=0`. The lead approved this approach at
15:13Z on 2026-10-09. There is no Responses API or Agents SDK substitute.

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
not on a provider CAS. Generic tool confirmations and native input bypasses refuse.

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
intent is stored in reserved `mux_skill_pins` metadata, bounded at 512 characters.
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
reruns remain subject to review of this fix.

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
guarantees or resource workflows. No recorder fixtures were made.
selection still require their shared fixture adapters. A guarded smoke on
2026-10-09 returned `invalid_request`, with unknown billing and a retained $0.45
reservation; it did not establish live correctness. No recorder fixtures were made.

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
