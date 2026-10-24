# Upstream session cleanup when `create_ma_session` fails after `sessions.create`

`SessionCreateCleanup` asks one question: can a turn leave a live upstream
Managed Agents session that no row names, and can a cleanup archive a session
that a row names or that another turn owns?

Before the fix, a cancel (turn Stop, the `asyncio.wait_for` ceiling) or a
database error in the personal-memory lookup that follows `sessions.create`
unwound with no `CreatedSession` ever reaching the caller. `create_session` had
already returned, so it no longer cleaned up, and the caller held nothing to
clean. The upstream session stayed live with no row naming it.

Ownership of one session, as modelled:

| From | Until | Owner |
| --- | --- | --- |
| `sessions.create` accepted | `create_session` returns | `create_session` (archives on any failure; assumed correct, not modelled) |
| `create_session` returns | `create_ma_session` returns `CreatedSession` | `create_ma_session` (fixed: archives on any failure; a session returned by a replayed recorded create is archived only if no `thread_sessions` row names it) |
| `CreatedSession` returned | row committed | the caller (recovery, native worker; `create_fresh_session` has no cleanup) |
| row committed | | the row; nobody archives it |
| adopted from another turn | | that turn's row; this turn never archives it |

## Action to code

Line numbers are for the fixed code.

| Model action | Code |
| --- | --- |
| `StartCreate` | callers of `create_ma_session`: [`run.py:717`](../../packages/core/daimon/core/turn/run.py#L717) (dead-session recovery), [`prepare.py:899`](../../packages/core/daimon/core/turn/prepare.py#L899) (`create_fresh_session`), [`prepare.py:1569`](../../packages/core/daimon/core/turn/prepare.py#L1569) (native worker) |
| `StartAdopt` | [`run.py:641`](../../packages/core/daimon/core/turn/run.py#L641) finds another turn's live row and returns it at [`run.py:674`](../../packages/core/daimon/core/turn/run.py#L674) without calling `create_ma_session` |
| `Create` | [`prepare.py:685`](../../packages/core/daimon/core/turn/prepare.py#L685) `create(...)` returns through [`sessions.py:1313`](../../packages/core/daimon/core/sessions.py#L1313) `return created`; the first await after it is the lookup, so the return and the start of the lookup are one action |
| `FailBeforeCreate` | `create_session` archives its own session at [`sessions.py:1314`](../../packages/core/daimon/core/sessions.py#L1314) (native and app-mode paths) and the vault at [`sessions.py:1339`](../../packages/core/daimon/core/sessions.py#L1339); a `SkillUnavailable` retry loops at [`prepare.py:683`](../../packages/core/daimon/core/turn/prepare.py#L683) |
| `LookupDone` | [`prepare.py:721`](../../packages/core/daimon/core/turn/prepare.py#L721) `get_personal_memory_store_id` completes |
| `CloseDone` | [`prepare.py:721`](../../packages/core/daimon/core/turn/prepare.py#L721) `async with deps.sessionmaker()` exit awaits the connection close; the snapshot build and `return CreatedSession` at [`prepare.py:768`](../../packages/core/daimon/core/turn/prepare.py#L768) have no await |
| `FailInWindow` | fixed: the `except BaseException` in `create_ma_session` calls `_archive_orphaned_session` ([`run.py:476`](../../packages/core/daimon/core/turn/run.py#L476), shielded, fixed 5 s deadline). `allocated_here` is set inside the call passed to `persistence.mutate`; when it is false the session was replayed and a `thread_ids_for_sessions` read must show no row names it, otherwise (row found, or read failed) nothing is archived and the skip is logged |
| `Insert`, `Commit` | recovery: [`run.py:720`](../../packages/core/daimon/core/turn/run.py#L720) `insert_mapping` inside `db.begin()` ([`run.py:612`](../../packages/core/daimon/core/turn/run.py#L612)), committed at the `async with` exit; `create_fresh_session`: [`prepare.py:908`](../../packages/core/daimon/core/turn/prepare.py#L908) and `session.commit()`; native worker: [`prepare.py:1592`](../../packages/core/daimon/core/turn/prepare.py#L1592) |
| `CallerFail` | recovery archives at [`run.py:744`](../../packages/core/daimon/core/turn/run.py#L744) when `created is not None`; the native worker archives only the span before its insert ([`prepare.py:1580`](../../packages/core/daimon/core/turn/prepare.py#L1580)); `create_fresh_session` ([`prepare.py:899`](../../packages/core/daimon/core/turn/prepare.py#L899)) has no cleanup |
| `CommitThenError` | a commit that lands but raises (lost acknowledgement) inside the same `except BaseException` blocks |
| `AdoptedOk`, `AdoptedFail` | [`run.py:674`](../../packages/core/daimon/core/turn/run.py#L674) returns the adopted session; there is no cleanup of it |
| `Retry` | `SkillUnavailable` loop in `create_ma_session`; `SessionBusyError` from the seal fence ([`prepare.py:575`](../../packages/core/daimon/core/turn/prepare.py#L575)) re-prepares; the user asking again |

## Constants

| Constant | Safe value | Meaning |
| --- | --- | --- |
| `MaxAttempts` | 2 | a failed attempt can be followed by one more, with a new session |
| `CleanupInCreate` | TRUE | `create_ma_session` archives on a failure in its window (the fix) |
| `CallerCleans` | TRUE | the caller archives a `CreatedSession` it holds when it fails (models the recovery caller) |
| `CallerArchivesAdopted` | FALSE | rejected design: a caller that archives whatever it holds |
| `AmbiguousCommit` | FALSE | a commit can land and still raise |
| `ReplayPossible` | FALSE in `CleanupSafe`, TRUE in the replay configs | attempt 1's create is a replay of an earlier attempt's recorded create; that attempt may or may not have committed a row |
| `GuardReplays` | TRUE | the window archives a replayed session only if a row read shows no row names it |
| `RowReadFails` | FALSE | that row read fails, so a replayed session is left alone |

One turn, one upstream session per attempt, one adopted session. More
sessions or turns add no ordering: no cleanup reads another session's state.

## Properties

| Invariant | Statement |
| --- | --- |
| `NoOrphan` | once an attempt is over, a live upstream session is one a row names |
| `NoArchiveNamed` | a session a committed row names is never archived (by anyone) |
| `WindowNeverArchivesNamed` | the cleanup inside `create_ma_session` never archives a session a row names; this is the property of the fix, since callers' own cleanups are separate (see `CleanupReplayCallerArchives`) |
| `NoArchiveAdopted` | the session another turn created is never archived by this turn |
| `SuccessNotArchived` | a successful turn's session is not archived |

## Results

TLC 2.19 (`tla2tools.jar` v1.7.4), one worker. `formal/check.sh` pins the counts.

| Config | Verdict | States | What it shows |
| --- | --- | --- | --- |
| `CleanupSafe` | clean | 48 | no replay: the fix, the recovery caller and the adopt path keep all five invariants |
| `CleanupSafeReplay` | clean | 65 | with a replayed recorded create (row-named or not) the window cleanup keeps `NoOrphan` and `WindowNeverArchivesNamed` |
| `CleanupReplayUnconditional` | violates `WindowNeverArchivesNamed` | 10 | rejected rule: archive a replayed session without asking whether a row names it |
| `CleanupReplayCallerArchives` | violates `NoArchiveNamed` | 20 | residual, not fixed here: a caller that archives a replayed row-named session it holds on failure |
| `CleanupReplayReadFails` | violates `NoOrphan` | 8 | accepted trade-off: if the row read fails a replayed session is left alone and stays live |
| `CleanupOldCode` | violates `NoOrphan` | 8 | pre-fix code: the leak |
| `CleanupArchivesAdopted` | violates `NoArchiveAdopted` | 7 | rejected design: a caller archiving everything it holds archives an adopted session |
| `CleanupCallerNoCleanup` | violates `NoOrphan` | 15 | residual, not fixed here: `create_fresh_session` has no cleanup after `create_ma_session` returns |
| `CleanupAmbiguousCommit` | violates `NoArchiveNamed` | 22 | residual, not fixed here: a commit that lands but raises makes the caller archive a named session |
| `CleanupWitnessWindow` | violates `NoWindowCleanup` | 8 | witness: the window cleanup is reachable |
| `CleanupWitnessSuccess` | violates `NoCreatedSuccess` | 21 | witness: a created session reaches a committed row |

Checked within these bounds, not proved for the production code.

## Counterexample: pre-fix leak (`CleanupOldCode`)

1. A user's session dies upstream. Dead-session recovery takes the thread lock
   and calls `create_ma_session`.
2. `sessions.create` succeeds. `create_session` returns the new session and
   `create_ma_session` starts the personal-memory lookup.
3. The turn ceiling fires (or Stop is pressed, or the database drops the
   connection) while the lookup is awaiting.
4. The exception unwinds out of `create_ma_session`. Recovery's `created` is
   still `None`, so its cleanup skips the archive. The recovery transaction
   rolls back and no row names the session.
5. The upstream session is live and nothing will ever archive it.

## Assumptions

Unsupported until a contract test or a real log shows them.

| Assumption | Status |
| --- | --- |
| Archive succeeds when it is attempted. The real call is best effort: it logs and gives up after a 5 s wait ([`run.py:476`](../../packages/core/daimon/core/turn/run.py#L476)); a failed archive leaves the leak | upstream behaviour, not modelled; the tests answer 200 and 500 and check only that the attempt is made and the original error is kept |
| Archiving an already archived or deleted session is harmless and idempotent | not tested against staging |
| `create_session` cleans up its own session on every failure before `return created` | read from [`sessions.py:1314`](../../packages/core/daimon/core/sessions.py#L1314); read from the code: after creation only the app-mode and native paths await |
| A cancel is delivered only at an await | asyncio semantics |
| A recorded `session_create` is replayed by a later attempt under the same operation key ([`persistence.py:214`](../../packages/core/daimon/core/turn/persistence.py#L214), [`persistence.py:234`](../../packages/core/daimon/core/turn/persistence.py#L234)). The key is `fleet_root:{origin_id}` for a controlled root (new origin per turn execution, [`turn_origin.py:158`](../../packages/core/daimon/core/turn_origin.py#L158)) and `fleet:{run_id}:spawn:{ordinal}` for a native worker ([`fleet.py:196`](../../packages/core/daimon/core/fleet.py#L196), passed at [`headless_runner.py:928`](../../packages/core/daimon/core/headless_runner.py#L928)), so a restarted or retried worker spawn reuses it | read from the code; `test_root_controls.py` (`first == second`) shows the replay for two calls in one process. Whether a worker spawn is re-prepared after its mapping committed is not shown, which is why the replay configs are included and the fix does not rely on it not happening |

## Not modelled

- `create_session`'s internal steps (vault, files, repository resources, app
  access); only its guarantee is used.
- The caller's later steps after the commit: the turn itself, billing, markers.
- Two turns racing for one thread. The per-thread advisory lock serialises
  recovery and preparation, and [`session_preparation/`](../session_preparation/README.md)
  models that.
- Process death: a crash runs no cleanup in any variant. The recorded
  `session_create` operation is what lets a restarted preparation find the
  session again on the prepared path; no other path has that.
- Archive failure, timeout and retry. The model archives in the same step as the
  failure; in the code the archive is an await after it, shielded from a second
  cancel and bounded by a 5 s deadline, and nothing else runs for this turn
  meanwhile.
- A row committed between the replay's row read and its archive: the row of a
  replayed session was committed by an earlier attempt, before the read.
- What `create_session` itself does when a replay fails before it returns: its
  cleanup ([`sessions.py:1314`](../../packages/core/daimon/core/sessions.py#L1314))
  archives a replayed session it owns on the native paths without asking whether
  a row names it, the same exposure the window rule closes. `FailBeforeCreate` and
  `StartAdopt` are disabled for a replaying attempt.

## Replay status

- `packages/core/tests/turn/test_create_ma_session_cleanup.py` reproduces
  `CleanupOldCode` against the real code with a fake upstream: it fails before
  the fix and passes after. It also covers the adopt path, the success paths,
  the prepared (recorded `session_create`) path, a replay with no row (archived),
  a replay whose mapping row is committed (not archived) and a replay whose row
  read fails (not archived). The ceiling tests expire a real `asyncio.timeout`.
- Mutations: removing the cleanup fails 8 of 13 tests; archiving a wrong id fails
  the same 8; archiving on success fails the 3 negative tests; dropping the
  replay row check fails the two row-guard tests; never archiving a replay fails
  the replay-without-row test.
- No contract test against the real Managed Agents archive endpoint and no
  trace validation. The results above are provisional until both exist.

## Running

```sh
cd formal/session_create_cleanup
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config CleanupSafe.cfg SessionCreateCleanup.tla
```
