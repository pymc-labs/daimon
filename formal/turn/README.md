# Turn reducer and render model

Run from the repository root with a JRE and the TLA+ tools jar (setup in the
[coverage report](../README.md)):

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
TLC_META_DIR="${TMPDIR:-/tmp}/daimon-turn-tlc"
mkdir -p "$TLC_META_DIR/terminal" "$TLC_META_DIR/replay" "$TLC_META_DIR/bounded" "$TLC_META_DIR/progress"
# The terminal configuration intentionally allows impossible post-terminal
# delivery (TLC exit 12); the replay model includes the merge fix.
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/terminal" -config formal/turn/TurnLifecycle.cfg formal/turn/TurnLifecycle.tla || test "$?" -eq 12
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/replay" -config formal/turn/TurnLifecycleReplay.cfg formal/turn/TurnLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/bounded" -config formal/turn/TurnLifecycleBounded.cfg formal/turn/TurnLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/progress" -config formal/turn/TurnProgress.cfg formal/turn/TurnProgress.tla
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
| Reconnect replay folds the current-turn suffix onto accumulated state, using reducer event-ID dedup | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:769) |
| Replay boundary scan anchors on the latest `user.message`, applies posture-specific prior-idle boundaries, and truncates at this turn's first terminal event | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:648) |
| SSE lifecycle hook is deduplicated across reconnects before invocation | [`driver.py`](../../packages/core/daimon/core/turn/driver.py:842) |

## TLC checks and interpretation

`TurnLifecycle.cfg` keeps the original adversarial terminal ordering enabled.
It checks `TypeOK`, `TerminalExclusive`, and `RenderAnchorBound`; TLC fails
`TerminalExclusive` with this trace:

1. `FoldIdle`: `stopReason=TRUE`, `turnError=FALSE`.
2. `FoldTerminated`: `stopReason=TRUE`, `turnError=TRUE`.

This ordering is unreachable through the turn driver's live consume loop:
`_consume_with_reconnect` returns as soon as it folds either a
`session.status_idle` event or a `session.status_terminated` event, so it cannot
fold the second terminal event from that stream. Replay scopes history to the
current `user.message`, selects only earlier idles as prior-turn boundaries,
and truncates at this turn's first terminal event. The executable regression
`test_terminated_replay_keeps_answer_after_current_turn_idle` includes an older
completed turn and confirms the current response survives a termination event
following its idle under both confirmation postures. This is a source-level
guarantee, not an asserted Managed Agents guarantee.
The model's adversarial switch explicitly permits the driver's terminal guard
to be bypassed so the original counterexample remains reproducible. The model
does not claim that `TurnState.error` and `stop_reason` are universally
exclusive: a nonterminal `session.error` can be followed by an idle event.

`TurnLifecycleReplay.cfg` keeps the stale-replay environment enabled. Before
the fix, TLC failed the anchor bound with this trace:

1. `FoldMessage`: revision 1.
2. `RenderTick`: anchor 1.
3. `ReplayStalePrefix`: revision resets to 0 while anchor remains 1.

The replay helper walks the SDK's paginated events-list endpoint, but neither
the SDK's list documentation nor the paginator contract promises a consistent
snapshot across pages. Fault injection therefore treats omission of an already
folded event as possible. The driver now folds replay events onto the current
`TurnState`, using `seen_event_ids` to deduplicate overlap, instead of replacing
that state with a fresh fold. This preserves already-rendered content if a
replay page is incomplete. The executable regression
`test_incomplete_replay_does_not_erase_already_folded_content` injects an empty
replay after live content and verifies the later terminal result retains it.
The model's stale replay action now represents this merge behavior and preserves
the existing fold state.

For the historical counterexample configurations,
`AllowConflictingTerminalEvents` and `AllowStaleReplay` are enabled so TLC can
explore the behaviors in question. `TurnLifecycleBounded.cfg` disables both
adversarial conditions. After the merge fix, stale replay preserves the state
even when enabled; terminal exclusivity still depends on the source guarantee
that consumption stops at the first terminal status event. TLC checks
`TypeOK`, `TerminalExclusive`, and `RenderAnchorBound` under these finite bounds.
These checks do not establish completeness of every possible SDK response or
prove the Python implementation correct.

`TurnProgress.tla` checks a separate liveness abstraction. It has exactly two
data deliveries followed by one terminal delivery. A single cancellation window
allows either one cancel request or its closure. Weak fairness is assumed for
each continuously enabled data and terminal delivery, cancellation-window
resolution, successful render when it remains enabled, and finalization. TLC
checks that the turn eventually finalizes and that periodic rendering either
succeeds or is cancelled. It explores 57 distinct states and reports no error.
These finite delivery and fairness assumptions are explicit model inputs, not
claims about unbounded real-time guarantees.

The replay boundary selection is reviewed in source but not implemented as an
event-list model here. `_events_since_last_turn_boundary` anchors to the latest
`user.message`, treats `requires_action` idles as mid-turn only in
`AutoApprove`, and stops at the current turn's first terminal event. If an
incomplete replay omits every `user.message`, attribution is ambiguous; the
legacy idle-boundary fallback remains and already-folded in-memory content is
still preserved by merging. If the replay also omits current-turn content that
was never received live, its turn membership cannot be recovered reliably and
that content may be absent from the finalized state. TLC's event abstraction
does not include multi-turn session histories, user-message anchors, terminal
stop-reason variants, confirmation sends, or the session-status query that gates
replay.

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
