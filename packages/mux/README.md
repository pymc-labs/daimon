# mux

The provider-neutral managed-agent contract: types, port protocols, errors,
profiles and admission, the `StateStore` protocol and its in-memory test
store (`mux.state`), with one driver package per provider under
`mux/drivers/`. Daimon wraps its existing Anthropic client with resource
ports while retaining host policy, authorization, locking and client lifetime.
Turn and event ports remain independently injectable in the driver factory.
Resource drivers include vault credentials, files and session administration;
credential specifications carry opaque references resolved for one request.
Credential errors and SDK request logs redact raw, quote-escaped and nested
repr/JSON secret values. Request codecs preserve the original JSON key order.
Session resource lists resolve all repository token references for the same
request. Native environment creation retains explicit null descriptions through
its closed environment config schema.

The import name is the top-level `mux`, not `daimon.mux`, so it can be split
into its own repository without a rename. `mux` never imports `daimon`, and
only `mux.drivers.<provider>` imports that provider's SDK; import-linter
contracts in the root `pyproject.toml` enforce both.

Install a driver's SDK with its extra: `daimon-mux[anthropic]`,
`daimon-mux[openai]` or `daimon-mux[gemini]`.

The full description is [docs/mux.md](../../docs/mux.md). Tests:
`uv run pytest packages/mux`.

Conformance adapters can declare individual fixtures PENDING with typed capability,
live-key or dependency reasons. Declarations remain visible and never certify.
The host can consume durable accounting outbox rows through its existing database
transaction, applying usage revisions and signed ledger corrections together.
The C12 conformance fixture requires a host accounting evidence hook with actual
usage rows, Decimal ledger amounts, correction totals and rollback/restart facts;
a transport without that hook remains pending for host billing certification.

`mux.conformance.budget.BudgetGuard` provides standalone `reserve`/`settle` spend
receipts for authorized manual probes. It requires an explicitly initialized,
pinned ledger with checkpoint and retained sequence lock, and refuses past 80%
of provider/total caps. It imports no recorder or provider SDK.

The Anthropic skills version extension also supports workspace-key downloads by
native version ID, omitting the skills beta header on that request. The ordinary
pinned-version port and generic recovery download keep their existing SDK requests. Both paths enforce the same
host-provided scope and skill grant before I/O.

