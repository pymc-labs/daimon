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

### Channel backends in Daimon

Daimon keeps a channel's configuration as its chain of revisions in
`channel_config_revision`; the newest applies.
`daimon.core.channel_backend.set_channel_backend` records a new revision
when the configuration changes, and `clear_channel_backend` returns the
channel to the default. Both refuse a selection that does not hold
together (`InvalidConfig`).

Admission reads it only when `DAIMON_TURN__CHANNEL_BACKENDS` is on; it is
off by default, and then nothing is read. With it on, a channel nobody
configured has no revision and admits exactly as before. A configured
channel is checked after the config cascade and before any provider call:
its profile must meet the configuration (`admit` above), and this release
must be able to run it, which today means `anthropic.managed_agents` with
the agent's own model, per caller or shared (below), or, per caller only,
`openai.persistent_workspace` with `gpt-6-luna` or `gemini.inline_reuse`
with `gemini-3.8-flash`. Anything else is refused as
`backend_unsupported`, recorded as `admission_denied` in the turn outcome,
never run on Anthropic in its place.

A channel whose configuration says `thread_mode="shared"` shares one session
per new thread between everyone who writes in it
(`daimon.core.shared_threads`). Only new threads share: a thread where
someone already has a session, of any status, stays per caller after the
channel starts sharing, and setup conversations and DMs always do. The
session row belongs to an owner derived from the thread (tenant, platform,
channel, thread), never a person's account and never the key the old
shared mode used, so nobody's private session or that mode's history is
ever selected. The thread's binding lives in the state store's shared slot
and is pinned to the configuration revision it was bound under: a later
change to the channel applies to new threads, and a thread whose revision
can no longer run is refused. A shared session carries no caller's
credentials (no personal vault, MCP identity or GitHub user grant), so the
agent must run in app mode, otherwise the turn is refused as
`backend_unsupported`; and every MCP server someone connected through their
own OAuth sign-in is left off it, whoever writes. Each turn's usage is still
recorded against the person who wrote it. Admission reads whether any
revision of the channel ever shared in the same statement as the revision
itself, so an unconfigured channel and a channel that never shared read
nothing more than before, however often it was reconfigured. A channel that
shares, or once did, reads the thread's binding, one indexed lookup. Two
first turns racing in a new shared thread prepare the same session and
record one binding.

Sharing is not tied to Anthropic. On a channel configured for another
runnable profile that shares, the thread's provider preparation
(`register_turn_preparation`) receives the shared owner as
`session_account_id` and keys its native session by it, with host
credentials only; the binding records that provider and profile. A shared
thread runs under the revision it was bound with, so after the channel moves
to another provider the thread's turns still dispatch to the first one, and
only new threads use the second.
Neither the OpenAI nor the Gemini host shares in this release: a channel
configured for either with `thread_mode="shared"` is refused as
`backend_unsupported` at admission, and again when its thread would be
bound, before any host preparation runs, so no shared owner, binding or
session row is created for it. Each host's preparation also refuses a
shared turn itself, before any provider request.

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
over the tables of migration `0087_neutral_state`. Races are settled by the
database (unique constraints, row locks, and an advisory lock per usage
observation), and lease expiry follows the database's wall clock, read
after the locks a decision depends on are held. Its module
functions take an `AsyncSession`, so `mark_outbox_applied` can run in the
transaction that writes the ledger rows. The migration backfills a binding for
each caller's thread (keyed by tenant, platform, thread and account, as the
legacy reader is) that has a live `thread_sessions` row. Its current
generation is the newest live row, the one Daimon resumes today, and the
caller's other rows are earlier generations. A thread with no live row
gets no binding. Rows with no account are not backfilled, and a session
recorded by two callers is owned by neither. The migration reads
`thread_sessions` once, in one statement, and derives everything it writes
from that snapshot. The steps that lock existing tables run last, so their
locks last only until the commit: the tenant foreign keys (which block
tenant writes) and two instant nullable `ADD COLUMN`s on `thread_sessions`.
At 200,000 rows both are held for well under a second. It leaves `binding_id` and `binding_generation`
NULL: `mux_state.link_legacy_thread_sessions(session, batch=2000)` fills
backfilled rows a batch per call, skipping rows already linked, so it can
be run, stopped and rerun at any time. Nothing in Daimon calls
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
Retrieval can bind a partial reply to its already authorized requested identity;
the opaque provider snapshot retains only fields present in that reply.
Tenant-aware creation also forwards its scope through vault bootstrap, credential
mirroring and rollback; vault discovery retains its existing exact-name inventory
check. Tenantless established creation uses a named legacy host capability.
Workspace export/restore remain unsupported lifecycle methods and fail explicitly. The driver keeps no operation cache
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

The host's `accounting_outbox.record_observation_usage` records and applies a
revision within a caller-owned `AsyncSession` transaction;
`apply_usage_outbox` also drains committed pending rows after restart. The bridge
uses the Postgres StateStore's session-level claim function, serializes revisions
by binding and observation, preserves historical `turn:` keys and uses signed
`adjust:` entries for later revisions. The nullable legacy
`observation_revision` column guards the latest projection without rewriting
historical identities or timestamps. The caller selects `billing_grain`, defaulting
to `model_request`; both increment and cumulative bases produce signed corrections.
Explicit aggregate coverage must resolve to durable leaves at that grain in the
same binding, tenant and session before acknowledgement without another charge.
Uncovered mismatched grains and unresolved coverage fail before claiming, so a
provider exposing turn observations needs an explicit `billing_grain="turn"`.

C12 accepts an optional asynchronous `host_accounting_evidence()` hook on its
transport. It verifies complete legacy-row snapshots, exact Decimal amounts for
100→120→110 corrections, overlapping totals, and a failed transaction followed
by restart. The hook supplies database facts rather than a verdict. Missing host
evidence leaves C12 pending; the generic reference transport does not certify a
host ledger. No provider request is made by the accounting bridge itself.
Headless session assembly retrieves its agent and environment through the
existing resource ports, in the same order, and decodes their native snapshots
at the M0 host edge. Tenant callers use their existing tenant/account context
and authorized IDs; tenantless callers retain the explicitly tracked
`headless_runner:run_turn:tenantless` host authorization seam until they supply
that context. No provider lookups or retry policy are added.

The Anthropic usage port reads model-request spans with the shared observation
converter. Its typed walk keeps the SDK async paginator, ascending order and
model-request filter, including early closure and errors on later pages. Scope
and session-reference checks run before requests; the optional model identity
comes from the caller's existing session snapshot, without another lookup.

The usage factory registers both the core usage port and `anthropic.usage_walk`
version 1. Backfill validates a session's tenant stamp against the deployment's
known tenants before granting that session to the model-span reader. The host
restores the existing SDK exception type at its M0 boundary.
The complete billing inventory uses `anthropic.session_walk@1` with an explicit
workspace scope and the original argument-free native session list. The host
retains tenant validation, incremental cutoffs and billing-exempt handling.

### Gemini inline reuse

The Gemini driver is available only through explicit construction and the
non-core `gemini.inline_reuse` profile. It retains interaction history and
workspace references between turns, and exposes workspace expiry as a visible
continuity failure. Saved interaction steps supply authoritative events;
streamed deltas remain previews. Interaction usage preserves missing token
counts, raw meters and correction revisions. Gemini's separately reported
response and thought tokens are combined into inclusive neutral output tokens.
Unsupported capabilities fail admission or refuse before provider mutation.
Existing default resolution continues to select Anthropic.

Gemini workspace snapshots accept 40 MiB binary members within a 64 MiB archive
budget, checked on both received and expanded bytes. Snapshot member reads and
artifact downloads use chunks of at most 64 KiB; unsafe or incomplete archives
still fail with typed errors. Ordinary artifact uploads retain their separate
16 MiB cap. The host's 20 MiB compressed checkpoint-bundle limit is unchanged:
the snapshot budget governs expanded workspace files, rather than the bundle
delivered to a successor.

Required actions exclude saved calls with matching results, including filesystem
calls automatically executed in the environment. Open steps from in-progress
snapshots enter authoritative history only after the interaction pauses or ends.
A persisted provider timestamp watermark advances even when usage counts stay
equal, so older snapshots cannot rewind usage or session state after a restart.

