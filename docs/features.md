# Features

Every feature is set at one or more levels: Deployment, Workspace, Agent, Channel and
Thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above, and at the top it is the feature's built-in default.
Settings show which level decided.

An admin can lock a value at the Deployment, Workspace, Agent or Channel level. Levels
below then cannot change it. A locked Off turns the feature off below.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool, the web app or `daimon features`.

A chat conversation picks up a change to a feature that hides tools at its next message.
A CLI session, or an agent chat continued with `send_message`, keeps the tools it started
with; Daimon refuses a tool turned Off since then.

## All features

Where each feature can be set, and its default.

| Feature | Key | Kind | Default | Deployment | Workspace | Agent | Channel | Thread | Routine | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Data sources | `data_sources` | Model input | Off | ✓ | ✓ |  |  |  |  |  |
| Chat history | `history_depth` | Model input | 100 messages | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Direct message memory | `dm_history_depth` | Model input | 12 messages | ✓ | ✓ | ✓ |  |  |  | Only Discord, Slack, Teams, Web; 100 messages by default on Teams |
| Channel background | `channel_history` | Model input | 25 messages | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Setup guidance | `guidance` | Model input | On | ✓ | ✓ | ✓ |  |  |  |  |
| Run commands | `tool.bash` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Read files | `tool.read` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Edit files | `tool.edit` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Write files | `tool.write` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Search file contents | `tool.grep` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Find files | `tool.glob` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Open web pages | `tool.web_fetch` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Search the web | `tool.web_search` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Up to Agent on Gemini, OpenAI |
| Web app | `web_awareness` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Connected server briefing | `mcp_briefing` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Not on Gemini, OpenAI |
| Notes on non-chat messages | `origin_fragments` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Keys file | `agent_env_file` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Not on Gemini, OpenAI |
| Platform prompt | `platform_prompt` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Always Off on OpenAI |
| Platform skills | `platform_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Not on Gemini, OpenAI |
| Agent skills | `agent_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Always On on Gemini, OpenAI |
| Channel skills | `channel_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Thread participation | `participation` | Chat | Off | ✓ | ✓ |  | ✓ | ✓ |  | Only Discord, Teams |
| Progress plans | `progress_plans` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Cost footer | `cost_footer` | Chat | Shown |  | ✓ |  | ✓ | ✓ |  |  |
| Answer messages | `answer_messages` | Chat | 5 messages | ✓ | ✓ | ✓ | ✓ | ✓ |  | Only Discord |
| Thread titles | `thread_naming` | Chat | On | ✓ | ✓ |  | ✓ |  |  | Only Discord |
| Display name | `display_name` | Chat | The agent's id |  |  | ✓ |  |  |  |  |
| Thread title style | `thread_title_style` | Chat | Sentence case | ✓ | ✓ | ✓ | ✓ |  |  | Only Discord |
| Thread archive time | `thread_auto_archive` | Chat | 1 week | ✓ | ✓ | ✓ | ✓ |  |  | Only Discord |
| Status reactions | `status_reactions` | Chat | Off | ✓ | ✓ | ✓ | ✓ |  |  | Only Discord |
| Lock reaction | `protected_notice_reaction` | Chat | On | ✓ | ✓ | ✓ | ✓ |  |  | Only Discord |
| Text before tool calls | `pre_tool_messages` | Chat | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Only Discord |
| Working card timer | `card_timer` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  | Only Discord |
| Answer bots | `answer_bots` | Chat | Off | ✓ | ✓ | ✓ |  |  |  | Only Discord |
| Typed mentions | `plain_text_mentions` | Chat | Off | ✓ | ✓ |  | ✓ |  |  |  |
| Completion pings | `completion_pings` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Completion pings in direct messages | `dm_completion_pings` | Chat | Off | ✓ | ✓ |  |  |  |  | Only Discord |
| Model reactions | `model_reactions` | Chat | Off |  | ✓ | ✓ |  |  |  | Only Discord |
| Routine result header | `routine_result_header` | Chat | On | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| Tool server names | `tool_server_names` | Chat | On |  | ✓ | ✓ |  |  |  |  |
| Table images | `table_rendering` | Chat | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Only Discord, Slack; Off by default on Slack |
| Budget notices | `budget_notices` | Chat | On | ✓ | ✓ |  | ✓ |  |  |  |
| Vote buttons | `message_feedback` | Chat | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Direct message wait | `dm_debounce_seconds` | Chat | Off | ✓ | ✓ |  |  |  |  |  |
| /new in threads | `thread_new` | Chat | Off | ✓ | ✓ |  |  |  |  | Only Discord |
| Other providers per conversation | `thread_providers` | Chat | Off | ✓ | ✓ | ✓ |  |  |  |  |
| Repository memory | `repo_memory` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Context provider | `context_provider` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Not on Gemini, OpenAI |
| Personal memory | `memory_personal` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Shared memory | `memory_shared` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon; Always On on Gemini, OpenAI |
| Channel memory | `memory_channel` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Private channel messages | `private_channel_messages` | Capability | On | ✓ |  |  |  |  |  | Needs setup by whoever runs Daimon; Only Teams |
| Direct messages | `direct_messages` | Capability | Off | ✓ | ✓ | ✓ |  |  |  | On by default on Teams |
| Web app: chat | `web.chat` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Web app: agents | `web.agents` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Web app: channels | `web.channels` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Web app: routines | `web.routines` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Web app: credits | `web.credits` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Web app: integrations | `web.integrations` | Capability | On | ✓ | ✓ |  |  |  |  |  |
| Watches | `watches` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Routines | `routines` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Long jobs | `long_jobs` | Capability | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Secret sharing | `secret_sharing` | Capability | Off | ✓ | ✓ | ✓ |  |  |  | Only Discord |
| Wait for bot access | `bot_access_wait` | Capability | Off |  | ✓ |  |  |  |  | Only Discord |
| Cloud computers | `compute` | Capability | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Notebooks | `notebooks` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Report publishing | `report_publish` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Video transcripts | `media` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Microsoft 365 | `microsoft_365` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  | Needs setup by whoever runs Daimon |
| Ask a person | `support_escalation` | Capability | On | ✓ | ✓ | ✓ | ✓ |  |  | Needs setup by whoever runs Daimon |
| Feedback to support | `support_feedback` | Capability | Off | ✓ | ✓ | ✓ | ✓ |  |  | Needs setup by whoever runs Daimon |
| Own support channel | `support_own_channel` | Capability | On | ✓ |  |  |  |  |  |  |
| GitHub access | `github` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Lead intake | `lead_intake` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Data source reads | `data_reads` | Capability | Off |  | ✓ | ✓ | ✓ |  |  |  |
| Library skills | `library_skills` | Capability | Off |  | ✓ |  |  |  |  |  |
| Posting to others | `outbound_reach` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Long job dollar cap | `long_jobs_max_usd` | Limit | $5 | ✓ | ✓ | ✓ |  |  |  |  |
| Long job time limit | `long_jobs_max_deadline` | Limit | 70 minutes | ✓ | ✓ | ✓ |  |  |  |  |
| Callback watch lifetime | `callback_watch_lifetime` | Limit | 30 days | ✓ | ✓ |  |  |  |  |  |
| Answer time limit | `turn_time_limit` | Limit | 45 minutes |  | ✓ | ✓ |  |  |  |  |
| Unattended run time limit | `unattended_time_limit` | Limit | 45 minutes |  | ✓ | ✓ |  |  |  |  |
| New threads per reply | `outbound_threads` | Limit | 2 threads | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Places per reply | `outbound_places` | Limit | 2 places | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| People tagged per reply | `outbound_people` | Limit | 5 people | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Post length elsewhere | `outbound_length` | Limit | 1500 characters | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Daily spend per model | `daily_model_cap` | Limit | $20 | ✓ | ✓ | ✓ |  |  |  |  |
| Approvals | `approvals` | Guardrail | Ask |  | ✓ | ✓ | ✓ | ✓ |  |  |
| Read-only memory in direct messages | `dm_memory_read_only` | Guardrail | Writable | ✓ | ✓ |  |  |  |  |  |
| Approvals in direct messages | `dm_approvals` | Guardrail | Off | ✓ | ✓ |  |  |  |  |  |
| Rooms with outside people | `audience_hold` | Guardrail | Off | ✓ | ✓ |  |  |  |  | Only Discord |
| Tool safety | `tool_safety` | Guardrail | On | ✓ |  |  |  |  |  |  |
| Approve cards for OpenAI and Gemini | `host_call_gate` | Guardrail | On | ✓ |  |  |  |  |  | Only Discord, Slack, Teams, Web |
| Connectors on OpenAI and Gemini | `mcp_connector_relay` | Guardrail | Off | ✓ |  |  |  |  |  | Only Discord, Slack, Teams, Web |
| Reach check | `reach_budget` | Guardrail | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |  |
| Built-in tools in routines | `unattended_capabilities` | Guardrail | Allowed | ✓ | ✓ | ✓ |  |  |  |  |
| Sandbox tools in routines | `routine_sandbox_tools` | Guardrail | Off |  | ✓ | ✓ |  |  | ✓ |  |
| Approval for watches | `watch_approval` | Guardrail | Ask |  | ✓ | ✓ |  |  |  |  |
| Approval for cloud computers | `compute_approval` | Guardrail | Ask |  | ✓ | ✓ |  |  |  |  |
| Cloud computers continue after a job | `compute_wake_runs` | Guardrail | Off |  | ✓ | ✓ |  |  |  |  |
| Routines must say where they post | `require_routine_destination` | Guardrail | Optional | ✓ | ✓ |  |  |  |  |  |
| Monitor direct messages | `monitor_direct_messages` | Guardrail | Off | ✓ | ✓ | ✓ |  |  |  |  |
| Monitor coding tools | `monitor_mcp` | Guardrail | Off | ✓ | ✓ | ✓ |  |  |  |  |
| Follow the GitHub org | `github_follow_org` | Guardrail | Off |  |  | ✓ |  |  |  |  |

## Approvals

Ask before an agent writes to an outside service in a chat reply. Allow writes covers chat replies only, not routines or watches. A coordinator's value decides its callees' writes. Only a chat admin or the operator can change it.

| | |
| --- | --- |
| Key | `approvals` |
| Values | `on` (Ask), `off` (Allow writes) |
| Default | `on` |
| Levels | Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Thread participation

The agent joins a thread without being mentioned when it can help. A channel or thread can turn it on where it is off above, unless that is locked.

| | |
| --- | --- |
| Key | `participation` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_THREAD_PARTICIPATION__MODE` |

