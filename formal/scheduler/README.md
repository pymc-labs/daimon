# Routine scheduler model

Set `TLA2TOOLS_JAR` to a local TLC 2.19+ `tla2tools.jar` path, then run from
the repository root. This uses `java` from `PATH` and writes TLC state files
under the temporary directory:

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
TLC_META_DIR="${TMPDIR:-/tmp}/daimon-scheduler-tlc"
mkdir -p "$TLC_META_DIR/safety" "$TLC_META_DIR/progress"
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/safety" -config formal/scheduler/RoutineScheduler.cfg formal/scheduler/RoutineScheduler.tla
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/progress" -config formal/scheduler/RoutineSchedulerProgress.cfg formal/scheduler/RoutineScheduler.tla
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
swallowed failure to persist the error result. `ProcessDiesAfterClaim` abstracts
the scheduler process exiting after the claim transaction commits but before
the fire starts. PostgreSQL releases the process's session-scoped advisory lock;
the already-advanced `next_fire_at` means the next scheduler does not reclaim
that occurrence.

## Bounds and assumptions

- One routine and two schedulers are enough to expose same-row competing claims;
  independent rows, batch limits, ordering, caps, and semaphore capacity are
  abstracted away.
- Two recurrence slots allow checking that a later retry is a distinct
  occurrence. Cron arithmetic and wall-clock passage are abstracted as
  `NextOccurrence`.
- Claim and result operations are modeled at transaction/await boundaries. SQL
  isolation, database crashes, network ambiguity after a commit, and process
  death outside the modeled window are abstracted; there is no lease in the
  current implementation. The model includes process death in the specific
  window after claim commit and before dispatch. It confirms that this
  occurrence is skipped while the next occurrence remains schedulable. This
  matches the documented no-catch-up behavior: each slot is attempted at most
  once, and a missed slot is not queued. Retrying that slot after an ambiguous
  later crash could repeat an external Managed Agents turn and its billable
  work, so adding recovery would require durable attempt state and an explicit
  idempotency or delivery policy. The current schema has no such state. This
  at-most-once choice can lose an occurrence if the process dies after claim
  commit and before the turn begins; it does not guarantee that every scheduled
  slot is attempted.
- Safety checking uses no fairness assumptions. TLC checks the declared state
  invariants over all reachable states in the finite model.

The progress configuration assumes weak fairness for acquire, release, claim,
dispatch, and the next recurrence. `PendingEventuallyCompletes` checks that a
pending occurrence eventually reaches the model's terminal state, either by
completion or by the modeled process death; `FirstOccurrenceAdvances` checks
that the first occurrence advances to the next. This does not guarantee
upstream calls or database operations complete in the real system.

## Local TLC evidence

Checked on 2026-09-24 with TLC 2.19 and the local Java 21 runtime. Exact local
commands:

```sh
/tmp/daimon-tla-tools/jdk-21.0.12.1+1-jre/bin/java -jar /tmp/daimon-tla-tools/tla2tools.jar -metadir /tmp/daimon-tla-tools/scheduler-states -config formal/scheduler/RoutineScheduler.cfg formal/scheduler/RoutineScheduler.tla
/tmp/daimon-tla-tools/jdk-21.0.12.1+1-jre/bin/java -jar /tmp/daimon-tla-tools/tla2tools.jar -metadir /tmp/daimon-tla-tools/scheduler-progress-states -config formal/scheduler/RoutineSchedulerProgress.cfg formal/scheduler/RoutineScheduler.tla
```

Both runs completed with no error: 257 states generated, 110 distinct states,
depth 9. The safety run checked the invariants; the progress run also checked
both temporal properties. The local paths above are evidence of that run, not
requirements for using the portable command.

The checked safety properties are: one dispatch attempt per occurrence, only a
claimed occurrence can finish, consistent terminal results, and claim ownership
by the lock holder. This is a bounded abstraction of the implementation, not a
proof of Python, SQLAlchemy, PostgreSQL, or external turn execution.
