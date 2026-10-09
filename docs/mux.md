# mux

`packages/mux` is the provider-neutral contract for managed agents: the
types, ports, errors and admission rules a driver for Anthropic, OpenAI or
Gemini implements and Daimon calls. The import name is the top-level `mux`,
not `daimon.mux`, and `mux` never imports `daimon`.

Nothing in Daimon calls `mux` yet. A turn still goes from `daimon.core.turn`
to the Anthropic SDK directly. This page describes the contract the drivers
are written against.

## Layout

| Module | Contents |
| --- | --- |
| `mux.contracts.ids` | `Scope`, `ChannelRef`, `ThreadRef`, `ResourceRef`, `Revision`, `PageRequest`/`Page`, `ModelRef`, `SkillRef` |
| `mux.contracts.config` | `BackendConfig`, `ResolvedBackend`, `ConfigRevision`, `CapabilityRequirement`, `resolve_default` |
| `mux.contracts.profile` | `Capability`, `Support`, `CORE_CAPABILITIES`, `Profile` |
| `mux.contracts.admission` | `admit`, `Admission`, `FallbackApplied` |
| `mux.contracts.events` | content parts, `Event`, `NativeProvenance` and one payload model per event type |
| `mux.contracts.actions` | the input events (`UserMessage`, `UserToolConfirmation`, `UserToolResult`, `NativeInput`) and `RequiredAction` |
| `mux.contracts.receipts` | `Operation`, `SendReceipt`, `CancelReceipt`, `StopObservation`, `UpdateReceipt`, `DeletionReceipt`, `RestoreReceipt` |
| `mux.contracts.usage` | `UsageObservation` |
| `mux.contracts.resources` | agent, environment and session specs and records, `ProviderBinding`, `Continuity`, artifacts, skills, and the records behind extension ports |
| `mux.contracts.extensions` | `ExtensionRef`, `ExtensionConfig` and the declared namespaces |
| `mux.contracts.ports` | the port protocols |
| `mux.errors` | the error taxonomy |
| `mux.state` | the `StateStore` protocol, the pure operation, lease, journal and usage rules, and a restartable in-memory store |
| `mux.profiles` | the declared profiles and `get_profile` |
| `mux.drivers` | one subpackage per provider (empty so far) |

Every contract type is a frozen pydantic model that rejects unknown fields,
and survives a JSON round trip unchanged. Mapping fields are read-only views,
so a value cannot be edited in place after validation either. Provider SDK types, exceptions,
URLs and tokens never cross the contract.

## Configuration and the default

A channel stores a `BackendConfig`: `backend`, `profile`, `model`, `requires`
and `thread_mode`, all optional. `resolve_default` fills in the rest:

- Nothing configured resolves to `anthropic` / `anthropic.managed_agents`
  with `thread_mode="per_caller"` and no model override, so the agent's own
  model applies. That is how every channel runs today.
- A backend without a profile gets that backend's core profile:
  `openai.persistent_workspace` for `openai`. Gemini has no core profile, so a
  Gemini channel has to name `gemini.inline_reuse`.
- Any backend other than Anthropic must name its model; there is no default.
  A blank model is refused, and so is a profile id no declared profile has.
- `thread_mode="shared"` is opt-in. Unconfigured channels keep one thread per
  caller.

A `ConfigRevision` is one immutable resolved configuration for a channel,
numbered by `local` and carrying a SHA-256 `digest` of its content. The
digest is checked when a revision is loaded and again at admission, so a
stored revision that was edited by hand is refused. A resolved configuration
whose profile belongs to another backend, or whose non-default backend has no
model, fails validation however it was built.

## Profiles and admission

A `Profile` declares a `Support` level (`native`, `emulated`, `unsupported`
or `unknown`) per capability. A capability it does not declare is `unknown`,
and admission treats `unknown` as `unsupported`.

Nine capabilities are core: `thread_workspace_persistence`, `turn_lifecycle`,
`cancel`, `tool_loop`, `required_actions`, `skills_bundle`, `artifacts`,
`usage_observations` and `reconcile`. A profile can only declare itself core
when it supports all nine.

