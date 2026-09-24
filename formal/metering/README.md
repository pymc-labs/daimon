# Usage metering: live recorder, usage sweep and balance gate

Run from the repository root with a JRE and the pinned TLA+ tools jar described
in the [coverage report](../README.md). `formal/check.sh` runs every
configuration below and compares each verdict with `formal/expected.tsv`. To run
one configuration by hand:

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to tla2tools.jar}"
cd formal/metering
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config Metering.cfg Metering.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config BalanceGate.cfg BalanceGate.tla
```

The directory holds two models:

- `Metering.tla` covers who writes a model call's usage and ledger rows, how
  many times, and with which price and attribution.
- `BalanceGate.tla` covers how far a tenant's balance can go below zero when
  the gate reads the balance once, before spend lands.

State counts below come from single-worker TLC 2.19 (tla2tools v1.7.4) runs.

## `Metering.tla`

One Managed Agents session belongs to one tenant, and deployment `D1` created
it. MA appends `K` `span.model_request_end` events to the session log, then a
terminal idle. The driver consumes them over SSE stream generations. A
generation can end without its terminal event (`Drop`: a dropped connection,
the server's close about every ten minutes, or a 120 s read stall). The driver
then replays history and either reconnects (`Reconnect`, session still running)
or finalizes (`FinalizeFromReplay`, session idle). A new generation may
re-emit one already emitted event. MA does not document whether it re-emits,
so TLC explores both choices. The adapter can also die mid-turn (`Crash`).

Each deployment's scheduler sweep (`SweepStart`/`SweepStep`/`SweepEnd`) lists
the session's events and replays each through the same recorder. A second
deployment `D2` bills the session only when `SharedTenant` holds and the sweep
does not check which deployment created the session. `rows[d][e]` is the
`usage_events` + `tenant_ledger` pair for event `e` in deployment `d`'s
database. It is written by whichever writer commits first, because both rows
are inserted `ON CONFLICT DO NOTHING` in one transaction. A row records its
writer (`live` or `sweep`) and the model it was priced at. The live row carries
the turn's `platform_user_id` and ledger reason. The sweep row carries
`turn_debit` and the user resolved from the session's `daimon_account` stamp.

| Model item | Implementation |
| --- | --- |
| `Deliver`, `delivered`, `hookCalls` | consume loop in [`_consume_with_reconnect`](../../packages/core/daimon/core/turn/driver.py) (`delivered_event_ids`, 44528cb; `_bill_once`) |
| `Reconnect`, `FinalizeFromReplay`, `ReplayBill` | the two replay folds in [`driver.py`](../../packages/core/daimon/core/turn/driver.py) (`_consume_with_reconnect` retry, `_pump` eventless-cycle finalize) and `_bill_replayed` |
| `LivePrice`, `LiveFrozenPrice` | [`bind_recorder`](../../packages/core/daimon/core/turn/prepare.py) binds the session snapshot's model (1c4df18); the sweep uses `session.agent.model.id` |
| `Write`, `rows` | [`record_turn_usage`](../../packages/core/daimon/core/usage_recording.py), [`usage_events.record`](../../packages/core/daimon/core/stores/usage_events.py), [`tenant_ledger.insert_entry`](../../packages/core/daimon/core/stores/tenant_ledger.py) |
| `SweepStart`/`SweepStep`/`SweepEnd`, `Bills(d)` | [`sweep_headless_usage`](../../packages/core/daimon/core/usage_sweep.py) (`known_tenants` check), called every tick from [`scheduler/main.py`](../../packages/adapters/scheduler/daimon/adapters/scheduler/main.py) |
| `LiveExempt` | `BillingExempt` in [`posture.py`](../../packages/core/daimon/core/turn/posture.py) (`daimon run`); MCP `_admit` with `platform_user_id is None` in [`tools/_ctx.py`](../../packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py) |

Invariants:

- `HookOnce`: the billing hook runs at most once per event.
- `PriceAgreement`: every row is priced at the session's frozen model.
- `LiveMetersWholeTurn`: a turn the driver finalized has invoked its recorder
  for every model call.
- `NoForeignDebit`: no deployment debits another deployment's session.
- `SweepBackstop`: after an owner sweep pass that starts once MA is idle, every
  call has a row, whatever happened to the driver.
- `AttributionPreserved`: every row of a turn whose driver stayed alive was
  written by that driver.
- `ExemptNotBilled`: a `BillingExempt` turn has no rows.

"At most one debit per (session, event) per deployment" holds by
construction, because `rows` is a function. That is the unique idempotency key.

| Config | Toggles vs `Metering.cfg` | Verdict | Distinct states | Meaning |
| --- | --- | --- | --- | --- |
| `Metering` | all fixes on | clean | 8,006 (depth 17) | all safety invariants above except `AttributionPreserved` and `ExemptNotBilled` |
| `MeteringReplayGap` | `BillReplayed = FALSE` | violates `LiveMetersWholeTurn` | 160, trace 6 | **new finding, fixed by #234**: two calls emitted, the stream drops before delivering them, MA goes idle, and the driver finalizes from the replay with no recorder call |
| `MeteringPre44528cb` | `DedupeHooks = FALSE`, `BillReplayed = FALSE`; checks `TypeOK`, `HookOnce` | violates `HookOnce` | 230, trace 6 | **calibration**: an event re-emitted after a reconnect re-runs the billing hook |
| `MeteringPre1c4df18` | `LiveFrozenPrice = FALSE`, `BillReplayed = FALSE` | violates `PriceAgreement` | 28, trace 4 | **calibration**: after an `agents.update`, the live recorder prices an old session's call at the new model |
| `MeteringSharedTenant` | `SharedTenant = TRUE` | violates `NoForeignDebit` | 57, trace 4 | deployment precondition (see below) |
| `MeteringSharedTenantOwned` | `SharedTenant`, `SweepChecksOwner` | clean | 8,006 | hypothetical deployment stamp; clean by construction (see below) |
| `MeteringSweepRace` | checks `AttributionPreserved` only | violates | 41, trace 4 | documented residual: the sweep lists a live turn's call before the driver commits it |
| `MeteringExemptSwept` | `LiveExempt = TRUE` | violates `ExemptNotBilled` | 41, trace 4 | documented discrepancy (see below) |

### Calibration

| Bug | Pre-fix config | Pre-fix counterexample | Post-fix config | Post-fix |
| --- | --- | --- | --- | --- |
| 44528cb: delivery and billing hooks re-ran for events redelivered after a reconnect | `MeteringPre44528cb` | `HookOnce`, trace of 6 states: emit e1, deliver (hook), drop, reconnect re-emitting e1, deliver (hook again) | `Metering` | clean, 8,006 states |
| 1c4df18 (#177): turns billed at the agent's live model, not the session's | `MeteringPre1c4df18` | `PriceAgreement`, 4 states: agent model changes, e1 emitted and delivered, row priced at `m1` | `Metering` | clean |

Both calibration configs set `BillReplayed = FALSE`, because the replay folds
did not bill until #234, long after either fix; the post-fix column is today's
`Metering` shape. That shape also violates `LiveMetersWholeTurn` (the #234 gap
existed then too), and a breadth-first search reaches that violation first, so
`MeteringPre44528cb` checks only `TypeOK` and `HookOnce` to isolate 44528cb.
`MeteringPre1c4df18` keeps every invariant, because its `PriceAgreement`
trace is shorter.

Before 44528cb the double hook call did not double-debit, because the database
key absorbed it. The adapter's `on_sse_event` was not protected that way, and
that side effect is what the invariant models.

### Findings

1. **Replay-only model calls were not billed by the turn** (`MeteringReplayGap`).
   I confirmed this in the code. Neither replay fold in `driver.py` called the
   recorder, so a call MA made while no stream was attached reached the ledger
   only through the sweep: later, with the sweep's attribution, or never
   without a scheduler. Failing tests and the fix are in #234. The model's
   `BillReplayed = TRUE` is the fixed shape, and `Metering.cfg` is clean with it.
   `Metering` being clean therefore describes the code only once #234 merges;
   until then, main has the `MeteringReplayGap` shape.
2. **Cross-deployment debit** (`MeteringSharedTenant`). I confirmed this in the
   code. The sweep bills every session whose `daimon_tenant` stamp names a
   tenant in its own database. Tenant ids are `uuid5(platform, workspace)`, so
   two deployments that share an MA workspace (the sweep's own comment says
   shared workspaces exist) and that both have the same Discord guild or Slack
   workspace installed will both debit every such session. The deployment that
   did not run it debits too. Nothing on a session identifies the deployment.
   DECISION: documented as a deployment precondition in `docs/billing.md`, not
   fixed. The hosted topology is unknown, and a fix needs a deployment identity
   stamp (a new setting or table). `MeteringSharedTenantOwned` models such a
   stamp as "D2's sweep never bills D1's session" (`Bills` in `Metering.tla`),
   so it is clean by construction. Its state count equals `Metering`'s. It
   says nothing about how a real stamp would be written or checked.
3. **Sweep attribution race** (`MeteringSweepRace`). This is inherent to two
   writers with first-writer-wins rows. The amount is identical once prices
   agree. The reason and platform user are the sweep's when it commits first.
   The window is the driver's per-event latency. I documented it and did not
   fix it.
4. **Exempt turns are billed by the sweep** (`MeteringExemptSwept`). I confirmed
   this in the code. `daimon run` runs `BillingExempt` on an existing session.
   An MCP caller with no platform user skips the gates. Both still act on a
   session stamped with `daimon_tenant`, which the sweep then debits to the
   tenant. `docs/billing.md` said these callers get "no usage row and no
   debit". DECISION: this is a product-semantics question (who pays for an
   operator's turn on a tenant's session). I left the behaviour unchanged and
   corrected the documentation to state what happens.

## `BalanceGate.tla`

One tenant starts with balance `B0 = 1`. Each activity is admitted once:
`Admit` reads the balance and denies unless it is strictly positive when the
activity is gated. It then spends `C = 2` model calls of one unit each.
Activity kinds:

- a chat turn on a new session;
- a chat turn on a reused session;
- a thread-participation classifier call;
- a headless MCP `start_turn` session.

A unit of spend reaches the ledger in one of three ways: live at the call, at
the next `Sweep`, or never. At most `MaxInflight` activities run at once (the
per-tenant concurrency cap). Nothing re-checks the balance after admission.

| Model item | Implementation |
| --- | --- |
| `Admit` (new/reused chat) | [`admission.admit`](../../packages/core/daimon/core/turn/admission.py) (`is_over_balance` then `is_over_cap`); Slack [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py), Discord [`bot.py`](../../packages/adapters/discord/daimon/adapters/discord/bot.py) |
| `Admit` (headless) | [`tools/_ctx.py:_admit`](../../packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py) before `start_turn` / `continue_turn` in [`agent_chat.py`](../../packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py) |
| `Admit` / `Spend` (classifier) | Discord [`thread_participation.py`](../../packages/adapters/discord/daimon/adapters/discord/thread_participation.py), `record_classifier_usage` in [`usage_recording.py`](../../packages/core/daimon/core/usage_recording.py) |
| `Balance` | [`tenant_ledger.get_balance`](../../packages/core/daimon/core/stores/tenant_ledger.py) via [`tenant_balance.is_over_balance`](../../packages/core/daimon/core/tenant_balance.py) |
| `Sweep` | [`usage_sweep.sweep_headless_usage`](../../packages/core/daimon/core/usage_sweep.py) |

Invariants:

- `GatedSpend`: all spend belongs to an activity that started while the ledger
  balance was positive. `admitBalance[a]` records the balance `Admit` read, so
  the invariant is stated over the ledger, not over the gate flags: an ungated
  activity violates it only when it actually starts on a balance that is
  already zero or below.
- `SpendMetered`: a finished activity not left to the sweep is fully debited.
- `OverdraftBound`: `Balance >= -(MaxInflight * C)`. This is the bound implied
  by "N concurrent turns x max turn cost".

| Config | Activities / toggles | Verdict | Distinct states |
| --- | --- | --- | --- |
| `BalanceGate` | 2 new, 1 reused, 1 classifier, `MaxInflight = 2`, fixes on | clean (all three) | 969 (depth 13) |
| `BalanceGatePre8192ff7` | same, `GateOnReuse = MeterReuseLive = FALSE` | violates `GatedSpend` | 105, trace 5 |
| `BalanceGatePre8192ff7Overdraft` | same as above; checks `TypeOK`, `OverdraftBound` | violates `OverdraftBound` | 3,398, trace 12 |
| `BalanceGatePreFa83d3c` | same, `GateClassifier = MeterClassifier = FALSE` | violates `GatedSpend` | 104, trace 5 |
| `BalanceGatePreFa83d3cUnmetered` | same as above; checks `TypeOK`, `SpendMetered` | violates `SpendMetered` | 141, trace 5 |
| `BalanceGateHeadless` | 3 headless, `MaxInflight = 1` | violates `OverdraftBound` | 192, trace 9 |

### Calibration

| Bug | Pre-fix counterexample | Post-fix (`BalanceGate`) |
| --- | --- | --- |
| 8192ff7: Slack follow-up turns on a reused session skipped both gates and the live recorder | `GatedSpend`, trace of 5 states: a new turn is admitted at balance 1 and spends one unit, then the reused turn starts at balance 0 and spends. `BalanceGatePre8192ff7Overdraft` (only `OverdraftBound`): violated, trace of 12 states, the ledger below `-(MaxInflight * C)` | clean, 969 states |
| fa83d3c: the participation classifier ran before the gates and was never debited | `GatedSpend`, trace of 5 states: a new turn spends the balance to 0, then the classifier starts and spends. `BalanceGatePreFa83d3cUnmetered` (only `SpendMetered`): violated, trace of 5 states, a finished classifier call with no debit | clean |

### Findings

- **The overdraft bound is looser than the documentation implied**
  (`BalanceGateHeadless`). `docs/billing.md` said a single long turn can take a
  balance negative. That is true, but a headless MCP turn's spend reaches the
  ledger only when the scheduler sweep runs. The tick awaits every routine fire
  (up to the 45-minute turn ceiling) before the sweep, and the sweep walks the
  whole workspace serially. Until then, `_admit` reads a balance that excludes
  earlier headless turns, so sequential headless turns keep passing. The trace
  admits a second turn after the first finished but before the sweep, and the
  balance ends at -3 against a bound of -2. MCP start_turn has no per-tenant
  concurrency cap. Chat turns add up to `max_concurrent_turns_per_tenant` (3) x
  one turn's cost. Replay-only calls before #234 widened the lag the same way.
  DECISION: this is the by-design gate TOCTOU (M5), so the boundary is
  documented in `docs/billing.md` and not changed.
  The config's `MaxInflight = 1` makes the compared bound one turn's cost. With
  `MaxInflight = 3` the same three headless turns stay above `-(3 * C)` and the
  config is clean, so what the trace shows is sequential headless turns each
  passing a stale gate, not a specific numeric overdraft. In the fixed
  `BalanceGate` config the worst reachable balance is -3, so its
  `OverdraftBound` (-4) holds but is not tight.

## Related model: `billing/` (credits and clawbacks)

[`billing/`](../billing/README.md) (`Clawback.tla`, `OutOfOrder.tla`) models
the credit side of the same `tenant_ledger`: Stripe top-up credits, refund and
dispute clawbacks, and clawbacks that arrive before their credit. This
directory models the debit side: per-model-call usage rows, the usage sweep,
and the admission gate that reads the balance. The two do not model the same
writer:

- `billing/` abstracts "all other ledger writes", which are the usage debits
  modelled here.
- `Metering.tla` and `BalanceGate.tla` abstract credits and clawbacks, apart
  from `BalanceGate`'s starting balance `B0`.

They rely on the same mechanism, and do not contradict each other: every
ledger insert is `ON CONFLICT DO NOTHING` on a unique `idempotency_key`, and
the key spaces are disjoint (`turn:{session}:{event}`, and the media,
classifier and thread-naming prefixes, here; `topup:…` and
`clawback:{pi}:{event}` there).

One scope gap between them: `BalanceGate`'s `OverdraftBound` counts only turn
spend admitted against a positive balance. A clawback of credit that has
already been spent debits the ledger too, and can take the balance further
below zero than that bound. This is by design, since a refund of spent credit
is owed. Neither model states a combined bound.

## Bounds and assumptions

- `Metering`:
  - Bounds: `K = 2` calls, `MaxDrops = 2`, `MaxRedeliveries = 1`, and
    `MaxSweeps = 2` per deployment.
  - One session and one tenant.
  - Transactions are atomic model steps, which matches one transaction per
    `record_turn_usage` call.
  - The unique key is modelled as first-writer-wins, which is PostgreSQL
    `ON CONFLICT DO NOTHING` behaviour under `READ COMMITTED` (a concurrent
    second insert waits, then does nothing).
  - Not modelled: interrupts and turn ceilings, DB failures (the recorder is
    fail-closed and aborts the turn), malformed session metadata (S2, draft PR
    #150), and the replay heuristic's fallback when `user.message` events are
    missing.
- `BalanceGate`:
  - Integers stand in for dollars. Every call costs the same, and there is one
    tenant.
  - The monthly per-person cap gate is omitted. It is inert unless Stripe is
    configured, and nothing writes caps.
  - Media and thread-naming tools meter like the classifier after its fix.
  - Headless activities share `MaxInflight` only to keep the state space
    finite. The real MCP path has no cap, so the real bound is worse.
- Passing TLC runs check only these finite abstractions, not the Python code,
  PostgreSQL, or MA. The executable evidence for the replay finding is the
  #234 test file `packages/core/tests/turn/test_driver_replay_billing.py`
  (unit tests plus one real-Postgres test).