## Progress plans

The agent keeps a short plan of a longer task's steps on the working card, with one-line updates on what it found and does next.

| | |
| --- | --- |
| Key | `progress_plans` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_PROGRESS_PLANS__MODE` |

## Cost footer

Finished answers end with a footer naming the agent, the time taken, what the answer cost and the money left.

| | |
| --- | --- |
| Key | `cost_footer` |
| Values | `shown` (Shown), `hidden` (Hidden) |
| Default | `shown` |
| Levels | Workspace, Channel, Thread |
| Combines | Most specific wins |

## Repository memory

Each message starts with the notes in the agent's repository memory, and the agent gets the memory tools. Off hides both here. Whoever runs Daimon must set it up first.

| | |
| --- | --- |
| Key | `repo_memory` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Data sources

The data store, with its connected sources and search. The operator turns it on; a team can turn it off.

| | |
| --- | --- |
| Key | `data_sources` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Context provider

A context provider set up for this channel or agent adds what it knows to each message. Off skips it here. Claude agents only.

| | |
| --- | --- |
| Key | `context_provider` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Chat history

How many earlier chat messages a new conversation reads, up to 100 from the thread and from the channel around it. The smallest setting wins.

| | |
| --- | --- |
| Key | `history_depth` |
| Values | `100` (100 messages), `50` (50 messages), `25` (25 messages), `10` (10 messages), `0` (None) |
| Default | `100` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |

## Direct message memory

Earlier direct messages each reply carries; more costs more. The smallest wins.

| | |
| --- | --- |
| Key | `dm_history_depth` |
| Values | `12` (12 messages), `25` (25 messages), `50` (50 messages), `100` (100 messages) |
| Default | `12` |
| Levels | Deployment, Workspace, Agent |
| Combines | Lowest wins |

## Channel background

How many recent channel messages a new thread reads for background when a message in the channel starts it. Chat history still caps it.

| | |
| --- | --- |
| Key | `channel_history` |
| Values | `100` (100 messages), `50` (50 messages), `25` (25 messages), `10` (10 messages), `0` (None) |
| Default | `25` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Answer messages

The most messages one answer may post in a thread. A longer answer is attached as a single file instead.

| | |
| --- | --- |
| Key | `answer_messages` |
| Values | `1000` (1000 messages), `100` (100 messages), `25` (25 messages), `10` (10 messages), `5` (5 messages), `3` (3 messages), `1` (1 message) |
| Default | `5` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Setup guidance

Instructions at the top of the agent's prompt on where its keys, repo, memory and files are and what it may share. Off leaves the agent's own prompt and the safety rules. Applies the next time the agent is saved.

| | |
| --- | --- |
| Key | `guidance` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent |
| Combines | Most specific wins |

## Run commands

The agent can run commands. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.bash` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Read files

