# GitHub session tokens across installations

A session with grants in installations A and B needs a separate exact-scope token for each. A repo-aware git credential helper must return B's token for B's repo, and nothing for an uncovered repo. A change to A keeps B's token when the session remains in multi mode. Crossing between one and multiple installations re-mints all remaining groups into the target mode. M1 supplies per-installation token issuance, rotation, and saved issue times. M2 adds helper routing. M3 allows grants across up to 12 installations.

## Models and code boundaries

| Model action or state | Code boundary / status |
| --- | --- |
| `GrantInstall.LockA/B`, `WriteA/B` | `packages/core/daimon/core/stores/github_access.py:stage_grant` holds the per-agent grant lock while enforcing the installation cap and writing a grant |
| `GrantInstall.UnsafeReadA/B`, `UnsafeWriteA/B` | Rejected split read/write of the same grant operation; `lock_agent_grants` at `github_access.py:406` prevents this |
| `GrantCap.Stage` | `github_access.py:stage_grant` counts distinct installations and stages one grant under the per-agent lock; three model installations with a cap of two stand in for production's cap of 12 |
| `GrantAdmission.Stage` | `github_access.py:stage_grant` checks projected vault chunks and read-only access under the same lock before insertion; `projected_session_credential_count` is shared with `github_app_session.py` |
| `MultiInstallation.Init`, `Eligible`, `ExactLiveScope` | `_effective_rows`, `prepare_app_access`, and `group_repository_access` create an exact-scope token per installation and 500-repo chunk; `stores/github_issued_tokens.py:create_pending` records scope and expiry |
| `ClearWorking`, `AliasSelected` | `prepare_app_access` records the full desired working repo; `_combine_app_access` selects the alias only from its token group; `add_app_credentials` omits `GH_TOKEN` and Copilot when no alias exists |
| `FailChunkZero`, `MintAllChunks`, `RebuildAfterBRotation` | `prepare_app_access` discards every chunk of a failed installation; `create_pending` saves the chunk label with the inventory row; `_current_app_access` reads that label, and `add_app_credentials` syncs its credential name |
| `RevokeAChunk`, `DetectChunks`, `MissingChunkDetected` | `stores/github_issued_tokens.py:session_installations_needing_rotation` compares the complete grant-derived chunk set with the current inventory set; a revoked row is absent even if its sibling remains |
| `SweepContractA`, `DetectSnapshotContraction`, `SweptContractionDetected` | The same detector compares saved snapshot scopes with desired grants, so sweeping all A rows cannot hide an A-only contraction |
| `ContractAChunks`, `TickChunks`, `DetectDue`, `RetainedTokenAge`, `PerTokenDue` | Within multi mode, `github_app_session.py:_combine_app_access` retains unaffected token records; `session_snapshot.py:SessionSnapshot.token_scopes` stores each issue time; `session_compat.py:decide_session_compatibility` refreshes an installation when any chunk reaches 40 minutes |
| `TransitionToSingle`, `ModeTransitionShape` | A change between one and multiple installations re-mints all remaining groups into the target mode; single mode has no chunk labels or saved token scopes |
| `DeleteObsolete`, `UpdateExisting`, `CreateNew`, `VaultBudget` | `github_app_session.py:add_app_credentials` deletes obsolete `GH_TOKEN*` and Copilot credentials, updates survivors, then creates new names; `require_vault_budget` checks the final count |
| `Read(r)`, `Return` | `credential_env.py:render_github_repo_map` and `github_repo.sh` select the exact `GH_TOKEN_I<installation>_<chunk>` placeholder by normalized owner/repo. Ordinary `gh` lookups deny unknown repos; Git credential `get` returns nothing unless HTTPS, github.com, and the requested path match the map and clone origin. `github_app_session.py:add_app_credentials` supplies the per-token variables. A helper read racing with revocation keeps the captured scope, while freshness still relies on GitHub-side revocation. |
| `RevokeA` | `github_access.py:remove_grant` changes the grant; `stores/github_issued_tokens.py:select_stale_tokens` identifies A's row; `github_app_session.py:revoke_app_access` revokes the selected token without touching B |
| `ReassignA` | `session_snapshot.py:fingerprint_identity` and `session_compat.py:identity_change_reasons` compare canonical `token_scopes` even when repo URLs are unchanged |
| `AddRepoB` | `github_access.py:stage_grant` allows B while the agent stays under the cap; `session_compat.py:decide_session_compatibility` selects an in-place B refresh for a new unmounted repo, and `rotate_live_app_tokens(installation_ids=(B,))` mints B's new scope while retaining A |
| `Expire(i)`, `Rotate(i)` | `github_app_session.py:prepare_app_access` records a conservative 55-minute expiry; `turn/prepare.py` saves per-installation issue times; `session_compat.py:decide_session_compatibility` selects due installation IDs; `session_update_ops.py` passes them to `rotate_live_app_tokens` |
| `SessionToken.VersionBump`, `Mint`, `SwapResource`, `SwapVault` | Grant version update in `github_access.py:stage_grant`; `prepare_app_access` mints; `rotate_live_app_tokens` swaps resources and `add_app_credentials` syncs the vault |
| `SessionToken.LockDelivery`, `Deliver`, `LockRetire`, `RetireOld` | `finish_app_delivery` locks grants and delivers new rows; `rotate_live_app_tokens` locks again before `mark_session_tokens_superseded` |
| `SessionToken.SelectSweep`, `RecheckSweep`, `RestoreOld`, `RemoveGrant` | `stores/github_issued_tokens.py:select_stale_tokens` rechecks inventory; rotation rollback calls `restore_session_tokens`; `github_access.py:remove_grant` removes access |
| `SessionToken.Crash`, `Unsafe*` | Existing crash, cleanup, rollback and long-lock fault witnesses; see `SessionToken.tla` and the prior single-token lifecycle paths above |

