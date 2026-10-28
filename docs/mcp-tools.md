# MCP tool catalogue

The 174 tools Daimon's MCP server registers, plus the 8 on the hub login mounts.
Generated from the live registry by `scripts/generate_mcp_tool_catalogue.py` — edit the
tool's docstring, not this page. CI fails when the two disagree.

Each section is one module under `packages/adapters/mcp/daimon/adapters/mcp/tools/`. The
purpose column is the first sentence of the docstring the model itself reads; the full
text and the parameter schema live in the tool's source.

## Who can call what

Every tool below is registered on one server, and an identity middleware decides per
request which of them a caller may see. Untagged tools are visible to everyone; a tagged
tool is hidden by default and restored only for a matching caller.

- **admin only** — carries the `admin` tag.
- **agent tokens only** — carries the `agent-chat` tag.
- **Discord callers** — carries the `discord` tag.
- **fleet orchestrator turns** — carries the `fleet` tag.
- **operator tokens with agents:archive** — carries the `scope:agents:archive` tag.
- **operator tokens with channels:write** — carries the `scope:channels:write` tag.
- **operator tokens with promo:create** — carries the `scope:promo:create` tag.
- **operator tokens with promo:redeem** — carries the `scope:promo:redeem` tag.
- **operator tokens with tenant:read** — carries the `scope:tenant:read` tag.
- **Slack callers** — carries the `slack` tag.
- **Teams callers** — carries the `teams` tag.

A CLI token matches no platform tag, so it sees none of the Discord, Slack or Teams
tools. An agent token is narrowed to the agent-chat tools alone — everything else is
disabled for it, admin tools included. An operator token is narrowed the same way to the
tools tagged with its scopes; the `promo:create` tools are hidden from every other
caller.

A caller does not necessarily receive this list in one response: the server applies a
BM25 search transform, so an ordinary session discovers tools by searching the catalogue
rather than listing it in full. Sessions narrowed to agent-chat tools or to an operator
token's scopes skip the transform and see their tools directly.

## `admins`

Tenant-wide Daimon admin delegation, with fresh platform-only mutation gates.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `grant_admin` | admin only | Granted admins can change what agents do, including agents platform admins use. |
| `list_admins` | operator tokens with tenant:read | List this tenant's explicit Daimon admin grants (any member). |
| `revoke_admin` | admin only | Revoke an explicit Daimon admin grant immediately (platform admins only). |

## `agent_chat`

Agent-chat primitives plus bounded ``ask`` and completed-chart delivery.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_my_session` | agent tokens only | Archive a session when its conversation is finished. |
| `ask` | agent tokens only | Ask one question and wait for the final answer and any chart images. |
| `cancel_turn` | agent tokens only | Stop a running turn immediately. |
| `continue_turn` | agent tokens only | Send a follow-up message on an existing session, or resume a paused one. |
| `deliver_turn_charts` | agent tokens only | Return the newest completed reply with its chart images and optional links. |
| `describe_agent` | agent tokens only | Describe the agent associated with this MCP token. |
| `get_my_session` | agent tokens only | Get one session's status and metadata (no reply text, read-only). |
| `get_turn_cost` | agent tokens only | Return one finished turn's raw provider cost, before markup. |
| `list_events` | agent tokens only | List a session's events — the transcript. |
| `list_my_sessions` | agent tokens only | List the sessions you started with this agent (id, status, title, timestamps). |
| `start_turn` | agent tokens only | Start a new conversation turn with the agent and return a handle. |

## `agent_removal`

Agent removal tools: detach_mcp_server, remove_skill, remove_agent_key, list_agent_keys.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `detach_mcp_server` | all callers | Disconnect an MCP server such as Linear or Notion from an agent. |
| `list_agent_keys` | all callers | What keys does an agent have? List its stored API key names, never values. |
| `remove_agent_key` | all callers | Remove an old API key or token, such as a Toggl key, from an agent's stored keys. |
| `remove_skill` | all callers | Stop an agent using an attached skill, such as eda. |

## `agent_tool_rules`

Admin tools for one agent's MCP tool rules, third-party and Daimon's own.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_agent_tool_rule` | admin only, operator tokens with channels:write | Clear one agent's tool rule. |
| `list_agent_tool_rules` | admin only, operator tokens with tenant:read | List an agent's tool rules, optionally for one channel. |
| `set_agent_tool_rule` | admin only, operator tokens with channels:write | Set one agent's tool rule. |

