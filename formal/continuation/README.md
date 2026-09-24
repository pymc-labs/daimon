# Continuation dispatch model

Run from the repository root with a JRE and TLA+ tools jar:

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
TLC_META_DIR="${TMPDIR:-/tmp}/daimon-continuation-tlc"
mkdir -p "$TLC_META_DIR/safety" "$TLC_META_DIR/crash-progress" "$TLC_META_DIR/crash-free-progress" "$TLC_META_DIR/recovery"
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/safety" -config formal/continuation/ContinuationDispatch.cfg formal/continuation/ContinuationDispatch.tla
# This expected counterexample shows a claim stranded by process death (TLC exit 13).
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/crash-progress" -config formal/continuation/ContinuationCrashProgress.cfg formal/continuation/ContinuationDispatch.tla || test "$?" -eq 13
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/crash-free-progress" -config formal/continuation/ContinuationProgress.cfg formal/continuation/ContinuationDispatch.tla
# This expected invariant violation shows why blindly retrying old claims can duplicate a turn (TLC exit 12).
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/recovery" -config formal/continuation/ContinuationRecovery.cfg formal/continuation/ContinuationDispatch.tla || test "$?" -eq 12
```

`ContinuationDispatch.tla` models one durable request and one adapter process.
It bounds execution to two claims, enough for one hypothetical stale-claim
retry. `ExternalEffect` abstracts a follow-up turn being initiated and its
platform-visible/billable effects. The model splits that from `SettleDelivered`
because the code performs the turn and then writes the status in separate
operations. `Crash` can happen at any point after the claim; `Restart` models a
new process. In the current implementation, a new process lists pending rows
only, so a claimed row is not resumed. `RecoverClaimed` is a hypothetical
transition used only to test a blanket stale-claim retry policy.

| Model item | Implementation |
| --- | --- |
| `Claim` | Conditional `pending` → `claimed` update in [`task_continuations.py`](../../packages/core/daimon/core/stores/task_continuations.py:67) |
| `ExternalEffect` and settlement | `run_follow_up` followed by delivered/skipped settlement in [Discord dispatch](../../packages/adapters/discord/daimon/adapters/discord/continuation_dispatch.py:98) and [Slack dispatch](../../packages/adapters/slack/daimon/adapters/slack/continuation_dispatch.py:120) |
| Pending-only restart lookup | `list_pending_continuations` filters on `status == "pending"` in [`task_continuations.py`](../../packages/core/daimon/core/stores/task_continuations.py:135) |
| `Crash` / `Restart` | Process failure/restart is an environment abstraction; no recovery transition for claimed rows exists in production code. |

## Runtime regression coverage

The real-Postgres dispatcher tests inject process death at the follow-up
callback boundary, before or after recording an observable effect:
[`Discord`](../../packages/adapters/discord/tests/test_continuation_dispatch.py)
and [`Slack`](../../packages/adapters/slack/tests/test_continuation_dispatch.py).
They assert that the committed claim remains `claimed` and a later dispatch
does not retry it. The effect is deliberately injected; these tests verify
the dispatcher/status boundary and do not establish atomic exactly-once
behavior across MA, billing, or platform APIs.

## Bounds and assumptions

- One continuation and one active dispatcher are enough to show permanent
  stranding and the duplicate-effect tradeoff. Two adapters share this core
  status protocol; platform API details are abstracted into one external
  effect.
- `Claim` abstracts the committed conditional SQL update. Database isolation,
  transient database errors, multiple rows, and simultaneous claim races are
  excluded here; the real-Postgres race is covered by the store tests.
- One external effect per in-memory attempt is assumed. It may complete before
  settlement, and a process can die on either side of it. The model does not
  claim to know whether an upstream operation succeeded when its caller dies.
- Crash-free progress assumes weak fairness for claim, external effect, and
  settlement. Real network calls can hang or fail, and continuous process
  failure violates that assumption.
- TLC checks this finite abstraction, not Python, SQLAlchemy, PostgreSQL, MA,
  or Discord/Slack behavior.

## Findings

The safety run passes: with no recovery transition, this model permits at most
one claim/effect and preserves terminal-state consistency. Crash-free progress
also passes under the stated fairness assumption. The crash-progress config
violates `PendingEventuallySettles` with this shortest trace:

1. `pending`, no effect.
2. `Claim` commits `claimed`.
3. `Crash` leaves the row `claimed`, before any follow-up effect.
4. A restarted process cannot list the row, and the behavior stutters.

A crash after `ExternalEffect` but before settlement strands the row in the
same way, with the effect already visible or billed. The recovery config
demonstrates the other side: resetting an old claim to pending after an effect,
then claiming it again, violates `AtMostOneExternalEffect`. `claimed_at` gives
the age of a claim but cannot reveal which side of the external-effect boundary
the crashed process reached.

This is an at-most-once versus eventual-delivery product tradeoff, not a safe
local retry fix. Keeping the current policy avoids duplicate billed turns but
can lose a promised follow-up after a crash. Retrying old claims improves
delivery before the effect but can duplicate a turn after an ambiguous send.
Resolving both requires a durable idempotency key honored by the downstream
turn/post boundary, or an explicit product choice about which failure to favor.
No production transition was changed pending that choice.

## TLC evidence

Checked 2026-09-24 with TLC 2.19 and Java 21. Safety: 9 distinct states,
no errors. Crash-free progress: 5 distinct states, no errors. Crash progress:
expected `PendingEventuallySettles` counterexample above (9 distinct states).
Hypothetical recovery: expected `AtMostOneExternalEffect` violation after two
claims/effects (17 distinct states). These results establish only the stated
finite transition behavior.