`mux.conformance.budget` provides standalone manual budget admission and spend
receipts without recorder or SDK imports. Its pinned ledger, checkpoint and
stable sequence lock refuse reset/rollback attempts. Authorized operators supply
reviewed model prices and enforce token bounds across the whole probe. Admission
also requires an exact model in the immutable `LIVE_MODEL_ALLOWLIST`: OpenAI
`gpt-6-luna`; Anthropic `claude-haiku-5-5`; Gemini `gemini-3.8-flash`,
`gemini-flash-latest` or `gemini-3.5-flash-lite`. The Gemini harness must start
with 3.8 Flash and may use the latter two IDs only after a 503 from 3.8 Flash.
Unknown providers, unlisted aliases and other models receive a blocked receipt
with zero spend before the callback can access a key. An allowed model without
reviewed prices still refuses; policy membership alone cannot enable a live call.
Anthropic documents Haiku 5.5 on its official
[model page](https://platform.claude.com/docs/en/models/haiku-5-5/overview#model-ids).
The canonical budget config uses the conservative Haiku 5.5 rates for prompts
over 100,000 tokens: input $0.50, cache read $0.05, cache write $1 (1h upper bound),
output $2.50 per million tokens. Its flat price schema cannot select a tier per
request, so shorter prompts are overestimated. See the official
[pricing table](https://platform.claude.com/docs/en/about-claude/pricing#model-pricing).

`mux.conformance.recording` retains only normalized mux events and request
metadata, excluding raw HTTP bodies, body values and opaque native provenance.
Tool/action argument mappings become a fixed omission sentinel; export and replay
refuse arbitrary mappings outside the closed normalized payload schema.
An offline event fake re-runs fixture checks with the same credential audit;
unsupported opaque event content refuses recording. Native codec replay is
outside this format. `live_probe.run_probe` optionally combines reservation,
callback, settlement and normalized export. See `mux/conformance/README.md`.

### Anthropic turn event translation

`AnthropicManagedAgents` registers an `AnthropicEvents` port over the
host's existing SDK client; an explicitly injected Events implementation still
wins. The host bridge is experimental and requires `DAIMON_TURN__PATH=mux`;
unset or empty selects the unchanged `legacy` path. No database writes or
client retry settings are added by the bridge.
Sessions lifecycle remains a separately injected port.

The Events port checks host resource authorization before sending, listing,
streaming or requesting cancellation. `open_stream` returns after the HTTP
connection opens, independently of its first event, so hosts can preserve
connect-before-send order even on idle sessions. Records and previews normalize to owned
`Event` values; previews use separate identities and never imply completion.
Required actions carry the referenced call and retain native thread routing.
Tool results with absent or null content retain their call pairing and native
record while exposing empty neutral content. Listed pages follow the SDK's
continuation rule; terminal pages expose no cursor. Malformed records raise an
owned provider error on both the list and stream ports.
Unknown native records survive as `native.*`, while subagent status records use
`agent.thread.*`. A cancel receipt acknowledges a request, not a stop. A lost
POST acknowledgment returns `outcome_unknown` without a port-level resend.

Anthropic has no root-turn IDs, resumable SSE cursor or atomic turn precondition.
`EventNormalizer` can receive the host's root identity; a chronological history
walk otherwise anchors turns on user input or the first running record. Pending
actions whose source call is outside that walk remain native. SSE cursors, turn
preconditions, reconciliation remains explicitly unsupported.
The closed `anthropic.session_system_message@1` input schema carries only
privileged text framing and validates the final-event ordering before I/O. The cancellation
and host bridge units provide their policies separately.

`usage.observation_from_event` converts a model-request end span to revision 1 of an
owned `UsageObservation`, retaining the native meter. Input tokens include cache
reads and writes; a missing bucket remains unknown rather than becoming zero.
`usage.observed` events reference the observation ID and revision. No accounting
write or provider call occurs during normalization.

Native provenance can retain an opaque JSON `record` for the temporary host
compatibility edge, including native error retry timestamps and permission
details. It never contains an SDK object and survives JSON and pickle round
trips. Neutral reducers consume the normalized payload.
Session lifecycle ports now create, retrieve and list native sessions, with archive
delegated to session administration. Closed `anthropic.session_create@1` and
`anthropic.session_resource_create@1` configs preserve omitted and explicit empty
fields, agent overrides and mount ordering. Session specs carry extensions and
records carry an opaque optional native snapshot for the temporary host codec.

Anthropic model spans convert to `UsageObservation` in the usage driver,
retaining event IDs, timestamps and the untouched native meter. Neutral input
counts include cache stages; the host projects them into its existing billing
and telemetry columns. Temporary host compatibility entrypoints accept existing
SDK callers while turn and adapter ports migrate.

The Anthropic usage implementation supplies scoped page reads and a typed
`UsageWalk` for model-request spans. Both use the shared pure converter and
preserve the SDK paginator, native event time, meter and revision 1. Model
identity may come from an existing session snapshot without a provider lookup.
Billing reconciliation combines this span walk with `anthropic.session_walk@1`
under an explicit workspace scope. Tenant, watermark and billing-exempt policy
remain in the host, including its handling of unknown native session statuses.

Gemini's explicitly constructed `GeminiManagedAgents` driver runs the non-core
`gemini.inline_reuse` profile. The host injects transactional driver storage,
a StateStore and a private transport; importing or constructing it makes no
provider requests. Agents and environment definitions are local inline
configuration records. Turns respecify that configuration and reuse both
`previous_interaction_id` and the returned environment ID. Expired or missing
continuity raises `ContinuityLost` instead of provisioning a fresh workspace.
The scripted transport and memory storage are offline test tools, not a
production persistence implementation or a live certification.

Gemini skills are host-stored, immutable text bundle versions, deployed as inline
`.agents/skills/<id>/` files when an interaction first creates its workspace.
Existing pins do not follow later publications. Uploaded artifact bytes remain
in the injected host storage; UTF-8 uploads can seed inline workspace files.
Workspace artifact discovery uses the documented Files API workspace tar snapshot.
Downloads verify the discovered content digest; unavailable or changed files
produce typed errors. Unsafe, oversized or malformed archives are refused without
extracting files to disk. Native vaults and workspace-file deletion are unsupported.

### OpenAI driver core

`mux.drivers.openai.OpenAIDriver` is an explicitly constructed, unwired Agents API
driver for persistent hosted workspaces and opt-in conversation-only sessions.
It uses the pinned OpenAI SDK's public HTTP primitives, normalizes root outcomes
and previews, reconciles paginated saved work while buffering the stream, and
reports nullable revisioned turn usage. Host authorization, recovery checkpoints
and revision allocation are injected. Unsupported native preconditions and
unsupported resource updates refuse explicitly. See the driver's README for verified
endpoints, state ownership and offline validation. Existing defaults stay Anthropic.
The persistent profile is core with evidence for all nine mandatory capabilities.
An explicitly selected OpenAI backend defaults to that profile and requires a model.
Multiagent remains unsupported and complete workspace export/import is unknown.

The native `anthropic.session_walk@1` extension exposes `SessionWalk.walk(scope)`
as an async iterator of neutral sessions with opaque native snapshots. It sends
no list filters and follows the SDK paginator exactly. Workspace-wide billing
sweeps pass a justified platform scope; tenant walks retain grants and tag checks.
In-place session changes use closed `anthropic.session_update@1` configs, carried
by SessionSpec and UpdatePlan extensions. Planning performs no provider read;
apply checks the caller's plan revision and makes one native update. Anthropic's
endpoint has no server revision CAS. Unsupported generic revision/environment
changes and fresh-state requests are refused; they never silently replace a thread.

Session output delivery uses `anthropic.outputs@1`: one listing page per poll,
with opaque entries preserving partial SDK response fields. The host filters
pending entries before reading delivery metadata; no timestamp is synthesized.
Buffered download and deletion retain the managed-agents beta. Standalone bundle
uploads and the workspace-wide TTL queue retain the default Files API headers.
The host keeps settle timing, exclusions, size limits, consent deferral and
post-before-delete ordering. No additional provider requests are introduced.

The native session-resources extension adds lazy `walk` and identity-returning
`add_file` methods. Existing methods retain their behavior. The host stops at
the first `.env` mount without fetching later pages and records the one add
response identity without a retrieve. Session and file grants are checked before I/O.
OpenAI's opt-in offline conformance registration lives in
`mux.drivers.openai.conformance`. It exercises the actual pinned SDK and driver
over synthetic HTTP/SSE replies, with fresh state per shared fixture. Six probes
pass; twelve declare typed pending dependencies or unsupported capabilities.
Broken driver variants must fail shared fixture checks. Registration adds no
provider discovery, host wiring or live certification; see the driver README
for the complete matrix and cleanup context.
`anthropic.memory_stores@1` supplies memory store creation, reads, archival and
deletion, plus a native prefix walk for the temporary host compatibility edge.
Memory paths and prefixes are distinct owned records; SDK objects remain inside
the driver. The existing client supplies retries and its agent-memory beta
header. Operation keys pass through without caching or deduplication, and
conditional writes fail before I/O. Tenant and account checks apply to every
store reference, with tagged-record and authorized-list checks after native
reads.

Environment identity reads retain omitted or null native configuration fields,
including the SDK snapshot and field-presence semantics used by host caches.

M0 memory listing/content reads and environment identity retrieval preserve the
SDK's partial native response snapshots through operation-specific driver
extensions (`anthropic.memory_stores@1` native reads/walks and
`anthropic.environment_reads@1`). The compatibility edge reconstructs SDK records without requiring
unused paths, identifiers, configuration or timestamps. Prefix and unknown list
rows retain their original discriminator and pagination behavior. Tenant and
account checks still run before requests, and environment metadata checks still
run after the existing response. Neutral resource records remain validated.
Thread handoff uses lifecycle reads with its tenant/account context. Workspace
bundle reuploads use the native outputs extension with the existing Files API
headers and bytes. Rehosting consumes only the returned ID, so a partial upload
reply still produces the full handoff and cleanup entry. The request helper is
shared with Artifacts; the host retains checkpoint billing and fallback policy.

Workspace transfer uses `anthropic.workspace_transfer@1` to carry the inline
full/transcript/history rung. The closed schema stays in the driver. Pure native
export declares its archive, digest, transcript presence and losses with
`best_effort` consistency; restore verifies the payload, source authorization
and accepted losses, then returns neutral mounts for the existing create call.
No manifest upload, provider lookup, second session create or first send is added.
The host retains the one billed checkpoint, its access fence, fallback notices,
quoted transcript and system-message policy. Generic lifecycle export/restore
stay unsupported; this explicit native extension handles the existing MA ladder.

OpenAI's resource slice supplies default inline skills, exact binary artifacts and
scoped vault ports. Its persistent profile is core with nine-capability offline
evidence and is the profile for an explicitly selected OpenAI backend; a model is
still required. Existing unconfigured channels resolve to Anthropic as before.
C09 joins the five earlier offline passes; twelve typed dependencies remain pending.
The guarded live smoke failed and provides no live certificate.

Gemini offline conformance now proves C05 through canonical saved-step GETs,
durable SSE gap markers and restart/deduplication checks. Its adapter declares
`step:0` as the saved-message identity; other adapters retain the C05 default
`item`. The matrix is eight PASS and ten typed PENDING. Snapshot tests alone
do not certify C09's missing vault/session-deletion requirements, and host
scenarios stay pending until their actual host evidence is connected.

Cancellation receipts never establish a stopped turn. `Events.wait_stopped`
requires an authoritative root idle or termination record before its deadline,
ignores previews and interrupt echoes, and closes the private stream on every
exit. The typed `anthropic.event_history@1` extension normalizes a full SDK
paginator walk without changing its request or termination rules.

The typed `anthropic.session_reads@1` extension returns the original native
session JSON after scope, reference and tenant-tag checks. It preserves partial
SDK responses and nullable location metadata without constructing a binding;
generic session records retain their validation. Turn seal stamping uses this
read and the existing metadata-only update port, with no extra provider request.
Prepared legacy replay and interruption pass the caller's admitted scope to the
private transport while preserving SDK traffic and recovery behavior.
The Anthropic environment driver exposes a scoped create-and-discard edge for legacy environment forks that ignored the SDK reply. It validates the request configuration and tenant authorization without decoding unused response fields; generic Environment records retain their existing validation.

MCP agent chat and session inspection use tenant/account scopes derived from
verified MCP identities. Their native `anthropic.session_tools@1` extension
provides the existing user-message/interrupt send, native session retrieval,
lazy SDK session walk and single event-page read. Retrieval preserves nullable
native location metadata without constructing or changing a durable provider
binding. It preserves omitted filters, opaque cursors, partial
SDK replies and send echoes; the adapter retains ownership checks, seals,
mutation fences and Decimal cost folding. Scope/ref/grant mismatches fail
before I/O, without a platform or legacy-host authorization escape. Operation
keys pass through in M0, without a second replay cache. Bundle existence probes
use the Artifacts native metadata projection so unused reply fields remain
optional. Offline ScriptedTransport proofs compare raw request body bytes and
protocol headers, as well as SDK projections and call order.

Gemini's explicitly invoked probe harness defaults to MockTransport and a private
spend ledger. It records the offline C-matrix separately from its narrow SDK
smoke and cannot issue a full live certificate. Live mode uses the pinned key
file and reviewed shared-ledger budget configuration ($30 allocation, $24 stop).
It requires exactly `gemini-3.5-flash-lite` before reading a key or creating a client.
See `mux/drivers/gemini/LIVE-CERT.md` for the prepared command and remaining gates.
MCP repository binding and vault summaries use the verified tenant/account scope
and an exact account/agent-derived vault-name grant. Repository writes retain
create-before-commit, compensation and old-credential revocation ordering.
Bundle uploads retain the bounded spool, multipart filename/media type and
cleanup retention. Hosted charts retain the lazy Files API scan cap, Managed
Agents beta header and binary read. Hub sessions retain account, legacy-access
and seal filtering; skill details count partial native version rows. The native
compatibility edge consumes only fields used by these existing callers.
`anthropic.core_admin@1` retains the account-purge session paginator, hard-delete
request and best-effort orphan interrupt without additional provider reads.
Tenant operations check the host's authorized agent/session IDs before I/O.
Its workspace-wide snapshot walks require explicit platform Scope and preserve
`limit=100` with omitted archive filters. Destructive test cleanup keeps both the
host opt-in and disposable-sentinel gate, its early exit and archive fallbacks.
Native snapshots retain SDK partial-response fields without requiring unused
agent/session properties. DM archive retains the selected TurnIO/backend
dispatch. Responder identity keeps its SDK read and untouched response for
configuration snapshot backfill, including null channel and thread metadata.

### Default capability conformance

The supplementary F1 scenario in `mux.conformance.default_capability` provisions
the authored Daimon default's eleven skill bundles and immutable pins, mapped
builtin tools and reserved `daimon-mcp` attachment. One scripted turn loads the
pinned file-handling skill, invokes the real read-only `describe_agent` and
`list_my_sessions` MCP tools, exercises the six
logical builtin capabilities in a disposable workspace and completes one root.
`run_default_capability` and `replay_default_capability` are explicit runner
entry points; the C01–C18 matrix stays unchanged. The initial Anthropic evidence
is a synthetic SDK/driver turn and a normalized-only tape, not a live certificate.

Adapters declare `builtin_mapping` from logical capabilities to actual ToolSpecs
(e.g. reads through bash, writes through apply_patch). Incomplete mappings are
typed PENDING before provider I/O. `atomic_revision_pin=False` provisions without
a native session revision precondition and adds an explicit capability gap to a
passing result; immutable skill pins remain mandatory. Replay checks pairings
and recorded results, while free-form tool arguments remain omitted. See
`mux/conformance/README.md` for the contract and offline reproduction command.
dispatch. Responder identity uses `anthropic.session_reads@1` with the authorized
row's tenant/account scope, retaining the SDK response for configuration snapshot
backfill, including null channel and thread metadata, without binding projection
or an additional read.

OpenAI's supplementary F1 adapter provisions the eleven authored default skills,
declares their logical builtin mapping to hosted Bash (write/edit via separate
`apply_patch` commands), and attaches `daimon-mcp` through remote HTTP MCP. Native
command/MCP items normalize into paired tool records. The checked-in F1 tape is
scripted offline evidence with fresh provisioning on replay; it makes no live,
model-quality, tool-argument or atomic agent revision-pin claim. See the OpenAI
driver README for the explicit mapping and supported boundaries.
Gemini's supplementary F1 default-capability adapter is replay-only and returns
typed PENDING before provisioning while complete binary skill deployment and
authenticated MCP/session seams are unavailable. Ten complete text skill bundles
are supported; that subset cannot certify the full authored default. See
`mux/drivers/gemini/DEFAULT-CAPABILITY.md` for the explicit adapter entry.

Gemini F1 maps bash, read, edit, grep, glob and write through its native
`code_execution` tool. Native scripted records exercise each route in normalized
replay; missing calls fail. The complete default remains typed PENDING for binary
skill deployment and authenticated MCP/session adapters.

The Gemini live smoke pins `gemini-3.8-flash` and advances to the two reviewed
Flash fallbacks only after a definite create HTTP 503. Separate reservations,
fallback receipts and per-response usage counters preserve failed holds and
settle known cumulative usage once. The moving alias remains estimated until
its resolved tariff is verified. See `mux/drivers/gemini/PRICING.md` and
`LIVE-CERT.md`; MockTransport preparation reads no real key.
### Offline Anthropic C01–C18 registration

The test adapter in `tests/drivers/anthropic/conformance_adapter.py` explicitly
registers `anthropic.offline` with the shared conformance `Registry`. It creates
fresh, unmodified `AnthropicManagedAgents` drivers using the real Anthropic SDK
and `daimon.testing.ma_transport.ScriptedTransport`. The adapter stays in the
test tree so production mux does not depend on `daimon.testing`. It does not
load credentials, discover providers or fall back to a network transport.

From the workspace root, run the whole matrix with:

```sh
uv run python packages/mux/tests/drivers/anthropic/conformance_adapter.py
uv run pytest -q packages/mux/tests/drivers/anthropic/test_conformance.py
```

The JSON report includes every C01–C18 result and typed reasons for PENDING
entries. C15 (migration refusal) and C16 (extension/raw-handle isolation) pass
through the actual SDK. The other sixteen remain PENDING: host attribution,
continuity, operation/journal/lease recovery, root occupancy, usage corrections,
preparation, deployment, billing, binding adoption, selection, wake fencing and
outcome persistence are not bridged; hard session deletion is unavailable.
C10 specifically assumes `memory_stores` must be refused, while Anthropic's real
profile declares it native. The adapter preserves that profile and reports the
missing provider-appropriate scenario. Native cancellation and foreign-scope
checks have separate regression proofs; they do not promote a partial scenario
to a matrix pass. This report is partial evidence, not an all-pass certificate.

Host profile dispatch is in `daimon.core.mux_backend` and `daimon.core.turn`.
Provider codecs register `register_turn_preparation`, `register_turn_backend`
and `register_turn_codec`. Preparation returns a real authorized native session;
factories preserve its identity and codecs own provider stream ordering,
replay/cancel truth and compatibility display records. Transport and durable
journal/usage dependencies are injected in `TurnRuntime`; mux still imports no
host package. Channel admission enables a profile separately, after its host
implementation exists. The default Anthropic path and generic ports are unchanged.
Live probe receipts separate `actual_usd`, `held_usd` and `accounting_status`.
Complete token observations under a dated, pinned price card plus measured
container/session time settle immediately; unknown evidence remains an
unverified hold. Default plans reserve 20k input/2k output tokens, create-only
plans zero, and hosted sessions add an explicit runtime allowance. Lead-approved
reconciliation appends a new receipt and preserves the full ledger history.
Anthropic MA settlement requires a session allowance and measured runtime;
missing or empty measurements retain the hold. Known request bounds still
detect overruns when other usage is missing and block subsequent dispatch.
See `mux/conformance/README.md` for the offline settlement and approval API.
### OpenAI session resource controls

The opt-in OpenAI driver accepts driver-owned `SessionControls` for an explicit
hosted container size and a strictly positive integer USD-cent session spend
limit. Omitted controls retain the existing provider/template defaults and request
bytes. The [driver README](mux/drivers/openai/README.md) records the official
2026-10-10 protocol audit, SDK binding boundary, nullable usage and separate Admin
cost-accounting requirements. These controls do not register a host backend.

Daimon's prepared mux turn path composes `PostgresStateStore` at its admitted
binding boundary. The host owns the slot lease, persists and claims mutations
before delivery, and journals normalized records before its existing reducer
consumes them. Shared turns retain their persisted accountless slot and the
writer's operation scope. Send claims survive restart; uncertain delivery never
permits automatic resend. The Anthropic stop observer can forward its normalized
wait events to this same journal without changing provider requests. Legacy
turns and unbound direct port callers retain their previous behavior. These host
proofs do not promote the separate C01–C18 adapter's PENDING entries.
