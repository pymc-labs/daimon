# Routines

A routine is a recurring headless turn: a cron expression, a timezone, an
agent and one prompt. The scheduler fires it, the agent runs with no human in
the thread, and the tail of its final message is written back onto the
routine row.
For an agent in GitHub App mode, a routine has no human asker and receives only
the agent's baseline repository grants.

A routine may name a **destination**: a channel or a thread. With one, the run
is told where its result goes, and if the agent does not post there itself,
Daimon posts its full final reply there after the run (see
[Delivery](#delivery)). A routine asked for in a conversation and naming no
destination gets that conversation as its destination: the thread it was asked
in (see [Creating one](#creating-one)). Without one — a routine made in a
private DM, by the Slack or Teams panel, or by a caller with no conversation —
nothing is delivered anywhere: the result is only recorded on the row, and if
it should land in a channel the prompt has to tell the agent to call
`send_message` itself (see [mcp-tools.md](mcp-tools.md#channels)). Session output files, which an
interactive Slack turn uploads to the thread, are **not** swept for a routine
either way; [slack.md](slack.md) says so, and the code agrees: the headless
path installs a no-op lifecycle with nothing to render to.

A one-off "remind me in two hours" or "check back tomorrow at nine" is not a
routine. Use a timer (`create_timer`, see [mcp-tools.md](mcp-tools.md#timers))
instead: it fires once and runs in the conversation it was set in, so the
conversation's context carries over. Nothing has to be deleted afterwards.

## The row

`routines`, declared in `packages/core/daimon/core/_models.py` and read
through `packages/core/daimon/core/stores/routines.py`:

| Column | Meaning |
| --- | --- |
| `tenant_id` | the partition; cascades on tenant delete |
| `created_by_user_id` | the **platform** user id of the creator, not an account id |
| `agent_name` / `agent_id` | the Daimon tag is authoritative; the MA id is a cache, self-healed at fire time |
| `cron_expr` / `timezone` | a five-field croniter expression and an IANA zone |
| `trigger_message` | the prompt sent as the turn's user message |
| `enabled` | the pause flag |
| `catch_up_policy` | `skip` (default) or `run-once` after downtime |
| `last_skipped_from` / `last_skipped_until` / `last_skip_reason` | most recent skipped range of scheduled slots and why (`stale` or `in_flight`) |
| `next_fire_at` | the claim key — `NULL` means claimed or paused |
| `last_fired_at`, `last_error`, `last_result_tail` | what the last run did |
| `destination_kind` / `destination_id` | optional: `channel` or `thread` and its id, set together or not at all. On Slack a thread is `<channel id>:<thread ts>`, on Teams `<channel id>;messageid=<root id>` |
| `channel_id` | the channel whose budget a run's spend counts against: the destination's parent channel, set when the destination is; without a destination, the channel the routine was made in (MCP `origin_context_id`, or the Slack panel's channel); `NULL` from a DM or with no origin. Clearing the destination keeps it |
| `delivery_status`, `delivery_note`, `delivered_at` | the outbox for the last result: `pending` → `claimed` → `delivered` or `skipped` (with why); `NULL` for a routine without a destination |
| `delivery_payload` | the text a pending post carries: that fire's result, copied so a writer that only knows `last_result_tail` (an older scheduler) cannot change what gets posted |
| `delivery_lease_owner` / `delivery_lease_expires_at` | the poster holding a `claimed` row |

Slot arithmetic lives in `packages/core/daimon/core/cron.py`, whose
`next_slot_at_or_after` converts into the routine's zone, steps one second
past the reference instant so it cannot land on it, and returns UTC.

Routines whose saved channel or delivery destination selects the self-hosted
backend stop before provider resolution, with
`last_error=selfhosted_routines_unsupported`. Headless routines currently require
Anthropic Managed Agents; switching a channel to self-hosted does not silently
run its scheduled prompts through Anthropic. Thread destinations also check the
requesting caller's retained backend. Legacy thread destinations whose parent
channel is unknown stop with `last_error=routine_destination_parent_unknown`;
set their destination again to record the parent before running them.

Timers and task continuations retain their thread's backend after a channel
backend change, including Anthropic timers in channels switched to self-hosted.
Each queued wake resolves its own provider guard, even when another caller's
turn drains the queue. A provider guard denial releases the claim for retry and
never discards the timer or counts as a failed attempt.

Timers and task continuations in a self-hosted conversation resolve their
target from tenant-scoped local records. They run through the conversation
runtime when installed; otherwise they stop with a notice asking the person
to send a message to continue. They never fall back to Anthropic resolution.

## Creating one

A `create_routine` call with no destination and a live `origin_context_id`
(the turn it was asked in) defaults its destination to that conversation:
on Discord the thread (or the channel, for a turn outside a thread), on Slack
`<channel>:<thread ts>`, on Teams the channel thread
`<channel>;messageid=<root>`. The defaulted destination goes through every
check a named one does — the caller must be able to post there, the access
policy, the agent's pin and the agent scope rule below — and if it fails the
call is refused, asking for a channel or thread to post to, rather than saved
record-only. A private DM (a `dm:` scope, a Slack IM, a Teams personal or
group chat) has no destination a routine can post to, so one made there is
still only recorded. To keep a routine made in a channel record-only, clear
its destination afterwards with `update_routine(clear_destination=true)`.

Operators can opt into requiring a destination for the MCP `create_routine`
tool with `DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION=true`. A call that ends up
without one (it named none and had no conversation to default to) then
receives a warning that nothing was created, before agent lookup or saving a
routine. The default is `false`, preserving record-only creation for those
calls. Existing routines and `update_routine`, including clearing a
destination, are unaffected.

Five MCP tools — `create_routine`, `list_routines`, `get_routine`,
`update_routine`, `delete_routine` — are registered by
`packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py` and catalogued
in [mcp-tools.md](mcp-tools.md#routines). There is no separate pause tool:
pausing is `update_routine(enabled=False)`. Set missed-run behavior with
`create_routine(..., catch_up_policy="run-once")` or
`update_routine(routine_id=..., catch_up_policy="skip")`; the same creator-or-admin
permission applies to updates. The last run is read off the
`last_*` fields that `get_routine` returns.

A destination is set with `destination_kind` + `destination_id` on
`create_routine` or `update_routine`, and removed with
`update_routine(clear_destination=true)`. Before anything is saved the tool
checks that the pair comes together, that the id has the platform's shape (a
Discord id is a number; a Slack channel is `C…`, a Slack thread
`<channel>:<ts>`; a Teams channel `19:…@thread.tacv2`, a Teams thread
`<channel>;messageid=<root>`), that the channel exists in the caller's own server or
workspace with Daimon able to post there (on Slack, Daimon must be a member;
a thread must exist), that the kind matches (a Discord thread is not a
channel), that **the caller themselves may post there** — the same checks
`send_message` applies to a caller (Discord: view and send, send in threads,
membership of a private thread unless they manage threads; Slack: membership
of a private channel, or of any channel for a guest; Teams: on the channel's
roster, in a team Daimon is in) — and that the tenant's
access policy does not protect it — checked
with the parent channel and category resolved from the platform. Changing the
destination drops a result still pending for the old one. The adapter checks
all of it again at post time.

`create_routine` refuses four ways before it writes anything: a caller with no
platform user identity (a CLI-only token) cannot create a schedulable routine
at all, an unknown IANA zone and an unparseable cron expression each raise
their own error, and an `agent_name` that resolves to no agent in the tenant
is rejected. The tenant comes from the caller's verified token, never from an
argument.

The platform surfaces differ, and the difference is deliberate:

- **Slack** `/routines` can create and delete. The panel writes the row
  directly rather than going through the MCP tool, because the Slack
  interaction carries the real user id while an agent's token does not
  (`packages/adapters/slack/daimon/adapters/slack/routines_panel/`). The
  New Routine form opens with the plain way, asking the agent in a channel
  ("every weekday at 9am, post the sales numbers"), and labels the schedule
  field "Schedule (advanced: cron, like 0 9 * * 1-5)". The panel rows and the
  confirmation show the schedule in words (`daimon.core.cron_words`).
- **Teams** `routines` (1:1 chat, or privately in a channel thread) matches Slack: it creates through a
  dialog, and the panel writes the row the same way
  (`packages/adapters/teams/daimon/adapters/teams/routines_panel.py`). Its form
  and schedule words read the same as Slack's. The shared rules (glyph, label,
  ordering, admin-or-creator, and `may_schedule_agent`, who may schedule which
  agent) live in `daimon.core.routines`. Both panels apply the same rule as
  `create_routine` in chat: a member may create a routine for the agent the
  channel or chat the panel was opened in answers with (on Teams a channel
  thread's channel, or the chat's own setting, which is usually the workspace
  default agent), and its agent picker lists only that agent; an admin may
  pick any agent. The submission is checked again before the row is written,
  together with what an admitted chat turn would have checked
  (`panel_save_refusal`): the creator must be someone the access policy lets
  start turns (`ACT_FOR_CREATOR`), and `authorize(SAVE_ROUTINE)` must allow
  the agent with no destination, saved from that channel (a pinned agent, or
  another channel's own agent, is refused). A refusal uses the turn refusal
  copy, so the panel never confirms a routine every fire would refuse. A panel
  routine has no destination, so its confirmation says its results aren't
  posted anywhere and points at the panel's last-output action. A member whose
  channel is set to an agent that no longer exists is told that agent's name.
- **Discord** `/routines` is read-mostly: pick a routine, pause or resume it,
  view its last output. Creating one on Discord means asking the agent in
  chat, which calls `create_routine`
  (`packages/adapters/discord/daimon/adapters/discord/routines_panel/`).
- The CLI has no routine CRUD. `daimon routines backfill-agent-names` is an
  admin migration helper and nothing else.

## How the scheduler picks it up

`packages/adapters/scheduler/daimon/adapters/scheduler/main.py` is a separate
process (`daimon-scheduler`). At boot it takes `pg_try_advisory_lock` on a
dedicated connection held open for the process lifetime; a second scheduler
with the same key logs that it did not get the lock and exits non-zero. Only
after winning the lock does it start answering its liveness port, so a
standby never looks healthy.

Each tick, 30 seconds apart by default:

1. `advance_stale` rolls forward orphaned `NULL` claim keys, stale slots with
   policy `skip`, and slots that became due while their routine was still running.
2. `claim_due_fireable` selects at most 20 due rows with `FOR UPDATE SKIP LOCKED`.
   Across ticks the dispatcher owns at most `max(20, max_concurrent_fires)`
   claimed rows, including tasks waiting for the semaphore. It excludes in-flight
   routine ids, nulls each claimed `next_fire_at`, stamps `last_fired_at`, then
   computes the next future slot in the same transaction.
3. Claimed rows pass the monthly cap check and start independent tasks owned by
   a persistent `RoutineDispatcher`. The tick returns without awaiting those
   turns. Its semaphore and in-flight registry span ticks, so a slow routine
   cannot hold up a later tick or overlap another run of itself.
4. Housekeeping runs, including [promo credit](billing.md#promo-codes)
   settlement, then the loop sleeps interruptibly. Claimed work keeps its
   eligibility while waiting for a dispatch slot, preserving the previous batch
   behavior even when a slow sibling runs past the freshness window. Additional
   unclaimed work remains in PostgreSQL when the bounded batch is full.

The [usage sweep](billing.md#the-tables), unless switched off
(`DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED`, on by default; `--once` honours the
same switch), runs on its own loop with the same
pause between passes, so a long pass no longer blocks claims. It settles only
the headless sessions with a pending settle request. Shutdown cancels a pass in
flight; a session it did not finish keeps its request for the next pass.

The per-routine catch-up policy controls downtime recovery:

- **`skip` (default):** preserve the existing freshness window. A slot fires
  only if claimed inside `[now - max_age_s, now]`; older unclaimed slots roll
  forward without firing. Waiting for the semaphore does not expire a claim.
- **`run-once`:** claim an overdue slot regardless of age, fire once, then move
  `next_fire_at` beyond now. Missed slots coalesce into that single run; they
  are never replayed one by one.

For both policies, slots passing while the same routine is in flight are
skipped. `last_skipped_from` and `last_skipped_until` delimit the latest skipped
range, and `last_skip_reason` distinguishes stale work from an already-running
routine. These fields are returned by `get_routine` and `list_routines` and
also logged as `scheduler.slots_skipped`. They do not replace a running turn's
result. This is a latest-range record, not a permanent history of every slot.

Schedule changes made during a run survive completion: the fire records only
its result, while claims and skip advancement lock the current schedule row.
The process-wide advisory lock remains necessary; the in-flight registry is
local to the winning scheduler process. A crash after a claim can lose that
claimed run; catch-up applies to slots still pending in `next_fire_at`.

On shutdown the scheduler cancels and joins its fire tasks before releasing
the advisory lock, client or database resources. Started cancelled fires record
`scheduler_shutdown`. `--once` deliberately drains its one batch before exiting;
standalone callers can request that behavior with `wait_for_completion=True`.
By default `run_one_tick` returns its dispatcher immediately; callers retain
it for later ticks and close it at shutdown.

## The turn itself

A fire resolves the tenant, mints or finds the creator's principal on the
tenant's own platform, checks the tenant's invoker allowlist against the
creator (see [architecture.md](architecture.md#how-a-message-becomes-a-turn)),
checks the balance, binds a usage recorder, resolves
the agent and environment tags to live MA ids (writing back a healed id if it
drifted), and calls `run_turn` in
`packages/core/daimon/core/headless_runner.py`.

That runner creates a session through the same
`packages/core/daimon/core/sessions.py` assembly the chat path uses — same
credential vault, same env mount, same repo resource, same tenant metadata
stamps — and then hands the drain to the same driver,
`packages/core/daimon/core/turn/driver.py`. So a routine inherits the driver's
whole liveness story: status-checked reconnects, the per-call read timeout,
the cancel race. What it does not inherit is the chat chokepoint. It does not
call `admit()` or `bind_session()`, so there is no thread-session binding, no
continuity or handoff, and no dead-session recovery. Nobody is there to
click, so tool confirmations are answered without a person: with
`DAIMON_TOOL_SAFETY__ENABLED` off (the default) every call is auto-approved;
with it on, reads from attached third-party MCP servers run, and a write is
refused (the agent is told why) unless its server or `server/tool` is listed
in `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`. Daimon's own tools are not
affected, apart from an agent's `ask` or `deny` rule on one
(`daimon-mcp/<tool>`, with `DAIMON_TOOL_SAFETY__AGENT_RULES` on), which a
routine refuses. See [architecture.md](architecture.md).

A routine with a destination sends its trigger message after a
`<turn_controls>` block (`render_routine_controls` in
`packages/core/daimon/core/routine_delivery.py`): the routine's id, agent,
schedule, timezone and destination, and one line saying that nobody is
watching and that Daimon posts the full final reply to the destination
unless the agent posts there itself. If the destination's rule has since
become `writers: none`, the controls say so and tell
the agent not to post there (the result goes to the creator instead). The
scheduler has no Discord client, so it cannot see a thread's parent channel
or a channel's category: on Discord, whenever a parent channel or a category
that could apply has `writers: none`, the controls tell the agent not to
post to the destination itself at all, and the poster — which does resolve
placement — delivers or falls back. These are instructions, not a guard:
`send_message` itself does not yet check the writers rule (that guard is
SYS-048's), so the controls err on the side of not inviting a post. The
controls grant nothing else. A routine without a destination sends its
trigger message byte-for-byte as before.

On success the runner returns the complete final message. `last_result_tail`
keeps a preview of its last 1000 characters, with a truncation marker when
shortened. The outbox keeps the full reply for delivery.

## Delivery

For a routine with a destination, a successful fire also fills the row's
outbox. The scheduler looks through the finished turn for a completed,
non-error `send_message` on Daimon's own server — called directly or through
the MCP search interface's `call_tool(name="send_message", arguments=…)` —
whose `channel_id` is exactly the destination (a Slack thread counts only as
`<channel>:<ts>`; a top-level post in its channel is not the thread). If the
agent posted there, the outbox is `skipped` with note `agent_posted`.
Otherwise it is `pending`, with the complete reply copied into `delivery_payload`.
Discord, Slack and Teams split long results across messages at word or line
boundaries and preserve code fences. Creator fallback messages also keep the
complete result. A
failed fire records `last_error` as always and leaves a result still pending
from an earlier successful fire pending; otherwise the first failure in a row
queues a failure notice ([When a run fails](#when-a-run-fails)).

The scheduler has no chat client, so the post happens in the chat adapter for
the tenant's platform: Discord, Slack and Teams each run `run_delivery_poller`
next to their wake poller (Teams for its one organisation). Each poll claims `pending` rows for its platform
(`FOR UPDATE SKIP LOCKED`, with a two-minute lease), posts, and settles:

- **At most once.** A claim whose lease runs out is settled
  `skipped/interrupted`, never posted again: the poster may have posted before
  it died. A poster that raises is settled `skipped/post_failed`, not retried.
- **The newest result only.** A new fire replaces whatever is still in the
  outbox, so a backlog never posts a run of stale results.
- **Creator first.** Before resolving or sending anything, the poster reads
  the tenant access policy and checks the creator: if the policy cannot be
  read (`skipped/access_policy_unreadable`) or the creator may no longer
  invoke the agent (`skipped/invoker_not_allowed`; admins always pass),
  nothing is sent anywhere — not to the destination and not by direct
  message. Everything below applies only to a cleared result.
- **Policy, at post time.** The adapter resolves where the destination
  actually is and applies the tenant access policy
  ([permissions](permissions.md)): a channel with `writers: none`, a thread
  under one, or a Discord channel in such a category is refused as
  `protected_channel`. A Discord thread whose parent is not in the bot's
  cache has its parent fetched first; if that fails, nothing is posted there.
- **Only where the creator could post.** A routine posts on its creator's
  behalf, so the poster re-checks, every time, the same caller rules as at
  save time: a Discord creator who has lost view/send (or left the guild, or
  is not in a private thread), a Slack creator who is not in a private
  channel or a Teams creator off the channel's roster gets the result by their
  own DM (`dm_fallback:creator_cannot_post`)
  and nothing is posted to the destination. A stored Slack thread is looked
  up with `conversations.replies` before posting, as `send_message` does —
  Slack would otherwise post a reply to a deleted thread at the channel root —
  and a missing one is `destination_unavailable`.
- **Only the routine's own, live workspace.** Discord refuses a channel
  outside the tenant's guild; Slack builds its client from the tenant's own
  team. Either way that, like a channel that no longer exists or that Slack
  refuses (`not_in_channel`, `channel_not_found`, archived), is
  `destination_unavailable`. An archived tenant's rows are not claimed at all.
- **Never silently nowhere.** When nobody may write in the destination or it is
  unavailable, the result goes to the routine's creator by direct message
  instead, under the tenant's direct-message policy
  (`DAIMON_DIRECT_MESSAGE_POLICIES`) and only to a current human member —
  settled `delivered` with note `dm_fallback:<reason>`. If the DM is not
  allowed or fails, the row is `skipped/<reason>`.
- **No broadcasts.** Discord posts with every mention disabled; Slack escapes
  the text the way agent replies are escaped, so `<!channel>` and `<!here>`
  stay literal.
- **A Teams notice.** A result posted in a Teams channel or thread also sends
  the creator an activity feed notice linking to it, one feed entry per
  routine; a DM fallback sends none. A failed notice is only logged and never
  changes the outcome ([teams.md](teams.md)).

The post reads `Result from <agent>'s <schedule>:` followed by the result, for
example `Result from team-020-analyst's daily routine at 09:00 UTC:`. The
schedule is in words only for the cron shapes that say exactly one thing (every
day, weekdays or one weekday at a time, and every hour on the hour, as
`daily`, `weekday`, `Monday` and `hourly`); any other expression stays cron
text, `routine (<cron>, <timezone>)`. The timezone is always kept
(`daimon.core.cron_words`). With agent identity on for the workspace, Slack and
Discord post it as the agent, with its name and face. Discord then drops the
agent's name from the line, `Result from the <schedule>:`, because the webhook
or, without one, the bot's subtext name label already carries it. Slack keeps
the original line, since a workspace without `chat:write.customize` accepts the
post but silently drops the agent's header. The built-in agent and Teams keep
the original line. An empty result is `skipped/no_result`.

On Discord each message of a delivered result is recorded as the routine
agent's post, with the creator as the person who asked. A reply to any of
them starts a turn like a reply to an answer, which matters with agent
identity on, where a webhook post cannot be pinged. The channel tidy tools
then let the agent edit or delete it only for the creator or a server admin.

The fallback direct message to the creator stays Daimon's own notice: the
agent and schedule (`team-020-analyst, every day at 09:00 UTC`), then why the
result came by DM, then the result, a blank line between each. The reason
reads `Your routine couldn't post to its channel. Here's the result.` with
`Ask the agent to change where it posts.` for a destination that could not be
reached; `Your routine couldn't post: you no longer have permission there.`
for `creator_cannot_post`; and `Your routine couldn't post: posting is blocked
there.` for `protected_channel`, each ending `Here's the result.`

The outbox is a small at-most-once queue on the routine row rather than a wake
(`daimon.core.continuity.wakes`): a wake runs an agent turn in a thread,
while this posts fixed text, so it takes the access-policy checks above rather
than the admission chokepoint. A platform with no poller
leaves rows `pending`; nothing is lost or posted wrongly.

## Timeouts, and the ceiling above them

| Bound | Default | Where |
| --- | --- | --- |
| Per-turn ceiling `TURN_CEILING_S` | 45 minutes | `packages/core/daimon/core/turn/ceiling.py` |
| `DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S` | ceiling + 300s margin | `packages/adapters/scheduler/daimon/adapters/scheduler/settings.py` |
| `DAIMON_SCHEDULER__TICK_INTERVAL_S` | 30s | same |
| `DAIMON_SCHEDULER__MAX_AGE_S` | 900s | same |
| `DAIMON_SCHEDULER__MAX_CONCURRENT_FIRES` | 10 | same |
| SSE read timeout | 120s per call | `packages/core/daimon/core/turn/driver.py` |

The ordering is the point. The inner 45-minute ceiling is the one that
normally fires; it covers both session assembly and the drain under a single
deadline the runner computes for itself. `dispatch_timeout_s` is an outer
process guard that only catches a fire hanging *outside* those two legs — row
bookkeeping, agent resolution, recording the result. Its default is derived
from `TURN_CEILING_S` plus a margin rather than hardcoded, precisely so it
cannot be set below the ceiling it is meant to backstop. Each fire has its own
timeout; the continuous scheduler keeps ticking while a fire approaches that
bound. If all dispatch slots are occupied, already-claimed routines wait for
the semaphore without losing eligibility. Once the bounded batch is full,
additional routines remain in the database and follow their catch-up policy
when claim capacity becomes available. Shutdown cancels and joins both running
and queued work, recording `scheduler_shutdown` for cancelled tasks.

## Who may do what

| Action | MCP | Discord | Slack and Teams |
| --- | --- | --- | --- |
| create | a caller with a platform user identity, for the agent they are talking to or the one the destination channel answers with; any agent for an admin | via the agent calling the tool | any member, for the agent the panel's channel answers with; any agent for an admin |
| list / read last output | admin or the routine's creator | `Manage Server`, and only the command's invoker | admin or the routine's creator (the panel lists only your own routines unless you are an admin) |
| pause / resume | `update_routine`: admin or creator | admin or creator, re-checked at click | admin or creator |
| delete | admin or creator | not offered | admin or creator |

Ownership is `created_by_user_id`, and the check fails closed on both nulls:
a caller with no platform user id never matches, and an ownerless routine is
admin-only. Every refusal reuses the same "not found" message so a forbidden
routine is indistinguishable from one that does not exist. The Slack panel
holds reading `last_result_tail` to the same bar as pausing, on the grounds
that a scheduled run's output routinely carries business data.

The MCP `list_routines` and `get_routine` tools show a non-admin only the
routines they created, so another member's trigger and last output (often a
client's work) stay private. A routine runs with its agent's repo, keys,
connectors and memory, which is why a member may only schedule the agent they
are talking to or the one the destination channel answers with.

A channel kept to its own agents (`readers: own`) narrows the MCP tools. A
routine of such a channel's own agent, or one whose `channel_id` is that
channel, is visible (list, read, update, delete) only from inside it, and
`authorize(SAVE_ROUTINE)` creates or moves a routine only where its agent may
answer: a channel's own agent posts only into its channel, and other agents
never post there. Routing can
change after a routine is saved, so each fire asks again: after the agent is
resolved (self-healing may pick a replacement), the scheduler runs
`authorize(RUN_AGENT)` on that agent by every name it carries (the saved
routine name, its MA name and its config name) at the routine's destination,
and skips a run that would cross the line (`own_agents_only`). A routine of
such a channel never falls back to a DM. See
[permissions.md](permissions.md).

## When a run fails

The scheduler does not retry a failed run, count consecutive failures, or
disable a routine after repeated failures. On the mux turn path, an individual
provider write can retry within the caller's existing attempt allowance and
the turn deadline when the provider definitely refused it, such as a rate
limit with `Retry-After`. A timeout or lost response that may follow acceptance
never causes that write to be sent again. Retry state lasts for that running
turn only; a process restart does not resume the routine or its retry allowance.
Session setup and the legacy turn path retain their existing SDK retry policy.

A failed run writes `last_error` and clears `last_result_tail`; a successful one
does the reverse. Both columns are overwritten every run, so `last_error is
not null` means exactly "the most recent run failed" — which is what the
Discord, Slack and Teams panels render.

The next slot was already stamped at claim time, before the outcome was known,
so a failing routine simply waits for its next slot and tries again, forever,
staying enabled.

A routine with a destination tells it when it starts failing. The first
failure after a success (or a routine's first run failing) queues a short
notice in the same outbox a result uses (`record_failure` in
`packages/core/daimon/core/routine_delivery.py`), so the chat adapter posts
it with every check a result gets: creator first, the destination's access
policy, the direct-message fallback. It reads, for example:

```
team-020-analyst's daily routine at 09:00 UTC failed, so there's no result this time.

The request to the AI model failed.

It will try again at its next scheduled time.
```

The middle line says what failed in plain words, never the raw error, by
the `TerminationReason` the scheduler records where it catches the failure: a
model request that failed for good (`UPSTREAM`), a run that took too long
(`CEILING`), a used-up monthly cap, or otherwise "Something went wrong while
it was running." Only these say it will try again at the next scheduled time.

A refusal no later run gets past by itself says it in the turn refusal's words
(`daimon.core.turn.notices`, naming the platform's commands), then
`It will fail the same way at every scheduled time until that's fixed.`: a
depleted balance, a channel that isn't set up or whose setup is out of date
(`MISSING_CONFIG`, `RESOLVER_MISS`), any admission refusal raised during the
run (`AdmissionDenied`, worded by its own reason and cause, so a provider
that isn't turned on names the provider and the chat words that switch it),
and the protected, pinned, isolated or otherwise denied admission reasons. A
provider refusal that names no cause reads "This agent's model provider can't
run here." A used-up channel budget ends `It won't run until the budget has
room again.` A used-up prepaid provider hold (`provider_hold_cap_reached`)
frees up on its own as attempts settle or age out, so it keeps the turn
refusal's words but ends with the try-again line. The chat copy for a pinned agent and
for a creator from another organisation tells someone to ask or mention the
agent elsewhere, which nobody can do for a scheduled run, so a routine says
to change where it posts, or to ask an admin, instead.

The notice is queued only
while `last_error` is still null, so a routine that keeps failing posts once
and stays quiet until a run succeeds; the panels still show every failure.
It never replaces a post that is pending or claimed, so an undelivered result
wins. The outbox row carries `delivery_note = failure_notice` until it
settles. Nothing is posted for `scheduler_shutdown`,
a routine with no tenant or creator, any access-policy refusal
(`invoker_not_allowed`, `access_policy_unreadable`, `runs_elsewhere`,
`own_agents_only`), or a routine without a destination.

The scheduler imports no chat adapter. The strings that land in `last_error`
come from a small, closed set: `invoker_not_allowed` (the
creator is no longer on the allowlist and not a stored admin),
`access_policy_unreadable`, `balance_depleted`,
`cap_exceeded`, `channel_budget_exceeded`, `routine has no created_by_user_id`, `routine tenant not
found`, `scheduler_shutdown`, `timeout: exceeded <n>s` from the outer guard, and otherwise
`<ExceptionType>: <message>` truncated to 500 characters — a ceiling breach
arrives as `TurnError: ceiling: …`.

## Turning routines on

There is no feature flag. Routines fire if and only if the scheduler process
is running, so a deployment that omits the `scheduler` service accumulates
rows that never fire. The process refuses to boot without
`DAIMON_MCP__JWT_SECRET` and `DAIMON_MCP__PUBLIC_URL`, because each fire binds
the per-account MCP vault credential with them. Everything else is tuning:
the six `DAIMON_SCHEDULER__*` settings in
[configuration.md](configuration.md#scheduler).

Per run, four gates still apply — the tenant's invoker allowlist, checked
against the creator, the tenant's credit balance, the per-person monthly cap
and, for a routine with a `channel_id`, that channel's budget, checked last,
after the agent's rule. See [billing.md](billing.md#channel-budgets).
A run refused by a budget records
`channel_budget_exceeded` and the routine fires again at its next slot. A
Discord thread destination saved before channel budgets existed has no
`channel_id` until its destination is set again. Taking someone off the
allowlist stops
their routines at the next fire; the routine stays enabled and records
`invoker_not_allowed`. A fire has no live platform role, so only the stored
admin role exempts the creator.

Routine turns receive core unattended-run framing: avoid clarification questions,
verify evidence, and use delivery tools for requested output. Agents can customize
it with `context_fragments.routine` in their YAML spec; see [architecture](architecture.md#invocation-context-fragments).

Routine sessions always mount persistent agent memory read-only, regardless of the
tenant's chat or DM policy. They can use saved memory but cannot change it. A routine
with a destination (or a channel) mounts the agent's shared memory only: it posts where
other people may read it, with nobody there to ask. A routine with neither, made in a DM
or with no origin, answers its owner alone and also mounts its owner's personal memory,
read-only. This is decided from the routine at every fire, so giving it a destination
stops the personal mount at its next run. A routine whose destination channel is recorded
as shared with another organisation runs held to that channel at every fire: it mounts
that channel's memory read-only and no shared or personal memory, and its Daimon
credential is the held one. It is never run unheld. Delivery follows: a held result goes to
its destination only, with no direct-message fallback (and, on Teams, no activity-feed
notice quoting it), on every platform.

A routine session is stamped like a turn in the channel it fires into: the
destination's channel (a thread's parent), the thread, and the readers limits
over them at fire time (`rule_views.routine_origin`). A routine with no
destination is stamped with its saved channel: the channel it was made in,
or for one made in a DM, the channel that DM came from. So the routine
transcript of a channel with limited readers is read only from inside it,
as its conversations are, by its owner too (a server admin owner still reads
it from the hub, as with their own DMs). A routine with neither is not
stamped. Every stamped routine session also carries the private-DM stamp
(`daimon_private_dm=routine:<id>`): a routine runs on its owner's
credentials, so its transcript stays its owner's alone. No server admin reads it from the hub, as before routine sessions carried a
channel.


### Durable fire diagnostics

Each dispatched routine owns one content-free `turn_outcomes` observation with
origin `routine`. Headless execution borrows it; cancellation injected by the
scheduler deadline leaves finalization to the scheduler, which records `ceiling`.
This preserves the existing timeout/error handling and does not delay the turn
for a database write. Cap refusals are recorded without running a model. A fire
that returns before executing a turn (for example, a missing routine or agent)
currently records `unknown`; shutdown cancellation can also be `unknown`.
The bounded best-effort writer may lose diagnostics during outages, queue
saturation or process crashes. Routine result/error fields remain unchanged.

## Offline scheduler clock hook

`daimon.testing.routine_clock` exposes `RoutineJobClock(anchor=aware_UTC)`
and `ClockedRoutineJobs` for an explicitly configured offline QA runner.
Share the callable clock with `OfflineDiscordGateway`/`DiscordSurfaceHarness`
and wrap it in the host observation clock when binding root/step windows.
Supply the existing scheduler `caps` and `fire` edges and the real adapter
`RoutinePoster`; native scheduled session creation is a separate capability.
`RoutineBinding(tenant_id, routine_id)` joins independently created durable
rows, rather than interpreting model text as successful routine creation.

`await jobs.advance(150.0)` (also 240 or 1920 seconds) runs the real scheduler
and delivery poller in chronological ticks at most 15 seconds apart, draining
each fire before moving time again. Receipts retain actual DB readbacks,
destination decisions, claimed delivery counts and the adapter's captured
HTTP posts. Delivery settlement uses the injected wall clock only when
explicitly supplied; ordinary callers still settle at wall time. Constructors
and invocations validate all database binds through the shared owned-lane or
registered isolated-CI guard. Plain local `daimon_test` is refused by design.

These are component receipts, not catalog PASSes. Unit-test fire callbacks do
not certify a native scheduled session, its memory, or failure-copy delivery.
NEW23 still needs an observed real-path post or explicit warning; the hook
never writes either for a routine without a destination. Without an explicit
credential-form expiry binding, requesting
`advance(1920.0, job="credential_form_expiry")` raises `LongWaitUnavailable`
with code `CREDENTIAL_FORM_EXPIRY_JOB_UNAVAILABLE` before connecting or moving
time. Wizard expiry and GitHub-request expiry cannot substitute for that job.

For the opt-in credential-form expiry profile, pass
`credential_form_expiry=CredentialFormExpiryBinding(tenant_id=authorized_id,
edit=adapter_callback)` when constructing `ClockedRoutineJobs`. The callback
calls the real `edit_expired_posted_card(client, row=row)` companion on the
client attached to the shared gateway clock. Chronological ticks call the real
`poll_credential_form_expiries_once` with the shared wall clock and authorized
tenant. Cursor state remains private; failed card edits stay durable pending
and are retried by the product job. Every poll rechecks all database binds before
and after the job, including its final tick.

Expiry receipts are labelled `opt_in_credential_form_expiry` and retain actual
poll counts and accepted HTTP card edits. Counts alone cannot prove an edit or
promote a source cell. Bind independently created requests/cards with their
persisted 30-minute expiry, actual card identity/readback and the native/root
oracle context. A component test's store creation/renderer proves host expiry,
not native credential-target ingress. Routine jobs and ordinary product defaults
are unchanged.