`GrantInstall` allows both installations and checks that completed grants persist. `MultiInstallation` has token scope, validity, expiry and generation per installation, plus separate chunk detection, age, and vault delivery actions. `Read` captures the helper's token selection; `Return` may run after a revocation. A fresh read never selects an expired or revoked token. A token selected before revocation can still be returned, but `ResultScoped` requires the captured token to have belonged to the requested repository and installation. `NoStaleUsable` assumes GitHub invalidates the token at revocation, so the returned placeholder cannot authenticate. `AddRepoB` re-mints B with its new exact scope and leaves A unchanged. `SessionToken` retains the detailed mint, delivery, and rollback races for one token. The multi-installation model still treats each token's rotation as one atomic transition.

`GrantCap` checks three distinct installations against a scaled cap of two. Its unsafe variant admits the third. The production limit is 12 (`github_access.py:MAX_AGENT_INSTALLATIONS`); the model checks the same count-before-insert rule under the per-agent lock, without enumerating all 13 installations. `test_github_access_foundation.py` replays the boundary with 13 installations and confirms that another repo in an existing installation still succeeds.

`GrantAdmission` uses two repositories per token chunk and a vault cap of five, scaled from 500 and 15. Its budget start has two full token chunks, a working-repo alias, Copilot and the reserved chat MCP credential. A new A repo requires a third chunk and is refused. Its read start has a read-only A grant, so a B grant is refused before multi mode. The model makes each admission atomic, as the per-agent grant lock does in `stage_grant`; the 11-installation concurrency test checks that lock against PostgreSQL.

For multi-installation sessions, each saved `token_scopes` entry contains `(installation_id, chunk, repo_ids, issued_at, token_id)`. The token ID joins the snapshot entry to its encrypted inventory row. `create_pending` saves `chunk` under the reserved `__credential_chunk__` key in that row's existing JSONB `grant_versions` column; version checks read only the `grant:<repo>` and `authorization:<repo>` keys. A B-only rotation within multi mode uses the saved chunk of each retained A row. A mode transition re-mints every remaining group. Single mode keeps the old snapshot shape, inventory rows without chunk labels, and refresh timestamp. A failed read-group mint does not advance the healthy write token's refresh deadline.

