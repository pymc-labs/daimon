# MCP tool catalogue

The 85 tools daimon's MCP server registers, plus the 8 on the hub login mounts.
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
- **Slack callers** — carries the `slack` tag.

A CLI token matches no platform tag, so it sees neither the Discord nor the Slack tools.
An agent token is narrowed to the agent-chat tools alone — everything else is disabled
for it, admin tools included.

A caller does not necessarily receive this list in one response: the server applies a
BM25 search transform, so an ordinary session discovers tools by searching the catalogue
rather than listing it in full. Sessions narrowed to agent-chat tools skip the transform
and see their tools directly.

## `agent_chat`

Agent-chat primitives plus bounded ``ask`` and completed-chart delivery.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_my_session` | agent tokens only | Archive a session when its conversation is finished. |
| `ask` | agent tokens only | Ask one question and wait for the final answer and any chart images. |
| `cancel_turn` | agent tokens only | Stop a running turn immediately. |
| `continue_turn` | agent tokens only | Send a follow-up message on an existing session. |
| `deliver_turn_charts` | agent tokens only | Return the newest completed reply with its chart images and optional links. |
| `describe_agent` | agent tokens only | Describe the agent associated with this MCP token. |
| `get_my_session` | agent tokens only | Get one session's status and metadata (no reply text, read-only). |
| `get_turn_cost` | agent tokens only | Return one finished turn's raw provider cost, before markup. |
| `list_events` | agent tokens only | List a session's events — the transcript. |
| `list_my_sessions` | agent tokens only | List this agent's sessions (id, status, title, timestamps). |
| `start_turn` | agent tokens only | Start a new conversation turn with the agent and return a handle. |

## `agent_removal`

Agent removal tools: detach_mcp_server, remove_skill, remove_agent_key, list_agent_keys.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `detach_mcp_server` | all callers | Disconnect an MCP server such as Linear or Notion from an agent. |
| `list_agent_keys` | all callers | What keys does an agent have? List its stored API key names, never values. |
| `remove_agent_key` | all callers | Remove an old API key or token, such as a Toggl key, from an agent's stored keys. |
| `remove_skill` | all callers | Stop an agent using an attached skill, such as eda. |

## `agents`

Agent tools: list / get / create / update / fork / archive.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_agent` | admin only | Delete an agent, for example churn-explorer, by archiving it. |
| `attach_mcp_server` | all callers | Add an MCP server that needs no token, such as Context7, to an agent. |
| `create_agent` | all callers | Create an agent called, for example, churn-explorer. |
| `fork_agent` | all callers | Make a copy of Daimon or another agent that you can edit under a new name. |
| `get_agent` | all callers | Show what an agent can access: attached MCP servers and skills. |
| `list_agents` | all callers | List agents in the tenant pool, including each agent's attached ``mcp_servers`` and ``skills``. |
| `update_agent` | all callers | Change an agent's system prompt or switch its model; add existing skills such as build-models. |

## `channels`

Shared channel MCP tools with per-platform dispatch.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_thread` | Discord callers, Slack callers | Create a new thread and post content as its first message. |
| `get_message` | Discord callers, Slack callers | Fetch a single message by channel and message id (Slack: the message ts). |
| `list_channels` | Discord callers, Slack callers | List channels in this server/workspace that you can view. |
| `list_threads` | Discord callers | List active and archived public threads for a channel. |
| `parse_link` | Discord callers, Slack callers | Extract IDs from a channel or message link. |
| `read_channel` | Discord callers, Slack callers | Read channel messages, oldest-first, with pagination metadata. |
| `read_thread` | Discord callers, Slack callers | Read messages from a thread, oldest-first. |
| `rename_thread` | Discord callers | Rename a Discord thread; ``name`` is the new title (1-100 characters). |
| `search_messages` | Discord callers, Slack callers | Search messages with server-side filters. |
| `send_message` | Discord callers, Slack callers | Post a message to a channel. |
| `set_display_identity` | Discord callers | Change how daimon appears in this Discord server: its display name, its avatar, or both. |

## `cli_token`

get_cli_token MCP tool.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_cli_token` | all callers | Mint a short-lived CLI access token for the named service. |