## `agents`

Agent tools: list / get / create / update / fork / archive.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_agent` | admin only | Delete an agent, for example churn-explorer, by archiving it. |
| `attach_mcp_server` | all callers | Add an MCP server that needs no token, such as Context7, to an agent. |
| `create_agent` | all callers | Create an agent called, for example, churn-explorer. |
| `fork_agent` | all callers | Make a copy of Daimon or another agent that you can edit under a new name. |
| `get_agent` | all callers | Show what an agent can access: attached MCP servers and skills. |
| `list_agents` | all callers | List agents visible here, including their attached ``mcp_servers`` and ``skills``. |
| `request_agent_backend_switch` | all callers | Preview a requested agent backend switch, then Confirm in this chat. |
| `set_agent_backend` | all callers | Set an enabled model provider, or clear the agent setting with backend=null. |
| `update_agent` | all callers | Change an agent's system prompt or switch its model; add existing skills such as build-models. |

## `channel_budgets`

Channel budgets for platform admins acting directly with personal user credentials.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_channel_budget` | admin only, operator tokens with channels:write | Remove a channel's spending budget, so only the balance and caps apply. |
| `get_channel_budget` | Discord callers, Slack callers, Teams callers, operator tokens with tenant:read | Show a channel's spending budget: its limit, window and what it has spent. |
| `list_channel_budgets` | admin only, operator tokens with tenant:read | List channel budgets with a personal platform-admin CLI credential. |
| `set_channel_budget` | admin only, operator tokens with channels:write | Set or replace a channel's spending budget. |

## `channel_copies`

Channel copy tool: retire the copy a closing channel was given as its own agent.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_channel_copy` | admin only, operator tokens with agents:archive | Archive the agent copied as a channel's own, when that channel closes. |

## `channel_environments`

Channel environment tools: which environment a channel's turns run in.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_channel_environment` | admin only, operator tokens with channels:write | Stop a channel picking its own environment, so it uses the workspace default. |
| `set_channel_environment` | admin only, operator tokens with channels:write | Choose the environment a channel's turns run in, or the workspace default. |

## `channel_rules`

Channel and agent rules.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `set_agent_rule` | admin only, operator tokens with channels:write | Set where an agent runs: only in the ``runs_in`` channels and their threads, or, with null, wherever it is set to answer; an empty list runs it nowhere. |
| `set_channel_rule` | admin only, operator tokens with channels:write | Set who can read a channel and who can post there. |

## `channel_skills`

Channel skill tools: extra skills one channel's turns run with.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `add_channel_skill` | admin only, operator tokens with channels:write | Add a skill to whatever agent answers in one channel, there only. |
| `list_channel_skills` | admin only, operator tokens with tenant:read | List the extra skills a channel's turns run with, on top of its agent's. |
| `remove_channel_skill` | admin only, operator tokens with channels:write | Remove an extra skill from a channel, by id or name. |

## `channels`

Shared channel MCP tools with per-platform dispatch.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_thread` | Discord callers, Slack callers, Teams callers | Create a new thread and post content as its first message. |
| `get_message` | Discord callers, Slack callers, Teams callers | Fetch a single message by channel and message id. |
| `list_channels` | Discord callers, Slack callers, Teams callers | List the channels here that you can view. |
| `list_threads` | Discord callers, Teams callers | List active and archived public threads for a channel. |
| `parse_link` | Discord callers, Slack callers, Teams callers | Extract IDs from a channel or message link. |
| `read_channel` | Discord callers, Slack callers, Teams callers | Read channel messages, oldest-first, with pagination metadata. |
| `read_thread` | Discord callers, Slack callers, Teams callers | Read messages from a thread, oldest-first. |
| `rename_thread` | Discord callers | Rename a Discord thread; ``name`` is the new title (1-100 characters). |
| `replace_channel_file` | Teams callers | Save an edited file over a Teams channel file as its new version. |
| `search_messages` | Discord callers, Slack callers, Teams callers | Search messages with server-side filters. |
| `send_direct_message` | Discord callers, Slack callers, Teams callers | Privately message one human member of the current workspace. |
| `send_message` | Discord callers, Slack callers, Teams callers | Post a message to a channel. |
| `set_display_identity` | Discord callers | Change how Daimon appears in this Discord server: its display name, its avatar, or both. |

## `cli_token`

get_cli_token MCP tool.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_cli_token` | all callers | Mint a short-lived CLI access token for the named service. |

