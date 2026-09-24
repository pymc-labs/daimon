# GitHub App repository-set updates

Run both bounded configurations from the repository root with the pinned TLA+
tools jar:

```sh
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationUpdatesStaleWrite.cfg \
  formal/github_installation/InstallationUpdates.tla
java -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -config formal/github_installation/InstallationUpdatesSafe.cfg \
  formal/github_installation/InstallationUpdates.tla
```

## Implementation mapping

| Model | Implementation |
| --- | --- |
| `ReadA` / `ReadB` / `ReadRemoval` then stale writes | The prior `add_repos` and `remove_repos` read the array with `_load`, computed a replacement in Python, and passed it to `upsert`. |
| Atomic `WriteA` / `WriteB` / `WriteRemoval` | `stores/github_app_installations.py::{add_repos,remove_repos}` now updates the array in one PostgreSQL `UPDATE`. Each expression reads the row version PostgreSQL locks for that update. |
| `EachCompletedDeltaIsPresent` | `test_concurrent_repo_updates_preserve_each_delta` holds three real PostgreSQL readers at a barrier, then releases two adds and one remove against independent connections. |

## Bounds and exclusions

The model has one installation and three transactions: two add different
repository names, and one removes a repository that was present initially.
Every transaction reads before it writes. The stale-write configuration lets
each transaction replace the row from its captured array, which loses deltas.
The safe configuration applies each set change to the current array in one
atomic row update.

The model assumes the three event payloads are valid and distinct. It checks
that completed deltas survive concurrent execution. It does not establish the
actual order of GitHub events, resolve opposite add/remove events delivered out
of order, deduplicate manually redelivered delivery IDs, or model process
crashes. Those require a separate event freshness or reconciliation policy.
TLC checks this finite abstraction, not SQLAlchemy or PostgreSQL; the regression
test supplies the database-level evidence.

TLC 2.19 checked 27 distinct states in the stale-write configuration and found
a counterexample after two transactions read the initial array. The atomic
configuration checked 93 distinct states with no invariant violation.
