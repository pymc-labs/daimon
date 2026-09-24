# Per-thread admission queue and turn tail

`ThreadQueue.tla` models one adapter process and one platform thread: the
in-memory `_processing` / `_pending` state, the ⌛-reaction enqueue, the drain
loop, the turn tail (clear the active-turn marker, dispatch queued task
continuations) and continuation dispatch requested from outside a turn by a
credential form submission. Every action boundary is an `await` in the asyncio
code; everything inside one action runs without yielding. Three mentions arrive
in order (`m1` and `m3` from author A, `m2` from B).

| Model item | Implementation |
| --- | --- |
| `Arrive`, `ReactDone` | Slack `_orchestrate` queue check ([`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py)); Discord `on_message` queue check ([`bot.py`](../../packages/adapters/discord/daimon/adapters/discord/bot.py)) |
| `DrainOrRelease`, `Partition` | Slack drain loop and `finally`; Discord `_drain_pending_mentions` and the mention `finally` |
| `Tail1`, `Tail2` | Slack `_run_thread_turn` tail: marker clear, then `_dispatch_continuations`; Discord continuation dispatch at turn end |
| `QueueHandoff` | a handoff tool call recording a `task_continuations` row mid-turn |
| `FormRecord`, `FormDispatch` | credential submission recording its continuation and calling `dispatch_continuations_in_thread` ([Slack](../../packages/adapters/slack/daimon/adapters/slack/credential_submissions.py), [Discord](../../packages/adapters/discord/daimon/adapters/discord/credential_modals.py)) |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/thread_queue
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-thread-queue-tlc/$1" -config "$1.cfg" ThreadQueue.tla; }
for c in QueueBeforeReact HandoffClearFirst FormRedispatch; do run $c; done
for c in DiscordReactFirst DrainMergedAuthors HandoffDispatchFirst FormDuringTail; do run $c || test $? -eq 12; done
```

## Properties

| Invariant | Meaning |
| --- | --- |
| `PrincipalIsAuthor` | each message is answered in its own author's turn (session, vault, billing) |
| `NoStrandedMention` | once the thread is idle, every delivered mention has been answered |
| `HandoffOnDestination` | a handoff's first turn runs in the incoming agent's session |
| `NoStrandedContinuation` | a recorded continuation is not left pending once the thread is idle |

## Calibration

| Fix | Pre-fix config | Verdict | Fixed config | Verdict |
| --- | --- | --- | --- | --- |
| `997b3d8` partition the drain queue by author | `DrainMergedAuthors` | violates `PrincipalIsAuthor` | `QueueBeforeReact` | clean |
| `090ecc1` clear the marker before dispatching the handoff | `HandoffDispatchFirst` | violates `HandoffOnDestination` | `HandoffClearFirst` | clean |
| Slack WR-05 enqueue before the ⌛ reaction (before the public history) | `DiscordReactFirst` (Discord's order on main until pymc-labs/daimon#228) | violates `NoStrandedMention` | `QueueBeforeReact` | clean |

## Findings (current code)

- **Discord strands a mention queued while the turn ends** (`DiscordReactFirst`).
  Discord awaited the ⌛ reaction before appending to `_pending`; if the running
  turn drained and released the thread during that await, the message sat in
  `_pending` until the next mention. Found first by code reading; the model
  reproduces it. Fix: pymc-labs/daimon#228.
- **A continuation recorded as the turn ends waits for the next turn**
  (`FormDuringTail`). A credential form submitted after the running turn's tail
  dispatch but before the thread is released calls
  `dispatch_continuations_in_thread`, which skips a processing thread; nothing
  dispatches the row until another turn in that thread completes. Trace: turn
  ends → marker cleared → continuations dispatched (none yet) → form records its
  continuation → dispatch skipped (thread processing) → thread released.
  `FormRedispatch` (a skipped dispatch re-runs when the thread is released) is
  clean. Fix: pymc-labs/daimon#233.

## Bounds and assumptions

- One process, one thread, three mentions from two authors, one handoff and one
  form submission. A drained batch's failure is not modelled; `997b3d8`'s
  per-author error isolation is outside these properties.
- Cross-process behaviour (restart, orphan sweep, MA session state) is in
  [`adapter_recovery/AdapterOverlap.tla`](../adapter_recovery/README.md).
- A passing TLC run checks only this finite abstraction.
