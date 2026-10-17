# GitHub `owner/*` grant model

This bounded TLA+ model asks whether a minted token can contain a repo outside
the tenant-associated installation's current coverage, a repo explicitly
removed from an active `owner/*` grant, or a repo with no grant. It also checks
that newly covered repos can join an existing `owner/*` grant. The source of
intended behavior is the private linked-grants `CONTRACT.md`, section 5.
Counterexample behavior is replayed by `packages/core/tests/test_github_owner_grants.py`.

Run `python3 formal/github_owner_grant/run_models.py` from the repository root.
Set `TLA2TOOLS_JAR` to override the default jar path. The runner uses one TLC
worker, a 45-second timeout, a 64 MiB metadir cap, and deletes each metadir.
`expected.tsv` pins both verdict and distinct-state count. The central
`formal/expected.tsv` also runs these configurations through `formal/check.sh`.

## Model and contract mapping

| Action | Implementation |
| --- | --- |
| `GrantSingle(r)` | `github_linked_grant.py:88` verifies single-repo authority; `github_linked_grant.py:318` saves and `:330` clears the exclusion. |
| `GrantOwner` | `github_linked_grant.py:340` checks every covered repo and rechecks manager, link and installation in the serializable commit; `github_owner_grants.py:51` saves the grant. |
| `RemoveSingle(r)` | `github_remove_repo.py:41` removes the single grant and calls `github_owner_grants.py:124` when `owner/*` is active. |
| `RemoveOwner` | `github_remove_repo.py:41` calls `github_owner_grants.py:105`, which leaves exclusions untouched. |
| `CoverRepo(r)` / `UncoverRepo(r)` | `github_installation_reconciliation.py:248` replaces the GitHub snapshot; `github_owner_grants.py:150` reads that snapshot at resolution. |
| `Suspend` / `Unsuspend` / `DeleteInstallation` | `github_installation_reconciliation.py:233` and `:251` update installation state; `github_owner_grants.py:150` filters inactive installations. |
| `Mint` | `github_app_session.py:397` resolves and groups names; `github_app_auth.py:238` mints name-scoped tokens. `github_issued_tokens.py:197` checks live coverage and exclusions for issued tokens. |

One owner, one agent, one installation, and three repos stand in for arbitrary
names. `alpha` and `beta` start covered; `future` starts uncovered. A single
grant can only be made while its repo is covered. Revoking a single grant under
`owner/*` records a durable removal intent; `GrantSingle(r)` explicitly clears
that intent. Removing the owner grant leaves exclusions in place so a later
wildcard grant cannot silently restore an explicitly removed repo. `token` is
the latest minted repo set. The three safety monitors are set during `Mint`,
using the coverage and grant state **at that mint**; later installation changes
cannot retroactively invalidate an earlier token. No liveness claim is made.

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `clean` | clean | 66,096 | Every mint respects current coverage, removal intent, and grants. |
| `no_exclusion` | violates `ExcludedNeverMinted` | 114 | Dropping only the single row under `owner/*` reintroduces the repo. |
| `mint_from_cache` | violates `MintedSubsetCovered` | 133 | A grant-time coverage snapshot mints a repo after installation coverage drops it. |
| `future_repo_witness` | violates `FutureRepoNeverMinted` | 139 | Positive witness: `owner/*` mints a newly covered repo that was absent at grant time and has no single grant. |

Counterexamples in plain words:

1. `no_exclusion`: grant `owner/*`; explicitly remove `alpha`; mint. Without an
   exclusion, `alpha` is in the token even though its removal intent is active.
2. `mint_from_cache`: grant `owner/*` while `alpha` is covered; drop `alpha`
   from installation coverage; mint. The cached grant-time set still puts
   `alpha` in the token. Suspension or deletion can cause the same class of
   stale-coverage error.
3. `future_repo_witness`: grant `owner/*` while `future` is uncovered; cover
   `future`; mint. The token contains `future` without a single-repo grant.

## Assumptions and limits

| Assumption | Basis |
| --- | --- |
| A grant action starts after the caller's identity, manager, GitHub permission, and commit-time checks have passed. | `CONTRACT.md` §§4–6; those checks belong to the spike grant model. |
| Installation coverage at `Mint` is the latest stored GitHub snapshot when one exists, else the confirmed set; it can lag GitHub. | `github_owner_grants.py:150`; no staging trace yet. |
| A single mint returns exactly the resolved set for one installation. | `github_app_session.py:397`; the 500-repo cap, token splitting, and GitHub API behavior are outside this three-repo model. |
| Exclusions survive owner-grant removal and regrant until explicit single-repo regrant. | `github_owner_grants.py:105`, `:124`, `:138`. |
| Mint, grant, exclusion, and coverage transitions are atomic here. | Abstraction; actual interleaving boundaries must be checked against code when implemented. |

The model does not cover multiple installations, access levels, grantor
authorization, token expiry, commit-time authority, or GitHub's API response.
Counterexample tests: `no_exclusion`, `mint_from_cache`, and `future_repo_witness`
are replayed against the store and mint path in
`packages/core/tests/test_github_owner_grants.py` using PostgreSQL and
httpx MockTransport. Upstream staging contract tests: 0 run. Real traces:
0 validated. The clean verdict remains bounded and provisional for GitHub's
live API behavior.