The agent can read files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.read` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Edit files

The agent can edit files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.edit` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Write files

The agent can write new files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.write` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Search file contents

The agent can search inside files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.grep` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Find files

The agent can find files by name. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.glob` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Open web pages

The agent can open web pages. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message.

| | |
| --- | --- |
| Key | `tool.web_fetch` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Search the web

The agent can search the web. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message. Each search is billed at the provider's list price, past any free allowance.

| | |
| --- | --- |
| Key | `tool.web_search` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Web app

The agent knows about the web app: messages, routine runs and tool results carry its address and the pages it may link to, its invite and access tools are offered, and Daimon's cards show buttons into it. Off leaves it out.

| | |
| --- | --- |
| Key | `web_awareness` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Connected server briefing

Before each chat message, the agent's connected servers that offer a context tool see the message, and their answers go in front of it as quoted text. Claude agents only.

| | |
| --- | --- |
| Key | `mcp_briefing` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |
| Deprecated | `DAIMON_MCP_CONTEXT__ENABLED` |

## Notes on non-chat messages

A short note in front of a message that did not come from chat, such as a routine, a handoff or another agent's request. Off sends the message alone.

| | |
| --- | --- |
| Key | `origin_fragments` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Keys file

The agent's saved keys as a .env file in its working files. Off leaves the file out; the keys stay saved. Applies from the next message. Claude agents only.

| | |
| --- | --- |
| Key | `agent_env_file` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Platform prompt

The agent's prompt is adjusted to the chat platform's words and formatting. Off sends the agent's own prompt as written. A conversation keeps what it started with until /new. OpenAI agents always get their own prompt.

| | |
| --- | --- |
| Key | `platform_prompt` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Platform skills

The skills Daimon adds for the chat app, such as its formatting. They need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new. Claude agents only.

| | |
| --- | --- |
| Key | `platform_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Agent skills

