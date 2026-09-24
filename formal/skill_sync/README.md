# Per-binding GitHub skill resync results

`BindingResult` models one durable queue job through claim, one categorized
partial failure, and either retry or completion. It checks that a transient
fetch failure is not acknowledged as success, a permanent attach failure keeps
its error while completing, and permanent failures do not enter retry-wait.
A later successful retry clears the stored error.

| Model action or variable | Code and executable check |
| --- | --- |
| `Claim`, `RetrySucceeds`, `queueState` | [`claim_due`](../../packages/core/daimon/core/stores/github_push_resync.py#L87) claims pending or due retry work. [`drain_github_push_resync_queue`](../../packages/core/daimon/core/skill_sync/resync_queue.py#L40) calls [`resync_bound_repo`](../../packages/core/daimon/core/skill_sync/resync.py#L325), then calls the store retry or complete operation. [`test_resync_queue.py`](../../packages/core/tests/skill_sync/test_resync_queue.py#L99) checks a binding report completes permanently failed jobs and retries transient ones. |
| `TransientFetchFailure`, `PermanentAttachFailure`, `storedError` | [`SyncReport`](../../packages/core/daimon/core/skill_sync/orchestrator.py#L139) carries skipped fetches, failed uploads, attach failures, and the retryable classification. [Generic fetch errors](../../packages/core/daimon/core/skill_sync/orchestrator.py#L597) classify transport/429/5xx as transient; credential/proof failures, auth/404, size limits, and invalid bundles remain permanent. [Upload errors](../../packages/core/daimon/core/skill_sync/orchestrator.py#L416) and [attach results](../../packages/core/daimon/core/skill_sync/orchestrator.py#L769) are also classified. [`_resync_one_binding`](../../packages/core/daimon/core/skill_sync/resync.py#L374) persists all report categories to `last_sync_error`; a clean later run clears it. Tests drive a real HTTP 503, MA upload/attach failures, and a missing attach agent through the transport-level fake and Postgres. |

## Bounds and assumptions

- One durable job, one claim at a time, and at most one transient fetch failure
  followed by one success, or one permanent attach failure. Queue claim and
  completion are abstracted as atomic transitions; lease expiry/crash recovery
  is covered by the separate GitHub push resync queue model.
- Retry-wait represents the existing queue backoff; exact delay values,
  attempts, multiple bindings, cancellation, credential rotation, and database
  transaction failures are not modeled. Fairness assumes no process crash and
  that enabled claim and resync actions eventually run.
- The bounded transient fetch path occurs before that binding has uploaded
  skills, so it has no partial MA write. Other retries can follow earlier MA
  uploads when a later upload/attach fails; that partial-effect path is not
  modeled, and exactly-once MA writes are not claimed.
- Fair progress assumes the transient provider recovers on the next attempt,
  in addition to no process crash and eventual scheduling of enabled actions.
- `BindingResultFetchOmitted` retains the old behavior where a skipped fetch is
  absent from `failed_uploads`, so the durable job completes with no stored
  error. `BindingResultRetryAll` mutates permanent attach handling to retry;
  its trace enters retry-wait with a permanent error.

Run all configurations with `formal/check.sh`.
