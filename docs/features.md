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

## All features

Where each feature can be set, and its default.

| Feature | Key | Kind | Default | Deployment | Workspace | Agent | Channel | Thread | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Chat history | `history_depth` | Model input | 100 messages | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Setup guidance | `guidance` | Model input | On | ✓ | ✓ | ✓ |  |  |  |
| Daimon tools | `daimon_tools` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Always On on Gemini, OpenAI |
| Run commands | `tool.bash` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Read files | `tool.read` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Edit files | `tool.edit` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Write files | `tool.write` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Search file contents | `tool.grep` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Find files | `tool.glob` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Open web pages | `tool.web_fetch` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Search the web | `tool.web_search` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Up to Agent on Gemini, OpenAI |
| Message controls | `turn_controls` | Model input | Full | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Web app links | `web_awareness` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Connected server briefing | `mcp_briefing` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Notes on non-chat messages | `origin_fragments` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Keys file | `agent_env_file` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Carry working files | `checkpoint_prompt` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Platform prompt | `platform_prompt` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Always Off on OpenAI |
| Platform skills | `platform_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Agent skills | `agent_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Always On on Gemini, OpenAI |
| Channel skills | `channel_skills` | Model input | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Thread participation | `participation` | Chat | Off | ✓ | ✓ |  | ✓ | ✓ | Only Discord, Teams |
| Progress plans | `progress_plans` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Cost footer | `cost_footer` | Chat | Shown |  | ✓ |  | ✓ | ✓ |  |
| Thread titles | `thread_naming` | Chat | On | ✓ | ✓ |  | ✓ |  | Only Discord |
| Typed mentions | `plain_text_mentions` | Chat | Off | ✓ | ✓ |  | ✓ |  |  |
| Completion pings | `completion_pings` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Table images | `table_rendering` | Chat | On | ✓ | ✓ | ✓ | ✓ | ✓ | Only Discord, Slack; Off by default on Slack |
| Budget notices | `budget_notices` | Chat | On | ✓ | ✓ |  | ✓ |  |  |
| Separate agent messages | `status_log` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Cost on Gemini and OpenAI footers | `native_footer_cost` | Chat | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Vote buttons | `message_feedback` | Chat | On | ✓ | ✓ | ✓ | ✓ | ✓ | Only Discord |
| Reply hint | `unmentioned_reply_hint` | Chat | Off | ✓ | ✓ |  | ✓ |  | Only Discord |
| No-writers notice | `writers_none_notice` | Chat | Off | ✓ | ✓ |  | ✓ |  | Only Discord |
| Waiting card | `waiting_card` | Chat | Off | ✓ | ✓ |  |  |  | Only Discord |
| Other providers per conversation | `thread_providers` | Chat | Off | ✓ | ✓ | ✓ |  |  |  |
| Repository memory | `repo_memory` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Context provider | `context_provider` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Not on Gemini, OpenAI |
| Personal memory | `memory_personal` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Shared memory | `memory_shared` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon; Always On on Gemini, OpenAI |
| Channel memory | `memory_channel` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Private channel messages | `private_channel_messages` | Capability | On | ✓ |  |  |  |  | Needs setup by whoever runs Daimon; Only Teams |
| Direct messages | `direct_messages` | Capability | Off | ✓ | ✓ |  |  |  | Only Discord, Slack |
| Web app: chat | `web.chat` | Capability | On | ✓ | ✓ |  |  |  |  |
| Web app: agents | `web.agents` | Capability | On | ✓ | ✓ |  |  |  |  |
| Web app: channels | `web.channels` | Capability | On | ✓ | ✓ |  |  |  |  |
| Web app: routines | `web.routines` | Capability | On | ✓ | ✓ |  |  |  |  |
| Web app: credits | `web.credits` | Capability | On | ✓ | ✓ |  |  |  |  |
| Web app: integrations | `web.integrations` | Capability | On | ✓ | ✓ |  |  |  |  |
| Watches | `watches` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Routines | `routines` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Long jobs | `long_jobs` | Capability | Off | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Cloud computers | `compute` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Notebooks | `notebooks` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Report publishing | `report_publish` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Video transcripts | `media` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Microsoft 365 | `microsoft_365` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ | Needs setup by whoever runs Daimon |
| Chart images in answers | `artifact_images` | Capability | On | ✓ | ✓ |  |  |  |  |
| Ask a person | `support_escalation` | Capability | On | ✓ | ✓ |  |  |  | Needs setup by whoever runs Daimon |
| GitHub access | `github` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Lead intake | `lead_intake` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Posting to others | `outbound_reach` | Capability | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Long job dollar cap | `long_jobs_max_usd` | Limit | $5 | ✓ | ✓ | ✓ |  |  |  |
| Long job time limit | `long_jobs_max_deadline` | Limit | 70 minutes | ✓ | ✓ | ✓ |  |  |  |
| New threads per reply | `outbound_threads` | Limit | 2 threads | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Places per reply | `outbound_places` | Limit | 2 places | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| People tagged per reply | `outbound_people` | Limit | 5 people | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Post length elsewhere | `outbound_length` | Limit | 1500 characters | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Daily spend per model | `daily_model_cap` | Limit | $20 | ✓ | ✓ | ✓ |  |  |  |
| Approvals | `approvals` | Guardrail | Ask |  | ✓ |  | ✓ | ✓ |  |
| Read-only memory in direct messages | `dm_memory_read_only` | Guardrail | Writable | ✓ | ✓ |  |  |  | Only Discord, Slack |
| Tool safety | `tool_safety` | Guardrail | On | ✓ |  |  |  |  |  |
| Approve cards for OpenAI and Gemini | `host_call_gate` | Guardrail | On | ✓ |  |  |  |  | Only Discord, Slack, Teams, Web |
| Connectors on OpenAI and Gemini | `mcp_connector_relay` | Guardrail | On | ✓ |  |  |  |  | Only Discord, Slack, Teams, Web |
| Reach check | `reach_budget` | Guardrail | On | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| Built-in tools in routines | `unattended_capabilities` | Guardrail | Allowed | ✓ | ✓ | ✓ |  |  |  |
| Routines must say where they post | `require_routine_destination` | Guardrail | Optional | ✓ | ✓ |  |  |  |  |