| Profile | Core | Notes |
| --- | --- | --- |
| `anthropic.managed_agents` | yes | Every capability native. |
| `openai.persistent_workspace` | yes | `reconcile` is emulated from saved items; missed events cannot be replayed. |
| `openai.conversation_only` | no | No workspace. Admitted only when the channel names it. |
| `gemini.inline_reuse` | no | The inline environment expires after inactivity, so workspace persistence is not guaranteed. Its usage reporting is undeclared, so admission refuses it until a driver shows usage observations. |

`admit(config, profile)` is pure and runs before any provider call:

- On a core profile every core capability is required.
- A capability the config marks `required` that the profile does not support
  raises `UnsupportedCapability`, listing every gap at once.
- An `optional` capability must declare a fallback when the config is
  written. If the profile lacks it, admission succeeds and lists the fallback
  in `Admission.fallbacks`.
- Capabilities met by emulation are listed in `Admission.emulated`.
- A non-core profile named by the config goes without the core capabilities
  it lacks; those are listed in `Admission.waived_core`. A core capability
  the config explicitly requires is still refused.
- `usage_observations` is required on every profile, core or not: a turn
  that cannot be metered is never admitted.
- The profile must be the one the config selects, for the same backend.

## Ports

`ManagedAgents` groups the eight ports (`agents`, `environments`, `sessions`,
`events`, `artifacts`, `skills`, `models`, `usage`) with `capabilities()`,
`admit()` and `extension()`. Every port call takes the caller's `Scope`,
built from the host's own authorization decision. Every mutating call takes
an operation `key`, and `expected` (a revision or binding generation) where
two writers could race. `Sessions.migrate` always raises
`MigrationUnsupported`: a backend change applies to new threads only.

A `ResourceRef` names the provider account or workspace a resource lives in
as `account_scope_id`. That is not a thread binding: a `ProviderBinding`
(which provider session backs a thread, at which generation) has its own
`id`, stable across generations.

In a spec, `None` means "not set": the driver sends nothing and the
provider's default applies, while an explicitly empty tuple or mapping is
sent as empty. Agent and environment patches carry `extensions` keyed by
namespace, each replacing that namespace's config. In a patch's `metadata`, a key mapped to `None` deletes that key. Records carry
`created_at` and, where the provider reports them, `updated_at` and
`archived_at`, plus an optional `native` copy of the provider's own record
that only the driver reads. A `PageRequest` field left as `None` is not sent.
A `Page` carries the provider's `has_more`, and `next_cursor` is set exactly
when it is true.

`Skills.create` makes a new skill and `Skills.publish_version` adds a version
to an existing one, returning the full `SkillVersion`. Both take the bundle
inline as a `SkillUpload` (file bytes in the same request, never a separate
upload first). `Skills.list` returns full `Skill` records. A `SkillRef` may
leave `version` unset, so the provider uses the skill's latest version, as
existing agent configurations do.

Native features are typed extension ports addressed by
`(port type, namespace, version)`. Anthropic offers `agent_tools`,
`memory_stores`, `vaults`, `session_resources`, `skills_versions` (list,
download and delete versions), `multiagent`, `environments_fork` and
`platform_export`; OpenAI offers `vaults` and `steer`, all at version 1.
Provider-specific agent shapes, such as Anthropic's toolset configuration or
a multiagent roster, travel on the agent as an `ExtensionConfig` for
`anthropic.agent_tools` or `anthropic.multiagent`; the driver owns and
checks that schema. `anthropic.platform_export` returns native JSON on
purpose, because exporting native state is the feature.
Asking for a namespace the profile does not offer raises
`UnsupportedCapability`, and asking for another version raises
`ExtensionVersionError`. There is no raw client attribute on any port.

## State

`mux.state.store.StateStore` is the durable state a host keeps for the
library. Each method is one atomic transaction, and every decision it makes
comes from a pure function in `mux.state`, so every store implementation
decides the same way. Records a store returns are copies: changing one never
changes what was committed.

