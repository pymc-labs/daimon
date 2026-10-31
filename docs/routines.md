# Routines

How to create a routine, how the scheduler runs it, where its result goes and
what happens when a run fails. Read it if you schedule agents or run the
scheduler service.

A routine is a recurring headless turn: a cron expression, a timezone, an agent
and one prompt. The scheduler fires it, the agent runs with no human in the
thread, and the tail of its final message is saved on the routine. An agent in
GitHub App mode has no human asker in a routine, so it gets only its baseline
repository grants.

A routine may name a **destination**, a channel or a thread. With one, Daimon
posts the agent's full final reply there after the run, unless the agent posted
there itself. Without one, the result is only recorded. If it should land in a
channel, the prompt must tell the agent to call `send_message`
([mcp-tools.md](mcp-tools.md#channels)). Session output files are never
delivered for a routine.

For a one-off "remind me in two hours", use a timer (`create_timer`,
[mcp-tools.md](mcp-tools.md#timers)). It fires once, runs in the conversation it
was set in and needs no cleanup.

## Create a routine

Five MCP tools manage routines: `create_routine`, `list_routines`,
`get_routine`, `update_routine` and `delete_routine`
([mcp-tools.md](mcp-tools.md#routines)). There is no pause tool: pause with
`update_routine(enabled=False)`. The last run is in the `last_*` fields that
`get_routine` returns.

Chat is the simplest way: ask the agent ("every weekday at 9am, post the sales
numbers"). Each platform also has a panel:

- **Slack** `/routines` and **Teams** `routines` (in a 1:1 chat): workspace
  admins create and delete. The form's schedule field takes cron, such as
  `0 9 * * 1-5`, and the panel shows schedules in words.
- **Discord** `/routines` is read-mostly: pick a routine, pause or resume it,
  or view its last output. Create one by asking the agent.

The CLI has no routine CRUD.

`create_routine` refuses a caller with no platform user identity (a CLI-only
token), an unknown IANA timezone, an unparseable cron expression and an
`agent_name` that is not an agent in the tenant. The tenant always comes from
the caller's verified token.

### Set a destination

Set `destination_kind` and `destination_id` together on `create_routine` or
`update_routine`. Remove them with `update_routine(clear_destination=true)`.
Changing the destination drops a result still pending for the old one. The
tool checks, before saving, that:

- the id has the platform's shape: a Slack thread is `<channel>:<ts>` and a
  Teams thread is `<channel>;messageid=<root>`;
- the channel exists in the caller's own server or workspace and Daimon can
  post there;
- the caller may post there themselves, by the same rules as `send_message`;
- the tenant's access policy does not protect it ([permissions.md](permissions.md)).

To make a destination mandatory, set
`DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION=true`
([configuration.md](configuration.md#daimon_mcp__require_routine_destination)).
A `create_routine` call without one then creates nothing and returns a warning.

### Choose a catch-up policy

Set `catch_up_policy` on `create_routine` or `update_routine`:

- **`skip`** (default): a slot fires only if it is claimed within the freshness
  window. Older slots roll forward without firing.
- **`run-once`**: after downtime, an overdue slot fires once whatever its age.
  Missed slots coalesce into that one run and are never replayed one by one.

Slots that pass while the same routine is still running are skipped under both
policies. `get_routine` and `list_routines` show the latest skipped range and
reason (`stale` or `in_flight`). It is a latest-range record, not a history.

A routine whose channel selects the self-hosted backend does not run. It stops
with `last_error=selfhosted_routines_unsupported`, because headless routines
need Anthropic Managed Agents. A thread destination with an unknown parent
channel stops with `routine_destination_parent_unknown`. Set the destination
again to record the parent.

## How the scheduler runs it

The scheduler is a separate process, `daimon-scheduler`. At boot it takes a
PostgreSQL advisory lock, and a second scheduler with the same key exits
non-zero. Only the lock holder answers its liveness port, so a standby never
looks healthy.

Each tick, 30 seconds apart by default, the scheduler:

1. Rolls forward stale slots and slots that came due while the routine ran.
2. Claims at most 20 due routines, stamping the run and computing the next
   slot in the same transaction.
3. Checks the monthly cap, then starts each run as an independent task.
4. Runs housekeeping, such as [promo credit](billing.md#promo-codes)
   settlement, then sleeps.

The tick does not wait for the runs. A persistent dispatcher bounds concurrency
across ticks, so a slow routine cannot hold up later ticks or overlap itself.
Claimed routines wait for a free slot without losing eligibility. Unclaimed ones
stay in the database until capacity frees. A crash after a claim can lose that
run.

The usage sweep runs on its own loop
([billing.md](billing.md#recover-unbilled-usage)). On shutdown the scheduler
cancels its runs, records `scheduler_shutdown` and then releases the lock.
`--once` drains its one batch before exiting. A schedule change made during a
run survives the run's completion.

## What a run does

A run resolves the tenant, finds or creates the creator's account, checks the
invoker allowlist and the balance, and resolves the agent. It then creates a
session the way chat does and drives it with the same turn driver, so
reconnects and read timeouts behave the same. It is not a chat turn. There is
no thread binding, continuity, handoff or dead-session recovery.

Nobody can click a confirmation, so tool calls are answered without a person.
With `DAIMON_TOOL_SAFETY__ENABLED` off (the default), every call is
auto-approved. With it on, reads from third-party MCP servers run and a write is
refused unless its server or `server/tool` is listed in
`DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`
([configuration.md](configuration.md#daimon_tool_safety__unattended_writes)).
An agent's `ask` or `deny` rule on a Daimon tool is refused too.

A routine with a destination gets a short controls block before its prompt: its
id, schedule and destination, a line saying nobody is watching, and that Daimon
posts the final reply unless the agent does. If the destination is
`writers: none`, the block tells the agent not to post there. Routine turns also
get unattended-run framing, which an agent can override with
[`context_fragments.routine`](defaults.md#add-invocation-context-fragments).

Routine sessions mount memory read-only. A routine with a destination or channel
mounts the agent's shared memory only, since others read what it posts. A routine
with neither answers its owner alone and also mounts the owner's personal
memory. A routine in a channel shared with another organisation runs held to
that channel. It mounts only that channel's memory read-only, its result goes to
the destination only with no DM fallback, and it is never run unheld.

Each routine session is stamped like a turn in the channel it fires into, and
as private to its owner. A routine of a channel with limited readers is read
only from inside it, and no server admin reads a routine's transcript from the
hub.

On success the run saves the last 1,000 characters of the result as
`last_result_tail`. The full reply goes to the delivery outbox.

## Delivery

For a routine with a destination, a successful run queues its full result. If
the agent already called `send_message` to that exact destination, the result is
skipped as `agent_posted`. Otherwise the chat adapter for the tenant's platform
posts it, in chunks that keep code fences intact. The post reads `Result from
<agent>'s daily routine at 09:00 UTC:` followed by the result.

Delivery is at most once, and it checks before it sends:

- **Newest only.** A new run replaces a result still queued, so a backlog never
  posts stale results.
- **Creator first.** If the access policy cannot be read, or the creator may no
  longer invoke the agent (admins always pass), nothing is sent anywhere.
- **Policy at post time.** A `writers: none` channel, a thread under one or a
  Discord channel in such a category is refused as `protected_channel`.
- **Only where the creator could post.** A creator who lost view or send access,
  or is not in a private channel, gets the result by direct message instead.
- **Live workspace only.** A channel in another server, or one that no longer
  exists or is archived, is `destination_unavailable`.
- **Never silently nowhere.** When nobody may write in the destination, the
  result goes to the creator by DM, under the tenant's DM policy
  ([configuration.md](configuration.md#daimon_direct_message_policies)). If
  that is not allowed or fails, the result is dropped with the reason.
- **No broadcasts.** Mentions are disabled on Discord, and `<!channel>` stays
  literal on Slack. On Teams, a post also sends the creator one activity feed
  notice.

A claim whose lease expires is not posted again, because the poster may have
posted before it died. A poster that raises is not retried. An empty result is
skipped. With [agent identity](agent-identity.md) on, Slack and Discord post as
the agent. On Discord a reply to any delivered message starts a turn.

## Timeouts

| Bound | Default |
| --- | --- |
| Per-turn ceiling | 45 minutes |
| `DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S` | ceiling plus 300 s |
| `DAIMON_SCHEDULER__TICK_INTERVAL_S` | 30 s |
| `DAIMON_SCHEDULER__MAX_AGE_S` | 900 s |
| `DAIMON_SCHEDULER__MAX_CONCURRENT_FIRES` | 10 |
| Read timeout per call | 120 s |

The 45-minute ceiling normally fires. It covers session assembly and the run
under one deadline. The dispatch timeout is an outer guard for a run that hangs
outside those, and its default cannot be set below the ceiling. Each run has
its own timeout, and the scheduler keeps ticking while one nears it. Settings
are in [configuration.md](configuration.md#scheduler).

## Who may do what

| Action | MCP | Discord | Slack and Teams |
| --- | --- | --- | --- |
| create | a caller with a platform user identity, for the agent they are talking to or the one the destination answers with; any agent for an admin | by asking the agent | workspace admin only |
| list and read last output | admin or the creator | `Manage Server`, the command's invoker only | admin or the creator |
| pause and resume | admin or the creator | admin or the creator | admin or the creator |
| delete | admin or the creator | not offered | admin or the creator |

Ownerless routines and callers with no platform user id are admin-only. A
refusal reads as "not found", so a forbidden routine looks the same as a missing
one. Non-admins see only their own routines, because a run's output often holds
business data. A routine runs with its agent's repo, keys, connectors and memory,
which is why a member may schedule only the agent they are talking to.

In a `readers: own` channel, a routine of that channel's own agent is visible
and editable only from inside it. Each run re-checks the agent's rule at the
destination and skips a run that would cross the line (`own_agents_only`).

## When a run fails

The scheduler does not retry a failed run, count failures or disable a routine.
A failing routine waits for its next slot and tries again, enabled. A failed run
writes `last_error` and clears `last_result_tail`, and a success does the
reverse. The panels show a failure whenever `last_error` is set.

A routine with a destination announces the first failure after a success, or a
first run that fails. The notice goes through the same checks as a result, with
the DM fallback. It says what failed in plain words, never the raw error, and
that the routine will try again. It never replaces an undelivered result. A
routine that keeps failing posts once and stays quiet until a run succeeds.

The `last_error` values are a closed set: `invoker_not_allowed` (the creator was
removed from the allowlist), `access_policy_unreadable`, `balance_depleted`,
`cap_exceeded`, `channel_budget_exceeded`, `scheduler_shutdown`,
`timeout: exceeded <n>s`, and otherwise `<ExceptionType>: <message>` cut to 500
characters. A ceiling breach reads `TurnError: ceiling: ...`. Taking someone
off the allowlist stops their routines at the next run, and a routine can be
refused for a rule it later violates.

## Turn routines on

There is no feature flag. Routines run only while the scheduler process runs,
so a deployment without the `scheduler` service collects routines that never
fire. The process will not boot without `DAIMON_MCP__JWT_SECRET` and
`DAIMON_MCP__PUBLIC_URL`
([JWT secret](configuration.md#daimon_mcp__jwt_secret),
[public URL](configuration.md#daimon_mcp__public_url)).

Four gates apply to every run: the invoker allowlist against the creator, the
credit balance, the per-person monthly cap and, for a routine with a channel,
that channel's budget, checked last after the agent's rule
([billing.md](billing.md#channel-budgets)). A refused run records the reason and
fires again at its next slot. Only the creator's stored admin role exempts
them from the allowlist, since a run has no live platform role.

A Discord thread destination saved before channel budgets existed has no
channel until its destination is set again.
