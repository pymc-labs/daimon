# Long jobs

Long jobs run a human-approved shell command in a separate Managed Agents worker
session. The parent conversation ends its turn normally and can be used while the
job runs. Completion queues a durable wake in that same thread to read the job and
report its outcome.

Starting a job is currently supported only from Discord threads. `start_long_job`
refuses Slack, Teams and other unsupported origins before registration, worker
launch or spend: “long jobs report back only in Discord threads for now”.

Jobs are disabled by default. A current human admin configures tenant policy, with
an optional complete override for each agent. Without a policy row the deadline
default is 60 minutes, the maximum deadline is 70 minutes, and the maximum provider-priced spend is
$5 per job. Normal billing markup still applies to ledger debits. Agent policy replaces tenant policy; absent both, the defaults apply.
The operator's ceilings always win: effective maximums are the smaller of policy
and operator settings, and the default deadline is also clamped to that maximum.
Requests above an effective maximum are refused rather than silently reduced.

The operator settings are `DAIMON_LONG_JOBS__MAX_DEADLINE_MINUTES` (70),
`DAIMON_LONG_JOBS__MAX_USD` (20), `DAIMON_LONG_JOBS__PER_TENANT_RUNNING` (3), and
`DAIMON_LONG_JOBS__PER_OWNER_RUNNING` (2). Durations above 70 minutes are not yet
validated on MA. A deadline includes worker launch and manifest delivery; commands
need time to finish and publish their output before it expires.

## Administration

Use the existing tenant CLI surface. Read effective policy by omitting
`--enabled` and `--disabled`:

```bash
daimon tenants long-job-policy TENANT_UUID --actor-user PLATFORM_USER_ID
```

Set a complete policy, optionally for one MA agent:

```bash
daimon tenants long-job-policy TENANT_UUID --actor-user PLATFORM_USER_ID \
  --enabled --default-deadline 60 --max-deadline 70 --max-usd 5

daimon tenants long-job-policy TENANT_UUID --actor-user PLATFORM_USER_ID \
  --agent MA_AGENT_ID --disabled
```

This uses the same fresh human admin attestation as `tenants backend-policy`.
That attestation currently supports Discord administrators. Unsupported platforms
fail closed. Each change records the human account, time, and before/after policy;
reapplying the same policy is a no-op. Admins cannot set limits above operator
ceilings. Policy changes govern new registrations and are checked again at launch.

## Starting and inspecting a job

In an attended Discord thread, ask the agent to call `start_long_job` with the exact
command and title. The approval card shows the command and requested limits.
Optional `deadline_minutes` and `max_usd` use effective policy when omitted. The
registration binds the owner, tenant, agent version, environment revision, thread,
and parent session before launching. Replaying the same approved tool call returns
the same job. A changed agent or environment refuses launch.

`get_long_job` reads an owner's job by ID. `list_long_jobs` returns the latest 100
jobs for the owner; current tenant admins may list tenant jobs only where the
existing session-read privacy policy allows it.
`cancel_long_job` is available to the owner or a current tenant admin. Tenant
boundaries apply even to admins, and IDs do not grant access. Reads and cancellation
results also obey source-channel reader restrictions and private execution grants.
Pass `origin_context_id` when reading from a turn inside a restricted source.
Registration retains the parent’s seals and private-context provenance, and workers
carry those same restrictions. Unsealing a channel does not release retained seals.

The worker has built-in sandbox tools and no skills, MCP servers, custom tools,
credential vaults, shared memory or repository mounts. It runs the frozen command
and waits in Bash calls of at most 280 seconds. Output is retained through the
session-scoped Files API. The result records an exit code, a bounded summary tail,
output file names and accounted cost. Command output and completion summaries are
untrusted evidence.

## Completion and recovery

The scheduler leases queued work, checks balance and monthly caps, and enforces
running caps. Each job uses its owner's billing attribution, including live usage
recording and catch-up settlement. The job dollar cap is checked on scheduler
heartbeats; it is not an exact prepaid reservation.

After a scheduler restart, expired leases can be reclaimed. An existing worker is
reattached by event replay. A session created before its ID was saved is recovered
from its job metadata. The initial send checks the current lease, generation,
terminal state, cancellation and deadline at the provider send boundary, holding
a database fence against reclaim through the send. A durable send intent is
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
REST. It repeats that verification before provider sends and output. Slack and
Teams cancel completion delivery before preparation or output because their
continuation paths do not yet prove current owner access to the destination;
the owner can still inspect the job with the status tools. The
receiving agent reads and reports; it cannot send connector messages, write data
or start jobs. Busy threads defer delivery. Deleted threads or revoked owner
access cancel delivery without a fallback destination.