A binding lives in a slot: a thread plus the caller account that owns it.
Per-caller threads (the default) have one private slot per account, keyed by
the binding's `legacy_account_id`; a thread opted into sharing has one slot
with no account. Leases are per slot.

| Record | Unique on | Rule |
| --- | --- | --- |
| config revision | (channel, local) | Immutable. The same number with different content raises `ConfigRevisionConflict`. |
| binding | (slot, generation) | Compare-and-swap on generation (0 = unbound); the loser raises `BindingConflict`. A rebind keeps the binding `id`, and an `id` names one slot only. `bind_new_slot` returns the winner of a race. |
| operation | (tenant, account, key) | Owned by the principal that began it; another principal gets `ScopeViolation`. Persisted as `pending` before any I/O. The same key and request digest returns the existing record; a different digest raises `OperationConflict`. Only `claim_send`, a compare-and-swap from `pending`, moves it to `sent`, so exactly one caller sends. It goes back to `pending` only when reconciling proves the provider never got it. |
| lease | slot | One active root turn per slot. Each acquisition gets a higher fence. Taking over an expired lease sets `took_over`. |
| journal | (session, sequence) and (session, source key, revision, preview) | An append, its projection and the stream cursor commit together; an entry already journaled is dropped. Previews are kept in their own namespace and never change the projection, so they can neither complete a turn nor block the record that does. |
| usage | (binding, observation, revision) | Recorded only under the binding that names the observation's session. A higher revision writes one outbox row of signed deltas; a lower one is ignored, and the same revision with different counts raises `UsageRevisionConflict`. |
| accounting outbox | (binding, observation, revision) | Each row carries `prior_applied_revision`, the revision its deltas are measured against. `mark_outbox_applied` is true once per row. |

Fencing: `claim_send` and `advance_operation` on an operation begun with a
slot, and every `append_events`, need the active lease of the record's own
slot. A journal belongs to the slot of the binding whose
`native_refs["session"]` names its session, registered when the binding is
written and kept across rebinds; an append never claims a journal, and a
session no binding names takes no appends. A missing or
foreign lease raises `ScopeViolation`, and a superseded one raises
`StaleFence`. The other writes are safe without a lease: beginning an
operation is idempotent, `put_binding` is its own compare-and-swap,
usage is ordered by revision, and an outbox row flips once. Time is passed
in, but a database store checks lease expiry against its own transaction
clock.

After a crash, `operations.recovery` says what a successor does with an
operation it finds: claim and send a `pending` one, reconcile a `sent` or
`outcome_unknown` one, observe an `accepted` one. A lost lease never means
the request was not sent. Only conclude absence once the claimer's lease has expired and its send
deadline has passed, or a request still in flight lands after the resend.
So a driver keeps its send timeout below the lease TTL and never starts
I/O on a lease past its expiry.

Usage revisions follow the null-is-not-zero rule: a count the provider did
not report leaves the accounted value alone and its delta is `None`. An
observation of 100, then 120, then 110 output tokens yields deltas of 100,
+20 and -10, and replaying any of them yields nothing.

`mux.state.memory.MemoryStateStore` keeps its committed state in a
`MemoryStateData` that outlives the store, so `restart()` simulates a
process restart, and a `crash` plan raises `SimulatedCrash` just before or
just after a named method commits.

`mux.state.suite` holds the protocol checks every store must pass. Each
check takes a function returning a store over the same durable state (a
second call is a restart). The memory store runs them in
`packages/mux/tests`, and Daimon's Postgres store runs the same checks.

Daimon's Postgres store is `daimon.core.stores.mux_state.PostgresStateStore`,
over the tables of migration `0074_neutral_state`. Races are settled by the
database (unique constraints, row locks, and an advisory lock per usage
observation), and lease expiry follows the database clock. Its module
functions take an `AsyncSession`, so `mark_outbox_applied` can run in the
transaction that writes the ledger rows. The migration backfills a binding
per caller-owned `thread_sessions` row (one generation per row, in creation
order) and records it in the row's new `binding_id` and `binding_generation`
columns. Rows with no account are not backfilled. Nothing in Daimon calls
the store yet.

## Events

