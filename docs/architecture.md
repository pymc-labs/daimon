# Architecture

daimon is built on
[Anthropic Managed Agents](https://platform.claude.com/docs/en/managed-agents/quickstart).
Managed Agents (MA) owns the agent, its sandbox, its skills and the session
that runs a turn. daimon owns everything around that: the chat surfaces, the
tenancy model, the config cascade, credentials, the credit ledger, and the
pipeline that turns a chat message into a session and streams the result back
into a thread.

This page is the map to read before the code. `CONTRIBUTING.md` has the dev
setup and the quality gates; [configuration.md](configuration.md) has every
setting; [self-hosting.md](self-hosting.md) has the deployment.

## The shape

```mermaid
flowchart TB
    subgraph adapters["packages/adapters — one platform each"]
        direction LR
        Discord
        Slack
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
| `daimon.core` | `packages/core/daimon/core/` | Schema and migrations, stores, MA helpers, the turn pipeline. Imports no adapter. |
| `daimon.adapters.discord` | `packages/adapters/discord/` | Discord I/O, rendering, permissions, slash commands. |
| `daimon.adapters.slack` | `packages/adapters/slack/` | Slack I/O, Block Kit rendering, per-user OAuth. |
| `daimon.adapters.mcp` | `packages/adapters/mcp/` | The MCP server the agent calls, plus the OAuth, webhook and hub HTTP routes. |
| `daimon.adapters.scheduler` | `packages/adapters/scheduler/` | The routine poll loop. |
| `daimon.adapters.cli` | `packages/adapters/cli/` | The `daimon` admin binary. |
| `daimon.testing` | `packages/testing/` | Shared fixtures. |
| `mux` | `packages/mux/` | A separate namespace, not part of `daimon`. See [mux.md](mux.md). |
| `notebook_host`, `report_host` | `apps/*/src/` | Standalone services that talk to daimon over HTTP only. |

None of that is convention. Eight `import-linter` contracts in the root
`pyproject.toml` are the authority, run as `uv run lint-imports` in pre-commit
and CI:

| Contract | Forbids |
| --- | --- |
| Core must not import adapters | `daimon.core` → `daimon.adapters` |
| Adapters must not import each other | any of cli, mcp, discord, scheduler, slack → another |
| ORM module is private to stores and defaults | anything but `daimon.core.stores.**` / `daimon.core.defaults.**` → `daimon.core._models` |
| Core must not import testing | `daimon.core` → `daimon.testing` |
| CLI admin commands and run must not import each other | `daimon.adapters.cli.commands` ⟂ `daimon.adapters.cli.run` |
| Mux must not import daimon | `mux` → `daimon` |
| notebook-host must not import daimon | `notebook_host` → `daimon` |
| report-host must not import daimon | `report_host` → `daimon` |

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

## How a message becomes a turn

Discord and Slack run the same two-stage chokepoint in `daimon.core.turn`.
The staging is deliberate — neither stage returns a boolean, both raise typed
errors, so an adapter cannot forget a gate.

**Stage one, `admit()` — `packages/core/daimon/core/turn/admission.py`.** One
call does identity resolution, config resolution, and every pre-turn gate, and
returns a frozen `Admission` (account id, MA agent, MA environment, resolved
config). The order is load-bearing and documented as such in the module:

1. Resolve the platform user to an `accounts` row, via
   `get_or_create_platform_principal` in
   `packages/core/daimon/core/stores/identity.py`.
2. Invoker policy — the tenant's access policy (below) may restrict who can
   start a turn. A refused user raises `AdmissionDenied("invoker_not_allowed")`
   before the cascade, so they learn nothing about the tenant's configuration
   and no MA call is made.
3. Resolve config through the cascade
   `thread → channel → tenant → deployment`, in
   `packages/core/daimon/core/stores/scoped_config_read.py`. The tiers are
   named by `ConfigTier` in `packages/core/daimon/core/scope.py`; the bottom
   one comes from `defaults/config.yaml`, see [defaults.md](defaults.md).
4. Raise `MissingTurnConfigError` if no agent or environment resolved — before
   any MA call, so a misconfigured tenant sees the config error rather than a
   billing one.
5. Resolve the agent and environment to live MA ids via
   `packages/core/daimon/core/ma_resolver.py`, which self-heals by re-running
   defaults reconciliation when a tag no longer resolves, and rejects an agent
   whose `archived_at` is set.
6. Balance gate — `tenant_balance.is_over_balance`.
7. Monthly cap gate — `billing.is_over_cap`.

The policy, balance and cap gates each raise `AdmissionDenied` with a
reason literal; each adapter renders its own notice. See [billing.md](billing.md).

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
and skips the policy, as it skips billing. Ids are the platform's own (Discord snowflakes, Slack ids):

| Field | Empty means | Enforced by |
| --- | --- | --- |
| `invoker_user_ids` | anyone may start a turn; admins always may | `admit()`, the MCP turn tools (`_admit` in `tools/_ctx.py`), routine fires |
| `protected_channel_ids`, `protected_category_ids` | nothing is write-protected | not yet |
| `sealed_channel_ids` | nothing is sealed | `admit()` sets `Admission.memory_read_only` for a turn from a sealed channel or a thread under one |
| `dm_memory_read_only` (default `false`) | DM turns get writable memory | `admit(is_dm=True)` sets `Admission.memory_read_only` |

A sealed channel's content is readable only from a turn inside it (the channel
or a thread under it). There is no editor yet; operators write the row directly.

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

### The driver

`packages/core/daimon/core/turn/driver.py` opens the SSE stream, posts the
user message, and runs a consume loop and a render loop concurrently until the
session goes idle or errors. Adapters plug in through the `TurnLifecycle`
protocol in `packages/core/daimon/core/turn/lifecycle.py`, which documents a
per-hook cost contract:
`on_render` is the sole content-delivery path and may talk to the network,
because it runs on its own task and cannot stall the pump; `on_sse_event` is
awaited inline in the consume loop and must stay a cheap local tap.

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

### How a turn ended

`packages/core/daimon/core/turn/termination.py` defines `TerminationReason`,
one closed enum for every way a turn can end: it completed, the user stopped
it, the stream or MA failed in one of several named ways, the ceiling fired, or
admission or binding refused it before a driver ran. Each driver finalizer, and
both ceiling handlers, set `TurnState.termination` before the terminal hook
fires, so a lifecycle and the caller's `RunOutcome.termination` always agree.
Refusals raise before any state exists; `termination_reason(err)` maps the
exception the adapter caught to its member, and never raises: anything it does
not recognise is `unknown`. Two members have no exception behind them and are
set outside the mapper: `admission_concurrency_shed` by callers when
`should_admit_turn` refuses, and `recovery_failed` by `run_prepared_turn` on
the terminal hook when replacing a lost session raises (the exception it
re-raises maps to `unknown`). A session MA reports terminated without any terminal
event for this turn is `session_terminated`, never `completed`.
`TurnError.kind` is unchanged, and
every `TurnKind` value is also a `TerminationReason` value with the same
string.

`packages/core/daimon/core/turn/notices.py` turns a reason into a
`TerminationNotice`: a short headline, the cause, the tool work still running
and how much had finished, what survived, the next step, and a request id. The
copy lives in core; Discord and Slack draw it in `on_terminal_failure` as the
body of the red card, with the headline as the footer reason, and log the
request id with the underlying error so it is the handle for the detail. No
lifecycle hook carries it -- the reason rides on the state every lifecycle
already receives -- so the CLI, headless routines and any new adapter keep
their existing failure path, and `TerminationNotice.plain_text()` is the
fallback wording for a surface without markup.

### Outside text is data

Anything daimon quotes into a turn from someone other than the person asking
goes through one envelope, `packages/core/daimon/core/untrusted.py`: an
element marked `trust="untrusted"`, opened by a fixed line saying the content
is data, not instructions, with every value escaped so the content cannot
close the element early. The Discord and Slack context builders wrap replayed
thread history, deltas and channel backfill in it; `fetch_youtube_transcript`
returns its transcript in it; the quoted transcript on a workspace
replacement (`render_previous_session`) uses it too. The channel read and
search tools return JSON rows, so their results carry the same marker as
`trust` and `trust_note` fields instead. The paragraph in the agent guidance
block (`packages/core/daimon/core/agent_guidance.py`) tells every agent what
the marker means. Only the `<user_query>` is the request.

Third-party MCP tool results travel from Managed Agents straight to the model
without passing through daimon, so they carry no marker; the guidance
paragraph covers them by name ("whatever a tool returns").

## Tenancy and isolation

One Discord guild or one Slack workspace is one tenant. The tenant UUID is
derived, not allocated: `derive_tenant_uuid(platform, workspace_id)` in
`packages/core/daimon/core/ma_identity.py` is a UUID5 under a frozen
namespace, so the same workspace maps to the same tenant across database
resets and processes.

Isolation is enforced in two places at once.

**In Postgres.** Tenant-scoped tables carry `tenant_id` with a cascading FK to
`tenants.id`, and stores take `tenant_id` as a parameter rather than reading
it from anywhere ambient. Erasure is not left to cascades:
`packages/core/daimon/core/purge.py` deletes every row referencing a principal
in FK-safe order in one transaction, and
`packages/core/daimon/core/privacy.py` is its read-only mirror for the preview
panel. A schema-reflecting drift-guard test fails when a new person-scoped
table joins one path and not the other.

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

## Sessions and Managed Agents

A turn does not create a session per message. `thread_sessions` maps
`(tenant, platform, thread)` to one MA session id, and its `status` column
carries the lifecycle: `live` is the caller's current session, `dead` is one MA
no longer has, `superseded` points at the successor that carries its work
through `replaced_by_id`, and `retired` is an explicit fresh start. That
lineage is why a thread can survive a session replacement with its context
intact.

What MA holds is the agent, the environment, the skills and the session
transcript. What Postgres holds is metadata: identity, the mapping above, the
config cascade, credentials and billing. An MA session freezes its agent spec
at creation time, so the agent read at admission is not necessarily what
executes — the mapping row stores a `SessionSnapshot` of the configuration the
session is actually running (`packages/core/daimon/core/session_snapshot.py`),
and that snapshot, not the current spec, is what later turns compare and bill
against.

## Entry points that are not a chat message

- **Scheduled routines** go through
  `packages/core/daimon/core/headless_runner.py`, which creates a session with
  the same `create_session` the chat path uses and delegates the drain to the
  same driver under the same ceiling — but it calls neither `admit()` nor
  `bind_session()`. See [routines.md](routines.md).
- **MCP agent-chat tools**, in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py`, let a caller
  drive a session directly. They do not use the chokepoint either; they re-run
  the same balance and cap gates through `_admit` in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py` and create
  sessions via `daimon.core.sessions.create_session`.
- **`daimon run`**, in
  `packages/adapters/cli/daimon/adapters/cli/run/command.py`, is a single-turn
  subprocess entry point that calls `run_turn` directly with `BillingExempt`.
- **Wakes** run a turn in an existing thread later, with nobody mentioning the
  bot: a handoff's first turn for the new agent, work unblocked by a private
  form, a one-shot timer (`daimon.core.continuity.timers`, the `create_timer`
  tool), and anything else queued through `daimon.core.continuity.wakes`. A wake is
  a `task_continuations` row. The Discord and Slack adapters each run a wake
  poller (`run_wake_poller`) that opens threads with due rows and hands them
  to the adapter's continuation dispatch. That dispatch takes the thread's
  turn guard and goes through `admit()`, `bind_session()` and
  `run_prepared_turn()` like a mention, so the balance and cap gates apply.
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
  A timer only runs as the agent it was set with. If the thread answers to
  another agent when the timer fires, the adapter refuses it after
  `admit()` and before anything is bound or billed. It settles the row
  `skipped/skip_target_changed` and posts a notice in the thread.

If you add another, reuse `admit()` rather than re-deriving the gate order.

## Standalone apps

`apps/notebook-host/` serves published marimo notebooks, spawning one
`marimo edit` subprocess per notebook behind a reverse proxy.
`apps/report-host/` serves one published PDF report with a chat sidebar.
Both are FastAPI processes that hold no Anthropic key and no database
credential; they reach daimon over HTTP with capability tokens, and the
`must not import daimon` contracts keep it that way.

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

### Google tokens in ordinary chat

Chat sessions attach a per-account, per-agent vault whose signed JWT carries
`chat_agent_id`, derived from the tenant and Managed Agent ID. MCP resolves this
as the executing agent identity for `get_cli_token(service="gcloud")`, while
preserving the ordinary chat tool surface and live account-role checks. The
separate `agent_id` claim still selects the restricted external agent-chat surface.
Neither claim is supplied through tool arguments. Chat identity is stored separately
from `AuthIdentity.agent_id` and is consumed only by the Google broker path. GitHub
chat calls still resolve the account principal-default PAT; other identity gates
and the two-tool search interface remain unchanged.

The operator must configure `credentials.google_sa_json`, authorize domain-wide
delegation, and bind the agent with `daimon agents bind-google <agent> <email>
--scopes <scope>...`. The broker impersonates only that agent's bound Workspace
user and scopes; an unbound agent receives a clear operator-binding error.
Core does not ship curated Workspace tools. Agents may use the token themselves
or a deployment-provided Google MCP server.

Existing static-bearer vault credentials are upgraded in place on the next
session creation. Credential metadata records the identity version so subsequent
creates leave the token stable; unrelated and OAuth credentials are preserved.

### Completion signals

The core driver calls an optional `on_acknowledgment` lifecycle hook with
`accepted` after the initial event send and `done` after successful answer
delivery. Missing hooks are no-ops; reaction failures are bounded and do not
fail the turn. Opted-in Discord and Slack tenants react with eyes, then a check
mark on success. Unprompted Discord turns stay silent; failures and cancellation
do not get a completion marker. Continuations without a trigger message skip
reactions.

Set `DAIMON_COMPLETION_PINGS` to a JSON object keyed by tenant UUID, for example
`{"00000000-0000-0000-0000-000000000001": true}`, to deliver that tenant's final
answer as a fresh thread reply mentioning only the requester. Missing or false
entries keep the existing in-place answer and reactions (none on Discord; Slack keeps its admission eyes). Slack admission adds eyes once; the lifecycle only replaces it on opted-in completion. Recovery lifecycles retain this policy;
continuity notices and feedback target the new answer. Other adapters need no changes.

### Routine dispatch

The scheduler owns a persistent, bounded routine dispatcher across ticks.
Routine turns run independently of the tick; the same routine cannot overlap
itself. Per-routine missed-run policy and the latest skipped range are exposed
by the routine MCP tools. See [routines.md](routines.md) for catch-up and shutdown.

### Agent-initiated direct messages

The shared channel tool `send_direct_message(recipient_id, content)` dispatches
to Discord or Slack under the authenticated tenant. Both sender and recipient
are checked for current platform membership before a DM is opened. Discord bot
recipients and Slack inactive, external, or bot users are rejected. Other
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

Session memory mounts are read-only for sealed channels (including their threads),
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
### Platform table rendering

Enable per tenant with `DAIMON_TABLE_RENDERING`, a JSON map of tenant UUIDs to
booleans. Missing/false preserves current plain-text delivery. UUID keys are
validated at startup. Core `tables.render_tables` parses pipe-delimited Markdown tables outside fenced
code and accepts an optional async platform hook. Without a hook the input is
returned unchanged. Both adapter helpers also default to disabled. Hook failures
and oversized tables retain their raw text. Rejected Discord attachment edits
and Slack table blocks are logged and retried as the original Markdown; Slack
keeps already-delivered chunks in place and retries only the rejected table.
The shared bound is 20 columns, 100 rows including the header, and 10000 cell
characters; at most ten tables render per answer.

Discord renders final-answer tables off-thread as PNG attachments using bundled
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



### Durable turn outcomes

`turn_outcomes` stores one content-free terminal record per logical turn. A UUID
follows admission, session binding and execution; a dead-session recovery remains
one turn. Discord, Slack, their continuation/wizard paths, headless routines and
CLI runs use the same recorder. Admission and concurrency refusals are recorded
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
