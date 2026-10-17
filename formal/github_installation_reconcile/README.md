# GitHub installation reconciliation ordering model

The user-visible risk is a cached installation with the wrong repositories,
suspension flag, or existence after webhook traffic stops. This model checks
whether duplicate and reordered deliveries, changes during a refresh, and a
lease takeover can strand stale cache data or allow an old worker to finish.
It maps to the installation reconciliation implementation on main.
It also checks the deploy ordering: migration 0108 queues the first refresh
while the previous image's scheduler is still running, and that scheduler can
finish the job without writing the snapshot. Every installation must still end
up with a GitHub snapshot.

Run `python3 formal/github_installation_reconcile/run_models.py` from the repo
root. The runner uses one TLC worker, a 180-second limit and 256 MiB metadir cap
per case (the largest case peaks near 177 MiB), removes each metadir, and pins
distinct-state counts in `expected.tsv`; the central `formal/expected.tsv`
runs the same configurations. Liveness is checked once after the
full search (`-lncheck final`); the default checks at timed intervals and makes
the counts of liveness counterexamples vary between runs. Terminal states are allowed because TLA's stuttering step
represents a system at rest.

## Action-to-code map

| Model action | Implementation boundary |
| --- | --- |
| `GitHubChange` | External GitHub event; webhook routing in `packages/adapters/mcp/daimon/adapters/mcp/webhooks.py:182-192`, installation handler at `:208-269` accepts `suspend`/`unsuspend`, repository handler at `:271-325`. Connect saves at `packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py:1925-1941,2143-2159` upsert and enqueue a synthetic refresh. |
| `Duplicate` | A second delivery with the same ID enters the handler at `webhooks.py:245-253` or `:301-309`; GitHub redelivery behavior is unverified. |
| `Enqueue` | `packages/core/daimon/core/stores/github_installation_reconciliation.py:28-102`: receipt conflict check at `:38-51`, generation and pending/running update at `:53-83`, deletion side effect at `:84-101`. The migration's own enqueue is `packages/core/alembic/versions/0108_github_installation_truth.py:32-47`, modelled by `Init` (generation 1, job pending, no snapshot). |
| `Claim` | `github_installation_reconciliation.py:169-208`: pending or expired running row (`:176-185`), `FOR UPDATE SKIP LOCKED` (`:193`), owner and claimed generation. Worker transaction at `packages/core/daimon/core/github_installation_reconcile.py:58-67`. New code only (`~oldCode`). |
| `ReadGitHub` | `github_installation_reconcile.py:79-109`: installation lookup (including suspension), token mint, complete repository listing. These calls are separate in real execution. |
| `LeaseExpire` | `github_installation_reconciliation.py:176-184`, `:205-207`; lease is two minutes at `github_installation_reconcile.py:23` with renewal every 30 seconds at `:24,160-189`. |
| `Finish` | `github_installation_reconciliation.py:232-342`: owner/claim check `:246-260`, generation check `:262-271`, deletion `:273-292`, live snapshot and suspension write `:293-333` (`github_refreshed_at` at `:285,301`, modelled as `refreshed`), done `:335-341`. Connect's `upsert_github_app` at `packages/core/daimon/core/stores/github_app_installations.py:55-94` writes suspension only on insert; reconciliation owns subsequent writes. `LegacyOnlyWrite` represents the old behavior that skipped App truth updates; the remaining `app == "legacy"` guard at `:330` protects Connect's confirmed set. Worker finish transaction is `github_installation_reconcile.py:110-127`. |
| `OldDrain` | The previous image's `drain_github_installation_reconciliations` and `finish`, which did not know `github_repo_full_names` or `github_refreshed_at`. Not in this tree; observed on staging 2026-10-17 (05:07:36 and 05:07:42 claimed and marked `done` with the new columns still null; the new scheduler started at 05:08:43). Modelled as one atomic step that completes the migration's job (generation 1) without writing; the real claim, read and finish are separate. |
| `NewCodeStart` | The new scheduler process starting after the deploy swaps images. Before it, no new-code `Claim` is possible. Fair, because a deploy completes. |
| `Sweep` | `github_installation_reconciliation.py:105-154` (`enqueue_never_refreshed`), called at the start of each drain tick at `github_installation_reconcile.py:46-53` with a per-tick limit of 50 (`:26`). Selects `github_app` rows with no snapshot (`github_refreshed_at IS NULL`) and not removed whose queue row is missing or `done` (`:130`); the conflict clause re-checks `done` (`:150`), so pending, backing-off and running jobs are untouched. |

