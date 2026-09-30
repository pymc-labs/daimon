# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Encrypt agent environment values with rotatable deployment keys when configured, including existing rows on upgrade. Store encoding separately from user text so every literal value, including `enc:v1:` prefixes, remains valid. A database trigger keeps writes from older code tagged as plaintext; decryption errors identify the affected row without exposing values.

### Upgrade notes

- Agent environment encryption is opt-in through `DAIMON_CRYPTO__KEYS`; keyless deployments retain plaintext storage and initialization still succeeds. Stop old readers/writers before the migration when enabling encryption. Keep keys available for reads and reversible downgrade; see `docs/self-hosting.md`.

### Added

- Discord delivers files generated during tool-using turns into the chat thread,
  with upload-limit notices and protected-channel checks.
- Optional Discord process-wide turn limit for guild chats and DMs. Excess
  requested turns get a retry notice; surfaced Anthropic 429/529 responses
  emit structured logs.
- Operators can credit tenants with `daimon tenants credit --note` and set default
  or per-person monthly caps with `daimon tenants cap`. Caps apply without Stripe;
  prepaid Discord and Slack turn footers show the remaining balance.
- Operators can set a tenant's concurrent chat-turn cap with
  `daimon tenants turn-cap`, or clear it to use the deployment default.
- Record content-free turn outcomes across chat, headless, routines and MCP hub/agent-chat, including attributed admission refusals, with bounded best-effort persistence. MCP `ask` records its terminal reason; fire-and-forget `start_turn`/`continue_turn` record dispatch only (`unknown`), without a later terminal update. Pre-attribution and adapter readiness gates are outside coverage.
- Routines can name an optional destination channel or thread
  (`create_routine`/`update_routine` `destination_kind` + `destination_id`,
  `clear_destination`), validated on save against the caller's own server or
  workspace. The run is told where its result goes, and if the agent does not
  post there itself, the Discord or Slack adapter posts the result, at most
  once, after checking the tenant's protected channels and invoker allowlist.
  A protected or unreachable destination sends the result to the routine's
  creator by direct message instead. Routines without a destination behave as
  before.

- Per-turn token, cache and estimated provider-cost telemetry shares the terminal outcome row; operators can query tenant usage by channel and origin with `daimon usage turns`. MCP SDK polling outcomes retain unknown usage rather than zero. Billing and admission behavior are unchanged.


- Opt-in Discord and Slack DM conversations: admins enable with `/dm enable`;
  `/dm` moves recent channel context into a private, resettable session. Every
  private turn checks live membership and the tenant invoker policy. Privacy
  preview and deletion include bounded local DM context. Slack checks IM scopes
  before setup and uses signed execution grants in isolated session vaults.
  Private session transcripts and controls require that exact grant, preventing
  same-account routines, channel turns and MCP callers from borrowing access.
  Slack private turns use fresh sessions with bounded history replay.

- Every turn now ends with a typed `TerminationReason` from the turn core,
  set on `TurnState.termination` and `RunOutcome.termination` for every driver
  exit and derivable from admission and session-binding refusals with
  `termination_reason(err)`. Notices and outcome records can build on one
  vocabulary.
- When a turn ends early, the red Discord card and the Slack error card now
  explain it: what happened, which tools were still running, what was kept,
  what to do next, and a request id to find the error in the logs. The raw,
  truncated error text no longer appears in the card.

- Memory mounts are read-only for sealed-channel turns, routines, and DMs when
  the tenant policy requests it. Session reuse enforces policy changes before
  another turn runs, including wizard submissions in sealed threads.
- Security audit writes run in bounded background tasks. Privacy deletion erases user identifiers, tenant deletion removes audit rows, and `daimon audit prune` applies configurable retention (90 days by default). Tool errors are distinguished from authorization denials.
- Database deletion triggers erase audit identifiers even when older privacy workers delete accounts or tenants during a rolling upgrade.
- The security audit covers the main JWT MCP application. Separate hub OAuth applications (`/discord/mcp`, `/slack/mcp`) and adapter setup panels are excluded in this version.
- Authenticated main-JWT MCP calls and listings now append tenant-scoped security audit metadata, including shared operation-policy decisions. Operators can query and export it with `daimon audit list`; database guards prevent ordinary updates, deletes and truncation.

