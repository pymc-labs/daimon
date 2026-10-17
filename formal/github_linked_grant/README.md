# Linked GitHub chat grant model

`GitHubChatGrant.tla` asks whether a chat request can save a grant after link identity, manager authority, or installation coverage changes, including while a lookup or the grant transaction is still in flight. One intent and one request cross the browser, GitHub, and serializable database boundaries. Run `FORMAL_CHECK_FILTER=github_linked_grant formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md).

Three questions, one per part of the request path:

1. **Link and commit authority** (original): can a forwarded link attach someone else's GitHub account, or a grant commit after the manager, link, or coverage changed?
2. **Coverage removed during the lookup** (`LateCoverageCheck`, `LockRepoCheck`): the lookup finds a repo covered, local coverage is removed while the repo permission GET is outstanding, and the late snapshot then reads the already-removed set. Can the grant commit, and does the commit put the repo back into the installation's `repo_full_names`?
3. **Authority read after the transaction's first read** (`RoleRead`, `RuleRead`): the requester's authority comes from more than one stored row: the role (the `Account` row and the channel-admin rows) and the agent's access-policy ownership rule. A revocation of either commits after the grant transaction took its snapshot and before it reads that row. Can the grant still commit on the old value? The fixed design locks every authority row `FOR SHARE` in the commit-time recheck. The `rule_read_unlocked` config is the code at `9441d430d`, which locked the role and read the rule unlocked.

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`). Line numbers are approximate and drift; the function names are the contract.

| Model action | Code boundary |
| --- | --- |
| `Mint`, `Open` | `core/stores/github_personal_links.py` `mint_link` creates a one-use intent; `oauth_github_personal.py` opens it. |
| `PlatformCallback`, `RejectForeign` | `oauth_github_personal.py` verifies the chat identity before GitHub authorization. |
| `GitHubCallback`, `ReplayCallback` | `oauth_github_personal.py` saves a verified link and consumes the intent in one transaction. |
| `Request` | `mcp/tools/github_connect.py` (direct path) and `mcp/tools/github_linked_pending.py` `resume_pending` (after the link) call `linked_grant`. |
| `Discover` | `mcp/tools/github_linked_grant.py` `linked_grant`: `linked_access_token_with_generation` (`:207`, reads the link generation paired with the token), `_installations` (`:217`), `get_app_installation_details` (`:236`). GitHub says the repo is covered. |
| `CheckPermission` | `linked_grant` repo GET at `:257`, then `proposed_linked_grant_policy`. This is the outstanding call coverage can be removed during. |
| `LateSnapshot`, `LateReject` | `linked_grant` `:302-311`: `get_installation`, `originally_present`, `original_names`, and the `installation_covers_repo` early exit (`changed_try_again`). |
| `Begin`, `BeginReject` | `linked_grant` `:314-335`: `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE`, then `get_account_link_for_update`, `get_user_for_update`, `get_for_update` (installation) and the link, generation, and coverage comparison. The first statement fixes the snapshot. |
| `ReadRole`, `RoleDeny` | `manages_now(session)` (`:335`, `:556`): `github_connect.py` `manages_now` (`:430-458`) and `github_linked_pending.py` `_outcome_message` (`:195`) call `get_account(..., for_share=True)` (`core/stores/accounts.py:74-95`, `SELECT ... FOR SHARE`) and `requester_manages_agent(..., lock_authority=True)` (`core/github_panel.py:97`), whose only read is `read_stored_admin` (`core/native_admins.py:291`, `get_account(..., for_share=lock)`), and `return stored.is_admin` (`github_panel.py:101`) decides. Since the merge of main's channel-admin removal (#1081) the role is the account row alone. |
| `ReadRule`, `RuleDeny` | **Historical (no current code path).** After #1081, `requester_manages_agent` reads no rule and `can_manage_agent_github` returns `caller.is_server_admin`. At `9441d430d`, when the role did not grant, `requester_manages_agent` continued to `can_manage_agent_github` (`github_panel.py:162`), then `core/agent_reach.py` `_caller_locality` (`:354`) and `_caller_holds` (`:404`), which calls `load_access_policy` (`core/stores/access_policy.py:137`, `:418` in `agent_reach.py`). The ownership rule decides because `operation_policy.py:120` requires `is_held_by_caller`. At `9441d430d` this read is plain; the fixed design reads it `FOR SHARE` in the same transaction. |
| `Commit` | `linked_grant` `:338-400`: `upsert_linked_agent_authorization`, `upsert_github_app` (`core/stores/github_app_installations.py:55-94`, rebuilds `repo_full_names` from the active `TenantGitHubRepo` rows), `enqueue_reconciliation`, `stage_grant`, `clear_exclusion`. |
| `RevokeRole` | `core/stores/accounts.py` `set_role` (one `UPDATE` of `Account`). Channel-admin rows no longer grant authority after #1081. |
| `RevokeRule` | Historical, as `ReadRule`. `core/stores/access_policy.py` `set_access_policy` (`:159`) and `clear_access_policy` (`:182`): an `UPDATE` or `DELETE` of the tenant's `TenantAccessPolicyRecord` row. |
| `Unlink`, `Rotate` | `core/stores/github_links.py` unlink and generation rotation. |
| `RemoveCoverage`, `ReconcileInsert` | `core/stores/github_installation_reconciliation.py` `finish` (`:232-342`) replaces the installation's repo set, suspension, and existence. |
| `RevokePermission` | External: GitHub changes the user's repo permission after the GET answered. |

