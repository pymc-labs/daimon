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

## Bound MA identity and duplicate-name refusal

`BindingIdentity` abstracts one repo binding that has resolved MA agent A,
one tenant/name (`agent`), and at most one same-name replacement B. It
reproduces the pre-fix name substitution, checks the current exact-ID and
ambiguity guards, and retains a separate counterexample for a duplicate created
after the last pre-write roster check. That final trace leaves a shared
name-scoped skill title write in place, although the exact attach guard refuses
to update B.

| Model action or variable | Code and executable check |
| --- | --- |
| `BridgeResolve`, `bridgeAgent` | [`_resolve_agent_name_and_principal`](../../packages/core/daimon/core/skill_sync/resync.py) matches the binding UUID to a tenant-listed MA ID and returns its name and exact MA ID. A duplicate visible at this stage is refused before credential selection. |
| `AddSameNameAgent`, `duplicatePresent` | Abstracts a new MA agent with the same tenant and Daimon name becoming visible after the bridge's list. `test_resync_refuses_duplicate_added_after_bridge_resolution` injects it after the first target preflight. |
| `TargetPreflight`, `targetAgent` | [`_get_sync_target_agent`](../../packages/core/daimon/core/skill_sync/orchestrator.py) retrieves the supplied ID, checks tenant and active status, then refuses a duplicate logical name before fetch. Bound callers never fall back to canonical name lookup. |
| `FetchDone`, `PostFetchCheck`, `WriteTitle`, `registryWritten` | `sync_agent_skills` fetches and bundles before rechecking the bound roster and entering `_upload_all` or orphan cleanup. The regression confirms a later-observed duplicate blocks upload, orphan deletion, and ledger writes, including an empty tarball. A duplicate visible before the first bridge list is refused before GitHub access. |
| `Attach`, `attachedAgent` | The final attach reads the same exact ID and the version-retry closure updates that ID. [`test_resync_uses_exact_binding_agent_identity`](../../packages/core/tests/skill_sync/test_resync.py) checks the selected PAT, exact MA update target, and A-keyed ledger; archived, foreign, and missing targets are covered in [`test_orchestrator.py`](../../packages/core/tests/skill_sync/test_orchestrator.py). |
| `NoWrongAgentAttach`, `SafePreflightUsesExactAgent`, `EarlyAmbiguityWritesNothing`, `LateAmbiguousTitleWriteRefusesAttach` | Safe bounded invariants, checked by `BindingIdentitySafe`. |

TLC bounds this model to A and one B, one tenant/name, and one bound resync.
The safe run explores 17 distinct states. The `BindingIdentityNameSubstitution`
mutation explores 18 states and violates `NoWrongAgentAttach`: after A is
resolved, B appears, and the name-only attach chooses B. The
`BindingIdentityTitleRace` run explores 13 states and violates
`NoAmbiguousTitleWrite`: B can be created after the last roster check but
before A's MA skill upload uses the tenant/name-scoped display title. The model
records that ambiguous write; it does not model B's own title write or prove an
actual overwrite. MA has no atomic title reservation tied to an agent ID, so
this narrow external collision risk remains. The exact-ID guard still prevents
the later agent update from substituting B. Credential lookup, GitHub token
contents, repository bytes, multiple bindings, API failures, and the database
implementation are outside this model.

The executable ambiguity tests assert the duplicate refusal is stored as a
failed, non-retryable binding outcome (`retryable_bindings == 0`); an operator
must archive the duplicate before retrying the resync.
