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
| Mux contracts and profiles import no provider SDK or driver | `mux.contracts`, `mux.profiles`, `mux.errors` → `anthropic`, `openai`, `google`, `mux.drivers` |
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
