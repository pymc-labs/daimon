# GitHub issued-token prune

Question: can deleting closed rows from `github_issued_tokens` remove a row that a
concurrent mint, store, deliver, supersede or revoke still needs, or lose track of a live
GitHub token?

The policy under test: a `revoked` row is deleted 7 days after `expires_at`, a never-stored
`pending` row 1 hour after `expires_at`, and `stored` and `delivered` rows are never
deleted. `security_audit_events` is never touched. The model covers one token and one minting
process; `IssuedTokensPrune.tla` is the spec and each `.cfg` is one configuration.

Short answer: no, within these bounds. A `pending` row holds no token (`encrypted_token` is
null), so deleting one cannot lose track of a token. The only thing a pending row is for is
the minter's own `store_token`. Under the timing assumptions below, the 1 hour grace means
the store finds its row; nothing in the code bounds how late a store can arrive (a suspended
process, a slow database), and the `StallUnboundedMisses` witness shows a store missing its
row when that bound is dropped. When a store does miss its row, the caller deletes the token
at GitHub; if that DELETE also fails, the token stays live and untracked until GitHub expires
it. A token minted that late is normally dead already: the app JWT used for the mint is built
before the mint loop and expires after nine minutes
([`github_app_auth.py:215`](../../packages/core/daimon/core/github_app_auth.py#L215)), GitHub
refuses a mint with an expired JWT, and installation tokens last one hour.

`MarkRevoked` here only fires for a dead token. The code also marks rows `revoked` without a
GitHub call when it presumes the token expired, and when it closes an uncertain mint; both
happen well inside the 7-day retention, so the abstraction does not change the result.

## Action to code

| Model action | Code |
| --- | --- |
| `Create` | [`create_pending`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L101), called at [`github_app_session.py:430`](../../packages/core/daimon/core/github_app_session.py#L430) in its own transaction, with `expires_at` 55 minutes out ([`:428`](../../packages/core/daimon/core/github_app_session.py#L428)). |
| `MintOK`, `MintRefused` | `mint_installation_token` at [`:446`](../../packages/core/daimon/core/github_app_session.py#L446). A 403, 404 or 422 marks the pending row `revoked` ([`:459`](../../packages/core/daimon/core/github_app_session.py#L459)); another failure raises `GitHubTokenMintUnavailable` and the outer handler marks it revoked ([`:523`](../../packages/core/daimon/core/github_app_session.py#L523)). |
| `Crash` | No code. A process that dies after the GitHub call leaves a `pending` row and a live token nothing records. The prune does not change that. |
| `StoreOK` | [`store_token`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L151) takes `FOR UPDATE` and sets `stored`; called at [`:478`](../../packages/core/daimon/core/github_app_session.py#L478). |
| `StoreMiss`, `RevokeLate` | `store_token` raises `GitHubTokenRowClosedError` for a revoked row ([`:157`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L157)) and `ValueError` for a missing one ([`:159`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L159)). Both reach the handler that calls [`revoke_token`](../../packages/core/daimon/core/github_app_session.py#L312) and re-raises ([`:498-505`](../../packages/core/daimon/core/github_app_session.py#L498)). `revoke_token` raises on a 5xx. |
| `Deliver` | [`mark_delivered`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L166), called from [`finish_app_delivery`](../../packages/core/daimon/core/github_app_session.py#L694) at [`:712`](../../packages/core/daimon/core/github_app_session.py#L712). It also rewrites `session_id` ([`set_session_id`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L354)); that changes neither status nor expiry, so the model leaves it out. |
| `RevokeAtGitHub`, `MarkRevoked` | The scheduler sweep rechecks the row under `FOR UPDATE` ([`recheck_token_revoke_due`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L599)), calls GitHub ([`main.py:736`](../../packages/adapters/scheduler/daimon/adapters/scheduler/main.py#L736)) unless the token already expired, then [`mark_revoked`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L175) ([`:743`](../../packages/adapters/scheduler/daimon/adapters/scheduler/main.py#L743)). A failed GitHub call only records an attempt, so the row stays `delivered` or `stored`. Supersede and `revoke_after` ([`mark_session_tokens_superseded`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L374), [`keep_failed_rotation_tokens`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L420)) only schedule this and touch no pending or revoked row. |
| `GitHubExpiry` | GitHub installation tokens last one hour ([`mint_installation_token` docstring](../../packages/core/daimon/core/github_app_auth.py#L248)). |
| `Tick` | Time. Its guards are the timing assumptions below. |
| `Prune` | [`prune_closed_tokens`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L187): one `DELETE` whose `WITH ... MATERIALIZED` subquery selects and locks (`FOR UPDATE SKIP LOCKED`). Run by [`sweep_closed_github_issued_tokens`](../../packages/core/daimon/core/github_issued_token_sweep.py) from [`_sweep_github_issued_tokens`](../../packages/adapters/scheduler/daimon/adapters/scheduler/main.py#L687). |
| `SelectCandidate`, `DeleteCandidate` | The rejected design: select ids, then delete by id with no lock. Not in the code. |

Writers the model leaves out, and why they are safe: `erase_requester_identity`
([`:92`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L92), account purge at
[`purge.py:551`](../../packages/core/daimon/core/purge.py#L551)) only nulls `github_user_id`
and at worst makes the sweep skip the row once. `record_revoke_attempt`
([`:231`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L231)) only touches
`stored` and `delivered` rows. Tenant deletion cascades the rows and is documented to revoke
live tokens first ([`_models.py:3056`](../../packages/core/daimon/core/_models.py#L3056)). No
code reads a revoked row, and none holds the id of a row past its expiry.

## Constants and scaling

The clock is four ages of one row, relative to its `expires_at`, so the real durations do not
enlarge the state space:

| Age | Real time since the row was inserted | Source |
| --- | --- | --- |
| 0 | before `expires_at`, 0 to 55 minutes | [`github_app_session.py:428`](../../packages/core/daimon/core/github_app_session.py#L428) |
| 1 | past `expires_at`, within `PENDING_GRACE` (1 hour) | [`github_issued_tokens.py:29`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L29) |
| 2 | past `PENDING_GRACE`, within `REVOKED_RETENTION` | |
| 3 | past `REVOKED_RETENTION` (7 days) | [`github_issued_tokens.py:22`](../../packages/core/daimon/core/stores/github_issued_tokens.py#L22) |

| Constant | Code value | Meaning of the other value |
| --- | --- | --- |
| `GraceHour` | TRUE | FALSE prunes pending rows at `expires_at` |
| `FilterStatus` | TRUE | FALSE prunes any row, not only revoked and pending |
| `StoreStallBounded` | TRUE | FALSE lets a minter hold a minted token for over an hour before it stores |
| `LateRevokeCanFail` | TRUE | FALSE assumes the late DELETE at GitHub always works |
| `SplitPrune` | FALSE | TRUE selects ids, then deletes them with no lock or recheck |

One token, one minter and no second token keep each run under a second. A second token or a
second minter adds no interleaving the row lock does not already order: each row is locked
by whoever changes it and `Prune` skips locked rows.

## Properties

- `NeverDeleteLiveRow`: no `stored` or `delivered` row is ever deleted. This is the retention
  policy. It is a rule about status, not a consequence of GitHub's token life: a stored row
  past its expiry holds a dead token, but it is also the record a live session's rotation
  check reads.
- `NoLiveTokenLost`: once the minter finishes, a live GitHub token sits in a `stored` or
  `delivered` row. This is the one that matters for security.
- `StoreNeverMissesRow`: a store never finds its row gone. A miss is safe (`RevokeLate`) but
  fails the session start, so it is availability, not security.
- Witnesses `NoPendingPruned` and `NoRevokedPruned`: meant to be violated, to show both
  deletions are reachable and the clean results are not vacuous.

No liveness claim: the model does not show that a closed row is eventually deleted. A row
locked at every sweep would be skipped every time; the tests pin only that a skipped row is
taken once its lock is gone.

## Results

| Configuration | Distinct states | Verdict | What it shows |
| --- | ---: | --- | --- |
| `IssuedTokensPruneSafe` | 40 | clean | The code. All three properties hold, even if the late revoke can fail. |
| `IssuedTokensPruneStallUnbounded` | 48 | clean | A store stalled for over an hour still cannot lose a live token. By then the token has expired. |
| `IssuedTokensPruneStallUnboundedMisses` | 41 | `StoreNeverMissesRow` | Witness: such a stalled store does find its row pruned. The caller handles it. |
| `IssuedTokensPruneNoGraceMisses` | 27 | `StoreNeverMissesRow` | Pruning pending rows at `expires_at` makes a healthy mint miss its row. |
| `IssuedTokensPruneNoGraceStillSafe` | 50 | clean | With no grace but a late revoke that works, the token is still revoked. |
| `IssuedTokensPruneNoGraceRevokeFails` | 38 | `NoLiveTokenLost` | No grace plus a failed late revoke leaves a live token with no row. The grace is the real protection. |
| `IssuedTokensPruneNoStatusFilter` | 38 | `NeverDeleteLiveRow` | Without the status filter a stored row is deleted. |
| `IssuedTokensPruneNoStatusFilterNoGrace` | 27 | `NoLiveTokenLost` | Without the filter or the grace, a stored row with a live token is deleted. |
| `IssuedTokensPruneSplitSafe` | 46 | clean | Select-then-delete behind the grace holds only while stores finish within the hour. |
| `IssuedTokensPruneSplitNoGrace` | 42 | `NoLiveTokenLost` | Select, a store commits, delete by id: a live stored row is deleted. |
| `IssuedTokensPruneSplitStallUnbounded` | 54 | `NeverDeleteLiveRow` | The same loss through a store stalled past the grace. |
| `IssuedTokensPruneWitnessPending`, `...WitnessRevoked` | 25, 30 | violated on purpose | Both deletions are reachable. |

Counts are TLC `-workers 1`, pinned in [`../expected.tsv`](../expected.tsv).

## Counterexamples in plain words

- **No grace** (`NoGraceMisses`): the row is inserted and GitHub issues the token. The
  minter is slow to store it, and the pending row passes its `expires_at` 55 minutes after
  the insert. A prune deletes the row. The store finds nothing and the session start fails.
  The caller deletes the token at GitHub.
- **No grace, failed revoke** (`NoGraceRevokeFails`): the same, and the DELETE at GitHub
  returns a 5xx. The session start fails and the token stays live, with no row, until it
  expires. The 1 hour grace removes the first step.
- **Delete by id** (`SplitNoGrace`): a sweep picks a pending row, the minter's store commits,
  and the sweep deletes the row, now `stored`, whose token is live. One `DELETE` that locks
  its rows with `SKIP LOCKED` has no such gap: a locked row is skipped, and a row that
  committed a change after the scan began is rechecked against its new version.
- **No status filter** (`NoStatusFilter`): the delete matches a row by age alone and removes
  a `stored` row.

## Assumptions

| Assumption | Basis |
| --- | --- |
| The GitHub call follows the insert within the row's 55 minutes (`Tick` blocks age 0 to 1 while the minter is between insert and mint). | Nothing sits between [`:430`](../../packages/core/daimon/core/github_app_session.py#L430) and [`:446`](../../packages/core/daimon/core/github_app_session.py#L446) but one assignment, and both callers use `httpx.AsyncClient()` ([`github_app_session.py:954`](../../packages/core/daimon/core/github_app_session.py#L954), [`sessions.py:1018`](../../packages/core/daimon/core/sessions.py#L1018)), whose default timeout is 5 s per phase (httpx 0.28.1 `DEFAULT_TIMEOUT_CONFIG`). |
| A GitHub token dies one hour after the mint, so one minted within 55 minutes of the insert is dead by `expires_at` plus 1 hour (`Tick` blocks age 1 to 2 while a token is live). | Code docstring above and GitHub's documented one-hour life. Not checked against a live GitHub. |
| A minted token is stored within an hour (`StoreStallBounded`). | `store_token` is one short transaction. A connection wait is capped by `database.pool_timeout`, default 30 s ([`config.py:72`](../../packages/core/daimon/core/config.py#L72)). No statement timeout exists. Unsupported as a hard bound, so `StallUnbounded` checks safety without it. |
| Only the minter changes a pending row. | `mark_revoked` on a pending row is called only at [`:459`](../../packages/core/daimon/core/github_app_session.py#L459) and [`:523`](../../packages/core/daimon/core/github_app_session.py#L523), by the minter. Sweeps list only stored and delivered rows. |
| `Prune` is atomic and a locked row is never deleted. | PostgreSQL `READ COMMITTED` and `FOR UPDATE SKIP LOCKED` ([docs](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)). Two-connection tests in `test_github_issued_tokens_prune.py` check the skip. The recheck-after-concurrent-commit step is taken from the documentation and is not tested. |
| Clock skew between hosts is far below an hour. | `expires_at` comes from the minter's clock, `now` from the scheduler's. Unsupported. |

## Not modelled

A mint response lost to a timeout after GitHub issued the token: the row is marked revoked
and the token is live and unknown for up to an hour, with or without the prune. A second
live token or session. Statement and lock timeouts on the prune itself (tested separately).
Rows reappearing after deletion.

## Replay status

Provisional. Replayed against the code: `NoGraceMisses` and `NoGraceStillSafe` by
`test_late_store_after_its_pending_row_was_pruned_revokes_the_github_token`, which forces
the prune between the mint and the store and checks the token is deleted at GitHub. The
`SKIP LOCKED` behaviour by the two-connection tests. Not replayed: `NoGraceRevokeFails` (a
5xx on the late DELETE), any real GitHub contract test, and trace validation against
production logs.