## `credential_requests`

Post requester-only private forms for agent keys, MCP tokens and GitHub access.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `request_agent_key` | Discord callers, Slack callers, Teams callers | Give an agent an API key or token for any service: Toggl, OpenAI, Higgsfield, or a platform that just launched. |
| `request_mcp_oauth` | Discord callers, Slack callers, Teams callers | Connect an agent to an MCP server that signs people in through the browser, such as Notion or Atlassian. |
| `request_mcp_token` | Discord callers, Slack callers, Teams callers | Connect an agent such as research-bot to Linear through an MCP endpoint with a bearer token, not browser OAuth. |

## `environments`

Environment tools: list / get / create / update / archive.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_environment` | admin only | Archive the MA environment and delete from the tenant pool. |
| `create_environment` | all callers | Create a sandbox environment a channel or the workspace can later run in. |
| `get_environment` | all callers | Return one environment by name. |
| `list_environments` | operator tokens with tenant:read | List environments in the tenant pool. |
| `update_environment` | admin only | Patch-update an environment. |

## `fleet`

Fleet tools (on by default) over the authorized host orchestration service.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `fleet_agents` | fleet orchestrator turns | List what this fleet run's workers can be: on-server agents, and models. |
| `fleet_collect` | fleet orchestrator turns | Collect verified outputs only after contracts and required reviews pass. |
| `fleet_cross_review` | fleet orchestrator turns | Spawn a reviewer of verified journal evidence through the same worker path. |
| `fleet_kill` | fleet orchestrator turns | Request a scoped stop; acknowledgement is not proof the worker stopped. |
| `fleet_resume` | fleet orchestrator turns | Reconcile and explicitly resume under original ceilings and current authority. |
| `fleet_spawn` | fleet orchestrator turns | Start one worker in this turn's fleet run. |
| `fleet_status` | fleet orchestrator turns | Read the run's durable worker status; do not start work. |

## `fleet_config`

Chat configuration over the fleet-grant and tenant-provider service owners.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `request_channel_pin_clear` | admin only | Show or clear THIS channel's model-provider pin, with admin confirmation. |
| `request_fleet_config` | all callers | Change worker spending limits with a personal CLI credential and live platform authority. |
| `request_provider_allowlist` | admin only | Preview and confirm the server's allowed model providers. |

## `fly`

Native owner-only Fly sessions bound to verified, attended conversation origins.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `fly_extend_session` | all callers | Set your session's expiry to this many hours from now, after owner approval. |
| `fly_get_session_status` | all callers | Read your session's machine state, capacity and expiry without renewing it. |
| `fly_launch_session` | all callers | Launch your own temporary Fly machine from an operator image profile. |
| `fly_list_files` | all callers | List paths and sizes on your session. |
| `fly_list_sessions` | all callers | List your live sessions in this conversation. |
| `fly_list_templates` | all callers | List operator image profiles, fixed CPU/RAM and allowed regions. |
| `fly_read_file` | all callers | Read a file from your session in byte chunks. |
| `fly_run_bash` | all callers | Run bash inline on your session; return exit code and stdout. |
| `fly_stop_session` | all callers | Permanently delete a session and its files. |
| `fly_write_file` | all callers | Write UTF-8 text on your session and renew its idle lease. |

## `github_app`

GitHub App install-link tool: post_github_app_install_link.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `post_github_app_install_link` | Discord callers, Slack callers, Teams callers | Install the GitHub App: post a link inviting the user to grant repository access. |

## `github_connect`

Private, agent-bound GitHub setup from an active conversation.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `github_connect` | Discord callers, Slack callers, Teams callers | Give this agent token access to more repos, such as a private GitHub repo. |

## `github_remove_repo`

Remove one repository from one agent after confirmation in chat.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `remove_repo` | Discord callers, Slack callers, Teams callers | Remove owner/repo from this agent only. |

## `github_requests`

Turn-bound GitHub access requests and private admin delivery.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `request_github_access` | Discord callers, Slack callers | Ask for token access to a repo needed for this agent's current request. |

## `github_working_repo`

Conversational selection of an agent's one filesystem repository.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `set_working_repo` | Discord callers, Slack callers, Teams callers | Choose the one repo in this agent's filesystem, or pass 'none' to clear it. |

## `here`

Current-place status tool with short text and complete structured facts.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `where_am_i` | all callers | Return the short who-answers-here text and the full structured facts. |

## `lead_intake`

First-party lead intake tools; authority derives only from live server context.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `lead_intake_get` | Discord callers | Read one authorized tenant lead, its review state and durable effect progress. |
| `lead_intake_health` | Discord callers | Read tenant lead counts, oldest pending age and mailbox coverage. |
| `lead_intake_list` | Discord callers | Read up to 100 tenant leads the caller may read, oldest first. |
| `lead_intake_read_source` | Discord callers | Read attachment-free original Gmail text for an unexpired discovered message. |
| `lead_intake_resolve` | Discord callers | Apply evidence-backed human CRM selections as a sales resolver or core-role member. |
| `lead_intake_resume` | Discord callers | Recover lead effects and mailbox coverage without blindly retrying uncertain creates. |
| `lead_intake_submit` | Discord callers | Verify and durably register one classified Gmail or own explicit Discord lead. |

## `long_jobs`

Start approved jobs, manage them as owner/admin, and read under source policy.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `cancel_long_job` | all callers | Request interrupt and archive as owner or current human tenant admin. |
| `get_long_job` | all callers | Read an owner/admin job; return source-restricted metadata when content is private. |
| `list_long_jobs` | all callers | List your latest 100 jobs; current human tenant admins may list all tenant jobs. |
| `start_long_job` | all callers | Run an exact human-approved shell command in a fresh isolated Managed Agents worker. |

## `media`

Media MCP tools: YouTube transcript and file upload.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_file_upload_url` | all callers | Post, send, or share a file to another channel, or from a routine — step 1 of 2. |
| `fetch_youtube_transcript` | all callers | Fetch the transcript of a public YouTube video for summarization or Q&A. |

