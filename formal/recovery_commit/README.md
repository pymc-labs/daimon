# Recovery replacement commit boundary

`RecoveryCommit` isolates the database failure boundary in dead-session
recovery. The safe configuration inserts the replacement mapping in the
transaction that marks the old mapping dead and links the replacement. The
unsafe configuration models the former helper committing the mapping in a
separate transaction.

| Model action/state | Implementation or regression assertion |
| --- | --- |
| `BeginRecovery` | [`run.py:314`](../../packages/core/daimon/core/turn/run.py#L314) opens `db.begin()`; [`run.py:315`](../../packages/core/daimon/core/turn/run.py#L315) acquires the per-thread preparation advisory lock. |
| `MarkOldDead` | [`run.py:330`](../../packages/core/daimon/core/turn/run.py#L330) calls [`thread_sessions.py:301`](../../packages/core/daimon/core/stores/thread_sessions.py#L301), flushed in that transaction. |
| `CreateUpstreamSession` | [`run.py:369`](../../packages/core/daimon/core/turn/run.py#L369) calls [`prepare.py:162`](../../packages/core/daimon/core/turn/prepare.py#L162); the Managed Agents session is external to the database transaction. |
| `InsertReplacementRow` | [`run.py:370`](../../packages/core/daimon/core/turn/run.py#L370) calls [`prepare.py:213`](../../packages/core/daimon/core/turn/prepare.py#L213) in the safe model. The unsafe model abstracts the former separately committed mapping. |
| `LinkReplacement` | [`run.py:385`](../../packages/core/daimon/core/turn/run.py#L385) calls [`thread_session_lineage.py:55`](../../packages/core/daimon/core/stores/thread_session_lineage.py#L55), flushed in the same transaction. |
| `CommitRecovery` / `AbortRecovery` | [`run.py:314`](../../packages/core/daimon/core/turn/run.py#L314) scopes commit or rollback; [`run.py:386`](../../packages/core/daimon/core/turn/run.py#L386) attempts best-effort upstream archive after an abort. |
| `recoveryAttempted` | [`run.py:526`](../../packages/core/daimon/core/turn/run.py#L526) reaches `_replace_dead_session` at most once in a turn; the model prevents a second modeled attempt after commit or abort. |
| `NoOrphanAfterRecovery` | [`test_run_prepared_turn.py:2351`](../../packages/core/tests/turn/test_run_prepared_turn.py#L2351) asserts that rollback leaves only the old row. It uses a separate engine because the shared-connection fixture would hide the independent commit. The [archive regression](../../packages/core/tests/turn/test_run_prepared_turn.py#L2443) checks the separate best-effort upstream call. |

## Bounds and assumptions

- One recovery attempt, one existing dead-session mapping and one replacement
  suffice to expose the failure. `recoveryAttempted` prevents a retry after
  abort; model booleans abstract row identity and status, and the upstream
  session has only an existence bit.
- The outer transaction and advisory lock serialize recovery and competing
  session preparation. While it is active, the invariant permits an independently
  committed row to be temporarily visible; abort must not leave it behind.
- Database writes within one transaction are atomic. `CreateUpstreamSession`
  represents an external side effect and is not rolled back if the database
  transaction aborts. The `upstreamSession` bit records creation, not whether
  the session was later archived. The implementation attempts a best-effort
  archive after rollback; this model omits its success, failure, timeout and
  retry, and checks the mapping row rather than external cleanup.
- No fairness is assumed and no liveness claim is made. Cancellation is
  represented by `AbortRecovery` at each reachable transaction stage.
- A passing TLC run validates only this finite transition abstraction. The
  Postgres regression checks the Python transaction boundary but does not prove
  all database schedules or external API behavior.

## TLC evidence

With TLC 2.19 and one worker, `UnsafeInnerCommit` violates
`NoOrphanAfterRecovery` in 9 distinct states. Its counterexample is:

`BeginRecovery` → `MarkOldDead` (uncommitted) → `CreateUpstreamSession` →
`InsertReplacementRow` (separate commit) → `AbortRecovery`.

The rollback restores the old row as live and unlinked while the replacement
mapping remains live. `RecoveryCommit` passes in 9 distinct states: the row,
dead mark, and link are all staged until `CommitRecovery`, so the same abort
leaves no replacement row.