Missing usage stages remain unknown pending recorded provider evidence; no zero
counts or tool-token billing rules are inferred. Interaction meters under a
shared root are separate observations, rather than a claimed root-turn aggregate.
The host accounting bridge must establish coverage before summing overlapping
grains. Production metadata storage is still injected: its transaction mechanism
must avoid retaining database connections during provider I/O.

The explicit offline Gemini conformance adapter executes C03, C05, C06, C07,
C10, C13, C15 and C16 against the actual driver and injected test store. C13
proves that store's binding CAS, rather than production host registration.
The pinned C01–C18 matrix has eight PASS and ten typed PENDING results, and
cannot certify. Reasons distinguish unavailable capabilities from missing
adapter or host dependencies. Registering the adapter discovers no real
transport. C09's exact binary snapshot, pagination and interrupted-read tests
do not prove its missing provider vault and session-deletion requirements.

C05 scripts documented native saved steps and canonical GET revisions. Its
saved-message identity expectation is `step:0`: the pinned SDK ModelOutputStep
has no native ID. Other adapters retain the fixture default `item`. A streamed
child-completion signal and EOF cannot release the root. Only the canonical
root completion journals final content and one authoritative outcome. Native
stream errors persist a deduplicated gap before delivery, exposed by journal
reads and reconciliation after restart. Saved steps recover final content but
cannot establish lossless replay of the interrupted SSE domain, so that gap
remains explicitly unrecoverable. Preview content never enters saved history.

Broken-driver checks reject missing projection gaps, wrong item expectations,
public raw handles, unknown usage converted to zero, queued ambiguous sends,
premature occupancy release, unconditional admission, supported migration and
ignored transport faults. Cancellation fault revisions advance on a logical
script clock rather than relying on wall-clock resolution. C10's admission
faults are a documented no-op: the profile statically refuses the required
memory store before any writes. C08 awaits the next-turn update port; C11
awaits interaction-time inline skill deployment evidence. C04 lacks an
ambiguous accepted-POST lookup. Host admission/batching, accounting recovery,
backend selection, wake fencing and outcome persistence remain typed adapter
dependencies until their genuine scenarios and hooks are connected.

Gemini resource ports emulate a scoped skill catalogue and host artifact uploads.
Skill bundles require a UTF-8 root `SKILL.md`, canonical relative paths and at
most 256 files. Inline text mounts obey 1 MiB per file and 2 MiB combined;
an explicitly injected full-byte GCS publisher supports binary assets and
bundles up to the driver's 64 MiB bound. Agents use explicit immutable
version pins; publishing changes future pins only. UTF-8 uploaded files mount
as inline environment sources. Binary uploads are downloadable from host storage,
but binary inline mounts are refused. Conflicting source targets are rejected.

Workspace artifact discovery downloads the current environment's binary tar
snapshot through the Files API. Its roster includes workspace files and inputs;
the host selects which files to deliver. Pagination keeps a stable discovered roster;
downloads fetch a fresh snapshot and require the original file digest. Missing
workspace snapshots expose `ContinuityLost`; changed or missing files fail visibly.
Historical per-turn snapshots, native vaults and workspace-file deletion are
unsupported. Snapshot parsing accepts plain tar or gzip-compressed tar, bounds decompression
before parsing metadata, and limits archive/expanded data to 64 MiB, each file
to 16 MiB and members to 2048, and rejects traversal, links and duplicate paths.
The SDK buffers network bytes before these parser limits apply. Host uploads
also have a 16 MiB limit. This resource support requires injected transactional
host storage and does not enable a new default backend or production host wiring.

## OpenAI driver core

The unwired `mux.drivers.openai` implementation uses OpenAI Agents API sessions
with a hosted environment for `openai.persistent_workspace`, or no environment for
explicit `openai.conversation_only`. Root turn outcomes are authoritative;
subagent completion, idle and EOF are not. Recovery merges saved work with a
buffered live stream and discloses unreplayable event gaps. Usage counts remain
nullable and corrections retain one observation identity with higher revisions.
The host supplies authorization and durable snapshot/revision state. This package
does not enable OpenAI in Daimon or change Anthropic defaults. The driver README
lists the implemented surface and refused operations.
The persistent profile is core with offline evidence for all nine mandatory
capabilities, including skills and artifacts. Vault ports are implemented;
multiagent refuses admission and complete workspace export/import remains unknown.
## CLI GitHub grant cleanup

GitHub grant changes archive live sessions and their App vaults through
resource ports. Both archive operations use the tenant and CLI admin
account established by the existing authorization check. Session archive,
token revocation, vault archive and local session retirement retain their
existing order, duplicate-session handling, operator output and SDK errors.
Repeated vault cleanup keeps its existing missing, conflict and already-archived
responses. Grant edits that can rotate tokens in place keep that path.

### Adapter-declared conformance gaps

Adapters can declare individual fixtures PENDING with a typed capability,
live-key or dependency reason. The declaration remains visible and blocks
certification; undeclared capability errors still fail their probes.

### Manual probe budgets

`mux.conformance.budget` reserves worst-case spend before provider work and
persists admission and settlement receipts. Runs exceeding 80% of a configured
provider line or total cap are refused. Missing, altered or rolled-back ledger
state also refuses admission. An authorized caller supplies reviewed pricing
and enforces token limits; the guard itself makes no provider calls.
The native `anthropic.session_walk@1` extension exposes `SessionWalk.walk(scope)`
as an async iterator of neutral sessions with opaque native snapshots. It sends
no list filters and follows the SDK paginator exactly. Workspace-wide billing
sweeps pass a justified platform scope; tenant walks retain grants and tag checks.
### In-place session updates

Session tool/MCP changes use a pure plan followed by one apply request. The closed
`anthropic.session_update@1` config accepts only the native agent tool/MCP patch
and session metadata; it retains native ordering, omissions, nulls and empty
arrays. Plans carry the caller's snapshot revision and apply rejects a different
revision before I/O. The Anthropic endpoint does not offer server revision CAS.
An explicit empty patch reuses the session without I/O; unsupported generic
revision/environment changes and fresh-state requests fail explicitly. The host
still decides changes and preserves existing busy deferral and snapshots.

Mount deletion and repository token rotation use session administration. Token
material is resolved and redacted at the existing driver boundary. Secret file
upload and inherited-seal reads receive the existing tenant/account context.
An unreadable predecessor remains conservatively sealed to the successor thread.

Session output delivery uses `anthropic.outputs@1`: one listing page per poll,
whose entries retain their opaque SDK snapshots. Listing does not require
delivery fields on pending entries or invent missing creation timestamps;
the host applies its existing downloadable filter and delivery field checks.
Buffered download and deletion retain the managed-agents beta. Standalone bundle
uploads and the workspace-wide TTL queue retain the default Files API headers.
The host keeps settle timing, exclusions, size limits, consent deferral and
post-before-delete ordering. No additional provider requests are introduced.

The native session-resources extension adds lazy `walk` and identity-returning
`add_file` methods. Existing methods retain their behavior. The host stops at
the first `.env` mount without fetching later pages and records the one add
response identity without a retrieve. Session and file grants are checked before I/O.
### OpenAI offline conformance

Explicit registration through `mux.drivers.openai.conformance.register` runs the
actual OpenAI driver and pinned SDK against synthetic HTTP/SSE records. Each
shared fixture gets a fresh driver and independent request log. C05, C06, C09, C10,
C15 and C16 pass; the other twelve entries declare typed pending reasons for
unsupported conditional updates or missing host certification adapters. Pending entries prevent a complete certificate. Broken variants cover
content corruption, premature root release/stop, admission/scope bypass,
successful unsupported migration, public handles and undeclared extensions.
The registration context closes streams and SDK clients. This adds no provider
discovery or host activation and makes no live calls.

### Experimental turn bridge