## `microsoft.register`

Register connect_microsoft and the Microsoft Graph tools (Teams only).

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `connect_microsoft` | Teams callers | Send the person a private Connect Microsoft card so Daimon can use their Microsoft 365. |
| `microsoft_get` | Teams callers | GET a Microsoft Graph v1.0 path as the person, e.g. /me/drive/root/children. |
| `microsoft_search` | Teams callers | Search the person's Microsoft 365 as them: files, SharePoint, mail or calendar. |
| `microsoft_send` | Teams callers | Send an email or a calendar invite from the person's Microsoft account. |
| `microsoft_write` | Teams callers | Change the person's files or notes through Microsoft Graph, as them. |

## `models`

``list_models``: the models an agent here can be set to, by name, with list prices.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `list_models` | all callers | List the models an agent here can run on: plain name, id, provider and list price. |

## `notebook`

Notebook MCP tools.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_attachment_upload_url` | all callers | Mint a one-time upload URL for a raw data file in a notebook/blog workspace. |
| `create_notebook_upload_url` | all callers | Mint a one-time upload URL for a marimo notebook. |
| `delete_notebook` | all callers | Un-publish a notebook or blog you published (deletes it from the host). |
| `list_notebooks` | all callers | List what you've published — scratch notebooks and permanent blogs alike. |

## `plain_text_mentions`

Plain-text mention tools: let a typed "@<bot name>" start a turn, workspace-wide.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_plain_text_mentions` | all callers | Report whether a typed "@<your name>" in a channel or thread starts a reply here. |
| `set_plain_text_mentions` | admin only | Let a typed "@<your name>" in a channel or thread start a reply. |

## `progress_plans`

