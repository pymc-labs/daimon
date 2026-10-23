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
| `anthropic.managed_agents` | yes | Every core capability is native. `reconcile` reads the session status and the newest 5,000 events; older history is reported as a `history_truncated` gap. Steering, stream-cursor replay, environment fork and native schedules are not offered. |
| `openai.persistent_workspace` | yes | `reconcile` is emulated from saved items; missed events cannot be replayed. |
| `openai.conversation_only` | no | No workspace. Runnable for explicitly granted conversation-only fleet workers. |
| `gemini.inline_reuse` | no | The inline environment expires after inactivity, so workspace persistence is not guaranteed. Its usage reporting is undeclared, so admission refuses it until a driver shows usage observations. |

A profile only declares what its driver does. The profile honesty test
(`packages/mux/tests/test_profile_honesty.py`) calls each driver for every
capability its profile declares `native` or `emulated`, with no network, and
fails when the driver refuses it. It also checks that the profile's
`accounting_mode` names the usage grain its driver emits: `usage_grain` is
`model_request` for `request_spans` and `native_requests` (self-hosted) and
`turn` for `native_totals` and `native_root_totals`. A capability added to a
profile without a probe fails the test.

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
`openai.persistent_workspace` with `gpt-6-luna` or `gpt-6-astra`, or `gemini.inline_reuse`
with `gemini-3.8-flash`. Anything else is refused as
`backend_unsupported`, recorded as `admission_denied` in the turn outcome,
never run on Anthropic in its place. The refusal carries a `cause` for its
copy: `channel_pin` (the channel's own provider pin), `provider_not_allowed`
(the tenant's allowlist doesn't turn the provider on) or `agent_backend` (the
agent's provider can't serve it here yet), and the provider involved. Each
names the chat words that fix it; a refusal with no cause never blames the
channel.

A channel pin outranks every agent's own provider there, so an admin can clear
it from chat: "clear this channel's pin" calls `request_channel_pin_clear`,
which previews what this channel pins and, once the admin confirms the card,
writes a `channel_clear` revision (`clear_channel_backend`). Only the chat's own
channel can be cleared: the card binds the channel it is posted in (the
admitted turn's origin channel), never a channel named in the tool call, and it
re-checks the pin's revision and digest and that the clicker is a current
admin. Existing threads keep their model.

A channel may also name `gemini-4-argon` (Gemini 4 Argon, 1M-token context)
on `gemini.inline_reuse`, and an agent's config may pick it (below); the
default stays `gemini-3.8-flash`. An Argon turn makes one attempt: a 503 is the
turn's answer, never a fallback to the Flash chain. A fleet worker runs Argon
only on the bounded token envelope (as Flash does, at Argon's tariff, within its
budget and the per-turn hold); an Argon worker that cannot get one is refused at
spawn, never run on the 4096-token cap. Token limits a caller sets explicitly
get the same bound: a Gemini worker whose worst case (max input at the model's
input rate plus max output at its output rate) exceeds min(budget / markup,
hold) is refused at spawn, and again at plan admission against the row's hold.
Configuring a channel to Argon, and admitting its turns, both refuse whenever
`provider_prices` has no current Argon row (today: from 2026-12-01; see
[billing](billing.md)); the message names the date the price window ended.
Argon's rates are about 2.7x Flash's, so raise the runtime row's `hold_usd`
(and `cap_usd`) for an Argon channel rather than reusing a Flash row's values.

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

## Public import paths

Every public module declares `__all__`, and each public name is reachable
from one module. `packages/mux/tests/test_public_exports.py` checks that every
listed name exists and has no second path.

| Import from | For |
| --- | --- |
| `mux.contracts` | `Contract` and `FrozenMap`, the base types defined in the private `_base` module |
| `mux.contracts.<module>` | the contract types in the Layout table above, each from the module that defines it |
| `mux.errors` | the error taxonomy (the stable path for `mux.contracts.errors`) |
| `mux.state.<module>` | the state protocol and rules |
| `mux.profiles` | the declared profiles and their lookups |
| `mux.drivers` | `isolated_ssl_context` |
| `mux.drivers.anthropic`, `mux.drivers.openai`, `mux.drivers.gemini`, `mux.drivers.selfhosted` | the driver factory and the host-injection API: transports, credential and secret schemas, resource ports, payload and record helpers |

Import a driver's names from its package, not from a submodule such as
`mux.drivers.anthropic.transport`. The one public driver submodule is
`mux.drivers.selfhosted.wire`, the `daimon-harness/0` wire format shared by the
host, the harness and the conformance kit; it declares its own `__all__`. Submodules and `_`-prefixed modules are
implementation files and may move. One import-linter contract keeps Daimon's
own code to this rule. Five generic names stay on their submodules: openai
`KEY` and `reserved`, gemini `credential`, and selfhosted `segment` and `usage`. Two intentional second paths exist: `mux.errors` and
`mux.contracts.errors`, and `RequiredAction` in `mux.contracts.events` and
`mux.contracts.actions`.

## Dependencies and import rules

`daimon-mux` depends on `pydantic` and `httpx`. Each driver's SDK is an
extra: `daimon-mux[anthropic]` (`anthropic>=0.117`, the same pin as core),
`[openai]` (`openai>=2.54.0,<3`) and `[gemini]` (`google-genai>=2.7.0,<3`).

Import-linter contracts in the root `pyproject.toml` hold the boundaries:

| Contract | Forbids |
| --- | --- |
| Mux must not import daimon | `mux` → `daimon` |
| Mux contracts and profiles import no provider SDK or driver | `mux.contracts`, `mux.profiles`, `mux.errors`, `mux.state` → `anthropic`, `openai`, `google`, `mux.drivers` |
| Only mux.drivers.anthropic imports the anthropic SDK | `mux` → `anthropic`, except from `mux.drivers.anthropic` and `mux.testing.drivers.anthropic` (plus `mux.testing.isolated_sdk`) |
| Only mux.drivers.openai imports the openai SDK | `mux` → `openai`, except from `mux.drivers.openai` and `mux.testing.drivers.openai` (plus `mux.testing.isolated_sdk`) |
| Only mux.drivers.gemini imports the google.genai SDK | `mux` → `google`, except from `mux.drivers.gemini` and `mux.testing.drivers.gemini` (plus `mux.testing.isolated_sdk`) |
| Product code must not import test harnesses | `daimon.core`, `daimon.adapters` and every mux module except `mux.testing` → `mux.testing`, `daimon.testing` |
| Daimon product code imports provider drivers through their packages | `daimon.core`, `daimon.adapters` → submodules of `mux.drivers.anthropic`, `.openai`, `.gemini` and `mux.contracts._base`, with three named exceptions |

The tests run in CI as the `pytest-mux` job (`uv run pytest packages/mux`). The
`mux-standalone` job builds an environment with only `daimon-mux`, its provider
extras and the test dependencies, checks that `daimon` cannot be imported, and
runs the same tests there: `mux.testing` carries the test doubles, so the mux
tests need nothing from the Daimon host.

## Conformance

`mux.testing.conformance` provides an offline C01–C18 runner. Drivers explicitly
register a fresh fake-transport adapter per fixture; the existing pytest-mux
job collects the runner's regression tests. Results distinguish pass, fail
and pending with evidence. State-store and host seams that have not landed
remain pending and prevent certification. The in-memory reference driver is
a test oracle, not a backend. See
`packages/mux/docs/conformance/README.md` for adapter
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

`mux.testing.conformance.budget` reserves worst-case spend before provider work and
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

Explicit registration through `mux.testing.drivers.openai.conformance.register` runs the
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

The standalone `mux.testing.conformance.budget` guard reserves conservative spend before
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
See `packages/mux/docs/conformance/README.md` for the APIs and limitations.

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
Gemini probe preparation uses `python -m mux.testing.drivers.gemini.live_cert`, with an
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
`packages/mux/docs/conformance/README.md` for the adapter contract.
Responder identity uses the authorized row's tenant/account scope and the native
`anthropic.session_reads@1` extension. It retains the SDK response for configuration
snapshot backfill, including null channel and thread metadata, without an additional
read or a generic provider-binding projection.
The supplementary Gemini F1 adapter is explicitly selected from
`mux.testing.drivers.gemini.default_capability`. It returns typed ADAPTER_DEPENDENCY
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

A prepared turn that finds the lease held by another running turn on the same
thread waits for it, polling the store with a short backoff, rather than raising
`LeaseBusy`. The wait is bounded by what is left of the turn's ceiling, and a
turn that exhausts it ends through the ordinary ceiling outcome without retiring
the thread's session mapping. Stop ends the wait as an interrupt. The store
decides whether the lease is free, so a holder that dies frees the slot when its
TTL lapses. If the holder replaced the session while the turn waited, the turn
adopts that replacement through the same recovery that heals a dead session.
Callers that use `TurnPersistence` directly keep the immediate `LeaseBusy`.

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

Explicit caller-private `openai.persistent_workspace` channels run `gpt-6-luna` or `gpt-6-astra`
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

Discord selects that legacy display path from the registered profile's
`emits_legacy_events` declaration. Profiles without legacy events use neutral
drafts and typed action cards; the existing draft ordering, thread filtering,
requester controls and terminal callbacks remain unchanged.

Slack also consumes root neutral snapshots and ordered previews through the optional local tap. Authoritative snapshots replace draft fragments; terminal output comes from host state. Existing status-card replacement, Cancel routing, generic confirmation controls, and Anthropic pricing remain unchanged. An unverified alternate-provider cost is omitted.

Teams renders root neutral snapshots and ordered previews locally, retaining its debounce, unprompted silence and existing terminal card retirement. Host state owns final answers. Alternate-provider observations and missing observations bypass Anthropic display meters, so unknown cost is omitted; the native Anthropic path is unchanged.
### Alternate resource catalog

The host's `PostgresNeutralCatalog` retains OpenAI/Gemini agent revisions and checked skill bundles within an authorized tenant, account and provider workspace. Agent edits publish a new immutable native agent with a compare-and-swap host revision; historical revisions retain their native references and skill pins. The host revision does not change a provider's native revision. Exact-version bundles preserve their bytes, title and owning agent. Upload ownership is retained with each immutable version; callers commit a checked bundle before attachment.

This store requires additive migration `0089_neutral_catalog`. It supplies storage for explicitly selected alternate resource helpers; callers still perform policy rechecks, native creation and attachment. The default Anthropic resource path uses its existing storage and requests. Downgrading drops only the alternate catalog tables and their retained content. Retained versions store bounded skill archives in Postgres; storage grows with published versions. Account deletion cascades to both catalog tables.

### Scoped host resource helpers

For an explicitly provisioned OpenAI or Gemini catalog, `NeutralResourceHost` constructs
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
Gemini inline resource edits retain concrete UTF-8 versions and their digests.
Forks clone owned skill ids and add `code_execution`; SDK interactions mount the
retained files while existing sessions preserve their old definition and pins.
Binary inline sources and native agent deletion remain typed unsupported, and
failed cleanup keeps the catalog identity rather than publishing a false tombstone.
Runtime provisioning, remote MCP
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

`daimon.testing.adapters.discord.conformance.multi_human_evidence` is an explicitly
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

The explicitly imported `daimon.testing.adapters.slack.conformance` and
`daimon.testing.adapters.teams.conformance` probes use dedicated idle apps and normal
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
`packages/mux/docs/drivers/gemini/CONFORMANCE-SDK.md`.
Explicit channel backend configuration resolves runtime dependencies separately
for each admitted turn. Native preparation and execution use the same resolved
runtime and authorized scope; runtime lookup does not cache one channel's native
plan for another channel or configuration revision. Default Anthropic channels
do not construct provider runtimes. An explicitly selected provider whose
deployment constructor is absent is refused before provider transport access.

Inspecting the runtime mapping's size or profile names never constructs a
provider runtime. `PreparedTurn.runtime` is optional and defaults to `None`;
its private usage recorder remains outside the public prepared-turn fields.

`PreparedTurn.native_footer_cost` is an optional public field that defaults to
`None`. Explicit native footer-cost opt-in populates it with a fresh context
bound to the admitted OpenAI or Gemini session and turn attempt, which passes
through to billing and adapter rendering. Default and Anthropic turns leave it
unset.

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

`platform`, `channel_id` and `account_id` each additionally accept the
literal `"*"` as a deployment-wide default: a row with `"*"` on all three
authorizes every channel and account under its one `tenant_id` for its one
`profile`, so a single-tenant self-host operator can write one row instead
of one per channel. `tenant_id` and `profile` never accept `"*"` — a
manifest with either refuses at load, before any admission or credential
read. This is the one hard scope boundary the matcher preserves exactly: a
row's resources can never serve a tenant other than the one it names, no
matter how many other fields are wildcarded. Selection tries an exact match
on every field first (unchanged from the schema above); only when no row
matches exactly does it retry allowing `"*"` rows on those three fields.
In that retry a row wins when the fields it names exactly strictly include
every other matching row's, so a channel's own row with `"*"` accounts beats
a deployment-wide default. Rows naming different fields (one channel's row
and one account's row) refuse as ambiguous, as do equally specific rows.
An operator mixing one wildcard default with narrower rows gets the narrower
row for the channels it names and the default for everything else. A
narrower row whose `config_digest` or `configuration_grant` doesn't match the
channel's configuration doesn't match at all, so that channel falls back to
the default. `daimon openai
init` (see `docs/self-hosting.md`'s "Self-host on OpenAI") provisions the
agent and environment and writes this row; it defaults to the wildcard form.

Export the referenced key variable through the deployment's secret mechanism.
The manifest contains environment variable names, never credentials; unknown
fields, ambiguous matches, stale configuration digests and missing resources
refuse. `skill_ids` grants only explicitly named native skills used by the saved
agent. The selected plan pins real native resources to the authorized account and
project; admission's Anthropic agent/environment IDs are never reused for OpenAI.
An optional positive integer `spend_limit_usd_cents` is passed through the
provider's public session controls only when an operator supplies it for a
verified project. Omission uses the provider fix's unset control; its native
representation belongs to the OpenAI driver. The host runs the admitted model
(`gpt-6-luna` or `gpt-6-astra`) and prices it at that model's row, with small
containers and disabled delegation.

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
`packages/mux/docs/drivers/gemini/DEFAULT-CAPABILITY.md`.

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

Offline catalog sentinel clients and native OpenAI/Gemini replay clients use the
shared `mux.testing.isolated_sdk` factories. Ambient provider options,
including empty settings and options added before SDK copies, refuse before
HTTP client allocation without fetching their values. Auth, headers, routes,
TLS and retry budgets stay explicit; both Gemini HTTP clients close with their
replay. Recipes and native request/result matching are unchanged.

Offline platform-surface host tests construct their Anthropic SDK client through
`mux.testing.isolated_sdk.build_async_anthropic`, with explicit routing,
headers and retry settings. Ambient provider configuration is refused before
HTTP allocation, including empty environment values.

The offline catalog executor accepts `--anthropic-fixtures` to select the pinned
TARGET-53 Anthropic manifest. It uses the real mux turn and owned journal, checks
native session/model selection and usage folding, and captures text through the
selected platform adapter. The manifest covers all 53 targets, including explicit
unsupported recipes; it does not fall back to older authored replies. Missing
concurrency, remote-effect, asset, routing and administrative hooks remain
PENDING. Without this option the existing replay selection is unchanged.

Offline ordered catalog steps can consume an explicitly injected
`daimon.testing.admin_bindings.AdminBindings`. The runner preserves each original
operation and its product readback receipt. Unsupported admin operations remain
PENDING. A mutation does not bind a later synthetic host turn to the provisioned
tenant's configuration and policy; that missing context remains an explicit
barrier. Administrative receipts alone provide no scenario PASS credit.
### Alternate resource catalog

The host's `PostgresNeutralCatalog` retains OpenAI/Gemini agent revisions and checked skill bundles within an authorized tenant, account and provider workspace. Agent edits publish a new immutable native agent with a compare-and-swap host revision; historical revisions retain their native references and skill pins. The host revision does not change a provider's native revision. Exact-version bundles preserve their bytes, title and owning agent. Upload ownership is retained with each immutable version; callers commit a checked bundle before attachment.

This store requires additive migration `0079_neutral_catalog`. It supplies storage for explicitly selected alternate resource helpers; callers still perform policy rechecks, native creation and attachment. The default Anthropic resource path uses its existing storage and requests. Downgrading drops only the alternate catalog tables and their retained content. Retained versions store bounded skill archives in Postgres; storage grows with published versions. Account deletion cascades to both catalog tables.

### Scoped host resource helpers

For an explicitly provisioned OpenAI or Gemini catalog, `NeutralResourceHost` constructs
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
Gemini inline resource edits retain concrete UTF-8 versions and their digests.
Forks clone owned skill ids and add `code_execution`; SDK interactions mount the
retained files while existing sessions preserve their old definition and pins.
Binary inline sources and native agent deletion remain typed unsupported, and
failed cleanup keeps the catalog identity rather than publishing a false tombstone.
Runtime provisioning, remote MCP
authentication and live turns are separate gates. Unconfigured Anthropic paths
retain their existing requests, storage and policy.


The offline catalog executor accepts an explicit per-cell `ProductContext`
alongside its matching `AdminBindings`. It maps catalog destinations to owned
channels and threads, uses `configure_channel` and the actual database revision,
then calls real admission, `prepare_session_for_turn` with the product session
operations, and `run_prepared_turn`. Its journal and input acknowledgement come
from Postgres, with no memory-store substitution or manually adopted session.
N6's public resource edge supplies D1/D7 skill attachment/removal and readback.

Each Anthropic turn needs a separately source-pinned `ProductTurnRecipe` covering
admission, preparation and the native turn requests. The original native source,
codec, root, session, text, normalized events and usage remain pinned. A missing,
busy, deferred or refused preparation preserves `ADMIN_HOST_CONTEXT_UNBOUND`;
changing an admin policy alone provides no full-cell execution credit. The
current public preparation function does not dispatch provider-native sessions,
so OpenAI/Gemini opt-ins retain that gap before SDK or memory-store allocation.
Without the opt-in, existing catalog replay behavior is unchanged. These are
offline input/host proofs, with no live provider or model-behavior certification.

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
private to the process owner. For multiple hosts, select
`"storage_backend": "postgres"` and omit both file paths. This uses the existing
application state database and requires migration
`0106_gemini_runtime_state`; it creates no separate database URL or credential.
SQLite remains the default on hosts with durable local storage. Configured Gemini
runtimes refuse SQLite when Cloud Run's `K_SERVICE` or `CLOUD_RUN_JOB` marker is
present, before credential discovery. A PostgreSQL plan with SQLite paths refuses
rather than silently abandoning the original state.

PostgreSQL writers use the existing nonblocking fence retries and release pooled
connections between acquisition attempts. Pre-dispatch acquisition returns
retryable session busy after five seconds. Recording accepted results or definite
refusals after dispatch polls every 25–75ms without increasing backoff, so older
outcome writers retain the same polling cadence as fresh contenders. This is not
a FIFO guarantee. The enclosing host send timeout is 120 seconds by default,
including pre-dispatch work and POST time already spent; an earlier request
deadline also cancels acquisition. The storage's longer internal fallback does
not extend either host bound. Only the active record writer keeps a connection
during provider I/O; its account lineage locks are released beforehand. Records and holds commit
independently, and commits finish before cancellation propagates. Unchanged record
payloads are not rewritten. Account privacy purge previews and removes conversation
records; conservative financial holds are deliberately retained as financial
evidence and contain no conversation/artifact payload. A purge never releases a
hold: only settlement, a definite 4xx refusal or expiry do (below).

Known storage limitation: each namespace still stores one complete Records blob,
including artifact bytes, and the conservative hold map grows with attempts. There
is currently no payload-size bound or automatic hold compaction (holds settle or
expire, below, but released rows stay).
Operators must monitor payload bytes and database storage before increasing
workload or admitting large artifacts; this change does not authorize unlimited
storage, deleting unknown holds, or truncating records. Reconciliation and any
historical SQLite transfer remain explicit existing-owner operations. Do not switch
an active namespace to an empty PostgreSQL row and call it a migration.

Each Flash or Argon attempt first persists an explicit operator hold (stamped
`held_at`), then records its accepted/refused/unknown receipt. Holds are
estimates against the row's `cap_usd`, not actual spend, and leave the cap three
ways. A definite 4xx refusal releases at once (nothing to bill). An accepted
attempt settles when its interaction's terminal usage is recorded: only a
`final` observation (status completed, failed, cancelled, incomplete or
budget_exceeded, or requires_action, since a tool-result round continues as a new
interaction with its own hold) settles, never a running snapshot. Then `held_usd` becomes 0,
`settled_usd` keeps the hold and `actual_usd` the token cost at the model's
tariff (a later cumulative revision replaces `actual_usd` only, so nothing counts
twice). Anything else (a lost response, a 503 fallback, a crash before usage, an
unpriced alias) expires after `HOLD_TTL` (24 hours), logged as
`provider_hold_expired`. Expiry and stamps commit even when the attempt that
finds them is refused, so a full ledger ages out; a hold from before stamping
starts its clock when first seen. Released rows stay as evidence.

`cap_usd` therefore bounds the unsettled exposure in flight per (tenant, account,
project) ledger, not total spend: a settled or expired attempt no longer counts.
Total Gemini spend is bounded by the turn gates every backend passes (the
prepaid balance gate, the per-person monthly cap and channel budgets, plus a
fleet worker's run budget; see
[billing](billing.md#the-gates-in-order)) and, outside Daimon, by the Google Cloud project's own
billing budget or quota. Billing follows the usage outbox, never holds.
`scripts/gemini_hold_report.py` reports each ledger's outstanding holds from one
READ ONLY transaction; this runtime is not a live certificate or QA parity claim.

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


### Host conformance facts

Offline C14/C17/C18 probes can consume optional host fact hooks for backend
selection, wake ownership and committed outcomes. Missing hooks or `None` facts
remain PENDING. Installed facts must preserve existing-thread bindings, fence
stale wake owners and retain the authorized native session/account in outcome
rows; a callback verdict cannot substitute for these observations. C14 also
requires each prepared and persisted provider to agree with the declared provider
of its selected config profile, including after a revision change.


The shared C18 oracle consumes N4's `TerminationSnapshot` fields. Each case must
have a distinct observed outcome ID and exactly one committed row; every field
must be explicitly present. Actual and persisted reason, error class, native
session and recovery flags must match the independent case, and the committed
account, tenant and platform must retain the authorized scope. A pre-native
case may explicitly declare a null session; missing fields never count as null
observations. Missing hooks remain PENDING. Provider owners must declare and
audit complete reachable-reason coverage before clearing their adapter's typed
PENDING; shared oracle controls do not certify native or live execution.

C18 completed and session-terminated cases require a non-empty native
session retained in both execution and the committed outcome. At least one case
must prove native session retention; an all-null collection cannot certify C18.
A queued first-turn Stop may retain null for interrupted outcomes because slot
cancellation precedes native preparation. Explicit pre-native refusals such as
missing configuration may also retain null.


### C03 completion before a second new turn

The durable-send conformance probe keeps its same-key receipt and uncertain-send
checks, then waits for the first accepted root's authoritative saved terminal
and fresh idle session occupancy before submitting its second distinct input.
The barrier has a 60-second deadline, follows saved-history pagination, and
refuses before the second mutation when completion cannot be proved. Acceptance,
EOF, preview/child records and an idle-only snapshot do not release occupancy;
provider input-mode guards are unchanged.

The runner records the full GO digest as four colon-delimited hexadecimal groups
in its public receipt. Admission still compares the original canonical digest;
receipt reconstruction uses the same audit and writer, preserving secret refusal
and leaving settlement unchanged.

Every completion poll revalidates the accepted input's root across all saved
history pages. Conflicting roots or reassignment from an acknowledged or
previously resolved root refuse before the acknowledgement-loss fault or second
send, even when the original root has a terminal and the session is idle.


Offline catalog MCP prerequisites retain their original `mcp`/`deepwiki`
gap locations in evidence. The dispatcher accepts these scopes only with a
selected native recipe, explicit connection intent and source-matched MCP calls.
Authentication and server-count gaps block every host dispatch; unsupported
effects and external discovery block the affected turns. Authored tool packets
provide no remote-effect or full-scenario PASS credit. Unknown scopes refuse.



### Offline rejected-token credential observation

Discord's explicit offline `daimon.testing.discord_credential_conformance.rejected_token_submit` hook
executes the existing native credential modal against an owned loopback MCP
initialize rejection and reads the genuine form's scoped `used_at` predicate.
It records the actual before/probe-entry/after database values without changing
the product path or the frozen QA oracle. See the adapter's
`packages/adapters/discord/CREDENTIAL-CONFORMANCE.md`
for the accepted request-token binding and the shared database isolation guard.

### Offline Discord thread-permission observation

Discord's explicitly imported test support can bind an owned parent channel to
an offline `discord.Forbidden` thread-creation edge, invoke normal ingress and
capture the actual serialized parent notice plus native HTTP trace. Default
host naming stays enabled and its exact Messages request remains visible;
only that named host request is excluded from provider-turn writes. It checks


capture the actual serialized parent notice plus native HTTP trace. It checks
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
`packages/mux/docs/drivers/gemini/CONFORMANCE-HOOKS.md`
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



The offline catalog executor can explicitly inject a `RejectedTokenBinding` for
the Anthropic NEW27 cell through `rejected_token_runs`. It uses that hook's actual
owned form and loopback response, evaluates the original unused-row predicate,
and retains an administrative envelope without provider `TurnEvidence` or SDK
request credit. The default remains unbound. OpenAI and Gemini remain PENDING
under A3. The source mapping pin in `tests/judge/REJECTED-TOKEN-BINDING.md`
documents the exact signed hook, original source and approved token projection.

The offline QA catalog runner can construct fresh product surfaces per explicit
product profile in one job. Its default and opt-in boards keep independent
evidence and counts; missing owner surfaces remain pending. Product settings
receipts alone never prove native execution, delivery or spend.

The offline catalog CLI with `--http-apps` executes default and opt-in product
profiles in one job using fresh app settings, stores and observers. The public
`offline_http_apps(..., profile=None)` retains its original default settings;
an explicit profile applies the seven declared settings before the actual app
factories. Applied settings are configuration receipts, not source outcome
proof. Missing administration, reaction, scheduler or measured footer bindings
stay PENDING. Opt-in successes are reported as `PASS under opt-in profile` and
never increase default-profile PASS counts.

### Offline Anthropic C6 admin/readback edge

`daimon.testing.anthropic_admin` binds the frozen D14 fork and D5 fixture
operations to unchanged Anthropic CLI functions on an owned test tenant. Its
member probe executes the real MCP update/attach paths with the independently
selected native agent identity and a real local user account. Independent CLI
JSON and SDK revision reads preserve provider skill IDs and refuse interleaved
revision changes. After main #505 (`bfaffd2a`), both attempted member edits of an
unbound agent refuse with an admin handoff. Independent actual-SDK reads prove
the payload and revision are unchanged, and the captured trace has zero native
mutation POSTs. This is a bound refusal observation, not a whole-scenario PASS.
Host message/log assertions remain separate runner obligations.
Explicit fixture manifests and isolated SDK/database guards are required;
unsupported providers/shapes remain typed pending. No product behavior or native
create conversion changes.

### Native attached-skill deletion refusal

`NativeSkillDeleteGuard` is an explicit operator capability. It defaults to
`operator_enabled=False`; its tenant/account Scope and provider workspace must
be supplied by the already-authorized host. `delete_skill(..., native_guard=...)`
selects the native CLI function branch. Existing command wiring and the
Anthropic deletion path remain unchanged. An absent guard preserves that path;
a supplied disabled guard refuses before database or SDK access.

When enabled for OpenAI or Gemini, `refuse_native_skill_delete` resolves the
skill from the real scoped catalog and checks retained agent revisions plus
the tenant's channel-skill rows. Retained revisions matter because an existing
session can still use an earlier native agent. A stale expected revision,
missing history or a changed catalog/channel observation refuses without a
mutation. An attached skill raises `NativeSkillStillAttached` with the actual
reference identities and the CLI renders it as exit 1.

This capability authorizes inspection and refusal only. It issues no provider
DELETE. A stable unattached observation cannot fence a later attachment, so
unattached deletion remains `unattached_delete_unbound` until all attachment
writers participate in a catalog deletion fence. OpenAI's driver DELETE is not
invoked; Gemini's local skill-record deletion is not represented as a provider
resource deletion. Channel rows have no provider/workspace column: matching
tenant channel references are retained conservatively.

The offline `daimon.testing.skill_delete_bindings.SkillDeleteAdminBindings`
attaches to `AdminBindings.resource_edge`, optionally delegating other resource
operations. It accepts the literal `skills --guild <owned> delete <name> --yes`
shape and reports the native attached refusal with real service readback.
Anthropic returns only `ADMIN_ANTHROPIC_SKILL_DELETE_OBSERVATION_ONLY`, after
actual SDK reads and scoped channel observation; it never changes that
backend's deletion behavior. A bound refusal predicate is not a full NEW19
scenario pass: setup, host turn and teardown evidence remain separate.
### Gemini chat Add-skill capability

The operator may supply `McpRuntime.gemini_skill_capability` as a
`GeminiSkillCapability` from `daimon.adapters.mcp.tools._gemini_resources`.
The runtime default is `None`; the capability's own `enabled` default is
`False`. Enable it only with an A1 native-catalog resource factory and an exact
`GeminiSkillGrant` for the tenant, caller account, platform, channel, current
configuration digest and provider workspace. Only explicitly selected
`gemini.inline_reuse`, `gemini-3.8-flash`, per-caller channels use this route.
Grants for another caller or channel keep the existing path without adding a
configuration read. A selected stale, duplicate or wrong-workspace grant
refuses before the factory is called.

The returned host must match the complete authorized scope and enabled Gemini
catalog adapter before any catalog access. The existing selected-native-ID,
caller channel-isolation, pin and skill-change gates run before fetching or
previewing a source. Confirmation uses the unchanged pending-preview state
machine, bound to the native agent ID and content hash. The A1 host retains the
concrete bundle and creates a new independently verified native definition via
PostgreSQL CAS; older definitions remain available to existing sessions. The
same source handlers, preview/result schemas and confirmation text apply.

The shared matcher accepts a whole `yes`, `y`, `confirm` or `approve` after its
existing normalization. `yes please` still cancels the preview on every
provider. Consequently the frozen NEW18 scenario remains FAIL; the offline
canonical-yes workflow proof does not award a full chat/provider cell PASS.
No SDK, operator credential or environment discovery is added by this route.

The offline C14 regression in
`packages/core/tests/turn/test_c14_anthropic_to_openai.py` exercises normal
admission and preparation against owned Postgres with isolated SDK mock
transports. After an explicit channel switch, the existing shared Anthropic
thread retains its session and revision, while a new private thread runs
OpenAI. An unavailable retained backend produces the typed
`backend_unsupported` refusal before provider I/O, without changing either
binding. This does not certify live provider behavior or OpenAI shared threads.

Gemini's conformance round entry is an explicit testing surface, independent of
backend selection. Only C06 and C04 are selectable, with the primary model,
empty tools, one/three native interactions and separately pinned financial and
GO documents. Native terminal usage is fetched once before owned cleanup;
missing or decreasing evidence preserves held spend and the original workflow
verdict. The standard conformance registry and historical live certificates
remain unchanged. See `packages/mux/docs/drivers/gemini/LIVE-ROUND.md` for the
reviewed recipe, exact model-limit arithmetic and pending boundaries.


### Shared native credential authorization

Explicit native credential offers use `daimon.core.turn.native_credential_requests.NativeCredentialAdapter`
with the selected mux profile. The provider constructors retain their existing control
prefixes and disabled defaults. Both require an independently authorized resource scope,
a caller-bound per-caller session, an unchanged durable binding, and a catalog model
matching the retained channel revision. Registration does not consume or probe secrets.
The adapter rechecks the caller, policy, binding and catalog after native readback and
again after request registration before posting the shared product card.

Native MCP grant verification and resource host binding share these rules across the
selected providers. A resource host must match the caller's tenant, account, principal,
operator authorization and workspace, and its enabled native catalog must match its
profile and account scope. Channel skill routes use `check_backend` before allocating
that host. Unconfigured channels and disabled capabilities retain the existing route.
### CLI resource profile validation

The CLI validates an operator-provided resource host against the provider and
profile metadata declared by the mux profiles. A host must still match the
authorized tenant, account, principal, authorization and provider workspace.
Gemini catalog readback must also match the same declared profile and configured
adapter. Default routing, missing grants and mismatch refusals keep their
existing behavior; the CLI constructs no additional provider clients.

## Agent backend preferences

Admin backend preferences use the existing neutral-path flags: both
`turn.path=mux` and `turn.channel_backends=true` must be set. No preference
means the same Anthropic agent and model as before. An explicit channel
configuration takes priority over the agent preference, which takes priority
over that default. Clearing a channel override restores agent inheritance;
explicitly selecting Anthropic remains a channel override. Clearing an agent
preference restores inheritance; reverting
appends a revision with the previous value. Existing threads retain their
immutable configuration, rather than following a later preference change.

The tenant access policy's `enabled_backends` allowlist defaults to Anthropic.
Selections must be explicitly enabled before an admin can save them. The existing
policy store keeps this allowlist in a defaulted column on its existing record,
leaving the legacy policy JSON readable by older processes and after downgrade.
Compatibility
previews list unsupported attachments/capabilities, target thread-mode limits,
existing model list prices, and an explicitly unknown whole-turn maximum where
no complete bound is enforced. Preview digests bind the current agent facts,
policy, price line and preference revision; stale confirmations refuse.

Changes use a compare-and-set revision and record an existing security-audit
event in the same transaction. Preferences do not store credentials or copy
provider resources. Agent-derived effective configurations share the existing
immutable channel revision namespace but never become channel overrides.

Operator deployment manifests may keep their exact `config_digest` authorization
or supply a closed `configuration_grant` with explicit `profiles`, `models`,
`thread_modes`, and `requires`. Every tenant/account/channel constraint remains
mandatory; bounds are not authorization to substitute an unrelated native agent.
Agent-origin native preparation therefore refuses before manifest, credential
or native I/O until independently authorized resource-counterpart proof is
available. Channel-origin native deployments keep their existing behavior.

Disable the feature through the existing flags to stop new preference selection;
retain stored revisions and audit history. Destructive schema downgrade removes
agent-origin materializations and preference history and must not be used while
threads depend on those revisions. Default/channel-origin history survives. Before rolling back to an older image,
first disable `DAIMON_TURN__CHANNEL_BACKENDS` or set `DAIMON_TURN__PATH=legacy`,
then roll back the image, and only then optionally downgrade the schema. The
downgrade removes agent-origin rows but preserves channel-clear markers; older
readers interpret those markers as explicit default Anthropic overrides.

Backend resolution requires the host-resolved scoped agent identity; names are
only display snapshots. Recreated agents start without the deleted identity's
preference. Read-only selection uses the existing shared policy fence. Concurrent
materializations use the existing immutable insert conflict check and a bounded
retry, while operator writes retain the existing exclusive fence. A held writer
causes acquisition-only refusal/retry through the existing transaction owner;
readers do not bypass policy isolation. The generic StateStore latest-revision
API includes all materializations; explicit channel override reads use
`current_backend`, including immutable clear markers.

Slack and Teams read `Profile.emits_legacy_events` to retain the existing SDK
rendering path for Managed Agents and normalized drafts and action callbacks
for native profiles. Events and resource references carry only the provider;
`mux.profiles.emits_legacy_events_for` reads the registered profiles and refuses
missing or conflicting declarations. This routing capability does not admit a
backend or change channel configuration.

### Native counterpart readback

OpenAI and Gemini deployments may explicitly pin `source_agent_id` (the scoped
source UUID), `source_agent_version`, `counterpart_catalog_id`, and
`counterpart_catalog_revision`. All four pins are required together. They do not
replace the deployment's tenant, account, channel, project, or exact/bounded
configuration authorization. Names and caller-provided digests cannot establish
a counterpart.

One shared manifest selector, pin validator, and readback helper check the
admitted source identity/version and retained catalog revision before native
requests, then use the existing
resource host and driver to read the native agent. OpenAI also checks its
provisioned hosted environment; Gemini requires an explicitly enabled named-agent
catalog and checks the compiled native definition, explicit tools, system
instructions, and every concrete skill bundle file. Gemini counterpart rows
require a native `agent_id`; a display name does not identify a resource.
Unproven environment policy refuses. Missing, foreign, stale, or changed
resources refuse. A final catalog read detects
changes during native readback; it does not claim an atomic provider revision
pin. This component returns the existing retained catalog revision.

Agent-origin native execution remains refused, including when
`DAIMON_TURN__AGENT_BACKENDS=true`. The flag is default off and retains the
readback groundwork; it does not turn a catalog component into complete source
preservation authority. The existing admission and preparation origin guards
refuse before publishing an agent-origin effective revision, constructing a
native session, or dispatching native input.

An installed counterpart may have valid source identity, version, configuration
and catalog pins while omitting the source's instructions, tool-approval policy,
skills, MCP attachments, credentials, files, repository, memory or environment.
Complete preservation must include implicit resources added by the existing
session preparation owner, including persistent agent memory. Empty attachment
tables do not prove those obligations are absent.

Scoped catalog and native drift readback remains available as a component check.
Gemini's named-agent and OpenAI's provisioned-agent plans remain groundwork;
readback does not provide a native CAS guarantee or permit dropping source
obligations. Channel-origin deployments and the default Anthropic path retain
their existing behavior.

### Agent backend selection surfaces

With `DAIMON_TURN__PATH=mux` and `DAIMON_TURN__CHANNEL_BACKENDS=true`,
admins can select **Model provider** in an agent's setup Details on
Discord, Slack or Teams. Only the tenant's enabled backends are offered. Preview
shows unsupported attachments and capabilities, shared-thread restrictions,
and the shared service's list-price estimate or explicit unknown values. Confirm
checks the live admin role and rereads the agent; a changed revision, policy or
preview requires a fresh preview. **Use default** clears the agent preference.
Existing threads retain their backend, and explicit channel overrides still
have precedence.

An admin may also pick a model with the provider: `model` on
`request_agent_backend_switch` and `set_agent_backend`, and `--model` on the
CLI ("switch agent to gemini-4-argon"). Only the provider's selectable models
are accepted: Gemini's gemini-3.8-flash (the default) and gemini-4-argon;
OpenAI's gpt-6-luna (the default) and gpt-6-astra. Gemini's gemini-3.5-flash-lite stays a fleet-worker model:
the host runs it only token-capped. Naming the default stores no model,
as before. The model is part of the preview digest and the stored revision;
revert restores the previous model; the card names it. Switching the
deployment's default agent says it affects new conversations in every channel
on the server (workspace on Slack, team on Teams); a lost capability (memory
on Gemini and OpenAI) is listed before Confirm.

An agent also states its own **settings** on the same revision: its thread
mode (`shared` is the agent's intent to share threads), its required
capabilities and, rarely, a profile of its backend. Unset is the backend's
default. Turns that follow the agent's setting use them. A channel that pins a
backend stays an override and keeps its own thread mode, but it is
tighten-only for the agent: when the pinned profile lacks a capability the
agent requires, the turn is refused (`channel_pin_lacks_capability`) rather
than run without it. A provider switch keeps the settings that still apply
(a profile belongs to one backend); a provider that can't share threads
refuses a switch of an agent that shares them. In chat,
`request_agent_backend_switch` takes `thread_mode` and
`required_capabilities` (stated together; an omitted one returns to the
default) and the card shows them. Only an admin may turn sharing on; on the CLI, `daimon agents backend settings
NAME [--thread-mode MODE] [--profile PROFILE] [--require CAPABILITY ...]`.

The agent's owner may make every change in this section that an admin may,
except the tenant allowlist (see [Permissions](permissions.md#changing-agent-setup)).

The CLI exposes `daimon agents backend set NAME BACKEND [--model MODEL]` and
`daimon agents backend clear NAME`, using the existing tenant selector and
confirmation prompt. `--yes` skips that prompt; machine-readable `--json`
requires `--yes`. These commands use the same shared preview and transactional
revision/audit service as the panels.

The direct MCP API registers one additional admin tool, `set_agent_backend`,
only when both settings are enabled. Its required `backend` argument selects
an enabled backend; explicit `null` clears the preference. An optional
`expected_revision` refuses stale callers. Agent-scoped keys, external accounts
and chat-agent callers cannot use the direct write: chat changes go through
the separate admin-confirmation flow. Disabled settings leave the default tool
registry and setup controls unchanged.

Backend selection discovery is deferred until the CLI agent command group is
used. Unrelated help and version commands do not parse turn settings. Default
tenant policy JSON retains its legacy shape; an explicitly configured backend
allowlist is included in the output. The existing direct `set_agent_backend`
MCP tool accepts `preview_only=true` to return compatibility and cost information
with `applied=false`, without changing a setting. Chat credentials use the
existing confirmation flow. Admin Details omit unavailable backend controls,
and stale selections refuse without applying a change. Clear previews and their
labels derive from the shared default backend.

### Agent template rows

An OpenAI or Gemini agent-origin turn normally needs a manifest row pinned to the exact
source agent, its MA version and a pre-installed native counterpart. A
**template row** serves every agent of one tenant instead: `"root_materialization":
true`, with no `source_agent_id`, `source_agent_version`, `counterpart_catalog_id`,
`counterpart_catalog_revision` or `agent_id`, and usually `"*"` on platform,
channel and account. `tenant_id` and `profile` stay exact, and a
`configuration_grant` lists the models it serves (Flash and Argon for Gemini,
`gpt-6-luna` for OpenAI). An OpenAI persistent-workspace template still pins
its `environment_id`, which must be an active hosted environment.

On an agent's first turn after the switch is confirmed, source preparation
proves the drop consent, then the host publishes the confirmed target spec
into the tenant's catalog once per user (catalog entries are scoped to the
caller's account), at
`root:<agent>:<MA version>:<profile>:<model>:<target digest>`. Every later
turn for that agent version reuses it; editing the agent, changing the model
or a different derived target publishes a new entry. A concurrent first turn
loses the catalog compare-and-set and reads the winner's entry, and native
creation uses the catalog id as its key. Readback then verifies the entry like
an operator-installed counterpart. A template row carries no operator
capability inputs, so its turns run the agent's own instructions with the
confirmed drops (tools, skills and connections are declared drops for now).
An OpenAI template refuses a target with custom tools or MCP connections
before any native write: nothing would run them.

A template row may add `"template_capabilities": "mcp"` to keep the agent's
Daimon tools. Capture then represents the source's daimon-mcp toolset from
its live catalogue (tools that need a confirmation stay listed as
unavailable, as for any Gemini representation), and the agent's own tools and
custom skills become declared drops the switch card names. Each turn mints one
registered caller credential in memory when the request is first sent: it acts
as the requesting person's account with the agent's chat-execution grant
(never an admin or the bot; the server re-reads roles), lives at most
min(turn deadline + 60 s, 900 s), is checked against the consented catalogue,
and is revoked and read back when the turn ends, whether it answered, failed,
was cancelled or hit its deadline. A turn that outlives the credential gets
tool errors rather than a refreshed token; a credential left by a crashed host
expires on its own. If Gemini echoes the request's MCP header in an
interaction, the transport removes only that exact header and refuses any
other trace of the token (`mcp_header_echo`, `mcp_token_echo`). The MCP
service's runtime manifest needs the same row, since the switch preview there
captures the representation the consent pins.

With the same capability the agent keeps its memory. The Daimon catalogue's
memory tools (`memory_list`, `memory_read`, `memory_write`, `memory_delete`)
reach the stores an Anthropic turn would mount: `shared/<path>` is the
agent's store and `personal/<path>` the caller's own (only for a single-caller
turn). So memory is no longer a declared drop for such a template. The turn's
credential mint also records a `turn_memory_grants` row from the turn's
admission (whether a personal store applies, the read-only posture, the
channel and thread, the credential's expiry). The tools read it only by the
credential's jti, and refuse any credential without one. Every write and delete
re-checks the posture against the tenant policy as it is now, so a seal or the
DM memory policy switched on mid-turn takes effect at the next write. An
overwrite must name the content hash it read (`expected_content_sha256`);
writes are otherwise create-only, and a replacement whose create fails restores
the memory it replaced. One memory holds at most 100 kB (UTF-8), a store at
most 1,000 memories through these tools, and a listing returns at most 1,000.
A held turn (which natively mounts only its channel's store) and a fleet
worker get no grant, so no memory. A pinned row's credential carries no grant
yet, so its representation keeps the memory tools as declared losses.

Selection only offers a row to the turn origin it serves: an agent's turn
considers template and source-pinned rows; a channel's turn considers fixed
plans with neither. A tenant-wide channel row and a template row therefore
coexist without ambiguity.

### Worker template rows

A fleet worker row normally pins the source agent and its version
(`source_agent_id`, `source_agent_version`) and usually the root's channel, so
editing the agent or running it elsewhere refuses its workers. A **worker
template row** sets `"worker_materialization": true` with `"*"` (or no value)
for both source fields, and usually `"*"` on platform, channel and account. It
serves every granted agent of its one tenant, at the agent's live version, in
any channel: identity comes from the worker request's own admitted source
agent (the agent the worker runs as), never from the row. `tenant_id` and
`profile` stay exact, the configuration grant bounds the models, and the
per-request checks are unchanged (tenant, caller and session account, the
root's thread and run). A row pins both source fields or neither, and `"*"`
source pins are refused on any row that is not a worker row. A row pinned to a
source agent serves only that agent at that version: another agent's worker,
or the same agent after an edit, falls through to the template. A pinned row
still wins over the template in its channel. Selection partitions rows by turn
kind: a root turn considers root templates and pinned rows, a worker considers
worker rows and pinned rows, and a channel's turn considers only fixed plans.
So a root template and a worker template coexist in one tenant, and the
manifest hold a fleet worker is sized for follows the same rule. Workers still publish their per-run counterpart from the resolved
worker spec, as before.

### Output delivery pagination

Delivery follows every Files API page through the authorized output port, so
retained oversized entries cannot hide a newer deliverable file. Oversized
outputs remain listed; successful notices enter the bounded notice cache, and
the caller's explicit discard threshold applies only to smaller skipped files.
The port's `all_pages` option defaults to false for workspace-transfer and other
existing consumers. Delivery opts in; SDK cursors, MA headers and errors are
preserved across later pages, with authorization checked before the first call.

### Resource profile capabilities

Neutral resource hosts and catalog guards read catalog eligibility from the
existing mux profile registry. Profiles declare the execution tool, authored
or compiled tool readback, skill version digest and catalog verification mode.
Agent retirement requires the profile's declared deletion support. Missing
catalog support refuses at the same host/store authorization boundary; provider,
tenant, account and workspace identity checks remain mandatory. This changes
no default routes, operator grants or native request bodies.
Retained and disabled backend selection needs no fresh agent projection. Fresh
selection still requires the real scoped identity. Admission can reuse its
already-read active Managed Agents response through `load_backend_agent`;
the projection checks the same tenant/name metadata and avoids another native
list or retrieve. Unbound or archived responses refuse without additional I/O.

### Backend preferences and existing turns

Normal channel turn preparation publishes the first session's binding
through the existing state store before returning it to the caller. That binding pins
the provider and immutable effective revision; publication completes inside the
preparation owner’s existing deadline, and a delayed store write produces the
same ceiling refusal as a delayed session preparation. An existing thread keeps them
after a preference change or revert. Older caller-owned Anthropic session
mappings also retain the default when backend selection is first enabled.

A missing original profile, unavailable retained revision or provider removed
from the current tenant allowlist produces `backend_unsupported` before any
provider request. It does not redirect an existing conversation. Agent
preferences use the existing channel revision namespace with agent origin,
and remain excluded from explicit channel overrides. A native agent preference
that lacks a proven counterpart is refused before materialization, leaving no
channel revision for an older reader to mistake for an override. Every configured
admission requires a recognized origin alongside its revision; missing or
unknown origin refuses, and a retained shared revision carries its stored origin. One-step revert
uses the settings owner's existing revision and authorization checks and
affects new threads; it does not rewrite existing bindings.

The shared chat summary displays the admitted backend on explicitly configured
turns, using trusted profile metadata. Unconfigured turns and either disabled
flag retain their existing footer. These rules do not authorize a deployment,
staging write, live provider call or a provider unsupported on the selected
surface.

The settings, preview, selection and revert proofs establish their own
behavior; they do not prove an agent-origin native turn can run. With agent execution disabled,
runtime loaders and injected native runtimes retain the shared origin refusal.
Enabling the reserved execution flag does not bypass the source-preservation refusal above. New preferences also
resolve preferences by the UUID of the already authorized source agent before
session assembly. Matching names across catalogs cannot establish identity,
and a recreated namesake does not inherit the old preference. Channel clear
allows new threads to inherit that identity’s preference or the default;
retained threads keep their original revision.


### Installing provider runtimes

Provider deployment registrars install their existing driver, codec and preparation
callbacks through the channel runtime registry. The host selects the admitted profile's
`daimon.turn_runtimes` package entry point only when that profile is needed.
Registration constructs no SDK client and reads no credential. The default
Anthropic path does not discover optional packages. An absent or ambiguous
installation refuses the explicit profile instead of selecting another provider.

An application-supplied runtime constructor does not replace installation of the
host's preparation, backend or codec edge. The same selected registrar fills any
missing edge, while provider fallback registrations preserve every explicit
constructor, authorization reader and host callback. A missing required edge
refuses preparation before the supplied runtime constructor runs.

Resource consumers use the same registry's read-only deployment authorization
callback and retain their existing tenant, account, channel, digest and workspace
filters. The callback re-reads the operator's validated manifest; it constructs
no turn runtime. Anthropic and unconfigured channel copies return through their
existing path before this optional lookup.

### Replay fixture integrity

Offline replay checks recorded scenario inputs, native request and response
payloads, roots, sessions, terminal outcomes, usage, and current registered tool
schemas. Scenario, asset, native tape, schema and manifest hashes remain data
integrity checks. Implementation source hashes and commit IDs are historical
provenance; a source refactor does not require re-recording unchanged payloads.
Test turn dependencies use the real frozen `TurnDeps` bundle with explicit
replacements for the dependency under test.

### Confirm an agent backend change from chat

With the neutral turn path and backend selection enabled, a workspace admin or
the agent's owner can ask `@Daimon switch Researcher to openai`. The chat tool first returns the current
compatibility preview, including unsupported skills, MCPs and capabilities,
shared-thread limits and the cost line. The confirmation card shows those same
server-side facts. Only the requester may Confirm, and only while they are
still the agent's owner or an admin; permission is checked again on the click.

The card binds the current setting revision and complete preview digest. An
expired card, a changed agent or policy, or a newer setting requires a fresh
preview and confirmation. Backends outside the workspace allowlist are refused.
Existing threads retain their backend. For new threads, a channel override takes
priority over the agent setting; otherwise the agent setting applies.
When backend selection is off, the chat tool and its card are absent and existing
approval cards retain their behavior.

A switch to another profile (for example OpenAI or Gemini) can lose source
capabilities. The preview lists them as `declared_drops`, and the card shows
"Won't be available on <backend>: ..." with "Revert any time; nothing is
deleted." Both are computed from the server's own preview. The confirmed call
must carry exactly those categories as `accepted_drops`; missing, partial or
additional categories refuse, and a card whose drop list, digest or source
changed after it was drawn is refused on the click. Discord, Slack and Teams
cards read the source as the turn's own admitted caller and only in a public
channel or thread: DMs refuse (`source_context_unavailable`), as the MCP
tool's public-context gate does. "DM" is the canonical DM-shaped classifier
(Slack `D…` IMs, Teams `a:…` personal chats, Discord DM channels) -- a Slack
private channel or legacy `G…` group DM, or a Teams meeting thread, is not a
DM by that classifier and is not refused here.
Revert with `daimon agents backend revert`. A source carrying native tool
controls (such as its own `daimon-mcp` toolset) cannot yet be previewed on
another profile and refuses.

Chat confirmation currently requires an Anthropic Managed Agents conversation.
A native conversation without approved-card proof refuses the request. Saving a
preference does not provision a native counterpart: agent-origin native turns
remain refused until that separate authorization is available.
Backend-switch confirmation is included in a turn's approval posture only when
the admitted profile supports tool confirmation. Unsupported or unknown support
keeps the existing required-action termination path without sending an
unsupported confirmation event.


### Fleet worker slots

A fleet worker (`daimon.core.fleet`) is one more private binding slot in the
state store: its thread is `fleet/<run id>/<ordinal>` under the orchestrator's
channel, owned by the orchestrator's account, so it never touches a shared slot.
Its binding, fenced lease, idempotent spawn operation (`fleet:<run>:spawn:<n>`),
journal and usage are the store's existing ones. The worker's result is one
journal entry of its own, with an id and source key derived from the worker
(`fleet:<worker>:result`), so it never repeats an event the turn already
journaled and a lease successor appending it again is a duplicate.
### Worker permission derivation

Backend loaders and worker derivation use the same skill and MCP requirement mapping. Worker skills retain their root provider restriction; catalog previews retain their portable skill capability check. SDK MCP sources keep their conservative vault requirement because their records do not expose credential intent.

The core resource owner exposes one worker spec resolver over existing
`AgentSpec` and sparse `AgentPatch` shapes. Host-supplied current root facts bind
the real tenant, account, channel, thread and origin, and retain the existing
host-loaded `Place`, including category and unresolved-category facts; the same
facts are retained in the derived grant and reused by every agent decision.
The place must match the root thread and channel. Hosted network omission is
an unknown provider default and refuses until the driver supplies a proved,
normalized network ceiling. Spawning uses the existing
writer, invoker and agent policy on the fleet surface, with no private-admin
exemption. Worker depth is one; workers cannot spawn more workers.

The resolver preserves the protected root prompt and derives finite model,
backend, exact skill version, tool schema, MCP endpoint and credential, resource,
network, budget, deadline and turn-count subsets. A clone requires an authorized
source reference and independently loaded current revision. Opaque native
extensions, effort options and environment knobs refuse when their effect has
no proven attenuation mapping. Neutral per-tool confirmation also refuses until
its compiler maps that control. MCP attenuation requires one resolver-bound
server and the profile-declared finite tool policy keys; a native compiler that
drops those keys cannot admit the connection. An implicit execution tool still counts toward
authority when omitted from the requested tool list.

Worker skills and MCP use the same provider and capability predicate as the
backend compatibility preview. Native skill pins stay on their proved source
provider; equal names or IDs do not prove cross-provider portability. Unknown
or unsupported selected-profile capabilities refuse before derivation returns.
Credential-backed MCP also requires the preview's declared vault capability;
a native compiler alone does not establish that profile contract.

Clone metadata uses patch semantics: omitted keys stay, an empty patch changes
nothing, and null values remove individual source-only keys. An explicit null
map clears metadata only when the root has no stamps. All root metadata is an
immutable ceiling for fresh specs and clones: removing or changing a root stamp,
or adding an ungranted stamp, refuses. The clone source is never mutated.

This is an admission primitive, not a launch operation. The host must
preflight the resolved configuration through the existing native compiler
before allocation; known generic controls can still be unsupported natively.
It must reload current authority at preparation and resume, retain the original ceiling, apply
the derived grants at every resource/tool edge, and reserve funds with the worker
transition in one transaction. Audit uses the existing scoped append-only store
and contains identities and typed outcomes rather than prompts or credentials.
A missing or erased actor cannot authorize launch. Existing turn paths do not
call the resolver and remain unchanged.
### Worker completion wakes

Queue readers filter reasons against their own closed reason vocabulary in SQL,
before selecting a thread batch or decoding rows. Unknown reasons cannot hide
known timers or acquire a lease through an older compatible reader. The same
filter covers direct reads, mutations and expired-claim cleanup; known rows
retain strict validation of every other field. These readers retain the existing
readable worker-completion groundwork while completion publication stays closed.
Operators must establish this compatible reader release as the deployed and
permitted rollback floor before installing any producer activation.

Legacy request-based completion writes remain closed. An authorized host can
freeze an exact worker batch with `arm_worker_finished` in its own transaction.
The existing continuation row holds the canonical batch, frozen quorum and
original root binding/session; unready waits are excluded from normal scans.
`enqueue_worker_finished` passes frozen members and quorum to `fleet.run_settled`
and offers each ready wait once, in the terminal writer's transaction. Initial
and review batches have separate deterministic keys; reviews await every member.
The run stays running so it can admit cross-review. Deadline refusal preserves
the worker's terminal write. Generic completion enqueue helpers remain refused.
No caller is installed; run-end cancellation and original-root dispatch remain required.

The prior release aborts an entire queue scan on the new reason, hiding
unrelated legacy timers. Before enabling completion writes, deploy a separate
compatible-reader release whose closed queue readers ignore unsupported
reasons, and prove every reader and permitted rollback version is at least
that release. Installation of frozen-wait publication remains deferred
until that boundary is established. Rows must never be relabeled as timers or
handoffs. Reader tests can inject future rows directly into an isolated test
schema; those fixtures are not successful host production or fleet execution.

Bind `worker_finished_decision` to `decide_worker_finished` with the host factory.
It re-reads frozen batches, original roots and live turns. Missing hooks use the
unavailable backoff; stale facts defer. Normal runner checks remain required, without a legacy agent
lookup or human-history supersession rule. The existing claim/start/settle
ladder fences the turn before its first effect, so an expired started wake is
interrupted rather than automatically replayed. Native execution and result
collection still require their independently authorized host integrations.

Migration `0094_worker_finished_wakes`, after `0093_fleet_runs`, widens only
the existing reason check. Its downgrade removes worker-completion rows that
older applications cannot interpret before restoring the previous reasons.


## Fleet orchestration tools

The opt-in fleet registration exposes `fleet_spawn`, `fleet_status`, `fleet_kill`,
`fleet_resume`, `fleet_cross_review` and `fleet_collect`. Registration requires
fleet enablement plus the mux turn path and channel-backend configuration. It is
disabled by default. An authenticated live-root loader and the host fleet owner
ports must be installed; absent bindings refuse before worker effects. A chat
agent ID, run handle, role or matching agent name does not supply those grants.
Every root and session decision reuses the host-loaded complete `Place`, including
category and unresolved-category facts, against current tenant policy.
Kill, resume and collect additionally require the original orchestrator principal
or a trusted tenant admin; an agent key cannot supply the admin exemption. The
loader retains the run's original scope principal independently of the current
interaction's subject. The shared service owns this check for every adapter;
open channel policy alone does not grant control of another person's run.
Role refusals use the existing request audit as `authz:controller_required`
denials, rather than tool errors; outside an audit capture the recorder is a no-op.

Workers reuse `AgentSpec`, `BackendConfig` and a pinned `Agent` revision with
sparse `AgentPatch` overrides. The single worker resolver derives their tools,
models, resources, safety prompt and limits from the root. Arbitrary caller
`SessionSpec` resources are currently refused until scoped worker preparation is
bound. Resume rechecks current permissions and keeps the original budget, turn
and deadline ceilings; it cannot reopen a terminal worker or reset a hold.

Cross-review attaches the target's verified journal event through the same spawn
path, with a backend different from the primary's. Admission refuses a same-backend
reviewer, and collection rechecks retained review rows before reporting. The host
pins a closed review contract: JSON containing `verdict`
(`approved` or `refused`) and the exact `reviewed_digest`. A primary's stored
result contract declares a required review with `review_required: true`; omitted
or false does not require one, and any non-boolean value refuses. Reviewers use
the same admission path, capacity slots, budgets and deadlines as primaries and
count within the run's worker limit. Three primaries and one required review
therefore use four workers. Collection waits for unfinished workers and blocks on failed
workers, refused reviews, stale digests or invalid output contracts. A wake or
quorum is scheduling evidence, never output approval. `ready` returns validated
primary outputs for the orchestrator's summary; card excerpts and review prose
are not used as results.

The shared `FleetOrchestrator` serves both MCP and authenticated platform
controls. Store, budget, driver and wake bindings own atomic admission, audit,
reservation, operation replay, scoped binding reads and frozen wait generations.
The tool service adds no database tables, native client or permission resolver.
Missing bindings stay unavailable; offline dependency stubs do not certify
native worker execution. Existing self-only agent-chat tools continue to refuse
inside chat turns.
### Source intent before native execution

Backend selection retains the authorized source definition and concrete revision, including instructions, tools, skills and native constraints. The existing session owner can project caller-specific tool policy and resource intent using metadata-only store reads. Projection never decrypts credentials, uploads files, mints tokens or provisions native resources. It retains the actual admission scope; an unknown native workspace is unbound rather than assigned an invented routing identity.

Persistent memory is required even when no binding exists: the ordinary session composer lazily provisions it. The projection records required provisioning, access and guidance without creating a store. Source environment defaults, repository access, credentials and inherited restrictions also remain obligations. The projection carries captured facts; the preparation boundary alone derives the outstanding preservation obligations. Projection and materialization share the same repository, PAT, caller-vault and Copilot selectors, including the app-mode Copilot exclusion. The projection receives the actual credential-key availability without decrypting anything; metadata reads share the PAT resolver’s overlay-only selector. Caller JWT vault capability and fresh app-vault intent are explicit owned references. A fresh app vault remains opaque until the existing authorization/materializer proves its remote access; a memory or sandbox drop does not remove that obligation. Source capture and counterpart identity readback do not establish preservation. All unproved target mappings remain typed refusals at the existing preparation boundary; default and channel-origin turn dispatch remain unchanged.

The shared preparation seam reports closed obligation labels for instructions,
tools and approval, skills, MCP, credentials, files, repository, memory and
environment under the existing `backend_unsupported` refusal. It captures the
actual caller's effective seals, read-only memory and publishing restrictions
before checking an agent-origin revision. A native catalog component remains
readback evidence; it cannot replace the complete source and resource proof.
No installed source-preservation positive is claimed by this capture path.

Obligation labels are derived only at the shared preparation boundary from the actual captured definition and resource intent. An empty source baseline still requires implicit memory and an unproved environment; unrelated instruction, tool, attachment and credential labels are not pre-filled. With agent backend capture enabled, existing balance, cap and channel-budget refusals win before source projection and publish no backend revision.

### Declared worker restrictions

Worker specs accept one default-empty `declared_drops` tuple: `memory`, `sandbox`, `skills`, `mcp`, `tools`, `files`, or `repository`. These declarations apply only to authorized fleet derivation; ordinary agent backend changes retain their source-preservation refusals. Unknown/duplicate declarations refuse. Scope, source revision, current policy, budget, protected guidance, seals and readonly ceilings still apply. Inherited declarations cannot be cleared.

A sandbox declaration removes its environment and execution builtins and prevents implicit execution injection. Tools declarations remove tools and remote MCP connections; MCP declarations also remove their toolsets. Other declarations remove the corresponding owned mounts or attachments. The validated spec, digest and derived permissions retain the declarations; worker guidance describes relevant restrictions. Call the existing audit helper with `worker=resolved_spec` to record the closed labels in its existing reason field and verify root provenance. Pure attenuation is not native execution authority: the registered preparer must consume the exact restricted plan and independently refuse any implicit tool/resource it cannot remove.

The existing provider preparation request can carry the canonical resolved worker beside its actual source and scoped counterpart. The worker is hidden from representation and absent by default. Carrying a resolved spec alone is not an execution grant: the shared source proof and registered native plan must independently verify and consume the same restrictions before a worker can run.

OpenAI hosted worker plans require an explicit persistent-memory drop, an authorized Bash grant, known hosted network/package policy, explicit empty template inputs and no inherited resource mounts. Readback compares the exact resolved agent and environment, including protected instructions, attachments and metadata stamps. Residual template files, opaque environment configuration and unbound MCP credentials refuse. A no-tools or no-sandbox declaration cannot silently retain hosted Bash. An explicitly selected `openai.conversation_only` worker instead requires all five losses (`sandbox`, `tools`, `mcp`, `memory`, `skills`) and no remaining tools, skills, connections, environments or resource mounts. Its deployment must omit `environment_id` and skill identities. Creation includes the task's initial input under the same fenced worker input claim used by turn sends. Acknowledged creation is replayed without another input; an uncertain create retains its reservation and requires reconciliation. Read-only memory posture remains recorded and can execute only because the worker has no memory mounts. Ordinary persistent-workspace channel plans and their seal, read-only and publishing restrictions retain their behavior.

Gemini's deployment owner checks the exact resolved worker instructions, tools,
skill pins and native catalog readback before its preparer consumes the checked
native identity. A Gemini worker must declare persistent memory dropped. A
sandbox drop uses an explicitly authored remote environment without sources
and removes execution tools; it does not remove the provider-hosted runtime.
Retained session resources, unsupported environment policies and custom host
functions refuse. Seals, shared ownership, publishing restrictions and transfer
remain admission constraints; dropping memory leaves the admission's readonly
ceiling intact. Ordinary agent switches without a resolved worker remain
refused.

The shared worker preparation proof captures the authorized source through the
existing session resource composer. A canonical resolved worker can discharge
only its explicitly declared attachment, memory and sandbox losses; protected
instructions, publication policy, credentials and opaque private resources are
still obligations. The proof checks root scope, source identity/version, derived
permissions and deadline, reads the complete native counterpart, then re-reads
resource intent to reject drift before handing that exact verified snapshot to
preparation. Gemini preparation consumes that snapshot without another native
read. OpenAI and Gemini allocation share the held-owner validation and guarded
catalog publication; provider environment and deployment checks remain scoped.
Existing agent switches without a resolved worker remain refused. Configured
caller MCP credentials also remain obligations, so these workers refuse in a
host configuration requiring them until a credential materializer exists.

Native readback does not grant fleet execution authority: the headless caller
must use the canonical resolver output and obtain fresh fleet/budget admission
before native execution. Ordinary OpenAI legacy-source environment preservation
remains refused. All switches remain default off; the legacy and channel paths
keep their existing behavior.

Restricted worker preparation uses the earlier of the caller's turn deadline and
the canonical worker deadline. A shorter caller bound is valid. The same bound
travels with the preparation request through source capture, counterpart readback,
source recapture and native preparation. Expiry is checked before successful proof
and prepared-turn handoffs; exhausted authority uses the existing ceiling refusal.
The headless caller must pass that bound to the existing turn runner as well.

Worker preparation can use the existing `TurnPersistence` on an explicitly
authorized private `Slot` before any binding exists. `mutate(session=None, ...)`
uses the same lease, durable operation and native single-attempt context; no
placeholder session or binding is created. Allocation callers supply a fresh
financial guard and set `retry_failed=False`. An acknowledged native resource is
restored from its real receipt; an unknown or refused attempt never creates again.
The result seam selects only complete, authoritative journal messages accepted by
the worker driver and calls the existing fleet result writer under that lease.

Source facts are captured through the same preparation request for preservation
proof and worker resource composition. Capture does not grant execution or
allocation; the existing proof and materializer validate the captured identity
and obligations. Worker preparation carries its derived scope through the real
binding and turn codec. Expiry after preparation or its binding callback invokes
the existing orphan cleanup before refusing the handoff.

Prepared native scope is checked by the existing admitted-session scope owner.
Binding publication, turn I/O and worker handoff use that single identity rule.
The worker allocation prototype is deferred with its actual caller and complete
source/native preservation proof; it is not an exported preparation path.

Worker normalization accepts an optional `captured_source` on the existing
`resolve_worker_spec`. Only the scoped loader and read-only resource projector
supply this input. Root authorization, finite role-selected models and backend
grants remain the ceiling; names and stored digests grant no authority. Captured
instructions and represented tools, skills, MCP, environment and resource intent
must be retained unless explicitly narrowed through declared drops. Accepted native
namespaces flow through preparation's shared source-obligation classifier; represented
tool intent uses the existing profile projection and closed provider compiler.
Opaque native extensions, model options, unretained memory (including existing
memory-store bindings) and credential resources refuse with
`WorkerSpecRefused` before allocation rather than being silently discarded.
The canonical digest includes the complete owner capture (including hidden source
and resource-generation facts), so stale stored intent must be refused rather
than rewritten. Inherited seals and read-only restrictions remain in force.
This normalization does not prove native execution; the existing preparation
owner still classifies obligations and independently recaptures source and native
readback before input. Omitting the new argument preserves the existing path.

Normalization and preparation use one exact source-instruction prefix rule. The
source prefix is retained ahead of the root and role text; a shared leading root
paragraph appears once. The default path without a capture retains its earlier
composition bytes. A successful normalization proves representability and root
authorization; native execution still requires independent preparation readback.


The headless `run_worker` entry accepts an already-claimed pool worker and its
original root authority. It reloads current policy and captures the actual source
before resolving and comparing stored intent. Supported pre-provisioned targets
use the registered preparer and their real journal store; unsupported allocation
paths refuse. The caller owns pool release and the existing run-level budget watch.

The first primary input joins the current bound budget guard, worker turn-count
CAS and journal send claim in one transaction. A lost acknowledgment consumes at
most that one claimed input and retains the hold for reconciliation; recovery
cannot create another session. Successful results come from accepted authoritative
journal messages through the existing fleet result writer. Ordinary headless and
legacy turns keep their existing behavior.

A worker dispatch exception does not mark its input rejected, refund its reserve
or release the caller's pool slot. The caller releases its slot in `finally` and
uses the existing journal operation readback before classifying a possibly-sent
input. A count returned inside the claim transaction is never adopted as durable
worker state; terminal updates reread the worker through the budget lock owner.

The existing `TurnRuntime` can expose an optional `materialize_worker(request)`
callback returning the existing `AgentRevision` acknowledgement. It is absent by
default. Read-only counterpart proof does not invoke it. The headless worker
passes its existing fresh financial guard as `before_native`; preparation checks
the full scope, shared store and private slot before invoking materialization.
The existing publisher owns the lease, single-attempt mutation and acknowledgment.
Counterpart readback verifies that actual publication through the catalog owner,
then preparation recaptures the source and checks the worker deadline before
handoff. Missing authority, unknown outcomes and mismatched readback refuse.

Source environment capture projects a closed cloud policy only when networking
and every package-manager list are explicit. Limited networking must explicitly
disable implicit MCP-server and package-manager access. Apt, pip and npm map to
the neutral system, Python and npm lists; other managers must be explicitly
empty. Unknown fields, missing defaults and self-hosted policy retain their
native obligation and refuse when unrepresented. The source's native snapshot,
identity metadata and visibility remain intact. Ordinary Anthropic environment
requests retain their native configuration bytes. A projected environment is
still subject to the root's sandbox grant and independent native readback.

Worker clone normalization retains the actual authorized source reference and
revision through the existing resolver. A changed source or missing source grant
refuses before allocation; stored worker intent and digests are never rewritten.

Worker cleanup uses the verified prepared resource ports through the existing
bounded archive owner. Unsupported archive remains a logged limitation and
preserves the original dispatch failure, input count and reconciliation hold.

A clone retains the native source reference's tenant-wide service identity. The
root must explicitly grant that exact source and revision, and the owner capture
must bind the complete source snapshot to the root's authorized account scope.
An arbitrary foreign or unbound account reference cannot use this path; the
source identity is never relabeled as the worker's account.

An OpenAI deployment can explicitly enable `worker_materialization` (default
`false`) with exact source identity/revision pins and finite configuration bounds.
Dynamic plans omit fixed native agent and catalog target pins. The actual worker
uses the existing scoped publisher and session mutation owner with one attempt;
unknown acknowledgments retain their operation for reconciliation. The verified
native target is consumed directly without changing the deployment manifest.
Complete message journal identities match the stable accepted provider item ID;
the true native stream event ID remains in provenance. Unconfigured and fixed
deployments retain their existing behavior.

A Gemini deployment can explicitly enable `worker_materialization` (default
`false`) under its existing finite source/configuration/tool bounds. Fresh and
cloned targets use the same scoped publisher, owned creation acknowledgment and
independent catalog readback before preparation. Unsupported retained memory,
private resources and approval requirements refuse; tools remain explicit.
The worker input and authoritative result reference the acknowledged native
agent, and an unknown creation outcome is retained without another allocation.

Fleet host composition uses the existing store and budget admission owners and
reads full results from each worker's actual private binding and journal. Card
excerpts cannot substitute for result contracts or mandatory review evidence.
Text/JSON contracts are checked before funding. A retry dispatches a committed
worker while it is pending; the existing pool claim prevents another execution.
Refused admissions remain typed refusals when replayed.
An explicitly installed MCP bridge requires the live turn's exact
`origin_context_id`; stale, foreign or mismatched contexts refuse before the
root loader runs. Native dispatch, control and frozen-batch wake bindings must
be installed by the host; an unavailable binding does not authorize work.

Gemini fleet workers use an explicit managed-agent token budget of 4096 per
interaction. A worker can select the supported cost-oriented
`gemini-3.5-flash-lite` model only through its canonical spec, backend revision,
root model grant and independently verified native definition; execution never
rewrites a retained model identity. The existing journal and input claim permit
one worker interaction, including after restart; continuation requires another
interaction and is refused. Non-worker runtime selection keeps its existing
primary model.

The Gemini managed-agent token budget is best-effort and excludes cached tokens.
It is not a hard spending ceiling. Worker runtime admission refuses a deadline
more than 120 seconds away. Worker execution retains its admitted deadline,
cancellation and financial guard; complete measured usage is required for exact
settlement.
The session creator accepts complete scoped worker preparation through its existing
host entry. A root-granted warm memory binding must still match at the memory
owner's lookup; missing or changed bindings refuse without provisioning.
The creation operation retains the acknowledgement before independent readback.
A lost acknowledgement keeps an unknown outcome and cannot allocate again;
interrupted proof restores a known receipt and resumes readback. Rejected sessions
are archived through the bounded orphan owner only after independent identity and
tenant/account ownership proof. Foreign or unproved sessions are never archived.
Ordinary session calls keep their existing request and lazy-memory behavior.

Explicitly selected Anthropic fleet workers can allocate a fresh native agent
under the existing private-slot lease and claimed operation. The captured source,
compiled agent readback and verified warm session must remain exact before the
binding is published and input is sent. The root must grant the original scoped
warm memory resource; readonly narrowing is retained. Cold memory and
unrepresented credential, file, repository or application resources refuse.
Ordinary turns and deployments without explicit backend selection retain their
existing path.
Frozen fleet root permissions are built from trusted finite host grants, the
original admission and the scoped source/resource capture. The existing worker
resolver checks policy, profiles, limits and source obligations; the root keeps
its complete admitted place, subject and exact source revision pins. Missing,
foreign, stale or unsupported facts refuse. The caller must first verify the
exact active origin, native binding and card intent association. This projection
does not derive authority from balance, pricing, mutable agent names or tool
requests, and does not reserve funds or activate a run.


Fleet roots are projected only when fleet orchestration is enabled and the actual admitted agent has a current DB fleet grant. The captured-source constructor supplies finite authority through the existing `WorkerPermissions` contract; installed `TurnDeps.fleet_root_grants` loaders are refused when a grant exists. Tool bodies, cards, tenant balances and current model prices cannot supply authority. Session preparation retains its scoped source capture privately. Before native input, the real turn joins its genuine card intent, active origin, original requester and accepted private binding under the existing lease, then uses `freeze_root_permissions` and the existing fleet/operation owners to retain one immutable root. Turns without a root intent, including ordinary turns with the loader installed, keep their normal input path. Missing prerequisites refuse; unconfigured turns perform no new capture or persistence.

`read_frozen_root` in `daimon.core.turn.runtime_manifest` takes the original scope and exactly one selector: the genuine card `intent_id` or verified native `origin_id`. A captured `run_id`, when provided, must match. Cards and authenticated tools consume the same record and fresh policy; recovered controls must provide their captured run. The lookup preserves the original scope principal and finite bounds, checks the current origin, intent, requester, binding and run, and refuses stale or foreign associations. Fresh read authorization cannot enlarge or replace the frozen authority. This seam does not install an operator grant policy or certify a live deployment.

Before creating a root run, the existing operation owner durably reserves its exact origin and intent. The intent reservation checks prior associations across all accounts in the tenant under the card row lock; public readers remain account-scoped. The origin key and immutable intent association remain authoritative while delivery is pending, claimed, unknown or processed; neither a competing intent nor a missing receipt can allocate a second run or root budget. Only a processed receipt is readable as a frozen root. Stored receipts are decoded through the existing persistence owner in JSON mode, preserving strict money and scope fields.



Fleet controls read the prepared root through the existing frozen-root projection.
The MCP service can use the real turn dependencies and verified active origin;
handles do not supply authority. Controller admission resolves the caller's
platform identity to the original account. A trusted tenant administrator may
control an already-authorized root.
Scoped, versioned stop acknowledgements and their retries use the existing
operation store. They preserve worker bindings, usage and held reservations;
a stop acknowledgement does not certify native termination. Resume without
accepted-attempt reconciliation refuses explicitly. Completion waits freeze an
exact initial cohort, then separate review cohorts for each reviewed target,
in the existing continuation queue. Each wait reloads the original frozen root
through the turn dependencies; a shared ports instance never holds a fixed wake
address. A successful worker
result publishes that cohort's wake in the same transaction. The deployment must
install the compatible completion reader and the normal runner's fresh root
checks. Native dispatch and its budget watchdog remain separate installation
prerequisites; absent hooks keep spawn unavailable. A run-wide stop does not
publish an armed completion wait.
Discord installs the completion decision in its existing guarded continuation
caller only when fleet is enabled. The normal admission and responder checks
still apply. The same root validator fences card/preparation entry, the prepared
session/account and the runner's input; that input hook disables replacement
recovery. Other wake reasons and fleet-disabled calls keep their existing path.
Original-run tool authority, collection certification, run-end cleanup and the
deployed compatible-reader/rollback floor require their separate owner proofs.

QA cleanup evidence uses `daimon.testing.admin_resources.ResourceManifestEntry`
and `freeze_resource_manifest` to retain exact scope/run/binding/native references
and owner creation receipts. `ownership` and `eligible_actions` are derived;
borrowed resources have no destructive eligibility, and unknown creations retain
no guessed native identity. The signed action list is only a ceiling, never a
production authorization. Original admitted input receipts bind cancellation.

`verify_cancel_readback` performs the existing driver's independent stop readback
and retains its actual observation. `verify_archive_readback` performs the scoped
session-admin GET, verifies identity/account/agent/environment and an aware archive
timestamp, and never sets a native-stop result. Unknown, missing or unsupported
readbacks remain pending/retained. These read-only QA functions do not cancel,
archive, delete, release holds, authorize live work or award a scenario point.
N9 must supply the owner-captured records and independently pinned operation IDs;
creation receipt provenance and current mutation authorization remain with their
existing owners. Borrowed memory bindings remain unchanged; the helper does not
certify DB binding preservation or deletion, which need their real owner readbacks.

Ordinary tool-free Gemini channel turns can opt into a native output-token limit
through the existing operator runtime manifest. Set `max_output_tokens` to a
strict integer from 1 to 65,536;4096 is a small example. This requires
`environment: {"name": "no-execution", "execution": "none"}` and `tools: []`.
The existing driver compiles a model interaction with
`generation_config.max_output_tokens`, retains the same scoped journal and
conversation history, and preserves the limit on subsequent turns and 503
fallbacks. No sandbox environment is created or reused in this configuration.

Managed tools, skills, MCP, mounts and native agent definitions are incompatible
with this ordinary-turn configuration and refuse before provider access.
Omitting the field preserves existing managed-agent turns and worker option-A
budgets. The native model response limit is distinct from the managed-agent
`max_total_tokens` budget, which is best-effort and does not enforce an output
ceiling. No input-token or final-cost bound is implied.

A terminal fleet run transition retires its addressed completion wakes in the
same transaction. Pending/claimed rows become cancelled and their lease metadata
is cleared; delivered/skipped records remain unchanged. Stale claim writes
cannot revive scheduling. This does not interrupt native work or release
money. Other runs, roots and wake reasons retain their scheduling.
Fleet completion turns carry the actual claimed and started durable wake row to
the normal prepared-turn runner. They retain a locator in the existing root
operation, referencing the exact original processed root and run rather than
creating another run or reloading grants. Authenticated tools verify the current
live origin; cards use their genuine current intent. Both resolve the same
one-hop association and recheck original scope, source, native binding, account,
policy and finite deadline. Unknown receipts and conflicting claims refuse.

Opted-in original root origins and mapped card intents are retained at exit
only while their exact run is active and unexpired. Original origin expiry is
capped to its existing expiry and the original deadline. Wake origins keep their
normal execution lifetime. The frozen-root reader rechecks durable run state on
every resolution: only running or collecting runs retain execution authority.
Completing, failing or cancelling a run after its original turn exits invalidates
both original and completion-locator readers even if bounded origin/card records
remain. Removing those audit records at run end is a separate lifecycle owner
unit. Ordinary turns and disabled lifecycle calls retain their existing behavior.

Fleet dispatch obtains each original preparation request through a trusted owner
callable keyed by the verified root. The request's existing source capture and
worker derivation validate the original subject, place, scope, pinned source and
bounded deadline before resolving a worker revision. The shared ports use the
process worker pool to claim the exact admitted worker, then call the existing
headless worker driver. Native cancellation events are monitored by the existing
budget watch, shared across review cohorts, and tasks join the turn shutdown
registry. Unknown outcomes retain their existing reservations. The store refuses to claim
pending workers with a retained stop intent, and the same budget producer
withdraws admission so the existing watch cancels bound workers. Neither action
releases native outcome reservations. Dispatch remains unavailable without its
explicit owner dependencies.
Preparation-owner loading shares the original root/worker deadline and shutdown
registry. Known refusals during preparation derivation or the headless driver's
initial policy, funding and backend checks end only the same pre-native claim
through its existing locked transition owner, retaining reservations and
publishing eligible frozen completion wakes. Cleanup allows a concurrent stop
intent and refuses changed attempts, bindings or native phases. Once allocation
starts, native-unknown outcomes stay with the existing reconciliation owner.
The minimum deployed rollback reader is `>= #963`: the completion-aware reader
with closed SQL reason predicates and fair callback-unavailable deferral.
It recognizes `worker_finished`; it does not ignore that
reason. Without an installed completion callback, dispatch releases the row
without provider input, history fallback or a notice, refunds the claim attempt
and advances availability. That backoff lets later threads reach the next
bounded poll batch. Readers that decode unsupported reasons before selection
are below this floor; no legacy-vocabulary backport is implied.

Before removing completion-wake schema support, stop fleet publication and
dispatch, drain native work, and retire pending/claimed scheduling through the
existing owners. Read back zero `worker_finished` rows in `pending` or `claimed`
before starting restore. The reason-check downgrade takes a bounded table lock
and refuses while any such row remains; refusal rolls back the restore step.
After retirement, its reason-only deletion and legacy-check restoration can
succeed. Removing queue rows is not cancellation of native work or a refund of
usage; those confirmations remain independent requirements.

Per-agent fleet grants are finite admin settings in the existing immutable
`agent_backend_revision` lineage. The nullable grant retains exact paired
backend/profile and model references, worker count, per-run worker authority budget,
expiry and explicit run/worker/input ceilings. The current admin must confirm a
fresh preview and setting revision; backend and grant edits share one policy-
fenced revision insert and security audit transaction. An epoch is the local revision that granted or amended authority.
Backend set, clear and revert carry the current grant unchanged; grant-only
appends preserve the backend undo pointer. Revoke appends no grant, and re-grant
uses a new epoch. A backend revert never restores a historical grant.

Grant reads do not infer funding, prices, source fidelity or a native final-bill
cap. Missing or expired grants supply no fleet authority; malformed grant data
is logged without its body and refused by the fleet reader while ordinary
backend reads remain usable. A backend edit after an invalid grant appends no
grant, retiring that authority without rewriting the malformed history. Old
rows have no grant. During a mixed-image
rollout, an old neutral writer appends no grant and therefore revokes rather
than broadens authority; re-granting cannot reuse its former epoch. Dropping the
grant column removes all grants. Runtime construction and admin surfaces are
separate installation steps; this persistence API alone enables no fleet task.
The financial grant ceiling covers workers only. The ordinary orchestrator root
retains its normal billing and input path; whole-root financial and input
controls must be installed separately before a fleet live retry.
Operator policy mutations can verify Discord authority through `core.native_admins.verify_backend_actor`. It reads the registered guild, current owner/roles/member and existing account binding with bounded requests; unknown or revoked authority refuses. Verification never creates an account, promotes a stored role or saves an authorization token. The mutation owner still checks its policy revision and writes its audit atomically.

### Operator tenant backend policy

`daimon tenants backend-policy` snapshots and changes the backend allowlist
through the existing policy service. Every invocation verifies the named
Discord member's current Manage Server, Administrator or guild-owner authority
using the configured Discord bot token, then resolves that person's existing
tenant account. There is no admin override or saved authorization. Other
platforms and missing, malformed or unavailable current authority refuse.

```bash
daimon tenants backend-policy snapshot discord GUILD --actor-user USER --output before.json
daimon tenants backend-policy apply discord GUILD --actor-user USER --snapshot before.json --backends anthropic,openai,gemini --dry-run
daimon tenants backend-policy apply discord GUILD --actor-user USER --snapshot before.json --backends anthropic,openai,gemini --yes
daimon tenants backend-policy revert discord GUILD --actor-user USER --snapshot before.json --expected-revision APPLIED_REVISION --yes
```

For writes, the existing backend-selection flags must enable the service.
The CLI refreshes the admin attestation after confirmation and uses an explicit
compare-and-set revision. Apply uses the original snapshot revision; revert
requires the revision returned by the successful apply. Both return JSON
receipts with the committed owner result and an independent readback. A stale
revision refuses, including a concurrent change to any other policy field.
Revert restores only the allowlist and preserves the remaining policy.
Mutations append the service's scoped security audit in the same transaction.

Snapshot files contain only tenant/workspace identifiers, the backend allowlist
and policy revision. Creation is exclusive: an existing snapshot is never
overwritten. Dry-run performs current admin, policy and CAS validation without
policy or audit writes; snapshot dry-run also creates no file. Current authority
is required again for rollback. These commands grant no fleet permissions and
do not make provider calls, allocate native resources or change default chat
behavior.
The continuation store exposes a read-only tenant/run-scoped completion audit
reader. It requires an actual owned run, returns all statuses in creation/ID
order, and matches the literal canonical run prefix before decoding. Malformed
owned bodies and mismatched roots remain visible for exporter refusal;
canonical cohort/key/address validation and snapshot coherence remain exporter
checks. Reads project persisted columns without flushing or overwriting caller
objects; effective dirty or deleted state for its source tables is refused by
the shared store reader. Reading this audit never claims or runs a wake.

## Captured root permission composition

`neutral_resources.fleet_root_permissions` derives the existing `WorkerPermissions`
from an owner-captured `BackendAgent`, its actual `AdmissionGrant`, a current scoped
`AgentFleetGrant`, and the caller's joined `FleetFacts`. The caller rechecks the grant
epoch and expiry around awaited reads and validates the original origin/binding.
The constructor preserves the exact admitted subject/place/category, source revision,
environment, seals, and paired backend/model ceilings, including explicit model pins.
It delegates normalization and source-obligation checking to `freeze_root_permissions`.

The common permission deadline is conservatively bounded by grant expiry and the
smaller of the finite run/worker durations and the existing turn ceiling.
Worker budget and request ceilings come
from the grant's existing `WorkerLimits`; funding remains a separate owner decision.
The caller retains worker counts, grant epoch, and root-input controls through the
existing run/context. This constructor does not impose a whole-root financial cap.

Only a captured, already-bound warm memory store can become a mount. It uses the
existing memory mount codec and exact caller resource scope, with the captured access
mode; there is no provisioning. The complete source remains available to the existing
obligation classifier after represented native custom tools are projected. Credentials,
unsupported MCP/native controls, missing memory, repositories, and opaque requirements
remain typed refusals. No obligations or declared drops are inferred or filtered.
This API alone does not install runtime fleet execution or prove main defaults portable.


An enabled fleet freezes worker authority from the actual admitted agent's
current per-agent grant. A missing grant leaves ordinary root input unchanged.
Revocation or replacement invalidates retained roots by their pinned epoch and
grant digest. The frozen worker deadline is the minimum of the original request
bound, grant expiry and finite permission duration; reconstruction cannot extend
that bound. Worker budget, model choices and request ceilings come only from the current
DB grant through the captured-source permission constructor. An installed
permission loader is refused before allocation when a DB grant exists; loader
narrowing is not supported. The worker request reader joins the exact original run, origin,
account, binding, source revision and current policy; it refuses missing or
legacy preparation facts and source or environment drift. It retains neutral
facts, never SDK clients, transports, credential values or accounting callbacks.

The grant's budget ceiling covers workers only. The ordinary root's requests
are separate; fleet installation does not establish a whole-root provider bill
cap. Root financial controls require their own installation before a bounded
root-and-workers run.


### Fleet runtime composition and draining

With `DAIMON_FLEET__ENABLED=true`, the ordinary Discord `build_runtime(settings)`
and MCP `create_mcp_app` factories construct the existing fleet orchestrator,
worker pool and scoped host ports. They use the process's real sessions, client,
markup and turn dependencies. No permission loader needs to be injected: root
preparation uses the current per-agent DB grant. The factories own only what
they construct, and close fleet work before draining and disposing their existing
clients and database pools. With the flag off they construct no fleet owner,
pool, state store or tariff lookup.

The existing runtime registry can supply cold native tariff callbacks without
constructing a provider client or inspecting credentials or deployment files.
The host pricing context retains canonical managed-agent rates and every supplied
native rate. Actual attenuated worker backend/model pairs resolve through the
pricing owner before financial admission; a new pair does not discard rates for
prior workers. A missing native tariff remains missing and is refused by financial
admission. Registering a callback is not proof of a complete provider bill, funding
or execution authority. Native deployment suppliers must supply matching dated
request-tier rates; this composition does not invent them.

An ordinary initial card read may return no fleet only after the shared reader
verifies the current admitted source/session, original origin/account/thread,
current persisted binding and policy, genuinely absent per-agent grant, and no
root reservation in any state. A foreign account's reservation on the exact
intent refuses rather than appearing absent. Ordinary sealed and final responses
then retain their existing delivery path.

Revocation blocks new inputs and allocation. Scoped Stop, status, wait and
collection for an already-running root continue under its original frozen epoch;
they do not refresh authority or permit new worker inputs. Native preparation,
spawn and resume stay strict, and completed/cancelled/failed roots remain refused.
Grant ceilings cover workers only. Whole-root financial/input controls are a
separate prerequisite before any live fleet execution.
Per-agent fleet grants can carry `native_root_budget_usd` and
`root_cash_budget_usd` together through the existing audited preference service.
Both are optional; omitting them preserves the previous grant encoding. Native
budget values use the same strict, finite, positive cent precision as worker
native budgets. Cash values must be strict, finite and positive. The pair is
separate from `limits`, which continue to govern workers and retain their
single-input native dispatch restriction.

The session compilers accept an explicit root native budget: Anthropic compiles
USD cents into its native session budget, while OpenAI compiles cents into
`spend_control.limit`. Explicit compilation requires exact acknowledgement and
owned-session readback; a missing or mismatched budget refuses. OpenAI native
spend-control acceptance remains a live-provider gap: prior attempts were
rejected, so offline request-shape proofs do not certify provider acceptance.
No ordinary root selects these new grant values yet. Root funding, distinct-send
counting and attributed settlement are separate prerequisites for activation.
The normal request continues to omit the optional budget.

With fleet enabled and an applicable per-agent grant, Discord establishes the
real card intent and scoped origin before binding and retains the same origin
through execution. Preparation failure, cancellation and completion release
that context once. With fleet disabled or no applicable grant, the caller keeps
its previous preparation arguments and origin timing.


Failed controlled native session allocation archives a returned session only
when its native readback proves the expected tenant, requester, agent and
environment. That verified session uses the existing bounded single-attempt
orphan cleanup, including after cancellation; an unverified readback or unknown
allocation acknowledgement does not authorize a native archive. The ordinary
uncontrolled app-session archive remains the mutually exclusive fallback.
App-access revocation and cleanup of the session's owned app vault continue
independently. Cleanup does not erase the allocator's original acknowledgement
or authorize release of an unknown financial hold.

Discord orchestration callers may omit the origin stack. The existing owner then
manages its lifetime internally; constructing the stack makes no database or
provider change. Admission and the queue gate still complete before entering
the opt-in fleet origin or binding its session.

### Source-loss consent persistence

Agent backend revisions can retain a closed source-loss consent body containing
the canonical declared drops, the complete source pin and the target-spec digest.
Existing revisions have no consent and serialize as before. Consent-bearing
effective configuration rows retain the original logical agent UUID and local
revision; identical backend/model selections with different consent do not share
an effective revision. Retained reads resolve that exact original association,
and refuse foreign, malformed or missing originals. This storage alone enables
no ordinary cross-backend execution.

The migration adds two nullable JSONB columns without changing existing data.
Downgrade removes consent and its associations; subsequent source-loss execution
requires fresh confirmation rather than recovering consent from another revision.

Retained consent and its original association are read from persisted columns.
Cached ORM values cannot hide cleared or malformed consent. Effective dirty or
deleted proof sources refuse without flushing, refreshing or overwriting caller
state; pending new objects remain unflushed. Ordinary revision reads retain their
existing ORM behavior unless the authoritative persisted mode is selected.

The backend service may receive a trusted complete source-capture callback. Its
preview derives losses through the existing worker source-preservation rule and
includes the exact losses, source pin and target digest in the confirmation
digest. A switch must accept exactly that tuple. Switching from Anthropic to
another provider declares `model_controls` when the source's valid effort or
speed controls cannot be represented. Identical same-provider native model
controls are preserved. Unknown or invalid controls still refuse as
`source_definition`, even with `model_controls` consent. DD3 cards, CLI previews
and MCP results show this loss through the same canonical declared-drop tuple.
A credential paired to an MCP server is lost with an explicit `mcp` declaration
only when that server is absent from the target. The session composer's scoped
caller-MCP credential is paired to its injected MCP server even if the native
agent's MCP list is empty. Credentials for retained servers, unpaired credentials,
instructions, unknown obligations and publishing approval cannot be discarded. Missing source
or publishing context refuses. Clear removes consent; revert rechecks the original
consent against a fresh capture, and fleet-grant changes carry the current consent.

Turn preparation reads the original accepted revision through the effective
configuration association, never through the latest preference. It freshly reads
and projects the source, derives the target again, and supplies that canonical
target to the existing lazy runtime factory before construction. Native readback
still must prove the counterpart matches, followed by another fresh source read.
Source drift requires confirmation again. Provider and adapter owners supply their
runtime and trusted capture implementations separately; no-consent execution
retains its existing refusal and all feature defaults remain unchanged.

Cross-backend source consent refuses unclassified native agent and environment fields, including unknown skill, MCP, tool and environment-config policy fields. Channel skills pass through the same native-source validation as agent skills before neutral projection; declaring a skills loss cannot discard unknown channel-skill fields or values. Original native definitions are validated before requester filtering, including required discriminator presence, supported discriminator values and environment visibility scope. Hidden MCP attachments cannot erase unknown or invalid control fields. These remain nondroppable source-definition obligations even when all supported attachment losses are explicitly confirmed. Schema-supported omission and null values retain their existing meanings.


Ordinary Gemini agent-origin turns can use a plain chat counterpart after the
existing backend-switch flow records the exact declared losses on the original
immutable revision. Preparation rederives that target from fresh source facts,
checks the installed source/catalog/configuration pins and native definition,
and carries the verified named-agent runtime into execution. Memory must be
explicitly dropped; tools, skills and MCP are empty, and the target uses an
explicit remote environment without sources. Credentials, publishing approval,
unknown source controls, shared ownership, seals and transfer remain refused.
The source instructions and logical name are retained; operator deployment text
cannot replace them. No worker is fabricated, and ordinary chat does not inherit
worker token controls. Missing consent, source drift or incomplete native proof
refuses preparation. The existing explicit backend-selection flags remain
required; unconfigured channels and the worker option-A limits are unchanged.
### Preview and accept backend losses from the CLI

With model backend selection enabled, `daimon agents backend set Research openai
--dry-run` prints the server-derived preview without confirming or changing a
setting. `--json --dry-run` needs no `--yes`. Applying a preview that declares
losses requires `--accept-drops` with exactly its comma-separated categories;
`--yes` only skips the interactive question. Unknown, duplicate, incomplete or
additional categories refuse. Order and delimiter whitespace use the existing
consent domain's normalization. Prices remain list-price previews.

The preview reads the actual configured agent and environment and the current
account, publishing, memory and resource policy. It never provisions defaults,
resolves credential values or starts a provider session. Missing or foreign
context, an unrelated configuration target, credentials, private resources and
unknown source obligations remain refusals. The explicit feature-off path stays
cold. A setting applies to new threads; existing threads retain their backend.

`daimon agents backend revert Research` previews the actual preceding retained
setting and applies the existing revision compare-and-set. Source drift requires
confirmation again; revert does not restore historical fleet grants. `--dry-run`
on revert is read-only. Clearing still uses the existing explicit clear operation.

Gemini fleet workers can explicitly request both `max_input_tokens` and
`max_output_tokens` in their existing `WorkerLimits`. This requires
`max_turns=1`, `gemini-3.8-flash`, no tools, skills, MCP or mounted resources,
and explicit memory/sandbox drops. The worker retains counterpart verification
and uses a model interaction for execution. The complete prompt, including
system instructions, is counted before dispatch; missing counts or excess input
refuse. Cached input remains inside the ceiling, and output includes thinking.
The existing journal permits one accepted or uncertain interaction across the
worker lifetime; subsequent turns refuse, including after restart. This mode
has no fallback to another model. Worker limits remain monotonic under the
existing permission resolver. Other profiles and uncapped Gemini turns retain
their existing behavior.

### Explicit source capability representations for Gemini

An agent-origin Gemini plan may supply `source_capabilities` in the existing
provider runtime manifest. This is separate from accepting portable losses:
preview and preparation read the original immutable skill versions and compare
**every retained file byte**, then map supported automatic filesystem operations
to the profile execution tool. Coarse skills, tools or MCP losses cannot be
accepted in this mode. A confirmation-gated MCP tool can be excluded only with
the explicit per-tool loss consent described below; its sibling tools remain
available. Unknown native controls, publishing approval, foreign credentials,
non-automatic filesystem tools and incomplete source inventories still refuse.
Memory and the original sandbox remain explicit losses; no existing workspace
is copied.

`source_capabilities` contains the complete `source_pin`, target `model`,
`mcp_tools` source catalogue and one `skills` entry per source skill. Each entry provides
concrete `source` and `target` SkillRefs plus an absolute retained ZIP `archive`.
The target version and digest name the canonical full-byte SkillUpload. Provider
archive names retain their original spelling, including older underscore names.
Derived representation evidence is excluded from the source pin, so existing
consent pins and deployments without this configuration keep their meaning.

The plan also supplies `skill_ids`, `skill_deployments` and an absolute
`mcp_credential_file`. Provision target skills through the existing Skills port
using `RetainedSkillDeployments`; each receipt pins tenant, account, project,
version, credential-free immutable GCS directory and the entire path/SHA-256
manifest. The provisioner must upload and independently read back every object
before supplying a receipt. The adapter performs no GCS upload or credential
discovery. Missing or changed deployment/file receipts refuse.

Publish the counterpart through the existing NeutralResourceHost and record its
native ACK/readback before invoking the credential owner. Persistent native
agent definitions contain the MCP URL and allowlist, never bearer headers.
`mcp_credential_file` must be an owned regular 0600 file with a short-lived
registered chat-execution caller token for the original canonical agent
(`{sub, chat_agent_id, jti, exp}`). The after-ACK owner must select
`AgentMCPAuthority.chat_execution=True`; its default remains the restricted
external agent-key surface, which full mode refuses. The runtime checks
signature, exact claims, account/tenant/agent, registry expiry and revocation
before each provider exposure, after independent native counterpart verification.
It does not mint or store a bearer in a catalog or runtime manifest.

This mode requires agent-origin preparation and all existing source/catalog/
configuration pins; channel plans and workers cannot activate it. Unconfigured
turns remain cold and the explicit Gemini worker token envelope is unchanged.

A representation row applies only to its exact agent-origin platform, channel,
account, source version and backend profile. Verified public backend previews
explicitly use their existing channel context before a backend revision exists;
the preview owner considers the evidence only for the requested target. Ordinary
headless captures do not select this preview context. DMs, routines without that binding,
workers and other profiles keep their original source capture. Invalid or stale
representation evidence is discarded; the full deployment independently refuses
without it. Capture never substitutes a reduced target after that refusal.

The profile's execution mapping requires all six automatic filesystem capabilities
(`bash`, `read`, `edit`, `grep`, `glob`, `write`). A read-only subset, missing
capability or permission prompt cannot acquire general `code_execution` authority.
When the source also enables `web_fetch` or `web_search` (including native
enabled defaults), the same profile mapping retains them as `url_context` and
`google_search`. They are never silently omitted to fit a token-only quote;
provider web-tool fees need their own finance terms before a live attempt.
The native per-toolset schema represents MCP only with an explicitly verified
catalogue and fully resolved automatic enablement; legacy and worker projections
still exclude native-only MCP toolsets. Capture authenticates to the configured
server using a registered short-lived chat-execution credential from the existing
MCP mint/registry owner, reads `tools/list`, and revokes that preview credential
in a finally block before returning. This retains the ordinary source caller
visibility, including search collapse. Runtime uses the same registered authority
mode, account and canonical agent after its native ACK. An enabled automatic
`daimon-mcp` entry must actually exist in the source; a credential binding alone
cannot synthesize tool authority. The operator allowlist and any source connection
allowlist must equal the complete actual server catalogue. A source restricted to
`describe_agent` and `list_my_sessions` cannot be claimed equivalent to the ordinary
chat catalogue (`search_tools`/`call_tool` and any directly exposed approval tools);
it refuses full mode rather than dropping those capabilities. Before
provider exposure the registered target credential must see the same catalogue.
Unknown names, silent narrowing, unresolved policies and unexpected toolsets
therefore cannot count as preserved capabilities.

Full plans additionally require an absolute `gcs_credential_file`, read using the
same owned regular 0600/no-follow credential reader as MCP. It supplies an explicit
source OAuth access token in an exact JSON object with `access_token` and
timezone-aware RFC3339 `expires_at`. Its expiry must cover the admitted turn plus
30 seconds and be no more than one hour from the current check; a raw token,
unknown field, missing file or expired/insufficient lifetime refuses. The owner
rotates the protected file explicitly; there is no ambient credential discovery
or source-token minting. Full plans also require explicit `gcs_mount_bucket` and
`gcs_mount_prefix`; every verified deployment must lie inside that exact boundary.
The prefix must equal the complete component-wise common directory of the
verified deployment sources; an operator cannot widen it to an ancestor prefix.
The private transport uses `google.auth.downscoped.Credentials` to exchange the
source token at Google's STS for a token with exactly one Credential Access
Boundary rule: `inRole:roles/storage.objectViewer` on that bucket, conditioned on
object names and `storage.googleapis.com/objectListPrefix` starting with the
approved nonempty prefix. No permission is available on other buckets or prefixes,
even if the source worker has broader roles. The source bearer never goes to the
sandbox. Exchange failure, unsupported scope, an unchanged/missing result, or
insufficient returned lifetime refuses; there is no unscoped fallback. STS uses
one explicit TLS-verified POST with no ambient proxy, redirect, ADC or retry.
On each
native counterpart read and each provider credential exposure, the runtime lists
every configured GCS prefix, rejects missing/extra/duplicate objects, requires
locked object retention through the admitted deadline plus the seven-day inline
environment lifetime, and downloads each current generation to check SHA-256.
Receipt metadata alone is insufficient. The storage read path never writes or
modifies retention. Provisioners must grant creation only to the explicit
provisioner during upload, then revoke that grant and all other effective create
permissions under published prefixes before final inventory verification. Retain
read-back bucket/project/inherited IAM evidence that the provisioner and runtime
principals cannot append objects through cleanup. Locked retention prevents
replacement/deletion, not new objects. A readback is a point-in-time observation,
not a transactional directory mount.

Private mounts use only the exchanged downscoped token in Gemini's
managed environment egress proxy, scoped to the exact `storage.googleapis.com`
domain through `environment.network.allowlist[].transform.Authorization`. The
existing bound session credential seam refreshes and revalidates both MCP and
GCS credentials at each SDK dispatch, including fallback attempts. It binds the
exact admitted GCS sources and caller scope; unsupported or modified network
rules refuse. Credentials exist only in transient, repr-redacted dispatch memory,
never in retained native definitions or host session requests. Follow-up turns
refresh the same environment using `type=remote`, its actual `environment_id`,
and the fresh network rule, without remounting or resetting files. Ordinary
turns without the full-source resolver retain their existing request shape.
The credential's actual identity needs only bucket-scoped read authority; a
Gemini-project service agent is not required for this explicit OAuth mechanism.
Storage-owner IAM evidence must establish the principal and read-only scope;
local source-token expiry metadata is not an IAM attestation. CAB restricts the
exchanged authority regardless of those broader source permissions; the actual
exchange and access boundary must still be checked in the authorized preflight.
The one-domain allowlist blocks all other sandbox egress, including pip installs
and external URL fetches. This is an explicit sandbox restriction: the canonical
default-capability scenario only reads its skill, performs local file operations
and bash printf, and invokes provider-side MCP; it needs no other sandbox egress.
Other skill workflows must not assume unrestricted networking. Verify MCP and
local builtin operations on the exact image.

For protected interactions the SDK edge inspects the raw create/GET JSON before
SDK parsing/serialization. Any `network.transform` presence refuses with a fixed
error, preventing serializer warnings or host records from echoing a bearer.
The zero-dollar preflight's existing HTTP response observer asserts the same
presence-only check and records a boolean, never bodies or credentials. A POST
stopped at the paid-send fence produces no response and proves no echo property;
never report an unobserved response as a negative observation.
Verify the real mount on
the exact deployed image before any paid send. See Google's
[private-source and credential refresh documentation](https://ai.google.dev/gemini-api/docs/agent-environment#private-sources)
and [Credential Access Boundary listing conditions](https://cloud.google.com/iam/docs/downscoping-short-lived-credentials#limit_permissions_when_listing_objects).

Capture currently re-downloads the attached source versions and verifies the
retained archives on every matching capture, including preview and follow-up.
This deliberately fails closed for full deployments on source unavailability or
drift, and adds cross-provider latency and rate-limit dependency. There is no
process-global success cache that could outlive a changed pin or credential.
Storage verification likewise incurs listing and download operations on every
use; quote these separately from model tokens.

The existing MCP token is bound to tenant/account/canonical agent and registry
JTI, with expiry at most 900 seconds (or the provisioning deadline when shorter),
not to a provider session ID. This mode does not assert session isolation between
threads sharing that caller/agent binding. A single-session live fixture must use
its isolated account/channel and revoke the exact JTI at cleanup. No automatic
refresh exists: expiry or revocation refuses further exposure, and continuing
requires a newly authorized credential through the existing mint owner.

### Preserving source capabilities in an OpenAI hosted workspace

An agent-origin `openai.persistent_workspace` deployment can use the same
`source_capabilities` proof as the inline profile. This is explicit operator
configuration; unconfigured channels and conversation-only workers keep their
existing paths. Preview and preparation capture the actual source again and
check the same source pin, account, agent version, channel and exact MCP
catalogue. All six automatic filesystem builtins map through the profile's
hosted bash capability. Unknown tools, confirmation policies and unrepresented
web capabilities refuse instead of acquiring an implicit mapping.

Each skill mapping contains its concrete source version, an absolute retained
archive path, and the native OpenAI skill ID and numeric version. The target's
`digest` is the hash of the canonical `SkillUpload` (files sorted by path),
independent of the native version number and ZIP timestamps. Capture compares
all retained archive bytes with the actual source version. The OpenAI driver
independently downloads `/skills/{id}/versions/{version}/content` and verifies
every file against that digest before publishing the agent or attaching skills
to a session. Existing pins without a digest keep their request shape.

Optional `source_capabilities.environment` must exactly equal the captured
canonical hosted environment policy, including explicit network and packages.
This evidence lets the existing preview/classifier retain that sandbox rather
than declare its removal. Preparation reads the operator's actual native
`environment_id` and checks that policy; source ownership labels are checked
by capture and do not become execution authority. Unrepresentable environment
configuration still refuses.

The deployment also needs all target `skill_ids`, normal counterpart catalogue
pins and an absolute `mcp_credential_file`. The credential file must be a regular
file, owned by the runtime user, mode 0600. Provisioners use the existing
provider-neutral after-ACK credential binding with `chat_execution=True` against
the retained canonical source; they must not mint a token before the native agent has been acknowledged
and read back. The real host factory passes the scoped resolver into the OpenAI
driver. Before session creation, the resolver checks the bearer's registered
identity, account/tenant, expiry, revocation and exact authenticated catalogue.
The driver writes it through the native vault port into a new caller-scoped
session vault, bound to the captured MCP URL, and attaches only `vault_ids` and
the credential ID to the session request. Inline MCP authorization is not sent.
Agent metadata, session bindings and runtime manifests contain references,
never bearer values. The binding retains the owned vault identity so deleting
the session also deletes its vault and credentials after a process restart;
independently supplied vaults remain retained. A definite session-create
rejection cleans the new vault. An uncertain native creation or malformed create
ACK keeps the vault because accepted work may still need it. The raised error
notes its owned vault ID and requires manual provider cleanup after resolving
the session outcome; there is no automatic reconciler. A process crash between
vault creation and persisting the session binding can also leave an untracked
vault that requires operator recovery from native inventory/creation receipts.
Cleanup failures preserve the original error and add a safe failure-type/owned-ID
note. Provider message, code and parameter diagnostics are suppressed throughout
the MCP authentication flow because they may echo credentials from earlier
requests; normalized categories and HTTP status remain available.
A newly authorized credential remains required after caller-token expiry; this
adds no automatic refresh or broader MCP permissions.

Memory has no native preservation mapping in this profile and remains an
explicit preview/consent loss. Model controls, repositories and any other source
obligations retain their existing classification. Preserving skills, filesystem
tools and authenticated MCP does not discharge those separate obligations.

The source `mcp_tools` list must equal the complete ordinary chat catalogue
after the server's search transform, including directly listed approval tools
alongside `search_tools` and `call_tool`. The target execution allowlist can
exclude a confirmation-gated tool only with its named loss consent; that does
not waive complete source-catalogue verification. A recorded source
restricted to direct `describe_agent`/`list_my_sessions` is refused; preserving
those operations through the proxy requires explicit consent to the actual
source catalogue, rather than narrowing or expanding authority during a switch.

#### Confirmation-gated MCP tools in source representations

Capture resolves each enabled tool's effective native permission against the
verified ordinary chat catalogue. A confirmation requirement is never converted
to automatic permission. OpenAI's hosted Agents API currently exposes no MCP
`require_approval` policy or MCP approval response event; the Responses API's
MCP approval protocol cannot be used on Agents sessions. The existing OpenAI
browser-origin approval path remains unchanged.

The Gemini adapter also has no MCP approval request/response path. It compiles
the retained tools as `allowed_tools: [{"mode": "auto", "tools": [...]}]` and
omits each confirmation-gated tool from that list. This preserves automatic
execution for the unaffected tools without granting automatic permission to a
tool that required approval. Private skill mounts and the runtime MCP token
still require the complete original source catalogue, not just the reduced
execution list. Eleven attached skills and mapped filesystem tools can remain
available alongside a named MCP tool loss.

For this unsupported case, capture retains the server and its other tools, but
excludes the gated tool from its executable `allowed_tools`. Preview declares
`mcp_tool:<server>:<tool>` (for example `mcp_tool:daimon-mcp:add_skill`) and the
normal backend selection consent must explicitly accept that loss. The target
instructions disclose it. Omitted loss consent refuses preparation; it never
authorizes execution. Disabled source tools remain disabled without being
reported as lost enabled capabilities. A server with no automatically executable
tools is refused rather than publishing an ambiguous empty allowlist. Existing unconfigured channels and native
Anthropic permission policies retain their behavior.

Provider contract: [Agents MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp).


#### Native builtin defaults in hosted OpenAI source mappings

Native toolsets with an enabled default can include `web_fetch` and `web_search`
even when only the six file/shell tools appear in their explicit configs.
Capture resolves those defaults rather than narrowing the source to the configs.
OpenAI retains native web search and the existing file/shell execution mapping.
Its hosted Agents profile has no native URL-fetch tool, so preview names
`builtin_tool:web_fetch` as a loss and requires explicit acceptance through the
normal chat-agent backend selection consent. The target instructions disclose
that loss. The source remains unchanged, and the declaration does not waive any
sibling capability. Hosted bash is not claimed to emulate the provider's native
URL-fetch behavior; its existing execution and network authority are unchanged.
