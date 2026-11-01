# Architecture

Account erasure previews and deletes the account’s self-hosted resource retry
receipts. The tenant’s shared agent and environment records remain available.
Personal resource writes take the account-erasure lock and reject erased accounts,
so an in-flight mutation cannot leave a new personal receipt after erasure.

For a selected `selfhosted.loop` profile that passes capability admission, the
host resolves the agent and environment from tenant-scoped Postgres resource
ports. Identity and channel-access checks precede resolution; agent policy and
billing gates follow it. It makes no Anthropic calls, including defaults
reconciliation and platform/channel skill resolution. The driver reads MCP
configuration and skill references from the complete stored mux agent spec.
Admission projects only the policy facts into its existing SDK-shaped fields;
that projection does not create a provider client. Existing Anthropic turns
retain their resolver and self-healing behavior. The operator entry point
`daimon selfhosted init` creates or reuses these records without provider
credentials.

Agent fleet configuration uses `request_fleet_config` in an active chat with a
verified confirmation card. The owner or a current admin chooses worker agents
by name and the run's worker, spending and deadline limits. The host resolves the
names within the tenant. Partial edits retain all omitted limits and the saved
expiry; the card lists explicit cap removals. Current-settings reads return the
complete editable configuration. The host reloads the preview and editor permission when Confirm
is clicked, and applies the revision-checked write. Provider allowlist changes
use a separate admin-only confirmation; API keys never enable providers by themselves.

Opted-in Discord worker fleets use the existing posted-card renderer and Stop
guard. The card shows each worker's role, backend, model and current state.
Complete rows stay in ordinal order within Discord's total text limit; large
fleets show one `+N more` count for the rows that do not fit.
Enabled runtime composition requires the shared fleet service and authenticated
root resolver. Root lookup waits until render time; control recovery and
replacement capture reuse the recorded post and existing Discord transport.
Proven fleet absence preserves ordinary sealed and final delivery. Unresolved
or foreign roots and active fleets never disclose intermediate sealed text.
Live and recovered replacement posts share the bounded core turn-post writer.
With the explicit trusted grant loader and shared service supplied, Discord's
runtime installs the real frozen-root reader over the same Postgres state
store as the turn. It selects by the genuine intent, verifies a recovered run,
and preserves the original account and host scope while refreshing the click
subject through existing role readers. Unbound or stale authority refuses.
Stop and Resume reload current root permissions and use the shared fleet
operation owner with a scoped version and idempotency key. The fleet's starter
or a current server admin may press them; each accepted press logs
`fleet.control_pressed` with who pressed it. The ordinary
orchestrator answer is delivered only after authoritative collection and
required result reviews succeed. Default fleet settings create no card,
service discovery or additional sender.

Self-hosted channels use the opening message as the Discord thread title and
stay mention-only on Discord and Teams, without the optional Anthropic
participation classifier. Responder-change notices use a generic previous-agent
label. Automatic Anthropic session-file sweeps are skipped on Discord, Slack
and Teams; the self-hosted harness can deliver files through Daimon's MCP tools.
Self-hosted MCP credentials refuse Anthropic-specific management operations
before any provider request; provider-neutral tools remain available. Channel
authorization resolves their execution identity from tenant-scoped local agent
records, including agent rules. Isolation views, timer lists, current-place cards
and tenant summaries use the same live local records and retain channel
visibility boundaries. Cosmetic Slack identity lookup is skipped.
Adapter turns and their child tasks inherit the same Anthropic prohibition.
Queued continuations retain the thread concurrency lock but start a fresh provider
context for each wake, then apply the requesting caller's own retained-backend
guard. Draining another caller's timer never transfers the originating turn's
provider prohibition. A guard denial defers the queued work for retry.
Organic participation, adapter turns, handovers and continuation guards use one
effective backend resolver with the thread and caller identity. It checks the
retained backend binding before the channel's current selection. A retained self-hosted thread
stays mention-only after a channel backend change; a retained Anthropic thread
can still reply after its channel switches to self-hosted. New threads use the
current channel selection. Discord `/here`, Slack `/here` and Teams `here`
establish the backend context before loading their current-place cards, so
self-hosted cards resolve local agent records without Anthropic requests.
Timers and task continuations check self-hosted targets in local records and
require an installed self-hosted runtime; an unavailable runtime produces a
clear notice. Self-hosted responder notices omit the Hand over button, and
previously posted buttons refuse without a provider lookup.

With the mux turn path and channel backends enabled, an admin can choose **Model
backend** in an agent's setup Details on Discord, Slack or Teams. The preview
lists unsupported skills, connectors and capabilities, shared-thread limits,
and known list prices before confirmation. Apply rereads the agent and checks
the current admin role, tenant allowlist and preview revision through the shared
backend service. The selection applies to new threads; existing threads retain
their backend. Disabled settings add no controls or selection reads.

The MCP `create_routine` tool can require an explicit destination when
`DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION=true`. Missing destinations are
refused after authentication and destination-pair validation, before agent
lookup or creation. This option defaults to `false`; existing routines and
routine updates retain their current behavior.