Progress-plan tools: the switch (`set_progress_plans`, `get_progress_plans`) and the
plan.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_progress_plans` | Discord callers, Slack callers, Teams callers | Report whether progress plans are on for a thread or channel, and which tier decided it. |
| `set_progress_plans` | Discord callers, Slack callers, Teams callers | Turn progress plans on or off where you are asked to. |
| `update_plan` | Discord callers, Slack callers, Teams callers | Show the task's plan as a checklist on the progress card. |

## `promo_codes`

Promo code tools: redeem an operator-issued code for the caller's server or workspace.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `redeem_promo_code` | admin only, operator tokens with promo:redeem | Redeem credit with a direct personal platform-admin CLI credential. |

## `promo_issuing`

Deployment-wide promo issuance requires the human operator CLI.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_promo_code` | operator tokens with promo:create | Unavailable over MCP: issue deployment-wide promos with the human operator CLI. |
| `list_promo_codes` | operator tokens with promo:create | Unavailable over MCP: list deployment-wide promos with the human operator CLI. |
| `revoke_promo_code` | operator tokens with promo:create | Unavailable over MCP: revoke deployment-wide promos with the human operator CLI. |

## `propagation`

Propagation tools: set and clear agent defaults at workspace or channel scope.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_agent_default` | admin only, operator tokens with channels:write | Stop an agent answering in a channel by clearing its default routing. |
| `explain_agent_resolution` | all callers | Who answers in this channel, for example #growth? Report who answers, the environment it runs in, and which routing tier decided each. |
| `set_agent_default` | admin only, operator tokens with channels:write | Make an agent answer in a channel or become the whole workspace's default. |

## `publish`

Report publishing MCP tools.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `delete_report` | all callers | Un-publish a report you published. |
| `publish_report` | all callers | Publish a report: a page where the named people can read it and ask it questions. |

## `repo_memory`

Repository-scoped persistent memory, authorized by the executing turn.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `repo_memory_delete` | all callers | Forget a memory by slug only when the person explicitly asks. |
| `repo_memory_list` | all callers | List repository memories: shared entries and your own personal directives. |
| `repo_memory_save` | all callers | Remember information for the bound working repository across sessions. |
| `repo_memory_view` | all callers | Read a repository memory's full body by its listed slug. |

## `routines`

Routines tools: create / list / get / update / delete, and an admin's sandbox opt-in.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_routine` | all callers | Create a recurring routine; owner DMs run as unattended live-DM turns. |
| `delete_routine` | all callers | Delete a routine (hard delete, tenant-scoped). |
| `get_routine` | all callers | Get a routine by id. |
| `list_routines` | all callers | List the routines you created (an admin sees every routine in the workspace). |
| `set_routine_sandbox_tools` | admin only | Turn built-in sandbox tools (bash, file reads and edits and the rest) on or off for one routine's own scheduled runs. |
| `update_routine` | all callers | PATCH-update a routine, including owner-only DM delivery where supported. |

## `self_edit`

MCP tools for an agent to edit its own ``agent_files`` from inside an MA turn.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `self_delete_file` | agent tokens only | Delete a per-agent key by `key`. |
| `self_list_files` | agent tokens only | List all keys + metadata (no values) in your private agent_files namespace. |
| `self_read_file` | agent tokens only | Check a per-agent file by `key`. |
| `self_write_file` | agent tokens only | Add a per-agent key under `key`. |

## `sessions`

Sessions tools: list / get / events.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_session` | all callers | Look up one of your sessions by id (tenant-scoped). |
| `list_session_events` | all callers | List events for one of your sessions (SDK pass-through, single page). |
| `list_sessions` | all callers | List your sessions in the tenant pool. |

## `setup_target`

Authenticated turn origins and identity-pinned configuration targets.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `set_setup_target` | all callers | Switch this setup conversation to an explicitly selected MA agent identity. |

## `skill_uploads`

add_skill: add one pasted, attached or GitHub skill to one agent, after a preview.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `add_skill` | all callers | Add a skill to one agent from a pasted SKILL.md, a .md or .zip attached in this chat, or one folder of a GitHub repository. |

## `skills`

Skill tools: sync / list / get / delete.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `delete_skill` | admin only | Delete a skill such as eda from the workspace, destroying all versions for every agent. |
| `get_skill` | all callers | Look up a visible skill by name, including version count. |
| `list_skills` | all callers | List visible custom skills. |
| `sync_skills` | admin only | Install skills from a GitHub repo into the workspace's shared skill library. |

## `task_continuity`

Conversational task handoff and fresh start.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `hand_off_task` | all callers | Hand this task over to another agent in the same conversation: "have that one take over", "let churn-explorer finish this". |
| `start_fresh_task` | all callers | Start this conversation's work over with an empty workspace: "let's start fresh", "start over", "clear the workspace and begin a new task". |

## `tenant_summary`

Billing and tenant configuration summaries for direct personal platform admins.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_tenant_summary` | admin only, operator tokens with tenant:read | Read billing and configuration with a personal platform-admin CLI credential. |