The skills listed on the agent. Off leaves them out of new conversations; the agent keeps its list. Skills need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `agent_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Channel skills

The skills added to a channel. They need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new.

| | |
| --- | --- |
| Key | `channel_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Personal memory

Each person's own memory with the agent, in conversations only they can read. On never adds it anywhere else. A conversation keeps what it started with until /new.

| | |
| --- | --- |
| Key | `memory_personal` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Shared memory

The agent's memory shared by everyone who talks to it. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `memory_shared` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Channel memory

The memory a restricted channel keeps for itself in place of the shared one. Off leaves such a channel with no memory at all. A conversation keeps what it started with until /new.

| | |
| --- | --- |
| Key | `memory_channel` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Thread titles

A thread Daimon starts is titled from its opening message by a short model call.

| | |
| --- | --- |
| Key | `thread_naming` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Channel |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_THREAD_NAMING__ENABLED` |

## Display name

The name people see for an agent: up to 40 plain characters, never another agent's.

| | |
| --- | --- |
| Key | `display_name` |
| Values | `id` (The agent's id), or any name up to 40 characters |
| Default | `id` |
| Levels | Agent |
| Combines | Most specific wins |

## Thread title style

Sentence case up to 50 characters, or Title Case up to 100, for a thread opened to answer a mention.

| | |
| --- | --- |
| Key | `thread_title_style` |
| Values | `sentence` (Sentence case), `title` (Title Case) |
| Default | `sentence` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Most specific wins |

## Thread archive time

How long an idle thread opened to answer a mention stays open.

| | |
| --- | --- |
| Key | `thread_auto_archive` |
| Values | `10080` (1 week), `1440` (1 day) |
| Default | `10080` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Most specific wins |

## Status reactions

⏳ while the agent works, then ✅ first however it ended, and 📨 instead of a notice on a queued message. Off shows 👀, and ✅ only with Completion pings.

| | |
| --- | --- |
| Key | `status_reactions` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Most specific wins |

## Lock reaction

A 🔒 on a mention the agent may not answer, besides the direct message saying why.

| | |
| --- | --- |
| Key | `protected_notice_reaction` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Most specific wins |

## Text before tool calls

Text written before a tool call is posted as its own message. Off keeps it on the working card.

| | |
| --- | --- |
| Key | `pre_tool_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Working card timer

