# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Bounded TLA+ models and a source-linked coverage report for turn rendering,
  scheduling, session preparation, continuations, adapter recovery, billing,
  and wizard submission.
- TLA+ models for usage metering (live recorder, usage sweep, balance gate)
  and Slack event dedupe and redelivery, each calibrated against earlier bug
  fixes. `docs/billing.md` now states the overdraft bound for concurrent and
  MCP-started turns, the sweep's attribution and its debits of operator-run
  turns, and why deployments must not share a Managed Agents workspace.

### Changed

- Opus 5.5 can be selected for an agent and is metered at its published rates.
  The seeded and new-agent defaults remain Sonnet 5.
- The documentation site carries daimon's own look: the readme sticker as
  logo and favicon, and a palette taken from it.

### Fixed

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