- Core selects chat, routine, relay and handoff prompt fragments per invocation.
  Agent specs can replace or extend each fragment; default chat is unchanged.
- Per-tenant operator-funded mode replaces depleted-balance refusals with structured alerts while preserving usage metering and configured caps. Configure it with `daimon tenants funding-mode`.
- Optional Postgres backup service, checksum-verified empty-database restore and
  isolated restore drill; workspace-wide Managed Agents object export and a
  state-by-state disaster recovery contract in the self-hosting guide.
- Routine dispatch runs independently of scheduler ticks, with bounded in-flight tasks, per-routine `skip`/`run-once` catch-up policies, and visible skipped-slot ranges.
- Tenants can opt into accepted/done reactions and a fresh final reply that pings
  only the requester on Discord and Slack with `completion_pings`; defaults stay unchanged.
- `daimon tenants access-policy get|set PLATFORM EXTERNAL_ID` shows and edits a
  tenant's access policy (invoker allowlist, protected channels and
  categories, sealed channels, DM memory); `--clear` puts the tenant back on
  the open default. IDs are validated per platform before writing, and concurrent
  edits preserve fields changed by other CLI commands.
- Sealed channels: the channel read tools refuse a channel the tenant access
  policy seals, and threads under it, unless the call passes the
  `origin_context_id` of a turn inside that channel and the token executes
  as that turn's agent. A single Discord thread, or a Slack thread keyed
  `channel_id:thread_ts`, can be sealed on its own. Search withholds sealed
  hits and, once anything is sealed, counts only what it shows. The read
  tools gain an optional `origin_context_id` parameter.
- Protected channels: the agent, including its own replies, never writes into
  channels, threads and Discord categories the tenant access policy marks
  protected, for admins as well. A mention there is dropped silently (no
  thread, reply or upload), and the write tools (send message, create or
  rename a thread, credential, wizard and app-install cards) refuse them.
  When the policy can't be read (including a database outage) the agent
  stays silent in that channel rather than posting an error there.
- Tenant access policy: a tenant can limit who may start a turn to a list of
  platform user ids (admins are always allowed). Discord and Slack refuse
  anyone else at admission with a notice, and so do the MCP and hub turn
  tools; routines whose creator is no longer allowed skip with
  `invoker_not_allowed`. Tenants without a policy are unchanged. The policy also carries protected and sealed channel lists for
  the channel tools.
- Opt-in write safety for attached third-party MCP tools
  (`DAIMON_TOOL_SAFETY__ENABLED`, off by default). Each tool is classified read
  or write (operator override, then MCP annotations, then its name; unknown
  means write). In chat, a write waits for the requester to press Approve on a
  Discord or Slack confirmation card showing the exact input; in routines,
  writes are refused unless listed in `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`;
  `DAIMON_TOOL_SAFETY__DENIED` blocks a server or tool everywhere. The
  confirmation card is a reusable `ConfirmationHook`; surfaces without one
  refuse the write.
- `update_agent` now refuses to re-point the reserved `daimon-mcp` server or to
  register the deployment's own MCP endpoint under another name, matching
  `attach_mcp_server`.

- `send_direct_message` delivers private agent messages to verified Discord/Slack
  tenant members, with per-tenant disabled/allowlist policies and delivery receipts.
- A durable wake queue runs a turn in an existing thread later, at most once,
  through the same admission, bind and run path as a mention. The Discord and
  Slack adapters poll for due wakes. A wake whose process dies before its turn
  starts is retried when the lease expires. One that dies after the turn
  starts is settled `interrupted` and never re-run. Migration
  `0028_feat003_wake_queue` adds the lease columns to `task_continuations`.
- Tenants can opt in to final replies rendering Markdown tables as readable PNG attachments on Discord
  and native wrapped table blocks on Slack, with plain-text fallback for other adapters.
  Discord wizard replies honor the same opt-in; unsupported font glyphs retain the original Markdown.
  Rejected PNG uploads and Slack table blocks retry as plain Markdown without dropping the answer.