## `credential_requests`

Post requester-only private forms for agent keys, MCP tokens and GitHub access.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `request_agent_key` | Discord callers, Slack callers | Give an agent an API key or token for any service: Toggl, OpenAI, Higgsfield, or a platform that just launched. |
| `request_mcp_oauth` | Discord callers, Slack callers | Connect an agent to an MCP server that signs people in through the browser, such as Notion, Slack or Atlassian. |
| `request_mcp_token` | Discord callers, Slack callers | Connect an agent such as research-bot to Linear or GitHub through an MCP endpoint with a bearer token, not browser OAuth. |
| `request_repo_binding` | Discord callers, Slack callers | Let an agent read a GitHub working repo or repository, public or private. |
| `request_skill_repo_token` | Discord callers, Slack callers | The skills repo is private: collect a GitHub token to import its skills. |

## `environments`

Environment tools: list / get / create / update / archive.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `archive_environment` | admin only | Archive the MA environment and delete from the tenant pool. |
| `create_environment` | all callers | Create a sandbox environment an agent can later be scoped onto. |
| `get_environment` | all callers | Return one environment by name. |
| `list_environments` | all callers | List environments in the tenant pool. |
| `update_environment` | admin only | Patch-update an environment. |

## `github_app`

GitHub App install-link tool: post_github_app_install_link.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `post_github_app_install_link` | Discord callers, Slack callers | Install the GitHub App: post a link inviting the user to grant repository access. |

## `media`

Media MCP tools: YouTube transcript and file upload.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_file_upload_url` | all callers | Attach, post, send, or share a file in Discord — step 1 of 2. |
| `fetch_youtube_transcript` | all callers | Fetch the transcript of a public YouTube video for summarization or Q&A. |

## `notebook`

Notebook MCP tools.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_attachment_upload_url` | all callers | Mint a one-time upload URL for a raw data file in a notebook/blog workspace. |
| `create_notebook_upload_url` | all callers | Mint a one-time upload URL for a marimo notebook. |
| `delete_notebook` | all callers | Un-publish a notebook or blog you published (frees its host port). |
| `list_notebooks` | all callers | List what you've published — scratch notebooks and permanent blogs alike. |

## `propagation`

Propagation tools: set and clear agent defaults at workspace or channel scope.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_agent_default` | admin only | Stop an agent answering in a channel by clearing its default routing. |
| `explain_agent_resolution` | all callers | Who answers in this channel, for example #growth? Report who answers and which routing tier decided it. |
| `set_agent_default` | admin only | Make an agent answer in a channel or become the whole server/workspace default. |

## `publish`

Report publishing MCP tools.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `delete_report` | all callers | Un-publish a report you published. |
| `publish_report` | all callers | Publish a report: a page where the named people can read it and ask it questions. |

## `repo_binding`

Bind an agent to a public GitHub repo from inside an ordinary chat turn.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `bind_public_repo` | Discord callers, Slack callers | Have an agent work in a public GitHub repo: "work in github.com/owner/project", "point it at our open-source repository". |

## `routines`

Routines tools: create / list / get / update / delete.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `create_routine` | all callers | Create a routine in the caller's tenant partition. |
| `delete_routine` | all callers | Delete a routine (hard delete, tenant-scoped). |
| `get_routine` | all callers | Get a routine by id (tenant-scoped; raises if not found or cross-tenant). |
| `list_routines` | all callers | List all routines in the caller's tenant partition. |
| `update_routine` | all callers | PATCH-update a routine. |

## `self_edit`

MCP tools for an agent to edit its own ``agent_files`` and manage its
``agent_repo_binding`` from inside an MA turn.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `clear_repo_binding` | agent tokens only | Remove the repo binding for your agent. |
| `get_repo_binding` | agent tokens only | Return the current repo binding for your agent, or null if unbound. |
| `self_delete_file` | agent tokens only | Delete a per-agent file by `key`. |
| `self_list_files` | agent tokens only | List all keys + metadata for files in your private agent_files namespace. |
| `self_read_file` | agent tokens only | Read a per-agent file by `key`. |
| `self_write_file` | agent tokens only | Write or overwrite a per-agent file under `key`. |
| `set_repo_binding` | agent tokens only | Bind your agent to a git repo. |

## `sessions`

Sessions tools: list / get / events.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_session` | all callers | Look up a session by id (tenant-scoped). |
| `list_session_events` | all callers | List events for a session (SDK pass-through, single page). |
| `list_sessions` | all callers | List sessions in the tenant pool. |