The card shows Working or Thinking with the time, and the model in its footer.

| | |
| --- | --- |
| Key | `card_timer` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Answer bots

Answer a bot that mentions the agent, under people's access rules; never itself.

| | |
| --- | --- |
| Key | `answer_bots` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Most specific wins |

## Typed mentions

A message with "@" and the agent's name typed as plain text is answered like a real mention. Direct messages and bots are unchanged.

| | |
| --- | --- |
| Key | `plain_text_mentions` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Channel |
| Combines | Most specific wins |

## Completion pings

The person who asked is pinged when a long answer is finished.

| | |
| --- | --- |
| Key | `completion_pings` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_COMPLETION_PINGS` |

## Completion pings in direct messages

Direct messages follow Completion pings.

| | |
| --- | --- |
| Key | `dm_completion_pings` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Most specific wins |

## Model reactions

The first message in a thread, and the first after the model or effort changes, gets an emoji for the model and one for the effort it runs on.

| | |
| --- | --- |
| Key | `model_reactions` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace, Agent |
| Combines | Most specific wins |

## Routine result header

Off drops the "Result from" line above a routine's result.

| | |
| --- | --- |
| Key | `routine_result_header` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Routine |
| Combines | Most specific wins |

## Tool server names

Tool lines on the working card name the connected server each tool belongs to.

| | |
| --- | --- |
| Key | `tool_server_names` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Workspace, Agent |
| Combines | Most specific wins |

## Table images

Tables in an answer are drawn as an image or a native table instead of text.

| | |
| --- | --- |
| Key | `table_rendering` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TABLE_RENDERING` |

## Budget notices

Admins are told in a direct message when a channel's budget is nearly used or spent.

| | |
| --- | --- |
| Key | `budget_notices` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_BUDGET_NOTICES` |

## Vote buttons

Finished answers get thumbs up and down for feedback.

| | |
| --- | --- |
| Key | `message_feedback` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Private channel messages

Every message in private and shared channels is read through Microsoft Graph, not only the ones that mention the agent. Needs the public address the bot is reached at. Whoever runs Daimon sets it, and it applies after a restart.

| | |
| --- | --- |
| Key | `private_channel_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED` |

## Direct messages

People may message the agent directly; on for the team, every agent takes part unless it opts out. Who may message which agent is set apart from this.

| | |
| --- | --- |
| Key | `direct_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Off above is a ceiling |

## Direct message wait

Holds a direct message for more, so a burst gets one reply; at most five seconds, and never an approval reply.

| | |
| --- | --- |
| Key | `dm_debounce_seconds` |
| Values | `0` (Off), `1` (1 second), `3` (3 seconds), `5` (5 seconds) |
| Default | `0` |
| Levels | Deployment, Workspace |
| Combines | Most specific wins |

## Read-only memory in direct messages

A reply in a direct message can read the agent's memory but not change it.

| | |
| --- | --- |
| Key | `dm_memory_read_only` |
| Values | `off` (Writable), `on` (Read-only) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Stricter wins |

## Approvals in direct messages

Direct messages follow the agent's Approvals.

| | |
| --- | --- |
| Key | `dm_approvals` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## /new in threads

/new works in threads Daimon started.

| | |
| --- | --- |
| Key | `thread_new` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Rooms with outside people