`DAIMON_TURN__PATH=legacy` remains the default, including an empty value.
Setting `mux` opts into Events/Sessions ports for turn input, confirmed stream
opening and status checks. The host keeps its existing authorization, billing,
rendering and lifecycle policies. Prepared turns derive the scope from the
admitted tenant and account; direct callers must supply their authorized scope.
An injected backend also requires the session's bound ResourceRef. Ancillary
Messages calls retain a separate client injection point in TurnDeps.

The bridge waits for GET stream to open before POST input, independently of the
first SSE event. It closes abandoned streams on cancellation and preserves the
existing per-call read timeout. A temporary host edge decodes the opaque native
JSON snapshot for existing reducers and hooks; SDK objects never cross neutral
ports. Normalized root outcomes select the host termination reason after its
existing failure refinements. Replay, cancellation and orphan cleanup use the same bound session ports.
Legacy turns introduce no StateStore journal or additional database writes.
Prepared mux turns use the binding-owned persistence path described below.

The Anthropic-only `anthropic.session_system_message@1` config carries privileged
text framing for the initial handoff batch. Its closed schema accepts text
blocks only, and the driver requires exactly one system event at the end,
immediately following user input. It cannot send arbitrary native event types
or SDK kwargs. The unchanged empty framing path omits the event entirely.

Mux live model spans carry the owned UsageObservation into turn telemetry.
N8's input projection retains the existing four stage totals and event-ID
deduplication; unknown buckets remain unknown on the observation and contribute
no measurement to totals. Overlapping turn/session or cumulative meters are
rejected rather than combined with disjoint model-request increments.

Unknown native root-idle reasons remain opaque in the normalized contract. The
Anthropic M0 compatibility edge retains legacy terminal handling for those
records, without adding a neutral outcome or a replay request.


## Manual conformance probes

The standalone `mux.conformance.budget` guard reserves conservative spend before
provider work and appends receipts. All callers share a pinned ledger, checkpoint
and stable sequence lock; resets and paired snapshot rollbacks refuse admission.
Runs exceeding 80% of provider or total caps are refused. Failed, cancelled and
unknown-usage runs retain their reservations. Operators supply reviewed prices
and enforce token bounds. The harness discovers no keys or SDKs.

Conformance tapes contain normalized mux events and request metadata: method,
path, a redacted header allowlist and body field names. They never retain raw
HTTP bodies, body values, URL queries or opaque native event records. Unsupported
opaque event content is refused. Tool/action argument mappings retain only an
omission sentinel; export and replay refuse other arbitrary payload mappings.
A defense-in-depth credential audit covers
written evidence and reconstructed normalized text before export and replay.
Replay feeds a caller-supplied offline event fake and recomputes fixture checks;
PENDING never certifies. Native HTTP/SSE codec certification is separate work.
See `packages/mux/mux/conformance/README.md` for the APIs and limitations.

## Persistent memory and resource identity reads

Persistent memory provisioning, orphan cleanup, archival and memory command
reads use `anthropic.memory_stores@1`. The driver preserves the SDK's memory
beta header, prefix entries, pagination stop rules and selected full-content
reads. Warm provisioning performs no provider request. A lost binding race or
failed insert deletes only the newly created orphan; agent archival clears its
binding even when the provider reports the store missing. Session archival
does not archive or delete shared memory.

Resolver reads retain partial native SDK environment snapshots when configuration
is omitted or null; admission still reads metadata and archive state without an
additional request. Resolver reads keep the host's existing TTL caches and in-flight coordination;
the driver introduces no cache or replay layer. Owned resolver, setup, report
reader and OAuth/token attachment callers pass their tenant scope through
reads, writes and version-conflict retries. Memory command and MCP attachment
helpers accept explicit scopes while retaining named legacy authorization
seams for existing adapter callers without scope arguments. Conditional memory
writes are unsupported and fail before provider I/O.

M0 memory listing/content reads and environment identity retrieval preserve the
SDK's partial native response snapshots through operation-specific driver
extensions (`anthropic.memory_stores@1` native reads/walks and
`anthropic.environment_reads@1`). The compatibility edge reconstructs SDK records without requiring
unused paths, identifiers, configuration or timestamps. Prefix and unknown list
rows retain their original discriminator and pagination behavior. Tenant and
account checks still run before requests, and environment metadata checks still
run after the existing response. Neutral resource records remain validated.
Thread handoff collects session seals through scoped lifecycle reads before its
policy transaction. Bundle reuploads use the native outputs extension and share
the Artifacts upload request, archive bytes and retention queue. Checkpoint billing,
access rechecks and the full/transcript/history fallback ladder remain host policy.

Workspace transfer uses `anthropic.workspace_transfer@1` to carry the inline
full/transcript/history rung. The closed schema stays in the driver. Pure native
export declares its archive, digest, transcript presence and losses with
`best_effort` consistency; restore verifies the payload, source authorization
and accepted losses, then returns neutral mounts for the existing create call.
No manifest upload, provider lookup, second session create or first send is added.
The host retains the one billed checkpoint, its access fence, fallback notices,
quoted transcript and system-message policy. Generic lifecycle export/restore
stay unsupported; this explicit native extension handles the existing MA ladder.

Workspace checkpoint runners accept the account from the caller's existing
authorization decision and pass its tenant/account scope to the turn bridge and
native restore. Existing direct callers retain their optional-account semantics
and their existing requests, checkpoint debit, before-send callback and fallback
behavior; the production chat caller supplies its reauthorized account.

Workspace rehosting uses the native outputs extension's ID-only upload result.
The request helper is shared with the generic Artifacts port; beta headers,
multipart bytes and download/delete/upload ordering stay unchanged. Successful
partial upload replies do not require a filename, media type or timestamp before
the uploaded file is mounted and queued for cleanup.

### OpenAI resource ports

The OpenAI persistent profile now has actual-driver offline evidence for all nine
core capabilities: inline skills with immutable session pins, exact binary artifact
transfer and shared-vault retention join its existing turn/cancel/action/usage and
recovery ports. Explicit backend `openai` may omit its profile but still requires
a model. No configuration continues to choose Anthropic and per-caller threads.
C09 passes offline; host certification and twelve fixture dependencies remain
pending. The guarded live smoke returned `invalid_request`; it establishes no live
certificate. See the driver README for the evidence table and operation refusals.

### Turn cancellation and replay

On the experimental mux path, a cancel receipt reports request acceptance or
uncertainty; it never proves interruption. Stop waiting observes an authoritative
root idle or termination event and closes its stream on EOF, deadline or caller
cancellation. Interrupt echoes, previews, subagent completion and a running
status do not confirm a stop. A missing observation remains INTERRUPT_TIMEOUT.
Anthropic root idle after cancellation retains the legacy interrupted outcome;
independent session termination remains SESSION_TERMINATED.

The typed `anthropic.event_history@1` extension walks the existing SDK paginator
with one normalizer across pages. Reconnect replay preserves native pagination,
HTTP requests, event identity and existing reducers' de-duplication. Recovery
transcript reads and orphan archives use the admitted tenant/account scope;
sealed-DM retirement uses the conversation owner. Archives retain the existing
bounded, shielded cleanup and background completion logging. Sessions archive
comes from the existing resource driver; turn code adds no lifecycle port.

PR3 caller coordination includes the CLI's direct turn entry at
`packages/adapters/cli/daimon/adapters/cli/run/command.py:131`: mux must pass the
actual authorized tenant/account scope before provider I/O. N7 owns that
adapter change; the core bridge refuses a missing scope rather than supplying
a legacy-host authorization escape. The default legacy caller stays unchanged.

Headless routine turns on the mux path pass the caller's authorized tenant and
account to the shared turn driver. A missing identity is rejected before session
assembly or provider I/O. The default legacy path retains its existing requests.

Prepared turns also carry their admitted tenant/account scope through legacy
replay, interruption and confirmation recovery. Seal publication commits before
the existing provider read and conditional metadata update. The native
`anthropic.session_reads@1` extension preserves omitted fields and nullable
location metadata without constructing a provider binding. The metadata-only
update retains the original request bytes, existing seals and busy retry policy.

## Remaining CLI, Discord memory and scheduler calls

