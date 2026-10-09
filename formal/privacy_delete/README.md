# Privacy session deletion retry

The local account transaction must retain enough information to delete MA sessions after a process dies. `PrivacyDeleteUnsafe` models the former behavior: local deletion commits without a durable target. The two-state counterexample is simply a commit that removes the account while the upstream session still exists.

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `PrivacyDeleteSafe` | clean | 35 | A still-live session always has a queued deletion target after local commit. |
| `PrivacyDeleteUnsafe` | violates `LocalDeletionTracked` | 2 | The old commit loses its target immediately. |
| `PrivacyDeleteProgress` | clean | 35 | With fair restarts and eventual successful API calls, the session is deleted. |

Run through `formal/check.sh`; CI pins these counts in `formal/expected.tsv`. The model has one account, one tenant, one upstream session, at most one crash and one transient failure. Its `queued` state stands for the `privacy_session_deletes` row. `pending` stands for a discovered session ID in its JSON map. This is a scaled-down check of the transaction and retry order, not a proof of complete MA enumeration.

| Model action | Code boundary |
| --- | --- |
| `CommitLocal` | `purge.py:498-511` locks the account and inserts the work item with `ensure_work` inside the local purge transaction. |
| `Crash`, `Restart` | Process death and a later scheduler tick; durable rows remain. |
| `EnumerationFailure`, `Enumerate` | `ma.py:438-447` lists tenant agents and sessions; `purge.py:648-665` retains work on an API error. |
| `Remember` | `ma.py:449-450` calls the `purge.py:636-642` callback, which commits discovered IDs before MA deletion. |
| `DeleteFailure`, `DeleteSuccess` | `ma.py:455-467` counts non-404 status errors; success and 404 both count as deleted. |
| `Acknowledge` | `purge.py:644-648` removes a confirmed deleted ID in its own transaction. |
| `Finish` | `purge.py:669-674` clears the work row only after all tenant scans succeed with no failed or pending IDs. |

The old commit counterexample is replayed by `test_privacy_delete_can_retry_upstream_after_transient_failure`. The new crash and partial-failure cases are replayed by `test_privacy_delete_crash_before_upstream_is_recovered_by_sweep` and `test_privacy_delete_failed_session_stays_queued`. Those tests use an HTTP transport fake; they do not establish that MA always enumerates every relevant session. The model assumes a successful API enumeration is complete, 404 means gone, and a live scheduler eventually runs another tick. Those upstream and scheduling assumptions have not been checked against staging traces.
