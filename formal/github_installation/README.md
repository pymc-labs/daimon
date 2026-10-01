# GitHub App installation repository cache

Run the bounded configurations from the repository root with the pinned TLA+
tools jar:

```sh
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationUpdatesStaleWrite.cfg \
  formal/github_installation/InstallationUpdates.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationUpdatesSafe.cfg \
  formal/github_installation/InstallationUpdates.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationDeliveryOrderCreated.cfg \
  formal/github_installation/InstallationDeliveryOrder.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationDeliveryOrderDeltas.cfg \
  formal/github_installation/InstallationDeliveryOrder.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationReconciliation.cfg \
  formal/github_installation/InstallationReconciliation.tla
```

## Implementation mapping

| Model | Implementation |
| --- | --- |
| `ReadA` / `ReadB` / `ReadRemoval` then stale writes | The earlier `add_repos` and `remove_repos` read the array with `_load`, computed a replacement in Python, and passed it to `upsert`. |
| Atomic `WriteA` / `WriteB` / `WriteRemoval` | [`github_app_installations.py`](../../packages/core/daimon/core/stores/github_app_installations.py) `add_repos` / `remove_repos` update the array in one PostgreSQL `UPDATE`. |
| `EachCompletedDeltaIsPresent` | [`test_github_app_installations.py`](../../packages/core/tests/stores/test_github_app_installations.py) holds three real PostgreSQL readers at a barrier, then releases two adds and one remove against independent connections. |
| `DeliverCreated` / `DeliverAdded` / `DeliverRemoved` | [`InstallationDeliveryOrder.tla`](InstallationDeliveryOrder.tla) records the webhook-only behavior that produced the original stale states. Current webhooks enqueue refresh work instead of applying these event payloads to the cache. |
| `Notify` / `FetchComplete` / `CommitCurrent` / `DiscardStale` | [`github_installation_reconciliation.py`](../../packages/core/daimon/core/stores/github_installation_reconciliation.py) coalesces event generations and only writes a complete repository snapshot for the current lease and generation. |
| Authoritative `FetchComplete` | [`github_app_auth.py`](../../packages/core/daimon/core/github_app_auth.py) uses a metadata-only installation token and reads every page from `GET /installation/repositories`; an API or payload failure leaves the last complete cache unchanged and the job retryable. |
| Delete fence | The `installation.deleted` webhook clears the cache and advances the same durable generation. A result already in flight cannot write across that generation change. The TLA+ model checks the generation fence only; immediate cache deletion is covered by executable tests, not modeled. |
| `CompletedSnapshotIsCurrent` | Signed webhook tests in [`test_webhooks_github.py`](../../packages/adapters/mcp/tests/test_webhooks_github.py) run both reported delivery orders through PostgreSQL and a deterministic GitHub API fake, then compare the cache with the fake's authoritative set. |

GitHub [documents that webhook deliveries can arrive out of order](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/troubleshooting-webhooks#webhooks-deliveries-are-out-of-order)
and suggests using payload timestamps when comparing event time. The [installation event schema](https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation)
describes a repository snapshot; the [installation_repositories schema](https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation_repositories)
describes added and removed deltas. Those schemas do not define a per-event
revision or comparable sequence value for ordering the snapshot against deltas.
The installation object may have resource timestamps, but the docs do not say
they identify the repository-set revision represented by each delivery. The
generic ordering guidance therefore does not support a source-grounded
freshness comparison for these events.

## Bounds and findings

`InstallationUpdates` models one installation and three overlapping database
transactions: two add different names, and one removes a name initially
present. It isolates lost updates caused by replacing the array from a stale
read. Atomic SQL expressions preserve those concurrent deltas.

`InstallationDeliveryOrder` models one installation, one changed repository,
and at most three deliveries. In the `Created` configuration the creation
snapshot is `{base}`, an add happens later, and GitHub's final set is
`{base, delta}`. In the `Deltas` configuration an add happens before a removal,
so the final set is `{base}`. TLC can deliver the events in any order. The
model applies the handler's snapshot, union, and subtraction behavior and
checks the cached set after all deliveries. Both configurations find
counterexamples: a delayed creation snapshot can lose an earlier-delivered
delta, and opposite deltas delivered out of occurrence order can leave stale
membership.

The original signed-webhook PostgreSQL tests established those stale outcomes.
They now run the reported delivery traces through the refresh queue and verify
the completed PostgreSQL snapshot against a deterministic GitHub API fake.

The current handler treats `installation` and `installation_repositories`
deliveries as refresh notifications. It records a receipt and advances a
coalesced per-installation job before acknowledging the webhook. The scheduler
checks the current installation with the App JWT, mints a metadata-only token,
and fetches the full repository listing. The list is committed in one DB
transaction only if the worker still owns the current generation. A deleted
delivery removes the cached row immediately; a later API lookup can restore it
if GitHub confirms the installation still exists, which handles delayed
deletion notifications without trusting their delivery order.

The cache may be temporarily unavailable while a new installation is
reconciled, and remains at its last complete value during API failures. Durable
retries do not make API availability or webhook delivery a strong freshness
guarantee. Normal clone requests still use a one-repository token, and the
tenant-local recorded access proof remains the authorization gate; the
installation-wide metadata listing only refreshes the deployment cache.

Migration `0026` queues every installation already in the cache, so a
previously stale row gets an initial refresh after rollout without waiting for
another webhook. The page-count check detects missing, repeated, or inconsistent
results, but the REST API does not provide an atomic multi-page snapshot. The
listing therefore assumes repository membership stays stable while its pages
are read; a changed count or malformed page is retried, and freshness remains
eventual rather than immediate or strongly guaranteed.

The delivery-order model uses one installation and distinct valid repository
names. The reconciliation-fence model checks that an older fetch cannot
complete after a newer notification. Neither represents GitHub API failures,
event loss, multiple installations, or PostgreSQL transactions. TLC checks
finite abstractions; the PostgreSQL/API-fake tests provide evidence for the
handler, queue, and client paths they exercise.

TLC 2.19 checked 27 states in the stale-write configuration and found its
counterexample. The atomic configuration checked 93 states without an invariant
violation. The event-order state counts and traces are recorded in
`formal/expected.tsv`. `InstallationReconciliation` checked 27 distinct states
without an invariant violation under its bound of three queued generations.
