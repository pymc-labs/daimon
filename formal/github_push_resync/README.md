# GitHub push resync delivery queue

`PushResync` bounds two signed pushes to one repository/ref, two scheduler
workers, and three external writes. It checks the durable acknowledgement and
generation fence introduced with the PostgreSQL push queue. The old webhook
returned 200 before its background resync began, with no durable job; the `UnsafeAck`
configuration keeps that two-state counterexample. `UnsafeComplete` removes
the generation check to show a stale S1 pass marking a newer S2 push done.

| Model action or variable | Code and executable check |
| --- | --- |
| `Receive`, `acknowledged`, `generation` | The signed [webhook](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py#L125) awaits [receipt and queue enqueue](../../packages/core/daimon/core/stores/github_push_resync.py#L18) in one transaction before 200. [Webhook tests](../../packages/adapters/mcp/tests/test_webhooks_github.py#L178) check committed work, duplicate delivery and a non-200 database failure. `UnsafeAck` abstracts the former response with only volatile background work and no durable queue row. |
| `Claim`, `owner`, `claimGeneration` | [Store claim](../../packages/core/daimon/core/stores/github_push_resync.py#L87) uses a row lock with `SKIP LOCKED`, records the claimed generation and a lease owner. [PostgreSQL tests](../../packages/core/tests/stores/test_github_push_resync.py#L47) check owner takeover after expiration. |
| `AcquireRepoLock` | [Queue worker](../../packages/core/daimon/core/skill_sync/resync_queue.py#L25) obtains a session advisory lock for the repo/ref before the batch. The model treats this as exclusive while a pass runs. |
| `SyncCurrentBranch`, `externalVersion`, `externalWrites` | [Binding resync](../../packages/core/daimon/core/skill_sync/resync.py#L268) fetches current branch contents and may repeat a completed Managed Agents effect after a crash. The [mid-batch death regression](../../packages/core/tests/skill_sync/test_resync_queue.py#L95) asserts the repeated write. |
| `Complete` | [Store completion](../../packages/core/daimon/core/stores/github_push_resync.py#L144) marks `done` only for the current generation and lease owner; otherwise it leaves newer work pending. [Stalled S1/S2 regression](../../packages/core/tests/skill_sync/test_resync_queue.py#L161) checks convergence. |
| `CrashAndExpire` | A process death releases the session lock; [claim](../../packages/core/daimon/core/stores/github_push_resync.py#L87) can take the job after its lease expires. The model combines death and expiration in one action; it does not model the two-minute delay. |

## Bounds and assumptions

- One repo/ref, two ordered branch versions, two workers and at most three
  external writes. Delivery IDs are abstracted to generations. The real
  [receipt store](../../packages/core/daimon/core/stores/github_push_resync.py#L18)
  and [PostgreSQL test](../../packages/core/tests/stores/test_github_push_resync.py#L12)
  cover duplicate delivery IDs and coalescing.
- Enqueue and HTTP acknowledgement are one abstract action only in the safe
  configuration. This assumes a successfully committed database transaction;
  a pre-commit failure returns non-200, and GitHub does not automatically
  redeliver it. Operator redelivery or reconciliation is required.
- `SyncCurrentBranch` abstracts a successful batch as one external write of
  the branch version at that moment. Per-binding failures and backoff are
  executable checks and [queue documentation](../../docs/github-push-resync.md),
  outside this state space. A continuously failing provider does not satisfy
  the progress assumption.
- `CrashAndExpire` releases the advisory lock. The model excludes a database
  connection loss while an external call remains in flight; that can permit
  overlapping external effects and late stale writes, as the queue docs say.
  It does not claim strict external ordering or exactly-once effects.
- The progress configuration removes crashes, assumes both pushes eventually
  occur, and applies weak fairness to claim, lock, sync and completion. It
  checks convergence after finite arrivals and successful external calls.
  The safety configuration permits crashes and checks no progress claim.

## TLC evidence

Run one configuration from this directory with:

```sh
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config PushResyncSafe.cfg PushResync.tla
```

Or run every repository configuration with `formal/check.sh`. TLC 2.19 with
one worker finds:

| Configuration | Distinct states | Result |
| --- | ---: | --- |
| `PushResyncSafe` | 297 | `AckHasDurableGeneration` and `DoneHasCurrentBranch` hold. |
| `PushResyncEarlyAck` | 2 | `Receive` acknowledges version 1 while generation remains 0. |
| `PushResyncStaleComplete` | 29 | S1 syncs version 1, S2 arrives, then S1 marks version 2 done with external version 1. |
| `PushResyncCrashDuplicate` | 111 | S1 writes, dies before completion, then a recovered pass writes again. This is the documented at-least-once cost. |
| `PushResyncProgress` | 55 | Under the stated fairness and crash-free bound, two arrivals eventually leave version 2 done and externally current. |

These finite traces validate the abstraction and source correspondence within
the stated bounds. They do not prove the Python, PostgreSQL, GitHub or Managed
Agents implementations.