`Event` is one journal entry: an id, the session, a local `sequence`, a
`type`, turn, thread and item ids, `caused_by`, timestamps, an `authority`
(`record`, `preview`, `reconciled` or `gap`), a payload and its
`NativeProvenance`. Each fixed type's payload is checked against its model
(`session.turn_ended` carries `root_turn_id`, `outcome`, `native_reason` and
`cancel_receipt`, for example). `agent.thread.*` and `native.*` events carry a
provider-shaped payload that is not checked. `agent.message.delta` is valid
only with `authority="preview"`: previews never bill or complete a turn.
`session.requires_action` carries the full `RequiredAction` records, so the
host knows each action's kind and call id without looking back.
`session.turn_ended` has no revision of its own; a corrected outcome is a new
`session.turn_ended` with `authority="reconciled"` for the same root turn.

## Errors

`MuxError` is the root. `InvalidConfig`, `UnsupportedCapability`,
`ExtensionVersionError`, `ScopeViolation`, `ContinuityLost`,
`BindingConflict`, `OperationConflict`, `MigrationUnsupported` and
`ProviderError` derive from it. `ContinuityLost.binding_id` is the
`ProviderBinding.id` of the thread. `ProviderError.category` is one of `auth`,
`permission`, `not_found`, `conflict`, `invalid_request`, `rate_limited`,
`overloaded`, `upstream` or `transient_network`. A delivery the driver cannot
confirm is not an error: its receipt reports `outcome_unknown`.

## Usage

`UsageObservation` is one measurement with an `id` and an integer
`revision`. A higher revision of the same `id` supersedes a lower one, and an
observation never overwrites a higher revision already applied;
`native_revision` keeps the provider's own version string for audit. Token
counts are nullable, because a count the provider did not report is unknown,
not zero. `input_tokens` includes the cached and cache-write counts, and
`output_reasoning_tokens` is part of `output_tokens`. `native_meter` keeps the
provider's own usage record unchanged. A later revision of the same
observation corrects it, and the corrections are applied as a delta.

## Dependencies and import rules

`daimon-mux` depends on `pydantic` and `httpx`. Each driver's SDK is an
extra: `daimon-mux[anthropic]` (`anthropic>=0.117`, the same pin as core),
`[openai]` (`openai>=2.54.0,<3`) and `[gemini]` (`google-genai>=2.7.0,<3`).

Import-linter contracts in the root `pyproject.toml` hold the boundaries:

| Contract | Forbids |
| --- | --- |
| Mux must not import daimon | `mux` → `daimon` |
| Mux contracts and profiles import no provider SDK or driver | `mux.contracts`, `mux.profiles`, `mux.errors`, `mux.state` → `anthropic`, `openai`, `google`, `mux.drivers` |
| Only mux.drivers.anthropic imports the anthropic SDK | `mux` → `anthropic`, except from `mux.drivers.anthropic` |
| Only mux.drivers.openai imports the openai SDK | `mux` → `openai`, except from `mux.drivers.openai` |
| Only mux.drivers.gemini imports the google.genai SDK | `mux` → `google`, except from `mux.drivers.gemini` |

The tests run in CI as the `pytest-mux` job (`uv run pytest packages/mux`).

## Conformance

`mux.conformance` provides an offline C01–C18 runner. Drivers explicitly
register a fresh fake-transport adapter per fixture; the existing pytest-mux
job collects the runner's regression tests. Results distinguish pass, fail
and pending with evidence. State-store and host seams that have not landed
remain pending and prevent certification. The in-memory reference driver is
a test oracle, not a backend. See
`packages/mux/mux/conformance/README.md` for adapter
requirements and the dependency matrix. No default runtime behavior changes.

