# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Organic thread participation (Discord, opt-in).** An agent can be asked to
  follow a thread and then keep replying in it. `DAIMON_DISCORD__THREAD_PARTICIPATION__MODE`
  sets the deployment default: `off` (the default — mention-only, until someone
  turns a scope on), `on`, or `disabled` (mention-only, and requests to turn it
  on are refused). Below that, the `set_thread_participation` tool writes a
  cascade of workspace, channel and thread scopes — any member may set a
  thread, channel and workspace are admin (Manage Server) — where the narrowest
  scope with a setting wins and a `disabled` tier cannot be overridden below
  itself. In a thread that resolves to `on`, a burst of messages is judged once
  after a quiet period (`DAIMON_DISCORD__THREAD_PARTICIPATION__QUIET_SECONDS`,
  default 8) by a Haiku classifier that decides whether a reply adds anything,
  with an hourly per-thread cap as a backstop; auto turns carry an
  `unprompted="true"` hint so the agent keeps them short, and one that fails
  admission (credit depleted, cap, missing config) stays silent instead of
  posting the notice a mention would earn. Off by default, so
  existing deployments behave exactly as before. Migration
  `0014_thread_participation` adds two empty tables.

### Removed

- **`DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN`** — the Slack testing-only admin bypass.
  It made the admin check return true before `users.info` was ever called,
  opening every Slack admin gate for every member of every install on the
  deployment. Settings ignores unknown keys, so a deployment still setting it
  boots normally: the variable is dropped and the gates begin enforcing. Remove
  it from your environment, and promote a real workspace admin for any account
  that relied on it.

### Security

- Slack mentions queued behind an in-flight turn are now partitioned by author,
  one turn per caller. Previously the whole queue was coalesced into a single
  turn run as the first queued author, so a second member's instructions
  executed inside the first member's session, under their credentials and
  visibility, billed to them.
- `tokens_revoked` no longer tears down the install unless the event names the
  bot token. Slack also emits it when a single member revokes their own user
  token, which meant one member disconnecting could uninstall the app for the
  entire workspace.
- Agents seeded from `defaults/` can no longer be deleted. Both adapters refuse
  server-side before archiving; the Discord panel's disabled button was
  client-side only, and the Slack panel offered deletion outright.
- Reading a routine's last run output now requires the same authority as
  pausing or deleting it (workspace admin, or the routine's creator).

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

[0.1.0]: https://github.com/pymc-labs/daimon/releases/tag/v0.1.0
