# GitHub push skill resync

The GitHub webhook verifies the request signature, extracts the canonical
`owner/repo` and ref, then commits a queue row and delivery receipt before it
returns HTTP 200. The database stores no tenant identity, webhook body, token,
or credential. A database failure returns a non-2xx response. GitHub does not
automatically redeliver failed webhook deliveries, so an operator must
redeliver the event or reconcile the repository/ref if persistence fails before
acknowledgement.

The queue has one coalesced row per `(repo, ref)`. Each distinct delivery
advances its generation and records its delivery ID; a duplicate delivery ID
does not add work. Pushes received while a row is running advance the
generation and leave the active lease in place. On completion, the worker may
mark the row done only when both its lease owner and claimed generation still
match. A newer generation stays pending for another pass.

The scheduler processes up to three due rows on each tick. It claims work in
database order with `FOR UPDATE SKIP LOCKED`, renews the two-minute lease every
30 seconds, and retries failed setup or transient binding failures with
exponential backoff capped at five minutes. Transient GitHub/MA transport
errors, 429s, and 5xx responses retry; permanent credential, permission,
repository, size-limit, validation, and attach errors complete the queue job
while remaining visible in the binding's `last_sync_error` for operator action.
Retries have no terminal attempt limit. A transient failure summary stays on
the row until a new push or a successful pass, and a later clean binding sync
clears its stored error. Delivery receipts
are removed after 30 days, so GitHub redelivery is deduplicated for that
window; a later replay creates another safe current-branch pass.

For each repo/ref, a PostgreSQL session advisory lock is held across the full
binding batch. This serializes ordinary workers for the same branch. The lock
uses one scheduler database connection for the duration of the batch; the
active sync also needs its normal database sessions and one lease-renewal
session every 30 seconds. The single active scheduler processes queue batches
sequentially, so a slow batch delays other queued repositories until it
returns. Backoff on a failing job leaves other due rows eligible on the next
queue pass.

For a successfully acknowledged and persisted push, the delivery contract is
**at least once**. A process death after an external
Managed Agents call takes effect but before queue completion can repeat that
call after the lease expires. A database/advisory-lock connection loss during
an in-flight external call can release the lock while the call is still
running; a newer worker may overlap it. When a stale worker returns, it cannot
complete another owner's claim and requests an extra current-generation pass.
If the process dies before reporting that stale result, strict external effect
ordering is not guaranteed. Repeated passes fetch the branch's current
contents and are expected to converge the local binding state, but the queue
does not prove exactly-once Managed Agents effects.

Progress depends on the scheduler continuing to poll, Postgres becoming
available, and retries receiving execution time. A continuous backlog can
delay later rows because the scheduler is single-active and processes at most
three jobs per tick.