The conformance runner uses `mux.state.store.StateStore`. The memory oracle
executes C03 operation replay, C04 crash recovery/fencing, C07 usage revisions
and C13 binding races against a fresh restartable store. Missing store adapters
stay pending; host queue/attribution, historical billing, backend selection,
wake generation and outcome-row probes remain pending until their adapters land.
These offline oracle results certify no provider or host integration.
Recovery requires the owning slot's active lease and independent acceptance
evidence: prior durable acknowledgement or a native response read during replay.
The replay's own writes cannot establish that evidence, and ambiguous intent
cannot yield an unsupported queued or processed receipt. Unbound sessions cannot
acquire journal ownership through an append, including a refused one. Yielding
race tests exercise send claiming, and a later same-count usage revision checks
that stale replay preserved the latest accounted state.

The manual `tests/judge` harness grades recorded
transcripts separately from conformance, using a fixed 12-task rubric and
structured `codex exec` output. Replay repetitions default to three. API-key
authentication is refused; normal pytest never invokes the judge.

`tests/baselines` contains lead-run, read-only
14-day telemetry SQL, a baseline JSON converter requiring the deployed SDK pin
and export date, and an offline paired M0 replay benchmark. First-token latency
is explicitly unavailable in the current telemetry schema. The benchmark remains
pending until N4 supplies the legacy/mux transport adapter; pending never passes.

## Resources (Anthropic driver)

`mux.drivers.anthropic.AnthropicManagedAgents` assembles resource ports around
Daimon's existing `AsyncAnthropic` client. The host continues to configure and
close that client; constructing the backend makes no requests and introduces
no settings or defaults. Agent, environment and skill requests retain omitted
fields, explicit empty lists, metadata patches and provider version checks.
The defaults pipeline keeps its current reconcile, duplicate and sweep policy.
Its full agent/environment list walks use the typed `anthropic.resource_walk@1`
port, returning neutral records while preserving SDK async iteration. The core
page APIs remain available for callers that request one page at a time.

Anthropic toolsets and coordinator rosters use closed, versioned schemas in
`mux.drivers.anthropic.schemas`. Native tool configuration is carried by
`anthropic.agent_tools@1`; coordinator configuration uses
`anthropic.multiagent@1`. Unknown properties are rejected before a request.
The operator recovery export uses `anthropic.platform_export@1`, which exposes
native JSON for the archive while keeping the SDK client private. It preserves
the existing request pagination and closes each skill download response.

The driver converts SDK failures to `ProviderError`, including failures while
reading later pages, and retains the original exception as its cause. For M0,
`daimon.core.mux_compat` restores the existing SDK exception and model types at
the host edge so unchanged consumers keep their current behavior and copy.
That module makes no SDK calls and is tracked for removal after M0 in the
neutral-core sprint's `FOLLOWUPS.md`.

M0 resource drivers accept operation keys and issue the existing requests
without a driver cache or deduplication layer. Durable operation handling is
owned by the host StateStore integration; resource drivers do not add provider
headers or retry policy for it.


Resource authorization is host supplied. `ResourceAuthorization` binds the
backend to one immutable `Scope` and the native IDs the host already authorized.
Tenant calls reject another tenant/account or unstamped references before I/O;
returned agent/environment references carry their minting scope. Native
`daimon_tenant` tags are checked after the existing request, and tenant lists
exclude foreign tagged records without additional requests. Agent, environment
and skill reconciliation, tenant indexing and all three tenant sweeps use the
tenant scope already established by their caller. Existing metadata and skill
title predicates retain the same results.

Only multi-tenant agent listing, workspace skill collection, organization-wide
referenced-skill inventory, operator recovery export and the untagged model
acceptance probe use `Scope.platform(reason=...)`. These are six production
call sites; the probe creates and archives its workspace test agent. Export
additionally requires its existing operator authorization name. The two shared `ma.py` resource helpers accept a
scope; unmigrated callers retain their current host authorization through the
separate, temporary `Scope.legacy_host_authorized(call_site=...)` capability.
Those callers are enumerated in sprint FOLLOWUPS.md for their owning lanes.

Skill collection and version walks use the SDK paginator inside the driver,
including its stop behavior for empty data and empty-string cursors. They preserve
page truncation detection and version deletion during iteration.


For tenant skill lists and page walks, custom records must appear in the host's
ResourceAuthorization skill-ID grant. Anthropic catalog records remain shared.
Filtering runs after each existing SDK request and leaves its cursor and page
iterator untouched, so a page containing only foreign custom records still
advances to later authorized records. Platform and approved legacy inventories
retain the entire workspace view. CLI/MCP keep their existing tenant-title and
channel-isolation output filters.

