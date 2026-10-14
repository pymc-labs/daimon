# Gemini host termination evidence

`daimon.testing.gemini_termination` exposes an explicit, offline C18 envelope on
N4's public `termination_evidence` API. Application code never installs it.
`compose_gemini_termination` wraps an existing Gemini conformance adapter with
`host_termination_evidence`, a zero-argument asynchronous facts hook. Its native
controls and every other pending declaration remain unchanged.

The independent inventory contains 21 reachable host reasons. Supplying only a
subset retains the entire inventory and typed C18 `adapter_dependency` PENDING.
Declaring all cases permits the shared fixture to evaluate the facts; it does
not itself certify them. A missing row, wrong attribution, mismatched reason,
error class, session or recovery flag cannot be repaired from expected values.
The public collector uses a fresh observation ID, the normal once-only host
finalizer, the writer drain and the exact committed row. All 21 snapshot fields
remain explicit, including nullable values. Completed and session-terminated
cases retain their session. Interrupted may occur before native preparation.

## Independent reachability audit

| Reasons | Production source |
| --- | --- |
| completed, interrupted, interrupt_timeout, connection_lost, upstream, rate_limited, session_terminated, requires_action | `core/turn/driver.py` finalizers; `core/turn/gemini.py` codec and interrupt/replay; `mux/drivers/gemini/normalize.py` saved native status mapping |
| ceiling | Public `run_prepared_turn` deadline and driver deadline |
| admission_balance_depleted, admission_cap_exceeded, admission_channel_budget_exceeded, admission_channel_protected, admission_agent_pinned_elsewhere, admission_channel_isolated, admission_denied | Common `core/turn/admission.py` gates and explicit Gemini preparation refusal |
| admission_concurrency_shed, pre-native interrupted | `core/turn/slots.py` / `core/turn_queue.py` before preparation |
| missing_config, resolver_miss, session_busy, unknown | Common admission/preparation exceptions and total `termination_reason` mapper |

The seven exclusions are explicit, independent of which tests ran:
`reducer_bug` has no production raiser; `mcp_degraded_empty` and
`retrying_unsettled` need legacy record kinds that the Gemini codec does not
produce; `recovery_cancelled` and `recovery_failed` require the Anthropic-only
replacement branch in `run_prepared_turn`; `session_preparation_failed` and
`session_agent_mismatch` require legacy configuration-application/replacement
paths. Native history replay keeps the Gemini host session and does not set
`RunOutcome.recovered`. Exclusions must be reviewed again if those paths change.

## Offline evidence and limits

The provider proof uses the actual google-genai SDK with owned MockTransport,
explicit credentials, no ambient overrides and no network fallback. Native
POST/GET request bytes traverse `SDKTransport`, the real Gemini driver and the
public `bind_session` / `run_prepared_turn` wrappers. Persistent bindings, event
journal, usage writes and outcome rows use an isolated Postgres database. Native
replies are authored from the documented response shape; these are offline
provider-path proofs, not live certification. No normalized outcome is injected.
The host admission is independently supplied; these tests do not certify the
full admission-policy pipeline. Driver-record storage is in memory, so there
is no process-restart durability claim.

Proved paths include native completed/cancelled/failed/incomplete/budget-exceeded
and requires-action statuses, native GET 429, explicit runtime refusal and the
public deadline. Full C18 remains PENDING until every independently inventoried
reason has real owner-reviewed execution evidence, including queue/admission
and recovery/cancellation distinctions. No hand-created prepared turn, exception
reason or persisted row substitutes for a missing execution.

The shared `db_preflight` runs before engine setup or connections. Every session
bind is validated by the shared guard. Contributor `daimon_test` skips; absent
configuration refuses without allocation. Only a registered process-owned schema
permits the shared CI database. Evidence entry re-admits the source factory and
copies its admitted routing map into a separate factory. Both queued outcome
writes and snapshot reads keep these routes if a callback reconfigures its host's
original factory. Every evidence session allocation rechecks all snapshot binds
and refuses local overrides. This matches the OpenAI envelope; the public
collector and product outcome writer are unchanged.

A continuously rate-limited final usage GET retains an already-finalized
`rate_limited`/`TurnError` and its original session. The failure-only codec hook
reads correlated local usage after the native fetch fails; missing evidence,
local storage, validation and billing failures still propagate. Unknown meters
remain durable pending and keep their unverified hold. Ordinary success and
reconnect reconciliation still require the native fetch. The partial C18 reason
inventory continues to report PENDING.