- One-shot timers: `create_timer`, `list_timers` and `cancel_timer` let an agent
  come back to a conversation once, at a set time, with a note it left itself
  ("remind me in two hours"). A timer runs in the thread it was set in, as the
  person who asked for it, and goes through the wake queue. A cancelled timer
  never fires, and a timer whose thread now answers to a different agent is
  skipped with a notice instead of running under that agent. Migration
  `0029_feat084_timers` adds the `timer` reason; deploy it and timer-aware
  Discord, Slack and MCP builds before anyone can create timers (see
  `docs/architecture.md`). Downgrading it deletes all timer rows.
- Added durable initial-card intent rows and bounded Discord and Slack history
  lookup. Both adapters now commit an intent before posting, record the
  returned message ID, and reconcile unresolved cards after a restart. A
  bounded TLA+ model retains the stale-edit, new-intent snapshot, and lossy
  history counterexamples.
- The scheduler prunes retired initial-card intents after seven days, at most
  500 rows per tick; active intents remain available for restart recovery.
- GitHub push-triggered skill resyncs now persist before webhook acknowledgement,
  recover through scheduler leases, retry transient binding failures with
  backoff, and retain permanent binding errors for operator action. See
  `docs/github-push-resync.md` for delivery and concurrency limits.
- Bounded TLA+ models and a source-linked coverage report for turn rendering,
  scheduling, session preparation, continuations, adapter recovery, billing,
  wizard submission, and recovery transaction atomicity.
- A bounded report-publish model checks that the visible PDF, bundle, digest,
  and retry archive stay coherent across seam failure and the local commit.
- A bounded GitHub push resync model checks durable acknowledgements,
  generation-fenced completion, crash duplicates, and fair crash-free progress.
- TLA+ models for usage metering (live recorder, usage sweep, balance gate)
  and Slack event dedupe and redelivery, each calibrated against earlier bug
  fixes. `docs/billing.md` now states the overdraft bound for concurrent and
  MCP-started turns, the sweep's attribution, and why deployments must not
  share a Managed Agents workspace.
- A bounded model and signed PostgreSQL tests capture out-of-order GitHub App
  installation repository events and the unresolved need for reconciliation.
- GitHub App installation webhooks now queue a durable repository refresh.
  The scheduler fetches all pages from GitHub and commits only the current
  generation; existing installation caches are queued on migration.
- A bounded TLA+ model for notebook upload capability replay, restart persistence,
  and the loss window after a token is burned but before its body is read.

### Changed

