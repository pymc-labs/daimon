# Features

Every feature is set at one or more levels: deployment, server, agent, channel and
thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above. Settings show which level decided.

An admin can lock a value at the deployment, server, agent or channel level. Levels
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool or the web app.

## Approvals

Ask before an agent writes to a third-party service. Kept in the access policy and decided most specific first, as before the registry: only a native platform admin, checked live, may change it, and it applies only once the operator enables approval overrides.

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

Daimon's own tools: chat, settings, routines, memory and the rest. On keeps at least the conversation ids in the turn controls, which the tools need. An isolated agent never has them, and Gemini and OpenAI agents keep them. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `daimon_tools` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

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

## Turn controls

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