The model treats each enqueue, claim, and finish database transaction as
atomic. The read and finish are separate, so GitHub can change and a new
delivery can arrive between them. A successful GitHub read is represented as
one complete snapshot; the separate API calls above need replay or a contract
test before that abstraction can be trusted.

## Bounds and properties

| Quantity | Model | Production source or scaling |
| --- | --- | --- |
| Installations | 1 | Per-installation queue row at `github_installation_reconciliation.py:53-64`; independent IDs are omitted. |
| Repositories | 2 | Smallest set showing distinct add/remove choices; no repository cap is assumed. |
| Reconcilers | 2 | Smallest count showing lease takeover; each worker creates an owner UUID at `github_installation_reconcile.py:58`. |
| GitHub changes/delivery IDs | 2 | Smallest sequence showing order reversal and a generation race. Production is unbounded; receipts are retained 30 days at `github_installation_reconciliation.py:25,377-383`. |
| Duplicate deliveries | 1 extra per ID | Enough to exercise receipt deduplication at `github_installation_reconciliation.py:38-51`; production duplicates are unbounded. |
| Lease expiry | 1 | Enough for a takeover. Actual lease and heartbeat are two minutes and 30 seconds at `github_installation_reconcile.py:23-24`. |
| Old-code drains | at most 1, only the generation-1 migration job | Staging showed two jobs consumed, one per installation; with one modelled installation one is enough. A webhook-queued job consumed the same way leaves the snapshot unwritten too and the sweep heals it, but `NoStaleOverwrite` would flag it, so it is outside the bounds. |
| Sweep batch | unbounded | One installation, so the production limit (`github_installation_reconcile.py:26`, 50 per tick) is not modelled; a larger backlog drains over several ticks. |

`NoStaleOverwrite` requires every completed write to be made by the current
lease owner, to keep the highest version previously written, and to have read
at or after the newest *enqueued* event. The version is a model-only witness
for read order; the database does not store it. `EventuallyConsistent` says
that, under weak fairness for each queued delivery and for Claim, ReadGitHub,
and Finish, the cache eventually equals GitHub truth forever once the bounded
environment stops changing. There is no fairness assumption for GitHub
changes, duplication, or lease expiry. The bounded model permits at most one
expiry and assumes each successful read and write can finish.

`EventuallyRefreshed` (`<>refreshed`) says every installation eventually gets
a snapshot or a removal marker: `refreshed` turns true on any `Finish` that
writes, including the deletion branch (`github_installation_reconciliation.py:285-286`).
It relies on weak fairness for `NewCodeStart`, `Sweep`, `Enqueue`, `Claim`,
`ReadGitHub` and `Finish`; there is none for GitHub changes, duplicates, lease
expiry or the old drainer, and lease expiry is never forced. `Init` queues the
migration's job (generation 1), so `NoSweep` with `OldDrainer = FALSE` tests
the pre-fix behaviour without the deploy race. The witness
config negates a reachable state where delivery 2 is received first, a
duplicate is consumed, all deliveries drain, and the cache equals truth.

## TLC results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `Safe` | clean | 607,089 | Both safety and liveness hold, including `EventuallyRefreshed` with the old drainer and the sweep. |
| `NoGenerationCheck` | violates `NoStaleOverwrite` | 1,434 | An old read can complete after a newer event was enqueued. |
| `LegacyOnlyWrite` | violates `EventuallyConsistent` | 126,481 | A `github_app` row never receives the refreshed snapshot (no sweep, as in the old code). |
| `NoLeaseOwnerCheck` | violates `NoStaleOverwrite` | 1,469 | A worker whose lease was replaced can write. |
| `ReorderDuplicateWitness` | witness reached | 21,883 | Reordered delivery and duplicate processing can still converge. |
| `NoSweep` | violates `EventuallyRefreshed` | 303,669 | The staging bug: the old drainer completes the migration's job, nothing re-queues it, and the installation never gets a snapshot. |
| `NoSweepNoOldDrainer` | clean | 303,611 | Without the old drainer the migration's one-shot enqueue is enough; the old drainer is the cause, not the queue. |