Report only changes nothing.

| | |
| --- | --- |
| Key | `audience_hold` |
| Values | `off` (Off), `shadow` (Report only), `on` (On) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Stricter wins |

## Web app: chat

The chat pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.chat` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Web app: agents

The agents pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.agents` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Web app: channels

The channels pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.channels` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Web app: routines

The routines pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.routines` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Web app: credits

The credits pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.credits` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Web app: integrations

The integrations pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.integrations` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Watches

The agent can watch a page or feed and start a conversation when it changes. Off removes the watch tools here and stops the watches made here. Watches only run once whoever runs Daimon allows them, whatever this says.

| | |
| --- | --- |
| Key | `watches` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_WATCHES__ENABLED` |

## Routines

The agent can set up and run routines: tasks that run on a schedule. Off removes the routine tools here.

| | |
| --- | --- |
| Key | `routines` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Long jobs

The agent can start a long job that keeps working after the chat moves on. Off until a server admin turns it on; off removes the long-job tools here.

| | |
| --- | --- |
| Key | `long_jobs` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Secret sharing

/share-secret sends a secret to one person's DMs; the agent sends its keys to an admin, its owner or its setter. They reveal it once, within a day. A server admin turns it on.

| | |
| --- | --- |
| Key | `secret_sharing` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Off above is a ceiling |

## Wait for bot access

Work for a channel Daimon cannot reach waits, without failing, and runs once it can.

| | |
| --- | --- |
| Key | `bot_access_wait` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace |
| Combines | Off above is a ceiling |

## Cloud computers

The agent can start a short-lived cloud computer to run code and keep files. Only a server admin turns it on; Off removes its tools here.

| | |
| --- | --- |
| Key | `compute` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Notebooks

The agent can publish and manage notebooks. Off removes the notebook tools here.

| | |
| --- | --- |
| Key | `notebooks` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Report publishing

The agent can publish a report as a web page and delete it later. Off removes the report tools here.

| | |
| --- | --- |
| Key | `report_publish` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Video transcripts

The agent can fetch the transcript of a video. Off removes that tool here.

| | |
| --- | --- |
| Key | `media` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Microsoft 365

People can connect a Microsoft account and the agent can search, read, write and send with it. Off removes the Microsoft tools here.

| | |
| --- | --- |
| Key | `microsoft_365` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Other providers per conversation

Anyone who can change a conversation's model may pick an OpenAI or Gemini model for it. Off, only the agent's owner or an admin can.

| | |
| --- | --- |
| Key | `thread_providers` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Off above is a ceiling |

## Ask a person

Answers offer a way to ask a person for help, posted in the support channel. Off removes the button and the form. The operator sets the default channel and credits; an admin sets limits, a menu and their own channel.

| | |
| --- | --- |
| Key | `support_escalation` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Off above is a ceiling |

## Feedback to support

A submitted thumbs-down form (reasons, text, person, agent and a link to the answer) is also posted where Ask a person requests go, and the form says so first. It spends no credits. Off keeps the form in the database only.

| | |
| --- | --- |
| Key | `support_feedback` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` |

## Own support channel

An admin may send Ask a person requests to a channel of their own instead of the default one; whoever runs Daimon then no longer sees them. Off sends every request to the default channel.

| | |
| --- | --- |
| Key | `support_own_channel` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment |
| Combines | Off above is a ceiling |

## GitHub access

The agent can ask for access to GitHub repositories, connect an account and pick its working repository. Off removes those tools here.

| | |
| --- | --- |
| Key | `github` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Lead intake

The agent can take in sales leads and look them up. Off removes the lead tools here.

| | |
| --- | --- |
| Key | `lead_intake` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## Data source reads

The agent can search the workspace's data sources, showing only what everyone who will see the answer may read. It then starts fresh for each message in a channel, keeps no memory, and other agents cannot ask it. Not in rooms people outside the team can read.

| | |
| --- | --- |
| Key | `data_reads` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace, Agent, Channel |
| Combines | Off above is a ceiling |

## Library skills

Coding keys, the hub and outside clients can read the skill library, by scope. Chat agents keep their own skill tools.