## `thread_model`

Switch this conversation's model when someone asks in words ("use opus", "think
harder").

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `set_thread_model` | all callers | Run this conversation on another model when the person asks in words: "use opus here", "think harder". |

## `thread_participation`

Thread-participation tools: follow a thread (or a channel, or the workspace).

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_thread_participation` | Discord callers, Teams callers | Report whether you follow a thread or channel, and which tier decided it. |
| `set_thread_participation` | Discord callers, Teams callers | Start or stop replying in a thread without being addressed each time. |

## `tidy`

Channel tidy tools: edit_message, delete_message, archive_thread, delete_thread.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_thread` | Discord callers | Archive a Discord thread you opened, with create_thread or from a mention. |
| `delete_message` | Discord callers, Slack callers, Teams callers | Delete one message you posted, or one of your replies or status cards. |
| `delete_thread` | Discord callers, Slack callers | Remove your own messages from a thread you opened. |
| `edit_message` | Discord callers, Slack callers, Teams callers | Replace the text of a message you posted. |

## `time`

Time tools: ``now`` and ``convert``.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `convert` | all callers | Convert an ISO-8601 ``time`` from ``from_tz`` to ``to_tz``. |
| `now` | all callers | Return the current wall-clock time in the given IANA timezone as ISO-8601. |

## `timers`

One-shot timers: create, list and cancel.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `cancel_timer` | all callers | Cancel a pending timer by id (from `create_timer` or `list_timers`). |
| `create_timer` | all callers | Come back to this conversation once, later: "remind me in two hours", "check back tomorrow at 9". |
| `list_timers` | all callers | List the caller's pending timers in this workspace, soonest first. |

## `vault`

Vault tool: list_credentials — safe projection of caller's MCP vault credentials.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `list_credentials` | agent tokens only | List credentials in the caller's MCP vault (safe projection — no secrets). |

## `watches`

Owner-bound watch management.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_watch` | Discord callers | Watch a reviewed Discord read tool from this conversation. |
| `delete_watch` | Discord callers | Delete your watch and its retained evidence after cancelling queued wakes; admins may delete within their workspace. |
| `list_watches` | Discord callers | List your watches in this workspace, including paused imports and expiry; no signing secrets. |
| `pause_watch` | Discord callers | Pause your watch and cancel its queued wakes. |
| `resume_watch` | Discord callers | Resume your paused watch from an attended owner turn after fresh access checks. |
| `rotate_watch_key` | Discord callers | Rotate an OS page signing key, revoke the old key and cancel queued wakes. |
| `show_watch` | Discord callers | Show your watch's immutable destination, source, status and expiry. |

## `wizard`

post_wizard: the agent-facing tool that posts a multi-step form.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `post_wizard` | Discord callers, Teams callers | Post a multi-step form in the channel instead of asking in prose. |

## Hub login mounts: `hub`

Tool surface for the hub mounts: every Daimon a logged-in person can reach.

A second surface, separate from the tools above: one app per platform, mounted at
`/discord/mcp` and `/slack/mcp` behind that platform's OAuth login, and present only
when the matching `DAIMON_HUB__*` client credentials are set. These tools carry no
visibility tags, so a logged-in caller sees all of them; each takes a `daimon_id` from
`list_daimons`, because one person reaches several workspaces here.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `ask` | all callers | Ask a Daimon one question and wait up to about two minutes for its answer. |
| `continue_turn` | all callers | Send a follow-up on an existing session without waiting. |
| `describe_daimon` | all callers | Describe one Daimon: role, skills, repo, environment, platform and workspace. |
| `get_session` | all callers | Status of one session. |
| `list_daimons` | all callers | List every Daimon you can reach on this platform, across all your workspaces. |
| `list_events` | all callers | A session's transcript. |
| `list_my_sessions` | all callers | Sessions you started with this Daimon, for resuming with ``handle``. |
| `start_turn` | all callers | Start a turn without waiting. |