## Approvals

Ask before an agent writes to an outside service. Only an admin of the chat app can change it, once whoever runs Daimon allows approval changes.

| | |
| --- | --- |
| Key | `approvals` |
| Values | `on` (Ask), `off` (Allow writes) |
| Default | `on` |
| Levels | Workspace, Channel, Thread |
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

How many earlier chat messages a new conversation reads, up to 100 from the thread and 25 from the channel around it. The smallest setting wins.

| | |
| --- | --- |
| Key | `history_depth` |
| Values | `100` (100 messages), `50` (50 messages), `25` (25 messages), `10` (10 messages), `0` (None) |
| Default | `100` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Lowest wins |

## Setup guidance

Instructions at the top of the agent's prompt on where its keys, repo, memory and files are and what it may share. Off leaves the agent's own prompt and the safety rules. Applies the next time the agent is saved.

| | |
| --- | --- |
| Key | `guidance` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent |
| Combines | Most specific wins |

## Daimon tools

Daimon's own tools: chat, settings, routines, memory and the rest. An agent kept apart from the team's data never has them, and Gemini and OpenAI agents always do. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `daimon_tools` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Run commands

The agent can run commands. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can read files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can edit files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can write new files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can search inside files. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can find files by name. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can open web pages. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

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

The agent can search the web. Off removes it here; On never gives more than the agent's own tool settings allow. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.web_search` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Workspace, Agent |
| Levels on OpenAI | Deployment, Workspace, Agent |

## Message controls

What each message tells the agent: where it was sent, who is answering and how to act on that. Where and who drops the how-to; Basics keeps only the model and the conversation. Daimon tools need Where and who or more.

