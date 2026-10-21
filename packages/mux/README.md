# mux

Postgres-backed resource ports are available in
`daimon.core.stores.selfhosted_resources` for host injection. Admission resolves these records after the selected profile passes its
capability checks. `daimon selfhosted init` initializes matching named records
and prints their IDs without provider credentials. Both the host and driver use `HarnessConfig`
from `mux.drivers.selfhosted.config` for `selfhosted.environment` version 1.

`HarnessConfig.session_pool` opts direct transport into ACA dynamic-session
routing. It requires HTTPS and Entra audience `https://dynamicsessions.io`;
the host binds that audience to the exact pool origin before acquiring tokens.
`DirectTransport(session_pool=True)` adds the deterministic sandbox identifier
to every C3 call while preserving polling cursors and request bodies. Ordinary
direct and Foundry invocation transports retain their existing defaults.
Its transport fields are `harness_url`, `auth_mode`, `auth_ref`, optional
`packages_note`, and `transport` (default `direct`). `foundry_invocations`
requires `agent_name` and `project_endpoint`. Resource `account_scope_id` is
the tenant UUID. Foundry agent names are 1–63 ASCII letters, digits, underscores
or hyphens and must start with a letter or digit. Full mux specs are persisted
as JSONB alongside names,
revisions and lifecycle timestamps. Active names are unique within a tenant;
archived names can be reused, and name/ID resolution excludes archived rows.
Mutations use account- and resource-kind-scoped keys, with principal ownership.
Receipts contain only resource references/revisions and expire after seven days.
Account erasure previews and deletes its receipts while retaining tenant-shared
agent and environment records. Personal writes take the same account lock as
erasure and require a live account. Deployment bootstrap uses the explicit
`operator`/`cli`/`cli-deployment-operator` scope without personal attribution.
Resource retries reread the row and refuse deleted or changed revisions. Writes
prune expired receipts for their tenant; operators can prune idle tenants with
`cleanup_expired_receipts` in the store module. Archive preserves historical
resources; hard deletion invalidates any session still referring to that ID.

The shared resource validator rejects credential-shaped configuration values, credential
URLs (including package/index/VCS sources), arbitrary extensions and MCP tool-policy dictionaries. Bearer references
must name environment variables; Entra references must be audience URIs.
MCP credential references must start with `HARNESS_MCP_CRED_`; credentialed
MCP endpoints require HTTPS except on loopback.
Names, instructions, descriptions and package notes remain free text.
Routing uses HTTPS, with loopback HTTP accepted for local use. Direct HTTP on
internal service names, RFC1918 IPv4 or ULA IPv6 requires `allow_insecure_http=true`;
public, link-local and reserved IP addresses are refused, including alternate IPv4 spellings;
this permits the in-cluster ACA fallback explicitly. Foundry fields are accepted
only for the Foundry transport.

Source loss consent can name the unsupported native URL-fetch capability as
`builtin_tool:web_fetch`. This does not waive other builtin tools or authorize
shell-based emulation of the missing native tool.

MCP source permissions can be resolved per tool with `mcp_permissions` without
relaxing native toolset projection. Declared losses can name one unavailable
MCP tool as `mcp_tool:<server>:<tool>`; this does not waive sibling capabilities
or authorize executing the unavailable tool.

OpenAI session observations preserve native busy occupancy even when the active
root is not visible yet. Accepted creation is not retried, and empty turn history
does not admit a new turn or certify completion.

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

OpenAI probe holds can be proposed for reconciliation by the host's offline
`daimon.testing.conformance.usage_reconciliation` command using captured Admin Usage/Costs
exports. Only an externally audited isolated project/day window with one run and
finalized billing qualifies. Unknown or mixed attribution keeps the hold. The
captured effective requests must cover the full project, and billed line-item
amounts and usage counts must match independent complete-project evidence.
The command never discovers an Admin key or approves/applies a ledger correction.
The existing `BudgetGuard` remains the sole append-only application path.
Foreign host billing adoption requires authorized native preparation, genuine
usage observations, bounded authoritative usage replay within the admitted
journal lease, and an adapter that omits unverified cost. An accounting posture
does not enable a profile. Aggregates require disjoint transitive coverage,
including absent descendants; their coverage sets remain fixed across revisions.

The host's offline `daimon.core.usage_delegation` API accounts complete,
explicitly attributed OpenAI root/child turn inventories atomically. It retains
native child IDs/meters, refuses reassignment or omitted known work, and charges
measured shared infrastructure once. Unverified membership/disjointness, models,
prices or measurements cannot establish actual spend. A native collector must
prove the scope before delegation is enabled; this API does not register a
profile or lift the delegation-disabled host restriction.

The host's `daimon.core.selfhosted_usage` consumes selfhosted incremental
model-request observations through the transactional usage outbox and credit
ledger. Before model I/O, runtime admission must persist the deployment,
underlying model and effective tariff; the recorder settles from that snapshot,
never current deployment mappings. Deployment/model aliases share the admitted
underlying-model accounting identity across revisions. Native meters stay
unchanged and receipt context retains the original aliases. Unknown usage and
pricing gaps remain pending, visible in the paginated `daimon usage pending`
report. This host API does not enable execution by itself, and mux continues
to import no Daimon code.

Conformance adapters can declare individual fixtures PENDING with typed capability,
live-key or dependency reasons. Declarations remain visible and never certify.
The host can consume durable accounting outbox rows through its existing database
transaction, applying usage revisions and signed ledger corrections together.
The C12 conformance fixture requires a host accounting evidence hook with actual
usage rows, Decimal ledger amounts, correction totals and rollback/restart facts;
a transport without that hook remains pending for host billing certification.

`daimon.testing.conformance.budget.BudgetGuard` provides standalone `reserve`/`settle` spend
receipts for authorized manual probes. It requires an explicitly initialized,
pinned ledger with checkpoint and retained sequence lock, and refuses past 80%
of provider/total caps. It imports no recorder or provider SDK.

The Anthropic skills version extension also supports workspace-key downloads by
native version ID, omitting the skills beta header on that request. The ordinary
pinned-version port and generic recovery download keep their existing SDK requests. Both paths enforce the same
host-provided scope and skill grant before I/O.

`daimon.testing.conformance.budget` provides standalone manual budget admission and spend
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

`daimon.testing.conformance.recording` retains only normalized mux events and request
metadata, excluding raw HTTP bodies, body values and opaque native provenance.
Tool/action argument mappings become a fixed omission sentinel; export and replay
refuse arbitrary mappings outside the closed normalized payload schema.
An offline event fake re-runs fixture checks with the same credential audit;
unsupported opaque event content refuses recording. Native codec replay is
outside this format. `live_probe.run_probe` optionally combines reservation,
callback, settlement and normalized export. See `../testing/docs/conformance/README.md`.

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