CLI and Discord memory reads retain their existing tenant authorization, pagination, formatting and errors while using the memory ports. Environment forks retain explicit null descriptions and the original native config key order. Scheduler refresh and cleanup use tenant/account scope from the session rows already read, including cleanup after an account is erased. Raw `daimon sessions get` remains a workspace operator inspection under an explicit platform scope and adds no database work. The opt-in CLI mux turn resolves and verifies the local operator's tenant/account before provider work; legacy turns retain their existing requests, deadline and billing exemption.

Environment forks validate the scoped request configuration and discard the SDK create reply, preserving success when unused response fields are omitted or null. Session and vault archive cleanup likewise consumes no provider reply fields.

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
Gemini probe preparation uses `python -m mux.drivers.gemini.live_cert`, with an
explicit model and empty output directory. Its default MockTransport command
uses a private ledger and no real key. An authorized `--live` command additionally
requires the pinned mode-0600 key file and reviewed model prices in the shared
N9 ledger configuration ($30 Gemini allocation, $24 admission stop). Live mode
requires exactly `gemini-3.8-flash` as primary; only a create POST returning 503
permits `gemini-flash-latest`, then `gemini-3.5-flash-lite`. Reviewed prices and
N9's matching policy are required before key access. Each attempt has its own
reservation, normalized tape and fallback receipt; every response saves nullable
token counters and known usage settles once. The moving alias remains unverified.
It records a bounded SDK smoke and the separate offline C-matrix, preserving
typed PENDING and unknown spend. Neither key presence nor offline passes grant a
live certificate. See the driver's `LIVE-CERT.md` for commands and provider-budget
limitations.
MCP repository binding and vault summaries use the verified tenant/account scope
and an exact account/agent-derived vault-name grant. Repository writes retain
create-before-commit, compensation and old-credential revocation ordering.
Bundle uploads retain the bounded spool, multipart filename/media type and
cleanup retention. Hosted charts retain the lazy Files API scan cap, Managed
Agents beta header and binary read. Hub sessions retain account, legacy-access
and seal filtering; skill details count partial native version rows. The native
compatibility edge consumes only fields used by these existing callers.
### Scoped core cleanup

Account purge, private-conversation quarantine and adapter orphan interruption
use the authorized tenant and account at the port boundary. Orphan rows with no account skip the provider interrupt and log the
reason while retaining card and marker retirement. Account session walks keep
SDK pagination and filter account metadata before deletion; missing sessions
retain the existing idempotent count.
Disposable-workspace test cleanup remains explicitly workspace-wide behind its
destructive opt-in and sentinel gates, retaining deletion order, pagination,
early sentinel exit and the environment archive fallback.

Responder identity retains its SDK read and response for configuration snapshot
backfill, including null channel and thread metadata, without an additional read.

### Default capability conformance

The supplementary F1 runner provisions the authored default agent's eleven
skills and immutable pins, adapter-mapped builtin tools and `daimon-mcp`. One
scripted turn loads a skill, uses two read-only MCP tools, exercises file and
bash capabilities and completes one root. The initial Anthropic tape supports
offline normalized replay, with corruption checks; it is synthetic evidence.
Incomplete tool mappings are typed PENDING. Missing atomic session revision
pinning is an explicit capability gap, while skill pins remain required.
Replay retains omitted tool arguments and does not certify their contents.
The standard eighteen-case matrix stays unchanged. See the package-local
`packages/mux/mux/conformance/README.md` for the adapter contract.
Responder identity uses the authorized row's tenant/account scope and the native
`anthropic.session_reads@1` extension. It retains the SDK response for configuration
snapshot backfill, including null channel and thread metadata, without an additional
read or a generic provider-binding projection.
The supplementary Gemini F1 adapter is explicitly selected from
`mux.drivers.gemini.default_capability`. It returns typed ADAPTER_DEPENDENCY
before I/O for the complete default's binary skill bundle and authenticated
MCP/session seams. Tests load the actual default YAML and complete skill trees;
supported text-only subsets cannot pass the full-default scenario. This offline
adapter supplies no live path and does not change the C01–C18 matrix or host
backend selection.

Gemini F1 maps bash, read, edit, grep, glob and write through its native
`code_execution` tool. Native scripted records exercise each route in normalized
replay; missing calls fail. The complete default remains typed PENDING for binary
skill deployment and authenticated MCP/session adapters.

### Provider turn composition

Host turn composition dispatches by the admitted channel profile. Provider
modules register a native preparation hook, a backend factory and a `TurnIO`
codec. Preparation must return an authorized provider-native session reference;
a missing hook refuses the selection before Anthropic binding. The backend
factory must preserve that reference and the admitted profile. Explicit selection
of another provider never falls back to Anthropic preparation or recovery.

Factories and codecs receive the admitted configuration/model, caller scope,
native session and an optional injected runtime containing the transport factory
and durable journal/usage stores. The host does no credential discovery in this
seam. Anthropic remains the default and keeps its existing model-less binding
and requests. Other runnable profiles require an explicit model; registering a
factory alone does not make its profile runnable at channel admission.

### Prepared mux turn persistence

With `DAIMON_TURN__PATH=mux`, prepared turns acquire the persisted binding's
slot lease before opening the provider stream. Private sessions are adopted
with their native IDs through binding CAS on the Anthropic path; shared sessions use the binding
already published by shared-thread preparation, with an accountless slot.
The writer's admitted tenant/account remains the operation owner. Legacy turns
perform none of these StateStore reads or writes.

An input, action decision, cancel or archive persists its intent and wins
`claim_send` before provider mutation. Claimed Anthropic mutations use a
single native attempt even when the shared client has its production retry
budget; read requests and ordinary legacy I/O retain that budget. A committed sent claim or uncertain
acknowledgement is replayed without another send; an acknowledged receipt is
restored. Reusing an operation key for changed input raises `OperationConflict`.
Only an explicit HTTP client refusal permits a fresh send attempt. A prepared
turn uses its current invocation observation ID by default; callers resuming
a known invocation supply its original `operation_key` to `run_prepared_turn`.
A mapping's older active-message marker never becomes a new invocation's key.

Normalized stream and replay records commit to the binding-owned journal
before reaching the existing reducers. Overlap is deduplicated by native source
ID, revision and preview authority. A stream gap checkpoints its last cursor
without fabricating completion; the host still replays through its existing
native history walk. Cancel acknowledgements stay distinct from independently
observed stop events, which are also journaled. Lease renewal runs during the
turn, sends have a timeout below the lease TTL, and losing renewal cancels the
pump. Claims, receipt commits and journal appends all require the active fence.

Direct driver calls without a prepared binding context retain their existing
port behavior and are not claimed to persist a complete turn journal. Native
confirmation recovery retains its existing HTTP proof and bounds while keeping
the prepared mux turn's claimed mutations and journaled stream records under
the same lease. Provider conformance certification remains separate from the
host restart and wire-parity proofs.

Foreign provider preparation must publish its actual `ProviderBinding` to the
injected StateStore before running a prepared turn. The host consumes that
binding only when profile, native session, channel, scope and configuration
revision match. It never adopts an Anthropic mapping for another provider. The
registered codec receives `TurnCodecRequest.persistence` and owns forwarding
its neutral records and mutations through this journal/claim context. Missing
native references or bindings fail before provider I/O.

Provider-native usage frames may carry an actual usage observation without a
display event. Prepared foreign sessions use their bound native observation
recorder, preserving nullable measurements and pending accounting. The default
Anthropic session continues to use its SDK span recorder and existing recovery.

Provider recovery can atomically publish a collected journal snapshot through
`TurnPersistence.record_many(session, events, cursor=...)`. It uses the active
binding lease and the same source identity/revision rules as individual records.
All records, the projection and the supplied cursor commit together; a malformed
late record leaves the entire batch unchanged.

### OpenAI host turns

Explicit caller-private `openai.persistent_workspace` channels run `gpt-6-luna`
through the mux turn path when channel backends are enabled and the host supplies
`OpenAIHostRuntime`. That injection contains scoped provisioned native resource
references, a private transport factory, explicit small hosted-container controls,
durable journal/revision adapters and verified accounting context.
Missing injection raises typed `AdmissionDenied(backend_unsupported)` before
native plan resolution or key discovery. Unsupported shared/transfer and
restricted-policy preparation also uses that visible host refusal. The unconfigured channel still
uses the existing Anthropic path and requests.

