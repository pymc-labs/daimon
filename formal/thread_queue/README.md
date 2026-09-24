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
| `FormRecord`, `FormDispatch` | credential submission recording its continuation and calling `dispatch_continuations_in_thread` ([Slack](../../packages/adapters/slack/daimon/adapters/slack/credential_submissions.py), [Discord](../../packages/adapters/discord/daimon/adapters/discord/credential_modals.py)); the claim step: skip (and with #233 remember) a processing thread, else add it to `_processing` and run the continuation turn |
| `FormRelease` | the dispatch's `finally`: `_release_thread` on main; with `DrainAfterDispatch` (#237) the queued mentions are drained first, one turn per author |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/thread_queue
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-thread-queue-tlc/$1" -config "$1.cfg" ThreadQueue.tla; }
for c in QueueBeforeReact HandoffClearFirst FormRedispatch FormDispatchDrain; do run $c; done
for c in DiscordReactFirst DrainMergedAuthors HandoffDispatchFirst FormDuringTail FormDispatchNoDrain; do run $c || test $? -eq 12; done
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

## Regression rows (fixed on main)

These bugs were found while building the model and are fixed on main by
pymc-labs/daimon#228 and #233. The pre-fix configs stay as regression rows, not
independent calibrations: Slack's enqueue-before-reaction order (WR-05) predates
the public history, so `DiscordReactFirst` is confirmed by #228 rather than by a
recoverable past commit.

- **Discord stranded a mention queued while the turn ends** (`DiscordReactFirst`;
  fixed on main by #228). Before #228, Discord awaited the ⌛ reaction before appending to `_pending`; if the running
  turn drained and released the thread during that await, the message sat in
  `_pending` until the next mention. Found first by code reading; the model
  reproduces it. Fixed on main by pymc-labs/daimon#228.
- **A continuation recorded as the turn ends waited for the next turn**
  (`FormDuringTail`; fixed on main by #233). Before #233, a credential form submitted after the running turn's tail
  dispatch but before the thread is released calls
  `dispatch_continuations_in_thread`, which skips a processing thread; nothing
  dispatches the row until another turn in that thread completes. Trace: turn
  ends → marker cleared → continuations dispatched (none yet) → form records its
  continuation → dispatch skipped (thread processing) → thread released.
  `FormRedispatch` (a skipped dispatch re-runs when the thread is released) is
  clean. Fixed on main by pymc-labs/daimon#233. As in the code, the re-run is
  spawned at release and claims the thread again after an await
  (`FormDispatch`), so it can defer again if a mention claimed the thread first.
  `FormRedispatch` matches main and does not check `NoStrandedMention`, because
  main still strands a mention queued behind the dispatch (next section).

## Open bug on main (fix in #237)

- **A mention queued behind an out-of-turn dispatch is stranded**
  (`FormDispatchNoDrain`, violates `NoStrandedMention`; fix proposed in
  pymc-labs/daimon#237, open). `dispatch_continuations_in_thread` (Slack
  `app.py`, Discord `bot.py`) adds the thread to `_processing` for the whole
  continuation turn, so a mention arriving then is appended to `_pending` and
  gets ⌛. Its `finally` only calls `_release_thread`, which discards the slot
  and re-runs a deferred dispatch but never reads `_pending`, so the mention
  waits for the next mention in that thread. Trace (7 states): form records its
  continuation → dispatch claims the thread → three mentions arrive and queue →
  dispatch releases the thread with all three still queued. The same holds for
  the #233 re-run, which goes through the same function.
  `FormDispatchDrain` (drain `_pending` before releasing, #237's shape) is
  clean with all four invariants. The config differs from `FormDispatchNoDrain`
  only in `DrainAfterDispatch`. #237's Discord path leaves the queue for the
  next turn when the dispatch raises; a raising dispatch is not modelled.

## Bounds and assumptions

- One process, one thread, three mentions from two authors, one handoff and one
  form submission. The continuation turn an out-of-turn dispatch runs is one
  step between its claim and its release; mentions can arrive in between. A drained batch's failure is not modelled; `997b3d8`'s
  per-author error isolation is outside these properties.
- Cross-process behaviour (restart, orphan sweep, MA session state) is in
  [`adapter_recovery/AdapterOverlap.tla`](../adapter_recovery/README.md).
- A passing TLC run checks only this finite abstraction.
