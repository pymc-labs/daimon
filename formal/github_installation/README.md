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
```

## Implementation mapping

| Model | Implementation |
| --- | --- |
| `ReadA` / `ReadB` / `ReadRemoval` then stale writes | The earlier `add_repos` and `remove_repos` read the array with `_load`, computed a replacement in Python, and passed it to `upsert`. |
| Atomic `WriteA` / `WriteB` / `WriteRemoval` | [`github_app_installations.py`](../../packages/core/daimon/core/stores/github_app_installations.py) `add_repos` / `remove_repos` update the array in one PostgreSQL `UPDATE`. |
| `EachCompletedDeltaIsPresent` | [`test_github_app_installations.py`](../../packages/core/tests/stores/test_github_app_installations.py) holds three real PostgreSQL readers at a barrier, then releases two adds and one remove against independent connections. |
| `DeliverCreated` | [`webhooks.py`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py) `_handle_installation` replaces the cached set with the `installation` event's `repositories` array for `action=created`. |
| `DeliverAdded` / `DeliverRemoved` | [`webhooks.py`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py) `_handle_installation_repositories` applies repository deltas with `add_repos` / `remove_repos`. |
| `FinalSetMatchesGitHub` | Signed tests in [`test_webhooks_github.py`](../../packages/adapters/mcp/tests/test_webhooks_github.py) drive event deliveries through the MCP handler and verify the resulting PostgreSQL row. The two event-order tests assert the stale states produced by current delivery handling. |

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

The signed-webhook PostgreSQL tests reproduce those outcomes through the real
handler and store. They establish the current behavior; they do not make it an
acceptable result.

There is no freshness fix in this change. A repository delta may arrive before
the installation row exists, in which case the handler drops it. An atomic
update or conflict policy cannot recover that lost delta. Opposite deltas also
cannot be reordered using a source-backed event revision. Correctness needs an
explicit reconciliation policy, such as fetching the authoritative repository
list or durably recording events for ordered/reconciled processing. The
remaining policy question is whether reconciliation should happen in the
webhook request, through a durable retry/reconcile path, or tolerate temporary
cache drift.

The webhook now ignores a `created` event whose present `repositories` field
is not an array, avoiding interpretation of malformed data as an authoritative
empty set. The docs do not mark `repositories` as required, so an absent field
continues to be treated as an empty snapshot; the guard distinguishes a
malformed present value from an omitted optional value. This does not solve
valid but delayed snapshots.

Both models use one installation and distinct valid repository names. They do
not model simultaneous handler executions, duplicate redeliveries, event loss,
process crashes, multiple installations, or API reconciliation. TLC checks
finite abstractions, not SQLAlchemy or PostgreSQL; the database tests supply
evidence for the handler/store paths they exercise.

TLC 2.19 checked 27 states in the stale-write configuration and found its
counterexample. The atomic configuration checked 93 states without an invariant
violation. The event-order state counts and traces are recorded in
`formal/expected.tsv`.