The model's token ID is an abstraction for a vault credential, not a secret value present in the sandbox. The M2 helper returns the selected vault variable's placeholder; GitHub request-header injection supplies the real token (`packages/core/daimon/core/github_app_session.py:add_app_credentials`; `packages/core/daimon/core/github_repo.sh`). The shell replay in `packages/core/tests/test_github_repo_helper.py` checks exact selection, denied lookups, protocol and path checks, and clone credential configuration that resets inherited helpers. `NoStaleUsable` describes whether GitHub accepts that placeholder after substitution.

## Bounds and assumptions

| Item | Model bound | Source / limit |
| --- | --- | --- |
| Installations and repos | Two installations; A starts with `a`, B with `b`; `c` can be granted in B and `a` can move to B | `github_app_session.py:401-408` groups arbitrary live rows; this bound is a model choice |
| Token generations | Two per installation | Model choice for one rotation or B re-mint; production has no two-generation cap |
| Expiry | A Boolean per installation for helper reads; two B chunks with ages 39 and 40 for refresh | `github_app_session.py:prepare_app_access` records a conservative 55-minute expiry; `session_compat.py:decide_session_compatibility` refreshes when any chunk reaches 40 minutes |
| Repository inventory | At most three repos | `stores/github_issued_tokens.py:create_pending` accepts 1..500 IDs per token |
| Alias and mint chunks | Working installation A or none; three ordered A chunks | `prepare_app_access` chooses the working repo's token; `group_repository_access` sorts IDs within each chunk, and a failed multi-installation mint discards every chunk of that installation |
| Vault credentials | 12 named tokens, alias, Copilot, and one other begin at 15; two named tokens are added | `add_app_credentials` deletes obsolete alias and Copilot credentials before creating names; `require_vault_budget` enforces the 15-credential cap before mint. Single-installation sessions use only `GH_TOKEN` for App access |
| Helper read and return | Two actions, with grant/revoke/expiry/rotation allowed between them | `github_repo.sh` reads `github-repos.tsv` and uses the selected vault placeholder. The shell test replays scope selection and denial; the GitHub-side revocation timing remains an unsupported assumption until staging. |
| Upstream revocation | `invalidated` gains the old token ID at the same model step as grant removal | Unsupported atomicity assumption. `github_app_session.py:revoke_app_access` calls GitHub over the network; a failed call or a commit-to-sweep delay is not modeled |

`NoStaleUsable` is conditional on that upstream revocation assumption. The `MultiNoUpstreamRevoke` counterexample shows the gap: the helper reads A, A's grant is removed, and it returns A's placeholder while GitHub still accepts the underlying token. The result is provisional until a contract test and a real execution replay verify revocation timing. The repository-to-installation mapping is assumed unambiguous at lookup time. `UnrelatedSurvives` checks A revocation against B; the symmetric B case is assumed. The existing single-token race model does not re-prove full-set delivery and rollback.

## TLC results

Run `TLA2TOOLS_JAR=/home/derick/scratch/daimon-os-devenv/tla2tools.jar FORMAL_CHECK_FILTER=github_session_token formal/check.sh`. The checker uses one TLC worker, a timeout, and a metadir cap. `formal/expected.tsv` pins verdicts and distinct-state counts.