| | |
| --- | --- |
| Key | `library_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace |
| Combines | Off above is a ceiling |

## Long job dollar cap

The most one long job may spend. The smallest setting wins.

| | |
| --- | --- |
| Key | `long_jobs_max_usd` |
| Values | `20` ($20), `10` ($10), `5` ($5), `2` ($2), `1` ($1) |
| Default | `5` |
| Levels | Deployment, Workspace, Agent |
| Combines | Lowest wins |
| Deprecated | `DAIMON_LONG_JOBS__MAX_USD` |

## Long job time limit

The longest one long job may run. The smallest setting wins.

| | |
| --- | --- |
| Key | `long_jobs_max_deadline` |
| Values | `70` (70 minutes), `60` (60 minutes), `30` (30 minutes), `15` (15 minutes) |
| Default | `70` |
| Levels | Deployment, Workspace, Agent |
| Combines | Lowest wins |
| Deprecated | `DAIMON_LONG_JOBS__MAX_DEADLINE_MINUTES` |

## Callback watch lifetime

How long a callback watch lasts, new or moved in, before it must be renewed. The smallest setting wins.

| | |
| --- | --- |
| Key | `callback_watch_lifetime` |
| Values | `0` (Permanent), `90` (90 days), `30` (30 days), `7` (7 days) |
| Default | `30` |
| Levels | Deployment, Workspace |
| Combines | Lowest wins |

## Answer time limit

The longest an answer may take before it is stopped when someone is talking to the agent: the standard 45 minutes, or shorter. The smallest setting wins. Routines and watches use the unattended run limit.

| | |
| --- | --- |
| Key | `turn_time_limit` |
| Values | `45` (45 minutes), `30` (30 minutes), `20` (20 minutes), `15` (15 minutes) |
| Default | `45` |
| Levels | Workspace, Agent |
| Combines | Lowest wins |

## Unattended run time limit

The longest a run nobody is watching may take before it is stopped: a routine, a watch wake-up, an agent-to-agent chat or a command-line run. The standard 45 minutes, or shorter. The smallest setting wins. Chats use the answer time limit.

| | |
| --- | --- |
| Key | `unattended_time_limit` |
| Values | `45` (45 minutes), `30` (30 minutes), `20` (20 minutes), `15` (15 minutes) |
| Default | `45` |
| Levels | Workspace, Agent |
| Combines | Lowest wins |

## Posting to others

The agent can post where other people are: other channels, new threads and direct messages. Off keeps it to the conversation it is in.

| | |
| --- | --- |
| Key | `outbound_reach` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Off above is a ceiling |

## New threads per reply

How many new threads or forum posts outside its own channel, or tagging someone else, one reply may open without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_threads` |
| Values | `2` (2 threads), `1` (1 thread), `0` (None) |
| Default | `2` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_THREADS` |

## Places per reply

How many channels, threads and direct-message recipients outside its own conversation one reply may post into without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_places` |
| Values | `2` (2 places), `1` (1 place), `0` (None) |
| Default | `2` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_PLACES` |

## People tagged per reply

How many different people one reply may tag without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_people` |
| Values | `5` (5 people), `3` (3 people), `1` (1 person), `0` (None) |
| Default | `5` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_PEOPLE` |

## Post length elsewhere

The longest post, in characters, a reply may send outside its own conversation. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_length` |
| Values | `1500` (1500 characters), `1000` (1000 characters), `500` (500 characters), `250` (250 characters) |
| Default | `1500` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_LENGTH` |

## Daily spend per model

The most the team may spend on one OpenAI or Gemini model in a day, at list price. The smallest setting wins, and the provider cap set by whoever runs Daimon still applies.

| | |
| --- | --- |
| Key | `daily_model_cap` |
| Values | `20` ($20), `10` ($10), `5` ($5), `2` ($2), `1` ($1) |
| Default | `20` |
| Levels | Deployment, Workspace, Agent |
| Combines | Lowest wins |

## Tool safety

A change through a connected tool waits for the person to press Approve. In a routine or other run nobody watches, it is refused unless whoever runs Daimon allowed that tool. Reads always run.

| | |
| --- | --- |
| Key | `tool_safety` |
| Values | `off` (Off), `on` (On) |
| Default | `on` |
| Levels | Deployment |
| Combines | Stricter wins |
| Deprecated | `DAIMON_TOOL_SAFETY__ENABLED` |

## Approve cards for OpenAI and Gemini

On an OpenAI or Gemini agent, a tool that needs approval posts an Approve card and runs once the person presses it. Off refuses those tools there.

| | |
| --- | --- |
| Key | `host_call_gate` |
| Values | `off` (Off), `on` (On) |
| Default | `on` |
| Levels | Deployment |
| Combines | Stricter wins |

## Connectors on OpenAI and Gemini

An agent's own connectors work on OpenAI and Gemini agents with the person's sign-in, and that provider receives what they return. Off leaves them out.

| | |
| --- | --- |
| Key | `mcp_connector_relay` |
| Values | `off` (Off), `on` (On) |
| Default | `off` |
| Levels | Deployment |
| Combines | Stricter wins |

## Reach check

Before one reply posts in more places, opens more threads or tags more people than the limits allow, the person approves it on a card. Where no card can be shown, the post is refused.

| | |
| --- | --- |
| Key | `reach_budget` |
| Values | `off` (Off), `on` (On) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Stricter wins |
| Deprecated | `DAIMON_OUTBOUND__REACH_BUDGET_ENABLED` |

## Built-in tools in routines

Allowed lets an admin turn on built-in tools for an agent's or one routine's runs. Off keeps them off in every routine, even one an admin turned them on for.

| | |
| --- | --- |
| Key | `unattended_capabilities` |
| Values | `on` (Allowed), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent |
| Combines | Stricter wins |

## Sandbox tools in routines

On gives an agent's or one routine's runs its built-in tools, for the agent and environment it was set with. A routine's value beats its agent's; Built-in tools in routines set to Off still wins. Only a server admin can change it.

| | |
| --- | --- |
| Key | `routine_sandbox_tools` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace, Agent, Routine |
| Combines | Most specific wins |

## Approval for watches

Ask shows a card with the destination and duration before a watch is made or resumed. Skip makes it without the card, in the same thread for the default duration. Editing a watch, long jobs and key rotation keep asking. Only a server admin can change it.

| | |
| --- | --- |
| Key | `watch_approval` |
| Values | `skip` (Skip the card), `ask` (Ask) |
| Default | `ask` |
| Levels | Workspace, Agent |
| Combines | Most specific wins |

## Approval for cloud computers

Ask shows a card before a cloud computer starts or gets more time. Skip runs both without it, for the default time only. OpenAI and Gemini agents still get the card. Only a server admin can change it.

| | |
| --- | --- |
| Key | `compute_approval` |
| Values | `skip` (Skip the card), `ask` (Ask) |
| Default | `ask` |
| Levels | Workspace, Agent |
| Combines | Most specific wins |

## Cloud computers continue after a job

When a background command ends with nobody in the conversation, the agent may run the next command on that cloud computer. After a set number in a row it waits for the owner to run a command or write a file there; a message does not count. Off: it only reports the result. Only a server admin can change it.

| | |
| --- | --- |
| Key | `compute_wake_runs` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Workspace, Agent |
| Combines | Most specific wins |

## Routines must say where they post

A routine the agent makes must name where its results go, or it is not made. Existing routines are not changed.

| | |
| --- | --- |
| Key | `require_routine_destination` |
| Values | `off` (Optional), `on` (Required) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Stricter wins |
| Deprecated | `DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION` |

## Monitor direct messages

Keeps what people ask this agent in direct messages and web chat, and what it answers, for admins to read. People are told in the conversation. Kept from when it is turned on, for the retention period.

| | |
| --- | --- |
| Key | `monitor_direct_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Off above is a ceiling |

## Monitor coding tools

Keeps what people ask this agent from their coding tools and what it answers, plus the name, time and outcome of their other tool calls, never what they sent or returned. People are told in the conversation or their first tool result. Kept from when it is turned on, for the retention period.

| | |
| --- | --- |
| Key | `monitor_mcp` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent |
| Combines | Off above is a ceiling |

## Follow the GitHub org

Follows the whole org.

| | |
| --- | --- |
| Key | `github_follow_org` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Agent |
| Combines | Off above is a ceiling |
