# Routines

A routine is a recurring headless turn: a cron expression, a timezone, an
agent and one prompt. The scheduler fires it, the agent runs with no human in
the thread, and the tail of its final message is written back onto the
routine row.

A routine may name a **destination**: a channel or a thread. With one, the run
is told where its result goes, and if the agent does not post there itself,
daimon posts the tail of its final reply there after the run (see
[Delivery](#delivery)). Without one — every routine created before this
existed, and any created without it — nothing is delivered anywhere: the
result is only recorded on the row, and if it should land in a channel the
prompt has to tell the agent to call `send_message` itself (see
[mcp-tools.md](mcp-tools.md#channels)). Session output files, which an
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
| `agent_name` / `agent_id` | the daimon tag is authoritative; the MA id is a cache, self-healed at fire time |
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

## Creating one

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
workspace with daimon able to post there (on Slack, daimon must be a member;
a thread must exist), that the kind matches (a Discord thread is not a
channel), that **the caller themselves may post there** — the same checks
`send_message` applies to a caller (Discord: view and send, send in threads,
membership of a private thread unless they manage threads; Slack: membership
of a private channel, or of any channel for a guest; Teams: on the channel's
roster, in a team daimon is in) — and that the tenant's
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
  (`packages/adapters/slack/daimon/adapters/slack/routines_panel/`).
- **Teams** `routines` (1:1 chat) matches Slack: admins create through a
  dialog, and the panel writes the row the same way
  (`packages/adapters/teams/daimon/adapters/teams/routines_panel.py`). The
  shared rules (glyph, label, ordering, admin-or-creator) live in
  `daimon.core.routines`.
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
affected. See [architecture.md](architecture.md).

A routine with a destination sends its trigger message after a
`<turn_controls>` block (`render_routine_controls` in
`packages/core/daimon/core/routine_delivery.py`): the routine's id, agent,
schedule, timezone and destination, and one line saying that nobody is
watching and that daimon posts the end of the final reply to the destination
unless the agent posts there itself. If the destination has become a
protected channel since the routine was made, the controls say so and tell
the agent not to post there (the result goes to the creator instead). The
scheduler has no Discord client, so it cannot see a thread's parent channel
or a channel's category: on Discord, whenever the policy protects a parent
channel or a category that could apply, the controls tell the agent not to
post to the destination itself at all, and the poster — which does resolve
placement — delivers or falls back. These are instructions, not a guard:
`send_message` itself does not yet check protected channels (that guard is
SYS-048's), so the controls err on the side of not inviting a post. The
controls grant nothing else. A routine without a destination sends its
trigger message byte-for-byte as before.

On success the runner returns the tail of the final message, truncated to
1000 characters, and that is what lands in `last_result_tail`.

## Delivery

For a routine with a destination, a successful fire also fills the row's
outbox. The scheduler looks through the finished turn for a completed,
non-error `send_message` on daimon's own server — called directly or through
the MCP search interface's `call_tool(name="send_message", arguments=…)` —
whose `channel_id` is exactly the destination (a Slack thread counts only as
`<channel>:<ts>`; a top-level post in its channel is not the thread). If the
agent posted there, the outbox is `skipped` with note `agent_posted`.
Otherwise it is `pending`, with the result copied into `delivery_payload`. A
failed fire leaves the outbox alone and records `last_error` as always, so a
result still pending from an earlier successful fire stays pending.

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
  (`packages/core/daimon/core/access_policy.py`): a protected channel, a
  thread under one, or a Discord channel in a protected category is refused
  as `protected_channel`. A Discord thread whose parent is not in the bot's
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
- **Never silently nowhere.** When the destination is protected or
  unavailable, the result goes to the routine's creator by direct message
  instead, under the tenant's direct-message policy
  (`DAIMON_DIRECT_MESSAGE_POLICIES`) and only to a current human member —
  settled `delivered` with note `dm_fallback:<reason>`. If the DM is not
  allowed or fails, the row is `skipped/<reason>`.
- **No broadcasts.** Discord posts with every mention disabled; Slack escapes
  the text the way agent replies are escaped, so `<!channel>` and `<!here>`
  stay literal.

The post reads `Routine result from <agent> (<cron>, <timezone>):` followed by
the result. An empty result is `skipped/no_result`.

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
| create | a caller with a platform user identity, for the agent they are talking to or the one the destination channel answers with; any agent for an admin | via the agent calling the tool | workspace admin only |
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

Confidential channels narrow the MCP tools. A routine of a confidential channel's own
agent, or one whose `channel_id` is that channel, is visible (list, read,
update, delete) only from inside it, and `authorize(SAVE_ROUTINE)` creates or
moves a routine only where its agent may answer: a confidential channel's agent
posts only into its channel, and other agents never post there. Routing can
change after a routine is saved, so each fire asks again: after the agent is
resolved (self-healing may pick a replacement), the scheduler runs
`authorize(RUN_AGENT)` on that agent by every name it carries (the saved
routine name, its MA name and its config name) at the routine's destination,
and skips a run that would cross the line (`channel_isolated`). A routine of
a confidential channel never falls back to a DM. See
[architecture.md](architecture.md) (Confidential channels).

## When a run fails

There is no retry, no backoff, no failure counter and no disable-after-N. A
failed run writes `last_error` and clears `last_result_tail`; a successful one
does the reverse. Both columns are overwritten every run, so `last_error is
not null` means exactly "the most recent run failed" — which is what the
Discord, Slack and Teams panels render.

The next slot was already stamped at claim time, before the outcome was known,
so a failing routine simply waits for its next slot and tries again, forever,
staying enabled.

Nothing is sent anywhere on failure. The scheduler imports no chat adapter;
discovery is pull-only, through the `/routines` panel. The strings that land
in `last_error` come from a small, closed set: `invoker_not_allowed` (the
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
after the agent's channel pin. See [billing.md](billing.md#channel-budgets).
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
tenant's chat or DM policy. They can use saved memory but cannot change it.

A routine session is stamped like a turn in the channel it fires into: the
destination's channel (a thread's parent), the thread, and the seal over
them at fire time (`channel_isolation.routine_origin`). A routine with no
destination is stamped with its saved channel: the channel it was made in,
or for one made in a DM, the channel that DM came from. So a sealed or
confidential channel's routine transcript is read only from inside that channel,
as its conversations are, by its owner too (a server admin owner still reads
it from the hub, as with their own DMs). A routine with neither is not
stamped. Every stamped routine session also carries the private-DM stamp
(`daimon_private_dm=routine:<id>`): a routine runs on its owner's
credentials, so its transcript stays its owner's alone. No server admin or
channel admin reads it from the hub, as before routine sessions carried a
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