## Bounds and scaling

| Quantity | Model | Why this size |
| --- | --- | --- |
| People | 2 (`manager`, `member`) | Smallest set where one can be a manager of one agent and not another. |
| GitHub users | 2 | A forwarded link needs a second identity. |
| Agents | 2 (`shared`, `own`) | Manager pairs differ per agent. |
| Repos | 2 | The removed repo and a bystander. Removal only targets the requested repo, so a third adds nothing. |
| Installation row | present or absent at the start (both initial states) | The late-snapshot code treats a missing row differently from a row that lost the repo. |
| Authority sources | 2: role pairs `{manager:shared, manager:own}`, rule pairs `{manager:own, member:own}` | `manager:shared` holds by role only, `member:own` by rule only, `manager:own` by both. Revoking one of two sources must leave the other authority standing. |
| Requests | 1 | Pending-batch behavior is in `formal/github_linked_pending/`. |
| Link generation | 0 to 2 | One unlink or rotation plus the callback's increment. |

The model collapses the account role and the channel-admin rows into one set `roles` of (person, agent) pairs, and the access-policy ownership rule into `rules`. In the code the policy path also needs the caller's administered channels (conjunction of channel-admin rows and the rule); a revocation of either has the same stale shape, so the model keeps one write per source. The deployment default is not modelled. `Discover` folds the token read, the installation listing and the installation detail call into one step; the interleavings between those calls are not separate actions because everything they read is rechecked under the transaction's locks. Link, user, and installation rows are locked `FOR UPDATE` from `Begin` on, so `Unlink`, `Rotate`, `RemoveCoverage`, and `ReconcileInsert` cannot run in `begun` or `roleRead`; a writer would wait there. `RevokeRole` is enabled in `checked` and `begun` always, and after `ReadRole` only when the role read takes no lock. `RevokeRule` is enabled in `checked`, `begun` and `roleRead`, and in `authRead` only when the rule was not read with a lock (`ruleLocked`): a role that grants ends the check before the rule is read, so the rule row is never locked on that path, and a rule revocation there does not matter because the locked role still grants. Stages: `begun`, then `roleRead` (role did not grant) or `authRead` (decided), then `done`.

## Properties

