# Turn slots and the queue at the concurrency cap

`TurnQueue.tla` models one adapter process's turn slots and the queue in front
of them ([`turn_queue.py`](../../packages/core/daimon/core/turn_queue.py),
[`turn/slots.py`](../../packages/core/daimon/core/turn/slots.py)): a global
cap, a per-tenant cap, a bounded per-tenant FIFO queue served round-robin
across tenants, the max-wait safeguard, Stop while queued and while running,
every end path of a running turn, follow-up turns in one thread, and a restart
that drops the in-process queue.

The question it answers: can a turn be lost, run twice, start after its max
wait, leak a slot, or starve another tenant, on any interleaving of arrivals,
releases, Stop clicks, timeouts, follow-ups and restarts?

Every action boundary is an `await` in the asyncio code; everything inside one
action runs without yielding. A slot release and the dispatch it owes run in
one synchronous span: `owed` blocks every other action until `Dispatch` has
run.

| Model item | Implementation |
| --- | --- |
| `Arrive` | `TurnQueue.admit` ([`turn_queue.py:269`](../../packages/core/daimon/core/turn_queue.py)): run now when the tenant and the process have a free slot and nobody of the tenant is waiting; else queue; else (queue full) `None`, and the adapter refuses with its plain notice. A follow-up arrives the same way through `TurnQueue.readmit` (`turn_queue.py:309`), called from `wait_for_slot` (`slots.py:97`), once the turn before it in the thread has ended |
| `Dispatch` | `TurnQueue._dispatch` (`turn_queue.py:329`), called from `free`: first eligible tenant in rotation order, its FIFO head, tenant moved to the back. A head already past its max wait is timed out there (`_expire`, `turn_queue.py:377`); the tenant keeps its place and dispatch continues |
| `PassMaxWait` | time passing `max_wait_s` for a queued ticket (`TurnTicket.overdue`) |
| `Stop`, `Withdraw` | the card's Stop button sets the turn's cancel event; `TurnTicket.wait` (`turn_queue.py:122`) wakes and calls `leave` |
| `Expire` | `TurnTicket.wait` after `max_wait_s`; the adapter ends the card with its ordinary error |
| `Finish` | `release_turn_slot` (`slots.py:89`) at the end of every turn (Discord `_handle_mention`, Slack `_run_thread_turn`, Teams `_run_turn`), and `holding`'s exit, after an answer, a failure, the turn ceiling or Stop |
| `Restart`, `Recover` | process exit drops the queue; the boot sweep (`turn_card_recovery`, `turn.orphans_found`) retires each card left "working" as "Stopped: Daimon restarted." |
| `Pop` (unsafe only) | a dispatcher that starts the head and pops it after an await |
| `ChainFollowUp` (unsafe only) | the pre-review drain: a follow-up ran on the slot its thread's first turn claimed |
| `Arrive` with `FollowUpReusesTicket` (unsafe only) | the pre-review drain: a follow-up reused a ticket that Stop or the max wait had already taken out of the queue |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/turn_queue
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-turn-queue-tlc/$1" -config "$1.cfg" TurnQueue.tla; }
for c in TurnQueueSafe TurnQueueThreeTenants TurnQueueFull TurnQueueProgress FollowUpsReadmit; do run $c; done
for c in TurnQueueFullWitness LeakOnFailure GlobalFifoStarves GrantThenPop DispatchGrantsLate FollowUpKeepsSlot FollowUpReusesTicket; do run $c || test $? -eq 12; done
run LeakStallsQueue || test $? -eq 13
```

`formal/check.sh` runs them all against the verdicts and state counts pinned
in `formal/expected.tsv`.

## Properties

| Name | Kind | Meaning |
| --- | --- | --- |
| `NoDoubleStart` | invariant | no turn body starts twice |
| `SlotAccounting` | invariant | the slot counters equal the turns actually running, after every end path |
| `WithinCaps` | invariant | slots in use never exceed the global or the tenant cap |
| `QueueBounded` | invariant | queue depth stays within the per-tenant and global bounds |
| `NoLostTurn` | invariant | a queued turn sits exactly once in its own tenant's queue, and the rotation holds exactly the tenants with waiting turns |
| `CardTruth` | invariant | the card says "working" exactly while the turn is queued or running, or dead and not yet swept |
| `WorkConserving` | invariant | once dispatch has run, no eligible turn waits while a slot is free |
| `NoStarvation` | invariant | while a tenant has an eligible waiting turn, at most one start per other tenant happens before it is served |
| `NoLateStart` | invariant | a turn never starts after its max wait has passed |
| `FollowUpWaitsItself` | invariant | a follow-up never ends with the error unless it waited in the queue itself or ran |
| `QueuedResolves` | temporal | every queued turn starts, is stopped, times out, or dies in a restart |
| `CardsSettle` | temporal | no card stays "working" forever, restarts included |
| `SlotsReturned` | temporal | every slot taken is eventually returned |

The temporal properties assume weak fairness on dispatch, on some end path of
each running turn, on the stopped waiter waking, on the pop and on the boot
sweep. Arrivals, Stop, the max wait and restarts get no fairness, and the
progress config turns the max wait off, so `QueuedResolves` does not rely on
the timeout to hide a stranded turn.

## Constants and scaling

| Production value | Source | Model | Kept relation |
| --- | --- | --- | --- |
| global cap 200 | `DAIMON_DISCORD__MAX_CONCURRENT_TURNS` ([`config.py:399`](../../packages/core/daimon/core/config.py)), set to 200 in production (prod-scale, 2026-10-08) | `GlobalCap = 2` | more than one slot, so slots are shared |
| busy tenant's raised cap | `daimon tenants turn-cap` override | `FlooderCap = 2` | the busy tenant alone can fill the process (worst case for fairness) |
| per-tenant cap 3 (10 for participant guilds) | `max_concurrent_turns_per_tenant` (`config.py:391`, `:522`, `:587`) | `OtherCap = 1` | below the global cap |
| queue 50 per tenant | `DAIMON_TURN_QUEUE__MAX_PER_TENANT` (`config.py:1269`) | `TenantQueueMax = 2` | below the global bound |
| queue 500 in total | `DAIMON_TURN_QUEUE__MAX_TOTAL` (`config.py:1278`) | `GlobalQueueMax = 3` | below the sum of tenant bounds (500 < 50 × guilds), so the global bound can bind first |
| max wait 300 s | `DAIMON_TURN_QUEUE__MAX_WAIT_S` (`config.py:1286`) | `PassMaxWait`, untimed | only the ordering of "max wait passed" against releases matters |
| 240 mentions over a cap of 200 | stress runs 1 to 3 | 4 turns from the busy tenant, 1 from another, over a cap of 2 | the burst exceeds the cap and queues; scaled, the overflow is larger (150% versus 20%) |
| a thread's follow-ups | the per-thread drain | `FollowUps = 1` (safe), `2` (follow-up configs) | a follow-up arrives only after the turn before it ends |

`TurnQueueFull` shrinks further (cap 1, bounds 2 and 2) so that both bounds
bind; `TurnQueueFullWitness` shows refusal is reachable there. The follow-up
configs use a global cap of 1, where keeping a slot across follow-ups shows
most clearly. The progress config uses three turns from the busy tenant and
no max wait to keep liveness checking fast. `TurnQueueThreeTenants` checks
round-robin with three tenants. `LeakStallsQueue` has one tenant, so TLC
explores every state before its liveness check and the count is stable.

## Results

Checked within these bounds, not proved for the production numbers.

| Config | Verdict | Distinct states | What it shows |
| --- | --- | --- | --- |
| `TurnQueueSafe` | clean | 560,415 | two tenants, burst over the cap, max wait, Stop, every end path, a follow-up, one restart: all invariants |
| `TurnQueueThreeTenants` | clean | 302,963 | round-robin over three tenants |
| `TurnQueueFull` | clean | 416,349 | the same invariants with both queue bounds binding |
| `TurnQueueFullWitness` | violates `NoRefusal` | 257 | witness: the plain refusal is reachable, from a full queue |
| `TurnQueueProgress` | clean | 19,795 | liveness with the max wait off: every queued turn resolves, every card settles, every slot returns |
| `FollowUpsReadmit` | clean | 3,551 | a two-follow-up thread at global cap 1: each follow-up re-enters admission; all invariants |
| `LeakOnFailure` | violates `SlotAccounting` | 13 | unsafe: the failure path forgets its slot |
| `LeakStallsQueue` | violates `QueuedResolves` | 823 | unsafe: the same leak without the max wait strands a queued turn |
| `GlobalFifoStarves` | violates `NoStarvation` | 127,426 | unsafe: one global FIFO instead of round-robin |
| `GrantThenPop` | violates `NoDoubleStart` | 9,486 | unsafe: dispatch starts the head and pops it after an await |
| `DispatchGrantsLate` | violates `NoLateStart` | 68 | pre-review code: dispatch starts a turn already past its max wait |
| `FollowUpKeepsSlot` | violates `NoStarvation` | 45 | pre-review code: a thread's follow-ups keep its slot |
| `FollowUpReusesTicket` | violates `FollowUpWaitsItself` | 87 | pre-review code: a follow-up reuses a stopped turn's ticket |

### Counterexamples in plain words

- `LeakOnFailure`: a turn arrives, runs and fails; the counter still says one
  turn is running when none is.
- `LeakStallsQueue` (one tenant): its first turn fails and keeps its slot;
  its second runs and fails too, and both slots are now counted forever. Its
  third turn queues and, with no max wait, waits forever.
- `GlobalFifoStarves`: the busy tenant fills both slots and queues two more;
  the other tenant's turn queues behind them. Each release serves the oldest
  waiting turn, so the busy tenant's two queued turns both start before the
  other tenant's, which round-robin would have served after one.
- `GrantThenPop`: two turns run and a third waits. A slot frees and the
  dispatcher starts the third but has not yet popped it. That turn ends before
  the pop, its release dispatches again, and the same turn starts a second
  time.
- `DispatchGrantsLate`: one turn runs and a second waits. The second's max
  wait passes, but before its waiter wakes the first turn ends and the
  release hands the slot to the second, which starts five minutes late
  instead of timing out.
- `FollowUpKeepsSlot`: with one slot, the busy tenant's thread runs and the
  other tenant's turn queues. The thread's first follow-up starts on the same
  slot, then its second, so the other tenant is overtaken twice and, with a
  longer thread, would wait until its max wait expires.
- `FollowUpReusesTicket`: a turn queues, the person clicks Stop, and it
  leaves the queue. Their follow-up in the thread inherits that finished
  ticket and ends at once with "Something went wrong" without ever waiting.

## Replay status

Each counterexample has a test against the real code that the fix makes pass:

| Counterexample | Test |
| --- | --- |
| `LeakOnFailure`, `LeakStallsQueue` | `test_release_is_idempotent_and_counts_stay_exact`, `test_holding_releases_a_queued_ticket_on_error` (core); `test_inflight_decrements_after_failed_turn`, `test_global_slot_released_when_turn_fails` (Discord) |
| `GlobalFifoStarves` | `test_round_robin_a_flooding_tenant_does_not_starve_another`, `test_round_robin_over_three_tenants` |
| `GrantThenPop` | `test_grant_and_pop_happen_in_the_release_span` |
| `DispatchGrantsLate` | `test_dispatch_times_out_a_ticket_past_its_max_wait_instead_of_starting_it`, `test_an_overdue_ticket_wakes_its_waiter_at_once` |
| `FollowUpKeepsSlot` | `test_follow_ups_release_and_re_queue_so_another_tenant_is_not_starved` (core), `test_a_drained_follow_up_re_queues_behind_another_tenants_waiting_turn` (Discord) |
| `FollowUpReusesTicket` | `test_a_follow_up_after_a_stopped_turn_takes_a_fresh_ticket`, `test_a_follow_up_after_a_timed_out_turn_takes_a_fresh_ticket` |

Trace validation against staging logs (`turn.queue.*` events from the
prod-scale rerun) has not been done yet, so the clean results are provisional
until the model accepts those traces.

## Assumptions and what is not modelled

- The per-tenant cap is a constant here; the code reads it per admission and
  runs a dispatch first, so a raised cap starts waiting turns before a
  newcomer.
- A turn that waited runs the balance gate again once it holds a slot; a
  refusal there is one more end path of a started turn (`Finish`).
- Thread-level queueing (`_processing`/`_pending`) is in
  [`thread_queue/`](../thread_queue/README.md). Here a thread is only the
  ordering "a follow-up arrives after the turn before it ends".
- Several adapter processes each keep their own queue, and the caps are per
  process; the model is one process.
- Unprompted participation (`try_claim`) and Teams continuation wakes
  (`claim`) never queue, so they are `Arrive` without the queue branch and are
  not separate actions.
- How a continuation's dispatcher settles a turn that never got a slot
  (`TurnNotStarted` in `continuity/dispatch.py`: capacity back to pending,
  Stop settled not delivered) is outside this model; a test covers it.
- TLC checks the abstraction, not the Python or asyncio scheduling.
