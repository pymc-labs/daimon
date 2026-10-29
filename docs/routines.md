# Routines

A routine runs as the person who last authored its behavior. When another
person changes its trigger message, agent or target, core transfers
`created_by_user_id` to that editor and records an authority-transfer audit
in the same transaction. The scheduler uses that author’s current credential
and permissions. Pause, resume and delete do not transfer authority. A stale
edit is refused. Transfers erase retained output and pending delivery, and
private delivery follows the new author.

A routine is a recurring scheduled turn: a cron expression, a timezone, an
agent and one prompt. The scheduler fires it, the agent runs with no human in
the thread, and the tail of its final message is written back onto the
routine row.
For an agent in GitHub App mode, a routine has no human asker and receives only
the agent's baseline repository grants.

A routine may name a **destination**: a channel, a thread, or its owner’s Discord DM. With one, the run
is told where its result goes, and if the agent does not post there itself,
Daimon posts its full final reply there after the run (see
[Delivery](#delivery)). Without one — every routine created before this
existed, and any created without it — nothing is delivered anywhere: the
result is only recorded on the row, and if it should land in a channel the
prompt has to tell the agent to call `send_message` itself (see
[mcp-tools.md](mcp-tools.md#channels)). Session output files go out with
the reply only when the routine has a destination (see
[Generated files](#generated-files)); without one they stay in the workspace.

For Discord private delivery, set `destination_kind="dm"` and omit
`destination_id`. Daimon resolves it to the authenticated owner's Discord
user ID. A supplied ID must match that owner exactly; admins cannot redirect
private delivery. Slack and Teams reject DM destinations. The user ID never
passes through channel lookup or channel budget resolution: an optional
source channel remains the budget source, otherwise normal tenant/account
billing applies. A routine created from a moved private conversation inherits
that conversation's saved guild channel/thread source, rather than its private
scope or physical DM channel. Source-free cold conversations remain source-free;
missing or restricted moved-conversation provenance cannot create DM delivery.

Both the tenant's inbound DM enablement and outbound recipient policy apply.
Daimon checks current owner membership and invoker permissions at fire and
again before each delivery chunk. Restricted sources cannot be converted to
DM delivery; source restrictions and agent rules are checked again at fire
and delivery, including the original source thread and its parent. Source
provenance is written once at creation and never changed by retargeting,
clearing delivery, changing the budget, or selecting DM. Legacy routines with
unknown provenance keep ordinary channel/thread edits, clearing and normal firing
on every platform, but cannot select DM. Recreate the routine from its channel
or thread to get DM delivery. DM routines run through the normal live-DM path,
with the owner’s ordinary credentials and tools, and can read and write the
owner’s personal memory under the normal live-DM memory policy. Execution is unattended;
permitted tools retain ordinary DM side-effect risk, and nobody watches intermediate actions.

A watch that reports by DM runs each report through the same cold-DM
admission, but read-and-report only (see [watches](architecture.md#signed-page-event-watch-entry-point)).

The host queues the final reply through the outbox and sends chunks of at
most 2,000 characters with mentions disabled. The notification includes the
routine label and run identity. It neither selects a cold-DM tenant nor adds
the notification to an active conversation's model history. Blocked DMs,
revoked access, interrupted claims and uncertain or partial sends are visible
via MCP `get_routine` / `list_routines` in `delivery_status` / `delivery_note`.
Admins can also see failures for their own routines in the Discord `/routines`
panel. There is no public fallback and no automatic resend; a later scheduled
run may try again. Execution failures and slots skipped because the owner
already has a DM turn running record `last_error` without sending a private notice.
Destination changes cancel queued results; a destination changed during a
run prevents that run's result from being queued to the new destination.

Operators can create an owner-DM routine with:

```bash
daimon routines create --tenant TENANT_UUID --owner DISCORD_USER_ID \
  --agent daimon --schedule '0 9 * * *' --timezone UTC \
  --trigger 'Summarize my tasks' --destination dm --dry-run
```

Remove `--dry-run` to write it. Add `--paused` to create it disabled with no
next fire time; without that flag it starts enabled. Resuming a paused routine
computes its first slot from the resume time. Dry-run still verifies the live
owner and agent, and prints the resolved recipient, budget source and whether
the routine would be created paused or enabled. An optional
`--source-channel` must resolve to a supported channel or thread in the selected
tenant's Discord guild. Threads retain their parent for budgeting and their own
ID for source restriction checks. Dry-run performs the same source verification
and sealing checks as a save. There is no independent recipient option. Use MCP
for owner edits. The Discord `/routines` panel requires Manage Server; its
delivery choice is available only when that admin owns the routine. MCP list/get
include the resolved owner ID and delivery status. `clear_destination=true`
retains its existing meaning.

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
| `created_by_user_id` | the **platform** user id of the current behavior author, not an account id |
| `agent_name` / `agent_id` | the Daimon tag is authoritative; the MA id is a cache, self-healed at fire time |
| `cron_expr` / `timezone` | a five-field croniter expression and an IANA zone |
| `trigger_message` | the prompt sent as the turn's user message |
| `enabled` | the pause flag |
| `catch_up_policy` | `skip` (default) or `run-once` after downtime |
| `last_skipped_from` / `last_skipped_until` / `last_skip_reason` | most recent skipped range of scheduled slots and why (`stale` or `in_flight`) |
| `next_fire_at` | the claim key — `NULL` means claimed or paused |
| `last_fired_at`, `last_error`, `last_result_tail` | what the last run did |
| `destination_kind` / `destination_id` | optional: `channel`, `thread` or `dm` and its id, set together or not at all. On Slack a thread is `<channel id>:<thread ts>`, on Teams `<channel id>;messageid=<root id>` |
| `channel_id` | the channel whose budget a run's spend counts against: the destination's parent channel, set when the destination is; without a destination, the channel the routine was made in (MCP `origin_context_id`, or the Slack panel's channel); `NULL` from a DM or with no origin. Clearing the destination keeps it |
| `delivery_status`, `delivery_note`, `delivered_at` | the outbox for the last result: `pending` → `claimed` → `delivered` or `skipped` (with why); `NULL` for a routine without a destination |
| `delivery_payload` | versioned full reply and generated file references (legacy rows contain plain text): that fire's result, copied so a writer that only knows `last_result_tail` (an older scheduler) cannot change what gets posted |
| `delivery_lease_owner` / `delivery_lease_expires_at` | the poster holding a `claimed` row |

Slot arithmetic lives in `packages/core/daimon/core/cron.py`, whose
`next_slot_at_or_after` converts into the routine's zone, steps one second
past the reference instant so it cannot land on it, and returns UTC.

Concurrent runs for the same owner reuse one platform principal and account,
including when the owner's enrollment is still committing. An enrollment
conflict leaves no extra account behind; other database errors still fail the run.

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

Operators can opt into requiring a destination for the MCP `create_routine`
tool with `DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION=true`. A call without both
destination fields then receives a warning that nothing was created, before
agent lookup or saving a routine. The default is `false`, preserving
record-only creation. Existing routines and `update_routine`, including
clearing a destination, are unaffected.

Five MCP tools — `create_routine`, `list_routines`, `get_routine`,
`update_routine`, `delete_routine` — are registered by
`packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py` and catalogued
in [mcp-tools.md](mcp-tools.md#routines). There is no separate pause tool:
pausing is `update_routine(enabled=False)`. Set missed-run behavior with
`create_routine(..., catch_up_policy="run-once")` or
`update_routine(routine_id=..., catch_up_policy="skip")`; the same creator-or-admin
permission applies to updates. The last run is read off the
`last_*` fields that `get_routine` returns.

A channel/thread destination is set with `destination_kind` + `destination_id` on
`create_routine` or `update_routine`, and removed with
`update_routine(clear_destination=true)`. Before anything is saved the tool
checks channel/thread destinations as a pair, that the id has the platform's shape (a
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

The platform panels support routine management:

- **Slack** `/routines` can create and delete. The panel writes the row
  directly rather than going through the MCP tool, because the Slack
  interaction carries the real user id while an agent's token does not
  (`packages/adapters/slack/daimon/adapters/slack/routines_panel/`). The
  New Routine form opens with the plain way, asking the agent in a channel
  ("every weekday at 9am, post the sales numbers"), and labels the schedule
  field "Schedule (advanced: cron, like 0 9 * * 1-5)". The panel rows and the
  confirmation show the schedule in words (`daimon.core.cron_words`).
- **Teams** `routines` (1:1 chat) matches Slack: admins create through a
  dialog, and the panel writes the row the same way
  (`packages/adapters/teams/daimon/adapters/teams/routines_panel.py`). Its form
  and schedule words read the same as Slack's. The shared rules (glyph, label,
  ordering, admin-or-creator) live in `daimon.core.routines`.
- **Discord** opening `/routines` remains admin-only. Server admins use **New**
  to choose an agent, cron schedule, timezone and message. Creation validates
  the agent and schedule and enforces the agent's routine rules before saving,
  with the creator's Discord
  id and verified source. Routines created in threads retain the parent and
  thread IDs, and count against the parent channel's budget. Unresolved parents
  are refused. The creator or a server admin can pause, resume or delete a
  routine; deletion requires confirmation and rechecks permissions.
  Server admins include the owner, Administrator and Manage Server. The panel
  also shows the last output and refreshes after creation or deletion
  (`packages/adapters/discord/daimon/adapters/discord/routines_panel/`).
- The CLI has no routine CRUD. `daimon routines backfill-agent-names` is an
  admin migration helper, `daimon routines create --destination dm` creates
  an owner-DM routine, and `daimon routines sandbox-tools` turns a routine's
  built-in sandbox tools on or off (see [Sandbox tools](#sandbox-tools)).

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
   routine ids, nulls each claimed `next_fire_at`, stamps the claim wall time in `last_fired_at`
   and the selected scheduled slot in `last_fired_slot`, then
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

Compute cleanup uses its own `_repeat_until_stopped` loop alongside the usage
sweep, so routine dispatch never waits for Fly inventory or deletion. A sweep
has no total timeout: large inventories can finish, and inventory failures or
request timeouts still allow registered machines to be cleaned up.
Background Fly command polling has a third loop of its own, so a slow machine
delays neither cleanup nor dispatch. `--once` runs one cleanup pass, then one
polling pass.

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

### Moving routines into Daimon and rolling back

Use the operator CLI with database credentials. Before moving from a source
system to Daimon, pause the source system's scheduler and drain its runs; take each
routine's last **attempted scheduled slot** T from the source system. Recreate
owner-DM routines ahead of the switch with `daimon routines create --paused`
and the required creation options above. Then use
`daimon routines link-source --apply` to set each routine's source ID before
`daimon routines seed-first-run --apply` enables it. Keep the matching Daimon
routines paused and never fired until seeding.

Export each source routine's UUID, cron expression, timezone, last attempted
scheduled slot T and next scheduled run into the seed document below. Capture
these values with the source scheduler stopped and its executions drained.

The seed command is a dry run unless `--apply` is given:

```sh
daimon routines seed-first-run --file seed.json --tenant TENANT_UUID
daimon routines seed-first-run --file seed.json --tenant TENANT_UUID --apply
```

`seed.json` is a nonempty JSON array, with no wrapper or version field:

```json
[
  {
    "source_routine_id": "00000000-0000-0000-0000-000000000001",
    "last_completed_slot": "2026-10-26T09:00:00+00:00",
    "source_next_run": "2026-10-27T09:00:00+00:00",
    "cron_expr": "0 9 * * *",
    "timezone": "UTC"
  }
]
```

Wrapped objects and legacy field names are rejected without aliases. All five
routine fields are required. T and `source_next_run` must be ISO timestamps
with explicit offsets; T cannot be in the future. Cron and timezone must match
the Daimon row, and Daimon's computed first slot must equal `source_next_run`
exactly as an instant. A mismatch refuses the entire file, including differences
between cron implementations around DST or special syntax. Capture with the
source loop stopped and no execution running; omit source-disabled routines so
they remain paused. T means the last attempted slot, including failed, timed-out
or cancelled runs; slots coalesced during a run are not owed.

Identity uses the dedicated `source_routine_id` UUID, unique within each team.
Migration `0161_routine_source_id` preserves existing imported identities while
renaming their column and unique constraint. Later prompt edits preserve this
stored identity. `seed-first-run` needs this stored identity. Linking uses only
the explicit source ID; prompt text does not establish a link.
`daimon routines create --paused` leaves `source_routine_id` unset. For a later
import or a routine recreated by its owner, first create it with `--paused`, then
use `daimon routines link-source --apply` to set the source ID, and finally
run `daimon routines seed-first-run --apply`:

```bash
daimon routines link-source --tenant TENANT_UUID --routine DAIMON_ROUTINE_UUID --source-id SOURCE_UUID
daimon routines link-source --tenant TENANT_UUID --routine DAIMON_ROUTINE_UUID --source-id SOURCE_UUID --apply
```

The default is a dry run; `--apply` writes only `source_routine_id`. Linking
refuses routines outside the selected active team, enabled routines, evidence of
an execution or delivery (including retained results, errors, payloads or leases),
an existing link (even to the same ID), or a source ID already linked to
another routine in that team. Linking and seeding share the same never-fired
check: an edit-only `skipped` delivery status and its note, and skip-window
bookkeeping, do not count as a fire. It preserves the schedule, prompt and
owner. Create with `--paused` so it stays never-fired before linking and seeding.
Seed refuses both missing and ambiguous matches, and never matches a Daimon
routine UUID against the input.

The seed command prints T, Daimon's cron/timezone, the first slot strictly after T (computed in the routine's
zone), the catch-up policy and freshness window, and one of: `wait`, `fire next tick`,
or `roll forward unrun under skip: older than the freshness window`.
It reads `DAIMON_SCHEDULER__MAX_AGE_S` (900 seconds by default); run it with the
scheduler's environment so the preview uses the same window. This describes what a
tick at preview time would do; time passing before the next actual tick can change
freshness. Review every skipped-slot warning before applying.

Apply locks and validates the whole file, then stores T in `seeded_after` and
sets `next_fire_at` and `enabled=true` together per routine in one transaction.
A refusal writes nothing for the entire batch. Refusals include duplicate or unknown ids, ids outside
`--tenant`, enabled routines, any Daimon fire or delivery evidence, and cron/timezone
mismatches. An already enabled routine cannot be resumed again: pause it first.
An ordinary resume of a paused routine schedules after now; use the seed command
for the initial move so it starts strictly after T.

For rollback, use the dedicated handback pause while the Daimon scheduler is
stopped, then drain executions and delivery before exporting. Use the moved
source ids in both commands:

```sh
daimon routines pause-for-handback --tenant TENANT_UUID --ids-file moved-ids.json
daimon routines pause-for-handback --tenant TENANT_UUID --ids-file moved-ids.json --apply
daimon routines export-rollback --tenant TENANT_UUID --ids-file moved-ids.json --output rollback.json
```

`moved-ids.json` is a nonempty JSON array of source UUID strings. The pause
command previews each routine's enabled value and next run by default. Apply
locks and validates the entire selection, then pauses it in one transaction,
recording `handback_paused_at`, its pre-pause enabled value and its pre-pause
`next_fire_at` (N). Unknown ids, ids outside the selected team, duplicate ids
and empty selections refuse the whole batch. Repeating this command preserves
the original captured values. Seeding or resuming clears the handback record.

For a handback-paused routine, export restores the captured enabled value and
sets `expected_next_run` to N. Its `resume_after` is the cron slot immediately
before N in the routine's timezone. Daimon verifies that its next-slot function
applied to that watermark returns N exactly; an invalid or ambiguous DST
calculation prevents finality. This consumes slots already coalesced or skipped,
including slots after a run-once routine's actual claimed slot. The source system
must compute its next slot strictly after `resume_after`, check it equals
`expected_next_run`, and restore `enabled` from the export. A handback that
recorded an on routine without N cannot produce a final export.

A routine paused by the user without the handback command exports `enabled=false`
and `expected_next_run=null`, so it stays off. Its `resume_after` is the latest
claimed slot, else the stored seed T, else null. A claim counts even if execution
fails or delivery is skipped: it may have had effects. An imported routine with
no watermark prevents finality. Historical claims without recorded slots cannot
be reconstructed from wall time and also prevent finality.

Pass the moved source ids with `--ids-file` for handback. An unfiltered export
includes every routine in the team, including native Daimon routines; any enabled
routine in that selection prevents finality. Export also accepts repeated
`--id SOURCE_ROUTINE_UUID` filters. Empty id files and selections matching no
routines are refused. Without `--output`, JSON goes to stdout. Export never
writes to the database.

The rollback document uses `format_version: 2`; version 1 and legacy keys are
not supported. It has `format_version`, `exported_at`, `final`,
`not_final_reasons`, and a `routines` array. Each entry contains
`source_routine_id` (null for routines not imported), Daimon's `routine_id`,
`agent_name`, `cron_expr`, `timezone`, `enabled`, `last_fired_slot`, `last_fired_at`,
`in_flight`, `delivery_status`, `delivered_at`, `resume_after`, and
`expected_next_run`. All exported timestamps are UTC. Use only `final=true` for
handback. Pending or claimed delivery requires operator reconciliation.

`last_finished_at` and `last_finished_claim_at` are cleared at each claim.
Completion records its wall time and the exact claim time it completed, separately
from result content, so an edited destination rejecting the result still counts
as finished. Exceptions while checking spending caps or routine configuration
after claiming record a failure and completion rather than leaving an open claim.
`in_flight` means a claim has no completion recorded for it. A crash or a legacy
claim without completion evidence conservatively remains in flight. If the
scheduler has stopped and left such a claim open, its slot is already included
in `resume_after`; never rerun that slot to recover its output. The open question
is whether that execution's output was delivered. Check the run logs and the
destination, then have the database operator reconcile completion and delivery
for that exact claim before relying on a final export. Preserve its claimed-slot
watermark. This also applies if writing a failure result timed out.

An image reverted to a scheduler older than migration 0151 can update claim time
without updating the recorded slot or completion fence. If claim time differs
from the completion's recorded claim time, export is non-final with an older-
scheduler warning, even when old completion/output fields are present. That
watermark may be stale: recover the actual claimed slot before handback, rather
than treating a completed delivery as proof the slot record is current. The
completion fence is private evidence; it is not an export field.

The export is
non-final if any selected routine is enabled, in flight, has pending/claimed delivery,
or has a historical fire without a recorded slot. It is also non-final if
`resume_after` precedes `last_fired_slot` or is later than `exported_at`, exactly
one of `last_fired_slot` and `last_fired_at` is null, or delivery is marked
`delivered` without `delivered_at`. Inconsistent handback records or a failed
next-slot equality check also prevent finality. Only paused, drained routines
can produce a final snapshot. Finality describes this single database snapshot;
keep the routines paused through the handback. Resolve undelivered work once
rather than rerunning its slot on the source. The export never writes to the database.

On shutdown the scheduler cancels and joins its fire tasks before releasing
the advisory lock, client or database resources. Started cancelled fires record
`scheduler_shutdown`. `--once` deliberately drains its one batch before exiting;
standalone callers can request that behavior with `wait_for_completion=True`.
By default `run_one_tick` returns its dispatcher immediately; callers retain
it for later ticks and close it at shutdown.

## The turn itself

Explicit owner-DM fires use the ordinary live-DM admission, binding and runner,
with normal owner memory and credentials, as described below. The headless path
in this section applies to channel/thread and legacy destination-free routines.

A headless fire resolves the tenant, mints or finds the creator's principal on the
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
`packages/core/daimon/core/turn/driver.py`. So a headless routine inherits the driver's
whole liveness story: status-checked reconnects, the per-call read timeout,
the cancel race. What it does not inherit is the chat chokepoint for channel routines. A channel routine does not
call `admit()` or `bind_session()`, so there is no thread-session binding, no
continuity or handoff, and no dead-session recovery. Nobody is there to
click, so tool confirmations are answered without a person, by the same
unattended decider agent-chat turns use (`unattended_posture`): with
`DAIMON_TOOL_SAFETY__ENABLED` off (the default) an ordinary paused call is
allowed; with it on, reads from attached third-party MCP servers run, and a
write is refused (the agent is told why) unless its server or `server/tool`
is listed in `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`. Every MCP call
pauses for this decider, Daimon's own and with the policy off included, so
none runs unasked. Daimon's own tools are allowed, apart from an agent's `ask` or `deny` rule on one
(`daimon-mcp/<tool>`, with `DAIMON_TOOL_SAFETY__AGENT_RULES` on), which a
routine refuses, and its card-only actions: a routine's session asks on a
backend switch and Microsoft mail and invites, policy on or off, and on
Microsoft file changes with it on, and the routine refuses them. A routine
cannot use built-in sandbox tools (bash, file reads and edits, web fetch
and the rest) or custom tools unless an admin has opted that routine in
(see [Sandbox tools](#sandbox-tools)); custom tools stay off even then. A routine whose agent coordinates other agents (`multiagent`), or whose session
carries a capability this release does not know, is refused before its
message and records `AdmissionDenied: backend_unsupported`; see
[permissions.md](permissions.md#what-an-unattended-session-may-carry). See
[architecture.md](architecture.md). Stopping or cancelling a routine, or reaching
its turn ceiling, leaves pending calls paused and interrupts without sending
confirmations, including on runtimes without a confirmation gate. This also holds when
cancellation arrives while the driver drains a completed decision or a Stop
winner, or retires confirmation cards.

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
agent posted there and left no generated files to deliver, the outbox is
`skipped` with note `agent_posted`. Remaining files are queued without
repeating the final reply.
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

- **No outbox replay across polls.** A claim whose lease runs out is settled
  `skipped/interrupted`, never claimed again: the poster may have posted before
  it died. A poster that raises is settled `skipped/post_failed`, not requeued.
  This is an outbox guarantee: a platform SDK may retry a single send after
  a server error or connection reset, including an uncertain response.
- **The newest result only.** A new fire replaces whatever is still in the
  outbox, so a backlog never posts a run of stale results.
- **Agent invoker roles.** An agent with `invoker_role_ids` cannot run routines,
  including DM routines, even when the creator currently holds one of those
  roles or is a server admin. These agents require a human invocation.
- **Creator first.** Before resolving or sending anything, the poster reads
  the tenant access policy and checks the creator: if the policy cannot be
  read (`skipped/access_policy_unreadable`) or the creator may no longer
  invoke the agent (`skipped/invoker_not_allowed`; admins pass when `invoker_admin_bypass` is true),
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
| create | a caller with a platform user identity, for the agent they are talking to or the one the destination channel answers with; any agent for an admin | via the agent calling the tool | workspace admin only |
| list / read last output | admin or the routine's creator | `Manage Server`, and only the command's invoker | admin or the routine's creator (the panel lists only your own routines unless you are an admin) |
| pause / resume | `update_routine`: admin or creator | `Manage Server` to open; admin or creator re-checked at click | admin or creator |
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

An owner-DM routine records execution failures and `owner_busy` skips in
`last_error` without sending a notice. Channel and thread routines tell their
destination when they start failing. The first
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
it was running." A depleted balance or used-up channel budget says it in the
turn refusal's words (`daimon.core.turn.notices`, naming the platform's
billing command), then that it will try again. The notice is queued only
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

Explicit owner-DM routines are normal live-DM turns: scheduled messages from the
owner into their own DM. Each fire re-resolves membership, the invoker role gate
and admin bypass switch, tenant DM controls, recipient policy, agent eligibility
and immutable creation provenance. It uses the ordinary admission, binding and
runner, including normal owner credentials and personal/shared memory. Explicit
DM routines can read and write the owner’s personal memory under the normal
live-DM memory policy, including `dm_memory_read_only` when enabled. A memory
outage degrades as it does for a live DM.

Headless routine sessions, including legacy destination-free and owner-only runs,
mount the agent's shared memory read-only and never mount personal memory. They
can use saved memory but cannot change it. Explicit owner-DM routines use the
live-DM path described above. A routine firing into a channel recorded as shared
with another organisation runs held to that channel: only that channel's memory
mounts, read-only, and the Daimon credential carries the same hold. A held result
stays in its destination, with no direct-message fallback or Teams activity-feed
notice quoting it.

Each fire owns a fresh private scope with empty history. It never changes the
selected conversation or reads its physical DM history. A shared database owner
guard serializes live and scheduled DMs across tenants and processes. If the
owner already has a DM turn running, the scheduled slot is skipped with
`owner_busy`; no prompt waits for later execution.

DM routines support Managed Agents only and refuse other runtimes before session
allocation. They run with `attended=False`: the shared unattended session gate
checks the actual session before each message or confirmation batch, including
after replacement.
Tool rules, trusted annotations and their credentials use that session's frozen
agent and endpoints. DM execution has no channel tool-rule scope; its saved
budget channel does not grant tool permissions. Card-only actions are refused,
with safety enabled or disabled. **Nobody watches intermediate actions.** The
owner accepts the ordinary DM side-effect risk of permitted tools running
unattended; there is no approval inbox.

Crash, cancellation, timeout and stop guarantees are exactly those of live DMs.
A locally ended turn does not prove that remote work ceased. There is no durable
stop ledger, restart recovery or guaranteed unresolved-stop notice. A successful
turn queues its final result; a delivery failure does not change its execution
outcome. Agent reassignment or any private routine edit clears old private
previews, diagnostics and pending/claimed results atomically with the edit.
Concurrent edits, pauses and resumes compare their authorized revision and refuse
a stale write; reload before retrying so validation and erasure use the current
classification. Revisions strictly advance even if the clock moves backwards;
resume computes its next slot from the same cron/timezone revision it saves.
Late writes compare the captured producer, destination and revision; every chunk
and file rechecks them.
An old result cannot acquire a new agent's authority or move to a public target.

Only explicit owner-DM routines mount personal memory, with read and write
access under the normal live-DM memory policy. Every headless routine uses shared memory read-only and never
mounts personal memory, including legacy rows with neither destination nor
channel and routines switched to **No automatic delivery**. Clearing DM delivery
erases the old private result; the next fire runs headless without personal
memory. Producer or destination changes erase retained results and diagnostics,
and late writes compare the captured producer, destination and revision.
Admins can manage DM routine metadata but do not gain access to its private results.

A channel routine session is stamped like a turn in the channel it fires into: the
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

## Discord creator membership

Discord scheduled fires check the creator's current server membership, role
IDs, and native admin permissions against the tenant invoker policy. Stored
account admin status cannot satisfy a configured role gate. The Discord
poster checks again with a live membership lookup before delivery, including
a private fallback. A departed creator, revoked role, or unavailable lookup
refuses the result; no destination post or private fallback is authorized
by stale evidence. User exceptions and current server admins retain their
invoker-policy bypass; destination and agent restrictions remain separate.

### Sandbox tools

Built-in sandbox tools are off for every routine run by default. They can be
turned on for one channel, thread or destination-free routine at a time, for
that routine's own scheduled runs only. DM routines cannot be opted in: they run
as the owner's private DM turn. Owning a routine is not enough to opt it in.

An opt-in covers exactly the agent and environment the routine runs with when
it is made. **Built-ins are enabled for a run only when the run's agent
version and environment are exactly the ones recorded at opt-in; any mismatch
or missing record keeps them off.** Opting in records the routine's agent id
and version and its environment's id and revision (`updated_at`), and an
opt-in is refused when they can't be read. At each fire the scheduler compares
the run's own session with that record. After anyone edits the agent (its
prompt, MCP servers, tools, skills or anything else that makes a new version)
or the environment, or the routine resolves to a different one, the next run
goes ahead without built-ins, the mismatch is logged, and the opt-in is
cleared with an `edit` history row by `agent_changed`. The operator then
re-approves the routine as it now is by running the command again.

The record covers only what Managed Agents versions: the agent and its
environment. It does not cover what Daimon attaches to a run, such as the
agent's `.env` credentials, its repository binding, vault credentials or the
channel's skills, so changing one of those does not turn the opt-in off. After
changing any of them for an opted-in routine's agent or channel, re-review the
routine and turn its sandbox tools off if the change is not wanted under bash.
The CLI prints the agent version and environment it records for each routine,
including on `--dry-run`.

- **Operator CLI**, the supported way today, for one routine or a confirmed list of
  them:

  ```bash
  daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID [ROUTINE_ID ...] --dry-run
  daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID [ROUTINE_ID ...]
  daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID --off
  ```

  Every id must name a routine in that tenant, none may be a DM routine when
  turning tools on, and each routine's agent and environment must be readable,
  or nothing changes. A routine already set that way, for the same agent
  version and environment, is left alone, so the command can be re-run; one
  whose agent or environment changed is re-approved as it now is. `--dry-run`
  prints what would change and writes nothing.
- **The `set_routine_sandbox_tools` MCP tool** (`routine_id`, `enabled`)
  accepts only a live Discord server admin's own token, re-checked against the
  server on every call. No Daimon surface issues such a token yet; the tool is
  kept for a future admin surface. It refuses agent keys, agents acting in a
  conversation, operator tokens, legacy tokens without a token id, and Slack or
  Teams callers.

Each change sets `sandbox_tools_set_by` (the account or CLI principal) and
`sandbox_tools_set_at` on the routine and appends a row to
`routine_sandbox_history` (routine id, before and after, who, when, and
whether it came from `mcp`, `cli` or an `edit`). An edit that changes what was
approved, meaning the routine's agent, trigger message, destination or budget
channel (including turning it into a DM routine), turns the opt-in off and is
logged as `edit`. Changing the schedule, pausing or resuming keeps it. When an
account is erased for privacy, its id in `sandbox_tools_set_by` and in the
history becomes `erased`; when, which routine and how are kept.
`get_routine` and `list_routines` return `sandbox_tools_enabled`, and the
Discord `/routines` panel shows "Sandbox tools: on (admin opt-in)",
"Sandbox tools: off", or for a DM routine "off (DM routines can't use sandbox
tools)".

A change takes effect from the routine's next run. A run already in progress
keeps the setting it started with, and because the change moves the routine
to a new revision, that run's result is then neither recorded nor delivered.

The opt-in reaches only the routine entry point. Watches, timers and
worker-finished wakes, agent chat, fleet workers, `daimon run` and workspace
checkpoints keep built-ins off with no opt-in.

### Generated files

Routines cannot generate files with sandbox tools unless an admin opted the
routine in ([Sandbox tools](#sandbox-tools)). The delivery pipeline
still supports files already present in a session or queued in an outbox.

After a successful run, Daimon collects any downloadable session outputs with
the same settled-listing collector used by interactive turns. The delivery
outbox stores the full reply and file references, never file bytes. Discord
channels, threads and owner DMs receive attachments; Slack uses its existing
file uploader for channels, threads and creator fallback DMs. Teams routine
posts include a plain notice that files are not supported there.

A delivery includes at most ten files. Files above Daimon's 200 MB ceiling,
or the destination's upload limit (Discord's guild limit, 10 MB in DMs;
200 MB on Slack), are reported in text and remain in the workspace. Files
beyond the count limit are reported too. Internal workspace handoff bundles
and empty files are excluded. Provider copies remain subject to normal
workspace retention; an unavailable reference stops delivery with a failure.

Files share the result's at-most-once outbox claim: a partial or uncertain
send is not replayed by a later poll. The platform SDK may retry a single send
after a server error or connection reset. Changing the destination cancels pending
payloads, including their files. Each file send rechecks the saved destination
and live owner access. Creator fallback DMs also recheck current DM controls,
recipient eligibility and isolation, including a resolved Discord thread parent,
before each file and after download. Files follow the same memory and privacy rules
as text: headless runs never mount personal memory, and explicit owner-DM runs use
the normal live-DM path. Run-failure notices never carry files.

For configured [lead intake](lead-intake.md), begin each triage run with
`lead_intake_resume`, classify every discovered message (including `other`),
and inspect coverage separately from delivery/CRM status. The named routine's
signed execution grant expires when its scheduler fire ends; stale grants and
routine resolution are refused. Capture has no external effects. `crm_paused`
still posts dossiers and archives Gmail. Alert only configured authorized owners
with minimal IDs/error codes and private links, never public fallback content.

Lead-intake routines use isolated session credentials with signed routine and
execution identity, retained during refresh. They cannot borrow an attended
conversation’s read or approval authority, and lead resolution is always refused
unattended.

Retained output and diagnostics on destination-free routines (no destination kind,
destination ID, or channel ID) are owner-only, like DM results. Administrators can
manage their metadata but cannot read those private previews, errors, delivery
payloads, or delivery notes. Upgrade migration `0140_clear_legacy_routine_output`
erases previously retained output for these legacy rows; see the
[self-hosting rollout instructions](self-hosting.md#clearing-retained-legacy-routine-output).

## Long-job dispatch

The scheduler also dispatches [long jobs](long-jobs.md) as separate leased tasks. Their
worker deadline is independent of the interactive turn ceiling. Workers retain leases
across heartbeats, and expired launching/running leases are reclaimed by replaying the
same session. Long-job policy does not enable sandbox tools for routines or other
unattended entry points.


## Repository memory

When native repository memory is enabled, routine turns receive the shared directive
and reference catalog for the agent's live GitHub App working repository, within the
turn's memory size budget. They receive no person's private directives. The four
`repo_memory_*` tools refuse unattended calls, including routine, watch and worker
executions, so a routine cannot save or delete durable state attributed to its owner.
Held or foreign conversations do not receive repository memory.