Gemini skills are host-stored, immutable bundle versions; text deploys as inline
`.agents/skills/<id>/` files when an interaction first creates its workspace.
Existing pins do not follow later publications. Uploaded artifact bytes remain
in the injected host storage; UTF-8 uploads can seed inline workspace files.
Workspace artifact discovery uses the documented Files API workspace tar snapshot.
Downloads verify the discovered content digest; unavailable or changed files
produce typed errors. Unsafe, oversized or malformed archives are refused without
extracting files to disk. Native vaults and workspace-file deletion are unsupported.

Workspace snapshot members use the snapshot's 64 MiB budget, allowing a 40 MiB
file even though ordinary artifact uploads retain their separate 16 MiB limit.
Both the received archive and its expanded tar contents remain capped at 64 MiB;
member reads and artifact downloads use chunks of at most 64 KiB. This snapshot
limit does not change the host's 20 MiB compressed checkpoint-bundle policy.

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
`daimon.testing.drivers.openai.conformance`. It exercises the actual pinned SDK and driver
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

Workspace checkpoint runners carry the caller's authorized tenant and account
into the turn scope and native restore without another authorization lookup or
provider request. The production chat caller supplies its reauthorized account;
existing direct callers retain their optional-account semantics, request and
billing behavior.

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
See `../testing/docs/drivers/gemini/LIVE-CERT.md` for the prepared command and remaining gates.
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
host opt-in and disposable-sentinel gate, its early exit and archive fallbacks. The dev-lane ownership check also uses platform-scope walks with
`include_archived`, a has-any-environment/vault/memory-store probe, and a bare marker-agent create.
Native snapshots retain SDK partial-response fields without requiring unused
agent/session properties. DM archive retains the selected TurnIO/backend
dispatch. Responder identity keeps its SDK read and untouched response for
configuration snapshot backfill, including null channel and thread metadata.

### Default capability conformance

The supplementary F1 scenario in `daimon.testing.conformance.default_capability` provisions
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
`../testing/docs/conformance/README.md` for the contract and offline reproduction command.
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
typed PENDING before provisioning until an authorized GCS bundle publisher and
authenticated MCP/session adapters are supplied. All eleven full bundles have
offline GCS compilation evidence; that does not certify a live default. See
`../testing/docs/drivers/gemini/DEFAULT-CAPABILITY.md` for the explicit adapter entry.

Gemini F1 maps bash, read, edit, grep, glob and write through its native
`code_execution` tool. Native scripted records exercise each route in normalized
replay; missing calls fail. The complete default remains typed PENDING for authorized
skill publication and authenticated MCP/session adapters.

The Gemini live smoke pins `gemini-3.8-flash` and advances to the two reviewed
Flash fallbacks only after a definite create HTTP 503. Separate reservations,
fallback receipts and per-response usage counters preserve failed holds and
settle known cumulative usage once. The moving alias remains estimated until
its resolved tariff is verified. See `../testing/docs/drivers/gemini/PRICING.md` and
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
See `../testing/docs/conformance/README.md` for the offline settlement and approval API.

### OpenAI session resource controls

The opt-in OpenAI driver accepts driver-owned `SessionControls` for an explicit
hosted container size and a strictly positive integer USD-cent session spend
limit. Omitted controls retain the existing provider/template defaults and request
bytes. The [driver README](../testing/docs/drivers/openai/README.md) records the official
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

Claimed Anthropic mutations suppress SDK retries only during their delivery
call, so losing an acknowledgement leaves an unknown outcome for reconciliation
instead of a second native POST. Read operations and legacy requests retain the
injected client's retry budget; the shared client is never modified.

Host codecs carry native `UsageObservation` values separately from display
events and expose an async `replay_usage()` walk for interrupted, failed and
completed turns. The host's `ObservationBilled` posture records those snapshots
through its durable accounting outbox; it never constructs an Anthropic meter.
Consumers bind one billing grain, the authorized session/model, verified dated
prices and actual infrastructure cost. Unknown actual remains pending. SDK
display-event deduplication does not suppress higher observation revisions.

Host accounting requires fixed coverage sets across revisions of each alternate
provider observation ID. It refuses overlapping sibling aggregates at ingest,
including shared transitive descendants which have not arrived yet. Corrections
may update amounts within that fixed coverage; dynamic coverage is unsupported.

The host's provider usage edge allows `TurnEvent(usage=observation)` without a
native SDK display record. Prepared recorder selection requires the admitted
foreign profile and matching authorized native session. The original Anthropic
recorder remains unchanged. Native accounting is supplied by its observation
callback, independently of provider display events.

The host persistence context exposes fenced `record_many(session, events,
cursor=...)` over the existing atomic StateStore append. Provider recovery
collects all pages before publication; it does not need a private host fence or
an independent lease. Empty snapshots can checkpoint their explicit cursor.

### Explicit OpenAI host turns

Daimon's explicit channel mux path registers `openai.persistent_workspace` for
caller-private `gpt-6-luna` threads. Both `channel_backends` and `path=mux` must be
enabled, and the host must inject `OpenAIHostRuntime` with a scoped provisioned
native session plan, private transport factory, resource authorization, small
container, native integer-cent spend cap, durable recovery/revision adapters,
and accounting context. Missing injection refuses before transport/key access.
No unconfigured/default client, backend or Anthropic request bytes change.

Preparation claims creation once, publishes the actual native binding and
reuses its hosted workspace. Lost acknowledgements never permit another POST.
Model overrides use the documented session `agent.model` field and every
session read verifies the admitted model. A chat platform's system prompt
variant uses the documented `agent.system` override the same way. Native conditional revision pins are
not claimed. Changed bound configuration/plans, shared threads, sealed/read-only policy,
publishing restrictions, scoped Slack execution contexts and workspace transfer refuse until a native continuity policy is available.

The codec journals before display, preserves message/tool/result identities,
routes only current-root browser-origin confirmations, and keeps browser
credentials separate. Recovery publishes completed history atomically through
A4, restores the durable pre-input root baseline, and displays saved items
before their terminal. Cancellation completes only after observed native stop;
unknown EOF records a recoverable gap. Post-run accounting retains nullable
current-root turn usage and excludes earlier turns and unattributed children.
Usage revisions persist under the same slot lease across process restart.
Verified dated prices plus measured container cost can settle; missing evidence
stays pending without a zero-cost fallback or an Anthropic meter. Authenticated
remote MCP and paid host certification ship separately.