## `setup_target`

Authenticated turn origins and identity-pinned configuration targets.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `set_setup_target` | all callers | Switch this setup conversation to an explicitly selected MA agent identity. |

## `skills`

Skill tools: sync / list / get / delete.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `delete_skill` | admin only | Delete a skill such as eda from the workspace, destroying all versions for every agent. |
| `get_skill` | all callers | Look up a skill by name. |
| `list_skills` | all callers | List all custom skills. |
| `sync_skills` | admin only | Install skills from a GitHub repo into the workspace's shared skill library. |

## `task_continuity`

Conversational task handoff and fresh start.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `hand_off_task` | all callers | Hand this task over to another agent in the same conversation: "have that one take over", "let churn-explorer finish this". |
| `start_fresh_task` | all callers | Start this conversation's work over with an empty workspace: "let's start fresh", "start over", "clear the workspace and begin a new task". |

## `thread_participation`

Thread-participation tools: follow a thread (or a channel, or the workspace).

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `get_thread_participation` | all callers | Report whether you follow a thread or channel, and which tier decided it. |
| `set_thread_participation` | all callers | Start or stop replying in a thread without being addressed each time. |

## `time`

Time tools: ``now`` and ``convert``.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `convert` | all callers | Convert an ISO-8601 ``time`` from ``from_tz`` to ``to_tz``. |
| `now` | all callers | Return the current wall-clock time in the given IANA timezone as ISO-8601. |

## `vault`

Vault tool: list_credentials — safe projection of caller's MCP vault credentials.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `list_credentials` | agent tokens only | List credentials in the caller's MCP vault (safe projection — no secrets). |

## `wizard`

post_wizard: the agent-facing tool that posts a multi-step form.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `post_wizard` | Discord callers | Post a multi-step form in the channel instead of asking in prose. |

## Hub login mounts: `hub`

Tool surface for the hub mounts: every daimon a logged-in person can reach.

A second surface, separate from the tools above: one app per platform, mounted at
`/discord/mcp` and `/slack/mcp` behind that platform's OAuth login, and present only
when the matching `DAIMON_HUB__*` client credentials are set. These tools carry no
visibility tags, so a logged-in caller sees all of them; each takes a `daimon_id` from
`list_daimons`, because one person reaches several workspaces here.

| Tool | Who can call it | Purpose |
| --- | --- | --- |
| `ask` | all callers | Ask a daimon one question and wait up to about two minutes for its answer. |
| `continue_turn` | all callers | Send a follow-up on an existing session without waiting. |
| `describe_daimon` | all callers | Describe one daimon: role, skills, repo, environment, platform and workspace. |
| `get_session` | all callers | Status of one session. |
| `list_daimons` | all callers | List every daimon you can reach on this platform, across all your workspaces. |
| `list_events` | all callers | A session's transcript. |
| `list_my_sessions` | all callers | Sessions you started with this daimon, for resuming with ``handle``. |
| `start_turn` | all callers | Start a turn without waiting. |
