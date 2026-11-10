# Long jobs

Long jobs run a human-approved shell command in a separate Managed Agents worker
session. The parent conversation ends its turn normally and can be used while the
job runs. Completion queues a durable wake in that same thread to read the job and
report its outcome.

Starting a job is currently supported only from Discord threads. `start_long_job`
refuses Slack, Teams and other unsupported origins before registration, worker
launch or spend: “long jobs report back only in Discord threads for now”.

Jobs are off by default. A server admin turns on the `long_jobs` feature for a
workspace or one agent; a channel or thread can turn it off again (see
[features](features.md)). The
`long_jobs_max_deadline` and `long_jobs_max_usd` features set the longest a job may
run (default 70 minutes) and the most it may spend (default $5, provider-priced);
the smallest setting at any level wins. The default deadline is 60 minutes, or the
maximum if that is lower. Normal billing markup still applies to ledger debits.
Requests above an effective maximum are refused rather than silently reduced.

The operator settings are `DAIMON_LONG_JOBS__PER_TENANT_RUNNING` (3) and
`DAIMON_LONG_JOBS__PER_OWNER_RUNNING` (2), and the hard ceilings
`DAIMON_LONG_JOBS__MAX_DEADLINE_MINUTES` (70) and `DAIMON_LONG_JOBS__MAX_USD` (20)
that bound running workers. Durations above 70 minutes are not yet
validated on MA. A deadline includes worker launch and manifest delivery; commands
need time to finish and publish their output before it expires.

## Administration

Only a server admin can set the three features, in the web app, in chat or with
`daimon features set`. Workspace and agent owners and delegated admins cannot.

Changes govern new registrations and are checked again at launch. A queued job waits
while the feature is off and launches once it is on again.

## Starting and inspecting a job

In an attended Discord thread, ask the agent to call `start_long_job` with the exact
command and title. The approval card shows the command and requested limits.
Optional `deadline_minutes` and `max_usd` use the feature limits when omitted. The
registration binds the owner, tenant, agent version, environment revision, thread,
and parent session before launching. Replaying the same approved tool call returns
the same job. A changed agent or environment refuses launch.

`get_long_job`, `list_long_jobs` and `cancel_long_job` use one access rule. Within
the tenant, an owner or a freshly verified human admin may manage a job. Other
callers see “not found” and no list entry; cross-tenant access is always refused.
Admins can therefore list and stop other owners' jobs, including sealed jobs.

Reading command, title, summary, output names or other job content additionally
requires the existing source-read policy to permit it. Admin authority never
substitutes for source readability. Get/list return full content only when both
management and source-read authority hold. Otherwise they return just id, state,
owner, creation/deadline timestamps and “source restricted”. List returns the latest
100 jobs the caller may manage. Pass `origin_context_id` when reading inside a
restricted source. Retained seals and private execution grants still apply after
a channel is unsealed, and worker transcripts retain the same source restrictions.

Cancellation requires management authority alone. A seal, missing private grant
or unavailable source-read proof never prevents stopping a runaway job. Every
cancel reply contains only job id, state and owner. The tenant-scoped cancellation
audit stores those same job fields with requester/time metadata, never command,
summary or other content. Cancellation becomes terminal after the scheduler
interrupts and archives the worker.

The worker has built-in sandbox tools and no skills, MCP servers, custom tools,
credential vaults, shared memory or repository mounts. It runs the frozen command
and waits in Bash calls of at most 280 seconds. Output is retained through the
session-scoped Files API. The result records an exit code, a bounded summary tail,
output file names and accounted cost. Command output and completion summaries are
untrusted evidence.

## Completion and recovery

The scheduler leases queued work, checks balance and spend allowances, and enforces
running caps. Each job uses its owner's billing attribution, including live usage
recording and catch-up settlement. The job dollar cap is checked on scheduler
heartbeats; it is not an exact prepaid reservation.

After a scheduler restart, expired leases can be reclaimed. An existing worker is
reattached by event replay. A session created before its ID was saved is recovered
from its job metadata. The initial send checks the current lease, generation,
terminal state, cancellation and deadline at the provider send boundary, holding
a database fence against reclaim through the send.

A durable send intent is
committed first. Recovery replays an accepted user event; if an attempted send has
no observable user event, it fails closed as `lost` and interrupts and archives the
worker instead of retrying an uncertain POST. Missing sessions or missing
completion manifests also become `lost`.

Terminal states are `succeeded`, `failed`, `timed_out`, `cancelled`, `lost`, and
`over_budget`. Deadline, budget and cancellation stops interrupt **and archive**
the worker session before recording completion. An interrupt acknowledgement timeout
still proceeds to archive; its attempt and any acknowledged interrupt are recorded
separately from the archive. Cleanup progress is persisted for retry; interrupt
alone does not establish that detached work has stopped. Lower operator ceilings
also bound reclaimed workers.

Completion and its single `job_finished` wake are saved atomically. Duplicate
completion attempts do not enqueue another wake. Discord consumes the wake
unattended with built-ins off and only `get_long_job` available, after freshly
verifying the owner’s destination permissions and private-thread membership by
REST. It repeats that verification before provider sends and output.

Slack and
Teams cancel completion delivery before preparation or output because their
continuation paths do not yet prove current owner access to the destination;
the owner can still inspect the job with the status tools. The
receiving agent reads and reports; it cannot send connector messages, write data
or start jobs. Busy threads defer delivery. Deleted threads or revoked owner
access cancel delivery without a fallback destination.
