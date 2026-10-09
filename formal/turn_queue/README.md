# Turn slots and the queue at the concurrency cap

`TurnQueue.tla` models one adapter process's turn slots and the queue in front
of them ([`turn_queue.py`](../../packages/core/daimon/core/turn_queue.py)): a
global cap, a per-tenant cap, a bounded per-tenant FIFO queue served
round-robin across tenants, the max-wait safeguard, Stop while queued and while
running, every end path of a running turn, and a restart that drops the
in-process queue. Every action boundary is an `await` in the asyncio code;
everything inside one action runs without yielding. A slot release and the
dispatch it owes run in one synchronous span: `owed` blocks every other action
until `Dispatch` has run.

| Model item | Implementation |
| --- | --- |
| `Arrive` | `TurnQueue.admit`: run now when the tenant and the process have a free slot and nobody of the tenant is waiting; else queue; else (queue full) `None`, and the adapter refuses with its plain notice |
| `Dispatch` | `TurnQueue._dispatch`, called from `free`: first eligible tenant in rotation order, its FIFO head, tenant moved to the back |
| `Stop`, `Withdraw` | the card's Stop button sets the turn's cancel event; `TurnTicket.wait` wakes and calls `leave` |
| `Expire` | `TurnTicket.wait` after `max_wait_s`; the adapter ends the card with its ordinary error |
| `Finish` | `TurnTicket.release` in the adapter's `finally`, after an answer, a failure, the turn ceiling or Stop |
| `Restart`, `Recover` | process exit drops the queue; the boot sweep (`turn_card_recovery`, `turn.orphans_found`) retires each card left "working" as "Stopped: Daimon restarted." |
| `Pop` (unsafe only) | a dispatcher that starts the head and pops it after an await |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/turn_queue
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-turn-queue-tlc/$1" -config "$1.cfg" TurnQueue.tla; }
for c in TurnQueueSafe TurnQueueThreeTenants TurnQueueFull TurnQueueProgress; do run $c; done
for c in TurnQueueFullWitness LeakOnFailure GlobalFifoStarves GrantThenPop; do run $c || test $? -eq 12; done
run LeakStallsQueue || test $? -eq 13
```

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
| `QueuedResolves` | temporal | every queued turn starts, is stopped, times out, or dies in a restart |
| `CardsSettle` | temporal | no card stays "working" forever, restarts included |
| `SlotsReturned` | temporal | every slot taken is eventually returned |

The temporal properties assume weak fairness on dispatch, on some end path of
each running turn, on the stopped waiter waking, on the pop and on the boot
sweep. Arrivals, Stop, the max wait and restarts get no fairness, so
`QueuedResolves` holds without relying on the max wait (it is off in the
progress config).

## Constants and scaling

The production numbers are the deployment cap of 200 (`max_concurrent_turns`),
a per-guild cap of 3 (10 for participant guilds at the event, and a raised cap
for the hackathon workspace), queue bounds of 50 per tenant and 500 in total,
and the stress shapes from runs 1 to 3: a 200-mention burst at the cap and a
240-mention probe, 20% over it. TLC cannot hold those counts, so the configs
keep the orderings that decide behavior and shrink the numbers:

| Production | Model | Kept relation |
| --- | --- | --- |
| global cap 200 | `GlobalCap = 2` | more than one slot, so slots are shared |
| busy tenant's raised cap | `FlooderCap = 2` | the busy tenant alone can fill the process (worst case for fairness) |
| per-tenant cap 3 or 10 | `OtherCap = 1` | below the global cap |
| queue 50 per tenant | `TenantQueueMax = 2` | below the global bound |
| queue 500 in total | `GlobalQueueMax = 3` | below the sum of tenant bounds (500 < 50 × guilds), so the global bound can bind first |
| 240 mentions over a cap of 200 | 4 turns from the busy tenant, 1 from another, over a cap of 2 | the burst exceeds the cap and queues behind it; scaled, the overflow is larger (150% versus 20%) |

`TurnQueueFull` shrinks further (cap 1, bounds 2 and 2) so that four turns from
one tenant overflow its bound and a fifth from another overflows the global
bound: refusal is reachable there, and `TurnQueueFullWitness` shows it. The
progress configs use three turns from the busy tenant to keep liveness checking
fast. `TurnQueueThreeTenants` checks round-robin with three tenants (one start
per other tenant is then two).

## Results

| Config | Verdict | Distinct states | What it shows |
| --- | --- | --- | --- |
| `TurnQueueSafe` | clean | 773,237 | two tenants, burst over the cap, max wait, Stop, every end path, one restart: all invariants |
| `TurnQueueThreeTenants` | clean | 130,235 | round-robin over three tenants |
| `TurnQueueFull` | clean | 174,405 | the same invariants with both queue bounds binding |
| `TurnQueueFullWitness` | violates `NoRefusal` | 255 | witness: the plain refusal is reachable, from a full queue |
| `TurnQueueProgress` | clean | 33,475 | liveness: every queued turn resolves, every card settles, every slot returns |
| `LeakOnFailure` | violates `SlotAccounting` | 13 | unsafe: the failure path forgets its slot |
| `LeakStallsQueue` | violates `QueuedResolves` | 823 | unsafe: the same leak without the max wait strands a queued turn (one tenant, so TLC explores every state before its liveness check and the count is stable) |
| `GlobalFifoStarves` | violates `NoStarvation` | 107,295 | unsafe: one global FIFO instead of round-robin |
| `GrantThenPop` | violates `NoDoubleStart` | 8,084 | unsafe: dispatch starts the head and pops it after an await |

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
  dispatcher starts the third but has not yet popped it. That turn ends
  (answered, failed, or stopped) before the pop, its release dispatches again,
  and the dispatcher starts the same turn a second time. TLC's shortest trace
  ends it with an answer; Stop racing the release gives the same double start
  one step later. The code starts and pops in one span, which `TurnQueueSafe`
  checks.

## What is not modelled

The per-tenant cap is a constant here; the code reads it per admission and runs
a dispatch first, so a raised cap starts waiting turns before a newcomer.
Thread-level queueing (`_processing`/`_pending`) is in
[`thread_queue/`](../thread_queue/README.md); a queued turn holds its thread
while it waits, so a follow-up in that thread joins the per-thread queue and
needs no slot of its own. Several adapter processes each keep their own queue;
the caps are per process, as before. TLC checks the abstraction, not the Python
or asyncio scheduling.