| Config | Verdict | States | User-visible failure shown |
| --- | --- | ---: | --- |
| `GrantSafe` | clean | 8 | Both concurrent grants persist |
| `GrantUnsafe` | `CompletedGrantsPersist` | 31 | A stale write loses a granted installation |
| `GrantCapSafe` | clean | 10 | A third installation is refused at the scaled cap |
| `GrantCapUnsafe` | `WithinCap` | 8 | Without the cap, a third installation is granted |
| `GrantAdmissionBudgetSafe` | clean | 2 | A new chunk is refused when the vault is full |
| `GrantAdmissionBudgetUnsafe` | `WithinBudget` | 2 | Admitting that chunk exceeds the vault cap |
| `GrantAdmissionReadSafe` | clean | 2 | Read-only A blocks a B grant |
| `GrantAdmissionReadUnsafe` | `MultiWrite` | 2 | B joins a read-only A grant and token minting cannot proceed |
| `MultiSafe` | clean | 5,593 | Exact scope, independent expiry/rotation, B re-mint, and read/return race |
| `MultiSingleToken` | `GrantedReachable` | 3 | B repo cannot be reached after the guard is removed |
| `MultiFallback` | `ResultScoped` | 28 | Helper returns A's token for uncovered B repo `c` |
| `MultiRevokeAll` | `UnrelatedSurvives` | 5 | A revocation also invalidates B's token |
| `MultiOldFingerprint` | `FingerprintExact` | 5 | Reassignment keeps the old map fingerprint |
| `MultiExpiredLookup` | `FreshRead` | 55 | Helper reads A's token after A expires |
| `MultiExpireAll` | `ExpiryIsolation` | 8 | A expiry also invalidates B |
| `MultiRotationStaleB` | `RotationRefreshes` | 11 | B rotation leaves B's old token in place |
| `MultiGrantStaleScope` | `ExactLiveScope` | 7 | New B grant leaves B's live token without `c` |
| `MultiGrantOldFingerprint` | `FingerprintExact` | 7 | New B grant leaves the old mapping fingerprint |
| `MultiGrantTouchesA` | `GrantIsolation` | 7 | New B grant kills A's token |
| `MultiNoUpstreamRevoke` | `NoStaleUsable` | 95 | Read, revoke, return yields a token GitHub still accepts |
| `MultiAliasSafe` | clean | 14,612 | No working repo keeps `GH_TOKEN` absent through B-only rotation |
| `MultiAliasFallback` | `AliasSelected` | 108 | Partial B rotation introduces an unrelated alias |
| `MultiAtomicSafe` | clean | 42,801 | Complete chunks retain stable names; a failed installation discards all chunks |
| `MultiPartialMint` | `AtomicChunks` | 12 | Failed chunk zero leaves later A chunks live |
| `MultiRenumberNames` | `StableNames` | 109 | B rotation renumbers surviving A rows but skips an unchanged stale vault name |
| `MultiChunkSafe` | clean | 3 | Revoking A's second chunk is detected |
| `MultiAnyRowDetection` | `MissingChunkDetected` | 3 | A's surviving chunk hides its revoked sibling |
| `MultiSnapshotContraction` | clean | 3 | Saved scopes identify A after its rows are swept |
| `MultiSweptContractionMissed` | `SweptContractionDetected` | 3 | Live-only comparison misses swept A |
| `MultiModeTransitionSafe` | clean | 2 | Contraction re-mints B into unlabelled single-mode rows |
| `MultiTransitionKeepsLabels` | `ModeTransitionShape` | 2 | Contraction carries multi-mode labels into single mode |
| `MultiAgeSafe` | clean | 4 | Contraction retains both B issue times and the 40-minute refresh |
| `MultiResetAgeOnContraction` | `RetainedTokenAge` | 2 | Contraction resets B's token age |
| `MultiVaultSafe` | clean | 6 | Delete, update, create stay within 15 credentials |
| `MultiCreateBeforeDelete` | `VaultBudget` | 3 | New token creation reaches 16 before obsolete credentials are deleted |

The existing `SessionToken` configs remain in `formal/expected.tsv`: `Safe` is clean at 50 states, and each unsafe config still witnesses its named lifecycle invariant. `GrantUnsafe` loses one completed grant when two writers act on stale reads. Database tests replay the revoked A chunk with B live, B-only remint, retained token ages, and vault deletion before creation. Chunk labels `0`, `1`, and `2` abstract sorted repository-ID chunks. M2 shell tests replay helper selection; the read/return interleaving with GitHub-side revocation still needs a real execution replay.
