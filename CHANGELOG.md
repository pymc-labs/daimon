# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- A private form's pinned-agent check now runs in the same transaction that spends the form, on Discord, Slack and Teams, so a pin committed after the earlier check still refuses it and the form stays unspent. That transaction holds the tenant's policy lock from the check until the form is spent, so a pin edit made meanwhile waits and applies to the next form. The routine fire and delivery checks (protected destination, the creator still on the invoker allowlist), the protection of a turn's own notices, and the shared-agent replace/remove table now all go through the one access decision. They decide exactly as before.
- The action-time access checks now run at the last moment before each effect: right before a session is created (after the predecessor lookup and environment, vault and memory setup), after a dead-session recovery waits for the preparation lock (a seal added meanwhile defers adoption so the session is rebuilt read-only), after an agent chat resolves the agent's names, and inside the MCP OAuth vault write and server attach. A refused sign-in's saved grant is removed, retried once, and logged by id if it can't be. A DM whose source was sealed during a session replacement or recovery is closed instead of reporting a failed preparation. Session preparation decides again once it holds its lock, so a pin or seal that lands while it waits stops a checkpoint from running on the old writable session. A recovery adopts another turn's replacement only when that session is already read-only and carries the current seal, and it decides again right before the recovered message is sent. An agent-chat `start_turn` decides again right before its session is created. Every turn's first message, and a workspace checkpoint's message into the old session, is decided again right before it is sent, after the stream opens: a pin added meanwhile refuses it, and a seal makes the turn wait so the next one is prepared read-only (Discord says to send the message again).
- Access is decided again at the moment of action, not only when a turn is admitted. Before a session is found, reused, replaced or recreated after a crash, the turn's pin, protection and invoker checks are re-run on the current access policy, so a pin or protection added in between refuses it, and a seal added in between is stamped on the session and makes memory read-only. The check runs again right before a replacement session is created after a workspace transfer, and agent-chat and hub turns re-check right before they create a session or send a message, resumed sessions included. A DM is closed if its source channel was sealed since the turn started. The MCP OAuth callback checks the pinned-agent rule after the code exchange, again before the grant is saved and again before the server is attached, so a pin that lands mid sign-in stores no grant and attaches nothing. An admin reading their own DM sessions from the hub reads them as before. `daimon agents rekey-guild-ownership` keeps a report reader's link to its source agent. A published report's reader variant now counts as its source agent for pins, and publishing a pinned agent's reader needs an admin or a request from inside its channels (`publish_report(origin_context_id=…)`). An agent-scoped key is never exempt as an admin, whoever minted it.
- **Seeded defaults are protected from chat edits.** Skill-repo imports now
  gate like other attachment writes: a member needs an admin on an agent
  that answers for others, and neither the import nor a push resync
  attaches to a defaults-managed agent, even for admins. An import may not
  reuse a seeded skill's name, and only an admin import may replace an
  existing library skill. `delete_skill` refuses seeded skills, and
  `update_environment`/`archive_environment` refuse defaults-managed
  environments. The skill-import card names what did not import or attach.
  Apply frees the names of retired default skills. `sync_skills` outcomes
  gain an optional `refusal` reason. Operators can still delete a seeded
  skill with `daimon skills delete`.
- Admins are trusted: pins and seals protect members and channels, not admins. An admin is exempt from an agent's channel pin in a DM (Teams personal chats included) and in their own hub turns (`ask`, `start_turn`, `continue_turn`), where the reply reaches only them, and may list and read anyone's sealed conversations from the hub; continuing a sealed channel conversation from the hub is refused for everyone ("continue it in its channel"). Credential and configuration tools on a pinned agent are open to an admin's chat turn. Pins still hold for admins in channels, threads, handoffs and routines, and `fork_agent` still refuses a pinned source. On every turn, wherever a pinned agent runs, its sends on Discord, Slack and Teams reach only its pinned channels, the requester's own DM with daimon (Slack IM or Teams personal chat), and direct messages to the requester. Every DM session is stamped private, so admins never read another member's DM (Slack, Teams or `/dm`) from the hub. A hub caller counts as an admin by the account's stored role, which the person's next platform turn refreshes. Agent-scoped keys and tokens with no platform user are never admins, and a pin now binds a signed bearer with no platform user too (previously it skipped admission entirely). A DM closed because its source channel was sealed now says so plainly. `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` can be stored as agent keys by anyone; every other `GIT_*` name is still refused.
- **`DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS` is removed.** Setting it
  to `false` made every caller in a Discord thread share one agent session,
  minted for whoever created it: anyone in the thread acted with that
  person's connected accounts and admin rights, and several tools refused
  everyone else. Each caller now always gets their own session, as the
  default already did. Unknown keys are ignored, so a deployment still
  setting it boots normally.