Skill sync, agent forks and channel copies use the same resource ports. Their
host authorization, title ownership checks, version retry, cleanup and channel
rules remain in Daimon. Legacy agent copies can carry explicit null create
fields through the closed `anthropic.agent_create_nulls@1` schema; ordinary
neutral specs continue to omit None. Duplicate native config namespaces are
rejected before I/O. Native model and environment config namespaces are
advertised by the assembled driver profile.

Skill sync, add, fork and channel-copy resource calls now pass the tenant from
Daimon's existing authorization context into the bound driver scope. The two
skills helper callers also pass this scope to version retry, removing their
legacy authorization seam. A denied bound sync target retains its existing
DaimonError message. Generic agent/environment pages normalize the SDK's empty
cursor to None; native full walks retain the SDK stop rule.

Skill import and repository-sync call-site checks retain the legacy multipart
file field, `SKILL.zip` filename, `application/zip` media type and archive bytes
for create, version and duplicate-title recovery uploads.

Workspace-key skill downloads from main #494 use the native version ID and omit
`anthropic-beta` on the content request through `anthropic.skills_versions@1`.
Agent forks resolve the pinned epoch version with the existing lazy SDK walk,
stopping at the first match; operator exports already have the ID and download
without another lookup. Forks use their authorized tenant scope; recovery exports
retain the explicit operator scope. Other pinned-version downloads retain their
existing SDK headers and arguments.


## CLI resource consumers

CLI agent archive, toolset backfill and ownership rekey, environment create,
update, archive, delete and retrieval, skill version inspection and title
backfill, routine agent-name backfill and session bootstrap use the resource
ports. Each operation passes the tenant established by the existing host
lookup; bootstrap also supplies the caller account. Skill deletion helpers
receive explicit tenant scopes, including each legacy skill's backfill tenant.
Request order, version checks, multipart filenames and bytes, output and SDK
exception types are retained. The CLI's temporary `mux_compat` module decodes
native records at the adapter edge without issuing SDK calls.
### MCP resource consumers

MCP agent create-result reads, updates, archives, skill attachment/removal and
environment writes consume neutral resource ports through the temporary host
SDK codecs. Callers pass the authenticated tenant and account scope, including
version retries and skill deletion. Tool schemas, confirmation text and
conflict retry boundaries remain the same.

Session lifecycle, session events, hosted artifacts/bundles and vault callers
retain their current SDK path until the lifecycle, events and remaining resource
ports land. The existing agent_chat turn calls wait for the turn lane.

The MCP version-count read also retains its SDK walk while the resource decoder
requires fields that the existing SDK counting caller does not consume.
### Chat platform and scheduler resource consumers

Discord, Slack and Teams skill checks, imported-skill attachment and applicable
agent writes consume neutral resource ports with the tenant from their existing
platform context. Version retries use that same tenant scope. The scheduler
passes the routine row's tenant when re-reading the selected agent for pin and
channel-isolation checks.

The adapters keep their current refusal and partial-attachment notices when an
agent re-read fails scope validation. Best-effort previous-agent name lookups
still fall back to the existing generic owner notice. Foreign-agent final skill
checks in Discord and Teams retain their existing refusal copy before upload.

Discord memory list/retrieve and scheduler session retrieve/archive calls retain
their current SDK path pending the memory, lifecycle and archive ports.
Vault credentials use closed native schemas with opaque host secret references.
The driver resolves those references only for the existing SDK write. Credential
snapshots retain public SDK fields and exclude write-only values, including
nested OAuth refresh and client authentication values. Credential and secret
file upload failures preserve SDK error classes and status while dropping request
bodies and authorization headers and redacting echoed secret values.
This includes escaped multiline values in SDK error messages. Helpers that
discarded credential-create responses continue to discard them; vault creation
preserves the original native response fields when the caller uses only its ID.