Native preparation publishes its actual session binding and reuses that hosted
workspace. Fenced send claims survive restart; an uncertain acknowledgement never
causes an automatic resend. Recovery journals a complete snapshot atomically,
keeps earlier turns out of current display/usage, and requires observed stop
before completing cancellation. Nullable usage and missing container cost remain
pending for reconciliation. Sealed/read-only/publishing-restricted policies,
shared threads, transfers and changed bound plans refuse in this initial host
composition. Browser-origin approval is distinct from browser authentication;
authenticated remote MCP is a separate integration.

OpenAI failed roots propagate a provider error after their native journal
commit, both during streaming and recovery. Recovered cancellation enters the
existing host interruption path after committing the snapshot; neither path
fabricates a successful idle, resends input, or sends another cancel. The
shared Anthropic finalizer remains unchanged.

This first OpenAI host slice requires verified delegation-disabled sessions.
The driver sends and checks `agent.multi_agent.enabled=false`; enabled saved
agents, missing verification, and delegation drift on reuse or before input
refuse. Delegated child accounting is deferred and is not certified by root-only
usage proofs.


OpenAI command/MCP items can normalize into separate tool-use and tool-result
records sharing one native event. Their host journal keys use each neutral event
ID for both live append and recovery batch append; provider provenance is retained
unchanged. Other providers keep the existing native-event journal key by default.


