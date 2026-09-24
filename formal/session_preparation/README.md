# Per-thread session preparation

Run from the repository root after setting `TLA2TOOLS_JAR` as described in
[`formal/README.md`](../README.md). Java must be available on `PATH`.

```sh
mkdir -p "${TMPDIR:-/tmp}/daimon-tlc-session-preparation-safety"
mkdir -p "${TMPDIR:-/tmp}/daimon-tlc-session-preparation-progress"
java -jar "$TLA2TOOLS_JAR" \
  -metadir "${TMPDIR:-/tmp}/daimon-tlc-session-preparation-safety" \
  -config formal/session_preparation/SessionPreparation.cfg \
  formal/session_preparation/SessionPreparation.tla
java -jar "$TLA2TOOLS_JAR" \
  -metadir "${TMPDIR:-/tmp}/daimon-tlc-session-preparation-progress" \
  -config formal/session_preparation/SessionPreparationProgress.cfg \
  formal/session_preparation/SessionPreparation.tla
```

## Implementation mapping

| Model | Implementation |
| --- | --- |
| `owner`, `Start`, `Acquire` | `packages/core/daimon/core/session_preparation_stages.py::lock_preparation`, called before reading the live mapping in `session_preparation.py::prepare_session_for_turn`; the transaction-scoped advisory lock is held through the function's decision and return. |
| `Reuse`, `Update`, `BeginReplacement` | `prepare_session_for_turn`: compare `recorded` and `desired`, choose `ReuseAsIs`, `UpdateInPlace`, or `ReplaceSession`; a waiter re-reads the live row after taking the lock and re-evaluates. |
| `Defer`, `BusyHandoff`, `EndTurn` | `turn_is_active` and the active-turn branches in `prepare_session_for_turn`. Active status expires at `TURN_CEILING_S`; handoff identity replacement returns busy without running the incoming turn on the outgoing session. |
| `stage`, `attempts`, `Checkpoint`, `Upload`, `CreateSuccessor`, `Fail`, `Retry` | `_run_replacement` and `stores/session_preparations.py::{upsert_preparation,advance_stage,fail_preparation}`. Durable stages are `decided`, `checkpointed`, `uploaded`, `created`, `completed`, `failed`; retry identity is `(mapping_id, target_fingerprint)`. |
| `successor`, `CloseOut`, `oldLive` | `_run_replacement` commits the successor before `_close_out` marks the old row superseded/retired, then records `completed`. `_heal_lineage` repairs a crash in the interval. |

Source tests exercising the modeled behaviors are in
`packages/core/tests/test_session_preparation.py` (active deferral, active
handoff busy, failed replacement/backoff, crash healing, and concurrent
preparations) and `packages/core/tests/stores/test_session_preparations.py`
(same-target uniqueness, retry count, durable stages and preserved transfer).

## Bounds and assumptions

- Two callers contend for one `(tenant, platform, thread, account)` lock and
  one initial live mapping. This covers the lock race checked by the existing
  concurrent preparation test. Each call's compatibility result is abstracted
  as reuse, in-place update, replacement, or responder handoff. A waiter
  recomputes after acquiring the lock; after a completed replacement, the same
  target is compatible and reuses the successor.
- The database transaction lock is abstracted as one exclusive owner. Python
  scheduling, PostgreSQL lock queue order, transaction isolation, hash
  collisions, connection loss, and deadlock detection are not modeled.
- The durable preparation row is represented by one stage and a bounded
  attempt counter (four claims maximum across two callers, with retry claims
  allowed only below the bound). A failed attempt preserves the old live
  mapping; the production exponential backoff clock is abstracted as an
  enabled retry. Stage writes and DB commits are atomic steps.
- Checkpoint and upload are represented as optional replacement stages in a
  single monotonically progressing pipeline. The model permits failure before
  each stage and after upload, but abstracts the transfer hook's idempotency,
  billed-work semantics, file contents, and upstream MA outcomes.
- Successor creation and old-row closeout are distinct atomic actions. A
  temporary state with both rows live is permitted between them, as it is in
  the implementation. The current mapping lookup's newest-row ordering and
  `_heal_lineage` repair are represented only by allowing closeout to finish;
  crashes and restart scheduling are not modeled.
- Active-turn status is sampled by each caller at start and stays fixed until
  `EndTurn`. The model checks safe deferral/busy outcomes, but not clock
  arithmetic, abandoned-marker expiry, or races between marker writes and
  upstream turn completion.
- Conditional progress assumes weak fairness for lock acquisition, enabled
  preparation actions, stage progression, and turn completion, with at most
  two calls and no unbounded stream of new lock contenders. Infinite external
  failure, a caller that never starts, process death, and upstream operations
  that never return are outside this liveness claim.
- No Python implementation is proved by TLC. These checks validate the stated
  finite abstraction only; executable tests remain the implementation oracle.

## Results

Both configurations pass with no counterexample in the final abstraction.
The safety run checked `TypeOK`, exclusive lock ownership, the four-claim bound,
preservation of the old mapping on failure, completed successor consistency,
and the active-turn deferral/busy outcome constraints: 7,329 states generated,
3,532 distinct states, depth 18. The fair progress run checked
`AllStartedEventuallyReturn` under the assumptions above: 7,329 states
generated, 3,532 distinct states, depth 18 (7,064 total distinct states in
temporal checking). No source-backed defect or counterexample trace was found.
Early TLC counterexamples were model artifacts (unbounded late arrivals and
retry counts); the stated final bounds exclude those paths.