Safe, `NoGenerationCheck` and `NoLeaseOwnerCheck` grew when the migration's
job (generation 1, pending) and the deploy window were added to `Init`; the
counts moved because the starting state moved, not because those properties
changed. Counts from before this change are not comparable.

Counterexamples in domain terms:

1. **No generation check:** GitHub changes, worker A claims and reads, then
   another GitHub change is enqueued. A finishes its earlier snapshot and
   marks the job done, losing the newer refresh signal.
2. **Legacy-only write:** GitHub changes a repository or installation state;
   the worker fetches current truth and finishes, but the `github_app` cache
   row is skipped. With no more deliveries, it remains stale.
3. **No lease-owner check:** A claims, its lease expires, and B takes over.
   A can finish while B owns the row. The owner guard is what rejects A's
   result; a later B finish may otherwise be rejected after A marks it done.
4. **No sweep (staging, 2026-10-17):** The migration queues one refresh per
   installation. The previous scheduler is still running, claims the job and
   marks it done without writing the repository list or `github_refreshed_at`,
   because it does not know those columns. The new scheduler starts and finds
   no due job. Unless a webhook, a Connect save or a mint refusal queues a
   refresh later, the installation never gets a snapshot; coverage falls back
   to the Connect-confirmed repositories, and removal and suspension from
   GitHub stay inactive. `Sweep` closes this by re-queuing any unrefreshed,
   unremoved installation that has no pending or running job.
5. **Reordered duplicate witness:** Event 2 is delivered before event 1,
   one ID is redelivered, and the receipt check suppresses its second
   generation bump. A later complete GitHub read brings the cache to truth.

## Assumptions and replay status

| Assumption | Evidence/status |
| --- | --- |
| Each GitHub change eventually emits a webhook that this service receives; duplicate IDs refer to the same change; deliveries can be delayed/reordered. | **UNVERIFIED GitHub webhook delivery semantics.** No vendor contract or staging trace was checked. Liveness depends on eventual delivery. |
| Installation lookup and complete repository pagination behave like one consistent snapshot, including suspension. | **UNVERIFIED.** Actual calls are separate at `github_installation_reconcile.py:84-109`; a change between pages or calls can yield a mixed view. |
| Database enqueue/claim/finish transactions commit atomically and are serialized on the queue row. | Code uses transaction blocks at `webhooks.py:245-253,301-309` and `github_installation_reconcile.py:46-53,58-67,110-127`; SQL row lock at `github_installation_reconciliation.py:193,255`. No database replay was run. |
| GitHub's installation-token contract, observed on staging 2026-10-17 (single run, not a contract test): minting for a repository the installation does not cover returns 422 `There is at least one repository that does not exist or is not accessible to the parent installation.`; a request mixing covered and uncovered repositories returns the same 422 for the whole request; an unknown installation returns 404; a covered request returns 201. | Observed once on staging; not pinned by a test against GitHub. The reconciliation job itself relies only on the 404 meaning uninstalled (`github_app_auth.py:382-383`). |
| The previous image's drain completes a queued job without writing the new columns, and only before the new scheduler starts. | Staging log 2026-10-17: two jobs marked `done` at 05:07:36 and 05:07:42, new scheduler up at 05:08:43, all `github_refreshed_at` null. The old code is not in this tree; the model has no trace-validation run beyond this one incident. Production promote has the same ordering and is expected, not observed, to race the same way. |
| Workers eventually run and successful GitHub API reads/DB writes finish. | Fairness assumption, not an operational guarantee. API failure/retry (`github_installation_reconcile.py:132-153`; a retrying job counts as queued for `Sweep`, so a failing GitHub cannot cause a re-enqueue loop), renewal, receipt expiry, process crash, and account changes are not modeled. |

The results are **provisional**. The `NoSweep` counterexample is replayed
against the real code by
`packages/core/tests/test_github_installation_reconcile_worker.py::test_drain_backfills_snapshot_after_old_scheduler_consumed_migration_job`
(fails before the fix, passes after); the other counterexamples were not
replayed, no GitHub contract test was run, and no real traces were checked
(0 of 0) beyond the staging incident above. The model checks these finite bounds, not production-scale behavior.
In particular, the clean verdict does not establish correctness for mixed
multi-call or multi-page GitHub reads, missing webhooks, or suspension until
the implementation and upstream behavior are verified.