| Property | Meaning |
| --- | --- |
| `NoForeignLink` | A saved link belongs to the person who proved the chat identity. |
| `SingleUseLink` | The intent saves at most one link. |
| `GrantRestsOnCurrentAuthority` | At commit the requester holds a role or a policy rule now, the link and generation are the ones the lookup used, and the repo is covered. This is stricter than serializability: a revocation that committed before the grant counts against it, even though "grant, then revocation" is a valid serial order. |
| `NoGrantAfterCoverageRemoval` | No grant is saved for a repo whose coverage was removed after the lookup found it. |
| `CoverageNotRestoredByGrant` | A commit never turns a repo that the installation row did not cover into a covered one. |
| `NoStaleAuthority` | When the authority check passes, the requester holds a role or a rule now: no role or rule revocation committed between the snapshot and the read that decided. |
| `NoStalePermission` | Checked only in `permission_gap`; GitHub permission is not re-read in the transaction. |
| `EveryRequestResolves` | Every started request ends `done` or `denied`. Weak fairness on the request's own steps (`ProgressStep`), no timeout shortcut, no fairness on the world's actions. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `clean` | clean | 32,294 | All properties above hold within the bounds: identity, single use, coverage, role and rule both read under lock. |
| `no_platform_identity` | violates `NoForeignLink` | 233 | A forwarded link can attach another person's GitHub account. |
| `no_commit_recheck` | violates `GrantRestsOnCurrentAuthority` | 15,736 | A revoked role or rule, relink, or coverage change can be accepted. |
| `permission_gap` | violates `NoStalePermission` | 19,184 | GitHub permission can be revoked after its response and before commit. A real, accepted limit. |
| `late_snapshot_sets_only` | violates `NoGrantAfterCoverageRemoval` | 25,653 | The spike's bug: no repo-covered requirement at the late snapshot or under the lock. |
| `lock_check_only` | clean | 38,310 | Witness: the repo-covered check under the installation lock alone is enough. |
| `late_check_only` | clean | 32,294 | Witness: the late-snapshot check plus the lock's name-set comparison is also enough. Same count as `clean` because the lock's repo check adds no reachable behavior. |
| `role_read_unlocked` | violates `NoStaleAuthority` | 13,643 | A plain role read returns the snapshot's old role after a demotion committed. |
| `role_lock_no_abort` | violates `NoStaleAuthority` | 13,642 | Locking the role without PostgreSQL's abort on a row changed since the snapshot would not help. The fix leans on that rule. |
| `rule_read_unlocked` | violates `NoStaleAuthority` | 21,587 | The code at `9441d430d`: role locked, policy rule read unlocked. A rule revoked after the snapshot is still honoured. |
| `rule_lock_no_abort` | violates `NoStaleAuthority` | 21,586 | Same assumption as `role_lock_no_abort`, for the rule row. |
| `role_abort_witness` | violates `RoleAbortNeverHappens` | 13,642 | Witness: the locked role read aborts the grant after a demotion. Shows the clean result is not vacuous for the role. |
| `rule_abort_witness` | violates `RuleAbortNeverHappens` | 21,586 | Witness: the locked rule read aborts the grant after the rule is revoked. |

`GrantRestsOnCurrentAuthority` is also violated in the four role and rule configs and in `late_snapshot_sets_only`; the table names the first invariant listed in each config. The state counts changed when the policy rule became a second authority source (25,926, 14,208, 17,220, 22,380, 30,438, 12,519 and 12,518 before). All counts are from `-workers 1 -seed 1 -fp 0` and were identical in two independent runs (one through `formal/check.sh`).

## Counterexamples in plain words

