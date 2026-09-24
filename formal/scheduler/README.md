# Routine scheduler model

Run TLC from the repository root:

```sh
/tmp/daimon-tla-tools/jdk-21.0.12.1+1-jre/bin/java -jar /tmp/daimon-tla-tools/tla2tools.jar -metadir /tmp/daimon-tla-tools/scheduler-states -config formal/scheduler/RoutineScheduler.cfg formal/scheduler/RoutineScheduler.tla
/tmp/daimon-tla-tools/jdk-21.0.12.1+1-jre/bin/java -jar /tmp/daimon-tla-tools/tla2tools.jar -metadir /tmp/daimon-tla-tools/scheduler-progress-states -config formal/scheduler/RoutineSchedulerProgress.cfg formal/scheduler/RoutineScheduler.tla
```

The finite model checks one routine, two scheduler processes contending for one
session-scoped advisory lock, and two consecutive recurrence slots. `Acquire`
and `Release` abstract `_acquire_advisory_lock` and the `finally` unlock in
`packages/adapters/scheduler/daimon/adapters/scheduler/main.py` (lock acquisition
around lines 419–425; release around 496–504). A process can claim only while it
owns the lock.

`Claim` abstracts `claim_due_fireable` in
`packages/core/daimon/core/stores/routines.py` (328–388) and its caller in
`packages/core/daimon/core/scheduler.py` (65–75). SQL `FOR UPDATE SKIP LOCKED`,
the null-out/`last_fired_at` update, and per-row recomputation are one transaction;
the model treats their committed effect as one step. `recomputeOk = FALSE` models
the caught per-row recomputation error leaving `next_fire_at` NULL. `RecoverNull`
abstracts `advance_stale` (391–426) on a later tick.

`DispatchSuccess`, `DispatchError`, and `DispatchErrorRecordLost` abstract the
guarded fire and result write in `packages/core/daimon/core/scheduler.py`
(107–123). Failure is a terminal attempt for the claimed occurrence; there is no
immediate retry in this code path. A later cron occurrence is modeled by
`NextOccurrence`. The two error actions cover recorded dispatch failure and the
swallowed failure to persist the error result.

## Bounds and assumptions

- One routine and two schedulers are enough to expose same-row competing claims;
  independent rows, batch limits, ordering, caps, and semaphore capacity are
  abstracted away.
- Two recurrence slots allow checking that a later retry is a distinct
  occurrence. Cron arithmetic and wall-clock passage are abstracted as
  `NextOccurrence`.
- Claim and result operations are modeled at transaction/await boundaries. SQL
  isolation, database crashes, network ambiguity after a commit, process death,
  and lease expiry are excluded. In particular, this does not establish
  exactly-once external side effects under a crash between claim commit and
  dispatch.
- No fairness or liveness condition is asserted: TLC checks state invariants,
  while whether a tick or fire eventually runs depends on scheduling, upstream
  responsiveness, and database availability.

The progress configuration assumes weak fairness for acquire, release, claim,
dispatch, and the next recurrence. Under that scheduling assumption and the
exclusion of process death, it checks that pending work completes and that the
first occurrence advances to the next. This does not guarantee upstream calls
or database operations complete in the real system.

The checked safety properties are: one dispatch attempt per occurrence, only a
claimed occurrence can finish, consistent terminal results, and claim ownership
by the lock holder. This is a bounded abstraction of the implementation, not a
proof of Python, SQLAlchemy, PostgreSQL, or external turn execution.