- **A tidier status card while a turn runs.** Discord shows one embed instead
  of two and Slack one matching card: a bold Thinking or Working headline with
  the elapsed time, up to six recent tool calls in a code block, and the latest
  draft quoted underneath. Built-in tools read as plain verbs ("Reading a
  file", then "Read a file"), MCP and custom tools get readable names, finished
  calls are ticked, failed ones marked, and older calls fold into "+N earlier".
  The list now includes MCP and custom tool calls and still never shows tool
  arguments. The finished-turn summary and the error card are unchanged.

- A handoff or private-input continuation whose process dies mid-dispatch is
  no longer stuck in `claimed`: it is retried if its turn had not started, and
  settled `skipped/interrupted` if it had. When the session is busy, the same
  row goes back to pending instead of being queued again under a new key. It
  still waits for the next turn in the thread, and busy retries stay
  unlimited. A continuation whose process dies five times before its turn
  starts is settled `skipped/attempts_exhausted`, and nothing is posted to the
  thread.

- `get_agent` now returns the agent's `system` prompt to an admin caller on
  an agent chat tools may edit, so a setup flow can save the prompt before
  replacing it and verify the change afterwards. Non-admin callers, and every
  caller on Daimon or another defaults-managed agent, get `system: null`.

- The seeded `pymc-artifact-style` skill now follows the live pymc-labs.com
  palette and type (re-derived from the site CSS on 2026-09-28): it adds the
  site's readable text accents (teal, indigo, dark orange) and navy-header,
  uses Inter 600/500 headings with the site's tracking, bundles Inter Medium,
  and drops the legacy Archivo and Fira Mono fonts, cover art, old logos and
  the non-website chart variants.
- GitHub App installation-token mint rate limits during bound skill resync now
  defer the durable queue job using GitHub's retry deadline; permission 403s
  remain permanent. See `docs/github-push-resync.md` for the covered request
  paths and remaining scope.
- Opus 5.5 can be selected for an agent and is metered at its published rates.
  The seeded and new-agent defaults remain Sonnet 5.
- The documentation site carries daimon's own look: the readme sticker as
  logo and favicon, and a palette taken from it.
- Defaults reconciliation serializes writes and sweeps per tenant so concurrent
  callers cannot create duplicate seeded resources or archive an ID another
  caller has already resolved.

### Fixed

- Stop retrying Anthropic's monthly spend-cap response and show a clear model usage limit notice in Discord and Slack.

- The MCP server's hub login store opens one database connection at startup and
  grows to four, instead of holding ten per instance. During a deploy the old and
  new revisions no longer exhaust a small Cloud SQL tier's connection slots.

- Fresh Discord installs and boot reconciles share a bounded seed queue. Skills API
  calls are paced across the adapter process and retry temporary rate limits;
  defaults reconciliation lists workspace skills once per tenant instead of once
  per skill.

- Follow Anthropic Skills API cursors across multiple pages. A full final page
  without a cursor still fails closed before skill writes or deletes.

- Direct-message policies normalize tenant UUID keys and reject invalid keys at
  settings load, so restrictive policies cannot silently miss their tenant.

- Ordinary chat sessions carry their executing agent identity to the Google token broker,
  including existing vaults on the next session creation, while preserving chat tool
  visibility and caller isolation.
- Slack direct-message errors explain the required `im:write` scope and workspace
  admin reinstall for missing scopes or invalid authorization, preserving the
  count of messages already delivered.

- A continuation turn (the follow-up after a private form or a task handoff)
  on Discord or Slack now runs with the requester's live role, re-read from
  the guild or workspace at dispatch, instead of always as a plain user. An
  admin's setup run no longer loses its admin tools on the turn that applies
  their answer; a non-admin's form, or a failed role lookup, still runs as a
  user.

- Bound GitHub skill resyncs now preserve the binding's exact Managed Agents
  identity through ledger updates and skill attachment. Duplicate active agent
  names already present at bridge resolution refuse the resync before
  credential selection or repository fetch. Duplicates observed later refuse
  before MA or ledger writes, including orphan deletion; errors remain visible
  as permanent binding failures.
- Rate-limited GitHub tarball downloads now remain retryable, and durable push
  resync waits for GitHub's `retry-after` or reset deadline before claiming the
  job again. Other 403 permission failures remain permanent.
- Discord setup Details now drops superseded reads and binds setup and
  coding-tool actions to the agent shown on the card. A bounded TLA+ model
  retains the pre-fix wrong-target traces.
- Discord guild removal and rejoin transitions are serialized per guild, and
  delayed lifecycle work checks the current gateway cache before changing the
  tenant. Startup now revives archived tenants for guilds still joined without
  posting another welcome or issuing signup credit again.
- Slack shutdown now waits for acknowledged mention handlers that are still
  preparing a turn, closing the graceful-drain loss window before thread
  registration. A hard process crash after acknowledgement can still lose work.
- A Cancel click on a newly posted Slack status card is routed while Slack's
  `chat.postMessage` response is still pending; after the response, routing
  follows the message timestamp so recovery can rebind the active turn.
- A malformed GitHub installation creation payload with a present non-array
  `repositories` value no longer clears the existing repository cache.
- A malformed tenant tag in one Managed Agents session no longer aborts the
  usage sweep; malformed optional account metadata drops member attribution
  while preserving tenant usage and its debit.
- The usage sweep no longer attributes a session to a platform user from a
  different tenant when its account metadata points across tenant boundaries.

- The scheduler's usage sweep no longer debits a tenant for `BillingExempt`
  usage: MCP turns started by a caller with no platform user (an operator,
  CLI or internal token) and headless runs with no recorder. Such sessions are
  now stamped `daimon_billing_exempt` when created, the sweep skips them, and
  the operator absorbs their cost. The sweep logs each skipped session's
  would-be cost as `usage_sweep.exempt_skipped` and totals it per pass in
  `usage_sweep.completed`. See `docs/billing.md`.
- A session recovery that is rolled back (by the turn time limit or a
  cancel) after creating its replacement session now archives that session
  instead of leaving it running upstream with nothing pointing at it. The
  archive wait is bounded, its eventual result is logged, and repeated
  cancellation does not replace the error that caused the rollback.
- A failed report publish keeps the reader's current PDF paired with its
  accepted bundle. Each upload archive is stored separately, so a failed push
  cannot replace the archive used to recover that bundle. A later publish
  prunes unreferenced archives after at least 24 hours and the configured turn
  deadline.
- Concurrent GitHub App repository add/remove updates no longer overwrite
  changes from another delivery.
- GitHub App suspension, unsuspension, and permission-change events no longer
  clear the cached repository list. Only installation creation replaces it.
- A pending report publish keeps its archive while another publish prunes old
  uploads, so a delayed seam response cannot commit a missing archive reference.
- A session loss recovered right at a turn's time limit no longer leaves a
  replacement session behind that the next mention silently continues on
  without being told the previous work was lost.

- Slack now keeps a substantive answer when the agent uses a tool after writing
  it, including turns that end without a further reply.
- When two turns in one thread (for example a Discord form submit and a
  mention) both find the thread's session gone, they now continue on one
  replacement session. Previously each created its own, leaving the thread
  with two active sessions and one turn's work in a session later messages
  never reached.
- A Discord mention sent into a thread while its turn is finishing is no
  longer left unanswered until the next mention, and a failed ⌛ reaction
  (for example, a missing Add Reactions permission) no longer drops the
  queued mention.
- Finishing an MCP OAuth sign-in no longer fails with "Sign-in did not
  complete" when one of your turns re-copies the agent's shared token for the
  same server at that moment; the sign-in replaces it and is kept. That turn
  also no longer fails when the sign-in replaces the token it was updating.
- That sign-in is now kept however many of your turns with the same agent are
  running while it finishes. With three or more of them it could still fail
  with "Sign-in did not complete".
- Reinstalling daimon in a Slack workspace that had uninstalled it leaves the
  workspace live again. The uninstall's archive stamp used to survive the
  reinstall, so the hub and the boot defaults sweep kept treating the workspace
  as gone; and an uninstall event delivered late, after the reinstall, no
  longer deletes the fresh bot token.
- A private form submitted in Slack or Discord just as the thread's turn was
  finishing now resumes the task that was waiting on it, instead of waiting
  for the next message in the thread.
- A Slack or Discord mention sent into a thread while a task resumed by a
  private form is running there now gets its own reply once that task
  finishes, even when resuming the task fails (Slack posts an apology in that
  case). It used to keep its ⌛ reaction and wait for the next mention in the
  thread.
- After a restart cuts off a turn in Slack or Discord, the next message in that
  thread gets its own reply instead of the cut-off turn's answer: the boot
  sweep that marks the turn as interrupted now also stops it on Managed
  Agents, so it no longer keeps running and billing after its card says it
  was interrupted.
- A Discord mention answered in the first seconds after a restart, before
  the bot has finished connecting to every server, is no longer mistaken for
  a turn the restart cut off: its card is no longer marked as interrupted
  while it is still running.
- Reconnect replay keeps the current turn's answer and rendered content when
  the event history is incomplete or ends with a session termination; repeated
  SSE events no longer repeat adapter callbacks.
- Stripe Checkout credits once per payment intent. Concurrent refunds and
  disputes cannot claw back more than the original credit, and a refund that
  arrives before Checkout completion is applied when the credit appears.
- Discord and Slack orphan recovery no longer clears a newer active turn;
  Slack retries a failed startup sweep before admitting turns.
- A stale wizard submit cannot replace newer answers, and expiry cannot
  abandon an already submitted session.
- A per-agent MCP token (coding tools) now reaches only the sessions its own
  account started with that agent: `list_my_sessions`, `get_my_session`,
  `list_events`, `continue_turn`, `ask`, `cancel_turn`, `archive_my_session`,
  `get_turn_cost` and `deliver_turn_charts` no longer see other workspace
  members' sessions of the same agent.
- A model call made while a turn's event stream was disconnected or stalled
  is metered by that turn when it replays the session history, under the
  turn's own member and ledger reason, instead of waiting for the usage sweep
  (or going unmetered where no scheduler runs).

### Security

- Text daimon quotes from outside the request now arrives marked as data:
  replayed thread and channel messages on Discord and Slack sit inside
  `trust="untrusted"` envelopes, YouTube transcripts come back wrapped, and
  the channel read and search tools mark their results. Every agent's
  guidance block gains a paragraph saying such text is data, not
  instructions; agents pick it up at their next reconcile or edit.
- GitHub App installation tokens are minted for the one bound repository
  instead of every repository in the installation, and read-only when the
  binding was verified as a public repo or the token is used for skill sync.
  If you bound your own public repository with `bind_public_repo` and the
  GitHub App is installed on it, the agent can no longer push to it through
  that binding: its token is now read-only. To let the agent push, rebind the
  repository with a personal access token (the repo credential form in Discord
  or Slack).

## [0.2.0] - 2026-09-21

Everything since the first release. Slack catches up with Discord across
setup, credentials, feedback and file delivery; conversations survive a
restart and can hand work to a fresh session; charts and published reports
get a delivery path of their own; and self-hosting is documented end to end.
140 pull requests, 23 schema migrations, two new workspace packages.

### Breaking

- **`POSTGRES_PASSWORD` has no default.** Compose refuses to start without
  it. Set it in `.env` before upgrading.
- **Postgres and the notebook host publish on `127.0.0.1` only.** Anything
  that reached either from another host now needs a tunnel or its own
  published port.
- **The scheduler starts through the `daimon-scheduler` console script**,
  not `python -m daimon.adapters.scheduler`. Compose is updated; custom
  process definitions are not.
- **MCP tools `generate_image` and `generate_audio` are removed.** The
  `pydub` dependency went with them.
- **MCP tools `create_blog_upload_url`, `delete_blog` and `list_blogs` are
  removed.** Notebooks are one tool plus `list_notebooks` and
  `delete_notebook`; published documents use `publish_report` and
  `delete_report`.
- **The duplicate `skills_*` aliases are removed.** Use `sync_skills`,
  `list_skills`, `get_skill` and `delete_skill`. No compatibility aliases
  are kept.
- **`DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN` is removed.** It made the admin
  check return true before `users.info` was called, opening every Slack
  admin gate for every member of every install on the deployment. Unknown
  keys are ignored, so a deployment still setting it boots and starts
  enforcing; promote a real workspace admin for any account that relied on
  it.
- **The seeded skill `marimo_blog` is gone**, replaced by
  `marimo_notebooks` and the report skills.
- **The root `compose.notebook.yml` and `compose.worker.yml` are deleted.**
  `docker-compose.yml` is the supported stack.
- **Changed defaults:** agent model `claude-sonnet-4-6` → `claude-sonnet-5`;
  `DAIMON_BILLING__SIGNUP_CREDIT` 5.00 → 10.00;
  `DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S` 600 → 3000, an outer process guard
  that now sits above the core's ~45 minute turn ceiling rather than below
  it; `DAIMON_PRIVACY_POLICY_URL` points at this repository's `PRIVACY.md`.
- **Minimum `anthropic` SDK is 0.117** (was 0.96), and the root
  distribution is named `daimon` (was `daimon-cma-open-source`).
- Upgrading runs **23 migrations**. Every one declares `downgrade: safe`,
  and none drops a table or column.

### Added

#### Agent and sessions

- Session continuity: a thread's work survives the session behind it being
  replaced, through snapshots, replacement lineage and continuations queued
  and dispatched back into the thread.
- `start_fresh_task` and `hand_off_task` — start clean, or move the current
  task to a new session with its context.
- `cancel_turn` stops a running turn; `get_turn_cost` reports what one cost.
- `explain_agent_resolution` answers which agent replies in a channel and
  why.
- Per-agent memory stores, with a `daimon memory` CLI group.
- Opus 5 in the pricing table; the agent model allowlist is scoped to
  Anthropic models.

#### Discord

- Structured mid-turn forms with typed answers (`post_wizard`).
- Reaction feedback votes with a private free-text follow-up.
- Opt-in organic thread participation, off by default
  (`DAIMON_THREAD_PARTICIPATION__*`).
- Automatic thread titles and `rename_thread`
  (`DAIMON_THREAD_NAMING__*`).
- `set_display_identity`: the agent can change its own nickname and
  per-server avatar when an admin asks.
- Human-support escalation, metered per user (`DAIMON_SUPPORT__*`).
- Nominated QA bots may start turns by mention
  (`DAIMON_DISCORD__QA_BOT_USER_IDS`).
- A read-only setup panel listing the agent roster, details and routing.

#### Slack

Most of this release's parity work: Slack now matches Discord on the
surfaces below.

- Feedback votes on the final answer, chat-initiated credential buttons and
  single-use private modals.
- Output file delivery (requires the `files:write` scope).
- `send_message` posts to channels as well as threads, and `parse_link`
  resolves permalinks.
- Tools that only apply to the other platform are hidden from callers.
- Cross-process turn liveness, a boot sweep and recovery-card adoption, so a
  restart no longer strands a thread.
- A read-only setup panel and targeted setup conversations.
- Self-hosted Slack setup documented, with an app manifest and a compose
  profile.

#### Setup, credentials and repositories

- Posted control cards on both platforms: private forms, card states that
  tell the truth, `.env` import, agent model chosen at creation, and the
  provenance of a request recorded when it is minted.
- `set_setup_target` aims setup at a specific conversation.
- GitHub App install-link cards on both platforms
  (`DAIMON_GITHUB__APP_SLUG`).
- Repository binding from chat (`bind_public_repo`, `request_repo_binding`),
  with proof of access taken at bind time and enforced on every write.
- Skill-repo tokens are stored apart from the working-repo binding, so
  enrolling a skill repository no longer re-points the clone target.
- A per-person connect flow for OAuth-only MCP servers
  (`request_mcp_oauth`), and `detach_mcp_server` to undo it.

#### MCP surface

- `/slack/mcp` and `/discord/mcp` OAuth hub mounts, plus a Claude Code
  plugin in `plugin/` that reaches every daimon a person can see
  (`DAIMON_HUB__*`).
- `create_file_upload_url` takes a file by URL instead of base64 tool
  arguments, and `PUT /bundles` streams bundle uploads under an hourly
  per-token limit (`DAIMON_MCP__BUNDLE_*`).
- 33 net-new tools in total; `.env.example` and the tool list are the
  reference.

#### Reports, notebooks and charts

- New app `apps/report-host`: a per-recipient report reader with chat, admin
  publish, delete and revoke routes, reader-variant agents, and sweeps for
  restart-resume, deadline-cancel, idle-archive and recipient-prune
  (`DAIMON_REPORT_HOST__*`, and the app's own `DAIMON_REPORT__*`).
- `publish_report` and `delete_report`, with `report-publish` and
  `report-reader` seeded skills.
- Charts produced during a turn are delivered from S3-compatible storage
  (`deliver_turn_charts`, `DAIMON_ARTIFACTS__*`).
- Notebooks are one tool plus `list_notebooks` and `delete_notebook`, each
  notebook isolated in its own filesystem jail on the host.

#### Skills

- Newly seeded: `workspace-setup`, `file-handling`, `pymc-artifact-style`,
  `report-publish`, `report-reader`, and a data-analysis set
  (`data-ingestion`, `data-cleaning`, `data-validation`,
  `exploratory-data-analysis`, `eda-storytelling`).
- `sync_skills` can attach to an agent and refuses unmetered models; chat
  can collect a token for a private skill repository; `remove_skill`.

#### CLI

- New commands: `daimon memory`, `daimon repo-bindings`, `daimon smoke`,
  `daimon defaults verify`, `daimon mcp mint-agent-token`, `daimon agents
  bind-google`.
- `--tenant` / `--guild` overrides on `agents`, `skills` and `config`, and
  `config --scope` accepts Slack scopes.

#### Self-hosting

- `docs/self-hosting.md` covers prerequisites, environment, the Discord app,
  the stack, Slack, Claude Code login mounts, chart delivery and connecting
  external MCP servers. The readme is rewritten around a three-step
  quickstart, and `PRIVACY.md` is new.
- Tagged releases publish a multi-architecture image to
  `ghcr.io/pymc-labs/daimon`, so the first run is a pull rather than a
  build.
- New settings blocks a self-hoster may need: `DAIMON_HUB__*` (its
  `JWT_SIGNING_KEY` must be 32 bytes once any mount is configured),
  `DAIMON_ARTIFACTS__*`, `DAIMON_REPORT_HOST__*`, `DAIMON_SUPPORT__*`,
  `DAIMON_THREAD_PARTICIPATION__*`, `DAIMON_THREAD_NAMING__*`,
  `DAIMON_MCP__BUNDLE_*` and `DAIMON_GITHUB__APP_SLUG`. All are optional
  unless the feature is wanted, and `.env.example` lists every one with its
  default.

### Changed

- One admission, session-bind and prepared-turn path now drives Discord,
  Slack and MCP, so behaviour that used to differ by adapter no longer does.
- Agents seeded from `defaults/` cannot be deleted on either platform. Both
  adapters refuse server-side; the Discord panel's disabled button was
  client-side only and the Slack panel offered deletion outright.
- Reading a routine's last run output needs the same authority as pausing or
  deleting it: workspace admin, or the routine's creator.
- `create_environment` is no longer gated.

### Fixed

- The Discord Details card's coding-tools token now stays scoped to its exact
  MA agent ID and refuses archived, missing, or foreign-tenant identities
  instead of substituting a newer agent with the same name.
- Threads no longer freeze. Dead sessions are recovered, a dropped
  mid-stream connection reconnects, turns orphaned by a restart are retired,
  and liveness is tracked across processes rather than per worker.
- One failing or unauthenticated MCP server no longer discards the whole
  reply: a session mounts only the servers its caller can authenticate, and
  a forked agent drops what it cannot.
- Channel and thread history pagination is bounded and flags truncation
  instead of quietly returning a partial view.
- Rotated MCP credentials propagate in place and reach every caller.
- Skill sync is hardened: decompressed tarballs are size- and member-capped,
  paging goes past 100 entries, the boot sweep no longer exhausts the Skills
  API rate limit, and duplicate mount names are refused.
- Billing errors are honest: the Slack top-up modal answers when checkout
  fails, error rendering no longer surfaces SQL, and pooled database
  connections are pre-pinged.
- Wiping a workspace is refused unless the workspace is marked disposable.

### Security

- Slack mentions queued behind an in-flight turn are partitioned by author,
  one turn per caller. The whole queue used to be coalesced into a single
  turn run as the first queued author, so a second member's instructions
  executed inside the first member's session, under their credentials and
  visibility, billed to them.
- `tokens_revoked` no longer tears down the install unless the event names
  the bot token. Slack also emits it when a single member revokes their own
  user token, which meant one member disconnecting could uninstall the app
  for the entire workspace.
- Slack refreshes the caller's admin role on every mention and re-checks it
  at click time; a failed lookup does not overwrite a stored role. Both
  adapters carry caller admin status into turn context, and Discord turn
  replies suppress mass mentions.

## [0.1.0] - 2026-07-15

Initial public release.

- Self-hostable Discord bot built on Anthropic Managed Agents, with one-click
  operator install and per-guild tenant isolation.
- `cli` adapter: the `daimon` admin CLI for driving turns and managing agents,
  environments, and skills from a terminal.
- `discord` adapter: mention-triggered threaded conversations and a
  slash-command admin surface.
- `mcp` adapter: an MCP server for agent-to-agent orchestration.
- `scheduler` adapter: polls due routines and dispatches headless turns.
- `slack` adapter (optional): Slack parity with the Discord adapter, off by
  default.
- Docker Compose deployment with a single-revision schema bootstrap.

[Unreleased]: https://github.com/pymc-labs/daimon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pymc-labs/daimon/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pymc-labs/daimon/releases/tag/v0.1.0