| | |
| --- | --- |
| Key | `turn_controls` |
| Values | `full` (Full), `origin` (Where and who), `minimal` (Basics) |
| Default | `full` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Web app links

Each message tells the agent the web app's address and the pages it may link to.

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

The agent's saved keys as a .env file in its working files. Off leaves the file out; the keys stay saved. Applies from the next message in a conversation. Claude agents only.

| | |
| --- | --- |
| Key | `agent_env_file` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Carry working files

When a long conversation starts over, the agent is first asked to save its working files to carry across. Off carries only the conversation and skips that extra paid step. Claude agents only.

| | |
| --- | --- |
| Key | `checkpoint_prompt` |
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

The skills Daimon adds for the chat app, such as its formatting. A conversation keeps what it started with until /new. Claude agents only.

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

The skills listed on the agent. Off leaves them out of new conversations; the agent keeps its list. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `agent_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Channel skills

The skills added to a channel. They need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new. Claude agents only.

| | |
| --- | --- |
| Key | `channel_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

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

The memory a restricted channel keeps for itself in place of the shared one. A conversation keeps what it started with until /new.

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

## Separate agent messages

An answer made of several agent messages keeps each one, set apart by a blank line, instead of joining them into one block.

| | |
| --- | --- |
| Key | `status_log` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TURN__STATUS_LOG` |

## Cost on Gemini and OpenAI footers

A finished answer from a Gemini or OpenAI agent shows what it cost, or that the cost is unknown or partial. Claude agent footers are unchanged.

| | |
| --- | --- |
| Key | `native_footer_cost` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TURN__NATIVE_FOOTER_COST` |

## Vote buttons

Finished answers get thumbs up and down for feedback.

| | |
| --- | --- |
| Key | `message_feedback` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent, Channel, Thread |
| Combines | Most specific wins |

## Reply hint

A short reminder that a reply needs a mention, where an unmentioned message is skipped.

| | |
| --- | --- |
| Key | `unmentioned_reply_hint` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__UNMENTIONED_REPLY_HINT` |

## No-writers notice

Says so when a channel's write access is set to no one and a message is skipped.

| | |
| --- | --- |
| Key | `writers_none_notice` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__WRITERS_NONE_NOTICE` |

## Waiting card

A card shows in a channel whose reply is waiting on the team's limit on replies at once.

| | |
| --- | --- |
| Key | `waiting_card` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__TENANT_CAP_WAITING_CARD` |

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

Conversations may move to a direct message with the agent. The agent that answers there is the team's DM agent, which is chosen apart from this.

| | |
| --- | --- |
| Key | `direct_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |

## Read-only memory in direct messages

A reply in a direct message can read the agent's memory but not change it.

| | |
| --- | --- |
| Key | `dm_memory_read_only` |
| Values | `off` (Writable), `on` (Read-only) |
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

## Cloud computers

The agent can start a short-lived cloud computer to run code and keep files. Off removes the computer tools here.

| | |
| --- | --- |
| Key | `compute` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
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

## Chart images in answers

Charts the agent makes come back inside its answer to a connected app as pictures the app can see. Off sends the links only, for an app that cannot take pictures.

| | |
| --- | --- |
| Key | `artifact_images` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_ARTIFACTS__EMBED_IMAGES` |

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

Answers offer a way to ask the Daimon team for help, which posts the request in the support channel. Off removes the button and the form. Whoever runs Daimon sets the channel and each person's credits.

| | |
| --- | --- |
| Key | `support_escalation` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace |
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

An agent's own connectors work on OpenAI and Gemini agents: Daimon calls them for the agent with the person's sign-in. Off leaves them out there.

| | |
| --- | --- |
| Key | `mcp_connector_relay` |
| Values | `off` (Off), `on` (On) |
| Default | `on` |
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

Allowed lets an admin turn on built-in tools for one routine. Off keeps them off in every routine, even one an admin turned them on for.

| | |
| --- | --- |
| Key | `unattended_capabilities` |
| Values | `on` (Allowed), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Workspace, Agent |
| Combines | Stricter wins |

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