The Gemini host turn codec accepts an explicitly selected `gemini.inline_reuse`
channel and an injected, revision-pinned `GeminiDeployment`, driver `Storage`,
neutral `StateStore`, transport factory and accounting runtime. Its stream opens
lazily after interaction acceptance, preserves provider root identities across
replay/cancel and emits cumulative interaction usage through the neutral host
recorder. The host configures `gemini-3.8-flash` as primary; only create HTTP503
permits the reviewed Flash fallback chain. Preparation rejects missing stores,
foreign bindings, seals and unsupported deployment policies before dispatch.
Unsupported runtime/model/policy selections return the host's typed
`backend_unsupported` refusal. Native journal source identities are scoped to
the interaction, so reused-session turns and function continuations remain
distinct without changing neutral event IDs or ordering domains.
Gemini interactions always serialize configured tools explicitly, using `[]` when none are configured. This includes reused sessions and fallback creates; provider default search and URL tools are never enabled by omission.
The host's optional typed provider-action callback keeps browser origin access
and browser authentication separate. Provider codecs supply validated native
response objects and send the chosen body through claimed mutations; the mux
input contract has no arbitrary native-response bypass. Unsupported
credential forms remain refused.

The opt-in OpenAI driver accepts `mcp_secrets(scope, credential_ref, destination)`
for authenticated remote MCP connections. The host must authorize the reference
and exact HTTPS destination for that tenant/account before returning a bearer.
Agent definitions store reference intent and the closed `allowed_tools`/`required`
policy; the driver resolves each session's credential immediately before its
create request, preserving any admitted model and delegation controls. The native
session transport carries the bearer with a redacted SDK debug representation.
Missing resolvers, changed destinations/policies and mixed inline/vault auth
refuse before a session write. Omitted authentication keeps the existing request
shape. See the [OpenAI Agents MCP protocol](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp).
Offline SDK/loopback proofs cover discovery, owned reads and revoked credentials;
they do not certify live model execution or HTTPS service reachability.

The authenticated OpenAI MCP edge validates every saved native MCP tool before
any scoped credential resolver runs, including tools absent from the reference
map and agents with no references. Inline authorization, native credential
bindings and mixed authentication refuse before session creation. Only a
resolver value bound to the caller and exact destination enters the ephemeral
session request; actual SDK DEBUG logs retain its redacted representation.
Credential-free anonymous-only and custom tools preserve their existing request
bytes. All MCP destinations require HTTPS without userinfo, query strings,
fragments or malformed authority. A closed native tool/transport shape refuses
headers, authentication and undeclared credential carriers before resolution.
Every MCP tool copied into an authenticated session override must match its own
scoped resolver binding exactly; mixed bound/anonymous MCP definitions refuse
before agent creation. Authenticated sessions accept exactly one resolver-bound MCP server. Non-MCP
entries have exact type and key allowlists: `web_search`, `tool_search` and
`programmatic_tool_calling` carry only `type`; `function` permits only `type`,
`name`, `description`, `parameters` and boolean `strict`. Unknown types (including
MCP spelling variants), extra fields and multiple bindings refuse before any
resolver or session write. The override rebuilds these admitted tools and the
bound MCP connection; it never copies a saved tool object verbatim. Multiple
bound servers are deferred under RULES 24.

