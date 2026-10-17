# One GitHub installation per agent

`OneInstallation.tla` asks one question: can an agent end up with grants on two GitHub installations? An agent may use one GitHub org. Three code paths write an agent's grants, and they run under different PostgreSQL isolation levels:

| Writer | Code | Isolation |
| --- | --- | --- |
| `fast`: explicit repo grant for a server-wide repo | `mcp/tools/github_connect.py` (`session_factory.begin()` near `:299`, `stage_grant` near `:407`) | READ COMMITTED |
| `linked`: explicit linked grant | `mcp/tools/github_linked_grant.py` `linked_grant` (`SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` near `:341`, installation `get_for_update` near `:344`, `stage_grant` near `:403`) | SERIALIZABLE |
| `owner`: owner grant | `linked_owner_grant` (SERIALIZABLE near `:570`, installation locks near `:588`, `require_owner_grant_installations` near `:605`, `upsert_owner_grant` near `:654`) | SERIALIZABLE |

For a user: two people (or one person twice) grant the same agent repos from different orgs at the same moment. One grant must win and the other must be told to try again. A silent second installation would give the agent repos from two orgs, which the product rules out.

Run `FORMAL_CHECK_FILTER=github_one_installation formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md). The safe and liveness configs use `-seed 1 -fp 0 -lncheck final`; the rest use `-seed 1 -fp 0`. All runs use one TLC worker. Each run takes a few seconds.

The Python was being changed while this model was written (the fence row, the lock order and the error mapping). Function names are the contract; the line numbers are approximate and must be re-read. The re-check list is below the action table.

## Model

One agent, two installations (`A`, `B`), three writers. Each writer's kind and target installation are chosen at `Init`, so one run covers every assignment of kinds and targets. A writer runs a list of steps; each step is one atomic action because the code can be interleaved there (a statement, a lock wait, the commit).

| Kind | Steps |
| --- | --- |
| `fast` | `fence`, `check`, `commit` |
| `linked` | `snap`, `inst`, `fence`, `check`, `commit` |
| `owner`, `OwnerOrder = "instFirst"` (design) | `snap`, `inst`, `fence`, `check`, `commit` |
| `owner`, `OwnerOrder = "fenceFirst"` (ac782e183) | `snap`, `fence`, `inst`, `check`, `commit` |

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`).

| Model action | Code boundary |
| --- | --- |
| `DoSnap` | The first statement of the SERIALIZABLE transaction in `linked_grant` and `linked_owner_grant` (`get_account_link_for_update`). PostgreSQL fixes the snapshot there. READ COMMITTED writers have no step: every statement sees what is committed when it starts. |
| `DoInst` | `get_for_update(session, installation_id=...)` on the installation row (`mcp/tools/github_linked_grant.py`, both paths). The fast path takes no installation lock. Modelled as a plain exclusive lock: a `SELECT ... FOR UPDATE` that does not change the row raises no 40001 for a stale snapshot. |
| `DoFence` | `lock_agent_grants` (`core/stores/github_access.py` near `:87`): `INSERT ... ON CONFLICT DO UPDATE SET version = version + 1` on `agent_github_grant_fences`, called first thing in `stage_grant` and in `require_owner_grant_installations`. The row lock lasts until commit. `LockMode = "advisory"` is the code at `ac782e183`, a `pg_advisory_xact_lock`. |
| `DoCheck` | The reads in `stage_grant` (`list_agent_grants`, `repo_for_agent`, `_refuse_other_owner_installation`) and in `require_owner_grant_installations` (same lists, then `_refuse_other_owner_installation`). A mismatch raises `SecondInstallationError` and the transaction rolls back: outcome `refused`. SERIALIZABLE reads its snapshot; READ COMMITTED reads what is committed now. |
| `DoCommit` | The grant row (`AgentGitHubGrant`) or the owner grant row (`upsert_owner_grant`) becomes visible, the fence version moves, locks go. |
| `DoDeadlock` | PostgreSQL's deadlock detector aborts one participant of a lock cycle with SQLSTATE 40P01. |
| `MapSerialization`, `MapDeadlock` | `is_concurrent_grant_change` (`core/stores/github_access.py` near `:82`, SQLSTATE `40001` and `40P01`) and its callers: `mcp/tools/github_connect.py` (near `:485`, `changed_try_again`), `mcp/tools/github_linked_pending.py` (near `:278`, `CHANGED_MESSAGE`), `mcp/oauth_github.py` (near `:1995`). Off = the error escapes. |

Re-check after the Python change: that `lock_agent_grants` is the first write of every grant writer (also `remove_grant`, `remove_agent_repo`, the CLI `commands/github.py` near `:363`, `core/stores/github_connect.py` near `:380`, `core/stores/github_panel_grants.py` near `:268`, which this model does not run as separate writers but which call `stage_grant`); that the owner path locks every installation (in id order) before `require_owner_grant_installations` and the explicit path locks the installation before `stage_grant`; that no path takes a row lock on a table the fence order does not cover; that the fast path is still READ COMMITTED and the confirm page (`oauth_github.py` near `:1903`) is still SERIALIZABLE; which isolation level the CLI and the panel grants use; and that every caller of a grant transaction maps 40001 and 40P01 to the retry message.

