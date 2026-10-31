# mux

`packages/mux` is the provider-neutral contract for managed agents: the types,
ports, errors and admission rules that a driver for Anthropic, OpenAI, Gemini
or the self-hosted harness implements and Daimon calls. This page is for
contributors working in the package. Operators who enable a backend should
read [providers](providers.md).

The import name is the top-level `mux`, not `daimon.mux`, and `mux` never
imports `daimon`. Daimon runs turns through mux's ports by default.
[`DAIMON_TURN__PATH`](configuration.md#daimon_turn__path) is the emergency
stop that returns every turn to the Anthropic-only path.

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
| `mux.drivers` | one subpackage per provider: `anthropic`, `openai`, `gemini`, `selfhosted` |
| `mux.testing` | test doubles, the conformance runner and the fake model server |

Every contract type is a frozen pydantic model that rejects unknown fields and
survives a JSON round trip unchanged. Mapping fields are read-only views.
Provider SDK types, exceptions, URLs and tokens never cross the contract.

## Profiles and admission

A channel stores a `BackendConfig`: `backend`, `profile`, `model`, `requires`
and `thread_mode`, all optional. `resolve_default` fills in the rest. Nothing
configured resolves to `anthropic.managed_agents`, one thread per caller and
the agent's own model. Any other backend must name its model, and a profile id
that no declared profile has is refused.

A `ConfigRevision` is one immutable resolved configuration for a channel,
carrying a SHA-256 digest of its content. The digest is checked on load and
again at admission, so a hand-edited revision is refused.

A `Profile` declares a `Support` level (`native`, `emulated`, `unsupported` or
`unknown`) per capability. An undeclared capability is `unknown`, and
admission treats `unknown` as `unsupported`. Nine capabilities are core, and a
profile can be core only if it supports all nine.

| Profile | Core | Notes |
| --- | --- | --- |
| `anthropic.managed_agents` | yes | Every core capability is native. |
| `openai.persistent_workspace` | yes | Missed events cannot be replayed; `reconcile` is emulated from saved items. |
| `openai.conversation_only` | no | No workspace; a conformance profile only. |
| `gemini.inline_reuse` | no | The inline environment expires after inactivity, so workspace persistence is not guaranteed. |
| `selfhosted.loop` | no | The `daimon-harness/0` wire format, explicitly selected. |

A profile declares only what its driver does. The profile honesty test
(`packages/mux/tests/test_profile_honesty.py`) exercises every capability a
profile marks `native` or `emulated`, offline, and fails if the driver refuses
it. A capability added to a profile without a probe fails the test.

`admit(config, profile)` is pure and runs before any provider call:

- On a core profile every core capability is required.
- A `required` capability the profile lacks raises `UnsupportedCapability`,
  listing every gap at once.
- An `optional` capability must declare a fallback when the config is written.
  If the profile lacks it, admission succeeds and lists the fallback in
  `Admission.fallbacks`.
- Capabilities met by emulation are listed in `Admission.emulated`.
- A non-core profile goes without the core capabilities it lacks, listed in
  `Admission.waived_core`, unless the config requires one explicitly.
- `usage_observations` is required on every profile: a turn that cannot be
  metered is never admitted.

### Channel backends in Daimon

Daimon keeps a channel's configuration as a chain of revisions in
`channel_config_revision`; the newest applies. Admission reads it only while
[`DAIMON_TURN__CHANNEL_BACKENDS`](configuration.md#daimon_turn__channel_backends)
is on, after the config cascade and before any provider call. A channel nobody
configured has no revision and runs as before. A configured channel must pass
`admit` and also be one this release can run. Otherwise the turn is refused as
`backend_unsupported` and never falls back to Anthropic.

A channel with `thread_mode="shared"` shares one session per new thread
between everyone who writes in it. Threads where someone already has a session
stay per caller. A shared session carries no caller's credentials, so the
agent must run in GitHub App mode. Usage is still recorded against the person
who wrote each turn. The OpenAI and Gemini backends do not support shared
threads.

## Ports

`ManagedAgents` groups eight ports (`agents`, `environments`, `sessions`,
`events`, `artifacts`, `skills`, `models`, `usage`) with `capabilities()`,
`admit()` and `extension()`. Every port call takes the caller's `Scope`, built
from the host's own authorization decision. Every mutating call takes an
operation `key`, and `expected` (a revision or binding generation) where two
writers could race. `Sessions.migrate` always raises `MigrationUnsupported`: a
backend change applies to new threads only.

A `ResourceRef` names the provider account or workspace a resource lives in.
That is not a thread binding: a `ProviderBinding` records which provider
session backs a thread at which generation, and keeps a stable `id` across
generations.

In a spec, `None` means "not set": the driver sends nothing and the provider's
default applies, while an explicitly empty tuple or mapping is sent as empty.
In a patch's `metadata`, a key mapped to `None` deletes that key. A `Page`
carries the provider's `has_more`, and `next_cursor` is set exactly when it is
true.

Native features are typed extension ports addressed by `(port type,
namespace, version)`, for example Anthropic's `agent_tools`, `memory_stores`,
`vaults`, `multiagent` and `platform_export`. Provider-specific shapes travel
on the agent as an `ExtensionConfig`, and the driver owns and checks that
schema. Asking for a namespace the profile does not offer raises
`UnsupportedCapability`, and another version raises `ExtensionVersionError`.
No port exposes a raw client.

## State

`mux.state.store.StateStore` is the durable state a host keeps for the
library. Each method is one atomic transaction, and every decision comes from
a pure function in `mux.state`, so every store decides the same way. A binding
lives in a slot: a thread plus the caller account that owns it. Per-caller
threads have one private slot per account, and a shared thread has one slot
with no account.

| Record | Unique on | Rule |
| --- | --- | --- |
| config revision | (channel, local) | Immutable. The same number with different content raises `ConfigRevisionConflict`. |
| binding | (slot, generation) | Compare-and-swap on generation (0 = unbound); the loser raises `BindingConflict`. |
| operation | (tenant, account, key) | Persisted as `pending` before any I/O. The same key and request digest returns the existing record; a different digest raises `OperationConflict`. Only `claim_send` moves it to `sent`, so exactly one caller sends. |
| lease | slot | One active root turn per slot. Each acquisition gets a higher fence. |
| journal | (session, sequence) | An append, its projection and the stream cursor commit together; an entry already journaled is dropped. Previews never change the projection. |
| usage | (binding, observation, revision) | A higher revision writes one outbox row of signed deltas; a lower one is ignored. |

Writes that act on a slot's operation or journal need that slot's active lease.
A missing or foreign lease raises `ScopeViolation`, and a superseded one
raises `StaleFence`.

After a crash, a successor claims and sends a `pending` operation, reconciles
a `sent` or `outcome_unknown` one, and observes an `accepted` one. A lost
lease never means the request was not sent. A driver therefore keeps its send
timeout below the lease TTL and never starts I/O on an expired lease.

Usage follows the null-is-not-zero rule: a count the provider did not report
leaves the accounted value alone. Observations of 100, 120 and 110 output
tokens yield deltas of 100, +20 and -10, and replaying any of them yields
nothing.

`MemoryStateStore` is the in-memory reference store; `restart()` simulates a
process restart. `mux.state.suite` holds the protocol checks every store must
pass. Daimon's Postgres store, `daimon.core.stores.mux_state.PostgresStateStore`,
runs the same checks.

## Events

`Event` is one journal entry: an id, the session, a local `sequence`, a
`type`, turn and item ids, timestamps, an `authority` (`record`, `preview`,
`reconciled` or `gap`), a payload and its `NativeProvenance`. Each fixed
type's payload is checked against its model. `agent.thread.*` and `native.*`
events carry an unchecked provider-shaped payload.

`agent.message.delta` is valid only with `authority="preview"`: previews
never bill or complete a turn. A corrected outcome is a new
`session.turn_ended` with `authority="reconciled"` for the same root turn.

## Errors

`MuxError` is the root. `InvalidConfig`, `UnsupportedCapability`,
`ExtensionVersionError`, `ScopeViolation`, `ContinuityLost`, `BindingConflict`,
`OperationConflict`, `MigrationUnsupported` and `ProviderError` derive from
it. `ProviderError.category` is one of `auth`, `permission`, `not_found`,
`conflict`, `invalid_request`, `rate_limited`, `overloaded`, `upstream` or
`transient_network`. A delivery the driver cannot confirm is not an error: its
receipt reports `outcome_unknown`.

## Usage

`UsageObservation` is one measurement with an `id` and an integer `revision`.
A higher revision of the same `id` supersedes a lower one and is applied as a
delta, and an observation never overwrites a higher revision already applied.
Token counts are nullable, because an unreported count is unknown, not zero.
`input_tokens` includes cached and cache-write counts, and
`output_reasoning_tokens` is part of `output_tokens`. `native_meter` keeps the
provider's own usage record unchanged.

## Public import paths

Every public module declares `__all__`, and each public name is reachable from
one module. `packages/mux/tests/test_public_exports.py` checks this.

| Import from | For |
| --- | --- |
| `mux.contracts.<module>` | the contract types in the layout table, each from the module that defines it |
| `mux.errors` | the error taxonomy |
| `mux.state.<module>` | the state protocol and rules |
| `mux.profiles` | the declared profiles and their lookups |
| `mux.drivers.<provider>` | the driver factory and host-injection API: transports, credential schemas, resource ports |

Import a driver's names from its package, not from a submodule. The one public
driver submodule is `mux.drivers.selfhosted.wire`, the `daimon-harness/0` wire
format shared by the host, the harness and the conformance kit. Other
submodules and `_`-prefixed modules are implementation files and may move. An
import-linter contract keeps Daimon's own code to this rule.

## Dependencies and import rules

`daimon-mux` depends on `pydantic` and `httpx`. Each provider SDK is an extra:
`daimon-mux[anthropic]`, `[openai]` and `[gemini]`.

Import-linter contracts in the root `pyproject.toml` hold the boundaries:

| Contract | Forbids |
| --- | --- |
| Mux must not import daimon | `mux` → `daimon` |
| Contracts, profiles and state import no provider SDK or driver | `mux.contracts`, `mux.profiles`, `mux.errors`, `mux.state` → `anthropic`, `openai`, `google`, `mux.drivers` |
| Only a provider's driver imports its SDK (one contract per provider) | `mux` → that SDK, except from `mux.drivers.<provider>` and `mux.testing.drivers.<provider>` |
| Product code must not import test harnesses | `daimon.core`, `daimon.adapters` and every mux module except `mux.testing` → `mux.testing`, `daimon.testing` |
| Daimon imports drivers through their packages | `daimon.core`, `daimon.adapters` → submodules of a driver, with named exceptions |

CI runs the mux tests in the `pytest-mux` job. The `mux-standalone` job builds
an environment with only `daimon-mux`, its extras and the test dependencies,
checks that `daimon` cannot be imported, and reruns the tests there.
`mux.testing` carries the test doubles, so the tests need nothing from the
Daimon host.

## Conformance

`mux.testing.conformance` provides an offline runner for the C01-C18
conformance checks. A driver registers a fresh fake-transport adapter per
fixture, and the `pytest-mux` job collects the results as pass, fail or
pending. A pending check blocks certification, and pending never counts as
a pass. The in-memory reference driver is a test oracle, not a backend. The
adapter requirements are in `packages/mux/docs/conformance/README.md`.

Each driver also has its own notes under `packages/mux/docs/drivers/`.
