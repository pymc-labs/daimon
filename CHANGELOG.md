# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Organic thread participation (Discord, opt-in).** Ask the agent to follow
  a thread and it keeps replying there unprompted. `DAIMON_THREAD_PARTICIPATION__MODE`
  sets the deployment default (`off` by default, `on`, or `disabled`, which also
  refuses requests to turn it on); the `set_thread_participation` tool writes
  workspace, channel and thread scopes below it (thread by any member, wider
  scopes by Manage Server), narrowest wins, `disabled` cannot be overridden
  from below. In an `on` thread a burst of messages is judged once after a
  quiet period by a metered Haiku classifier, gated by balance, cap and an
  hourly per-thread ledger, then answered as an ordinary turn flagged
  `unprompted="true"` so the agent stays brief. An unprompted turn is silent
  until it has something to say: no "thinking" embed goes up front, a turn that
  ends with nothing to add leaves no trace in the thread, and the messages it
  does post suppress the push notification. Turns that fail admission stay
  silent too. Existing deployments are unaffected; migration
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