## Constants and scaling

| Quantity | Model | Source and scaling |
| --- | --- | --- |
| Agent | 1 | The rule is per agent; grants of different agents never interact. |
| Installations | 2 | Smallest number that can break "one installation". |
| Writers | 3 | Two writers race, a third can wait, abort or deadlock behind them. Each takes the one grant it came for. Production has no fixed limit. |
| Grants per writer | 1 | One repo, or one owner grant on one installation. The owner path locks several installations in id order; a single one is enough to show the lock-order cycle with an explicit writer. |
| Fence version | 0 to 3 | Counts commits; 0 means the row does not exist yet, so a first insert after a snapshot counts as a changed row (PostgreSQL waits for the concurrent insert and a SERIALIZABLE transaction then fails with 40001). |
| Time | not counted | Every interleaving of the steps is explored; deadlock detection (the `deadlock_timeout` delay) is an action that is enabled whenever a cycle exists. |

## Properties

| Property | Meaning |
| --- | --- |
| `AtMostOneInstallation` (invariant) | The agent's explicit and owner grants span at most one installation. |
| `NoUnhandledOutcome` (invariant) | Every writer ends with a grant, a refusal or `changed_try_again`; no database error escapes the tool. |
| `NoDeadlock` (invariant) | A lock cycle is never reached. Holds in the design; the inverted order reaches it. |
| `AllEnd` (temporal) | Every writer ends. Weak fairness on every step, including deadlock detection. A cycle PostgreSQL did not break would fail this. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `safe` | clean | 36,732 | The design: fence row, installation lock before the fence on every path, 40001 and 40P01 mapped. At most one installation, no deadlock, every writer ends. SSI is off, so the result does not rely on it. |
| `advisory_lock` | violates `AtMostOneInstallation` | 18,044 | The code at `ac782e183` (advisory lock, owner path locks the agent first, SSI on). See counterexample 1. |
| `advisory_fast_owner` | violates `AtMostOneInstallation` | 4,060 | The same with only a fast-path writer and an owner writer: the schedule in the review of `ac782e183`. |
| `advisory_two_serializable` | clean | 24,008 | Witness: with only `linked` and `owner` writers the advisory lock holds, because SSI (modelled as an abort at commit) catches the write skew. The READ COMMITTED path is what breaks it. |
| `advisory_serializable_no_ssi` | violates `AtMostOneInstallation` | 12,180 | The same without SSI: the advisory lock alone does not refresh a snapshot. |
| `fence_no_abort` | violates `AtMostOneInstallation` | 18,394 | The fence row without the 40001 on a row changed after the snapshot: the design depends on that PostgreSQL rule. |
| `inverted_order` | clean | 35,784 | The owner path takes the agent fence first (the `ac782e183` order) with 40P01 mapped: no second installation, no unhandled outcome, every writer ends. |
| `inverted_order_witness` | violates `NoDeadlock` | 6,788 | Witness: the inverted order reaches a deadlock. Counterexample 3. |
| `inverted_order_unmapped` | violates `NoUnhandledOutcome` | 6,788 | The inverted order without the 40P01 mapping (`ac782e183`): the aborted writer ends with an error. Counterexample 3. |
| `serialization_unmapped` | violates `NoUnhandledOutcome` | 9,851 | 40001 not mapped: a stale snapshot ends with an error. |
| `serialization_abort_witness` | violates `NoSerializationFailure` | 9,851 | Witness: the fence aborts a SERIALIZABLE writer whose snapshot predates a committed grant. |
| `refusal_witness` | violates `NeverRefused` | 6,696 | Witness: a writer for the other installation is refused. |
| `owner_commit_witness` | violates `NoOwnerCommit` | 6,835 | Witness: an owner grant commits. |
| `two_commits_witness` | violates `NoTwoCommits` | 9,809 | Witness: two writers for the same installation both commit. |

## Counterexamples in plain words