- Each marimo notebook now requires its own access token. The link the host returns carries it, and a notebook's code can no longer open another notebook by curling its localhost port or reusing a slug it read from `ps`. Scratch notebooks are read-only apps; the editor needs `create_notebook_upload_url(editable=True)` on a deployment that sets `DAIMON_NOTEBOOK__ALLOW_EDITABLE`, and switching a slug between read-only and editor issues a new link. The host also needs its own `DAIMON_NOTEBOOK__ALLOW_EDITABLE` to serve an editor, and `PUT /admin/notebooks/{slug}` is read-only unless it asks for `editable`. The host kills every process of a notebook's uid before reusing or respawning it, quarantines a uid it can't clear, and deletes the uid's files in `/tmp` and `/dev/shm`. Jailed notebooks run with no-new-privileges, a process cap and a private `TMPDIR`, and switching a slug between editor and read-only wipes its home and workspace. Tokens are kept out of marimo's log and redacted from the host's logs, marimo is pinned exactly, and the host refuses to boot with plain-http links off localhost. With `DAIMON_NOTEBOOK__ORIGIN_BASE` (wildcard DNS and TLS), each notebook gets its own origin, `https://<label>.<origin_base>/`, and the host routes by Host, refuses cross-origin requests and WebSockets, and keeps cookies host-only. Without it, notebooks share one origin, so a public host admits only the tenants its operator lists in `DAIMON_NOTEBOOK__TENANTS`, for one operator's own Discord server, Slack workspace and Teams tenant; any other tenant gets a 403 naming its id. Over https, notebook cookies are `__Host-` prefixed and `Secure`, and HSTS is sent. The slug root is owned by the host and the host never follows a symlink the notebook planted when it chowns, writes or logs. httpx request logs are silenced and every log line is redacted after formatting. The Fly example sets up per-notebook origins.
- `/dm` now refuses in a sealed channel or a Discord thread under one, before reading any history. It used to copy the last 12 messages into a DM that sits outside the seal. On Slack, a thread sealed on its own is dropped from the copied history instead. An existing DM whose source channel, thread or (on Slack) any copied thread is sealed later ends on its next message: the conversation and its copied context are deleted and its sessions retired. DMs started before this release end as soon as the workspace seals anything. Run migration `0032_dm_source_ids` before deploying.
- The Slack `/routines` panel lists only your own routines unless you are an admin. `daimon agents fork` refuses a pinned source and leaves token-backed MCP servers off the copy, like the chat tool. The unused panel fork helpers and credential-copy stores are removed, and member-facing copy no longer points members at `fork_agent`, which is admin-only.
- Cross-agent protection is complete only for pinned agents: pin every client project agent. `decide_handoff` now requires the caller's admin status and the channel's agent explicitly.
- Pins can no longer be dropped by accident: removing an agent's last channel by id, naming an agent in both add and remove, mixed remove forms, unknown or look-alike agent names, Slack DM ids and `--clear` without `--replace-pins` are refused, and every newly unpinned agent is printed. Members can't add keys or connectors to a pinned agent from outside its channels (checked at request and at submit). DM conversations are outside every pin for handoffs and continuations, and `/dm` refusals show readable copy.
- `fork_agent` is admin-only, refuses to copy an agent pinned to channels, and a fork now starts with no credentials: no GitHub access, repo binding or proof of access, and no agent-wide MCP token. MCP servers that only work with a stored token are left off the copy. Previously any member could fork another project's agent and walk off with its repo access and connector tokens.
- A member can no longer operate another project's agent from outside its channels. MCP and hub turns (`start_turn`, `ask`, `continue_turn`, new or resumed) refuse a pinned agent. `hand_off_task` to an agent other than the channel's own, and `create_routine`/`update_routine` for an agent other than the one you are talking to or the destination channel's, now need an admin. Routines are listed and read only by their creator and admins.
- Operators can pin an agent to named channels (`daimon tenants access-policy set --add-pin-agent AGENT=CHANNEL_ID`, `--remove-pin-agent AGENT[=CHANNEL_ID]`). Pin edits keep every other agent's pin, and `--pin-agent`, which replaces the whole map, refuses to drop an unnamed agent's pin without `--replace-pins`. A pinned agent refuses turns anywhere else, including DMs and threads handed to it from other channels, and its routines must post into a pinned channel. Agents without a pin are unchanged.
- Agent keys are no longer stored in plaintext by default. Without `DAIMON_CRYPTO__KEYS`, saving one is refused (`request_agent_key` refuses before posting a form, and the form says why) unless `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true` opts in for local development. The session factory logs `agent_env.encryption_keys_missing` at error level at startup.
- New `daimon crypto verify` (fails when keys are missing, any agent key is stored in plaintext, or an encrypted key can't be decrypted with the current keys; counts per tenant only) and `daimon crypto encrypt-plaintext` (encrypts legacy plaintext rows in place).
- `self_read_file` and `self_list_files` no longer return any agent key value, only keys and metadata; the sandbox `.env` still carries them.
- Credential submissions no longer log any part of tokens, PATs or credential request tokens.
- Make the mounted agent `.env` inert under `set -a; source`: any value bash would expand, substitute or split is now single-quoted (double-quoted only when it holds a line break), so a key value such as `$(curl …)` set by any member no longer runs code in the agent's sandbox.
- Refuse key *names* that are themselves a capability, in two structural layers rather than a fixed list. Hard-denied for everyone (including admins), and filtered out of the mounted file even for rows stored earlier: any name that controls an interpreter, archiver, loader, locale, package manager, git, an HTTP client or a CA bundle, or that redirects an SDK's own traffic — recognised by exact names plus prefix/suffix classes (`*_OPTIONS`, `*OPTS`, `*_COMMAND`, `*STARTUP`, `*RC`, `*_PATH`/`PATH`, `*_PROXY`, `*_CONFIG`, `*_PRELOAD`, `*_BASE_URL`, `*_ENDPOINT`, `*_INDEX_URL`, `LD_*`, `GIT_*`, `LC_*`, …). This closes `TAR_OPTIONS`, `BASH_ENV`, `PYTHONSTARTUP`, `GIT_SSH_COMMAND`, `LESSOPEN` and their kind as cross-agent execution routes. Non-admin members may additionally only add a *secret* name — `^[A-Z][A-Z0-9_]{1,63}$` ending in `_KEY`, `_KEY_ID`, `_TOKEN`, `_SECRET`, `_PASSWORD`, `_PASSPHRASE` or `_PAT` (plus `GH_TOKEN`/`GITHUB_TOKEN` and bare words like `TOKEN`) — never an identity or targeting name (`*_USER`, `*_ID`, `*_EMAIL`, `*_ORG`, `*_PROJECT`, `*_REGION` …) or a `*_URL`/`*_HOST`; an admin may add any name that is not hard-denied. Language and build loaders (`R_*`, `JULIA_*`, `LUA_INIT*`, `JUPYTER_*`, `PYTEST_PLUGINS`, `*FLAGS`, `CC`/`LD`, `CMAKE_*`), libc and TLS controls (`GLIBC_TUNABLES`, `MALLOC_*`, `OPENSSL_*`, `NODE_TLS_REJECT_UNAUTHORIZED`, `SSLKEYLOGFILE`), config directories (`XDG_*`, `*_CONFIG_DIR`, `GNUPGHOME`, `PG*FILE`) and endpoint overrides (`AWS_ENDPOINT_URL*`, `GH_HOST`, `GITHUB_API_URL`, `CLOUDSDK_API_ENDPOINT_OVERRIDES_*`, `TF_CLI_*`, `HELM_*`, `CONDA_*`, `BUNDLE_*`, `POETRY_*`) are hard-denied; `NPM_TOKEN`, `CARGO_REGISTRY_TOKEN`, `UV_PUBLISH_TOKEN` and `DOCKER_PASSWORD` are exempt as opaque secrets. `self_write_file` now stores only secret-named keys (every key it writes is exported into the sandbox `.env`), so it no longer accepts file-like keys such as `config.yaml`. Enforced at `request_agent_key`, the Discord and Slack forms and uploads, and `self_write_file`; the store and the `.env` assembler apply the hard-deny layer under all of them.
- Session transcript tools now honour sealed channels. `list_sessions`, `get_session` and `list_session_events`, agent chat's session tools (`list_my_sessions`, `get_my_session`, `list_events`, `continue_turn`, `ask` with a handle, and the rest), and the hub's equivalents no longer list, return or continue a conversation that ran in a sealed channel unless the call comes from a turn inside that channel. New sessions record their channel and thread; older channel sessions are hidden outside their own thread while the tenant seals anything. Agent chat's `start_turn`, `ask` and `continue_turn` also refuse a chat turn's own credential, so a sealed turn can't open or continue another session. A conversation that ran sealed stays sealed after an unseal, including a session that replaces it (transcript, checkpoint, bundle, handoff or recovery), and its own thread can still read it.
- Connecting an MCP server through `request_mcp_token` or `request_mcp_oauth` can no longer replace one the agent already has unless an admin does it, when the agent is shared (a default somewhere) or defaults-managed. Replacing means repointing an existing server name at another URL, or pasting a new token for a URL that already has the agent-wide token. Checked when the card is requested, again against the person submitting, and once more by the attach itself. New server names are unchanged for members. A pasted MCP token is published as the agent-wide credential only after the server is attached and re-checked, under one per-agent lock that the OAuth callback, `attach_mcp_server` and `update_agent` also take, so no other session can mirror a refused token. A failed attach now stores nothing.
- Key names: hard-deny more exec-on-env names (`*_RSH`, `*PASSCOMMAND`, `SVN_SSH`, `CONFIG_SITE`, `GCC_EXEC_PREFIX`, `CCACHE_PREFIX`, `RUSTC_WORKSPACE_WRAPPER`, `RUSTDOC`, `COR_PROFILER*`, `CORECLR_*`, `DOTNET_ADDITIONAL_DEPS`, `LUA_PATH*`/`LUA_CPATH*`, `CLOUDSDK_PYTHON`, `COVERAGE_PROCESS_START`/`COVERAGE_RCFILE`, `NODE_REPL_EXTERNAL_MODULE`, `PYENV_VERSION`, `LESSKEYIN`/`LESSKEY_SYSTEM`/`LESSEDIT`/`MANROFFOPT`, `AS`/`NM`/`RANLIB`/`STRIP`/`FC`/`MAKE`/`MAKESHELL`, `GOINSECURE`/`GONOPROXY`/`GODEBUG`, `FCEDIT`, `ANSIBLE_VAULT_PASSWORD_FILE`). Path-valued names — `*_FILE`, `*_CLIENT_KEY`, `ETCDCTL_KEY` — are admin-only. Adding a name a tool reads as the same credential as one already stored (`GH_TOKEN`/`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, `AWS_SESSION_TOKEN`/`AWS_SECURITY_TOKEN`, `OPENAI_API_KEY`/`OPENAI_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`) now counts as replacing it: single keys go through the replacement gate and `.env` imports refuse it, naming both keys. The keys a turn is told about no longer include rows the mount skipped. `self_write_file` is add-only: an agent key can no longer overwrite a stored key or add an alias of one. `self_delete_file` is refused on the built-in agent and on any agent reachable in the workspace (as `remove_agent_key` is for members), so delete-then-add can no longer replace a key. Key writers take a per-agent lock before reading key names, so two concurrent writers can no longer add both names of an alias pair, and a `.env` file that contains both names of a pair is refused. Teams key submits now apply the same alias gate, in-transaction re-check and per-agent lock as Discord and Slack. Key and MCP-server replace and remove decisions (`remove_agent_key`, `detach_mcp_server`, MCP replacement, the setup wizard, every key form and `self_delete_file`) now treat an agent as shared when any of its names (display or `daimon_name`) is a channel, tenant or deployment default, when a live handoff or setup thread is bound to it, when it is anyone's personal default, when an enabled routine someone else created runs it, or when another account has a live session with it. Admins can again rotate one key of a stored AWS family; a related key added or removed after the gate still makes the write stale. The AWS access key, secret and session token are a credential family rather than aliases: a fresh agent may import them together, and adding to a stored family counts as a replacement. Alias groups also cover `GITLAB_TOKEN`/`GLAB_TOKEN`, `FLY_API_TOKEN`/`FLY_ACCESS_TOKEN`, `NPM_TOKEN`/`NODE_AUTH_TOKEN`, `AWS_SECURITY_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`, and are re-checked inside the write; the refusal names both keys. `VIMINIT`/`EXINIT` are hard-denied; `*_SSL_KEY`/`*_TLS_KEY` are admin-only.
- uvicorn access and error logs (MCP server, Teams adapter, report host) no longer print capability tokens in request paths (`/uploads/{token}`, `/slack/file/{token}`, report `/upload/`, `/publish/`) or any query value (OAuth `code`/`state`, `?k=` reader links); `httpx` request logging is quieted to warnings. The JSON and CLI log chains redact credential text in exception text and string fields before rendering, and Slack key-save failures log the error type only.
- Sentry no longer captures frame local variables or breadcrumbs, and `before_send` drops any that arrive and redacts secret-shaped text in exception messages and contexts, so decrypted agent keys and submitted form values can't reach error reports. Agent key values, `.env` entries, credential-request tokens and OAuth flow secrets are hidden from reprs; SQL errors (app and migrations) no longer carry bound parameters; CLI tracebacks no longer print locals; a malformed `DAIMON_CRYPTO__KEYS` error no longer echoes the keys. Outbound HTTP span `http.query`/`url.query` values are redacted whatever the parameter is called, and fragments dropped. Requests in error and transaction events keep only scheme, host and path in the URL (with capability tokens in paths such as `/uploads/{token}`, `/slack/file/{token}` and Discord webhook URLs replaced, and any credential-looking path segment redacted, in URLs anywhere in the event) and parameter names in the query (OAuth `code`/`state` included); credential headers, cookies, bodies and environ are dropped or redacted; JSON, dict-repr and quoted `key: value` secrets, `NAME_TOKEN=value`-style pairs (quoted values to the closing quote, unquoted to the next space), `--password value` flags and argv lists, escaped JSON, Slack (including `xapp-`), GitHub, Anthropic/OpenAI, JWT and Discord-webhook token shapes, form-encoded pairs (only `%26`/`%3D` are decoded), `Authorization` schemes, URL userinfo and query values, and pydantic `input_value` echoes inside exception and log messages are redacted, in time linear in the text (capped at 64 KB per string and 256 KB per event); a scrubber failure sends a stripped event instead of the original. User data is never sent.
- Encrypt agent environment values with rotatable deployment keys when configured, including existing rows on upgrade. Store encoding separately from user text so every literal value, including `enc:v1:` prefixes, remains valid. A database trigger keeps writes from older code tagged as plaintext; decryption errors identify the affected row without exposing values.

### Upgrade notes

- Deployments that set `DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS=false`: each caller in an existing thread starts a fresh session of their own at their next message; the shared session is not carried over.
- Notebook links change once: after the notebook host upgrades, links shared earlier (scratch notebooks and blogs) stop working. Blogs get a token on their first respawn; `list_notebooks` returns the new links. Upgrade the notebook host before the bot. A host with a non-localhost `DAIMON_NOTEBOOK__PUBLIC_HOST` and no `https://` `DAIMON_NOTEBOOK__PUBLIC_URL_BASE` no longer boots; set one, or `DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS=true` on a trusted private network. Editors need `DAIMON_NOTEBOOK__ALLOW_EDITABLE=true` on both the bot and the host. A public notebook host without `DAIMON_NOTEBOOK__ORIGIN_BASE` refuses every upload until `DAIMON_NOTEBOOK__TENANTS` lists the tenant UUIDs it may serve (comma-separated or a JSON array; `daimon tenants list --json` shows them, and each refusal names the id), and refuses tokens that name no tenant, so upgrade the bot with it. Set `ORIGIN_BASE` (wildcard DNS `*.<domain>` and a wildcard certificate) to serve any tenant in isolation. A `tenant.json` left in the data directory by a pre-release build is ignored and can be deleted.
- Set `DAIMON_CRYPTO__KEYS` before upgrading: keyless deployments still start and read existing values, but refuse new agent key writes unless `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true`, including the agent's own `self_write_file` notes (the tool error names `DAIMON_CRYPTO__KEYS`). `DAIMON_CRYPTO__KEYS` now also accepts the raw `Fernet.generate_key()` output or a comma-separated list; before this, only a JSON list parsed and a bare key stopped every service at boot. After setting keys and restarting **every** process with them, run `daimon crypto encrypt-plaintext`, then `daimon crypto verify`. Stop old readers/writers before the migration when enabling encryption. Keep keys available for reads and reversible downgrade; see `docs/self-hosting.md`.
- Agents whose keys hold shell metacharacters get a re-quoted `.env` once, so those sessions pick up a fresh mount on their next turn. Keys already stored under a hard-denied or non-identifier name stop being exported (an admin-set `DATABASE_URL` still mounts — only tool-control names are dropped); each skipped name is logged as `credential_env.row_skipped` and still shows in `list_agent_keys` so it can be removed or re-added under another name. If you suspect a name like `TAR_OPTIONS` or `LD_PRELOAD` was set on a shared agent before this release, refresh that agent's sessions and rotate any credential the agent could have reached.
- Audit what members set up before the cross-agent fixes: `list_routines` as an admin for routines whose agent is not the one their destination channel answers with, and thread handoff bindings to agents from other projects. These keep working until removed.
- Agent environment encryption is opt-in through `DAIMON_CRYPTO__KEYS`; keyless deployments retain plaintext storage and initialization still succeeds. Stop old readers/writers before the migration when enabling encryption. Keep keys available for reads and reversible downgrade; see `docs/self-hosting.md`.
- The seeded agents move to Sonnet 5.5 on the next defaults reconcile, so each existing `daimon` and `dev_agent` thread replaces its session on its next message, with one checkpoint turn on the old session if it had replied. Reports and spend caps reprice history at read time, so this month's Sonnet 5 and Opus 4.7 spend drops at once; set `DAIMON_BILLING__MARKUP` if the old rates stood in for a margin.

### Added

- **Scoped operator tokens for integrations.** `daimon mcp mint-operator-token`
  mints a token acting over `/mcp` for one server admin with only the scopes it
  names (`tenant:read`, `channels:write`, `promo:redeem`, deployment-wide
  `promo:create` with an optional `--max-issued-usd`), for 30 days by default
  and at most 90. Each request rechecks the row and the account's stored admin
  role, which changes on the person's next platform turn, so `revoke-token` is
  the immediate stop and `set-token-scopes` narrows. Calls are rate limited
  (`DAIMON_MCP__OPERATOR_CALLS_PER_MINUTE`); calls, refusals and token changes
  are audited. Channel budget, agent and admin tools take the scopes. New tools:
  `get_tenant_summary` (with each channel's admins) and, for `promo:create`,
  `create_promo_code`, `list_promo_codes`, `revoke_promo_code`. `mint-token`
  tokens now expire and can be revoked. Run migration `0036_operator_tokens`.
- **Teams takes every private form Discord and Slack do.** A key value can
  span lines, `request_agent_key` without a key posts a `.env` form on Teams
  (the file is pasted, since a dialog has no file input), and repo and
  skill-repo cards open a GitHub token form that binds the working repo or
  imports and attaches the skills, with Slack's admin gate and whole-file
  rules. A token that cannot read the repo is refused in the dialog before
  the request is spent.
- **Teams channel turns replay their thread, as on Discord and Slack.** The
  first turn in a thread reads the root post and its newest replies through
  Microsoft Graph, a later turn only what came after the last message it read,
  and a mention that starts a thread the channel's recent posts, all marked
  untrusted. A bare @mention asks about the thread. Images pasted into a
  channel message now reach the agent; files shared in a channel are named and
  the person is told they can't be opened, as is anything Graph can't read.
  This needs the resource-specific consent `ChannelMessage.Read.Group`, which
  a team owner grants at install: upload the updated app package again. A
  refused or slow read never fails a turn, which then runs without history.

- **Teams channels can take files, once an admin grants the team's site.**
  With the Graph application permission `Sites.Selected` and a write grant on
  a team's SharePoint site, files shared in its channels reach the agent as
  download links, and files the agent writes are uploaded to the channel's
  Files tab and linked below its answer, or in one message; one that fails to
  upload is named there. The turn tells the agent whether this works in that
  channel. Without a grant, in private and shared channels, or when Graph
  refuses, channels behave as before. The manifest is unchanged; `docs/teams.md` has
  the grant steps.

- **Teams threads can be followed, as on Discord.** Ask the agent to follow a
  thread (`set_thread_participation`; a channel or the whole organisation
  needs a listed admin) and it reads replies nobody addressed to it, then
  joins in when the same classifier, quiet timer and hourly cap Discord uses
  say it can help. The turn passes the usual admission and billing gates,
  runs as the newest author, and posts only its answer: no status card,
  notice or error. Root posts, bots and protected channels are never judged,
  and without Graph history a followed thread stays mention-only. Quoting one
  of the bot's messages in a channel now counts as mentioning it, and the
  quoted text reaches the agent in place.

- `scripts/hackathon_rehearsal_readout.py` prints stage readouts from staging logs, Monitoring metrics and content-free turn outcomes.
- Long-running adapters emit `runtime.health` logs every 30 seconds with Anthropic response attempts, database pool use, event loop lag and turns in flight; `DAIMON_OBSERVABILITY__HEALTH_INTERVAL_S=0` disables them.
- Microsoft Teams adapter: answers in 1:1 chats and when @mentioned in
  channels, with an in-place status card, author-only Cancel, per-thread
  queueing, feedback buttons and restart recovery. The configured Entra
  organisation is provisioned at boot. The 1:1 chat offers `help`, `new`,
  `setup` (with setup conversations), `routines`, `memory`, `privacy` and
  `billing`; admins are listed in
  `DAIMON_TEAMS__ADMIN_USER_IDS`. Pasted images and files shared in 1:1 chats
  reach the agent, output files are delivered with file consent cards, and
  `send_message`, `create_thread`, task handoffs and timers work from Teams
  turns; the wake poller runs handoffs and timers across restarts.
  Agent keys, MCP tokens and MCP sign-ins are collected privately through
  Teams dialogs, and with tool safety on, attached-tool writes wait on an
  Approve/Deny card only the requester can answer. A turn that ends early
  explains why with the termination notice. Protected channels get no
  replies, notices or tool posts. Tenant turn caps, agent pins, key-name
  rules, prepaid balance footers and spend-limit alerts match Slack.
  Builds on #220 by @jchu96. See `docs/teams.md`.
- Staging-only load rehearsal script for synthetic installs and metered headless turns, with a dry run, spend guard, and tenant cleanup.

- Optional Discord webhook alerts for new installs, Stripe top-ups, and Anthropic spend or overload events.

- Discord delivers files generated during tool-using turns into the chat thread,
  with upload-limit notices and protected-channel checks.
- **Channel budgets.** Admins can cap what one Discord or Slack channel may
  spend, monthly, in total or over a fixed date range, with
  `set_channel_budget`, `clear_channel_budget` and `list_channel_budgets`, or
  `daimon channels budget set|clear|list`. Once a channel's debits (markup
  included, threads counting toward their channel) reach its limit, new turns
  there are refused, as are unprompted replies, wakes, `/dm` and DMs moved
  from there, YouTube transcripts asked for there and routine fires that post
  there or were made there. Members can read a budget with `get_channel_budget`, and
  `/billing` shows the channel's spend against it. Nothing changes until a
  budget is set, with or without Stripe. Usage and debits now record their
  channel from this release on, and sessions carry it as
  `daimon_budget_channel`.
- **Channel admins.** Server and workspace admins can name the roles and
  members who run a channel, with `set_channel_admins`, `list_channel_admins`
  and `clear_channel_admins`, from Who answers where in the setup panel, or
  with `daimon channels admins`. A channel admin may change agents that answer
  only in channels they run (instructions, skills, keys, MCP servers, repos)
  and set or clear those channels' default agent, though never to another
  channel's own agent; built-in agents and the server default stay with server
  admins. Slack grants are by member only. A routine or wake set up by someone
  with more rights keeps the agent out of a channel admin's hands, and so do a
  personal default, someone else's live session or routine in another channel
  or in no known one, and, for key, MCP server and skill repo changes,
  answering nowhere. Connecting a skill repo now counts an agent as shared
  wherever a key change does (someone's personal default, a bound thread, or
  another member's routine or live session) on Discord and Slack too, as it
  already did over MCP. An admin of every channel an agent is pinned to may
  change it from outside them, and from the hub may list and read the sealed
  conversations of the channels they run, as a server admin may of any, when
  every seal on one lies in those channels; DMs stay private. No chat
  tool, panel or CLI `config` write can make a pinned agent the default of a
  channel outside its pin. Nothing changes until a channel admin is named.
- **Channel-bound coding-tool tokens.** "Use from your coding tools" pressed
  in a sealed channel, or in a channel the agent is pinned to (a thread counts
  as its parent), now mints a token bound to that channel, and the reply says
  so. Its calls run as a turn there: the agent's pin admits it, it reads that
  channel's sealed conversations and continues those opened under the seal,
  and it uses the channel's environment and budget. Its sessions are stamped
  with the channel (sealed, with read-only memory, in a sealed one), so their
  spend counts there. The token never carries its minter's channel admin
  grants. A channel admin of every channel an agent is pinned to may now mint
  one from inside those channels, always bound. Tokens minted elsewhere, and
  existing ones, are unchanged. Run migration `0037_mcp_token_channels`.
- **Channel isolation.** A server admin can isolate a channel from Who
  answers where in the setup panel, with `set_channel_isolation` (also under
  an operator token's `channels:write`) or with `--isolated-channel`. That
  seals it and pins its default agent to it alone in one write; a default
  that is built in or answers elsewhere is refused with the reason, unless
  the admin asks for a copy, made without credentials. Inside, only the
  channel's own agents run, post, read and take routines or bindings, with
  writable memory; they are hidden elsewhere, post nowhere else, send no DMs,
  and `/dm` there is refused. The panel shows the channel as private,
  dedicated agent and hidden. Ending keeps the seal and pins unless lifted too,
  and warns that the agents keep what they remembered there. No migration;
  clear isolation before rolling back, as older releases reject the field.
- Optional Discord process-wide turn limit for guild chats and DMs. Excess
  requested turns get a retry notice; surfaced Anthropic 429/529 responses
  emit structured logs.
- Operators can credit tenants with `daimon tenants credit --note` and set default
  or per-person monthly caps with `daimon tenants cap`. Caps apply without Stripe;
  prepaid Discord and Slack turn footers show the remaining balance.
- Operators can set a tenant's concurrent chat-turn cap with
  `daimon tenants turn-cap`, or clear it to use the deployment default.
- **Promo codes.** Operators create credit or timed codes with `daimon promo`,
  and admins redeem them from `/billing` on Discord and Slack or with the MCP
  tool `redeem_promo_code`. Timed credit is spent first inside its window and
  the unspent rest expires when it closes; spend dated inside the window is
  credited back if it is recorded within 15 minutes of the close. Nothing is
  visible until a code can be redeemed: only then do the Redeem code button
  and the join-message lines appear. Turn debits now store the model call's
  time as `occurred_at`.
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
  skipped with a notice instead of running under that agent. Handoffs and
  applied private input are now skipped the same way. Migration
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
- **Teams shows the same status card, and answers without a usage line.** The
  running card on Teams now matches Discord and Slack: a bold Thinking or
  Working headline with the elapsed time, up to six recent tool calls with
  readable names and done or failed marks (MCP and custom tools included),
  and the latest draft underneath, with Cancel kept. Answers, the close of a
  turn that only ran tools ("✅ Done.") and failure notices no longer end with
  the agent, time, token, cost and balance line. The feedback buttons stay on
  the last part of the answer.
- **Teams agents know how file delivery works there.** The guidance block
  every agent gets now has a Teams paragraph: in a 1:1 chat, saving a file
  under `/mnt/session/outputs` is the delivery path, with Slack's write rules,
  and the person accepts it from a download card; in a channel no file can be
  attached, so the agent says so once in its reply, pastes short text inline
  or points to a 1:1 chat. Channel threads no longer get a separate note per
  file. `send_message`'s refusal of files on Teams now says where files go
  instead of "not available yet". Agents pick it up at their next reconcile
  or edit.

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
- **Sonnet 5.5 is the default model.** It can be selected for an agent and is
  metered at its published rates, and the seeded `daimon` and `dev_agent`
  agents and every new agent now start on it instead of Sonnet 5, at the same
  per-token price. Asking an agent tool for "Sonnet" or "Opus" now means
  Sonnet 5.5 or Opus 5.5. Opus 5.5 and the older models remain selectable per
  agent; fork a seeded agent to keep it on another model. Sonnet 5.5 returns longer notes between tool calls as thinking, so the in-progress
  card can show less draft text than it did on Sonnet 5.
- Opus 5.5 can be selected for an agent and is metered at its published rates.
- The documentation site carries daimon's own look: the readme sticker as
  logo and favicon, and a palette taken from it.
- Defaults reconciliation serializes writes and sweeps per tenant so concurrent
  callers cannot create duplicate seeded resources or archive an ID another
  caller has already resolved.

### Fixed

- **Teams posts nothing but its answer.** A file it could not read, an oversize output and a declined file offer each sent a status message of its own under the answer. The agent is now told what it could not open and why, and the rest is only logged, as on Slack and Discord. Cards and file offers still post.
- **Teams channel files without Graph Explorer.** When a channel file is refused, a daimon admin gets an Enable files card: one sign-in by a SharePoint or global admin grants the bot that team's site. It needs `DAIMON_TEAMS__PUBLIC_URL` and a redirect URI on the app (see `docs/teams.md`).
- **The MCP endpoint is stateless.** It kept MCP sessions in one process's
  memory, so after a redeploy, or on a deployment running the MCP server on
  more than one instance, a client's tool calls failed with "server terminated
  the MCP session" (HTTP 400) or "Session not found" (HTTP 404) for the rest of
  its session. Every request now stands alone, needs no `initialize` first and
  answers with JSON, as the hub endpoints already did; an unknown session id
  is ignored. Identity and tool visibility were already worked out per request
  from the token, so no tool changes.
- A Teams channel message the bot ignores because it was not mentioned is now
  logged as `teams.message.ignored`, and a refusal reply that fails to send as
  `teams.refusal.send_failed`, each with the conversation type and reason and
  never the message text. Both cases used to leave no trace.
- The Slack bot no longer answers thread replies that don't mention it. Slack
  can deliver an `app_mention` event for a reply in a thread the bot is in even
  when the reply never mentions it, and each one ran a billed turn. A turn now
  runs only when the message contains `@daimon`, as on Discord, so follow-ups
  in a thread need the mention every time. Dropped events are logged as
  `slack.event_dropped.no_explicit_mention`.
- Slack answers that put a bare link in bold or italics (`**https://…**`) no
  longer render as a dead link ending in `*`. The link is posted as an explicit
  Markdown link, with any trailing full stop or question mark left outside it.
  Code, existing links and table cells are left as written.
- The checkout landing pages no longer tell every payer to return to Discord.
- Continuity notices (workspace loss, fresh start, a failed preparation, a new
  responder, a timer that did not run) say "here" and "ask again" instead of
  "this thread" and "mention me", so they read right in a chat without threads.
- **Sonnet 5 and Opus 4.7 are metered at list price.** Sonnet 5 was charged at
  $3/$15 per million tokens, the rise Anthropic announced and then withdrew;
  its standard price stayed $2/$10. Opus 4.7 was charged Opus 4.1's $15/$75
  instead of $5/$25. Every agent-model row in the pricing table is now the
  provider's list price. Deployments that want a margin set `DAIMON_BILLING__MARKUP`, which
  applies the same multiplier to every model and leaves the cost reports
  showing provider cost.

- Stop retrying Anthropic's monthly spend-cap response and show a clear model usage limit notice in Discord and Slack.

- Avoid repeated Managed Agents event reads for unchanged sessions during the usage sweep, with hourly full passes.

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

- Sentry's event scrubber checks nested values, so a token inside a dict local,
  such as request headers, no longer ships with an error event.
- Slack top-ups sent the checkout route an internal admin token for the
  clicking account. They now send a plain account token, as Discord does;
  the route only needs the account.
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
