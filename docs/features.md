# Features

Every feature is set at one or more levels: deployment, server, agent, channel and
thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above. Settings show which level decided.

An admin can lock a value at the deployment, server, agent or channel level. Levels
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool, the web app or `daimon features`.

## Approvals

Ask before an agent writes to a third-party service. Decided most specific first: only a native platform admin, checked live, may change it, and it applies only once the operator enables approval overrides.

| | |
| --- | --- |
| Key | `approvals` |
| Values | `on` (Ask), `off` (Allow writes) |
| Default | `on` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

## Thread participation

Reply in a thread without being mentioned when a short classifier call says the agent can help. A lower tier may turn it on under a higher Off, as before the registry; a Lock stops that.

| | |
| --- | --- |
| Key | `participation` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_THREAD_PARTICIPATION__MODE` |

## Progress plans

The agent keeps a short plan of a longer task's steps on the working card, with one-line updates on what it found and does next.

| | |
| --- | --- |
| Key | `progress_plans` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_PROGRESS_PLANS__MODE` |

## Cost footer

Finished answers end with a footer naming the agent, the time taken, what the answer cost and the money left.

| | |
| --- | --- |
| Key | `cost_footer` |
| Values | `shown` (Shown), `hidden` (Hidden) |
| Default | `shown` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

## Repository memory

Each message starts with the notes the agent keeps in its repository memory. The operator turns it on, with the memory tools; below that it can only be turned off, which also switches the memory tools off.

| | |
| --- | --- |
| Key | `repo_memory` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_REPO_MEMORY__ENABLED` |

## Context provider

A context provider set up for this channel or agent adds what it knows to each message. Off skips it here. Managed Agents only.

| | |
| --- | --- |
| Key | `context_provider` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
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
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |

## Setup guidance

Instructions at the top of the agent's prompt on where its keys, repo, memory and files are and what it may share. Off leaves the agent's own prompt and the safety rules. Applies when the agent is next saved or synced.

| | |
| --- | --- |
| Key | `guidance` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Daimon tools

Daimon's own tools: chat, settings, routines, memory and the rest. On keeps at least the conversation ids the tools need. An isolated agent never has them. Gemini and OpenAI agents always have them, so it can't be changed there. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `daimon_tools` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |

## Built-in tool: bash

The agent's built-in bash tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.bash` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: read

The agent's built-in read tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.read` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: edit

The agent's built-in edit tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.edit` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: write

The agent's built-in write tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.write` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: grep

The agent's built-in grep tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.grep` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: glob

The agent's built-in glob tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.glob` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: web fetch

The agent's built-in web fetch tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.web_fetch` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: web search

The agent's built-in web search tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.web_search` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Message controls

Facts sent with each message: where it was sent, who is answering, and how to use them. Origin only keeps the ids and the honesty facts; Minimal keeps only the facts about the model and the conversation. Daimon tools keep it at Origin only or more.

| | |
| --- | --- |
| Key | `turn_controls` |
| Values | `full` (Full), `origin` (Origin only), `minimal` (Minimal) |
| Default | `full` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Web app links

Each message tells the agent the web app's address and the pages it may link to.

| | |
| --- | --- |
| Key | `web_awareness` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |

## Connected server briefing

Before each chat message, the agent's connected servers that offer a context tool are sent the message, and their answers go in front of it as quoted data. Managed Agents only.

| | |
| --- | --- |
| Key | `mcp_briefing` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |
| Deprecated | `DAIMON_MCP_CONTEXT__ENABLED` |

## Run instructions

A short note in front of a message that did not come from chat: a routine, a relayed draft, a handoff or another agent's request, with any wording the agent's spec adds. Off sends the message alone.

| | |
| --- | --- |
| Key | `origin_fragments` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Agent |
| Combines | Most specific wins |

## Keys file

The agent's saved keys as a .env file in its working files. Off leaves the file out; the keys stay saved. Applies from the next message in a conversation. Managed Agents only.

| | |
| --- | --- |
| Key | `agent_env_file` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Agent |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Carry working files

When a long conversation starts over, the agent is first asked to save its working files to carry across. Off carries only the conversation and skips that extra paid step. Managed Agents only.

| | |
| --- | --- |
| Key | `checkpoint_prompt` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
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
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Platform skills

The skills Daimon adds for the chat platform, such as its formatting. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `platform_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
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
| Levels | Deployment, Server, Agent, Channel |
| Combines | Most specific wins |

## Channel skills

The skills added to a channel. They need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `channel_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Server, Agent, Channel |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Personal memory

Each person's own memory with the agent, in conversations only they can read. On never adds it anywhere else. A conversation keeps what it started with until /new. Off for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `memory_personal` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Shared memory

The agent's memory shared by everyone who talks to it. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `memory_shared` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Channel memory

The memory a restricted channel keeps for itself in place of the shared one. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `memory_channel` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Server, Agent, Channel |
| Combines | Off above is a ceiling |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |
