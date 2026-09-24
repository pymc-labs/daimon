# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Connect Notion and other OAuth-only MCP servers with your own account.**
  Ask the agent to connect a server that signs people in through a browser
  and the new `request_mcp_oauth` tool posts a card whose button hands the
  requester a private sign-in link. daimon registers itself with the server,
  runs the PKCE authorization-code flow on `/oauth/mcp/start` and
  `/oauth/mcp/callback`, stores the grant as an `mcp_oauth` credential in
  that person's per-agent vault (Anthropic refreshes it) and attaches the
  server to the agent. Grants are per person: each member connects their
  own account and nobody inherits another's. Needs `DAIMON_MCP__PUBLIC_URL`
  and `DAIMON_CRYPTO__KEYS`. Migration `0020_mcp_oauth_flows`.

- **daimon can change its own name and picture on Discord.** Ask it to
  rename itself or use an attached image as its profile picture and the new
  `set_display_identity` MCP tool edits the bot's nickname and per-server
  avatar. Both apply to the whole server, since Discord has no per-channel
  identity, and the tool says so in its result. A server admin must ask;
  the image must be a Discord attachment (png, jpeg, gif or webp). Slack
  bots cannot rename themselves, so the tool is Discord-only.
- Slack supports `post_github_app_install_link` and the configurable
  `DAIMON_SLACK__BOT_DISPLAY_NAME`. An unconfigured GitHub App link names the
  operator setting needed; posting a link does not prove installation or repo access.
- `daimon agents bind-google` lets operators bind an agent's Google identity
  and scopes without writing SQL.

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
- **Discord threads get a real title.** A thread daimon opens for a mention is
  titled from the opening message by a short Haiku call before it is created,
  so it never opens with a "renamed the thread" notice; the channel shows
  typing meanwhile. The static "Chat with <agent>" title remains only when
  naming is off, the message has no text, the model answers blank, or the
  call fails or outlasts `DAIMON_THREAD_NAMING__TIMEOUT_SECONDS` (default 5).
  The call is metered to the tenant like any other model call and can be
  turned off with `DAIMON_THREAD_NAMING__ENABLED=false`. A new `rename_thread`
  MCP tool lets the agent retitle a thread on request: anyone who can post in
  a thread daimon opened may rename it, other threads need Manage Threads.
  Slack threads have no title, so both are Discord-only.

### Changed

- Opus 5.5 can be selected for an agent and is metered at $4 input, $20 output,
  $5 five-minute cache write, and $0.20 cache read per million tokens. The
  ledger cannot distinguish one-hour cache writes and prices them as five-minute
  writes. The seeded and new-agent defaults remain Sonnet 5.

### Fixed

- **A forked agent no longer warns everyone about a server it copied but
  nobody can open.** Forking copies the source's MCP servers, but not what
  authenticates them: an OAuth grant lives in the vault of (person, source
  agent) and cannot follow, and an agent-wide token stored for the source was
  simply left behind. On the fork, Managed Agents opened the copied server on
  every turn, failed it, and every reply carried the "was unavailable this
  turn" notice — for the person who signed in on the source as much as for
  anyone else. Agent-wide tokens now travel with the fork. A server anyone in
  the workspace has signed in to is treated as a sign-in server on every agent
  that carries its URL, and stays off a session until its caller has signed
  in on that agent; before, only sign-ins on the very same agent counted, so
  a fork looked like a server nobody needed to sign in to. Migration
  `0023_mcp_oauth_flows_url_ix`.

- **One member's OAuth sign-in no longer warns everyone else on every
  message.** An OAuth grant is stored in the vault of whoever signed in, but
  the server it unlocks is attached to the agent the whole workspace shares,
  so Managed Agents opened it on every other member's turn, failed it for
  want of a credential, and hung the "was unavailable this turn: it rejected
  the connection's credentials" notice under every reply — including turns
  with nothing to do with that server. A session now mounts only the servers
  its caller can authenticate: one nobody but another member has signed in to
  is left off that session's MCP server and toolset lists, while the members
  who did connect it keep it, and a server whose token is stored on the agent
  stays visible to everyone as before. Declining a sign-in link no longer
  counts as connecting, either. The notice is back to meaning what it says:
  your own connection needs attention. Migrations
  `0021_mcp_oauth_flows_agent_ix` and `0022_mcp_oauth_flows_completed`.

- **Connecting an OAuth MCP server no longer breaks every later turn.** After
  someone signed in to Notion, each turn failed with "could not get the agent
  ready": the per-turn vault mirror only knew static tokens, so it tried to
  create the agent's old shared token next to the person's `mcp_oauth` grant
  and Managed Agents refused (one credential per URL). Every vault writer now
  treats a URL held by a grant as taken: the mirror, the daimon-mcp bootstrap
  and the Copilot mount leave it alone, a pasted token replaces it, and a 409
  from a concurrent create is tolerated. Detaching a server also finds a
  token row stored with a trailing slash, and the sign-in card refuses the
  click when the deployment has no crypto keys instead of handing out a dead
  link. Asking to connect the deployment's own `daimon-mcp` name or URL is
  refused, as attaching it already was.

- **One failing MCP server no longer discards the reply.** When Managed
  Agents cannot reach or authenticate one of an agent's MCP servers it keeps
  the session running without it; daimon treated that error as fatal and
  threw away the answer that followed, so a rejected Notion token turned
  every turn into a blank failure (#79). The turn now succeeds with a line
  under the reply naming the unavailable server, and only a turn that
  produced nothing fails, naming the server in its error. The MCP token form
  probes the server first and refuses a token it rejects, pointing at the
  OAuth path; `detach_mcp_server` lets an admin disconnect a server from
  built-in Daimon and forgets the shared token stored for it.

### Removed

- **Breaking MCP tool changes:** `request_env_credential`,
  `list_env_credential_keys`, `remove_env_credential`, `request_mcp_credential`,
  and `request_skill_repo_credential` are now `request_agent_key`,
  `list_agent_keys`, `remove_agent_key`, `request_mcp_token`, and
  `request_skill_repo_token`, respectively. The duplicate `skills_sync`,
  `skills_list`, `skills_get`, and `skills_delete` aliases are removed; use
  `sync_skills`, `list_skills`, `get_skill`, and `delete_skill`. Update external
  callers; compatibility aliases are not retained.

- **`DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN`** — the Slack testing-only admin bypass.
  It made the admin check return true before `users.info` was ever called,
  opening every Slack admin gate for every member of every install on the
  deployment. Settings ignores unknown keys, so a deployment still setting it
  boots normally: the variable is dropped and the gates begin enforcing. Remove
  it from your environment, and promote a real workspace admin for any account
  that relied on it.

### Security

- Slack refreshes the caller's admin role on every mention; failed lookups do
  not overwrite a stored role. Both adapters include caller admin status in
  turn context. Discord turn replies suppress mass mentions.
- Setup guidance and private-input controls use consistent key/token language,
  preserve operation permissions and partial results, and avoid obsolete panel
  redirects or claims that newly stored keys refreshed an existing session.

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