1. `advisory_lock`: an agent has no grants. A `fast` writer (READ COMMITTED) takes the agent lock, finds nothing and writes a grant on `A`. A `linked` writer for `B` begins its SERIALIZABLE transaction before that commit, so its snapshot shows no grant. The first writer commits and releases the advisory lock. The second acquires it, reads its old snapshot, sees no grant on `A` and commits a grant on `B`. The agent now has repos from two orgs. The advisory lock waited for the first writer but did not give the second a new snapshot.
2. `advisory_fast_owner`: the same with an owner grant instead of `linked`: the owner transaction takes its snapshot, waits for the lock, and writes an owner grant on `A` after a fast-path grant on `B` committed.
3. `inverted_order_witness` and `inverted_order_unmapped`: an explicit linked grant for `A` holds the installation lock and waits for the agent fence. An owner grant for `A` holds the fence and waits for the installation lock. PostgreSQL detects the cycle and aborts one with 40P01. With the mapping the person is told to try again and nothing was written. Without it (`ac782e183`) the error escapes the tool and the request is lost without an answer.
4. `fence_no_abort`: the fence row serializes writers but the waiting SERIALIZABLE writer proceeds on its old snapshot, like the advisory lock.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| A SERIALIZABLE transaction's snapshot is fixed at its first statement and every later read sees it. A READ COMMITTED statement sees what was committed when it started. | [PostgreSQL 16, transaction isolation](https://www.postgresql.org/docs/16/transaction-iso.html) | Supported by the documentation; not replayed against the project's database. |
| An `UPDATE` or `INSERT ... ON CONFLICT DO UPDATE` that meets a row changed by a concurrent committed transaction after the SERIALIZABLE snapshot fails with 40001 (`could not serialize access due to concurrent update`); READ COMMITTED waits for the other transaction and goes on with the new row. | Same page, "Read Committed" and "Repeatable Read" (SERIALIZABLE inherits the update rule). | Supported by the documentation; a test with two real connections would confirm the `ON CONFLICT` case. `fence_no_abort` shows what rests on it. |
| An advisory lock blocks until commit and leaves the snapshot alone. A deadlock is detected after `deadlock_timeout` and one transaction fails with 40P01. | [PostgreSQL 16, explicit locking](https://www.postgresql.org/docs/16/explicit-locking.html) | Supported by the documentation. Which participant is aborted is not specified; the model aborts any member of the cycle. |
| SSI (serializable snapshot isolation) aborts a SERIALIZABLE transaction in a read-write cycle with another SERIALIZABLE one; it never involves a READ COMMITTED transaction. The model abstracts it as: a SERIALIZABLE writer aborts at commit if another SERIALIZABLE writer committed after its snapshot. | Same page, "Serializable". | Over-approximation of the aborts (real SSI aborts less). It is on only in the `advisory_*` configs, to keep their counterexamples about READ COMMITTED; `safe` runs with it off. |
| `SELECT ... FOR UPDATE` on the installation row, with no change to the row, causes no 40001 for a stale snapshot. | [PostgreSQL 16, explicit locking, row-level locks](https://www.postgresql.org/docs/16/explicit-locking.html) | Supported by the documentation for a lock-only tuple; the code's `upsert_github_app` does update that row in the explicit path before `stage_grant`, which can add a 40001 the model does not have. More aborts only make a grant retry, never a second installation. |
| Between the check and the commit, no other writer commits a grant, because the writer holds the agent lock (or fence row lock) until commit. | `lock_agent_grants` in `stage_grant` and `require_owner_grant_installations`; row locks and advisory transaction locks last until commit. | Supported by the code and the documentation, if every grant writer calls `lock_agent_grants` (re-check list). |
| `SecondInstallationError` rolls the transaction back, so a refusal writes nothing. | `except SecondInstallationError` in `linked_grant` and `linked_owner_grant` after `sessionmaker.begin()`. | Supported by the code. |

## Not modelled

More than one repo per writer, removals (`remove_grant`), the `owner/*` exclusion rows, the writers that call `stage_grant` outside the three (CLI, panel, the confirm page) as separate kinds, multi-installation owner grants (locked in id order, so a cycle among owner writers is excluded by the sort and not checked here), authority and installation checks (see `github_linked_grant/`), a retry after `changed_try_again`, and the `deadlock_timeout` delay.

## Replay status

Provisional. The model has not been replayed against PostgreSQL by this run. Counterexample and witness tests, by name in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (present in the working tree at the time of writing, some uncommitted; re-check the names if the file moves; not rerun here, and whether each fails without its fix was not checked). The file uses real database connections (`other_db`), so the tests are the replay for the PostgreSQL rules below if they pass in CI.

| Model result | Test |
| --- | --- |
| `advisory_fast_owner`, `advisory_lock` (counterexamples 1, 2): the fast-path commit after the owner snapshot | `test_fast_path_explicit_grant_committed_after_the_owner_snapshot_fails_the_owner_grant`, `test_owner_grant_waiting_on_a_fast_path_grant_fails_when_that_commits` |
| the old advisory lock as a control | `test_the_old_advisory_lock_let_both_installations_through` |
| `safe`: a fast-path writer waiting on an owner grant sees it and refuses | `test_fast_path_grant_waiting_on_an_owner_grant_sees_it_and_refuses` |
| both writers bump one fence row | `test_both_paths_bump_the_same_fence_row` |
| `inverted_order`, `safe`: installation before fence | `test_both_linked_paths_lock_the_installation_before_the_agent_fence` |
| `serialization_unmapped`, `inverted_order_unmapped` (counterexample 3): 40001 and 40P01 become try-again, other errors do not | `test_a_deadlock_or_serialization_failure_asks_again_in_the_tool_and_on_resume`, `test_another_database_error_is_not_mapped_to_ask_again` |
| `refusal_witness`, owner and explicit grants keep one installation sequentially | `test_owner_grants_keep_one_installation_per_agent`, `test_second_installation_is_a_defined_result` |

No test found by name for a real two-connection inverted-order deadlock (the order test checks the sequence of locks, not a live cycle) or for the `ON CONFLICT` fence upsert under a stale SERIALIZABLE snapshot with no other writer in play. Upstream staging contract tests: 0 run. Real traces validated: 0.