1. `late_snapshot_sets_only`: a manager asks to grant a repo. The lookup finds it covered. While the repo permission GET is outstanding, reconciliation drops the repo from the installation's repo set. The late snapshot reads the set without the repo and records it as the baseline. The transaction compares the baseline with the current set, finds them equal, and commits. `upsert_github_app` then rebuilds `repo_full_names` from active authorization rows, which now include the new one, so the removed repo is covered again. The grant exists and so does the coverage that was removed.
2. `role_read_unlocked`: the grant transaction takes its snapshot while the requester is a manager. The requester is demoted and the demotion commits. The transaction reads the role from its snapshot, still sees manager, and commits a grant for someone who was already demoted when it committed.
3. `role_lock_no_abort`: same, except the transaction holds a share lock on the role row after reading it. The demotion committed before the lock, so the lock changes nothing unless the database aborts the transaction for reading a row changed after its snapshot.
4. `rule_read_unlocked` (the code at `9441d430d`): a member whose only authority over the agent is its policy ownership rule asks for a grant. The grant transaction takes its snapshot, locks the member's account row (not a manager, so the role does not grant), and goes on to read the access policy with a plain read. An operator removes the rule and commits. The transaction reads the policy from its snapshot, still sees the rule, and commits a grant for a person who no longer owns the agent. The role lock does not conflict with the policy writer, which is why locking one source is not enough.
5. `rule_lock_no_abort`: same, with a share lock on the policy row that does not abort. The lock is taken after the revocation committed, so it changes nothing without the abort rule.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| In a SERIALIZABLE (like REPEATABLE READ) transaction, `SELECT ... FOR SHARE` or `FOR UPDATE` on a row that another transaction updated or deleted after this transaction's snapshot fails with a serialization failure (SQLSTATE `40001`) instead of returning the old row. | [PostgreSQL 16, transaction isolation](https://www.postgresql.org/docs/16/transaction-iso.html), Repeatable Read (`SELECT FOR UPDATE` and `SELECT FOR SHARE` search for target rows like `SELECT`; if the first updater commits, the repeatable read transaction is rolled back with `could not serialize access due to concurrent update`; paraphrased) and Serializable ("works exactly the same as Repeatable Read except that ..."). The account read uses `with_for_update(read=True)` (`core/stores/accounts.py:84-90`). | Supported by the documentation; not replayed against the project's database. `role_lock_no_abort` and `rule_lock_no_abort` show what breaks without it. |
| The access-policy rule lives in one row per tenant (`TenantAccessPolicyRecord`), and every policy writer (`set_access_policy`, `clear_access_policy`) updates or deletes that row. A `FOR SHARE` lock on it therefore conflicts with every writer. | `core/stores/access_policy.py:137-190`. The writers also take the policy-write advisory lock and a `Tenant` row lock first (`:56-132`); those do not conflict with the grant's locks, which is the gap Astra found. A tenant with no policy row has nothing to lock, and a first `INSERT` of a row is not modelled. | Code read; the fixed locking is the design under review, not yet read in the diff. Re-check after the Python change. |
| A role that grants ends the check before the policy is read (`if stored.is_admin: return True`), so the design need not lock the rule for an admin. If the design locks the rule on every path, the model stays clean. | `core/github_panel.py:143` at `9441d430d`. | Code supported at `9441d430d`; re-check after the Python change. |
| A row held `FOR SHARE` makes a later `UPDATE` of that row wait until the holder commits or rolls back (`UPDATE` takes `FOR NO KEY UPDATE`, which conflicts with `FOR SHARE`). | [PostgreSQL 16, explicit locking, row-level locks](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS). | Supported by the documentation; not replayed. |
| Link, user, and installation rows are locked `FOR UPDATE` in the transaction and the writers that change them take the same row locks, so they wait. | `get_account_link_for_update`, `get_user_for_update`, `get_for_update` in `linked_grant`; writers in `core/stores/github_links.py` and `github_installation_reconciliation.py` were not read line by line for lock use. | Partly supported; writer-side locking unsupported. |
| Commit-time serialization failure only aborts. It never makes a commit succeed that the model forbids. | PostgreSQL SSI documentation (same page). | Supported. |
| `upsert_github_app` rebuilds `repo_full_names` from active authorization rows and leaves those rows as they were when coverage is removed. | `core/stores/github_app_installations.py:55-94`; whether reconciliation deactivates authorizations on removal was not checked. | Code supported for the rebuild; the "rows stay" half is unsupported. |
| A row present with the repo absent is rejected as `changed_try_again` even when the repo was simply never listed locally. | `linked_grant` late snapshot and lock check. | Supported; conservative. |

## Not modelled

Multiple requests, OAuth provider failure, token refresh internals, repo renames, read and write levels, the second-installation restriction (`stage_grant` raising), owner-wide grants (`formal/github_owner_grant/`), installation row deletion after the late snapshot, suspension separate from repo removal, a repo re-covered by reconciliation, serialization failures from rw-dependency cycles (they abort, so they only remove behaviors), the conjunction of channel-admin rows and the policy rule on the policy path (folded into `roles` and `rules`), rule insertion or a tenant with no policy row, and a role or rule *granted* after the snapshot (it only adds authority). GitHub permission is not re-read in the transaction (`permission_gap`). The platform's own group membership is outside the database and can change after the policy lookup.

## Replay status

Provisional. The model has not been replayed against PostgreSQL or GitHub. Counterexample tests in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (names as of `9441d430d`; re-check them if the file moves): `test_coverage_removed_while_the_repo_permission_get_is_outstanding` for counterexample 1; `test_unlocked_role_read_misses_a_demotion_after_the_first_select`, `test_locked_role_read_fails_the_grant_when_a_demotion_commits_after_the_snapshot` and `test_locked_role_read_waits_for_the_grant_when_the_demotion_comes_later` for 2 and 3, plus `test_direct_tool_demotion_after_the_first_select_changes_nothing` and `test_resume_demotion_after_the_first_select_changes_nothing` for the two entry points. Counterexamples 4 and 5 (policy rule revoked after the snapshot) have no test yet; one is needed that revokes the rule between the snapshot and the policy read and expects `CHANGED_MESSAGE`. Whether the existing tests fail before the fix was not checked here. Upstream staging contract tests for GitHub: 0 run. Real traces validated: 0.

Line references for `ReadRole` and `RevokeRole` are to the branch after the #1081 merge; the rest are to `9441d430d`. After that merge the code has one authority source, the requester's account row, read `FOR SHARE` in the commit-time recheck. The policy-rule actions stay in the model and record the design the merge made unnecessary. The role path the code keeps is unchanged and is checked by the same `safe` config, but no config checks the role-only code with `rules` empty from the start.
