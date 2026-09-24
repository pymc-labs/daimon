# Turn reducer and render model

Run with a JRE and the TLA+ tools jar:

```sh
java -jar "$TLA2TOOLS_JAR" -config formal/turn/TurnLifecycle.cfg formal/turn/TurnLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -config formal/turn/TurnLifecycleReplay.cfg formal/turn/TurnLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -config formal/turn/TurnLifecycleBounded.cfg formal/turn/TurnLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -config formal/turn/TurnProgress.cfg formal/turn/TurnProgress.tla
```

Run TLC commands sequentially in one checkout: TLC creates timestamp-named
state directories and concurrent runs can collide.

The turn model is a finite abstraction of the core fold and render anchor.
Each event ID is unique in the finite set; deliveries are otherwise unordered,
and a duplicate ID is a no-op. A message adds one abstract text chunk. A tool
use appends one pending block; a result completes it only when the use has
already arrived. An orphan result is remembered as seen, matching the reducer.
The render anchor advances on successful non-empty renders; failures leave it
unchanged for retry, and cancellation stops further ticks.

| Model item | Implementation |
| --- | --- |
| Event ID dedup and fold order | [`reducers.py`](../../packages/core/daimon/core/turn/reducers.py:38) |
| Message append, tool append, result pairing/orphan behavior | [`reducers.py`](../../packages/core/daimon/core/turn/reducers.py:87) |
| Independent idle and error/terminated fields | [`reducers.py`](../../packages/core/daimon/core/turn/reducers.py:61) |
| Render anchor advances only after successful adapter call | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:389) |
| Timed render task and cancellation | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:420) |
| Guarded final render and terminal callback | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:997) |
| Snapshot diff append-only assumption | [`render.py`](../../packages/core/daimon/core/turn/render.py:84) |
| Reconnect replay replaces folded state | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:759) |
| Replay boundary scan skips final event and applies posture-specific idle boundary | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:650) |
| SSE lifecycle hook is deduplicated across reconnects before invocation | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:842) |

## TLC checks and interpretation

`TurnLifecycle.cfg` checks `TypeOK`, `TerminalExclusive`, and `RenderAnchorBound`.
TLC fails `TerminalExclusive` with this trace:

1. `FoldIdle`: `stopReason=TRUE`, `turnError=FALSE`.
2. `FoldTerminated`: `stopReason=TRUE`, `turnError=TRUE`.

The reducer does permit those independent field assignments if both event kinds
occur in one fold. This does not establish a production defect: the model makes
all event kinds arbitrarily reorderable, while Managed Agents may guarantee one
terminal event for a turn. Verify that upstream contract before treating the
trace as reachable. The current model deliberately leaves this safety property
failing so the assumption stays visible.

`TurnLifecycleReplay.cfg` checks `TypeOK` and `RenderAnchorBound`. TLC fails the
anchor bound with this trace:

1. `FoldMessage`: revision 1.
2. `RenderTick`: anchor 1.
3. `ReplayStalePrefix`: revision resets to 0 while anchor remains 1.

The source does replace `state_cell` with a fresh fold on reconnect, while the
render anchor is separate. This trace requires replay to omit an already-seen
event. The driver expects `replay_events` to return authoritative history, so
stale replay is an environment assumption that this model cannot validate. If
that assumption fails, `diff` has no deletion/truncation representation and
can fail to reconcile the adapter's previously rendered output with the replayed
state.

For each expected counterexample configuration, `AllowConflictingTerminalEvents`
and `AllowStaleReplay` are enabled so TLC can explore the behavior in question.
`TurnLifecycleBounded.cfg` disables both environment actions. Under those
assumptions, TLC checks `TypeOK`, `TerminalExclusive`, and `RenderAnchorBound`
over 3,042 distinct states and reports no error. This is conditional evidence:
the bounds exclude conflicting terminal events and stale reconnect replay.

`TurnProgress.tla` checks a separate liveness abstraction. It has exactly two
data deliveries followed by one terminal delivery. A single cancellation window
allows either one cancel request or its closure. Weak fairness is assumed for
each continuously enabled data and terminal delivery, cancellation-window
resolution, successful render when it remains enabled, and finalization. TLC
checks that the turn eventually finalizes and that periodic rendering either
succeeds or is cancelled. It explores 57 distinct states and reports no error.
These finite delivery and fairness assumptions are explicit model inputs, not
claims about unbounded real-time guarantees.

The idle-boundary heuristic is reviewed in source but not implemented as an
event-list model here. `_events_since_last_turn_boundary` scans all but the
last event, excludes `requires_action` idles only in `AutoApprove`, then folds
the suffix. This preserves the current turn's own idle on eventless finalization
and prevents prior-turn content from leaking through reconnect. TLC's event
abstraction does not include multi-turn session histories, terminal stop-reason
variants, confirmation sends, or the session-status query that gates replay.

Other deliberate bounds: only two message IDs, one tool ID/result, boolean
terminal payloads, one render result per revision, and no wall clock. It checks
the reducer/render transition abstraction, not Python execution, asyncio
scheduling, adapter effects, SDK ordering guarantees, billing, reconnect retry
budgets, eventless status gating, or cancellation races during stream setup.
The reducer/render safety model has no fairness assumption and makes no liveness
claim; its safety failures are reachable-state results. The separate progress
model states its fairness assumptions above. TLC model checking does not prove
the implementation correct.

The reducer/render model does not include adapter callbacks. In particular,
`driver.py` invokes `on_sse_event` before folding an event, so reducer dedup alone
does not suppress duplicate callback effects. The driver now tracks event IDs
delivered to this hook across reconnects and skips repeated callbacks. The
associated regression is in `packages/core/tests/turn/test_driver_hooks.py`;
the model treats callback delivery as outside its checked boundary.
