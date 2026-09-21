# Routines

A routine is a recurring headless turn: a cron expression, a timezone, an
agent and one prompt. The scheduler fires it, the agent runs with no human in
the thread, and the tail of its final message is written back onto the
routine row.

The most important thing to know up front is what a routine does **not** do.
There is no destination column on the row and nothing is delivered anywhere
when the turn finishes. If the result should land in a channel, the prompt has
to tell the agent to put it there — the agent calls a tool such as
`send_message` itself during the turn (see
[mcp-tools.md](mcp-tools.md#channels)). Session output files, which an
interactive Slack turn uploads to the thread, are **not** swept for a routine;
[slack.md](slack.md) says so, and the code agrees: the headless path installs a
no-op lifecycle with nothing to render to.

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
| `next_fire_at` | the claim key — `NULL` means claimed or paused |
| `last_fired_at`, `last_error`, `last_result_tail` | what the last run did |

Slot arithmetic lives in `packages/core/daimon/core/cron.py`, whose
`next_slot_at_or_after` converts into the routine's zone, steps one second
past the reference instant so it cannot land on it, and returns UTC.

## Creating one

Five MCP tools — `create_routine`, `list_routines`, `get_routine`,
`update_routine`, `delete_routine` — are registered by
`packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py` and catalogued
in [mcp-tools.md](mcp-tools.md#routines). There is no separate pause tool:
pausing is `update_routine(enabled=False)`, and the last run is read off the
`last_*` fields that `get_routine` returns.

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

1. `advance_stale` rolls forward rows that were orphaned (claimed but never
   fired, e.g. a crash mid-claim) or that slipped more than `max_age_s`
   behind.
2. `claim_due_fireable` selects at most 20 due rows with
   `FOR UPDATE SKIP LOCKED`, then in the same statement nulls `next_fire_at`
   and stamps `last_fired_at`. A second phase immediately recomputes each
   row's next slot, per row, so one unparseable cron cannot block the batch.
3. Claimed rows are checked against the monthly cap, then fired concurrently
   under a semaphore sized by `max_concurrent_fires`.
4. Five housekeeping sweeps run, then the loop sleeps — interruptibly, so
   SIGTERM is prompt.

**There is no catch-up.** A slot only fires if it falls inside
`[now - max_age_s, now]`. A scheduler that was down over a slot does not
replay it: `advance_stale` rolls the row forward to the next slot instead.
Missed runs are skipped, never queued, and two due slots are never coalesced
because the claim computes the next slot immediately.

## The turn itself

A fire resolves the tenant, mints or finds the creator's principal on the
tenant's own platform, checks the balance, binds a usage recorder, resolves
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
continuity or handoff, and no dead-session recovery. Tool confirmations are
auto-approved, because nobody is there to click. See
[architecture.md](architecture.md).

On success the runner returns the tail of the final message, truncated to
1000 characters, and that is what lands in `last_result_tail`.

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
cannot be set below the ceiling it is meant to backstop. Note the knock-on:
the tick awaits the whole batch, so a fire that runs to the outer bound holds
the loop for that long, and slots that slip past `max_age_s` meanwhile are
advanced rather than fired.

## Who may do what

| Action | MCP | Discord | Slack |
| --- | --- | --- | --- |
| create | any caller with a platform user identity | via the agent calling the tool | workspace admin only |
| list / read last output | any caller in the tenant | `Manage Server`, and only the command's invoker | admin or the routine's creator |
| pause / resume | `update_routine`: admin or creator | admin or creator, re-checked at click | admin or creator |
| delete | admin or creator | not offered | admin or creator |

Ownership is `created_by_user_id`, and the check fails closed on both nulls:
a caller with no platform user id never matches, and an ownerless routine is
admin-only. Every refusal reuses the same "not found" message so a forbidden
routine is indistinguishable from one that does not exist. The Slack panel
holds reading `last_result_tail` to the same bar as pausing, on the grounds
that a scheduled run's output routinely carries business data.

One asymmetry worth knowing: the MCP `list_routines` and `get_routine` tools
are tenant-wide and ungated, so any authenticated caller in the tenant can
read every routine, including other people's last output.

## When a run fails

There is no retry, no backoff, no failure counter and no disable-after-N. A
failed run writes `last_error` and clears `last_result_tail`; a successful one
does the reverse. Both columns are overwritten every run, so `last_error is
not null` means exactly "the most recent run failed" — which is what the
Discord and Slack panels render.

The next slot was already stamped at claim time, before the outcome was known,
so a failing routine simply waits for its next slot and tries again, forever,
staying enabled.

Nothing is sent anywhere on failure. The scheduler imports no chat adapter;
discovery is pull-only, through the `/routines` panel. The strings that land
in `last_error` come from a small, closed set: `balance_depleted`,
`cap_exceeded`, `routine has no created_by_user_id`, `routine tenant not
found`, `timeout: exceeded <n>s` from the outer guard, and otherwise
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

Per run, two gates still apply — the tenant's credit balance and the
per-person monthly cap. See [billing.md](billing.md).