Files preserve the existing multipart filename, media type and bytes. The
`anthropic.session_resources@1` driver lists, adds and removes mounts and rotates
repository tokens by reference. Session archive is a separate administration
operation; it never archives or deletes shared vaults or memory stores. Workspace
vault janitor and credential sweep retain their explicit operator scopes and host
policy; GitHub session provisioning and token rotation use their tenant context.

The vault administration branch adds two explicit platform call sites: the
workspace orphan-vault janitor and stale-admin-credential operator sweep. Their
existing inventories span accounts; tenant provisioning and rotation use tenant
scopes. The construction inventory test includes both operator paths.

Vault bootstrap, external-token writes, OAuth replacement, credential mirroring
and `.env` uploads also use these ports. Public helpers accept an explicit host
scope; established callers without tenant context use a named
`Scope.legacy_host_authorized` capability. Owned callers with tenant context
forward it through credential writes, retries, rollback and file upload. Vault
name discovery retains its workspace inventory and exact account/agent name
predicate before the host knows a vault ID. Its named legacy capability and
remaining caller seams are recorded in the sprint follow-up inventory.

The SDK's DEBUG request-options logs are redacted within credential I/O, including
escaped values and exception text. The logging filter holds no credential values;
request-local material is cleared and its context reset after I/O. Operation keys
remain pass-through, including repeated keys, with no driver journal or deduplication.

Session creation resolves repository token references inside resource lists at
the SDK write. All resolved tokens share the same request-local error and log
redaction, including a failure attributed to the first repository. Free-text
metadata is outside reference resolution.

Native environment creation preserves an explicit null description through the
closed `anthropic.environment_config@1` `create_nulls` field. Its only accepted
field name is `description`; the marker is removed before the SDK request.
Neutral description omission remains unchanged, including when configuration
is absent or explicitly null.

The factory advertises the session lane's closed configuration namespaces
`anthropic.session_create@1`, `anthropic.session_resource_create@1` and
`anthropic.session_update@1` for admission. The session lifecycle driver owns
their payload validation and execution.

Credential request codecs retain the established JSON key order, including
header/body injection flags and OAuth scope/resource fields. Error redaction
covers both quote-escaping styles and their nested SDK repr/JSON forms. The
`.env` upload retains its `.env` filename, `text/plain` media type and exact
assembled bytes.
### Session creation and preparation

Daimon's session creation, isolated creation, preparation and recovery now call
the neutral Sessions port. The Anthropic lifecycle driver accepts closed
`anthropic.session_create@1` and `anthropic.session_resource_create@1` configs.
It retains native agent overrides, tool ordering, omitted versus empty resources
and vault IDs; repository tokens remain host secret references until the write.
Tenant and resource authorization runs before I/O, and tenant tags on returned
records are checked. Native list uses the SDK terminal-page rule. Archive delegates
to the existing session administration driver.

The optional Session native snapshot supports a temporary host codec that retains
SDK response extras, fields-set semantics and exception types. Host preparation
policy, billing checkpoints, recovery order and first feedback remain unchanged.
Tenant-aware creation also forwards its scope through vault bootstrap, credential
mirroring and rollback; vault discovery retains its existing exact-name inventory
check. Tenantless established creation uses a named legacy host capability.
Update planning and workspace export/restore are separate follow-on changes;
unsupported lifecycle methods fail explicitly. The driver keeps no operation cache
and makes no request during construction.
## Accounting observations

The Anthropic usage driver converts each model-request end event into one
`UsageObservation` with its native event ID, integer revision 1, provider
timestamp and raw native meter. The host retains the existing `(session, event)`
usage deduplication and `turn:{session}:{event}` debit key. Cache stages are
subtracted from inclusive neutral input before applying the existing pricing
formula, retaining its operation order and exact stored Decimal debits.

`UsageSample` holds the neutral observation. The temporary host module
`usage_compat` converts unchanged SDK event callers without provider requests;
pricing also accepts the existing structural four-stage usage values during
the M0 migration. Unknown token stages remain unpriced and cannot be written
as measured billing rows. Higher observation revisions require the accounting
outbox rather than a second turn debit.