Daimon is built on
[Anthropic Managed Agents](https://platform.claude.com/docs/en/managed-agents/quickstart).
Managed Agents (MA) owns the agent, its sandbox, its skills and the session
that runs a turn. Daimon owns everything around that: the chat surfaces, the
tenancy model, the config cascade, credentials, the credit ledger, and the
pipeline that turns a chat message into a session and streams the result back
into a thread.

This page is the map to read before the code. `CONTRIBUTING.md` has the dev
setup and the quality gates; [configuration.md](configuration.md) has every
setting; [self-hosting.md](self-hosting.md) has the deployment.

Discord, Slack, scheduler and MCP emit `runtime.health` every 30 seconds with
Anthropic response attempts, database pool use, event loop lag and active turns.
The Discord process also reports Discord 429 retries by route and longest retry wait.
A Discord mention gets a best-effort 👀 reaction before turn admission resolves
its agent and environment. The reaction clears when the turn completes, fails
or is cancelled. A queued follow-up uses ⌛ instead, cleared after its batch
settles. On Discord, Slack and Teams, a mention queued behind a turn older than
three minutes also gets one short notice that its message runs next, at most
once per active turn in that thread. Core owns the timing and deduplication.
A mention waiting for a turn slot also gets ⌛, cleared when the wait ends. An opening mention gets no reply in the parent channel while its thread
opens, and its existing acknowledgment covers naming and creation. A thread
that cannot be opened gets one plain reply under the mention.

Credential cards on Discord, Slack and Teams remove their form button while
saving an MCP or GitHub token. A rejected token or failed save records an
outcome, updates the card with a short error and restores the same form for a
retry, with a fresh 30-minute form expiry. Each retry rechecks the requester, expiry, agent rules and replacement
permissions. After 90 seconds the card says "Still saving; we'll update this
card when it finishes." The save continues with the form consumed, including
long skill imports throttled by the Skills API. Completion replaces that notice
with success or a safe error; only a finished failure restores the form. Confirmed partial progress stays on the card. Successful saves consume
the request and resume waiting work once; failed attempts do not queue work.
Browser OAuth flows have their own lifecycle.

On Discord, a form nobody clicks is retired by the bot's request-expiry poller
(`daimon.core.github_request_expiry`, the same 60-second loop that settles
GitHub access waits, started where the wake poller runs). Each tick spends at
most 20 posted, unused, past-deadline requests, newest first, with outcome
`expired`, then re-renders each card into its expired state: "⌛ This form
expired." and how to ask again, with no button. The edit is best effort: a
card that cannot be edited stays spent, and a late click on it still gets the
expired reply and edits the card again. A backlog, slow GitHub delivery or slow
card edits can push a card past its first minute. Slack and Teams cards are
still corrected only by a late click.

## The shape

```mermaid
flowchart TB
    subgraph adapters["packages/adapters — one platform each"]
        direction LR
        Discord
        Slack
        Teams
        MCP
        Scheduler
        CLI
    end

    subgraph core["packages/core — daimon.core"]
        direction LR
        turn["turn/<br>admit · bind_session · run_prepared_turn · driver"]
        stores["stores/<br>Pydantic over the private ORM"]
        defaults["defaults/<br>seed + reconcile"]
    end

    adapters --> core
    core <--> ma["Anthropic Managed Agents<br>agents · environments · sessions · skills"]
    core --> pg[("Postgres<br>tenants · identity · thread↔session<br>config · credentials · ledger")]
    ma -. "tool calls back over HTTP" .-> MCP
```

The dotted edge is worth noticing early: the MCP adapter is both an inbound
adapter and the server the running agent calls its tools on. That is why a
turn started from Discord still reaches `packages/adapters/mcp/` — the
sandbox makes an authenticated HTTP call back to it mid-turn.

## Packages, and why the boundaries exist

| Package | Path | Owns |
| --- | --- | --- |
| `daimon.core` | `packages/core/daimon/core/` | Schema and migrations, stores, MA helpers, the turn pipeline and pre-turn context providers. Imports no adapter. |
| `daimon.adapters.discord` | `packages/adapters/discord/` | Discord I/O, rendering, permissions, slash commands. |
| `daimon.adapters.slack` | `packages/adapters/slack/` | Slack I/O, Block Kit rendering, per-user OAuth. |
| `daimon.adapters.teams` | `packages/adapters/teams/` | Teams HTTP ingress, Adaptive Card rendering, text and `/` commands answered privately (`private_replies.py`) and panels, file consent delivery, channel files through SharePoint. |
| `daimon.adapters.mcp` | `packages/adapters/mcp/` | The MCP server the agent calls, plus the OAuth, webhook and hub HTTP routes. |
| `daimon.adapters.sidecar` | `packages/adapters/sidecar/` | Signed callback ingress and bounded poll scheduling; no turns or provider credentials. |
| `daimon.adapters.scheduler` | `packages/adapters/scheduler/` | Routine dispatch, long-job workers and maintenance sweeps. |
| `daimon.adapters.cli` | `packages/adapters/cli/` | The `daimon` admin binary, including `context-provider set` bindings. |
| `daimon.testing` | `packages/testing/` | Shared fixtures. |
| `mux` | `packages/mux/` | A separate namespace, not part of `daimon`: the provider-neutral managed-agent contract. Not called by Daimon yet. See [mux.md](mux.md). |
| `notebook_host`, `report_host` | `apps/*/src/` | Standalone services that talk to Daimon over HTTP only. |

None of that is convention. Twelve `import-linter` contracts in the root
`pyproject.toml` are the authority, run as `uv run lint-imports` in pre-commit
and CI:

| Contract | Forbids |
| --- | --- |
| Core must not import adapters | `daimon.core` → `daimon.adapters` |
| Adapters must not import each other | any of cli, mcp, discord, scheduler, slack, teams → another |
| ORM module is private to stores and defaults | anything but `daimon.core.stores.**` / `daimon.core.defaults.**` → `daimon.core._models` |
| Core must not import testing | `daimon.core` → `daimon.testing` |
| CLI admin commands and run must not import each other | `daimon.adapters.cli.commands` ⟂ `daimon.adapters.cli.run` |
| Mux must not import daimon | `mux` → `daimon` |
| Mux contracts and profiles import no provider SDK or driver | `mux.contracts`, `mux.profiles`, `mux.errors` → a provider SDK or `mux.drivers` |
| Only mux.drivers.anthropic / openai / gemini imports its SDK (three contracts) | `mux` → `anthropic`, `openai` or `google`, except from that provider's driver and its test doubles under `mux.testing.drivers` |
| Product code must not import test harnesses | `daimon.core`, `daimon.adapters`, `mux` (except `mux.testing`) → `mux.testing`, `daimon.testing` |
| notebook-host must not import daimon | `notebook_host` → `daimon` |
| report-host must not import daimon | `report_host` → `daimon` |
| harness must not import daimon | `daimon_harness` → `daimon` |

The first two make the platform surfaces replaceable: a change to how Slack
renders a table cannot reach Discord, and core can be exercised without any
chat client. The ORM contract is the one contributors trip over most — the
schema lives in `packages/core/daimon/core/_models.py` behind a leading
underscore, and everything outside `daimon.core.stores` and
`daimon.core.defaults` sees Pydantic models from
`packages/core/daimon/core/stores/domain.py` instead of SQLAlchemy rows. `packages/core/tests/test_orm_import_contract.py` fails when the
contract's enumerated module list drifts from the directory, because
import-linter cannot express "every sibling except `_models`".

The last three are trust boundaries rather than tidiness: a service that
cannot import `daimon` cannot hold the Anthropic key or a database
credential, whatever a future contributor is tempted to do inside it.

## MCP browser pages

Discord, Slack and Teams share the native message draft reducer in
`daimon.core.turn.provider_events`; each lifecycle keeps its own draft state.
Core bundles the Inter faces used by Discord table images and MCP browser
pages. The MCP static mount searches those packaged assets at the existing
`/web/Inter-*.ttf` URLs.

The MCP adapter renders its GitHub, Slack, personal link, MCP connection, and
billing pages through `web_shell.render_page`. The shell owns the Daimon mark,
local Inter fonts, semantic colours, responsive card, and cache and frame
headers. The pages are native HTML using shadcn New York styling recipes;
checkboxes, radios and forms remain native controls. Page modules escape any
dynamic content before passing their trusted markup to the shell. The
stylesheet and assets are served at `/web/` from the MCP package.
Meaningful page icons render as local inline Lucide SVGs. The GitHub mark is
from Feather, since Lucide excludes brand marks; both licences are bundled
beside the Inter font licence.

`scripts/generate_web_css.py` compiles `static/input.css` using the pinned
Tailwind v4 standalone Linux x64 CLI. Run the generator on Linux x64; it does
not ship macOS binaries. The script verifies the binary's SHA-256, scans only
the page modules listed in `input.css`, and writes the committed `static/web.css`.
Run `uv run python scripts/generate_web_css.py` after editing styles; CI runs
the same command with `--check`. The CLI is a build tool and is not part of the
runtime image. The notebook and report hosts have their own page shells.

## How a message becomes a turn

Discord, Slack and Teams run the same two-stage chokepoint in `daimon.core.turn`.
The staging is deliberate — neither stage returns a boolean, both raise typed
errors, so an adapter cannot forget a gate.

Discord displays non-Anthropic message snapshots and preview fragments through
the optional `on_provider_event` lifecycle tap. It updates local draft state;
the render task posts progress and the existing terminal callbacks retire the
card and deliver the final answer. Anthropic keeps its native event tap.
Alternate-provider usage does not become an Anthropic meter; an unverified
display cost is omitted.

Whatever a model writes for a person on one chat platform has the other two
apps' names replaced by "another app" before it is posted
(`platform_words.scrub_other_platforms`, a plain-text pass that leaves lowercase
"slack" and "teams" alone). A URL is opaque to the pass: one on another app's
host is replaced whole, any other is kept byte for byte and only a link's label
is scrubbed, judged by the parsed host. A scrub that fails logs
`outbound_scrub.failed` and falls back to a blunt name replacement, so a reply is
never lost or leaked. `run_prepared_turn` wraps each chat turn's lifecycle
so the card, the answer and any sealed answer arrive scrubbed on all three
platforms, and the status card does the same for its draft and tool lines. The
render loop hands over the whole turn state about every two seconds, so the
wrapper scrubs again only the text block that grew since the last tick. The
tools that post for the model (`send_message`, `create_thread`, `edit_message`,
`rename_thread`, `send_direct_message`, `post_wizard`, `set_display_identity` and
the GitHub install card's reason), a routine's delivered result, the Discord
thread title and a Discord reply recovered after a restart pass through it too.
Stored transcripts, logs and everything sent to the model keep the original text.
The one deliberate exception is an approval card: it shows exactly what will be
sent or run, so it is never scrubbed.

**Stage one, `admit()` — `packages/core/daimon/core/turn/admission.py`.** One
call does identity resolution, config resolution, and every pre-turn gate, and
returns a frozen `Admission` (account id, MA agent, MA environment, resolved
config). The order is load-bearing and documented as such in the module:

1. Resolve the platform user to an `accounts` row, via
   `get_or_create_platform_principal` in
   `packages/core/daimon/core/stores/identity.py`.
2. Channel writers — a turn whose reply would land in a channel with
   `writers: none`, a thread under one or (Discord) a channel in such a
   category raises `AdmissionDenied("writers_none")`, admins included. It runs
   before the invoker gate, whose refusal would otherwise be posted there. If a
   Discord thread's category can't be resolved while any category has a rule,
   the turn is refused.
3. Invoker policy — the tenant's access policy (below) may restrict who can
   start a turn. A refused user raises `AdmissionDenied("invoker_not_allowed")`
   before the cascade, so they learn nothing about the tenant's configuration
   and no MA call is made.
4. Resolve config through the cascade
   `thread → channel → tenant → deployment`, in
   `packages/core/daimon/core/stores/scoped_config_read.py`. The tiers are
   named by `ConfigTier` in `packages/core/daimon/core/scope.py`; the bottom
   one comes from `defaults/config.yaml`, see [defaults.md](defaults.md).
5. Raise `MissingTurnConfigError` if no agent or environment resolved — before
   any MA call, so a misconfigured tenant sees the config error rather than a
   billing one.
6. Resolve the agent and environment to live MA ids via
   `packages/core/daimon/core/ma_resolver.py`, which self-heals by re-running
   defaults reconciliation when a tag no longer resolves, and rejects an agent
   whose `archived_at` is set.
7. Balance gate — `tenant_balance.is_over_balance`.
8. Monthly cap gate — `billing.is_over_cap`.
9. Channel budget gate — `channel_budget.is_over_channel_budget`, against the
   parent channel, or for a DM the channel it was moved from with `/dm-me`
   (`dm_source_channel_id`); skipped in an older DM or where the channel has
   no budget. A channel's own agent counts toward its home channel wherever an
   exempt caller runs it (`AgentPermissions.budget_channel`). The channel is
   carried on `Admission.channel_id` so every debit
   for the turn is attributed to it. The window's first refusal also
   DMs the channel's admins through the adapter's `TurnDeps.budget_notifier`
   (`daimon.core.channel_budget_notice`).
10. Conversation model — `daimon.core.thread_models.apply_thread_model` reads
   the model the conversation's people chose (`thread_model_settings`, keyed
   by the thread, or a DM's own channel) and puts it in `Admission.agent`,
   with `Admission.model_override` naming it. It is read again for every
   turn, so the choice survives restarts and no other conversation sees it.
   A choice no longer in `ALLOWED_MODEL_IDS`, or a channel that runs another
   backend, leaves the agent's own model and sets `Admission.model_fell_back`;
   the turn's user message then carries a `<model_notice>` asking the agent
   to say so in one sentence.

The policy, writers, balance, cap and channel
budget gates each raise `AdmissionDenied` with a reason literal, which
`admission_refusal_text` words in each platform's nouns. See
[billing.md](billing.md).

A Slack `app_mention` runs a turn only when its text contains the bot's own
`<@U…>` mention token, follow-ups in a thread included. Slack's docs say the
event fires only on a direct mention, but installs have reported it arriving
for thread replies that never mentioned the bot, so `_handle_app_mention`
checks the text itself, as Discord checks `message.mentions`. The bot's user
id comes from `auth.test`, cached per workspace. The check runs after the
token read and before dedup, so a sender (an external member included)
who never addressed the bot gets no notice. A dropped event is logged as
`slack.event_dropped.no_explicit_mention`; a failed `auth.test` drops the event
without an error reply.

After admission, Slack resolves the answering agent's message name and avatar
once for the turn. The status card and each answer or continuation post carry
that identity in Slack's message header. The built-in Daimon agent uses the
app header. A missing face is queued for generation after the turn proceeds;
the current picture remains in use until it is stored. Slack is expected to keep the header when the status card is edited into an
answer; each new turn post is recorded under the turn's agent and card intent.

Discord and Slack render a failure as two plain lines, what happened and what
to do, chosen by cause: the AI service busy, unreachable or refusing the
request, the usage limit, a missing Slack permission or unreadable token, a
platform rejection, or "Something went wrong on our side." A small third line,
`Ref` and the last six characters of the request id, lets support find the
`error.rendered` log line that carries the full id and the exception. Provider
JSON, exception bodies and tool details stay out of failure copy. A failed
reply can still be followed by files created during the turn. The output sweep suppresses an oversize notice when
the same filename and size were already attached by Daimon in that turn.

With `DAIMON_DISCORD__WRITERS_NONE_NOTICE=true`, the single posting owner may
add one ⛔ reaction to an explicit bot mention refused by a confirmed
`writers=none` rule. This host signal sends no text or DM and starts no agent
turn. The default is off; unreadable protection and unresolved categories
remain silent. Attempts are limited to one per requester in a parent channel
per five minutes, one per parent channel per 30 seconds, and ten per minute
across the posting process. Failed attempts consume the cooldown; no fallback
message is sent. Limits are process-local and reset on restart.

Discord starts a turn on a direct bot mention or a reply to a recorded bot or
application-owned webhook post in the same tenant and channel. Recorded posts
are the agent's tool posts, a turn's card and answer (a setup-wizard turn's
included) and a routine's delivered result. With agent
identity off, the reply must be to a turn post or routine result the bot itself
sent (the card or any chunk of the answer), so a reply to the second message of
a long answer counts like a reply to the first, pinged or not. The usual
admission path follows either trigger. Agent turn posts use a pool of up to
three application-owned webhooks per text or forum channel, with each thread
assigned by its ID; built-in Daimon posts use the bot. If a webhook is
unavailable, the first answer chunk carries a bold agent name. The bot and MCP
Discord tools resolve the webhook matching a message's webhook ID to edit or
delete that agent's recorded post.
The turn driver closes Discord progress generation before cancelling the render
loop. A shared per-message queue then lets each already-sent progress edit and
missing-card replacement finish before sending the answer, stop notice or
error to that card. The five-second local settle wait does not let a terminal
write overtake a request still in the queue. A dead-session recovery lifecycle
adopts the same queue and card; stale writes queued by the old lifecycle are
dropped. A defensive repair reasserts the final render if a stale completion is
detected. The bot's outer error boundary finishes the card through the same
lifecycle. Long answers keep their summary on the last chunk, and failed turns
preserve sealed answer text before showing the error card. Discord limits a turn
to five answer messages, reserving one for the terminal answer or error card.
Excess sealed or final prose is combined into `answer.txt` on that final message,
with a notice and a warning in the logs. Consecutive identical answer blocks
and text consisting entirely of pseudo tool calls are suppressed. Process death loses
the in-memory queue: a request already sent to Discord may still arrive after
the new process edits an orphaned card to its restarted notice.

A Discord Unknown Message (10008) while editing a turn card posts a fresh
message if the answer has not arrived yet. Once an answer is delivered, a
stale edit is logged and ignored. Stop still interrupts the turn when its
card has been deleted; restart recovery treats a card deleted between its
lookup and edit as already resolved. Other edit failures still propagate.

A turn card's Stop is pressed by the person who asked or by a current admin,
checked on each click: on Discord the owner or a member with
Administrator or Manage Server, on Slack a host-workspace admin or owner, on
Teams an owner of the admin team (read through the shared owner cache, as
every Teams admin check is). When Slack or Teams can't say whether the
clicker is an admin, the click is refused as "try again", not as "not an
admin". Each accepted Stop logs `turn.stop_pressed` with who pressed it.

A turn stops when its own thread is lost. Discord answering a write to the
thread with Unknown Channel (10003), or a thread-delete event for a thread with
a running turn, marks the thread gone. A send still refused as archived (50083)
after the transport's unarchive-and-retry marks it closed; an edit or delete
refused as archived does not, because a thread can auto-archive during a slow
turn and the final send unarchives it. Either stop sets the turn's Stop event,
so the driver takes its normal interrupt path and bills what was already
consumed; the bot then sends the turn's session one best-effort interrupt in
case the stop landed before any event was read. From then on the turn writes
nothing: no progress edit, terminal card, replacement card or error, and
nothing in the parent channel. The transport rechecks the stop before each
Discord call, after any webhook lookup. Messages queued behind it are not
started, their admission rows are finished so startup replay skips them, and
no reaction is added or removed in the lost thread. One `turn.thread_gone` log
records the first signal (reason `gone` or `closed`). The stop record outlives
the turn's release, so a reaction call still in flight sees it; the next turn
to claim the thread clears a `closed` record, and a late error from an earlier
turn cannot close the new turn. A gone thread's live sessions are marked dead
and its in-flight marker cleared, including when a delete event arrives with no
turn running; a closed thread keeps its sessions. Unknown Webhook (10015) is
not a lost thread. The turn's stored outcome is the ordinary interrupted one.

On gateway readiness, Discord scans recently active channels and threads for
missed addressed messages. The window and per-guild/channel limits are
configurable (`discord.startup_replay_*`). Replay defaults to off
(`discord.startup_replay_minutes=0`). Replayed messages use the same protection,
mention/reply, provisioning and admission gates as live events.
The `discord_message_admissions` ledger deduplicates each tenant/message ID;
queued follow-ups remain pending until a card intent is committed or the
request reaches a visible terminal outcome. A PostgreSQL session advisory
lock identifies each process for its lifetime, using one dedicated database
connection outside the turn connection pool. On a failed or slow probe the
guard invalidates the connection and retries `pg_try_advisory_lock` with the
same key and exponential backoff (1–10 seconds) for 90 seconds. Transient
failures leave the adapter running; exhausted recovery signals the entrypoint
to drain in-flight turns through `_drain_and_close` before closing the gateway.
Another worker can reclaim a pending message only after
that owner's lock disappears. Inputs whose card intent was committed before a crash
but never posted are requeued only after two complete no-card history reads
separated by a minute; the ledger reset and intent retirement commit together.
Turn markers and card intents also carry the owner,
so startup never retires another live worker's cards.
Channels whose ledger predates the replay window scan that whole window using
per-message dedupe. Legacy-only channels retain their stored answer boundary
during the first rollout.

Teams admits every message through `teams_message_admissions`, keyed by
tenant, channel (a thread's conversation id without `;messageid=`) and
activity id, which is also the Graph message id, so a Graph notification of
the same message collides. The row keeps the message only until its turn
starts or it is answered (the finish fence); the owner is a per-process
advisory lock, as for Discord. Boot, and a 30-second pass after it, replay
unfinished rows whose owner exited, oldest first per chat, through the normal
intake; rows older than 10 minutes are closed first and then told to resend.
That pass also reruns this process's messages whose fence hit a database error.

Rollout and rollback procedure: on the first deployment of this ledger, set
`DAIMON_DISCORD__STARTUP_REPLAY_MINUTES=0`. Keep that setting for at least the
intended replay window while the new image records live admissions, then
enable replay. Follow the same procedure on roll-forward after running any
older image that did not write receipts: disable replay for the first boot
and wait a full window before enabling it. Otherwise already answered
parent-channel mentions or mentions handled by the rolled-back image may
be answered again. This bootstrap interval intentionally does not replay
downtime messages. If a crash occurs after Discord creates a thread but
before the card intent is committed, replay can still fail to reopen that
thread; the visible notice asks the user to mention Daimon again.

An hourly sweep removes receipts older than the maximum 60-minute replay
window, retaining live pending inputs and one oldest receipt per channel.
That channel checkpoint preserves the ledger's start boundary when pruning;
all other expired inputs owned by exited workers can be removed. The ledger
has both tenant/activity and tenant/channel/activity indexes. Deduplication
of non-opening messages outside the replay horizon is not guaranteed.
The database connection must be direct or use a session-mode pooler;
transaction-mode pooling does not support these session advisory locks.

Orphan recovery runs at most four rows concurrently. Successful completed
provider text is delivered by editing the existing card, with a text attachment
for replies over Discord's message length limit. It verifies the provider's
latest user message belongs to the interrupted turn before recovering its
answer. Other orphaned turns edit that card to explain the restart and invite
a retry, then interrupt the old session. Both recovery edits use the same
terminal card write sequencer as live turns, with no replacement post. If the
provider interrupt errors or times out, recovery marks the old thread-session
mapping dead. The next turn creates a fresh session instead of reusing a
possibly busy one.
Marker cleanup and the dead mark commit together. The orphan card still shows
the restart notice. Provider result reads are bounded to five seconds and card
fetch/edit work to thirty seconds per row. Recovery
serializes edits across workers and conditionally clears the original marker;
a newer turn keeps its marker and its running session. Cards that are already
terminal retain their formatted answer and feedback controls. A retryable
Discord fetch/edit failure retains the marker; a 30-second periodic sweep
retries only the original failed orphan rows, with the same owner and marker
checks. Missing cards and permanent permission/identity failures clear the
marker without posting a replacement. Recovery never sends
a new model request. Generated-file recovery and resuming a still-running
provider session are not part of this text-recovery path.

Deploy drain. On shutdown the Discord adapter refuses new mentions, wizard
submits and DMs, then waits up to 60 seconds for every turn in progress. That
covers mention-path threads it has claimed, held turn slots, and
`TurnDeps.running_turns`. `run_prepared_turn` registers every turn there,
whichever entry point started it. Wizard submits and DMs register the whole
operation, from the slot wait through cleanup. The drain logs
`running_turns` and `turn_slots` beside `inflight_threads`. When the grace runs
out it stops granting turn slots, so cancelling one turn cannot start a queued
one, then cancels everything still in progress and allows five seconds for
cleanup. Cancelling the task ends this process's delivery, not the provider
turn. A thread turn keeps its marker, still owned by this process. If a
dead-session recovery moved the turn onto a replacement session, the marker
moved with it, before the replacement's turn was sent. The move happens only
while the marker still names this turn's card, and never over another turn's
marker. Mention, continuation and wizard cards and markers carry the same
owner key. While that owner's advisory lock or heartbeat grace protects the
marker, boot card recovery leaves the card alone too. Once both have expired,
the next process's orphan sweep delivers the answer if the session has
finished, or edits the card to the restart notice and interrupts the session
(above). Until then the session keeps running. A queued turn's card intent stays unresolved, so boot
recovery shows the restart notice on it. A Discord DM turn shows the same progress
card as a thread, but records no card intent or marker, so a restart leaves that
card at its last update with a Stop button that no longer works. Its
cleanup sends the session one interrupt, bounded to three seconds, while its
delivery still holds the conversation's claim. If that interrupt is not
confirmed, it marks the mapping dead so the next DM starts a fresh session.
The drain does not keep a long turn alive across a deploy, and does not
re-attach a new process to a running session's stream.

Restart recovery keeps a card intent active when a pending card cannot be
edited. It does not post a replacement status card, because the original
button could remain. A definite missing-webhook, missing-token, permission or
deleted-thread failure can mark an aged intent unrecoverable. So can a configured
number of failed recovery passes, counted across restarts. An hourly pass
revisits aged intents without waiting for another restart. It does not overlap
startup reconciliation or inspect a turn still running in this process. The
unrecoverable age setting must exceed the turn ceiling. If the bot can
manage messages, it fetches each known matching card and deletes it only while
it still carries that turn's pending button. Answered cards without that button
stay intact. An isolated transient API failure and shutdown cancellation keep
the intent active for another recovery attempt. An unprompted turn likewise retains its
intent when its card delete fails, including an unknown-webhook error.

Plain-text mentions are a per-tenant switch (`tenants.plain_text_mentions`,
off by default, set with the admin-only `set_plain_text_mentions`). Each
adapter first runs the pure matcher in `daimon.core.name_mentions` on a
person's channel or thread message: "@" plus a bot name, case-insensitive,
any whitespace between its words, not after a word character, "@" or "/"
and not running on into a word character, hyphen, dotted domain or "(";
"Name (Suffix)" also matches "@Name". The price is that "@Name (urgent)" and
"@Name.Thanks" are ignored. A following word does not stop it, so bots whose
names share a prefix all answer the longer one. Bots, webhooks, DMs, messages
that already carry a real mention, Slack `<...>` links and `>` quote lines,
and Teams reply quotes never match. Only a match pays one read of the switch,
and a failed read counts as off. With it on, Discord continues down the
mention path (configured name, account name and server nickname), Slack runs
the `app_mention` handler for the `message` event with the switch, read
first, in place of the mention check; both events claim the dedup slot only
after the mention check, so a message with a real mention runs once, through
its own `app_mention`. Teams (`parse_inbound` marks the message `by_name`; the
name is `recipient.name`) takes the ordinary `_handle` path with the typed
name stripped, or gets the too-long notice a mention would. Off, nothing
changes: a Teams thread reply still goes to participation.

Teams private and shared channels deliver only @mentions to the bot. With
`DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED` on, `channel_subscriptions.py` keeps a
Graph change-notification subscription per such channel
(`teams_channel_subscriptions`, renewed by one task that sleeps until the
earliest expiry). A notification reads the message once, `parse_graph_message`
builds the same `TeamsInbound` as `parse_inbound`, and `handle_graph_message`
claims it under the Bot Framework delivery key before the shared dispatch, so
a message seen by both sources runs once. Graph @mentions are left to Bot
Framework. The permission is
tenant-wide and no read tells whether the bot is in a channel, so a channel is
dropped on the bot's removal event, an app removal (the whole team), or a send
there refused with 403 `BotNotInConversationRoster` (`_RemovalWatch`).

An unmentioned reply in a Discord or Teams thread costs one cascade read of
`thread_participation_scopes`. In a followed thread it joins a quiet-timer
batch; once the thread goes quiet the shared gates in
`packages/core/daimon/core/participation_gates.py` run the hourly cap, balance,
cap and channel budget checks, then the metered classifier. A `respond`
verdict runs an ordinary turn through `admit()` as the burst's newest author,
with every notice withheld. Teams also counts a quote of the bot's message as
a mention.

With `DAIMON_DISCORD__UNMENTIONED_REPLY_HINT=true` (off by default), a person's
first unmentioned reply in a thread the bot opened, in a thread that isn't
followed, gets a 🔔 reaction and no turn: only a mention continues the
conversation. The hint shows once per thread per
`DAIMON_DISCORD__UNMENTIONED_REPLY_HINT_COOLDOWN_H` (in memory, so a restart
allows one more), never on a reply that mentions someone else, never to any
bot, and never where the channel's protection state forbids posting. Every
Discord thread turn carries a `<delivery>` element telling the agent what
starts a turn and that other threads are separate sessions, so it does not
tell someone an earlier message never reached it.

Discord, Slack and Teams limit simultaneous chat turns per tenant, and Discord
also has an optional process-wide limit (`DAIMON_DISCORD__MAX_CONCURRENT_TURNS`,
unset by default). `daimon tenants turn-cap PLATFORM WORKSPACE_ID N` stores a
tenant override; `default` clears it and restores the adapter's deployment
setting (3 by default). The limits cover Discord mentions, DMs, thread
participation and wizard submits, Slack mentions, and Teams messages, wakes and
thread participation. They do not limit MCP or routine turns, and they count
per adapter process.

Each adapter process keeps its slots in one `TurnQueue`
(`packages/core/daimon/core/turn_queue.py`). A requested turn over either limit
is not refused: it joins its tenant's queue (`DAIMON_TURN_QUEUE__*`), posts the
ordinary "Working on it…" card with Stop, and waits after the card and before
`bind_session` (`wait_for_slot` in `packages/core/daimon/core/turn/slots.py`).
Nothing on the card says it is queued; on Discord the mention gets ⌛ for the
live wait (`wait_for_slot`'s `on_queued`), cleared once the wait ends; both
reaction calls run in the background, so they never delay Stop, the max wait or
a granted slot. A released slot goes, in the same
synchronous span, to the first tenant in round-robin order that has a waiting
turn and a free tenant slot; within a tenant the queue is FIFO. A slot covers
one turn: it is returned when the turn ends (`release_turn_slot`), and each
follow-up drained in the same thread takes a fresh ticket through admission,
so a busy thread queues behind other tenants instead of keeping its slot.
Stop removes a waiting turn and ends its card as Stopped. After `max_wait_s`
(300 s) the turn leaves the queue, whether its own timer or the dispatcher
notices first, and its card ends with "Something went wrong. Mention me to
try again." A turn that waited runs the balance gate again once it has a
slot, since `admit()` ran before the wait. The per-turn ceiling starts after
the wait. A Discord wizard submit posts its card and card intent first, like a
mention, and binds its session only once it holds the slot, so session
preparation never runs outside the caps. A Teams continuation turn that never
gets a slot raises `TurnNotStarted` to its dispatcher: a full queue or the max
wait puts the row back to pending, and Stop settles it not delivered, never
as delivered. Only a full queue (50 per tenant, 500 in total) refuses, with the plain
capacity notice and the existing `turn.skipped.*concurrency_shed` logs, now
with a `reason`. A Discord DM waits for its slot under the typing indicator, and its
progress card appears once the turn starts;
Slack DMs are not limited. Unprompted replies and Teams continuation wakes
never queue: they take a free slot or keep their silent refusal or retry. The
queue is in memory, so a restart drops it; queued turns' card intents are
written before the wait, so the boot sweep retires their cards as "Daimon
restarted before this request finished." like any orphan. `formal/turn_queue/` models it.

Queue events: `turn.queue.enqueued`, `turn.queue.started` (`waited_ms`),
`turn.queue.cancelled`, `turn.queue.timed_out` (`waited_ms`),
`turn.queue.full` (`reason`) and `turn.queue.balance_depleted`. The
`runtime.health` heartbeat carries `turn_queue` beside `turns_in_flight`: depth
now (global and largest tenant) and the p50, p95 and max wait of the turns that
started in that window.

A channel with `writers: none` hears nothing from the agent, not even a
refusal or an error. Each turn entry decides FIRST, before tenant liveness, provisioning or
any other read that can fail, whether the agent may post there:
`protection_state` (`packages/core/daimon/core/turn/protection.py`) returns
`unprotected`, `protected` or `unknown`, and never raises -- a policy that
doesn't parse, a database or pool failure, or a failed category lookup all
give `unknown`. The channel and its parent are checked first; a Discord
thread's uncached parent is fetched for its category only when a category
has a rule and the channel isn't already closed, and cached so
admission doesn't fetch it again. Anything but `unprotected` drops the turn
with only a log line, and the entries' error boundaries post only when the
state is `unprotected`. The one exception is a Discord mention whose state is
`protected` (at `on_message` or as admission's `writers_none` refusal) whose rule
positively matches (`turn/protection.py` `confirmed_protection`, which never
counts an unresolved category): it gets a 🔒 reaction and a DM to its author
naming the rule and who can change it, once per person and channel per
`protected_channel_notice_cooldown_min` (`DaimonBot._signal_protected_channel`).
`unknown`, a fail-closed refusal and a startup replay stay silent. The entries are Discord `on_message`, organic thread
participation, wizard submit and continuation turns, and Slack
`_handle_app_mention` (with a second check in `_orchestrate` after it claims
the thread, and on its ephemeral shed notice), and Teams `_handle` and
thread participation (plus the refusals `handle_message` posts). The continuation dispatchers (Teams uses
core `continuity/dispatch.py`), which can post skip or responder-changed copy
outside any turn (from the wake poller or a credential submission), ask the
same decision right before each post and settle the row skipped without
posting when it isn't `unprotected`.

**Tenant access policy — `packages/core/daimon/core/access_policy.py`.** One
`TenantAccessPolicy` per tenant, stored as JSON in `tenant_access_policies`
(`packages/core/daimon/core/stores/access_policy.py`). A tenant with no row
gets the open default, so nothing changes until a policy is written; a present
row that is not a valid policy object (JSON `null` included) raises
`AccessPolicyUnreadable` and callers refuse rather than fall open. Unknown
fields are rejected too, so rolling back past a release that added a field
locks the tenant out until the row is rewritten.

Who counts as an admin differs by path. `admit()` trusts only the live role the
adapter passes; no role means non-admin. The MCP turn tools (`ask`,
`start_turn`, `continue_turn`, on the hub and per agent, plus billed media)
and routine fires have no live platform role, so they use the account's
stored role, refreshed on every chat turn. The operator path
(`platform_user_id` unset: CLI and internal tokens) is not a platform member
and skips the policy, as it skips billing. Ids are the
platform's own (Discord snowflakes, Slack ids, Teams Entra object and
conversation ids):

| Field | Empty means | Enforced by |
| --- | --- | --- |
| `invoker_user_ids` | anyone may start a turn; admins may when `invoker_admin_bypass` is true (default) | `admit()`, the MCP turn tools (`_admit` in `tools/_ctx.py`), routine fires |
| `channel_rules`, `category_rules` | every channel open | `authorize()`, asked by `admit()` (every path: mention, follow-up, wizard submit, continuation) and `reauthorize`, every Discord, Slack and Teams write tool (`require_channel_writable` in `packages/adapters/mcp/daimon/adapters/mcp/tools/_channel_policy.py`), the channel read tools via `ChannelReadPolicy`, the session transcript tools via `tools/_session_access.py`, the routine save and fire checks, every channel default bind, and visibility in the agent, skill, routine, routing and handoff tools, the hub, the setup panel and `/memory` |
| `agent_rules` | every agent runs wherever the cascade sends it | `admit()` after the agent is retrieved (every turn path, DM included), the MCP and hub turn tools, `hand_off_task` via `decide_handoff`, `fork_agent`, and routines at save (`_check_agent_pin`) and at every fire (scheduler) |
| `agent_rules.*.invoker_role_ids` | no extra human role requirement | `authorize(RUN_AGENT)` before admin exemptions; fresh Discord membership at admission, reauthorization, handoff and worker delegation; MCP chat and tool confirmations; non-human headless and routine gates |
| `dm_memory_read_only` (default `false`) | DM turns get writable memory | `admit(is_dm=True)` sets `Admission.memory_read_only` |

[Permissions](permissions.md) defines the rules and every limit they set. A
row an older build wrote (protected, sealed and isolated channel lists and
channel pins) reads as the same rules; writes store rules.

`writers: none` covers threads under the channel and, on Discord, channels in
such a category; it applies to admins too, and runs after the caller's own
permission check so it never reveals a channel the caller cannot see. A
channel whose readers are `inside` or `own` is readable only from a turn
inside it (the channel or a thread under it): the read must pass the
`origin_context_id` from that turn's controls, and a missing, expired or
foreign origin counts as outside. The origin must also belong to the agent
the token executes as (`agent_id`, or `chat_agent_id` for ordinary chat); a
token bound to neither can't claim one. A single thread can take
`readers: inside` on its own: a Discord thread by its id, a Slack thread as
`channel_id:thread_ts`. Such a thread, its messages and (on Discord) its name
are withheld from outside turns: in `read_thread` and `get_message`, in
channel history (a Slack thread's root and broadcast replies; Discord's
thread-created notice, whose text is the name), in the channel context a Slack
top-level mention starts with, in `list_threads` and in search. Once any
readers are limited, search reports only the hits it shows as its total,
scoped or not, on both platforms, and Discord hints at more results only when
a full page of visible hits came back, so neither the count nor the hint can
reveal hidden matches. A turn inside a limited channel or thread gets
read-only memory. An origin also requires the signed `chat_session_id` of
its calling provider session. Each session mounts a separate identity vault,
with a distinct `daimon_session` URL query selecting its credential slot;
the query itself conveys no authority. Shared personal OAuth vaults remain
mounted separately. `run_prepared_turn` binds the origin to the session's
recorded grant before driving it and clears that binding when the attempt
ends, including recovery. Binding a reused session also revokes any origin
left by an earlier crashed execution of that session. Retained fleet-root origins lose their execution
grant. Native OpenAI/Gemini credential resolution derives an origin grant
inside the server's executing turn, retaining the source credential's registry
owner, expiry and revocation. Legacy shared-credential Managed Agents sessions
are replaced on their next bind.
Empty origin IDs never discover a running turn. Outside reads are refused after the
platform's own caller check, and search drops hidden hits.

A session transcript holds everything its turns saw, so the transcript tools
apply the same limit. `admit()` records the turn's channel and thread on the
`Admission`, with every id limiting its readers (its channel and a thread with
its own rule), and `create_session` stamps them on the session
(`daimon_channel`, `daimon_thread`, `daimon_sealed=<ids>`;
`daimon.core.session_seal`). The recorded ids only grow: a limited turn that
reuses a session adds its id (`bind_session`, which waits rather than run the
turn if Managed Agents refuses the update mid-turn), and a session that
replaces another -- by transcript, checkpoint, bundle, handoff or dead-session
recovery -- inherits its predecessor's ids, or is limited to its own thread
when the predecessor can't be read. Each read or follow-up requires the
calling turn's origin to be inside every recorded id, whatever the current
policy, and judges the channel and thread against the current policy as a
channel read would: the main MCP server's session tools take the calling
turn's `origin_context_id`, claimable only by a chat turn's own credential;
agent-chat keys run outside every channel, so they never list, read or
continue a limited conversation, and the hub does the same for members. A
workspace admin may list and read any limited conversation from the hub, and
no other account may read them (see [Trust model](#trust-model)). Limiting a channel's readers
later covers its existing sessions, and opening it never releases a session
that ran limited -- only that thread, when the thread had its own rule. A
session from before the stamp that a thread ran on (`thread_sessions`) has no
known parent channel: while any readers are limited it is shown only to a turn
in that same thread. Nor can a limited turn open or drive another session to
carry its content out: agent chat's `start_turn`, `ask` and `continue_turn`
are off the surface a chat turn's token (`chat_agent_id`) sees, and refuse
that credential outright if they are ever reached with it
(`_require_outside_chat_turn`), so every session they create comes from a
headless caller outside every channel. The one exception is an agent key
minted with "Use from your coding tools" in a channel with limited readers, or
in a channel its agent's rule names (a thread counts as its parent; Teams
panels live in the 1:1 chat, so its dialog asks which channel): its
`mcp_tokens` row records that channel (`coding_token_channel`), and its calls
run as a turn there -- under the channel's rules, environment and budget, with
its sessions stamped to the channel (`token_channel_id`). `authorize` sees it
as the place of the key's turns (`mcp_place`) and as the read origin, nowhere
else, and re-decides both at the moment of action; the rule is read right
before a session is created, and a conversation opened before its channel's
readers were limited can be read but not continued. Keys minted anywhere else
are unchanged. A server admin mints anywhere (`authorize(MINT_CODING_TOKEN)`, through
`authorize_coding_token`).

```bash
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers own --writers own \
    [--copy-from AGENT]            # keep it to its own agents
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers inside
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers any [--release-agents]
daimon channels rule set discord GUILD_ID CATEGORY_ID --category --writers none
daimon channels answerable discord GUILD_ID [--expect FILE] [--json]   # read-only check
daimon agents rule set discord GUILD_ID AGENT --runs-in CHANNEL_ID [--runs-in ...]
daimon agents rule set discord GUILD_ID AGENT --anywhere | --nowhere
daimon tenants access-policy rules discord GUILD_ID [--json]
daimon tenants access-policy get discord GUILD_ID [--json]
daimon tenants access-policy set discord GUILD_ID --invoker USER_ID [--dm-memory-read-only]
daimon tenants access-policy set discord GUILD_ID --clear [--drop-agent-rules]   # back to open
```

An agent with a rule runs only in its listed channels and the threads under
them. The rule is keyed by agent name and checked against both the cascade's
name and the agent's own metadata name, so a thread handed to the agent by id
is covered; setting a rule replaces one on the agent's other names. A turn
anywhere else is refused with `runs_elsewhere`, a member's DM included.
Admins are exempt only where the reply reaches no one else -- their own DM
and hub turns -- and even there the agent's channel sends reach only its
channels (see [Trust model](#trust-model)). `hand_off_task` refuses to bring
it into another channel before anything is written. Its routine must post
straight into one of its channels (a channel destination, not a thread or
none), because the scheduler cannot resolve a Discord thread's parent at fire
time; it is refused at save and skipped at fire otherwise.

`fork_agent` is admin-only and refuses a source with a rule: a copy would be
the agent's prompt, skills and connectors under a name with no rule. A fork
also starts with no credentials (no GitHub access, repo binding or proof, and
no agent-wide MCP token), so copying an agent never hands out another
project's access; MCP servers that only work with a stored token are left off
the copy. The source's own uploaded skills are copied as new skills of the
fork's.

An MCP or hub turn (`start_turn`, `ask`, `continue_turn`, on a new session or
a resumed one) runs in no channel, so it is outside every agent rule, as a DM
is: the policy is read on every call, and an agent with a rule is refused
before any session is created or message sent. Only the operator's internal
tokens, which carry no platform user, bypass it.

Cross-agent separation is complete only for agents with a rule: an agent
without one still answers wherever the cascade sends it, so give every client
project agent a rule. Beyond the rule, whatever an agent can reach (its repo,
keys, connectors and memory) is also guarded where a member could otherwise
borrow it:

A rule naming a channel kept to its own agents must name it alone, and makes
the agent one of them only if it could be the channel's own (custom, and
answering nowhere else); a channel's own agent keeps its rule until that
channel's readers change. An agent without a rule runs anywhere, so dropping
one is explicit (`--anywhere`, `runs_in: null`). `access-policy set --clear`
needs `--drop-agent-rules` while agent rules exist. Rule names must match an
agent of the tenant. Archiving an agent drops its rule in the same step as
clearing the channel defaults naming it, and a channel copy never takes a
name that still has a rule; a rule left by an agent archived some other way is
dropped with `--anywhere`, the only change allowed once no agent has the name. Every supplied id is validated before writing: Discord
ids are 15–21 decimal digits; Slack user ids start with `U` or `W`, channel
ids with `C` or `G`, followed by uppercase letters or digits (a Slack thread
rule is keyed `channel_id:thread_ts` and only takes `readers: inside`). A
Teams rule names a whole `19:…@thread.tacv2` channel; a thread id names its
channel. Invalid input names the field and value and writes nothing.

**Native admins.** Server and workspace admins retain the full setup and channel policy controls.

**Channels kept to their own agents — `packages/core/daimon/core/channel_rules.py`.**
A channel C with `readers: own` keeps its own agents to itself: those whose
agent rule names C alone (`agent_permissions(...).home`, by every name the
agent carries). `set_channel_rule` does it in one write under the policy lock:
it sets C's rule and gives C's default agent a rule naming C. The default must
not be built in, have a rule naming elsewhere or answer anywhere else
(`agent_reach`, checked at write time only); otherwise the call is refused
with the reason, unless asked for a copy. Then `copy_from`, or whoever answers
in C, is copied by `agent_fork.copy_agent` (`authorize(FORK)`: an admin's
call, never an agent with a rule; no credentials, no agent-scoped skills,
which the reply names) under a name from the channel, made C's default and
given its rule; a copy the locked re-check refuses is archived. Moving readers
off `own` keeps the agents' rules unless `release_agents` drops them, and warns
that they keep what they remembered in C.

Enforcement is `authorize()`'s: it fills `AgentRef.permissions` and
`Place.permissions` from the policy ([permissions](permissions.md)), so every
check and re-check is fresh. In C only C's own agents run, post, read, get
routines or become the default (`own_agents_only` on `RUN_AGENT`, `POST`,
`READ_CHANNEL`, `SAVE_ROUTINE` and `BIND_CHANNEL_DEFAULT`); a setup thread
under C (`Place.setup_thread`) still answers as the built-in agent. C's own
agents post nowhere outside C, not even the requester's DM, and send no direct
messages (`DIRECT_MESSAGE`) or create agents (`CREATE_AGENT`, also refused for
any call whose verified turn origin is in C, such as its setup thread), whose
prompts would answer outside C. Publishing (`PUBLISH`: `publish_report`, the
notebook and attachment upload URLs and `set_display_identity`, each seen
outside C) from C, or by any agent with a rule, waits for the requester's
Approve ([publishing](permissions.md#publishing)); a chat turn naming no
verified origin while some readers are `own` is refused, as for
`CREATE_AGENT`. Admission, `reauthorize` and the scheduler's fire check (the
resolved agent, by every name, at the routine's destination) decide through
`RUN_AGENT`; thread participation skips a refused turn before its classifier
runs. Memory stays writable for C's own agents in C and is read-only for any
other agent there, as with `readers: inside`; a session opened with a
coding-tool token bound to C follows the same rule. A session whose recorded
ids lie in C is read and continued only by C's own agents (`READ_SESSION`,
`CONTINUE_SESSION`). A verified turn origin in C holds the call to C whatever
agent runs it (`origin`): its posts, cards and routines stay in C and it sends
no direct messages; only its setup thread may still configure C's own agent.
Held to C, by its own agent or such an origin, a call also reads only C and
the sessions that ran there (`own_agents_only` on `READ_CHANNEL`,
`READ_SESSION`, `CONTINUE_SESSION`; `home_hold`); a session with no channel
stamp counts as outside C. A chat token's signed session grant identifies
its live origin, so a call remains held there even if the model omits or
names another origin (`_held_origin`). Other sessions' origins never supply
a hold or read authority. Held, `list_channels` and the
search tools name nothing else, so nothing read elsewhere, a prompt planted in
C included, is reposted into C. A thread routine saved without its parent
channel is treated as inside any such channel until delivery places it
(`Place.parent_unresolved`). The rule constrains agents: a caller with no
executing agent (an operator token, the CLI) may still post into C, which is
input, not a leak; the readers rule keeps reads inside.

When C closes, `archive_channel_copy` (server admins, or the `agents:archive`
operator scope; `core/channel_copies.py`) archives the copy `set_channel_rule`
made for it, stamped `daimon_isolation_copy`, with its rule and default in C.
It archives no other agent and no default, and refuses a copy whose rule or a
default names anywhere but the channel named as closing. C keeps its rule, so
nothing answers there after.

**Channel skills — `packages/core/daimon/core/channel_skills.py`.** A channel
can add skills to whatever agent answers there, for its turns only, so a
shared agent carries one team's skill without every channel getting it.
Server admins and operator tokens (`channels:write`) add and remove them,
never a channel's own admins (`authorize(SET_CHANNEL_SKILLS)`), with the
`*_channel_skill(s)` MCP tools, Who answers where in the Discord and Slack
setup panels (the Channel settings dialog on Teams), or the CLI:

```bash
daimon channels skills list slack TEAM_ID [CHANNEL_ID]
daimon channels skills add slack TEAM_ID CHANNEL_ID SKILL
daimon channels skills remove slack TEAM_ID CHANNEL_ID SKILL
```

A channel may add a library skill of its tenant, or one uploaded to the agent
answering there now, unless that agent is another channel's own.
The row (`channel_skills`) pins the latest version at add time; adding it
again picks up a newer one. Admission reads the rows once per turn
(`turn_channel_skills`) and drops another agent's upload, a skill the agent
holds, a clashing mount name and anything past the session cap; the session
is created with the agent's skills plus these, and the drift check hashes the
same list (`session_snapshot.session_skills`), so adding or removing one
replaces the conversation's session on its next message. Routines, headless
runs and MCP chat don't use them.

**Deleting a skill — `packages/core/daimon/core/skills/attachments.py`.** A
session resolves every skill its agent pins and its channel adds, so a deleted
skill that something still names fails every turn there. `daimon skills
delete` and the `delete_skill` MCP tool look for references first (any
non-archived agent, since skills are workspace-wide, and every `channel_skills`
row) and refuse while there are any, naming the agents and channels and how to
detach. The MCP refusal names only the caller's own workspace's agents and
channels and counts the rest. `daimon skills delete --force` detaches the skill
from each agent (keeping their other skills at the versions they pin) and each
channel rule, in every tenant (`--tenant`/`--guild` only picks the skill's
name), then deletes it; its confirmation gives those counts and how many are
outside the selected tenant. Chat has no force.

Deleting and attaching are serialized per skill
(`packages/core/daimon/core/skills/reference_lock.py`): a delete takes a
Postgres advisory lock on the skill id exclusively and holds it from its final
reference check through the provider delete (`delete_skill_unless_attached`).
Every delete goes through it: the CLI, MCP, the defaults sweep, the defaults
duplicate cleanup (a same-title duplicate still in use is kept), the skill-repo
sync's orphan cleanup (the synced agent's own pin is retired with the skill;
any other agent or channel rule keeps the skill and its registry row), and the
skills backfill. Every write that adds a skill reference takes the same lock
shared and checks the skill still exists first: a channel rule
(`add_skill_to_channel`, held until the row commits) and any agent create or
update that adds a skill (`guarded_agent_update` / `skill_attach_guard`: skill
add, repo sync attach, the skill-repo credential forms, `update_agent`, fork,
`reconcile_agent` — defaults, `daimon agents create`/`update`, the panels and
`create_agent` — the report reader variant, and the backfill re-pin).
`reconcile_agent` and `ensure_reader_variant` take the lock scope as a
required argument; the defaults reconcile passes its own lock session. An
attach that waited for a delete is refused ("was deleted just now"; a reader
variant is rebuilt once from its source as it is now); a delete that waited for
an attach sees the new reference. If a session is still
refused for a missing skill (`Could not resolve one or more skills`),
`create_session` raises `SkillUnavailable`, a `UserFacingError` that names the
skill and says to remove it from the agent or channel, instead of the generic
"AI service couldn't accept the request"; a replacement session re-raises it
rather than suggesting a retry.

**Stage two, `bind_session()` — `packages/core/daimon/core/turn/prepare.py`.**
Finds the live `thread_sessions` row for this thread or creates a fresh MA
session, assembles every `create_session` argument (credential env mount, MCP
vault, repo resource, memory store), writes the mapping row, and binds the
usage recorder. It returns a frozen `PreparedTurn` whose recorder field is
underscore-prefixed: adapters never construct billing wiring, and the only way
to reach the recorder is to hand the `PreparedTurn` back to stage three.

Whether an existing session may be reused, refreshed in place, or must be
replaced is decided in `packages/core/daimon/core/session_preparation.py`
against the fingerprints stored on the mapping row; the outcome rides back on
`PreparedTurn.continuity` so the adapter can say what happened.

GitHub App mode uses the agent's live grants when assembling a turn. External
askers get no App token. Each session gets one write token scoped to the agent's
live repositories in each installation, with a separate token for each exact
repository group. An agent can hold grants from up to 12 GitHub accounts or
orgs. The token is recorded before minting
and checked again after delivery. A repository is live when it remains in both
the tenant-confirmed Connect set and the
latest complete GitHub installation snapshot. Until the first refresh, the
confirmed set applies. A GitHub-confirmed uninstall or suspension disables
coverage; later refreshes can restore it when the repo is still confirmed.
The refresh never adds repos to the tenant-confirmed set.
The browser repo editor lists installations per owner, with at most 20 pages
(2,000 repos) per owner. A failed owner's list is isolated from the others.
The form signs both the grant snapshot and the GitHub inventory shown to the
person. Its one POST resolves each selected repo's installation from the
verified user lists and applies additions, removals, and the working repo in
one serializable transaction under the tenant grant lock. A changed inventory
or grant snapshot returns 409 before any grant changes. Previously granted
repos missing from the person's verified GitHub listing show as hidden rows and
can only be removed. A capped owner list explains its 2,000-repo limit without
revealing names of unlisted grants. A picker save changes only the submitted
grants; it leaves other grants in place even when their authorization is stale.
The synthetic browser layout and interaction check is manual:
`uv run --with playwright==1.58.0 python scripts/check_github_picker.py`.
If token minting returns 403, 404, or 422 for a repository group, that group's
pending token is closed, its installation is queued for reconciliation, and
only successfully minted groups enter the session. The refused group's repos
are recorded with a per-URL first-refusal time in the session snapshot. A pending
notice is announced on the next turn, including after reconciliation removes the
repository or the exclusion expires, subject to the requester's repository-name
visibility. The notice is sent once for a continuous refusal; if access returns
and a later mint refuses it again, a new notice is sent. Authentication failures,
rate limits, timeouts, and server failures stop preparation so it can be retried.
Each exclusion remains valid for 60 minutes from its first refusal. Token
rotation never retries a refused repository, even after its 60 minutes, so
the deadline holds and retries stay at one per turn. If the repository is still desired when the exclusion expires,
the next turn replaces the session and retries minting once. A refusal in the
replacement starts a new 60-minute period.
Reconciliation can change coverage sooner, triggering normal session change
handling. The scheduler leaves a session with a recorded refusal or an
unannounced notice to that turn-boundary replacement, which carries the notice
history, instead of retiring it; until then the removed repository's mount gets
no new token. Issued installation tokens remain valid for up to one hour, so
reconciliation does not immediately revoke access already delivered to a
session. GitHub does not document a unique status code for an uncovered or
suspended repository; HTTP 422 can also mean spam detection.
MCP and headless sessions do not store refusal history. When their token
rotation reports a refused repository, the scheduler backs off later mint
attempts using the same delay as transient refresh failures, including while a
turn is active.
The session mounts only the selected working
repository, if there is one, and uses its own vault for `GH_TOKEN` and Copilot.
Other granted repos are available through that token
for cloning by name. A change to the working mount or the token repository
set replaces the session on its next turn; unchanged sets receive refreshed
tokens through resource updates. A session holding an App token also mounts
`/mnt/session/uploads/git-identity.sh`, two `git config --global` lines that
make sandbox commits author as the App's bot (`<app-slug>[bot]`, with the
noreply email `<bot-user-id>+<app-slug>[bot]@users.noreply.github.com`); the
bot id is read once per process from GitHub and cached, and a failed lookup
just leaves the file off. It is a separate file because an agent with no secrets
has no `.env` and the `.env` bytes feed the session fingerprint. A member-set
`GIT_AUTHOR_*` or `GIT_COMMITTER_*` key in the `.env` still wins over it, as
git's environment variables outrank its config. The agent guidance tells agents
to commit with git and open pull requests with `gh pr create` on `GH_TOKEN`,
ending the body with "Requested by <author_name>" (or "Requested in chat" when
the `<user_query>` has no `author_name`), instead of the GitHub MCP, where a
person's own sign-in would outrank the agent's credential and author the work.
In the Managed Agents sandbox `GH_TOKEN` is a placeholder swapped in only on
outbound requests, so the guidance has agents push with the explicit
`https://x-access-token:${GH_TOKEN}@github.com/<owner>/<repo>.git` URL and never
run plain `git push` or `git fetch` against origin, which hang. Slack renders
`author_name` (the requester's profile display name, else real name, else
username, from the turn's one `users.info` call) on the `<user_query>` as Discord
already does; Teams does not.
An agent’s working repo is the App grant marked `is_working_repo`. Existing repo bindings are carried over during the App-only migration when a matching grant exists.

**Stage three, `run_prepared_turn()` —
`packages/core/daimon/core/turn/run.py`.** Calls the driver, and on a
dead-session 404 recovers exactly once: mark the mapping dead, create a
replacement, read the dead session's event log back into the reseeded message,
rebind the recorder, re-run. A second dead signature is returned as-is.

Both stages two and three are wrapped by one shared per-turn ceiling from
`packages/core/daimon/core/turn/ceiling.py` — `TURN_CEILING_S`, 45 minutes. It
is a backstop against an MA session that never leaves `running`, not a latency
target; legitimate turns that fit a model or build a notebook run for many
minutes. `admit()` sits deliberately outside it.

### Pre-turn context providers

Before the driver sends inference input, `core/context_provider.py` loads an
optional binding through `core/stores/context_providers.py`. Bindings select one
provider by channel and MA agent: channel+agent, channel, agent, then tenant.
An explicitly disabled override stops fallback. With no binding the input is
unchanged. Discord supplies raw message text and ordered non-bot mentions;
other surfaces supply no mentions or raw text. The hook currently serves
Managed Agents sessions, including replacement sessions during recovery.
For DMs, `channel_id` carries `private_dm_id` when known, otherwise the admitted
`dm:` scope ID. `thread_id` carries the adapter's DM conversation identity,
which is likewise the private DM ID when known or the `dm:` scope ID. These
values identify the destination; the provider must still verify the requester.

A deployment operator can bind a provider with:

```bash
daimon context-provider set provider.json --tenant TENANT_UUID --channel CHANNEL_ID --agent MA_AGENT_ID
```

Omit either selector for a broader binding. This is a deployment-operator CLI:
shell access to the configured host is the gate. The store also rejects setters
without human-admin authority and setters using agent keys; the CLI supplies
operator authority. To disable a binding, set `"enabled": false` in its JSON and
run the same command with the same selectors. There is no delete command;
a disabled override continues to stop fallback. Example `provider.json`:

```json
{
  "transport": "mcp",
  "endpoint": "https://provider.example/mcp",
  "bearer_env": "DAIMON_CONTEXT_00000000000000000000000000000001_BRIDGE",
  "enabled": true
}
```

The bearer environment name must start with `DAIMON_CONTEXT_`, followed by the
tenant UUID without hyphens in uppercase, then `_`. The secret stays in the
host environment and is never stored in the binding. The provider verifies
caller, server, requester and destination; request metadata grants no placement
authority. Endpoints require HTTPS, with redirects and environment proxies off.
Each provider owner keeps its own connection pool and refuses all cookies;
authentication headers are set independently on every request. The process
loads TLS roots once, outside the turn path, and reuses that SSL context when
building further provider owners. Discord, Slack, Teams and the scheduler start
best-effort background pre-connects to enabled HTTPS binding origins at runtime
startup (except scheduler `--once`). The same pooled client sends unauthenticated
`GET /health` after about 200 seconds without traffic, below its 240-second
connection expiry and the
provider's 300-second idle close. Health requests carry no credentials or
session/tenant metadata, ignore status codes, and never follow redirects. The
owner checks idle connections every 20 seconds and reloads bindings every tenth
check (about 200 seconds). An initial failed load retries on the next check;
malformed bindings are individually skipped and logged. The task stops on
runtime shutdown. Database reads and health requests have five-second limits;
health bodies are read up to 4 KiB, then closed, and failures only log.
Provider calls emit one timing record from the HTTP trace (connect including
DNS, TLS, response-header wait, and whether a connection opened); DNS is null
because the standard transport trace does not expose resolution separately.
The client posts MCP `tools/call` for `context_cards_for_turn` without initialize,
for FastMCP `stateless_http=True, json_response=False`; both JSON and SSE replies
must contain one JSON-RPC result with a v1 `ContextResponse` in `structuredContent`.

The host allows 400 ms total for the configuration read, session lock and
provider call while no system fragment has been acknowledged for that session
(`system_fragment_received` is false). The binding field `first_call_timeout_ms`
defaults to 400 and can lower this budget, with a maximum of 400 ms. After system
acknowledgement, the total ceiling is 200 ms, lowered by `timeout_ms` (default
and maximum 200). The decision is made under the session lock: if another turn
acknowledges the fragment during the wait, the 200 ms deadline applies from
the hook's start. The configuration read always has its own 200 ms bound.
`deadline_ms` tells the provider how much time remains. There are no retries. It drops
the entire response above 64 KiB (enforced while streaming), 2,500 turn tokens,
or the independent system budget (1,000 by default, configurable up to 2,000
with `system_max_tokens`). `max_tokens` can lower the turn budget. The host
recomputes tokens as `ceil(chars / 3.5)`. A refusal, timeout, invalid response
or exception produces a normal turn without context; failures log only tenant
and exception class, never provider text, card IDs, sources or credentials.

Connections are reused and idle connections are kept for 240 s. A late provider
call or configuration read finishes in the background and its result is discarded,
letting the connection warm for later turns without advancing acknowledgements.
Each owner retains at most eight late calls;
additional late calls are cancelled. These eight slots are shared across tenants
on an owner in the process. Provider calls and configuration reads have the same
five-second wall-clock cap. Cancelling the parent turn still cancels its pending
work.

The turn fragment precedes user input. The once-only system fragment joins the
system message, or precedes user input on models without system-message support.
`PreparedTurn.session_model_id` carries the frozen session model for this
capability decision, including when a reused session differs from its live agent.
Acknowledgements advance only after inference input send succeeds. A new MA
session starts fresh. Acknowledgements use a bounded process-local LRU (1,024
sessions, 24 hours idle, at most 512 hashes each); restart or eviction can repeat
cards. They are neither durable nor shared across workers, and do not cache
provider authorization. Already injected historical content cannot be retracted.

### The driver

`packages/core/daimon/core/turn/driver.py` opens the SSE stream, posts the
user message, and runs a consume loop and a render loop concurrently until the
session goes idle or errors. Adapters plug in through the `TurnLifecycle`
protocol in `packages/core/daimon/core/turn/lifecycle.py`, which documents a
per-hook cost contract:
`on_render` is the answer-text delivery path and may talk to the network,
because it runs on its own task and cannot stall the pump; `on_sse_event` is
awaited inline in the consume loop and must stay a cheap local tap.
The render loop calls `on_render` on every tick (2 s on the chat paths),
whether or not the turn state changed, so a card's elapsed time keeps moving
while a long tool call produces no events. Each adapter's own debounce decides
which calls become platform edits: 10 s on Discord, 5 s on Slack and Teams. The
finalizers' render after the terminal event stays guarded, so it runs only if
that event changed the state.

Managed Agents delivers each `agent.message` whole (its streamed deltas are
previews that never reach the reducer). With `DAIMON_TURN__STATUS_LOG` off, the
default, the reducer joins consecutive messages into one text block and the
answer is every text after the last tool call. With it on, the reducer keeps
every message as its own text block, and an answer of several messages keeps
every one set apart by a blank line (a run of messages before a tool call is
one candidate for a sealed answer, joined the same way, so Discord compares
like with like when it drops a repeat). `daimon.core.turn.status_log` holds the
switch. Either way the status card on Discord, Slack and Teams shows the last
three messages as a small log right under the headline, oldest first, each cut
to one line of 120 characters. The plan and the tool lines follow, in that
order (`status_lines.CARD_SECTIONS`). On Slack the log's section is also held under
Slack's 3,000 character limit after escaping. For a non-Anthropic provider,
each new message item starts a new entry and its previews and snapshots revise
that entry.

A tool line names the tool and nothing it was called with, with one
exception: an allowlist in `daimon.core.turn.status_lines` adds one field of a
built-in tool's input, clipped to one line. `web_search` shows its query,
`web_fetch` the host name only, and `read`, `write` and `edit` the file's base
name. Bash, MCP and custom tools stay name-only, so a command, a URL's path or
query, or a directory never reaches the card.

Progress plans are a second switch on the same scope cascade as thread
participation (`daimon.core.scope_cascade`): `DAIMON_PROGRESS_PLANS__MODE`,
then workspace, channel and thread rows in `progress_plan_scopes`, set with the
`set_progress_plans` tool, Slack included (a Slack thread is keyed
`<channel>:<thread ts>`). The `update_plan` tool is registered on every turn,
so the tool list and the system prompt stay the same whether the switch is on
or off. Before a chat turn `run_prepared_turn` resolves the cascade once; when
it resolves on, a short `<progress_plans>` block goes in front of the user
message and the turn's `TurnObservation` is marked, so the card draws the
latest `update_plan` steps as a checklist under a Plan header, between the
latest messages and the tool lines' Details, in the card's own icons (☑️ done,
⏳ in progress, a pending step set in line by figure spaces; finished steps at
the top fold into one line, at most six lines), and leaves the call out of the
tool lines. Like the tool lines, each step is drawn in code (a Discord embed
field, a Slack context block, a Teams monospace block), so the icons of both
look the same. Off, nothing is added and the call is an
ordinary tool line.

Stop also opens its SSE subscription before sending `user.interrupt`, so an
immediate idle acknowledgement is observed. The interrupt and acknowledgement
wait are bounded by the same 120-second timeout; the subscription closes when
the wait finishes or fails.

After a tool-using Discord or Slack turn, the adapter starts a detached,
per-MA-session-chained sweep of downloadable session files through
`daimon.core.output_delivery`. It delivers each file before deleting its MA
listing entry. Failed deliveries stay listed for a later sweep. The sweep runs
after the turn has released the thread, so in a shared thread another person's
turn may have started by the time a file lands; every post the sweep makes
therefore names the person whose turn made the file. Slack posts each file
into the conversation thread; the sweep's first post carries a "For @name"
mention, and the rest do not repeat it, since a Slack mention always pings.
Files are labelled, not reordered: they can still land after a later turn's
answer. Discord attaches each file to the turn's answer: the message carrying
the summary line and the 👍 👎 🙋 emoji (the last chunk of a long answer, or the
"Done." card of a tool-only turn). It edits that message once per file through
the turn's post transport, passing the attachments already on it back, and
never reposts the answer when a webhook message cannot be edited. Any edit
error that reached Discord (a timeout, a 5xx, or a 4xx after discord.py's own
retry of a 5xx) may follow an applied edit, so the sweep reads the answer
back: the file counts as delivered only if a new attachment holds its exact
bytes, and the next edit starts from what is there. A file the answer cannot
take (Discord's 10-attachment limit, or a failed edit) is posted on its own,
and a turn that ended without an answer (stopped or failed) gets its files as
separate posts. Such a post, and an oversize notice, is a reply to the
person's own message (or to the answer, when that message is not in the
thread) and says "For @name" without pinging anyone. Discord uses the guild's
upload limit (10 MB, up to 100 MB for a boosted server; 10 MB where there is
no guild) and checks the channel's writers before posting. No limit goes above
200 MB, because each file is read into memory whole. A Discord DM turn gets
the same sweep once its answer is sent, under the 10 MB default, with no
"For @name" and no reply reference, since a DM has one reader; before each
file it checks the DM's recipient policy and the turn's delivery grant
instead of channel writers. A DM turn on a self-hosted backend is not swept.
A file over the limit
(the guild's on Discord, 200 MB on Slack, 20 MB on Teams) is never deleted: it
stays listed and in the agent's workspace, and the thread gets one notice
naming the file, its size and the limit, and offering a smaller version or
parts under the limit; on Discord, and on Slack when it is the sweep's first
post, it starts "For @name", since only that person's session holds the file.
Each adapter remembers which kept files it has announced (bounded, per
process), so later sweeps that list the file again stay quiet; a restart can
announce a kept file once more. Teams only logs an oversize file and keeps
it too, including in a channel without file access, where files under the
limit are still logged and removed. The sweep reads every page of the
listing, so kept files never hide a newer one. A
file Daimon already attached in the thread after the turn's card (the agent
sent it itself), with the same name and bytes, is cleared from the listing
without a second copy. The session listing is shared by every turn and the
sweep takes whatever it holds once it settles, so a file the next turn writes
while this turn's sweep is still settling can land on this turn's answer; it
is still delivered once. A long answer carries the summary on its
last chunk on both platforms.

Reconnection is two loops for two failure modes. The outer loop handles
eventless cycles — the server closes cleanly roughly every ten minutes by
design — and asks MA whether the session is still running before reconnecting,
so silence can never be mistaken for a truncated success. The inner
`AsyncRetrying` block is a bounded two-attempt budget for a genuinely dropped
connection. The outer loop has no attempt cap; the per-turn ceiling is its
only backstop.

Every call must declare a billing posture, from
`packages/core/daimon/core/turn/posture.py`: `Billed` meters each
`span.model_request_end` event through the bound recorder, `BillingExempt`
meters nothing and logs why.

It also declares a tool-confirmation posture: what the driver does when MA
pauses the session on a `requires_action` idle. `RequireApproval` ends the
turn, `AutoApprove` allows every blocked call, and `PolicyApproval` asks a
decider per call and sends each answer on the same stream. Routines and
other headless runs use `PolicyApproval` with the unattended decider
(`unattended_posture`), with tool safety on or off, and so do `daimon run`,
a workspace transfer's checkpoint turn, fleet workers and unattended wakes.
The driver-run ones go through one send boundary
(`turn/driver.unattended_sends`): the session is gated
(`unattended.session_gate`) right before the message and before every
confirmation batch, live or on reconnect, and every send is a single
attempt; a failed gate stops the send. The gate builds the session through
`turn/unattended_capabilities.py` under the entry point's `CapabilityPolicy`
(`ENTRY_POINT_POLICY`; `session_gate`, `gate_session_snapshot` and
`unattended_posture` take an `entry_point`, and one that names none gets no
built-ins and no skills). `require_allowed_capabilities` is a pure check on the
snapshot that refuses a coordinator (`multiagent`) and disallowed skills. One
schema walker checks parsed SDK fields and response extras, or raw provider
objects. Unknown fields are refused in the agent capability
containers, execution identity, resources and repository checkouts, skills,
MCP servers, tools, toolset defaults, per-tool configs and permission policies.
Unknown tool, resource, skill, identity and policy types are refused too.
The refusal names the field path and explains that its capability behavior
has not been reviewed. A new top-level session field is ignored and its name
is logged once per process, so provider bookkeeping additions cannot stop
unattended turns. Known resource, vault, environment and agent fields keep their
capability checks. Memory-store access accepts only `read_only`, `read_write`
or the provider's omitted/null default. Non-capability parts are opaque:
usage and all its sub-objects, stats, budget, outcome evaluations, metadata and
`agent.model` have neither their keys nor their values checked. Capability
fields retain their reviewed value shapes, and unknown capability keys or type
tags refuse the turn. Custom tool `input_schema` contents remain opaque.
SDK private instance attributes are not checked.
`constrain_capabilities` computes the arrays to send (custom tools removed
and built-ins disabled except in isolated `long_job` worker sessions; the admin opt-in is
`Routine.sandbox_tools_enabled`, which an admin sets per routine and which
`capability_policy` honours for the `routine` entry point only, through
`headless_runner.run_turn(sandbox_tools=...)` from the scheduler fire;
an optional `server/tool` allowlist, and every retained
MCP toolset's default and per-tool configs on `always_ask`, trusted
`daimon-mcp` and safety off included, so every MCP call reaches the
unattended decider); `invalidate_cached_tools` clears and commits the thread
session's cached tool hashes and `send_capabilities` then sends the arrays
(`apply_capabilities` runs both; a caller that restores registers its restore
between them), and `restore_capabilities` puts the session's own arrays
back, finishing through repeated cancellation. `capability_posture` makes the
decider refuse what the session no longer carries, including every built-in
call (unless the routine is opted in or this is an isolated long-job worker)
and every custom call. Other unattended runs cannot generate files with sandbox
tools; checkpoint turns cannot create
sandbox archives. Prepared turns map their
origin to an entry point (`unattended_entry_point`: `worker` is a fleet worker,
`routine` a routine, anything else a wake). For `run_prepared_turn` (fleet workers
and unattended wakes), this also covers
dead-session recovery. Platform queue dispatchers explicitly mark timers and
worker-completion wakes unattended. User-initiated handoffs and private-input
continuations retain their attended approval surfaces. Managed Agents runtimes refresh
the snapshot before **every** confirmation batch. Decision-task joins and card
retirement remain inside the driver's abort boundary:
an outside cancellation, including one during Stop cleanup, finishes bounded,
shielded interruption and leaves unattended calls unanswered before propagating.
An injected Managed Agents backend without a session read (or without a
session ref) cannot have its snapshot checked, so `run_prepared_turn` refuses
the unattended turn with `AdmissionDenied(backend_unsupported)` before its
first message. A non-Anthropic runtime refuses every unattended confirmation
with a plain explanation that it cannot refresh the tool safety gate. This rule
applies with safety on or off; the model receives a denial and the turn can
continue or end. Unattended turns never invoke a human provider-action hook.
Native providers without host tool inspection must expose a reviewed tool
configuration and cannot run automatic sandbox, custom tools, ungated MCP
connections or unreviewed provider options unattended;
the send boundary refuses before posting a provider message. In particular,
OpenAI persistent workers that require automatic `bash` are refused.
Conversation-only workers without those tools remain supported. Selfhosted
execution inspects every declared tool at the host boundary, even with tool
safety off, so it can permit allowed reads while refusing sandbox calls.

Approval gate overrides are resolved from the tenant access policy at admission,
then refreshed from storage immediately before each automatic allowance in
`interactive_decider`; unreadable settings require confirmation. The final
pre-send authorization rejects a stale looser gate for attended turns.
Session endpoint bindings identify Daimon's own server, including legacy URL
aliases, for toolset configuration and fixed-gate decisions.
Only `interactive_decider` can relax a third-party write verdict, and only with
the deployment opt-in enabled. Protected confirmation and permission-setter
names never receive an override on any server, even an unrecognized alternate
host or an unrelated server with a colliding name. Direct messages never consume
scope overrides. Unattended decisions keep their existing paths. The shared
`approval_gate` setter authorizes the `SET_APPROVAL_GATE` action and atomically
writes the policy and audit event. Only native `/here` controls invoke this
setter, obtaining fresh platform-admin evidence for each click or submission.
Discord and Teams offer server/channel/thread choices. Slack server/channel
choices apply directly; its thread modal verifies a root message in the invoking
channel. CLI and MCP mutation surfaces are absent.

Tool rules come from the session's
conversation channel (`session_rules_channel`), never its budget channel. The decider comes
from `packages/core/daimon/core/turn/approvals.py`, over the pure read/write
model in `packages/core/daimon/core/tool_safety.py`. With
`DAIMON_TOOL_SAFETY__AGENT_RULES` on as well, each third-party paused call
reads its agent's exact tool rule from the database. The lookup uses the
session's stable agent id, attached server URL and admitted parent channel.
Agent-wide and matching channel rules combine by the stricter outcome. The
operator deny list wins; a matching rule then decides before classification.
A failed lookup asks in chat or refuses unattended. The flag is off by default.
The driver also reads rules for Daimon's own trusted `daimon-mcp` server (see
the paragraph on `call_tool` below), and an agent-wide `deny` on one of its
tools is additionally enforced by
`IdentityMiddleware` (`middleware/tool_rule_backstop.py`): for a chat-vault
credential (`chat_agent_id` set, no `agent_id`) it reads the tenant's `deny`
rows for the tool being invoked, which for a `call_tool` proxied call is the
inner tool, and refuses when one belongs to the caller's agent and names this
deployment's MCP URL. An agent-wide `deny` on `daimon-mcp/*` refuses every call from
that agent's credential, `search_tools` and `call_tool` included. The same two settings
gate it, and a failed read refuses.
With `DAIMON_TOOL_SAFETY__ENABLED` on, `create_session` sends every attached
third-party toolset as `always_ask` (a per-session override, so it holds
however the agent was written); reads then run, a write in a routine is
refused unless the operator allowed it there, and a write in chat waits for
the requester to press Approve on a confirmation card. Read or write comes
from an operator `DAIMON_TOOL_SAFETY__EFFECTS` pin, then, for a server the
operator lists in `DAIMON_TOOL_SAFETY__TRUSTED_ANNOTATIONS`, the tool's MCP
annotations (`destructiveHint: true` or `readOnlyHint: false` is a write;
`readOnlyHint: true` alone is a read), then its name: a plainly-read leading verb (`get_`, `search_`,
`list_` and the like, or the report noun `briefing`) with no write or chaining
word anywhere is a read, and everything else is a write. `ask` and `verify`
read only in a question-and-answer shape (`ask_wiki_question`,
`eap_verify_answer`); a bare `ask`, such as another Daimon's agent_chat `ask`,
which starts a turn in that agent, stays a write. Managed Agents does not
forward third-party annotations, so the decider reads them itself
(`packages/core/daimon/core/mcp_tool_annotations.py`), only for trusted servers
(the list is empty by default, and an unlisted server is never asked): on the
first paused call to such a server in a turn it lists that server's tools once, with the agent-wide
token stored for its URL or with none (never for a caller from another
organisation). A failed or slow read, or a tool that sends no hints, leaves the
name to decide; each annotation-based allow is logged at info. A `denied`
entry still refuses the call, and a misnamed write on an untrusted server or
one without hints needs a pin. The card is a platform
hook: `run_prepared_turn(confirm_write=...)` takes a `ConfirmationHook`
(`packages/core/daimon/core/confirmation.py`), Discord, Slack and Teams each draw the
shared card from `packages/core/daimon/core/posted_controls/confirmation.py`,
with action-specific copy and a Details control with plain labelled inputs.
When agent tool rules are enabled, a classification-based third-party write
card also offers **Always allow**. The adapter captures both rule revisions
before posting, checks the clicker's current admin rights, then claims the
single-use pending card before saving a chat-scoped rule by compare-and-set.
Discord defers the button response before the live check; Slack acknowledges
the Socket Mode envelope before dispatch; Teams returns an immediate invoke
response and performs the check in a tracked task. Slack and Teams bypass the
short group-membership cache for this check; Discord fetches the member from
the guild. The answered-once edit is queued before the rule save, so
retiring an unsent call does not wait for the database. Teams sends later
refusals as ordinary messages in personal chats and targeted messages to the
clicker in group conversations.
A failed compare-and-set leaves the call approved once and tells the clicker
privately. The decider re-reads the rule after approval so a new explicit
`deny` refuses the call before it is sent.
A pause shows one card per blocked call, and the driver sends one
`user.tool_confirmation` event per call after its answer. An adapter that
passes no hook gets `no_confirmation_surface`, which
refuses the write. Plugins can build their own `ConfirmationPrompt` and call
the same hook. Daimon's own `daimon-mcp` tools are not gated here by
classification (they keep their `operation_policy` checks), except `add_skill`: its confirming call, the
one naming a preview's `content_hash`, is sent `always_ask`, so it waits on
the same card in chat and is always refused in a routine, whatever
`unattended_writes` allows. Each bind compares a reused session's tools with
the gated ones a new session would get and updates them in place, so a
session started before tool safety was turned on is gated from its next turn,
and a tools change never writes the agent's own `always_allow` back. An agent
`ask` or `deny` rule on `daimon-mcp/<tool>` (see [Permissions](permissions.md))
adds `call_tool` and each tool such a rule names to the asking set: admission
reads the agent's rule tool names once per turn and again in `reauthorize`
(`agent_tool_rules.daimon_rule_set`; no query unless both settings are on, and
tool names outside `[A-Za-z0-9_.-]{1,128}` are skipped), and they enter the tools hash like
`asks_before_publishing`, so a rule change updates the session on the next
bind. The decider unwraps `call_tool` to the inner tool (`unwrap_daimon_call`)
and looks the rule up under `daimon-mcp/<inner>`, against the agent's attached
URL and the deployment's own; a call with no rule is allowed without a card.
Precedence on a daimon tool: the operator deny of a confirmed write, an agent
`deny`, the fixed gates (`add_skill` with a preview hash, the publish tools),
then an agent `ask`; a rule never loosens a fixed gate. Gemini and OpenAI
sessions, which cannot hold these asks, refuse a turn for an agent with an
`ask` rule or a channel `deny` on a daimon tool (`daimon_rules_need_driver`);
an agent-wide `deny` is the server's and does not block them. A daimon call that
needs no rule lookup (the agent has none) is not looked up at all. An agent-wide `deny` on `daimon-mcp/*` adds `call_tool` to the
asking set and the lookup returns that deny for every daimon call, so the
driver refuses each proxied call too (the server refuses all of them anyway).
The exemption holds only for the deployment's verified
endpoint: with the policy on, `create_session` re-points a `daimon-mcp` entry
naming any other URL at `DAIMON_MCP__PUBLIC_URL`, and without a public URL the
reserved name is gated like any other server. A pending card is owned by the
turn: stopping the turn, a replayed pause, or the turn ceiling cancels the
decision and retires the card, so an Approve that arrives after Stop never
runs anything. Attended cleanup refuses pending calls. An unattended
turn leaves them paused and interrupts instead: even a denial would resume
execution. The driver tracks unattended cleanup independently of its optional
confirmation gate, so Stop, cancellation and ceiling expiry also leave calls
paused on runtimes without a confirmation gate. Normal decisions on those
runtimes (non-Anthropic ones; a Managed Agents runtime without a session read
is refused before it starts) still refuse unattended confirmations. Cancellation during a confirmation
gate cancels that gate before interrupting; a failed gate sends no confirmations. That cleanup is best effort within a few
seconds per step (`CLEANUP_BUDGET_S`): a chat platform or MA that stops
answering cannot hold a turn past its ceiling or a Stop.

A queued handoff inherits attendance from its trusted execution origin. Timer
and worker-completion turns and their model-created handoffs stay unattended;
a real human message or private-input action supplies attended provenance.
Queue reasons alone cannot upgrade a turn to human approval surfaces.

OpenAI captures MCP bearers at session creation, so its preparation host
installs a scope-bound session grant before allocation and records it in the
binding. The grant participates in the creation digest, refusing legacy
ungranted bindings or receipts. The prepared driver binds it to each current
origin, narrows attendance from dispatch provenance, and clears it on exit.
Child preparation installs its own isolated grant, independently of inherited
rendering or execution contexts. Source token expiry and revocation still apply.

Agent-chat session creation records headless provenance. Continue, ask with a
handle, cancel and archive require it before waiting on the mutation fence
and again inside it. An unauthorized nested caller cannot wait on the human
driver it is interrupting.
A platform session cannot lend its credential or attended origin grant to a
headless caller. Legacy sessions without this provenance refuse mutation.

Agent-chat turns (`start_turn`, `continue_turn` and `ask`, per agent and on
the hub) have nobody to press Approve, so they take the unattended rules
through one core gate, `packages/core/daimon/core/turn/unattended.py`. Before
every send, `gate_session_snapshot` rewrites the session's own frozen tool
config from the current policy (every gated toolset's default and per-tool
configs to `always_ask`, Daimon rule tools, a healed `daimon-mcp` URL) and
fails the send if the update fails; it never restores attachments the
session hides. It also makes Daimon's card-only actions (`protected_tools`,
the same selection as chat: the backend switch and Microsoft sends with the
policy on or off, Microsoft writes with it on) ask, and heals a foreign
`daimon-mcp` URL, with the policy on or off. After the send, the turn's
boundary and tool-rule channel are recorded on the session's settle row
(`session_settle_state.record_unattended_turn`), and the sending process runs
one bounded worker for that turn (`_agent_chat_safety.watch_turn`), which
answers only pauses after its own `user.message` boundary, each decided at
call time by `unattended_posture`: `unattended_decider` with the agent's
rules for that channel (`channel_for_tool_rules`, which retains routine
channels despite their private transcript stamp and excludes genuine DM
source channels), the protected tools
refused, never `AutoApprove` and never the session's frozen config.
Each confirmation batch resumes the turn, so the worker gates the session
again before deciding it, and a failed gate sends nothing. Messages,
confirmations and gate updates go out with one attempt (`single_attempt`,
no SDK retry), so nothing is resent after its gate and decision; a failed or
ambiguous attempt raises and is left for a new, fully checked call. The
registry keeps each worker's turn boundary: a worker reads as `running`, and
a resume reuses it, only for the turn it owns. The worker reads operator-trusted
MCP hints (`DAIMON_TOOL_SAFETY__TRUSTED_ANNOTATIONS`) through `annotations_for`,
bound once from the session's own agent and credential. A read never answers a pause: `get_session`
(both), `get_my_session` and hub `get_session` report `running` while this
process's worker owns the turn and `paused` with a hint otherwise; list tools
report the provider's status. Only the session's own account (an agent key,
or the hub in `continue` mode) can resume a pause, and only on the turn the
record names, so a chat card is never adopted: `continue_turn` re-gates the
session, starts a worker on the paused turn with the recorded channel, which
decides from the policy now in force, and, given a message, sends it once
that turn ends, after repeating every check and the gate.

With both `DAIMON_TURN__PATH=mux` and `DAIMON_TURN__CHANNEL_BACKENDS=true` (the defaults),
an active chat admin can request an agent backend change through that same
confirmation hook. The card shows the server's current compatibility and cost
preview. Confirm rechecks the requester and current admin permission, then
applies the bound revision and preview digest by compare-and-set. Native
sessions without verified approval remain refused. Invalid prompt inputs are
denied inside the existing card boundary; sibling decisions continue.

### How a turn ended

`packages/core/daimon/core/turn/termination.py` defines `TerminationReason`,
one closed enum for every way a turn can end: it completed, the user stopped
it, the stream or MA failed in one of several named ways, the ceiling fired, or
admission or binding refused it before a driver ran. Each driver finalizer, and
both ceiling handlers, set `TurnState.termination` before the terminal hook
fires, so a lifecycle and the caller's `RunOutcome.termination` always agree.
Refusals raise before any state exists; `termination_reason(err)` maps the
exception the adapter caught to its member, and never raises: anything it does
not recognise is `unknown`. Each `AdmissionDenied` reason with a member of its
own (balance, cap, channel budget, writers none, agent rule, own agents only) maps to it, and
`denial_termination_reason` gives the same member to a gate that decides with
`authorize` instead of raising; the rest are `admission_denied`. Two members have no exception behind them and are
set outside the mapper: `admission_concurrency_shed` by callers when the
turn queue is full or a queued turn passes its max wait, and `recovery_failed` by `run_prepared_turn` on
the terminal hook when replacing a lost session raises (the exception it
re-raises maps to `unknown`). A session MA reports terminated without any terminal
event for this turn is `session_terminated`, never `completed`. A turn the model
refuses ends with an ordinary `end_turn` idle whose `stop_details` says
`refusal`; the reducer reads it and the turn is `refused`, never an empty
`completed`.
`TurnError.kind` is unchanged, and
every `TurnKind` value is also a `TerminationReason` value with the same
string.

`packages/core/daimon/core/turn/notices.py` turns a reason into a
`TerminationNotice`: a short headline, the cause, the tool work still running
and how much had finished, what survived, the next step, and a request id. Discord
and Slack pass the request id only when the person whose turn it is holds the
admin role there, so everyone else sees no `Ref` line. The copy lives in
core; Discord and Slack draw it in `on_terminal_failure` as the
body of the red card, with the headline as the footer reason, and log the
request id with the underlying error so it is the handle for the detail;
Teams draws it as plain text on the ❌ card. No
lifecycle hook carries it -- the reason rides on the state every lifecycle
already receives -- so the CLI, headless routines and any new adapter keep
their existing failure path, and `TerminationNotice.plain_text()` is the
fallback wording for a surface without markup.
Anthropic's monthly spend-cap response stops SDK retries at the HTTP transport.
If Anthropic reports that cap or a user-set spend limit, Discord, Slack and Teams
show a model usage limit notice and log `anthropic.spend_limit_reached` with
the tenant and limit type.

### Outside text is data

Anything Daimon quotes into a turn from someone other than the person asking
goes through one envelope, `packages/core/daimon/core/untrusted.py`: an
element marked `trust="untrusted"`, opened by a fixed line saying the content
is data, not instructions, with every value escaped so the content cannot
close the element early. The Discord, Slack and Teams context builders wrap
replayed thread history, deltas and channel backfill in it; `fetch_youtube_transcript`
returns its transcript in it; the quoted transcript on a workspace
replacement (`render_previous_session`) uses it too. The channel read and
search tools return JSON rows, so their results carry the same marker as
`trust` and `trust_note` fields instead. The paragraph in the agent guidance
block (`packages/core/daimon/core/agent_guidance.py`) tells every agent what
the marker means. Only the `<user_query>` is the request.

Discord turns say which message a reply points at. Each replayed message carries
its `id`, plus `reply_to` when it is a reply, and the `<user_query>` carries
`reply_to` when its single trigger is one. Its `message_id` and `channel_id`
identify the human's request message and the channel or thread where they posted
it, even when Daimon opens a different response thread. `reply_to` identifies the
message they replied to, not the request. A merged query includes `query_sources`
inside `user_query`, with each original message's `message_id`, `channel_id`,
text and optional `reply_to`; identical text still has distinct source entries.
When the target is not among the replayed messages (before the delta's watermark,
a delivered answer chunk the delta leaves out, or an older channel post), an
untrusted `reply_targets` block holds it, cut at 2,000 characters. A merged queued
query lists one entry per reply, each with the replying message's id, author and a
short excerpt; a target shown elsewhere or unavailable gets an id-only entry
(`shown` or `unavailable`).
A message with no text shows its embed's title and description. A deleted target is
marked `deleted="true"`, and an unavailable one leaves only the id. The turn's
own status card, posted before the context is built, is kept out of the history.

For a new Discord thread started by a channel mention, the channel backfill
keeps 25 messages. A separate untrusted `recent_channel_files` block lists up
to 10 attachments older than that window, from at most 200 channel messages
within 24 hours of the mention. It includes live attachment URLs, filenames,
uploaders, upload times and message links, but no older message text. Both
reads use the same parent channel; a failed older read leaves the 25-message
context intact.

Third-party MCP tool results travel from Managed Agents straight to the model
without passing through Daimon, so they carry no marker; the guidance
paragraph covers them by name ("whatever a tool returns").

## Trust model

[Permissions](permissions.md) has the rules as one model; this section has
who is exempt and why.

Admins are trusted; rules protect members and channels. An agent rule and a
channel's readers rule exist to keep one client's context away from other
people -- members of other channels, and anyone reading where an agent posts
-- not to restrict a workspace admin. So an admin is exempt only where the
output reaches no one but them:

| Surface | Members | Admins |
| --- | --- | --- |
| Channel, thread, handoff, routine that posts to a channel | rules apply | rules apply |
| DM (`admit(is_dm=True)`, Teams personal chats included) | agent with a rule refused | agent rule exempt |
| Hub `ask` / `start_turn` / `continue_turn` | agent with a rule refused | agent rule exempt |
| Hub `continue_turn` / `ask(handle)` on such a channel conversation | refused | refused: continue it in its channel |
| `fork_agent` of an agent with a rule | refused | refused |
| Agent chat and any agent-scoped key or bearer token with no platform user | rules apply | rules apply |
| An agent key minted in a channel with limited readers, or one its agent's rule names | runs inside that channel only | runs inside that channel only |

Channel and agent rules, the invoker allowlist and fork are decided by one
pure function, `daimon.core.authz.authorize` (who is acting, what they want to
do, where the result lands, which agent, which channel); the turn pipeline,
the MCP gates, the channel tools, the routine and handoff tools and the fork
paths gather their facts and ask it, including the hub's admin and channel
admin read of a limited conversation and the refusal to continue one. The
scheduler's writers and invoker checks and the shared-agent replace/remove
table are still decided where they are.

**Decided again at the moment of action.** Admission is a decision about a
turn that has not run yet, so it is asked again, on the policy as it is
then, where it matters:

A demoted admin keeps their stored role until their next platform turn
refreshes it; that is accepted.

On every turn, wherever an agent with a rule runs (including an exempt admin
turn and a member's turn inside its channel), its sends (messages, replies,
threads and posts, files and cards on Discord, Slack and Teams) reach only:
its rule's channels and threads under them; the requester's own 1:1 DM with
Daimon (a Slack IM whose user is the requester, or a Teams personal chat the
requester is in); and direct messages to the requester. Its context never
lands in another channel or another person's DM. A channel's own agent is
stricter: it posts only into its channel and the threads under it, never the
requester's DM, and sends no direct messages.

`readers: own` constrains agents, not callers. Inside such a channel C only
C's own agents run, post and read; a caller with no executing agent, such as
an operator token or the CLI, may still post into C. That is input, not a
leak: the readers rule keeps C's messages readable only from inside it. A
session that ran in a DM (a Slack IM, a Teams personal chat, or a `/dm-me`
conversation) is private: admins never read it from the hub.

Continuing a limited channel conversation from the hub stays refused for
admins because a follow-up would join the channel's own conversation, which
the channel goes on reusing. Branching a private copy for the admin is a
possible follow-up.

Who counts as an admin:

An operator token (below) is an admin only while its account's stored role is,
checked on every request. Like the hub, it sees a demotion only once the
person's next platform turn records it, so revoke the token to stop it at once.
It always carries a platform user, so it is billed and rule-checked like that
admin's own chat turn, never trusted as the operator.

Agent-scoped keys, chat-turn credentials in the hub and tokens with no platform
user are never admins on these surfaces, whatever role their account holds or
who minted them. An agent rule binds a bearer with no platform user too: such
callers skip billing, not admission.

## Tenancy and isolation

One Discord guild, Slack workspace or Teams (Entra) organisation is one tenant. The tenant UUID is
derived, not allocated: `derive_tenant_uuid(platform, workspace_id)` in
`packages/core/daimon/core/ma_identity.py` is a UUID5 under a frozen
namespace, so the same workspace maps to the same tenant across database
resets and processes.

Isolation is enforced in two places at once.

**In Postgres.** Tenant-scoped tables carry `tenant_id` with a cascading FK to
`tenants.id`, and stores take `tenant_id` as a parameter rather than reading
it from anywhere ambient. Erasure is not left to cascades:
`packages/core/daimon/core/purge.py` deletes every row referencing a principal
in FK-safe order in one transaction (a Teams message receipt is cleared of the
person but kept for 24 hours, as the duplicate guard), and
`packages/core/daimon/core/privacy.py` is its read-only mirror for the preview
panel. A schema-reflecting drift-guard test fails when a new person-scoped
table joins one path and not the other.
Account erasure also queues its account and tenant IDs in the same transaction
before removing principals. The scheduler retries Managed Agents session deletion
on each tick, retaining the queue row through upstream errors and failed deletes
until all enumerated sessions are deleted or confirmed gone.

**In Managed Agents.** One deployment runs on one Anthropic key, so tenant
separation inside the MA workspace is carried by metadata stamps defined in
`packages/core/daimon/core/defaults/metadata.py` — `daimon_tenant`,
`daimon_account`, `daimon_name`, `daimon_managed`, `daimon_spec_hash`. The
resolver checks the `daimon_tenant` stamp on every cached-id retrieve, so a
stale id belonging to another tenant is rejected rather than used. Credentials
never enter a prompt: they are mounted into the session as an encrypted env
file (`packages/core/daimon/core/credential_env.py`) or brokered per call
(`packages/core/daimon/core/broker/`), and the MCP server the sandbox calls
back into authenticates a JWT whose `agent_id` claim is the derived agent
UUID from `packages/core/daimon/core/ma_identity.py`.

Because that env file is `source`d in the sandbox, `credential_env.py`
serializes every value so bash cannot expand or execute it, and
`packages/core/daimon/core/env_file.py` decides which key *names* may be
stored. Names that control an interpreter, archiver, loader, locale, package
manager, git, an HTTP client or a CA bundle, or that redirect an SDK's own
endpoint (`TAR_OPTIONS`, `BASH_ENV`, `LD_PRELOAD`, `GIT_SSH_COMMAND`,
`*_BASE_URL`, …) are hard-denied for everyone (the four git commit-identity
names `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
`GIT_COMMITTER_EMAIL` excepted) and dropped from the mount even
if stored earlier; a non-admin member may additionally add only a secret
name (ending in `_KEY`, `_KEY_ID`, `_TOKEN`, `_SECRET`, `_PASSWORD`,
`_PASSPHRASE` or `_PAT` — never an identity, region or `*_URL`/`*_HOST`
name, which only an admin may add). This keeps one tenant member from handing another client's
agent code execution or a redirected connector through a key value or name.

## Sessions and Managed Agents

Discord credential-card expiry is separately opt-in through
`DiscordSettings.credential_form_expiry` (default false) and the existing
single posting-owner switch. `core/credential_form_expiry.py` exposes
`poll_credential_form_expiries_once(sessionmaker, now=..., edit=...)` for
production and clocked offline execution. The store atomically retires a
bounded batch with `used_at IS NULL`, recording `expired_card_pending` in
the existing outcome column; a consume or supersede that won first is left
alone. That transaction commits before Discord HTTP. The result-returning
`edit_expired_posted_card` verifies the persisted tenant/thread/message and
forbids webhook replacement; an acknowledged edit CAS changes the outcome
to `expired_card_updated`. Failures remain pending and a bounded cursor
allows later cards to progress before retries. These outcomes never mean
credential application. The click helper, default TTL, token-consumption
predicates and other platform paths are unchanged; no migration is needed.

A turn does not create a session per message. `thread_sessions` maps
`(tenant, platform, thread)` to one MA session id, and its `status` column
carries the lifecycle: `live` is the caller's current session, `dead` is one MA
no longer has, `superseded` points at the successor that carries its work
through `replaced_by_id`, and `retired` is an explicit fresh start. That
lineage is why a thread can survive a session replacement with its context
intact.

Each caller has their own row per thread. A channel configured for shared
threads (`thread_mode="shared"`, with `DAIMON_TURN__CHANNEL_BACKENDS` on)
instead keys the row on an owner derived from the thread, so everyone in it
uses one session with the agent's own credentials, and records the binding
in the state store (`packages/core/daimon/core/shared_threads.py`). That
session mounts no caller's vault; it mounts a vault of the thread's own,
`daimon-shared:<owner>:<agent>`, holding only the agent-wide MCP tokens.
Each turn in the thread by a member of the organisation brings that vault
up to date, so a token added, rotated or removed reaches the running session
on that turn.

What MA holds is the agent, the environment, the skills and the session
transcript. What Postgres holds is metadata: identity, the mapping above, the
config cascade, credentials and billing. An MA session freezes its agent spec
at creation time, so the agent read at admission is not necessarily what
executes — the mapping row stores a `SessionSnapshot` of the configuration the
session is actually running (`packages/core/daimon/core/session_snapshot.py`),
and that snapshot, not the current spec, is what later turns compare and bill
against.

A conversation's model choice (`/model` on Discord, or the agent's
`set_thread_model` tool when someone asks in words) is a session override,
never an agent edit. Admission puts it in the desired snapshot, so the next
turn's compatibility check finds a `model` change and replaces the session,
carrying the work over as for any model edit. The successor is created with
`agent_with_overrides.model`. A Managed Agents override names a model and
speed but no effort, so no effort is offered. The tool runs only for a
verified, attended turn origin: routines, wakes and agent-chat callers can't
change a conversation's model. `/model` runs the same `START_TURN` check that
admission makes. Fleet roots and workers ignore the choice (see Worker fleets
below), and `/model` says so when the thread's agent holds a fleet grant.

### Worker fleets

A fleet run is an orchestrator turn's durable fan-out
(`packages/core/daimon/core/fleet.py`, rows in `fleet_run` and `fleet_worker`).
Each worker gets a private binding slot of its own in the state store, under the
orchestrator's channel and account. Its session, fenced lease, idempotent spawn
operation, journal and usage are the store's existing ones. A worker's result is
one entry in its own journal; the worker row keeps only a pointer to it, with its
digest and a short excerpt. Every status change is a compare-and-swap on the
row's version. Worker turns have a concurrency pool of their own, counted from
these rows per tenant (`DAIMON_FLEET__MAX_CONCURRENT_WORKERS_PER_TENANT`) and
per process, so a fan-out never takes the chat turns' slots. Fleets are on by
default; `DAIMON_FLEET__ENABLED=false` is an emergency stop that refuses new
workers before they are funded.

A conversation's model choice (`/model`) never reaches a fleet. When the
answering agent holds a fleet grant, `bind_session_impl` puts the authored agent
back (`Admission.authored`) before the root session is prepared. A worker's
bind does the same, and its session is never created with an override. A
frozen root therefore records the model the agent's owner approved, and the
override is not among its retained admission facts.

### Same-thread handoff

A thread can move to another agent without a new thread. Two triggers:

- Ask the agent in the thread ("hand this to research-bot"). It calls
  `hand_off_task` with the destination's id. This works on Discord, Slack and
  Teams.
- When a channel's agent changes, a thread whose session belongs to the old
  agent can't run a turn, and its next mention gets a notice instead. On
  Discord, Slack and Teams the notice has a Switch to agent button that moves the
  thread to the channel's agent and re-sends the clicker's latest message to it.

Asking the agent is the default because it is how every other thread change
works (keys, repo, fresh start). The button exists because no agent can answer
in that stuck thread, so there is nobody to ask.

Both go through `daimon.core.thread_handoff.hand_over_thread`, which decides
`authorize(HAND_OFF)` and writes the thread binding in one transaction. It
takes the tenant policy lock (`lock_access_policy`), then the requester's
account row FOR KEY SHARE, then the binding row FOR UPDATE, and reads the
policy after the first lock. A policy edit committed before the switch refuses
it; an edit that arrives later waits for the commit. The module docstring
gives the order against every other lock holder. A Hand over click by an admin,
or one `authorize` refuses, records a `panel:handoff` audit row, as
`hand_off_task`'s refusals are audited.

The button's switch writes only the binding. On a successful click, the adapter
re-sends the clicker's latest readable message before the notice through its
normal mention ingress, sanitized and bounded, or uses a fixed prompt when
history is unavailable. The synthetic input belongs to the clicker, is addressed
to the new agent, and bypasses command/participation rerouting. A busy thread
queues it with the usual hourglass. It stays a separate request when a later
mention arrives, including another mention from the same person. The normal
admission, billing, interactive approvals, drain and cleanup apply; no button
continuation or separate ownership mechanism is created. A refused or unchanged
switch sends no input. Provider selection also remains with normal admission;
the button does not implement a provider switch or release a retained slot.

For a handoff that keeps its provider, the bind on that first turn replaces the
old session with one for the new agent: the old session's
transcript and files are carried across by the workspace transfer, the
successor is stamped with the old session's recorded ids, memory is read-only
when the thread's readers are limited (or a DM with `dm_memory_read_only`), and
the old row is
marked `superseded`. The work is carried only when the new agent could read the
old session itself from this thread (`authorize(READ_SESSION)`); otherwise the
new session starts with nothing. A target its rules refuse here is refused by
admission before any of this runs.

## Entry points that are not a chat message

- **`daimon context-provider set`**, in
  `packages/adapters/cli/daimon/adapters/cli/commands/context_provider.py`, binds
  a pre-turn MCP provider as the deployment operator. It does not run a turn.

- **Agent setup picture controls** show the current agent picture in Details.
  Admins get **Use default**, which restores the generated face. Custom
  uploads are turned off: a Change button, Slack upload form or Discord
  `/agent-setup` picture option from before answers with the refusal and
  writes nothing. Details keeps
  visibility and cache guidance off the main row.

- **`/here`** in Discord and Slack, and `here` in Teams (answered privately in
  the thread, or in the 1:1 chat), reads routing and access policy, then shows a private card built from
  `daimon.core.here_card`: who answers here,
  reading scope, and whether publishing needs approval. Discord uses an embed;
  Slack uses Block Kit with a state colour and notes that threads can differ;
  Teams uses an Adaptive Card.
  The card omits credential names, rule provenance, memory and session details.
  `where_am_i` returns the same short text plus the complete structured facts.
  The model does not compose either summary. `/agent-setup` and
  `explain_agent_resolution` provide routing detail. The facts use
  `daimon.core.permissions` and `authorize`. The MCP channel-turn view uses
  member visibility even for an admin, because the model's answer may be
  posted to the channel. Slack slash commands have no thread identifier, so
  Slack `/here` reports channel routing only.
  Discord read and scoped search tools explain when the bot lacks View Channel
  or Read Message History permission, including on an otherwise empty read.

- **Scheduled routines** go through
  `packages/core/daimon/core/headless_runner.py`, which creates a session with
  the same `create_session` the chat path uses and delegates the drain to the
  same driver under the same ceiling — but it calls neither `admit()` nor
  `bind_session()`. The scheduler runs the balance, cap and channel budget
  gates itself before each fire. A routine with a destination is told where its result
  goes; owner-DM results always use the outbox. For a channel/thread destination,
  the outbox is used when the agent does not post there itself. It goes `pending` and
  the chat adapter for the tenant's platform posts the result tail through
  its delivery poller (`daimon.core.routine_delivery`), after the access
  policy's writers and invoker checks. See [routines.md](routines.md).
- **MCP agent-chat tools**, in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py`, let a caller
  drive a session directly. They do not use the chokepoint either; they re-run
  the same balance and cap gates through `_admit` in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py` and create
  sessions via `daimon.core.sessions.create_session`. A run of a channel's
  own agent, a hub run by an admin or that channel's admin included, is
  gated by and charged to that channel's budget. The billed media tool
  runs the same gates, then the budget of the channel named by the calling
  turn's `origin_context_id`, and charges its spend to that channel.
- **`daimon run`**, in
  `packages/adapters/cli/daimon/adapters/cli/run/command.py`, is a single-turn
  subprocess entry point that calls `run_turn` directly with `BillingExempt`.
  Nobody answers a card there, so it is unattended: before the message it
  gates the operator's session (`gate_session_snapshot`, whatever the session
  was created with) and answers every pause with `unattended_posture`.
- **Wakes** run a turn in an existing thread later, with nobody mentioning the
  bot: a handoff's first turn for the new agent, work unblocked by a private
  form, a one-shot timer (`daimon.core.continuity.timers`, the `create_timer`
  tool), and anything else queued through `daimon.core.continuity.wakes`. A wake is
  a `task_continuations` row. The Discord, Slack and Teams adapters each run
  a wake poller (`run_wake_poller`) that opens threads with due rows and hands them
  to the adapter's continuation dispatch. That dispatch takes the thread's
  turn guard and goes through `admit()`, `bind_session()` and
  `run_prepared_turn()` like a mention, so the balance, cap and channel
  budget gates apply.
  A claim holds a lease, and `started_at` is committed just before the turn
  starts. If a claim's lease expires before `started_at` is set, the wake is
  retried. If it expires after, the wake is settled `interrupted` and never
  run again (`formal/continuation/WakeLease.tla`). Waiting on a busy thread
  refunds the claim, so it never counts against the crash budget
  (`WAKE_MAX_ATTEMPTS`). A thread the adapter cannot open (no token,
  archived workspace, platform error) is pushed back five minutes, so it
  cannot hold up other threads. An adapter that starts no poller leaves its
  wakes pending. The scheduler process has no platform client, so it never
  runs wakes.

  **Rollout order.** Migration `0028_feat003_wake_queue` first, then every
  Discord and Slack adapter process, and only then anything that enqueues
  wakes (timers). The pre-queue claim and list calls in the store skip rows
  whose `available_at` is in the future. But an adapter binary built before
  this change dispatches without leases or fences, and never polls, so its
  wakes would only run at a turn tail. Downgrading the migration settles
  every scheduled wake that has not run as `skipped/downgraded`, so none of
  them runs early.

  Timers add a reason (`timer`) that older code rejects when it reads a row.
  A FEAT-003-only adapter or MCP process fails to load a batch containing a
  timer row. So roll timers out in this order: migration `0029_feat084_timers`,
  then every Discord, Slack and MCP process on a timer-aware build. Only then
  may `create_timer` be called, and it is only exposed by that MCP build.
  Downgrading `0029_feat084_timers` deletes every timer row, fired or not.
  A wake (timer, handoff, applied private input) only runs as the agent it
  was queued for; applied private input may also resume the agent of the
  requester's live session in the thread. If the thread answers to
  another agent when the wake fires, the adapter refuses it after
  `admit()` and before anything is bound or billed. It settles the row
  `skipped/skip_target_changed` and posts a notice in the thread.

If you add another, reuse `admit()` rather than re-deriving the gate order.

## Standalone apps

`apps/notebook-host/` serves published marimo notebooks, spawning one
marimo subprocess per notebook behind a reverse proxy. Each subprocess has
its own access token, and scratch notebooks are read-only unless the
publisher asks for the editor and the operator allows it
(`notebook.allow_editable` on the bot and `allow_editable` on the host). With
`DAIMON_NOTEBOOK__ORIGIN_BASE` set, each notebook is served from its own origin
(`<label>.<origin_base>`); the proxy routes by Host and refuses cross-origin
requests and WebSockets. Without it, notebooks share one origin, so a public
host admits uploads only from the tenants the operator lists in
`DAIMON_NOTEBOOK__TENANTS`, matched against the tenant named in the bot's
signed upload token.
`apps/report-host/` serves one published PDF report with a chat sidebar.
A browser (`Accept: text/html`) opening an unavailable document link gets a
static “Link unavailable” HTML page instead of a JSON error: unresolved
`GET /n/{slug}/` requests (404) and refused `GET /r/{slug}` requests (403).
Other clients keep the JSON error. Token and expiry checks, successful pages, and API/file/admin
responses retain their existing behavior.
Both are FastAPI processes that hold no Anthropic key and no database
credential; they reach Daimon over HTTP with capability tokens, and the
`must not import daimon` contracts keep it that way.

`apps/harness/` is a standalone FastAPI agent runtime. It uses the OpenAI Agents SDK
against Azure OpenAI and imports only mux wire contracts from the Daimon
workspace. Authenticated HTTP endpoints submit turns, replay durable session
events, resolve tool approvals and cancel work. Each sandbox owns its workspace
and SQLite history under its persistent home directory. It needs Azure model
credentials or managed identity, with no Anthropic dependency.

## Where to look next

- [routines.md](routines.md) — the scheduler and headless turns.
- [billing.md](billing.md) — metering, the gates, the ledger.
- [defaults.md](defaults.md) — what is seeded and how reconciliation works.
- [mcp-tools.md](mcp-tools.md) — every tool the agent can call.
- [configuration.md](configuration.md) — every setting.

The operator CLI also exposes `daimon tenants funding-mode PLATFORM EXTERNAL_ID
MODE`. This stores a per-tenant `prepaid` or `operator_funded` policy. Shared
balance admission emits a warning instead of a refusal for operator-funded
tenants; usage recording and configured caps continue through the same path.

`daimon promo create|list|revoke|redemptions` manages deployment-wide promo
codes. Admins redeem server-wide codes, and any member a channel code
(`--channel-cap`) into the channel they are in, from `/billing` on Discord and
Slack (a Redeem code button, shown only while a code the viewer may redeem is
redeemable, and a modal), the Teams `billing` card (a Redeem code action
opening a code field, shown the same way) or, server-wide codes only, with the
MCP tool `redeem_promo_code`; each surface calls
`daimon.core.promo_credit.redeem_promo_code`. Scheduler housekeeping settles
timed credit windows through `daimon.core.promo_settlement`. See
[billing.md](billing.md#promo-codes).

Each adapter remembers the names it is handed on the way in: the author of a
Discord mention and the user of any Discord interaction (`DaimonBot.on_interaction`),
the name a Slack event, slash command or click carries (right after the ack in
`SlackApp.on_request`), and the sender and channel of every Teams message and
card click. `daimon.core.platform_names` queues them in a bounded in-process
map, latest name per person or channel, and one background task per process
writes them to `platform_user_names` and `platform_channel_names` in batches of
50, one session each. A name this process already wrote is skipped, the
recording adds no wait to a turn, and a failed batch is only logged; a person's
name is only stored while they have a principal in that tenant.
The billing panel names people and Teams channels from them when the platform
does not answer live. See [billing.md](billing.md#what-you-can-see).

### Invocation context fragments

Core adds a `turn_context` block before the user message, chosen by the trusted
caller origin (`chat`, `routine`, `relay`, or `handoff`). Default chat adds nothing.
Scheduler runs use routine framing; Discord and Slack handoff continuations use
handoff framing. Callers drafting a relay pass `origin="relay"` to the core runner.
The block is included again on dead-session recovery.

Agent YAML accepts `context_fragments`, keyed by origin, with `text` and an optional
`mode` (`replace`, the default, or `extend`). For example:

```yaml
context_fragments:
  routine:
    mode: extend
    text: "Include the source timestamps in the result."
  relay:
    text: "Write a concise client-ready answer in Spanish."
```

The spec converter stores this configuration in the agent's system field so it
survives upload, forks and defaults fingerprints. Replacing that system field
without the configuration removes the overrides. An empty replacement disables a
fragment. These blocks affect prompting only and grant no extra permissions.

Operator recovery tools: `daimon backup platform-export` exports the dedicated
MA workspace through core; `scripts/backup/postgres.sh` backs up/restores Postgres.
See [self-hosting](self-hosting.md#backup-and-disaster-recovery) for the recovery
contract and limits.

### Platform words

`daimon.core.platform_words` is one table of what each chat platform calls
things and how a person reaches each command there: Discord's server, server
admin and `/agent-setup`, Slack's workspace and `/agent-setup`, Teams'
organisation, admin and `setup` in a 1:1 chat, and where each command works
(Discord commands other than `/privacy` and the DM-only `/new` and `/dm-settings` are server-only;
Teams `setup` and `support` work only in the 1:1 chat). Help cards, refusals,
the here card and routine failure notices read it, and MCP tool results that name who must act
use the caller's platform (`tools/_words.py`). The model gets the same words per
turn as `words` in `<turn_controls>` (chat turns, from the origin's platform and
place: DM, channel or group chat) and in routine controls. The system prompt,
skills and tool descriptions stay platform-neutral and point at `words`, so
changing a platform's wording moves no live session to a new workspace.

### Google tokens in ordinary chat

Chat sessions attach an isolated identity vault whose signed JWT carries
`chat_agent_id`, derived from the tenant and Managed Agent ID, and a unique
`chat_session_id` grant. Personal OAuth credentials stay in a separate
per-account, per-agent vault. MCP resolves this
as the executing agent identity for `get_cli_token(service="gcloud")`, while
preserving the ordinary chat tool surface and live account-role checks. The
separate `agent_id` claim still selects the restricted external agent-chat surface.
Neither claim is supplied through tool arguments. Chat identity is stored separately
from `AuthIdentity.agent_id`; origin tools additionally require the matching
session grant. GitHub chat calls use the agent’s App grants; other identity gates
and the two-tool search interface remain unchanged.

The operator binds the agent with `daimon agents bind-google <agent> <email>
--scopes <scope>...` and configures one of two routes. When
`credentials.google_user_email` matches the bound email (without case
sensitivity) and `credentials.google_user_json` holds that account's
authorized-user JSON, the broker refreshes that sign-in for the binding's
scopes; those must be scopes the sign-in was granted, or Google refuses with
`invalid_scope`. A binding with no scopes is refused before the refresh. A
failed refresh names only Google's OAuth error code (`invalid_scope`,
`invalid_grant`, or "network or unknown") and never falls back to the service
account. Every other binding uses `credentials.google_sa_json` with domain-wide
delegation, impersonating only the bound Workspace user and scopes. An unbound
agent receives a clear operator-binding error.
Core does not ship curated Workspace tools. Agents may use the token themselves
or a deployment-provided Google MCP server.

Existing static-bearer vault credentials are upgraded in place on the next
session creation. Credential metadata records the identity version so subsequent
creates leave the token stable; unrelated and OAuth credentials are preserved.

### Microsoft 365 per person

Teams people connect their own Microsoft account (Entra v2 authorization
code with PKCE, confidential client). `core/microsoft_identity.py` builds the
link, redeems and refreshes tokens, and checks the id token's tenant and
object ID against the Teams caller. `microsoft_sign_ins` holds single-use,
requester-bound state; `microsoft_user_tokens` holds one Fernet-encrypted
refresh token per (tenant, user), rotated on each refresh and deleted on
`invalid_grant` or `interaction_required`. Purge and the privacy preview
cover it. The MCP routes are `/oauth/microsoft/start` and `/callback`; the
`microsoft_*` tools in `tools/microsoft/` call Graph v1.0 through a path
allowlist, keep access tokens in memory, and decide 1:1-only scopes (mail,
calendar, OneNote) from the verified turn origin. `microsoft_send` is a
confirmed daimon tool, so it always goes through the approval card.

### Completion signals

The core driver calls an optional `on_acknowledgment` lifecycle hook with
`accepted` after the initial event send and `done` after successful answer
delivery. Missing hooks are no-ops; reaction failures are bounded and do not
fail the turn. Discord and Slack acknowledge admitted mentions with eyes; queued Discord
follow-ups and mentions waiting for a turn slot use an hourglass. These markers clear on every terminal path.
Opted-in tenants also get a check mark on success. Unprompted Discord turns stay silent; failures and cancellation
do not get a completion marker. Continuations without a trigger message skip
reactions.

A cancelled prompted Discord or Slack turn keeps any partial answer and appends
"Stopped. Send a message to start again."; a turn cancelled after only tool calls shows that notice in its
status card. A cancelled turn sends no completion ping and no feedback controls.
An unprompted Discord turn cancelled before it has an answer stays silent.

Set `DAIMON_COMPLETION_PINGS` to a JSON object keyed by tenant UUID, for example
`{"00000000-0000-0000-0000-000000000001": true}`, to deliver that tenant's final
answer as a fresh thread reply mentioning only the requester. Missing or false
entries keep the existing in-place answer. On Discord the fresh answer carries the
turn's summary line; the status card above it shows "Done." without the summary and
is then deleted, so a card that cannot be deleted never repeats the summary. If the
answer fails before its summary is posted, the card keeps the summary instead.
Discord adds eyes before admission;
Slack adds eyes at admission. Both clear eyes independently of completion pings.
Recovery lifecycles retain this policy;
continuity notices and feedback target the new answer. Teams posts the answer fresh (with
an @mention in a channel), then sets its card to "Done."; bots cannot
react there.

### Routine dispatch

The scheduler owns a persistent, bounded routine dispatcher across ticks.
Routine turns run independently of the tick; the same routine cannot overlap
itself. Per-routine missed-run policy and the latest skipped range are exposed
by the routine MCP tools. See [routines.md](routines.md) for catch-up and shutdown.

### Agent-initiated direct messages

The shared channel tool `send_direct_message(recipient_id, content)` dispatches
to Discord, Slack or Teams under the authenticated tenant. Both sender and recipient
are checked for current platform membership before a DM is opened (on Teams,
a team Daimon is in that both belong to). Discord bot recipients, Slack
inactive, external, or bot users and Teams anonymous members are rejected. Other
platforms return unsupported. Channel tools continue to reject DM channel IDs.

Default recipient policy is tenant members. `DAIMON_DIRECT_MESSAGE_POLICIES` is
a JSON map keyed by tenant UUID, for example:

```json
{"00000000-0000-0000-0000-000000000001": {"mode": "allowlist", "recipient_ids": ["U123"]}}
```

Tenant UUID keys are normalized at settings load, including uppercase and
unhyphenated UUIDs. Invalid keys fail settings validation.

`mode` accepts `members`, `allowlist`, or `disabled`. All modes that allow sending
still require live tenant membership. The tool sends at most 19000 characters
as plain text in bounded chunks and returns every platform message ID. Partial
failures state the number already sent; callers should not retry the whole text
blindly. Attachments and cross-tenant delivery are outside this tool's scope.
### Memory write policy

Session memory mounts are read-only for channels with limited readers (including their threads),
for DMs when the tenant access policy sets `dm_memory_read_only`, and for routines.
Other chat turns retain writable memory. Admission carries the trusted decision;
the mount mode is recorded in the session snapshot and checked before reuse.
Tightening access replaces an idle writable session, including a legacy session
whose mount can no longer be inspected. An active session refuses the restricted
turn instead of deferring enforcement. Replacement
when tightening memory access skips the old session's checkpoint, since that would execute
with its previous permissions; platform history supplies the new turn's context.
Uncommitted workspace files are not transferred on this restricted replacement.

Memory content is managed directly by the MA memory store. This safeguard does not
add per-memory author/origin records or rollback tooling.

### Repository memory

With `repo_memory.enabled` (off by default), repository memories live in the
`repo_memory` Postgres table, keyed by tenant, GitHub repository id and title slug.
They are shared across agents using that repository, independently of MA memory stores.
`repo_memory_list`, `repo_memory_view`, `repo_memory_save` and `repo_memory_delete`
use a verified signed conversation origin and the responder's live App working-repo grant.
The administrator-approved working repo needs no personal GitHub link. An alternate
`repo=` read requires both an active agent grant and the caller's linked GitHub read access.

Anyone using the bound repo can replace or delete a shared entry. Personal directives
(`kind=directive`, `scope=user`) are visible and mutable only by their creator; a save
cannot change an entry's scope. Titles produce slugs up to 64 characters; re-saving
replaces content and warns if another title occupied that slug. References are catalog
pointers; directives include their bodies in the turn prefix. The common turn path uses
one indexed query, returning at most 512 entries and 40 KB of projected content; the
escaped prefix has a hard 48 KB UTF-8 budget. Overflow is marked for explicit retrieval; an oversized directive becomes a pointer
to its full body rather than a partial instruction.
Repository state follows the trusted turn controls. The first turn receives the rendered state; subsequent turns receive it only when
that state changes. The same query reads the last delivered hash by tenant, provider
and session in `repo_memory_sessions`. A successful turn records the new hash; failed turns can retry it, and
replacement sessions receive a fresh copy from the same turn’s state read. Empty state supersedes earlier saved
rules. No reference bodies are preloaded.

Both mutations refuse restricted, held, foreign and memory-read-only conversations,
including personal saves. Unknown or unattended origins refuse all four tools.
Shared sessions never inject a speaker's personal directives.
Held/foreign turns receive no repository-memory prefix. Routines receive only shared
state for their authorized working repo and cannot invoke the tools. Reviewed facts in
the dedicated reviewed-memory repository belong in Git/PRs rather than reference entries.
Account erasure deletes personal directives; shared entries retain content with attribution removed.

Before enabling native memory, import and verify existing entries and configure
`repo_memory.legacy_mcp_server_names`. Session tool overrides disable only those servers'
`repo_memory_list` and `repo_memory_view`, so the model uses one native source.
Retire old sessions at cutover. There is no fallback to a stale legacy list after a native write.

### Shared and personal memory

An agent has three kinds of memory store (`packages/core/daimon/core/memory_resource.py`):

- **Shared**: one per agent (`agent_memory_store`). Every session of the agent mounts
  it, for every person and channel. Its instructions keep it to team and agent
  knowledge: conventions, project facts, how tools and skills behave.
- **Personal**: one per agent and person (`agent_personal_memory_store`, keyed by
  account). It is created on that person's first new chat session and mounted at
  `/mnt/memory/personal-<account id>/` beside the shared store. Its instructions keep
  it to that person's preferences and facts about them, and forbid copying from it
  into the shared store.
- **Channel**: one per agent and parent channel (`agent_channel_memory_store`, keyed by
  platform and channel id), created on the first held session there. It is what a
  held turn mounts, and nothing else (see below).

A chat session is already keyed on the caller's account (`thread_sessions`), so only
that person's own sessions (channel threads, DMs, on every platform) mount their
personal store. Another person's session never has it mounted and cannot read it;
the separation does not depend on the agent following its instructions. Only the
choice of which store a fact is written to depends on the instructions.

The personal mount has the same access as the shared one: read-only in channels with
limited readers and in DMs under `dm_memory_read_only`. A session without a personal
store gets shared-store instructions saying that no personal memory is mounted and
nothing personal is to be recorded. Who else gets one:

- **Routines**: explicit owner DMs use the normal live-DM memory policy, including
  personal memory and ordinary write modes. Channel routines use shared memory
  read-only without personal memory, as do legacy destination-free routines and
  routines switched to No automatic delivery. Only explicit DM routines mount
  personal memory. DM routines run unattended: nobody watches
  intermediate actions, and permitted tools retain ordinary DM side-effect risk.
- **MCP**: the hub's `ask` and `start_turn` mount the caller's personal store. A hub
  caller is a person: the hub token comes from Daimon's OAuth proxy after a Discord or
  Slack sign-in, `HubIdentityMiddleware` checks its signature, issuer, audience and
  platform, `_auth_for` builds the identity for that person's own account in the tenant
  (re-reading the account and tenant), and only that account can use the session handle.
  A per-agent key, an operator or CLI token and an internal caller get the shared store
  alone: a key is the agent, its sessions are the agent's, and whoever holds it is not
  established as one person.
- **CLI**: `daimon sessions create` identifies the operator by `$USER`
  (`cli.local_user`), which is configuration rather than a verified person, so it gets
  no personal store.

**Held turns.** A turn is held (`Admission.hold`) when its requester is from another
organisation or unknown, or its channel is shared with another organisation. A held
session mounts its channel's store read-write (read-only when the turn's memory is
restricted) and no other: not the shared store and no personal store, whoever asks. The
channel store's instructions tell the agent that everyone in the channel, including
people from another organisation, sees it and that it must not record anything it would
not say there. The isolation holds both ways: a held session never has the shared or a
personal store mounted, and an unheld session never has a channel store. A thread shares
its parent channel's store, and a shared-thread session (no caller account, so no held
credential) mounts the place's store too. `desired_snapshot_for` takes the same store, so
a session still on the shared store when its channel turns shared (or the reverse) is
replaced, with nothing carried over and refused while a turn is active (the memory
boundary above); a session already on the channel store is reused. The session snapshot
also records the isolation scope (`memory_scope`: absent when unheld, else
`channel:<platform>:<id>`), whether or not a store mounted, so a held place whose store
failed to provision is still told apart from an unheld one and from another place. Any
scope change is a boundary, a permitted handoff to another agent included: it carries
nothing across, though a handoff that stays on one side of the boundary keeps its carry
rule. The field is omitted when unset, so the previous build reads the stored snapshot. A fleet worker
captures exactly one memory resource, the shared store, so a held admission never reaches
that capture. Archiving an agent archives its channel stores with the others. Rolling back
to a build without channel stores replaces those sessions with shared-store ones (again
carrying nothing over); the table is ignored.

MA attaches memory stores only when a session is created. A live session recorded
without a personal store is reused, not replaced, after the caller's store exists
(`session_compat`); the store is mounted on the caller's next new session. A session
whose recorded personal store differs from the caller's current one is replaced.
Existing shared memory is never moved or split.
`daimon memory personal-report [--platform P --workspace W]` scans every shared store
for entries that look personal (a mention or a name with a preference word, first-person
preferences, per-person paths; `daimon.core.memory_personal_scan`) and prints per agent
the number of memories, how many look personal and a few redacted examples (preference
words kept, names, mentions, e-mail addresses and every other word masked). It only lists
and reads memories; deciding what to do with them is left to the operator.

`/memory` (Discord, Slack) and `memory` (Teams) still show the shared store read-only,
or in a held place (the channel is recorded as shared, or the person asking is recorded
as from another organisation) the channel's own store. The command takes the live facts
from its adapter, not only the recorded ones: who the asker is (a workspace member, from
another organisation, or unknown) and whether the platform calls the channel shared. Only
a verified member in a channel neither the platform nor the registry calls shared reads the
agent-wide store; an unknown asker is held, and nothing is recorded about them. Slack asks
`users.info` and `conversations.info`; Teams reads the activity's tenant evidence (no
evidence is unknown) and its channel type; Discord has no other-organisation members and
uses the registry. In Teams the answer to a command typed in a held channel is never moved
to the sender's 1:1 chat when the private reply is refused. In a held place `mine` and
`forget-mine` answer that personal memory is not used there and touch nothing.
`mine` lists the caller's own personal store and `mine /<path>` shows one file;
`forget-mine` shows what would be deleted and a six-character code, and
`forget-mine <code>` deletes every memory in that store. The store is always the one of
the person who typed the command, resolved from their own account; no argument names
an account. The code is a hash of the store and each memory's id, path and content
hash, so a store that changed since the preview needs a new code. The confirmation lists
the store once, checks the code against that listing and deletes only the memories in it,
each with its listed content hash as the API's `expected_content_sha256` precondition. A
memory written or rewritten in between (by a live conversation) is kept, and the reply
says how many were kept. Deleted
memories keep their MA version history for MA's retention period (30 days); account
deletion deletes the whole store.

Deleting an agent archives its shared store and every personal store, each on its own so
one failure leaves the rest archived. A personal binding is then only stamped
`archived_at`, never dropped: an archived store still holds that person's memories, and
the row is how account deletion finds it. Account deletion deletes every personal store
the person has at Anthropic, archived ones included, and drops each binding once its store
is gone. The bindings have no foreign key to `accounts`, so they outlive the account row:
the scheduler's privacy sweep (`sweep_erased_personal_memory`) deletes the stores of any
erased account that still has bindings. That covers a failed upstream delete, an erasure
run without an Anthropic client, and an account with no principal left to queue session
deletion.

### Platform table rendering

`DAIMON_TABLE_RENDERING` is a JSON map of tenant UUIDs to booleans. Discord
renders tables unless the tenant is set to false; Slack renders them only when
set to true. Teams renders Markdown tables natively, so the setting does nothing
there. UUID keys are validated at startup. Core `tables.render_tables` parses pipe-delimited Markdown tables outside fenced
code and accepts an optional async platform hook. Without a hook the input is
returned unchanged. Both adapter helpers also default to disabled. Hook failures
and oversized tables retain their raw text. Rejected Discord attachment edits
and Slack table blocks are logged and retried as the original Markdown; Slack
keeps already-delivered chunks in place and retries only the rejected table.
The shared bound is 20 columns, 100 rows including the header, and 10000 cell
characters; at most ten tables render per answer.

Discord renders final-answer tables off-thread as PNG attachments, each with the
raw Markdown (up to 1024 characters) as its description, using bundled
Inter fonts (including wizard submissions and their recovery turns), navy headers, light alternating rows, and horizontal rules. Wide
cells wrap without truncation, all-numeric body columns align right even without
an explicit `---:` marker, and a pixel budget
prevents excessive allocations. Tables containing glyphs absent from the selected
font, including CJK text, remain unchanged Markdown so no values are lost. Table markers preserve their position in the
surrounding answer text. Slack final replies use native wrapped table blocks,
with each table in a separate message so table budgets stay bounded and prose
order is preserved. Feedback stays on the final delivery and continuity notices
can be inserted before a leading table. See the [Slack table block reference](https://docs.slack.dev/reference/block-kit/blocks/table-block/).

Streaming status previews and MCP `send_message` remain plain text. Tables inside
code fences remain literal examples. Other adapters need no renderer changes.


### Selfhosted usage reconciliation

Selfhosted billing has a separate transactional path in
`daimon.core.selfhosted_usage`. The runtime must call its tariff admission API
before paid model work; this API does not enable the reserved profile by itself.
Admission saves the deployment, underlying model and effective tariff in the
operation store. The recorder uses that snapshot for request-level settlement
and corrections, with one underlying-model accounting identity across native
deployment/model aliases. Unknown usage or unverified pricing stays pending.

Operators inspect these gaps with `daimon usage pending TENANT_UUID --json`.
The content-free report includes reasons, tenant-bound cursor pagination and
an explicit truncation flag; malformed rows are flagged without hiding other
requests. It is read-only: runtime replay supplies authoritative repairs.
See [selfhosted billing](billing.md#selfhosted-azure-usage) for admission and
reconciliation details.

### Durable turn outcomes

`turn_outcomes` stores one content-free terminal record per logical turn. A UUID
follows admission, session binding and execution; a dead-session recovery remains
one turn. Discord, Slack, their continuation/wizard paths, headless routines,
fleet workers and CLI runs use the same recorder. Fleet workers retain their own
`worker` origin; their diagnostic never masquerades as an orchestrator or routine. Admission and concurrency refusals are recorded
even when no model runs. The row contains tenant/account and agent identifiers,
platform/channel/thread identifiers, the shared `TerminationReason`, UTC start/end
and monotonic duration, recovery status, exception class, package release and
observed usage-event keys. It contains no messages, prompts, answers, tool inputs,
rendered error strings or credentials. Missing attribution stays null (for example,
a CLI run against an existing MA session); no tenant is inferred from user text.

The terminal path schedules a bounded background write and never awaits database
I/O. Inserts are idempotent on the turn UUID. At most 256 writes are pending; each
has a one-second timeout and owns its database connection. Failures and queue
saturation log identifiers and exception class only. Runtime shutdown drains
pending writes before disposing its engine. These are best-effort diagnostics:
process crashes, queue saturation and database outages can lose an outcome. They
are not a transactional audit log, and never change admission, billing or replies.
Library-only headless calls without a session factory remain unrecorded; all
production headless entrypoints provide one. Usage references are the natural
`(managed_session_id, event_id)` keys, including calls observed during recovery;
billing-exempt calls may have no corresponding `usage_events` row.


MCP agent-chat and hub `ask` calls also record one outcome, attributed to tenant,
account, agent and session with platform `mcp` and origin `chat`. A returned idle
reply is `completed`, an observed terminated session is `session_terminated`, and
the bounded polling deadline is `ceiling`. Shared admission gates record balance,
cap and access-policy refusals. `start_turn` and `continue_turn` record the
accepted dispatch with reason `unknown`: they return before model execution ends,
so these records are dispatch observations, not claims of terminal completion.
No follow-up terminal update or model-span usage capture is implemented for these
SDK polling paths. Their usage fields remain null rather than implying zero
model calls or cost. Channel/thread identifiers are unavailable on these calls.
Identity resolution failures before tenant attribution and adapter readiness /
draining gates before the turn boundary are outside this coverage. Library-only
headless calls without a session factory remain unrecorded.

The terminal outcome row also carries optional per-turn usage measurements:
model-span token/cache totals, model IDs and estimated provider cost. The driver
observes billed and exempt spans without changing metering; natural
`(session_id, event_id)` keys deduplicate replay and retain recovery-attempt usage.
The operator command `daimon usage turns` queries tenant-scoped rows and channel /
origin summaries without upstream requests. See [billing](billing.md#per-turn-usage-telemetry)
for unknown-cost and historical-row semantics.

### Security audit trail

Authenticated requests through the main JWT MCP application (`tools/call` and
`tools/list`) produce one tenant-scoped
`security_audit_events` row. The identity middleware records the tenant, account,
platform user and agent identifiers, tool name, timestamp, outcome and a fixed
reason code. Agent identity includes the signed external `agent_id` or ordinary
chat `chat_agent_id` claim. A registered token adds its kind and jti, and a
scoped tool adds the operator scope it checked. The shared operation policy annotates that same request with the
operation name and its decision; a policy denial remains a denial even if a tool
catches it. A tool that refuses a call on an access decision (`authorize`: a channel
or agent rule, the invoker allowlist, a routine or default binding, an
environment pick) records the action as the operation and `authz:<reason>` as
the reason, so those refusals are denials rather than tool errors; a list that
filters on the same decisions records nothing. Arguments, messages, credentials, response bodies and exception text
are never copied into the row. Tool names outside the supported identifier syntax
are recorded as `<invalid>`.

Operators can read records using `daimon audit list TENANT_UUID --since
2026-09-28T00:00:00Z --json`. Use `--account ACCOUNT_UUID` to include that person's
audit metadata in an operator-assisted privacy export. Results are chronological;
`--limit` (1–1000) and `--offset` paginate retained records. Repeat per tenant for
a person active in multiple workspaces. This operator command uses local database
access; it is not an MCP tool and is not exposed to tenant users.

Retention defaults to 90 days (`security_audit_retention_days`). Operators must
schedule `daimon audit prune TENANT_UUID` for each tenant, for example daily. The
command removes only that tenant's events older than the configured age; `0`
explicitly opts into indefinite retention. Privacy erasure still applies. Include
the schedule and backup expiry in the deployment's published retention policy.

The shared account privacy purge clears account and platform-user identifiers in
all of that account's audit tenants, even when no principal remains. Tenant deletion
removes its audit rows in the same transaction. PostgreSQL AFTER DELETE triggers
on accounts and tenants enforce these same erasure rules even when an older
application binary issues the deletion during a rolling upgrade. The triggers
restore the previous transaction-local maintenance flag after scoped erasure;
rollback restores both data and flag on failure. Install the migration before
starting audit-producing services; privacy workers need no coordinated upgrade.
Queued writes lock live identity
rows: after account deletion they omit personal identifiers, and after tenant
deletion they are discarded, so delayed writes cannot restore erased data.

Ordinary UPDATE, DELETE and TRUNCATE remain blocked by a statement trigger.
Dedicated store maintenance functions enable a transaction-local GUC only within
a savepoint, perform scoped erasure or expiry, then reset the GUC. Rollback also
resets it; TRUNCATE is never allowed. This guards accidental application mutation,
not a malicious database administrator or SQL caller able to set arbitrary GUCs.
Existing privacy panels do not yet deliver a full audit export: operators include
the CLI JSON export in the requested bundle.

### Operator tokens

An operator token lets an external integration call a few MCP tools over
`/mcp` for one server admin. `daimon mcp mint-operator-token` mints one, with
one or more scopes, a TTL of 30 days by default and at most 90 and, with
`promo:create`, an optional `--max-issued-usd` ceiling in whole cents. Server
admins also mint, list and revoke them from Who answers where on the Discord,
Slack and Teams setup panels (`core/panel_operator_tokens.py`): tenant scopes
only, never `promo:create`, 30 days, shown once, and they list and revoke
only tokens within those scopes; the live admin check is
stored as the account's role, as a turn stores it, so the verifier admits it. `daimon mcp list-tokens` and
`revoke-token` manage every registered token, and `set-token-scopes --jti ...
--scope ...` narrows an operator token to the scopes given: it only removes
scopes, so adding one takes a new token. `mint-token` CLI tokens are
registered and expire too, while older jti-less ones keep working.

| Scope | Tools |
| --- | --- |
| `tenant:read` | `list_channel_skills`, `list_agent_tool_rules`, `list_environments` |
| `channels:write` | `set_agent_default` and `clear_agent_default` (channel defaults only), `set_channel_rule`, `set_agent_rule`, `set_channel_environment` and `clear_channel_environment` (channels only), `add_channel_skill`, `remove_channel_skill`, `set_agent_tool_rule`, `clear_agent_tool_rule` |
| `agents:archive` | `archive_channel_copy` |
| `promo:redeem` | Refused: promo redemption requires a direct personal user credential |
| `promo:create` | Refused: deployment-wide issuing remains on the local operator CLI |

`get_tenant_summary` requires a direct personal platform-admin credential and lists every channel with a default, a budget, admins or
a rule, and `channels[].own_agents_only` says whether its readers are `own`.
Its read is `daimon.core.tenant_summary`, which `daimon channels list
PLATFORM WORKSPACE_ID [--json]` prints too, with the same JSON plus each
channel's `readers` and `writers`. The MCP tool omits those two keys: no other
MCP or chat path shows every channel's rule. `timed_credit` lists the live
timed promo credit. `set_channel_rule` with an operator token acts as that
admin: the copy it may make is an admin's fork, and its rule writes are the
same as the panel's.

Each request, `DaimonJWTVerifier` reads the token's `mcp_tokens` row: it must
be unrevoked and unexpired, its account still a server admin by stored role
with a platform user, and its scopes non-empty. The stored role changes only
on the account's next platform turn, so a demoted admin's token keeps working
until then or until it expires; `revoke-token` is the immediate stop, taking
effect on the token's next request. Scopes come from the row, so narrowing
them applies to the next request. An operator token cannot open a billing
checkout (`/billing/checkout` answers 403). The identity middleware disables
every tool for the token, enables those tagged `scope:<name>` for its scopes,
skips the search collapse and limits it to `operator_calls_per_minute` calls.
Each tool re-checks its scope (`tools/_scopes.py`). `promo:create` tools carry
no `admin` tag and a server baseline hides them from everyone else. A later
`channels:write` tool joins by taking the tag and the `require_scope` call;
`_scopes.py` describes the steps.

### Opt-in DM conversations

DM conversations are disabled unless the tenant's separate `direct_message_policies`
row enables them. Admins use `/dm-me enable` or `/dm-me disable`; the invoker allowlist
still applies through `admit(is_dm=True)` on every move and every private turn.
Discord uses a live guild-member lookup and Slack checks the current workspace
membership and role. No stored admin role grants access.

`/dm-me` in a server/channel moves recent text context into a new private scope. This
selects the workspace explicitly. The scope uses a
normal thread-agent binding, config cascade and per-account session mapping. Each
new selection resets the scope, so a physical Discord DM shared across servers
never contributes the previous server's private history. The live membership check
is bound to that scope to prevent a concurrent selection from changing its authority.
The context is escaped with the existing handoff builder and includes a source link.
Discord also accepts a first text DM when a ready, unarchived registered tenant has
DMs enabled and a non-null `dm_agent_name` in its durable DM policy. Null leaves
cold DMs off without disabling `/dm-me` moves. Admins select or clear the name with
`/dm-me action:agent agent_name:NAME` and `/dm-me action:clear-agent`; operators can
inspect or update it with `daimon tenants dm-policy discord GUILD_ID`,
`--agent NAME --enabled`, and `--clear-agent`. The agent must already exist in that
tenant; there is no fallback to the server-default agent or automatic provisioning.

Discovery checks fresh membership, invoker access, recipient restrictions and the
selected agent's rules before exposing any server. One eligible server starts a
new private scope and answers the original message. Several produce a requester-only,
route-bound server selector, including pagination for more than 25 servers. It retains
the original message for five minutes and rechecks eligibility on selection. Later
messages on that route are silently ignored while selection is pending. After expiry,
the next message discovers again. Unknown membership asks for a retry rather than
presenting an incomplete choice. No eligible tenant means no model turn and a structured
reason log; Daimon sends a one-time `/dm-me` guidance reply per person per process.
Discovery and server switching share a per-user bucket of three attempts,
refilling one every 20 seconds; exhaustion is silent. Ordinary active replies do not
consume this bucket. Pending controls and rate buckets are process-local; a restart
invalidates selectors, so the person sends a new message.

A cold scope binds its own agent without changing the physical DM channel's config.
Its context and history start empty, its source channel/thread are absent, and its
origin label is `Private conversation`. This explicit provenance is distinct from
old moved conversations that lack recorded sources. The opening notice names the
server and agent, points to `/dm-settings` and offers **Switch server** and
**New conversation**; replies carry no controls. `/dm-settings` (DM-only) names the
current conversation and shows the same controls again. Only the latest control for
each user/DM route remains active; a later one stops its predecessor. Controls expire
absolutely after fifteen minutes, and adapter unload stops all controls and pending
selectors. Run `/dm-settings` to get fresh controls. Switching creates a fresh scope, retires the
previous session mapping and never replays the previous prompt or history.
**New conversation**, and the DM-only `/new` command, replace the active conversation
with an empty cold scope in the same tenant with its current DM agent, so the new
session reflects agent changes made since. They recheck eligibility as discovery does
and use the same rate bucket; a control whose conversation has since changed refuses.
An active conversation always wins over discovery, even if access to its tenant is
revoked; revoked access is silent and never reroutes the message to another server.
Route writes serialize even before a conversation exists, and message claims deduplicate
turns. Distinct simultaneous first messages may each run once as successive turns
in the same newly created scope; duplicate gateway events and concurrent selector
clicks never answer a message twice. The reply result carries the exact admitted
agent grant. Immediately before each private output chunk, delivery rechecks fresh
membership, current tenant/invoker policy, recipient restrictions, the answering
agent's `RUN_AGENT` and `DIRECT_MESSAGE` permissions without an admin exemption,
and moved-source sealing. Unavailable bound identities and unreadable private
policies are typed core denials that Discord logs without sending a reply.
Routine notifications deliver through their transport without calling
`start_dm`, `start_cold_dm` or `start_conversation`. The active route is exposed by
`stores.direct_messages.get_conversation(platform, route_key, external_user_id)`;
routine delivery neither replaces it nor appends to its private model history.

`/dm-me` refuses in a channel with limited readers or a Discord thread that has
or sits under one, before any history is read: the DM sits outside the rule
and can reach open channels. Admission reports this as `source_sealed`, and
`start_dm` refuses it too. Slack's slash command carries no thread, so a Slack
thread with its own rule (`channel:thread_ts`) is not refused; instead its root
and broadcast replies are dropped from the copied channel history, as the read
tools do. Discord system notices (thread created, pins) are never copied. The DM
row records its source channel and thread and, on Slack, the `channel:thread_ts`
of every copied message. Every private turn re-checks them against the current
rules. Once any of them has limited readers the DM is quarantined: the turn is refused,
the conversation row (copied context and private history) is deleted, and the
scope's provider sessions are retired and archived, so a fresh `/dm-me` is needed.
Rows from before provenance was recorded cannot prove their source open and
are quarantined as soon as the tenant limits any readers.

Turns use the shared billing/session/recovery pipeline. Row claims prevent overlapping
or duplicate DM deliveries. The route retains bounded source context and private
history; privacy preview and deletion include it. Memory restrictions inherited
from the source remain attached to its DM conversation, and current DM policy is
checked on each admission. Session preparation enforces the selected memory mount access.
Slack private turns register their physical DM destination under a random execution
ID carried in a signed JWT in an isolated MA vault. The read guard resolves only
that exact row for the authenticated tenant/account and removes it on completion
or cancellation. Concurrent routine, headless, channel, MCP agent-chat and hub
credentials have no execution grant and cannot inherit account activity.
Each Slack DM turn starts a fresh MA session and vault; bounded source/private
history preserves conversation context, but ephemeral workspace files and personal
OAuth credentials stored only in the shared vault are not transferred. Old execution
tokens cease granting DM reads when their row is removed; expired rows fail closed.
Both platforms stamp private MA sessions with `daimon_private_dm`. Generic session,
agent-chat and hub lists, transcript reads and mutation/cost tools default-deny
these sessions even to the same account or an admin. Only a verified credential
whose execution ID matches the stamp can access one. Discord retains session
reuse but carries no transcript-browsing grant, so its private sessions are hidden
from all MCP session tools. Ordinary unmarked sessions keep their ownership rules.
Discord session reuse and ordinary shared vaults are unchanged.
The provenance thread ID remains the private scope ID, matching session mapping.

The first version delivers text replies after completion. Attachments, streaming
cards, cancellation controls, and moving Slack thread replies are deferred; Slack's
slash command carries recent channel messages. Run `/dm-me` again to reset or select
another channel. Disabling DMs prevents new turns; Discord also suppresses private result delivery. Disabling DMs does not cancel a model turn already running.

Slack consumes the same optional neutral-event tap for alternate-provider drafts. Its render task retains debounce and Cancel registration, and terminal callbacks replace the progress card and deregister Cancel. Foreign provider observations never become Anthropic display meters.

Teams consumes neutral snapshots and draft fragments through the optional lifecycle tap for alternate providers. Terminal callbacks retire the progress card and deliver authoritative answers; unprompted drafts remain silent. Unknown alternate-provider cost stays absent from the summary instead of becoming an Anthropic usage meter.

### Typed provider-action cards

Discord attaches the typed provider-action callback only to prepared non-Anthropic sessions. Browser-origin cards return the supplied approve, deny or cancel response; authentication cards offer completion only when the provider supplies a verified response. Requester and destination checks precede selection, and expiry or Stop retires the controls without granting permission. Native action payloads stay out of cards.

Slack attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.

Teams attaches the scoped provider-action callback only for prepared alternate-provider sessions. Browser-origin permission offers Approve, Deny and Cancel; authentication offers Cancel and includes completion only when core supplies that response. Cards bind the requester and destination, return the offered native response object unchanged and retire on expiry or Stop. Native payloads remain hidden; default Anthropic calls attach no new callback.

### Neutral output upload edges

Discord, Slack and Teams output-delivery modules accept verified `DeliverableFile`
bytes through `deliver_provider_output`. The caller owns admitted tenant/session/root
validation. Uploads reuse answer attachment edits, Slack's external upload flow,
and Teams Graph or requester-bound consent with SharePoint PUT. These edges
perform no provider selection, native payload parsing or artifact deletion.

Discord passes through only explicitly audited `UserFacingError` guidance. Raise
that marker with deliberate copy, never exception text, provider bodies, secrets
or internal identifiers. Known domain errors map to fixed next steps; unmarked
exceptions keep a generic public fallback and diagnostics in logs. Both Discord
and shared New Agent creators use the typed name-collision refusal, as does
agent copying. Shared setup-responder, agent-visibility and DM routing refusals
mark their fixed guidance explicitly. Terminal
failure cards retain audited guidance as their next step, including an expired
setup context encountered during reconnection.

### Opt-in Discord waiting cards

`discord.tenant_cap_waiting_card` defaults to false. When enabled, an initial
mention's admitted ticket shows “Queued — waiting for a free slot…” on its
existing progress card while it waits behind the tenant cap. The same card
returns to ordinary progress when the slot is granted. Stop, queue limits and
FIFO admission are unchanged, and the card displays no tenant identities or
queue counts. Drained follow-up turns keep their existing card wording.

## Current Discord membership evidence

The pure authorization subject carries platform role IDs and verified tenant
membership. Admission and reauthorization use these facts for the tenant's
invoker policy. `daimon.core.membership.MembershipResolver.resolve(tenant_id,
guild_id, user_id, require_fresh=False)` is the shared asynchronous boundary.
`Membership` reports membership (true, false, or unknown), current role IDs,
and current server-admin permissions. Callers select a registered tenant's
guild; internal account UUIDs never substitute for Discord user IDs.

The Discord adapter timestamps gateway facts at ingress, before queue waits;
queued or replayed message snapshots cannot restore revoked roles. Its live
SDK fetch refreshes guild role/owner metadata as well as the member. MCP and scheduler hosts use the core Discord REST transport
with the existing bot credential, without importing the Discord adapter.
The shared cache keys by tenant, guild, and user, shares concurrent fetches,
and discards results invalidated while in flight. `require_fresh=True` bypasses
completed cache entries. Unknown evidence cannot satisfy a role gate.


## Owner-DM routine delivery

The Discord `/routines` panel requires server admin access and includes an
admin-only creation modal with schedule and agent policy validation, confirmed
creator-or-admin deletion, and owner-only delivery choices and failure reasons
for the admin's own routines. MCP accepts `destination_kind="dm"` with the
recipient derived from the authenticated owner; the operator CLI has
`routines create --destination dm`
and a verifying dry-run. Slack and Teams explicitly reject DM writes.
Retained previews, errors and delivery diagnostics for DM and legacy
destination-free routines are owner-only on every reading surface, including
administrator panels. The rollout migration clears legacy retained output.

DM routines are scheduled owner messages through core's normal live-DM admission,
binding and runner; [routines](routines.md) has the rules a user sees. A shared
PostgreSQL advisory owner guard skips a scheduled slot when a live DM turn is
running, without changing the selected cold-DM conversation or its history.
Execution is unattended (`attended=False`) on gateable Managed Agents runtimes
only, through the unattended session gate on every send batch. Source channels
and threads are verified against the tenant's guild through core Discord REST
transport. The creation-time source pair and its known/unknown marker are
written once and checked at DM conversion, fire and every delivery chunk.

For an explicit owner DM, the scheduler submits the scheduled message through
`direct_messages.run_dm_routine`; live and scheduled DMs share
`_run_admitted_dm_turn`, `admit`, `bind_session` and `run_prepared_turn`. Channel
routines retain their read-only headless path without personal memory. The final
answer enters the routine outbox through an atomic captured producer/destination/
revision fence. Private edits erase old previews, diagnostics and pending results;
claimed deliveries recheck the same identity before every chunk and file. The Discord host checks live membership, tenant DM
enablement, recipient policy and source restrictions before opening the
owner's DM and before each chunk. Recipient IDs are never channel targets or
budget sources. Notifications do not alter the selected cold-DM conversation
or its model history. Delivery uses the existing at-most-once claim/settle
leases, with no public fallback or outbox replay after an uncertain send.
A platform SDK may retry a single send after a server error or connection reset.

Routine result delivery collects settled session outputs after the successful
turn and stores a versioned text-and-file-reference envelope in the
existing outbox payload. Adapters download and upload those references inside
the claim/settle lease, rechecking destination and owner access before each
file. Discord and Slack reuse their interactive upload paths; Teams routine
posts explain that attachments are unsupported. [Routines](routines.md#generated-files)
lists the file limits, the fallback-DM rechecks and the replay guarantee.
Delivery error logs record the error type without arbitrary exception text,
which may contain signed upload URLs.


### Signed page-event watch entry point

Polled watches work end to end, on Discord `read_thread` and `read_channel` or on a
listed read tool of an attached MCP server.
See [supported sources and page-event limitations](self-hosting.md#signed-page-event-watch-sidecar).
The callback infrastructure verifies HMAC, binding, freshness and replay before
acknowledging a transaction that stores evidence and enqueues a `watch_fired` wake.
It has no direct table access or turn runner; core's durable queue handles dispatch. Discord, Slack and Teams share
compatible readers; CLI and scheduler do not dispatch continuations. Old readers
ignore the new reason before batching, preserving unrelated work on rollback.

Migration `0142_watches` follows `0141_lead_intake`. The producer setting
and durable global gate default off. Watch turns explicitly use `attended=False`
and require the current origin's exact session grant. Registration, key generation,
owner access, destination, balance, member caps and channel budget are rechecked
before model/confirmation sends and report writes. Deleted threads have no fallback;
unsupported backends and implicit session replacement are refused.

`core.turn.unattended_capabilities` supplies the `watch` policy and shared
validation of capability fields, tool projection, cache
invalidation, send and restore. The thin watch wrapper refuses duplicate server
toolsets and retains only enabled, exact-name third-party read configs. Skills, multiagent coordinators and
unknown fields in capability containers (including newly parsed SDK fields) refuse
before mutation. Non-capability sub-parts are opaque: neither their keys nor
their values are checked. SDK private instance attributes are not checked. Built-ins,
custom tools, management endpoints and writes are unavailable; defaults are disabled
and every retained tool asks the live read/report decider. Default-only toolsets
provide no tools. Mounted data/vaults grant no native or delegated execution.

Cache invalidation commits before provider mutation. Original tool/server arrays
restore before release, including repeated cancellation; origin authority is revoked
after cleanup. The next bind reconciles tools after interruption. Each platform
send/edit/upload, chunk and terminal success/error uses a latched output check:
once refused, cleanup and late tasks cannot publish retained content. Watch turns
start no detached output-file sweeps after their grant ends.

Revoking a key, stopping a watch or disabling an owner/tenant/global gate cancels
queued wakes. Re-enabling does not revive them. Evidence expires after seven days;
independent byte, row and rate quotas prevent a pending batch growing without bound.
Migration `0143_watch_polling` follows `0142_watches`. Sidecar claims at most four
poll leases through `claim_watch_polls()`, then submits only a watch UUID and opaque
60-second lease to the separate private `watch-executor:8767` service's
`/internal/watches/{watch_id}/poll` endpoint. The public MCP app has no poll route.
Only fixed private executor origins are supported and redirects are disabled. The lease
is a scheduling capability; the caller supplies no tool, arguments, identity or
result. Sidecar cannot read watch rows or submit poll results. The watch executor loads
the immutable registration and rechecks current membership, agent rules, seals,
exact destination visibility and source permissions before every read, and again
before saving its result. It uses existing Discord `read_thread` and `read_channel`
implementations as the human owner, or `core.mcp_tool_call` for an MCP server
source. No AI turn is spent polling. Both producer
gates apply before a source is read; stopped/expired leases cannot commit.

Polls exclude bot/assistant rows before selecting the latest human-message window
and before projection; pagination metadata is excluded too. Neither self-reports
nor cross-watch reports can trigger polls. A scan stops at 201 messages; an
incomplete human window backs off without changing baseline or evidence.
Source IDs must match `^[1-9][0-9]{0,19}$`; resolved places are compared before
reads. Enrollment verifies the attended owner's exact-tool approval with
`session_card_gap` inside the tool. Unsupported backends and ungated/unreadable
proofs refuse, including with tool safety off.

`continuity.poll_tools.POLL_TOOLS` is an explicit reviewed READ catalogue of what
*can* be polled; unlisted, non-READ and NO_WATCH operations cannot enroll. The
operator's `DAIMON_WATCHES__POLLABLE_TOOLS` registry says what *is* polled in a
deployment, keyed by `daimon-mcp` or a canonical server URL and the exact tool
name. `poll_tools.require_listed` reads it fresh at create, at resume and inside
`authorize_poll` before and after each read; a watch whose tool is delisted pauses
as `tool_unlisted` and its owner gets the lifecycle DM. Poll arguments must explicitly
name a source different from the reporting thread, preventing self-report wake loops.
The agent must currently have that read enabled with `always_allow` in its own
Daimon MCP toolset; missing, disabled or approval-required reads stop the watch. Arguments exclude URLs and limit
reads to 20 messages. The safe jq-style projection subset supports identity, object
fields, numeric indexes and one `[]` map step for polls. The raw read is bounded
(256 KiB, depth 32, 20,000 nodes) and only the projection is kept: a baseline of at
most 64 KiB (keyed `added` keeps key fingerprints only) and a payload of at most
16 KiB and 20 items. `0156_watch_mcp_sources` enforces the kept bounds again in
SQL; page events keep the 4 KiB limits.

An `mcp_poll` source stores the canonical URL, never the server name, and each poll
needs that URL still attached, enabled, visible to the owner and not denied. The
executor sends exactly one credential, to one pinned public address,
without redirects or proxies, reading at most 256 KiB and no compressed reply. The live `tools/list` entry
must match the digest approved at create and keep `readOnlyHint`; the hint can only
refuse, since the registry is what allows a poll. The credential is the agent-wide
one for that URL, or for an `owner` entry the watch owner's own key. That key
arrives only through the requester-only `request_watch_credential` form, is stored
encrypted in `watch_credentials` (`0162_watch_credentials`), and is looked up by
tenant, owner and URL. A missing key pauses the watch as `credential_missing` and
tells its owner. Each URL gets two polls at once
and 30 a minute, and a busy URL's watches step back in the
queue. `0158_watch_mcp_claim` lets the sidecar lease these watches.

The first poll saves a baseline without firing. Later `change`, `added` and `updated`
comparisons save their baseline with evidence and the existing `watch_fired` wake in
one transaction. False/empty change results re-arm without firing; zero is meaningful.
Removals alone do not fire added/updated rules. Per-watch/owner caps, coalescing,
retention and the read-and-report turn path are shared with page events. Poll errors
back off exponentially to one day; revoked access pauses the watch and cancels wakes.

`0164_watch_lifetime_caps` lets a watch be permanent (`expires_at` NULL), approved by a
freshly rechecked admin who is also its owner; every expiry check keeps the NULL arm,
in SQL and Python. The claim expires a permanent watch paused for 30 days. Daily caps
come from `watch_controls` ceilings, read on every firing: a tenant row, else the
global row, else 24 per watch and 100 per owner, never above 240 and 1000.

A polled watch reports where `delivery` says: its own thread, the owner's DM, or a
new public thread per report. The last two queue a `watch_report` wake, which the
thread wake path never lists; `0163_watch_delivery` adds it. The Discord report
poller runs a DM report as a fresh DM turn under watch posture, like a DM routine.
It rechecks DMs, the owner's DM permission and guild membership before each chunk.
A new-thread report checks the owner's thread rights on the parent over REST, then
records its one thread on the firing, so a retry reuses it. The watch's own
thread stays its policy and budget place. Turned-off DMs or lost thread rights
pause the watch.

MCP enrollment requires an attended turn origin and binds its tenant, owner, agent,
thread and parent; a watch that reports in its thread also binds that thread's session. Owner/admin management cancels queued wakes on pause,
rotation, editing and deletion. Only the owner edits, through `preview_watch_edit` then
`update_watch` with the previewed revision and old values; the card shows each change
old → new, and the old values must match the stored row. An edit moves the revision on
and drops the baseline, so the next poll re-baselines without firing. `list_watches(scope="all")` lists every watch in the caller's tenant for a freshly rechecked admin only (`fresh_admin`), and `daimon watches list --tenant` gives operators the same read-only view; neither shows instructions, keys, baselines or evidence. Fresh signing keys go only to the owner's private DM, once;
MCP results and stored rows contain no plaintext key. The offline CLI importer
accepts no signing keys, imports everything paused, and requires explicit exact-binding confirmation before
activation. An unmapped source cannot be confirmed or resumed. The existing Discord
notice poller sends its owner a private notice that the watch is paused, without
running a model or resuming the watch. Every owner notice records an intent receipt before the send and
its outcome after, so a restarted poller checks the DM instead of resending.
Account erasure removes owned registrations, evidence, pending wakes, owner
stop controls and owner watch keys; the privacy preview includes the watches it will erase.


## Lead intake

The [lead intake](lead-intake.md) coordinator and evidence contracts live in
core; tenant stores own encrypted submissions, effect intents, resolution audit,
mailbox coverage and short-lived routine execution grants. The MCP service
implements Gmail, HubSpot and Discord HTTP ports without adapter-to-adapter
imports. Seven first-party tools expose submit/resume/resolve, attachment-free
preclassification source reads, and private get/list/health; `daimon leads configure` provisions encrypted operator-owned
integration configuration. Scheduler dispatch signs execution provenance into
routine credentials and revokes it when the fire ends; retention maintenance
erases expired evidence independently of intake activity.

The Discord `/leads` entry point reads that same core tenant store and admission
contract for an ephemeral queue/review status; decisions continue through the
first-party resolver tool. It imports no MCP adapter implementation.

Opportunity reservations retain tenant/CRM/portal-scoped normalized project
identities. Separate mailbox scan history and message dispositions preserve
page discoveries and failed coverage independently of the accepted intake queue.
Sensitive opportunity mappings and discovered message IDs are encrypted and
expire with the retention policy.

Intake authorization carries the actual destination thread through recovery and
write intents. Both MCP record readback and the private Discord queue evaluate
that thread’s current policy and native visibility before returning its data.
Approval cards cache no lead evidence: each Details click authorizes and renders
from the live record, and removed records are unavailable. Store list traversal
applies the same live read predicate before counting the visible page limit.
Automatic resume batches use the current destination and revision and apply live
read authorization before filling their processing window; deferred captures
remain available for authorized manual review. Mailbox scan readback independently authorizes the current discovery destination,
including when a resume batch contains no records.

Routine intake provenance is signed only into the isolated provider-session
credential. Refresh preserves its routine/execution discriminator and session
grant; the shared personal-credential vault never receives routine provenance.
Every unattended confirmation path protects lead resolution, even with general
tool safety disabled.

### Self-hosted harness turns

The `selfhosted.loop` turn-runtime entry point composes injected tenant-scoped
agent/environment ports with the mux HTTP driver. A manifest authorizes the
exact admitted channel revision and caller. Price admission happens before
transport use; the host binds a deterministic session without model I/O and
records sends, receipts and canonical events in the existing fenced mux state
store. Invocation baselines keep previous roots out of each new reply.

The driver polls the durable harness journal through direct HTTP or Foundry
invocations. Reconnect reconciles the same root; cancellation holds occupancy
until a canonical terminal arrives. Model-request observations pass through the
selfhosted recorder, including unknown observations for unmatched request
starts. Display conversion uses SDK DTOs for adapter compatibility without
calling an Anthropic API. Tools and inline skills are assembled under the
admitted scope, and the host revokes the header-only MCP token in turn cleanup.

With Blob recovery configured, both harness loops depend on Azure Blob Storage
for turn claims, request inventory and reserved sequence blocks before paid
model I/O. Journal/history and workspace checkpoints upload asynchronously after
tool cleanup. Failed write-ahead or reservation commits prevent paid work with
retryable `durability_unavailable`; failed or degraded workspace snapshots allow
turns and polling to continue. Storage credentials remain in the service identity.

A degraded snapshot or upload failure queues `checkpoint_warning` for the next
new turn. The mux driver preserves it as a native event, and core converts it to
a non-terminal thread notice about possible message/file loss, emitted once per
session. A local pending marker retains late failures across process restarts;
a notice not captured in a snapshot can still be lost with a destroyed sandbox.

Recovery authenticates and persists the client cursor, loads remote reservations
and WAL inventory, restores an empty sandbox, and merges original event identities
before flushing local pending measurements. It fences new sequences above the
checkpoint, reservation and cursor, emits covering history gaps, then repairs
unmatched requests as unknown and unfinished turns as interrupted. A divergent
journal stays sealed until reconciliation succeeds; cleanup must succeed before
a terminal or another turn. This ordering preserves I1 (immutable sequences),
I2 (one usage outcome, preserving measured usage), I3 (one terminal with no later
turn events), I4 (every claim has an executor or recovery), and I5 (no tool side
effects after termination). See the harness [recovery order and invariants](https://github.com/pymc-labs/daimon/blob/main/apps/harness/README.md#recovery-order-and-invariants)
for the complete specification.



## Long-job workers

`start_long_job` registers an approved command with tenant, owner and pinned
agent/environment provenance. The scheduler owns leased isolated worker sessions and
reattaches after restart. Completion atomically records a result and a `job_finished`
wake. Discord verifies current owner destination access and runs that wake unattended
with only `get_long_job`; built-ins remain off in the parent. Slack and Teams fail
closed before completion delivery until they can prove current owner destination
access. Owners and current tenant admins may manage jobs; source policy separately
controls content in get/list, with restricted records redacted. Cancellation needs
management authority only and records a content-free audit. Worker transcripts
retain source privacy restrictions.
A durable initial-send intent and lease fence prevent a reclaimed executor from
sending another initial message. See [Long jobs](long-jobs.md) for policy and recovery.

The Discord and Slack `/here` cards and current-channel Permissions screens check
platform outsider visibility. Discord uses configured internal roles and permission
overwrites; Slack uses current external, Enterprise and pending sharing evidence.
Admins can set readers to `own` with **Make confidential**, with authorization checked at click.

Slack uses one fresh `conversations.info` response on each `/here` and Permissions
request for sharing flags and workspace-ID lists. It performs no guest detection
and caches nothing. Both screens explicitly say that Slack guest accounts are not
checked when readers is not `own`. Failed or incomplete sharing reads show
**Outsider check skipped**. Admin facts stay live. When later Slack metadata fails,
`/here` renders only fixed current-channel visibility facts against the stored
reading rule, with no admin controls or unrelated channel details. A bot missing
from a private channel still gets the invite-the-bot card with the skipped note.

### Tenant admin delegation

`core/native_admins.py` combines live platform authority with uncached,
tenant-scoped `daimon_admins` grants. The single `DelegatedAction` allow-list
limits which admin gates accept delegation. Adapter boundaries pass their
live verdict into configuration services and stores. Unnamed actions,
credentials, MCP connections, GitHub connect/approval/repo binding and DM
toggles require platform authority.

Discord interaction gates are asynchronous; Slack binds each per-event Web
API client to its verified workspace; Teams resolves grants at its runtime
admin boundary. MCP independently attests platform authority and current
grants on each request. The verifier distinguishes chat execution from
external agent keys. Authority changes and money require a directly clicked,
live-checked platform panel or a registered personal CLI credential with live
platform attestation. Every execution binding and operator credential is
refused for those operations, including an attended admin chat. Billing-panel
tokens are dedicated checkout HTTP credentials and cannot exercise personal
user authority or MCP money tools.

Account roles have exactly three write sites: turn admission and the
Discord/Slack GitHub connect commands, all from live platform facts. GitHub
connection panels pass current platform authority to invitation stores;
browser links re-attest new admins whose stored role is still user. These
paths do not write role snapshots. Operator panels refuse minting until an
admin turn has recorded that platform snapshot, with an actionable message.
This prevents minting a token already barred by the verifier’s native-role
check. GitHub link helpers, removal notices and operator-token minting never
write roles.
Turn-origin roles also record platform authority; `effective_role` is
rendered separately for the model. The hub retains its platform-only
transcript exception. Its live delegated agent-pin exemption grants no
transcript access. Revocation removes only the grant, with an atomic audit.

Granted admins can change what agents do, including agents platform admins use.
Their changes to instructions, skills, environments and tool rules also affect
platform admins’ routines and attended chats that use those agents.

Discord's Permissions panel and Slack's Who answers where panel link to
Admins. The screen lists grants and gives platform admins grant/revoke
controls; every mutation rechecks platform authority. Teams uses CLI/MCP.
The CLI uses authenticated MCP calls. Slack notice handlers check platform
authority internally and hide GitHub/operator-token controls from delegates.
Grant/revoke audits record actor, subject and timestamp; privacy erasure
removes user grants and scrubs attribution.

Material routine edits transfer the execution author in the core store,
with atomic audit, revision checks and output/delivery erasure. Scheduler
credential selection and private delivery follow the new author.
Pause/resume/delete retain authority.

Worker spending-limit MCP tools require a registered personal CLI credential and
live platform authority. Configuration confirmation cards carry a separate live
platform check for provider policy, channel-pin clearing and worker limits;
delegated model/settings and tool-rule checks remain explicit actions.

### Temporary compute sessions

The scheduler runs compute cleanup on its own repeating loop alongside the usage
sweep. Routine dispatch never awaits it, and there is no total sweep timeout.
Each Fly request is bounded; inventory failures and timeouts still allow known
rows to be reconciled. Deleted registrations are excluded from
the sweep query; tagged resources without a registration are still reaped.

The native `fly_*` MCP tools use a verified attended origin and owner identity to
register disposable compute in `compute_sessions`. The core service/store fence
every lookup by tenant and owner plus the originating conversation. Launch and
extend verify exact approval evidence; current human Discord server admins may
only emergency-stop, without reading session contents. Operator profiles fix the image
and machine capacity. File operations and inline bash use the image's
authenticated thin MCP server; no provider token is mounted in the machine.

The existing scheduler reconciles expiry warnings, interrupted creation, pending
deletes and deployment-tagged orphans. Registration commits before creation;
deletion intent commits before provider deletion, which is retried until confirmed.
Row locks serialize active machine actions and renewal against deletion. Disabling
new launches does not disable cleanup. The provider port has an in-memory fake and
HTTP contract tests; it does not depend on chat adapters. Expiry warnings enter
the existing owner-bound continuation queue.

#### Background commands and completion wakes

`fly_run_bash` with `run_in_background` registers a `compute_tasks` row (session,
owner, provider tool-use key, deadline, state) and commits it before touching the
machine. One inline `run_bash` then starts the command detached under `setsid`
and `timeout`, writing a pid, exit and output file under `/tmp/daimon-tasks/` on
the machine. A noclobber lock file makes a second start of the same task id a
no-op, and a retried tool use returns its existing row. Task handles never live
in an MCP client session, so scheduler, thin-server and control-plane restarts
lose nothing.

The scheduler polls active tasks on its own `_repeat_until_stopped` loop, beside
compute cleanup and never awaited by dispatch. Each pass reads a task's files
with one inline `run_bash` under the session row lock (skipping a session busy
with an owner call) and stores the output size and its last 16 KiB. While a
task runs, the poller renews the session lease, never past the max TTL counted
from the task's start. Outcomes:

| Machine says | Task becomes |
| --- | --- |
| exit 0 | `succeeded` |
| exit 124, or another nonzero exit at or after the deadline | `timed_out` |
| other exit | `failed` |
| still running after deadline plus five minutes | process group killed, `timed_out` |
| process gone without an exit file, or no pid two minutes after a start | `lost` |
| unreachable past deadline plus five minutes | `lost` |
| session stopping, deleted or expired | `cancelled` |

A command is never started again. The terminal versioned update and
`record_continuation` commit in one transaction, keyed by
`uuid5(task_id, "finished")`, so the session's own conversation (the only one a
task can start from) receives exactly one unattended `timer` wake. Its text is
short: the outcome, exit code and at most 1,500 characters of output inside
markers that call it untrusted data, never instructions. When the owner's own
`fly_task_status` call observes the end, no wake is queued, since the result is
already in that conversation. `fly_task_status` and `fly_task_result` use the
same attended origin, owner, conversation, tenant and source checks as the other
session tools; tenant admins cannot read them.