OpenAI G1 no longer requires the optional native `spend_control` setting. The
2026-10-10 Agents reference documents `spend_control.limit` as positive whole USD
cents with no higher stated minimum, but our guarded project probe rejected the
field with HTTP 400 and `param=spend_control`. G1 live tooling omits the field and
uses the canonical host budget guard plus bounded turn counts and whole-turn
deadlines. Those host limits do not create a provider token ceiling or an exact
bill: missing final usage/container evidence remains held and unverified. Explicit
native spend controls remain available to driver callers with verified support.
Gemini host turns use explicit channel configuration naming `gemini.inline_reuse`
and `gemini-3.8-flash`. A provider runtime supplies a revision-pinned neutral
agent/environment deployment, transactional driver records, the state journal,
a private transport factory and neutral accounting. The codec owns lazy stream
closure, reads authoritative saved interaction records for recovery, waits for
provider stop evidence on cancellation and bills cumulative interaction usage.
A lost or expired inline workspace surfaces a continuity error. Unconfigured
channel resolution retains its existing Anthropic digest. Shared ownership,
authenticated remote MCP and full default-capability deployment are follow-ups;
unsupported policies are refused explicitly. Offline proofs certify this host
slice; paid host-thread certification is a separate unit. Failed current roots
produce provider errors after durable recording; SDK-only replay of an externally
cancelled root retains its interrupted outcome. User-requested cancellation also
retains the independently observed interrupted outcome.
Gemini admission requires the configured Flash primary and caller-private mode.
Missing runtime dependencies or unsupported deployment policies return the visible
`backend_unsupported` refusal before transport construction. Journal source
identities include the interaction identity, preserving later turns and function
continuations in a reused session.
Gemini always sends an explicit `tools` list compiled from the agent configuration; an omitted neutral tool list becomes `[]`. The SDK boundary also makes older stored requests explicit. This prevents Gemini from silently enabling its default search and URL tools. Continuations and selected 503 fallback sends retain the same reviewed tools. See [Gemini supported tools](https://ai.google.dev/gemini-api/docs/antigravity-agent#supported-tools).

### Provider action callback

Opt-in provider codecs receive a scoped `TurnCodecRequest.provider_actions`
callback when a prepared caller supplies `provider_action`. It carries the
original required action and the admitted native session, current root,
requester and expiry. Browser origin permission has separate approve, deny and
cancel controls; browser authentication has completion and cancel controls,
with completion omitted until the provider verifies its native response.
Adapters authorize the requester and return an offered native response without
rendering native payloads or collecting credentials. Missing support, expiry
and turn cancellation leave the action unanswered. Responses still go through
the provider's claimed mutation path. Default Anthropic confirmations retain
their existing hook and wire requests.

Each provider-native approval starts its configured confirmation window when the
card is requested, capped by the turn deadline. An adapter hook failure refuses
the action and logs only the boundary name. Foreign dead sessions remain failed
outcomes; native replacement with its corresponding binding, accounting and
approval context requires a provider-specific recovery path. Anthropic recovery
continues to use its existing tool confirmation surface.
### Opt-in table rendering

Table rendering uses the same per-tenant setting for Anthropic, OpenAI and Gemini
turns. Enable it for an existing tenant UUID in the application's environment:

```bash
DAIMON_TABLE_RENDERING='{"11111111-1111-1111-1111-111111111111":true}'
```

Replace the example UUID with the tenant's UUID. Missing tenants and `false`
retain plain-text delivery. On Discord, supported final-answer Markdown tables
become PNG attachments with a marker at their position in the answer; Slack uses
native table blocks. Teams already renders tables natively. Provider text in the
journal remains unchanged. Streaming previews and fenced examples remain text;
rendering or attachment failures preserve the original answer. See
[platform table rendering](architecture.md#platform-table-rendering) for limits.

## Provider turn displays

Discord consumes neutral message snapshots and ordered previews through an optional local lifecycle tap. Previews affect the draft only; existing host terminal callbacks finalize the progress card and deliver the authoritative answer. Anthropic retains its native display path. Alternate-provider usage remains observation-based, and an unverified cost is omitted from the footer. Generic confirmation cards retain Approve/Deny and approved/denied/expired states.

Slack also consumes root neutral snapshots and ordered previews through the optional local tap. Authoritative snapshots replace draft fragments; terminal output comes from host state. Existing status-card replacement, Cancel routing, generic confirmation controls, and Anthropic pricing remain unchanged. An unverified alternate-provider cost is omitted.

Teams renders root neutral snapshots and ordered previews locally, retaining its debounce, unprompted silence and existing terminal card retirement. Host state owns final answers. Alternate-provider observations and missing observations bypass Anthropic display meters, so unknown cost is omitted; the native Anthropic path is unchanged.
### Alternate resource catalog

The host's `PostgresNeutralCatalog` retains OpenAI/Gemini agent revisions and checked skill bundles within an authorized tenant, account and provider workspace. Agent edits publish a new immutable native agent with a compare-and-swap host revision; historical revisions retain their native references and skill pins. The host revision does not change a provider's native revision. Exact-version bundles preserve their bytes, title and owning agent. Upload ownership is retained with each immutable version; callers commit a checked bundle before attachment.

This store requires additive migration `0089_neutral_catalog`. It supplies storage for explicitly selected alternate resource helpers; callers still perform policy rechecks, native creation and attachment. The default Anthropic resource path uses its existing storage and requests. Downgrading drops only the alternate catalog tables and their retained content. Retained versions store bounded skill archives in Postgres; storage grows with published versions. Account deletion cascades to both catalog tables.

### Scoped host resource helpers

For an explicitly provisioned OpenAI catalog, `NeutralResourceHost` constructs
the store from the authorized tenant/account/project routing tuple and calls
only the injected resource ports. `add_host_agent_skill` requires the caller's
fresh policy gate. It retains each native uploaded version and checked bundle
before replacing the catalog agent under local CAS. A known failed attachment
can reuse that retained upload after restart. An unknown native upload remains
the caller's claimed-operation reconciliation responsibility.

Catalog publication failures retain and log the native identity, including a
cancelled or ambiguous commit. A lost CAS does not prove an idempotently created
agent unpublished: a current or historical catalog revision can name that same
identity. Automatic CAS cleanup requires a fenced identity/history proof and is
not attempted by this edge. Readback compares the fields after OpenAI's optional
tool normalization while preserving the authored description and schema.

`fork_host_agent` applies the existing FORK authorization, copies source-owned
bundle bytes under new native skill ids, keeps shared library pins and omits
foreign-agent skills. Passing this host to `set_channel_rule` exercises the same
locked channel write and repeat-request behavior. Missing catalog ownership or
bytes refuses before mutation. An owned unversioned pin requires an injected
scoped native-default resolver; no latest-version or lexical fallback is used.
Gemini resource hosts remain pending G2. Runtime provisioning, remote MCP
authentication and live turns are separate gates. Unconfigured Anthropic paths
retain their existing requests, storage and policy.

## Gemini scoped MCP authentication

An opted-in `GeminiManagedAgents(..., mcp_secrets=resolver)` can prepare one
credential-referenced HTTPS MCP connection. The callback receives the complete
`Scope`, credential reference and exact destination and must authorize that
tuple. Its default is None; credential-referenced intent then refuses before
provider I/O. Ordinary no-MCP agents retain explicit `tools=[]`, and the
credential-free anonymous HTTPS MCP shape remains supported. Every MCP
connection, including anonymous intent and unmarked native SDK requests, rejects
userinfo, queries, fragments, ambiguous authorities, whitespace, invalid names
and non-HTTPS URLs before persistence, provider I/O or SDK DEBUG logging.

Authenticated policies accept exactly `allowed_tools`, a nonempty distinct list
of tool names. The native Interactions shape is
`allowed_tools=[{"mode":"auto","tools":[...]}]`; `required`, unknown policy
fields, wildcards, mixed anonymous/authenticated connections and multiple bound
servers refuse. Authentication cannot be supplied by saved native headers,
unknown tool types/spelling variants, undeclared tool fields or top-level
request credential carriers. The dispatch binding must stay on its exact
`mcp_server` tool and destination; moving it to an adjacent function refuses.

Only references and credential-free native intent persist in driver storage.
Each new send checks the complete request against the owned agent/environment
before resolution and before a history GET or delivery claim. A restored
acknowledgement also rechecks admission before its history read without another
POST. A preflight resolution admits the send; each actual SDK create resolves
again, including each existing Flash fallback. A revoked callback therefore
refuses before native dispatch. Model selection, fallback triggers, accounting
and continuity rules retain their existing owners' behavior.

The bearer appears once, in the sole server's native
`headers.Authorization`, as a request-memory value with a redacted DEBUG
representation. JSON serialization carries the actual value to that destination;
a JSON-roundtripped header is not a resolver binding and is refused on reuse.
The marker binds the full owned payload, allowing only the existing wrappers
to select the model and positive max_total_tokens. Changed input, environment,
instructions or nested credential carriers refuse before resolution.
Duplicating the resolved value elsewhere in the request also refuses. Resolver
failures return a sanitized permission error and do not log private details.

Offline proof uses the actual installed SDK against scripted HTTPS request
capture and an explicitly mapped disposable N2 loopback host for
`describe_agent`/`list_my_sessions`, with invalid/missing/revoked bearer and
cleanup controls. That mapping is a test bridge, not a public HTTPS tunnel,
provider-side remote execution, production resolver wiring or F2 live
certification. Runtime injection remains the host owner's follow-up; no operator
keys or live calls are involved.
For explicitly configured OpenAI sessions, `spend_limit_usd_cents` is an optional
integer cap in USD cents. The driver verifies its exact native echo on creation,
reuse and before input. Missing or changed configured limits refuse; omission
preserves existing behavior. If an acknowledged creation fails verification,
its native session reference remains in the durable creation claim for exact
owned cleanup, and preparation does not resend it automatically.

### Discord multi-human host conformance

`daimon.adapters.discord.conformance.multi_human_evidence` is an explicitly
imported offline probe for a dedicated idle bot with a normally provisioned
tenant. Every first and follow-up message passes through `bot.on_message`,
including protection/liveness reads, busy-thread routing and tenant slots.
The owner-controlled admitted-root barrier holds the first callback while the
probe observes production queue state; the bot owns draining and final release,
including deferred continuations. Evidence retains thread ownership, caller batch
order, composed text, and attachment identities. The callback performs normal
host admission/binding/turn execution; provider choreography, independent
workspace/configuration/principal checks and persisted charge facts remain
with the host envelope. The probe returns facts, never a verdict, and does
not activate a shared C01 fixture or certify a live run. Application imports
and default admission, queues and rendering are unchanged.

### Slack and Teams multi-human host conformance

The explicitly imported `daimon.adapters.slack.conformance` and
`daimon.adapters.teams.conformance` probes use dedicated idle apps and normal
platform orchestration, including real host cap/protection/setup reads and
queue settlement and final release; the probe does not clear production state.
Completion/overlap are observed at the selected callback boundary. Mutation
controls bypass real busy-thread routing or merge callers and fail the same
independent platform fixture. An owner-controlled native-root barrier holds the first
turn while authored follow-ups queue. Slack keeps each caller's first trigger
and composed file IDs; Teams keeps the last reply activity, earlier composed
IDs and ordered file names/kinds. Facts retain whether the callback returned;
that is distinct from a native successful outcome. Owner callbacks and the
shared oracle supply independent admission, workspace, principal, journal and
charge evidence. Application imports, default queues and provider pending
declarations are unchanged; a raw platform control does not certify C01.
An explicitly imported Gemini SDK conformance component checks cancellation/EOF
occupancy and usage revision replay on MockTransport. Tool/mount updates, native
vaults and deletion keep their typed limits; live certificates and exact-spend
settlement are separate. See
`packages/mux/mux/drivers/gemini/CONFORMANCE-SDK.md`.
Explicit channel backend configuration resolves runtime dependencies separately
for each admitted turn. Native preparation and execution use the same resolved
runtime and authorized scope; runtime lookup does not cache one channel's native
plan for another channel or configuration revision. Default Anthropic channels
do not construct provider runtimes. An explicitly selected provider whose
deployment constructor is absent is refused before provider transport access.

Inspecting the runtime mapping's size or profile names never constructs a
provider runtime. `PreparedTurn.runtime` is optional and defaults to `None`;
its private usage recorder remains outside the public prepared-turn fields.

### OpenAI deployment runtime

Install the `daimon-mux[openai]` extra in deployments that explicitly enable
OpenAI channels. The default application dependency remains unchanged.

The application resolves `openai.persistent_workspace` per admitted turn when
`DAIMON_TURN__PATH=mux` and `DAIMON_TURN__CHANNEL_BACKENDS=true` are explicitly
enabled. Injected runtimes remain supported. Without an injection, export
`DAIMON_TURN__PROVIDER_RUNTIME_FILE` in the application's process environment
pointing to an operator-owned JSON file. Its entries authorize provisioned native
resources for one exact tenant, account, channel and backend configuration digest:

```json
[
  {
    "profile": "openai.persistent_workspace",
    "tenant_id": "<tenant UUID>",
    "platform": "slack",
    "channel_id": "<channel ID>",
    "account_id": "<authorized account UUID>",
    "config_digest": "<admitted ConfigRevision.digest>",
    "project": "<actual OpenAI project ID>",
    "agent_id": "<provisioned OpenAI agent ID>",
    "environment_id": "<provisioned OpenAI environment template ID>",
    "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
    "skill_ids": []
  }
]
```

Export the referenced key variable through the deployment's secret mechanism.
The manifest contains environment variable names, never credentials; unknown
fields, ambiguous matches, stale configuration digests and missing resources
refuse. `skill_ids` grants only explicitly named native skills used by the saved
agent. The selected plan pins real native resources to the authorized account and
project; admission's Anthropic agent/environment IDs are never reused for OpenAI.
An optional positive integer `spend_limit_usd_cents` is passed through the
provider's public session controls only when an operator supplies it for a
verified project. Omission uses the provider fix's unset control; its native
representation belongs to the OpenAI driver. The host fixes `gpt-6-luna`,
small containers and disabled delegation.

Unconfigured channels and the default Anthropic path read neither the manifest
nor any provider key and construct no foreign client. G1 rechecks policy before
the selected transport reads its key. Missing credentials refuse; another
provider's entry is never used as a fallback. Native SDK request and stream
lifetimes belong to the driver. Journal and usage revisions use the persisted
host binding/lease, with unknown prices and container costs left pending for
reconciliation. Gemini deployment construction follows in a separate delta.

Configured OpenAI runtimes refuse `OPENAI_CUSTOM_HEADERS` before constructing
a client, including changes introduced after runtime preparation. This prevents
ambient header injection from replacing the selected channel key or project;
the refusal never includes header contents.
Gemini can compile the complete eleven-skill default through an explicitly
injected full-byte GCS bundle publisher. Its immutable receipts bind ownership,
version and the complete file hash manifest, survive driver restart, and mount
directories at `.agents/skills/<skill-id>`. Existing text-only inline mounts
remain the default. Missing authorized publication is a typed deployment
dependency; offline compiler proofs do not promote live F1/QA parity. The host
supplies GCS access/retention and authenticated MCP separately. See the Gemini
`packages/mux/mux/drivers/gemini/DEFAULT-CAPABILITY.md`.

Gemini accepts a neutral MCP toolset selector only when it names a declared
server; the server’s auth and policy checks remain mandatory. The F2 offline
proof uses all original default skill bytes, one mapped code-execution tool,
and two read-only MCP calls served by a disposable loopback host. Model/GCS
execution stays replayed and the uninjected default adapter remains PENDING.

### Ordered offline catalog steps

The QA catalog executor binds channel selection, host turns, completion deadlines
and waits of at most 30 seconds in source order. Completion deadlines cancel and
drain the owned turn task; wait receipts require complete terminal captures for
the acknowledged session and root. Every source operation retains a receipt.
An unsupported admin operation, ingress route, context message, attachment,
native schema or completion gate stops dependent steps. Earlier completed turns
remain partial evidence; catalog capabilities and remote tool/workspace effects
remain typed PENDING and cannot earn a full scenario PASS.

Native OpenAI/Gemini fixtures retain exact wire consumption and source validation
before any mock dispatch. Anthropic inputs remain the existing authored native
SDK tapes; missing tapes stay PENDING until the fixture owner supplies pinned
inputs. Frozen TARGET-53 membership, assertions and source hashes are unchanged.

### Provider terminal outcomes

On explicitly selected OpenAI and Gemini channels, authoritative root outcomes
`errored` and `terminated` reach terminal failure delivery and the persisted turn
outcome as `upstream` and `session_terminated`, respectively. Display compatibility
records do not replace that provider truth. Preview, gap and child-frame metadata
cannot promote a root failure. Anthropic's existing outcome behavior is unchanged.
Gemini native `incomplete` is `session_terminated`, including when its real stream
or restart replay refuses the failed root before display delivery. That refusal
remains nonretryable and does not permit billing success or a recovery send.
### Typed provider-action cards

Discord renders the scoped provider-action callback as separate browser-origin and authentication cards. It returns the exact offered response object after checking the requester and bound destination. Origin access offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when the provider supplies that response. Expiry and turn Stop retire the controls. Native action payloads and credentials never enter card components, and Anthropic prepared calls attach no provider-action callback.

Slack attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.

Teams attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.

### Neutral lifecycle event identity

The optional `on_provider_event(Event)` lifecycle tap receives non-Anthropic
normalized frames. Its per-turn identity ledger survives reconnects and is
separate from the native compatibility display ledger, so a revised media item
can reach the tap without replaying the native display event. Missing hooks are
a no-op. The recovery lifecycle wrapper forwards the optional tap; the
Anthropic native callback keeps its existing order and payload.

### Platform backend-selection callbacks

The Discord, Slack and Teams adapters provide explicitly imported offline
`backend_conformance` callbacks over their normal admission and binding paths.
They return the actual prepared turn or the normally rendered admission refusal
for host conformance collectors; they do not select revisions, inject bindings
or change application startup. Use dedicated idle applications and scripted
transports. Each adapter's `BACKEND-CONFORMANCE.md` describes its input boundary
and the independent database and SDK mutation controls.

### Offline host turn conformance evidence

`daimon.testing.turn_conformance` exposes optional evidence collectors for backend selection (C14) and durable wake
fencing (C17). Provider harnesses supply authorized callbacks using normal host
admission and the real provider driver. The collectors return independent
binding, journal and wake facts; they never return a conformance verdict.
Application startup and unconfigured channels do not import these collectors.

C14 callbacks must use normal current-config admission. On explicitly configured mux channels, admission selects an authorized existing binding and validates its persisted provider/profile/revision before preparing the turn. A new thread selects the current channel revision. Missing retained configuration or an unavailable original registration is refused as `backend_unsupported` before provider effects. Legacy and unconfigured admission do not read native bindings. C17
requires an independently expected native event in the journal, durable wake
claim takeover, stale generation refusal and released driver leases. Missing
provider envelopes remain PENDING. Wake probes refuse non-isolated databases
before connection. The shared `daimon_test` name is allowed only with both
GitHub Actions CI flags and `isolated_test_schema()` builder provenance for the
current process; a missing registry getter fails closed. Scripted host persistence controls are
reported separately from complete provider/live conformance. The C18 terminal-failure
fix is separate from the optional outcome evidence collector.

These explicit C17 envelopes call the shared `daimon.testing.db.require_owned_sessionmaker()` before accessing their store or constructing a session. The shared guard validates the primary bind and every model/table route, including connection binds; each engine must independently satisfy the lane or registered isolated-CI policy.

The owned-database host hook test fixture skips the contributor default local `daimon_test` before tenant provisioning. Exact isolated GitHub Actions CI still validates registered schema provenance and all database routes before provisioning.

### Committed termination evidence

`daimon.testing.turn_conformance.TerminationCase` and `termination_evidence`
collect typed C18 facts under an explicitly supplied authorized scope and owned
database. Provider harnesses declare reachable reasons and expected outcomes
before running normal host callbacks. The collector activates a fresh observation,
drains its detached writer and reads the exact committed outcome ID. Expected
account/session values do not populate actual attribution. Recovery preserves
the host finalizer's once-only write. Missing rows, wrong attribution, unfinalized
states and incomplete case coverage remain visible; there is no verdict field.
Absent provider envelopes remain PENDING. No application defaults change.

### Gemini deployment runtime

Install `daimon-mux[gemini]` only for deployments selecting
`gemini.inline_reuse` with `gemini-3.8-flash`. Enable the same explicit mux and
channel-backend flags used for OpenAI. `DAIMON_TURN__PROVIDER_RUNTIME_FILE` may
contain both providers; selection requires the exact admitted profile, tenant,
platform, channel, account and configuration digest.

A Gemini entry uses the common selector fields plus:

```json
{
  "profile": "gemini.inline_reuse",
  "tenant_id": "<tenant UUID>",
  "platform": "slack",
  "channel_id": "<channel ID>",
  "account_id": "<authorized account UUID>",
  "config_digest": "<admitted ConfigRevision.digest>",
  "project": "<actual Gemini project associated with this key>",
  "api_key_env": "DAIMON_CHANNEL_GEMINI_KEY",
  "agent_name": "configured-agent",
  "system": "<explicit native system instruction>",
  "tools": [],
  "environment": {"name": "inline-workspace"},
  "records_path": "/var/lib/daimon/gemini-records.sqlite",
  "holds_path": "/var/lib/daimon/gemini-holds.sqlite",
  "hold_usd": "0.05",
  "cap_usd": "0.25"
}
```

`tools` is required: `[]` sends an explicitly empty native list. Omitted/null
tools refuse before key access. Search, URL context, execution or custom tools
run only when an operator explicitly lists a supported `ToolSpec`. The separate
Gemini explicit-tools product also protects older requests; it is required
before live deployment. Unsupported workspace/MCP/confirmation policies refuse.
Anthropic admission agent/environment IDs never become Gemini identities.

Provision the parent directories and persistent local volumes before enabling
the channel. SQLite commits the complete driver record transaction and
serializes workers sharing the same database; all workers for a provider
account must share the paths. Account data is partitioned by authorized tenant,
account and project. The two paths must be absolute and distinct, so a durable
hold can commit before native dispatch without deadlocking a record transaction.
These files are created only when the configured provider uses them; no store,
manifest or provider key is read on the default path. Newly created files are
private to the process owner. Multi-host storage requires a separate adapter.

Each Flash attempt first persists an explicit operator hold, then retains its
accepted/refused/unknown receipt. Holds are estimates, not actual spend. They
remain reserved until an accounting reconciler supplies verified provider and
infrastructure actuals; this slice never treats a refusal as a free call or
releases an unknown outcome. Exhausting the configured hold cap refuses further
attempts. The provider usage outbox preserves native usage with unknown prices
and infrastructure costs pending. Automatic actual settlement/hold release is a
follow-up; this runtime is not a live certificate or QA parity claim.

Credential discovery happens only in the selected authorized transport factory.
Private request-owned SDK clients use the explicit key, pinned Google origin,
`vertexai=False`, no environment proxies and no native mutation retries. Stream
completion, close, cancellation and close failure dispose the client lifetime.

## Runtime credential reference validation

Use host-scoped credential names such as `DAIMON_CHANNEL_OPENAI_KEY` and
`DAIMON_CHANNEL_GEMINI_KEY` in `api_key_env`. Reserved provider prefixes
`OPENAI_`, `ANTHROPIC_`, `GOOGLE_` and `GEMINI_` are refused case-insensitively: the
SDK isolation guard rejects ambient provider variables, including custom names
under those prefixes. The guard remains in force.

With the mux path and channel backends explicitly enabled, the settings loaders
check an explicitly configured runtime manifest before the first turn and raise
`ProviderRuntimeConfigurationError` for a reserved reference. This check reads
manifest metadata only; it does not look up a key, construct a runtime or open
provider storage. Provider plan validation applies the same reference policy.
Missing manifests retain the selected-turn refusal. Scope/digest, complete native
policy and key availability still require admission; no credential is read for
unconfigured channels. Legacy or disabled channel-backend settings do not inspect
the runtime manifest.


### Offline A7 Anthropic preparation

`python -m daimon.testing.a7_anthropic` prepares an explicit offline probe pack, a state-aware legacy/mux replay benchmark and a native admission inventory. It is never imported by the app. The mock SDK client refuses ambient Anthropic settings before allocation, copy and dispatch; the database guard checks every sessionmaker bind through the shared owned-sessionmaker policy. The benchmark times actual SDK/driver/journal/claim/lease work on fresh binding slots and compares request/event/state digests. Existing-client quiet runs require explicit lead GO, a clean pinned checkout, 200 measured plus 20 warmup pairs and fixed CPU/load limits. Cold or loaded diagnostics cannot certify latency; no mode reads keys or executes a live request. Recipe readiness stays PENDING until native callbacks, supported limit acknowledgement, canonical budget reservation, disposable scope and cleanup are provided by the owners. This latency gate is separate from the complete M0 certificate and live token/dollar equivalence.

Use a new evidence directory: `uv run python -m daimon.testing.a7_anthropic --mode offline-probes --output /tmp/a7-offline-unique`. `--mode live-plan` checks immutable native recipes only. `--mode quiet-benchmark --quiet-window-go` is opt-in and remains entirely offline; run it detached after the lead declares the host quiet. Preserve raw timings and failed results.
### Discord restart recovery reads

Discord startup replay reads the session authorized by its durable tenant and
account mapping through the native session-read extension. The recovery edge
retains SDK partial-response behavior and consumes only the session status before
replaying events. It keeps the existing read deadline, SDK retries, event ordering
and completed-current-turn checks, and adds no provider lookup or turn write.
### Adapter initial progress receipts

Discord, Slack and Teams lifecycles expose optional `on_initial_progress` for
accepted `post_initial` sends/edits before native session binding. Default
construction observes nothing. The closed receipt carries the actual public
card reference, route, native render input and injected clock; it is joined to
independent wire, binding and fresh-root evidence by QA owners. Initial UI alone
does not certify running provider work or release a completion gate. See each
adapter's `PROGRESS-RECEIPTS.md` for the observation boundary and failure rules.

### Explicit in-process Discord ingress

Discord's test package provides an opt-in gateway harness that delivers original
message events through discord.py dispatch and the real admission, queue and
thread-title paths. Its HTTP and admission/deadline clock dependencies are
explicit; ordinary bot construction retains the existing behavior. Captured
thread-create requests, title-call bytes, naming-meter rows and original history
reads are host observations. Full native completion and catalog assertion joins
remain independent runner responsibilities; a title or initial card never
certifies a provider turn. No routing context or message identity is synthesized.

The title/admission harness refuses opaque provider runtime factories, injected
backends and stores before real gateway callbacks. Its allowed SDK and DB
dependencies must be the actual captured client and owned factories, including
nested admission dependencies. Native alternate-provider completion requires a
separate explicit offline binding; initial-card pauses do not authorize it.

### Offline Discord thread-permission observation

Discord's explicitly imported test support can bind an owned parent channel to
an offline `discord.Forbidden` thread-creation edge, invoke normal ingress and
capture the actual serialized parent notice plus native HTTP trace. Default
host naming stays enabled and its exact Messages request remains visible;
only that named host request is excluded from provider-turn writes. It checks
real tenant/account rows and configured backend selection before invocation.
The source text/deadline predicates remain independent of the receipt; an offline
result does not certify live Discord permission settings. Product error copy and
default behavior are unchanged. See the Discord test support's
`FORBIDDEN-CONFORMANCE.md` for the explicit runner contract.


### Concurrent offline Discord ingress observations

The opt-in `daimon.testing.discord_ingress.DiscordConcurrentDriver` captures
actual production thread/tenant queues, gateway IDs, timestamped reactions,
posts, durable provider bindings and leases while native roots remain gated.
It requires an actual active host binding before concurrent delivery, retains
observed ordering and leaves native gate/oracle ownership with the runner.
The owned cap probe uses the signed admin turn-cap/default mutation/readback;
an isolated Anthropic SDK transport exercises real host continuation/release.
Alternate-provider capture bindings and independent native recipe pins remain
separate from these adapter observations.
A full real waiting-card interval distinguishes missing queue copy from
successful queue release. No adapter policy or default path changes.

### Offline channel burst and chunk reply observations

Discord testing ingress captures actual message-fetch responses and the SDK's
parsed reply references alongside production admission, queue and card facts.
Authored equal-text mentions retain distinct identities and their real separate
threads. A reply to a later answer chunk uses its emitted message identifier and
resolved SDK author, with native completion evidence supplied independently.

Gemini driver-owned conformance hooks are opt-in: `recover_send` preserves typed
unknown delivery without native key lookup or resending; `prepare_inline_skill`
binds a local immutable bundle and host-supplied session before interaction-time
deployment. Actual-SDK MockTransport tests retain exact request bytes and prove
required-action occupancy. C04/C11 remain shared-fixture PENDING until complete
host/provider envelopes are composed. See
`packages/mux/mux/drivers/gemini/CONFORMANCE-HOOKS.md`
for the evidence boundary; no live calls or default selection changes.

### D11 native footer binding (offline QA)

`daimon.testing.d11_binding.D11TurnBinding` connects explicit OpenAI/Gemini QA
turns to the real recorder and the read-only per-turn accounting API. Construct
a fresh binding inside each profile factory, after authorizing a UUID tenant,
native session/root/model and persisted provider binding in an owned test DB.
Install `binding.billing()` before native dispatch. The default profile gets no
native cost context; the opt-in profile gets its root-bound cost context. Missing
rates, infrastructure evidence or native totals never become guessed charges.

After dispatch, call `await binding.evaluate(evidence=run_evidence,
capture=actual_adapter_capture, receipts=native_receipts, inventory=native_inventory,
assertions=source_assertions)`. Supply normalized receipts and an exhausted
inventory independently from the native execution, never from queried ledger
rows. The returned result validates accepted post history and raw text payloads,
evaluates every supplied predicate and replaces only the two D11 accounting
checks with the owned ledger join. Complete opted results are labelled
`PASS under opt-in profile`; complete default native posts lacking dollars fail.
Unknown coverage or cost retains its typed accounting reason. Controlled rates
prove offline host arithmetic only. The consumer must retain all independent
source, ingress and capability gaps; this hook cannot certify those gaps or live
spend. C12 aggregate coverage remains deferred.