Final table rendering belongs to the platform adapter. The host's existing
`DAIMON_TABLE_RENDERING` tenant UUID map opts into Discord PNGs and Slack native
tables for Anthropic, OpenAI and Gemini alike; missing/false stays plain text.
Codecs and journals retain the provider's original text. See
[`docs/mux.md`](../../docs/mux.md#opt-in-table-rendering) for configuration.
Host adapters may consume normalized Events through an optional local lifecycle tap. Discord uses authoritative snapshots and ordered preview fragments for drafts, without granting previews terminal or history authority. ProviderUsageTotals retain observation provenance and are never reconstructed into Anthropic display meters.

Slack folds normalized message Events into local progress drafts through the optional lifecycle tap. The existing host terminal state owns answer delivery and control retirement; ProviderUsageTotals are never turned into Anthropic display meters.

Teams also uses the optional neutral lifecycle tap for local drafts. Child and stale previews cannot replace the active draft, and terminal callbacks close the existing card. Alternate-provider observations or a known provider turn with no observation never become Anthropic display meters.

### Offline Anthropic default-capability composition

`tests/parity/test_f2_anthropic_default_capability.py` reuses the Anthropic F1
scenario and its strict SDK script for all eleven authored skill uploads,
immutable pins, default builtin routes and one completed root. The existing
vault port resolves a host credential reference, and the native session
extension mounts that vault. The scripted managed-agents provider reads the
serialized credential, mounted vault and agent MCP destination before calling
the disposable loopback gate's real `describe_agent` and `list_my_sessions`
tools. Their actual synthetic owned results enter the native stream, then a
normalized-only tape is audited and replayed through a fresh driver.

Revoked or incorrect bearers fail the scenario. Foreign scopes refuse before
secret resolution or a native credential write; manifest-owned cleanup revokes
the bearer and removes the isolated schema. Run with a lane-owned local
`DAIMON_DATABASE__TEST_URL`. Inference, skill execution and file operations are
scripted, and MCP backing data is synthetic: this is offline composition
evidence, not live F2 certification. It opens no tunnel and reads no provider
key. Live Anthropic F2 still needs a disposable key and separate lead GO.

The optional TARGET-53 Anthropic catalog replay consumer exercises the existing
mux turn, persistence and native usage fold through pinned SDK MockTransport
inputs. It preserves unsupported-effect and host-hook gaps and uses actual
adapter text captures; it changes no product turn or channel default.
The host may use `daimon.core.stores.neutral_catalog.PostgresNeutralCatalog` for durable alternate-backend agent revisions and retained skill bundles with upload ownership. Its local revision is separate from native revisions: drivers continue to refuse unsupported native mutation. Tenant/account/provider-workspace boundaries and immutable exact-version bundles are enforced in the store; host policy and native calls remain with the resource edge. No mux dependency on this host store is introduced.

Host skill authoring decisions live in `daimon.core.skills.ownership` and
`daimon.core.skills.service`. The service accepts a tenant-authorized host
resource/ledger edge and opaque agent values; it has no SDK client or native
storage columns. Refusals precede upload, and the host records the upload before
attachment so a retry can reuse it. The default Anthropic edge preserves native
requests. This extraction does not provide missing alternate-provider mutation,
owned-bundle download or storage itself; the explicit host composition below
supplies retained bundles and immutable native replacement for OpenAI.

Explicit OpenAI and Gemini host resource operations now use
`daimon.core.neutral_resources.NeutralResourceHost` over the retained catalog.
The caller injects an authorized driver, tenant/account Scope, project, database
sessions and claimed operation key. `add_host_agent_skill` requires the caller's
fresh policy/pin gate; `fork_host_agent` repeats the existing FORK policy and
queues the copy's face. `set_channel_rule(resource_host=...)` uses this edge for
its copy and locked policy write, without an Anthropic resource request.

An Add-skill upload is committed as an exact checked bundle before attachment.
Attachment creates a new immutable native agent and publishes it under the
catalog's local CAS; old agent refs and already-bound sessions are preserved.
Catalog publication failures retain the native ref and log it for reconciliation.
A lost CAS can return the committed winner's identity on a same-key replay,
including an identity retained by a historical revision; it never proves the
replacement unpublished. This edge performs no automatic CAS cleanup without a
fenced identity/history proof. A restarted
Add-skill operation locates owned retained versions and reads the actual native
latest version; it never guesses from version ordering or creates another upload
after a known attachment failure. The caller owns operation claiming and unknown
native-upload reconciliation; this helper adds no retry loop or operation journal.

The `gemini.inline_reuse` edge retains UTF-8 skill files as exact concrete inline
versions and creates a new host agent definition for each catalog edit. Existing
sessions keep their earlier definition and pinned bytes. Updating an existing
inline pin also updates its version digest; forks clone owned bundles under new
skill ids and add `code_execution`. The real SDK receives retained files as
interaction environment sources. Binary inline sources return
`binary_inline_source`; native agent deletion returns `agent_delete` before a
catalog tombstone. Failed channel-write cleanup keeps that identity and preserves
the original write error. The host injects transactional Gemini Storage/StateStore;
offline in-memory fixtures do not certify production process-restart durability.

Forks preserve shared library pins, upload source-owned bundles under distinct
ids for the copy, omit foreign-agent skills and report failed copies. Owned
unversioned pins require an injected scoped resolver for the native default
version, which can differ from the newest version. Missing ownership, retained
bytes or resolution refuses before mutation. Native agent revision fingerprints
remain descriptive: session preparation uses unpinned native `Revision(local=0)`,
never the catalog CAS revision or that fingerprint as a provider precondition.

These helpers require the explicit persistent-workspace OpenAI profile and
catalog provisioning. Default Anthropic calls use their existing edge and native
bytes. Gemini host resource operations remain pending G2; live host/runtime
resolver wiring and model execution are separate from these offline SDK proofs.


Gemini authenticated MCP is opt-in through the scoped `mcp_secrets` callback.
It admits one HTTPS server with a closed `allowed_tools` policy, stores only
credential references, and resolves fresh credentials before each actual SDK
create (including existing Flash fallbacks). Unknown native authentication
carriers and unsupported policies refuse before provider I/O. The default
callback remains None. Anonymous MCP also requires a clean HTTPS destination;
userinfo, query/fragment carriers and invalid server names refuse at compilation
and the SDK edge before I/O or logging. See [the driver contract](../../docs/mux.md#gemini-scoped-mcp-authentication)
for admission, DEBUG redaction, continuation and offline proof limits.
OpenAI explicit worker session budgets use integer USD cents and require the
sent cap to appear unchanged in native session responses before host input.
Ordinary deployment-level caps preserve existing readback behavior; the
worker-budget compiler alone enables strict echo validation. Control failures retain scoped
acknowledged resource references for exact cleanup, without automatic creation
retry or an exact-spend claim.

The opt-in OpenAI offline adapter now exercises C04 through actual SDK delivery
and a restartable fenced StateStore bridge. Ambiguous accepted input remains
unknown without another POST or a guessed native input identity. Its seven
offline passes do not certify live crash recovery or production host storage.

OpenAI interaction preparation has an actual SDK proof composing exact skill
upload, immutable hosted pin, remote MCP, an uploaded file and an unavailable
function action. A file mount does not establish a repository checkout; canonical
C11 remains PENDING while those scenario requirements are unavailable.
The explicit Gemini `offline_sdk_adapter` component proves the unchanged
C06/C07 cancellation and usage oracles through actual SDK request/response bytes.
It retains C08/C09 refusals, declares other component envelopes PENDING and leaves
the standard offline matrix unchanged. No live settlement or host factory is
installed. See [SDK conformance component](../testing/docs/drivers/gemini/CONFORMANCE-SDK.md).
The host can register a deployment constructor through
`daimon.core.turn.runtimes.register_channel_runtime(profile, factory)`. The factory
receives `ProviderPreparationRequest` only when the selected provider preparer
looks up its runtime for an explicit admitted channel revision. It must return
the provider's public runtime with authorized native resources and durable state;
missing deployment constructors fail closed. `PreparedTurn.runtime` retains the
resolved dependencies for execution. No mux driver discovers host credentials.

Application deployment wiring can construct the public OpenAI runtime from an
operator manifest selected by the admitted tenant/account/channel/configuration
digest. Credential environment references are revealed only after preparation's
policy recheck; SDK construction remains private to the OpenAI driver. Default
channels do not inspect the manifest or provider credentials. Durable host
journal/usage adapters are used; unknown accounting stays pending.
Gemini full skill bundles can explicitly use `skill_bundle_publisher` on
`GeminiManagedAgents`. Its `SkillBundlePublisher` receives every original byte
and returns a tenant/account/version-bound `GCSBundleDeployment` with the exact
file digest manifest. Persisted receipts mount immutable directories at the
original skill paths, including binary fonts/images, and retain old version
pins after publication. The host owns authorized GCS publication/access and
retention; no publisher or credentials are selected implicitly. Without that
adapter, binary/oversize bundles refuse `skill_bundle_gcs_deployment`; normal
text bundles retain inline mounts. See
[the capability notes](../testing/docs/drivers/gemini/DEFAULT-CAPABILITY.md) for limits and
offline evidence. A scripted publisher does not certify a live full default.

The Gemini F2 offline proof retains the full default skill bundle and maps the
logical toolset to one native code-execution tool plus its declared authenticated
MCP server. N2’s loopback host serves both read-only tools; wrong/revoked bearers
fail. Explicit deployment/auth adapters are required; no live/CAS claim is made.
The full-byte bundle and F2 SDK proofs use the shared isolated test factory with
explicit fixture keys and owned sync/async transports; ambient provider settings
refuse before construction. Product client construction is unchanged.

### Typed provider-action cards

Discord consumes the typed provider-action callback for prepared foreign sessions, returning an offered NativeActionResponse object unchanged. Its cards authorize the requester and destination, suppress duplicate selections, and retire on expiry or cancellation. Authentication remains distinct from origin permission; a missing completion response produces no completion button.

Slack attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.

Teams attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.


Offline C14/C17/C18 conformance accepts optional async host fact hooks for backend
selection, durable wake fencing and persisted termination outcomes. Missing or
`None` facts remain PENDING; installed facts must satisfy the shared retention
checks. The reference provides no host certification.

C18's optional host evidence uses the public committed termination snapshot
contract, including fresh row identity/count, authorized tenant/account/platform,
error class and recovery. Null pre-native sessions are explicit facts, never
missing-field defaults. Incomplete provider envelopes remain typed PENDING.

C18 host conformance refuses missing native session identities for completed and
session-terminated outcomes; pre-native refusal evidence remains
explicitly nullable without replacing native session retention coverage. A queued
first-turn Stop is a legitimate interrupted outcome before native preparation.

### Tool-only post history checks

The offline outcome oracle's `no_blank_message` predicate checks complete
accepted Discord send/edit/delete history against an independently retained
send receipt and authoritative host root. A finalized card is a message body
even when the provider emits no answer text. A blank draft remains a failure
after an edit or deletion. Missing capture or acknowledgement remains PENDING.
`TurnEvidence.post_history` is optional and omitted from serialization when
absent, preserving existing consumer bytes. Authored native replay proves
host behavior only; it does not certify model behavior or remote execution.

I3 native bindings are exercised by
`packages/mux/tests/drivers/gemini/test_i3_native_replay.py`. Anthropic and
OpenAI consume unchanged recorded fixture bytes; Gemini consumes an owned
explicit `code_execution` tape. All three run the real host turn and Discord
serializer. OpenAI's empty HTTP 202 receipt is retained unchanged and joined
to the unique fresh authoritative native user item that echoes the accepted
input. That item has a separate acknowledgement field; no native receipt ID
is invented. The binder verifies the original scoped operation, immutable
request digest, journal ownership and completed root before attaching posts.
The manual conformance budget guard supports one lead-signed hold for a backend
column of up to 53 fully executable cells. Recorded counters size a conservative
token/runtime bound; disjoint complete native evidence settles each claimed
cell exactly once, using decimal precision 80 from measurement through totals.
Terminal closure releases never-started cells and retains
unknown usage as held. Runner adoption and native spending controls remain
separate from this offline API. See the [column admission contract](../testing/docs/conformance/README.md#whole-column-admission).

Gemini resource administration can explicitly select
`resource_catalog_enabled=True` with an owned `AgentCatalogTransport`. The
native catalog adapter compiles concrete bundle pins, fences creation before
POST and verifies independent SDK GET readback. Omitted tool and environment source
lists are equivalent only to compiled empty lists; explicit nulls, non-empty
capability changes and unknown environment fields still refuse. Local PostgreSQL CAS retains old
native IDs and authored revisions; Gemini provider versions are never invented.
The default is false and existing inline ports remain selected. The host also
requires its explicit default-false capability and the same enabled agent
adapter with exact Gemini profile/workspace routing; the guard remains effective
when off or mismatched. Independent readback uses the public verification edge.
Named-agent turns refuse the inline session plan. Whole D14/D5 cells remain
PENDING. See [resource catalog capability](../testing/docs/drivers/gemini/RESOURCE-CATALOG.md).

Manual probes may use `BudgetGuard.reserve_with_hold(..., minimum_hold_usd=Decimal(...))`
to reserve the larger of a priced token/runtime bound and an explicit cash
floor. The floor preserves the real limits, counts against the existing stop,
and stays held until complete measured spend is available. It grants no live
authorization; runners must verify their signed envelope before admission and
read credentials afterward. The `A7` receipt label identifies a paired probe.
Update ledger readers before writing that new label; old receipt bytes are unchanged.
Optional `maximum_hold_usd`, `expected_config_sha256` and `expected_ledger_id`
refuse stale installation pins or a reservation above the caller's verified
envelope maximum under the ledger lock. These checks do not verify a signature.
The ordinary `reserve` signature and existing adapter overrides remain unchanged.

`usage_settlement.actual_from_anthropic_sessions` combines one or more reserved
session walks into one `ActualSpend` for a single settlement. Supply normalized
disjoint model-request observations with independently verified served models
and matching measured running-time totals. Missing sessions, finality, cache
write durations, pricing basis or runtime retain the hold; foreign, stale and
overlapping evidence refuses. Known partial runtime still proves overrun floors.
Runner controls and native budget acknowledgements remain separate. Ordinary
guard admission and settlement use decimal precision 80 regardless of the caller.
Gemini application deployment uses an explicitly admitted manifest plan with a
required tools list (including `[]`), authorized native system/environment
specs and a deferred key reference. The host supplies complete durable SQLite
record transactions and separately committed attempt holds; the private driver
owns configured request/stream clients without ambient routing. Unknown actual
costs remain pending and holds stay reserved until reconciliation. No Gemini
runtime, manifest or credential is discovered for an unconfigured channel.


Explicit OpenAI deployment transports refuse ambient `OPENAI_*` overrides by
name before SDK construction and copies. They retain the selected credential,
project and origin, with request-owned HTTP clients using explicit TLS roots and
no environment proxies; see [the driver policy](../testing/docs/drivers/openai/README.md).
The private Gemini deployment constructor refuses ambient `GOOGLE_`/`GEMINI_`
settings and TLS/proxy overrides by name before SDK or HTTP allocation. Its
explicit key uses the Developer API with owned sync/async HTTP clients, an
explicit TLS context and no proxy. Each native interaction gets one attempt;
request and stream closure dispose both clients. This affects configured Gemini
transport only.

OpenAI also refuses ambient TLS/proxy settings before context creation, including
`SSLKEYLOGFILE`, so configured channels cannot enable TLS session-key files.
Credential-guidance backfills use the existing tenant-authorized agent update port. The host keeps its version check, prompt-only patch and best-effort boot behavior.
Legacy session budget-stamp backfill uses `anthropic.session_reads@1` with
the tenant/account scope authorized by the mapping lookup. Its native snapshot
keeps the SDK's partial metadata and exception behavior without projecting a
generic session binding. The host still commits the stamp before preparation
locks the mapping and leaves unreadable stamps for the next bind to retry.
Gemini native credential offers have a separate, default-off operator adapter
and MCP registration edge. They verify the authorized resource scope, durable
caller/binding/catalog identity and native target readback before posting shared
cards. Production native origin/registry wiring and timed expiry remain pending;
see `../testing/docs/drivers/gemini/NATIVE-CREDENTIAL-TARGET.md`.

### Tool-only post history checks

The offline outcome oracle's `no_blank_message` predicate checks complete
accepted Discord send/edit/delete history against an independently retained
send receipt and authoritative host root. A finalized card is a message body
even when the provider emits no answer text. A blank draft remains a failure
after an edit or deletion. Missing capture or acknowledgement remains PENDING.
`TurnEvidence.post_history` is optional and omitted from serialization when
absent, preserving existing consumer bytes. Authored native replay proves
host behavior only; it does not certify model behavior or remote execution.

I3 native bindings are exercised by
`packages/mux/tests/drivers/gemini/test_i3_native_replay.py`. Anthropic and
OpenAI consume unchanged recorded fixture bytes; Gemini consumes an owned
explicit `code_execution` tape. All three run the real host turn and Discord
serializer. OpenAI's empty HTTP 202 receipt is retained unchanged and joined
to the unique fresh authoritative native user item that echoes the accepted
input. That item has a separate acknowledgement field; no native receipt ID
is invented. The binder verifies the original scoped operation, immutable
request digest, journal ownership and completed root before attaching posts.

Gemini C18 host facts are available through the explicit offline
`daimon.testing.gemini_termination` envelope (see the testing package's
`GEMINI-TERMINATION.md`). Its independent reason inventory and exact committed
outcome snapshots preserve PENDING when execution evidence is partial. Default
Gemini registration and application runtime behavior remain unchanged.
The Gemini evidence envelope snapshots admitted database routing before callbacks
so queued outcome writes and readback retain owned binds. Its offline tests use
the shared database preflight; partial execution remains PENDING.

After an already-finalized Gemini host rate-limit failure, a retryable 429 from
the final usage fetch preserves that original failure and session. Correlated
retained interaction usage still passes through the existing recorder: unknown
usage remains pending, while known revisions are charged once. Storage,
validation and accounting errors propagate. Successful turns and reconnects
retain mandatory native usage reconciliation.

Explicit Gemini conformance recovery and inline-skill preparation hooks have
actual-SDK offline crash and request-byte proofs. They preserve unknown delivery
and required-action occupancy; C04/C11 remain typed PENDING in the shared matrix.
See [driver conformance hooks](../testing/docs/drivers/gemini/CONFORMANCE-HOOKS.md). These hooks
are not installed by the host factory and perform no live key discovery.
OpenAI also provides the explicit
`daimon.testing.drivers.openai.conformance_sdk.offline_sdk_adapter()` component. Its fixed
SDK MockTransport executes the unchanged C06/C07/C09 oracles and tests C08
conditional tool/file-mount refusal without update writes. Cancellation EOF
retains occupancy, synthetic usage corrections survive memory-outbox restart,
and binary artifacts/shared-vault retention use independent HTTP effects.
This component changes neither the standard registry nor live certification;
see [its scope and mutation controls](../testing/docs/drivers/openai/CONFORMANCE-SDK.md).

OpenAI host tests use the shared isolated SDK factory for direct clients and
inject it into offline C04/component and F1 replay factories. The testing package
retains guarded offline factories; injected clients refuse ambient provider
settings again when the SDK copies them.

Gemini C06/C04 round runners are separately invoked and require merged source,
lead-pinned financial and GO documents, the isolated five-dollar primary-only
line, and fresh evidence output before key access. Offline SDK proofs retain
native cancel/EOF occupancy, ambiguous-delivery recovery, all fenced store
boundaries and monotonic terminal settlement. Historical certificates and
standard pending declarations are unchanged; see
[money and scenario boundaries](../testing/docs/drivers/gemini/LIVE-ROUND.md).


Explicit native credential offers share one profile-bound host adapter and MCP grant
check. The returned resource host's complete scope and native catalog authority are
verified before catalog access. OpenAI's existing agent port exposes its provider,
profile, account scope and enabled native catalog state for this same binder; no SDK
handle crosses the port. Provider constructors preserve their existing control prefixes
and remain disabled without an operator delivery grant.

Product modules cannot import test or conformance harnesses. The repository's
import-linter contract checks direct and transitive imports from core, adapters
and mux outside its conformance package. Boundary tests run the configured
checker against reintroduced imports; the mux initializer and new top-level
module inventory are covered too. Harness consumers belong in daimon-testing.

Conformance and manual live-probe helpers now live in `daimon.testing.conformance`,
`daimon.testing.drivers` and `daimon.testing.adapters`, outside the runtime wheels.
The legacy mux conformance package is removed; its tests, saved receipts and
reference docs are under `packages/testing` outside the wheel package. CI also
checks built workspace wheels for test modules, QA helpers and receipt files.

### Implicit execution authority

Profiles declare whether execution is intrinsic to their host harness. The
persistent OpenAI workspace declares implicit Bash; an empty agent tool list
does not remove it. New profiles default to no implicit execution. Host worker
attenuation checks this declaration against the root's actual execution and
environment grants before returning a derived spec.

Profiles also declare the MCP tool policy keys their native compiler preserves.
An empty declaration refuses worker MCP delegation. OpenAI preserves finite
`allowed_tools` and the exact `required` flag; Gemini preserves the bound finite
allowlist. Anthropic's neutral MCP compiler does not preserve policy controls,
so its declaration stays empty. Native confirmation support does not by itself
prove that the neutral per-tool permission field is compiled.

The explicit Gemini deployment edge also exposes `configured_catalog_transport`
for native agent create/readback, reusing the same request-owned SDK client and
ambient-configuration refusal. Catalog capability alone still cannot execute a
named agent. A host that has verified its complete scoped counterpart may inject
the exact neutral `Agent` into the existing driver. Session creation then retains
that native ID, and each dispatch checks the stored definition against it. The
Flash accounting transport uses the verified model for one named-agent attempt;
it never substitutes the inline fallback chain. Existing inline callers omit this
optional dependency and retain their requests and accounting policy.

### Claimed worker resources

Canonical worker specs can use the existing scoped Anthropic create edge without
passing through a legacy SDK payload projection. Agent creation observes the
existing single-attempt mutation context only for a claimed operation; ordinary
resource callers retain their request and retry behavior.

Alternate-backend worker publication uses the existing catalog publisher with
an owned private-slot lease, the canonical worker, and a fresh authorization and
financial guard. Its operation binds the run, ordinal, root, source revision,
resource context and authored spec. It invokes the existing unbound turn persistence owner, which records the
native acknowledgment before catalog publication. A catalog retry restores that
identity rather than creating another agent. A sent or unknown operation requires reconciliation.
The existing read edge can verify the processed operation against the scoped
catalog before native readback. Runtime installation, source classification and
session preparation remain required; this resource edge does not grant execution.

The Anthropic session composer accepts a captured resource source together with
the canonical worker. The original source UUID selects memory, repository,
files and credentials while the actual worker ID selects execution. The existing
source projection guard is reused, and the existing preparation fence runs before
resource effects and again before session creation. Worker memory or Copilot
failures propagate; ordinary degradation remains unchanged. Native worker resource
drops are refused until this composer can prove them. Claimed memory/vault/file
creation and credential mirroring use the same single-attempt SDK selector.

Session creation also uses the existing context-aware single-attempt client for
claimed mutations, including repository credential resolution. Ordinary session
calls retain their request and retry policy.

The existing Anthropic environment codec can project fully explicit cloud
networking and package policy for source-preservation checks. Implicit defaults
and unsupported policy retain their native obligations. Native environment
request encoding remains unchanged.

OpenAI completed messages receive their stable item identity during native event
normalization. Recovery retains previously recorded event IDs and native
provenance, including journals written with stream IDs before that normalization
rule; no journal migration is required. Worker allocation and session creation
reuse the shared preparation and fenced mutation guards.
Gemini worker sessions can carry an explicit `max_total_tokens` control through
the existing agent compiler and durable request template. The budget is native
best-effort behavior, excluding cached tokens. A budgeted worker permits one
interaction and retains its exact named-agent model, without fallback; ordinary
Gemini sessions retain their existing behavior.

Profiles declare `model_request_limit`, `input_token_limit` and
`output_token_limit` through the existing capability map. Unknown support
refuses a requested worker ceiling. Output ceilings require native enforcement;
host request admission can be emulated only where the host controls each actual
model request. Observing a managed agent's inference events does not establish
that admission control. Unrequested limits preserve existing serialized intent.

`session_spend_control` declares support for compiling an explicitly requested
native spend control. It does not promise a hard final bill or imply support
for model-request or token ceilings. Hosts retain their original input claims,
deadlines, cancellation and unknown-usage holds around that control.


Anthropic and OpenAI declare `session_spend_control` for the existing native
session creation codecs. Anthropic's closed budget value uses a positive cents
string and is merged through the SDK `extra_body`; OpenAI uses strict positive
integer cents in `spend_control.limit`, verifying its native echo. Neither flag
advertises hard internal model-request or output limits. Missing controls remain
omitted for unconfigured callers.


Explicitly budgeted OpenAI worker creation returns a provisioning acknowledgement with its scoped
session reference and control echo. The host retains that receipt before using
the driver's single spend-control validator; retrieval validates independently.
Execution checks retrieve the session again, so a provisioning receipt does not
authorize model input. Rejected allocations use owned session deletion through
the existing bounded cleanup path; OpenAI's unsupported archive port is not
reported as a successful archive.

Default-off worker echo admission is omitted from serialized controls so
existing ordinary host plan and creation identities remain unchanged. Enabled
echo admission is serialized and distinguishes the worker policy.
Anthropic builtin tool configs accept the explicit `type` field returned by
native agent retrieval. Session overrides preserve its original presence and
request bytes; the legacy shape without that field remains valid. Unknown
properties, invalid tool names, permission policies and non-string type values
remain rejected by the existing strict configuration schema.

An ordinary tool-free Gemini channel can explicitly configure `max_output_tokens`
on its existing runtime plan (a strict integer from 1 to 65,536, for example 4096).
This uses the same driver and journal with a model interaction's native
`generation_config.max_output_tokens`; it requires an explicit no-execution
environment and carries no managed tools, skills, MCP, mounts or named agent.
Conversation history reuses `previous_interaction_id`, and the limit survives
continuation and the existing 503 fallback policy. An unsupported or rejected
limit never retries without it. Omission retains managed-agent requests and
option-A worker defaults. This is an output limit per interaction, not an input
or final-spend limit.

Native toolset projection excludes well-formed capabilities without a complete neutral
form from worker ceilings. Backend switches declare those losses. Builtin projection
requires resolved controls and either disabled defaults or a complete builtin list;
duplicate configs use the strictest enablement and permission. Anthropic neutral
builtins compile to one explicit allowlist with disabled defaults.

Explicit Gemini worker input/output envelopes use the existing capped model
interaction path. Supply both `WorkerLimits.max_input_tokens` and
`max_output_tokens` and set `max_turns=1`; output must be at most 65,536.
This mode requires `gemini-3.8-flash`, explicit drops of memory and sandbox,
and no tools, skills, MCP or mounted resources. Counterpart ownership is still
verified and materialization still uses the existing catalog. Execution submits
the verified instructions to the pinned model with no agent-side inference loop.

Before a billable POST, the SDK counts the complete text prompt and system
instructions with `models.countTokens.generateContentRequest`. Unknown or
excess input refuses; cached tokens stay inside the inclusive input ceiling.
`generation_config.max_output_tokens` natively bounds visible plus thinking
output. The existing durable send claim permits at most one accepted or unknown
interaction for the worker, including after restart, so these are aggregate
worker ceilings. No second turn, tool result, model fallback or replacement on
uncertain delivery can obtain another envelope. Omitting token ceilings retains
the existing managed worker path and ordinary Gemini turns.

Explicit Gemini catalog deployments can carry caller-bound MCP references when
an MCP resolver is injected. Catalog compilation and native readback preserve
only destination and allowlist; bearer values are resolved afresh for interaction
requests. `RetainedSkillDeployments` implements the existing publisher contract
for independently provisioned immutable GCS directories, verifying the complete
scope, version and every retained file digest without making storage writes.

The opt-in `selfhosted.loop` profile uses per-caller threads and selects
`gpt-6.1-sol` by default; an environment may use another deployment name. It is
non-core. Azure deployments can select `gpt-6-luna` for cheap smoke tests;
see the [model parameters and region guidance](../../deploy/azure/README.md#model-selection).
Steer, multiagent, vaults, memory stores, workspace export/import and
native skill versions are explicitly unsupported.

The self-hosted driver's `SelfHostedDriver` takes injected `Agents` and
`Environments` ports, an authorization callback and an optional durable binding
lookup. `DirectTransport` calls the harness `/v0` endpoints;
`FoundryInvocationsTransport` wraps those same calls in `/invocations` and sends
one deterministic sandbox id on every call. Both submit turns with JSON
acknowledgements and poll the durable event log. `open_stream` opens the first
HTTP replay request before returning its iterator. With no cursor it starts at
the active turn or resumes after the last event delivered by this driver instance. A turn
that finished before the reader opened is still delivered. Explicit cursors
control historical replay and recovery after driver recreation. Idle readers
may wait for the next turn; individual HTTP requests remain bounded. A lost reader can reconnect
at a sequence cursor without running the turn again.

Usage identities include the session, turn and model request, so replay preserves
one observation per request. Missing measurements are unknown observations when
the harness explicitly reports `usage_unavailable`/`usage_missing`; a terminal
turn with no measured, unavailable or started request is retained as unresolved (errored unless interrupted)
for operator reconciliation without blocking later turns. Unmatched
request starts remain unknown observations; measured revision 2 supersedes an
unknown revision 1 under the same observation id. Tokens are never
inferred as zero. Agent/environment revision changes require a new binding.
Artifacts, session listing/deletion and native skill administration refuse
explicitly. Built-in `auto`/`ask` and multiple pending approvals are supported.
Narrowed built-in sets deny omitted tools. MCP permissions use `server/tool`
and `server/*` keys; `allowed_tools` denies unlisted tools. Credentialed MCP
servers require an injected tools resolver that enforces tenant/origin bindings.
Pending approval retries submit the same recorded decision; the harness
deduplicates identical actions.

`mux.drivers.selfhosted.wire` defines the `daimon-harness/0` HTTP boundary:
turn requests, approval decisions, durable event replay and typed event data.
These models require no provider SDK. Inline image parts use `data_b64`;
credentials in MCP configuration are environment variable names.

Model attempts use `model_request_started` before model I/O; measured usage or
an explicit `usage_unavailable` error closes the same request id. Approval actions
use `allow` to execute, `deny` to refuse, and `result` to substitute a host result.
Only `result` carries a result field; JSON null remains a valid host result.


Inline harness skills carry text `files`, base64 `binary_files`, and a verified
`sha256`. `InlineSkill` computes a missing digest for local builders and rejects
an explicitly supplied digest that differs from the content. Use the shared
`skill_sha256(name, decoded_files)` helper: it hashes UTF-8 JSON with sorted keys,
compact separators and `ensure_ascii=False`, shaped as
`{"name": name, "files": {path: standard_base64(decoded_bytes)}}`.
The digest includes all paths and bytes regardless of their text/binary map.
Paths must be canonical relative POSIX paths; duplicates, traversal and
file/directory collisions are refused. Limits are 8 MiB decoded per skill and
24 MiB per turn; seeded names such as `marimo_notebooks` remain valid.

Assistant message deltas and snapshots require the same session-unique
`message_id`, allowing a driver to accumulate deltas and replace them with the
final snapshot.

A turn may supply `tool_policy: {tool_name: "auto" | "ask" | "deny"}`. The default is
empty; unlisted tools execute automatically. Explicit `ask` requires approval.
`deny` omits the tool from model discovery and refuses any attempted execution.
Keys are `bash`, `read`, `write`, `edit`, `<server>/<tool>`, or `<server>/*`.
MCP server/tool components use letters, digits, underscores, hyphens and dots.
An exact key overrides `<server>/*`, which overrides the default `auto`.
For example, `{"sample/*": "deny", "sample/search": "ask"}` exposes only
`sample/search` from that server and requires approval before it runs.
The driver uses the agent deployment unless `model` is explicitly overridden.
The host supplies `deployment` for its selected environment; a mismatching
request refuses before submission. Transport token providers are awaited on
every HTTP call (use an expiry-aware Entra credential provider). Pass
`mcp_authorization` per `driver.events.send`, never at transport construction.
Foundry calls include configurable `api_version` (default `v1`). Authenticated
HTTP endpoints require HTTPS except on loopback. Replay uses `after_seq` with
jittered exponential polling and retries 429/502/503/504 with `Retry-After`.

The real harness/driver interop suite must cover:

1. Cancel before the first model request, then a second turn on that session.
2. MCP or skill setup failure before model I/O.
3. Harness restart mid-turn and reconcile of unmatched request usage.
4. Dropped stream reopened at the last sequence across the terminal event, without gaps or duplicates.
5. Idempotent resend from a fresh driver, one execution, and changed-payload conflict.
6. Approval that executes a write, denial, and host result substitution.
7. Per-turn MCP headers absent from journals/logs, with a different token on the second turn.
8. Foundry invocation envelope with string `query.after_seq`.
9. Concurrent second send receiving 409.
10. Two sequential turns on one session.

Session creation uses tenant/account/principal plus the caller's idempotency key,
independently of renewable authorization grants. The binding retains a separate
specification digest; a changed specification under that key conflicts, including
on reconstruction. Durable agent/environment pins retain both local and native
revisions. Creation replay reuses those pins instead of current resource versions.
Approval retries reconcile a known call's tool result; an uncertain HTTP reply
cannot cause another tool execution. Retry-After is capped at 30 seconds and the
poll deadline; network polling and cancellation observation use bounded backoff.

Host-side MCP and skill preparation lives in
[`daimon.core.turn.selfhosted_tools`](../core/README.md#self-hosted-tool-preparation).
It returns credential-free wire fields; the registered Daimon MCP credential
travels only in the per-turn `X-Daimon-MCP-Authorization` transport header.

`mux.profiles.anthropic.is_builtin_skill` recognizes native built-in skill refs
by source or reserved id when imported refs omit source. Explicit custom refs
with reserved names remain custom skills. Hosts can skip native
bundles on other backends without duplicating provider policy.

Host retained-backend resolution reads the legacy provider from
`mux.profiles.MANAGED_AGENTS.provider` and the unconfigured-channel default from
`mux.contracts.config.DEFAULT_BACKEND`.

`apps/harness` is a standalone consumer of `daimon-mux` for the shared self-hosted
wire models. It does not import Daimon core or adapters. It implements the C3 HTTP
service, Azure Responses model loop, durable request inventory and per-session
tool isolation; its README documents deployment and authentication requirements.

Core registers `selfhosted.loop` through its selfhosted deployment runtime.
The shell authorizes an exact tenant/channel/account configuration, checks
selfhosted tariffs before HTTP, injects scoped resource ports, and records
model-request usage through the shared accounting outbox. Its turn codec keeps
canonical events and fenced idempotency receipts while rendering adapter DTOs;
no Anthropic API is used for these turns. The MCP credential stays in a turn
transport closure and is revoked by the prepared-turn finalizer.

Self-hosted replay validates the environment revision pin before transport I/O.
The usage adapter also exposes `reconcile_turn` for host readers, filtering by
root turn before applying host bounds, so prior session observations do not
consume the current turn's usage budget. MCP approval display preserves the
qualified server/tool identity.

The dynamic-session pool audience requires `session_pool=true`; transport
requests require a nonempty session ID. The pool container must independently
validate Entra tokens for that audience and allowlist the invoking host principal.


Selfhosted C3 transports send the nonsecret `session_id` query parameter on every
request, including actions/cancel and event polling, so an empty sandbox can
restore and verify session/turn ownership before handling it. Every C3 request
also carries `after_seq`, the highest sequence the driver has seen. After sandbox
loss, `history_gap` normalizes to an authoritative `session.history_gap`; recovery
continues above the client cursor and reconciles interrupted-turn usage.
